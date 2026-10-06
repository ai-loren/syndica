"""基线策略（spec §8.6）：只在完成判据成立时说停。"""

from __future__ import annotations

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
from syndica.kernel.long_horizon import BaselineStrategy, LongHorizonContext, ProgressSnapshot

_A = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'q'),)
_R1 = Rubric(id='R1', statement='s1', kind=RubricKind.CHECKABLE, anchors=_A)
_R2 = Rubric(id='R2', statement='s2', kind=RubricKind.CHECKABLE, anchors=_A, mandatory=False)


def _ctx(verdict: object) -> LongHorizonContext:
    return LongHorizonContext(
        agent_id='a1',
        cycle=1,
        turn=5,
        goal=Goal(intent='g'),
        rubrics=(_R1, _R2),
        progress=ProgressSnapshot(satisfied=('R1',), mandatory=('R1',), measured_turn=5),
        budget=None,
        stalled_turns=0,
        verdict=verdict,  # type: ignore[arg-type]
    )


async def test_it_stops_only_when_every_mandatory_rubric_is_met_and_the_verdict_passed() -> None:
    satisfied = RubricOutcome('R1', OutcomeKind.SATISFIED, _A)
    passed = derive_verdict([_R1], {'R1': satisfied})
    partial = derive_verdict([_R1, _R2], {'R1': satisfied})
    strategy = BaselineStrategy()
    assert (await strategy.should_stop(_ctx(passed))).stop
    assert not (await strategy.should_stop(_ctx(partial))).stop
    assert not (await strategy.should_stop(_ctx(None))).stop


def _many(count: int, satisfied: int) -> LongHorizonContext:
    ids = tuple(f'R{i:02d}' for i in range(1, count + 1))
    return LongHorizonContext(
        agent_id='a1',
        cycle=1,
        turn=5,
        goal=Goal(intent='g'),
        rubrics=(),
        progress=ProgressSnapshot(satisfied=ids[:satisfied], mandatory=ids, measured_turn=5),
        budget=None,
        stalled_turns=0,
    )


async def test_it_names_the_first_ten_missing_rubrics_and_counts_the_rest() -> None:
    reason = (await BaselineStrategy().should_stop(_many(30, 5))).reason
    assert 'R06、R07、R08、R09、R10、R11、R12、R13、R14、R15 等 25 条' in reason
    assert 'R16' not in reason


async def test_with_ten_or_fewer_missing_it_names_them_all_without_a_count() -> None:
    reason = (await BaselineStrategy().should_stop(_many(12, 2))).reason
    assert 'R03、R04、R05、R06、R07、R08、R09、R10、R11、R12）' in reason
    assert '等' not in reason


async def test_with_everything_met_but_no_pass_it_asks_for_nothing() -> None:
    satisfied = RubricOutcome('R1', OutcomeKind.SATISFIED, _A)
    partial = derive_verdict([_R1, _R2], {'R1': satisfied})
    decision = await BaselineStrategy().should_stop(_ctx(partial))
    assert not decision.stop and decision.reason == ''
