"""内核与长程 Module 之间的接缝（私有）：turn 循环在固定的几处问控制器，控制器只交回要追加的块与几个
请求，署名与信任由内核定。

问的几处（计划 P1 M5 的轮内顺序）：轮开始的预算判定计过这一轮之后问 `wrap_up_requested`（策略的
`on_budget_low` 交回了 WRAP_UP，这一轮就是收尾轮）；不是收尾轮时，压缩与模型调用之前问
`before_model`，它的伴生花过的预算随后重判一次；`before_model` 之后问 `final_requested`（
`should_stop` 在完成判据成立时被兑现：这一轮只交付）；每次完成提议得出裁决之后交 `record_verdict`（
打回与否都交）；完成提议没被打回之后问 `after_completion`（交回一段话就接着干，None 就照常完成）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from syndica.kernel.context import Context, TrustLevel
from syndica.kernel.history_blocks import HistoryKind

__all__ = ['LongHorizonBlock', 'LongHorizonController']


@dataclass(frozen=True)
class LongHorizonBlock:
    """控制器要追加的一块。`trust` 是控制器给的下限：内核取它与主轨迹溯源里最严的那个。"""

    kind: HistoryKind
    text: str
    key: str
    trust: TrustLevel = TrustLevel.TRUSTED


class LongHorizonController(Protocol):
    def wrap_up_requested(self) -> bool: ...

    async def before_model(self, ctx: Context, turn: int) -> Sequence[LongHorizonBlock]: ...

    def final_requested(self) -> bool: ...

    async def record_verdict(self, ctx: Context, turn: int) -> None: ...

    async def after_completion(self, ctx: Context, turn: int) -> LongHorizonBlock | None: ...
