"""进度账本（spec §8.3）：进度只来自验收的逐条结论，回退只算「满足 → UNMET」，停滞要量过才算。"""

from __future__ import annotations

from syndica.kernel.acceptance import (
    Anchor,
    AnchorSource,
    OutcomeKind,
    Rubric,
    RubricKind,
    RubricOutcome,
    RubricStatus,
    derive_verdict,
)
from syndica.kernel.progress import ProgressLedger

_A = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'q'),)
_EVIDENCE = (Anchor(AnchorSource.USER_MESSAGE, 'reply', 'done'),)


def _rubric(
    rid: str,
    *,
    kind: RubricKind = RubricKind.CHECKABLE,
    mandatory: bool = True,
    status: RubricStatus = RubricStatus.ACTIVE,
) -> Rubric:
    return Rubric(
        id=rid,
        statement=f'statement {rid}',
        kind=kind,
        anchors=_A,
        mandatory=mandatory,
        status=status,
    )


def _verdict(rubrics: list[Rubric], **results: OutcomeKind):  # type: ignore[no-untyped-def]
    reported = {
        rid: RubricOutcome(rid, kind, _EVIDENCE if kind is not OutcomeKind.UNCONFIRMED else ())
        for rid, kind in results.items()
    }
    return derive_verdict(rubrics, reported)


S, U, C = OutcomeKind.SATISFIED, OutcomeKind.UNMET, OutcomeKind.UNCONFIRMED


def test_the_ratio_counts_only_mandatory_checkable_rubrics() -> None:
    rubrics = [
        _rubric('R3'),
        _rubric('R1'),
        _rubric('R2', mandatory=False),
        _rubric('J1', kind=RubricKind.JUDGMENT),
        _rubric('R4', status=RubricStatus.PROPOSED),
        _rubric('R5'),
    ]
    record = ProgressLedger().record(_verdict(rubrics, R1=S, R2=S, R3=C, R5=S), rubrics, turn=3)
    snapshot = record.snapshot
    assert snapshot.mandatory == ('R1', 'R3', 'R5')
    assert snapshot.satisfied == ('R1', 'R5')
    assert snapshot.pending_human == ('J1',)
    assert snapshot.ratio == 2 / 3 and not snapshot.complete
    assert snapshot.measured_turn == 3 and not record.fallback


def test_without_mandatory_checkable_rubrics_every_active_checkable_counts() -> None:
    rubrics = [_rubric('R1', mandatory=False), _rubric('R2', mandatory=False)]
    record = ProgressLedger().record(_verdict(rubrics, R1=S, R2=C), rubrics, turn=1)
    assert record.fallback
    assert record.snapshot.mandatory == ('R1', 'R2') and record.snapshot.satisfied == ('R1',)


def test_with_nothing_checkable_the_ratio_is_unknown_not_zero() -> None:
    rubrics = [_rubric('J1', kind=RubricKind.JUDGMENT)]
    snapshot = ProgressLedger().record(_verdict(rubrics), rubrics, turn=1).snapshot
    assert snapshot.ratio is None and not snapshot.complete


def test_only_satisfied_then_unmet_is_a_regression() -> None:
    rubrics = [_rubric('R1'), _rubric('R2'), _rubric('R3')]
    ledger = ProgressLedger()
    ledger.record(_verdict(rubrics, R1=S, R2=S, R3=C), rubrics, turn=1)
    record = ledger.record(_verdict(rubrics, R1=U, R2=C, R3=U), rubrics, turn=3)
    assert record.regressed == ('R1',)


def test_a_changed_rubric_set_starts_over_instead_of_reporting_a_regression() -> None:
    before = [_rubric('R1'), _rubric('R2')]
    after = [_rubric('R1'), _rubric('R3')]
    ledger = ProgressLedger()
    ledger.record(_verdict(before, R1=S, R2=S), before, turn=1)
    record = ledger.record(_verdict(after, R1=U, R3=C), after, turn=2)
    assert record.regressed == ()
    assert record.improved


def test_an_unverified_verdict_without_outcomes_measures_nothing() -> None:
    rubrics = [_rubric('R1')]
    ledger = ProgressLedger()
    ledger.record(_verdict(rubrics, R1=S), rubrics, turn=1)
    failed = derive_verdict(rubrics, {}, failure='verifier_timeout')
    record = ledger.record(failed, rubrics, turn=5)
    assert record.snapshot.satisfied == ('R1',) and record.snapshot.measured_turn == 1
    assert ledger.stalled_turns(9) is None


def test_stalling_needs_a_measurement_after_the_last_improvement() -> None:
    rubrics = [_rubric('R1'), _rubric('R2')]
    ledger = ProgressLedger()
    assert ledger.stalled_turns(4) is None
    ledger.record(_verdict(rubrics, R1=C, R2=C), rubrics, turn=3)
    assert ledger.stalled_turns(10) is None
    ledger.record(_verdict(rubrics, R1=C, R2=C), rubrics, turn=5)
    assert ledger.stalled_turns(7) == 4
    ledger.record(_verdict(rubrics, R1=S, R2=C), rubrics, turn=8)
    assert ledger.stalled_turns(12) is None


def test_matching_the_best_so_far_is_not_an_improvement() -> None:
    rubrics = [_rubric('R1'), _rubric('R2')]
    ledger = ProgressLedger()
    first = ledger.record(_verdict(rubrics, R1=S, R2=C), rubrics, turn=1)
    swapped = ledger.record(_verdict(rubrics, R1=C, R2=S), rubrics, turn=4)
    assert first.improved and not swapped.improved
    assert ledger.stalled_turns(6) == 5
