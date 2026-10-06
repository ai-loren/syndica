"""内核与长程控制器的接缝：收尾轮不问控制器、控制器花掉的预算当轮重判、控制器的块由内核署名并取最严的
信任。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from syndica.kernel.agent import (
    _BUDGET_BLOCK_SAMPLE,  # pyright: ignore[reportPrivateUsage]
    AgentOptions,
    BaseAgent,
)
from syndica.kernel.budget import Budget, BudgetPolicy, Usage
from syndica.kernel.budget_tree import BudgetAccount
from syndica.kernel.context import Context, ContextBlock, StopReason, ToolCall, TrustLevel
from syndica.kernel.events import EventBus
from syndica.kernel.history_blocks import HistoryKind, kind_of
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import LLMConfig
from syndica.kernel.long_horizon_seam import LongHorizonBlock
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.tool import Tool, ToolResult

_TIMEOUT = 5.0


class _Echo(Tool):
    __tool_name__ = 'Echo'
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        return ToolResult(output='echo', trust=TrustLevel.UNTRUSTED)


class _Busy:
    """每次都调 Echo、报一份很小的 usage。"""

    def __init__(self) -> None:
        self.config = LLMConfig(provider='scripted', model='m', request_args={'max_tokens': 64})
        self.calls = 0

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        self.calls += 1
        return ContextBlock(
            role='assistant',
            content=f'step {self.calls}',
            tool_calls=[ToolCall(id=f'e{self.calls}', name='Echo', arguments='{}')],
            extra={'usage': {'input_tokens': 20, 'output_tokens': 5}},
        )


class _Probe:
    def __init__(
        self,
        act: Callable[[BaseAgent, Context, int], Sequence[LongHorizonBlock]],
        *,
        final_at: int | None = None,
    ) -> None:
        self.asked: list[int] = []
        self.agent: BaseAgent | None = None
        self._act = act
        self._final_at = final_at

    def wrap_up_requested(self) -> bool:
        return False

    async def before_model(self, ctx: Context, turn: int) -> Sequence[LongHorizonBlock]:
        self.asked.append(turn)
        assert self.agent is not None
        return self._act(self.agent, ctx, turn)

    def final_requested(self) -> bool:
        return bool(self.asked) and self.asked[-1] == self._final_at

    async def record_verdict(self, ctx: Context, turn: int) -> None:
        return None

    async def after_completion(self, ctx: Context, turn: int) -> LongHorizonBlock | None:
        return None


def _factory(llm: _Busy, probe: _Probe) -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = BaseAgent(
            AgentOptions(name='Worker', llm=llm, tools={'Echo': _Echo()}, system_prompt='SP'),  # type: ignore[arg-type]
            hooks=hooks,
            events=events,
        )
        agent._attach_long_horizon(probe)  # pyright: ignore[reportPrivateUsage]
        probe.agent = agent
        return agent

    return build


async def _run(budget: Budget, probe: _Probe, llm: _Busy | None = None) -> tuple[Context, _Busy]:
    llm = llm or _Busy()
    runtime = Runtime(budget=BudgetPolicy(root=budget))
    try:
        agent_id = await runtime.spawn(_factory(llm, probe), query='go')
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()
    return ctx, llm


def _nothing(agent: BaseAgent, ctx: Context, turn: int) -> Sequence[LongHorizonBlock]:
    return ()


def _spend_leaving(
    calls: float, at_turn: int
) -> Callable[[BaseAgent, Context, int], Sequence[LongHorizonBlock]]:
    """在 `at_turn` 把预算花到只剩 `calls` 次调用的估算。"""

    def act(agent: BaseAgent, ctx: Context, turn: int) -> Sequence[LongHorizonBlock]:
        if turn != at_turn:
            return ()
        budget: BudgetAccount = agent._budget  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
        request = {
            'messages': [
                ContextBlock(role='system', content='SP'),
                *ctx.current_traj,
                _BUDGET_BLOCK_SAMPLE,
                _BUDGET_BLOCK_SAMPLE,
            ],
            'tools': [tool.schema for tool in agent.options.tools.values()],
        }
        tokens, _, _ = budget.estimate(agent.options.llm, request)
        left = budget.view().remaining.tokens
        assert left is not None
        budget.charge(Usage(input_tokens=left - int(tokens * calls)))
        return ()

    return act


def _wraps(ctx: Context) -> list[int]:
    return [i for i, b in enumerate(ctx.current_traj) if '最后一轮' in str(b.content)]


async def test_the_controller_is_not_asked_on_the_wrap_up_turn() -> None:
    probe = _Probe(_nothing)
    ctx, llm = await _run(Budget(turns=3), probe)
    assert ctx.stop_reason is StopReason.BUDGET
    assert probe.asked == [1, 2]
    assert llm.calls == 3


async def test_spending_by_the_controller_that_leaves_one_call_makes_this_turn_the_wrap_up() -> (
    None
):
    probe = _Probe(_spend_leaving(1.5, at_turn=2))
    ctx, llm = await _run(Budget(tokens=200_000), probe)
    [wrap] = _wraps(ctx)
    assert ctx.stop_reason is StopReason.BUDGET
    assert llm.calls == 2
    assert any(b.role == 'assistant' for b in ctx.current_traj[wrap:])


async def test_spending_by_the_controller_that_leaves_no_call_stops_before_calling() -> None:
    probe = _Probe(_spend_leaving(0.5, at_turn=2))
    ctx, llm = await _run(Budget(tokens=200_000), probe)
    assert ctx.stop_reason is StopReason.BUDGET
    assert llm.calls == 1
    assert _wraps(ctx) == []


async def test_controller_blocks_take_the_strictest_trust_and_are_added_once() -> None:
    def remind(agent: BaseAgent, ctx: Context, turn: int) -> Sequence[LongHorizonBlock]:
        return (LongHorizonBlock(HistoryKind.STRATEGY, 'keep going', key='strategy:k'),)

    probe = _Probe(remind)
    ctx, _ = await _run(Budget(turns=4), probe)
    blocks = [b for b in ctx.current_traj if kind_of(b) is HistoryKind.STRATEGY]
    assert len(blocks) == 1
    assert blocks[0].trust is TrustLevel.TRUSTED
    second = ctx.current_traj.index(blocks[0])
    assert all(b.role != 'tool' for b in ctx.current_traj[:second])


async def test_a_controller_block_after_untrusted_output_is_untrusted() -> None:
    def remind(agent: BaseAgent, ctx: Context, turn: int) -> Sequence[LongHorizonBlock]:
        if turn < 2:
            return ()
        return (LongHorizonBlock(HistoryKind.STRATEGY, 'keep going', key=f'strategy:{turn}'),)

    probe = _Probe(remind)
    ctx, _ = await _run(Budget(turns=4), probe)
    blocks = [b for b in ctx.current_traj if kind_of(b) is HistoryKind.STRATEGY]
    assert blocks and all(b.trust is TrustLevel.UNTRUSTED for b in blocks)


def _answer(ctx: Context, call_id: str) -> str:
    return str(next(b for b in ctx.current_traj if b.tool_call_id == call_id).content)


async def test_a_final_answer_turn_does_not_carry_over_into_a_wrap_up_turn() -> None:
    probe = _Probe(_nothing, final_at=1)
    ctx, _ = await _run(Budget(turns=2), probe)
    assert '强制验收条都已满足' in _answer(ctx, 'e1')
    assert '预算用完了' in _answer(ctx, 'e2')
    assert ctx.stop_reason is StopReason.BUDGET


async def test_a_recheck_wrap_up_wins_over_a_final_answer_offered_in_the_same_turn() -> None:
    probe = _Probe(_spend_leaving(1.5, at_turn=2), final_at=2)
    ctx, llm = await _run(Budget(tokens=200_000), probe)
    assert '预算用完了' in _answer(ctx, 'e2')
    assert ctx.stop_reason is StopReason.BUDGET
    assert llm.calls == 2
