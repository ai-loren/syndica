"""进度账本（spec §8.3）：进度只来自验收的确定结论，没能确认的不覆盖证据，回退只算「满足 → UNMET」，
停滞要量过才算。"""

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
from syndica.kernel.progress import ProgressLedger, denominator_ids

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
        _rubric('J2', kind=RubricKind.JUDGMENT, mandatory=False),
        _rubric('J3', kind=RubricKind.JUDGMENT, status=RubricStatus.PROPOSED),
        _rubric('R4', status=RubricStatus.PROPOSED),
        _rubric('R5'),
    ]
    record = ProgressLedger().record(
        _verdict(rubrics, R1=S, R2=S, R3=C, R5=S), rubrics, turn=3, milestone=True
    )
    snapshot = record.snapshot
    assert snapshot.mandatory == ('R1', 'R3', 'R5') == denominator_ids(rubrics)
    assert snapshot.satisfied == ('R1', 'R5')
    assert snapshot.pending_human == ('J1',)
    assert snapshot.ratio == 2 / 3 and not snapshot.complete
    assert snapshot.measured_turn == 3 and not record.fallback


def test_without_mandatory_checkable_rubrics_every_active_checkable_counts() -> None:
    rubrics = [_rubric('R1', mandatory=False), _rubric('R2', mandatory=False)]
    record = ProgressLedger().record(_verdict(rubrics, R1=S, R2=C), rubrics, turn=1, milestone=True)
    assert record.fallback
    assert record.snapshot.mandatory == ('R1', 'R2') and record.snapshot.satisfied == ('R1',)


def test_with_nothing_checkable_the_ratio_is_unknown_not_zero() -> None:
    rubrics = [_rubric('J1', kind=RubricKind.JUDGMENT)]
    snapshot = ProgressLedger().record(_verdict(rubrics), rubrics, turn=1, milestone=True).snapshot
    assert snapshot.ratio is None and not snapshot.complete


def test_only_satisfied_then_unmet_is_a_regression() -> None:
    rubrics = [_rubric('R1'), _rubric('R2'), _rubric('R3')]
    ledger = ProgressLedger()
    ledger.record(_verdict(rubrics, R1=S, R2=S, R3=C), rubrics, turn=1, milestone=True)
    record = ledger.record(_verdict(rubrics, R1=U, R2=C, R3=U), rubrics, turn=3, milestone=True)
    assert record.regressed == ('R1',)


def test_an_unconfirmed_measurement_in_between_does_not_hide_a_regression() -> None:
    rubrics = [_rubric('R1')]
    ledger = ProgressLedger()
    ledger.record(_verdict(rubrics, R1=S), rubrics, turn=1, milestone=True)
    ledger.record(_verdict(rubrics, R1=C), rubrics, turn=3, milestone=True)
    record = ledger.record(_verdict(rubrics, R1=U), rubrics, turn=5, milestone=True)
    assert record.regressed == ('R1',)


def test_an_unconfirmed_measurement_does_not_take_satisfied_rubrics_away() -> None:
    rubrics = [_rubric('R1'), _rubric('R2')]
    ledger = ProgressLedger()
    ledger.record(_verdict(rubrics, R1=S, R2=S), rubrics, turn=1, milestone=True)
    record = ledger.record(_verdict(rubrics, R1=C, R2=C), rubrics, turn=3, milestone=True)
    assert record.snapshot.satisfied == ('R1', 'R2') and record.snapshot.complete


def test_a_changed_rubric_set_starts_over_instead_of_reporting_a_regression() -> None:
    before = [_rubric('R1'), _rubric('R2')]
    after = [_rubric('R1'), _rubric('R3')]
    ledger = ProgressLedger()
    ledger.record(_verdict(before, R1=S, R2=S), before, turn=1, milestone=True)
    record = ledger.record(_verdict(after, R1=U, R3=C), after, turn=2, milestone=True)
    assert record.regressed == ()
    assert record.improved and record.snapshot.satisfied == ()


def test_an_unverified_verdict_measures_nothing() -> None:
    rubrics = [_rubric('R1')]
    ledger = ProgressLedger()
    ledger.record(_verdict(rubrics, R1=S), rubrics, turn=1, milestone=True)
    failed = derive_verdict(rubrics, {}, failure='verifier_timeout')
    record = ledger.record(failed, rubrics, turn=5, milestone=True)
    assert not record.measured
    assert record.snapshot.satisfied == ('R1',) and record.snapshot.measured_turn == 1
    assert ledger.stalled_turns(9) is None


def test_a_completion_verdict_without_a_definite_outcome_measures_nothing() -> None:
    rubrics = [_rubric('R1')]
    ledger = ProgressLedger()
    record = ledger.record(_verdict(rubrics, R1=C), rubrics, turn=2, milestone=False)
    assert not record.measured and ledger.latest is None


def test_a_milestone_that_confirms_nothing_still_counts_toward_a_stall() -> None:
    rubrics = [_rubric('R1'), _rubric('R2')]
    ledger = ProgressLedger()
    assert ledger.stalled_turns(4) is None
    ledger.record(_verdict(rubrics, R1=C, R2=C), rubrics, turn=3, milestone=True)
    assert ledger.stalled_turns(10) is None
    ledger.record(_verdict(rubrics, R1=C, R2=C), rubrics, turn=5, milestone=True)
    assert ledger.stalled_turns(7) == 4
    ledger.record(_verdict(rubrics, R1=S, R2=C), rubrics, turn=8, milestone=True)
    assert ledger.stalled_turns(12) is None


def test_matching_the_best_so_far_is_not_an_improvement() -> None:
    rubrics = [_rubric('R1'), _rubric('R2')]
    ledger = ProgressLedger()
    first = ledger.record(_verdict(rubrics, R1=S, R2=U), rubrics, turn=1, milestone=True)
    swapped = ledger.record(_verdict(rubrics, R1=U, R2=S), rubrics, turn=4, milestone=True)
    assert first.improved and not swapped.improved
    assert ledger.stalled_turns(6) == 5
