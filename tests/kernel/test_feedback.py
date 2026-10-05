"""人的反馈升级为标准（§7.7）：只认人发的、提议不等于生效、人确认后才进验收、重投影不冲掉它。

本模块保留 `from __future__ import annotations` 与仓库其余模块一致。
"""

from __future__ import annotations

import json

from syndica.kernel.acceptance import (
    Anchor,
    AnchorSource,
    Rubric,
    RubricKind,
    RubricStatus,
)
from syndica.kernel.context import TrustLevel
from syndica.kernel.feedback import is_human_feedback, propose_feedback_rubric
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.rubric_redaction import redacted_rubric
from syndica.kernel.stores.memory_rubric import InMemoryRubricStore

_FEEDBACK = '这张表第 3 列口径不对，要用已确认收入'
_REDACTOR = RuleRedactor()


def test_a_message_from_an_agent_is_not_human_feedback() -> None:
    # SendMessage 由内核盖上发送 Agent 的 id：别的 Agent 冒充不了人（评审 H11）。
    agents = {'a1', 'a2'}
    assert is_human_feedback('runtime', agents) is True
    assert is_human_feedback('a1', agents) is False
    assert is_human_feedback('a2', agents) is False


async def _propose(store: InMemoryRubricStore, **kwargs: object) -> Rubric | None:
    return await propose_feedback_rubric(
        scope='s1',
        rubric_id='F1',
        feedback=_FEEDBACK,
        locator='msg:7',
        store=store,
        redactor=_REDACTOR,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_a_proposal_is_stored_but_not_active() -> None:
    # 提议 ≠ 生效：进库是 proposed，默认验收看不到它（§7.7）。
    store = InMemoryRubricStore()
    rubric = await _propose(store)
    assert rubric is not None and rubric.status is RubricStatus.PROPOSED
    assert await store.rubrics('s1') == []
    assert [r.id for r in await store.rubrics('s1', include_proposed=True)] == ['F1']


async def test_a_confirmed_proposal_enters_acceptance() -> None:
    store = InMemoryRubricStore()
    await _propose(store)
    await store.activate('s1', 'F1')  # 人确认
    assert [r.id for r in await store.rubrics('s1')] == ['F1']


async def test_the_feedback_itself_is_the_anchor() -> None:
    store = InMemoryRubricStore()
    rubric = await _propose(store)
    assert rubric is not None
    assert rubric.anchors[0] == Anchor(AnchorSource.USER_MESSAGE, 'msg:7', _FEEDBACK)


async def test_an_artifact_location_from_the_feedback_is_anchored_too() -> None:
    store = InMemoryRubricStore()
    rubric = await _propose(store, artifacts={'sales.xlsx#col-3': 'col 3'})
    assert rubric is not None
    assert [a.source for a in rubric.anchors] == [AnchorSource.USER_MESSAGE, AnchorSource.ARTIFACT]


async def test_without_a_model_the_feedback_is_kept_verbatim_as_a_judgment() -> None:
    # 拿不准时宁可要人判：原话即标准、按 judgment，不会被自动判通过。
    store = InMemoryRubricStore()
    rubric = await _propose(store)
    assert rubric is not None
    assert rubric.statement == _FEEDBACK
    assert rubric.kind is RubricKind.JUDGMENT


async def test_a_model_can_rephrase_a_checkable_standard() -> None:
    store = InMemoryRubricStore()
    reply = json.dumps(
        {'statement': 'sales.xlsx 第 3 列使用「已确认收入」口径', 'kind': 'checkable'}
    )
    rubric = await _propose(store, llm=ScriptedLLM([ScriptedTurn(content=reply)]))
    assert rubric is not None
    assert rubric.kind is RubricKind.CHECKABLE
    assert '已确认收入' in rubric.statement


async def test_a_broken_proposer_falls_back_to_verbatim_judgment() -> None:
    store = InMemoryRubricStore()
    rubric = await _propose(store, llm=ScriptedLLM([ScriptedTurn(content='no json')]))
    assert rubric is not None
    assert rubric.statement == _FEEDBACK and rubric.kind is RubricKind.JUDGMENT


async def test_untrusted_feedback_yields_an_untrusted_rubric() -> None:
    store = InMemoryRubricStore()
    rubric = await _propose(store, trust=TrustLevel.UNTRUSTED)
    assert rubric is not None and rubric.trust is TrustLevel.UNTRUSTED


async def test_reprojection_does_not_wipe_a_confirmed_feedback_rubric() -> None:
    # Goal 变更重新投影是 upsert：人确认过的反馈（别的 id）存活（评审 H10）。
    store = InMemoryRubricStore()
    await _propose(store)
    await store.activate('s1', 'F1')
    projected = Rubric(
        id='R1',
        statement='has section 4',
        kind=RubricKind.CHECKABLE,
        anchors=(Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'x'),),
    )
    await store.put_rubrics('s1', [redacted_rubric(projected, _REDACTOR)])
    assert {r.id for r in await store.rubrics('s1')} == {'R1', 'F1'}
