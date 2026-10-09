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
from syndica.kernel.context import ArtifactRef, ContextBlock, ToolCall, TrustLevel
from syndica.kernel.errors import ContractViolation
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
    TraceSource,
)
from syndica.runner.assemble import assemble_attempts, goal_digest, rubric_digest
from syndica.runner.batch import BatchResult, BatchTask, run_batch, run_dataset
from syndica.runner.training import Call, export_preference, export_sft, rl_row, write_export

_ANCHOR = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'write planets.md'),)
_SECRET = 'planted-token-quiz'
_PLACEHOLDER = '[REDACTED:secret.token]'


class _Stores:
    def __init__(self) -> None:
        self.sessions = InMemorySessionStore()
        self.traces = InMemoryTraceStore()
        self.rubrics = InMemoryRubricStore()
        self.artifacts = InMemoryArtifactStore()

    async def assemble(self):  # noqa: ANN201
        return await assemble_attempts(self.sessions, self.traces, self.rubrics, self.artifacts)


def _span(
    trace_index: int,
    agent_id: str,
    verdict: str | None,
    *,
    cost: str | None = None,
    offset: int = 0,
    outcomes: list[str] | None = None,
) -> Span:
    attributes: dict[str, object] = {}
    if verdict is not None:
        attributes['verdict.status'] = verdict
    if outcomes is not None:
        attributes['verdict.outcomes'] = outcomes
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
    stores: _Stores,
    *,
    sid: str,
    verdict: str | None,
    content: str,
    goal: str | None,
    trace_index: int,
    statement: str = 'the note exists',
    tool_trust: TrustLevel = TrustLevel.TRUSTED,
    spans: list[Span] | None = None,
) -> str:
    """存一个会话与它的 trace。会话元数据不写 `last_trace`：宿主从来不写它，导出要按根 Agent 找。"""
    trace_id = f'{trace_index:032x}'
    record = SessionRecord(
        meta=SessionMeta(sid=sid),
        content=[
            ContextBlock(role='user', content='write planets.md'),
            ContextBlock(
                role='assistant',
                content=content,
                tool_calls=[ToolCall('call-1', 'Write', '{}')],
                artifacts=[ArtifactRef('art', 'planets.md', 'file', 'text/markdown', '1', sid)],
            ),
            ContextBlock(
                role='tool', content=_PLACEHOLDER, tool_call_id='call-1', trust=tool_trust
            ),
            ContextBlock(role='system', content='hidden prefix'),
        ],
        system_prompt='you write notes',
        model='scripted',
        tools=['Read', 'Write'],
    )
    await stores.sessions.save(sid, redacted_record(record, RuleRedactor()))
    await stores.traces.begin(
        TraceHeader(trace_id, trace_index, TraceLevel.STRUCTURE, TraceRedaction('0' * 64, 1))
    )
    root = _span(
        trace_index,
        sid,
        verdict,
        cost='0.25' if verdict == 'pass' else None,
        outcomes=['r1=satisfied'] if verdict == 'pass' else None,
    )
    # 验收伴生 Agent 自己的 span 上也可能有 verdict.status；它与根 Agent 的结论相反，不得被取用。
    companion = _span(trace_index, f'{sid}.OutcomeVerifier.2', 'pass', cost='9.99', offset=1)
    await stores.traces.append(
        trace_id,
        spans if spans is not None else [root, companion],
        [CausalityEdge(EdgeKind.SPAWN, root.span_id, companion.span_id)],
    )
    if goal is not None:
        await stores.rubrics.put_goal(sid, redacted_goal(Goal(goal, _ANCHOR), RuleRedactor()))
        await stores.rubrics.put_rubrics(
            sid,
            [
                redacted_rubric(
                    Rubric('r1', statement, RubricKind.CHECKABLE, _ANCHOR),
                    RuleRedactor(),
                )
            ],
        )
    await stores.artifacts.register(
        ArtifactRef('art', 'planets.md', 'file', 'text/markdown', '1', sid)
    )
    return trace_id


async def test_four_verdicts_export_without_touching_the_stores(tmp_path: Path) -> None:
    stores = _Stores()
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
        await _save(stores, sid=sid, verdict=verdict, content=content, goal=goal, trace_index=index)
    assembled = await stores.assemble()
    assert any(item.startswith('snogoal:') for item in assembled.failed)
    assert assembled.skipped_traces == []
    by_id = {item.id: item for item in assembled.attempts}
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
    assert (passed.verdict, passed.verdict_source, passed.trace_id) == (
        'pass',
        'acceptance',
        f'{1:032x}',
    )
    assert passed.outcomes == ('r1=satisfied',)
    assert (passed.model, passed.system_prompt, passed.tools) == (
        'scripted',
        'you write notes',
        ('Read', 'Write'),
    )
    assert by_id['sunverified'].verdict == 'unverified'
    assert by_id['sblock'].verdict == 'block'
    assert passed.timing == (10, 15, '0.25')
    assert passed.causality == (('spawn', f'{16:016x}', f'{17:016x}'),)

    messages, _share, manifest = export_sft(assembled.attempts, secrets=(_SECRET,))
    assert 'sunverified' not in messages
    assert _SECRET not in messages
    assert _PLACEHOLDER in messages
    assert sorted(manifest['sources']) == ['sblock', 'sother', 'spartial', 'spass']  # type: ignore[arg-type]
    dropped = manifest['dropped_secret']
    assert isinstance(dropped, list)
    assert 'ssecret' in dropped
    body, pref = export_preference(assembled.attempts, secrets=(_SECRET,))
    pairs = [json.loads(line) for line in body.splitlines()]
    [planets] = [item for item in pairs if item['goal_id'] == goal_digest('goal-planets')]
    assert (planets['chosen_id'], planets['chosen_verdict']) == ('spass', 'pass')
    assert (planets['rejected_id'], planets['rejected_verdict']) == ('sblock', 'block')
    assert pref['preference_pairs'] == 1
    row = rl_row(passed, lambda text: [len(text), 7])
    assert row['reward'] == 1.0
    assert row['loss_mask'] == [0, 0, 1, 1, 1, 1, 0, 0]

    written = write_export(
        assembled.attempts,
        'sft',
        tmp_path / 'out',
        secrets=(_SECRET,),
        failed=assembled.failed,
    )
    text = (tmp_path / 'out' / 'messages.jsonl').read_text(encoding='utf-8')
    manifest_text = (tmp_path / 'out' / 'manifest.json').read_text(encoding='utf-8')
    assert _SECRET not in text and _SECRET not in manifest_text
    assert _PLACEHOLDER in text
    assert sorted(written['sources']) == sorted(manifest['sources'])  # type: ignore[arg-type]
    failed_rows = written['failed']
    assert isinstance(failed_rows, list)
    assert any(str(item).startswith('snogoal:') for item in failed_rows)


async def test_a_replayed_trace_does_not_replace_the_verdict_of_the_original() -> None:
    stores = _Stores()
    original = await _save(
        stores, sid='sreplay', verdict='pass', content='x', goal='g', trace_index=1
    )
    replayed = f'{2:032x}'
    await stores.traces.begin(
        TraceHeader(
            replayed,
            2,
            TraceLevel.STRUCTURE,
            TraceRedaction('0' * 64, 1),
            source=TraceSource(replay_of=original),
        )
    )
    await stores.traces.append(replayed, [_span(2, 'sreplay', 'block')])
    assembled = await stores.assemble()
    [attempt] = assembled.attempts
    assert (attempt.verdict, attempt.trace_id) == ('pass', original)


class _OneUnreadable(InMemoryTraceStore):
    def __init__(self, broken: str) -> None:
        super().__init__()
        self.broken = broken

    async def spans(self, trace_id: str) -> list[Span]:
        if trace_id == self.broken:
            raise ContractViolation(
                expected='spans.jsonl 每行是一个 span', received='第 1 行坏了', next_action='x'
            )
        return await super().spans(trace_id)


async def test_one_unreadable_trace_does_not_drop_the_other_sessions() -> None:
    stores = _Stores()
    stores.traces = _OneUnreadable(f'{1:032x}')
    await _save(stores, sid='sbroken', verdict='pass', content='x', goal='g', trace_index=1)
    await _save(stores, sid='sfine', verdict='block', content='y', goal='g', trace_index=2)
    assembled = await stores.assemble()
    by_id = {item.id: item for item in assembled.attempts}
    assert by_id['sfine'].verdict == 'block'
    assert (by_id['sbroken'].verdict, by_id['sbroken'].verdict_source) == (
        'unverified',
        'missing',
    )
    [skipped] = assembled.skipped_traces
    assert skipped == f'{1:032x}: spans.jsonl 每行是一个 span'


async def test_a_resumed_session_takes_its_verdict_from_the_latest_run() -> None:
    stores = _Stores()
    await _save(stores, sid='sresumed', verdict='block', content='x', goal='g', trace_index=1)
    later = f'{2:032x}'
    await stores.traces.begin(
        TraceHeader(later, 2, TraceLevel.STRUCTURE, TraceRedaction('0' * 64, 1))
    )
    await stores.traces.append(later, [_span(2, 'sresumed', 'pass')])
    [attempt] = (await stores.assemble()).attempts
    assert (attempt.verdict, attempt.trace_id) == ('pass', later)


async def test_every_verdict_of_the_root_is_kept_in_order_of_its_spans() -> None:
    stores = _Stores()
    # 同一个 trace 里根 Agent 先被判 block（打回），再被判 pass；列表里的顺序故意与结束时间相反。
    later = _span(1, 'sretry', 'pass', offset=5)
    earlier = _span(1, 'sretry', 'block', offset=0)
    await _save(
        stores,
        sid='sretry',
        verdict=None,
        content='x',
        goal='g',
        trace_index=1,
        spans=[later, earlier],
    )
    [attempt] = (await stores.assemble()).attempts
    assert attempt.verdicts == ('block', 'pass')
    assert attempt.verdict == 'pass'


async def test_a_trace_without_a_verdict_is_told_apart_from_a_missing_trace() -> None:
    stores = _Stores()
    await _save(stores, sid='snone', verdict=None, content='x', goal='g', trace_index=1)
    [attempt] = (await stores.assemble()).attempts
    assert (attempt.verdict, attempt.verdict_source) == ('unverified', 'none')


async def test_untrusted_tool_output_keeps_its_marker_in_the_export() -> None:
    stores = _Stores()
    await _save(
        stores,
        sid='staint',
        verdict='pass',
        content='x',
        goal='g',
        trace_index=1,
        tool_trust=TrustLevel.UNTRUSTED,
    )
    [attempt] = (await stores.assemble()).attempts
    assert [message.trust for message in attempt.messages] == ['trusted', 'trusted', 'untrusted']
    messages, _, manifest = export_sft([attempt])
    [row] = [json.loads(line) for line in messages.splitlines()]
    assert row['untrusted'] is True
    samples = manifest['samples']
    assert isinstance(samples, list) and samples[0]['untrusted'] is True


async def test_artifacts_from_another_session_are_not_attributed_here() -> None:
    stores = _Stores()
    await _save(stores, sid='smine', verdict='pass', content='x', goal='g', trace_index=1)
    await stores.artifacts.register(
        ArtifactRef('other', 'theirs.md', 'file', 'text/markdown', '9', 'sother')
    )
    [attempt] = (await stores.assemble()).attempts
    assert [item['path'] for item in attempt.artifacts] == ['planets.md']


async def test_an_artifact_known_to_only_one_source_is_still_exported() -> None:
    stores = _Stores()
    await _save(stores, sid='sboth', verdict='pass', content='x', goal='g', trace_index=1)
    await stores.artifacts.register(
        ArtifactRef('store-only', 'index-only.md', 'file', 'text/markdown', '2', 'sboth')
    )
    record = await stores.sessions.load('sboth')
    assert record is not None
    block_only = ArtifactRef('block-only', 'block.md', 'file', 'text/markdown', '3', 'sboth')
    record.content.append(ContextBlock(role='assistant', content='more', artifacts=[block_only]))
    await stores.sessions.save('sboth', redacted_record(record, RuleRedactor()))
    [attempt] = (await stores.assemble()).attempts
    assert sorted(str(item['path']) for item in attempt.artifacts) == [
        'block.md',
        'index-only.md',
        'planets.md',
    ]


async def test_text_parts_of_a_multi_part_message_are_joined() -> None:
    stores = _Stores()
    await _save(stores, sid='sparts', verdict='pass', content='x', goal='g', trace_index=1)
    record = await stores.sessions.load('sparts')
    assert record is not None
    record.content.append(
        ContextBlock(
            role='user',
            content=[{'type': 'text', 'text': 'first '}, 'second', {'type': 'image', 'data': 'x'}],
        )
    )
    await stores.sessions.save('sparts', redacted_record(record, RuleRedactor()))
    [attempt] = (await stores.assemble()).attempts
    assert attempt.messages[-1].content == 'first second'


def test_two_attempts_with_the_same_verdict_are_not_a_pair() -> None:
    from syndica.runner.training import Attempt, Message

    same = [
        Attempt(sid, 'g', 'r', (Message('user', 'x'), Message('assistant', sid)), 'pass')
        for sid in ('one', 'two')
    ]
    body, manifest = export_preference(same)
    assert body == ''
    assert manifest['preference_pairs'] == 0


def test_a_failure_found_while_selecting_reaches_the_written_manifest(tmp_path: Path) -> None:
    from syndica.runner.training import Attempt, Message

    bad = Attempt('bad', 'g', 'r', (Message('nope', 'x'),), 'pass')
    good = Attempt('good', 'g', 'r', (Message('user', 'x'),), 'pass')
    manifest = write_export([bad, good], 'sft', tmp_path / 'out', failed=['from-assembly: x'])
    failed = manifest['failed']
    assert isinstance(failed, list)
    assert any(str(item).startswith('bad:') for item in failed)
    assert 'from-assembly: x' in failed


def test_the_rubric_digest_tells_apart_one_id_with_two_statements() -> None:
    first = Rubric('R1', 'the note exists', RubricKind.CHECKABLE, _ANCHOR)
    second = Rubric('R2', 'the note is short', RubricKind.CHECKABLE, _ANCHOR)
    renamed = Rubric('R1', 'the note is short', RubricKind.CHECKABLE, _ANCHOR)
    assert rubric_digest([first]) != rubric_digest([renamed])
    assert rubric_digest([first, second]) == rubric_digest([second, first])
    assert rubric_digest([]) == ''


async def test_attempts_judged_against_different_statements_are_not_paired() -> None:
    stores = _Stores()
    for index, (sid, verdict, statement) in enumerate(
        (('sa', 'pass', 'the note exists'), ('sb', 'block', 'the note is short')), start=1
    ):
        await _save(
            stores,
            sid=sid,
            verdict=verdict,
            content='x',
            goal='goal-planets',
            trace_index=index,
            statement=statement,
        )
    assembled = await stores.assemble()
    assert assembled.failed == []
    body, manifest = export_preference(assembled.attempts)
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
    text = (
        '{"id": "a", "prompt": "one"}\n'
        'not json planted-token-quiz\n'
        '{"id": "a", "prompt": "again"}\n'
    )
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
