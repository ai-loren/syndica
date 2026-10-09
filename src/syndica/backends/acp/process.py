"""外部进程的进程组。

关 stdin、宽限、SIGTERM 整组、宽限、SIGKILL 整组。组长退出后组里还可能有孙进程，所以结束看的是
进程组，不是组长的 wait。stderr 一直读到进程结束：超过上限只丢内容，不停止读取——没人读的管道
会把子进程堵死。写出日志之前先过 Redactor。

`env` 是完整环境，不是在宿主环境上追加。追加会把宿主的密钥交给外部进程。

别的 backend 不得 import 这个模块。MCP 与 A2A 各自有一份监管：backend 之间不共享代码。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Self, cast

from syndica.kernel import ConfigError, Redactor

__all__ = ['ExternalProcess', 'ProcessSupervisor']

logger = logging.getLogger(__name__)

_DEFAULT_GRACE_S = 2.0
_DEFAULT_STDERR_LIMIT = 1_048_576
_POLL_S = 0.02


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
    """一个外部进程和它的进程组。`aclose` 幂等：组已经杀过之后不再发信号，避免组号被复用后误杀。"""

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
        self._reader = asyncio.create_task(self._read_stderr(), name='acp-stderr')
        self._watch = asyncio.create_task(self._watch_leader(), name='acp-leader')

    def streams(self) -> tuple[asyncio.StreamWriter, asyncio.StreamReader]:
        """交给 ACP 连接的 stdin 与 stdout。stderr 不在这里：它由监管一直读到进程结束。"""
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise ConfigError(
                expected='外部进程已经 start，stdin 与 stdout 还开着',
                received='进程还没拉起，或管道已经关掉',
                next_action='先 start，再把这两端交给 ACP 连接；协议字节要在 aclose 之前写完',
            )
        return process.stdin, process.stdout

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
        if process is None or self._pgid is None:
            return
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
        kept = 0
        dropped = False
        pending: bytes = b''
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            room = self._stderr_limit - kept
            if room > 0:
                take = chunk[:room]
                kept += len(take)
                pending += take
                while True:
                    newline = pending.find(b'\n')
                    if newline < 0:
                        break
                    self._log_line(pending[:newline])
                    pending = pending[newline + 1 :]
                if kept >= self._stderr_limit and not dropped:
                    dropped = True
                    if pending:
                        self._log_line(pending)
                        pending = b''
                    self._log_cap()
        if pending and not dropped:
            self._log_line(pending)

    def _log_line(self, line: bytes) -> None:
        text = line.decode('utf-8', errors='replace')
        if not text:
            return
        redacted = self._redactor.redact(text)
        logger.info('external process %s stderr: %s', self._pgid, redacted)

    def _log_cap(self) -> None:
        logger.warning(
            '期望: 外部进程 %s 的 stderr 不超过 %s 字节。'
            ' 实际: 超出的部分已丢弃，读取仍在继续（停止读取会让管道写满、进程堵死）。'
            ' 下一步: 让这个进程少写 stderr，或把它自己的日志写到文件里。',
            self._pgid,
            self._stderr_limit,
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
