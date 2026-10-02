"""TraceRecorder 在各条路径上（M3 阶段二审计）：Fork、拓扑槽、邮箱确认、kill。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, cast

from syndica.kernel import (
    AgentOptions,
    BaseAgent,
    ContextBlock,
    Disposition,
    EventBus,
    HooksCenter,
    InMemoryTraceStore,
    MailboxConfirmationHandler,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    SpanKind,
    SpanStatus,
    StopReason,
    Tool,
    ToolCall,
    ToolResult,
    TraceRecorder,
)
from syndica.kernel.llm import LLMConfig
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.trace import EdgeKind, PayloadRef, Span
from syndica.kernel.trace_recorder import RequestFingerprints
from syndica.kernel.tracing import span_id_of

_TIMEOUT = 5.0


def _call(call_id: str, name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def _factory(name: str, llm: object, tools: dict[str, Tool] | None = None, **options: Any) -> Any:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name=name, llm=cast(Any, llm), tools=dict(tools or {}), **options),
            hooks=hooks,
            events=events,
        )

    return build


async def _recording(**runtime_options: Any) -> tuple[InMemoryTraceStore, TraceRecorder, Runtime]:
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    return store, recorder, Runtime(tracer=recorder, **runtime_options)


async def _spans(store: InMemoryTraceStore, recorder: TraceRecorder) -> dict[str, Span]:
    return {span.span_id: span for span in await store.spans(recorder.trace_id)}


async def _payload(store: InMemoryTraceStore, recorder: TraceRecorder, digest: object) -> Any:
    text = await store.payload(PayloadRef(recorder.trace_id, cast(str, digest)))
    assert text is not None
    return json.loads(text)


async def test_a_fork_is_spawned_from_its_call_and_its_inherited_history_has_the_same_units():
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('f', 'Fork', query='onward')]),
            ScriptedTurn(content='forked answer'),
            ScriptedTurn(content='parent done'),
        ]
    )
    store, recorder, runtime = await _recording(orchestration=True)
    try:
        parent = await runtime.spawn(_factory('Root', llm), query='go')
        context = await asyncio.wait_for(runtime.wait(parent), _TIMEOUT)
    finally:
        await runtime.shutdown()
    await recorder.finish()

    assert context.stop_reason is StopReason.COMPLETED
    spans = await _spans(store, recorder)
    fork_call = span_id_of(SpanKind.TOOL_CALL, parent, 1, 1, 0)
    [spawn] = [edge for edge in await store.edges(recorder.trace_id) if edge.kind is EdgeKind.SPAWN]
    child = cast(str, spawn.attributes['child.agent_id'])
    assert spawn.source_span_id == fork_call
    assert spawn.target_span_id == span_id_of(SpanKind.AGENT, child, 1)
    assert spans[spawn.target_span_id].attributes['stop_reason'] == 'completed'
    fingerprints = RequestFingerprints(RuleRedactor())
    parent_units, child_units = (
        fingerprints.of({'messages': messages, **kwargs})
        for messages, kwargs in zip(llm.calls[:2], llm.call_kwargs[:2], strict=True)
    )
    assert child_units[:4] == parent_units[:4]
    assert len(child_units) > len(parent_units)
    for agent, units in ((parent, parent_units), (child, child_units)):
        span = spans[span_id_of(SpanKind.LLM_CALL, agent, 1, 1)]
        assert span.attributes['llm.request_digest'] == (
            hashlib.sha256(''.join(units).encode()).hexdigest()
        )


async def test_an_instance_taken_from_a_topology_slot_is_spawned_from_the_call_that_took_it():
    from syndica.kernel.catalog import AgentCatalog
    from syndica.kernel.topology import AgentSlot, Topology

    catalog = AgentCatalog()
    catalog.load_mapping({'name': 'Worker', 'description': 'Worker'})
    topology = Topology(
        slots=(AgentSlot(name='hand', agent='Worker', cardinality='on-demand', boot='manual'),)
    )
    store, recorder, runtime = await _recording()
    taken: list[str] = []

    class _Occupier(Tool):
        __tool_name__ = 'Occupier'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """从槽里要一个实例。"""
            taken.append(await runtime.occupy('hand'))
            return ToolResult(output=taken[-1])

    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('o', 'Occupier')]), ScriptedTurn('ok')])
    try:
        await runtime.assemble(
            topology, catalog, tools={}, models={'default': ScriptedLLM([ScriptedTurn('ok')])}
        )
        root = await runtime.spawn(_factory('Root', llm, {'Occupier': _Occupier()}), query='go')
        await asyncio.wait_for(runtime.wait(root), _TIMEOUT)
    finally:
        await runtime.shutdown()
    await recorder.finish()

    [spawn] = [edge for edge in await store.edges(recorder.trace_id) if edge.kind is EdgeKind.SPAWN]
    assert spawn.source_span_id == span_id_of(SpanKind.TOOL_CALL, root, 1, 1, 0)
    assert spawn.attributes == {'child.agent_id': taken[0]}
    assert spawn.target_span_id == span_id_of(SpanKind.AGENT, taken[0], 1)
    assert spawn.target_span_id not in await _spans(store, recorder)


class _Probe(Tool):
    __tool_name__ = 'Probe'
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        """被确认之后才跑。"""
        return ToolResult(output='probed')


class _ConfirmEverything:
    def decide(self, request: Any) -> Disposition:
        return Disposition.CONFIRM


async def _take(events: Any, kind: str) -> Any:
    while True:
        event = await asyncio.wait_for(anext(events), timeout=_TIMEOUT)
        if event.kind == kind:
            return event


async def test_a_confirmation_answered_through_the_mailbox_is_recorded_on_its_wait():
    store, recorder, runtime = await _recording(
        confirmation_handler=MailboxConfirmationHandler(timeout=30)
    )
    events = runtime.events.subscribe()
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('c1', 'Probe')]), ScriptedTurn('ok')])
    build = _factory('Root', llm, {'Probe': _Probe()}, danger_policy=_ConfirmEverything())
    try:
        agent_id = await runtime.spawn(build, query='go')
        requested = await _take(events, 'tool_confirmation_requested')
        await runtime.send(
            agent_id,
            'approve',
            activate=False,
            sender='human',
            correlation_id=cast(str, requested.payload['request_id']),
        )
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        events.close()
        await runtime.shutdown()
    await recorder.finish()

    spans = await _spans(store, recorder)
    by_kind = {span.kind: span for span in spans.values()}
    tool = spans[span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0)]
    assert (tool.attributes['tool.outcome'], tool.attributes['danger.disposition']) == (
        'ok',
        'confirm',
    )
    assert by_kind[SpanKind.CONFIRMATION].attributes['confirmation.outcome'] == 'approved'
    wait = by_kind[SpanKind.WAIT]
    assert (wait.attributes['wait.kind'], wait.attributes['wait.outcome']) == ('human', 'answered')
    answer = await _payload(store, recorder, wait.attributes['wait.answer'])
    assert (answer['sender'], answer['content']) == ('human', 'approve')
    assert (await _payload(store, recorder, tool.attributes['tool.result']))['output'] == 'probed'


class _Hanging:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.config = LLMConfig(provider='fake', model='hangs')

    async def create(self, messages: list[Any], **kwargs: Any) -> ContextBlock:
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError('unreachable')


async def test_a_killed_run_is_recorded_with_its_cut_spans_cancelled_and_no_reply():
    llm = _Hanging()
    store, recorder, runtime = await _recording()
    try:
        agent_id = await runtime.spawn(_factory('Root', llm), query='go')
        await asyncio.wait_for(llm.entered.wait(), _TIMEOUT)
        await asyncio.wait_for(runtime.kill(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()
    await recorder.finish()

    spans = await _spans(store, recorder)
    statuses = {span.kind: span.status for span in spans.values()}
    assert statuses == {
        SpanKind.AGENT: SpanStatus.CANCELLED,
        SpanKind.TURN: SpanStatus.CANCELLED,
        SpanKind.LLM_CALL: SpanStatus.CANCELLED,
    }
    [llm_call] = [span for span in spans.values() if span.kind is SpanKind.LLM_CALL]
    assert 'llm.response' not in llm_call.attributes
    header = await store.get(recorder.trace_id)
    assert header is not None and header.status is SpanStatus.OK and recorder.dropped == 0
