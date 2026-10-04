"""工作区路由（M5 D2、D3）。

工具共享一份（`catalog.freeze` 绑定的那张表、工具的定义次序与前缀缓存都不变），路由按此刻在驱动
的 Agent 把调用分到它的工作区，并在这一层做乐观并发：一次不带版本前提的写，前提是这个 Agent 上
一次看到的版本。后端只管它自己那份目录，不知道有几个 Agent。
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from typing import cast

from syndica.kernel.driving import driving_agent
from syndica.kernel.errors import ConfigError
from syndica.kernel.versions import VersionStore
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

logger = logging.getLogger(__name__)

_Seen = tuple[str | None, str]


def _version(data: bytes) -> str:
    return f'sha256:{hashlib.sha256(data).hexdigest()}'


class WorkspaceRouter:
    """一个 Runtime 的工作区路由。它自己就是 `Portal`，交给工具的就是它。

    「看到的版本」按（Agent, 路径）记：读到的、写成的都算。没看过就覆盖一个已存在的文件是冲突——
    两个都没读过就各写一遍时，后写的那个看不见先写的内容，只有这样才拦得住。宿主代码（不在任何
    Agent 的驱动任务里）共用一份记录。
    """

    def __init__(self, root: Workspace, *, versions: VersionStore | None = None) -> None:
        self._root = root
        self._assigned: dict[str, Workspace] = {}
        self._seen: dict[_Seen, str | None] = {}
        self._versions = versions

    @property
    def root(self) -> str:
        return self._root.root

    @property
    def versions(self) -> VersionStore | None:
        """版本层；没打开时是 None。只记根工作区上经路由的写入：隔离副本里的写在合并回来时才记。"""
        return self._versions

    async def restore(self, path: str, version: str) -> str:
        """把根工作区里的 `path` 写回版本层里的 `version`，不带版本前提（这是宿主明确要的覆盖）。
        写回本身记一个新版本，交回它。"""
        if self._versions is None:
            raise ConfigError(
                expected='路由打开了版本层（WorkspaceRouter(..., versions=VersionStore(...))）',
                received='这个路由没有版本层',
                next_action='构造路由时传入 versions，或在 .syndica.yaml 里打开 workspace.versions',
            )
        data = self._versions.content(path, version)
        written = await self._root.portal().write_bytes(path, data)
        self._seen[self._key(self._root, path)] = written
        self._record(self._root, path, data)
        return written

    def _record(self, workspace: Workspace, path: str, data: bytes | None) -> None:
        if self._versions is None or workspace is not self._root:
            return
        relative = self._key(workspace, path)[1]
        producer = driving_agent() or 'host'
        try:
            if data is None:
                self._versions.record_removal(relative, producer=producer)
            else:
                self._versions.record(relative, data, producer=producer)
        except (OSError, ConfigError) as exc:
            # 写已经落了，不能因为没记下版本就报写失败；没记下的这一版不能回滚，必须留下证据。
            logger.error('版本层没有记下 %s 的这次改动（%s）：这一版不能回滚', relative, exc)

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
        write: Callable[[Portal, Expected], Awaitable[str]],
    ) -> str:
        workspace = self._target()
        portal = workspace.portal()
        key = self._key(workspace, path)
        if expected is ANY:
            expected = await self._precondition(portal, path, key)
        await write(portal, expected)
        version = _version(data)
        self._seen[key] = version
        self._record(workspace, path, data)
        return version

    async def read_text(self, path: str, *, offset: int, limit: int) -> str:
        text, _ = await self.read_versioned(path, offset=offset, limit=limit)
        return text

    async def read_versioned(self, path: str, *, offset: int, limit: int) -> tuple[str, str]:
        workspace = self._target()
        portal = workspace.portal()
        key = self._key(workspace, path)
        try:
            text, version = await portal.read_versioned(path, offset=offset, limit=limit)
        except ConfigError:
            # 读不出来也算看过：文件不在了就记「看到它不存在」，读不了（太大、不是 utf-8）就记
            # 它此刻的版本。否则冲突消息里「先读它」那一步永远做不成，这个文件此后只有 Bash 写得动。
            current = await self._quiet_stat(portal, path)
            self._seen[key] = None if current is None else current.version
            raise
        self._seen[key] = version
        return text, version

    async def read_bytes(self, path: str) -> bytes:
        workspace = self._target()
        data = await workspace.portal().read_bytes(path)
        self._seen[self._key(workspace, path)] = _version(data)
        return data

    async def write_text(self, path: str, content: str, *, expected: Expected = ANY) -> str:
        return await self._write(
            path,
            content.encode('utf-8'),
            expected,
            lambda portal, given: portal.write_text(path, content, expected=given),
        )

    async def write_bytes(self, path: str, data: bytes, *, expected: Expected = ANY) -> str:
        if not isinstance(cast(object, data), bytes | bytearray):
            raise ConfigError(
                expected='data 是 bytes',
                received=f'data 的类型是 {type(data).__name__}',
                next_action='写文本用 write_text；写二进制先编码成 bytes',
            )
        return await self._write(
            path,
            bytes(data),
            expected,
            lambda portal, given: portal.write_bytes(path, data, expected=given),
        )

    async def edit_text(self, path: str, old: str, new: str, *, expected: Expected = ANY) -> str:
        workspace = self._target()
        portal = workspace.portal()
        key = self._key(workspace, path)
        data = await portal.read_bytes(path)
        if expected is ANY:
            # 没读过就按此刻的内容改：`old` 恰好出现一次本身就是对内容的前提。
            expected = self._seen[key] if key in self._seen else _version(data)
        read_before = key in self._seen
        version = await portal.edit_text(path, old, new, expected=expected)
        # 后端按同一个版本前提改的就是 `data` 时，改完的内容能在这里算出来，不必再读一次（再读会
        # 撞上别人紧接着的写）；算出来的与后端交回的版本对不上，就说明改的不是这一份，不记。
        text = data.decode('utf-8', errors='surrogateescape')
        edited = text.replace(old, new, 1).encode('utf-8', errors='surrogateescape')
        if _version(edited) == version:
            self._record(workspace, path, edited)
        elif self._versions is not None and workspace is self._root:
            logger.error(
                '版本层没有记下 %s 的这次 Edit：改的不是读到的那一份，这一版不能回滚', path
            )
        if read_before and expected == _version(data):
            self._seen[key] = version
        else:
            # 没读过就改：`old` 恰好出现一次只证明它见过那一段，没见过文件的其余部分。记成看过，
            # 接下来的一次整篇覆盖就会盖掉它没看过的内容。
            self._seen.pop(key, None)
        return version

    async def remove(self, path: str, *, expected: Expected = ANY) -> None:
        workspace = self._target()
        portal = workspace.portal()
        key = self._key(workspace, path)
        if expected is ANY:
            expected = await self._precondition(portal, path, key)
        await portal.remove(path, expected=expected)
        self._seen[key] = None
        self._record(workspace, path, None)

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
