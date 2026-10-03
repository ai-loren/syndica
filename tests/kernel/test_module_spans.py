"""trace 里的 Module（M4 D14，T12）：每个 Agent spawn 提交之后，每个已装 Module 一个开即关的根
span；扩展切点被触发时记在当前 span 上；Module 的名字与工具名照常过脱敏。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from syndica.kernel import (
    AgentInfo,
    AgentOptions,
    BaseAgent,
    EventBus,
    ExtensionJunction,
    FlagDeclaration,
    HooksCenter,
    InMemoryAgentRegistry,
    InMemoryTraceStore,
    JunctionKind,
    NullTracer,
    Runtime,
    RuntimeModule,
    ScriptedLLM,
    ScriptedTurn,
    Tool,
    ToolCall,
    ToolResult,
    TraceLevel,
    TraceRecorder,
)
from syndica.kernel.redaction import RedactionRule, RuleRedactor
from syndica.kernel.trace import AttributeValue, SpanKind, SpanStatus, verbatim_attribute


class _Spans(NullTracer):
    def __init__(self) -> None:
        self.order: list[tuple[str, str]] = []
        self.started: dict[
            str, tuple[str | None, SpanKind, str, str, dict[str, AttributeValue]]
        ] = {}
        self.events: list[tuple[str, str, dict[str, AttributeValue]]] = []

    def start_span(
        self,
        span_id: str,
        parent_span_id: str | None,
        kind: SpanKind,
        name: str,
        agent_id: str,
        attributes: dict[str, AttributeValue],
    ) -> None:
        self.order.append(('start', span_id))
        self.started[span_id] = (parent_span_id, kind, name, agent_id, dict(attributes))

    def end_span(
        self, span_id: str, status: SpanStatus, attributes: dict[str, AttributeValue]
    ) -> None:
        self.order.append(('end', span_id))

    def add_event(self, span_id: str, name: str, attributes: dict[str, AttributeValue]) -> None:
        self.events.append((span_id, name, dict(attributes)))

    def module_spans(self) -> list[tuple[str, tuple[Any, ...]]]:
        return [
            (span_id, value)
            for span_id, value in self.started.items()
            if value[1] is SpanKind.MODULE
        ]


class _Named(Tool):
    __tool_command_args__ = ()

    def __init__(self, name: str) -> None:
        self._forced = name

    @property
    def name(self) -> str:
        return self._forced

    async def execute(self) -> ToolResult:
        return ToolResult(output=self._forced)


class _Plugin:
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def register(self, hooks: HooksCenter) -> None:
        del hooks


def _audit(*, failing: bool = False) -> RuntimeModule:
    def tools(*_: object) -> Mapping[str, Tool]:
        if failing:
            raise RuntimeError('cannot build the tools')
        return {'Zeta': _Named('Zeta'), 'Alpha': _Named('Alpha')}

    return RuntimeModule(
        'audit',
        version='1.2.0',
        tool_factories=(tools,),
        agent_plugins=(lambda *_: _Plugin('audit-hooks'),),
        flags=(FlagDeclaration(key='audit_level', default=1, description='审计的详细程度'),),
    )


_QUIET = RuntimeModule(
    'quiet', flags=(FlagDeclaration(key='quiet_mode', default=False, description='静默'),)
)


def _factory(llm: ScriptedLLM | None = None) -> Callable[[HooksCenter, EventBus], BaseAgent]:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name='Worker', llm=llm or ScriptedLLM([ScriptedTurn(content='done')])),
            hooks=hooks,
            events=events,
        )

    return build


async def test_each_installed_module_gets_one_zero_length_root_span_per_spawned_agent():
    spans = _Spans()
    runtime = Runtime(modules=[_audit(), _QUIET], tracer=spans)
    try:
        agent_id = await runtime.spawn(_factory())
    finally:
        await runtime.shutdown()

    recorded = spans.module_spans()
    assert [(value[2], value[3]) for _, value in recorded] == [
        ('audit', agent_id),
        ('quiet', agent_id),
    ]
    assert all(value[0] is None for _, value in recorded)
    assert recorded[0][1][4] == {
        'module.name': 'audit',
        'module.version': '1.2.0',
        'module.tools': ['Zeta', 'Alpha'],
        'module.plugins': ['audit-hooks'],
        'module.flags': ['audit_level'],
    }
    assert recorded[1][1][4]['module.tools'] == []
    for span_id, _ in recorded:
        position = spans.order.index(('start', span_id))
        assert spans.order[position + 1] == ('end', span_id)


async def test_a_refused_spawn_leaves_no_module_span():
    spans = _Spans()
    runtime = Runtime(modules=[_QUIET, _audit(failing=True)], tracer=spans)
    try:
        with pytest.raises(RuntimeError):
            await runtime.spawn(_factory())
    finally:
        await runtime.shutdown()
    assert spans.module_spans() == []


_RECORDED = ExtensionJunction('audit:recorded', JunctionKind.OBSERVABLE)


class _Record(Tool):
    __tool_command_args__ = ()

    def __init__(self, agent: BaseAgent) -> None:
        self._agent = agent

    async def execute(self) -> ToolResult:
        await self._agent.trigger_extension(_RECORDED)
        return ToolResult(output='recorded')


async def test_a_triggered_extension_junction_is_an_event_on_the_current_span_only():
    spans = _Spans()
    module = RuntimeModule(
        'audit',
        junctions=[_RECORDED],
        tool_factories=(lambda runtime, agent, flags: {'Record': _Record(agent)},),
    )
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='r1', name='Record', arguments='{}')]),
            ScriptedTurn(content='done'),
        ]
    )
    runtime = Runtime(modules=[module], tracer=spans)
    try:
        agent_id = await runtime.spawn(_factory(llm), query='go')
        await runtime.wait(agent_id)
        await runtime.hooks.trigger(_RECORDED)
    finally:
        await runtime.shutdown()

    [(span_id, name, attributes)] = [
        event for event in spans.events if event[1] == 'junction.triggered'
    ]
    assert spans.started[span_id][1] is SpanKind.TOOL_CALL
    assert attributes == {'junction.name': 'audit:recorded'}


def test_module_attributes_are_redacted_like_any_other_third_party_text():
    keys = ('module.name', 'module.version', 'module.tools', 'module.plugins', 'module.flags')
    assert [verbatim_attribute(key) for key in (*keys, 'junction.name')] == [False] * 6


async def test_a_user_rule_that_knows_a_tool_name_redacts_it_in_the_module_span():
    store = InMemoryTraceStore()
    redactor = RuleRedactor(rules=[RedactionRule('custom.codename', r'Zebra[0-9]+')])
    recorder = TraceRecorder(store, redactor, level=TraceLevel.STRUCTURE)
    await recorder.begin()
    module = RuntimeModule('audit', tool_factories=(lambda *_: {'Zebra7': _Named('Zebra7')},))
    runtime = Runtime(modules=[module], tracer=recorder)
    try:
        await runtime.spawn(_factory())
    finally:
        await runtime.shutdown()
    await recorder.finish()

    [span] = [span for span in await store.spans(recorder.trace_id) if span.kind is SpanKind.MODULE]
    assert 'Zebra7' not in str(span.attributes['module.tools'])


class _RefusingRegistry(InMemoryAgentRegistry):
    async def register(self, info: AgentInfo) -> None:
        del info
        raise RuntimeError('registry offline')


async def test_a_spawn_the_registry_refuses_after_assembly_leaves_no_module_span():
    spans = _Spans()
    runtime = Runtime(modules=[_audit(), _QUIET], tracer=spans, registry=_RefusingRegistry())
    try:
        with pytest.raises(RuntimeError):
            await runtime.spawn(_factory())
    finally:
        await runtime.shutdown()
    assert spans.module_spans() == []


class _Spawner(Tool):
    __tool_command_args__ = ()

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime
        self.children: list[str] = []

    async def execute(self) -> ToolResult:
        self.children.append(await self._runtime.spawn(_factory()))
        return ToolResult(output='spawned')


async def test_a_child_spawned_inside_a_tool_call_still_gets_root_module_spans():
    spans = _Spans()
    spawners: list[_Spawner] = []

    def tools(runtime: Runtime, agent: object, flags: object) -> Mapping[str, Tool]:
        del agent, flags
        spawners.append(_Spawner(runtime))
        return {'Spawner': spawners[-1]}

    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='s1', name='Spawner', arguments='{}')]),
            ScriptedTurn(content='done'),
        ]
    )
    runtime = Runtime(modules=[RuntimeModule('spawning', tool_factories=(tools,))], tracer=spans)
    try:
        parent = await runtime.spawn(_factory(llm), query='go')
        await runtime.wait(parent)
    finally:
        await runtime.shutdown()

    [child] = spawners[0].children
    child_spans = [value for _, value in spans.module_spans() if value[3] == child]
    assert [value[0] for value in child_spans] == [None]


async def test_a_module_span_lists_its_tools_in_assembly_order_not_sorted():
    spans = _Spans()
    module = RuntimeModule(
        'audit',
        tool_factories=(lambda *_: {name: _Named(name) for name in ('Mike', 'Alpha', 'Zulu')},),
    )
    runtime = Runtime(modules=[module], tracer=spans)
    try:
        await runtime.spawn(_factory())
    finally:
        await runtime.shutdown()

    [(_, value)] = spans.module_spans()
    assert value[4]['module.tools'] == ['Mike', 'Alpha', 'Zulu']


async def test_a_module_span_lists_plugins_and_flags_in_declaration_order_not_sorted():
    spans = _Spans()

    def plugin(name: str) -> Callable[..., _Plugin]:
        return lambda *_: _Plugin(name)

    module = RuntimeModule(
        'audit',
        agent_plugins=tuple(plugin(name) for name in ('mike-hooks', 'alpha-hooks', 'zulu-hooks')),
        flags=tuple(
            FlagDeclaration(key=key, default=0, description='d')
            for key in ('audit_mike', 'audit_alpha', 'audit_zulu')
        ),
    )
    runtime = Runtime(modules=[module], tracer=spans)
    try:
        await runtime.spawn(_factory())
    finally:
        await runtime.shutdown()

    [(_, value)] = spans.module_spans()
    assert value[4]['module.plugins'] == ['mike-hooks', 'alpha-hooks', 'zulu-hooks']
    assert value[4]['module.flags'] == ['audit_mike', 'audit_alpha', 'audit_zulu']
