"""可复现的 id 与「当前工具调用」（M3 D10）。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from typing import Any, cast

import pytest

from syndica.kernel import (
    AgentOptions,
    BaseAgent,
    ConfigError,
    ContractViolation,
    Event,
    EventBus,
    ExitMode,
    HookResult,
    HooksCenter,
    HumanInteractionModule,
    Junction,
    MailboxConfirmationHandler,
    Reply,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    StableKey,
    StopReason,
    Tool,
    ToolCall,
    ToolResult,
    WaitState,
)
from syndica.kernel.danger import DangerRequest, Disposition
from syndica.kernel.ids import RandomIds, ToolCallKey, current_tool_call
from syndica.kernel.redaction import RuleRedactor, encode_redacted
from syndica.kernel.stores import InMemoryAgentRegistry
from syndica.kernel.tools import Delegate

_TIMEOUT = 5.0
_HEX32 = re.compile(r'[0-9a-f]{32}')


class _KeyedIds:
    """按稳定键交 id：记录过的键交回记录的那个，没记录过的按种类加序号新造一个。"""

    def __init__(self, recorded: dict[StableKey, str] | None = None) -> None:
        self.recorded = dict(recorded or {})
        self.issued: dict[StableKey, str] = {}
        self._count = 0

    def _issue(self, kind: str, key: StableKey) -> str:
        if key in self.recorded:
            value = self.recorded[key]
        else:
            self._count += 1
            value = f'{kind}-{self._count}'
        self.issued[key] = value
        return value

    def agent_id(self, key: StableKey) -> str:
        return self._issue('agent', key)

    def message_id(self, key: StableKey) -> str:
        return self._issue('message', key)


def _call(call_id: str, name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def _factory(name: str, llm: ScriptedLLM, tools: dict[str, Tool] | None = None):
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name=name, llm=llm, tools=dict(tools or {})), hooks=hooks, events=events
        )

    return build


class _Probe(Tool):
    """记下自己看到的「当前工具调用」；可并发。"""

    __tool_name__ = 'Probe'
    __tool_command_args__ = ()

    def __init__(self, seen: list[ToolCallKey | None]) -> None:
        self._seen = seen

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        return True

    async def execute(self, delay: float = 0.0) -> ToolResult:
        """先睡一会儿，再记下当前工具调用。
        delay: 先睡多少秒
        """
        await asyncio.sleep(delay)
        self._seen.append(current_tool_call())
        return ToolResult(output='seen')


class _Spawner(Tool):
    """在一次调用里派出子 Agent、发出消息；可并发，先睡 `delay` 秒，好让几次调用的先后颠倒。"""

    __tool_name__ = 'Spawner'
    __tool_command_args__ = ()

    def __init__(self, runtime: Runtime, agent_id: str, child: Any) -> None:
        self._runtime = runtime
        self._agent_id = agent_id
        self._child = child
        self.created: list[tuple[int, str]] = []

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        return True

    async def execute(self, delay: float = 0.0, spawns: int = 1, messages: int = 0) -> ToolResult:
        """派出子 Agent 并给它发消息。
        delay: 先睡多少秒
        spawns: 派出几个
        messages: 给第一个子 Agent 发几条
        """
        await asyncio.sleep(delay)
        children = [
            await self._runtime.spawn(self._child, parent_id=self._agent_id) for _ in range(spawns)
        ]
        call = current_tool_call()
        assert call is not None
        for child in children:
            self.created.append((call.index, child))
        for number in range(messages):
            await self._runtime.send(children[0], f'note {number}', activate=False)
        return ToolResult(output=' '.join(children))


def _idle_child(hooks: HooksCenter, events: EventBus) -> BaseAgent:
    return BaseAgent(AgentOptions(name='Child', llm=ScriptedLLM([])), hooks=hooks, events=events)


class _SerialProbe(_Probe):
    __tool_name__ = 'SerialProbe'

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        return False


class _Await(Tool):
    """在一次调用里依次等一到两条答复；可并发。"""

    __tool_name__ = 'Await'
    __tool_command_args__ = ()

    def __init__(self, runtime: Runtime, agent_id: str) -> None:
        self._runtime = runtime
        self._agent_id = agent_id
        self.entered: list[int] = []
        self.started: list[str] = []
        self.results: list[tuple[str, WaitState]] = []

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        return True

    async def execute(self, first: str, second: str = '', delay: float = 0.0) -> ToolResult:
        """等带这些关联 ID 的答复。
        first: 第一条答复的关联 ID
        second: 第二条答复的关联 ID，空串表示只等一条
        delay: 开始等之前先睡多少秒
        """
        call = current_tool_call()
        assert call is not None
        self.entered.append(call.index)
        await asyncio.sleep(delay)
        for correlation in (first, second) if second else (first,):
            self.started.append(correlation)
            result = await self._runtime.wait_for(
                self._agent_id, Reply(correlation_id=correlation, timeout=_TIMEOUT)
            )
            self.results.append((correlation, result.state))
        return ToolResult(output='answered')


def _awaiting(runtime: Runtime, name: str, llm: ScriptedLLM, made: list[_Await]):
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory(name, llm)(hooks, events)
        tool = _Await(runtime, agent.agent_id)
        made.append(tool)
        agent.install_runtime_tools({'Await': tool})
        return agent

    return build


async def _until(ready: Callable[[], bool]) -> None:
    async with asyncio.timeout(_TIMEOUT):
        while True:
            if ready():
                return
            await asyncio.sleep(0.005)


async def test_default_ids_keep_their_form():
    runtime = Runtime()
    try:
        agent_id = await runtime.spawn(_factory('Root', ScriptedLLM([ScriptedTurn(content='x')])))
        await runtime.send(agent_id, 'hello', activate=False)
        context = await runtime.wait(agent_id)
    finally:
        await runtime.shutdown()

    injected = next(block for block in context.current_traj if block.role == 'user')
    assert _HEX32.fullmatch(agent_id)
    assert _HEX32.fullmatch(cast(str, injected.extra['message_id']))


async def test_an_injected_source_names_agents_and_messages_by_where_they_came_from():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    try:
        first = await runtime.spawn(_factory('A', ScriptedLLM([ScriptedTurn(content='a')])))
        second = await runtime.spawn(_factory('B', ScriptedLLM([ScriptedTurn(content='b')])))
        await runtime.send(second, 'one', activate=False)
        await runtime.send(first, 'two', activate=False)
    finally:
        await runtime.shutdown()

    assert ids.issued == {
        ('host', 'spawn', 0): first,
        ('host', 'spawn', 1): second,
        ('host', 'message', 0): 'message-3',
        ('host', 'message', 1): 'message-4',
    }


async def test_an_id_bound_inside_the_factory_is_the_one_the_agent_is_registered_under():
    """工厂里就绑定了 `agent.agent_id`（这里是派活工具的发起方）：工厂返回之后再换 id，派活时
    注册表会拒掉那个旧的父 id。"""
    ids = _KeyedIds()
    child_llm = ScriptedLLM([ScriptedTurn(content='child done')])
    runtime = Runtime(ids=ids, agents={'Child': _factory('Child', child_llm)})
    bound: list[str] = []
    parent_llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('d1', 'Delegate', agent='Child', query='go')]),
            ScriptedTurn(content='parent done'),
        ]
    )

    def parent(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = BaseAgent(AgentOptions(name='Parent', llm=parent_llm), hooks=hooks, events=events)
        bound.append(agent.agent_id)
        agent.install_runtime_tools({'Delegate': Delegate(runtime, agent.agent_id)})
        return agent

    try:
        parent_id = await runtime.spawn(parent, query='start')
        context = await asyncio.wait_for(runtime.wait(parent_id), _TIMEOUT)
        child_id = ids.issued[('tool_call', parent_id, 1, 1, 0, 'spawn', 0)]
        child = await runtime.registry.get(child_id)
    finally:
        await runtime.shutdown()

    assert bound == [parent_id] == [ids.issued[('host', 'spawn', 0)]]
    assert context.stop_reason is StopReason.COMPLETED
    assert child is not None and child.parent_id == parent_id


def _missed_assignments(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == 'syndica.kernel.runtime' and '回放时它拿不到' in record.getMessage()
    ]


async def test_a_factory_that_returns_an_agent_built_elsewhere_keeps_that_agents_id(
    caplog: pytest.LogCaptureFixture,
):
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    prebuilt = BaseAgent(
        AgentOptions(name='Prebuilt', llm=ScriptedLLM([])), hooks=HooksCenter(), events=EventBus()
    )
    try:
        agent_id = await runtime.spawn(lambda hooks, events: prebuilt)
        built_later = _idle_child(HooksCenter(), EventBus())
    finally:
        await runtime.shutdown()

    assigned = ids.issued[('host', 'spawn', 0)]
    assert agent_id == prebuilt.agent_id != assigned
    assert built_later.agent_id != assigned
    [record] = _missed_assignments(caplog)
    assert record.levelno == logging.WARNING
    assert record.args == (assigned, ('host', 'spawn', 0), agent_id)


async def test_only_the_first_agent_built_by_a_factory_takes_the_assigned_id(
    caplog: pytest.LogCaptureFixture,
):
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    helpers: list[BaseAgent] = []

    def factory(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        helpers.append(_idle_child(hooks, events))
        return _idle_child(hooks, events)

    try:
        agent_id = await runtime.spawn(factory)
    finally:
        await runtime.shutdown()

    assert helpers[0].agent_id == ids.issued[('host', 'spawn', 0)]
    assert _HEX32.fullmatch(agent_id) and agent_id != helpers[0].agent_id
    [record] = _missed_assignments(caplog)
    assert record.args == (helpers[0].agent_id, ('host', 'spawn', 0), agent_id)


async def test_a_missed_assignment_is_not_reported_when_ids_are_random_anyway(
    caplog: pytest.LogCaptureFixture,
):
    runtime = Runtime()

    def factory(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        _idle_child(hooks, events)
        return _idle_child(hooks, events)

    try:
        await runtime.spawn(factory)
    finally:
        await runtime.shutdown()

    assert _missed_assignments(caplog) == []


async def test_a_factory_that_builds_its_agent_first_is_not_reported(
    caplog: pytest.LogCaptureFixture,
):
    runtime = Runtime(ids=_KeyedIds())
    try:
        await runtime.spawn(_idle_child)
    finally:
        await runtime.shutdown()

    assert _missed_assignments(caplog) == []


async def test_the_current_tool_call_is_set_for_pre_tool_use_and_cleared_after_it():
    seen: list[ToolCallKey | None] = []
    hooked: list[ToolCallKey | None] = []
    between: list[ToolCallKey | None] = []
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('c1', 'Probe'), _call('c1', 'Probe')]),
            ScriptedTurn(tool_calls=[_call('', 'Probe')]),
            ScriptedTurn(content='done'),
        ]
    )
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)

    async def pre(**kwargs: Any) -> HookResult:
        hooked.append(current_tool_call())
        return HookResult()

    async def turn_end(**kwargs: Any) -> HookResult:
        between.append(current_tool_call())
        return HookResult()

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm, {'Probe': _Probe(seen)})(hooks, events)
        agent.register_hook(Junction.PRE_TOOL_USE, pre)
        agent.register_hook(Junction.TURN_END, turn_end)
        return agent

    try:
        agent_id = await runtime.spawn(build, query='start')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    expected = [
        ToolCallKey(agent_id, 1, 1, 0, 'c1'),
        ToolCallKey(agent_id, 1, 1, 1, 'c1'),
        ToolCallKey(agent_id, 1, 2, 0, ''),
    ]
    assert sorted(hooked, key=str) == sorted(expected, key=str)
    assert sorted(seen, key=str) == sorted(expected, key=str)
    assert between == [None, None, None]
    assert current_tool_call() is None


async def test_calls_running_together_each_see_their_own_tool_call():
    seen: list[ToolCallKey | None] = []
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    _call('a', 'Probe', delay=0.03),
                    _call('b', 'Probe', delay=0.01),
                    _call('c', 'Probe', delay=0.02),
                ]
            ),
            ScriptedTurn(content='done'),
        ]
    )
    runtime = Runtime()
    try:
        agent_id = await runtime.spawn(_factory('Root', llm, {'Probe': _Probe(seen)}), query='go')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert seen == [
        ToolCallKey(agent_id, 1, 1, 1, 'b'),
        ToolCallKey(agent_id, 1, 1, 2, 'c'),
        ToolCallKey(agent_id, 1, 1, 0, 'a'),
    ]


async def test_spawns_and_messages_inside_a_tool_call_are_keyed_by_it():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    spawners: list[_Spawner] = []
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('s', 'Spawner', spawns=2, messages=2)]),
            ScriptedTurn(content='done'),
        ]
    )

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm)(hooks, events)
        spawner = _Spawner(runtime, agent.agent_id, _idle_child)
        spawners.append(spawner)
        agent.install_runtime_tools({'Spawner': spawner})
        return agent

    try:
        agent_id = await runtime.spawn(build, query='go')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    call = ('tool_call', agent_id, 1, 1, 0)
    assert [child for _, child in spawners[0].created] == [
        ids.issued[(*call, 'spawn', 0)],
        ids.issued[(*call, 'spawn', 1)],
    ]
    assert (*call, 'message', 0) in ids.issued and (*call, 'message', 1) in ids.issued
    assert ('host', 'message', 0) not in ids.issued


async def test_children_created_in_a_different_order_get_their_recorded_ids():
    """记录时三个子 Agent 按 1、2、0 的先后创建；回放时按相反、同样不单调的 0、2、1 创建：按键取的
    id 不随先后变。"""

    async def run(delays: tuple[float, float, float], ids: _KeyedIds) -> list[tuple[int, str]]:
        runtime = Runtime(ids=ids)
        spawners: list[_Spawner] = []
        llm = ScriptedLLM(
            [
                ScriptedTurn(
                    tool_calls=[
                        _call(f'k{index}', 'Spawner', delay=delay)
                        for index, delay in enumerate(delays)
                    ]
                ),
                ScriptedTurn(content='done'),
            ]
        )

        def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
            agent = _factory('Root', llm)(hooks, events)
            spawner = _Spawner(runtime, agent.agent_id, _idle_child)
            spawners.append(spawner)
            agent.install_runtime_tools({'Spawner': spawner})
            return agent

        try:
            root = await runtime.spawn(build, query='go')
            await asyncio.wait_for(runtime.wait(root), _TIMEOUT)
        finally:
            await runtime.shutdown()
        return spawners[0].created

    recording = _KeyedIds()
    recorded = await run((0.03, 0.01, 0.02), recording)
    replay = _KeyedIds(recording.issued)
    replayed = await run((0.01, 0.03, 0.02), replay)

    assert [index for index, _ in recorded] == [1, 2, 0]
    assert [index for index, _ in replayed] == [0, 2, 1]
    assert sorted(replayed) == sorted(recorded)


async def test_a_woken_agent_does_not_inherit_the_senders_tool_call():
    """唤醒 zombie 的那次 send 在发送方的工具调用里：被唤醒的驱动任务建在那个上下文里。"""
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    woken: list[ToolCallKey | None] = []
    sleeper_llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='woken')])

    async def turn_start(**kwargs: Any) -> HookResult:
        woken.append(current_tool_call())
        return HookResult()

    def sleeper(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Sleeper', sleeper_llm)(hooks, events)
        agent.register_hook(Junction.TURN_START, turn_start)
        return agent

    class _Waker(Tool):
        __tool_name__ = 'Waker'
        __tool_command_args__ = ()

        def __init__(self, target: list[str]) -> None:
            self._target = target

        async def execute(self) -> ToolResult:
            """叫醒那个睡着的 Agent。"""
            delivered = await runtime.send(self._target[0], 'wake up')
            return ToolResult(output=str(delivered.ok))

    target: list[str] = []
    waker_llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('w', 'Waker')]), ScriptedTurn(content='done')]
    )
    try:
        target.append(await runtime.spawn(sleeper, query='start'))
        await asyncio.wait_for(runtime.wait(target[0]), _TIMEOUT)
        waker = await runtime.spawn(
            _factory('Waker', waker_llm, {'Waker': _Waker(target)}), query='go'
        )
        await asyncio.wait_for(runtime.wait(waker), _TIMEOUT)
        await asyncio.wait_for(runtime.wait(target[0]), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert woken == [None, None]
    assert ('tool_call', waker, 1, 1, 0, 'message', 0) in ids.issued


async def test_agents_from_a_topology_slot_are_keyed_by_slot_key_and_count():
    from syndica.kernel.catalog import AgentCatalog
    from syndica.kernel.topology import AgentSlot, Topology

    catalog = AgentCatalog()
    catalog.load_mapping({'name': 'Worker', 'description': 'Worker'})
    topology = Topology(
        slots=(
            AgentSlot(name='hand', agent='Worker', cardinality='on-demand', boot='manual'),
            AgentSlot(
                name='by-repo', agent='Worker', cardinality='keyed', key_field='repo', boot='lazy'
            ),
        )
    )
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    try:
        await runtime.assemble(
            topology,
            catalog,
            tools={},
            models={'default': ScriptedLLM([ScriptedTurn(content='ok')])},
        )
        first = await runtime.occupy('hand')
        keyed = await runtime.resolve('by-repo', key='syndica', spawn=True)
        second = await runtime.occupy('hand')
    finally:
        await runtime.shutdown()

    assert ids.issued == {
        ('slot', 'hand', '', 0): first,
        ('slot', 'by-repo', 'syndica', 0): keyed,
        ('slot', 'hand', '', 1): second,
    }


async def test_an_agent_spawned_by_a_hook_during_a_slot_spawn_is_not_keyed_by_the_slot():
    from syndica.kernel.catalog import AgentCatalog
    from syndica.kernel.topology import AgentSlot, Topology

    catalog = AgentCatalog()
    catalog.load_mapping({'name': 'Worker', 'description': 'Worker'})
    topology = Topology(
        slots=(AgentSlot(name='hand', agent='Worker', cardinality='on-demand', boot='manual'),)
    )
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    helpers: list[str] = []
    started: list[bool] = []

    async def on_spawn(**kwargs: Any) -> HookResult:
        if not started:
            started.append(True)
            helpers.append(await runtime.spawn(_idle_child))
        return HookResult()

    runtime.hooks.register(Junction.AGENT_SPAWN, on_spawn)
    try:
        await runtime.assemble(
            topology,
            catalog,
            tools={},
            models={'default': ScriptedLLM([ScriptedTurn(content='ok')])},
        )
        worker = await runtime.occupy('hand')
    finally:
        await runtime.shutdown()

    assert ids.issued == {('slot', 'hand', '', 0): worker, ('host', 'spawn', 0): helpers[0]}


async def test_an_answer_that_arrives_after_the_wait_ended_is_keyed_as_the_hosts():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids, modules=[HumanInteractionModule()])
    events = runtime.events.subscribe()
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('ask', 'AskUser', question='which?', timeout=0.05)]),
            ScriptedTurn(content='done'),
        ]
    )
    try:
        agent_id = await runtime.spawn(_factory('Root', llm), query='go')
        request = await _take(events, 'user_input_requested')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        await runtime.send(
            agent_id,
            'too late',
            activate=False,
            sender='human',
            correlation_id=cast(str, request.payload['request_id']),
        )
    finally:
        events.close()
        await runtime.shutdown()

    assert ('host', 'message', 0) in ids.issued
    assert ('tool_call', agent_id, 1, 1, 0, 'request', 0, 'reply') not in ids.issued


async def _take(events: Any, kind: str) -> Event:
    while True:
        event = await asyncio.wait_for(anext(events), timeout=_TIMEOUT)
        if event.kind == kind:
            return event


async def test_a_human_request_carries_its_key_and_the_answer_is_keyed_by_it():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids, modules=[HumanInteractionModule()])
    events = runtime.events.subscribe()
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('ask', 'AskUser', question='which?', timeout=30)]),
            ScriptedTurn(content='done'),
        ]
    )
    try:
        agent_id = await runtime.spawn(_factory('Root', llm), query='go')
        request = await _take(events, 'user_input_requested')
        await runtime.send(
            agent_id,
            'this one',
            activate=False,
            sender='human',
            correlation_id=cast(str, request.payload['request_id']),
        )
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        await runtime.send(
            agent_id,
            'late',
            activate=False,
            sender='human',
            correlation_id=cast(str, request.payload['request_id']),
        )
    finally:
        events.close()
        await runtime.shutdown()

    key = ['tool_call', agent_id, 1, 1, 0, 'request', 0]
    assert request.payload['request_ref'] == key
    assert (*key, 'reply') in ids.issued
    assert ('host', 'message', 0) in ids.issued


async def test_mailbox_confirmation_events_carry_the_key_of_the_current_tool_call():
    from syndica.kernel import MailboxConfirmationHandler
    from syndica.kernel.danger import ConfirmationRequest, DangerVerdict, RiskLevel
    from syndica.kernel.ids import entering_tool_call, leaving_tool_call
    from syndica.kernel.waiting import WaitResult, WaitState

    class _Runtime:
        def __init__(self) -> None:
            self.events = EventBus()

        async def wait_for(self, agent_id: str, waiter: Any) -> WaitResult:
            return WaitResult(state=WaitState.TIMEOUT)

    host = _Runtime()
    subscription = host.events.subscribe()
    request = ConfirmationRequest(
        request_id='r1',
        agent_id='a1',
        tool_call_id='c1',
        tool_name='Bash',
        arguments='{}',
        verdict=DangerVerdict(rule_id='x', level=RiskLevel.HIGH, reason='why'),
    )
    token = entering_tool_call(ToolCallKey('a1', 2, 3, 1, 'c1'))
    try:
        await MailboxConfirmationHandler(timeout=1).confirm(host, request)
    finally:
        leaving_tool_call(token)
    event = await _take(subscription, 'tool_confirmation_requested')
    subscription.close()

    assert event.payload['request_ref'] == ['tool_call', 'a1', 2, 3, 1, 'request', 0]


@pytest.mark.parametrize('ids', [object(), 'uuid', None.__class__], ids=['object', 'str', 'type'])
def test_ids_must_be_an_id_source(ids: object):
    with pytest.raises(ConfigError) as caught:
        Runtime(ids=cast(Any, ids))

    assert 'IdSource' in caught.value.expected


def test_an_id_source_class_passed_without_constructing_it_is_refused_up_front():
    with pytest.raises(ConfigError) as caught:
        Runtime(ids=cast(Any, _KeyedIds))

    assert '_KeyedIds' in caught.value.received
    assert '_KeyedIds(...)' in caught.value.next_action


async def test_an_id_source_that_raises_is_reported_with_the_key_it_was_asked_for():
    recorded: dict[StableKey, str] = {('host', 'spawn', 0): 'recorded-agent'}

    class _Recorded:
        def agent_id(self, key: StableKey) -> str:
            return recorded[key]

        def message_id(self, key: StableKey) -> str:
            raise KeyError(key)

    runtime = Runtime(ids=_Recorded())
    try:
        agent_id = await runtime.spawn(_idle_child)
        with pytest.raises(ContractViolation) as unknown_agent:
            await runtime.spawn(_idle_child)
        with pytest.raises(ContractViolation) as unknown_message:
            await runtime.send(agent_id, 'hello', activate=False)
    finally:
        await runtime.shutdown()

    assert agent_id == 'recorded-agent'
    assert "('host', 'spawn', 1)" in unknown_agent.value.received
    assert 'KeyError' in unknown_agent.value.received
    assert "('host', 'message', 0)" in unknown_message.value.received
    assert '记录' in unknown_agent.value.next_action
    assert isinstance(unknown_agent.value.__cause__, KeyError)


@pytest.mark.parametrize('value', ['', None, 7], ids=['empty', 'none', 'int'])
async def test_an_id_source_must_hand_back_a_non_empty_string(value: object):
    class _Broken:
        def agent_id(self, key: StableKey) -> str:
            return cast(str, value)

        def message_id(self, key: StableKey) -> str:
            return cast(str, value)

    runtime = Runtime(ids=_Broken())
    try:
        with pytest.raises(ContractViolation) as caught:
            await runtime.spawn(_idle_child)
    finally:
        await runtime.shutdown()

    assert "('host', 'spawn', 0)" in caught.value.received
    assert '非空字符串' in caught.value.expected


async def test_what_an_agents_own_hooks_create_is_keyed_by_that_agent_not_by_arrival():
    """两个 Agent 的 hook 各派一个子 Agent、各发一条消息；记录时 B 先到，回放时 A 先到。"""

    async def run(delays: dict[str, float], ids: _KeyedIds) -> tuple[dict[str, str], list[str]]:
        runtime = Runtime(ids=ids)
        children: dict[str, str] = {}

        def build(name: str):
            llm = ScriptedLLM([ScriptedTurn(content=name)])

            async def turn_start(**kwargs: Any) -> HookResult:
                if name not in children:
                    children[name] = ''
                    await asyncio.sleep(delays[name])
                    children[name] = await runtime.spawn(_idle_child)
                    await runtime.send(children[name], 'hi', activate=False)
                return HookResult()

            def factory(hooks: HooksCenter, events: EventBus) -> BaseAgent:
                agent = _factory(name, llm)(hooks, events)
                agent.register_hook(Junction.TURN_START, turn_start)
                return agent

            return factory

        try:
            parents = [await runtime.spawn(build(name), query='go') for name in ('A', 'B')]
            await asyncio.wait_for(asyncio.gather(*map(runtime.wait, parents)), _TIMEOUT)
        finally:
            await runtime.shutdown()
        return children, parents

    recording = _KeyedIds()
    recorded, parents = await run({'A': 0.03, 'B': 0.01}, recording)
    replayed, replayed_parents = await run({'A': 0.01, 'B': 0.03}, _KeyedIds(recording.issued))

    assert replayed == recorded and replayed_parents == parents
    for name, parent in zip(('A', 'B'), parents, strict=True):
        assert recording.issued[('agent', parent, 'spawn', 0)] == recorded[name]
        assert ('agent', parent, 'message', 0) in recording.issued


async def test_an_agents_own_numbering_carries_on_after_it_is_woken_again():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    spawned: list[str] = []
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])

    async def agent_end(**kwargs: Any) -> HookResult:
        spawned.append(await runtime.spawn(_idle_child))
        return HookResult()

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm)(hooks, events)
        agent.register_hook(Junction.AGENT_END, agent_end)
        return agent

    try:
        agent_id = await runtime.spawn(build, query='go')
        await _until(lambda: len(spawned) == 1)
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        await runtime.send(agent_id, 'again')
        await _until(lambda: len(spawned) == 2)
    finally:
        await runtime.shutdown()

    assert [ids.issued[('agent', agent_id, 'spawn', n)] for n in (0, 1)] == spawned


async def test_what_a_kill_tears_down_is_keyed_by_the_despawn_not_by_the_call_that_killed():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    helpers: list[str] = []
    victim = await runtime.spawn(_idle_child, exit_mode=ExitMode.EXIT)

    async def on_despawn(agent_id: str, **kwargs: Any) -> HookResult:
        if agent_id == victim and not helpers:
            helpers.append('')
            helpers[0] = await runtime.spawn(_idle_child)
        return HookResult()

    class _Killer(Tool):
        __tool_name__ = 'Killer'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """杀掉那个 Agent，再派一个子 Agent。"""
            await runtime.kill(victim)
            return ToolResult(output=await runtime.spawn(_idle_child))

    runtime.hooks.register(Junction.AGENT_DESPAWN, on_despawn)
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('k', 'Killer')]), ScriptedTurn(content='ok')])
    try:
        killer = await runtime.spawn(_factory('Killer', llm, {'Killer': _Killer()}), query='go')
        await asyncio.wait_for(runtime.wait(killer), _TIMEOUT)
        await _until(lambda: bool(helpers and helpers[0]))
    finally:
        await runtime.shutdown()

    call = ('tool_call', killer, 1, 1, 0)
    assert ids.issued[('despawn', victim, 'spawn', 0)] == helpers[0]
    assert (*call, 'spawn', 0) in ids.issued
    assert (*call, 'spawn', 1) not in ids.issued


async def test_what_a_shutdown_hook_sends_is_keyed_by_the_shutdown():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    agent_id = await runtime.spawn(_idle_child)
    delivered: list[bool] = []

    async def on_shutdown(**kwargs: Any) -> HookResult:
        delivered.append((await runtime.send(agent_id, 'bye', activate=False)).ok)
        return HookResult()

    runtime.hooks.register(Junction.RUNTIME_SHUTDOWN, on_shutdown)
    await runtime.shutdown()

    assert delivered == [True]
    assert ('shutdown', 'message', 0) in ids.issued
    assert ('host', 'message', 0) not in ids.issued


async def test_a_send_that_finds_no_one_still_uses_up_its_number():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    try:
        agent_id = await runtime.spawn(_idle_child)
        missed = await runtime.send('nobody', 'lost', activate=False)
        found = await runtime.send(agent_id, 'found', activate=False)
    finally:
        await runtime.shutdown()

    assert missed.ok is False and found.ok is True
    assert ('host', 'message', 0) not in ids.issued
    assert ('host', 'message', 1) in ids.issued


async def test_agents_a_tool_call_takes_from_a_slot_are_keyed_by_that_call():
    from syndica.kernel.catalog import AgentCatalog
    from syndica.kernel.topology import AgentSlot, Topology

    catalog = AgentCatalog()
    catalog.load_mapping({'name': 'Worker', 'description': 'Worker'})
    topology = Topology(
        slots=(AgentSlot(name='hand', agent='Worker', cardinality='on-demand', boot='manual'),)
    )
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    taken: list[str] = []

    class _Occupier(Tool):
        __tool_name__ = 'Occupier'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """从槽里要两个实例，再派一个子 Agent。"""
            taken.extend([await runtime.occupy('hand'), await runtime.occupy('hand')])
            taken.append(await runtime.spawn(_idle_child))
            return ToolResult(output=' '.join(taken))

    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('o', 'Occupier')]), ScriptedTurn(content='ok')]
    )
    try:
        await runtime.assemble(
            topology,
            catalog,
            tools={},
            models={'default': ScriptedLLM([ScriptedTurn(content='ok')])},
        )
        root = await runtime.spawn(_factory('Root', llm, {'Occupier': _Occupier()}), query='go')
        await asyncio.wait_for(runtime.wait(root), _TIMEOUT)
    finally:
        await runtime.shutdown()

    call = ('tool_call', root, 1, 1, 0)
    assert taken == [
        ids.issued[('slot', 'hand', '', *call, 'spawn', 0)],
        ids.issued[('slot', 'hand', '', *call, 'spawn', 1)],
        ids.issued[(*call, 'spawn', 2)],
    ]


async def test_positions_count_across_the_whole_batch_not_within_a_segment():
    seen: list[ToolCallKey | None] = []
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[_call('a', 'Probe'), _call('b', 'SerialProbe'), _call('c', 'Probe')]
            ),
            ScriptedTurn(content='done'),
        ]
    )
    tools: dict[str, Tool] = {'Probe': _Probe(seen), 'SerialProbe': _SerialProbe(seen)}
    runtime = Runtime()
    try:
        agent_id = await runtime.spawn(_factory('Root', llm, tools), query='go')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert seen == [
        ToolCallKey(agent_id, 1, 1, 0, 'a'),
        ToolCallKey(agent_id, 1, 1, 1, 'b'),
        ToolCallKey(agent_id, 1, 1, 2, 'c'),
    ]


async def test_a_woken_agents_calls_carry_its_cycle_number():
    seen: list[ToolCallKey | None] = []
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('p1', 'Probe')]),
            ScriptedTurn(content='first'),
            ScriptedTurn(tool_calls=[_call('p2', 'Probe')]),
            ScriptedTurn(content='second'),
        ]
    )
    runtime = Runtime()
    try:
        agent_id = await runtime.spawn(_factory('Root', llm, {'Probe': _Probe(seen)}), query='go')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        await runtime.send(agent_id, 'again')
        await _until(lambda: len(seen) == 2)
    finally:
        await runtime.shutdown()

    assert seen == [ToolCallKey(agent_id, 1, 1, 0, 'p1'), ToolCallKey(agent_id, 2, 3, 0, 'p2')]


async def test_each_wait_in_a_call_has_its_own_request_key_and_only_the_first_answer_takes_it():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    awaits: list[_Await] = []
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('w', 'Await', first='a', second='b')]),
            ScriptedTurn(content='done'),
            ScriptedTurn(content='more'),
        ]
    )
    try:
        agent_id = await runtime.spawn(_awaiting(runtime, 'Root', llm, awaits), query='go')
        await _until(lambda: bool(awaits) and awaits[0].started == ['a'])
        await runtime.send(agent_id, 'answer a', activate=False, correlation_id='a')
        await runtime.send(agent_id, 'again a', activate=False, correlation_id='a')
        await _until(lambda: awaits[0].started == ['a', 'b'])
        await runtime.send(agent_id, 'answer b', activate=False, correlation_id='b')
        await _until(lambda: len(awaits[0].results) == 2)
    finally:
        await runtime.shutdown()

    call = ('tool_call', agent_id, 1, 1, 0)
    assert awaits[0].results == [('a', WaitState.ANSWERED), ('b', WaitState.ANSWERED)]
    assert (*call, 'request', 0, 'reply') in ids.issued
    assert (*call, 'request', 1, 'reply') in ids.issued
    assert ('host', 'message', 0) in ids.issued


async def test_an_answer_sent_from_a_tool_call_leaves_the_calls_message_numbers_alone():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    awaits: list[_Await] = []
    waiting_llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('w', 'Await', first='q')]),
            ScriptedTurn(content='done'),
            ScriptedTurn(content='more'),
        ]
    )
    target: list[str] = []

    class _Answerer(Tool):
        __tool_name__ = 'Answerer'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """答复那次等待，再补一句。"""
            await runtime.send(target[0], 'answer', activate=False, correlation_id='q')
            await runtime.send(target[0], 'note', activate=False)
            return ToolResult(output='sent')

    answerer_llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('s', 'Answerer')]), ScriptedTurn(content='ok')]
    )
    try:
        target.append(
            await runtime.spawn(_awaiting(runtime, 'Waiting', waiting_llm, awaits), query='go')
        )
        await _until(lambda: bool(awaits) and awaits[0].started == ['q'])
        answerer = await runtime.spawn(
            _factory('Answerer', answerer_llm, {'Answerer': _Answerer()}), query='go'
        )
        await asyncio.wait_for(runtime.wait(answerer), _TIMEOUT)
        await _until(lambda: len(awaits[0].results) == 1)
    finally:
        await runtime.shutdown()

    call = ('tool_call', answerer, 1, 1, 0)
    assert ('tool_call', target[0], 1, 1, 0, 'request', 0, 'reply') in ids.issued
    assert (*call, 'message', 0) in ids.issued
    assert (*call, 'message', 1) not in ids.issued


async def test_two_agents_waiting_on_the_same_correlation_each_keep_their_own_request_key():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    awaits: list[_Await] = []

    def build(name: str):
        llm = ScriptedLLM(
            [
                ScriptedTurn(tool_calls=[_call('w', 'Await', first='same')]),
                ScriptedTurn(content='ok'),
            ]
        )
        return _awaiting(runtime, name, llm, awaits)

    try:
        first = await runtime.spawn(build('A'), query='go')
        second = await runtime.spawn(build('B'), query='go')
        await _until(lambda: len(awaits) == 2 and all(tool.started for tool in awaits))
        await runtime.send(second, 'to B', activate=False, correlation_id='same')
        await runtime.send(first, 'to A', activate=False, correlation_id='same')
        await asyncio.wait_for(asyncio.gather(runtime.wait(first), runtime.wait(second)), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert ('tool_call', first, 1, 1, 0, 'request', 0, 'reply') in ids.issued
    assert ('tool_call', second, 1, 1, 0, 'request', 0, 'reply') in ids.issued
    assert ('host', 'message', 0) not in ids.issued


async def test_a_preempted_wait_leaves_the_request_key_of_the_wait_that_preempted_it():
    """挂起中的 Agent 上第二次等待抢占第一次（同一个关联 ID）：被抢占的那次收尾时不能摘掉抢占者
    的登记。"""
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    awaits: list[_Await] = []
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    _call('x', 'Await', first='c', delay=0.05),
                    _call('y', 'Await', first='c', delay=0.15),
                ]
            ),
            ScriptedTurn(content='done'),
        ]
    )
    try:
        agent_id = await runtime.spawn(_awaiting(runtime, 'Root', llm, awaits), query='go')
        await _until(lambda: bool(awaits) and len(awaits[0].entered) == 2)
        await runtime.pause(agent_id)
        await _until(lambda: awaits[0].results == [('c', WaitState.PREEMPTED)])
        await runtime.send(agent_id, 'answer', activate=False, correlation_id='c')
        await _until(lambda: len(awaits[0].results) == 2)
        await runtime.resume(agent_id)
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert awaits[0].results == [('c', WaitState.PREEMPTED), ('c', WaitState.ANSWERED)]
    assert context.stop_reason is StopReason.COMPLETED
    assert ('tool_call', agent_id, 1, 1, 1, 'request', 0, 'reply') in ids.issued
    assert ('host', 'message', 0) not in ids.issued


async def test_a_wait_that_never_starts_leaves_no_request_key_behind():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    states: list[WaitState] = []

    class _GiveUp(Tool):
        __tool_name__ = 'GiveUp'
        __tool_command_args__ = ()

        def __init__(self, agent_id: str) -> None:
            self._agent_id = agent_id

        async def execute(self) -> ToolResult:
            """中止自己，再开始等一条答复。"""
            await runtime.abort(self._agent_id)
            result = await runtime.wait_for(
                self._agent_id, Reply(correlation_id='late', timeout=_TIMEOUT)
            )
            states.append(result.state)
            return ToolResult(output=result.state.value)

    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('g', 'GiveUp')]), ScriptedTurn(content='ok')])

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm)(hooks, events)
        agent.install_runtime_tools({'GiveUp': _GiveUp(agent.agent_id)})
        return agent

    try:
        agent_id = await runtime.spawn(build, query='go')
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        await runtime.send(agent_id, 'late answer', activate=False, correlation_id='late')
    finally:
        await runtime.shutdown()

    assert states == [WaitState.ABORTED]
    assert context.stop_reason is StopReason.ABORTED
    assert ('host', 'message', 0) in ids.issued
    assert ('tool_call', agent_id, 1, 1, 0, 'request', 0, 'reply') not in ids.issued


async def test_a_confirmation_is_answered_under_the_key_its_event_announced():
    class _ConfirmProbes:
        def decide(self, request: DangerRequest) -> Disposition:
            return Disposition.CONFIRM

    ids = _KeyedIds()
    runtime = Runtime(ids=ids, confirmation_handler=MailboxConfirmationHandler(timeout=30))
    events = runtime.events.subscribe()
    seen: list[ToolCallKey | None] = []
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('c1', 'Probe')]), ScriptedTurn(content='ok')])

    def build(hooks: HooksCenter, bus: EventBus) -> BaseAgent:
        options = AgentOptions(
            name='Root', llm=llm, tools={'Probe': _Probe(seen)}, danger_policy=_ConfirmProbes()
        )
        return BaseAgent(options, hooks=hooks, events=bus)

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

    key = ['tool_call', agent_id, 1, 1, 0, 'request', 0]
    assert requested.payload['request_ref'] == key
    assert (*key, 'reply') in ids.issued
    assert seen == [ToolCallKey(agent_id, 1, 1, 0, 'c1')]
    stored = json.loads(encode_redacted(requested.payload, RuleRedactor()))
    assert stored['request_ref'] == key


async def test_a_human_request_reference_survives_redaction_before_it_is_stored():
    runtime = Runtime(ids=_KeyedIds(), modules=[HumanInteractionModule()])
    events = runtime.events.subscribe()
    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('ask', 'AskUser', question='which?', timeout=0.05)])]
        + [ScriptedTurn(content='done')]
    )
    try:
        agent_id = await runtime.spawn(_factory('Root', llm), query='go')
        request = await _take(events, 'user_input_requested')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        events.close()
        await runtime.shutdown()

    stored = json.loads(encode_redacted(request.payload, RuleRedactor()))
    assert stored['request_ref'] == ['tool_call', agent_id, 1, 1, 0, 'request', 0]


# --- topology slots ---------------------------------------------------------------------------


def _topology(*slots: Any) -> tuple[Any, Any]:
    from syndica.kernel.catalog import AgentCatalog
    from syndica.kernel.topology import Topology

    catalog = AgentCatalog()
    catalog.load_mapping({'name': 'Worker', 'description': 'Worker'})
    return Topology(slots=slots), catalog


async def test_a_lazy_singleton_gets_the_same_id_whoever_reaches_it_first():
    """两个 Agent 的调用都往同一个 lazy singleton 发消息；记录时 B 先到，回放时 A 先到。先到者在
    同一次调用里接着派的子 Agent 也不能因此换号。"""
    from syndica.kernel.topology import AgentSlot

    async def run(delays: dict[str, float], ids: _KeyedIds) -> tuple[str, dict[str, str]]:
        topology, catalog = _topology(
            AgentSlot(name='reviewer', agent='Worker', cardinality='singleton', boot='lazy')
        )
        runtime = Runtime(ids=ids)
        children: dict[str, str] = {}

        class _Ask(Tool):
            __tool_name__ = 'Ask'
            __tool_command_args__ = ()

            def __init__(self, name: str) -> None:
                self._name = name

            async def execute(self) -> ToolResult:
                """问 reviewer，再派一个子 Agent。"""
                await asyncio.sleep(delays[self._name])
                await runtime.send_to_slot('reviewer', f'from {self._name}')
                children[self._name] = await runtime.spawn(_idle_child)
                return ToolResult(output='asked')

        await runtime.assemble(
            topology,
            catalog,
            tools={},
            models={'default': ScriptedLLM([ScriptedTurn(content='ok')] * 4)},
        )
        try:
            askers = []
            for name in ('A', 'B'):
                llm = ScriptedLLM(
                    [ScriptedTurn(tool_calls=[_call('a', 'Ask')]), ScriptedTurn(content='done')]
                )
                askers.append(
                    await runtime.spawn(_factory(name, llm, {'Ask': _Ask(name)}), query='go')
                )
            await asyncio.wait_for(asyncio.gather(*map(runtime.wait, askers)), _TIMEOUT)
            reviewer = await runtime.resolve('reviewer')
        finally:
            await runtime.shutdown()
        assert reviewer is not None
        return reviewer, children

    recording = _KeyedIds()
    recorded = await run({'A': 0.03, 'B': 0.0}, recording)
    replayed = await run({'A': 0.0, 'B': 0.03}, _KeyedIds(recording.issued))

    assert replayed == recorded
    assert recording.issued[('slot', 'reviewer', '', 0)] == recorded[0]
    askers = [recording.issued[('host', 'spawn', n)] for n in (0, 1)]
    for name, asker in zip(('A', 'B'), askers, strict=True):
        assert recording.issued[('tool_call', asker, 1, 1, 0, 'spawn', 0)] == recorded[1][name]


async def test_each_key_of_a_keyed_slot_is_numbered_on_its_own():
    from syndica.kernel.topology import AgentSlot

    topology, catalog = _topology(
        AgentSlot(
            name='by-repo', agent='Worker', cardinality='keyed', key_field='repo', boot='lazy'
        )
    )
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    try:
        await runtime.assemble(
            topology,
            catalog,
            tools={},
            models={'default': ScriptedLLM([ScriptedTurn(content='ok')])},
        )
        first = await runtime.resolve('by-repo', key='alpha', spawn=True)
        second = await runtime.resolve('by-repo', key='beta', spawn=True)
    finally:
        await runtime.shutdown()

    assert ids.issued == {
        ('slot', 'by-repo', 'alpha', 0): first,
        ('slot', 'by-repo', 'beta', 0): second,
    }


# --- waits ------------------------------------------------------------------------------------


class _SlowRegistry(InMemoryAgentRegistry):
    """每次 CAS 都要等一会儿：远端注册表的往返。"""

    async def compare_and_set_status(self, agent_id: str, *, expected: Any, new: Any) -> bool:
        await asyncio.sleep(0.005)
        return await super().compare_and_set_status(agent_id, expected=expected, new=new)


def test_an_answer_takes_the_newest_registration_and_leaves_the_older_one():
    runtime = Runtime(ids=_KeyedIds())
    older = ('tool_call', 'a1', 1, 1, 0, 'request', 0)
    newer = ('tool_call', 'a1', 1, 1, 1, 'request', 0)
    registry = runtime._request_keys  # pyright: ignore[reportPrivateUsage]
    registry[('a1', 'c')] = [older, newer]

    first = runtime._message_key('a1', 'c')  # pyright: ignore[reportPrivateUsage]
    second = runtime._message_key('a1', 'c')  # pyright: ignore[reportPrivateUsage]

    assert (first, second) == ((*newer, 'reply'), (*older, 'reply'))
    assert registry == {}


async def test_an_answer_given_the_moment_the_request_is_announced_is_keyed_as_its_reply():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids, registry=_SlowRegistry(), modules=[HumanInteractionModule()])
    events = runtime.events.subscribe()
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('ask', 'AskUser', question='which?', timeout=30)]),
            ScriptedTurn(content='done'),
        ]
    )

    async def answer_at_once() -> None:
        request = await _take(events, 'user_input_requested')
        await runtime.send(
            request.agent_id,
            'this one',
            activate=False,
            sender='human',
            correlation_id=cast(str, request.payload['request_id']),
        )

    try:
        answering = asyncio.create_task(answer_at_once())
        agent_id = await runtime.spawn(_factory('Root', llm), query='go')
        await asyncio.wait_for(answering, _TIMEOUT)
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        events.close()
        await runtime.shutdown()

    assert context.stop_reason is StopReason.COMPLETED
    assert ('tool_call', agent_id, 1, 1, 0, 'request', 0, 'reply') in ids.issued
    assert ('host', 'message', 0) not in ids.issued


async def test_a_refused_wait_still_uses_up_its_request_number():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    started = asyncio.Event()
    idle: list[str] = []

    class _TryTwice(Tool):
        __tool_name__ = 'TryTwice'
        __tool_command_args__ = ()

        def __init__(self, agent_id: str) -> None:
            self._agent_id = agent_id

        async def execute(self) -> ToolResult:
            """先在一个没在跑的 Agent 上等（被拒），再在自己身上等。"""
            with pytest.raises(ContractViolation):
                await runtime.wait_for(idle[0], Reply(correlation_id='nope', timeout=_TIMEOUT))
            started.set()
            result = await runtime.wait_for(
                self._agent_id, Reply(correlation_id='mine', timeout=_TIMEOUT)
            )
            return ToolResult(output=result.state.value)

    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[_call('t', 'TryTwice')]), ScriptedTurn(content='ok')]
    )

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm)(hooks, events)
        agent.install_runtime_tools({'TryTwice': _TryTwice(agent.agent_id)})
        return agent

    try:
        idle.append(await runtime.spawn(_idle_child))
        agent_id = await runtime.spawn(build, query='go')
        await asyncio.wait_for(started.wait(), _TIMEOUT)
        await runtime.send(agent_id, 'answer', activate=False, correlation_id='mine')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        await runtime.send(idle[0], 'to the refused wait', activate=False, correlation_id='nope')
        leftover = dict(runtime._request_keys)  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()

    call = ('tool_call', agent_id, 1, 1, 0)
    assert (*call, 'request', 1, 'reply') in ids.issued
    assert (*call, 'request', 0, 'reply') not in ids.issued
    assert ('host', 'message', 0) in ids.issued
    assert leftover == {}


async def test_a_wait_the_host_starts_registers_nothing():
    runtime = Runtime(ids=_KeyedIds())
    entered = asyncio.Event()
    release = asyncio.Event()

    class _Hold(Tool):
        __tool_name__ = 'Hold'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """挂住，直到被放开。"""
            entered.set()
            await release.wait()
            return ToolResult(output='released')

    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('h', 'Hold')]), ScriptedTurn(content='ok')])
    try:
        agent_id = await runtime.spawn(_factory('Root', llm, {'Hold': _Hold()}), query='go')
        await asyncio.wait_for(entered.wait(), _TIMEOUT)
        waiting = asyncio.create_task(
            runtime.wait_for(agent_id, Reply(correlation_id='h1', timeout=_TIMEOUT))
        )
        await asyncio.sleep(0.01)
        during = dict(runtime._request_keys)  # pyright: ignore[reportPrivateUsage]
        await runtime.send(agent_id, 'answer', activate=False, correlation_id='h1')
        await asyncio.wait_for(waiting, _TIMEOUT)
        release.set()
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        after = dict(runtime._request_keys)  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()

    assert during == {} and after == {}


async def test_a_correlation_id_that_is_not_a_string_is_not_looked_up():
    runtime = Runtime(ids=_KeyedIds())
    try:
        agent_id = await runtime.spawn(_idle_child)
        delivered = await runtime.send(
            agent_id, 'odd', activate=False, correlation_id=cast(Any, ['not', 'hashable'])
        )
    finally:
        await runtime.shutdown()

    assert isinstance(delivered.ok, bool)


# --- assignment, despawn, warnings ------------------------------------------------------------


async def test_a_task_the_factory_starts_does_not_take_the_assigned_id_too():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    late: list[BaseAgent] = []
    tasks: list[asyncio.Task[None]] = []

    def factory(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        async def build_later() -> None:
            late.append(_idle_child(HooksCenter(), EventBus()))

        tasks.append(asyncio.get_running_loop().create_task(build_later()))
        return _idle_child(hooks, events)

    try:
        agent_id = await runtime.spawn(factory)
        await asyncio.wait_for(asyncio.gather(*tasks), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert agent_id == ids.issued[('host', 'spawn', 0)]
    assert late[0].agent_id != agent_id


async def test_a_task_the_factory_starts_cannot_take_an_id_the_factory_never_used():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    late: list[BaseAgent] = []
    tasks: list[asyncio.Task[None]] = []
    prebuilt = _idle_child(HooksCenter(), EventBus())

    def factory(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        async def build_later() -> None:
            await asyncio.sleep(0)
            late.append(_idle_child(HooksCenter(), EventBus()))

        tasks.append(asyncio.get_running_loop().create_task(build_later()))
        return prebuilt

    try:
        await runtime.spawn(factory)
        await asyncio.wait_for(asyncio.gather(*tasks), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert late[0].agent_id != ids.issued[('host', 'spawn', 0)]


async def test_a_despawn_inside_a_call_hands_the_call_its_scope_back():
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    helpers: list[str] = []
    after: list[str] = []
    doomed = await runtime.spawn(_idle_child)

    async def on_despawn(agent_id: str, **kwargs: Any) -> HookResult:
        if agent_id == doomed:
            helpers.append(await runtime.spawn(_idle_child))
        return HookResult()

    class _Remove(Tool):
        __tool_name__ = 'Remove'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """销毁那个 Agent，再派一个子 Agent。"""
            await runtime.despawn(doomed)
            after.append(await runtime.spawn(_idle_child))
            return ToolResult(output=after[0])

    runtime.hooks.register(Junction.AGENT_DESPAWN, on_despawn)
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('r', 'Remove')]), ScriptedTurn(content='ok')])
    try:
        remover = await runtime.spawn(_factory('Remover', llm, {'Remove': _Remove()}), query='go')
        await asyncio.wait_for(runtime.wait(remover), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert ids.issued[('despawn', doomed, 'spawn', 0)] == helpers[0]
    assert ids.issued[('tool_call', remover, 1, 1, 0, 'spawn', 0)] == after[0]


async def test_what_a_despawn_hook_creates_is_keyed_by_the_despawn_and_refusals_use_numbers():
    """级联拆除父时，子的拆除 hook 想给父再派一个子：父正在被拆，这次被拒，但它占掉序号。"""
    ids = _KeyedIds()
    runtime = Runtime(ids=ids)
    created: list[str] = []
    refused: list[str] = []
    parents: list[str] = []

    async def on_despawn(agent_id: str, **kwargs: Any) -> HookResult:
        if not parents or agent_id == parents[0] or created:
            return HookResult()
        try:
            await runtime.spawn(_idle_child, parent_id=parents[0])
        except ConfigError as error:
            refused.append(type(error).__name__)
        except ContractViolation as error:
            refused.append(type(error).__name__)
        created.append(await runtime.spawn(_idle_child))
        return HookResult()

    runtime.hooks.register(Junction.AGENT_DESPAWN, on_despawn)
    try:
        parents.append(await runtime.spawn(_idle_child))
        child = await runtime.spawn(_idle_child, parent_id=parents[0])
        await runtime.despawn(parents[0])
    finally:
        await runtime.shutdown()

    assert len(refused) == 1
    assert ('despawn', child, 'spawn', 0) not in ids.issued
    assert ids.issued[('despawn', child, 'spawn', 1)] == created[0]


async def test_a_source_that_subclasses_random_ids_still_hears_about_a_missed_assignment(
    caplog: pytest.LogCaptureFixture,
):
    class _Recording(RandomIds):
        pass

    runtime = Runtime(ids=_Recording())

    def factory(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        _idle_child(hooks, events)
        return _idle_child(hooks, events)

    try:
        await runtime.spawn(factory)
    finally:
        await runtime.shutdown()

    assert len(_missed_assignments(caplog)) == 1


# --- the source's own contract ----------------------------------------------------------------


def test_an_id_source_whose_methods_are_not_callable_is_refused_with_their_names():
    class _Half:
        agent_id = 'fixed'

        def message_id(self, key: StableKey) -> str:
            return 'm'

    with pytest.raises(ConfigError) as caught:
        Runtime(ids=cast(Any, _Half()))

    assert 'agent_id' in caught.value.received and 'message_id' not in caught.value.received


@pytest.mark.parametrize('missing', ['agent_id', 'message_id'])
def test_an_object_missing_a_method_is_told_which_one(missing: str):
    present = {'agent_id', 'message_id'} - {missing}
    source = type('_Partial', (), {name: lambda self, key: 'x' for name in present})()

    with pytest.raises(ConfigError) as caught:
        Runtime(ids=cast(Any, source))

    assert missing in caught.value.received
    assert 'uuid4' in caught.value.next_action


async def test_an_async_id_source_is_told_to_be_synchronous():
    class _Async:
        async def agent_id(self, key: StableKey) -> str:
            return 'never'

        async def message_id(self, key: StableKey) -> str:
            return 'never'

    runtime = Runtime(ids=cast(Any, _Async()))
    try:
        with pytest.raises(ContractViolation) as caught:
            await runtime.spawn(_idle_child)
    finally:
        await runtime.shutdown()

    assert 'agent_id(key)' in caught.value.expected
    assert 'awaitable' in caught.value.received
    assert 'def' in caught.value.next_action


async def test_a_bad_message_id_names_the_message_method():
    class _BadMessages:
        def agent_id(self, key: StableKey) -> str:
            return 'agent-1'

        def message_id(self, key: StableKey) -> str:
            return ''

    runtime = Runtime(ids=_BadMessages())
    try:
        agent_id = await runtime.spawn(_idle_child)
        with pytest.raises(ContractViolation) as caught:
            await runtime.send(agent_id, 'hello', activate=False)
    finally:
        await runtime.shutdown()

    assert 'message_id(key)' in caught.value.expected
    assert '记录里的那个 id' in caught.value.next_action


# --- the tool-call scope ----------------------------------------------------------------------


async def test_post_tool_use_runs_inside_the_call_it_reports_on():
    seen: list[ToolCallKey | None] = []

    async def post(**kwargs: Any) -> HookResult:
        seen.append(current_tool_call())
        return HookResult()

    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('a', 'Probe', delay=0.02), _call('b', 'Probe')]),
            ScriptedTurn(content='done'),
        ]
    )
    probes: list[ToolCallKey | None] = []

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory('Root', llm, {'Probe': _Probe(probes)})(hooks, events)
        agent.register_hook(Junction.POST_TOOL_USE, post)
        return agent

    runtime = Runtime()
    try:
        agent_id = await runtime.spawn(build, query='go')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert seen == [ToolCallKey(agent_id, 1, 1, 1, 'b'), ToolCallKey(agent_id, 1, 1, 0, 'a')]


async def test_an_agent_run_inline_inside_a_call_leaves_the_calls_scope_as_it_was():
    after: list[ToolCallKey | None] = []

    class _Inline(Tool):
        __tool_name__ = 'Inline'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """在这次调用里直接跑一个带工具的 Agent，再看当前工具调用。"""
            helper_llm = ScriptedLLM(
                [ScriptedTurn(tool_calls=[_call('h', 'Probe')]), ScriptedTurn(content='h')]
            )
            helper = _factory('Helper', helper_llm, {'Probe': _Probe([])})(
                HooksCenter(), EventBus()
            )
            await helper.run('help')
            after.append(current_tool_call())
            return ToolResult(output='inline')

    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('i', 'Inline')]), ScriptedTurn(content='ok')])
    runtime = Runtime()
    try:
        agent_id = await runtime.spawn(_factory('Root', llm, {'Inline': _Inline()}), query='go')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert after == [ToolCallKey(agent_id, 1, 1, 0, 'i')]
