"""RubricStore 契约。任何实现都必须通过（spec §13.3 / §7.6 / §8.2）。

一次钉全：Goal round-trip（含脱敏）、按 scope 隔离（基数 2、跨同一对象两次读）、put_rubrics upsert
（重投影更新同 id、反馈存活）、active / proposed 过滤、propose → activate 生效。先私有（随 wheel
发布但不进 kernel.__all__）。

**运行前提**见 `syndica.kernel.contracts.base`。
"""

from __future__ import annotations

from syndica.kernel.acceptance import Anchor, AnchorSource, Goal, Rubric, RubricKind, RubricStatus
from syndica.kernel.contracts.base import ContractSuite
from syndica.kernel.protocols.rubric import RubricStore
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.rubric_redaction import redacted_goal, redacted_rubric

__all__ = ['RubricStoreContract']

_REDACTOR = RuleRedactor()
_ANCHOR = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'do it'),)


def _goal(intent: str = 'ship a report', redactor: RuleRedactor = _REDACTOR):
    return redacted_goal(Goal(intent=intent, anchors=_ANCHOR), redactor)


def _rubric(
    rid: str,
    statement: str = 'has section 4',
    *,
    kind: RubricKind = RubricKind.CHECKABLE,
    status: RubricStatus = RubricStatus.ACTIVE,
):
    return redacted_rubric(
        Rubric(id=rid, statement=statement, kind=kind, anchors=_ANCHOR, status=status), _REDACTOR
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

    async def test_put_rubrics_upserts_and_keeps_others(self, store: RubricStore) -> None:
        # 重新投影：同 id 更新、别的 id（如人确认过的反馈）存活（§7.7 / 评审 H10）。
        await store.put_rubrics('s1', [_rubric('r1', 'first'), _rubric('r2', 'keep me')])
        await store.put_rubrics('s1', [_rubric('r1', 'updated')])
        got = {r.id: r.statement for r in await store.rubrics('s1')}
        assert got == {'r1': 'updated', 'r2': 'keep me'}

    async def test_rubrics_default_to_active_only(self, store: RubricStore) -> None:
        await store.put_rubrics('s1', [_rubric('r1')])
        await store.propose('s1', _rubric('r2', status=RubricStatus.PROPOSED))
        assert [r.id for r in await store.rubrics('s1')] == ['r1']
        both = {r.id for r in await store.rubrics('s1', include_proposed=True)}
        assert both == {'r1', 'r2'}

    async def test_activate_makes_a_proposed_rubric_count(self, store: RubricStore) -> None:
        await store.propose('s1', _rubric('r2', status=RubricStatus.PROPOSED))
        assert [r.id for r in await store.rubrics('s1')] == []  # proposed 默认不算
        await store.activate('s1', 'r2')
        assert [r.id for r in await store.rubrics('s1')] == ['r2']  # 人确认后生效

    async def test_activate_an_unknown_id_is_a_noop(self, store: RubricStore) -> None:
        await store.put_rubrics('s1', [_rubric('r1')])
        await store.activate('s1', 'nope')
        assert [r.id for r in await store.rubrics('s1')] == ['r1']

    async def test_two_scopes_do_not_bleed(self, store: RubricStore) -> None:
        # 基数 2：两个会话各有各的 Goal 与 Rubric，读其一不串另一（「作用在错主体」一等轴）。
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
