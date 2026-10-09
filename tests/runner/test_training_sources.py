"""从会话、Goal、trace 与产物拼轨迹，再导出。unverified 默认不进数据集。"""

from __future__ import annotations

import json
from pathlib import Path

from syndica.kernel.acceptance import (
    Anchor,
    AnchorSource,
    Goal,
    Rubric,
    RubricKind,
)
from syndica.kernel.context import ArtifactRef, ContextBlock, ToolCall
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.rubric_redaction import redacted_goal, redacted_rubric
from syndica.kernel.session_redaction import redacted_record
from syndica.kernel.session_types import SessionMeta, SessionRecord
from syndica.kernel.stores.memory_artifact import InMemoryArtifactStore
from syndica.kernel.stores.memory_rubric import InMemoryRubricStore
from syndica.kernel.stores.memory_session import InMemorySessionStore
from syndica.kernel.stores.memory_trace import InMemoryTraceStore
from syndica.kernel.trace import (
    CausalityEdge,
    EdgeKind,
    Span,
    SpanKind,
    SpanStatus,
    TraceHeader,
    TraceLevel,
    TraceRedaction,
)
from syndica.runner.assemble import assemble_attempts, goal_digest, rubric_digest
from syndica.runner.batch import BatchResult, BatchTask, run_batch, run_dataset
from syndica.runner.training import Call, export_preference, export_sft, rl_row, write_export

_ANCHOR = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'write planets.md'),)
_SECRET = 'planted-token-quiz'
_PLACEHOLDER = '[REDACTED:secret.token]'


def _span(
    trace_index: int, agent_id: str, verdict: str, *, cost: str | None = None, offset: int = 0
) -> Span:
    attributes: dict[str, object] = {'verdict.status': verdict}
    if cost is not None:
        attributes['budget.cost'] = cost
    return Span(
        span_id=f'{trace_index * 16 + offset:016x}',
        parent_span_id=None,
        kind=SpanKind.AGENT,
        name='agent',
        agent_id=agent_id,
        start_ns=trace_index * 10 + offset,
        end_ns=trace_index * 10 + 4 + offset,
        status=SpanStatus.OK,
        attributes=attributes,  # pyright: ignore[reportArgumentType]
    )


async def _save(
    sessions: InMemorySessionStore,
    traces: InMemoryTraceStore,
    rubrics: InMemoryRubricStore,
    artifacts: InMemoryArtifactStore,
    *,
    sid: str,
    verdict: str,
    content: str,
    goal: str | None,
    trace_index: int,
    statement: str = 'the note exists',
) -> None:
    trace_id = f'{trace_index:032x}'
    record = SessionRecord(
        meta=SessionMeta(sid=sid, last_trace=trace_id),
        content=[
            ContextBlock(role='user', content='write planets.md'),
            ContextBlock(
                role='assistant',
                content=content,
                tool_calls=[ToolCall('call-1', 'Write', '{}')],
                artifacts=[ArtifactRef('art', 'planets.md', 'file', 'text/markdown', '1', sid)],
            ),
            ContextBlock(role='tool', content=_PLACEHOLDER, tool_call_id='call-1'),
            ContextBlock(role='system', content='hidden prefix'),
        ],
    )
    await sessions.save(sid, redacted_record(record, RuleRedactor()))
    await traces.begin(TraceHeader(trace_id, 1, TraceLevel.STRUCTURE, TraceRedaction('0' * 64, 1)))
    root = _span(trace_index, sid, verdict, cost='0.25' if verdict == 'pass' else None)
    # 验收伴生 Agent 自己的 span 上也可能有 verdict.status；它与根 Agent 的结论相反，不得被取用。
    companion = _span(trace_index, f'{sid}-verifier', 'pass', cost='9.99', offset=1)
    await traces.append(
        trace_id,
        [root, companion],
        [CausalityEdge(EdgeKind.SPAWN, root.span_id, companion.span_id)],
    )
    if goal is not None:
        await rubrics.put_goal(sid, redacted_goal(Goal(goal, _ANCHOR), RuleRedactor()))
        await rubrics.put_rubrics(
            sid,
            [
                redacted_rubric(
                    Rubric('r1', statement, RubricKind.CHECKABLE, _ANCHOR),
                    RuleRedactor(),
                )
            ],
        )
    await artifacts.register(ArtifactRef('art', 'planets.md', 'file', 'text/markdown', '1', sid))


async def test_four_verdicts_export_without_touching_the_stores(tmp_path: Path) -> None:
    sessions = InMemorySessionStore()
    traces = InMemoryTraceStore()
    rubrics = InMemoryRubricStore()
    artifacts = InMemoryArtifactStore()
    plan = (
        ('spass', 'pass', 'wrote the note', 'goal-planets'),
        ('spartial', 'partial', 'half', 'goal-planets'),
        ('sblock', 'block', 'no', 'goal-planets'),
        ('sunverified', 'unverified', 'unknown', 'goal-planets'),
        ('ssecret', 'pass', f'note {_SECRET}', 'goal-planets'),
        ('sother', 'block', 'elsewhere', 'goal-other'),
        ('snogoal', 'pass', 'orphan', None),
    )
    for index, (sid, verdict, content, goal) in enumerate(plan, start=1):
        await _save(
            sessions,
            traces,
            rubrics,
            artifacts,
            sid=sid,
            verdict=verdict,
            content=content,
            goal=goal,
            trace_index=index,
        )
    before = await sessions.load('spass')
    before_spans = await traces.spans(f'{1:032x}')
    assert before is not None
    attempts, failed = await assemble_attempts(sessions, traces, rubrics, artifacts)
    assert any(item.startswith('snogoal:') for item in failed)
    by_id = {item.id: item for item in attempts}
    assert set(by_id) == {'spass', 'spartial', 'sblock', 'sunverified', 'ssecret', 'sother'}
    passed = by_id['spass']
    assert [message.role for message in passed.messages] == ['user', 'assistant', 'tool']
    assert passed.messages[2].content == _PLACEHOLDER
    assert passed.messages[1].tool_calls == (Call('call-1', 'Write', '{}'),)
    assert passed.artifacts == ({'path': 'planets.md', 'version': '1'},)
    assert passed.goal_id == goal_digest('goal-planets')
    assert passed.goal_id != by_id['sother'].goal_id
    assert passed.rubric_id.startswith('rubric:')
    assert passed.rubric_id == by_id['sblock'].rubric_id
    assert passed.verdict == 'pass'
    assert by_id['sunverified'].verdict == 'unverified'
    assert by_id['sblock'].verdict == 'block'
    assert passed.timing == (10, 15, '0.25')
    assert passed.causality == (('spawn', f'{16:016x}', f'{17:016x}'),)
    after = await sessions.load('spass')
    assert after is not None
    assert after.content[1].content == before.content[1].content
    assert await traces.spans(f'{1:032x}') == before_spans

    messages, _share, manifest = export_sft(attempts, secrets=(_SECRET,))
    assert 'sunverified' not in messages
    assert _SECRET not in messages
    assert _PLACEHOLDER in messages
    assert manifest['sources'] == ['sother', 'sblock', 'spartial', 'spass']
    dropped = manifest['dropped_secret']
    assert isinstance(dropped, list)
    assert 'ssecret' in dropped
    body, pref = export_preference(attempts, secrets=(_SECRET,))
    pairs = [json.loads(line) for line in body.splitlines()]
    planets = next(item for item in pairs if item['goal_id'] == goal_digest('goal-planets'))
    assert planets['chosen'] == {'id': 'spass', 'verdict': 'pass'}
    assert planets['rejected'] == {'id': 'sblock', 'verdict': 'block'}
    assert pref['preference_pairs'] == 1
    row = rl_row(passed, lambda text: [len(text), 7])
    assert row['reward'] == 1.0
    assert row['loss_mask'] == [0, 0, 1, 1, 1, 1, 0, 0]

    written = write_export(attempts, 'sft', tmp_path / 'out', secrets=(_SECRET,), failed=failed)
    text = (tmp_path / 'out' / 'messages.jsonl').read_text(encoding='utf-8')
    manifest_text = (tmp_path / 'out' / 'manifest.json').read_text(encoding='utf-8')
    assert _SECRET not in text and _SECRET not in manifest_text
    assert _PLACEHOLDER in text
    assert written['sources'] == manifest['sources']
    failed_rows = written['failed']
    assert isinstance(failed_rows, list)
    assert any(str(item).startswith('snogoal:') for item in failed_rows)
    assert await sessions.load('spass') == after


def test_the_rubric_digest_tells_apart_one_id_with_two_statements() -> None:
    first = Rubric('R1', 'the note exists', RubricKind.CHECKABLE, _ANCHOR)
    second = Rubric('R2', 'the note is short', RubricKind.CHECKABLE, _ANCHOR)
    renamed = Rubric('R1', 'the note is short', RubricKind.CHECKABLE, _ANCHOR)
    assert rubric_digest([first]) != rubric_digest([renamed])
    assert rubric_digest([first, second]) == rubric_digest([second, first])
    assert rubric_digest([]) == ''


async def test_attempts_judged_against_different_statements_are_not_paired() -> None:
    sessions = InMemorySessionStore()
    traces = InMemoryTraceStore()
    rubrics = InMemoryRubricStore()
    artifacts = InMemoryArtifactStore()
    for index, (sid, verdict, statement) in enumerate(
        (('sa', 'pass', 'the note exists'), ('sb', 'block', 'the note is short')), start=1
    ):
        await _save(
            sessions,
            traces,
            rubrics,
            artifacts,
            sid=sid,
            verdict=verdict,
            content='x',
            goal='goal-planets',
            trace_index=index,
            statement=statement,
        )
    attempts, failed = await assemble_attempts(sessions, traces, rubrics, artifacts)
    assert failed == []
    body, manifest = export_preference(attempts)
    assert body == ''
    assert manifest['preference_pairs'] == 0


def test_one_batch_failure_does_not_drop_the_rest() -> None:
    tasks = [
        BatchTask('a', 'one'),
        BatchTask('b:x', 'two'),
        BatchTask('c', 'three'),
        BatchTask('d', 'four'),
    ]

    def run_one(task: BatchTask) -> int:
        if task.id == 'b:x':
            raise RuntimeError('boom planted-token-quiz')
        return 1 if task.id == 'd' else 0

    results = run_batch(tasks, run_one)
    assert results == [
        BatchResult('a'),
        BatchResult('b:x', 'RuntimeError'),
        BatchResult('c'),
        BatchResult('d', 'exit 1'),
    ]


def test_a_bad_line_and_a_duplicate_id_are_reported_and_the_rest_run() -> None:
    text = '{"id": "a", "prompt": "one"}\nnot json\n{"id": "a", "prompt": "again"}\n'
    ran: list[str] = []

    def run_one(task: BatchTask) -> int:
        ran.append(task.prompt)
        return 0

    body, code = run_dataset(text, run_one)
    rows = [json.loads(line) for line in body.splitlines()]
    assert code == 1
    assert ran == ['one']
    assert [row.get('line') for row in rows if not row['ok']] == [2, 3]
    assert rows[-1] == {'id': 'a', 'ok': True}
    assert 'planted-token-quiz' not in body
