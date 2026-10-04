"""本地伪终端会话（spec §6.5）。`LocalWorkspace` 的 `pty_*` 方法都落在这里。

会话的从端是子进程的三条流。它会不会成为控制终端取决于平台：`subprocess` 不在 fork 与 exec 之间
调 `TIOCSCTTY`（只有多线程下不安全的 `preexec_fn` 能做），macOS 上会话首进程仍然拿到了它，Linux 上
拿不到。拿不到时打开 `/dev/tty` 的程序（`getpass`、`ssh` 的口令）读不到它，内核也不会在窗口大小
变化时通知进程组，所以 `resize` 自己发 `SIGWINCH`。读 stdin 的程序（REPL、`read`、提示输入）在
两边都照常工作。只支持 POSIX。
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import re
import signal
import struct
import termios
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from syndica.kernel.errors import ConfigError
from syndica.kernel.terminal_text import utf8_prefix_length
from syndica.kernel.workspace import PtyCapture, PtyStatus

__all__ = ['PtySessions']

# 一个工作区同时开着的会话数上限：每个会话占一对 fd 与一棵进程树。
_MAX_SESSIONS: int = 8
# 每个会话在内核里最多留的字节：开头一段加结尾一段，与命令输出同一个思路。
_KEEP_HEAD: int = 64 * 1024
_KEEP_TAIL: int = 448 * 1024
# kill 先发 SIGTERM，宽限这么久再 SIGKILL：交互式程序常常要收尾（写历史、恢复终端设置）。
_TERM_GRACE: float = 0.5
_KILL_GRACE: float = 1.0
_CHUNK: int = 64 * 1024
_MAX_WAIT: float = 60.0

# 去掉的控制序列：CSI（颜色、光标）、OSC（标题、超链接）、其余 ESC 序列（中间字节加一个终止字节，
# 如 `ESC =`、`ESC ( B`）；再去掉除换行与制表之外的 C0 控制符。交给模型的是文字，不是给终端画屏的
# 指令。
_CONTROL = re.compile(
    r'\x1b\[[0-?]*[ -/]*[@-~]'
    r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'
    r'|\x1b[ -/]*[0-~]'
    r'|[\x00-\x08\x0b-\x1f\x7f]'
)


@dataclass(eq=False)
class _Session:
    process: asyncio.subprocess.Process
    master: int
    cols: int
    rows: int
    head: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)
    dropped: int = 0
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    reading: bool = True

    @property
    def end(self) -> int:
        return len(self.head) + self.dropped + len(self.tail)

    def extend(self, chunk: bytes) -> None:
        if len(self.head) < _KEEP_HEAD:
            room = _KEEP_HEAD - len(self.head)
            self.head += chunk[:room]
            chunk = chunk[room:]
        self.tail += chunk
        excess = len(self.tail) - _KEEP_TAIL
        if excess > 0:
            del self.tail[:excess]
            self.dropped += excess
        self.arrived.set()

    def since(self, offset: int, *, settled: bool) -> tuple[str, bool, int]:
        """(`offset` 之后的输出去掉控制序列之后的文字, 这一段里是否有被丢弃的字节,
        交出的部分结束在哪)。还在跑的会话末尾不完整的 utf-8 字符留到下一次。"""
        if not self.dropped:
            data = bytes(self.head + self.tail)[offset:]
            keep = len(data) if settled else utf8_prefix_length(data)
            return _plain(data[:keep]), False, max(offset, offset + keep)
        head_end = len(self.head)
        tail_start = head_end + self.dropped
        parts: list[str] = []
        if offset < head_end:
            parts.append(_plain(bytes(self.head[offset:])))
        lost = tail_start - max(offset, head_end) if offset < tail_start else 0
        if lost:
            parts.append(f'\n…[这个会话中间 {lost} 个字节的输出没有保留]…\n')
        tail_from = max(0, offset - tail_start)
        data = bytes(self.tail[tail_from:])
        keep = len(data) if settled else utf8_prefix_length(data)
        parts.append(_plain(data[:keep]))
        return ''.join(parts), bool(lost), max(offset, tail_start + tail_from + keep)


def _plain(data: bytes) -> str:
    text = data.decode('utf-8', errors='replace').replace('\r\n', '\n')
    return _CONTROL.sub('', text)


def _size(cols: object, rows: object) -> tuple[int, int]:
    for name, value in (('cols', cols), ('rows', rows)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
            raise ConfigError(
                expected=f'{name} 是 1 到 1000 之间的整数',
                received=f'{name}={value!r}',
                next_action='传一个合理的窗口大小，例如 cols=120、rows=40',
            )
    return cols, rows  # pyright: ignore[reportReturnType]


def _set_window(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))


def _signal_group(pid: int, signum: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signum)


class PtySessions:
    """一个工作区的全部伪终端会话。"""

    def __init__(self, *, shell: str, env: dict[str, str]) -> None:
        self._shell = shell
        self._env = env
        self._sessions: dict[str, _Session] = {}

    async def create(self, command: str, *, cwd: Path, cols: int, rows: int) -> str:
        cols, rows = _size(cols, rows)
        if len(self._sessions) >= _MAX_SESSIONS:
            raise ConfigError(
                expected=f'同时开着的终端会话不超过 {_MAX_SESSIONS} 个',
                received=f'已经有 {len(self._sessions)} 个',
                next_action='先用 Terminal 的 kill 结束不再需要的会话，再开新的',
            )
        master, slave = os.openpty()
        try:
            _set_window(slave, cols, rows)
            os.set_blocking(master, False)
            process = await asyncio.create_subprocess_exec(
                self._shell,
                '-c',
                command,
                cwd=str(cwd),
                env=dict(self._env),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
        except BaseException as exc:
            os.close(master)
            if isinstance(exc, OSError):
                raise ConfigError(
                    expected='能为这个会话建伪终端并拉起 shell',
                    received=f'{type(exc).__name__}: {exc}',
                    next_action='多半是文件描述符或进程数用尽：结束不再需要的会话与后台命令再试',
                ) from exc
            raise
        finally:
            os.close(slave)
        session = _Session(process=process, master=master, cols=cols, rows=rows)
        handle = uuid.uuid4().hex
        self._sessions[handle] = session
        asyncio.get_running_loop().add_reader(master, self._readable, session)
        return handle

    def _readable(self, session: _Session) -> None:
        try:
            chunk = os.read(session.master, _CHUNK)
        except BlockingIOError:
            return
        except OSError:
            chunk = b''
        if chunk:
            session.extend(chunk)
            return
        # 从端在子进程那边全关了（macOS 报 EOF，Linux 报 EIO）：不再有输出。
        self._stop_reading(session)

    def _stop_reading(self, session: _Session) -> None:
        if session.reading:
            session.reading = False
            asyncio.get_running_loop().remove_reader(session.master)
            session.arrived.set()

    def _session(self, handle: str) -> _Session:
        session = self._sessions.get(handle)
        if session is None:
            raise ConfigError(
                expected='handle 是本工作区 Terminal create 交回的会话句柄',
                received=f'收到未知 handle={handle!r}（可能已经 kill 掉了）',
                next_action='用 create 交回的 handle；会话 kill 之后要重新 create',
            )
        return session

    async def send(self, handle: str, data: str) -> None:
        session = self._session(handle)
        if session.process.returncode is not None:
            raise ConfigError(
                expected='向仍在运行的会话写输入',
                received=f'会话已经结束，退出码 {session.process.returncode}',
                next_action='用 capture 看它最后的输出；要继续就重新 create',
            )
        payload = data.encode('utf-8')
        while payload:
            try:
                written = os.write(session.master, payload)
            except BlockingIOError:
                await asyncio.sleep(0.01)
                continue
            payload = payload[written:]

    async def capture(self, handle: str, *, since: int, wait: float) -> PtyCapture:
        session = self._session(handle)
        offset, seconds = cast(object, since), cast(object, wait)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ConfigError(
                expected='since 是非负整数（上一次 capture 交回的 end）',
                received=f'since={since!r}',
                next_action='从头看传 0',
            )
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, int | float)
            or not 0 <= seconds <= _MAX_WAIT
        ):
            raise ConfigError(
                expected=f'wait 是 0 到 {_MAX_WAIT} 之间的秒数',
                received=f'wait={wait!r}',
                next_action='不等就传 0；交互式程序一般 1 到 5 秒内有回应',
            )
        if wait and session.end <= since and session.reading:
            # 先清再查再等：标志是边沿，不清就会被很久以前的一次输出立刻叫醒。
            session.arrived.clear()
            if session.end <= since and session.reading:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(wait):
                        await session.arrived.wait()
        text, truncated, end = session.since(since, settled=not session.reading)
        return PtyCapture(
            text=text,
            end=end,
            running=session.process.returncode is None,
            exit_code=session.process.returncode,
            truncated=truncated,
        )

    async def resize(self, handle: str, cols: int, rows: int) -> None:
        session = self._session(handle)
        cols, rows = _size(cols, rows)
        if session.process.returncode is None:
            _set_window(session.master, cols, rows)
            _signal_group(session.process.pid, signal.SIGWINCH)
        session.cols, session.rows = cols, rows

    def status(self, handle: str) -> PtyStatus:
        session = self._session(handle)
        return PtyStatus(
            running=session.process.returncode is None,
            exit_code=session.process.returncode,
            cols=session.cols,
            rows=session.rows,
            end=session.end,
        )

    async def kill(self, handle: str) -> None:
        session = self._session(handle)
        await self._terminate(session)
        del self._sessions[handle]

    async def close(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            await self._terminate(session)

    async def _terminate(self, session: _Session) -> None:
        process = session.process
        if process.returncode is None:
            _signal_group(process.pid, signal.SIGTERM)
            try:
                async with asyncio.timeout(_TERM_GRACE):
                    await process.wait()
            except TimeoutError:
                pass
        # 组长退出了组里也可能还有进程（`cmd &`）：无条件杀整个组，与命令执行同一个判据。
        _signal_group(process.pid, signal.SIGKILL)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(_KILL_GRACE):
                await process.wait()
        self._stop_reading(session)
        with contextlib.suppress(OSError):
            os.close(session.master)
