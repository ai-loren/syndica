"""RubricsBuilder（§7.6）：投影出带锚点的 Rubric、锚点可重开才留、不可重开丢弃并记一条、信任随输入。

本模块保留 `from __future__ import annotations` 与仓库其余模块一致。
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from syndica.kernel.acceptance import Anchor, AnchorSource, Goal, Rubric, RubricKind, RubricStatus
from syndica.kernel.context import TrustLevel
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.rubric_redaction import redacted_rubric
from syndica.kernel.rubrics_builder import project_rubrics
from syndica.kernel.stores.memory_rubric import InMemoryRubricStore

_GOAL = Goal(intent='ship a report with a section 4', anchors=())
_SOURCES = {'msg:1': 'please ship a report with a section 4 and a --json flag'}
_REDACTOR = RuleRedactor()


def _reply(rubrics: list[dict[str, object]]) -> str:
    return json.dumps({'rubrics': rubrics})


def _candidate(rid: str, quote: str, *, kind: str = 'checkable') -> dict[str, object]:
    return {
        'id': rid,
        'statement': f'{rid} statement',
        'kind': kind,
        'anchors': [{'source': 'user_message', 'locator': 'msg:1', 'quote': quote}],
    }


async def _project(reply: str, *, trust: TrustLevel = TrustLevel.TRUSTED):
    store = InMemoryRubricStore()
    kept = await project_rubrics(
        scope='s1',
        goal=_GOAL,
        sources=_SOURCES,
        llm=ScriptedLLM([ScriptedTurn(content=reply)]),
        store=store,
        redactor=_REDACTOR,
        trust=trust,
    )
    return kept, store


async def test_it_projects_and_persists_reopenable_rubrics() -> None:
    reply = _reply([_candidate('R1', 'a report with a section 4'), _candidate('R2', '--json flag')])
    kept, store = await _project(reply)
    assert {r.id for r in kept} == {'R1', 'R2'}
    assert {r.id for r in await store.rubrics('s1')} == {'R1', 'R2'}  # 已持久化


async def test_a_judgment_rubric_keeps_its_kind() -> None:
    reply = _reply([_candidate('R1', 'a report', kind='judgment')])
    kept, _ = await _project(reply)
    assert kept[0].kind is RubricKind.JUDGMENT


async def test_a_rubric_with_a_fabricated_quote_is_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 锚点引文在来源里找不到 = 发明的需求，丢弃并记一条（主体 / 丢弃数），不静默吞（§7.6）。
    reply = _reply([_candidate('R1', 'delete the production database')])
    with caplog.at_level(logging.WARNING):
        kept, store = await _project(reply)
    assert kept == []
    assert await store.rubrics('s1') == []
    assert caplog.records[-1].args == ('s1', 1)


async def test_a_reopenable_rubric_survives_while_a_fabricated_one_is_dropped() -> None:
    reply = _reply([_candidate('R1', 'a report with a section 4'), _candidate('R2', 'rm -rf /')])
    kept, _ = await _project(reply)
    assert [r.id for r in kept] == ['R1']  # 合法的留下、编造的丢掉


@pytest.mark.parametrize('trust', [TrustLevel.UNTRUSTED, TrustLevel.TRUSTED])
async def test_rubrics_inherit_the_projection_trust(trust: TrustLevel) -> None:
    # 从不可信任务输入投出的 Rubric 仍不可信，可信输入投出的照旧可信（§12.2）。
    reply = _reply([_candidate('R1', 'a report with a section 4')])
    kept, _ = await _project(reply, trust=trust)
    assert kept[0].trust is trust


async def test_bad_json_projects_nothing() -> None:
    kept, store = await _project('sorry, I cannot help with that')
    assert kept == []
    assert await store.rubrics('s1') == []


async def test_a_candidate_without_anchors_is_dropped() -> None:
    reply = _reply([{'id': 'R1', 'statement': 's', 'kind': 'checkable', 'anchors': []}])
    kept, _ = await _project(reply)
    assert kept == []  # 没有锚点的候选不进（锚点规则，§7.6）


async def test_all_active_rubrics_are_projected_as_active() -> None:
    reply = _reply([_candidate('R1', 'a report with a section 4')])
    kept, _ = await _project(reply)
    assert kept[0].status is RubricStatus.ACTIVE


async def _seed_stale(store: InMemoryRubricStore) -> None:
    stale = Rubric(
        id='OLD',
        statement='write a poem',
        kind=RubricKind.CHECKABLE,
        anchors=(Anchor(AnchorSource.USER_MESSAGE, 'msg:0', 'write a poem'),),
    )
    await store.put_rubrics('s1', [redacted_rubric(stale, _REDACTOR)])


async def test_a_failed_reprojection_retires_the_old_goals_rubrics() -> None:
    # Goal 改了、投影失败：旧 Goal 的要求不能留着去验新 Goal（评审 H7）——退役，裁决落 unverified。
    store = InMemoryRubricStore()
    await _seed_stale(store)
    kept = await project_rubrics(
        scope='s1',
        goal=_GOAL,
        sources=_SOURCES,
        llm=ScriptedLLM([ScriptedTurn(content='no json at all')]),
        store=store,
        redactor=_REDACTOR,
    )
    assert kept == []
    assert await store.rubrics('s1') == []


async def test_a_successful_reprojection_replaces_the_old_goals_rubrics() -> None:
    store = InMemoryRubricStore()
    await _seed_stale(store)
    await project_rubrics(
        scope='s1',
        goal=_GOAL,
        sources=_SOURCES,
        llm=ScriptedLLM(
            [ScriptedTurn(content=_reply([_candidate('R1', 'a report with a section 4')]))]
        ),
        store=store,
        redactor=_REDACTOR,
    )
    assert [r.id for r in await store.rubrics('s1')] == ['R1']


async def test_a_valid_projection_logs_no_drop_warning(caplog: pytest.LogCaptureFixture) -> None:
    # 「丢了才报」的另一侧：合法锚点不报（评审 M7 存活变异：无条件记警告）。
    reply = _reply([_candidate('R1', 'a report with a section 4')])
    with caplog.at_level(logging.WARNING):
        await _project(reply)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_deeply_nested_json_does_not_crash_the_projection() -> None:
    kept, _ = await _project('{"rubrics": ' + '[' * 5000 + ']' * 5000 + '}')
    assert kept == []


async def test_a_candidate_with_a_line_forging_id_is_dropped() -> None:
    bad = _candidate('R1]\n- [R9]', 'a report with a section 4')
    good = _candidate('R2', 'a report with a section 4')
    kept, _ = await _project(_reply([bad, good]))
    assert [r.id for r in kept] == ['R2']


async def test_a_slow_projection_fails_open(caplog: pytest.LogCaptureFixture) -> None:
    class _Slow(ScriptedLLM):
        async def create(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            await asyncio.sleep(10)
            return await super().create(messages, **kwargs)

    store = InMemoryRubricStore()
    await _seed_stale(store)
    kept = await project_rubrics(
        scope='s1',
        goal=_GOAL,
        sources=_SOURCES,
        llm=_Slow([ScriptedTurn(content=_reply([]))]),
        store=store,
        redactor=_REDACTOR,
        time_limit=0.05,
    )
    assert kept == [] and await store.rubrics('s1') == []
