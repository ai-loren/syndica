"""长跑策略的协议与类型（spec §8.2 / §8.3 / §8.5 / §8.6）。

不同的长跑循环范式——简单重启循环、Actor-Critic、Monitor-Actor-Critic、分级质量门——是策略实现，不写死
在内核：换一种范式是写一个 Module，不是改骨架。内核给协议、上下文快照、动作类型与一个基线实现；长程
Module 在每轮模型调用之前把快照交给策略、按它交回的动作追加署名块或停下。

进度只来自验收（已满足的强制 checkable 条 / 强制 checkable 总数），不采信 Agent 自述；judgment 强制
条只能由人判，单列为待人判、不进比例。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from syndica.kernel.acceptance import Goal, Rubric, Verdict, VerdictStatus
from syndica.kernel.budget import BudgetView

__all__ = [
    'MESSAGE_LIMIT',
    'BaselineStrategy',
    'BudgetAction',
    'BudgetMove',
    'DriftAction',
    'DriftMove',
    'DriftReport',
    'LongHorizonContext',
    'LongHorizonStrategy',
    'ProgressSnapshot',
    'StallAction',
    'StallMove',
    'StopDecision',
]


# 三个动作枚举里都没有「停」：停滞与漂移是纠偏的信号、不是终止（§8.5），预算的终点归 `Exhaustion`；
# 唯一能让 Agent 停下的是 `should_stop`，而内核只在完成判据成立时兑现它。
class StallMove(Enum):
    CONTINUE = 'continue'
    NUDGE = 'nudge'


class BudgetMove(Enum):
    CONTINUE = 'continue'
    WRAP_UP = 'wrap_up'


class DriftMove(Enum):
    CONTINUE = 'continue'
    REMIND = 'remind'


@dataclass(frozen=True)
class StallAction:
    """进度停滞时怎么办。`NUDGE` 把 `message` 作为一段提示追加进历史（换条路，或说明卡在哪）。"""

    move: StallMove
    message: str = ''


@dataclass(frozen=True)
class BudgetAction:
    """预算低于阈值时怎么办：`CONTINUE` 带 `message` 时追加一段建议（先保住核心交付物）；`WRAP_UP`
    让下一轮就是收尾轮（与预算耗尽时同一种收尾）。耗尽之后的处置归 `Exhaustion`，不归这里。"""

    move: BudgetMove
    message: str = ''


@dataclass(frozen=True)
class DriftAction:
    """确认漂移之后怎么办。`REMIND` 让长程 Module 把 Goal 原文（带它自己的信任）重新注入一次，
    `message` 是附在前面的一句说明——策略不复述 Goal：复述出来的是「用户要什么」的第二个版本。"""

    move: DriftMove
    message: str = ''


@dataclass(frozen=True)
class StopDecision:
    stop: bool
    reason: str = ''


@dataclass(frozen=True)
class ProgressSnapshot:
    """最近一次量到的进度。`mandatory` 是强制的 checkable 条，`satisfied` 是其中被验收判满足的那些，
    `pending_human` 是强制的 judgment 条（只能由人判，不进比例）；`measured_turn` 是量的那一轮
    （None = 还没量过）。"""

    satisfied: tuple[str, ...] = ()
    mandatory: tuple[str, ...] = ()
    pending_human: tuple[str, ...] = ()
    measured_turn: int | None = None

    @property
    def ratio(self) -> float | None:
        """已满足的强制条 / 强制总数；没有强制 checkable 条时 None（没有可量的东西，不是 0 也不是
        1）。"""
        if not self.mandatory:
            return None
        done = set(self.satisfied) & set(self.mandatory)
        return len(done) / len(self.mandatory)

    @property
    def complete(self) -> bool:
        return bool(self.mandatory) and set(self.mandatory) <= set(self.satisfied)


@dataclass(frozen=True)
class DriftReport:
    """最近一个窗口的漂移检查。`anchored` 是零成本的锚点检查（窗口里的动作锚得回 Goal / Rubric
    吗）；`confirmed` 是锚点检查失败之后那一次语义比对的结论（None = 没跑：锚得上，或语义比对关着 /
    失败）。"""

    window_turns: int
    anchored: bool
    confirmed: bool | None = None
    reason: str = ''


@dataclass(frozen=True)
class LongHorizonContext:
    """交给策略的只读快照。策略据它决定，不经它改任何状态。"""

    agent_id: str
    cycle: int
    turn: int
    goal: Goal | None
    rubrics: tuple[Rubric, ...]
    progress: ProgressSnapshot
    budget: BudgetView | None
    stalled_turns: int
    drift: DriftReport | None = None
    verdict: Verdict | None = None


@runtime_checkable
class LongHorizonStrategy(Protocol):
    async def on_progress_stall(self, ctx: LongHorizonContext) -> StallAction: ...

    async def on_budget_low(self, ctx: LongHorizonContext) -> BudgetAction: ...

    async def on_goal_drift(self, ctx: LongHorizonContext) -> DriftAction: ...

    async def should_stop(self, ctx: LongHorizonContext) -> StopDecision: ...


# 策略交回的一段话进历史的上限：它进了历史，也就进了此后每次请求的前缀。契约按它断言，Module
# 按它截断。
MESSAGE_LIMIT = 2000
_NAMED = 10


class BaselineStrategy:
    """基线策略（spec §8.6）：停滞时请 Agent 换条路或说明卡在哪；预算低时先保住核心交付物；漂移时
    重申 Goal 原文；完成判据成立（强制条全满足且裁决 pass）时停。别的范式写成另一个策略交给长程
    Module，与它过同一份契约。"""

    async def on_progress_stall(self, ctx: LongHorizonContext) -> StallAction:
        done, total = len(ctx.progress.satisfied), len(ctx.progress.mandatory)
        return StallAction(
            StallMove.NUDGE,
            f'已经连续 {ctx.stalled_turns} 轮没有新的验收条被满足（{done}/{total}）。'
            '换一条路试试；如果卡住了，用一两句话说明卡在哪、缺什么。',
        )

    async def on_budget_low(self, ctx: LongHorizonContext) -> BudgetAction:
        return BudgetAction(
            BudgetMove.CONTINUE,
            '预算剩得不多了：先保住核心交付物、确认它们能交付，放弃锦上添花的部分。',
        )

    async def on_goal_drift(self, ctx: LongHorizonContext) -> DriftAction:
        return DriftAction(DriftMove.REMIND, '最近几轮的动作锚不回这次的目标。原样的目标如下：')

    async def should_stop(self, ctx: LongHorizonContext) -> StopDecision:
        verdict = ctx.verdict
        if ctx.progress.complete and verdict is not None and verdict.status is VerdictStatus.PASS:
            return StopDecision(True, '强制验收条全部满足，裁决是 pass')
        missing = sorted(set(ctx.progress.mandatory) - set(ctx.progress.satisfied))
        if not missing:
            return StopDecision(False)
        # 这段话进历史、也就进了此后每次请求的前缀：条目再多也只点名前几条。
        named = '、'.join(missing[:_NAMED])
        more = f' 等 {len(missing)} 条' if len(missing) > _NAMED else ''
        return StopDecision(
            False,
            f'还有 {len(missing)} 条强制验收条没满足（{named}{more}）：接着把它们做完再交付。',
        )
