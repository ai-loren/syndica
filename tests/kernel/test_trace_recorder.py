"""TraceRecorder（M3 T9）：缓冲与写入任务、级别、逐叶脱敏、请求指纹、store 故障。"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import hashlib
import json
import logging
import random
import re
import string
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, cast

import pytest

from syndica.kernel import (
    AgentOptions,
    ArtifactRef,
    BaseAgent,
    ConfigError,
    ContextBlock,
    ContractViolation,
    EventBus,
    HooksCenter,
    HumanInteractionModule,
    InMemoryTraceStore,
    Message,
    PayloadRefused,
    Reply,
    RetryModule,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    SpanKind,
    SpanStatus,
    StopReason,
    Tool,
    ToolCall,
    ToolResult,
    TraceLevel,
    Tracer,
    TraceRecorder,
    TraceSource,
    Trigger,
    TrustLevel,
)
from syndica.kernel import trace_recorder as recorder_module
from syndica.kernel.llm import LLMConfig
from syndica.kernel.redaction import (
    RULES_VERSION,
    RedactedText,
    RedactionRule,
    RuleRedactor,
    encode_redacted,
)
from syndica.kernel.trace import (
    ATTRIBUTE_DOMAINS,
    ATTRIBUTE_KINDS,
    CausalityEdge,
    EdgeKind,
    PayloadRef,
    Span,
)
from syndica.kernel.trace_recorder import RequestFingerprints
from syndica.kernel.tracing import span_id_of

_TIMEOUT = 5.0
_PAYLOAD_KEYS = sorted(key for key, kind in ATTRIBUTE_KINDS.items() if kind == 'payload')
_LLM = span_id_of(SpanKind.LLM_CALL, 'agent', 1, 1)
_TOOL = span_id_of(SpanKind.TOOL_CALL, 'agent', 1, 1, 0)


def _call(call_id: str, name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def _factory(
    name: str, llm: object, tools: dict[str, Tool] | None = None, *, prompt: str = 'You help.'
) -> Callable[[HooksCenter, EventBus], BaseAgent]:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name=name, llm=cast(Any, llm), tools=dict(tools or {}), system_prompt=prompt
            ),
            hooks=hooks,
            events=events,
        )

    return build


class _Echo(Tool):
    __tool_name__ = 'Echo'
    __tool_command_args__ = ()

    async def execute(self, text: str = '') -> ToolResult:
        """原样交回。
        text: 要交回的文字
        """
        return ToolResult(output=text or 'echo')


class _Counting(InMemoryTraceStore):
    """记下每一次写；`payload_texts` 是收下的每一份 payload（解码前的原文）。"""

    def __init__(self) -> None:
        super().__init__()
        self.payload_texts: list[str] = []
        self.appended: list[tuple[list[Span], list[CausalityEdge]]] = []

    async def put_payload(self, trace_id: str, payload: RedactedText) -> PayloadRef:
        ref = await super().put_payload(trace_id, payload)
        self.payload_texts.append(str(payload))
        return ref

    async def append(
        self,
        trace_id: str,
        spans: Sequence[Span] = (),
        edges: Sequence[CausalityEdge] = (),
    ) -> None:
        await super().append(trace_id, spans, edges)
        self.appended.append((list(spans), list(edges)))


async def _take(events: Any, kind: str) -> Any:
    while True:
        event = await asyncio.wait_for(anext(events), timeout=_TIMEOUT)
        if event.kind == kind:
            return event


async def _recorded(
    build: Callable[[HooksCenter, EventBus], BaseAgent],
    *,
    level: TraceLevel = TraceLevel.REPLAY,
    store: InMemoryTraceStore | None = None,
    redactor: RuleRedactor | None = None,
    answer: str | None = None,
    query: str = 'go',
    **runtime_options: Any,
) -> tuple[InMemoryTraceStore, TraceRecorder, str, Any]:
    """跑一个 Agent 并记下来；`answer` 给出时替人答复它的第一次 AskUser。"""
    store = _Counting() if store is None else store
    recorder = TraceRecorder(store, RuleRedactor() if redactor is None else redactor, level=level)
    await recorder.begin()
    if answer is not None:
        runtime_options.setdefault('modules', []).append(HumanInteractionModule())
    runtime = Runtime(tracer=recorder, **runtime_options)
    events = runtime.events.subscribe()
    try:
        agent_id = await runtime.spawn(build, query=query)
        if answer is not None:
            request = await _take(events, 'user_input_requested')
            await runtime.send(
                agent_id,
                answer,
                activate=False,
                sender='human',
                correlation_id=request.payload['request_id'],
            )
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        events.close()
        await runtime.shutdown()
    await recorder.finish()
    return store, recorder, agent_id, context


async def _spans(store: InMemoryTraceStore, recorder: TraceRecorder) -> dict[str, Span]:
    return {span.span_id: span for span in await store.spans(recorder.trace_id)}


async def _payload(store: InMemoryTraceStore, recorder: TraceRecorder, digest: object) -> Any:
    text = await store.payload(PayloadRef(recorder.trace_id, cast(str, digest)))
    assert text is not None
    return json.loads(text)


def _unit_digest(unit: object, redactor: RuleRedactor | None = None) -> str:
    redacted = encode_redacted(unit, RuleRedactor() if redactor is None else redactor)
    return hashlib.sha256(redacted.encode()).hexdigest()


def _block_unit(role: str, content: str | None) -> dict[str, object]:
    return {'role': role, 'content': content, 'tool_calls': [], 'tool_call_id': None}


async def _begun(level: TraceLevel = TraceLevel.REPLAY, **options: Any) -> TraceRecorder:
    recorder = TraceRecorder(_Counting(), RuleRedactor(), level=level, **options)
    await recorder.begin()
    return recorder


def _exchange(
    recorder: TraceRecorder,
    messages: Sequence[object],
    *,
    reply: ContextBlock | None = None,
    error: BaseException | None = None,
    span_id: str = _LLM,
    **params: Any,
) -> None:
    recorder.start_span(span_id, None, SpanKind.LLM_CALL, 'llm', 'agent', {'llm.attempt': 1})
    recorder.llm_exchange(span_id, {'messages': list(messages), **params}, reply, error)
    recorder.end_span(span_id, SpanStatus.OK, {})


def _fingerprinted(
    messages: Sequence[object], redactor: RuleRedactor | None = None, **params: Any
) -> list[str]:
    fingerprints = RequestFingerprints(RuleRedactor() if redactor is None else redactor)
    return fingerprints.of({'messages': list(messages), **params})


def _whole(units: Sequence[str]) -> str:
    return hashlib.sha256(''.join(units).encode()).hexdigest()


# --- construction and lifecycle --------------------------------------------------------------


async def test_the_recorder_is_a_tracer_the_runtime_takes_directly():
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())

    assert isinstance(recorder, Tracer)
    await recorder.begin()
    runtime = Runtime(tracer=recorder)
    try:
        agent_id = await runtime.spawn(
            _factory('Root', ScriptedLLM([ScriptedTurn('ok')])), query='go'
        )
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()
    await recorder.finish()

    kinds = sorted(span.kind.value for span in await store.spans(recorder.trace_id))
    assert kinds == ['agent', 'llm_call', 'turn']


@pytest.mark.parametrize(
    ('options', 'anchor'),
    [
        ({'store': InMemoryTraceStore}, 'TraceStore'),
        ({'store': object()}, 'TraceStore'),
        ({'redactor': object()}, 'RuleRedactor'),
        ({'level': TraceLevel.OFF}, 'TraceLevel.OFF'),
        ({'level': 'replay'}, 'TraceLevel.STRUCTURE'),
        ({'level': True}, 'TraceLevel.STRUCTURE'),
        ({'source': ('run',)}, 'TraceSource'),
        ({'clock': time.time_ns()}, 'time.time_ns'),
        ({'monotonic': None}, 'time.monotonic_ns'),
        ({'trace_id': 'A' * 32}, '32 个小写十六进制'),
        ({'trace_id': 'a' * 31}, '32 个小写十六进制'),
        ({'trace_id': 7}, '32 个小写十六进制'),
    ],
    ids=[
        'store-class',
        'store-object',
        'redactor',
        'level-off',
        'level-str',
        'level-bool',
        'source',
        'clock-called',
        'monotonic-none',
        'trace-id-upper',
        'trace-id-short',
        'trace-id-int',
    ],
)
def test_construction_refuses_what_it_cannot_record_with(options: dict[str, Any], anchor: str):
    arguments: dict[str, Any] = {'store': InMemoryTraceStore(), 'redactor': RuleRedactor()}
    arguments.update(options)

    with pytest.raises(ConfigError) as caught:
        TraceRecorder(**arguments)

    assert anchor in f'{caught.value.expected} {caught.value.next_action}'


async def test_begin_runs_once_and_finish_runs_once_after_it():
    recorder = TraceRecorder(InMemoryTraceStore(), RuleRedactor())
    with pytest.raises(ConfigError) as early:
        await recorder.finish()
    await recorder.begin()
    with pytest.raises(ConfigError) as twice:
        await recorder.begin()
    await recorder.finish()
    with pytest.raises(ConfigError) as again:
        await recorder.finish()

    assert early.value.received == '还没有 begin'
    assert '只 begin 一次' in twice.value.expected
    assert again.value.received == '已经 finish 过'


@pytest.mark.parametrize('status', ['ok', None, True], ids=['str', 'none', 'bool'])
async def test_finish_takes_only_a_span_status_and_a_refusal_does_not_use_it_up(status: object):
    recorder = await _begun()

    with pytest.raises(ConfigError) as caught:
        await recorder.finish(cast(SpanStatus, status))
    await recorder.finish(SpanStatus.ERROR)

    assert 'SpanStatus.OK' in caught.value.next_action


async def test_instrumentation_before_begin_is_refused_with_the_order_to_follow():
    recorder = TraceRecorder(InMemoryTraceStore(), RuleRedactor())

    with pytest.raises(ConfigError) as caught:
        recorder.start_span(_LLM, None, SpanKind.LLM_CALL, 'llm', 'agent', {})

    assert 'begin' in caught.value.next_action


class _SlowBegin(InMemoryTraceStore):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def begin(self, header: Any) -> None:
        self.entered.set()
        await self.release.wait()
        await super().begin(header)


async def test_finish_waits_its_turn_while_begin_is_still_running():
    store = _SlowBegin()
    recorder = TraceRecorder(store, RuleRedactor())
    beginning = asyncio.create_task(recorder.begin())
    await asyncio.wait_for(store.entered.wait(), _TIMEOUT)

    with pytest.raises(ConfigError) as caught:
        await recorder.finish()
    store.release.set()
    await asyncio.wait_for(beginning, _TIMEOUT)
    await recorder.finish()

    assert 'begin 还没完成' in caught.value.received
    header = await store.get(recorder.trace_id)
    assert header is not None and header.status is SpanStatus.OK


@pytest.mark.parametrize(
    'options',
    [{'clock': time.time}, {'monotonic': time.monotonic}, {'clock': lambda: True}],
    ids=['float-clock', 'float-monotonic', 'bool-clock'],
)
async def test_a_clock_that_does_not_count_integer_nanoseconds_is_refused_at_begin(
    options: dict[str, Any],
):
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor(), **options)

    with pytest.raises(ConfigError) as caught:
        await recorder.begin()

    assert '整数纳秒' in caught.value.expected and 'time_ns' in caught.value.next_action
    assert await store.traces() == []


# --- header ----------------------------------------------------------------------------------


async def test_begin_writes_the_header_with_the_ruleset_the_source_and_the_clock():
    store = InMemoryTraceStore()
    redactor = RuleRedactor(rules=[RedactionRule('custom.ticket', r'TCK-[0-9]{6}')])
    source = TraceSource(
        argv=('run', 'ship it'),
        config_sha256='5e' * 32,
        version='0.3.0',
        git_commit='d34db33f',
        replay_of='ab' * 16,
    )
    recorder = TraceRecorder(
        store,
        redactor,
        level=TraceLevel.FULL,
        source=source,
        clock=lambda: 1_700_000_000_000_000_000,
        trace_id='cd' * 16,
    )
    await recorder.begin()

    header = await store.get('cd' * 16)
    assert header is not None and recorder.trace_id == 'cd' * 16
    assert (header.level, header.started_ns, header.status) == (
        TraceLevel.FULL,
        1_700_000_000_000_000_000,
        None,
    )
    assert header.redaction.digest == redactor.digest
    assert header.redaction.rules_version == RULES_VERSION
    assert header.redaction.rules == redactor.rules
    assert header.source == source


def _secrets(seed: int) -> Iterator[str]:
    """看起来像凭证、每次都不同的值：固定装置里不写任何像密钥的字面量。"""
    rng = random.Random(seed)
    alphabet = string.ascii_letters + string.digits
    while True:
        yield ''.join(rng.choice(alphabet) for _ in range(21)) + 'q7Z'


@pytest.mark.parametrize(
    ('argv', 'expected'),
    [
        (('run', '--api-key', '{0}'), ('run', '--api-key', '[REDACTED:secret.named_assignment]')),
        (('run', '--token={0}'), ('run', '--token=[REDACTED:secret.named_assignment]')),
        (
            ('chat', '-p', '--password', '{0}'),
            ('chat', '-p', '--password', '[REDACTED:secret.named_assignment]'),
        ),
        (('run', '--name', '{0}'), ('run', '--name', '{0}')),
        (('run', 'token', '{0}'), ('run', 'token', '{0}')),
        (('{0}', 'run', '--api-key'), ('{0}', 'run', '--api-key')),
        (('run', '--api-key', 'short'), ('run', '--api-key', 'short')),
        (('run', '--trace', 'full', 'ship it'), ('run', '--trace', 'full', 'ship it')),
        (
            ('run', 'deploy', 'API_TOKEN:', '{0}', '--agent', 'Worker'),
            ('run deploy API_TOKEN: [REDACTED:secret.named_assignment] --agent Worker',),
        ),
        (('run', 'password=', '{0}'), ('run password= [REDACTED:secret.named_assignment]',)),
    ],
    ids=[
        'flag-then-value',
        'flag-equals-value',
        'later-pair',
        'harmless-flag',
        'bare-word',
        'first-item-before-a-trailing-flag',
        'short-value',
        'nothing-secret',
        'assignment-across-two-words',
        'equals-then-value',
    ],
)
async def test_argv_is_redacted_before_the_header_is_written(
    argv: tuple[str, ...], expected: tuple[str, ...]
):
    secret = next(_secrets(len(argv)))
    store = InMemoryTraceStore()
    recorder = TraceRecorder(
        store, RuleRedactor(), source=TraceSource(argv=tuple(a.format(secret) for a in argv))
    )
    await recorder.begin()

    header = await store.get(recorder.trace_id)
    assert header is not None
    assert header.source.argv == tuple(e.format(secret) for e in expected)


def _argv_cases() -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    rng = random.Random(41)
    alphabet = string.ascii_letters + string.digits
    token = 'ghp_' + ''.join(rng.choice(alphabet) for _ in range(36))
    value = ''.join(rng.choice(alphabet) for _ in range(21)) + 'k8P'
    symbols = ''.join(rng.choice(string.ascii_letters) for _ in range(6)) + '3$5^7&9*Q!'
    named = '[REDACTED:secret.named_assignment]'
    return [
        ((token, 'run'), ('[REDACTED:secret.github_token]', 'run')),
        (('--api-key', value, 'run'), ('--api-key', named, 'run')),
        (('run', '--password', symbols), ('run', '--password', named)),
    ]


@pytest.mark.parametrize(
    ('argv', 'expected'),
    _argv_cases(),
    ids=['first-item', 'value-right-after-a-leading-flag', 'value-with-symbols'],
)
async def test_argv_redaction_reaches_every_position_and_every_credential_shape(
    argv: tuple[str, ...], expected: tuple[str, ...]
):
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor(), source=TraceSource(argv=argv))
    await recorder.begin()

    header = await store.get(recorder.trace_id)
    assert header is not None and header.source.argv == expected


# --- clocks ----------------------------------------------------------------------------------


async def test_time_is_the_wall_clock_at_begin_plus_what_the_monotonic_clock_measured_since():
    wall = iter([1_000_000])
    mono = iter([100, 200, 260, 900])
    store = InMemoryTraceStore()
    recorder = TraceRecorder(
        store, RuleRedactor(), clock=lambda: next(wall), monotonic=lambda: next(mono)
    )
    await recorder.begin()

    recorder.start_span(_LLM, None, SpanKind.LLM_CALL, 'llm', 'agent', {'llm.attempt': 1})
    recorder.end_span(_LLM, SpanStatus.OK, {})
    await recorder.finish()

    [span] = await store.spans(recorder.trace_id)
    header = await store.get(recorder.trace_id)
    assert (span.start_ns, span.end_ns) == (1_000_100, 1_000_160)
    assert header is not None and (header.started_ns, header.ended_ns) == (1_000_000, 1_000_800)


@pytest.mark.parametrize(
    ('readings', 'expected'),
    [([500, 500, 100, 100], (0, 0, 0)), ([100, 300, 500, 200], (200, 400, 400))],
    ids=['below-begin', 'below-a-span-end'],
)
async def test_a_monotonic_clock_that_steps_back_never_moves_time_backwards(
    readings: list[int], expected: tuple[int, int, int]
):
    mono = iter(readings)
    store = InMemoryTraceStore()
    recorder = TraceRecorder(
        store, RuleRedactor(), clock=lambda: 7_000, monotonic=lambda: next(mono)
    )
    await recorder.begin()

    recorder.start_span(_LLM, None, SpanKind.LLM_CALL, 'llm', 'agent', {'llm.attempt': 1})
    recorder.end_span(_LLM, SpanStatus.OK, {})
    await recorder.finish()

    [span] = await store.spans(recorder.trace_id)
    header = await store.get(recorder.trace_id)
    assert header is not None and header.ended_ns is not None
    assert (span.start_ns - 7_000, span.end_ns - 7_000, header.ended_ns - 7_000) == expected


# --- buffering and order ---------------------------------------------------------------------


class _Slow(_Counting):
    """`append` 停在 `release` 上，直到测试放开。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def append(
        self,
        trace_id: str,
        spans: Sequence[Span] = (),
        edges: Sequence[CausalityEdge] = (),
    ) -> None:
        self.entered.set()
        await self.release.wait()
        await super().append(trace_id, spans, edges)


async def test_a_run_never_waits_for_the_store():
    store = _Slow()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('c', 'Echo')]), ScriptedTurn(content='ok')])
    runtime = Runtime(tracer=recorder)
    try:
        agent_id = await runtime.spawn(_factory('Root', llm, {'Echo': _Echo()}), query='go')
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        await asyncio.wait_for(store.entered.wait(), _TIMEOUT)
    finally:
        await runtime.shutdown()
    finishing = asyncio.create_task(recorder.finish())
    await asyncio.sleep(0)

    assert context.stop_reason is StopReason.COMPLETED
    assert await store.spans(recorder.trace_id) == [] and not finishing.done()
    store.release.set()
    await asyncio.wait_for(finishing, _TIMEOUT)
    kinds = [span.kind for span in await store.spans(recorder.trace_id)]
    assert sorted(kind.value for kind in kinds) == [
        'agent',
        'llm_call',
        'llm_call',
        'tool_call',
        'turn',
        'turn',
    ]


class _Checking(_Counting):
    """append 时核对：这批 span 引用的每一份 payload 此刻都已经读得回来。"""

    def __init__(self) -> None:
        super().__init__()
        self.checked = 0
        self.missing: list[str] = []

    async def append(
        self,
        trace_id: str,
        spans: Sequence[Span] = (),
        edges: Sequence[CausalityEdge] = (),
    ) -> None:
        for span in spans:
            for key in _PAYLOAD_KEYS:
                if key in span.attributes:
                    self.checked += 1
                    ref = PayloadRef(trace_id, cast(str, span.attributes[key]))
                    if await self.payload(ref) is None:
                        self.missing.append(f'{span.name}:{key}')
        await super().append(trace_id, spans, edges)


async def test_every_payload_is_stored_before_the_span_that_refers_to_it():
    store = _Checking()
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[_call('c', 'Echo', text='a'), _call('d', 'AskUser', question='?')]
            ),
            ScriptedTurn(content='ok'),
        ]
    )
    await _recorded(
        _factory('Root', llm, {'Echo': _Echo()}), level=TraceLevel.FULL, store=store, answer='yes'
    )

    assert store.missing == []
    assert store.checked == 2 * 2 + 2 + 1


async def test_spans_are_stored_in_the_order_they_ended_across_batches():
    store = _Counting()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    first, second, third = (span_id_of(SpanKind.TURN, 'agent', turn) for turn in (1, 2, 3))

    for span_id in (first, second, third):
        recorder.start_span(span_id, None, SpanKind.TURN, 'turn', 'agent', {})
    recorder.end_span(second, SpanStatus.OK, {})
    await asyncio.sleep(0)
    recorder.end_span(third, SpanStatus.OK, {})
    recorder.end_span(first, SpanStatus.OK, {})
    await recorder.finish()

    assert [span.span_id for span in await store.spans(recorder.trace_id)] == [second, third, first]
    assert len(store.appended) >= 2


async def test_finish_writes_spans_left_open_as_cancelled_and_then_the_outcome():
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    recorder.start_span(_LLM, None, SpanKind.LLM_CALL, 'llm', 'agent', {'llm.attempt': 1})

    await recorder.finish(SpanStatus.ERROR)
    recorder.start_span(_TOOL, None, SpanKind.TOOL_CALL, 'tool', 'agent', {})
    recorder.end_span(_TOOL, SpanStatus.OK, {})
    recorder.add_edge(CausalityEdge(EdgeKind.SPAWN, _LLM, _TOOL, {'child.agent_id': 'c'}))

    [span] = await store.spans(recorder.trace_id)
    header = await store.get(recorder.trace_id)
    assert (span.span_id, span.status) == (_LLM, SpanStatus.CANCELLED)
    assert await store.edges(recorder.trace_id) == []
    assert header is not None and header.status is SpanStatus.ERROR
    assert recorder.dropped == 2


async def test_a_cancelled_finish_still_drains_what_was_queued_and_leaves_no_outcome():
    store = _Slow()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    recorder.start_span(_LLM, None, SpanKind.LLM_CALL, 'llm', 'agent', {'llm.attempt': 1})
    recorder.end_span(_LLM, SpanStatus.OK, {})
    await asyncio.wait_for(store.entered.wait(), _TIMEOUT)

    finishing = asyncio.create_task(recorder.finish())
    await asyncio.sleep(0)
    finishing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finishing
    store.release.set()
    await asyncio.wait_for(_until(lambda: bool(store.appended)), _TIMEOUT)

    assert [span.span_id for span in await store.spans(recorder.trace_id)] == [_LLM]
    header = await store.get(recorder.trace_id)
    assert header is not None and header.status is None


async def _until(condition: Callable[[], bool]) -> None:
    while True:
        if condition():
            return
        await asyncio.sleep(0.001)


# --- levels ----------------------------------------------------------------------------------


def _ask_and_echo() -> ScriptedLLM:
    return ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('c', 'Echo', text='a')]),
            ScriptedTurn(tool_calls=[_call('d', 'AskUser', question='which?')]),
            ScriptedTurn(content='ok'),
        ]
    )


async def test_structure_records_spans_and_edges_and_no_payload_or_fingerprint():
    store, recorder, _, _ = await _recorded(
        _factory('Root', _ask_and_echo(), {'Echo': _Echo()}), level=TraceLevel.STRUCTURE, answer='y'
    )

    spans = await _spans(cast(_Counting, store), recorder)
    assert cast(_Counting, store).payload_texts == []
    assert len(spans) == 1 + 3 + 3 + 2 + 1 + 1
    assert [span.name for span in spans.values() if span.kind is SpanKind.MODULE] == [
        'human-interaction'
    ]
    assert all(
        key not in span.attributes
        for span in spans.values()
        for key in (*_PAYLOAD_KEYS, 'llm.request_digest')
    )


async def test_replay_records_replies_results_answers_and_fingerprints_but_not_requests():
    store, recorder, agent_id, _ = await _recorded(
        _factory('Root', _ask_and_echo(), {'Echo': _Echo()}), answer='this one'
    )

    spans = await _spans(store, recorder)
    llm_calls = [span for span in spans.values() if span.kind is SpanKind.LLM_CALL]
    assert len(llm_calls) == 3
    for span in llm_calls:
        assert {'llm.request_digest', 'llm.response'} <= set(span.attributes)
        assert not {'llm.request', 'llm.request_units'} & set(span.attributes)
    for index in (0,):
        tool = spans[span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, index)]
        assert (await _payload(store, recorder, tool.attributes['tool.result']))['output'] == 'a'
    [wait] = [span for span in spans.values() if span.kind is SpanKind.WAIT]
    assert await _payload(store, recorder, wait.attributes['wait.answer']) == {
        'kind': 'answer',
        'sender': 'human',
        'content': 'this one',
        'trigger': 'pending',
        'trust': 'trusted',
    }


async def test_full_also_records_each_whole_request():
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('c', 'Echo', text='a')]), ScriptedTurn('ok')])
    store, recorder, agent_id, _ = await _recorded(
        _factory('Root', llm, {'Echo': _Echo()}, prompt='Be brief.'),
        level=TraceLevel.FULL,
        query='hello',
    )

    spans = await _spans(store, recorder)
    second = spans[span_id_of(SpanKind.LLM_CALL, agent_id, 2, 1)]
    request = await _payload(store, recorder, second.attributes['llm.request'])
    assert list(request) == ['messages', 'params']
    assert [(m['role'], m['content']) for m in request['messages']] == [
        ('system', 'Be brief.'),
        ('user', 'hello'),
        ('assistant', None),
        ('tool', 'a'),
    ]
    assert request['messages'][2]['tool_calls'] == [
        {'id': 'c', 'name': 'Echo', 'arguments': {'text': 'a'}}
    ]
    assert [tool['name'] for tool in request['params']['tools']] == ['Echo']


# --- payload contents ------------------------------------------------------------------------


async def test_a_reply_is_recorded_with_parsed_arguments_and_its_extra():
    recorder = await _begun()
    reply = ContextBlock(
        role='assistant',
        content='calling',
        tool_calls=[
            ToolCall(id='c1', name='Echo', arguments='{"b": 2, "a": [1, true, null]}'),
            ToolCall(id='c2', name='Echo', arguments='{not json'),
            ToolCall(id='c3', name='Echo', arguments='"ls"'),
            ToolCall(id='c4', name='Echo', arguments='ls'),
        ],
        extra={'llm_route': 'main', 'usage': {'input_tokens': 3}},
        trust=TrustLevel.UNTRUSTED,
    )
    _exchange(recorder, [ContextBlock(role='user', content='go')], reply=reply)
    store = cast(InMemoryTraceStore, recorder._store)  # pyright: ignore[reportPrivateUsage]
    await recorder.finish()

    spans = await _spans(store, recorder)
    assert await _payload(store, recorder, spans[_LLM].attributes['llm.response']) == {
        'kind': 'reply',
        'role': 'assistant',
        'content': 'calling',
        'tool_calls': [
            {'id': 'c1', 'name': 'Echo', 'arguments': {'a': [1, True, None], 'b': 2}},
            {'id': 'c2', 'name': 'Echo', 'arguments_raw': '{not json'},
            {'id': 'c3', 'name': 'Echo', 'arguments': 'ls'},
            {'id': 'c4', 'name': 'Echo', 'arguments_raw': 'ls'},
        ],
        'tool_call_id': None,
        'extra': {'llm_route': 'main', 'usage': {'input_tokens': 3}},
        'trust': 'untrusted',
    }


class _ProviderError(Exception):
    def __init__(self, message: str, status_code: object, retry_after: object) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@pytest.mark.parametrize(
    ('status_code', 'retry_after', 'numbers'),
    [
        (429, 1.5, {'status_code': 429, 'retry_after': 1.5}),
        (True, '2', {}),
        (None, float('inf'), {'retry_after': 'inf'}),
    ],
    ids=['numbers', 'not-numbers', 'infinite'],
)
async def test_a_model_error_is_recorded_with_its_type_and_numbers(
    status_code: object, retry_after: object, numbers: dict[str, object]
):
    recorder = await _begun()
    error = _ProviderError('slow down', status_code, retry_after)
    _exchange(recorder, [ContextBlock(role='user', content='go')], error=error)
    store = cast(InMemoryTraceStore, recorder._store)  # pyright: ignore[reportPrivateUsage]
    await recorder.finish()

    spans = await _spans(store, recorder)
    assert await _payload(store, recorder, spans[_LLM].attributes['llm.response']) == {
        'kind': 'error',
        'type': f'{__name__}._ProviderError',
        'message': 'slow down',
        **numbers,
    }


async def test_a_tool_outcome_is_recorded_whole_or_as_the_error_it_raised():
    recorder = await _begun()
    artifact = ArtifactRef(
        id='a1', path='out/report.md', kind='doc', mime='text/markdown', version='v1', producer='p'
    )
    result = ToolResult(
        output=['line', {'n': 1}],
        meta={'rows': 3},
        artifacts=[artifact],
        trust=TrustLevel.UNTRUSTED,
    )
    failing = span_id_of(SpanKind.TOOL_CALL, 'agent', 1, 1, 1)
    for span_id, value, error in ((_TOOL, result, None), (failing, None, OSError('disk full'))):
        recorder.start_span(span_id, None, SpanKind.TOOL_CALL, 'tool Echo', 'agent', {})
        recorder.tool_result(span_id, value, error)
        recorder.end_span(span_id, SpanStatus.OK, {})
    store = cast(InMemoryTraceStore, recorder._store)  # pyright: ignore[reportPrivateUsage]
    await recorder.finish()

    spans = await _spans(store, recorder)
    assert await _payload(store, recorder, spans[_TOOL].attributes['tool.result']) == {
        'kind': 'result',
        'output': ['line', {'n': 1}],
        'meta': {'rows': 3},
        'trust': 'untrusted',
        'artifacts': [
            {
                'id': 'a1',
                'path': 'out/report.md',
                'kind': 'doc',
                'mime': 'text/markdown',
                'version': 'v1',
                'producer': 'p',
                'is_deliverable': False,
            }
        ],
    }
    assert await _payload(store, recorder, spans[failing].attributes['tool.result']) == {
        'kind': 'error',
        'type': 'builtins.OSError',
        'message': 'disk full',
    }


class _AwaitReply(Tool):
    __tool_name__ = 'AwaitReply'
    __tool_command_args__ = ()

    def __init__(self, runtime: list[Runtime], agent_id: list[str]) -> None:
        self._runtime = runtime
        self._agent_id = agent_id

    async def execute(self) -> ToolResult:
        """等一条带关联 ID q1 的答复，答复它的是宿主。"""
        runtime = self._runtime[0]
        waiting = asyncio.create_task(
            runtime.wait_for(self._agent_id[0], Reply(correlation_id='q1', timeout=_TIMEOUT))
        )
        await asyncio.sleep(0.01)
        await runtime.send(self._agent_id[0], 'from a peer', activate=False, correlation_id='q1')
        return ToolResult(output=(await waiting).state.value)


async def test_only_a_human_answer_is_recorded_on_its_wait():
    runtime_box: list[Runtime] = []
    agent_box: list[str] = []
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('w', 'AwaitReply')]), ScriptedTurn('ok')])

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm)(hooks, events)
        agent_box.append(agent.agent_id)
        agent.install_runtime_tools({'AwaitReply': _AwaitReply(runtime_box, agent_box)})
        return agent

    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    runtime = Runtime(tracer=recorder)
    runtime_box.append(runtime)
    try:
        agent_id = await runtime.spawn(build, query='go')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()
    await recorder.finish()

    [wait] = [span for span in await store.spans(recorder.trace_id) if span.kind is SpanKind.WAIT]
    assert (wait.attributes['wait.kind'], wait.attributes['wait.outcome']) == ('agent', 'answered')
    assert 'wait.answer' not in wait.attributes


class _Odd(enum.Enum):
    ONE = 'one'


@dataclasses.dataclass
class _Point:
    x: int
    y: float


async def test_values_json_cannot_hold_are_recorded_as_text_instead_of_losing_the_reply():
    recorder = await _begun()
    nested: dict[str, object] = {}
    deepest = nested
    for _ in range(80):
        deepest['next'] = {}
        deepest = cast(dict[str, object], deepest['next'])
    extra = {
        'set': {'b', 'a', 'c'},
        'ints': {8, 1, 3},
        'enum': _Odd.ONE,
        'point': _Point(1, float('nan')),
        'bytes': b'\x00',
        2: 'int key',
        'deep': nested,
    }
    reply = ContextBlock(role='assistant', content='ok', extra=cast(Any, extra))
    _exchange(recorder, [ContextBlock(role='user', content='go')], reply=reply)
    store = cast(InMemoryTraceStore, recorder._store)  # pyright: ignore[reportPrivateUsage]
    await recorder.finish()

    spans = await _spans(store, recorder)
    recorded = (await _payload(store, recorder, spans[_LLM].attributes['llm.response']))['extra']
    assert list({8, 1, 3}) != [1, 3, 8]
    assert (recorded['set'], recorded['ints']) == (['a', 'b', 'c'], [1, 3, 8])
    assert recorded['enum'] == 'one'
    assert recorded['point'] == {'x': 1, 'y': 'nan'}
    assert recorded['bytes'] == "b'\\x00'"
    assert recorded['2'] == 'int key'
    assert '<nested too deep>' in json.dumps(recorded['deep'])
    assert recorder.dropped == 0


# --- fingerprints ----------------------------------------------------------------------------


async def test_a_request_is_fingerprinted_as_params_system_tools_and_each_message():
    recorder = await _begun()
    tools = [{'type': 'function', 'function': {'name': 'Echo'}}]
    messages = [
        ContextBlock(role='system', content='You help.'),
        ContextBlock(role='user', content='go'),
        ContextBlock(role='assistant', content=None, tool_calls=[ToolCall('c', 'Echo', '{}')]),
    ]
    _exchange(recorder, messages, tools=tools, temperature=0)
    store = cast(InMemoryTraceStore, recorder._store)  # pyright: ignore[reportPrivateUsage]
    await recorder.finish()

    spans = await _spans(store, recorder)
    units = _fingerprinted(messages, tools=tools, temperature=0)
    assert units == [
        _unit_digest({'temperature': 0}),
        _unit_digest(_block_unit('system', 'You help.')),
        _unit_digest(tools),
        _unit_digest(_block_unit('user', 'go')),
        _unit_digest(
            {
                'role': 'assistant',
                'content': None,
                'tool_calls': [{'id': 'c', 'name': 'Echo', 'arguments': {}}],
                'tool_call_id': None,
            }
        ),
    ]
    assert spans[_LLM].attributes['llm.request_digest'] == _whole(units)
    assert 'llm.request_units' not in spans[_LLM].attributes


async def test_a_request_without_a_system_message_fingerprints_an_empty_system_unit():
    units = _fingerprinted([ContextBlock(role='user', content='go')])

    assert units == [
        _unit_digest({}),
        _unit_digest(None),
        _unit_digest([]),
        _unit_digest(_block_unit('user', 'go')),
    ]


@pytest.mark.parametrize(
    ('left', 'right'),
    [
        (
            ContextBlock(role='tool', content='x', tool_call_id='c', trust=TrustLevel.TRUSTED),
            ContextBlock(
                role='tool',
                content='x',
                tool_call_id='c',
                trust=TrustLevel.UNTRUSTED,
                extra={'llm_route': 'b'},
            ),
        ),
        (
            ContextBlock(
                role='assistant', content=None, tool_calls=[ToolCall('c', 'E', '{"a":1,"b":2}')]
            ),
            ContextBlock(
                role='assistant',
                content=None,
                tool_calls=[ToolCall('c', 'E', '{ "b": 2, "a": 1 }')],
            ),
        ),
    ],
    ids=['trust-and-extra', 'argument-bytes'],
)
async def test_what_the_provider_never_sees_does_not_change_a_fingerprint(
    left: ContextBlock, right: ContextBlock
):
    digests = [_fingerprinted([block]) for block in (left, right)]

    assert digests[0] == digests[1]


async def test_a_fingerprint_is_taken_after_redaction():
    keys = _secrets(11)
    digests = [
        _fingerprinted([ContextBlock(role='user', content=f'use API_TOKEN={next(keys)}')])
        for _ in range(2)
    ]

    assert digests[0] == digests[1]
    assert digests[0][3] == _unit_digest(
        _block_unit('user', 'use API_TOKEN=[REDACTED:secret.named_assignment]')
    )


async def test_each_unit_is_redacted_once_however_many_requests_repeat_it(
    monkeypatch: pytest.MonkeyPatch,
):
    import syndica.kernel.trace_recorder as module

    encoded: list[object] = []
    original = module.encode_redacted

    def counting(value: object, redactor: Any) -> RedactedText:
        encoded.append(value)
        return original(value, redactor)

    monkeypatch.setattr(module, 'encode_redacted', counting)
    recorder = await _begun()
    history: list[ContextBlock] = [ContextBlock(role='system', content='You help.')]
    for turn in range(1, 4):
        history.append(ContextBlock(role='user', content=f'message {turn}'))
        _exchange(recorder, history, span_id=span_id_of(SpanKind.LLM_CALL, 'agent', turn, 1))
    await recorder.finish()

    units = [value for value in encoded if not isinstance(value, list)]
    assert units == [
        {},
        _block_unit('system', 'You help.'),
        _block_unit('user', 'message 1'),
        _block_unit('user', 'message 2'),
        _block_unit('user', 'message 3'),
    ]


def test_the_unit_cache_is_keyed_by_content_and_not_by_position():
    fingerprints = RequestFingerprints(RuleRedactor())

    one = fingerprints.of(
        {
            'messages': [
                ContextBlock(role='system', content='A'),
                ContextBlock(role='user', content='u'),
            ]
        }
    )
    two = fingerprints.of(
        {
            'messages': [
                ContextBlock(role='system', content='B'),
                ContextBlock(role='user', content='v'),
                ContextBlock(role='user', content='u'),
            ]
        }
    )

    assert one[1] != two[1] and one[3] != two[3]
    assert one[3] == two[4] == _unit_digest(_block_unit('user', 'u'))


def _history(turns: int) -> list[ContextBlock]:
    blocks = [ContextBlock(role='system', content='You help.')]
    for index in range(turns):
        blocks.append(
            ContextBlock(
                role='assistant',
                content=None,
                tool_calls=[ToolCall(f'c{index}', 'Echo', json.dumps({'text': f't{index}'}))],
            )
        )
        blocks.append(ContextBlock(role='tool', content=f'out {index}', tool_call_id=f'c{index}'))
    return blocks


def test_a_warm_unit_cache_gives_what_a_cold_one_gives_however_the_history_grows():
    warm = RequestFingerprints(RuleRedactor())
    history = _history(6)
    requests = [history[: 2 * turns + 1] for turns in range(1, 7)]
    requests.append([*history[:-1], dataclasses.replace(history[-1], content='changed')])
    requests.append(
        [
            *history,
            ContextBlock(role='tool', content='out 0', tool_call_id='c-other'),
            ContextBlock(role='user', content='same words'),
            ContextBlock(role='assistant', content='same words'),
            ContextBlock(role='user', content=[{'type': 'text', 'text': 'same words'}]),
        ]
    )

    def both(messages: list[ContextBlock]) -> tuple[list[str], list[str]]:
        cold = RequestFingerprints(RuleRedactor())
        return warm.of({'messages': list(messages)}), cold.of({'messages': list(messages)})

    def alone(block: ContextBlock) -> str:
        return RequestFingerprints(RuleRedactor()).of({'messages': [block]})[3]

    pairs = [both(messages) for messages in requests]
    near = requests[-1]
    assert pairs[-1][0][3:] == [alone(block) for block in near[1:]]
    history[-2].tool_calls[0] = ToolCall('c5', 'Echo', '{"text": "rewritten"}')
    pairs.append(both(history))

    assert [seen for seen, _ in pairs] == [fresh for _, fresh in pairs]
    assert pairs[-1][0][-2] != pairs[-3][0][-2]


def test_each_history_block_is_encoded_once_however_many_requests_repeat_it(
    monkeypatch: pytest.MonkeyPatch,
):
    encoded: list[object] = []
    real = recorder_module._unit_value  # pyright: ignore[reportPrivateUsage]

    def counting(block: object) -> object:
        encoded.append(block)
        return real(block)

    monkeypatch.setattr(recorder_module, '_unit_value', counting)
    fingerprints = RequestFingerprints(RuleRedactor())
    history = _history(30)
    for turns in range(1, 31):
        fingerprints.of({'messages': [dataclasses.replace(b) for b in history[: 2 * turns + 1]]})

    assert len(encoded) == len(history) - 1


async def test_a_hook_that_put_something_else_in_the_messages_does_not_lose_the_exchange():
    recorder = await _begun(level=TraceLevel.FULL)
    _exchange(
        recorder,
        [{'role': 'user', 'content': 'raw dict'}],
        reply=ContextBlock(role='assistant', content='ok'),
    )
    store = cast(InMemoryTraceStore, recorder._store)  # pyright: ignore[reportPrivateUsage]
    await recorder.finish()

    spans = await _spans(store, recorder)
    request = await _payload(store, recorder, spans[_LLM].attributes['llm.request'])
    assert request['messages'] == [{'role': 'user', 'content': 'raw dict'}]
    assert (await _payload(store, recorder, spans[_LLM].attributes['llm.response']))[
        'content'
    ] == 'ok'


# --- store failures --------------------------------------------------------------------------


class _Failing(_Counting):
    def __init__(self, method: str) -> None:
        super().__init__()
        self.method = method

    async def put_payload(self, trace_id: str, payload: RedactedText) -> PayloadRef:
        if self.method == 'put_payload':
            raise OSError('disk full')
        return await super().put_payload(trace_id, payload)

    async def append(
        self,
        trace_id: str,
        spans: Sequence[Span] = (),
        edges: Sequence[CausalityEdge] = (),
    ) -> None:
        if self.method == 'append':
            raise OSError('disk full')
        await super().append(trace_id, spans, edges)

    async def finish(self, trace_id: str, status: SpanStatus, ended_ns: int) -> None:
        if self.method == 'finish':
            raise OSError('disk full')
        await super().finish(trace_id, status, ended_ns)


def _delegating() -> tuple[Callable[[HooksCenter, EventBus], BaseAgent], dict[str, Any]]:
    child = _factory('Child', ScriptedLLM([ScriptedTurn('child done')]))
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('d', 'Delegate', agent='Child', query='work')]),
            ScriptedTurn(tool_calls=[_call('e', 'Echo', text='a')]),
            ScriptedTurn('parent done'),
        ]
    )
    return _factory('Parent', llm, {'Echo': _Echo()}), {
        'orchestration': True,
        'agents': {'Child': child},
    }


async def _stored_count(store: _Counting, recorder: TraceRecorder) -> int:
    spans = await store.spans(recorder.trace_id)
    edges = await store.edges(recorder.trace_id)
    return len(store.payload_texts) + len(spans) + len(edges)


@pytest.mark.parametrize('method', ['put_payload', 'append', 'finish'])
async def test_a_failing_store_leaves_the_run_alone_and_reports_once(
    method: str, caplog: pytest.LogCaptureFixture
):
    build, options = _delegating()
    reference, reference_recorder, _, reference_context = await _recorded(build, **options)
    produced = await _stored_count(cast(_Counting, reference), reference_recorder)

    build, options = _delegating()
    store = _Failing(method)
    caplog.clear()
    _, recorder, _, context = await _recorded(build, store=store, **options)

    assert context.stop_reason is reference_context.stop_reason is StopReason.COMPLETED
    assert context.current_traj[-1].content == 'parent done'
    reports = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    [report] = reports
    assert report.levelno == logging.ERROR
    step = 'finish' if method == 'finish' else '写入'
    assert cast(tuple[object, ...], report.args)[:4] == (
        recorder.trace_id,
        '_Failing',
        step,
        'OSError',
    )
    assert recorder.dropped == produced - await _stored_count(store, recorder)
    assert (recorder.dropped > 0) is (method != 'finish')
    assert '检查 store' in str(cast(tuple[object, ...], report.args)[5])
    header = await store.get(recorder.trace_id)
    assert header is not None and header.status is None
    assert recorder.complete is False
    assert [t for t in asyncio.all_tasks() if t.get_name() == f'trace:{recorder.trace_id}'] == []


class _CancelsItself(_Counting):
    """store 自己抛出 `CancelledError`（它内部的超时、它自己的任务被取消），写入任务并没有被
    取消。"""

    def __init__(self, method: str) -> None:
        super().__init__()
        self.method = method

    async def put_payload(self, trace_id: str, payload: RedactedText) -> PayloadRef:
        if self.method == 'put_payload':
            raise asyncio.CancelledError()
        return await super().put_payload(trace_id, payload)

    async def append(
        self,
        trace_id: str,
        spans: Sequence[Span] = (),
        edges: Sequence[CausalityEdge] = (),
    ) -> None:
        if self.method == 'append':
            raise asyncio.CancelledError()
        await super().append(trace_id, spans, edges)

    async def finish(self, trace_id: str, status: SpanStatus, ended_ns: int) -> None:
        if self.method == 'finish':
            raise asyncio.CancelledError()
        await super().finish(trace_id, status, ended_ns)


@pytest.mark.parametrize('method', ['put_payload', 'append', 'finish'])
async def test_a_cancelled_error_the_store_raises_is_a_store_failure_and_not_a_cancellation(
    method: str, caplog: pytest.LogCaptureFixture
):
    build, options = _delegating()
    reference, reference_recorder, _, _ = await _recorded(build, **options)
    produced = await _stored_count(cast(_Counting, reference), reference_recorder)

    build, options = _delegating()
    store = _CancelsItself(method)
    caplog.clear()
    _, recorder, _, context = await _recorded(build, store=store, **options)

    assert context.stop_reason is StopReason.COMPLETED
    [report] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    assert report.levelno == logging.ERROR
    assert cast(tuple[object, ...], report.args)[3] == 'CancelledError'
    assert recorder.dropped == produced - await _stored_count(store, recorder)
    assert recorder.complete is False


class _Gate(Tool):
    __tool_name__ = 'Gate'
    __tool_command_args__ = ()

    def __init__(self, *, opened: bool = False) -> None:
        self.entered = [asyncio.Event(), asyncio.Event()]
        self.open = [asyncio.Event(), asyncio.Event()]
        if opened:
            for event in self.open:
                event.set()
        self._calls = 0

    async def execute(self) -> ToolResult:
        """第 n 次调用停在第 n 道门上，等测试放行。"""
        index = self._calls
        self._calls += 1
        self.entered[index].set()
        await self.open[index].wait()
        return ToolResult(output='through')


def _gated(gate: _Gate) -> Callable[[HooksCenter, EventBus], BaseAgent]:
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('g1', 'Gate')]),
            ScriptedTurn(tool_calls=[_call('e', 'Echo', text='between')]),
            ScriptedTurn(tool_calls=[_call('g2', 'Gate')]),
            ScriptedTurn('done'),
        ]
    )
    return _factory('Root', llm, {'Gate': gate, 'Echo': _Echo()})


async def test_a_writer_the_host_cancelled_costs_the_trace_and_not_whoever_finishes_it(
    caplog: pytest.LogCaptureFixture,
):
    reference, reference_recorder, _, _ = await _recorded(_gated(_Gate(opened=True)))
    produced = await _stored_count(cast(_Counting, reference), reference_recorder)

    store = _Slow()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    gate = _Gate()
    runtime = Runtime(tracer=recorder)
    try:
        agent_id = await runtime.spawn(_gated(gate), query='go')
        await asyncio.wait_for(store.entered.wait(), _TIMEOUT)
        await asyncio.wait_for(gate.entered[0].wait(), _TIMEOUT)
        gate.open[0].set()
        await asyncio.wait_for(gate.entered[1].wait(), _TIMEOUT)
        [writer] = [t for t in asyncio.all_tasks() if t.get_name() == f'trace:{recorder.trace_id}']
        caplog.clear()
        writer.cancel()
        await asyncio.wait_for(_until(writer.done), _TIMEOUT)
        await asyncio.sleep(0)
        reported = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
        gate.open[1].set()
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()
    await recorder.finish()

    assert context.stop_reason is StopReason.COMPLETED
    [report] = reported
    assert [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder'] == [report]
    args = cast(tuple[object, ...], report.args)
    assert (report.levelno, args[0], args[1]) == (logging.ERROR, recorder.trace_id, '_Slow')
    assert '写入任务被取消' in str(args[2])
    assert cast(int, args[3]) > 0
    assert recorder.dropped == produced - await _stored_count(store, recorder)
    header = await store.get(recorder.trace_id)
    assert header is not None and header.status is None
    assert recorder.complete is False


async def _relaying(store: _Counting, level: TraceLevel) -> tuple[TraceRecorder, Any]:
    """十二轮 Echo，再派一个子 Agent、用一条消息叫醒它、销毁它：派生边、消息边与事件都在后头。"""
    runtime_box: list[Runtime] = []
    calls = [
        ScriptedTurn(tool_calls=[_call(f'c{index}', 'Echo', text=f'turn {index}')])
        for index in range(12)
    ]
    llm = ScriptedLLM(
        [*calls, ScriptedTurn(tool_calls=[_call('r', 'Relay')]), ScriptedTurn('done')]
    )
    build = _factory('Root', llm, {'Echo': _Echo(), 'Relay': _Relay(runtime_box, [])})
    recorder = TraceRecorder(store, RuleRedactor(), level=level)
    recorder.describe_run(
        host={},
        roles={'Root': {'system_prompt': 'You help.', 'tools': ['Echo', 'Relay']}},
        entry={'mode': 'query', 'agent': 'Root', 'prompt': 'go'},
    )
    await recorder.begin()
    runtime = Runtime(agents={'Root': build}, tracer=recorder, ids=recorder.ids)
    runtime_box.append(runtime)
    try:
        agent_id = await runtime.spawn(build, query='go')
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()
    await recorder.finish()
    return recorder, context


@pytest.mark.parametrize('level', [TraceLevel.REPLAY, TraceLevel.FULL])
async def test_once_the_store_failed_nothing_more_is_encoded_and_dropped_stays_exact(
    level: TraceLevel, monkeypatch: pytest.MonkeyPatch
):
    reference = _Counting()
    reference_recorder, _ = await _relaying(reference, level)
    produced = await _stored_count(reference, reference_recorder)

    failed: list[logging.LogRecord] = []
    after: list[str] = []

    class _Noting(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.ERROR:
                failed.append(record)

    def counting(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            if failed:
                after.append(name)
            return real(*args, **kwargs)

        return call

    monkeypatch.setattr(
        recorder_module, 'encode_redacted', counting('encode', recorder_module.encode_redacted)
    )
    fingerprints = recorder_module.RequestFingerprints
    monkeypatch.setattr(fingerprints, 'of_parts', counting('of_parts', fingerprints.of_parts))
    capture = recorder_module.ReplayCapture
    for name in ('started', 'ended', 'exchanged', 'returned', 'sent', 'consumed'):
        monkeypatch.setattr(capture, name, counting(name, getattr(capture, name)))
    monkeypatch.setattr(RuleRedactor, 'redact', counting('redact', RuleRedactor.redact))
    handler = _Noting()
    logging.getLogger('syndica.kernel.trace_recorder').addHandler(handler)
    try:
        store = _Failing('append')
        recorder, context = await _relaying(store, level)
    finally:
        logging.getLogger('syndica.kernel.trace_recorder').removeHandler(handler)

    assert context.stop_reason is StopReason.COMPLETED
    assert len(failed) == 1
    assert after == []
    assert recorder.dropped == produced - await _stored_count(store, recorder)


class _Capped(_Counting):
    """单项有上限的 store（像 DynamoDB 的 400 KB）：大的那一段收不下，store 本身没坏。"""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.refused: list[int] = []

    async def put_payload(self, trace_id: str, payload: RedactedText) -> PayloadRef:
        size = len(str(payload).encode('utf-8'))
        if size > self.limit:
            self.refused.append(size)
            raise PayloadRefused(
                expected=f'一段 payload 不超过 {self.limit} 字节',
                received=f'这一段 {size} 字节',
                next_action='换一个单项上限更大的 store，或让工具把大的输出写成文件',
            )
        return await super().put_payload(trace_id, payload)


async def test_a_payload_the_store_cannot_take_costs_only_that_payload(
    caplog: pytest.LogCaptureFixture,
):
    def build() -> Callable[[HooksCenter, EventBus], BaseAgent]:
        llm = ScriptedLLM(
            [
                ScriptedTurn(tool_calls=[_call('c1', 'Echo', text='x' * 5000)]),
                ScriptedTurn(tool_calls=[_call('c2', 'Echo', text='small')]),
                ScriptedTurn('done'),
            ]
        )
        return _factory('Root', llm, {'Echo': _Echo()})

    reference, reference_recorder, _, _ = await _recorded(build())
    produced = await _stored_count(cast(_Counting, reference), reference_recorder)
    store = _Capped(4000)
    caplog.clear()
    _, recorder, _, context = await _recorded(build(), store=store)

    assert context.stop_reason is StopReason.COMPLETED
    [report] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    args = cast(tuple[object, ...], report.args)
    assert report.levelno == logging.ERROR
    assert (args[0], args[1], args[2]) == (recorder.trace_id, '_Capped', store.refused[0])
    assert args[4] == '换一个单项上限更大的 store，或让工具把大的输出写成文件'
    assert recorder.dropped == len(store.refused) >= 1
    assert await _stored_count(store, recorder) == produced - len(store.refused)
    header = await store.get(recorder.trace_id)
    assert header is not None and header.status is None
    assert recorder.complete is False


async def test_complete_is_true_only_once_this_recorder_wrote_the_outcome():
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    before = recorder.complete

    await recorder.finish(SpanStatus.ERROR)

    assert (before, recorder.complete) == (False, True)


class _Tampered(_Counting):
    async def append(
        self,
        trace_id: str,
        spans: Sequence[Span] = (),
        edges: Sequence[CausalityEdge] = (),
    ) -> None:
        raise ContractViolation(
            expected='trace 目录没被改过',
            received='spans.jsonl 换成了 FIFO',
            next_action='删掉被改过的那个 trace 目录',
        )


async def test_a_store_error_that_names_its_own_next_step_is_reported_with_that_step(
    caplog: pytest.LogCaptureFixture,
):
    build, options = _delegating()

    _, recorder, _, context = await _recorded(build, store=_Tampered(), **options)

    [report] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    args = cast(tuple[object, ...], report.args)
    assert context.stop_reason is StopReason.COMPLETED
    assert (args[3], args[5]) == ('ContractViolation', '删掉被改过的那个 trace 目录')
    assert '缺陷' not in report.getMessage()
    assert recorder.dropped > 0


class _Misreporting(_Counting):
    def __init__(self, wrong: str) -> None:
        super().__init__()
        self.wrong = wrong

    async def put_payload(self, trace_id: str, payload: RedactedText) -> PayloadRef:
        ref = await super().put_payload(trace_id, payload)
        if self.wrong == 'digest':
            return PayloadRef(trace_id, '0' * 64)
        return PayloadRef('f' * 32, ref.digest)


@pytest.mark.parametrize('wrong', ['digest', 'trace'])
async def test_a_store_that_misreports_a_payload_reference_is_a_store_failure(
    wrong: str, caplog: pytest.LogCaptureFixture
):
    reference, reference_recorder, _, _ = await _recorded(
        _factory('Root', ScriptedLLM([ScriptedTurn('ok')]))
    )
    produced = await _stored_count(cast(_Counting, reference), reference_recorder)
    caplog.clear()

    llm = ScriptedLLM([ScriptedTurn('ok')])
    _, recorder, _, context = await _recorded(_factory('Root', llm), store=_Misreporting(wrong))

    assert context.stop_reason is StopReason.COMPLETED
    [report] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    error = cast(tuple[object, ...], report.args)[4]
    assert isinstance(error, ContractViolation) and 'put_payload' in error.next_action
    assert cast(tuple[object, ...], report.args)[5] == error.next_action
    assert '修这个 store' in error.next_action
    assert recorder.dropped == produced


class _RefusesBegin(InMemoryTraceStore):
    async def begin(self, header: Any) -> None:
        raise PermissionError('read-only')


async def test_a_refused_begin_reaches_the_host_and_later_records_are_only_counted(
    caplog: pytest.LogCaptureFixture,
):
    recorder = TraceRecorder(_RefusesBegin(), RuleRedactor())
    with pytest.raises(PermissionError):
        await recorder.begin()
    runtime = Runtime(tracer=recorder)
    try:
        agent_id = await runtime.spawn(
            _factory('Root', ScriptedLLM([ScriptedTurn('ok')])), query='go'
        )
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()
    await recorder.finish()

    assert context.stop_reason is StopReason.COMPLETED
    assert recorder.dropped == 3 + 1
    assert [r for r in caplog.records if r.name.startswith('syndica.kernel')] == []


# --- redaction -------------------------------------------------------------------------------


def _fake_secrets(seed: int) -> dict[str, str]:
    """每个通道一个不同的假凭证，各命中一族规则；全部在运行时拼出来。"""
    rng = random.Random(seed)

    def pick(alphabet: str, count: int) -> str:
        return ''.join(rng.choice(alphabet) for _ in range(count))

    alnum = string.ascii_letters + string.digits
    key_lines = [pick(string.ascii_letters + string.digits + '+/', 64) for _ in range(3)]
    return {
        'prompt': 'sk-' + pick(alnum, 45) + 'Q7x',
        'argv': pick(alnum, 21) + 'k8P',
        'tool_args': pick(alnum, 21) + 'r5T',
        'tool_output': pick(alnum, 21) + 'm3W',
        'meta': '\n'.join(key_lines),
        'reply': 'sk-or-v1-' + pick('0123456789abcdef', 64),
        'extra': 'AIza' + pick(alnum, 35),
        'provider_error': pick(alnum, 29) + 'b2N',
        'unknown_tool': 'AKIA' + pick(string.ascii_uppercase + string.digits, 16),
        'call_id': 'xoxb-' + pick(string.digits, 10) + '-' + pick(alnum, 24),
        'env': pick(alnum, 25) + 'h4J',
        'answer': 'ghp_' + pick(alnum, 36),
    }


_PLACEHOLDERS = {
    'prompt': 'secret.openai_key',
    'argv': 'secret.named_assignment',
    'tool_args': 'secret.url_userinfo',
    'tool_output': 'secret.named_assignment',
    'meta': 'secret.private_key_block',
    'reply': 'secret.openrouter_key',
    'extra': 'secret.google_api_key',
    'provider_error': 'secret.authorization_header',
    'unknown_tool': 'secret.aws_access_key',
    'call_id': 'secret.slack_token',
    'env': 'secret.env_value',
    'answer': 'secret.github_token',
}


class _Leaky(Tool):
    __tool_name__ = 'Leaky'
    __tool_command_args__ = ()

    def __init__(self, secrets: Mapping[str, str]) -> None:
        self._secrets = secrets

    async def execute(self, url: str) -> ToolResult:
        """取回一个地址。
        url: 地址
        """
        begin, end = '-----BEGIN RSA PRIVATE KEY-----', '-----END RSA PRIVATE KEY-----'
        pem = f'{begin}\n{self._secrets["meta"]}\n{end}'
        return ToolResult(
            output=f'connected with password={self._secrets["tool_output"]}',
            meta={'key': pem},
        )


class _Printenv(Tool):
    __tool_name__ = 'Printenv'
    __tool_command_args__ = ()

    def __init__(self, value: str) -> None:
        self._value = value

    async def execute(self) -> ToolResult:
        """打印一个环境变量的值。"""
        return ToolResult(output=f'{self._value}\n')


class _WithExtra:
    """给每个回复带上 `extra`。"""

    def __init__(self, inner: ScriptedLLM, extra: dict[str, object]) -> None:
        self._inner = inner
        self._extra = extra
        self.config = LLMConfig(provider='fake', model='with-extra')

    async def create(self, messages: list[Any], **kwargs: Any) -> ContextBlock:
        reply = await self._inner.create(messages, **kwargs)
        return dataclasses.replace(reply, extra=dict(self._extra))


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, enum.Enum):
        yield from _strings(value.value)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for item in dataclasses.fields(value):
            yield from _strings(getattr(value, item.name))
    elif isinstance(value, Mapping):
        for key, item in cast(Mapping[object, object], value).items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in cast(Sequence[object], value):
            yield from _strings(item)


async def test_every_channel_is_redacted_before_it_reaches_the_store():
    secrets = _fake_secrets(20261002)
    url = f'https://deploy:{secrets["tool_args"]}@git.internal.test/repo.git'
    error = RuntimeError(f'401 from provider: Authorization: Bearer {secrets["provider_error"]}')
    first_turn = ScriptedTurn(
        tool_calls=[
            ToolCall(
                id=f'call-{secrets["call_id"]}', name='Leaky', arguments=json.dumps({'url': url})
            ),
            ToolCall(id='c2', name=f'Fetch_{secrets["unknown_tool"]}', arguments='{}'),
            ToolCall(id='c3', name='Printenv', arguments='{}'),
        ]
    )
    script = ScriptedLLM(
        [
            ScriptedTurn(raises=error),
            first_turn,
            ScriptedTurn(tool_calls=[_call('c4', 'AskUser', question='which token?')]),
            ScriptedTurn(content=f'done; the key was {secrets["reply"]}'),
        ]
    )
    llm = _WithExtra(script, {'note': f'route key {secrets["extra"]}'})
    store = _Counting()
    redactor = RuleRedactor(secrets=[secrets['env']])
    recorder = TraceRecorder(
        store,
        redactor,
        level=TraceLevel.FULL,
        source=TraceSource(argv=('run', '--api-key', secrets['argv'], 'fetch the repo')),
    )
    await recorder.begin()
    runtime = Runtime(
        tracer=recorder,
        modules=[HumanInteractionModule(), RetryModule({}, sleep=_no_sleep)],
        flags={'llm_retry_attempts': 1},
    )
    events = runtime.events.subscribe()
    tools: dict[str, Tool] = {'Leaky': _Leaky(secrets), 'Printenv': _Printenv(secrets['env'])}
    try:
        agent_id = await runtime.spawn(
            _factory('Root', llm, tools), query=f'deploy with {secrets["prompt"]}'
        )
        request = await _take(events, 'user_input_requested')
        await runtime.send(
            agent_id,
            f'use {secrets["answer"]}',
            activate=False,
            sender='human',
            correlation_id=request.payload['request_id'],
        )
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        events.close()
        await runtime.shutdown()
    await recorder.finish()

    assert context.stop_reason is StopReason.COMPLETED
    header = await store.get(recorder.trace_id)
    spans = await _spans(store, recorder)
    stored = [
        *_strings(header),
        *(text for span in spans.values() for text in _strings(span)),
        *(text for edge in await store.edges(recorder.trace_id) for text in _strings(edge)),
        *(text for raw in store.payload_texts for text in _strings(json.loads(raw))),
    ]
    for channel, secret in secrets.items():
        pieces = [secret] + [secret[i : i + 16] for i in range(len(secret) - 15)]
        leaked = [text for text in stored for piece in pieces if piece in text]
        assert leaked == [], channel

    def placeholder(channel: str) -> str:
        return f'[REDACTED:{_PLACEHOLDERS[channel]}]'

    async def payload(span_id: str, key: str) -> Any:
        return await _payload(store, recorder, spans[span_id].attributes[key])

    failed = span_id_of(SpanKind.LLM_CALL, agent_id, 1, 1)
    retried = span_id_of(SpanKind.LLM_CALL, agent_id, 1, 2)
    final = span_id_of(SpanKind.LLM_CALL, agent_id, 3, 1)
    leaky, unknown, printenv = (
        span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, index) for index in range(3)
    )
    request = await payload(span_id_of(SpanKind.LLM_CALL, agent_id, 2, 1), 'llm.request')
    reply = await payload(retried, 'llm.response')
    [wait] = [span for span in spans.values() if span.kind is SpanKind.WAIT]
    assert header is not None and header.source.argv[2] == placeholder('argv')
    assert placeholder('prompt') in request['messages'][1]['content']
    assert placeholder('tool_args') in reply['tool_calls'][0]['arguments']['url']
    assert placeholder('call_id') in reply['tool_calls'][0]['id']
    assert placeholder('call_id') in cast(str, spans[leaky].attributes['tool.call_id'])
    assert placeholder('unknown_tool') in reply['tool_calls'][1]['name']
    assert spans[unknown].attributes['tool.name'] == '<unknown>'
    assert placeholder('extra') in reply['extra']['note']
    assert placeholder('provider_error') in (await payload(failed, 'llm.response'))['message']
    leaky_result = await payload(leaky, 'tool.result')
    assert placeholder('tool_output') in leaky_result['output']
    assert placeholder('meta') in leaky_result['meta']['key']
    assert placeholder('env') in (await payload(printenv, 'tool.result'))['output']
    assert placeholder('answer') in (await payload(wait.span_id, 'wait.answer'))['content']
    assert placeholder('reply') in (await payload(final, 'llm.response'))['content']


async def _no_sleep(seconds: float) -> None:
    return None


class _Relay(Tool):
    __tool_name__ = 'Relay'
    __tool_command_args__ = ()

    def __init__(self, runtime: list[Runtime], child: list[str]) -> None:
        self._runtime = runtime
        self._child = child

    async def execute(self) -> ToolResult:
        """派一个子 Agent，等它干完，用一条消息叫醒它再等一次，最后销毁它。"""
        runtime = self._runtime[0]
        script = ScriptedLLM([ScriptedTurn('first'), ScriptedTurn('second')])
        child = await runtime.spawn(_factory('Child', script), query='work')
        self._child.append(child)
        await runtime.wait(child)
        await runtime.send(child, 'more work')
        await runtime.wait(child)
        await runtime.despawn(child)
        return ToolResult(output='relayed')


async def test_kernel_identifiers_references_and_closed_values_are_kept_verbatim():
    redactor = RuleRedactor(
        rules=[
            RedactionRule('custom.hex', r'[0-9a-f]{32}'),
            RedactionRule('custom.word', r'\b(?:completed|answered|human|ok)\b'),
        ]
    )
    runtime_box: list[Runtime] = []
    child_box: list[str] = []
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('r', 'Relay')]),
            ScriptedTurn(tool_calls=[_call('a', 'AskUser', question='which?')]),
            ScriptedTurn('done'),
        ]
    )

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm)(hooks, events)
        agent.install_runtime_tools({'Relay': _Relay(runtime_box, child_box)})
        return agent

    store = _Counting()
    recorder = TraceRecorder(store, redactor)
    await recorder.begin()
    runtime = Runtime(tracer=recorder, modules=[HumanInteractionModule()])
    runtime_box.append(runtime)
    events = runtime.events.subscribe()
    try:
        parent = await runtime.spawn(build, query='go')
        request = await _take(events, 'user_input_requested')
        await runtime.send(
            parent, 'yes', activate=False, correlation_id=request.payload['request_id']
        )
        context = await asyncio.wait_for(runtime.wait(parent), _TIMEOUT)
    finally:
        events.close()
        await runtime.shutdown()
    await recorder.finish()

    assert context.stop_reason is StopReason.COMPLETED
    [child] = child_box
    spans = await _spans(store, recorder)
    edges = await store.edges(recorder.trace_id)
    relay = spans[span_id_of(SpanKind.TOOL_CALL, parent, 1, 1, 0)]
    [(sent, sent_attributes)] = [
        (e.name, e.attributes) for e in relay.events if e.name == 'message.sent'
    ]
    [(gone, gone_attributes)] = [
        (e.name, e.attributes) for e in relay.events if e.name == 'agent.despawned'
    ]
    message_id = cast(str, sent_attributes['message.id'])
    assert len(message_id) == 32 and sent_attributes['message.to'] == child
    assert gone_attributes == {'agent.id': child}
    assert sorted((edge.kind.value, dict(edge.attributes)) for edge in edges) == [
        ('message', {'message.id': message_id}),
        ('spawn', {'child.agent_id': child}),
    ]
    assert {span.agent_id for span in spans.values()} == {parent, child}
    agent_spans = [span for span in spans.values() if span.kind is SpanKind.AGENT]
    assert [span.attributes['stop_reason'] for span in agent_spans] == ['completed'] * 3
    assert [span.attributes.get('tool.outcome') for span in spans.values()].count('ok') == 2
    [wait] = [span for span in spans.values() if span.kind is SpanKind.WAIT]
    assert (wait.attributes['wait.kind'], wait.attributes['wait.outcome']) == ('human', 'answered')
    references = 0
    for span in spans.values():
        for key in _PAYLOAD_KEYS:
            if key in span.attributes:
                references += 1
                await _payload(store, recorder, span.attributes[key])
        if 'llm.request_digest' in span.attributes:
            assert len(cast(str, span.attributes['llm.request_digest'])) == 64
    assert references == 5 + 2 + 1
    llm_calls = [span for span in spans.values() if span.kind is SpanKind.LLM_CALL]
    assert len(llm_calls) == 5
    for span in llm_calls:
        assert re.fullmatch(r'[0-9a-f]{64}', cast(str, span.attributes['llm.request_digest']))
    assert relay.attributes['tool.call_id'] == 'r' and relay.name == 'Relay'


async def test_span_names_string_lists_events_and_edge_attributes_are_redacted():
    secret = 'ghp_' + next(_secrets(5))[:24] + 'Ab12Cd34Ef56'
    placeholder = '[REDACTED:secret.github_token]'
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()

    listed: dict[str, Any] = {'danger.rule_ids': ['a', secret]}
    recorder.start_span(_TOOL, None, SpanKind.TOOL_CALL, f'tool {secret}', 'agent', listed)
    recorder.add_event(_TOOL, f'note {secret}', {'llm.error': f'boom {secret}', 'message.to': 'b'})
    recorder.add_edge(
        CausalityEdge(EdgeKind.MESSAGE, _TOOL, _LLM, {'message.id': 'm1', 'tool.name': secret})
    )
    recorder.end_span(_TOOL, SpanStatus.OK, {'confirmation.outcome': 'approved'})
    await recorder.finish()

    [span] = await store.spans(recorder.trace_id)
    [edge] = await store.edges(recorder.trace_id)
    [event] = span.events
    assert span.name == f'tool {placeholder}'
    assert span.attributes == {
        'danger.rule_ids': ['a', placeholder],
        'confirmation.outcome': 'approved',
    }
    assert (event.name, event.attributes) == (
        f'note {placeholder}',
        {'llm.error': f'boom {placeholder}', 'message.to': 'b'},
    )
    assert edge.attributes == {'message.id': 'm1', 'tool.name': placeholder}


async def test_free_text_that_looks_like_an_identifier_is_still_redacted():
    redactor = RuleRedactor(rules=[RedactionRule('custom.hex', r'[0-9a-f]{32}')])
    call_id = 'ab' * 16
    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[ToolCall(call_id, 'Echo', '{}')]), ScriptedTurn('ok')]
    )
    store, recorder, agent_id, _ = await _recorded(
        _factory('Root', llm, {'Echo': _Echo()}), redactor=redactor
    )

    spans = await _spans(store, recorder)
    tool = spans[span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0)]
    assert tool.attributes['tool.call_id'] == '[REDACTED:custom.hex]'


# --- what must not stop or bend the recording ------------------------------------------------


async def test_a_lone_surrogate_in_a_request_does_not_stop_the_recording():
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall('c', 'Echo', '{"text": "smile \\ud83d"}')]),
            ScriptedTurn(tool_calls=[_call('d', 'Echo', text='name-\udcff.txt')]),
            ScriptedTurn('ok'),
        ]
    )
    store, recorder, agent_id, context = await _recorded(_factory('Root', llm, {'Echo': _Echo()}))

    assert context.stop_reason is StopReason.COMPLETED
    spans = await _spans(store, recorder)
    for turn in (1, 2, 3):
        attributes = spans[span_id_of(SpanKind.LLM_CALL, agent_id, turn, 1)].attributes
        assert {'llm.request_digest', 'llm.response'} <= set(attributes), turn
    tool = spans[span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0)]
    assert (await _payload(store, recorder, tool.attributes['tool.result']))['output'] == (
        'smile \ud83d'
    )
    header = await store.get(recorder.trace_id)
    assert header is not None and header.status is SpanStatus.OK and recorder.dropped == 0


class _Unprintable(Exception):
    def __str__(self) -> str:
        raise RuntimeError('cannot print this error')


async def test_an_error_that_cannot_be_printed_is_recorded_by_its_type():
    recorder = await _begun()
    _exchange(recorder, [ContextBlock(role='user', content='go')], error=_Unprintable())
    store = cast(InMemoryTraceStore, recorder._store)  # pyright: ignore[reportPrivateUsage]
    await recorder.finish()

    spans = await _spans(store, recorder)
    assert await _payload(store, recorder, spans[_LLM].attributes['llm.response']) == {
        'kind': 'error',
        'type': f'{__name__}._Unprintable',
        'message': f'<{__name__}._Unprintable>',
    }
    assert recorder.dropped == 0


async def test_an_integer_too_long_for_decimal_is_recorded_in_hex():
    recorder = await _begun()
    huge = 1 << 20000
    recorder.start_span(_TOOL, None, SpanKind.TOOL_CALL, 'tool', 'agent', {})
    recorder.tool_result(_TOOL, ToolResult(output='x', meta={'huge': huge, 'big': 2**62}), None)
    recorder.end_span(_TOOL, SpanStatus.OK, {})
    store = cast(InMemoryTraceStore, recorder._store)  # pyright: ignore[reportPrivateUsage]
    await recorder.finish()

    spans = await _spans(store, recorder)
    meta = (await _payload(store, recorder, spans[_TOOL].attributes['tool.result']))['meta']
    assert meta == {'huge': hex(huge), 'big': 2**62}


@dataclasses.dataclass(eq=False)
class _Link:
    name: str
    previous: object = None
    next: object = None


class _Opaque:
    pass


async def test_cyclic_shared_and_opaque_values_are_recorded_with_stable_markers(
    monkeypatch: pytest.MonkeyPatch,
):
    import syndica.kernel.trace_recorder as module

    monkeypatch.setattr(module, '_MAX_NODES', 500)
    cyclic: list[object] = []
    cyclic.append(cyclic)
    shared: list[object] = ['leaf']
    for _ in range(16):
        shared = [shared, shared]
    leaf = ['x']
    first, second, third = _Link('a'), _Link('b'), _Link('c')
    first.next, second.previous, second.next, third.previous = second, first, third, second
    extra: dict[str, Any] = {
        'cyclic': cyclic,
        'pair': [leaf, leaf],
        'chain': first,
        'opaque': _Opaque(),
        'shared': shared,
    }
    recorder = await _begun()
    _exchange(
        recorder,
        [ContextBlock(role='user', content='go')],
        reply=ContextBlock(role='assistant', content='ok', extra=extra),
    )
    store = cast(InMemoryTraceStore, recorder._store)  # pyright: ignore[reportPrivateUsage]
    await recorder.finish()

    spans = await _spans(store, recorder)
    recorded = (await _payload(store, recorder, spans[_LLM].attributes['llm.response']))['extra']
    assert recorded['cyclic'] == ['<cycle>']
    assert recorded['pair'] == [['x'], ['x']]
    assert recorded['chain'] == {
        'name': 'a',
        'previous': None,
        'next': {
            'name': 'b',
            'previous': '<cycle>',
            'next': {'name': 'c', 'previous': '<cycle>', 'next': None},
        },
    }
    assert recorded['opaque'] == f'<{__name__}._Opaque>'
    assert '<truncated>' in json.dumps(recorded['shared'])


async def test_a_record_that_cannot_be_encoded_is_counted_reported_once_and_leaves_no_outcome(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    import syndica.kernel.trace_recorder as module

    original = module.encode_redacted

    def refusing_results(value: object, redactor: Any) -> RedactedText:
        if isinstance(value, dict) and cast(dict[str, object], value).get('kind') == 'result':
            raise ContractViolation(expected='a result', received='refused', next_action='none')
        return original(value, redactor)

    monkeypatch.setattr(module, 'encode_redacted', refusing_results)
    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('c', 'Echo'), _call('d', 'Echo')]), ScriptedTurn('ok')]
    )
    store, recorder, _, context = await _recorded(_factory('Root', llm, {'Echo': _Echo()}))

    assert context.stop_reason is StopReason.COMPLETED
    [report] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    args = cast(tuple[object, ...], report.args)
    assert report.levelno == logging.ERROR
    assert (args[0], args[2]) == (recorder.trace_id, 'ContractViolation')
    assert 'tool.result' in str(args[1])
    assert recorder.dropped == 2
    header = await store.get(recorder.trace_id)
    assert header is not None and header.status is None
    spans = list((await _spans(store, recorder)).values())
    tools = [span for span in spans if span.kind is SpanKind.TOOL_CALL]
    assert len(tools) == 2 and all('tool.result' not in span.attributes for span in tools)
    assert all('llm.response' in s.attributes for s in spans if s.kind is SpanKind.LLM_CALL)


class _Exploding(RuleRedactor):
    def redact(self, text: str) -> str:
        if 'explode' in text:
            raise RuntimeError('the redactor broke')
        return super().redact(text)


async def test_a_span_that_cannot_be_built_is_lost_alone_and_the_trace_has_no_outcome():
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, _Exploding())
    await recorder.begin()

    recorder.start_span(_TOOL, None, SpanKind.TOOL_CALL, 'explode', 'agent', {})
    recorder.end_span(_TOOL, SpanStatus.OK, {})
    recorder.start_span(_LLM, None, SpanKind.LLM_CALL, 'llm', 'agent', {'llm.attempt': 1})
    recorder.add_event(_LLM, 'explode', {})
    recorder.add_event(_LLM, 'kept', {})
    recorder.end_span(_LLM, SpanStatus.OK, {})
    await recorder.finish()

    [span] = await store.spans(recorder.trace_id)
    assert (span.span_id, [event.name for event in span.events]) == (_LLM, ['kept'])
    header = await store.get(recorder.trace_id)
    assert header is not None and header.status is None and recorder.dropped == 2


async def test_a_span_id_opened_twice_loses_the_first_and_says_why(
    caplog: pytest.LogCaptureFixture,
):
    recorder = await _begun()
    recorder.start_span(_TOOL, None, SpanKind.TOOL_CALL, 'first', 'agent', {})
    recorder.start_span(_TOOL, None, SpanKind.TOOL_CALL, 'second', 'agent', {})
    recorder.end_span(_TOOL, SpanStatus.OK, {})
    store = cast(InMemoryTraceStore, recorder._store)  # pyright: ignore[reportPrivateUsage]
    await recorder.finish()

    [span] = await store.spans(recorder.trace_id)
    [report] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    assert span.name == 'second' and recorder.dropped == 1
    assert 'Runtime' in str(cast(tuple[object, ...], report.args)[3])


async def test_replay_channels_for_a_span_that_is_not_open_record_nothing():
    recorder = await _begun()
    stray = '0' * 16
    recorder.llm_exchange(stray, {'messages': []}, ContextBlock(role='assistant'), None)
    recorder.tool_result(stray, ToolResult(output='x'), None)
    recorder.wait_answer(stray, Message(sender='h', receiver='agent', content='x'))
    store = cast(_Counting, recorder._store)  # pyright: ignore[reportPrivateUsage]
    await recorder.finish()

    assert store.payload_texts == [] and recorder.dropped == 0


async def test_every_channel_uses_the_hosts_whole_redactor():
    env = next(_secrets(23))
    redactor = RuleRedactor(secrets=[env], rules=[RedactionRule('custom.ticket', r'TCK-[0-9]{6}')])
    text = f'{env} and TCK-123456'
    redacted = '[REDACTED:secret.env_value] and [REDACTED:custom.ticket]'
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, redactor, source=TraceSource(argv=('run', text)))
    await recorder.begin()

    recorder.start_span(_TOOL, None, SpanKind.TOOL_CALL, text, 'agent', {'tool.name': text})
    recorder.tool_result(_TOOL, ToolResult(output=text), None)
    recorder.end_span(_TOOL, SpanStatus.OK, {})
    _exchange(recorder, [ContextBlock(role='user', content=text)])
    await recorder.finish()

    header = await store.get(recorder.trace_id)
    spans = await _spans(store, recorder)
    tool = spans[_TOOL]
    assert header is not None and header.source.argv == ('run', redacted)
    assert (tool.name, tool.attributes['tool.name']) == (redacted, redacted)
    assert (await _payload(store, recorder, tool.attributes['tool.result']))['output'] == redacted
    request = [ContextBlock(role='user', content=text)]
    assert spans[_LLM].attributes['llm.request_digest'] == _whole(_fingerprinted(request, redactor))
    assert _fingerprinted(request, redactor)[3] == _unit_digest(_block_unit('user', text), redactor)
    assert _fingerprinted(request, redactor) != _fingerprinted(request)


async def test_end_attributes_land_on_top_of_start_attributes_and_tuples_are_redacted_too():
    secret = 'ghp_' + next(_secrets(29))[:24] + 'Ab12Cd34Ef56'
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()

    recorder.start_span(
        _TOOL, None, SpanKind.TOOL_CALL, 't', 'agent', {'tool.outcome': 'ok', 'tool.index': 0}
    )
    recorder.end_span(
        _TOOL,
        SpanStatus.OK,
        {'tool.outcome': 'error', 'danger.rule_ids': cast(Any, ('b', secret))},
    )
    await recorder.finish()

    [span] = await store.spans(recorder.trace_id)
    assert span.attributes == {
        'tool.outcome': 'error',
        'tool.index': 0,
        'danger.rule_ids': ['b', '[REDACTED:secret.github_token]'],
    }


_VERBATIM_TEXT = {'agent.id', 'child.agent_id', 'message.id', 'message.to', 'llm.request_digest'}


def _free_text_keys() -> list[str]:
    return sorted(
        key
        for key, kind in ATTRIBUTE_KINDS.items()
        if kind in ('str', 'str[]') and key not in ATTRIBUTE_DOMAINS and key not in _VERBATIM_TEXT
    )


def test_the_free_text_attributes_are_exactly_these():
    assert _free_text_keys() == [
        'agent.role',
        'danger.rule_ids',
        'junction.name',
        'llm.error',
        'llm.model',
        'llm.platform',
        'module.flags',
        'module.name',
        'module.plugins',
        'module.tools',
        'module.version',
        'tool.call_id',
        'tool.name',
        'wait.target',
    ]


@pytest.mark.parametrize('key', _free_text_keys())
async def test_every_free_text_attribute_is_redacted(key: str):
    secret = 'ghp_' + next(_secrets(31))[:24] + 'Ab12Cd34Ef56'
    value: Any = [secret] if ATTRIBUTE_KINDS[key] == 'str[]' else secret
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()

    recorder.start_span(_TOOL, None, SpanKind.TOOL_CALL, 't', 'agent', {key: value})
    recorder.end_span(_TOOL, SpanStatus.OK, {})
    await recorder.finish()

    [span] = await store.spans(recorder.trace_id)
    placeholder = '[REDACTED:secret.github_token]'
    assert span.attributes[key] == ([placeholder] if isinstance(value, list) else placeholder)


async def test_an_answer_is_recorded_with_its_own_sender_trigger_and_trust():
    recorder = await _begun()
    wait = span_id_of(SpanKind.WAIT, 'host', 'wait', 0)
    recorder.start_span(wait, None, SpanKind.WAIT, 'wait human', 'agent', {'wait.kind': 'human'})
    recorder.wait_answer(
        wait,
        Message(
            sender='operator',
            receiver='agent',
            content='yes',
            trigger=Trigger.DEFERRED,
            trust=TrustLevel.UNTRUSTED,
        ),
    )
    recorder.end_span(wait, SpanStatus.OK, {})
    store = cast(InMemoryTraceStore, recorder._store)  # pyright: ignore[reportPrivateUsage]
    await recorder.finish()

    spans = await _spans(store, recorder)
    assert await _payload(store, recorder, spans[wait].attributes['wait.answer']) == {
        'kind': 'answer',
        'sender': 'operator',
        'content': 'yes',
        'trigger': 'deferred',
        'trust': 'untrusted',
    }


async def test_a_plain_result_is_recorded_as_trusted_and_a_reply_that_is_not_a_block_by_value():
    recorder = await _begun()
    recorder.start_span(_TOOL, None, SpanKind.TOOL_CALL, 'tool', 'agent', {})
    recorder.tool_result(_TOOL, ToolResult(output='plain'), None)
    recorder.end_span(_TOOL, SpanStatus.OK, {})
    _exchange(
        recorder,
        [ContextBlock(role='user', content='go')],
        reply=cast(ContextBlock, {'odd': ['reply']}),
    )
    store = cast(InMemoryTraceStore, recorder._store)  # pyright: ignore[reportPrivateUsage]
    await recorder.finish()

    spans = await _spans(store, recorder)
    assert await _payload(store, recorder, spans[_TOOL].attributes['tool.result']) == {
        'kind': 'result',
        'output': 'plain',
        'meta': {},
        'trust': 'trusted',
        'artifacts': [],
    }
    assert await _payload(store, recorder, spans[_LLM].attributes['llm.response']) == {
        'kind': 'reply',
        'value': {'odd': ['reply']},
    }
