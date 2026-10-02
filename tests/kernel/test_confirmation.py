"""Single-call confirmation and its exclusive execution interval."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any, cast

import pytest

from syndica.kernel import (
    AgentOptions,
    BaseAgent,
    ConfigError,
    ConfirmationOutcome,
    ConfirmationRequest,
    Disposition,
    EventBus,
    HooksCenter,
    MailboxConfirmationHandler,
    Message,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    Tool,
    ToolCall,
    ToolResult,
    TrustLevel,
    WaitResult,
    WaitState,
)
from syndica.kernel.context import Context
from syndica.kernel.danger import NO_RULE_MATCHED, DangerRequest
from syndica.kernel.hooks import HookResult, Junction
from syndica.kernel.tool_executor import ToolExecutor, _ExecutionGate
from syndica.kernel.waiting import Confirmation

_TIMEOUT = 2.0


class _ConfirmNamed:
    def __init__(self, name: str = 'Confirmable') -> None:
        self.name = name
        self.seen: list[DangerRequest] = []

    def decide(self, request: DangerRequest) -> Disposition:
        self.seen.append(request)
        return Disposition.CONFIRM if request.call.name == self.name else Disposition.ALLOW


class _Recorder(Tool):
    __tool_name__ = 'Confirmable'
    __tool_command_args__ = ()

    def __init__(self) -> None:
        self.values: list[str] = []

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        return True

    async def execute(self, value: str) -> ToolResult:
        self.values.append(value)
        return ToolResult(output=value)


def _configured(
    tool: Tool,
    policy: _ConfirmNamed,
    handler,
    *,
    hooks: HooksCenter | None = None,
) -> ToolExecutor:
    executor = ToolExecutor(
        tools={tool.name: tool},
        hooks=hooks if hooks is not None else HooksCenter(),
        policy=policy,
    )
    executor._configure_confirmation(  # pyright: ignore[reportPrivateUsage]
        'agent-1',
        handler,
    )
    return executor


def test_confirmation_public_values_and_field_order_are_exact() -> None:
    assert {item.name: item.value for item in ConfirmationOutcome} == {
        'APPROVED': 'approved',
        'REJECTED': 'rejected',
        'TIMEOUT': 'timeout',
        'PREEMPTED': 'preempted',
        'ABORTED': 'aborted',
    }
    assert [field.name for field in dataclasses.fields(ConfirmationRequest)] == [
        'request_id',
        'agent_id',
        'tool_call_id',
        'tool_name',
        'arguments',
        'verdict',
        'request_ref',
    ]


def test_confirmation_handler_and_timeout_are_validated_at_construction() -> None:
    with pytest.raises(ConfigError) as class_exc:
        Runtime(confirmation_handler=cast(Any, MailboxConfirmationHandler))
    assert 'confirmation_handler' in str(class_exc.value)
    assert '实例' in str(class_exc.value)

    with pytest.raises(ConfigError) as timeout_exc:
        MailboxConfirmationHandler(timeout=True)
    assert 'timeout' in str(timeout_exc.value)
    assert 'bool' in str(timeout_exc.value)


class _NotCallableHandler:
    confirm = 1


class _SyncHandler:
    def confirm(self, runtime: object, request: object) -> None:
        del runtime, request


class _WrongSignatureHandler:
    async def confirm(self, request: object) -> None:
        del request


@pytest.mark.parametrize(
    'handler',
    [
        _NotCallableHandler(),
        _SyncHandler(),
        _WrongSignatureHandler(),
    ],
    ids=['not-callable', 'sync', 'wrong-signature'],
)
def test_invalid_confirmation_handler_shapes_fail_during_runtime_construction(
    handler: object,
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        Runtime(confirmation_handler=cast(Any, handler))
    assert 'confirmation_handler' in excinfo.value.expected
    assert 'confirm' in excinfo.value.received
    assert 'async' in excinfo.value.next_action


class _MailboxRuntime:
    def __init__(self, result: WaitResult) -> None:
        self.events = EventBus()
        self.result = result
        self.waiters: list[tuple[str, Confirmation]] = []

    async def wait_for(self, agent_id: str, waiter: Confirmation) -> WaitResult:
        self.waiters.append((agent_id, waiter))
        return self.result


def _request() -> ConfirmationRequest:
    return ConfirmationRequest(
        request_id='nonce-1',
        agent_id='agent-1',
        tool_call_id='call-7',
        tool_name='Confirmable',
        arguments='{"value":"x"}',
        verdict=NO_RULE_MATCHED,
    )


@pytest.mark.parametrize(
    ('state', 'outcome'),
    [
        (WaitState.TIMEOUT, ConfirmationOutcome.TIMEOUT),
        (WaitState.PREEMPTED, ConfirmationOutcome.PREEMPTED),
        (WaitState.ABORTED, ConfirmationOutcome.ABORTED),
    ],
)
async def test_mailbox_handler_preserves_each_unanswered_state(
    state: WaitState,
    outcome: ConfirmationOutcome,
) -> None:
    runtime = _MailboxRuntime(WaitResult(state=state))
    events = runtime.events.subscribe()
    result = await MailboxConfirmationHandler(timeout=7).confirm(runtime, _request())
    event = await asyncio.wait_for(anext(events), timeout=_TIMEOUT)
    events.close()
    assert result is outcome
    assert event.kind == 'tool_confirmation_requested'
    assert event.payload == {
        'request_id': 'nonce-1',
        'tool_call_id': 'call-7',
        'tool_name': 'Confirmable',
        'arguments': '{"value":"x"}',
        'verdict': {
            'rule_id': 'danger.no_rule_matched',
            'level': 'none',
            'reason': NO_RULE_MATCHED.reason,
        },
    }
    assert runtime.waiters[0][0] == 'agent-1'
    assert runtime.waiters[0][1].tool_call_id == 'nonce-1'
    assert runtime.waiters[0][1].timeout == 7


async def test_mailbox_handler_accepts_only_the_explicit_approve_word() -> None:
    for content, expected in (
        ('approve', ConfirmationOutcome.APPROVED),
        (' APPROVE ', ConfirmationOutcome.APPROVED),
        ('yes', ConfirmationOutcome.REJECTED),
        ('reject', ConfirmationOutcome.REJECTED),
    ):
        runtime = _MailboxRuntime(
            WaitResult(
                state=WaitState.ANSWERED,
                message=Message(
                    sender='human',
                    receiver='agent-1',
                    content=content,
                    trust=TrustLevel.TRUSTED,
                ),
            )
        )
        assert await MailboxConfirmationHandler().confirm(runtime, _request()) is expected


async def test_policy_and_handler_each_run_once_and_see_the_rewritten_arguments() -> None:
    tool = _Recorder()
    policy = _ConfirmNamed()
    hooks = HooksCenter()
    requests: list[ConfirmationRequest] = []

    async def rewrite(**_: Any) -> HookResult:
        return HookResult(modified={'args': {'value': 'rewritten'}})

    async def approve(request: ConfirmationRequest) -> ConfirmationOutcome:
        requests.append(request)
        return ConfirmationOutcome.APPROVED

    hooks.register(Junction.PRE_TOOL_USE, rewrite)
    executor = _configured(tool, policy, approve, hooks=hooks)
    await executor.execute_tool_calls(
        Context(),
        [ToolCall(id='call-7', name='Confirmable', arguments='{"value":"original"}')],
    )

    assert len(policy.seen) == 1
    assert len(requests) == 1
    request = requests[0]
    assert request.agent_id == 'agent-1'
    assert request.tool_call_id == 'call-7'
    assert request.request_id != request.tool_call_id
    assert json.loads(request.arguments) == {'value': 'rewritten'}
    assert tool.values == ['rewritten']


async def test_confirmation_observer_cannot_mutate_the_arguments_that_are_later_executed() -> None:
    tool = _Recorder()
    hooks = HooksCenter()
    mutate = asyncio.Event()
    mutated = asyncio.Event()
    approve = asyncio.Event()
    confirmation_started = asyncio.Event()
    mutators: list[asyncio.Task[None]] = []
    requests: list[ConfirmationRequest] = []

    async def mutate_later(args: dict[str, Any], call: ToolCall) -> None:
        await mutate.wait()
        args['value'] = 'tampered'
        call.id = 'tampered-id'
        call.name = 'Tampered'
        mutated.set()

    async def observe(**kwargs: Any) -> None:
        mutators.append(asyncio.create_task(mutate_later(kwargs['args'], kwargs['tool_call'])))

    async def handler(request: ConfirmationRequest) -> ConfirmationOutcome:
        requests.append(request)
        confirmation_started.set()
        await approve.wait()
        return ConfirmationOutcome.APPROVED

    hooks.register(Junction.TOOL_CONFIRMATION_REQUIRED, observe)
    executor = _configured(tool, _ConfirmNamed(), handler, hooks=hooks)
    context = Context()
    running = asyncio.create_task(
        executor.execute_tool_calls(
            context,
            [ToolCall(id='c1', name='Confirmable', arguments='{"value":"approved"}')],
        )
    )
    await asyncio.wait_for(confirmation_started.wait(), timeout=_TIMEOUT)
    mutate.set()
    await asyncio.wait_for(mutated.wait(), timeout=_TIMEOUT)
    approve.set()
    await asyncio.wait_for(running, timeout=_TIMEOUT)
    await asyncio.gather(*mutators)

    assert json.loads(requests[0].arguments) == {'value': 'approved'}
    assert tool.values == ['approved']
    assert context.current_traj[0].tool_call_id == 'c1'


@pytest.mark.parametrize(
    ('outcome', 'word'),
    [
        (ConfirmationOutcome.REJECTED, '拒绝'),
        (ConfirmationOutcome.TIMEOUT, '超时'),
        (ConfirmationOutcome.PREEMPTED, '顶掉'),
        (ConfirmationOutcome.ABORTED, '中止'),
    ],
)
async def test_every_nonapproval_outcome_keeps_the_tool_unexecuted_and_distinct(
    outcome: ConfirmationOutcome, word: str
) -> None:
    tool = _Recorder()

    async def answer(_: ConfirmationRequest) -> ConfirmationOutcome:
        return outcome

    ctx = Context()
    await _configured(tool, _ConfirmNamed(), answer).execute_tool_calls(
        ctx,
        [ToolCall(id='call-7', name='Confirmable', arguments='{"value":"blocked"}')],
    )
    assert tool.values == []
    assert len(ctx.current_traj) == 1
    block = ctx.current_traj[0]
    assert block.tool_call_id == 'call-7'
    assert isinstance(block.content, str)
    assert word in block.content
    assert block.extra['tool_meta']['confirmation'] == outcome.value


async def test_reusing_a_model_tool_call_id_still_creates_one_time_confirmation_ids() -> None:
    tool = _Recorder()
    requests: list[ConfirmationRequest] = []

    async def approve(request: ConfirmationRequest) -> ConfirmationOutcome:
        requests.append(request)
        return ConfirmationOutcome.APPROVED

    executor = _configured(tool, _ConfirmNamed(), approve)
    call = ToolCall(id='reused-id', name='Confirmable', arguments='{"value":"once"}')
    await executor.execute_tool_calls(Context(), [call])
    await executor.execute_tool_calls(Context(), [call])
    assert [request.tool_call_id for request in requests] == ['reused-id', 'reused-id']
    assert requests[0].request_id != requests[1].request_id
    assert tool.values == ['once', 'once']


async def test_two_confirmations_for_one_agent_never_wait_concurrently() -> None:
    tool = _Recorder()
    first_waiting = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    peak = 0
    calls = 0

    async def approve(_: ConfirmationRequest) -> ConfirmationOutcome:
        nonlocal active, peak, calls
        calls += 1
        active += 1
        peak = max(peak, active)
        try:
            if calls == 1:
                first_waiting.set()
                await release_first.wait()
            return ConfirmationOutcome.APPROVED
        finally:
            active -= 1

    executor = _configured(tool, _ConfirmNamed(), approve)
    running = asyncio.create_task(
        executor.execute_tool_calls(
            Context(),
            [
                ToolCall(id='c1', name='Confirmable', arguments='{"value":"first"}'),
                ToolCall(id='c2', name='Confirmable', arguments='{"value":"second"}'),
            ],
        )
    )
    await asyncio.wait_for(first_waiting.wait(), timeout=_TIMEOUT)
    await asyncio.sleep(0)
    assert calls == 1
    assert peak == 1
    release_first.set()
    await asyncio.wait_for(running, timeout=_TIMEOUT)
    assert calls == 2
    assert peak == 1
    assert tool.values == ['first', 'second']


class _Window:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.entered = {name: asyncio.Event() for name in ('left', 'right', 'confirmed')}
        self.release_probes = asyncio.Event()
        self.snapshots: dict[str, frozenset[str]] = {}

    async def run(self, name: str) -> ToolResult:
        self.active.add(name)
        self.snapshots[name] = frozenset(self.active)
        self.entered[name].set()
        try:
            if name != 'confirmed':
                await self.release_probes.wait()
            return ToolResult(output=name)
        finally:
            self.active.remove(name)


class _Probe(Tool):
    __tool_name__ = 'Probe'
    __tool_command_args__ = ()

    def __init__(self, window: _Window) -> None:
        self.window = window

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        return True

    async def execute(self, name: str) -> ToolResult:
        return await self.window.run(name)


class _Confirmable(_Probe):
    __tool_name__ = 'Confirmable'


async def test_an_approved_call_executes_alone_between_concurrent_siblings() -> None:
    window = _Window()
    policy = _ConfirmNamed()
    confirmation_requested = asyncio.Event()
    approve = asyncio.Event()

    async def handler(_: ConfirmationRequest) -> ConfirmationOutcome:
        confirmation_requested.set()
        await approve.wait()
        return ConfirmationOutcome.APPROVED

    executor = ToolExecutor(
        tools={'Probe': _Probe(window), 'Confirmable': _Confirmable(window)},
        hooks=HooksCenter(),
        policy=policy,
        max_concurrency=4,
    )
    executor._configure_confirmation(  # pyright: ignore[reportPrivateUsage]
        'agent-1',
        handler,
    )
    running = asyncio.create_task(
        executor.execute_tool_calls(
            Context(),
            [
                ToolCall(id='c1', name='Probe', arguments='{"name":"left"}'),
                ToolCall(id='c2', name='Confirmable', arguments='{"name":"confirmed"}'),
                ToolCall(id='c3', name='Probe', arguments='{"name":"right"}'),
            ],
        )
    )
    await asyncio.wait_for(confirmation_requested.wait(), timeout=_TIMEOUT)
    await asyncio.wait_for(window.entered['left'].wait(), timeout=_TIMEOUT)
    await asyncio.wait_for(window.entered['right'].wait(), timeout=_TIMEOUT)
    assert window.active == {'left', 'right'}

    approve.set()
    await asyncio.sleep(0)
    assert window.entered['confirmed'].is_set() is False
    window.release_probes.set()
    await asyncio.wait_for(running, timeout=_TIMEOUT)

    assert window.snapshots['confirmed'] == frozenset({'confirmed'})
    assert [request.call.id for request in policy.seen] == ['c1', 'c2', 'c3']


async def test_an_approved_calls_post_hook_keeps_later_siblings_out() -> None:
    window = _Window()
    hooks = HooksCenter()
    confirmed_post_entered = asyncio.Event()
    release_post = asyncio.Event()

    async def approve(_: ConfirmationRequest) -> ConfirmationOutcome:
        return ConfirmationOutcome.APPROVED

    async def hold_confirmed_post(**kwargs: Any) -> HookResult:
        call = kwargs['tool_call']
        if call.name == 'Confirmable':
            confirmed_post_entered.set()
            await release_post.wait()
        return HookResult()

    hooks.register(Junction.POST_TOOL_USE, hold_confirmed_post)
    executor = ToolExecutor(
        tools={'Probe': _Probe(window), 'Confirmable': _Confirmable(window)},
        hooks=hooks,
        policy=_ConfirmNamed(),
        max_concurrency=4,
    )
    executor._configure_confirmation(  # pyright: ignore[reportPrivateUsage]
        'agent-1',
        approve,
    )
    running = asyncio.create_task(
        executor.execute_tool_calls(
            Context(),
            [
                ToolCall(id='c1', name='Confirmable', arguments='{"name":"confirmed"}'),
                ToolCall(id='c2', name='Probe', arguments='{"name":"right"}'),
            ],
        )
    )
    await asyncio.wait_for(confirmed_post_entered.wait(), timeout=_TIMEOUT)
    await asyncio.sleep(0)
    assert window.entered['right'].is_set() is False
    release_post.set()
    await asyncio.wait_for(window.entered['right'].wait(), timeout=_TIMEOUT)
    window.release_probes.set()
    await asyncio.wait_for(running, timeout=_TIMEOUT)


async def test_cancelling_an_exclusive_waiter_wakes_shared_waiters() -> None:
    gate = _ExecutionGate()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    later_entered = asyncio.Event()

    async def first_shared() -> None:
        async with gate.hold(exclusive=False):
            first_entered.set()
            await release_first.wait()

    async def exclusive() -> None:
        async with gate.hold(exclusive=True):
            pytest.fail('exclusive waiter should have been cancelled')

    async def later_shared() -> None:
        async with gate.hold(exclusive=False):
            later_entered.set()

    first = asyncio.create_task(first_shared())
    await asyncio.wait_for(first_entered.wait(), timeout=_TIMEOUT)
    writer = asyncio.create_task(exclusive())
    await asyncio.sleep(0)
    later = asyncio.create_task(later_shared())
    await asyncio.sleep(0)
    assert later_entered.is_set() is False

    writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writer
    await asyncio.wait_for(later_entered.wait(), timeout=_TIMEOUT)
    release_first.set()
    await asyncio.gather(first, later)


def _agent_factory(llm: ScriptedLLM, tool: _Recorder, policy: _ConfirmNamed):
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Root',
                llm=llm,
                tools={'Confirmable': tool},
                danger_policy=policy,
            ),
            hooks=hooks,
            events=events,
        )

    return build


async def _take_confirmation(events) -> Any:
    while True:
        event = await asyncio.wait_for(anext(events), timeout=_TIMEOUT)
        if event.kind == 'tool_confirmation_requested':
            return event


async def test_mailbox_confirmation_uses_a_nonce_and_a_trusted_human_reply() -> None:
    tool = _Recorder()
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='model-call-id',
                        name='Confirmable',
                        arguments='{"value":"approved"}',
                    )
                ]
            ),
            ScriptedTurn(content='done'),
        ]
    )
    runtime = Runtime(confirmation_handler=MailboxConfirmationHandler(timeout=30))
    events = runtime.events.subscribe()
    try:
        agent_id = await runtime.spawn(
            _agent_factory(llm, tool, _ConfirmNamed()),
            query='start',
        )
        event = await _take_confirmation(events)
        assert event.agent_id == agent_id
        assert event.payload['tool_call_id'] == 'model-call-id'
        assert event.payload['request_id'] != 'model-call-id'
        delivered = await runtime.send(
            agent_id,
            'approve',
            activate=False,
            sender='human',
            correlation_id=event.payload['request_id'],
        )
        assert delivered.ok is True
        context = await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
        assert context.current_traj[-1].content == 'done'
        assert tool.values == ['approved']
    finally:
        events.close()
        await runtime.shutdown()


async def test_a_host_handler_gets_the_stable_key_the_wait_and_the_event_use() -> None:
    tool = _Recorder()
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(id='c', name='Recorder', arguments='{"value":"x"}'),
                    ToolCall(id='c', name='Confirmable', arguments='{"value":"y"}'),
                ]
            ),
            ScriptedTurn(content='done'),
        ]
    )
    seen: list[ConfirmationRequest] = []

    class _Keyed:
        async def confirm(
            self, runtime: object, request: ConfirmationRequest
        ) -> ConfirmationOutcome:
            seen.append(request)
            return ConfirmationOutcome.APPROVED

    other = _Recorder()
    other.__class__ = type('Recorder', (_Recorder,), {'__tool_name__': 'Recorder'})

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Root',
                llm=llm,
                tools={'Recorder': other, 'Confirmable': tool},
                danger_policy=_ConfirmNamed(),
            ),
            hooks=hooks,
            events=events,
        )

    runtime = Runtime(confirmation_handler=_Keyed())
    try:
        agent_id = await runtime.spawn(build, query='start')
        await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
    finally:
        await runtime.shutdown()

    [request] = seen
    assert request.request_ref == ('tool_call', agent_id, 1, 1, 1, 'request', 0)


@pytest.mark.parametrize(
    ('sender', 'trust'),
    [
        ('worker', TrustLevel.TRUSTED),
        ('human', TrustLevel.UNTRUSTED),
    ],
)
async def test_mailbox_confirmation_rejects_forged_or_untrusted_replies(
    sender: str, trust: TrustLevel
) -> None:
    tool = _Recorder()
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='model-call-id',
                        name='Confirmable',
                        arguments='{"value":"must-not-run"}',
                    )
                ]
            ),
            ScriptedTurn(content='done'),
        ]
    )
    runtime = Runtime(confirmation_handler=MailboxConfirmationHandler(timeout=30))
    events = runtime.events.subscribe()
    try:
        agent_id = await runtime.spawn(
            _agent_factory(llm, tool, _ConfirmNamed()),
            query='start',
        )
        event = await _take_confirmation(events)
        delivered = await runtime.send(
            agent_id,
            'approve',
            activate=False,
            sender=sender,
            correlation_id=event.payload['request_id'],
            trust=trust,
        )
        assert delivered.ok is True
        context = await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
        assert context.current_traj[-1].content == 'done'
        assert tool.values == []
        blocked = next(block for block in context.current_traj if block.role == 'tool')
        assert blocked.extra['tool_meta']['confirmation'] == 'rejected'
        assert isinstance(blocked.content, str)
        assert '可信人类' in blocked.content
    finally:
        events.close()
        await runtime.shutdown()


async def test_the_request_key_a_host_handler_gets_is_the_one_its_event_and_wait_use() -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[ToolCall(id='c', name='Confirmable', arguments='{"value":"y"}')]
            ),
            ScriptedTurn(content='done'),
        ]
    )
    seen: list[ConfirmationRequest] = []
    mailbox = MailboxConfirmationHandler(timeout=_TIMEOUT)

    class _Watching:
        async def confirm(self, runtime: Any, request: ConfirmationRequest) -> ConfirmationOutcome:
            seen.append(request)
            return await mailbox.confirm(runtime, request)

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Root',
                llm=llm,
                tools={'Confirmable': _Recorder()},
                danger_policy=_ConfirmNamed(),
            ),
            hooks=hooks,
            events=events,
        )

    runtime = Runtime(confirmation_handler=_Watching())
    subscription = runtime.events.subscribe()
    announced: list[tuple[object, ...]] = []
    outcomes: list[bool] = []

    async def answer() -> None:
        async for event in subscription:
            if event.kind == 'tool_confirmation_requested':
                announced.append(tuple(event.payload['request_ref']))
                delivered = await runtime.send(
                    event.agent_id,
                    'approve',
                    activate=False,
                    sender='human',
                    correlation_id=event.payload['request_id'],
                )
                outcomes.append(delivered.ok)

    answering = asyncio.create_task(answer())
    try:
        agent_id = await runtime.spawn(build, query='start')
        context = await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
    finally:
        await runtime.shutdown()
        subscription.close()
        await asyncio.gather(answering, return_exceptions=True)

    [request] = seen
    assert announced == [request.request_ref]
    assert outcomes == [True]
    assert context.current_traj[-1].content == 'done'
