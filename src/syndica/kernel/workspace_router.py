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
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

from syndica.kernel.context import TrustLevel, call_trust, strictest_trust
from syndica.kernel.driving import driving_agent
from syndica.kernel.errors import ConfigError
from syndica.kernel.file_taint import FileTaint
from syndica.kernel.isolation import ISOLATIONS, IsolatedCopy, LocalIsolator, MergeReport
from syndica.kernel.tracing import current_span_id
from syndica.kernel.versions import VersionStore
from syndica.kernel.workspace import (
    ANY,
    ExecResult,
    Expected,
    FileStat,
    GrepHit,
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

    def __init__(
        self,
        root: Workspace,
        *,
        versions: VersionStore | None = None,
        isolator: LocalIsolator | None = None,
        taint: FileTaint | None = None,
    ) -> None:
        self._root = root
        self._assigned: dict[str, Workspace] = {}
        self._seen: dict[_Seen, str | None] = {}
        self._versions = versions
        self._isolator = isolator
        self._copies: dict[str, IsolatedCopy] = {}
        # 谁派生了谁：没有自己副本的 Agent 落在最近一个有副本的祖先那里，隔离于是可以嵌套。
        self._parents: dict[str, str] = {}
        # 每份副本是从谁的工作区拷的（空串是根，否则是那份副本的主人），合并就回到那里。记主人而不是
        # 工作区对象：主人的副本会被合并删掉、也会在合并失败后换一个重开的工作区，记对象就会合并进
        # 一个已经删掉或关掉的工作区，报告成功、一个字也没写进去。
        self._base_owners: dict[str, str] = {}
        self._merging: set[str] = set()
        self._taint = taint
        self._copy_taints: dict[str, FileTaint] = {}
        # （工作区, 路径, 版本）→ 写下它的那次工具调用的 span：读到它时连一条 artifact 边。
        self._writers: dict[tuple[int, str, str], str] = {}
        self._tracing: _ArtifactTracing | None = None
        # 不在 `tool.execute` 里（hook、`on_start` 与它们起的任务）发生的写，按正在干活的那段代码的
        # 身份定信任：没有属主是宿主、按可信算；被收紧的 Module 按不可信（评审 F6）。Runtime 接它。
        self._write_floor: Callable[[], TrustLevel] | None = None

    def _attach_tracing(self, tracing: _ArtifactTracing) -> None:
        self._tracing = tracing

    def _attach_write_floor(self, floor: Callable[[], TrustLevel]) -> None:
        self._write_floor = floor

    def file_trust(self, path: str, version: str | None) -> TrustLevel:
        """此刻在驱动的 Agent 的工作区里，`path` 内容是 `version`（None 时按此刻的内容）可信吗。
        没有文件污点时一律可信（与没有路由时相同）。"""
        taint = self._taint_of(self._target())
        return TrustLevel.TRUSTED if taint is None else taint.trust_of(path, version)

    def name_trust(self, path: str) -> TrustLevel:
        """列出来的这个路径名可信吗（`Glob` / `Grep` 按它给结果定信任级别）。名字与内容分开判：指向
        基线文件的新建符号链接，内容可信、名字不可信（评审 C2）。没有文件污点时一律可信。"""
        taint = self._taint_of(self._target())
        return TrustLevel.TRUSTED if taint is None else taint.name_trust(path)

    def script_trust(self, path: str) -> TrustLevel | None:
        """执行 `path` 这个脚本时它可信吗；不在工作区里（宿主的程序、工作区外的路径）交回 None，
        没有文件污点也交回 None：都不该让执行它的命令升级（评审 F3）。"""
        taint = self._taint_of(self._target())
        return None if taint is None else taint.script_trust(path)

    def _taint_of(self, workspace: Workspace) -> FileTaint | None:
        if workspace is self._root:
            return self._taint
        for agent_id, copy in self._copies.items():
            if copy.workspace is workspace:
                return self._copy_taints.get(agent_id)
        return None

    def _wrote(self, workspace: Workspace, path: str, version: str) -> None:
        """一次经路由的写落了：版本记成这次调用的溯源结论（宿主代码写的按可信），并记下写它的
        span。"""
        relative = self._key(workspace, path)[1]
        taint = self._taint_of(workspace)
        if taint is not None:
            taint.wrote(relative, version, self._write_trust())
        span = current_span_id()
        if span is not None:
            self._writers[(id(workspace), relative, version)] = span

    def _write_trust(self) -> TrustLevel:
        """这次写该记成什么信任：取「这次工具调用的溯源结论」与「正在干活的代码的身份下界」里更严的
        那个。身份下界（`_write_floor`）同样要作用在 `tool.execute` 之内——被收紧的 Module 的 hook
        在一次可信工具的执行窗口里写文件时，`call_trust()` 是那次可信调用的值，只看它就把 Module 的
        内容记成可信（评审 C6）。可信工具自己的写在 `acting_as(None)` 下身份下界可信，不受影响。"""
        call = call_trust()
        floor = self._write_floor() if self._write_floor is not None else TrustLevel.TRUSTED
        if call is None:
            return floor
        return strictest_trust(call, floor)

    def _read(self, workspace: Workspace, path: str, version: str) -> None:
        if self._tracing is None:
            return
        relative = self._key(workspace, path)[1]
        taint = self._taint_of(workspace)
        trust = TrustLevel.TRUSTED if taint is None else taint.trust_of(relative, version)
        writer = self._writers.get((id(workspace), relative, version))
        if writer is not None:
            self._tracing.artifact(writer, relative, version, trust.value)
        elif taint is not None:
            self._tracing.provenance(taint.source_of(relative, version), trust.value)

    @property
    def can_isolate(self) -> bool:
        return self._isolator is not None

    def isolate(self, agent_id: str, isolation: str) -> IsolatedCopy | None:
        """给 `agent_id` 建隔离副本并把它此后的调用路由过去；`shared` 什么都不做、交回 None。

        同步：副本必须在这个 Agent 的第一个周期之前就位，而 `spawn` 在那之前不能有挂起点。它驱动
        的任务里另起的任务继承驱动身份，于是同样落在这份副本上。"""
        if isolation == 'shared':
            return None
        if isolation not in ISOLATIONS:
            raise ConfigError(
                expected=f'isolation 是 {" / ".join(ISOLATIONS)} 之一',
                received=f'isolation={isolation!r}',
                next_action='改成上面三者之一；不要隔离就用 shared',
            )
        if self._isolator is None:
            raise ConfigError(
                expected='路由带着隔离器（WorkspaceRouter(..., isolator=LocalIsolator(...))）',
                received=f'这个路由不能隔离，却收到 isolation={isolation!r}',
                next_action='构造路由时传入 isolator，或者用 isolation=shared',
            )
        if agent_id in self._copies:
            raise ConfigError(
                expected=f'agent {agent_id} 还没有隔离副本',
                received='它已经有一份',
                next_action='先 merge_isolated 或 discard_isolated 那一份',
            )
        parent = self._parents.get(agent_id)
        base = self._workspace_of(parent)
        copy = self._isolator.isolate(
            Path(base.root), agent_id, isolation, under=Path(self._root.root)
        )
        self._copies[agent_id] = copy
        self._base_owners[agent_id] = '' if parent is None else self.scope_of(parent)
        self._assigned[agent_id] = copy.workspace
        base_taint = self._taint_of(base)
        if base_taint is not None:
            # 副本里的每个文件带着父那边此刻的可信度：从副本读到一份不可信的文件，仍是不可信的。
            copied = FileTaint(copy.location, baseline=False)
            for path, version in copy.base.items():
                copied.wrote(path, version, base_taint.trust_of(path, version))
            self._copy_taints[agent_id] = copied
        return copy

    def adopt(self, agent_id: str, parent_id: str | None) -> None:
        """记下 `agent_id` 由 `parent_id` 派生：它没有自己的副本时，落在父（或更上面的祖先）的副本
        里，而不是直接写共享工作区。"""
        if parent_id is not None:
            self._parents[agent_id] = parent_id

    def isolated(self, agent_id: str) -> IsolatedCopy | None:
        return self._copies.get(agent_id)

    def based_on(self, agent_id: str) -> list[str]:
        """隔离副本建在 `agent_id` 那份副本上、还没合并的那些 Agent：在它们合并回来之前删掉这份
        副本，它们的改动就合并不到任何地方。"""
        return [other for other, owner in self._base_owners.items() if owner == agent_id]

    def _base_of(self, agent_id: str) -> Workspace:
        owner = self._base_owners.get(agent_id, '')
        if not owner:
            return self._root
        base = self._copies.get(owner)
        if base is None:
            raise ConfigError(
                expected=f'agent {agent_id} 的隔离副本合并回它出发的那份副本（agent {owner} 的）',
                received='那份副本已经合并或丢弃，不在了',
                next_action=(
                    f'agent {agent_id} 的改动还在它自己的副本里'
                    f'（{self._copies[agent_id].location}）；'
                    '手动拿走需要的文件，再 discard_isolated 它'
                ),
            )
        return base.workspace

    def _reopen(self, agent_id: str, copy: IsolatedCopy) -> None:
        old = id(copy.workspace)
        cast(LocalIsolator, self._isolator).reopen(copy)
        self._assigned[agent_id] = copy.workspace
        self._writers = {key: span for key, span in self._writers.items() if key[0] != old}

    async def merge_isolated(self, agent_id: str) -> MergeReport:
        """把 `agent_id` 的隔离副本逐文件合并回根工作区，然后删掉副本；此后它的调用落在根上。
        合并写回的每个文件在版本层里记在这个 Agent 名下。"""
        copy = self._copies.get(agent_id)
        if copy is None or self._isolator is None:
            raise ConfigError(
                expected=f'agent {agent_id} 有一份隔离副本',
                received='它没有（没隔离过，或已经合并、丢弃）',
                next_action='只对用 isolation=worktree / snapshot 起来、还没合并的 Agent 合并',
            )
        self._refuse_with_dependents(agent_id, 'merge_isolated')
        base = self._base_of(agent_id)
        base_taint = self._taint_of(base)
        copied = self._copy_taints.get(agent_id)

        def written(target: str, data: bytes | None, source: str, mixed: bool) -> None:
            self._record(base, target, data, producer=agent_id)
            if data is None or base_taint is None:
                return
            trust = TrustLevel.UNTRUSTED if copied is None else copied.trust_of(source, None)
            if mixed:
                trust = strictest_trust(trust, before.get(source, TrustLevel.UNTRUSTED))
            base_taint.wrote(target, _version(data), trust)

        # 合并期间这个 Agent（与落在它副本里的后代）的调用一律被拒，副本里的进程先关掉：合并读的
        # 是副本此刻的样子，之后再落进副本的改动会随副本一起被删。
        self._merging.add(agent_id)
        try:
            await copy.workspace.close()
            before = (
                {}
                if base_taint is None
                else {path: base_taint.trust_of(path, None) for path in copy.changed()}
            )
            report = await self._isolator.merge(copy, base.portal(), written)
            late = copy.changed_since_merge()
        except BaseException:
            # 副本留着，下次接着在它里面干活：关掉的工作区得重开，否则此后每条命令都报「已关闭」。
            self._reopen(agent_id, copy)
            raise
        finally:
            self._merging.discard(agent_id)
        if late:
            logger.warning(
                'agent %s 的隔离副本在合并开始之后又改了 %d 个文件，这些改动没有合并：%s',
                agent_id,
                len(late),
                late,
            )
        if report.unsaved:
            # 有冲突的文件子 Agent 那份没能另存：删掉副本就是丢掉它唯一的一份。
            self._reopen(agent_id, copy)
            return replace(report, kept=str(copy.location))
        await self._drop_copy(agent_id, unmerged_ok=True)
        return report

    async def discard_isolated(self, agent_id: str) -> None:
        """丢掉 `agent_id` 的隔离副本（关掉它在副本里的进程）；没合并的改动会记一条警告。"""
        self._refuse_with_dependents(agent_id, 'discard_isolated')
        await self._drop_copy(agent_id, unmerged_ok=False)

    def _refuse_with_dependents(self, agent_id: str, operation: str) -> None:
        dependents = self.based_on(agent_id)
        if dependents:
            raise ConfigError(
                expected=f'{operation}({agent_id}) 时没有别的副本还建在它的副本上',
                received=(
                    f'agent {"、".join(dependents)} 的隔离副本是从它的副本拷出去的、还没合并回来；'
                    '现在合并或丢掉它，它们的改动就合并不到任何地方'
                ),
                next_action=(
                    f'先合并或丢掉 {"、".join(dependents)} 的副本，再对 {agent_id} {operation}'
                ),
            )

    async def release(self, agent_id: str) -> None:
        """Agent 不在了：丢掉它的副本，连同它在路由里记下的看过的版本与父子关系。"""
        await self._drop_copy(agent_id, unmerged_ok=False)
        self._parents.pop(agent_id, None)
        for key in [key for key in self._seen if key[0] == agent_id]:
            del self._seen[key]

    async def _drop_copy(self, agent_id: str, *, unmerged_ok: bool) -> None:
        copy = self._copies.pop(agent_id, None)
        self._assigned.pop(agent_id, None)
        self._copy_taints.pop(agent_id, None)
        self._base_owners.pop(agent_id, None)
        if copy is None:
            return
        self._writers = {
            key: span for key, span in self._writers.items() if key[0] != id(copy.workspace)
        }
        if not unmerged_ok:
            lost = copy.changed()
            if lost:
                logger.warning(
                    'agent %s 的隔离副本没合并就丢掉了，%d 个文件的改动随之丢失：%s',
                    agent_id,
                    len(lost),
                    lost,
                )
        await copy.discard()

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
        trust = TrustLevel.TRUSTED if self._taint is None else self._taint.trust_of(path, version)
        written = await self._root.portal().write_bytes(path, data)
        self._seen[self._key(self._root, path)] = written
        self._record(self._root, path, data)
        if self._taint is not None:
            # 回滚写回的内容可信与否取决于那个版本当初是怎么来的，不取决于谁调了 restore。
            self._taint.wrote(path, written, trust)
        return written

    def _record(
        self, workspace: Workspace, path: str, data: bytes | None, *, producer: str | None = None
    ) -> None:
        if self._versions is None or workspace is not self._root:
            return
        relative = self._key(workspace, path)[1]
        producer = producer or driving_agent() or 'host'
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

    def reader_for(self, agent_id: str) -> Portal:
        """伴生 Agent 用的只读视图（M7 评审 B2）：读 `agent_id` 那份工作区（含隔离副本），**不记**读
        基线（`_seen`）与溯源边。伴生在主 Agent 的驱动任务里跑，经路由读会记在主 Agent 名下、刷新它
        的写前提，于是主 Agent 下一次整篇覆盖就把别人的改动静默盖掉（乐观并发被绕过）。信任照算——
        Read / Grep / Glob 靠它给读到的内容定信任级别；写、命令、终端一律拒绝。"""
        return _ReadOnlyView(self, agent_id)

    def scope_of(self, agent_id: str) -> str:
        """`agent_id` 读写的那份工作区的标识：沿父链最近一个有隔离副本的 Agent 的 id，共用根工作区时
        是空串（与 `_workspace_of` 同一条父链规则）。产物 id 带上它：两个各在自己副本里写同一路径的
        Agent 是两个产物，不互相顶掉对方的「最新版本」（M7 评审 H5）。"""
        visited: set[str] = set()
        agent: str | None = agent_id
        while agent is not None and agent not in visited:
            visited.add(agent)
            if agent in self._copies:
                return agent
            agent = self._parents.get(agent)
        return ''

    def _target(self) -> Workspace:
        return self._workspace_of(driving_agent())

    def _workspace_of(self, agent: str | None) -> Workspace:
        """沿父链找最近一个有副本的 Agent；都没有就是根。"""
        visited: set[str] = set()
        while agent is not None and agent not in visited:
            visited.add(agent)
            if agent in self._merging:
                raise ConfigError(
                    expected=f'agent {agent} 的隔离副本不在合并中',
                    received='它正在合并回去，这期间副本里的读写一律被拒',
                    next_action='等这次合并结束再试；合并之后改动直接落在它合并到的那个工作区',
                )
            assigned = self._assigned.get(agent)
            if assigned is not None:
                return assigned
            agent = self._parents.get(agent)
        return self._root

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
        returned = cast(object, await write(portal, expected))
        version = returned if isinstance(returned, str) else _version(data)
        self._seen[key] = version
        if version == _version(data):
            self._record(workspace, path, data)
        elif self._versions is not None and workspace is self._root:
            # 后端写下的不是交给它的那几个字节（规范化了换行或编码）：记 `data` 会让版本层
            # 与文件对不上。
            logger.error('版本层没有记下 %s 的这次写：后端写下的内容与交给它的不同', path)
        self._wrote(workspace, path, version)
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
        self._read(workspace, path, version)
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
        self._wrote(workspace, path, version)
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

    async def set_executable(self, path: str, executable: bool) -> None:
        await self._target().portal().set_executable(path, executable)

    async def glob_paths(self, pattern: str) -> tuple[str, ...]:
        return await self._target().portal().glob_paths(pattern)

    async def grep_text(self, pattern: str, path: str) -> tuple[GrepHit, ...]:
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
        """关掉根与每份隔离副本里的进程。副本的目录留着：没合并的改动不在关机时替谁丢掉。"""
        for copy in list(self._copies.values()):
            await copy.workspace.close()
        await self._root.portal().close()


class _ArtifactTracing(Protocol):
    """Runtime 的插桩里路由用到的那两样：读到工具写下的版本连一条边，读到基线或外部的版本在读的
    span 上标出处。"""

    def artifact(self, writer: str, path: str, version: str, trust: str) -> None: ...

    def provenance(self, source: str, trust: str) -> None: ...


def _read_only(operation: str) -> ConfigError:
    return ConfigError(
        expected='伴生 Agent 的工作区视图只读（Read / Grep / Glob）',
        received=f'收到一次 {operation}',
        next_action='验收只看证据、不改工作区；要改产物由主 Agent 自己改',
    )


class _ReadOnlyView:
    """`WorkspaceRouter.reader_for` 交出的只读 Portal：按 `agent_id` 定工作区，读直达它的后端，
    不经路由的读基线与溯源记账；信任按那份工作区的文件污点照算（实现 `TaintAware`）。"""

    def __init__(self, router: WorkspaceRouter, agent_id: str) -> None:
        self._router = router
        self._agent = agent_id

    def _workspace(self) -> Workspace:
        return self._router._workspace_of(self._agent)  # pyright: ignore[reportPrivateUsage]

    def file_trust(self, path: str, version: str | None) -> TrustLevel:
        taint = self._router._taint_of(self._workspace())  # pyright: ignore[reportPrivateUsage]
        return TrustLevel.TRUSTED if taint is None else taint.trust_of(path, version)

    def name_trust(self, path: str) -> TrustLevel:
        taint = self._router._taint_of(self._workspace())  # pyright: ignore[reportPrivateUsage]
        return TrustLevel.TRUSTED if taint is None else taint.name_trust(path)

    async def read_text(self, path: str, *, offset: int, limit: int) -> str:
        return await self._workspace().portal().read_text(path, offset=offset, limit=limit)

    async def read_versioned(self, path: str, *, offset: int, limit: int) -> tuple[str, str]:
        return await self._workspace().portal().read_versioned(path, offset=offset, limit=limit)

    async def read_bytes(self, path: str) -> bytes:
        return await self._workspace().portal().read_bytes(path)

    async def stat(self, path: str) -> FileStat | None:
        return await self._workspace().portal().stat(path)

    async def glob_paths(self, pattern: str) -> tuple[str, ...]:
        return await self._workspace().portal().glob_paths(pattern)

    async def grep_text(self, pattern: str, path: str) -> tuple[GrepHit, ...]:
        return await self._workspace().portal().grep_text(pattern, path)

    async def write_text(self, path: str, content: str, *, expected: Expected = ANY) -> str:
        raise _read_only('write_text')

    async def edit_text(self, path: str, old: str, new: str, *, expected: Expected = ANY) -> str:
        raise _read_only('edit_text')

    async def write_bytes(self, path: str, data: bytes, *, expected: Expected = ANY) -> str:
        raise _read_only('write_bytes')

    async def remove(self, path: str, *, expected: Expected = ANY) -> None:
        raise _read_only('remove')

    async def set_executable(self, path: str, executable: bool) -> None:
        raise _read_only('set_executable')

    async def run(self, command: str, *, timeout: float, cwd: str) -> ExecResult:
        raise _read_only('run')

    async def start(self, command: str, *, cwd: str) -> str:
        raise _read_only('start')

    async def poll(self, handle: str, *, since: tuple[int, int] = (0, 0)) -> ExecResult:
        raise _read_only('poll')

    async def kill(self, handle: str) -> None:
        raise _read_only('kill')

    async def pty_create(self, command: str, *, cwd: str, cols: int = 120, rows: int = 40) -> str:
        raise _read_only('pty_create')

    async def pty_send(self, handle: str, data: str) -> None:
        raise _read_only('pty_send')

    async def pty_capture(self, handle: str, *, since: int = 0, wait: float = 0.0) -> PtyCapture:
        raise _read_only('pty_capture')

    async def pty_resize(self, handle: str, *, cols: int, rows: int) -> None:
        raise _read_only('pty_resize')

    async def pty_kill(self, handle: str) -> None:
        raise _read_only('pty_kill')

    async def pty_status(self, handle: str) -> PtyStatus:
        raise _read_only('pty_status')

    async def close(self) -> None:
        return None
