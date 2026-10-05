"""内核插桩（M3 D2 / D3）：同步的 tracer、span、因果边与失败隔离。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from collections.abc import Callable, Coroutine, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from syndica.kernel import (
    AgentOptions,
    BaseAgent,
    ConfigError,
    ConfirmationOutcome,
    ConfirmationRequest,
    ContractViolation,
    DangerJudge,
    Disposition,
    EventBus,
    ExitMode,
    HookResult,
    HooksCenter,
    HumanInteractionModule,
    Junction,
    Message,
    Reply,
    RetryModule,
    RiskLevel,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    StopReason,
    Tool,
    ToolCall,
    ToolResult,
    Trigger,
    TrustLevel,
)
from syndica.kernel.context import ContextBlock
from syndica.kernel.danger import DangerRequest, DangerVerdict
from syndica.kernel.llm import LLMConfig
from syndica.kernel.local_table import LocalAgentTable
from syndica.kernel.stores import InMemoryAgentRegistry, InMemoryMessageBroker
from syndica.kernel.trace import (
    ATTRIBUTE_DOMAINS,
    AttributeValue,
    CausalityEdge,
    EdgeKind,
    SpanKind,
    SpanStatus,
)
from syndica.kernel.tracing import (
    _CURRENT_SPAN,  # pyright: ignore[reportPrivateUsage]
    NullTracer,
    Tracer,
    _Tracing,  # pyright: ignore[reportPrivateUsage]
    current_llm_call,
    current_span_id,
    llm_attempt,
    span_id_of,
)

_TIMEOUT = 5.0
_RECORDERS: list[_Recorder] = []


@pytest.fixture(autouse=True)
def _recorders_saw_no_protocol_violations() -> Iterator[None]:
    _RECORDERS.clear()
    yield
    assert [problem for recorder in _RECORDERS for problem in recorder.problems] == []


@dataclass
class _Started:
    parent: str | None
    kind: SpanKind
    name: str
    agent_id: str
    attributes: dict[str, AttributeValue]


@dataclass
class _Recorder:
    """记下收到的一切。违约（同一个 span 开两次、结束两次、结束没开过的、给已结束的 span 加事件）
    记进 `problems` 而不是抛出：内核会把 tracer 抛出的异常吞掉，断言在这里抛出等于没断言。"""

    started: dict[str, _Started] = field(default_factory=dict[str, _Started])
    ended: dict[str, tuple[SpanStatus, dict[str, AttributeValue]]] = field(
        default_factory=dict[str, tuple[SpanStatus, dict[str, AttributeValue]]]
    )
    events: list[tuple[str, str, dict[str, AttributeValue]]] = field(
        default_factory=list[tuple[str, str, dict[str, AttributeValue]]]
    )
    edges: list[CausalityEdge] = field(default_factory=list[CausalityEdge])
    exchanges: list[tuple[str, Mapping[str, Any], ContextBlock | None, BaseException | None]] = (
        field(
            default_factory=list[
                tuple[str, Mapping[str, Any], ContextBlock | None, BaseException | None]
            ]
        )
    )
    tool_results: list[tuple[str, ToolResult | None, BaseException | None]] = field(
        default_factory=list[tuple[str, ToolResult | None, BaseException | None]]
    )
    arguments: list[tuple[str, dict[str, Any] | None]] = field(
        default_factory=list[tuple[str, dict[str, Any] | None]]
    )
    answers: list[tuple[str, Message]] = field(default_factory=list[tuple[str, Message]])
    problems: list[str] = field(default_factory=list[str])

    def __post_init__(self) -> None:
        _RECORDERS.append(self)

    def start_span(
        self,
        span_id: str,
        parent_span_id: str | None,
        kind: SpanKind,
        name: str,
        agent_id: str,
        attributes: dict[str, AttributeValue],
    ) -> None:
        if span_id in self.started:
            self.problems.append(f'{kind.value} span {span_id} started twice')
        self.started[span_id] = _Started(parent_span_id, kind, name, agent_id, dict(attributes))

    def end_span(
        self, span_id: str, status: SpanStatus, attributes: dict[str, AttributeValue]
    ) -> None:
        if span_id not in self.started or span_id in self.ended:
            self.problems.append(f'span {span_id} ended without being open')
        self.ended[span_id] = (status, attributes)

    def add_event(self, span_id: str, name: str, attributes: dict[str, AttributeValue]) -> None:
        if span_id not in self.started or span_id in self.ended:
            self.problems.append(f'event {name} on span {span_id} that is not open')
        self.events.append((span_id, name, attributes))

    def add_edge(self, edge: CausalityEdge) -> None:
        self.edges.append(edge)

    def llm_exchange(
        self,
        span_id: str,
        request: Mapping[str, Any],
        reply: ContextBlock | None,
        error: BaseException | None,
    ) -> None:
        self._before_end(span_id, SpanKind.LLM_CALL, 'llm exchange', reply, error)
        self.exchanges.append((span_id, request, reply, error))

    def tool_result(
        self,
        span_id: str,
        result: ToolResult | None,
        error: BaseException | None,
        *,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        self._before_end(span_id, SpanKind.TOOL_CALL, 'tool result', result, error)
        self.tool_results.append((span_id, result, error))
        self.arguments.append((span_id, None if arguments is None else dict(arguments)))

    def wait_answer(self, span_id: str, message: Message) -> None:
        self._before_end(span_id, SpanKind.WAIT, 'wait answer', message, None)
        self.answers.append((span_id, message))

    def _before_end(
        self, span_id: str, kind: SpanKind, what: str, value: object, error: object
    ) -> None:
        started = self.started.get(span_id)
        if started is None or span_id in self.ended:
            self.problems.append(f'{what} on span {span_id} that is not open')
        elif started.kind is not kind:
            self.problems.append(f'{what} on {started.kind.value} span {span_id}')
        if (value is None) == (error is None):
            self.problems.append(f'{what} on span {span_id} with {value!r} and {error!r}')

    def of(self, kind: SpanKind) -> list[str]:
        return [span_id for span_id, started in self.started.items() if started.kind is kind]

    def attributes(self, span_id: str) -> dict[str, AttributeValue]:
        return {**self.started[span_id].attributes, **self.ended[span_id][1]}

    def status(self, span_id: str) -> SpanStatus:
        return self.ended[span_id][0]

    def all_ended(self) -> bool:
        return set(self.started) == set(self.ended)


def _traced(**kwargs: Any) -> tuple[Runtime, _Recorder]:
    runtime = Runtime(**kwargs)
    recorder = _Recorder()
    runtime._set_tracer(recorder)  # pyright: ignore[reportPrivateUsage]
    return runtime, recorder


def _call(call_id: str, name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def _factory(name: str, llm: ScriptedLLM, tools: dict[str, Tool] | None = None, **options: Any):
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name=name, llm=llm, tools=dict(tools or {}), **options),
            hooks=hooks,
            events=events,
        )

    return build


class _Echo(Tool):
    __tool_name__ = 'Echo'
    __tool_command_args__ = ()

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        return True

    async def execute(self, text: str = '') -> ToolResult:
        """原样交回。
        text: 要交回的文字
        """
        return ToolResult(output=text or 'echo')


class _Raises(Tool):
    __tool_name__ = 'Raises'
    __tool_command_args__ = ()

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def execute(self) -> ToolResult:
        """抛出构造时给的异常。"""
        raise self._error


class _Blocks(Tool):
    """一直挂着，直到测试放开或它被取消。"""

    __tool_name__ = 'Blocks'
    __tool_command_args__ = ()

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self) -> ToolResult:
        """挂住，直到被放开。"""
        self.entered.set()
        await self.release.wait()
        return ToolResult(output='released')


@dataclass
class _Decide:
    disposition: Disposition

    def decide(self, request: DangerRequest) -> Disposition:
        return self.disposition


class _Answer:
    """确认通道：一律交回给定的结局。"""

    def __init__(self, outcome: ConfirmationOutcome) -> None:
        self.outcome = outcome

    async def confirm(self, runtime: Any, request: ConfirmationRequest) -> ConfirmationOutcome:
        return self.outcome


async def _run_one(
    runtime: Runtime, build: Callable[[HooksCenter, EventBus], BaseAgent], query: str = 'go'
) -> str:
    agent_id = await runtime.spawn(build, query=query)
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    return agent_id


def _length_prefixed(kind: str, *key: str | int) -> str:
    digest = hashlib.sha256()
    for part in (kind, *key):
        data = part.encode() if isinstance(part, str) else str(part).encode()
        digest.update(('s' if isinstance(part, str) else 'i').encode() + f'{len(data)}:'.encode())
        digest.update(data)
    return digest.hexdigest()[:16]


# --- span id ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('kind', 'key'),
    [
        (SpanKind.AGENT, ('a1', 1)),
        (SpanKind.TURN, ('a1', 3)),
        (SpanKind.TOOL_CALL, ('a1', 2, 3, 0)),
        (SpanKind.WAIT, ('tool_call', 'a1', 1, 1, 0, 'request', 0)),
    ],
)
def test_a_span_id_is_the_length_prefixed_hash_of_its_structural_key(
    kind: SpanKind, key: tuple[str | int, ...]
):
    assert span_id_of(kind, *key) == _length_prefixed(kind.value, *key)


def test_span_ids_do_not_collide_across_part_boundaries_types_or_kinds():
    ids = [
        span_id_of(SpanKind.TURN, 'ab', 'c'),
        span_id_of(SpanKind.TURN, 'a', 'bc'),
        span_id_of(SpanKind.AGENT, '1'),
        span_id_of(SpanKind.AGENT, 1),
        span_id_of(SpanKind.WAIT, 'a1', 1),
        span_id_of(SpanKind.TURN, 'a1', 1),
        span_id_of(SpanKind.AGENT, 'a1', 1),
    ]
    assert len(set(ids)) == len(ids)
    assert all(len(span_id) == 16 and int(span_id, 16) >= 0 for span_id in ids)


@pytest.mark.parametrize('part', [True, 1.5, None, b'x'], ids=['bool', 'float', 'none', 'bytes'])
def test_a_structural_key_only_takes_strings_and_integers(part: object):
    with pytest.raises(ContractViolation) as caught:
        span_id_of(SpanKind.TURN, 'a1', cast(Any, part))

    assert type(part).__name__ in caught.value.received


async def test_the_span_ids_of_a_run_are_exactly_their_structural_keys():
    runtime, recorder = _traced()
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[_call('dup', 'Echo'), _call('dup', 'Echo'), _call('', 'Echo')]
            ),
            ScriptedTurn(tool_calls=[_call('x', 'Echo')]),
            ScriptedTurn(tool_calls=[_call('y', 'Echo')]),
            ScriptedTurn(content='done'),
        ]
    )
    try:
        agent_id = await _run_one(runtime, _factory('Root', llm, {'Echo': _Echo()}))
    finally:
        await runtime.shutdown()

    expected = {
        span_id_of(SpanKind.AGENT, agent_id, 1): (SpanKind.AGENT, None),
        **{
            span_id_of(SpanKind.TURN, agent_id, turn): (
                SpanKind.TURN,
                span_id_of(SpanKind.AGENT, agent_id, 1),
            )
            for turn in (1, 2, 3, 4)
        },
        **{
            span_id_of(SpanKind.LLM_CALL, agent_id, turn, 1): (
                SpanKind.LLM_CALL,
                span_id_of(SpanKind.TURN, agent_id, turn),
            )
            for turn in (1, 2, 3, 4)
        },
        **{
            span_id_of(SpanKind.TOOL_CALL, agent_id, 1, turn, index): (
                SpanKind.TOOL_CALL,
                span_id_of(SpanKind.TURN, agent_id, turn),
            )
            for turn, index in [(1, 0), (1, 1), (1, 2), (2, 0), (3, 0)]
        },
    }
    assert {
        span_id: (started.kind, started.parent) for span_id, started in recorder.started.items()
    } == expected
    assert recorder.all_ended()
    calls = [
        (recorder.attributes(span_id)['tool.call_id'], recorder.attributes(span_id)['tool.index'])
        for span_id in recorder.of(SpanKind.TOOL_CALL)
    ]
    assert sorted(calls, key=str) == sorted(
        [('dup', 0), ('dup', 1), ('', 2), ('x', 0), ('y', 0)], key=str
    )


# --- the spans of a run ----------------------------------------------------------------------


async def test_the_agent_span_carries_the_final_verdict():
    # 终态裁决进 trace（§7.5）：不打开轨迹也看得到这次交付验没验过、为什么没验。
    from syndica.kernel.acceptance import (
        Anchor,
        AnchorSource,
        OutcomeKind,
        Rubric,
        RubricKind,
        RubricOutcome,
        derive_verdict,
    )
    from syndica.kernel.hooks import HookResult, Junction

    anchors = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'section four'),)
    checkable = Rubric(id='R1', statement='s', kind=RubricKind.CHECKABLE, anchors=anchors)
    judgment = Rubric(id='J1', statement='reads well', kind=RubricKind.JUDGMENT, anchors=anchors)
    satisfied = {'R1': RubricOutcome('R1', OutcomeKind.SATISFIED, anchors)}
    verdict = derive_verdict([checkable, judgment], satisfied)

    async def judge(**_: Any) -> HookResult:
        return HookResult(modified={'verdict': verdict})

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        hooks.register(Junction.COMPLETION_PROPOSED, judge)
        llm = ScriptedLLM([ScriptedTurn(content='done')])
        return BaseAgent(AgentOptions(name='Root', llm=llm, tools={}), hooks=hooks, events=events)

    runtime, recorder = _traced()
    try:
        agent_id = await _run_one(runtime, build)
    finally:
        await runtime.shutdown()
    attributes = recorder.attributes(span_id_of(SpanKind.AGENT, agent_id, 1))
    assert attributes['verdict.status'] == 'unverified'
    assert attributes['verdict.unverified_cause'] == 'needs_human'
    assert attributes['verdict.needs_human'] == ['J1']


async def test_agent_and_turn_spans_carry_role_cycle_turn_and_stop_reason():
    runtime, recorder = _traced()
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    try:
        agent_id = await _run_one(runtime, _factory('Root', llm))
        await runtime.send(agent_id, 'again')
        await asyncio.wait_for(_until(lambda: len(recorder.of(SpanKind.AGENT)) == 2), _TIMEOUT)
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    for cycle, turn in [(1, 1), (2, 2)]:
        agent_span = span_id_of(SpanKind.AGENT, agent_id, cycle)
        assert recorder.started[agent_span].parent is None
        assert recorder.started[agent_span].name == 'Root'
        assert recorder.attributes(agent_span) == {
            'agent.role': 'Root',
            'agent.cycle': cycle,
            'stop_reason': 'completed',
        }
        turn_span = span_id_of(SpanKind.TURN, agent_id, turn)
        assert recorder.started[turn_span].parent == agent_span
        assert recorder.attributes(turn_span) == {'turn': turn}
        assert recorder.status(agent_span) is SpanStatus.OK


async def test_a_run_that_fails_ends_its_spans_as_errors():
    runtime, recorder = _traced()
    llm = ScriptedLLM([ScriptedTurn(raises=RuntimeError('provider down'))])
    try:
        agent_id = await _run_one(runtime, _factory('Root', llm))
    finally:
        await runtime.shutdown()

    agent_span = span_id_of(SpanKind.AGENT, agent_id, 1)
    assert recorder.status(agent_span) is SpanStatus.ERROR
    assert recorder.attributes(agent_span)['stop_reason'] == 'error'
    assert recorder.status(span_id_of(SpanKind.TURN, agent_id, 1)) is SpanStatus.ERROR
    assert recorder.status(span_id_of(SpanKind.LLM_CALL, agent_id, 1, 1)) is SpanStatus.ERROR


async def test_a_killed_agent_ends_its_open_spans_as_cancelled():
    runtime, recorder = _traced()
    blocks = _Blocks()
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('b', 'Blocks')])])
    try:
        agent_id = await runtime.spawn(_factory('Root', llm, {'Blocks': blocks}), query='go')
        await asyncio.wait_for(blocks.entered.wait(), _TIMEOUT)
        await runtime.kill(agent_id)
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    for span_id in [
        span_id_of(SpanKind.AGENT, agent_id, 1),
        span_id_of(SpanKind.TURN, agent_id, 1),
        span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0),
    ]:
        assert recorder.status(span_id) is SpanStatus.CANCELLED
    assert 'tool.outcome' not in recorder.attributes(
        span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0)
    )


async def test_the_batch_a_kill_interrupted_is_traced_as_interrupted_when_the_agent_resumes():
    runtime, recorder = _traced()
    blocks = _Blocks()
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('e', 'Echo'), _call('b', 'Blocks')]),
            ScriptedTurn(content='resumed'),
        ]
    )
    try:
        agent_id = await runtime.spawn(
            _factory('Root', llm, {'Blocks': blocks, 'Echo': _Echo()}, max_tool_concurrency=1),
            query='go',
        )
        await asyncio.wait_for(blocks.entered.wait(), _TIMEOUT)
        await runtime.kill(agent_id)
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        await runtime.send(agent_id, 'carry on')
        await asyncio.wait_for(_until(lambda: len(recorder.of(SpanKind.AGENT)) == 2), _TIMEOUT)
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    repaired = span_id_of(SpanKind.TOOL_CALL, agent_id, 2, 1, 1)
    assert recorder.attributes(repaired)['tool.outcome'] == 'interrupted'
    assert recorder.started[repaired].parent == span_id_of(SpanKind.AGENT, agent_id, 2)
    assert (
        recorder.attributes(span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0))['tool.outcome']
        == 'ok'
    )


async def _until(ready: Callable[[], bool]) -> None:
    while True:
        if ready():
            return
        await asyncio.sleep(0.005)


# --- tool outcomes ---------------------------------------------------------------------------


async def _outcome_of(
    calls: list[ToolCall],
    tools: dict[str, Tool],
    *,
    index: int = 0,
    runtime_options: dict[str, Any] | None = None,
    **options: Any,
) -> tuple[_Recorder, str, dict[str, AttributeValue]]:
    runtime, recorder = _traced(**(runtime_options or {}))
    llm = ScriptedLLM([ScriptedTurn(tool_calls=calls), ScriptedTurn(content='done')])
    try:
        agent_id = await _run_one(runtime, _factory('Root', llm, tools, **options))
    finally:
        await runtime.shutdown()
    span_id = span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, index)
    return recorder, agent_id, recorder.attributes(span_id)


async def test_a_tool_that_returns_is_ok_and_its_span_names_the_call():
    recorder, agent_id, attributes = await _outcome_of([_call('c1', 'Echo')], {'Echo': _Echo()})

    span_id = span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0)
    assert recorder.started[span_id].name == 'Echo'
    assert recorder.status(span_id) is SpanStatus.OK
    assert attributes == {
        'tool.name': 'Echo',
        'tool.call_id': 'c1',
        'tool.index': 0,
        'tool.outcome': 'ok',
        'danger.rule_ids': [],
        'danger.risk': 'none',
        'danger.provenance': 'trusted',
        'danger.decided_by': 'policy',
        'danger.disposition': 'allow',
    }


@pytest.mark.parametrize(
    ('call', 'outcome'),
    [
        (_call('c', 'Missing'), 'unknown_tool'),
        (ToolCall(id='c', name='Echo', arguments='{not json'), 'bad_arguments'),
        (ToolCall(id='c', name='Echo', arguments='[1]'), 'bad_arguments'),
    ],
    ids=['unknown', 'unparsable', 'not-an-object'],
)
async def test_calls_the_tool_never_sees_have_their_own_outcomes(call: ToolCall, outcome: str):
    _, _, attributes = await _outcome_of([call], {'Echo': _Echo()})

    assert attributes['tool.outcome'] == outcome


async def test_a_tool_that_raises_an_ordinary_error_is_error():
    _, _, attributes = await _outcome_of(
        [_call('c', 'Raises')], {'Raises': _Raises(ValueError('bad input'))}
    )

    assert attributes['tool.outcome'] == 'error'


async def test_a_tool_that_raises_a_kernel_error_aborts_and_the_rest_of_the_batch_is_not_executed():
    recorder, agent_id, attributes = await _outcome_of(
        [_call('c', 'Raises'), _call('d', 'Echo'), _call('e', 'Echo')],
        {
            'Raises': _Raises(ContractViolation(expected='x', received='y', next_action='z')),
            'Echo': _Echo(),
        },
        max_tool_concurrency=1,
    )

    assert attributes['tool.outcome'] == 'aborted'
    assert recorder.status(span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0)) is SpanStatus.ERROR
    for index in (1, 2):
        rest = span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, index)
        assert recorder.attributes(rest)['tool.outcome'] == 'not_executed'
        assert recorder.started[rest].parent == span_id_of(SpanKind.TURN, agent_id, 1)


async def test_a_judge_that_raises_is_judge_failed():
    class _BrokenJudge(DangerJudge):
        def judge(self, tool: Tool, args: Mapping[str, Any]) -> DangerVerdict:
            raise RuntimeError('rule table corrupt')

    _, _, attributes = await _outcome_of(
        [_call('c', 'Echo')], {'Echo': _Echo()}, danger_judge=_BrokenJudge()
    )

    assert attributes['tool.outcome'] == 'judge_failed'


async def test_a_call_a_hook_skips_is_skipped():
    runtime, recorder = _traced()
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('c', 'Echo')]), ScriptedTurn(content='ok')])

    async def skip(**kwargs: Any) -> HookResult:
        return HookResult(skip=True, result=ToolResult(output='skipped'))

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm, {'Echo': _Echo()})(hooks, events)
        agent.register_hook(Junction.PRE_TOOL_USE, skip)
        return agent

    try:
        agent_id = await _run_one(runtime, build)
    finally:
        await runtime.shutdown()

    attributes = recorder.attributes(span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0))
    assert attributes['tool.outcome'] == 'skipped'
    assert recorder.arguments == [(span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0), None)]


async def test_the_tracer_gets_the_arguments_the_tool_really_ran_with():
    runtime, recorder = _traced()
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[ToolCall(id='c', name='Echo', arguments='{"text": "from model"}')]
            ),
            ScriptedTurn(content='ok'),
        ]
    )

    async def rewrite(**kwargs: Any) -> HookResult:
        return HookResult(modified={'args': {'text': 'from hook'}})

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm, {'Echo': _Echo()})(hooks, events)
        agent.register_hook(Junction.PRE_TOOL_USE, rewrite)
        return agent

    try:
        agent_id = await _run_one(runtime, build)
    finally:
        await runtime.shutdown()

    span = span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0)
    assert recorder.arguments == [(span, {'text': 'from hook'})]


async def test_a_high_risk_call_traced_to_untrusted_content_is_decided_by_the_flag():
    class _HighRisk(DangerJudge):
        def judge(self, tool: Tool, args: Mapping[str, Any]) -> DangerVerdict:
            return DangerVerdict(rule_id='test.high', level=RiskLevel.HIGH, reason='risky')

    runtime, recorder = _traced()
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('c', 'Echo')]), ScriptedTurn(content='ok')])
    try:
        agent_id = await runtime.spawn(
            _factory('Root', llm, {'Echo': _Echo()}, danger_judge=_HighRisk()),
            query='from the web',
            query_trust=TrustLevel.UNTRUSTED,
        )
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    attributes = recorder.attributes(span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0))
    assert {key: attributes[key] for key in attributes if key.startswith('danger.')} == {
        'danger.rule_ids': ['test.high'],
        'danger.risk': 'high',
        'danger.provenance': 'untrusted',
        'danger.decided_by': 'flag',
        'danger.disposition': 'block',
    }
    assert attributes['tool.outcome'] == 'blocked'


async def test_a_blocked_call_records_the_gate_decision():
    _, _, attributes = await _outcome_of(
        [_call('c', 'Echo')], {'Echo': _Echo()}, danger_policy=_Decide(Disposition.BLOCK)
    )

    assert attributes['tool.outcome'] == 'blocked'
    assert attributes['danger.disposition'] == 'block'
    assert attributes['danger.decided_by'] == 'policy'


async def test_a_call_that_needs_a_confirmation_nobody_can_give_is_no_channel():
    recorder, agent_id, attributes = await _outcome_of(
        [_call('c', 'Echo')], {'Echo': _Echo()}, danger_policy=_Decide(Disposition.CONFIRM)
    )

    assert attributes['tool.outcome'] == 'no_channel'
    assert recorder.of(SpanKind.CONFIRMATION) == []


@pytest.mark.parametrize(
    ('answer', 'outcome'),
    [
        (ConfirmationOutcome.APPROVED, 'ok'),
        (ConfirmationOutcome.REJECTED, 'rejected'),
        (ConfirmationOutcome.TIMEOUT, 'timeout'),
        (ConfirmationOutcome.PREEMPTED, 'preempted'),
        (ConfirmationOutcome.ABORTED, 'confirmation_aborted'),
    ],
)
async def test_each_confirmation_outcome_has_its_span_and_its_tool_outcome(
    answer: ConfirmationOutcome, outcome: str
):
    recorder, agent_id, attributes = await _outcome_of(
        [_call('c', 'Echo')],
        {'Echo': _Echo()},
        runtime_options={'confirmation_handler': _Answer(answer)},
        danger_policy=_Decide(Disposition.CONFIRM),
    )

    confirmation = span_id_of(SpanKind.CONFIRMATION, agent_id, 1, 1, 0)
    assert recorder.of(SpanKind.CONFIRMATION) == [confirmation]
    assert recorder.started[confirmation].parent == span_id_of(
        SpanKind.TOOL_CALL, agent_id, 1, 1, 0
    )
    assert recorder.attributes(confirmation) == {'confirmation.outcome': answer.value}
    assert attributes['tool.outcome'] == outcome
    assert attributes['danger.disposition'] == 'confirm'


async def test_the_calls_an_abort_after_the_model_stops_are_not_executed():
    runtime, recorder = _traced()
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('a', 'Echo'), _call('b', 'Echo')])])
    agent_ids: list[str] = []

    async def abort_on_response(**kwargs: Any) -> HookResult:
        await runtime.abort(agent_ids[0])
        return HookResult()

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm, {'Echo': _Echo()})(hooks, events)
        agent_ids.append(agent.agent_id)
        agent.register_hook(Junction.POST_LLM_RESPONSE, abort_on_response)
        return agent

    try:
        agent_id = await _run_one(runtime, build)
    finally:
        await runtime.shutdown()

    for index in (0, 1):
        span_id = span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, index)
        assert recorder.attributes(span_id)['tool.outcome'] == 'not_executed'
    assert recorder.attributes(span_id_of(SpanKind.AGENT, agent_id, 1))['stop_reason'] == 'aborted'


def test_every_tool_outcome_in_the_schema_is_produced_by_some_case_here():
    produced = {
        'ok',
        'error',
        'aborted',
        'judge_failed',
        'not_executed',
        'skipped',
        'blocked',
        'rejected',
        'timeout',
        'preempted',
        'confirmation_aborted',
        'no_channel',
        'unknown_tool',
        'bad_arguments',
        'interrupted',
    }
    assert produced == set(ATTRIBUTE_DOMAINS['tool.outcome'])


# --- llm calls -------------------------------------------------------------------------------


class _Seeing:
    """记下每次 `create` 时的「当前 LLM 调用」，然后交给内层模型。"""

    def __init__(
        self,
        inner: ScriptedLLM,
        seen: list[tuple[str, int, int] | None],
        model: str = 'seen-model',
    ) -> None:
        self._inner = inner
        self._seen = seen
        self.config = LLMConfig(provider='fake', model=model)

    async def create(self, messages: list[Any], **kwargs: Any) -> Any:
        self._seen.append(current_llm_call())
        return await self._inner.create(messages, **kwargs)


async def test_each_retry_is_its_own_llm_call_span_and_the_current_llm_call_follows_it():
    seen: list[tuple[str, int, int] | None] = []
    llm = _Seeing(
        ScriptedLLM(
            [
                ScriptedTurn(raises=RuntimeError('rate limited')),
                ScriptedTurn(raises=RuntimeError('rate limited')),
                ScriptedTurn(content='finally'),
            ]
        ),
        seen,
    )

    async def no_sleep(seconds: float) -> None:
        return None

    runtime, recorder = _traced(
        modules=[RetryModule({}, sleep=no_sleep)], flags={'llm_retry_attempts': 2}
    )
    try:
        agent_id = await _run_one(runtime, _factory('Root', cast(Any, llm)))
    finally:
        await runtime.shutdown()

    assert seen == [(agent_id, 1, 1), (agent_id, 1, 2), (agent_id, 1, 3)]
    attempts = [span_id_of(SpanKind.LLM_CALL, agent_id, 1, attempt) for attempt in (1, 2, 3)]
    assert recorder.of(SpanKind.LLM_CALL) == attempts
    assert [recorder.status(span_id) for span_id in attempts] == [
        SpanStatus.ERROR,
        SpanStatus.ERROR,
        SpanStatus.OK,
    ]
    assert [recorder.attributes(span_id) for span_id in attempts] == [
        {'llm.attempt': attempt, 'llm.model': 'seen-model'} for attempt in (1, 2, 3)
    ]
    assert current_llm_call() is None


async def test_a_new_turn_counts_its_llm_calls_from_one_again():
    seen: list[tuple[str, int, int] | None] = []
    llm = _Seeing(
        ScriptedLLM([ScriptedTurn(tool_calls=[_call('c', 'Echo')]), ScriptedTurn(content='ok')]),
        seen,
    )
    runtime, _ = _traced()
    try:
        agent_id = await _run_one(runtime, _factory('Root', cast(Any, llm), {'Echo': _Echo()}))
    finally:
        await runtime.shutdown()

    assert seen == [(agent_id, 1, 1), (agent_id, 2, 1)]


# --- waits -----------------------------------------------------------------------------------


async def _take(events: Any, kind: str) -> Any:
    while True:
        event = await asyncio.wait_for(anext(events), timeout=_TIMEOUT)
        if event.kind == kind:
            return event


@pytest.mark.parametrize('answered', [True, False], ids=['answered', 'timeout'])
async def test_a_human_wait_names_its_request_and_its_outcome(answered: bool):
    runtime, recorder = _traced(modules=[HumanInteractionModule()])
    events = runtime.events.subscribe()
    timeout = 30 if answered else 0.05
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('ask', 'AskUser', question='which?', timeout=timeout)]),
            ScriptedTurn(content='done'),
        ]
    )
    try:
        agent_id = await runtime.spawn(_factory('Root', llm), query='go')
        request = await _take(events, 'user_input_requested')
        if answered:
            await runtime.send(
                agent_id,
                'this one',
                activate=False,
                sender='human',
                correlation_id=request.payload['request_id'],
            )
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        events.close()
        await runtime.shutdown()

    key = ['tool_call', agent_id, 1, 1, 0, 'request', 0]
    wait = span_id_of(SpanKind.WAIT, *key)
    assert recorder.of(SpanKind.WAIT) == [wait]
    assert recorder.started[wait].parent == span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0)
    assert recorder.attributes(wait) == {
        'wait.kind': 'human',
        'wait.target': json.dumps(key),
        'wait.outcome': 'answered' if answered else 'timeout',
    }
    answers = [(span, message.sender, message.content) for span, message in recorder.answers]
    assert answers == ([(wait, 'human', 'this one')] if answered else [])


async def test_a_wait_cut_short_by_an_abort_is_aborted():
    runtime, recorder = _traced(modules=[HumanInteractionModule()])
    events = runtime.events.subscribe()
    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('ask', 'AskUser', question='which?', timeout=30)])]
    )
    try:
        agent_id = await runtime.spawn(_factory('Root', llm), query='go')
        await _take(events, 'user_input_requested')
        await runtime.abort(agent_id)
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        events.close()
        await runtime.shutdown()

    [wait] = recorder.of(SpanKind.WAIT)
    assert recorder.attributes(wait)['wait.outcome'] == 'aborted'
    assert recorder.answers == []


async def test_a_delegation_waits_on_the_child_and_spawns_it_from_the_delegate_call():
    child_llm = ScriptedLLM([ScriptedTurn(content='child done')])
    runtime, recorder = _traced(orchestration=True, agents={'Child': _factory('Child', child_llm)})
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('d', 'Delegate', agent='Child', query='work')]),
            ScriptedTurn(content='parent done'),
        ]
    )
    try:
        parent = await _run_one(runtime, _factory('Parent', llm))
    finally:
        await runtime.shutdown()

    delegate = span_id_of(SpanKind.TOOL_CALL, parent, 1, 1, 0)
    [spawn] = [edge for edge in recorder.edges if edge.kind is EdgeKind.SPAWN]
    child = cast(str, spawn.attributes['child.agent_id'])
    assert spawn.source_span_id == delegate
    assert spawn.target_span_id == span_id_of(SpanKind.AGENT, child, 1)
    assert spawn.target_span_id in recorder.started
    [wait] = recorder.of(SpanKind.WAIT)
    assert wait == span_id_of(SpanKind.WAIT, 'tool_call', parent, 1, 1, 0, 'wait', 0)
    assert recorder.started[wait].parent == delegate
    assert recorder.attributes(wait) == {
        'wait.kind': 'agent',
        'wait.target': child,
        'wait.outcome': 'answered',
    }
    assert recorder.answers == []


class _AwaitReply(Tool):
    __tool_name__ = 'AwaitReply'
    __tool_command_args__ = ()

    def __init__(self, runtime: Runtime, agent_id: list[str], started: asyncio.Event) -> None:
        self._runtime = runtime
        self._agent_id = agent_id
        self._started = started

    async def execute(self, correlation: str) -> ToolResult:
        """等一条带这个关联 ID 的答复。
        correlation: 关联 ID
        """
        self._started.set()
        result = await self._runtime.wait_for(
            self._agent_id[0], Reply(correlation_id=correlation, timeout=_TIMEOUT)
        )
        return ToolResult(output=result.state.value)


async def test_a_reply_from_another_agents_call_links_to_the_wait_it_ended():
    runtime, recorder = _traced(orchestration=True)
    started = asyncio.Event()
    waiting_id: list[str] = []
    waiting_llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('w', 'AwaitReply', correlation='q1')]),
            ScriptedTurn(content='got it'),
        ]
    )

    def waiting(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Waiting', waiting_llm)(hooks, events)
        waiting_id.append(agent.agent_id)
        agent.install_runtime_tools({'AwaitReply': _AwaitReply(runtime, waiting_id, started)})
        return agent

    class _Answer(Tool):
        __tool_name__ = 'Answer'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """答复那次等待。"""
            await runtime.send(waiting_id[0], 'answer', activate=False, correlation_id='q1')
            return ToolResult(output='sent')

    answer_llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('a', 'Answer')]), ScriptedTurn(content='done')]
    )
    try:
        waiter = await runtime.spawn(waiting, query='go')
        await asyncio.wait_for(started.wait(), _TIMEOUT)
        answerer = await _run_one(runtime, _factory('Answerer', answer_llm, {'Answer': _Answer()}))
        await asyncio.wait_for(runtime.wait(waiter), _TIMEOUT)
    finally:
        await runtime.shutdown()

    wait = span_id_of(SpanKind.WAIT, 'tool_call', waiter, 1, 1, 0, 'request', 0)
    assert recorder.attributes(wait) == {
        'wait.kind': 'agent',
        'wait.target': 'q1',
        'wait.outcome': 'answered',
    }
    assert [(span, message.content) for span, message in recorder.answers] == [(wait, 'answer')]
    [edge] = recorder.edges
    assert (edge.kind, edge.source_span_id, edge.target_span_id) == (
        EdgeKind.MESSAGE,
        span_id_of(SpanKind.TOOL_CALL, answerer, 1, 1, 0),
        wait,
    )


async def test_two_waits_the_host_starts_get_their_own_spans():
    runtime, recorder = _traced()
    blocks = _Blocks()
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('b', 'Blocks')]), ScriptedTurn(content='ok')])
    try:
        agent_id = await runtime.spawn(_factory('Root', llm, {'Blocks': blocks}), query='go')
        await asyncio.wait_for(blocks.entered.wait(), _TIMEOUT)
        for correlation in ('h1', 'h2'):
            waiting = asyncio.create_task(
                runtime.wait_for(agent_id, Reply(correlation_id=correlation, timeout=_TIMEOUT))
            )
            await asyncio.sleep(0.01)
            await runtime.send(agent_id, 'answer', activate=False, correlation_id=correlation)
            assert (await asyncio.wait_for(waiting, _TIMEOUT)).state.value == 'answered'
        blocks.release.set()
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert recorder.of(SpanKind.WAIT) == [
        span_id_of(SpanKind.WAIT, 'host', 'wait', 0),
        span_id_of(SpanKind.WAIT, 'host', 'wait', 1),
    ]
    assert {recorder.started[span].agent_id for span in recorder.of(SpanKind.WAIT)} == {agent_id}
    assert [span for span, _ in recorder.answers] == recorder.of(SpanKind.WAIT)


async def test_an_agent_run_directly_inside_a_span_does_not_hang_under_it():
    recorder = _Recorder()
    tracing = _Tracing(recorder)
    agent = _factory('Inner', ScriptedLLM([ScriptedTurn(content='x')]))(HooksCenter(), EventBus())
    agent._attach_tracing(tracing)  # pyright: ignore[reportPrivateUsage]
    with tracing.span(SpanKind.TOOL_CALL, ('outer', 1, 1, 0), name='outer', agent_id='outer'):
        await agent.run('go')

    assert recorder.started[span_id_of(SpanKind.AGENT, agent.agent_id, 1)].parent is None


def _sources(runtime: Runtime) -> dict[str, tuple[str, str]]:
    tracing = cast(_Tracing, runtime._tracing)  # pyright: ignore[reportPrivateUsage]
    return dict(tracing._sent_from)  # pyright: ignore[reportPrivateUsage]


def _host_span(runtime: Runtime) -> Any:
    tracing = cast(_Tracing, runtime._tracing)  # pyright: ignore[reportPrivateUsage]
    return tracing.span(SpanKind.TURN, ('host', 1), name='host', agent_id='host')


async def test_a_consumed_message_leaves_no_source_behind():
    runtime, recorder = _traced()
    llm = ScriptedLLM([ScriptedTurn(content='1'), ScriptedTurn(content='2')])
    try:
        agent_id = await _run_one(runtime, _factory('Root', llm))
        with _host_span(runtime):
            await runtime.send(agent_id, 'read me')
        await asyncio.wait_for(
            _until(lambda: span_id_of(SpanKind.TURN, agent_id, 2) in recorder.ended), _TIMEOUT
        )
        sources = _sources(runtime)
    finally:
        await runtime.shutdown()

    assert sources == {}
    assert [edge.kind for edge in recorder.edges] == [EdgeKind.MESSAGE]


async def test_a_message_that_could_not_be_delivered_leaves_no_source_behind():
    runtime, recorder = _traced()
    try:
        zombie = await _run_one(runtime, _factory('Done', ScriptedLLM([ScriptedTurn(content='1')])))
        with _host_span(runtime):
            delivered = await runtime.send(zombie, 'too late', activate=False)
        sources = _sources(runtime)
    finally:
        await runtime.shutdown()

    assert delivered.ok is False
    assert sources == {}
    assert recorder.events == []


async def test_a_message_its_receiver_never_read_is_forgotten_when_the_receiver_goes():
    runtime, _ = _traced()
    blocks = _Blocks()
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('b', 'Blocks')])])
    try:
        busy = await runtime.spawn(_factory('Busy', llm, {'Blocks': blocks}), query='go')
        await asyncio.wait_for(blocks.entered.wait(), _TIMEOUT)
        with _host_span(runtime):
            delivered = await runtime.send(busy, 'unread', activate=False)
        before = _sources(runtime)
        await runtime.despawn(busy)
        after = _sources(runtime)
    finally:
        await runtime.shutdown()

    assert delivered.ok is True
    assert [receiver for _, receiver in before.values()] == [busy]
    assert after == {}


async def test_the_current_llm_call_is_set_only_while_a_call_is_in_flight():
    seen: dict[str, tuple[str, int, int] | None] = {}

    async def before(**kwargs: Any) -> HookResult:
        seen['before'] = current_llm_call()
        return HookResult()

    async def after(**kwargs: Any) -> HookResult:
        seen.setdefault('after', current_llm_call())
        return HookResult()

    class _Looks(Tool):
        __tool_name__ = 'Looks'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """记下此刻的「当前 LLM 调用」。"""
            seen['tool'] = current_llm_call()
            return ToolResult(output='looked')

    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('l', 'Looks')]), ScriptedTurn(content='ok')])

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm, {'Looks': _Looks()})(hooks, events)
        agent.register_hook(Junction.PRE_LLM_REQUEST, before)
        agent.register_hook(Junction.POST_LLM_RESPONSE, after)
        return agent

    runtime, _ = _traced()
    try:
        agent_id = await _run_one(runtime, build)
    finally:
        await runtime.shutdown()

    assert seen == {'before': None, 'after': None, 'tool': None}
    assert agent_id


# --- causality -------------------------------------------------------------------------------


async def test_three_delegated_children_each_have_a_spawn_edge_from_their_own_call():
    child_llm = ScriptedLLM([ScriptedTurn(content='c')] * 3)
    runtime, recorder = _traced(orchestration=True, agents={'Child': _factory('Child', child_llm)})
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    _call(f'd{n}', 'Delegate', agent='Child', query=f'part {n}') for n in range(3)
                ]
            ),
            ScriptedTurn(content='parent done'),
        ]
    )
    try:
        parent = await _run_one(runtime, _factory('Parent', llm))
    finally:
        await runtime.shutdown()

    spawns = [edge for edge in recorder.edges if edge.kind is EdgeKind.SPAWN]
    assert sorted(edge.source_span_id for edge in spawns) == sorted(
        span_id_of(SpanKind.TOOL_CALL, parent, 1, 1, index) for index in range(3)
    )
    for edge in spawns:
        child = cast(str, edge.attributes['child.agent_id'])
        assert edge.target_span_id == span_id_of(SpanKind.AGENT, child, 1)


async def test_a_child_that_never_runs_still_has_its_spawn_edge():
    runtime, recorder = _traced()
    idle: list[str] = []

    class _SpawnIdle(Tool):
        __tool_name__ = 'SpawnIdle'
        __tool_command_args__ = ()

        def __init__(self, parent: list[str]) -> None:
            self._parent = parent

        async def execute(self) -> ToolResult:
            """派一个不给活的子 Agent。"""
            idle.append(
                await runtime.spawn(_factory('Idle', ScriptedLLM([])), parent_id=self._parent[0])
            )
            return ToolResult(output=idle[0])

    parent_box: list[str] = []
    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('s', 'SpawnIdle')]), ScriptedTurn(content='ok')]
    )

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Parent', llm)(hooks, events)
        parent_box.append(agent.agent_id)
        agent.install_runtime_tools({'SpawnIdle': _SpawnIdle(parent_box)})
        return agent

    try:
        parent = await _run_one(runtime, build)
    finally:
        await runtime.shutdown()

    [spawn] = [edge for edge in recorder.edges if edge.kind is EdgeKind.SPAWN]
    assert spawn.attributes == {'child.agent_id': idle[0]}
    assert spawn.source_span_id == span_id_of(SpanKind.TOOL_CALL, parent, 1, 1, 0)
    assert spawn.target_span_id not in recorder.started


async def test_a_host_spawn_has_no_spawn_edge():
    runtime, recorder = _traced()
    try:
        await _run_one(runtime, _factory('Root', ScriptedLLM([ScriptedTurn(content='x')])))
    finally:
        await runtime.shutdown()

    assert recorder.edges == []


async def test_three_messages_each_link_to_the_turn_that_consumed_them():
    runtime, recorder = _traced(orchestration=True)
    receivers: list[str] = []
    try:
        for name in ('A', 'B', 'C'):
            receivers.append(
                await _run_one(
                    runtime,
                    _factory(
                        name, ScriptedLLM([ScriptedTurn(content='1'), ScriptedTurn(content='2')])
                    ),
                )
            )
        llm = ScriptedLLM(
            [
                ScriptedTurn(
                    tool_calls=[
                        _call(f's{n}', 'SendMessage', agent_id=receiver, content=f'note {n}')
                        for n, receiver in enumerate(receivers)
                    ]
                ),
                ScriptedTurn(content='sent'),
            ]
        )
        sender = await _run_one(runtime, _factory('Sender', llm))
        for receiver in receivers:
            await asyncio.wait_for(
                _until(lambda r=receiver: span_id_of(SpanKind.AGENT, r, 2) in recorder.ended),
                _TIMEOUT,
            )
    finally:
        await runtime.shutdown()

    messages = [edge for edge in recorder.edges if edge.kind is EdgeKind.MESSAGE]
    assert [edge.kind for edge in recorder.edges] == [EdgeKind.MESSAGE] * 3
    by_target = {edge.target_span_id: edge for edge in messages}
    sent = {
        cast(str, attributes['message.to']): (span_id, attributes)
        for span_id, name, attributes in recorder.events
        if name == 'message.sent'
    }
    for index, receiver in enumerate(receivers):
        edge = by_target[span_id_of(SpanKind.TURN, receiver, 2)]
        assert edge.source_span_id == span_id_of(SpanKind.TOOL_CALL, sender, 1, 1, index)
        source, attributes = sent[receiver]
        assert source == edge.source_span_id
        assert attributes['message.id'] == edge.attributes['message.id']


async def test_a_host_message_has_no_edge():
    runtime, recorder = _traced()
    llm = ScriptedLLM([ScriptedTurn(content='1'), ScriptedTurn(content='2')])
    try:
        agent_id = await _run_one(runtime, _factory('Root', llm))
        await runtime.send(agent_id, 'from the host')
        await asyncio.wait_for(
            _until(lambda: span_id_of(SpanKind.AGENT, agent_id, 2) in recorder.ended), _TIMEOUT
        )
    finally:
        await runtime.shutdown()

    assert recorder.edges == []
    assert recorder.events == []


def _spawning_on_despawn_of(runtime: Runtime, watched: list[str], helpers: list[str]):
    async def on_despawn(agent_id: str, **kwargs: Any) -> HookResult:
        if agent_id in watched and not helpers:
            helpers.append('')
            helpers[0] = await runtime.spawn(_factory('Helper', ScriptedLLM([])))
        return HookResult()

    return on_despawn


async def test_what_a_kill_teardown_spawns_is_not_attributed_to_the_call_that_killed():
    """kill 建的收尾任务继承了发起它的那次调用的上下文。"""
    runtime, recorder = _traced()
    victim = await runtime.spawn(_factory('Victim', ScriptedLLM([])), exit_mode=ExitMode.EXIT)
    helpers: list[str] = []
    runtime.hooks.register(
        Junction.AGENT_DESPAWN, _spawning_on_despawn_of(runtime, [victim], helpers)
    )

    class _Killer(Tool):
        __tool_name__ = 'Killer'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """杀掉那个 Agent。"""
            await runtime.kill(victim)
            return ToolResult(output='killed')

    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('k', 'Killer')]), ScriptedTurn(content='ok')])
    try:
        await _run_one(runtime, _factory('Killer', llm, {'Killer': _Killer()}))
        await asyncio.wait_for(_until(lambda: bool(helpers and helpers[0])), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert recorder.edges == []


async def test_what_an_agent_spawns_after_its_run_is_not_attributed_to_whoever_spawned_it():
    """驱动任务建在 spawn 它的那次调用的上下文里；`ExitMode.EXIT` 的拆除跑在 `run` 之后。"""
    runtime, recorder = _traced()
    children: list[str] = []
    helpers: list[str] = []
    runtime.hooks.register(
        Junction.AGENT_DESPAWN, _spawning_on_despawn_of(runtime, children, helpers)
    )

    class _SpawnChild(Tool):
        __tool_name__ = 'SpawnChild'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """派一个干完就退出的子 Agent。"""
            child_llm = ScriptedLLM([ScriptedTurn(content='child done')])
            children.append(
                await runtime.spawn(
                    _factory('Child', child_llm), query='work', exit_mode=ExitMode.EXIT
                )
            )
            return ToolResult(output=children[0])

    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('s', 'SpawnChild')]), ScriptedTurn(content='ok')]
    )
    try:
        parent = await _run_one(runtime, _factory('Parent', llm, {'SpawnChild': _SpawnChild()}))
        await asyncio.wait_for(_until(lambda: bool(helpers and helpers[0])), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert [(edge.source_span_id, edge.attributes) for edge in recorder.edges] == [
        (span_id_of(SpanKind.TOOL_CALL, parent, 1, 1, 0), {'child.agent_id': children[0]})
    ]


async def test_a_despawn_hook_runs_outside_spans_and_the_call_gets_its_span_back():
    """拆除的 hook 可以由几条并发拆除里的任何一条触发：它派的活不能记在触发者的 span 上。"""
    runtime, recorder = _traced()
    doomed = await runtime.spawn(_factory('Doomed', ScriptedLLM([])))
    helpers: list[str] = []
    after: list[str] = []
    runtime.hooks.register(
        Junction.AGENT_DESPAWN, _spawning_on_despawn_of(runtime, [doomed], helpers)
    )

    class _Remove(Tool):
        __tool_name__ = 'Remove'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """销毁那个 Agent，再派一个子 Agent。"""
            await runtime.despawn(doomed)
            after.append(await runtime.spawn(_factory('After', ScriptedLLM([]))))
            return ToolResult(output=after[0])

    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('r', 'Remove')]), ScriptedTurn(content='ok')])
    try:
        remover = await _run_one(runtime, _factory('Remover', llm, {'Remove': _Remove()}))
    finally:
        await runtime.shutdown()

    call = span_id_of(SpanKind.TOOL_CALL, remover, 1, 1, 0)
    assert helpers and helpers[0]
    assert [(edge.source_span_id, edge.attributes) for edge in recorder.edges] == [
        (call, {'child.agent_id': after[0]})
    ]
    assert recorder.events == [(call, 'agent.despawned', {'agent.id': doomed})]


async def test_what_a_shutdown_hook_sends_is_not_attributed_to_the_span_that_called_shutdown():
    runtime, recorder = _traced()
    agent_id = await runtime.spawn(_factory('Root', ScriptedLLM([])))

    async def on_shutdown(**kwargs: Any) -> HookResult:
        await runtime.send(agent_id, 'bye', activate=False)
        return HookResult()

    runtime.hooks.register(Junction.RUNTIME_SHUTDOWN, on_shutdown)
    tracing = cast(_Tracing, runtime._tracing)  # pyright: ignore[reportPrivateUsage]
    with tracing.span(SpanKind.TURN, ('host', 1), name='host', agent_id='host'):
        await runtime.shutdown()

    assert recorder.events == []


async def test_a_despawn_from_a_tool_call_is_an_event_on_that_call():
    runtime, recorder = _traced()
    doomed = await runtime.spawn(_factory('Doomed', ScriptedLLM([])))

    class _Remover(Tool):
        __tool_name__ = 'Remover'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """销毁那个 Agent。"""
            await runtime.despawn(doomed)
            return ToolResult(output='gone')

    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('r', 'Remover')]), ScriptedTurn(content='ok')]
    )
    try:
        remover = await _run_one(runtime, _factory('Remover', llm, {'Remover': _Remover()}))
    finally:
        await runtime.shutdown()

    assert recorder.events == [
        (span_id_of(SpanKind.TOOL_CALL, remover, 1, 1, 0), 'agent.despawned', {'agent.id': doomed})
    ]


# --- no new suspension points ----------------------------------------------------------------


class _CountingLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        super().__init__()
        self.scheduled = 0

    def call_soon(self, callback: Any, *args: Any, context: Any = None) -> Any:
        self.scheduled += 1
        return super().call_soon(callback, *args, context=context)


def _scheduled(tracer: Tracer | None) -> int:
    """在一个数调度次数的事件循环里跑一段固定的多 Agent 场景，交回调度了多少次。"""

    async def scenario() -> None:
        child_llm = ScriptedLLM([ScriptedTurn(content='c')] * 2)
        runtime = Runtime(orchestration=True, agents={'Child': _factory('Child', child_llm)})
        if tracer is not None:
            runtime._set_tracer(tracer)  # pyright: ignore[reportPrivateUsage]
        llm = ScriptedLLM(
            [
                ScriptedTurn(
                    tool_calls=[
                        _call('d1', 'Delegate', agent='Child', query='one'),
                        _call('d2', 'Delegate', agent='Child', query='two'),
                        _call('e', 'Echo', text='x'),
                    ]
                ),
                ScriptedTurn(content='done'),
            ]
        )
        try:
            await _run_one(runtime, _factory('Root', llm, {'Echo': _Echo()}))
        finally:
            await runtime.shutdown()

    loop = _CountingLoop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()
    return loop.scheduled


def test_tracing_adds_no_scheduling_to_a_run():
    recorder = _Recorder()
    untraced = _scheduled(None)

    assert _scheduled(recorder) == untraced == _scheduled(NullTracer())
    assert len(recorder.started) > 10 and recorder.all_ended()


# --- failure isolation -----------------------------------------------------------------------


class _Broken:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def _fail(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise self.error

    start_span = end_span = add_event = add_edge = llm_exchange = tool_result = wait_answer = _fail


async def test_a_tracer_that_always_raises_changes_nothing_and_is_reported_once_then_counted(
    caplog: pytest.LogCaptureFixture,
):
    broken = _Broken(RuntimeError('disk full'))
    runtime = Runtime()
    runtime._set_tracer(cast(Tracer, broken))  # pyright: ignore[reportPrivateUsage]
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('c', 'Echo')]), ScriptedTurn(content='ok')])
    try:
        agent_id = await runtime.spawn(_factory('Root', llm, {'Echo': _Echo()}), query='go')
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert context.stop_reason is StopReason.COMPLETED
    reports = [record for record in caplog.records if record.name.startswith('syndica.kernel')]
    first, total = reports
    assert first.levelno == total.levelno == logging.ERROR
    assert cast(tuple[object, ...], first.args)[:4] == (
        '_Broken',
        'start_span',
        'RuntimeError',
        broken.error,
    )
    assert '同步返回' in str(cast(tuple[object, ...], first.args)[4])
    assert total.args == ('_Broken', broken.calls)
    assert broken.calls > 5


class _OldSignature(NullTracer):
    """照另一个签名写的 tracer：`tool_result` 少收了 `arguments`。"""

    def tool_result(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, span_id: str, result: Any, error: Any
    ) -> None:
        del span_id, result, error


def test_a_tracer_whose_method_signature_does_not_match_is_told_to_follow_the_protocol(
    caplog: pytest.LogCaptureFixture,
):
    tracing = _Tracing(cast(Tracer, _OldSignature()))

    with caplog.at_level(logging.ERROR, logger='syndica.kernel.tracing'):
        tracing.call(
            'tool_result', lambda tracer: tracer.tool_result('s', None, None, arguments={})
        )

    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.tracing']
    args = cast(tuple[object, ...], record.args)
    assert (args[1], args[2]) == ('tool_result', 'TypeError')
    assert '签名与 Tracer 协议对不上' in str(args[4])


@pytest.mark.parametrize(
    'error', [KeyboardInterrupt(), SystemExit(3)], ids=['keyboard-interrupt', 'system-exit']
)
def test_a_tracer_that_asks_the_process_to_stop_is_not_swallowed(error: BaseException):
    tracing = _Tracing(cast(Tracer, _Broken(error)))

    with pytest.raises(type(error)):
        tracing.call('start_span', lambda tracer: tracer.add_edge(cast(Any, None)))
    assert tracing.failures == 0


@pytest.mark.parametrize(
    'error', [asyncio.CancelledError(), GeneratorExit()], ids=['cancelled', 'generator-exit']
)
def test_a_tracer_that_raises_cancellation_is_counted_as_its_own_failure(error: BaseException):
    tracing = _Tracing(cast(Tracer, _Broken(error)))

    tracing.call('start_span', lambda tracer: tracer.add_edge(cast(Any, None)))

    assert tracing.failures == 1


def test_an_async_tracer_method_is_a_failure_and_its_coroutine_is_closed(
    caplog: pytest.LogCaptureFixture,
):
    created: list[Coroutine[Any, Any, None]] = []

    class _Async:
        def add_edge(self, edge: CausalityEdge) -> Coroutine[Any, Any, None]:
            async def body() -> None:
                return None

            created.append(body())
            return created[-1]

    tracing = _Tracing(cast(Tracer, _Async()))
    tracing.call('add_edge', lambda tracer: tracer.add_edge(cast(Any, None)))

    assert tracing.failures == 1
    assert inspect.getcoroutinestate(cast(Any, created[0])) == inspect.CORO_CLOSED
    [record] = caplog.records
    assert 'coroutine' in str(cast(tuple[object, ...], record.args)[3])
    assert 'async def' in str(cast(tuple[object, ...], record.args)[4])


@pytest.mark.parametrize('tracer', [_Recorder, object(), 'tracer'], ids=['class', 'object', 'str'])
def test_set_tracer_takes_only_a_tracer_instance(tracer: object):
    runtime = Runtime()

    with pytest.raises(ConfigError) as caught:
        runtime._set_tracer(cast(Tracer, tracer))  # pyright: ignore[reportPrivateUsage]

    assert 'Tracer' in caught.value.expected


async def test_a_refused_spawn_leaves_a_host_held_agent_on_its_own_tracing():
    class _Down(InMemoryAgentRegistry):
        async def register(self, info: Any) -> None:
            raise RuntimeError('registry down')

    runtime = Runtime(registry=_Down())
    runtime._set_tracer(_Recorder())  # pyright: ignore[reportPrivateUsage]
    held = BaseAgent(
        AgentOptions(name='Held', llm=ScriptedLLM([])), hooks=HooksCenter(), events=EventBus()
    )
    own = held._tracing  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(RuntimeError, match='registry down'):
            await runtime.spawn(lambda hooks, events: held)
    finally:
        await runtime.shutdown()

    assert held._tracing is own  # pyright: ignore[reportPrivateUsage]
    assert held._executor._tracing is own  # pyright: ignore[reportPrivateUsage]


def test_events_on_a_span_that_has_ended_are_dropped_but_edges_from_it_are_kept():
    recorder = _Recorder()
    tracing = _Tracing(recorder)
    with tracing.span(SpanKind.TURN, ('a1', 1), name='turn', agent_id='a1') as span:
        pass
    outlived = _CURRENT_SPAN.set(span)
    try:
        tracing.sending('m1', 'a2')
        tracing.sent('m1', 'a2', delivered=True)
        tracing.event('anything', {})
    finally:
        _CURRENT_SPAN.reset(outlived)
    with tracing.span(SpanKind.TURN, ('a2', 1), name='turn', agent_id='a2'):
        tracing.consumed('m1')

    assert recorder.events == []
    assert [(edge.source_span_id, edge.attributes) for edge in recorder.edges] == [
        (span.span_id, {'message.id': 'm1'})
    ]
    assert current_span_id() is None


# --- review: what a turn, a model and a tool record --------------------------------------------


async def test_a_turn_that_dies_while_injecting_is_not_reopened_by_the_next_cycle():
    runtime, recorder = _traced()
    held = asyncio.Event()
    entered = asyncio.Event()
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='after revive')])

    async def on_injected(**kwargs: Any) -> HookResult:
        if not entered.is_set():
            entered.set()
            await held.wait()
        return HookResult()

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm)(hooks, events)
        agent.register_hook(Junction.MESSAGE_INJECTED, on_injected)
        return agent

    try:
        agent_id = await _run_one(runtime, build)
        await runtime.send(agent_id, 'one')
        await asyncio.wait_for(entered.wait(), _TIMEOUT)
        await runtime.kill(agent_id)
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        await runtime.send(agent_id, 'two')
        await asyncio.wait_for(
            _until(lambda: span_id_of(SpanKind.AGENT, agent_id, 3) in recorder.ended), _TIMEOUT
        )
    finally:
        await runtime.shutdown()

    turns = recorder.of(SpanKind.TURN)
    assert turns == [span_id_of(SpanKind.TURN, agent_id, turn) for turn in (1, 2, 3)]
    assert recorder.status(turns[1]) is SpanStatus.CANCELLED


async def test_the_model_name_comes_from_the_llms_config():
    runtime, recorder = _traced()
    llm = ScriptedLLM([ScriptedTurn(content='x')])
    try:
        agent_id = await _run_one(runtime, _factory('Root', llm))
    finally:
        await runtime.shutdown()

    attempt = span_id_of(SpanKind.LLM_CALL, agent_id, 1, 1)
    assert recorder.attributes(attempt)['llm.model'] == llm.config.model


async def test_a_fallback_attempt_is_counted_and_named_after_the_fallback():
    original = _Seeing(ScriptedLLM([ScriptedTurn(raises=RuntimeError('down'))]), [], 'main-model')
    backup = _Seeing(ScriptedLLM([ScriptedTurn(content='from backup')]), [], 'backup-model')

    async def no_sleep(seconds: float) -> None:
        return None

    runtime, recorder = _traced(
        modules=[RetryModule({'backup': cast(Any, backup)}, sleep=no_sleep)],
        flags={'llm_fallbacks': 'backup'},
    )
    try:
        agent_id = await _run_one(runtime, _factory('Root', cast(Any, original)))
    finally:
        await runtime.shutdown()

    attempts = [span_id_of(SpanKind.LLM_CALL, agent_id, 1, n) for n in (1, 2)]
    assert recorder.of(SpanKind.LLM_CALL) == attempts
    assert [recorder.attributes(span)['llm.model'] for span in attempts] == [
        'main-model',
        'backup-model',
    ]
    assert [recorder.status(span) for span in attempts] == [SpanStatus.ERROR, SpanStatus.OK]


async def test_an_unknown_tool_is_recorded_as_unknown_not_by_the_name_the_model_made_up():
    recorder, agent_id, attributes = await _outcome_of(
        [_call('c', 'rm -rf / && Echo')], {'Echo': _Echo()}
    )

    span_id = span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0)
    assert recorder.started[span_id].name == '<unknown>'
    assert attributes['tool.name'] == '<unknown>'
    assert attributes['tool.outcome'] == 'unknown_tool'


async def test_a_call_id_reused_from_an_earlier_turn_still_gets_its_unexecuted_block():
    runtime, recorder = _traced()
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('call_0', 'Echo')]),
            ScriptedTurn(tool_calls=[_call('call_0', 'Echo'), _call('call_1', 'Echo')]),
        ]
    )
    agent_ids: list[str] = []
    responses = {'count': 0}

    async def abort_on_second_response(**kwargs: Any) -> HookResult:
        responses['count'] += 1
        if responses['count'] == 2:
            await runtime.abort(agent_ids[0])
        return HookResult()

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm, {'Echo': _Echo()})(hooks, events)
        agent_ids.append(agent.agent_id)
        agent.register_hook(Junction.POST_LLM_RESPONSE, abort_on_second_response)
        return agent

    try:
        agent_id = await runtime.spawn(build, query='go')
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    tool_blocks = [block.tool_call_id for block in context.current_traj if block.role == 'tool']
    assert tool_blocks == ['call_0', 'call_0', 'call_1']
    for index in (0, 1):
        span = span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 2, index)
        assert recorder.attributes(span)['tool.outcome'] == 'not_executed'


async def test_a_command_tool_reports_its_exit_code_and_whether_it_timed_out():
    class _Command(Tool):
        __tool_name__ = 'Command'
        __tool_command_args__ = ()

        async def execute(self, code: int = 0, timed_out: bool = False) -> ToolResult:
            """假装跑了一条命令。
            code: 退出码
            timed_out: 是否超时
            """
            return ToolResult(output='ran', meta={'exit_code': code, 'timed_out': timed_out})

    class _Odd(Tool):
        __tool_name__ = 'Odd'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """meta 里的类型不对。"""
            return ToolResult(output='odd', meta={'exit_code': True, 'timed_out': 'no'})

    recorder, agent_id, attributes = await _outcome_of(
        [_call('a', 'Command', code=3, timed_out=True), _call('b', 'Odd')],
        {'Command': _Command(), 'Odd': _Odd()},
    )

    assert (attributes['tool.exit_code'], attributes['tool.timed_out']) == (3, True)
    odd = recorder.attributes(span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 1))
    assert 'tool.exit_code' not in odd and 'tool.timed_out' not in odd


async def test_a_turn_aborted_at_its_boundary_has_no_turn_span():
    runtime, recorder = _traced()
    agent_ids: list[str] = []

    class _StopMe(Tool):
        __tool_name__ = 'StopMe'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """请求中止自己。"""
            await runtime.abort(agent_ids[0])
            return ToolResult(output='asked')

    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('s', 'StopMe')]), ScriptedTurn(content='x')])

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm, {'StopMe': _StopMe()})(hooks, events)
        agent_ids.append(agent.agent_id)
        return agent

    try:
        agent_id = await _run_one(runtime, build)
    finally:
        await runtime.shutdown()

    assert recorder.of(SpanKind.TURN) == [span_id_of(SpanKind.TURN, agent_id, 1)]
    assert recorder.attributes(span_id_of(SpanKind.AGENT, agent_id, 1))['stop_reason'] == 'aborted'


# --- review: what the kernel's own tasks and the host do -------------------------------------


async def test_an_agent_spawned_from_a_model_hook_does_not_count_on_the_parents_model_call():
    runtime, recorder = _traced()
    children: list[str] = []

    class _CallsAModel(Tool):
        __tool_name__ = 'CallsAModel'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """在工具里像一个模块那样包住一次模型调用。"""
            with llm_attempt(ScriptedLLM([])):
                pass
            return ToolResult(output='called')

    child_llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('c', 'CallsAModel')])]
        + [ScriptedTurn(content='child done')]
    )

    async def spawn_child(**kwargs: Any) -> HookResult:
        if not children:
            children.append(
                await runtime.spawn(
                    _factory('Child', child_llm, {'CallsAModel': _CallsAModel()}), query='go'
                )
            )
        return HookResult()

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Parent', ScriptedLLM([ScriptedTurn(content='p')]))(hooks, events)
        agent.register_hook(Junction.POST_LLM_RESPONSE, spawn_child)
        return agent

    try:
        parent = await _run_one(runtime, build)
        await asyncio.wait_for(_until(lambda: bool(children)), _TIMEOUT)
        await asyncio.wait_for(runtime.wait(children[0]), _TIMEOUT)
    finally:
        await runtime.shutdown()

    parent_attempts = [
        span for span in recorder.of(SpanKind.LLM_CALL) if recorder.started[span].agent_id == parent
    ]
    assert parent_attempts == [span_id_of(SpanKind.LLM_CALL, parent, 1, 1)]


async def test_a_model_call_a_tool_makes_is_not_an_attempt_of_the_turns_model_call():
    runtime, recorder = _traced()

    class _CallsAModel(Tool):
        __tool_name__ = 'CallsAModel'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """像一个模块那样包住一次模型调用。"""
            with llm_attempt(ScriptedLLM([])):
                pass
            return ToolResult(output='called')

    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('c', 'CallsAModel')]), ScriptedTurn(content='ok')]
    )
    try:
        agent_id = await _run_one(runtime, _factory('Root', llm, {'CallsAModel': _CallsAModel()}))
    finally:
        await runtime.shutdown()

    assert recorder.of(SpanKind.LLM_CALL) == [
        span_id_of(SpanKind.LLM_CALL, agent_id, turn, 1) for turn in (1, 2)
    ]


async def test_a_kill_teardown_records_nothing_on_the_call_that_killed_even_while_it_is_open():
    runtime, recorder = _traced()
    victim = await runtime.spawn(_factory('Victim', ScriptedLLM([])), exit_mode=ExitMode.EXIT)
    gone = asyncio.Event()

    async def on_despawn(agent_id: str, **kwargs: Any) -> HookResult:
        if agent_id == victim:
            gone.set()
        return HookResult()

    class _KillAndStay(Tool):
        __tool_name__ = 'KillAndStay'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """杀掉那个 Agent，并在它被拆完之前一直开着。"""
            await runtime.kill(victim)
            await asyncio.wait_for(gone.wait(), _TIMEOUT)
            return ToolResult(output='killed')

    runtime.hooks.register(Junction.AGENT_DESPAWN, on_despawn)
    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('k', 'KillAndStay')]), ScriptedTurn(content='ok')]
    )
    try:
        await _run_one(runtime, _factory('Killer', llm, {'KillAndStay': _KillAndStay()}))
    finally:
        await runtime.shutdown()

    assert recorder.events == []


async def test_a_runtime_run_inside_another_runtimes_tool_keeps_to_its_own_trace():
    outer, outer_recorder = _traced()
    inner_recorders: list[_Recorder] = []

    class _Nested(Tool):
        __tool_name__ = 'Nested'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """在这次调用里跑另一个 Runtime。"""
            inner, recorder = _traced()
            inner_recorders.append(recorder)
            try:
                child = await inner.spawn(
                    _factory('Inner', ScriptedLLM([ScriptedTurn(content='i')])), query='go'
                )
                await asyncio.wait_for(inner.wait(child), _TIMEOUT)
                await inner.send(child, 'hello', activate=False)
            finally:
                await inner.shutdown()
            return ToolResult(output='nested')

    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('n', 'Nested')]), ScriptedTurn(content='ok')])
    try:
        await _run_one(outer, _factory('Outer', llm, {'Nested': _Nested()}))
    finally:
        await outer.shutdown()

    inner = inner_recorders[0]
    assert inner.edges == [] and inner.events == []
    assert all(
        started.parent is None or started.parent in inner.started
        for started in inner.started.values()
    )
    assert set(inner.started).isdisjoint(outer_recorder.started)
    assert outer_recorder.edges == []


async def test_a_spawn_edge_points_at_the_first_cycle_the_child_runs_here():
    runtime, recorder = _traced()
    veteran = _factory(
        'Veteran', ScriptedLLM([ScriptedTurn(content='before'), ScriptedTurn(content='here')])
    )(HooksCenter(), EventBus())
    await veteran.run('earlier, elsewhere')

    class _Adopt(Tool):
        __tool_name__ = 'Adopt'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """把一个已经跑过的 Agent spawn 进来。"""
            await runtime.spawn(lambda hooks, events: veteran, query='again')
            return ToolResult(output='adopted')

    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('a', 'Adopt')]), ScriptedTurn(content='ok')])
    try:
        await _run_one(runtime, _factory('Root', llm, {'Adopt': _Adopt()}))
        await asyncio.wait_for(runtime.wait(veteran.agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    [edge] = recorder.edges
    assert edge.target_span_id == span_id_of(SpanKind.AGENT, veteran.agent_id, 2)
    assert edge.target_span_id in recorder.started


async def test_a_host_wait_on_a_bad_agent_id_gets_the_waits_own_refusal_and_no_span():
    runtime, recorder = _traced()
    try:
        with pytest.raises(ContractViolation) as caught:
            await runtime.wait_for(cast(Any, None), Reply(correlation_id='c', timeout=_TIMEOUT))
    finally:
        await runtime.shutdown()

    assert '结构键' not in caught.value.expected
    assert recorder.started == {}


async def test_a_refused_delegation_wait_still_uses_up_its_wait_number():
    runtime, recorder = _traced()
    owner: list[str] = []

    class _WaitTwice(Tool):
        __tool_name__ = 'WaitTwice'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """先等一个不存在的 Agent（被拒），再等一个刚派出的。"""
            child = await runtime.spawn(
                _factory('Child', ScriptedLLM([ScriptedTurn(content='c')])), query='go'
            )
            with pytest.raises(ContractViolation):
                await runtime._wait_in_turn(owner[0], 'gone')  # pyright: ignore[reportPrivateUsage]
            await runtime._wait_in_turn(owner[0], child)  # pyright: ignore[reportPrivateUsage]
            return ToolResult(output='waited')

    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('w', 'WaitTwice')]), ScriptedTurn(content='ok')]
    )

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Parent', llm)(hooks, events)
        owner.append(agent.agent_id)
        agent.install_runtime_tools({'WaitTwice': _WaitTwice()})
        return agent

    try:
        parent = await _run_one(runtime, build)
    finally:
        await runtime.shutdown()

    assert recorder.of(SpanKind.WAIT) == [
        span_id_of(SpanKind.WAIT, 'tool_call', parent, 1, 1, 0, 'wait', 1)
    ]


async def test_an_empty_model_name_is_not_recorded():
    class _Nameless:
        def __init__(self) -> None:
            self.config = type('_Config', (), {'model': ''})()
            self._inner = ScriptedLLM([ScriptedTurn(content='x')])

        async def create(self, messages: list[Any], **kwargs: Any) -> Any:
            return await self._inner.create(messages, **kwargs)

    runtime, recorder = _traced()
    try:
        agent_id = await _run_one(runtime, _factory('Root', cast(Any, _Nameless())))
    finally:
        await runtime.shutdown()

    assert recorder.attributes(span_id_of(SpanKind.LLM_CALL, agent_id, 1, 1)) == {'llm.attempt': 1}


async def test_a_delegation_cut_short_by_an_abort_records_that_outcome():
    child_entered = asyncio.Event()
    child_release = asyncio.Event()

    class _Slow(Tool):
        __tool_name__ = 'Slow'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """挂住，直到被放开。"""
            child_entered.set()
            await child_release.wait()
            return ToolResult(output='slow')

    child_llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('s', 'Slow')]), ScriptedTurn(content='c')]
    )
    runtime, recorder = _traced(
        orchestration=True, agents={'Child': _factory('Child', child_llm, {'Slow': _Slow()})}
    )
    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('d', 'Delegate', agent='Child', query='work')])]
    )
    try:
        parent = await runtime.spawn(_factory('Parent', llm), query='go')
        await asyncio.wait_for(child_entered.wait(), _TIMEOUT)
        await runtime.abort(parent)
        await asyncio.wait_for(runtime.wait(parent), _TIMEOUT)
        child_release.set()
    finally:
        await runtime.shutdown()

    wait = span_id_of(SpanKind.WAIT, 'tool_call', parent, 1, 1, 0, 'wait', 0)
    assert recorder.started[wait].agent_id == parent
    assert recorder.attributes(wait)['wait.outcome'] == 'aborted'


# --- review: message sources ----------------------------------------------------------------


class _SlowBroker(InMemoryMessageBroker):
    """publish 入队之后还要等一会儿才返回：收件人可能在这期间就消费了它。"""

    async def publish(self, agent_id: str, message: Any) -> Any:
        delivered = await super().publish(agent_id, message)
        await asyncio.sleep(0.02)
        return delivered


async def test_a_reply_consumed_before_publish_returns_still_links_to_its_sender():
    runtime, recorder = _traced(broker=_SlowBroker())
    started = asyncio.Event()
    waiting_id: list[str] = []
    waiting_llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('w', 'AwaitReply', correlation='q')]),
            ScriptedTurn(content='got it'),
        ]
    )

    def waiting(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Waiting', waiting_llm)(hooks, events)
        waiting_id.append(agent.agent_id)
        agent.install_runtime_tools({'AwaitReply': _AwaitReply(runtime, waiting_id, started)})
        return agent

    class _Answer(Tool):
        __tool_name__ = 'Answer'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """答复那次等待。"""
            await runtime.send(waiting_id[0], 'answer', activate=False, correlation_id='q')
            return ToolResult(output='sent')

    answer_llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('a', 'Answer')]), ScriptedTurn(content='done')]
    )
    try:
        waiter = await runtime.spawn(waiting, query='go')
        await asyncio.wait_for(started.wait(), _TIMEOUT)
        answerer = await _run_one(runtime, _factory('Answerer', answer_llm, {'Answer': _Answer()}))
        await asyncio.wait_for(runtime.wait(waiter), _TIMEOUT)
    finally:
        await runtime.shutdown()

    [edge] = recorder.edges
    assert edge.source_span_id == span_id_of(SpanKind.TOOL_CALL, answerer, 1, 1, 0)
    assert edge.target_span_id == span_id_of(
        SpanKind.WAIT, 'tool_call', waiter, 1, 1, 0, 'request', 0
    )


async def test_a_send_whose_publish_raises_leaves_no_source_behind():
    class _Broken(InMemoryMessageBroker):
        async def publish(self, agent_id: str, message: Any) -> Any:
            raise RuntimeError('broker down')

    runtime, recorder = _traced(broker=_Broken())
    try:
        agent_id = await runtime.spawn(_factory('Root', ScriptedLLM([])))
        with _host_span(runtime), pytest.raises(RuntimeError, match='broker down'):
            await runtime.send(agent_id, 'x', activate=False)
        sources = _sources(runtime)
    finally:
        await runtime.shutdown()

    assert sources == {}
    assert recorder.events == []


async def test_a_deferred_message_keeps_its_source_across_a_kill_and_links_once_revived():
    """延迟消息在轮边界被取出、暂存进控制块；周期被 kill 时邮箱摘掉，暂存的那条随唤醒注入。"""
    runtime, recorder = _traced()
    first = asyncio.Event()
    second = asyncio.Event()
    release_first = asyncio.Event()

    class _TwoStage(Tool):
        __tool_name__ = 'TwoStage'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """第一次等放开，第二次一直挂着。"""
            if not first.is_set():
                first.set()
                await release_first.wait()
                return ToolResult(output='first')
            second.set()
            await asyncio.Event().wait()
            return ToolResult(output='never')

    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('a', 'TwoStage')]),
            ScriptedTurn(tool_calls=[_call('b', 'TwoStage')]),
            ScriptedTurn(content='revived'),
        ]
    )
    try:
        agent_id = await runtime.spawn(_factory('Root', llm, {'TwoStage': _TwoStage()}), query='go')
        await asyncio.wait_for(first.wait(), _TIMEOUT)
        with _host_span(runtime):
            await runtime.send(
                agent_id, 'for the next cycle', activate=False, trigger=Trigger.DEFERRED
            )
        release_first.set()
        await asyncio.wait_for(second.wait(), _TIMEOUT)
        await runtime.kill(agent_id)
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        await asyncio.sleep(0.01)
        kept = _sources(runtime)
        await runtime.send(agent_id, 'wake up')
        await asyncio.wait_for(
            _until(lambda: span_id_of(SpanKind.AGENT, agent_id, 2) in recorder.ended), _TIMEOUT
        )
    finally:
        await runtime.shutdown()

    assert [receiver for _, receiver in kept.values()] == [agent_id]
    [edge] = recorder.edges
    assert edge.target_span_id == span_id_of(SpanKind.TURN, agent_id, 3)


async def test_messages_left_in_a_mailbox_that_a_finished_cycle_removes_drop_their_sources():
    runtime, _ = _traced()
    blocks = _Blocks()
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('b', 'Blocks')])])
    try:
        agent_id = await runtime.spawn(
            _factory('Root', llm, {'Blocks': blocks}, max_turns=1), query='go'
        )
        await asyncio.wait_for(blocks.entered.wait(), _TIMEOUT)
        with _host_span(runtime):
            for n in range(3):
                await runtime.send(agent_id, f'unread {n}', activate=False)
        blocks.release.set()
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        sources = _sources(runtime)
    finally:
        await runtime.shutdown()

    assert sources == {}


def test_forgetting_a_receiver_leaves_other_receivers_and_the_kept_ids():
    recorder = _Recorder()
    tracing = _Tracing(recorder)
    with tracing.span(SpanKind.TURN, ('a0', 1), name='turn', agent_id='a0'):
        for message_id, receiver in [('m1', 'a1'), ('m2', 'a1'), ('m3', 'a2')]:
            tracing.sending(message_id, receiver)

    tracing.forget_receiver('a1', keep={'m2'})

    assert sorted(tracing._sent_from) == ['m2', 'm3']  # pyright: ignore[reportPrivateUsage]


# --- review: tracer failures, swaps and statuses ----------------------------------------------


async def test_a_tracer_that_raises_cancellation_while_a_span_ends_does_not_wedge_the_agent():
    class _CancelsOnEnd(_Recorder):
        def end_span(
            self, span_id: str, status: SpanStatus, attributes: dict[str, AttributeValue]
        ) -> None:
            raise asyncio.CancelledError()

    tracer = _CancelsOnEnd()
    runtime = Runtime()
    runtime._set_tracer(tracer)  # pyright: ignore[reportPrivateUsage]
    try:
        agent_id = await runtime.spawn(
            _factory('Root', ScriptedLLM([ScriptedTurn(content='x')])), query='go'
        )
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        info = await runtime.find(agent_id)
        failures = runtime._tracing.failures  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()

    assert context.stop_reason is StopReason.COMPLETED
    assert info is not None and info.status.value == 'zombie'
    assert failures == 3


@pytest.mark.parametrize(
    ('error', 'status'),
    [
        (ValueError('x'), SpanStatus.ERROR),
        (asyncio.CancelledError(), SpanStatus.CANCELLED),
        (GeneratorExit(), SpanStatus.CANCELLED),
        (KeyboardInterrupt(), SpanStatus.CANCELLED),
        (SystemExit(1), SpanStatus.CANCELLED),
    ],
    ids=['error', 'cancelled', 'generator-exit', 'keyboard-interrupt', 'system-exit'],
)
def test_how_a_span_ends_follows_what_ended_it(error: BaseException, status: SpanStatus):
    recorder = _Recorder()
    tracing = _Tracing(recorder)

    with (
        pytest.raises(type(error)),
        tracing.span(SpanKind.TURN, ('a1', 1), name='t', agent_id='a1'),
    ):
        raise error

    assert recorder.ended[span_id_of(SpanKind.TURN, 'a1', 1)][0] is status


@pytest.mark.parametrize('kind', ['generator', 'async-generator', 'future'])
async def test_a_tracer_method_that_returns_something_to_iterate_or_await_is_a_failure(kind: str):
    def generator() -> Iterator[None]:
        yield None

    async def agen() -> Any:
        yield None

    made = {
        'generator': generator,
        'async-generator': agen,
        'future': lambda: asyncio.get_running_loop().create_future(),
    }[kind]

    class _Odd:
        def add_edge(self, edge: CausalityEdge) -> object:
            return made()

    tracing = _Tracing(cast(Tracer, _Odd()))
    tracing.call('add_edge', lambda tracer: tracer.add_edge(cast(Any, None)))

    assert tracing.failures == 1


async def test_a_single_tracer_failure_is_still_totalled_at_shutdown(
    caplog: pytest.LogCaptureFixture,
):
    class _FailsOnce(_Recorder):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def add_edge(self, edge: CausalityEdge) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError('once')

    runtime = Runtime(
        orchestration=True,
        agents={'Child': _factory('Child', ScriptedLLM([ScriptedTurn(content='c')]))},
    )
    runtime._set_tracer(_FailsOnce())  # pyright: ignore[reportPrivateUsage]
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('d', 'Delegate', agent='Child', query='go')]),
            ScriptedTurn(content='ok'),
        ]
    )
    try:
        await _run_one(runtime, _factory('Parent', llm))
    finally:
        await runtime.shutdown()

    totals = [
        r
        for r in caplog.records
        if r.name == 'syndica.kernel.runtime' and '一共失败了' in r.getMessage()
    ]
    assert [record.args for record in totals] == [('_FailsOnce', 1)]


async def test_failures_while_shutdown_cancels_the_agents_are_in_the_total(
    caplog: pytest.LogCaptureFixture,
):
    class _FailsOnCancelled(_Recorder):
        def end_span(
            self, span_id: str, status: SpanStatus, attributes: dict[str, AttributeValue]
        ) -> None:
            super().end_span(span_id, status, attributes)
            if status is SpanStatus.CANCELLED:
                raise RuntimeError('cannot record a cancellation')

    runtime = Runtime()
    runtime._set_tracer(_FailsOnCancelled())  # pyright: ignore[reportPrivateUsage]
    blocks = _Blocks()
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('b', 'Blocks')])])
    agent_id = await runtime.spawn(_factory('Root', llm, {'Blocks': blocks}), query='go')
    await asyncio.wait_for(blocks.entered.wait(), _TIMEOUT)
    await runtime.shutdown()

    totals = [
        r
        for r in caplog.records
        if r.name == 'syndica.kernel.runtime' and '一共失败了' in r.getMessage()
    ]
    assert [record.args for record in totals] == [('_FailsOnCancelled', 3)]
    assert agent_id


async def test_the_tracer_cannot_be_swapped_while_spans_are_open():
    runtime, recorder = _traced()
    blocks = _Blocks()
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('b', 'Blocks')]), ScriptedTurn(content='ok')])
    try:
        agent_id = await runtime.spawn(_factory('Root', llm, {'Blocks': blocks}), query='go')
        await asyncio.wait_for(blocks.entered.wait(), _TIMEOUT)
        with pytest.raises(ConfigError) as caught:
            runtime._set_tracer(_Recorder())  # pyright: ignore[reportPrivateUsage]
        blocks.release.set()
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        runtime._set_tracer(_Recorder())  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()

    assert '3 个 span' in caught.value.received
    assert recorder.all_ended()


async def test_a_swap_reports_the_old_tracers_failures_and_starts_the_new_one_from_zero(
    caplog: pytest.LogCaptureFixture,
):
    broken = _Broken(RuntimeError('old'))
    runtime = Runtime()
    runtime._set_tracer(cast(Tracer, broken))  # pyright: ignore[reportPrivateUsage]
    try:
        await _run_one(runtime, _factory('Root', ScriptedLLM([ScriptedTurn(content='x')])))
        runtime._set_tracer(_Recorder())  # pyright: ignore[reportPrivateUsage]
        after = runtime._tracing.failures  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()

    totals = [
        r
        for r in caplog.records
        if r.name == 'syndica.kernel.runtime' and '一共失败了' in r.getMessage()
    ]
    assert [record.args for record in totals] == [('_Broken', broken.calls)]
    assert after == 0


async def test_two_teardowns_of_one_agent_report_its_despawn_once():
    runtime, recorder = _traced()
    doomed = await runtime.spawn(_factory('Doomed', ScriptedLLM([])))

    class _RemoveTwice(Tool):
        __tool_name__ = 'RemoveTwice'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """同时销毁同一个 Agent 两次。"""
            await asyncio.gather(runtime.despawn(doomed), runtime.despawn(doomed))
            return ToolResult(output='gone')

    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('r', 'RemoveTwice')]), ScriptedTurn(content='ok')]
    )
    try:
        await _run_one(runtime, _factory('Remover', llm, {'RemoveTwice': _RemoveTwice()}))
    finally:
        await runtime.shutdown()

    assert [name for _, name, _ in recorder.events] == ['agent.despawned']


# --- review: the windows that must not suspend ------------------------------------------------


class _WatchedTable(LocalAgentTable[BaseAgent]):
    """put 时排一个回调：任务在 put 与 set_task 之间若让出过事件循环，回调就会先于 set_task 跑。"""

    def __init__(self) -> None:
        super().__init__()
        self.violations: list[str] = []

    def put(self, agent_id: str, agent: BaseAgent, task: asyncio.Task[None] | None = None) -> None:
        super().put(agent_id, agent, task)

        def check() -> None:
            if self.get_task(agent_id) is None:
                self.violations.append(agent_id)

        asyncio.get_running_loop().call_soon(check)


async def test_nothing_suspends_between_registering_a_spawn_and_starting_its_task():
    runtime, _ = _traced(
        orchestration=True,
        agents={'Child': _factory('Child', ScriptedLLM([ScriptedTurn(content='c')]))},
    )
    table = _WatchedTable()
    runtime._table = table  # pyright: ignore[reportPrivateUsage]
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('d', 'Delegate', agent='Child', query='go')]),
            ScriptedTurn(content='ok'),
        ]
    )
    try:
        await _run_one(runtime, _factory('Parent', llm))
    finally:
        await runtime.shutdown()

    assert table.violations == []


async def test_nothing_suspends_between_starting_a_delegation_wait_and_installing_its_token():
    violations: list[str] = []
    runtime_box: list[Runtime] = []

    class _Watching(_Recorder):
        def start_span(
            self,
            span_id: str,
            parent_span_id: str | None,
            kind: SpanKind,
            name: str,
            agent_id: str,
            attributes: dict[str, AttributeValue],
        ) -> None:
            super().start_span(span_id, parent_span_id, kind, name, agent_id, attributes)
            if kind is SpanKind.WAIT and attributes.get('wait.kind') == 'agent':
                slots = runtime_box[0]._slots  # pyright: ignore[reportPrivateUsage]

                def check() -> None:
                    if slots[agent_id].delegation is None:
                        violations.append(span_id)

                asyncio.get_running_loop().call_soon(check)

    runtime = Runtime(
        orchestration=True,
        agents={'Child': _factory('Child', ScriptedLLM([ScriptedTurn(content='c')]))},
    )
    runtime_box.append(runtime)
    runtime._set_tracer(_Watching())  # pyright: ignore[reportPrivateUsage]
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('d', 'Delegate', agent='Child', query='go')]),
            ScriptedTurn(content='ok'),
        ]
    )
    try:
        await _run_one(runtime, _factory('Parent', llm))
    finally:
        await runtime.shutdown()

    assert violations == []


# --- T8: what the backend reported about the answer --------------------------------------------


class _Reporting:
    """按剧本交回带 `extra` 的回复；剧本里的异常就抛。"""

    def __init__(self, model: str, script: list[BaseException | dict[str, Any]]) -> None:
        self.config = LLMConfig(provider='fake', model=model)
        self._script = list(script)

    async def create(self, messages: list[Any], **kwargs: Any) -> Any:
        from syndica.kernel.context import ContextBlock

        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return ContextBlock(role='assistant', content='answer', extra=dict(outcome))


async def test_an_attempt_records_which_platform_answered_and_the_token_counts():
    llm = _Reporting(
        'routed',
        [
            {
                'llm_route': 'gemini',
                'usage': {'input_tokens': 120, 'output_tokens': 7, 'cached_input_tokens': None},
            }
        ],
    )
    runtime, recorder = _traced()
    try:
        agent_id = await _run_one(runtime, _factory('Root', cast(Any, llm)))
    finally:
        await runtime.shutdown()

    assert recorder.attributes(span_id_of(SpanKind.LLM_CALL, agent_id, 1, 1)) == {
        'llm.attempt': 1,
        'llm.model': 'routed',
        'llm.platform': 'gemini',
        'llm.input_tokens': 120,
        'llm.output_tokens': 7,
        'llm.cached_input_tokens': None,
    }


async def test_what_a_backend_reports_in_the_wrong_shape_is_not_recorded():
    llm = _Reporting(
        'odd',
        [{'llm_route': '', 'usage': {'input_tokens': True, 'output_tokens': '7', 'other': 1}}],
    )
    runtime, recorder = _traced()
    try:
        agent_id = await _run_one(runtime, _factory('Root', cast(Any, llm)))
    finally:
        await runtime.shutdown()

    assert recorder.attributes(span_id_of(SpanKind.LLM_CALL, agent_id, 1, 1)) == {
        'llm.attempt': 1,
        'llm.model': 'odd',
        'llm.cached_input_tokens': None,
    }


async def test_each_retry_and_fallback_records_its_own_answer():
    original = _Reporting(
        'main', [RuntimeError('down'), {'llm_route': 'main-b', 'usage': {'input_tokens': 5}}]
    )
    backup = _Reporting('backup', [{'llm_route': 'backup-a', 'usage': {'output_tokens': 9}}])

    async def no_sleep(seconds: float) -> None:
        return None

    first_runtime, first = _traced(
        modules=[RetryModule({}, sleep=no_sleep)], flags={'llm_retry_attempts': 1}
    )
    try:
        first_agent = await _run_one(first_runtime, _factory('Root', cast(Any, original)))
    finally:
        await first_runtime.shutdown()
    second_runtime, second = _traced(
        modules=[RetryModule({'backup': cast(Any, backup)}, sleep=no_sleep)],
        flags={'llm_fallbacks': 'backup'},
    )
    failing = _Reporting('main', [RuntimeError('down')])
    try:
        second_agent = await _run_one(second_runtime, _factory('Root', cast(Any, failing)))
    finally:
        await second_runtime.shutdown()

    retried = first.attributes(span_id_of(SpanKind.LLM_CALL, first_agent, 1, 2))
    assert (retried['llm.platform'], retried['llm.input_tokens']) == ('main-b', 5)
    assert 'llm.platform' not in first.attributes(span_id_of(SpanKind.LLM_CALL, first_agent, 1, 1))
    fallback = second.attributes(span_id_of(SpanKind.LLM_CALL, second_agent, 1, 2))
    assert (fallback['llm.model'], fallback['llm.platform'], fallback['llm.output_tokens']) == (
        'backup',
        'backup-a',
        9,
    )


async def _attempt_attributes(llm: object) -> dict[str, AttributeValue]:
    runtime, recorder = _traced()
    try:
        agent_id = await _run_one(runtime, _factory('Root', cast(Any, llm)))
    finally:
        await runtime.shutdown()
    return recorder.attributes(span_id_of(SpanKind.LLM_CALL, agent_id, 1, 1))


async def test_token_counts_outside_int64_or_negative_are_not_recorded():
    attributes = await _attempt_attributes(
        _Reporting(
            'big',
            [
                {
                    'usage': {
                        'input_tokens': 2**63,
                        'output_tokens': -5,
                        'cached_input_tokens': 2**63 - 1,
                    }
                }
            ],
        )
    )

    assert 'llm.input_tokens' not in attributes and 'llm.output_tokens' not in attributes
    assert attributes['llm.cached_input_tokens'] == 2**63 - 1


async def test_a_route_that_is_not_text_is_not_recorded():
    attributes = await _attempt_attributes(_Reporting('odd', [{'llm_route': 7}]))

    assert 'llm.platform' not in attributes


async def test_usage_given_as_any_mapping_is_read():
    from types import MappingProxyType

    attributes = await _attempt_attributes(
        _Reporting('proxy', [{'usage': MappingProxyType({'input_tokens': 3})}])
    )

    assert attributes['llm.input_tokens'] == 3


async def test_each_platform_failed_over_from_is_an_event_on_the_attempt():
    runtime, recorder = _traced()
    llm = _Reporting(
        'routed',
        [
            {
                'llm_route': 'groq',
                'llm_failures': [
                    {'platform': 'gemini', 'error': 'ProviderHTTPError', 'status': 429},
                    {'platform': 'openrouter', 'error': 'TimeoutError', 'status': '503'},
                    {'platform': '', 'error': 'X', 'status': 1},
                    'not a record',
                ],
            }
        ],
    )
    try:
        agent_id = await _run_one(runtime, _factory('Root', cast(Any, llm)))
    finally:
        await runtime.shutdown()

    attempt = span_id_of(SpanKind.LLM_CALL, agent_id, 1, 1)
    assert [(span, name, attributes) for span, name, attributes in recorder.events] == [
        (
            attempt,
            'llm.platform_failed',
            {'llm.platform': 'gemini', 'llm.error': 'ProviderHTTPError', 'llm.status': 429},
        ),
        (
            attempt,
            'llm.platform_failed',
            {'llm.platform': 'openrouter', 'llm.error': 'TimeoutError', 'llm.status': None},
        ),
    ]


async def test_after_a_failover_the_attempt_names_the_model_of_the_platform_that_answered():
    from syndica.backends.llm_router import ModelRouter

    class _Member:
        def __init__(self, model: str, outcome: BaseException | None) -> None:
            self.config = LLMConfig(provider='fake', model=model)
            self._outcome = outcome

        async def create(self, messages: list[Any], **kwargs: Any) -> Any:
            from syndica.kernel.context import ContextBlock

            if self._outcome is not None:
                raise self._outcome
            return ContextBlock(role='assistant', content='answer')

    async def no_sleep(seconds: float) -> None:
        return None

    router = ModelRouter(
        [
            ('gemini', cast(Any, _Member('gemini-model', RuntimeError('503')))),
            ('openrouter', cast(Any, _Member('openrouter-model', None))),
        ],
        sleep=no_sleep,
    )
    attributes = await _attempt_attributes(router)

    assert (attributes['llm.model'], attributes['llm.platform']) == (
        'openrouter-model',
        'openrouter',
    )


async def test_a_reply_without_a_readable_extra_records_nothing_and_logs_nothing(
    caplog: pytest.LogCaptureFixture,
):
    class _NoExtra:
        config = LLMConfig(provider='fake', model='plain')

        async def create(self, messages: list[Any], **kwargs: Any) -> Any:
            from syndica.kernel.context import ContextBlock

            reply = ContextBlock(role='assistant', content='answer')
            object.__setattr__(reply, 'extra', None)
            return reply

    attributes = await _attempt_attributes(_NoExtra())

    assert attributes == {'llm.attempt': 1, 'llm.model': 'plain'}
    assert [r for r in caplog.records if r.name == 'syndica.kernel.tracing'] == []


async def test_a_reply_whose_extra_cannot_be_read_is_paid_for_once_and_reported(
    caplog: pytest.LogCaptureFixture,
):
    class _Exploding(dict[str, Any]):
        def get(self, key: str, default: Any = None) -> Any:
            raise RuntimeError('cannot read')

    calls: list[int] = []

    class _Odd:
        config = LLMConfig(provider='fake', model='odd')

        async def create(self, messages: list[Any], **kwargs: Any) -> Any:
            from syndica.kernel.context import ContextBlock

            calls.append(1)
            reply = ContextBlock(role='assistant', content='answer')
            object.__setattr__(reply, 'extra', _Exploding())
            return reply

    async def no_sleep(seconds: float) -> None:
        return None

    runtime, _ = _traced(modules=[RetryModule({}, sleep=no_sleep)], flags={'llm_retry_attempts': 1})
    try:
        agent_id = await runtime.spawn(_factory('Root', cast(Any, _Odd())), query='go')
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert context.stop_reason is StopReason.COMPLETED
    assert calls == [1]
    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.tracing']
    assert cast(tuple[object, ...], record.args)[0] == 'RuntimeError'


def test_observing_outside_a_model_call_does_nothing(caplog: pytest.LogCaptureFixture):
    with llm_attempt(ScriptedLLM([])) as attempt:
        attempt.observe(ContextBlock(role='assistant', content='x', extra={'llm_route': 'a'}))

    assert caplog.records == []


# --- replay channels -------------------------------------------------------------------------


async def _no_sleep(seconds: float) -> None:
    return None


def _outcomes(
    recorder: _Recorder,
) -> list[tuple[str, str | None, BaseException | None]]:
    return [
        (span, None if reply is None else cast(str | None, reply.content), error)
        for span, _, reply, error in recorder.exchanges
    ]


async def test_each_model_call_hands_over_the_request_it_sent_and_the_reply_it_got():
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('c', 'Echo')]), ScriptedTurn(content='ok')])
    runtime, recorder = _traced()
    try:
        agent_id = await _run_one(runtime, _factory('Root', llm, {'Echo': _Echo()}))
    finally:
        await runtime.shutdown()

    assert _outcomes(recorder) == [
        (span_id_of(SpanKind.LLM_CALL, agent_id, 1, 1), None, None),
        (span_id_of(SpanKind.LLM_CALL, agent_id, 2, 1), 'ok', None),
    ]
    first_reply = cast(ContextBlock, recorder.exchanges[0][2])
    assert [call.name for call in first_reply.tool_calls] == ['Echo']
    for (_, request, _, _), sent, sent_kwargs in zip(
        recorder.exchanges, llm.calls, llm.call_kwargs, strict=True
    ):
        assert list(request) == ['messages', 'tools']
        messages = cast(list[ContextBlock], request['messages'])
        assert [(block.role, block.content) for block in messages] == [
            (block.role, block.content) for block in sent
        ]
        assert request['tools'] == sent_kwargs['tools']


async def test_a_failed_model_call_hands_over_its_error_and_each_retry_its_own_outcome():
    error = RuntimeError('rate limited')
    llm = ScriptedLLM([ScriptedTurn(raises=error), ScriptedTurn(content='finally')])
    runtime, recorder = _traced(
        modules=[RetryModule({}, sleep=_no_sleep)], flags={'llm_retry_attempts': 1}
    )
    try:
        agent_id = await _run_one(runtime, _factory('Root', llm))
    finally:
        await runtime.shutdown()

    assert _outcomes(recorder) == [
        (span_id_of(SpanKind.LLM_CALL, agent_id, 1, 1), None, error),
        (span_id_of(SpanKind.LLM_CALL, agent_id, 1, 2), 'finally', None),
    ]
    assert [_sent(request) for _, request, _, _ in recorder.exchanges] == [
        [('system', ''), ('user', 'go')]
    ] * 2


def _sent(request: Mapping[str, Any]) -> list[tuple[str, object]]:
    return [(block.role, block.content) for block in cast(list[ContextBlock], request['messages'])]


async def test_a_fallback_model_hands_over_the_same_request_and_its_own_reply():
    error = RuntimeError('main is down')
    main = ScriptedLLM([ScriptedTurn(raises=error)])
    backup = ScriptedLLM([ScriptedTurn(content='from backup')])
    runtime, recorder = _traced(
        modules=[RetryModule({'backup': cast(Any, backup)}, sleep=_no_sleep)],
        flags={'llm_fallbacks': 'backup'},
    )
    try:
        agent_id = await _run_one(runtime, _factory('Root', main))
    finally:
        await runtime.shutdown()

    assert _outcomes(recorder) == [
        (span_id_of(SpanKind.LLM_CALL, agent_id, 1, 1), None, error),
        (span_id_of(SpanKind.LLM_CALL, agent_id, 1, 2), 'from backup', None),
    ]
    assert [_sent(request) for _, request, _, _ in recorder.exchanges] == [
        [('system', ''), ('user', 'go')]
    ] * 2


class _HangingModel:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.config = LLMConfig(provider='fake', model='hangs')

    async def create(self, messages: list[Any], **kwargs: Any) -> Any:
        self.entered.set()
        await asyncio.Event().wait()


async def test_a_model_call_cut_off_by_a_kill_hands_over_nothing():
    llm = _HangingModel()
    runtime, recorder = _traced()
    try:
        agent_id = await runtime.spawn(_factory('Root', cast(Any, llm)), query='go')
        await asyncio.wait_for(llm.entered.wait(), _TIMEOUT)
        await asyncio.wait_for(runtime.kill(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    [span] = recorder.of(SpanKind.LLM_CALL)
    assert recorder.status(span) is SpanStatus.CANCELLED
    assert recorder.exchanges == []


async def test_a_tool_hands_over_its_own_result_and_not_what_post_tool_use_made_of_it():
    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('c', 'Echo', text='original')]), ScriptedTurn(content='ok')]
    )

    async def rewrite(**_: Any) -> HookResult:
        return HookResult(modified={'result': ToolResult(output='rewritten')})

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm, {'Echo': _Echo()})(hooks, events)
        agent.register_hook(Junction.POST_TOOL_USE, rewrite)
        return agent

    runtime, recorder = _traced()
    try:
        agent_id = await _run_one(runtime, build)
    finally:
        await runtime.shutdown()

    [(span, result, error)] = recorder.tool_results
    assert (span, cast(ToolResult, result).output, error) == (
        span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0),
        'original',
        None,
    )
    assert llm.calls[1][-1].content == 'rewritten'


@pytest.mark.parametrize(
    'error',
    [RuntimeError('disk on fire'), ConfigError(expected='e', received='r', next_action='n')],
    ids=['exception', 'syndica-error'],
)
async def test_a_tool_that_raises_hands_over_the_exception_it_raised(error: Exception):
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('c', 'Raises')]), ScriptedTurn(content='ok')])
    runtime, recorder = _traced()
    try:
        agent_id = await _run_one(runtime, _factory('Root', llm, {'Raises': _Raises(error)}))
    finally:
        await runtime.shutdown()

    assert recorder.tool_results == [
        (span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0), None, error)
    ]


class _ReturnsText(Tool):
    __tool_name__ = 'ReturnsText'
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        """交回一个不是 ToolResult 的值。"""
        return cast(ToolResult, 'plain text')


async def _skip_echo(**_: Any) -> HookResult:
    return HookResult(skip=True, result=ToolResult(output='skipped by the host'))


@pytest.mark.parametrize(
    'call',
    [
        _call('c', 'Nope'),
        ToolCall(id='c', name='Echo', arguments='{not json'),
        _call('c', 'ReturnsText'),
    ],
    ids=['unknown-tool', 'bad-arguments', 'not-a-tool-result'],
)
async def test_a_call_whose_tool_gave_back_no_result_hands_over_nothing(call: ToolCall):
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[call]), ScriptedTurn(content='ok')])
    tools: dict[str, Tool] = {'Echo': _Echo(), 'ReturnsText': _ReturnsText()}
    runtime, recorder = _traced()
    try:
        await _run_one(runtime, _factory('Root', llm, tools))
    finally:
        await runtime.shutdown()

    assert len(recorder.of(SpanKind.TOOL_CALL)) == 1
    assert recorder.tool_results == []


async def test_a_skipped_call_hands_over_the_result_that_skipped_it():
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('c', 'Echo')]), ScriptedTurn(content='ok')])

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm, {'Echo': _Echo()})(hooks, events)
        agent.register_hook(Junction.PRE_TOOL_USE, _skip_echo)
        return agent

    runtime, recorder = _traced()
    try:
        agent_id = await _run_one(runtime, build)
    finally:
        await runtime.shutdown()

    span = span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, 0)
    [(handed, result, error)] = recorder.tool_results
    assert (handed, cast(ToolResult, result).output, error) == (span, 'skipped by the host', None)
    assert recorder.attributes(span)['tool.outcome'] == 'skipped'


class _ResultKind(ToolResult):
    pass


class _ReturnsSubclass(Tool):
    __tool_name__ = 'ReturnsSubclass'
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        """交回一个 ToolResult 子类的实例。"""
        return _ResultKind(output='from a subclass')


async def test_a_tool_result_subclass_is_handed_over_like_a_tool_result():
    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('c', 'ReturnsSubclass')]), ScriptedTurn(content='ok')]
    )
    runtime, recorder = _traced()
    try:
        await _run_one(runtime, _factory('Root', llm, {'ReturnsSubclass': _ReturnsSubclass()}))
    finally:
        await runtime.shutdown()

    [(_, result, _)] = recorder.tool_results
    assert isinstance(result, _ResultKind) and result.output == 'from a subclass'


async def test_a_wait_on_an_agent_this_runtime_does_not_hold_opens_no_span():
    runtime, recorder = _traced()
    try:
        with pytest.raises(ContractViolation):
            await runtime.wait_for('someone-else', Reply(correlation_id='q', timeout=_TIMEOUT))
    finally:
        await runtime.shutdown()

    assert recorder.started == {}
