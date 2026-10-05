"""turn 边界的内核压缩步骤（阶段三 / §8.8）：阈值 / 超限 / 主动三触发，改写走内核接口。

本模块保留 `from __future__ import annotations` 与仓库其余模块一致。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.compaction import (
    CompactionController,
    CompactionRequest,
    CompactionResult,
    PruningCompaction,
    RuleBasedCompaction,
)
from syndica.kernel.context import ContextBlock, ToolCall, TrustLevel
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import Event, EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import ContextOverflowError, ScriptedLLM, ScriptedTurn
from syndica.kernel.tool import Tool, ToolResult


class _Echo(Tool):
    __tool_command_args__ = ()

    async def execute(self, text: str = 'x') -> ToolResult:
        return ToolResult(output='o' * 200)


class _Spy:
    """包一个真策略，记下每次 compact 看到的块数——「压缩真的跑了」的确定性见证。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.seen: list[int] = []

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        self.seen.append(len(request.blocks))
        return await self.inner.compact(request)


def _agent(script: list[ScriptedTurn], *, events: EventBus | None = None) -> BaseAgent:
    options = AgentOptions(
        name='Worker',
        llm=ScriptedLLM(script),
        tools={'Echo': _Echo()},
        system_prompt='SP',
        max_turns=50,
    )
    return BaseAgent(options, hooks=HooksCenter(), events=events or EventBus())


async def _drain(sub: Any) -> list[Event]:
    out: list[Event] = []
    try:
        while True:
            async with asyncio.timeout(0.05):
                out.append(await sub.__anext__())
    except (TimeoutError, StopAsyncIteration):
        pass
    finally:
        sub.close()
    return out


def _call(cid: str) -> ScriptedTurn:
    return ScriptedTurn(tool_calls=[ToolCall(id=cid, name='Echo', arguments='{}')])


# ---- 触发路径 ----


def _overflow() -> ContextOverflowError:
    return ContextOverflowError(expected='请求在窗口内', received='超窗', next_action='压缩后重试')


async def test_threshold_trigger_compacts_at_the_turn_boundary() -> None:
    bus = EventBus()
    agent = _agent(
        [_call('c1'), _call('c2'), _call('c3'), ScriptedTurn(content='done')], events=bus
    )
    spy = _Spy(RuleBasedCompaction(min_keep_turns=1))
    agent._attach_compaction(  # pyright: ignore[reportPrivateUsage]
        CompactionController(strategy=spy, threshold_tokens=30, target_tokens=15)
    )
    sub = bus.subscribe()
    ctx = await agent.run('the-task-go')
    drained = await _drain(sub)

    assert spy.seen  # 阈值触发真的调了策略
    compactions = [e for e in drained if e.kind == 'compaction']
    assert compactions and compactions[0].payload['trigger'] == 'threshold'
    # 任务消息（Goal 代理）与最后一轮活下来，早期执行细节被压掉。
    texts = [str(b.content) for b in ctx.current_traj]
    assert any('the-task-go' in t for t in texts)
    assert any('done' in t for t in texts)
    assert len(ctx.current_traj) < 8  # 不压的话 4 轮会攒到 8 块


async def test_overflow_fallback_compacts_then_retries() -> None:
    bus = EventBus()
    agent = _agent(
        [_call('c1'), _call('c2'), ScriptedTurn(raises=_overflow()),
         ScriptedTurn(content='recovered')],
        events=bus,
    )
    spy = _Spy(RuleBasedCompaction(min_keep_turns=1))
    # 阈值很高：正常轮次不触发，唯一的压缩来自超限兜底。
    agent._attach_compaction(  # pyright: ignore[reportPrivateUsage]
        CompactionController(strategy=spy, threshold_tokens=10_000, target_tokens=20)
    )
    sub = bus.subscribe()
    ctx = await agent.run('go')
    drained = await _drain(sub)

    assert ctx.current_traj[-1].content == 'recovered'  # 压完下一轮重试成功
    compactions = [e for e in drained if e.kind == 'compaction']
    assert len(compactions) == 1
    assert compactions[0].payload['trigger'] == 'overflow'


async def test_overflow_without_a_controller_still_raises() -> None:
    agent = _agent([ScriptedTurn(raises=_overflow())])
    with pytest.raises(ContextOverflowError):
        await agent.run('go')


async def test_the_step_places_trust_via_apply_compaction() -> None:
    agent = _agent([ScriptedTurn(content='x')])
    agent.context._replace_traj(  # pyright: ignore[reportPrivateUsage]
        [
            ContextBlock(role='user', content='task'),
            ContextBlock(role='assistant', content='web said X', trust=TrustLevel.UNTRUSTED),
            ContextBlock(role='assistant', content='middle chatter'),
            ContextBlock(role='assistant', content='most recent'),
        ]
    )
    controller = CompactionController(
        strategy=RuleBasedCompaction(min_keep_turns=1), threshold_tokens=2, target_tokens=1
    )
    agent._attach_compaction(controller)  # pyright: ignore[reportPrivateUsage]
    changed = await agent._maybe_compact(  # pyright: ignore[reportPrivateUsage]
        agent.context, trigger='threshold', force=True
    )
    assert changed
    # 不可信块被压掉，但内核经 apply_compaction 补了署名标记——溯源不被洗白（绕过 apply_compaction
    # 的改动会让这条变红）。
    assert any(b.trust is TrustLevel.UNTRUSTED for b in agent.context.current_traj)


async def test_the_task_message_survives_a_real_compaction() -> None:
    agent = _agent([_call('c1'), _call('c2'), _call('c3'), ScriptedTurn(content='done')])
    agent._attach_compaction(  # pyright: ignore[reportPrivateUsage]
        CompactionController(
            strategy=PruningCompaction(), threshold_tokens=30, target_tokens=15
        )
    )
    ctx = await agent.run('UNIQUE-TASK-MARKER')
    assert any('UNIQUE-TASK-MARKER' in str(b.content) for b in ctx.current_traj)


# ---- 控制器迟滞 / 退避 ----


def _controller() -> CompactionController:
    return CompactionController(
        strategy=RuleBasedCompaction(), threshold_tokens=100, target_tokens=40
    )


def test_target_at_or_above_threshold_is_refused() -> None:
    with pytest.raises(ConfigError, match='threshold'):
        CompactionController(strategy=RuleBasedCompaction(), threshold_tokens=50, target_tokens=50)


def test_nonpositive_budgets_are_refused() -> None:
    with pytest.raises(ConfigError, match='正整数'):
        CompactionController(strategy=RuleBasedCompaction(), threshold_tokens=50, target_tokens=0)


def test_should_compact_follows_the_threshold() -> None:
    controller = _controller()
    assert not controller.should_compact(80)
    assert controller.should_compact(120)


def test_backoff_stops_repeated_empty_compaction_until_growth() -> None:
    controller = _controller()
    assert controller.should_compact(120)
    controller.settle(tokens_now=120, changed=False)  # 压不动
    assert not controller.should_compact(120)  # 没长就不反复空压
    assert controller.should_compact(130)  # 又长了，再试


def test_a_successful_compaction_clears_the_backoff() -> None:
    controller = _controller()
    controller.settle(tokens_now=30, changed=True)  # 压到阈值以下
    assert not controller.should_compact(30)
    assert controller.should_compact(120)


def test_a_request_bit_survives_until_the_next_compaction() -> None:
    controller = _controller()
    controller.request()
    assert controller.should_compact(10)  # 远低于阈值也压
    controller.settle(tokens_now=10, changed=True)
    assert not controller.should_compact(10)  # 位已清


def test_a_compaction_that_stays_over_threshold_keeps_backoff() -> None:
    controller = _controller()
    controller.settle(tokens_now=120, changed=True)  # 压了但仍在阈值上
    assert not controller.should_compact(120)
    assert controller.should_compact(140)
