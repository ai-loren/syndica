"""工作区与 Portal（spec §6.1）。

工具只调 Portal，不碰磁盘。磁盘与子进程只在 `LocalWorkspace`。
`ExecResult` 字段顺序是公开契约，新字段只能追加。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ['ExecResult', 'Portal', 'Workspace']


@dataclass(frozen=True)
class ExecResult:
    """一次命令的结果。后台仍在跑时 `exit_code is None` 且 `running is True`。

    超时的前台命令不抛错：`timed_out is True`、`exit_code is None`，两条流是终止前已经收到的部分。
    """

    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    running: bool
    timed_out: bool = False


class Portal(Protocol):
    """工作区上的文件与进程操作。路径越界由实现拒绝，工具把拒绝写成给模型的 output。"""

    async def read_text(self, path: str, *, offset: int, limit: int) -> str: ...

    async def write_text(self, path: str, content: str) -> None: ...

    async def edit_text(self, path: str, old: str, new: str) -> None: ...

    async def glob_paths(self, pattern: str) -> tuple[str, ...]: ...

    async def grep_text(self, pattern: str, path: str) -> tuple[str, ...]: ...

    async def run(self, command: str, *, timeout: float, cwd: str) -> ExecResult:
        """前台运行；超时返回 `timed_out=True` 的结果，不抛错。"""
        ...

    async def start(self, command: str, *, cwd: str) -> str: ...

    async def poll(self, handle: str) -> ExecResult: ...

    async def kill(self, handle: str) -> None: ...

    async def close(self) -> None: ...


class Workspace(Protocol):
    """宿主交给运行时的工作区。`root` 是绝对路径。"""

    @property
    def root(self) -> str: ...

    def portal(self) -> Portal: ...
