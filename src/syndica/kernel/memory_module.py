"""分层记忆 Runtime Module（阶段四 / §8.9）：Remember 工具记短期、阈值整理成长期、cycle 起点召回。

两层次：全局（跨所有工作）与工作区级（本项目）。两阶段：短期按 seq 记 → 到阈值整理成结构化长期。
写入权限收紧：**只有根协调者能写全局**（工人只读全局、写限本工作区）；不可信内容**永不进全局**
（后者由 `memory_redaction` 在铸造时强制，这里再挡一次「非根写全局」）。

召回**进历史不进 System Prompt**、按幂等键缓存同一份块（只在新会话 / 压缩掉之后重载，不每轮重拉——
否则前缀逐轮变、整个会话缓存全失效，spec §5.2 / §8.9）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import TYPE_CHECKING, cast

from syndica import __version__
from syndica.kernel.context import Context, ContextBlock, TrustLevel, call_trust, strictest_trust
from syndica.kernel.errors import ContractViolation
from syndica.kernel.history_blocks import HistoryKind, has_block, signed_block
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.memory_redaction import redacted_memory_entry, redacted_memory_sections
from syndica.kernel.memory_types import MemoryEntry, MemoryScope, MemorySection
from syndica.kernel.modules import FlagDeclaration, FlagValue, RuntimeModule
from syndica.kernel.protocols.memory import MemoryStore
from syndica.kernel.redaction import Redactor, RuleRedactor
from syndica.kernel.stores.memory_memory import InMemoryMemoryStore, MemoryConflict
from syndica.kernel.tool import Tool, ToolResult

if TYPE_CHECKING:
    from syndica.kernel.agent import BaseAgent
    from syndica.kernel.runtime import Runtime

__all__ = ['MemoryModule']

_SCOPES = {'workspace': MemoryScope.WORKSPACE, 'global': MemoryScope.GLOBAL}


async def _is_root(runtime: Runtime, agent_id: str) -> bool:
    """根协调者＝注册表里 `parent_id` 为空的那个 Agent。查不到按非根处置（§8.9：写全局要收紧）。"""
    info = await runtime.registry.get(agent_id)
    return info is not None and info.parent_id is None


class _RememberTool(Tool):
    """记一条记忆。信任随当前调用的溯源（读过不可信内容后记下的也是不可信）；写全局要是根、且内容可信。"""

    __tool_name__ = 'Remember'
    __tool_command_args__ = ()
    __tool_external_input__ = False

    def __init__(
        self,
        runtime: Runtime,
        agent: BaseAgent,
        store: MemoryStore,
        redactor: Redactor,
        workspace: str | None,
        consolidate_after: int,
    ) -> None:
        self._runtime = runtime
        self._agent = agent
        self._store = store
        self._redactor = redactor
        self._workspace = workspace
        self._consolidate_after = consolidate_after

    async def execute(self, text: str, scope: str = 'workspace') -> ToolResult:
        """记住一件跨会话值得保留的事（踩过的坑、约定、偏好）。scope 为 workspace（本项目）或 global
        （跨所有项目，仅根协调者可写）。"""
        memory_scope = _SCOPES.get(scope)
        if memory_scope is None:
            return ToolResult(
                output=f'scope 只能是 workspace 或 global，收到 {scope!r}。改用其一重试。'
            )
        trust = call_trust() or TrustLevel.TRUSTED
        if memory_scope is MemoryScope.GLOBAL and not await _is_root(
            self._runtime, self._agent.agent_id
        ):
            return ToolResult(
                output='只有根协调者能写全局记忆。把这条记进 workspace，或交给根协调者记。'
            )
        workspace = None if memory_scope is MemoryScope.GLOBAL else self._workspace
        entry = MemoryEntry(seq=0, scope=memory_scope, workspace=workspace, text=text, trust=trust)
        try:
            redacted = redacted_memory_entry(entry, self._redactor)
        except ContractViolation as exc:
            # 不可信内容进全局被铸造处挡下（spec §8.9）。把原因原样交回模型，让它改记 workspace。
            return ToolResult(output=f'这条记不进全局记忆：{exc.received}。改记 workspace。')
        await self._store.record(redacted)
        await self._maybe_consolidate(memory_scope, workspace)
        return ToolResult(output=f'已记住（{scope}）。')

    async def _maybe_consolidate(self, scope: MemoryScope, workspace: str | None) -> None:
        """短期累积到阈值就用一次性调用把它们整理成一段结构化长期记忆（spec §8.9；经 `_aux_llm_call`
        走录制 / 回放同一条路）。乐观并发：版本被别人抢先就跳，下次再整理。整理出的那段信任取被整理
        各条里最严的（不可信内容整理出来仍不可信，§12.2）。"""
        record = await self._store.read(scope, workspace)
        through = record.through_seq if record is not None else 0
        version = record.version if record is not None else 0
        fresh = await self._store.entries(scope, workspace, after_seq=through)
        if len(fresh) < self._consolidate_after:
            return
        rendered = '\n'.join(f'- {entry.text}' for entry in fresh)
        reply = await self._agent._aux_llm_call(  # pyright: ignore[reportPrivateUsage]
            [
                ContextBlock(
                    role='system',
                    content=(
                        '把以下零散记录整理成一段简洁的长期记忆：只保留记录里明确写出的事实'
                        '（约定、偏好、踩过的坑），不要补充记录里没有的细节、数值或建议。'
                    ),
                ),
                ContextBlock(role='user', content=rendered),
            ]
        )
        folded = MemorySection(
            heading=f'整理 seq {fresh[0].seq}–{fresh[-1].seq}',
            text=str(reply.content or ''),
            trust=_merged_trust(entry.trust for entry in fresh),
        )
        existing = list(record.sections) if record is not None else []
        sections = redacted_memory_sections(
            scope, workspace, [*existing, folded], fresh[-1].seq, self._redactor
        )
        try:
            await self._store.consolidate(sections, expected_version=version)
        except MemoryConflict:
            return  # 别人先整理了；下次带新版本再来


def _merged_trust(levels: Iterable[TrustLevel]) -> TrustLevel:
    result = TrustLevel.TRUSTED
    for level in levels:
        result = strictest_trust(result, level)
    return result


def _recaller(
    store: MemoryStore, workspace: str | None
) -> Callable[[Context], Awaitable[None]]:
    """cycle 起点注入长期记忆：全局一块 + 工作区一块（各自信任取段落最严）。幂等键挡重复——同一份块
    只注入一次，压缩掉或新会话后才重载（spec §8.9）。工人只注入自己工作区那层，看不到别的工作区。"""

    async def recall(ctx: Context) -> None:
        for scope, ws, key in (
            (MemoryScope.GLOBAL, None, 'memory:global'),
            (MemoryScope.WORKSPACE, workspace, 'memory:workspace'),
        ):
            if scope is MemoryScope.WORKSPACE and workspace is None:
                continue
            if has_block(ctx.current_traj, key):
                continue
            record = await store.read(scope, ws)
            if record is None or not record.sections:
                continue
            body = '\n'.join(f'- {section.heading}：{section.text}' for section in record.sections)
            trust = _merged_trust(section.trust for section in record.sections)
            ctx.append(
                signed_block(
                    HistoryKind.MEMORY,
                    f'（记忆 · {scope.value}）\n{body}',
                    key=key,
                    trust=trust,
                )
            )

    return recall


class MemoryModule(RuntimeModule):
    """把分层记忆接进 Runtime：Remember 记短期、阈值整理成长期、cycle 起点召回长期（spec §8.9）。

    `workspace` 是本次工作区的 id（宿主传）；`store` / `redactor` 缺省用内存 store + 规则脱敏器。
    """

    def __init__(
        self,
        *,
        workspace: str | None = None,
        store: MemoryStore | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self._store: MemoryStore = store if store is not None else InMemoryMemoryStore()
        self._redactor: Redactor = redactor if redactor is not None else RuleRedactor()
        self._workspace = workspace

        def plugin_factory(
            runtime: Runtime,
            agent: BaseAgent,
            flags: Mapping[str, FlagValue],
        ) -> _RecallPlugin:
            if flags['memory_recall']:
                agent._attach_recall(  # pyright: ignore[reportPrivateUsage]
                    _recaller(self._store, self._workspace)
                )
            return _RecallPlugin()

        def tool_factory(
            runtime: Runtime,
            agent: BaseAgent,
            flags: Mapping[str, FlagValue],
        ) -> Mapping[str, Tool]:
            return {
                'Remember': _RememberTool(
                    runtime,
                    agent,
                    self._store,
                    self._redactor,
                    self._workspace,
                    cast(int, flags['memory_consolidate_after']),
                )
            }

        super().__init__(
            'memory',
            version=__version__,
            description='分层记忆：Remember 记短期、阈值整理成长期、cycle 起点召回（spec §8.9）',
            capabilities=(),
            replay_safe=True,
            flags=(
                FlagDeclaration(
                    key='memory_recall',
                    default=True,
                    description='cycle 起点是否把长期记忆注入历史',
                ),
                FlagDeclaration(
                    key='memory_consolidate_after',
                    default=50,
                    description='短期记忆累积多少条后整理成一段长期记忆',
                    minimum=1,
                ),
            ),
            agent_plugins=(plugin_factory,),
            tool_factories=(tool_factory,),
        )


class _RecallPlugin:
    """只负责挂召回（在 plugin_factory 里做）；不注册 hook。"""

    @property
    def name(self) -> str:
        return 'memory'

    def register(self, hooks: HooksCenter) -> None:
        del hooks
