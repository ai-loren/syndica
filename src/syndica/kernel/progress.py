"""进度账本（spec §8.3）：进度只来自验收裁决的逐条结论，不来自 Agent 的自述。

- **分母**是生效的强制 checkable 条；一条强制的都没有时退回「全部生效的 checkable」（投影可能把什么
  都标成非强制，分母为空时停滞检测就整个哑了），并报一次。judgment 的强制条列为待人判，不进比例。
- **回退**只算「上次满足、这次 UNMET」，而且只比两次都在的那些条：验收器没能确认（UNCONFIRMED）是噪
  声，不是证据；Rubric 集合变了（重新投影、人确认了一条反馈）是要求变了，不是做坏了——集合一变就重新
  起算。
- **停滞**按「自上次进度提升以来过了几轮」算，而且这段时间里至少量过一次：一次都没量过，说的是不知
  道，不是没进展。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from syndica.kernel.acceptance import (
    OutcomeKind,
    Rubric,
    RubricKind,
    RubricStatus,
    Verdict,
    VerdictStatus,
)
from syndica.kernel.long_horizon import ProgressSnapshot

__all__ = ['ProgressLedger', 'ProgressRecord']


@dataclass(frozen=True)
class ProgressRecord:
    """一次量度：此刻的快照、相对上一次回退了哪些条、是否比此前最好的更好、分母是不是退回来的。"""

    snapshot: ProgressSnapshot
    regressed: tuple[str, ...] = ()
    improved: bool = False
    fallback: bool = False


def _denominator(rubrics: Sequence[Rubric]) -> tuple[list[Rubric], bool]:
    active = [r for r in rubrics if r.status is RubricStatus.ACTIVE]
    mandatory = [r for r in active if r.mandatory and r.kind is RubricKind.CHECKABLE]
    if mandatory:
        return mandatory, False
    checkable = [r for r in active if r.kind is RubricKind.CHECKABLE]
    return checkable, bool(checkable)


class ProgressLedger:
    """一个 Agent 的进度：每次裁决记一条，交回快照与回退。不持久化（与预算同按「这次调用」）。"""

    def __init__(self) -> None:
        self._digest: str | None = None
        self._outcomes: dict[str, OutcomeKind] = {}
        self._best = -1
        self._since = 0
        self._measured_since = False
        self.latest: ProgressSnapshot | None = None

    def record(self, verdict: Verdict, rubrics: Sequence[Rubric], turn: int) -> ProgressRecord:
        counted, fallback = _denominator(rubrics)
        ids = tuple(sorted(r.id for r in counted))
        digest = hashlib.sha256('\n'.join(ids).encode('utf-8')).hexdigest()
        outcomes = {o.rubric_id: o.kind for o in verdict.outcomes if o.rubric_id in ids}
        if verdict.status is VerdictStatus.UNVERIFIED:
            # 验收器没能判（超时、出错、预算用完、没有可查的条）：它补上的逐条 UNCONFIRMED
            # 不是结论，这一次什么都没量到，不是零进展。
            snapshot = self.latest or ProgressSnapshot((), ids, (), None)
            return ProgressRecord(snapshot, fallback=fallback)
        satisfied = tuple(i for i in ids if outcomes.get(i) is OutcomeKind.SATISFIED)
        pending = tuple(
            sorted(
                r.id
                for r in rubrics
                if r.status is RubricStatus.ACTIVE and r.mandatory and r.kind is RubricKind.JUDGMENT
            )
        )
        regressed: tuple[str, ...] = ()
        if digest == self._digest:
            regressed = tuple(
                i
                for i in ids
                if self._outcomes.get(i) is OutcomeKind.SATISFIED
                and outcomes.get(i) is OutcomeKind.UNMET
            )
        else:
            self._digest = digest
            self._best = -1
            self._outcomes = {}
        improved = len(satisfied) > self._best
        if improved:
            self._best = len(satisfied)
            self._since = turn
            self._measured_since = False
        else:
            self._measured_since = True
        self._outcomes.update(outcomes)
        snapshot = ProgressSnapshot(
            satisfied=satisfied, mandatory=ids, pending_human=pending, measured_turn=turn
        )
        self.latest = snapshot
        return ProgressRecord(snapshot, regressed, improved, fallback)

    def stalled_turns(self, turn: int) -> int | None:
        """自上次进度提升以来过了几轮；其间一次都没再量过就是 None（不知道，不是停滞）。"""
        if self.latest is None or not self._measured_since:
            return None
        return turn - self._since
