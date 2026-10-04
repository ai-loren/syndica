"""工作区与 Portal（spec §6.1）。

工具只调 Portal，不碰磁盘。磁盘与子进程只在 `LocalWorkspace`。
`ExecResult` 与 `FileStat` 的字段顺序是公开契约，新字段只能追加。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol

__all__ = [
    'ANY',
    'ExecResult',
    'Expected',
    'FileStat',
    'Portal',
    'PtyCapture',
    'PtyStatus',
    'Workspace',
    'WriteConflict',
]


class _Any(Enum):
    ANY = 'any'


ANY: Final = _Any.ANY
"""写入不检查版本。`None` 是「要求文件不存在」，字符串是「要求当前版本等于它」。"""

Expected = _Any | str | None


@dataclass(frozen=True)
class ExecResult:
    """一次命令的结果。后台仍在跑时 `exit_code is None` 且 `running is True`。

    超时的前台命令不抛错：`timed_out is True`、`exit_code is None`，两条流是终止前已经收到的部分。
    `stdout_end` / `stderr_end` 是这两条流到目前为止收到的字节数：下一次 `poll(since=...)` 交回它们
    就只拿到此后的新输出。
    """

    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    running: bool
    timed_out: bool = False
    stdout_end: int = 0
    stderr_end: int = 0


@dataclass(frozen=True)
class FileStat:
    """一个文件此刻的版本与大小。`version` 是内容摘要（`sha256:<hex>`）：同样的内容同样的版本，
    只做相等比较，不解析它的结构。"""

    version: str
    size: int


@dataclass(frozen=True)
class PtyCapture:
    """一次 `pty_capture` 交回的输出。`text` 去掉了终端控制序列，是输出流而不是渲染后的屏幕；
    `end` 交回下一次的 `since`；`truncated` 是这一段里有没有内核没保留的字节。"""

    text: str
    end: int
    running: bool
    exit_code: int | None
    truncated: bool


@dataclass(frozen=True)
class PtyStatus:
    """会话此刻的状态，不带输出。`end` 是到目前为止收到的字节数。"""

    running: bool
    exit_code: int | None
    cols: int
    rows: int
    end: int


class WriteConflict(Exception):
    """带版本前提的写入时，文件已经不是调用方以为的那个版本。

    它不是调用方用错了（那是 `ConfigError`），而是工作区在调用方上次看过之后变了：工具把它写成
    可恢复的结果交给模型，由模型决定重读合并还是换个地方写。`current` 是 None 时文件此刻不存在。
    """

    def __init__(
        self, path: str, expected: str | None, current: str | None, *, unread: bool = False
    ) -> None:
        self.path = path
        self.expected = expected
        self.current = current
        self.unread = unread
        if unread:
            message = (
                f'期望: 改动 {path}（覆盖或删除）之前读过它。实际: 它已经存在（版本 {current}），'
                '而你还没读过它，改下去就会丢掉你没看过的内容。'
                ' 下一步: 先用 Read 读它（读不出来的大文件或二进制文件，Read 一次也算看过），'
                '在最新的内容上再改；或者写到一个新路径。'
            )
        else:
            wanted = '不存在' if expected is None else f'版本是 {expected}'
            found = '不存在' if current is None else f'版本是 {current}'
            message = (
                f'期望: {path} {wanted}（写入的前提）。实际: 它此刻{found}，在那之后被改过。'
                ' 下一步: 重新读它，在最新的内容上再改；或者改写到另一个路径。'
            )
        super().__init__(message)


class Portal(Protocol):
    """工作区上的文件与进程操作。路径越界由实现拒绝，工具把拒绝写成给模型的 output。

    带 `expected` 的写入是一次原子的「检查版本再写」：两个调用方拿着同一个版本前提并发写，只有
    一个成功，另一个收到 `WriteConflict`。
    """

    async def read_text(self, path: str, *, offset: int, limit: int) -> str: ...

    async def read_versioned(self, path: str, *, offset: int, limit: int) -> tuple[str, str]:
        """`read_text` 加上读到的那份内容的版本。两者出自同一次读：分两步（先 `stat` 再读）
        时中间插进来的一次写，会让调用方以为自己看过它没看过的内容。"""
        ...

    async def write_text(self, path: str, content: str, *, expected: Expected = ANY) -> str:
        """写成之后交回写下的那份内容的版本：写完再 `stat` 取到的可能已经是别人紧接着的写。"""
        ...

    async def edit_text(self, path: str, old: str, new: str, *, expected: Expected = ANY) -> str:
        """同 `write_text`，交回改完之后的版本。"""
        ...

    async def read_bytes(self, path: str) -> bytes: ...

    async def write_bytes(self, path: str, data: bytes, *, expected: Expected = ANY) -> str:
        """同 `write_text`，交回写下的版本。"""
        ...

    async def remove(self, path: str, *, expected: Expected = ANY) -> None: ...

    async def stat(self, path: str) -> FileStat | None:
        """文件的版本与大小；不存在时是 None，存在但不是文件时拒绝。"""
        ...

    async def glob_paths(self, pattern: str) -> tuple[str, ...]: ...

    async def grep_text(self, pattern: str, path: str) -> tuple[str, ...]: ...

    async def run(self, command: str, *, timeout: float, cwd: str) -> ExecResult:
        """前台运行；超时返回 `timed_out=True` 的结果，不抛错。"""
        ...

    async def start(self, command: str, *, cwd: str) -> str: ...

    async def poll(self, handle: str, *, since: tuple[int, int] = (0, 0)) -> ExecResult:
        """后台命令的状态与输出。`since` 是上一次结果里的 (`stdout_end`, `stderr_end`)：只交回此后的
        输出。落在实现丢弃过的那一段里时从还留着的部分接着给，并把 `truncated` 置真。"""
        ...

    async def kill(self, handle: str) -> None: ...

    async def pty_create(self, command: str, *, cwd: str, cols: int = 120, rows: int = 40) -> str:
        """在伪终端里起一个会话，交回句柄。会话没有总时长上限；`pty_kill` 或 `close` 结束它。"""
        ...

    async def pty_send(self, handle: str, data: str) -> None: ...

    async def pty_capture(self, handle: str, *, since: int = 0, wait: float = 0.0) -> PtyCapture:
        """`since` 之后的输出；没有新输出时最多等 `wait` 秒，到点交回空文字而不是抛错。"""
        ...

    async def pty_resize(self, handle: str, *, cols: int, rows: int) -> None: ...

    async def pty_kill(self, handle: str) -> None:
        """结束会话的整个进程组并收掉它。之后这个句柄不再可用。"""
        ...

    async def pty_status(self, handle: str) -> PtyStatus: ...

    async def close(self) -> None: ...


class Workspace(Protocol):
    """宿主交给运行时的工作区。`root` 是绝对路径。"""

    @property
    def root(self) -> str: ...

    def portal(self) -> Portal: ...
