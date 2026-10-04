"""工作区路由（M5 D2、D3）。

工具共享一份（`catalog.freeze` 绑定的那张表、工具的定义次序与前缀缓存都不变），路由按此刻在驱动
的 Agent 把调用分到它的工作区，并在这一层做乐观并发：一次不带版本前提的写，前提是这个 Agent 上
一次看到的版本。后端只管它自己那份目录，不知道有几个 Agent。
"""

from __future__ import annotations

import hashlib
import posixpath
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath

from syndica.kernel.driving import driving_agent
from syndica.kernel.errors import ConfigError
from syndica.kernel.workspace import (
    ANY,
    ExecResult,
    Expected,
    FileStat,
    Portal,
    PtyCapture,
    PtyStatus,
    Workspace,
    WriteConflict,
)

__all__ = ['WorkspaceRouter']

_Seen = tuple[str | None, str]


def _version(data: bytes) -> str:
    return f'sha256:{hashlib.sha256(data).hexdigest()}'


class WorkspaceRouter:
    """一个 Runtime 的工作区路由。它自己就是 `Portal`，交给工具的就是它。

    「看到的版本」按（Agent, 路径）记：读到的、写成的都算。没看过就覆盖一个已存在的文件是冲突——
    两个都没读过就各写一遍时，后写的那个看不见先写的内容，只有这样才拦得住。宿主代码（不在任何
    Agent 的驱动任务里）共用一份记录。
    """

    def __init__(self, root: Workspace) -> None:
        self._root = root
        self._assigned: dict[str, Workspace] = {}
        self._seen: dict[_Seen, str | None] = {}

    @property
    def root(self) -> str:
        return self._root.root

    def portal(self) -> Portal:
        return self

    def _target(self) -> Workspace:
        agent = driving_agent()
        assigned = None if agent is None else self._assigned.get(agent)
        return self._root if assigned is None else assigned

    def _key(self, workspace: Workspace, path: str) -> _Seen:
        raw = PurePosixPath(path)
        if raw.is_absolute():
            try:
                raw = raw.relative_to(workspace.root)
            except ValueError:
                pass
        return driving_agent(), posixpath.normpath(str(raw))

    async def _quiet_stat(self, portal: Portal, path: str) -> FileStat | None:
        try:
            return await portal.stat(path)
        except ConfigError:
            return None

    async def _precondition(self, portal: Portal, path: str, key: _Seen) -> str | None:
        if key in self._seen:
            return self._seen[key]
        current = await portal.stat(path)
        if current is None:
            return None
        raise WriteConflict(path, None, current.version, unread=True)

    async def _write(
        self,
        path: str,
        data: bytes,
        expected: Expected,
        write: Callable[[Portal, Expected], Awaitable[None]],
    ) -> None:
        workspace = self._target()
        portal = workspace.portal()
        key = self._key(workspace, path)
        if expected is ANY:
            expected = await self._precondition(portal, path, key)
        await write(portal, expected)
        self._seen[key] = _version(data)

    async def read_text(self, path: str, *, offset: int, limit: int) -> str:
        workspace = self._target()
        portal = workspace.portal()
        before = await self._quiet_stat(portal, path)
        text = await portal.read_text(path, offset=offset, limit=limit)
        after = await self._quiet_stat(portal, path)
        key = self._key(workspace, path)
        # 读的前后版本一致才算看到了它：中间有人写过时记下来的版本不是读到的那份。
        if before is not None and after is not None and before.version == after.version:
            self._seen[key] = after.version
        else:
            self._seen.pop(key, None)
        return text

    async def read_bytes(self, path: str) -> bytes:
        workspace = self._target()
        data = await workspace.portal().read_bytes(path)
        self._seen[self._key(workspace, path)] = _version(data)
        return data

    async def write_text(self, path: str, content: str, *, expected: Expected = ANY) -> None:
        await self._write(
            path,
            content.encode('utf-8'),
            expected,
            lambda portal, given: portal.write_text(path, content, expected=given),
        )

    async def write_bytes(self, path: str, data: bytes, *, expected: Expected = ANY) -> None:
        await self._write(
            path,
            bytes(data),
            expected,
            lambda portal, given: portal.write_bytes(path, data, expected=given),
        )

    async def edit_text(self, path: str, old: str, new: str, *, expected: Expected = ANY) -> None:
        workspace = self._target()
        portal = workspace.portal()
        key = self._key(workspace, path)
        data = await portal.read_bytes(path)
        if expected is ANY:
            # 没读过就按此刻的内容改：`old` 恰好出现一次本身就是对内容的前提。
            expected = self._seen[key] if key in self._seen else _version(data)
        await portal.edit_text(path, old, new, expected=expected)
        if expected == _version(data):
            # 后端按同一个版本前提改的就是 `data`，新版本于是能在这里算出来，不必再读一次（再读会
            # 撞上别人紧接着的写）。
            self._seen[key] = _version(data.decode('utf-8').replace(old, new, 1).encode('utf-8'))
        else:
            self._seen.pop(key, None)

    async def remove(self, path: str, *, expected: Expected = ANY) -> None:
        workspace = self._target()
        portal = workspace.portal()
        key = self._key(workspace, path)
        if expected is ANY:
            expected = await self._precondition(portal, path, key)
        await portal.remove(path, expected=expected)
        self._seen[key] = None

    async def stat(self, path: str) -> FileStat | None:
        return await self._target().portal().stat(path)

    async def glob_paths(self, pattern: str) -> tuple[str, ...]:
        return await self._target().portal().glob_paths(pattern)

    async def grep_text(self, pattern: str, path: str) -> tuple[str, ...]:
        return await self._target().portal().grep_text(pattern, path)

    async def run(self, command: str, *, timeout: float, cwd: str) -> ExecResult:
        return await self._target().portal().run(command, timeout=timeout, cwd=cwd)

    async def start(self, command: str, *, cwd: str) -> str:
        return await self._target().portal().start(command, cwd=cwd)

    async def poll(self, handle: str, *, since: tuple[int, int] = (0, 0)) -> ExecResult:
        return await self._target().portal().poll(handle, since=since)

    async def kill(self, handle: str) -> None:
        await self._target().portal().kill(handle)

    async def pty_create(self, command: str, *, cwd: str, cols: int = 120, rows: int = 40) -> str:
        return await self._target().portal().pty_create(command, cwd=cwd, cols=cols, rows=rows)

    async def pty_send(self, handle: str, data: str) -> None:
        await self._target().portal().pty_send(handle, data)

    async def pty_capture(self, handle: str, *, since: int = 0, wait: float = 0.0) -> PtyCapture:
        return await self._target().portal().pty_capture(handle, since=since, wait=wait)

    async def pty_resize(self, handle: str, *, cols: int, rows: int) -> None:
        await self._target().portal().pty_resize(handle, cols=cols, rows=rows)

    async def pty_kill(self, handle: str) -> None:
        await self._target().portal().pty_kill(handle)

    async def pty_status(self, handle: str) -> PtyStatus:
        return await self._target().portal().pty_status(handle)

    async def close(self) -> None:
        await self._root.portal().close()
