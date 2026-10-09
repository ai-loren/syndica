"""外部进程的进程组。

关 stdin、宽限、SIGTERM 整组、宽限、SIGKILL 整组。组长退出后组里还可能有孙进程，所以结束看的是
进程组，不是组长的 wait。stderr 一直读到进程结束：超过上限只丢内容，不停止读取——没人读的管道
会把子进程堵死。写出日志之前先过 Redactor，而且整行脱敏之后才截。

`env` 是完整环境，不是在宿主环境上追加。追加会把宿主的密钥交给外部进程。

`ProcessTree` 跟的是进程树而不是进程组：MCP 的 stdio 服务由 SDK 拉起，SDK 收尾只管进程组。

别的 backend 不得 import 这个模块。ACP 与 A2A 各自有一份监管：backend 之间不共享代码。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Self, cast

from syndica.kernel import ConfigError, Redactor
from syndica.kernel.clipping import drop_cut_end

__all__ = ['ExternalProcess', 'ProcessSupervisor']

logger = logging.getLogger(__name__)

_DEFAULT_GRACE_S = 2.0
_DEFAULT_STDERR_LIMIT = 1_048_576
_POLL_S = 0.02
_TREE_POLL_S = 0.25
# 一行 stderr 最多攒这么多字节再脱敏。不换行的输出不能让缓冲无限长；攒满的那一截后面的字节看不见，
# 刀口前那段连续的非空白可能是半截凭证，脱敏器认不出，所以脱敏之后再退到最近的空白。
_LINE_CAP = 65_536


class StderrLines:
    """把 stderr 的字节切成行交给 `emit`：整行先脱敏，再按 `limit` 字节截。

    先截再脱敏会把跨过上限的凭证切成两截，前一截认不出来、原样进日志。所以一行攒齐（或攒满
    `_LINE_CAP`）才脱敏，截的是脱敏后的文本。用满 `limit` 之后的字节直接丢掉，调用方照常读。
    """

    def __init__(
        self,
        redactor: Redactor,
        limit: int,
        *,
        emit: Callable[[str], None],
        capped: Callable[[], None],
        clipped: Callable[[int], None],
    ) -> None:
        self._redactor = redactor
        self._limit = limit
        self._emit = emit
        self._capped = capped
        self._clipped = clipped
        self._kept = 0
        self._line = bytearray()
        self._overlong = False
        self._told_capped = False
        self._told_clipped = False

    def feed(self, chunk: bytes) -> None:
        position = 0
        while position < len(chunk):
            if self._kept >= self._limit:
                self._cap()
                return
            newline = chunk.find(b'\n', position)
            end = len(chunk) if newline < 0 else newline
            if not self._overlong:
                room = _LINE_CAP - len(self._line)
                if end - position > room:
                    self._line += chunk[position : position + room]
                    self._overlong = True
                    self._flush(cut=True)
                else:
                    self._line += chunk[position:end]
            if newline < 0:
                return
            if self._overlong:
                self._overlong = False
            else:
                self._flush(cut=False)
            position = newline + 1

    def close(self) -> None:
        """流结束了：最后那行没有换行，后面也不会再有字节，按完整的一行记。"""
        if self._line:
            self._flush(cut=False)

    def _flush(self, *, cut: bool) -> None:
        raw = bytes(self._line)
        self._line.clear()
        text = self._redactor.redact(raw.decode('utf-8', errors='replace'))
        if cut:
            text = drop_cut_end(text)
        room = self._limit - self._kept
        dropped = len(raw) > room
        if dropped:
            text = text.encode('utf-8')[:room].decode('utf-8', errors='ignore')
            self._kept = self._limit
        else:
            self._kept = min(self._limit, self._kept + len(raw) + 1)
        if text:
            self._emit(text)
        if cut and not self._told_clipped:
            self._told_clipped = True
            self._clipped(_LINE_CAP)
        if dropped:
            self._cap()

    def _cap(self) -> None:
        if not self._told_capped:
            self._told_capped = True
            self._capped()


@dataclass(frozen=True)
class _Row:
    ppid: int
    pgid: int
    zombie: bool
    started: str


def _process_table() -> dict[int, _Row] | None:
    """此刻的进程表；`ps` 拉不起来时交回 None。`started` 是秒级的启动时刻，pid 被复用后对不上。"""
    try:
        listing = subprocess.run(
            [
                'ps',
                '-ax',
                '-o',
                'pid=',
                '-o',
                'ppid=',
                '-o',
                'pgid=',
                '-o',
                'stat=',
                '-o',
                'lstart=',
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    rows: dict[int, _Row] = {}
    for line in listing.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        rows[pid] = _Row(ppid, pgid, parts[3].startswith('Z'), ' '.join(parts[4:]))
    return rows


def _kill(pids: Iterable[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            continue


class ProcessTree:
    """一个进程和它的全部后代，含用 setsid / setpgid 离开了进程组的。

    后代按 ppid 认，而父进程一退出孤儿就挂到 init 名下、父子链断了，所以跑着的时候后台定时走一遍
    进程表，把见过的后代连同启动时刻记下来；发信号只发给启动时刻还对得上的（pid 会被复用）。根进程
    的进程组成员也算：组里还有人时组号不会被复用。两次轮询之间就既离开进程组、又断了父子链的后代
    （两次 fork 的守护化，中间那一代立刻退出）认不出来。根进程退出后后台立刻收掉它留下的后代。
    """

    def __init__(self, root: int, *, grace: float = _DEFAULT_GRACE_S) -> None:
        self._root = root
        self._grace = _grace(grace)
        self._root_started: str | None = None
        self._seen: dict[int, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._told_blind = False

    def start(self) -> None:
        table = _process_table()
        row = None if table is None else table.get(self._root)
        self._root_started = None if row is None else row.started
        self._survivors(table)
        thread = threading.Thread(target=self._watch, name=f'mcp-tree-{self._root}', daemon=True)
        self._thread = thread
        thread.start()

    def record(self) -> None:
        """此刻还连在树上的后代记下来。根进程要退出之前调一次：退出之后父子链就断了。"""
        self._survivors(_process_table())

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    async def reap(self) -> None:
        """停掉后台，还活着的后代 SIGTERM，宽限之后 SIGKILL。被取消时直接 SIGKILL 再抛出。"""
        self.stop()
        try:
            survivors = self._survivors(_process_table())
            if not survivors:
                return
            _kill(survivors, signal.SIGTERM)
            if await self._cleared():
                return
            _kill(self._survivors(_process_table()), signal.SIGKILL)
            if await self._cleared():
                return
            logger.warning(
                '期望: 进程 %s 的后代在 SIGKILL 之后退出。 实际: 还剩 %s。'
                ' 下一步: 用 ps 查这些进程（多半换了用户，收不到这一侧的信号），手动结束它们。',
                self._root,
                sorted(self._survivors(_process_table())),
            )
        except asyncio.CancelledError:
            _kill(self._survivors(_process_table()), signal.SIGKILL)
            raise

    async def _cleared(self) -> bool:
        deadline = time.monotonic() + self._grace
        while self._survivors(_process_table()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(_POLL_S, remaining))
        return True

    def _watch(self) -> None:
        while not self._stop.wait(_TREE_POLL_S):
            table = _process_table()
            self._survivors(table)
            row = None if table is None else table.get(self._root)
            if table is not None and (
                row is None or row.zombie or row.started != self._root_started
            ):
                self._sweep()
                return

    def _sweep(self) -> None:
        survivors = self._survivors(_process_table())
        if not survivors:
            return
        _kill(survivors, signal.SIGTERM)
        deadline = time.monotonic() + self._grace
        while survivors and time.monotonic() < deadline:
            if self._stop.wait(_POLL_S):
                return
            survivors = self._survivors(_process_table())
        if not self._stop.is_set():
            _kill(self._survivors(_process_table()), signal.SIGKILL)

    def _survivors(self, table: dict[int, _Row] | None) -> set[int]:
        if table is None:
            if not self._told_blind:
                self._told_blind = True
                logger.warning(
                    '期望: 能用 ps 读进程表，好在收尾时找到进程 %s 的后代。 实际: ps 拉不起来。'
                    ' 下一步: 把 ps 放进宿主进程的 PATH；在此之前，'
                    '这个进程的后代在关闭之后可能留下。',
                    self._root,
                )
            return set()
        with self._lock:
            root = table.get(self._root)
            trusted = root is not None and root.started == self._root_started
            tree = {
                pid
                for pid, started in self._seen.items()
                if (row := table.get(pid)) is not None and row.started == started
            }
            if trusted:
                tree.add(self._root)
            if root is None or trusted:
                tree.update(pid for pid, row in table.items() if row.pgid == self._root)
            children: dict[int, list[int]] = {}
            for pid, row in table.items():
                children.setdefault(row.ppid, []).append(pid)
            frontier = list(tree)
            while frontier:
                for child in children.get(frontier.pop(), ()):
                    if child not in tree:
                        tree.add(child)
                        frontier.append(child)
            for pid in tree:
                self._seen[pid] = table[pid].started
            spared = {0, 1, os.getpid()}
            return {pid for pid in tree if not table[pid].zombie and pid not in spared}


def _group_members(pgid: int) -> set[int]:
    """此刻还在这个进程组里的 pid。进程表是独立于 wait 的事实：组长变成僵尸之后孙进程仍在。"""
    try:
        listing = subprocess.run(
            ['ps', '-ax', '-o', 'pid=', '-o', 'pgid='],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return set()
    members: set[int] = set()
    for line in listing.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid, group = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if group == pgid:
            members.add(pid)
    return members


class ExternalProcess:
    """一个外部进程、它的进程组和它的进程树（`ProcessTree`，含用 setsid 离开了组的后代）。

    `aclose` 幂等：组已经杀过之后不再发信号，避免组号被复用后误杀。组长退出（崩溃）时也走一遍
    `aclose`，不等宿主来关。
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        redactor: Redactor,
        grace: float = _DEFAULT_GRACE_S,
        stderr_limit: int = _DEFAULT_STDERR_LIMIT,
    ) -> None:
        self._argv = _argv(argv)
        self._env = {key: value for key, value in env.items()}
        self._cwd = cwd
        self._redactor = redactor
        self._grace = _grace(grace)
        self._stderr_limit = _limit(stderr_limit)
        self._process: asyncio.subprocess.Process | None = None
        self._pgid: int | None = None
        self._tree: ProcessTree | None = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._reader: asyncio.Task[None] | None = None
        self._watch: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._process is not None:
            raise ConfigError(
                expected='外部进程只拉起一次',
                received=f'{self._argv[0]!r} 已经在跑（pid {self._pgid}）',
                next_action='要再开一个就再构造一个 ExternalProcess，不要对同一个实例 start 两次',
            )
        try:
            process = await asyncio.create_subprocess_exec(
                *self._argv,
                cwd=self._cwd,
                env=self._env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ConfigError(
                expected=f'能拉起 {self._argv[0]!r}',
                received=f'找不到这个程序：{exc}',
                next_action=(
                    '把 command 的第一项改成可执行文件的路径，或把它的目录放进交给这个进程的 PATH'
                ),
            ) from exc
        except OSError as exc:
            raise ConfigError(
                expected=f'能拉起 {self._argv[0]!r} 并把它放进新的进程组',
                received=f'{type(exc).__name__}: {exc}',
                next_action='确认 cwd 存在、command 是字符串列表而不是交给 shell 的一行',
            ) from exc
        self._process = process
        # start_new_session 之后组长的 pid 就是进程组号。
        self._pgid = process.pid
        self._tree = ProcessTree(process.pid, grace=self._grace)
        self._tree.start()
        self._reader = asyncio.create_task(self._read_stderr(), name='mcp-stderr')
        self._watch = asyncio.create_task(self._watch_leader(), name='mcp-leader')

    async def read_stdout_line(self) -> str:
        process = self._process
        if process is None or process.stdout is None:
            raise ConfigError(
                expected='外部进程已经 start，stdout 还开着',
                received='进程还没拉起，或 stdout 已经关掉',
                next_action='先 start，再读；协议字节要在 aclose 之前读完',
            )
        line = await process.stdout.readline()
        return line.decode('utf-8', errors='replace')

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await self._ladder()
        await self._cancel_task(self._watch)
        await self._cancel_task(self._reader)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # 任务已经被取消时，接下来的每一个 await 都会立刻再抛 CancelledError，阶梯走不完，
        # 孙进程就留在组里。先把取消记下来、暂时消掉，阶梯跑完再重新抛出。
        task = asyncio.current_task()
        cancelled = exc_type is asyncio.CancelledError
        if cancelled and task is not None:
            while task.cancelling():
                task.uncancel()
        try:
            await self.aclose()
        finally:
            if cancelled:
                raise asyncio.CancelledError

    async def _watch_leader(self) -> None:
        # Process.wait() 要等全部管道都断开才把等待者叫醒。孙进程继承了 stdin/stdout/stderr，
        # 组长已经退出时 wait() 不返回，终止阶梯就永远不会开始，组里的人就留下了。
        # returncode 在进程退出时就有，不等管道。
        process = self._process
        if process is None:
            return
        try:
            while process.returncode is None:  # noqa: ASYNC110
                await asyncio.sleep(_POLL_S)
        except asyncio.CancelledError:
            return
        await self.aclose()

    async def _ladder(self) -> None:
        process = self._process
        tree = self._tree
        if process is None or tree is None:
            return
        # 关 stdin 之后组长一退出，它的后代就挂到 init 名下、父子链断了，趁还连着再记一遍。
        tree.stop()
        tree.record()
        # 阶梯中途被取消时 `_closed` 已经置位，之后的 aclose 什么都不做，这里是收掉后代的唯一机会。
        try:
            await self._group_ladder(process)
        finally:
            await tree.reap()

    async def _group_ladder(self, process: asyncio.subprocess.Process) -> None:
        stdin = process.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()
        if await self._settled():
            return
        self._signal(signal.SIGTERM)
        if await self._settled():
            return
        self._signal(signal.SIGKILL)
        await self._settled()

    async def _settled(self) -> bool:
        """这一档宽限之内：组长已回收，且进程组里没有别人。"""
        deadline = time.monotonic() + self._grace
        process = self._process
        if process is not None and process.returncode is None:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    await asyncio.wait_for(process.wait(), remaining)
                except TimeoutError:
                    pass
        pgid = self._pgid
        while True:
            if pgid is None or not _group_members(pgid):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(_POLL_S, remaining))

    def _signal(self, sig: int) -> None:
        pgid = self._pgid
        if pgid is None:
            return
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            # 组里只剩僵尸时 macOS 报 EPERM，不是 ESRCH。组已经在收，不能因此让 aclose 失败。
            return

    async def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        stream = process.stderr
        lines = StderrLines(
            self._redactor,
            self._stderr_limit,
            emit=self._log_line,
            capped=self._log_cap,
            clipped=self._log_clipped,
        )
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            lines.feed(chunk)
        lines.close()

    def _log_line(self, redacted: str) -> None:
        logger.info('external process %s stderr: %s', self._pgid, redacted)

    def _log_cap(self) -> None:
        logger.warning(
            '期望: 外部进程 %s 的 stderr 不超过 %s 字节。'
            ' 实际: 超出的部分已丢弃，读取仍在继续（停止读取会让管道写满、进程堵死）。'
            ' 下一步: 让这个进程少写 stderr，或把它自己的日志写到文件里。',
            self._pgid,
            self._stderr_limit,
        )

    def _log_clipped(self, line_cap: int) -> None:
        logger.warning(
            '期望: 外部进程 %s 的 stderr 每行不超过 %s 字节。'
            ' 实际: 有一行超出，只记了开头，刀口前那段连续的非空白也丢了（它可能是半截凭证）。'
            ' 下一步: 让这个进程按行写 stderr，或把大段输出写到文件里。',
            self._pgid,
            line_cap,
        )

    @staticmethod
    async def _cancel_task(task: asyncio.Task[None] | None) -> None:
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class ProcessSupervisor:
    """这一侧拉起的全部分组。关机时逐个走终止阶梯，一个失败不跳过其余的。"""

    def __init__(self) -> None:
        self._live: list[ExternalProcess] = []

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        redactor: Redactor,
        grace: float = _DEFAULT_GRACE_S,
        stderr_limit: int = _DEFAULT_STDERR_LIMIT,
    ) -> ExternalProcess:
        process = ExternalProcess(
            argv,
            env=env,
            cwd=cwd,
            redactor=redactor,
            grace=grace,
            stderr_limit=stderr_limit,
        )
        try:
            await process.start()
        except BaseException:
            await process.aclose()
            raise
        self._live.append(process)
        return process

    async def shutdown(self) -> None:
        live = list(self._live)
        self._live.clear()
        errors: list[BaseException] = []
        for process in live:
            try:
                await process.aclose()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise errors[0]


def _argv(argv: object) -> tuple[str, ...]:
    if isinstance(argv, str | bytes) or not isinstance(argv, Sequence):
        raise ConfigError(
            expected='外部进程的 argv 是非空字符串列表，不经 shell',
            received=f'收到 {argv!r}，类型是 {type(argv).__name__}',
            next_action='写成 [程序, 参数, ...]；一个字符串会被 shell 拆开',
        )
    items: list[str] = []
    for item in cast(Sequence[object], argv):
        if not isinstance(item, str) or not item:
            raise ConfigError(
                expected='argv 的每一项是非空字符串，且至少有程序名',
                received=f'收到 {argv!r}',
                next_action='去掉空项；程序名放在第一项',
            )
        items.append(item)
    if not items:
        raise ConfigError(
            expected='argv 至少有程序名',
            received='收到空列表',
            next_action='把可执行文件放在第一项',
        )
    return tuple(items)


def _grace(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ConfigError(
            expected='grace 是 >= 0 的秒数',
            received=f'收到 {value!r}，类型是 {type(value).__name__}',
            next_action='传 2（缺省）或更小的正数；不要传 True，YAML 的 yes 会被读成布尔',
        )
    return float(value)


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(
            expected='stderr_limit 是 >= 1 的字节数',
            received=f'收到 {value!r}，类型是 {type(value).__name__}',
            next_action='传一个正整数；0 会让第一行 stderr 就被丢掉，而读取上限的意义是留得下开头',
        )
    return value
