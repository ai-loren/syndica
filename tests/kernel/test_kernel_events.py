"""M9 加的内核事件：种类表、各自的 payload、发出的时机与顺序。"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from decimal import Decimal
from typing import Any

import pytest

from syndica.kernel import (
    AgentInfo,
    AgentOptions,
    BaseAgent,
    Budget,
    BudgetPolicy,
    ConfigError,
    ContextBlock,
    Disposition,
    EventBus,
    HooksCenter,
    InMemoryAgentRegistry,
    MailboxConfirmationHandler,
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
from syndica.kernel.budget import BudgetView, Usage
from syndica.kernel.context import ArtifactRef, Context
from syndica.kernel.danger import DangerRequest
from syndica.kernel.events import KERNEL_EVENTS, Event, Subscription
from syndica.kernel.history_blocks import HistoryKind, signed_block
from syndica.kernel.runtime import AgentFactory
from syndica.kernel.stores.memory_artifact import InMemoryArtifactStore
from syndica.kernel.tools import AskUser
from syndica.kernel.transcript import block_payload, budget_payload, plain_data
from syndica.kernel.waiting import UserInput

_TIMEOUT = 2.0


class _Echo(Tool):
    __tool_name__ = 'Echo'
    __tool_command_args__ = ()

    async def execute(self, text: str) -> ToolResult:
        """Echo the text back."""
        return ToolResult(output=text, meta={'echoed': True})


class _Untrusted(Tool):
    __tool_name__ = 'Fetch'
    __tool_command_args__ = ()

    async def execute(self, url: str) -> ToolResult:
        """Fetch a page."""
        return ToolResult(output=f'page at {url}', trust=TrustLevel.UNTRUSTED)


class _Make(Tool):
    __tool_name__ = 'Make'
    __tool_command_args__ = ()

    async def execute(self, path: str) -> ToolResult:
        """Write a document."""
        ref = ArtifactRef(
            id=f'file:{path}',
            path=path,
            kind='document',
            mime='text/markdown',
            version='v1',
            producer='worker',
        )
        return ToolResult(output=f'wrote {path}', artifacts=[ref])


class _ConfirmEcho:
    def decide(self, request: DangerRequest) -> Disposition:
        return Disposition.CONFIRM if request.call.name == 'Echo' else Disposition.ALLOW


def _call(name: str, call_id: str, **arguments: str) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def _factory(
    llm: ScriptedLLM,
    *,
    name: str = 'Worker',
    tools: dict[str, Tool] | None = None,
    policy: Any = None,
    seed: ContextBlock | None = None,
) -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = BaseAgent(
            AgentOptions(
                name=name,
                llm=llm,
                tools={'Echo': _Echo()} if tools is None else tools,
                system_prompt='SP',
                danger_policy=policy,
            ),
            hooks=hooks,
            events=events,
        )
        if seed is not None:
            agent.context.append(seed)
        return agent

    return build


async def _drain(runtime: Runtime, subscription: Subscription) -> list[Event]:
    await runtime.shutdown()
    return [event async for event in subscription]


def test_the_kernel_event_table_is_exact():
    assert dict(KERNEL_EVENTS) == {
        'agent_start': (),
        'agent_end': ('stop_reason',),
        'agent_error': ('error_type', 'expected', 'received', 'next_action'),
        'turn_start': ('turn',),
        'turn_end': ('turn',),
        'compaction': ('trigger', 'blocks_before', 'blocks_after', 'tokens_before', 'tokens_after'),
        'budget_exhausted': ('dimensions', 'disposition'),
        'budget_changed': ('view',),
        'progress_regressed': ('rubrics',),
        'progress_stalled': ('turns',),
        'goal_drift': ('confirmed',),
        'user_input_requested': ('request_id', 'question'),
        'user_question_requested': ('request_id', 'question', 'options'),
        'tool_confirmation_requested': (
            'request_id',
            'tool_call_id',
            'tool_name',
            'arguments',
            'verdict',
            'untrusted',
        ),
        'human_request_resolved': ('request_id', 'outcome'),
        'message_appended': ('generation', 'index', 'block'),
        'transcript_reset': ('generation', 'reason'),
        'tool_call_started': ('tool_call_id', 'name', 'arguments'),
        'tool_call_finished': ('tool_call_id', 'outcome'),
        'agent_spawned': ('parent_id', 'name'),
        'agent_status': ('status',),
        'agent_removed': (),
        'artifact_registered': ('ref',),
    }


async def test_the_bus_stamps_the_time_and_equality_ignores_it():
    bus = EventBus()
    async with bus.subscribe() as subscription:
        await bus.emit(Event('turn_start', 'a1', {'turn': 1}))
        await bus.emit(Event('turn_start', 'a1', {'turn': 1}, time_ns=7))
        await bus.close()
        first, second = [event async for event in subscription]
    assert first.time_ns > 0
    assert second.time_ns == 7
    assert dataclasses.replace(first, sequence=2) == second


async def test_every_kernel_event_of_a_run_carries_its_required_keys():
    store = InMemoryArtifactStore()
    runtime = Runtime(
        artifacts=store,
        budget=BudgetPolicy(root=Budget(turns=20)),
        confirmation_handler=MailboxConfirmationHandler(timeout=30),
    )
    subscription = runtime.events.subscribe()
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('Make', 'm1', path='a.md')]),
            ScriptedTurn(tool_calls=[_call('Echo', 'e1', text='hi')]),
            ScriptedTurn(content='done'),
        ]
    )
    tools: dict[str, Tool] = {'Echo': _Echo(), 'Make': _Make()}
    agent_id = await runtime.spawn(_factory(llm, tools=tools, policy=_ConfirmEcho()), query='go')
    child = await runtime.spawn(_factory(ScriptedLLM([])), parent_id=agent_id)
    seen: list[Event] = []
    async for event in subscription:
        seen.append(event)
        if event.kind == 'tool_confirmation_requested':
            await runtime.send(
                agent_id,
                'approve',
                activate=False,
                sender='human',
                correlation_id=event.payload['request_id'],
            )
            break
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    await runtime.despawn(child)
    events = seen + await _drain(runtime, subscription)
    kinds = {event.kind for event in events}
    assert kinds >= {
        'agent_spawned',
        'agent_status',
        'agent_removed',
        'message_appended',
        'tool_call_started',
        'tool_call_finished',
        'artifact_registered',
        'budget_changed',
        'human_request_resolved',
    }
    assert kinds <= set(KERNEL_EVENTS)
    for event in events:
        missing = set(KERNEL_EVENTS[event.kind]) - set(event.payload)
        assert missing == set(), (event.kind, missing)


async def test_a_spawned_agent_is_announced_before_its_seeded_and_first_blocks():
    runtime = Runtime()
    subscription = runtime.events.subscribe()
    seed = ContextBlock(role='user', content='seeded')
    agent_id = await runtime.spawn(
        _factory(ScriptedLLM([ScriptedTurn(content='done')]), seed=seed), query='go'
    )
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    events = [event for event in await _drain(runtime, subscription) if event.agent_id == agent_id]
    assert events[0].kind == 'agent_spawned'
    assert events[0].payload == {'parent_id': None, 'name': 'Worker'}
    assert [
        (event.payload['index'], event.payload['block']['text'])
        for event in events
        if event.kind == 'message_appended'
    ] == [(0, 'seeded'), (1, 'go'), (2, 'done')]


class _RefusingRegistry(InMemoryAgentRegistry):
    async def register(self, info: AgentInfo) -> None:
        raise ConfigError(expected='register', received='refused', next_action='none')


async def test_a_spawn_that_rolls_back_publishes_nothing_for_the_agent_it_built():
    runtime = Runtime(registry=_RefusingRegistry())
    subscription = runtime.events.subscribe()
    built: list[BaseAgent] = []
    inner = _factory(ScriptedLLM([]), seed=ContextBlock(role='user', content='seeded'))

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = inner(hooks, events)
        built.append(agent)
        return agent

    with pytest.raises(ConfigError):
        await runtime.spawn(build, query='go')
    events = await _drain(runtime, subscription)
    assert [event for event in events if event.agent_id == built[0].agent_id] == []


async def test_three_agents_each_number_their_own_blocks_from_zero():
    runtime = Runtime()
    subscription = runtime.events.subscribe()
    ids = [
        await runtime.spawn(
            _factory(ScriptedLLM([ScriptedTurn(content=f'answer {n}')])), query=f'task {n}'
        )
        for n in range(3)
    ]
    for agent_id in ids:
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    events = await _drain(runtime, subscription)
    for n, agent_id in enumerate(ids):
        assert [
            (event.payload['generation'], event.payload['index'], event.payload['block']['text'])
            for event in events
            if event.kind == 'message_appended' and event.agent_id == agent_id
        ] == [(0, 0, f'task {n}'), (0, 1, f'answer {n}')]


class _Seen:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def appended(self, generation: int, index: int, block: ContextBlock) -> None:
        self.calls.append(('appended', generation, index, block.content))

    def replaced(self, generation: int) -> None:
        self.calls.append(('replaced', generation))


def test_a_rewritten_trajectory_starts_the_next_generation_and_renumbers_from_its_length():
    context = Context()
    seen = _Seen()
    context._observe(seen)  # pyright: ignore[reportPrivateUsage]
    context.append(ContextBlock(role='user', content='a'))
    context.append(ContextBlock(role='assistant', content='b'))
    context._replace_traj([ContextBlock(role='user', content='summary')])  # pyright: ignore[reportPrivateUsage]
    context.append(ContextBlock(role='user', content='c'))
    assert seen.calls == [
        ('appended', 0, 0, 'a'),
        ('appended', 0, 1, 'b'),
        ('replaced', 1),
        ('appended', 1, 1, 'c'),
    ]


class _Raising:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.config = ScriptedLLM([]).config

    async def create(self, *args: Any, **kwargs: Any) -> ContextBlock:
        raise self._exc


@pytest.mark.parametrize(
    ('exc', 'expected'),
    [
        (
            ConfigError(expected='a key', received='none', next_action='set one'),
            {
                'error_type': 'ConfigError',
                'expected': 'a key',
                'received': 'none',
                'next_action': 'set one',
            },
        ),
        (
            RuntimeError('boom'),
            {
                'error_type': 'RuntimeError',
                'expected': '这个周期正常跑完',
                'received': 'RuntimeError: boom',
            },
        ),
    ],
)
async def test_agent_error_carries_the_three_parts_of_what_went_wrong(
    exc: Exception, expected: dict[str, str]
):
    bus = EventBus()
    agent = BaseAgent(
        AgentOptions(name='Worker', llm=_Raising(exc), tools={}, system_prompt='SP'),  # pyright: ignore[reportArgumentType]
        hooks=HooksCenter(),
        events=bus,
    )
    async with bus.subscribe() as subscription:
        with pytest.raises(type(exc)):
            await agent.run('go')
        await bus.close()
        events = [event async for event in subscription]
    [error] = [event.payload for event in events if event.kind == 'agent_error']
    assert {key: error[key] for key in expected} == expected
    assert error['next_action']


async def _confirmation_payload(script: list[ScriptedTurn]) -> tuple[dict[str, Any], list[Event]]:
    runtime = Runtime(confirmation_handler=MailboxConfirmationHandler(timeout=30))
    subscription = runtime.events.subscribe()
    tools: dict[str, Tool] = {'Echo': _Echo(), 'Fetch': _Untrusted()}
    agent_id = await runtime.spawn(
        _factory(ScriptedLLM(script), tools=tools, policy=_ConfirmEcho()), query='go'
    )
    seen: list[Event] = []
    async for event in subscription:
        seen.append(event)
        if event.kind == 'tool_confirmation_requested':
            await runtime.send(
                agent_id,
                'reject',
                activate=False,
                sender='human',
                correlation_id=event.payload['request_id'],
            )
            break
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    seen.extend(await _drain(runtime, subscription))
    [request] = [event.payload for event in seen if event.kind == 'tool_confirmation_requested']
    return request, seen


async def test_a_confirmation_says_the_call_traces_to_untrusted_content():
    request, _ = await _confirmation_payload(
        [
            ScriptedTurn(tool_calls=[_call('Fetch', 'f1', url='https://x')]),
            ScriptedTurn(tool_calls=[_call('Echo', 'e1', text='hi')]),
            ScriptedTurn(content='done'),
        ]
    )
    assert request['untrusted'] is True


async def test_a_confirmation_without_untrusted_input_says_so_and_is_resolved_once():
    request, events = await _confirmation_payload(
        [ScriptedTurn(tool_calls=[_call('Echo', 'e1', text='hi')]), ScriptedTurn(content='done')]
    )
    assert request['untrusted'] is False
    assert [event.payload for event in events if event.kind == 'human_request_resolved'] == [
        {'request_id': request['request_id'], 'outcome': 'rejected'}
    ]


class _Unanswered:
    def __init__(self) -> None:
        self.events = EventBus()

    async def wait_for(self, agent_id: str, waiter: UserInput) -> WaitResult:
        return WaitResult(state=WaitState.TIMEOUT)


async def test_an_ask_that_times_out_is_resolved_with_its_own_request_id():
    runtime = _Unanswered()
    async with runtime.events.subscribe() as subscription:
        await AskUser(runtime, 'agent-1').execute('which?', timeout=1)  # pyright: ignore[reportArgumentType]
        await runtime.events.close()
        events = [event async for event in subscription]
    asked, resolved = events
    assert asked.kind == 'user_input_requested'
    assert (resolved.kind, resolved.agent_id, resolved.payload) == (
        'human_request_resolved',
        'agent-1',
        {'request_id': asked.payload['request_id'], 'outcome': 'timeout'},
    )


async def test_each_status_change_is_published_once_and_a_despawn_publishes_removal():
    runtime = Runtime()
    subscription = runtime.events.subscribe()
    agent_id = await runtime.spawn(
        _factory(ScriptedLLM([ScriptedTurn(content='done')])), query='go'
    )
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    await runtime.despawn(agent_id)
    events = await _drain(runtime, subscription)
    assert [
        (event.kind, event.payload.get('status'))
        for event in events
        if event.kind in ('agent_status', 'agent_removed')
    ] == [('agent_status', 'running'), ('agent_status', 'zombie'), ('agent_removed', None)]


async def test_a_turn_end_and_a_host_grant_each_publish_the_budget_view():
    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=5)))
    subscription = runtime.events.subscribe()
    agent_id = await runtime.spawn(
        _factory(ScriptedLLM([ScriptedTurn(content='done')])), query='go'
    )
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    runtime._grant_budget(agent_id, Budget(turns=2))  # pyright: ignore[reportPrivateUsage]
    events = await _drain(runtime, subscription)
    views = [event.payload['view'] for event in events if event.kind == 'budget_changed']
    assert [(view['used']['turns'], view['limit']['turns']) for view in views] == [(1, 5), (1, 7)]


def test_a_budget_payload_writes_amounts_as_decimal_strings_and_unknown_as_none():
    view = BudgetView(
        limit=Budget(cost=Decimal('0.1'), turns=3),
        used=Usage(input_tokens=5, output_tokens=2, cost=None, turns=1, estimated=True),
        remaining=Budget(cost=Decimal('0.1'), turns=2),
        exhausted=('turns',),
    )
    assert budget_payload(view) == {
        'limit': {'tokens': None, 'cost': '0.1', 'wall_clock_s': None, 'turns': 3},
        'used': {
            'input_tokens': 5,
            'output_tokens': 2,
            'cached_input_tokens': 0,
            'cost': None,
            'turns': 1,
            'estimated': True,
        },
        'remaining': {'tokens': None, 'cost': '0.1', 'wall_clock_s': None, 'turns': 2},
        'exhausted': ['turns'],
    }


async def test_tool_calls_publish_their_start_and_the_outcome_of_the_execution():
    runtime = Runtime()
    subscription = runtime.events.subscribe()
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('Echo', 'e1', text='hi'), _call('Nope', 'n1')]),
            ScriptedTurn(content='done'),
        ]
    )
    agent_id = await runtime.spawn(_factory(llm), query='go')
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    events = await _drain(runtime, subscription)
    assert [
        (event.kind, event.payload) for event in events if event.kind.startswith('tool_call_')
    ] == [
        (
            'tool_call_started',
            {'tool_call_id': 'e1', 'name': 'Echo', 'arguments': '{"text": "hi"}'},
        ),
        ('tool_call_finished', {'tool_call_id': 'e1', 'outcome': 'ok'}),
        ('tool_call_started', {'tool_call_id': 'n1', 'name': 'Nope', 'arguments': '{}'}),
        ('tool_call_finished', {'tool_call_id': 'n1', 'outcome': 'unknown_tool'}),
    ]


async def test_a_registered_artifact_is_published_with_the_ref_the_index_holds():
    store = InMemoryArtifactStore()
    runtime = Runtime(artifacts=store)
    subscription = runtime.events.subscribe()
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('Make', 'm1', path='./a.md')]),
            ScriptedTurn(content='done'),
        ]
    )
    agent_id = await runtime.spawn(_factory(llm, tools={'Make': _Make()}), query='go')
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    events = await _drain(runtime, subscription)
    [ref] = [event.payload['ref'] for event in events if event.kind == 'artifact_registered']
    assert ref == {
        'id': 'file:a.md',
        'path': 'a.md',
        'kind': 'document',
        'mime': 'text/markdown',
        'version': 'v1',
        'producer': 'worker',
        'is_deliverable': False,
    }


def test_a_block_payload_keeps_text_calls_kind_and_artifacts():
    block = ContextBlock(
        role='assistant',
        content=[
            {'type': 'text', 'text': 'one'},
            {'type': 'image'},
            {'type': 'text', 'text': 'two'},
        ],
        tool_calls=[ToolCall(id='c1', name='Echo', arguments='{}')],
        trust=TrustLevel.UNTRUSTED,
    )
    assert block_payload(block) == {
        'role': 'assistant',
        'text': 'one\n[image]\ntwo',
        'trust': 'untrusted',
        'kind': None,
        'tool_calls': [{'id': 'c1', 'name': 'Echo', 'arguments': '{}'}],
        'tool_call_id': None,
        'artifacts': [],
        'tool_meta': {},
    }
    signed = signed_block(HistoryKind.BUDGET, 'note', key='budget:1')
    assert block_payload(signed)['kind'] == 'budget'


def test_tool_meta_keeps_only_what_json_can_carry():
    block = ContextBlock(
        role='tool',
        content='out',
        tool_call_id='c1',
        extra={'tool_meta': {'handle': 'pty-1', 'pair': (1, 2), 'live': object(), 3: 'x'}},
    )
    assert block_payload(block)['tool_meta'] == {'handle': 'pty-1', 'pair': [1, 2], 'live': None}
    assert plain_data({'deep': [[[[[[[[[['x']]]]]]]]]]}) == {'deep': [[[[[[[None]]]]]]]}
