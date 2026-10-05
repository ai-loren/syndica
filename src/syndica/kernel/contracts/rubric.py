"""RubricStore 契约。任何实现都必须通过（spec §13.3 / §7.6 / §8.2）。

一次钉全：Goal round-trip（含脱敏与信任）、按 scope 隔离（基数 2、跨同一对象两次读）、
`put_rubrics` 替换投影出的那批（删旧的 projected、不碰反馈）、active / proposed 过滤、
propose 恒存 proposed、activate 才生效、全字段 round-trip（judgment / 不可信 / 非强制 / 来源）、
登记顺序、三个写者并发不丢人的确认。全字段 round-trip 是承重的：第三方 store 把 judgment
读回成 checkable、或把信任读成可信，就能把「需人工确认」洗成通过。

先私有（随 wheel 发布但不进 kernel.__all__）。

**运行前提**见 `syndica.kernel.contracts.base`。
"""

from __future__ import annotations

import asyncio

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
from syndica.kernel.contracts.base import ContractSuite
from syndica.kernel.protocols.rubric import RubricStore
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.rubric_redaction import redacted_goal, redacted_rubric

__all__ = ['RubricStoreContract']

_REDACTOR = RuleRedactor()
_ANCHOR = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'do it now'),)


def _goal(
    intent: str = 'ship a report',
    redactor: RuleRedactor = _REDACTOR,
    trust: TrustLevel = TrustLevel.TRUSTED,
):
    return redacted_goal(Goal(intent=intent, anchors=_ANCHOR, trust=trust), redactor)


def _rubric(
    rid: str,
    statement: str = 'has section 4',
    *,
    kind: RubricKind = RubricKind.CHECKABLE,
    status: RubricStatus = RubricStatus.ACTIVE,
    trust: TrustLevel = TrustLevel.TRUSTED,
    mandatory: bool = True,
):
    return redacted_rubric(
        Rubric(
            id=rid,
            statement=statement,
            kind=kind,
            anchors=_ANCHOR,
            status=status,
            trust=trust,
            mandatory=mandatory,
        ),
        _REDACTOR,
    )


class RubricStoreContract(ContractSuite):
    async def test_a_goal_reads_back(self, store: RubricStore) -> None:
        await store.put_goal('s1', _goal('ship a report'))
        got = await store.get_goal('s1')
        assert got is not None and got.intent == 'ship a report'

    async def test_an_unset_goal_is_none(self, store: RubricStore) -> None:
        assert await store.get_goal('s1') is None

    async def test_a_goal_is_stored_redacted(self, store: RubricStore) -> None:
        redactor = RuleRedactor(secrets=['sk-SEKRIT-value'])
        await store.put_goal('s1', _goal('token is sk-SEKRIT-value', redactor))
        got = await store.get_goal('s1')
        assert got is not None and 'sk-SEKRIT-value' not in got.intent

    async def test_an_untrusted_goal_reads_back_untrusted(self, store: RubricStore) -> None:
        await store.put_goal('s1', _goal(trust=TrustLevel.UNTRUSTED))
        got = await store.get_goal('s1')
        assert got is not None and got.trust is TrustLevel.UNTRUSTED

    async def test_put_rubrics_replaces_the_projected_set(self, store: RubricStore) -> None:
        # 重新投影：同 id 更新、不在新集合里的 projected 退役——旧 Goal 的要求不能验新 Goal（评审
        # H7）。
        await store.put_rubrics('s1', [_rubric('r1', 'first'), _rubric('r2', 'stale')])
        await store.put_rubrics('s1', [_rubric('r1', 'updated')])
        assert {r.id: r.statement for r in await store.rubrics('s1')} == {'r1': 'updated'}

    async def test_an_empty_projection_retires_every_projected_rubric(
        self, store: RubricStore
    ) -> None:
        await store.put_rubrics('s1', [_rubric('r1')])
        await store.put_rubrics('s1', [])
        assert await store.rubrics('s1') == []

    async def test_reprojection_keeps_a_confirmed_feedback_rubric(self, store: RubricStore) -> None:
        await store.propose('s1', _rubric('f1', 'use confirmed revenue'))
        await store.activate('s1', 'f1')
        await store.put_rubrics('s1', [_rubric('r1')])
        assert {r.id for r in await store.rubrics('s1')} == {'r1', 'f1'}

    async def test_a_projection_cannot_overwrite_a_feedback_rubric(
        self, store: RubricStore
    ) -> None:
        # id 由模型取：撞上人的反馈条时反馈条胜出，投影改不了它的内容与状态（评审 H8）。
        await store.propose('s1', _rubric('f1', 'use confirmed revenue'))
        await store.put_rubrics('s1', [_rubric('f1', 'something else', status=RubricStatus.ACTIVE)])
        both = {r.id: r for r in await store.rubrics('s1', include_proposed=True)}
        assert both['f1'].statement == 'use confirmed revenue'
        assert both['f1'].status is RubricStatus.PROPOSED  # 投影没把它变成 active
        assert await store.rubrics('s1') == []

    async def test_rubrics_default_to_active_only(self, store: RubricStore) -> None:
        await store.put_rubrics('s1', [_rubric('r1')])
        await store.propose('s1', _rubric('r2'))
        assert [r.id for r in await store.rubrics('s1')] == ['r1']
        both = {r.id for r in await store.rubrics('s1', include_proposed=True)}
        assert both == {'r1', 'r2'}

    async def test_a_proposal_is_stored_as_proposed_feedback_whatever_it_claims(
        self, store: RubricStore
    ) -> None:
        # 交进来的是 status=ACTIVE 的反馈：store 仍存成 proposed，
        # 人 activate 之前不进验收（评审 H8）。
        await store.propose('s1', _rubric('f1', status=RubricStatus.ACTIVE))
        assert await store.rubrics('s1') == []
        stored = (await store.rubrics('s1', include_proposed=True))[0]
        assert stored.status is RubricStatus.PROPOSED
        assert stored.origin is RubricOrigin.FEEDBACK

    async def test_a_proposal_cannot_demote_an_existing_rubric(self, store: RubricStore) -> None:
        await store.put_rubrics('s1', [_rubric('r1')])
        await store.propose('s1', _rubric('r1', 'hijack'))
        got = await store.rubrics('s1')
        assert [(r.id, r.statement, r.status) for r in got] == [
            ('r1', 'has section 4', RubricStatus.ACTIVE)
        ]

    async def test_activate_makes_a_proposed_rubric_count(self, store: RubricStore) -> None:
        await store.propose('s1', _rubric('r2'))
        assert [r.id for r in await store.rubrics('s1')] == []
        await store.activate('s1', 'r2')
        assert [r.id for r in await store.rubrics('s1')] == ['r2']

    async def test_activate_an_unknown_id_is_a_noop(self, store: RubricStore) -> None:
        await store.put_rubrics('s1', [_rubric('r1')])
        await store.activate('s1', 'nope')
        assert [r.id for r in await store.rubrics('s1')] == ['r1']

    async def test_every_rubric_field_round_trips(self, store: RubricStore) -> None:
        # judgment / 不可信 / 非强制 / 反馈来源：任何一项读回成别的值，都能把「需人工确认」洗成通
        # 过。
        await store.propose(
            's1',
            _rubric(
                'f1',
                'reads well to a non-expert',
                kind=RubricKind.JUDGMENT,
                trust=TrustLevel.UNTRUSTED,
                mandatory=False,
            ),
        )
        await store.activate('s1', 'f1')
        await store.put_rubrics('s1', [_rubric('r1', trust=TrustLevel.UNTRUSTED)])
        got = {r.id: r for r in await store.rubrics('s1')}
        f1, r1 = got['f1'], got['r1']
        assert (f1.kind, f1.trust, f1.mandatory, f1.origin) == (
            RubricKind.JUDGMENT,
            TrustLevel.UNTRUSTED,
            False,
            RubricOrigin.FEEDBACK,
        )
        assert f1.anchors == _ANCHOR
        assert (r1.kind, r1.trust, r1.origin) == (
            RubricKind.CHECKABLE,
            TrustLevel.UNTRUSTED,
            RubricOrigin.PROJECTED,
        )

    async def test_rubrics_come_back_in_registration_order(self, store: RubricStore) -> None:
        # 基数 3、既非升序亦非降序：store 交回登记顺序，不排序。
        await store.put_rubrics('s1', [_rubric('r-m'), _rubric('r-a'), _rubric('r-z')])
        assert [r.id for r in await store.rubrics('s1')] == ['r-m', 'r-a', 'r-z']

    async def test_two_scopes_do_not_bleed(self, store: RubricStore) -> None:
        await store.put_goal('s1', _goal('goal one'))
        await store.put_goal('s2', _goal('goal two'))
        await store.put_rubrics('s1', [_rubric('r1')])
        await store.put_rubrics('s2', [_rubric('r9')])
        g1 = await store.get_goal('s1')
        g2 = await store.get_goal('s2')
        assert g1 is not None and g1.intent == 'goal one'
        assert g2 is not None and g2.intent == 'goal two'
        assert [r.id for r in await store.rubrics('s1')] == ['r1']
        assert [r.id for r in await store.rubrics('s2')] == ['r9']

    async def test_three_concurrent_writers_do_not_lose_a_human_confirmation(
        self, store: RubricStore
    ) -> None:
        # 基数 3：人的 activate 与两次重新投影并发——确认不能被投影的读改写盖掉。
        await store.propose('s1', _rubric('f1'))
        await asyncio.gather(
            store.activate('s1', 'f1'),
            store.put_rubrics('s1', [_rubric('r1')]),
            store.put_rubrics('s1', [_rubric('r2')]),
        )
        got = {r.id for r in await store.rubrics('s1')}
        assert 'f1' in got
        assert got - {'f1'} in ({'r1'}, {'r2'})  # 最后落定的是其中一次投影，不是两次拼起来
