"""验收切点 COMPLETION_PROPOSED 的内核契约（§7.5 / 评审 H4）。

裁决槽 = modified['verdict']；send_back 只对 partial / block 生效、由内核追加反馈块；终态绝不改成
pass；skip / result 与非 Verdict 都报错；取消照常上抛。

本模块保留 `from __future__ import annotations` 与仓库其余模块一致。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from syndica.kernel.acceptance import (
    Anchor,
    AnchorSource,
    OutcomeKind,
    Rubric,
    RubricKind,
    RubricOutcome,
    Verdict,
    VerdictStatus,
    derive_verdict,
)
from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import StopReason, ToolCall, TrustLevel
from syndica.kernel.control import NullTurnGate
from syndica.kernel.errors import ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.history_blocks import HistoryKind, kind_of
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.tool import Tool, ToolResult

_A = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'x'),)
_R1 = Rubric(id='R1', statement='has section 4', kind=RubricKind.CHECKABLE, anchors=_A)


class _Echo(Tool):
    async def execute(self, text: str) -> ToolResult:
        """回显 text。"""
        return ToolResult(output=text)


def _pass() -> Verdict:
    return derive_verdict([_R1], {'R1': RubricOutcome('R1', OutcomeKind.SATISFIED)})


def _block() -> Verdict:
    return derive_verdict([_R1], {'R1': RubricOutcome('R1', OutcomeKind.UNMET, _A)}, reason='no §4')


def _agent(turns: int, *, max_turns: int = 50) -> tuple[BaseAgent, HooksCenter]:
    hooks = HooksCenter()
    script = [ScriptedTurn(content=f'done {i}') for i in range(turns)]
    options = AgentOptions(
        name='Worker', llm=ScriptedLLM(script), tools={}, system_prompt='SP', max_turns=max_turns
    )
    return BaseAgent(options, hooks=hooks, events=EventBus()), hooks


async def test_without_a_verdict_the_agent_simply_completes() -> None:
    agent, _ = _agent(1)
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.COMPLETED
    assert agent._verdict is None  # pyright: ignore[reportPrivateUsage]


async def test_a_pass_verdict_completes_and_is_recorded() -> None:
    agent, hooks = _agent(1)

    async def judge(**_: Any) -> HookResult:
        return HookResult(modified={'verdict': _pass()})

    hooks.register(Junction.COMPLETION_PROPOSED, judge)
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.COMPLETED
    assert agent._verdict is not None and agent._verdict.status is VerdictStatus.PASS  # pyright: ignore[reportPrivateUsage]


async def test_a_block_with_send_back_appends_feedback_and_runs_another_turn() -> None:
    agent, hooks = _agent(2)
    seen = 0

    async def judge(**_: Any) -> HookResult:
        nonlocal seen
        seen += 1
        if seen == 1:
            return HookResult(modified={'verdict': _block(), 'send_back': True})
        return HookResult(modified={'verdict': _pass()})

    hooks.register(Junction.COMPLETION_PROPOSED, judge)
    ctx = await agent.run('go')
    assert seen == 2  # 打回后又判了一次
    feedback = [b for b in ctx.current_traj if kind_of(b) is HistoryKind.VERDICT]
    assert len(feedback) == 1 and 'R1' in str(feedback[0].content)  # 内核追加的反馈点名未满足条
    assert ctx.stop_reason is StopReason.COMPLETED


async def test_the_feedback_block_carries_a_bounded_reason() -> None:
    # 反馈带着理由，主 Agent 才知道改什么；理由有上限，很长的回复不会每次打回都撑大主上下文。
    agent, hooks = _agent(2)
    seen = 0

    async def judge(**_: Any) -> HookResult:
        nonlocal seen
        seen += 1
        if seen == 1:
            reason = 'missing the totals table ' + 'x' * 2000
            unmet = {'R1': RubricOutcome('R1', OutcomeKind.UNMET, _A)}
            verdict = derive_verdict([_R1], unmet, reason=reason)
            return HookResult(modified={'verdict': verdict, 'send_back': True})
        return HookResult(modified={'verdict': _pass()})

    hooks.register(Junction.COMPLETION_PROPOSED, judge)
    ctx = await agent.run('go')
    feedback = [b for b in ctx.current_traj if kind_of(b) is HistoryKind.VERDICT]
    text = str(feedback[0].content)
    assert 'missing the totals table' in text
    assert 'x' * 400 in text and 'x' * 500 not in text


async def test_send_back_is_ignored_for_a_pass_verdict() -> None:
    # 只对 partial / block 打回：pass 带 send_back 也直接完成，hook 不能把通过的 Agent 拖住。
    agent, hooks = _agent(1)

    async def judge(**_: Any) -> HookResult:
        return HookResult(modified={'verdict': _pass(), 'send_back': True})

    hooks.register(Junction.COMPLETION_PROPOSED, judge)
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.COMPLETED
    assert not any(kind_of(b) is HistoryKind.VERDICT for b in ctx.current_traj)


async def test_the_terminal_verdict_is_never_turned_into_pass() -> None:
    # 一直 block 打回、撞 MAX_TURNS：终态裁决是最后那次的 block，绝不是 pass（评审 H4）。
    agent, hooks = _agent(3, max_turns=2)

    async def judge(**_: Any) -> HookResult:
        return HookResult(modified={'verdict': _block(), 'send_back': True})

    hooks.register(Junction.COMPLETION_PROPOSED, judge)
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.MAX_TURNS
    assert agent._verdict is not None and agent._verdict.status is VerdictStatus.BLOCK  # pyright: ignore[reportPrivateUsage]


async def test_a_skip_at_the_completion_junction_is_refused() -> None:
    agent, hooks = _agent(1)

    async def judge(**_: Any) -> HookResult:
        return HookResult(skip=True)

    hooks.register(Junction.COMPLETION_PROPOSED, judge)
    with pytest.raises(ContractViolation, match='verdict'):
        await agent.run('go')


async def test_a_non_verdict_is_refused() -> None:
    agent, hooks = _agent(1)

    async def judge(**_: Any) -> HookResult:
        return HookResult(modified={'verdict': 'pass'})

    hooks.register(Junction.COMPLETION_PROPOSED, judge)
    with pytest.raises(ContractViolation, match='Verdict'):
        await agent.run('go')


async def test_cancellation_at_the_completion_junction_propagates() -> None:
    # 取消不是验收器失败：照常上抛，不能被兜成 unverified 的 COMPLETED（评审 H4）。
    agent, hooks = _agent(1)

    async def judge(**_: Any) -> HookResult:
        raise asyncio.CancelledError

    hooks.register(Junction.COMPLETION_PROPOSED, judge)
    with pytest.raises(asyncio.CancelledError):
        await agent.run('go')


async def test_a_later_hook_cannot_turn_a_block_into_a_pass() -> None:
    # 链上靠后的 hook 交 PASS 不能盖掉前面的 BLOCK：取最严的那个（评审 H1）。
    agent, hooks = _agent(3, max_turns=2)

    async def strict(**_: Any) -> HookResult:
        return HookResult(modified={'verdict': _block(), 'send_back': True})

    async def lenient(**_: Any) -> HookResult:
        return HookResult(modified={'verdict': _pass()})

    hooks.register(Junction.COMPLETION_PROPOSED, strict)
    hooks.register(Junction.COMPLETION_PROPOSED, lenient)
    ctx = await agent.run('go')
    assert ctx.verdict is not None and ctx.verdict.status is VerdictStatus.BLOCK
    assert any(kind_of(b) is HistoryKind.VERDICT for b in ctx.current_traj)  # 照样打回了


async def test_a_new_cycle_does_not_inherit_the_previous_verdict() -> None:
    # 第一周期 PASS；第二周期还没判完成就撞 MAX_TURNS：不能交出上一周期的 PASS（评审 H2）。
    hooks = HooksCenter()
    script = [
        ScriptedTurn(content='done'),
        ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Echo', arguments='{"text": "x"}')]),
    ]
    options = AgentOptions(
        name='Worker',
        llm=ScriptedLLM(script),
        tools={'Echo': _Echo()},
        system_prompt='SP',
        max_turns=1,
    )
    agent = BaseAgent(options, hooks=hooks, events=EventBus())

    async def judge(**_: Any) -> HookResult:
        return HookResult(modified={'verdict': _pass()})

    hooks.register(Junction.COMPLETION_PROPOSED, judge)
    first = await agent.run('one')
    assert first.verdict is not None and first.verdict.status is VerdictStatus.PASS
    second = await agent.run('two')
    assert second.stop_reason is StopReason.MAX_TURNS
    assert second.verdict is None


async def test_the_hook_is_told_which_cycle_it_is_judging() -> None:
    agent, hooks = _agent(2)
    seen: list[object] = []

    async def judge(**kwargs: Any) -> HookResult:
        seen.append(kwargs.get('cycle'))
        return HookResult()

    hooks.register(Junction.COMPLETION_PROPOSED, judge)
    await agent.run('one')
    await agent.run('two')
    assert len(seen) == 2 and seen[0] != seen[1]


class _AbortableGate(NullTurnGate):
    def __init__(self) -> None:
        self.armed = False

    async def check_abort(self) -> bool:
        return self.armed


async def test_an_abort_that_arrives_during_verification_wins() -> None:
    # 验收跑着的时候到了中止：通过的裁决不能把这次运行收成 COMPLETED（评审 M10）。
    agent, hooks = _agent(1)
    gate = _AbortableGate()
    agent._attach_gate(gate)  # pyright: ignore[reportPrivateUsage]

    async def judge(**_: Any) -> HookResult:
        gate.armed = True
        return HookResult(modified={'verdict': _pass()})

    hooks.register(Junction.COMPLETION_PROPOSED, judge)
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.ABORTED


@pytest.mark.parametrize('query_trust', [TrustLevel.UNTRUSTED, TrustLevel.TRUSTED])
async def test_the_feedback_block_is_at_least_as_untrusted_as_the_main_trajectory(
    query_trust: TrustLevel,
) -> None:
    # 反馈块会成为下一轮的输入：主轨迹不可信时它也不可信（评审 B1）；可信运行仍是可信的。
    agent, hooks = _agent(2)
    seen = 0

    async def judge(**_: Any) -> HookResult:
        nonlocal seen
        seen += 1
        if seen == 1:
            return HookResult(modified={'verdict': _block(), 'send_back': True})
        return HookResult(modified={'verdict': _pass()})

    hooks.register(Junction.COMPLETION_PROPOSED, judge)
    ctx = await agent.run('go', trust=query_trust)
    feedback = next(b for b in ctx.current_traj if kind_of(b) is HistoryKind.VERDICT)
    assert feedback.trust is query_trust


def test_delegate_hands_the_childs_verdict_back_to_the_parent() -> None:
    # 子 Agent 的裁决随 Delegate 交回（§7.5 / 评审 H2）：父 Agent 知道交回的东西验没验过、过没过。
    from syndica.kernel.context import Context, ContextBlock
    from syndica.kernel.tools.orchestration import _delivered  # pyright: ignore[reportPrivateUsage]

    ctx = Context()
    ctx.append(ContextBlock(role='assistant', content='here is the report'))
    ctx.stop_reason = StopReason.COMPLETED
    ctx.verdict = _block()
    result = _delivered('child-1', ctx)
    assert result.meta['verdict'] == 'block'
    assert 'block' in result.output


async def test_a_later_completion_without_a_verdict_clears_the_earlier_one() -> None:
    # 裁决说的是这份回复：被打回之前那份的 block 不能留到改好之后。
    agent, hooks = _agent(2)
    seen = 0

    async def judge(**_: Any) -> HookResult:
        nonlocal seen
        seen += 1
        if seen == 1:
            return HookResult(modified={'verdict': _block(), 'send_back': True})
        return HookResult()

    hooks.register(Junction.COMPLETION_PROPOSED, judge)
    ctx = await agent.run('go')
    assert seen == 2
    assert ctx.verdict is None


async def test_agent_end_reports_the_verdict() -> None:
    bus = EventBus()
    hooks = HooksCenter()
    options = AgentOptions(
        name='Worker', llm=ScriptedLLM([ScriptedTurn(content='done')]), tools={}, system_prompt='SP'
    )
    agent = BaseAgent(options, hooks=hooks, events=bus)

    async def judge(**_: Any) -> HookResult:
        return HookResult(modified={'verdict': _pass()})

    hooks.register(Junction.COMPLETION_PROPOSED, judge)
    async with bus.subscribe() as events:
        await agent.run('go')
        await bus.close()
        delivered = [event async for event in events]
    ending = [event for event in delivered if event.kind == 'agent_end']
    assert ending[0].payload == {'stop_reason': 'completed', 'verdict': 'pass'}
