"""验收的四值裁决推导（§7.5 / #45）：不确定不塌缩成 pass、judgment 不塌缩、空集合是 unverified。"""

from __future__ import annotations

import pytest

from syndica.kernel.acceptance import (
    Anchor,
    AnchorSource,
    OutcomeKind,
    Rubric,
    RubricKind,
    RubricOutcome,
    RubricStatus,
    VerdictStatus,
    derive_verdict,
    rubric_trust,
)
from syndica.kernel.context import TrustLevel
from syndica.kernel.errors import ContractViolation

_A = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', '做一份报告'),)


def _checkable(rid: str) -> Rubric:
    return Rubric(id=rid, statement=rid, kind=RubricKind.CHECKABLE, anchors=_A)


def _judgment(rid: str) -> Rubric:
    return Rubric(id=rid, statement=rid, kind=RubricKind.JUDGMENT, anchors=_A)


def test_anchor_source_name_to_value_map_is_exactly_this() -> None:
    assert {m.name: m.value for m in AnchorSource} == {
        'USER_MESSAGE': 'user_message',
        'FILE': 'file',
        'ARTIFACT': 'artifact',
    }


def test_rubric_kind_name_to_value_map_is_exactly_this() -> None:
    assert {m.name: m.value for m in RubricKind} == {
        'CHECKABLE': 'checkable',
        'JUDGMENT': 'judgment',
    }


def test_rubric_status_name_to_value_map_is_exactly_this() -> None:
    assert {m.name: m.value for m in RubricStatus} == {'ACTIVE': 'active', 'PROPOSED': 'proposed'}


def test_outcome_kind_name_to_value_map_is_exactly_this() -> None:
    assert {m.name: m.value for m in OutcomeKind} == {
        'SATISFIED': 'satisfied',
        'UNMET': 'unmet',
        'UNCONFIRMED': 'unconfirmed',
        'NEEDS_HUMAN': 'needs_human',
    }


def test_verdict_status_name_to_value_map_is_exactly_this() -> None:
    # unverified 与 pass 取值撞车就静默成别名，导出器便把「没验证」当「通过」（§13 最隐蔽的污染）。
    assert {m.name: m.value for m in VerdictStatus} == {
        'PASS': 'pass',
        'PARTIAL': 'partial',
        'BLOCK': 'block',
        'UNVERIFIED': 'unverified',
    }


def test_an_empty_active_rubric_set_is_unverified_not_pass() -> None:
    # RubricsBuilder 失败 / 全被锚点规则拒 / 只开 L3 不开 Rubric → active 集合为空；「每条都满足」
    # 空真不是 pass（评审 B3）。
    verdict = derive_verdict([], {})
    assert verdict.status is VerdictStatus.UNVERIFIED
    assert verdict.unverified_cause == 'no_active_rubrics'


def test_only_proposed_rubrics_is_also_unverified() -> None:
    proposed = Rubric('r1', 'x', RubricKind.CHECKABLE, _A, status=RubricStatus.PROPOSED)
    verdict = derive_verdict([proposed], {'r1': RubricOutcome('r1', OutcomeKind.SATISFIED)})
    assert verdict.status is VerdictStatus.UNVERIFIED


def test_verifier_failure_is_unverified_carrying_the_cause() -> None:
    verdict = derive_verdict([_checkable('r1')], {}, failure='verifier_timeout')
    assert verdict.status is VerdictStatus.UNVERIFIED
    assert verdict.unverified_cause == 'verifier_timeout'


def test_all_checkable_satisfied_is_pass() -> None:
    rubrics = [_checkable('r1'), _checkable('r2')]
    reported = {rid: RubricOutcome(rid, OutcomeKind.SATISFIED) for rid in ('r1', 'r2')}
    assert derive_verdict(rubrics, reported).status is VerdictStatus.PASS


def test_an_unmet_with_evidence_is_block() -> None:
    reported = {'r1': RubricOutcome('r1', OutcomeKind.UNMET, _A)}
    assert derive_verdict([_checkable('r1')], reported).status is VerdictStatus.BLOCK


def test_an_unconfirmed_checkable_is_partial_not_block() -> None:
    reported = {'r1': RubricOutcome('r1', OutcomeKind.UNCONFIRMED)}
    assert derive_verdict([_checkable('r1')], reported).status is VerdictStatus.PARTIAL


def test_block_outranks_partial() -> None:
    # 基数 ≥ 2、既有 unmet 又有 unconfirmed：block（已证伪）压过 partial（没确认）。
    rubrics = [_checkable('r1'), _checkable('r2')]
    reported = {
        'r1': RubricOutcome('r1', OutcomeKind.UNMET, _A),
        'r2': RubricOutcome('r2', OutcomeKind.UNCONFIRMED),
    }
    assert derive_verdict(rubrics, reported).status is VerdictStatus.BLOCK


def test_a_judgment_rubric_is_forced_to_needs_human_whatever_the_verifier_reports() -> None:
    # 验收器谎报 judgment 条 satisfied：内核无视、恒 needs_human，不进 checkable 状态（评审 B4）。
    rubrics = [_judgment('j1')]
    reported = {'j1': RubricOutcome('j1', OutcomeKind.SATISFIED)}
    verdict = derive_verdict(rubrics, reported)
    assert verdict.needs_human == ('j1',)
    assert all(o.kind is OutcomeKind.NEEDS_HUMAN for o in verdict.outcomes)
    # 只有 judgment 条、没有 checkable 条 → unverified，不是 pass。
    assert verdict.status is VerdictStatus.UNVERIFIED


def test_needs_human_does_not_drag_a_passing_checkable_set_down() -> None:
    rubrics = [_checkable('r1'), _judgment('j1')]
    reported = {'r1': RubricOutcome('r1', OutcomeKind.SATISFIED)}
    verdict = derive_verdict(rubrics, reported)
    assert verdict.status is VerdictStatus.PASS  # checkable 全满足；judgment 不拖累状态
    assert verdict.needs_human == ('j1',)


def test_a_verifier_cannot_mark_a_checkable_rubric_needs_human() -> None:
    # 验收器把 checkable 条报成 needs_human（它判不了）→ 降级 UNCONFIRMED（→ partial），不塌缩。
    reported = {'r1': RubricOutcome('r1', OutcomeKind.NEEDS_HUMAN)}
    verdict = derive_verdict([_checkable('r1')], reported)
    assert verdict.status is VerdictStatus.PARTIAL


def test_a_missing_report_for_a_checkable_rubric_is_unconfirmed() -> None:
    verdict = derive_verdict([_checkable('r1')], {})
    assert verdict.status is VerdictStatus.PARTIAL


def test_a_rubric_without_anchors_is_refused() -> None:
    with pytest.raises(ContractViolation, match='anchors'):
        Rubric(id='r1', statement='x', kind=RubricKind.CHECKABLE, anchors=())


def test_an_unmet_outcome_without_evidence_is_refused() -> None:
    with pytest.raises(ContractViolation, match='UNMET'):
        RubricOutcome('r1', OutcomeKind.UNMET)


def test_rubric_trust_is_the_strictest_input() -> None:
    assert rubric_trust([TrustLevel.TRUSTED, TrustLevel.TRUSTED]) is TrustLevel.TRUSTED
    assert rubric_trust([TrustLevel.TRUSTED, TrustLevel.UNTRUSTED]) is TrustLevel.UNTRUSTED
    assert rubric_trust([]) is TrustLevel.TRUSTED
