"""长程策略契约本身咬得住：每一条都有一个只违反它的策略让它变红。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from syndica.kernel.contracts.long_horizon import MESSAGE_LIMIT, LongHorizonStrategyContract
from syndica.kernel.long_horizon import (
    BaselineStrategy,
    BudgetAction,
    DriftMove,
    LongHorizonContext,
    StallAction,
    StallMove,
    StopDecision,
)


class _WrongType(BaselineStrategy):
    async def on_budget_low(self, ctx: LongHorizonContext) -> BudgetAction:
        return StallAction(StallMove.NUDGE, 'x')  # type: ignore[return-value]


class _ForeignMove(BaselineStrategy):
    async def on_progress_stall(self, ctx: LongHorizonContext) -> StallAction:
        return StallAction(DriftMove.REMIND, 'x')  # type: ignore[arg-type]


class _NotABool(BaselineStrategy):
    async def should_stop(self, ctx: LongHorizonContext) -> StopDecision:
        return StopDecision(1)  # type: ignore[arg-type]


class _Verbose(BaselineStrategy):
    async def on_progress_stall(self, ctx: LongHorizonContext) -> StallAction:
        return StallAction(StallMove.NUDGE, 'x' * (MESSAGE_LIMIT + 1))


class _Raises(BaselineStrategy):
    async def should_stop(self, ctx: LongHorizonContext) -> StopDecision:
        if ctx.budget is not None:
            raise RuntimeError('cannot read the budget')
        return await super().should_stop(ctx)


class _Mutates(BaselineStrategy):
    async def on_goal_drift(self, ctx: LongHorizonContext):  # type: ignore[no-untyped-def]
        object.__setattr__(ctx, 'progress', replace(ctx.progress, satisfied=()))
        return await super().on_goal_drift(ctx)


_CASES = {
    'test_every_hook_returns_its_own_action_type': [_WrongType, _Raises],
    'test_moves_belong_to_the_hooks_own_closed_enum': [_ForeignMove, _NotABool],
    'test_returned_text_fits_the_history_limit': [_Verbose],
    'test_the_snapshot_is_left_as_it_was': [_Mutates],
}


@pytest.mark.parametrize(
    ('case', 'strategy'),
    [(case, strategy) for case, strategies in _CASES.items() for strategy in strategies],
    ids=lambda value: getattr(value, '__name__', value),
)
async def test_each_clause_rejects_a_strategy_that_breaks_only_it(
    case: str, strategy: type[BaselineStrategy]
) -> None:
    with pytest.raises((AssertionError, RuntimeError)):
        await getattr(LongHorizonStrategyContract(), case)(strategy())


@pytest.mark.parametrize('case', sorted(_CASES))
async def test_the_baseline_passes_every_clause(case: str) -> None:
    await getattr(LongHorizonStrategyContract(), case)(BaselineStrategy())


def test_every_clause_has_a_strategy_that_breaks_it() -> None:
    clauses = {
        name
        for name in dir(LongHorizonStrategyContract)
        if name.startswith('test_') and name != 'test_async_cases_are_driven_by_an_async_plugin'
    }
    assert clauses == set(_CASES)
