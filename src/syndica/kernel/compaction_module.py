"""压缩 Runtime Module（阶段三 / spec §8.8）：声明 Flag、给 Agent 装压缩控制器、提供 Compact 工具。

内核的压缩步骤（`BaseAgent._maybe_compact`）是机制；本 Module 是开关与配置：选策略、把占用率阈值
换算成 token、把压缩后「重建缓存键」接到 prefix-drift 服务上（spec §8.8：压缩后一次性失效缓存）。

压缩默认**关**（`compaction_strategy='off'`）：它有损、不可逆，得由宿主显式打开并选策略。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, cast

from syndica import __version__
from syndica.kernel.compaction import (
    CompactionController,
    CompactionStrategy,
    PruningCompaction,
    RuleBasedCompaction,
    Summarize,
    SummaryCompaction,
)
from syndica.kernel.context import ContextBlock
from syndica.kernel.errors import ConfigError
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.modules import FlagDeclaration, FlagValue, RuntimeModule
from syndica.kernel.tool import Tool, ToolResult

if TYPE_CHECKING:
    from syndica.kernel.agent import BaseAgent
    from syndica.kernel.runtime import Runtime

__all__ = ['CompactionModule']

_OFF = 'off'
_STRATEGIES = (_OFF, 'rule_based', 'pruning', 'summary')


class _CompactTool(Tool):
    """Agent 主动要求在下一轮边界压缩（spec §8.8 第三条触发路径）。只置请求位，不自己改轨迹——
    改写是内核压缩步骤的事。"""

    __tool_name__ = 'Compact'
    __tool_command_args__ = ()
    __tool_external_input__ = False

    def __init__(self, agent: BaseAgent) -> None:
        self._agent = agent

    async def execute(self) -> ToolResult:
        """请求在下一轮边界整理上下文，为接下来吃上下文的操作腾出空间。"""
        controller = self._agent._compaction  # pyright: ignore[reportPrivateUsage]
        if controller is None:
            return ToolResult(output='压缩未启用：本 Agent 没有装压缩控制器，这次请求无效。')
        controller.request()
        return ToolResult(output='已请求在下一轮边界压缩上下文。')


class _AttachPlugin:
    """只负责把控制器装到 Agent 上；不注册任何 hook（压缩由内核步骤驱动，不走 hook）。"""

    def __init__(self, agent: BaseAgent, controller: CompactionController) -> None:
        agent._attach_compaction(controller)  # pyright: ignore[reportPrivateUsage]

    @property
    def name(self) -> str:
        return 'compaction'

    def register(self, hooks: HooksCenter) -> None:
        del hooks


class _NullPlugin:
    """压缩关着（`compaction_strategy='off'`）时装的空插件：不装控制器、不注册 hook。"""

    @property
    def name(self) -> str:
        return 'compaction'

    def register(self, hooks: HooksCenter) -> None:
        del hooks


def _build_strategy(name: str, summarize: Summarize | None) -> CompactionStrategy:
    if name == 'rule_based':
        return RuleBasedCompaction()
    if name == 'pruning':
        return PruningCompaction()
    if name == 'summary':
        return SummaryCompaction()
    raise ConfigError(
        expected=f'compaction_strategy 是 {list(_STRATEGIES)} 之一',
        received=f'收到 {name!r}',
        next_action='改 compaction_strategy Flag；关闭压缩用 off',
    )


def _default_summarize(agent: BaseAgent) -> Summarize:
    """用 Agent 自己的模型做一次性摘要。摘要器面对的是最长那份，策略已先做规则型预截断（§8.8）。
    经 `_aux_llm_call` 走录制 / 回放的同一条路（负 turn 号的 llm_call），回放可定位、不发散。"""

    async def summarize(blocks: Sequence[ContextBlock]) -> str:
        rendered = '\n'.join(f'[{b.role}] {b.content}' for b in blocks if b.content)
        reply = await agent._aux_llm_call(  # pyright: ignore[reportPrivateUsage]
            [
                ContextBlock(
                    role='system',
                    content=(
                        '概括以下对话历史：只保留其中明确出现的关键约束、已定决策与未完成事项，'
                        '去掉冗余执行细节，不要补充历史里没有的内容。'
                    ),
                ),
                ContextBlock(role='user', content=rendered),
            ]
        )
        return str(reply.content or '')

    return summarize


class CompactionModule(RuntimeModule):
    """把上下文压缩接进 Runtime：按 Flag 选策略，占用率到阈值（或超限、或 Agent 主动要求）时，内核
    在 turn 边界压缩到目标占用（spec §8.8）。"""

    def __init__(self) -> None:
        def plugin_factory(
            runtime: Runtime,
            agent: BaseAgent,
            flags: Mapping[str, FlagValue],
        ) -> _AttachPlugin | _NullPlugin:
            strategy_name = cast(str, flags['compaction_strategy'])
            if strategy_name == _OFF:
                return _NullPlugin()
            window = agent.options.llm.config.context_window or cast(
                int, flags['compaction_context_tokens']
            )
            threshold = max(1, window * cast(int, flags['compaction_threshold_percent']) // 100)
            target = max(1, window * cast(int, flags['compaction_target_percent']) // 100)
            summarize = _default_summarize(agent) if strategy_name == 'summary' else None
            controller = CompactionController(
                strategy=_build_strategy(strategy_name, summarize),
                threshold_tokens=threshold,
                target_tokens=target,
                summarize=summarize,
                on_compacted=_rebaseliner(runtime, agent),
            )
            return _AttachPlugin(agent, controller)

        def tool_factory(
            runtime: Runtime,
            agent: BaseAgent,
            flags: Mapping[str, FlagValue],
        ) -> Mapping[str, Tool]:
            del runtime
            if flags['compaction_strategy'] == _OFF:
                return {}
            return {'Compact': _CompactTool(agent)}

        super().__init__(
            'compaction',
            version=__version__,
            description='在 turn 边界把上下文压缩到目标占用（阈值 / 超限 / 主动三触发，spec §8.8）',
            capabilities=(),
            replay_safe=True,
            flags=(
                FlagDeclaration(
                    key='compaction_strategy',
                    default=_OFF,
                    description='压缩策略：off / rule_based / pruning / summary',
                    choices=_STRATEGIES,
                ),
                FlagDeclaration(
                    key='compaction_threshold_percent',
                    default=80,
                    description='占用率到上下文窗口的百分之几时提前触发压缩',
                    minimum=1,
                    maximum=99,
                ),
                FlagDeclaration(
                    key='compaction_target_percent',
                    default=50,
                    description='压缩压到上下文窗口的百分之几（必须小于触发阈值，迟滞才成立）',
                    minimum=1,
                    maximum=99,
                ),
                FlagDeclaration(
                    key='compaction_context_tokens',
                    default=128_000,
                    description='模型不报上下文窗口时，按多少 token 估算窗口大小',
                    minimum=1,
                ),
            ),
            agent_plugins=(plugin_factory,),
            tool_factories=(tool_factory,),
        )


def _rebaseliner(runtime: Runtime, agent: BaseAgent) -> Callable[[], None]:
    """压缩后让 prefix-drift 服务忘掉旧基线：缓存一次性失效、不被逐轮误报成漂移（spec §8.8）。
    没装 prefix-drift 时什么都不做。"""

    def rebaseline() -> None:
        service = getattr(runtime, 'prefix_drift', None)
        if service is not None:
            service._forget(agent.agent_id)  # pyright: ignore[reportPrivateUsage]

    return rebaseline
