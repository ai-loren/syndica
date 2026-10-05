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
from syndica.kernel.context import StopReason
from syndica.kernel.errors import ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.history_blocks import HistoryKind, kind_of
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn

_A = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'x'),)
_R1 = Rubric(id='R1', statement='has section 4', kind=RubricKind.CHECKABLE, anchors=_A)


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
