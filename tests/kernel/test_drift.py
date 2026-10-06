"""漂移的锚点检查（spec §8.2 第一层，零 LLM）：动作的词面对 Goal 与 Rubric 的词面。"""

from __future__ import annotations

import json

import pytest

from syndica.kernel.acceptance import Anchor, AnchorSource, Goal, Rubric, RubricKind, RubricStatus
from syndica.kernel.context import ArtifactRef, ContextBlock, ToolCall
from syndica.kernel.drift import action_terms, anchor_terms, anchored, call_lines


def _call(name: str, **arguments: object) -> ContextBlock:
    return ContextBlock(
        role='assistant',
        tool_calls=[ToolCall(id='c1', name=name, arguments=json.dumps(arguments))],
    )


_Q = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'revenue table'),)
_GOAL = Goal(
    intent='写一份季度报告到 reports/quarterly.md',
    anchors=(Anchor(AnchorSource.USER_MESSAGE, 'msg:1', '包含营收表格'),),
)


def test_tool_arguments_count_but_written_content_does_not() -> None:
    blocks = [
        _call('Write', path='reports/quarterly.md', content='weather forecast for paris'),
        _call('Edit', path='notes.txt', old_string='alpha', new_string='omega'),
    ]
    assert action_terms(blocks) == {'reports', 'quarterly', 'notes', 'txt'}


def test_reported_artifact_paths_count_as_actions() -> None:
    ref = ArtifactRef(
        id='file:charts/revenue.png',
        path='charts/revenue.png',
        kind='image',
        mime='image/png',
        version='v1',
        producer='a1',
    )
    block = ContextBlock(role='tool', tool_call_id='c1', content='ok', artifacts=[ref])
    assert action_terms([block]) == {'charts', 'revenue', 'png'}


def test_unparseable_arguments_are_read_as_text() -> None:
    block = ContextBlock(
        role='assistant', tool_calls=[ToolCall(id='c1', name='Bash', arguments='grep revenue')]
    )
    assert action_terms([block]) == {'grep', 'revenue'}


def test_anchors_come_from_the_goal_and_active_rubrics_only() -> None:
    rubrics = [
        Rubric(id='R1', statement='has a revenue table', kind=RubricKind.CHECKABLE, anchors=_Q),
        Rubric(
            id='R2',
            statement='mentions competitors',
            kind=RubricKind.CHECKABLE,
            anchors=(Anchor(AnchorSource.USER_MESSAGE, 'msg:2', 'rivals'),),
            status=RubricStatus.PROPOSED,
        ),
    ]
    found = anchor_terms(_GOAL, rubrics)
    assert {'quarterly', 'revenue', 'table', '营收'} <= found
    assert not {'competitors', 'rivals'} & found


def test_english_arguments_anchor_to_a_chinese_goal_through_the_path() -> None:
    anchors = anchor_terms(_GOAL, [])
    assert anchored(
        action_terms([_call('Write', path='reports/quarterly.md', content='x')]), anchors
    )
    assert not anchored(action_terms([_call('Bash', command='curl weather api')]), anchors)


def test_no_actions_or_no_anchors_is_not_suspicious() -> None:
    assert anchored(frozenset(), frozenset({'report'}))
    assert anchored(frozenset({'weather'}), frozenset())


@pytest.mark.parametrize(
    'argument', ['content', 'new_string', 'old_string', 'text', 'body', 'patch', 'diff', 'answer']
)
def test_each_written_content_argument_is_left_out(argument: str) -> None:
    block = _call('Tool', path='notes.txt', **{argument: 'weather forecast for paris'})
    assert action_terms([block]) == {'notes', 'txt'}


def test_list_valued_arguments_count() -> None:
    assert action_terms([_call('Read', paths=['reports/quarterly.md', 'data/sales.csv'])]) == {
        'reports',
        'quarterly',
        'data',
        'sales',
        'csv',
    }


def test_a_rubric_anchor_quote_contributes_its_own_words() -> None:
    rubric = Rubric(
        id='R1',
        statement='has a table',
        kind=RubricKind.CHECKABLE,
        anchors=(Anchor(AnchorSource.USER_MESSAGE, 'msg:2', 'competitor margins'),),
    )
    assert {'competitor', 'margins'} <= anchor_terms(Goal(intent='g'), [rubric])


def test_the_judge_sees_tool_names_and_arguments_but_never_written_content() -> None:
    lines = call_lines(
        [
            _call('Write', path='reports/q.md', content='SECRET' * 100),
            _call('Bash', command='x' * 500),
        ],
        line_chars=60,
    )
    assert lines[0] == '- Write {"path": "reports/q.md"}'
    assert lines[1].startswith('- Bash {"command": "xxx') and lines[1].endswith('…')
    assert len(lines[1]) == 61
