"""Goal / Rubric 写入的脱敏收口（§7.6 / §12.3）。

Goal 与 Rubric 的文本可能含用户原话、任务说明摘录、锚点引文——落盘前先逐叶脱敏（与会话、记忆、trace
同一招）。`RubricStore` 的写入面只收本模块铸造的 `RedactedGoal` / `RedactedRubric`，第三方 store
绕不过脱敏。只脱**文本**（intent / statement / 锚点 quote），不动结构字段（id / locator / source /
kind / status / trust）——它们不是自由文本，且锚点比对要对脱敏不敏感（两侧都取脱敏后文本再比）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import final

from syndica.kernel.acceptance import Anchor, Goal, Rubric
from syndica.kernel.errors import ContractViolation
from syndica.kernel.redaction import Redactor

__all__ = [
    'RedactedGoal',
    'RedactedRubric',
    'redacted_goal',
    'redacted_rubric',
]

_MINT = object()


def _redact_anchor(anchor: Anchor, redactor: Redactor) -> Anchor:
    return replace(anchor, quote=redactor.redact(anchor.quote))


@final
class RedactedGoal:
    """`redacted_goal` 的输出：脱敏过的 Goal。"""

    __slots__ = ('goal',)

    def __init__(self, goal: Goal, *, _mint: object = None) -> None:
        if _mint is not _MINT:
            raise ContractViolation(
                expected='RedactedGoal 只由 redacted_goal 构造',
                received='有代码直接构造了它',
                next_action='把 goal 交给 redacted_goal(goal, redactor)',
            )
        self.goal = goal


@final
class RedactedRubric:
    """`redacted_rubric` 的输出：脱敏过的一条 Rubric。"""

    __slots__ = ('rubric',)

    def __init__(self, rubric: Rubric, *, _mint: object = None) -> None:
        if _mint is not _MINT:
            raise ContractViolation(
                expected='RedactedRubric 只由 redacted_rubric 构造',
                received='有代码直接构造了它',
                next_action='把 rubric 交给 redacted_rubric(rubric, redactor)',
            )
        self.rubric = rubric


def redacted_goal(goal: Goal, redactor: Redactor) -> RedactedGoal:
    redacted = replace(
        goal,
        intent=redactor.redact(goal.intent),
        anchors=tuple(_redact_anchor(a, redactor) for a in goal.anchors),
    )
    return RedactedGoal(redacted, _mint=_MINT)


def redacted_rubric(rubric: Rubric, redactor: Redactor) -> RedactedRubric:
    redacted = replace(
        rubric,
        statement=redactor.redact(rubric.statement),
        anchors=tuple(_redact_anchor(a, redactor) for a in rubric.anchors),
    )
    return RedactedRubric(redacted, _mint=_MINT)
