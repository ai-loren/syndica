"""LongHorizonStrategy 契约。任何长程策略都必须通过（spec §8.6 / §13.3）。

四条：每个方法对任何合法快照都交回它声明的那种动作（不抛）；动作的 move 是该方法自己那个封闭枚举
的成员、`stop` 是布尔值；交回的文字不超过 `MESSAGE_LIMIT`（它每次都进历史，也就进了此后每次请求
的前缀）；快照原样不动。快照覆盖刚开跑、停滞、预算将尽、确认漂移、完成判据成立与上千条强制
验收条这几种形状：最后一种是文字超长最容易出现的地方。

长程 Module 对违约照样兜底（失败开放、截断、记 ERROR），这份契约让策略的作者在装上之前就知道。

**运行前提**见 `syndica.kernel.contracts.base`。
"""

from __future__ import annotations

import copy
from decimal import Decimal

from syndica.kernel.acceptance import (
    Anchor,
    AnchorSource,
    Goal,
    OutcomeKind,
    Rubric,
    RubricKind,
    RubricOutcome,
    derive_verdict,
)
from syndica.kernel.budget import Budget, BudgetView, Usage
from syndica.kernel.contracts.base import ContractSuite
from syndica.kernel.long_horizon import (
    MESSAGE_LIMIT,
    BudgetAction,
    BudgetMove,
    DriftAction,
    DriftMove,
    DriftReport,
    LongHorizonContext,
    LongHorizonStrategy,
    ProgressSnapshot,
    StallAction,
    StallMove,
    StopDecision,
)

__all__ = ['MESSAGE_LIMIT', 'LongHorizonStrategyContract']

_ANCHOR = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'quarterly report'),)
_GOAL = Goal(intent='写一份季度报告 reports/quarterly.md，包含营收表格', anchors=_ANCHOR)


def _rubrics(count: int) -> tuple[Rubric, ...]:
    return tuple(
        Rubric(id=f'R{i}', statement=f'requirement {i}', kind=RubricKind.CHECKABLE, anchors=_ANCHOR)
        for i in range(1, count + 1)
    )


def _snapshot(rubrics: tuple[Rubric, ...], satisfied: int, turn: int) -> ProgressSnapshot:
    ids = tuple(sorted(r.id for r in rubrics))
    return ProgressSnapshot(
        satisfied=ids[:satisfied], mandatory=ids, pending_human=(), measured_turn=turn
    )


def _budget(left: int) -> BudgetView:
    return BudgetView(
        limit=Budget(turns=100, tokens=1_000_000, cost=Decimal('5')),
        used=Usage(input_tokens=900_000, output_tokens=50_000, turns=100 - left),
        remaining=Budget(turns=left, tokens=50_000, cost=Decimal('0.25')),
    )


def _contexts() -> list[LongHorizonContext]:
    two = _rubrics(2)
    many = _rubrics(1000)
    passed = derive_verdict(
        two, {r.id: RubricOutcome(r.id, OutcomeKind.SATISFIED, _ANCHOR) for r in two}
    )
    partial = derive_verdict(two, {'R1': RubricOutcome('R1', OutcomeKind.SATISFIED, _ANCHOR)})
    base = LongHorizonContext(
        agent_id='a1',
        cycle=1,
        turn=1,
        goal=_GOAL,
        rubrics=two,
        progress=ProgressSnapshot(),
        budget=None,
        stalled_turns=0,
    )
    return [
        base,
        LongHorizonContext(
            agent_id='a1',
            cycle=1,
            turn=40,
            goal=_GOAL,
            rubrics=two,
            progress=_snapshot(two, 1, 20),
            budget=None,
            stalled_turns=20,
            verdict=partial,
        ),
        LongHorizonContext(
            agent_id='a1',
            cycle=2,
            turn=95,
            goal=_GOAL,
            rubrics=two,
            progress=_snapshot(two, 1, 90),
            budget=_budget(5),
            stalled_turns=5,
        ),
        LongHorizonContext(
            agent_id='a1',
            cycle=1,
            turn=17,
            goal=_GOAL,
            rubrics=two,
            progress=ProgressSnapshot(),
            budget=None,
            stalled_turns=0,
            drift=DriftReport(window_turns=8, anchored=False, confirmed=True, reason='off topic'),
        ),
        LongHorizonContext(
            agent_id='a1',
            cycle=1,
            turn=30,
            goal=_GOAL,
            rubrics=two,
            progress=_snapshot(two, 2, 30),
            budget=_budget(60),
            stalled_turns=0,
            verdict=passed,
        ),
        LongHorizonContext(
            agent_id='a1',
            cycle=3,
            turn=200,
            goal=_GOAL,
            rubrics=many,
            progress=_snapshot(many, 1, 190),
            budget=_budget(1),
            stalled_turns=60,
            drift=DriftReport(window_turns=8, anchored=False, confirmed=None),
        ),
    ]


_MOVES = (
    ('on_progress_stall', StallAction, StallMove),
    ('on_budget_low', BudgetAction, BudgetMove),
    ('on_goal_drift', DriftAction, DriftMove),
)


class LongHorizonStrategyContract(ContractSuite):
    async def test_every_hook_returns_its_own_action_type(
        self, strategy: LongHorizonStrategy
    ) -> None:
        for ctx in _contexts():
            for hook, action_type, _ in _MOVES:
                action = await getattr(strategy, hook)(ctx)
                assert isinstance(action, action_type), (hook, ctx.turn, action)
            assert isinstance(await strategy.should_stop(ctx), StopDecision), ctx.turn

    async def test_moves_belong_to_the_hooks_own_closed_enum(
        self, strategy: LongHorizonStrategy
    ) -> None:
        for ctx in _contexts():
            for hook, _, moves in _MOVES:
                action = await getattr(strategy, hook)(ctx)
                assert isinstance(action.move, moves), (hook, ctx.turn, action.move)
            decision = await strategy.should_stop(ctx)
            assert isinstance(decision.stop, bool), (ctx.turn, decision.stop)

    async def test_returned_text_fits_the_history_limit(
        self, strategy: LongHorizonStrategy
    ) -> None:
        for ctx in _contexts():
            for hook, _, _ in _MOVES:
                action = await getattr(strategy, hook)(ctx)
                assert isinstance(action.message, str), (hook, ctx.turn)
                assert len(action.message) <= MESSAGE_LIMIT, (hook, ctx.turn, len(action.message))
            decision = await strategy.should_stop(ctx)
            assert isinstance(decision.reason, str), ctx.turn
            assert len(decision.reason) <= MESSAGE_LIMIT, (ctx.turn, len(decision.reason))

    async def test_the_snapshot_is_left_as_it_was(self, strategy: LongHorizonStrategy) -> None:
        for ctx in _contexts():
            before = copy.deepcopy(ctx)
            for hook, _, _ in _MOVES:
                await getattr(strategy, hook)(ctx)
            await strategy.should_stop(ctx)
            assert ctx == before, ctx.turn
