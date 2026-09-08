"""本模块**必须**保留 `from __future__ import annotations`：仓库每个模块都带它，于是标注在运行时
是字符串。少了它，这里的工具类持有真类型对象，而 schema 反射按字符串取标注的缺陷在这里全绿、在每个
真实工具上必炸。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.agent_types import AgentInfo, AgentStatus, ExitMode
from syndica.kernel.context import StopReason, ToolCall
from syndica.kernel.errors import ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.message import DeliveryResult, Message, Trigger
from syndica.kernel.protocols import AgentRegistry, MissingQueueCallback
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.stores import InMemoryAgentRegistry, InMemoryMessageBroker
from syndica.kernel.tool import Tool, ToolResult

# 每个等待都设上限：回归应当让用例变红，而不是把 CI 挂住。
_TIMEOUT = 1.0
_SYSTEM_PROMPT = 'You are a worker.'
# 注册顺序既非升序（Concat / Echo / Upper）亦非降序，反转后同样两者都不是：n=1 或恰好有序时，
# 「保持注册顺序」与「排序」「反转」是同一个答案，而工具顺序逐字进请求前缀（spec §5.2）。
_TOOL_NAMES = ['Echo', 'Upper', 'Concat']
# 分隔符带前导空格：DeliveryResult.reason 是单行三件套，不带空格会匹配到被嵌进去的另一份。
_THREE_PART: tuple[str, ...] = ('期望: ', ' 实际: ', ' 下一步: ')


class _Echo(Tool):
    async def execute(self, text: str) -> ToolResult:
        """回显 text。"""
        return ToolResult(output=text)


class _Upper(Tool):
    async def execute(self, text: str) -> ToolResult:
        """把 text 转成大写。"""
        return ToolResult(output=text.upper())


class _Concat(Tool):
    async def execute(self, left: str, right: str) -> ToolResult:
        """拼接 left 与 right。"""
        return ToolResult(output=left + right)


class _Specialist(BaseAgent):
    """agent_type 取自实例的类名：只有一个 Agent 类时，写死的类名与真取值是同一个答案。"""


class _Block(Tool):
    """进入后挂起，直到测试放行。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self) -> ToolResult:
        """一直等到测试放行。"""
        self.entered.set()
        await self.release.wait()
        return ToolResult(output='released')


class _Gate(Tool):
    """每次调用停在自己的那道闸上：一个周期里需要两个可控停点时用它。

    第二个停点是「暂存队列已经装上东西、而这个周期还没结束」这一刻的唯一观测点——延迟消息
    只在轮边界进暂存，而周期结束时它们又立刻被取走。
    """

    def __init__(self, stops: int) -> None:
        self.entered = [asyncio.Event() for _ in range(stops)]
        self.release = [asyncio.Event() for _ in range(stops)]
        self._calls = 0

    async def execute(self) -> ToolResult:
        """一直等到测试放行本次调用。"""
        index = self._calls
        self._calls += 1
        self.entered[index].set()
        await self.release[index].wait()
        return ToolResult(output=f'released {index}')


class _RecordingRegistry:
    """记下每一次状态转换（尝试的与真的写进去的），并可让指定的一次转换失败。

    转换记录是「注册表看到了什么」的唯一证据：EXIT 的条目紧接着被 unregister 抹掉，只看注册表
    的最终内容分不出它中途走过哪些状态。尝试与生效分开记：`_switch` 逐个试 CAS，失败的那些是
    实现细节，走过的状态路径才是行为。
    """

    def __init__(self, *, deny: tuple[AgentStatus, AgentStatus] | None = None) -> None:
        self._inner = InMemoryAgentRegistry()
        self._deny = deny
        self.transitions: list[tuple[str, AgentStatus, AgentStatus]] = []
        self.applied: list[tuple[str, AgentStatus, AgentStatus]] = []

    async def register(self, info: AgentInfo) -> None:
        await self._inner.register(info)

    async def get(self, agent_id: str) -> AgentInfo | None:
        return await self._inner.get(agent_id)

    async def compare_and_set_status(
        self, agent_id: str, *, expected: AgentStatus, new: AgentStatus
    ) -> bool:
        self.transitions.append((agent_id, expected, new))
        if self._deny == (expected, new):
            return False
        applied = await self._inner.compare_and_set_status(agent_id, expected=expected, new=new)
        if applied:
            self.applied.append((agent_id, expected, new))
        return applied

    async def update(self, agent_id: str, **fields: Any) -> None:
        await self._inner.update(agent_id, **fields)

    async def children_of(self, agent_id: str) -> list[str]:
        return await self._inner.children_of(agent_id)

    async def list(
        self, *, status: AgentStatus | None = None, parent_id: str | None = None
    ) -> list[AgentInfo]:
        return await self._inner.list(status=status, parent_id=parent_id)

    async def unregister(self, agent_id: str) -> None:
        await self._inner.unregister(agent_id)

    def of(self, agent_id: str) -> list[tuple[AgentStatus, AgentStatus]]:
        return [
            (expected, new) for recorded, expected, new in self.transitions if recorded == agent_id
        ]

    def applied_of(self, agent_id: str) -> list[tuple[AgentStatus, AgentStatus]]:
        return [(expected, new) for recorded, expected, new in self.applied if recorded == agent_id]


class _SpyBroker:
    """观测 broker 这一侧的接缝：谁停在邮箱上等消息，以及摘队列的那一刻注册表怎么说。

    「事件循环在等消息」（spec §4.1 给 IDLE 的定义）在 Runtime 外面没有别的观测点——注册表里的
    `IDLE` 与「刚注册、驱动任务还没跑起来」是同一个取值，拿它当握手信号分不出这两件事。
    """

    def __init__(self, registry: AgentRegistry | None = None) -> None:
        self._inner = InMemoryMessageBroker()
        self._registry = registry
        # 边沿信号：入园即置位，等待者扫描前清位。「已入园 n 次」是电平，等的是「又入了一次」。
        self._parked = asyncio.Event()
        self._parks: dict[str, int] = {}
        self.removals: list[tuple[str, AgentStatus | None]] = []

    def parks(self, agent_id: str) -> int:
        return self._parks.get(agent_id, 0)

    async def reach(self, agent_id: str, parks: int) -> None:
        """等到该 agent 第 `parks` 次停到邮箱上。上界由调用方套一层 `wait_for` 给。"""
        while True:
            self._parked.clear()
            if self.parks(agent_id) >= parks:
                return
            await self._parked.wait()

    async def ensure_queue(self, agent_id: str) -> None:
        await self._inner.ensure_queue(agent_id)

    async def publish(self, agent_id: str, message: Message) -> DeliveryResult:
        return await self._inner.publish(agent_id, message)

    async def consume(self, agent_id: str, *, max_n: int = 0) -> list[Message]:
        return await self._inner.consume(agent_id, max_n=max_n)

    async def wait_for_messages(
        self,
        agent_id: str,
        timeout: float | None,  # noqa: ASYNC109
    ) -> bool:
        # 只有 timeout=None 才是「停在邮箱上等」。timeout=0 是纯轮询——问一句「现在有吗」，
        # 问完就走；把它也算成入园会让 parks 从「循环在等消息」退化成「循环碰过 broker」。
        if timeout is None:
            self._parks[agent_id] = self.parks(agent_id) + 1
            self._parked.set()
        return await self._inner.wait_for_messages(agent_id, timeout)

    async def take_by_correlation(
        self,
        agent_id: str,
        correlation_id: str,
        timeout: float | None,  # noqa: ASYNC109
    ) -> Message | None:
        return await self._inner.take_by_correlation(agent_id, correlation_id, timeout)

    async def remove_queue(self, agent_id: str) -> None:
        self.removals.append((agent_id, await self._seen_status(agent_id)))
        await self._inner.remove_queue(agent_id)

    def on_missing_queue(self, cb: MissingQueueCallback) -> None:
        self._inner.on_missing_queue(cb)

    async def _seen_status(self, agent_id: str) -> AgentStatus | None:
        if self._registry is None:
            return None
        info = await self._registry.get(agent_id)
        return None if info is None else info.status


def _tools() -> dict[str, Tool]:
    return {'Echo': _Echo(), 'Upper': _Upper(), 'Concat': _Concat()}


def _done(content: str = 'done') -> ScriptedLLM:
    return ScriptedLLM([ScriptedTurn(content=content)])


def _factory(
    llm: ScriptedLLM,
    *,
    name: str = 'Worker',
    tools: dict[str, Tool] | None = None,
    cls: type[BaseAgent] = BaseAgent,
) -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return cls(
            AgentOptions(
                name=name,
                llm=llm,
                tools=_tools() if tools is None else tools,
                system_prompt=_SYSTEM_PROMPT,
            ),
            hooks=hooks,
            events=events,
        )

    return build


def _capturing(built: list[BaseAgent], llm: ScriptedLLM) -> AgentFactory:
    inner = _factory(llm)

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = inner(hooks, events)
        built.append(agent)
        return agent

    return build


def _blocking(blocker: _Block) -> AgentFactory:
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Block', arguments='{}')]),
            ScriptedTurn(content='done'),
        ]
    )
    return _factory(llm, tools={'Block': blocker})


def _assert_three_part(text: str) -> None:
    positions = [text.find(mark) for mark in _THREE_PART]
    assert -1 not in positions
    assert positions == sorted(positions)


async def _status(runtime: Runtime, agent_id: str) -> AgentStatus | None:
    info = await runtime.find(agent_id)
    return None if info is None else info.status


async def _info(runtime: Runtime, agent_id: str) -> AgentInfo:
    info = await runtime.find(agent_id)
    assert info is not None
    return info


async def _mailbox_exists(broker: InMemoryMessageBroker, agent_id: str) -> bool:
    """邮箱在不在。没有 on_missing_queue 回调时，publish 失败当且仅当邮箱不存在。"""
    result = await broker.publish(
        agent_id, Message(sender='probe', receiver=agent_id, content='probe')
    )
    return result.ok


async def test_spawn_gives_each_agent_its_own_id_and_registry_entry():
    runtime = Runtime()
    names = ['Beta', 'Alpha', 'Gamma']
    ids = [await runtime.spawn(_factory(_done(), name=name), query='go') for name in names]
    assert len(set(ids)) == len(names)
    found = [await runtime.find(agent_id) for agent_id in ids]
    assert [info.name for info in found if info is not None] == names
    assert [info.agent_id for info in found if info is not None] == ids
    await runtime.shutdown()


async def test_spawn_files_each_child_under_its_own_parent():
    runtime = Runtime()
    parents = [await runtime.spawn(_factory(_done()), query='go') for _ in range(2)]
    plan = [parents[0], parents[0], parents[1]]
    children = [
        await runtime.spawn(_factory(_done()), query='go', parent_id=parent) for parent in plan
    ]

    assert [(await _info(runtime, child)).parent_id for child in children] == plan
    assert [(await _info(runtime, parent)).parent_id for parent in parents] == [None, None]
    assert await runtime.registry.children_of(parents[0]) == children[:2]
    assert await runtime.registry.children_of(parents[1]) == children[2:]
    await runtime.shutdown()


async def test_spawn_under_an_unknown_parent_leaves_no_half_built_agent_behind():
    runtime = Runtime()
    with pytest.raises(ContractViolation):
        await runtime.spawn(_factory(_done()), query='go', parent_id='nope')
    assert runtime._table.agent_ids() == []
    assert runtime._slots == {}
    await runtime.shutdown()


async def test_spawn_records_each_agents_own_metadata_and_class_name():
    runtime = Runtime()
    scout = await runtime.spawn(
        _factory(_done()), query='go', metadata={'role': 'scout', 'depth': 1}
    )
    scribe = await runtime.spawn(
        _factory(_done(), cls=_Specialist), query='go', metadata={'role': 'scribe'}
    )
    bare = await runtime.spawn(_factory(_done()), query='go')

    infos = [await _info(runtime, agent_id) for agent_id in (scout, scribe, bare)]
    assert [info.metadata for info in infos] == [
        {'role': 'scout', 'depth': 1},
        {'role': 'scribe'},
        {},
    ]
    assert [info.agent_type for info in infos] == ['BaseAgent', '_Specialist', 'BaseAgent']
    await runtime.shutdown()


async def test_wait_returns_the_context_of_the_agent_it_was_asked_about():
    runtime = Runtime()
    first = await runtime.spawn(_factory(_done('first answer')), query='first query')
    second = await runtime.spawn(_factory(_done('second answer')), query='second query')
    # 与 spawn 相反的顺序：按 spawn 顺序 wait 时，「取第一个」与「取对应那个」是同一个答案。
    second_ctx = await runtime.wait(second)
    first_ctx = await runtime.wait(first)
    assert [block.content for block in first_ctx.current_traj] == ['first query', 'first answer']
    assert [block.content for block in second_ctx.current_traj] == ['second query', 'second answer']
    assert first_ctx.stop_reason is StopReason.COMPLETED
    await runtime.shutdown()


async def test_wait_returns_once_its_own_agent_is_done_while_a_sibling_still_runs():
    """waitpid 等的是被点名的那个孩子。兄弟任务跑不完时，等错人就是永久挂死。"""
    runtime = Runtime()
    blocker = _Block()
    blocked = await runtime.spawn(_blocking(blocker), query='go')
    quick = await runtime.spawn(_factory(_done('quick answer')), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)

    ctx = await runtime.wait(quick)
    assert ctx.current_traj[-1].content == 'quick answer'
    assert await _status(runtime, blocked) is AgentStatus.RUNNING
    await runtime.shutdown()


async def test_idle_exit_mode_stays_resident_while_zombie_exit_mode_ends_its_own_loop():
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    idle = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.IDLE)
    zombie = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.ZOMBIE)
    await runtime.wait(zombie)
    await asyncio.wait_for(broker.reach(idle, 1), _TIMEOUT)

    assert await _status(runtime, idle) is AgentStatus.IDLE
    assert await _status(runtime, zombie) is AgentStatus.ZOMBIE
    assert [broker.parks(idle), broker.parks(zombie)] == [1, 0]
    assert [agent_id for agent_id, _ in broker.removals] == [zombie]
    await runtime.shutdown()


async def test_each_exit_mode_walks_its_own_status_path():
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    # 顺序既非升序（exit / idle / zombie）亦非降序，反转后同样两者都不是。
    modes = [ExitMode.ZOMBIE, ExitMode.EXIT, ExitMode.IDLE]
    zombie, reaped, resident = [
        await runtime.spawn(_factory(_done()), query='go', exit_mode=mode) for mode in modes
    ]
    await runtime.wait(zombie)
    with pytest.raises(ContractViolation):
        await runtime.wait(reaped)
    await asyncio.wait_for(broker.reach(resident, 1), _TIMEOUT)

    assert registry.applied_of(zombie) == [
        (AgentStatus.IDLE, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.ZOMBIE),
    ]
    assert registry.applied_of(resident) == [
        (AgentStatus.IDLE, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.IDLE),
    ]
    # EXIT 的归宿是「不在注册表里」而不是某个状态，因此它连一次终态转换都不试。
    assert registry.applied_of(reaped) == [
        (AgentStatus.IDLE, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.RUNNING),
    ]
    assert [pair for pair in registry.of(reaped) if pair[1] is AgentStatus.ZOMBIE] == []
    await runtime.shutdown()


async def test_zombie_keeps_its_registry_entry_but_loses_its_mailbox():
    """spec §4.1：ZOMBIE 是「事件循环已退出、队列已移除，但仍在注册表中」。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.ZOMBIE)
    await runtime.wait(agent_id)

    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    refused = await runtime.send(agent_id, '继续')
    assert refused.ok is False
    assert refused.message_id is None
    assert refused.reason is not None
    _assert_three_part(refused.reason)
    assert await broker.consume(agent_id) == []
    await runtime.shutdown()


async def test_the_mailbox_goes_before_the_status_so_a_racing_send_fails_instead_of_vanishing():
    """先改状态那条消息会成功投进一个即将消失的队列，静默丢掉；先摘队列它拿到的是显式失败。"""
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.ZOMBIE)
    await runtime.wait(agent_id)

    assert broker.removals == [(agent_id, AgentStatus.RUNNING)]
    await runtime.shutdown()


async def test_send_reaches_only_the_addressed_agents_mailbox():
    broker = InMemoryMessageBroker()
    runtime = Runtime(broker=broker)
    blockers = [_Block(), _Block()]
    first, second = [await runtime.spawn(_blocking(blocker), query='go') for blocker in blockers]
    for blocker in blockers:
        await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)

    assert (await runtime.send(second, 'for second')).ok is True
    assert [message.content for message in await broker.consume(first)] == []
    assert [message.content for message in await broker.consume(second)] == ['for second']
    await runtime.shutdown()


async def test_send_puts_the_requested_trigger_on_the_delivered_message():
    broker = InMemoryMessageBroker()
    runtime = Runtime(broker=broker)
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking(blocker), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)

    await runtime.send(agent_id, 'next turn')
    await runtime.send(agent_id, 'after the run', trigger=Trigger.DEFERRED)
    delivered = await broker.consume(agent_id)
    assert [message.trigger for message in delivered] == [Trigger.PENDING, Trigger.DEFERRED]
    await runtime.shutdown()


async def test_send_stamps_the_runtime_as_sender_on_every_addressees_message():
    broker = InMemoryMessageBroker()
    runtime = Runtime(broker=broker)
    blockers = [_Block(), _Block()]
    ids = [await runtime.spawn(_blocking(blocker), query='go') for blocker in blockers]
    for blocker in blockers:
        await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    for agent_id in ids:
        assert (await runtime.send(agent_id, 'ping')).ok is True

    delivered = [(await broker.consume(agent_id))[0] for agent_id in ids]
    assert [message.sender for message in delivered] == ['runtime', 'runtime']
    assert [message.receiver for message in delivered] == ids
    await runtime.shutdown()


async def test_send_refuses_an_unregistered_agent_even_when_a_stale_mailbox_exists():
    broker = InMemoryMessageBroker()
    runtime = Runtime(broker=broker)
    await broker.ensure_queue('ghost')

    result = await runtime.send('ghost', 'hello')
    assert result.ok is False
    assert result.message_id is None
    assert result.reason is not None
    _assert_three_part(result.reason)
    assert await broker.consume('ghost') == []
    await runtime.shutdown()


async def test_exit_mode_reaps_only_its_own_agent_from_every_per_agent_structure():
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    reaped = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.EXIT)
    kept = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.ZOMBIE)
    with pytest.raises(ContractViolation):
        await runtime.wait(reaped)
    await runtime.wait(kept)

    assert await runtime.find(reaped) is None
    assert runtime._table.get(reaped) is None
    assert reaped not in runtime._slots

    assert await runtime.find(kept) is not None
    assert runtime._table.get(kept) is not None
    assert kept in runtime._slots

    # 两条路都摘队列，区别只在注册表条目与本进程的记录还留不留。
    assert {agent_id for agent_id, _ in broker.removals} == {reaped, kept}
    await runtime.shutdown()


async def test_find_on_unknown_agent_returns_none_without_creating_a_mailbox():
    broker = InMemoryMessageBroker()
    runtime = Runtime(broker=broker)
    assert await runtime.find('nope') is None
    assert await _mailbox_exists(broker, 'nope') is False
    await runtime.shutdown()


async def test_wait_on_unknown_agent_raises_actionable_error():
    """裸 KeyError 违反错误三件套，调用方看不出该怎么办。"""
    runtime = Runtime()
    with pytest.raises(ContractViolation):
        await runtime.wait('nope')
    await runtime.shutdown()


async def test_wait_tells_a_never_started_drive_apart_from_an_agent_it_never_saw():
    denied = Runtime(registry=_RecordingRegistry(deny=(AgentStatus.IDLE, AgentStatus.RUNNING)))
    never_started = await denied.spawn(_factory(_done()), query='go')
    with pytest.raises(ContractViolation) as started:
        await denied.wait(never_started)
    with pytest.raises(ContractViolation) as unknown:
        await denied.wait('nope')
    assert never_started in started.value.expected
    assert started.value.received != unknown.value.received
    await denied.shutdown()


async def test_send_to_unknown_agent_returns_structured_failure():
    """投递失败返回结构化结果而不抛异常，调用方才能识别可重试的瞬时错误。"""
    runtime = Runtime()
    result = await runtime.send('nope', 'hello')
    assert result.ok is False
    assert result.message_id is None
    assert result.reason is not None
    _assert_three_part(result.reason)
    await runtime.shutdown()


async def test_tool_using_agent_completes_through_the_runtime():
    runtime = Runtime()
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(id='c1', name='Upper', arguments='{"text":"hi"}'),
                    ToolCall(id='c2', name='Echo', arguments='{"text":"there"}'),
                ]
            ),
            ScriptedTurn(content='done'),
        ]
    )
    agent_id = await runtime.spawn(_factory(llm), query='go')
    ctx = await runtime.wait(agent_id)
    assert [block.role for block in ctx.current_traj] == [
        'user',
        'assistant',
        'tool',
        'tool',
        'assistant',
    ]
    assert [block.content for block in ctx.current_traj if block.role == 'tool'] == ['HI', 'there']
    assert [schema['name'] for schema in llm.call_kwargs[0]['tools']] == _TOOL_NAMES
    await runtime.shutdown()


async def test_lost_transition_into_running_stops_the_drive_before_it_calls_the_model(
    caplog: pytest.LogCaptureFixture,
):
    registry = _RecordingRegistry(deny=(AgentStatus.IDLE, AgentStatus.RUNNING))
    broker = InMemoryMessageBroker()
    runtime = Runtime(registry=registry, broker=broker)
    llm = _done()
    agent_id = await runtime.spawn(_factory(llm), query='go')
    with pytest.raises(ContractViolation):
        await runtime.wait(agent_id)

    assert llm.calls == []
    assert registry.of(agent_id) == [(AgentStatus.IDLE, AgentStatus.RUNNING)]
    assert await _status(runtime, agent_id) is AgentStatus.IDLE
    # 转换被拒意味着这个 Agent 归别人所有，替它清理就是拆别人的台。
    assert await _mailbox_exists(broker, agent_id) is True
    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert agent_id in caplog.text
    await runtime.shutdown()


async def test_lost_terminal_transition_is_reported_and_leaves_the_registry_as_it_found_it(
    caplog: pytest.LogCaptureFixture,
):
    registry = _RecordingRegistry(deny=(AgentStatus.RUNNING, AgentStatus.ZOMBIE))
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.ZOMBIE)
    await runtime.wait(agent_id)

    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert agent_id in caplog.text
    # 转换被拒说明这个状态已经不是本进程写的了；保持注册表现有的说法比强行覆盖更诚实。
    assert await _status(runtime, agent_id) is AgentStatus.RUNNING
    assert [agent_id for agent_id, _ in broker.removals] == [agent_id]
    await runtime.shutdown()


async def test_shutdown_waits_for_every_cancelled_drive_and_leaves_its_cleanup_undone():
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    blockers = [_Block(), _Block(), _Block()]
    ids = [await runtime.spawn(_blocking(blocker), query='go') for blocker in blockers]
    for blocker in blockers:
        await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    assert [await _status(runtime, agent_id) for agent_id in ids] == [AgentStatus.RUNNING] * 3

    await runtime.shutdown()

    # 快照取在 shutdown 返回的那一刻：被丢下的任务事件循环转下去照样会跑完。
    tasks = [runtime._table.get_task(agent_id) for agent_id in ids]
    assert [task is not None and task.done() for task in tasks] == [True] * 3
    # 被取消的驱动任务必须**以取消结束**：吞掉 CancelledError 会让它报告正常完成，取消于是
    # 停在任务边界内，外层每一个 asyncio.timeout / wait_for 作用域都被击穿。
    assert [task is not None and task.cancelled() for task in tasks] == [True] * 3
    # 取消可能落在轮的中间，而收尾是一个有状态的动作：它归发起取消的那一方。
    assert [await _status(runtime, agent_id) for agent_id in ids] == [AgentStatus.RUNNING] * 3
    assert broker.removals == []


async def test_wait_returns_the_partial_trajectory_of_a_cancelled_run():
    runtime = Runtime()
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking(blocker), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    await runtime.shutdown()

    ctx = await runtime.wait(agent_id)
    assert [block.role for block in ctx.current_traj] == ['user', 'assistant']
    assert ctx.stop_reason is None


async def test_wait_returns_the_trajectory_of_a_failed_run_instead_of_raising(
    caplog: pytest.LogCaptureFixture,
):
    runtime = Runtime()
    failing = ScriptedLLM([ScriptedTurn(raises=RuntimeError('boom'))])
    failed = await runtime.spawn(_factory(failing), query='go')
    ctx = await runtime.wait(failed)
    assert ctx.stop_reason is StopReason.ERROR
    assert [record.levelname for record in caplog.records] == ['ERROR']

    # init 不因子进程崩溃而倒下。
    survivor = await runtime.spawn(_factory(_done('still here')), query='go')
    assert (await runtime.wait(survivor)).stop_reason is StopReason.COMPLETED
    await runtime.shutdown()


async def test_an_exit_mode_agent_that_fails_is_reaped_without_its_exception_leaving_the_driver(
    caplog: pytest.LogCaptureFixture,
):
    runtime = Runtime()
    failing = ScriptedLLM([ScriptedTurn(raises=RuntimeError('boom'))])
    agent_id = await runtime.spawn(_factory(failing), query='go', exit_mode=ExitMode.EXIT)
    # 只有 EXIT 能观测到「异常有没有逃出去」：条目被摘掉后 shutdown 的 gather 再也看不见这个
    # 任务，逃出来的异常无人取回，只会在 GC 时变成一行日志。
    task = runtime._table.get_task(agent_id)
    assert task is not None
    await asyncio.wait({task})

    assert task.exception() is None
    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert await runtime.find(agent_id) is None
    with pytest.raises(ContractViolation):
        await runtime.wait(agent_id)
    await runtime.shutdown()


async def test_wait_hands_back_the_agents_own_context_not_a_snapshot():
    runtime = Runtime()
    built: list[BaseAgent] = []
    agent_id = await runtime.spawn(_capturing(built, _done()), query='go')
    assert await runtime.wait(agent_id) is built[0].context
    await runtime.shutdown()


async def test_shutdown_is_idempotent():
    runtime = Runtime()
    agent_id = await runtime.spawn(_factory(_done()), query='go')
    await runtime.wait(agent_id)
    await runtime.shutdown()
    await runtime.shutdown()
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE


async def test_spawn_after_shutdown_is_refused_instead_of_dropping_the_new_agents_events():
    runtime = Runtime()
    await runtime.shutdown()
    with pytest.raises(ContractViolation):
        await runtime.spawn(_factory(_done()), query='go')
    assert runtime._table.agent_ids() == []


async def test_every_spawned_agent_gets_the_runtimes_own_hooks_and_event_bus():
    runtime = Runtime()
    queries: list[str] = []

    async def record(**kwargs: Any) -> HookResult:
        queries.append(kwargs['ctx'].current_traj[-1].content)
        return HookResult()

    runtime.hooks.register(Junction.AGENT_START, record)
    subscription = runtime.events.subscribe()
    first = await runtime.spawn(_factory(_done()), query='one')
    second = await runtime.spawn(_factory(_done()), query='two')
    await runtime.wait(first)
    await runtime.wait(second)
    await runtime.shutdown()

    ended = {event.agent_id async for event in subscription if event.kind == 'agent_end'}
    assert ended == {first, second}
    assert sorted(queries) == ['one', 'two']


async def test_spawning_without_a_query_parks_the_loop_instead_of_running_a_cycle(
    caplog: pytest.LogCaptureFixture,
):
    """lazy boot：起来就在等消息，第一条消息才是它的第一个周期。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    llm = _done('woken')
    agent_id = await runtime.spawn(_factory(llm), exit_mode=ExitMode.IDLE)
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)

    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert llm.calls == []
    assert agent.context.current_traj == []

    assert (await runtime.send(agent_id, 'wake up')).ok is True
    await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)

    assert [(block.role, block.content) for block in agent.context.current_traj] == [
        ('user', 'wake up'),
        ('assistant', 'woken'),
    ]
    assert caplog.records == []
    await runtime.shutdown()


async def test_a_resident_loop_runs_one_cycle_per_message_on_the_same_context(
    caplog: pytest.LogCaptureFixture,
):
    """一条消息一个周期，Context 不重建、轮次不重置（spec §5.1）。

    三个周期而非两个，且答复顺序既非升序（alpha / bravo / charlie）亦非降序，反转后同样两者都
    不是：只跑一个周期、或把注入顺序排一遍序，在两个周期下都能蒙对。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    replies = ['bravo said', 'alpha said', 'charlie said']
    agent_id = await runtime.spawn(
        _factory(ScriptedLLM([ScriptedTurn(content=reply) for reply in replies])),
        query='bravo',
        exit_mode=ExitMode.IDLE,
    )
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)
    for parks, content in enumerate(('alpha', 'charlie'), start=2):
        assert (await runtime.send(agent_id, content)).ok is True
        await asyncio.wait_for(broker.reach(agent_id, parks), _TIMEOUT)

    agent = runtime._table.get(agent_id)
    assert agent is not None
    traj = agent.context.current_traj
    assert [block.content for block in traj if block.role == 'user'] == [
        'bravo',
        'alpha',
        'charlie',
    ]
    assert [block.content for block in traj if block.role == 'assistant'] == replies
    assert caplog.records == []
    await runtime.shutdown()


async def test_the_loop_holds_the_in_run_flag_only_while_a_cycle_is_running():
    """`in_run` 的读者全在后续任务里（`resume` 靠它决定该回到 RUNNING 还是 IDLE）。

    这一对簿记写坏了不会在这里表现成失败，而是让此后**每一次** `resume` 把 Agent 送进错的状态。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking(blocker), query='go', exit_mode=ExitMode.IDLE)
    control = runtime._slots[agent_id].control
    # create_task 只排队不立刻运行，因此此刻第一个周期一定还没开始。
    assert control.in_run is False

    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    assert control.in_run is True

    blocker.release.set()
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)
    assert control.in_run is False
    await runtime.shutdown()


async def test_deferred_messages_wait_for_the_rest_of_the_cycle_not_merely_the_next_turn():
    """spec §4.5：DEFERRED 的语义是「先把手上的事做完，然后再看这个」。

    周期跑三轮而非两轮——恰好两轮时「这个周期剩下的部分」与「下一轮」是同一个答案，于是把等待
    从一个周期降级成一轮的实现照样全绿。消息三条，顺序既非升序亦非降序，反转后同样两者都不是。
    """
    runtime = Runtime()
    gate = _Gate(2)
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Gate', arguments='{}')]),
            ScriptedTurn(tool_calls=[ToolCall(id='c2', name='Gate', arguments='{}')]),
            ScriptedTurn(content='first cycle done'),
            ScriptedTurn(content='second cycle done'),
        ]
    )
    agent_id = await runtime.spawn(_factory(llm, tools={'Gate': gate}), query='go')
    await asyncio.wait_for(gate.entered[0].wait(), _TIMEOUT)
    for content in ('bravo', 'alpha', 'charlie'):
        assert (await runtime.send(agent_id, content, trigger=Trigger.DEFERRED)).ok is True

    # 放行第一个停点：第二轮的门把三条挪进暂存，随后停在第二个停点上——此时这个周期还剩第三轮。
    gate.release[0].set()
    await asyncio.wait_for(gate.entered[1].wait(), _TIMEOUT)
    control = runtime._slots[agent_id].control
    assert control.deferred_count == 3

    gate.release[1].set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert [(block.role, block.content) for block in ctx.current_traj] == [
        ('user', 'go'),
        ('assistant', None),
        ('tool', 'released 0'),
        ('assistant', None),
        ('tool', 'released 1'),
        ('assistant', 'first cycle done'),
        ('user', 'bravo'),
        ('user', 'alpha'),
        ('user', 'charlie'),
        ('assistant', 'second cycle done'),
    ]
    assert control.deferred_count == 0
    await runtime.shutdown()


async def test_one_agents_deferred_messages_never_reach_another_agents_context():
    """暂存队列是 per-agent 的：共用一个控制块会让一个 Agent 的延迟消息进到另一个的上下文里。"""
    runtime = Runtime()
    gate = _Gate(2)
    quiet_blocker = _Block()
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Gate', arguments='{}')]),
            ScriptedTurn(tool_calls=[ToolCall(id='c2', name='Gate', arguments='{}')]),
            ScriptedTurn(content='first cycle done'),
            ScriptedTurn(content='second cycle done'),
        ]
    )
    addressed = await runtime.spawn(_factory(llm, tools={'Gate': gate}), query='go')
    quiet = await runtime.spawn(_blocking(quiet_blocker), query='go')
    await asyncio.wait_for(gate.entered[0].wait(), _TIMEOUT)
    await asyncio.wait_for(quiet_blocker.entered.wait(), _TIMEOUT)
    for content in ('bravo', 'alpha', 'charlie'):
        assert (await runtime.send(addressed, content, trigger=Trigger.DEFERRED)).ok is True

    # 放行第一个停点：第二轮的门把这三条挪进暂存，随后停在第二个停点上，周期还没结束。
    gate.release[0].set()
    await asyncio.wait_for(gate.entered[1].wait(), _TIMEOUT)

    # 此刻让没被寻址的那个走到它自己的周期边界：它若看得见那份暂存，就会替收件人消费掉。
    quiet_blocker.release.set()
    quiet_ctx = await asyncio.wait_for(runtime.wait(quiet), _TIMEOUT)
    assert [block.content for block in quiet_ctx.current_traj if block.role == 'user'] == ['go']

    gate.release[1].set()
    ctx = await asyncio.wait_for(runtime.wait(addressed), _TIMEOUT)
    assert [block.content for block in ctx.current_traj if block.role == 'user'] == [
        'go',
        'bravo',
        'alpha',
        'charlie',
    ]
    await runtime.shutdown()


async def test_pausing_one_agent_leaves_the_others_running():
    """挂起门是 per-agent 的：共用一个控制块会让一次 pause 按住整个 Runtime。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    held, free = [
        await runtime.spawn(_factory(_done(f'{name} woke')), exit_mode=ExitMode.IDLE)
        for name in ('held', 'free')
    ]
    for agent_id in (held, free):
        await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)
    runtime._slots[held].control.pause()

    assert (await runtime.send(free, 'go ahead')).ok is True
    await asyncio.wait_for(broker.reach(free, 2), _TIMEOUT)
    assert (await runtime.send(held, 'not yet')).ok is True
    assert await runtime._slots[held].control.wait_until_parked(_TIMEOUT) is True

    held_agent = runtime._table.get(held)
    free_agent = runtime._table.get(free)
    assert held_agent is not None and free_agent is not None
    assert held_agent.context.current_traj == []
    assert [block.content for block in free_agent.context.current_traj] == [
        'go ahead',
        'free woke',
    ]
    await runtime.shutdown()


async def test_removing_the_mailbox_ends_a_resident_event_loop():
    """`wait_for_messages` 答 False 只有一个含义：邮箱已经被摘掉了，该退出。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.IDLE)
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)

    await broker.remove_queue(agent_id)
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert ctx.stop_reason is StopReason.COMPLETED
    assert broker.parks(agent_id) == 1
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    await runtime.shutdown()


async def test_an_exhausted_turn_budget_ends_the_event_loop_even_for_a_resident_agent():
    """预算耗尽的 Agent 对任何后续消息都产不出新的一轮，继续等只会被同一条消息反复唤醒。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    llm = _done('only turn')

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=llm,
                tools=_tools(),
                system_prompt=_SYSTEM_PROMPT,
                max_turns=1,
            ),
            hooks=hooks,
            events=events,
        )

    agent_id = await runtime.spawn(build, query='go', exit_mode=ExitMode.IDLE)
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)
    assert (await runtime.send(agent_id, 'more work')).ok is True
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert ctx.stop_reason is StopReason.MAX_TURNS
    assert len(llm.calls) == 1
    assert broker.parks(agent_id) == 1
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    await runtime.shutdown()


async def test_a_message_that_won_an_ok_receipt_after_the_last_gate_check_is_still_processed(
    caplog: pytest.LogCaptureFixture,
):
    """收据的用途是分辨可重试的瞬时失败与永久失败；一条「投递成功却永不被处理」的消息把它抹掉。

    窗口不是一瞬间：它覆盖本周期最后一轮的整个模型调用——门已经检查过了，而循环即将摘掉邮箱。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold(**kwargs: Any) -> HookResult:
        if kwargs['turn'] == 1:
            entered.set()
            await release.wait()
        return HookResult()

    runtime.hooks.register(Junction.TURN_END, hold)
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    agent_id = await runtime.spawn(_factory(llm), query='go', exit_mode=ExitMode.ZOMBIE)
    await asyncio.wait_for(entered.wait(), _TIMEOUT)

    assert (await runtime.send(agent_id, 'after the last gate check')).ok is True
    release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert [(block.role, block.content) for block in ctx.current_traj] == [
        ('user', 'go'),
        ('assistant', 'first'),
        ('user', 'after the last gate check'),
        ('assistant', 'second'),
    ]
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    # 没有输入被丢下，因此摘队列那一刻不该有任何东西要报告。
    assert caplog.records == []
    await runtime.shutdown()


async def test_an_exhausted_budget_keeps_the_deferred_stash_and_reports_what_it_abandons(
    caplog: pytest.LogCaptureFixture,
):
    """预算耗尽是唯一处理不了已收下输入的退出路径：没有轮次可用，再开周期就是空转。

    丢下它们可以，静默丢下不可以——暂存的三条必须原封不动留在控制块里，邮箱里剩的那条随队列
    消失，两件事都要出现在同一条 ERROR 里。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    gate = _Gate(2)
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Gate', arguments='{}')]),
            ScriptedTurn(tool_calls=[ToolCall(id='c2', name='Gate', arguments='{}')]),
        ]
    )

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=llm,
                tools={'Gate': gate},
                system_prompt=_SYSTEM_PROMPT,
                max_turns=2,
            ),
            hooks=hooks,
            events=events,
        )

    agent_id = await runtime.spawn(build, query='go', exit_mode=ExitMode.ZOMBIE)
    await asyncio.wait_for(gate.entered[0].wait(), _TIMEOUT)
    for content in ('bravo', 'alpha', 'charlie'):
        assert (await runtime.send(agent_id, content, trigger=Trigger.DEFERRED)).ok is True

    # 第二轮的门把三条挪进暂存；停在第二个停点上时预算已经用尽，不会再有第三轮的门。
    gate.release[0].set()
    await asyncio.wait_for(gate.entered[1].wait(), _TIMEOUT)
    assert (await runtime.send(agent_id, 'never consumed')).ok is True
    gate.release[1].set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert ctx.stop_reason is StopReason.MAX_TURNS
    control = runtime._slots[agent_id].control
    assert [message.content for message in control.take_deferred()] == [
        'bravo',
        'alpha',
        'charlie',
    ]
    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert caplog.records[0].args == (agent_id, 3, True)
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    await runtime.shutdown()


async def test_a_failing_cycle_ends_a_resident_event_loop_instead_of_retrying_it(
    caplog: pytest.LogCaptureFixture,
):
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    failing = ScriptedLLM([ScriptedTurn(raises=RuntimeError('boom'))])
    agent_id = await runtime.spawn(_factory(failing), query='go', exit_mode=ExitMode.IDLE)
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert ctx.stop_reason is StopReason.ERROR
    assert len(failing.calls) == 1
    assert broker.parks(agent_id) == 0
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    assert [record.levelname for record in caplog.records] == ['ERROR']
    await runtime.shutdown()


async def test_the_pause_gate_sits_between_a_waiting_message_and_starting_the_cycle():
    """被挂起的 Agent 既不开始周期，也不把消息从邮箱里取走——否则 resume 之后它已经不在了。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    subscription = runtime.events.subscribe()
    agent_id = await runtime.spawn(_factory(_done('woken')), exit_mode=ExitMode.IDLE)
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)
    control = runtime._slots[agent_id].control
    control.pause()

    assert (await runtime.send(agent_id, 'wake up')).ok is True
    assert await control.wait_until_parked(_TIMEOUT) is True

    assert await _status(runtime, agent_id) is AgentStatus.IDLE
    assert [message.content for message in await broker.consume(agent_id)] == ['wake up']
    await runtime.shutdown()
    assert [event.kind async for event in subscription] == []


async def test_an_abort_taken_between_cycles_drops_that_cycles_input_and_goes_back_to_waiting():
    """中止位一次性：被它掐掉的那个周期不开始，而下一条消息照常开一个新周期。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    llm = _done('woken')
    agent_id = await runtime.spawn(_factory(llm), query='go', exit_mode=ExitMode.IDLE)
    # create_task 只排队不立刻运行，因此这一位一定先于第一个周期被看见。
    runtime._slots[agent_id].control.request_abort()
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)

    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert llm.calls == []
    assert agent.context.current_traj == []

    assert (await runtime.send(agent_id, 'wake up')).ok is True
    await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
    assert [(block.role, block.content) for block in agent.context.current_traj] == [
        ('user', 'wake up'),
        ('assistant', 'woken'),
    ]
    await runtime.shutdown()


async def test_wait_refuses_an_agent_this_process_never_spawned_even_with_a_shared_registry():
    """交回的是本进程持有的那份 Context；共享注册表的另一个进程手里没有它。"""
    runtime = Runtime()
    agent_id = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.ZOMBIE)
    await runtime.wait(agent_id)

    second = Runtime(registry=runtime.registry, broker=runtime.broker)
    with pytest.raises(ContractViolation) as excinfo:
        await second.wait(agent_id)
    assert agent_id in excinfo.value.expected
    await second.shutdown()
    await runtime.shutdown()
