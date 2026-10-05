"""RubricsBuilder（§7.6）：投影出带锚点的 Rubric、锚点可重开才留、不可重开丢弃并记一条、信任随输入。

本模块保留 `from __future__ import annotations` 与仓库其余模块一致。
"""

from __future__ import annotations

import json
import logging

import pytest

from syndica.kernel.acceptance import Goal, RubricKind, RubricStatus
from syndica.kernel.context import TrustLevel
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.redaction import RuleRedactor
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


async def test_rubrics_inherit_the_projection_trust() -> None:
    # 从不可信任务输入投出的 Rubric 仍不可信（§12.2 / 评审 H9）。
    reply = _reply([_candidate('R1', 'a report with a section 4')])
    kept, _ = await _project(reply, trust=TrustLevel.UNTRUSTED)
    assert kept[0].trust is TrustLevel.UNTRUSTED


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
