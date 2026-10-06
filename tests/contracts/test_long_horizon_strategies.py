from __future__ import annotations

import pytest

from syndica.kernel.contracts.long_horizon import LongHorizonStrategyContract
from syndica.kernel.long_horizon import (
    BaselineStrategy,
    BudgetAction,
    BudgetMove,
    DriftAction,
    DriftMove,
    LongHorizonContext,
    LongHorizonStrategy,
    StallAction,
    StallMove,
    StopDecision,
)


class RestartLoop:
    """简单重启循环：停滞就从头再读一遍目标重来，预算低就直接收尾，只在完成判据成立时停。"""

    async def on_progress_stall(self, ctx: LongHorizonContext) -> StallAction:
        return StallAction(
            StallMove.NUDGE, '停下来，从头重读目标与验收标准，按它们重新排一遍步骤。'
        )

    async def on_budget_low(self, ctx: LongHorizonContext) -> BudgetAction:
        return BudgetAction(BudgetMove.WRAP_UP)

    async def on_goal_drift(self, ctx: LongHorizonContext) -> DriftAction:
        return DriftAction(DriftMove.REMIND)

    async def should_stop(self, ctx: LongHorizonContext) -> StopDecision:
        return StopDecision(ctx.progress.complete)


class TestBaselineStrategy(LongHorizonStrategyContract):
    @pytest.fixture
    def strategy(self) -> LongHorizonStrategy:
        return BaselineStrategy()


class TestRestartLoop(LongHorizonStrategyContract):
    @pytest.fixture
    def strategy(self) -> LongHorizonStrategy:
        return RestartLoop()
