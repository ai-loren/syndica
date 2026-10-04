"""本地目录工作区。内核里唯一读写这块磁盘的实现。

路径是否在根内，只看解析后的真实路径能否 `relative_to` 根。字符串前缀会把
`/root-evil` 和指向根外的符号链接都放进来。
不读进程环境，不查当前工作目录：根、shell、env 都是构造参数。
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import math
import os
import re
import signal
import stat
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from syndica.kernel.errors import ConfigError
from syndica.kernel.local_pty import PtySessions
from syndica.kernel.terminal_text import utf8_prefix_length
from syndica.kernel.workspace import (
    ANY,
    ExecResult,
    Expected,
    FileStat,
    GrepHit,
    Portal,
    PtyCapture,
    PtyStatus,
    WriteConflict,
)

__all__ = ['LocalWorkspace']

# 超过这个大小的文件拒绝整篇读入。模型上下文装不下，拒绝时必须报出实际字节数。
_MAX_READ_BYTES: int = 1024 * 1024
# `read_bytes` 的上限：它给版本层与产物处理器用，不进模型上下文，但仍是整篇进内存。
_MAX_BYTES: int = 64 * 1024 * 1024
# 命令的 shell 退出之后，最多再等这么久让 `cmd &` 留下的子进程关掉输出管道，然后杀整个进程组。
_PIPE_GRACE: float = 1.0
# 发出 SIGKILL 之后等进程退出、管道关闭的上限。逃出进程组的进程（`setsid`、`setpgid`，如 GNU
# `timeout` 不带 `--foreground`、bash 的 `set -m`）可以一直占着管道，过了这个上限就交回已经收到的
# 输出、关掉读端。它们逃出了进程组，这里杀不到，真正的边界是沙箱化的 Workspace 后端。
_KILL_GRACE: float = 1.0
_CHUNK: int = 64 * 1024
# 每条流在内核里最多留的字节：开头一段加结尾一段，中间丢掉。后台的 `yes` 两秒就能写出 GB 级输出，
# 而每次 poll 都要把留下的字节解码一遍——不设上限就是一次内存耗尽加上数秒的事件循环停顿。结尾留得
# 多：pytest、ruff 的汇总在末尾。展示给模型的字符上限（`local_tools` 的 `output_limit`）在这之上
# 再收窄。
_CAPTURE_HEAD: int = 256 * 1024
_CAPTURE_TAIL: int = 768 * 1024


class _Capture:
    """一条输出流留下的字节：开头 `_CAPTURE_HEAD` 加结尾 `_CAPTURE_TAIL`，丢掉的只计数。"""

    def __init__(self) -> None:
        self._head = bytearray()
        self._tail = bytearray()
        self._dropped = 0

    def extend(self, chunk: bytes) -> None:
        if len(self._head) < _CAPTURE_HEAD:
            room = _CAPTURE_HEAD - len(self._head)
            self._head += chunk[:room]
            chunk = chunk[room:]
        self._tail += chunk
        excess = len(self._tail) - _CAPTURE_TAIL
        if excess > 0:
            del self._tail[:excess]
            self._dropped += excess

    def rendered(self, since: int = 0, *, settled: bool) -> tuple[str, bool, int]:
        """(`since` 之后的输出解码成的文本, 这一段里是否有被丢弃的字节, 交出的部分结束在哪)。

        偏移按收到的字节数计，丢掉的那一段也算在内，所以前后两次查询之间不会因为丢弃而错位。
        还在跑的命令末尾不完整的 utf-8 字符留到下一次：拆开解码会在两次输出里各留一个替换符。
        """
        if not self._dropped:
            data = bytes(self._head + self._tail)[since:]
            keep = len(data) if settled else utf8_prefix_length(data)
            return _decoded(data[:keep]), False, since + keep
        head_end = len(self._head)
        tail_start = head_end + self._dropped
        parts: list[str] = []
        if since < head_end:
            parts.append(_decoded(self._head[since:]))
        if since < tail_start:
            parts.append(
                f'\n…[内核丢弃了这条流中间 {tail_start - max(since, head_end)} 个字节：每条流最多'
                f'保留开头 {_CAPTURE_HEAD} 与结尾 {_CAPTURE_TAIL} 字节]…\n'
            )
        tail_from = max(0, since - tail_start)
        data = bytes(self._tail[tail_from:])
        keep = len(data) if settled else utf8_prefix_length(data)
        parts.append(_decoded(data[:keep]))
        return ''.join(parts), since < tail_start, tail_start + tail_from + keep


@dataclass(eq=False)
class _ProcessRecord:
    process: asyncio.subprocess.Process
    stdout: _Capture
    stderr: _Capture
    # 命令结束且进程组收拾完时完成；值是是否超时。完成之后组号可能已被复用，外面不能再按它发信号。
    settled: asyncio.Task[bool]


class LocalWorkspace:
    """根目录、shell、子进程环境都由宿主传入。

    `private` 是宿主留给自己的目录（相对根）：文件工具（读、写、改，以及显式指向它们的 glob 与
    grep）碰不到，隐式的 glob 与 grep 结果里也不出现。比较不分大小写，给出的路径与解析后的路径都
    看。命令不受影响：shell 写下的东西本来就不可信，真正的边界是沙箱。
    """

    def __init__(
        self,
        root: str,
        *,
        shell: str,
        env: Mapping[str, str],
        private: Iterable[str] = (),
    ) -> None:
        self._root = _existing_absolute_dir(root)
        self._shell = _executable(shell)
        self._env = _copied_env(env)
        self._private = _private_dirs(private)
        self._processes: dict[str, _ProcessRecord] = {}
        # 正在跑的前台命令。登记它们，`close()` 才杀得到：前台命令同样可能在关闭时还没结束。
        self._foreground: set[_ProcessRecord] = set()
        self._process_lock = asyncio.Lock()
        self._closing: asyncio.Task[None] | None = None
        self._closed = False
        self._terminals = PtySessions(shell=self._shell, env=self._env)

    @property
    def root(self) -> str:
        return str(self._root)

    def portal(self) -> Portal:
        return self

    async def read_text(self, path: str, *, offset: int, limit: int) -> str:
        text, _ = self._read_lines(path, offset=offset, limit=limit)
        return text

    async def read_versioned(self, path: str, *, offset: int, limit: int) -> tuple[str, str]:
        return self._read_lines(path, offset=offset, limit=limit)

    def _read_lines(self, path: str, *, offset: int, limit: int) -> tuple[str, str]:
        """(那几行, 读到的那份内容的版本)：版本出自同一次读，读与取版本之间不会插进别人的写。"""
        if offset < 1:
            raise ConfigError(
                expected='offset 是从 1 开始的行号',
                received=f'offset={offset}',
                next_action='传入大于等于 1 的行号',
            )
        if limit < 1:
            raise ConfigError(
                expected='limit 是正整数',
                received=f'limit={limit}',
                next_action='传入大于等于 1 的行数上限',
            )
        real = self._inside(path)
        self._refuse_private(path, real)
        if not real.is_file():
            raise ConfigError(
                expected='path 指向根目录内的文件',
                received=f'path={path!r} 不是文件',
                next_action='改用文件路径',
            )
        size = real.stat().st_size
        if size > _MAX_READ_BYTES:
            raise ConfigError(
                expected=f'单文件不超过 {_MAX_READ_BYTES} 字节',
                received=f'文件有 {size} 字节',
                next_action='换一个不超过 1 MiB 的文件',
            )
        data = real.read_bytes()
        version = _version(data)
        lines = _utf8(data, path).splitlines()
        start = offset - 1
        if start >= len(lines):
            if len(lines) == 0 and offset == 1:
                return '', version
            raise ConfigError(
                expected=f'offset 不超过文件行数 {len(lines)}',
                received=f'offset={offset}',
                next_action='把 offset 调到实际行数以内',
            )
        return '\n'.join(lines[start : start + limit]), version

    async def write_text(self, path: str, content: str, *, expected: Expected = ANY) -> str:
        return self._write(path, content.encode('utf-8'), expected)

    async def write_bytes(self, path: str, data: bytes, *, expected: Expected = ANY) -> str:
        if not isinstance(cast(object, data), bytes | bytearray):
            raise ConfigError(
                expected='data 是 bytes',
                received=f'data 的类型是 {type(data).__name__}',
                next_action='写文本用 write_text；写二进制先编码成 bytes',
            )
        return self._write(path, bytes(data), expected)

    async def edit_text(self, path: str, old: str, new: str, *, expected: Expected = ANY) -> str:
        if old == '':
            raise ConfigError(
                expected='old 是文件里出现恰好一次的非空片段',
                received='old 是空串',
                next_action='传入要替换的原文片段',
            )
        real = self._writable(path)
        if not real.is_file():
            raise ConfigError(
                expected='path 指向根目录内的文件',
                received=f'path={path!r} 不是文件',
                next_action='改用文件路径',
            )
        data = real.read_bytes()
        if expected is not ANY:
            _check_expected(path, expected, _version(data))
        text = _utf8(data, path)
        count = text.count(old)
        if count != 1:
            raise ConfigError(
                expected='old 在文件中出现恰好 1 次',
                received=f'出现了 {count} 次',
                next_action='把 old 收窄到唯一的一段，或先读出文件再改',
            )
        written = text.replace(old, new, 1).encode('utf-8')
        real.write_bytes(written)
        return _version(written)

    async def read_bytes(self, path: str) -> bytes:
        real = self._inside(path)
        self._refuse_private(path, real)
        if not real.is_file():
            raise ConfigError(
                expected='path 指向根目录内的文件',
                received=f'path={path!r} 不是文件',
                next_action='改用文件路径',
            )
        size = real.stat().st_size
        if size > _MAX_BYTES:
            raise ConfigError(
                expected=f'单文件不超过 {_MAX_BYTES} 字节',
                received=f'文件有 {size} 字节',
                next_action='大文件不经 Portal 整篇读入；按需要分段处理',
            )
        return real.read_bytes()

    async def remove(self, path: str, *, expected: Expected = ANY) -> None:
        real = self._writable(path)
        if (self._root / path).is_symlink():
            raise ConfigError(
                expected='remove 只删普通文件',
                received=f'path={path!r} 是符号链接',
                next_action='符号链接与它指向的文件是两件东西；要删链接本身请用命令',
            )
        if not real.is_file():
            raise ConfigError(
                expected='path 指向根目录内已存在的文件',
                received=f'path={path!r} 不存在或不是文件',
                next_action='先确认路径；目录不经 remove 删除',
            )
        if expected is not ANY:
            _check_expected(path, expected, _file_version(real)[0])
        real.unlink()

    async def stat(self, path: str) -> FileStat | None:
        real = self._inside(path)
        self._refuse_private(path, real)
        if not real.exists():
            return None
        if not real.is_file():
            raise ConfigError(
                expected='path 指向根目录内的文件',
                received=f'path={path!r} 存在但不是文件',
                next_action='stat 只给文件版本；目录没有版本',
            )
        version, size = _file_version(real)
        return FileStat(version=version, size=size)

    def _write(self, path: str, data: bytes, expected: Expected) -> str:
        """写入的全部检查与写本身之间没有挂起点：同一个事件循环里并发的两次带前提的写，后到的那个
        一定看见先到的那个写下的版本。"""
        real = self._writable(path)
        if real.exists() and not real.is_file():
            raise ConfigError(
                expected='path 指向根目录内的文件',
                received=f'path={path!r} 已存在但不是文件',
                next_action='改用文件路径',
            )
        if expected is not ANY:
            _check_expected(path, expected, _file_version(real)[0] if real.is_file() else None)
        parent = real.parent
        self._must_stay(parent)
        parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(data)
        return _version(data)

    async def glob_paths(self, pattern: str) -> tuple[str, ...]:
        if pattern.strip() == '':
            raise ConfigError(
                expected='pattern 是相对根目录的非空模式',
                received='pattern 是空串',
                next_action='传入相对路径模式',
            )
        if Path(pattern).is_absolute():
            raise ConfigError(
                expected='pattern 是相对根目录的模式',
                received=f'pattern={pattern!r} 是绝对路径',
                next_action='去掉根前缀，只传相对模式',
            )
        if '..' in Path(pattern).parts:
            raise ConfigError(
                expected='pattern 是不含 ..、不会离开工作区根目录的相对模式',
                received=f'pattern={pattern!r} 含父目录段 ..',
                next_action='去掉 ..，从工作区根目录开始写相对模式',
            )
        self._refuse_private(pattern, None)
        named = [part for part in Path(pattern).parts if part.startswith('.')]
        found: list[str] = []
        for match in self._root.glob(pattern):
            try:
                real = match.resolve().relative_to(self._root)
            except ValueError:
                continue
            relative = match.relative_to(self._root)
            if any(_hidden(part) and not _named(part, named) for part in relative.parts):
                continue
            if self._private and (
                self._private_dir(relative.parts) or self._private_dir(real.parts)
            ):
                continue
            found.append(relative.as_posix())
        found.sort()
        return tuple(found)

    async def grep_text(self, pattern: str, path: str) -> tuple[GrepHit, ...]:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ConfigError(
                expected='pattern 是合法的正则',
                received=str(exc),
                next_action='修正正则后再搜',
            ) from exc
        start = self._inside('.' if path in ('', '.') else path)
        self._refuse_private(path, start)
        files = _files_under(start, self._root)
        hits: list[GrepHit] = []
        for file in files:
            if self._private_dir(file.relative_to(self._root).parts):
                continue
            rel = file.relative_to(self._root).as_posix()
            data = file.read_bytes()
            # 开头 8 KiB 里有 NUL 就当二进制文件跳过（git 与 grep 的同一判据）：字节码里偶然匹配上的
            # 「行」只会占掉输出的配额。
            if b'\0' in data[:8192]:
                continue
            # 搜索遇到非 utf-8 用替换字符：一次坏文件不能让整次搜索失败。
            text = data.decode('utf-8', errors='replace')
            for lineno, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line):
                    hits.append(GrepHit(rel, lineno, line))
        return tuple(hits)

    async def run(self, command: str, *, timeout: float, cwd: str) -> ExecResult:
        seconds = _positive_timeout(timeout)
        async with self._process_lock:
            self._ensure_open()
            record = await self._spawned(command, cwd, timeout=seconds)
            self._foreground.add(record)
        record.settled.add_done_callback(lambda _: self._foreground.discard(record))
        try:
            timed_out = await asyncio.shield(record.settled)
        except BaseException:
            _kill_unsettled(record)
            raise
        return _result(record, timed_out=timed_out)

    async def start(self, command: str, *, cwd: str) -> str:
        async with self._process_lock:
            self._ensure_open()
            record = await self._spawned(command, cwd, timeout=None)
            handle = uuid.uuid4().hex
            self._processes[handle] = record
            return handle

    async def poll(self, handle: str, *, since: tuple[int, int] = (0, 0)) -> ExecResult:
        self._ensure_open()
        record = self._record(handle)
        start_out, start_err = _offsets(since)
        if not record.settled.done():
            stdout, stdout_dropped, stdout_end = record.stdout.rendered(start_out, settled=False)
            stderr, stderr_dropped, stderr_end = record.stderr.rendered(start_err, settled=False)
            return ExecResult(
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                truncated=stdout_dropped or stderr_dropped,
                running=True,
                stdout_end=stdout_end,
                stderr_end=stderr_end,
            )
        return _result(record, timed_out=record.settled.result(), since=(start_out, start_err))

    async def kill(self, handle: str) -> None:
        self._ensure_open()
        record = self._record(handle)
        _kill_unsettled(record)
        await asyncio.shield(record.settled)

    async def pty_create(self, command: str, *, cwd: str, cols: int = 120, rows: int = 40) -> str:
        async with self._process_lock:
            self._ensure_open()
            return await self._terminals.create(
                command, cwd=self._directory(cwd), cols=cols, rows=rows
            )

    async def pty_send(self, handle: str, data: str) -> None:
        self._ensure_open()
        if not isinstance(cast(object, data), str):
            raise ConfigError(
                expected='data 是要写进终端的文字',
                received=f'data 的类型是 {type(data).__name__}',
                next_action='传 str；换行写 \\n，Ctrl-C 写 \\u0003',
            )
        await self._terminals.send(handle, data)

    async def pty_capture(self, handle: str, *, since: int = 0, wait: float = 0.0) -> PtyCapture:
        self._ensure_open()
        return await self._terminals.capture(handle, since=since, wait=wait)

    async def pty_resize(self, handle: str, *, cols: int, rows: int) -> None:
        self._ensure_open()
        await self._terminals.resize(handle, cols, rows)

    async def pty_kill(self, handle: str) -> None:
        self._ensure_open()
        await self._terminals.kill(handle)

    async def pty_status(self, handle: str) -> PtyStatus:
        self._ensure_open()
        return self._terminals.status(handle)

    async def close(self) -> None:
        if self._closed:
            return
        async with self._process_lock:
            if self._closed:
                return
            if self._closing is None:
                self._closing = asyncio.create_task(self._close_processes())
            closing = self._closing
        await asyncio.shield(closing)

    async def _spawned(self, command: str, cwd: str, *, timeout: float | None) -> _ProcessRecord:
        """拉起一条命令。取消不会打断拉起本身：进程一旦起来就一定有人收拾它的进程组。

        `asyncio.create_subprocess_exec` 在 fork 之后自己还有一次挂起；那里到达的取消只会 SIGKILL
        shell 本身（`Popen.kill()`），它这时可能已经拉起的子进程连同整个组就没人管了。所以拉起放进
        一个独立任务、用 shield 等它；被取消时给它挂一个回调，拉起完成的那一刻杀掉整个组。
        """
        spawning = asyncio.ensure_future(self._spawn_process(command, cwd, timeout=timeout))
        try:
            return await asyncio.shield(spawning)
        except BaseException:
            spawning.add_done_callback(_kill_abandoned)
            raise

    async def _spawn_process(
        self, command: str, cwd: str, *, timeout: float | None
    ) -> _ProcessRecord:
        working_directory = self._directory(cwd)
        # 两条输出管道自己建：读端的传输归这里，逃出进程组的进程占着写端时也能主动关掉；
        # `Process.wait()` 也因此只看进程退出，不等管道关闭。读端先接好再拉起进程，拉起之后到
        # 建好收拾它的任务之间没有挂起点。
        unowned: list[int] = []
        writers: list[int] = []
        connected: list[_Pipe] = []
        try:
            for _ in range(2):
                read_end, write_end = os.pipe()
                unowned.append(read_end)
                writers.append(write_end)
            while unowned:
                connected.append(await _pipe(unowned.pop(0)))
            # stdin 接 /dev/null：读 stdin 的命令立刻得到 EOF，不与宿主抢终端输入、也不挂到超时。
            # 新会话即新进程组，组号等于 shell 的 pid，超时、kill、close 都按组杀。
            process = await asyncio.create_subprocess_exec(
                self._shell,
                '-c',
                command,
                cwd=str(working_directory),
                env=dict(self._env),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=writers[0],
                stderr=writers[1],
                start_new_session=True,
            )
        except BaseException as exc:
            for _, transport in connected:
                transport.close()
            for fd in unowned:
                os.close(fd)
            if isinstance(exc, OSError):
                raise ConfigError(
                    expected='能为这条命令建输出管道并拉起 shell',
                    received=f'{type(exc).__name__}: {exc}',
                    next_action=(
                        '多半是文件描述符或进程数用尽：先用 KillBash 结束不再需要的后台命令，'
                        '再重试这条命令'
                    ),
                ) from exc
            raise
        finally:
            for fd in writers:
                os.close(fd)
        pipes = (connected[0], connected[1])
        stdout, stderr = _Capture(), _Capture()
        reading = asyncio.create_task(_read_both(pipes, stdout, stderr))
        return _ProcessRecord(
            process=process,
            stdout=stdout,
            stderr=stderr,
            settled=asyncio.create_task(_settle(process, reading, pipes, timeout)),
        )

    async def _close_processes(self) -> None:
        try:
            records = (*self._processes.values(), *self._foreground)
            for record in records:
                _kill_unsettled(record)
            for record in records:
                await asyncio.shield(record.settled)
            await self._terminals.close()
        finally:
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed or self._closing is not None:
            raise ConfigError(
                expected='工作区仍然打开，可以执行或查询命令',
                received='工作区已关闭或正在关闭',
                next_action='新建一个 LocalWorkspace；close() 之后同一个工作区不再接收进程操作',
            )

    def _record(self, handle: str) -> _ProcessRecord:
        record = self._processes.get(handle)
        if record is None:
            raise ConfigError(
                expected='handle 是本工作区 start() 返回的后台命令句柄',
                received=f'收到未知 handle={handle!r}',
                next_action='使用 Bash 启动后台命令后返回的 handle，不要复用其他工作区的句柄',
            )
        return record

    def _inside(self, path: str) -> Path:
        real = self._resolved(path)
        self._must_stay(real)
        return real

    def _writable(self, path: str) -> Path:
        """写入目标：在根内，且写的是 `.git` 之外的位置。

        给出的路径与解析后的路径都要看：`.git` 可能是指向别处的符号链接（给出的路径里有它），根内
        也可能有指进 `.git` 的符号链接（解析后的路径里有它）。不分大小写：APFS 默认不区分，
        `.GIT/hooks/x` 就是真的 hooks 目录。`.git` 是文件（worktree）时同样拒写。

        认不出的：`.git` 是符号链接或 `gitdir:` 指针时，真正的 git 目录用它自己的名字写
        （`other/gitdir/config`）；指向 `.git` 里文件的硬链接；检查与写入之间被别的进程换掉的路径。
        后两者要靠 Bash 才造得出来，归 `shell.git_state_change`；顶层仓库的 git 目录另有宿主的
        完整性快照兜底。
        """
        real = self._inside(path)
        if self._in_git_dir(path, real):
            raise ConfigError(
                expected='写入的位置不在 .git 里（git 的内部文件；worktree 的 .git 指针文件也算）',
                received=f'path={path!r} 落在 .git 里',
                next_action=(
                    '不要直接改 .git 下的文件：提交、分支、钩子、配置这类 git 状态由宿主在复核'
                    '之后处理。只改工作区里的普通文件'
                ),
            )
        self._refuse_private(path, real)
        for parts in (*self._named_parts(path), real.relative_to(self._root).parts):
            folded = tuple(part.casefold() for part in parts)
            for shown, private in self._private:
                if folded and len(folded) < len(private) and private[: len(folded)] == folded:
                    raise ConfigError(
                        expected=f'写下的文件不占住宿主保留的目录 {shown} 的位置',
                        received=f'path={path!r} 是 {shown} 的上级目录',
                        next_action='换一个文件名：在这里建了文件，宿主就建不出它要用的目录',
                    )
        return real

    def _private_dir(self, parts: Iterable[str]) -> str | None:
        """相对根的这些段落在哪个私有目录里（不分大小写）；不在任何一个里时是 None。"""
        folded = tuple(part.casefold() for part in parts)
        for shown, private in self._private:
            if folded[: len(private)] == private:
                return shown
        return None

    def _refuse_private(self, path: str, real: Path | None) -> None:
        """给出的路径或解析后的路径落在私有目录里就拒：经符号链接、`..`、根的别名与大小写变体进去的
        都算。"""
        candidates = list(self._named_parts(path))
        if real is not None:
            candidates.append(real.relative_to(self._root).parts)
        shown = next(filter(None, map(self._private_dir, candidates)), None)
        if shown is None:
            return
        reserved = '、'.join(name for name, _ in self._private)
        raise ConfigError(
            expected=f'文件工具的路径不在宿主保留的目录里（{reserved}）',
            received=f'path={path!r} 落在 {shown} 里',
            next_action=(
                '这些目录只归宿主读写：里面是宿主自己的记录，含此前标成不可信的工具输出，经文件工具'
                '读回来就成了可信内容。换一个不在这些目录下的路径'
            ),
        )

    def _in_git_dir(self, path: str, real: Path) -> bool:
        """给出的路径或解析后的路径里有一段是 `.git`（不分大小写）。"""
        given = [part for parts in self._named_parts(path) for part in parts]
        parts = (*given, *real.relative_to(self._root).parts)
        return any(part.casefold() == '.git' for part in parts)

    def _named_parts(self, path: str) -> tuple[tuple[str, ...], ...]:
        """给出的路径在根下的各段：照写的样子一份，`.` 与 `..` 按字面折叠之后一份（相同时只交
        一份）。两份都要：`src/../.syndica/traces` 折叠之后才看得出点了谁，`.git/../x` 照写的样子
        才看得出经过了谁。给的是根外的绝对路径时没有（越界另有 `_must_stay` 拒）；绝对路径可以经根
        的别名给出（macOS 的 `/tmp` 是 `/private/tmp`），所以从最近的一级往上找解析之后就是根的
        那一级。"""
        found: list[tuple[str, ...]] = []
        for raw in (Path(path), Path(os.path.normpath(path))):
            if not raw.is_absolute():
                found.append(raw.parts)
                continue
            for ancestor in (raw, *raw.parents):
                if ancestor.resolve() == self._root:
                    found.append(raw.relative_to(ancestor).parts)
                    break
        return tuple(dict.fromkeys(found))

    def _directory(self, path: str) -> Path:
        """命令的工作目录：根内的目录，且不在 `.git` 里。

        危险规则只看命令文本，从 `.git` 里出发的相对路径（`printf x >> config`、`rm -r objects`）
        不带 `.git` 这一段，规则看不出它在改 git 状态，所以在这里按目录身份拒。
        """
        real = self._inside(path)
        if not real.is_dir():
            raise ConfigError(
                expected='cwd 指向工作区根目录内的目录',
                received=f'cwd={path!r} 不是目录',
                next_action='传入工作区根目录内的相对目录，或用 "." 表示根目录',
            )
        if self._in_git_dir(path, real):
            raise ConfigError(
                expected='命令的 cwd 不在 .git 里',
                received=f'cwd={path!r} 落在 .git 里',
                next_action=(
                    '从工作区根（cwd 用 "."）或普通子目录运行命令；看 git 状态用 git status、'
                    'git log 这类只读子命令，读 .git 下的文件用 Read'
                ),
            )
        return real

    def _resolved(self, path: str) -> Path:
        raw = Path(path)
        candidate = raw if raw.is_absolute() else self._root / raw
        return candidate.resolve()

    def _must_stay(self, real: Path) -> None:
        try:
            real.relative_to(self._root)
        except ValueError:
            raise ConfigError(
                expected='路径解析后仍在工作区根目录内',
                received=f'解析后的路径落在根目录外（收到 {real.name!r}）',
                next_action='改用根目录内的相对路径',
            ) from None


def _file_version(path: Path) -> tuple[str, int]:
    """(版本, 字节数)，按块读：工作区里可以有几个 GB 的数据集，整篇读进内存再哈希会撑死宿主进程。"""
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return f'sha256:{digest.hexdigest()}', size


def _utf8(data: bytes, received: str) -> str:
    # Read / Edit 只接受 utf-8。解码失败必须是 ConfigError，不能把 UnicodeDecodeError 抛出去。
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ConfigError(
            expected='文件是 utf-8 文本',
            received=f'path={received!r} 不是 utf-8',
            next_action='换一个 utf-8 文本文件',
        ) from exc


def _version(data: bytes) -> str:
    return f'sha256:{hashlib.sha256(data).hexdigest()}'


def _check_expected(path: str, expected: object, current: str | None) -> None:
    if expected is ANY:
        return
    if expected is not None and not isinstance(expected, str):
        raise ConfigError(
            expected='expected 是 ANY、None（要求文件不存在）或一个版本字符串',
            received=f'expected={expected!r}，类型是 {type(expected).__name__}',
            next_action='传 stat() 交回的 version；不检查版本就不传',
        )
    if expected != current:
        raise WriteConflict(path, expected, current)


def _offsets(since: object) -> tuple[int, int]:
    pair = cast('tuple[object, ...]', since) if isinstance(since, tuple) else ()
    if len(pair) != 2 or not all(
        isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in pair
    ):
        raise ConfigError(
            expected='since 是 (stdout_end, stderr_end) 两个非负整数',
            received=f'since={since!r}',
            next_action='传上一次 poll 结果里的 stdout_end 与 stderr_end；从头看就不传',
        )
    return cast('tuple[int, int]', pair)


def _positive_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(
            expected='timeout 是有限正数秒数',
            received=f'timeout={value!r}，类型是 {type(value).__name__}',
            next_action='传入大于 0 的 int 或 float，不要传布尔值',
        )
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ConfigError(
            expected='timeout 是有限正数秒数',
            received=f'timeout={value!r}',
            next_action='传入大于 0 的有限秒数；0、负数、inf 与 nan 都不合法',
        )
    return seconds


def _decoded(value: bytes | bytearray) -> str:
    return bytes(value).decode('utf-8', errors='replace')


def _result(
    record: _ProcessRecord, *, timed_out: bool, since: tuple[int, int] = (0, 0)
) -> ExecResult:
    stdout, stdout_dropped, stdout_end = record.stdout.rendered(since[0], settled=True)
    stderr, stderr_dropped, stderr_end = record.stderr.rendered(since[1], settled=True)
    return ExecResult(
        exit_code=None if timed_out else record.process.returncode,
        stdout=stdout,
        stderr=stderr,
        truncated=stdout_dropped or stderr_dropped,
        running=False,
        timed_out=timed_out,
        stdout_end=stdout_end,
        stderr_end=stderr_end,
    )


_Pipe = tuple[asyncio.StreamReader, asyncio.BaseTransport]


async def _pipe(fd: int) -> _Pipe:
    """把一条管道的读端接进事件循环。从调用起 fd 归它：失败时由它关掉，调用方不能再关一次。"""
    reader = asyncio.StreamReader()
    file = os.fdopen(fd, 'rb', buffering=0)
    try:
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), file
        )
    except BaseException:
        file.close()
        raise
    return reader, transport


async def _read_both(pipes: tuple[_Pipe, _Pipe], stdout: _Capture, stderr: _Capture) -> None:
    await asyncio.gather(_drain(pipes[0][0], stdout), _drain(pipes[1][0], stderr))


async def _drain(stream: asyncio.StreamReader, sink: _Capture) -> None:
    while chunk := await stream.read(_CHUNK):
        sink.extend(chunk)


async def _finished_within(task: asyncio.Future[Any], seconds: float | None) -> bool:
    """等 task 至多 seconds 秒（None 是不设上限），交回它是否已完成。超时不取消它。"""
    done, _ = await asyncio.wait({task}, timeout=seconds)
    return bool(done)


async def _settle(
    process: asyncio.subprocess.Process,
    reading: asyncio.Task[None],
    pipes: tuple[_Pipe, _Pipe],
    timeout: float | None,
) -> bool:
    """等命令结束并收拾它的进程组；交回是否超时。

    组长（shell）退出不等于命令结束：`cmd &` 留下的子进程还在组里、还占着输出管道。所以先最多等
    `_PIPE_GRACE` 让管道关掉，然后**无条件**杀整个进程组——组比组长活得久。每一步等待都有上限：
    逃出进程组的进程可以一直占着管道，那时交回已经收到的输出、关掉读端。
    """
    exiting = asyncio.ensure_future(process.wait())
    try:
        timed_out = not await _finished_within(exiting, timeout)
        if not timed_out:
            await _finished_within(reading, _PIPE_GRACE)
        _kill_group(process.pid)
        await _finished_within(exiting, _KILL_GRACE)
        if not await _finished_within(reading, _KILL_GRACE):
            reading.cancel()
        return timed_out
    except asyncio.CancelledError:
        # 事件循环被拆掉而宿主没有调 close()：这时还没收拾完，组号仍是这条命令的，杀掉它再走。
        _kill_group(process.pid)
        raise
    finally:
        if not exiting.done():
            exiting.cancel()
        for _, transport in pipes:
            transport.close()


def _kill_group(pid: int) -> None:
    # PermissionError：macOS 对只剩僵尸的组报 EPERM 而不是 ESRCH；吞掉它，close() 才会接着收拾
    # 其余的组。
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _kill_unsettled(record: _ProcessRecord) -> None:
    """外面要杀一条命令时用它：只在它还没收拾完时发信号。

    收拾完的命令，组已经杀过、shell 也已回收，组号随时可能被一个无关的新进程组复用——这时再按它
    发 SIGKILL 杀的就是别人。`kill` 对着一个早已结束的句柄、`close()` 收尾时都会走到这里。
    """
    if not record.settled.done():
        _kill_group(record.process.pid)


def _kill_abandoned(spawning: asyncio.Future[_ProcessRecord]) -> None:
    """拉起途中被取消的命令：拉起一完成就杀掉它的组，`_settle` 随后收掉管道。"""
    if spawning.cancelled() or spawning.exception() is not None:
        return
    _kill_unsettled(spawning.result())


def _existing_absolute_dir(root: object) -> Path:
    if not isinstance(root, str) or root.strip() == '' or not Path(root).is_absolute():
        raise ConfigError(
            expected='root 是已存在目录的绝对路径',
            received=f'root={root!r}',
            next_action='传入绝对路径',
        )
    real = Path(root).resolve()
    if not real.is_dir():
        raise ConfigError(
            expected='root 是已存在目录的绝对路径',
            received=f'root={root!r} 不是已存在的目录',
            next_action='传入一个已经存在的目录的绝对路径',
        )
    return real


def _executable(shell: object) -> str:
    if not isinstance(shell, str) or shell.strip() == '' or not Path(shell).is_absolute():
        raise ConfigError(
            expected='shell 是可执行文件的绝对路径',
            received=f'shell={shell!r}',
            next_action='传入绝对路径',
        )
    path = Path(shell)
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ConfigError(
            expected='shell 是可执行文件的绝对路径',
            received=f'shell={shell!r} 不存在',
            next_action='传入一个可执行的绝对路径',
        ) from exc
    if not stat.S_ISREG(mode) or mode & 0o111 == 0:
        raise ConfigError(
            expected='shell 是可执行文件的绝对路径',
            received=f'shell={shell!r} 不可执行',
            next_action='传入一个可执行的绝对路径',
        )
    return shell


def _private_dirs(private: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """(显示用的写法, 不分大小写的各段)。"""
    if isinstance(private, str | bytes) or not isinstance(private, Iterable):
        raise ConfigError(
            expected='private 是相对根目录的目录路径列表',
            received=f'private 的类型是 {type(private).__name__}',
            next_action="传一个列表，例如 private=['.syndica/traces']：一个字符串会被逐字符拆开",
        )
    dirs: list[tuple[str, tuple[str, ...]]] = []
    for index, item in enumerate(cast('Iterable[object]', private)):
        path = PurePosixPath(item) if isinstance(item, str) else None
        if path is None or path.is_absolute() or not path.parts or '..' in path.parts:
            raise ConfigError(
                expected='private 的每一项是根目录下的相对目录，不含 ..、不是根本身',
                received=f'private[{index}]={item!r}',
                next_action="写成相对根的路径，例如 '.syndica/traces'",
            )
        dirs.append((path.as_posix(), tuple(part.casefold() for part in path.parts)))
    return tuple(dirs)


def _copied_env(env: object) -> dict[str, str]:
    if not isinstance(env, Mapping):
        raise ConfigError(
            expected='env 是 str 到 str 的映射',
            received=f'env 的类型是 {type(env).__name__}',
            next_action='传入 Mapping[str, str]，不要传入进程环境',
        )
    copied: dict[str, str] = {}
    pairs = cast('Iterable[tuple[object, object]]', env.items())
    for key, value in pairs:
        if not isinstance(key, str) or not isinstance(value, str):
            key_type = type(key).__name__
            value_type = type(value).__name__
            raise ConfigError(
                expected='env 的键和值都是 str',
                received=f'键 {key!r} 的类型是 {key_type}，值的类型是 {value_type}',
                next_action='把 env 的键和值都转成 str',
            )
        copied[key] = value
    return copied


def _files_under(start: Path, root: Path) -> tuple[Path, ...]:
    if start.is_symlink():
        start = start.resolve()
    if start.is_file():
        return (start,)
    if not start.is_dir():
        raise ConfigError(
            expected='path 指向根目录内的文件或目录',
            received=f'path 解析到 {start.name!r}，既不是文件也不是目录',
            next_action='改用根目录内的路径',
        )
    files: list[Path] = []
    for match in start.rglob('*'):
        if any(_hidden(part) for part in match.relative_to(start).parts):
            continue
        try:
            real = match.resolve()
            real.relative_to(root)
        except ValueError:
            continue
        if real.is_file():
            files.append(real)
    files.sort()
    return tuple(files)


def _hidden(part: str) -> bool:
    """以 . 开头的目录与文件（.git、.venv、.env、各种缓存）。搜索只在调用方点名时才进去：
    它们按字典序排在最前，输出有上限时整段配额都会花在它们身上。"""
    return part.startswith('.')


def _named(part: str, named: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(part, pattern) for pattern in named)
