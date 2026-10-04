"""工作区与 Portal（spec §6.1）。

工具只调 Portal，不碰磁盘。磁盘与子进程只在 `LocalWorkspace`。
`ExecResult` 与 `FileStat` 的字段顺序是公开契约，新字段只能追加。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol

__all__ = ['ANY', 'Expected', 'ExecResult', 'FileStat', 'Portal', 'Workspace', 'WriteConflict']


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


class WriteConflict(Exception):
    """带版本前提的写入时，文件已经不是调用方以为的那个版本。

    它不是调用方用错了（那是 `ConfigError`），而是工作区在调用方上次看过之后变了：工具把它写成
    可恢复的结果交给模型，由模型决定重读合并还是换个地方写。`current` 是 None 时文件此刻不存在。
    """

    def __init__(self, path: str, expected: str | None, current: str | None) -> None:
        self.path = path
        self.expected = expected
        self.current = current
        wanted = '不存在' if expected is None else f'版本是 {expected}'
        found = '不存在' if current is None else f'版本是 {current}'
        super().__init__(
            f'期望: {path} {wanted}（写入的前提）。实际: 它此刻{found}，在那之后被改过。'
            ' 下一步: 重新读它，在最新的内容上再改；或者改写到另一个路径。'
        )


class Portal(Protocol):
    """工作区上的文件与进程操作。路径越界由实现拒绝，工具把拒绝写成给模型的 output。

    带 `expected` 的写入是一次原子的「检查版本再写」：两个调用方拿着同一个版本前提并发写，只有
    一个成功，另一个收到 `WriteConflict`。
    """

    async def read_text(self, path: str, *, offset: int, limit: int) -> str: ...

    async def write_text(self, path: str, content: str, *, expected: Expected = ANY) -> None: ...

    async def edit_text(
        self, path: str, old: str, new: str, *, expected: Expected = ANY
    ) -> None: ...

    async def read_bytes(self, path: str) -> bytes: ...

    async def write_bytes(self, path: str, data: bytes, *, expected: Expected = ANY) -> None: ...

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

    async def close(self) -> None: ...


class Workspace(Protocol):
    """宿主交给运行时的工作区。`root` 是绝对路径。"""

    @property
    def root(self) -> str: ...

    def portal(self) -> Portal: ...
