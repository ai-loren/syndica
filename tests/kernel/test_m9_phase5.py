"""M9 阶段五：墙钟打断在飞的模型调用、预算跨会话、裁决退出码、带超时的工具收紧。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, cast

import pytest

from syndica.backends.llm_router import ModelRouter
from syndica.cli.host import _verdict_exit_code  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.artifacts import KINDS, ArtifactHandlers
from syndica.kernel.budget import Budget, BudgetPolicy
from syndica.kernel.call_limit import (
    WRAP_UP_GRACE_S,
    bind_wall_clock,
    model_call_limit,
    reset_wall_clock,
)
from syndica.kernel.context import ContextBlock, StopReason, ToolCall
from syndica.kernel.events import EventBus, Subscription
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import LLMConfig, ScriptedLLM, ScriptedTurn
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.runtime import AgentFactory, ExitMode, Runtime
from syndica.kernel.stores.file_session import FileSessionStore
from syndica.kernel.stores.memory_artifact import InMemoryArtifactStore
from syndica.kernel.tools.local import (
    _BUDGET_STOPPED,  # pyright: ignore[reportPrivateUsage]
    Deliver,
    local_tools,
)
from syndica.kernel.trace import AttributeValue, SpanKind
from syndica.kernel.tracing import NullTracer, span_id_of
from syndica.kernel.workspace import ExecResult

_TIMEOUT = 5.0


class _Probe:
    def __init__(self) -> None:
        self.config = LLMConfig(provider='scripted', model='probe')
        self.limit: float | None = None

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        self.limit = model_call_limit()
        return ContextBlock(role='assistant', content='done')


class _Hang:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.config = LLMConfig(provider='fake', model=name)
        self._calls = calls
        self._name = name

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        self._calls.append(self._name)
        if self._name == 'fast':
            return ContextBlock(role='assistant', content='later')
        await asyncio.Event().wait()
        raise AssertionError('unreachable')


class _Portal:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    async def run(self, command: str, *, timeout: float, cwd: str) -> ExecResult:  # noqa: ASYNC109
        self.timeouts.append(timeout)
        return ExecResult(
            exit_code=124,
            stdout='',
            stderr='',
            timed_out=True,
            truncated=False,
            running=False,
        )

    async def start(self, command: str, *, cwd: str) -> str:
        raise AssertionError('not started')


def _factory(llm: Any, *, last_turn_s: float = 0.0) -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = BaseAgent(
            AgentOptions(name='Worker', llm=llm, tools={}, system_prompt='SP'),
            hooks=hooks,
            events=events,
        )
        agent._last_turn_s = last_turn_s  # pyright: ignore[reportPrivateUsage]
        return agent

    return build


async def test_a_wrap_up_call_is_given_a_bounded_grace() -> None:
    probe = _Probe()
    runtime = Runtime(budget=BudgetPolicy(root=Budget(wall_clock_s=10)))
    try:
        _, ctx = await asyncio.wait_for(
            _drive(runtime, _factory(probe, last_turn_s=100.0)), _TIMEOUT
        )
    finally:
        await runtime.shutdown()
    # 预算收尾轮交回 BUDGET：宽限只让这一次模型调用跑完，不把结局改成完成。
    assert ctx.stop_reason is StopReason.BUDGET
    assert probe.limit is not None
    assert probe.limit > WRAP_UP_GRACE_S


async def test_a_normal_call_is_not_given_the_wrap_up_grace() -> None:
    probe = _Probe()
    runtime = Runtime(budget=BudgetPolicy(root=Budget(wall_clock_s=10)))
    try:
        await asyncio.wait_for(_drive(runtime, _factory(probe)), _TIMEOUT)
    finally:
        await runtime.shutdown()
    assert probe.limit is not None
    assert probe.limit < WRAP_UP_GRACE_S


async def test_a_deadline_cancels_the_in_flight_call_without_switching_platform() -> None:
    calls: list[str] = []
    slow = _Hang('slow', calls)
    fast = _Hang('fast', calls)
    router = ModelRouter([('slow', slow), ('fast', fast)])
    runtime = Runtime(budget=BudgetPolicy(root=Budget(wall_clock_s=0.3)))
    try:
        _, ctx = await asyncio.wait_for(_drive(runtime, _factory(router)), _TIMEOUT)
        view = runtime._budget.pool().view()  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()
    assert calls == ['slow']
    assert ctx.stop_reason is StopReason.BUDGET
    assert view.used.estimated is True
    assert view.used.input_tokens > 0


async def test_a_new_limit_replaces_the_stored_one_and_wall_clock_starts_over(
    tmp_path: Any,
) -> None:
    store = FileSessionStore(tmp_path / 'sessions')
    agent_id = await _saved_session(store, BudgetPolicy(root=Budget(turns=10, wall_clock_s=100)))
    second = Runtime(
        sessions=store,
        redactor=RuleRedactor(),
        budget=BudgetPolicy(root=Budget(turns=4, wall_clock_s=30)),
        agents={'Worker': _factory(ScriptedLLM([ScriptedTurn(content='two')]))},
    )
    try:
        assert await second.ensure_active(agent_id)
        view = second._budget.pool().view()  # pyright: ignore[reportPrivateUsage]
        own = second._budget_of(agent_id)  # pyright: ignore[reportPrivateUsage]
    finally:
        await second.shutdown()
    assert own is not None and own.used.turns == 1
    assert view.limit.turns == 4
    assert view.used.turns == 1
    assert view.limit.wall_clock_s == 30
    assert view.remaining.wall_clock_s is not None
    assert view.remaining.wall_clock_s > 20


async def test_resuming_the_same_session_twice_in_one_runtime_counts_its_usage_once(
    tmp_path: Any,
) -> None:
    store = FileSessionStore(tmp_path / 'sessions')
    agent_id = await _saved_session(store, BudgetPolicy(root=Budget(turns=10)))
    second = Runtime(
        sessions=store,
        redactor=RuleRedactor(),
        budget=BudgetPolicy(root=Budget(turns=10)),
        agents={'Worker': _factory(ScriptedLLM([]))},
    )
    try:
        assert await second.ensure_active(agent_id)
        await second.despawn(agent_id)
        assert await second.ensure_active(agent_id)
        view = second._budget.pool().view()  # pyright: ignore[reportPrivateUsage]
    finally:
        await second.shutdown()
    assert view.used.turns == 1


async def test_shutdown_saves_the_estimated_cost_of_an_in_flight_call(tmp_path: Any) -> None:
    started = asyncio.Event()

    class _Hold:
        def __init__(self) -> None:
            self.config = LLMConfig(provider='scripted', model='hold')

        async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError('unreachable')

    store = FileSessionStore(tmp_path / 'sessions')
    runtime = Runtime(
        sessions=store,
        redactor=RuleRedactor(),
        budget=BudgetPolicy(root=Budget(turns=10)),
    )
    try:
        agent_id = await runtime.spawn(_factory(_Hold()), query='go', session=True)
        await asyncio.wait_for(started.wait(), _TIMEOUT)
        await runtime.shutdown()
    finally:
        if not runtime._closed:  # pyright: ignore[reportPrivateUsage]
            await runtime.shutdown()
    record = await store.load(agent_id)
    assert record is not None and record.budget is not None
    assert record.budget.used.estimated is True
    assert record.budget.used.input_tokens > 0
    assert record.budget.turns == 10


async def test_a_tool_that_already_has_a_timeout_stops_at_the_budget_deadline() -> None:
    from syndica.kernel.tools.local import Bash

    portal = _Portal()
    token = bind_wall_clock(lambda: 0.0)
    try:
        result = await Bash(portal).execute('sleep 30', timeout=30)  # pyright: ignore[reportArgumentType]
    finally:
        reset_wall_clock(token)
    assert portal.timeouts == []
    assert _BUDGET_STOPPED in result.output


async def test_a_clamped_timeout_uses_the_budget_wording_when_it_fires() -> None:
    from syndica.kernel.tools.local import Bash

    portal = _Portal()
    token = bind_wall_clock(lambda: 0.2)
    try:
        result = await Bash(portal).execute('sleep 30', timeout=30)  # pyright: ignore[reportArgumentType]
    finally:
        reset_wall_clock(token)
    assert portal.timeouts == [pytest.approx(0.2)]
    assert _BUDGET_STOPPED in result.output


def test_verdict_exit_codes_are_10_through_13() -> None:
    assert [
        _verdict_exit_code(status) for status in ('pass', 'partial', 'block', 'unverified')
    ] == [
        10,
        11,
        12,
        13,
    ]
    assert _verdict_exit_code(None) == 13


class _Events(NullTracer):
    def __init__(self) -> None:
        self.kinds: dict[str, SpanKind] = {}
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
        self.kinds[span_id] = kind

    def add_event(self, span_id: str, name: str, attributes: dict[str, AttributeValue]) -> None:
        self.events.append((span_id, name, attributes))


async def test_a_grant_between_cycles_lands_once_on_the_next_agent_span() -> None:
    tracer = _Events()
    llm = ScriptedLLM([ScriptedTurn(content=text) for text in ('one', 'two', 'three')])
    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=10)))
    runtime._set_tracer(tracer)  # pyright: ignore[reportPrivateUsage]
    events = runtime.events.subscribe()
    try:
        agent_id = await runtime.spawn(_factory(llm), query='go', exit_mode=ExitMode.IDLE)
        await asyncio.wait_for(_ended(events, agent_id), _TIMEOUT)
        before = time.time()
        runtime._grant_budget(agent_id, Budget(turns=3))  # pyright: ignore[reportPrivateUsage]
        assert [name for _, name, _ in tracer.events if name == 'budget.granted'] == []
        for text in ('again', 'more'):
            assert (await runtime.send(agent_id, text)).ok
            await asyncio.wait_for(_ended(events, agent_id), _TIMEOUT)
    finally:
        events.close()
        await runtime.shutdown()
    grants = [
        (tracer.kinds[span_id], span_id, attributes)
        for span_id, name, attributes in tracer.events
        if name == 'budget.granted'
    ]
    assert len(grants) == 1
    kind, span_id, attributes = grants[0]
    assert kind is SpanKind.AGENT
    assert span_id == span_id_of(SpanKind.AGENT, agent_id, 2)
    assert attributes['budget.dimensions'] == ['turns']
    assert int(before * 1000) <= cast(int, attributes['budget.granted_at_ms']) <= time.time() * 1000


async def test_a_grant_never_followed_by_a_cycle_is_reported_at_shutdown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=10)))
    try:
        agent_id, _ = await asyncio.wait_for(
            _drive(runtime, _factory(ScriptedLLM([ScriptedTurn(content='one')]))), _TIMEOUT
        )
        runtime._grant_budget(agent_id, Budget(turns=3))  # pyright: ignore[reportPrivateUsage]
    finally:
        with caplog.at_level(logging.WARNING, logger='syndica.kernel.runtime'):
            await runtime.shutdown()
    [record] = [r for r in caplog.records if 'trace 里没有' in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert record.args == (agent_id, 1)


class _Slides:
    handles = (('slides', 'application/x-slides'),)

    async def preview(self, ref: Any, reader: Any) -> Any:
        raise AssertionError('not used')

    async def semantic_diff(self, a: Any, b: Any, reader: Any) -> Any:
        raise AssertionError('not used')

    async def validate(self, ref: Any, reader: Any) -> list[Any]:
        return []


def test_installed_handlers_add_the_kinds_deliver_accepts() -> None:
    handlers = ArtifactHandlers()
    assert handlers.kinds() == KINDS
    handlers.register(_Slides())
    assert handlers.kinds() == KINDS | {'slides'}


async def test_deliver_takes_its_kinds_from_the_registry_at_call_time(tmp_path: Any) -> None:
    (tmp_path / 'deck.txt').write_text('hello')
    handlers = ArtifactHandlers()
    tools = local_tools(
        LocalWorkspace(str(tmp_path), shell='/bin/sh', env={}).portal(),
        artifact_kinds=handlers.kinds,
    )
    deliver = tools['Deliver']
    refused = await deliver.execute(path='deck.txt', kind='slides')  # pyright: ignore[reportAttributeAccessIssue]
    handlers.register(_Slides())
    accepted = await deliver.execute(path='deck.txt', kind='slides')  # pyright: ignore[reportAttributeAccessIssue]
    plain = await Deliver(LocalWorkspace(str(tmp_path), shell='/bin/sh', env={}).portal()).execute(
        path='deck.txt', kind='slides'
    )
    refused_text = str(refused.output)
    assert '不认识' in refused_text and 'slides' not in refused_text.split('期望')[1]
    assert [ref.kind for ref in accepted.artifacts] == ['slides']
    assert '不认识' in str(plain.output)


async def test_a_delivery_note_reaches_the_event_and_the_trace_and_nothing_else_gets_one(
    tmp_path: Any,
) -> None:
    tracer = _Events()
    workspace = LocalWorkspace(str(tmp_path), shell='/bin/sh', env={})
    tools = local_tools(workspace.portal())
    calls = [
        ToolCall(id='w', name='Write', arguments=json.dumps({'path': 'a.md', 'content': '# a\n'})),
        ToolCall(
            id='d', name='Deliver', arguments=json.dumps({'path': 'a.md', 'note': 'the report'})
        ),
    ]
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=calls[:1]),
            ScriptedTurn(tool_calls=calls[1:]),
            ScriptedTurn(content='done'),
        ]
    )

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name='Worker', llm=llm, tools=dict(tools), system_prompt='SP'),
            hooks=hooks,
            events=events,
        )

    runtime = Runtime(artifacts=InMemoryArtifactStore())
    runtime._set_tracer(tracer)  # pyright: ignore[reportPrivateUsage]
    subscription = runtime.events.subscribe()
    try:
        agent_id = await runtime.spawn(build, query='go')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()
    registered = [
        (event.payload['ref']['is_deliverable'], event.payload['note'])
        async for event in subscription
        if event.kind == 'artifact_registered'
    ]
    delivered = [
        attributes for _, name, attributes in tracer.events if name == 'artifact.delivered'
    ]
    assert registered == [(False, ''), (True, 'the report')]
    assert [(item['artifact.path'], item['artifact.note']) for item in delivered] == [
        ('a.md', 'the report')
    ]


async def _saved_session(store: FileSessionStore, budget: BudgetPolicy) -> str:
    worker = _factory(ScriptedLLM([ScriptedTurn(content='one')]))
    first = Runtime(
        sessions=store, redactor=RuleRedactor(), budget=budget, agents={'Worker': worker}
    )
    try:
        agent_id = await first.spawn(worker, query='go', session=True)
        await asyncio.wait_for(first.wait(agent_id), _TIMEOUT)
    finally:
        await first.shutdown()
    return agent_id


async def _ended(events: Subscription, agent_id: str) -> None:
    async for event in events:
        if event.agent_id == agent_id and event.kind == 'agent_end':
            return


async def _drive(runtime: Runtime, factory: AgentFactory) -> Any:
    agent_id = await runtime.spawn(factory, query='go')
    return agent_id, await runtime.wait(agent_id)
