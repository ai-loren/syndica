"""压缩 Runtime Module（阶段三 / §8.8）：Flag 选策略、装控制器、Compact 工具、压缩后让 drift 重建。

本模块保留 `from __future__ import annotations` 与仓库其余模块一致。
"""

from __future__ import annotations

import asyncio

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.compaction_module import CompactionModule
from syndica.kernel.context import StopReason, ToolCall
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.prefix_drift import PrefixDriftModule, PrefixDriftService
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.tool import Tool, ToolResult

_TIMEOUT = 2.0
# 窗口压到很小，几轮工具输出就越过阈值——否则默认 128k token 窗口在单测里永远触发不了。
_SMALL_WINDOW = {
    'compaction_context_tokens': 60,
    'compaction_threshold_percent': 80,
    'compaction_target_percent': 40,
}


class _Echo(Tool):
    __tool_name__ = 'Echo'
    __tool_command_args__ = ()

    async def execute(self, text: str = 'x') -> ToolResult:
        return ToolResult(output='o' * 300)


def _factory(llm: ScriptedLLM) -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name='Worker', llm=llm, tools={'Echo': _Echo()}, system_prompt='SP'),
            hooks=hooks,
            events=events,
        )

    return build


def _script(tool_turns: int = 5) -> list[ScriptedTurn]:
    calls = [
        ScriptedTurn(tool_calls=[ToolCall(id=f'c{i}', name='Echo', arguments='{}')])
        for i in range(tool_turns)
    ]
    return [*calls, ScriptedTurn(content='done')]


def _agent_of(runtime: Runtime, agent_id: str) -> BaseAgent:
    agent = runtime._table.get(agent_id)  # pyright: ignore[reportPrivateUsage]
    assert agent is not None
    return agent


async def test_enabled_compaction_bounds_the_context_and_adds_the_tool() -> None:
    llm = ScriptedLLM(_script())
    runtime = Runtime(
        modules=[CompactionModule()],
        flags={'compaction_strategy': 'rule_based', **_SMALL_WINDOW},
    )
    try:
        agent_id = await runtime.spawn(_factory(llm), query='the-task')
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

        assert ctx.stop_reason is StopReason.COMPLETED
        assert len(ctx.current_traj) < 12  # 不压的话 6 轮会攒到 ~12 块
        assert any('the-task' in str(b.content) for b in ctx.current_traj)  # 任务消息活着
        assert 'Compact' in _agent_of(runtime, agent_id).options.tools
    finally:
        await runtime.shutdown()


async def test_off_by_default_leaves_the_context_and_omits_the_tool() -> None:
    llm = ScriptedLLM(_script(tool_turns=3))
    runtime = Runtime(modules=[CompactionModule()])  # 默认 compaction_strategy='off'
    try:
        agent_id = await runtime.spawn(_factory(llm), query='the-task')
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

        assert ctx.stop_reason is StopReason.COMPLETED
        assert len(ctx.current_traj) == 8  # 1 user + 3*(assistant+tool) + 1 assistant，无压缩
        assert 'Compact' not in _agent_of(runtime, agent_id).options.tools
    finally:
        await runtime.shutdown()


async def test_compaction_rebaselines_prefix_drift_instead_of_reporting_it() -> None:
    llm = ScriptedLLM(_script())
    runtime = Runtime(
        modules=[CompactionModule(), PrefixDriftModule()],
        flags={'compaction_strategy': 'rule_based', **_SMALL_WINDOW},
    )
    try:
        agent_id = await runtime.spawn(_factory(llm), query='the-task')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

        service = getattr(runtime, 'prefix_drift')  # noqa: B009
        assert isinstance(service, PrefixDriftService)
        # 压缩改了前缀，但压缩步骤让 drift 忘掉基线：这是一次性失效、不是逐轮漂移缺陷（spec §8.8）。
        # 去掉 rebaseline 接线，这里会冒出漂移记录。
        assert service.records == ()
    finally:
        await runtime.shutdown()


async def test_the_compact_tool_requests_a_compaction() -> None:
    llm = ScriptedLLM(_script(tool_turns=1))
    runtime = Runtime(
        modules=[CompactionModule()],
        flags={'compaction_strategy': 'rule_based', **_SMALL_WINDOW},
    )
    try:
        agent_id = await runtime.spawn(_factory(llm), query='t')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        agent = _agent_of(runtime, agent_id)
        result = await agent.options.tools['Compact'].execute()
        assert '请求' in str(result.output)
        controller = agent._compaction  # pyright: ignore[reportPrivateUsage]
        # 位置上了：远低于阈值也会在下一轮边界压（spec §8.8 第三条触发路径）。
        assert controller is not None and controller.should_compact(0)
    finally:
        await runtime.shutdown()


async def test_target_not_below_threshold_fails_at_spawn() -> None:
    llm = ScriptedLLM(_script(tool_turns=1))
    runtime = Runtime(
        modules=[CompactionModule()],
        flags={
            'compaction_strategy': 'rule_based',
            'compaction_context_tokens': 100,
            'compaction_threshold_percent': 40,
            'compaction_target_percent': 80,  # 目标 > 阈值：迟滞不成立
        },
    )
    try:
        with pytest.raises(Exception, match='threshold'):
            await runtime.spawn(_factory(llm), query='go')
    finally:
        await runtime.shutdown()
