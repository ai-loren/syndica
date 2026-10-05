"""Goal / Rubric 的落盘格式（M7 评审 H8）：全字段 round-trip、读回 fail-closed、新格式不猜着读。"""

from __future__ import annotations

import pytest

from syndica.kernel.acceptance import (
    Anchor,
    AnchorSource,
    Goal,
    Rubric,
    RubricKind,
    RubricOrigin,
    RubricStatus,
)
from syndica.kernel.context import TrustLevel
from syndica.kernel.errors import ConfigError
from syndica.kernel.rubric_serde import (
    NewerRubricSchema,
    goal_from_json,
    goal_to_json,
    rubrics_from_json,
    rubrics_to_json,
)

_A = (Anchor(AnchorSource.FILE, 'spec.md:12', 'the exact text'),)


def _judgment() -> Rubric:
    return Rubric(
        id='F1',
        statement='reads well',
        kind=RubricKind.JUDGMENT,
        anchors=_A,
        mandatory=False,
        status=RubricStatus.PROPOSED,
        trust=TrustLevel.UNTRUSTED,
        origin=RubricOrigin.FEEDBACK,
    )


def test_a_rubric_round_trips_every_field() -> None:
    assert rubrics_from_json(rubrics_to_json([_judgment()])) == [_judgment()]


def test_a_goal_round_trips_its_trust() -> None:
    goal = Goal(intent='g', anchors=_A, trust=TrustLevel.UNTRUSTED)
    assert goal_from_json(goal_to_json(goal)) == goal


@pytest.mark.parametrize('value', [None, 'trusted-ish', 1, True])
def test_a_missing_or_unknown_rubric_trust_reads_back_untrusted(value: object) -> None:
    # 反序列化不把未知当可信（§12.2）：读回成 TRUSTED 就是一条洗白路径。
    data = rubrics_to_json([_judgment()])
    entry = data['rubrics'][0]  # type: ignore[index]
    if value is None:
        del entry['trust']  # type: ignore[attr-defined]
    else:
        entry['trust'] = value  # type: ignore[index]
    assert rubrics_from_json(data)[0].trust is TrustLevel.UNTRUSTED  # type: ignore[arg-type]


def test_a_missing_goal_trust_reads_back_untrusted() -> None:
    data = goal_to_json(Goal(intent='g'))
    del data['trust']
    assert goal_from_json(data).trust is TrustLevel.UNTRUSTED  # type: ignore[arg-type]


@pytest.mark.parametrize('field', ['kind', 'status', 'origin'])
def test_an_unknown_enum_value_is_a_corrupt_file(field: str) -> None:
    # 把 judgment 读成 checkable、把反馈读成投影，都能悄悄改掉验收语义：不认识就报坏文件，不猜。
    data = rubrics_to_json([_judgment()])
    data['rubrics'][0][field] = 'mystery'  # type: ignore[index]
    with pytest.raises(ConfigError):
        rubrics_from_json(data)  # type: ignore[arg-type]


def test_a_newer_schema_is_refused() -> None:
    data = rubrics_to_json([_judgment()])
    data['schema'] = 'syndica.rubrics/2'
    with pytest.raises(NewerRubricSchema):
        rubrics_from_json(data)  # type: ignore[arg-type]


def test_a_rubric_without_anchors_is_a_corrupt_file() -> None:
    data = rubrics_to_json([_judgment()])
    data['rubrics'][0]['anchors'] = []  # type: ignore[index]
    with pytest.raises(ConfigError):
        rubrics_from_json(data)  # type: ignore[arg-type]
