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
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.message import Message
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


class _RecordingRegistry:
    """记下每一次状态转换，并可让指定的一次转换失败。

    转换记录是「注册表看到了什么」的唯一证据：EXIT 的终态紧接着被 unregister 抹掉，只看注册表
    的最终内容分不出它转去了哪一个状态。
    """

    def __init__(self, *, deny: tuple[AgentStatus, AgentStatus] | None = None) -> None:
        self._inner = InMemoryAgentRegistry()
        self._deny = deny
        self.transitions: list[tuple[str, AgentStatus, AgentStatus]] = []

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
        return await self._inner.compare_and_set_status(agent_id, expected=expected, new=new)

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


def _tools() -> dict[str, Tool]:
    return {'Echo': _Echo(), 'Upper': _Upper(), 'Concat': _Concat()}


def _done(content: str = 'done') -> ScriptedLLM:
    return ScriptedLLM([ScriptedTurn(content=content)])


def _factory(
    llm: ScriptedLLM, *, name: str = 'Worker', tools: dict[str, Tool] | None = None
) -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
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


async def test_exit_mode_idle_and_zombie_each_park_their_own_agent_in_their_own_status():
    runtime = Runtime()
    idle = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.IDLE)
    zombie = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.ZOMBIE)
    await runtime.wait(idle)
    await runtime.wait(zombie)
    assert await _status(runtime, idle) is AgentStatus.IDLE
    assert await _status(runtime, zombie) is AgentStatus.ZOMBIE
    await runtime.shutdown()


async def test_each_exit_mode_records_its_own_pair_of_transitions():
    registry = _RecordingRegistry()
    runtime = Runtime(registry=registry)
    modes = [ExitMode.EXIT, ExitMode.IDLE, ExitMode.ZOMBIE]
    ids = [await runtime.spawn(_factory(_done()), query='go', exit_mode=mode) for mode in modes]
    with pytest.raises(ConfigError):
        await runtime.wait(ids[0])
    await runtime.wait(ids[1])
    await runtime.wait(ids[2])
    assert registry.of(ids[0]) == [
        (AgentStatus.IDLE, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.ZOMBIE),
    ]
    assert registry.of(ids[1]) == [
        (AgentStatus.IDLE, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.IDLE),
    ]
    assert registry.of(ids[2]) == [
        (AgentStatus.IDLE, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.ZOMBIE),
    ]
    await runtime.shutdown()


async def test_zombie_agent_still_receives_a_message_addressed_to_it():
    broker = InMemoryMessageBroker()
    runtime = Runtime(broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.ZOMBIE)
    await runtime.wait(agent_id)
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE

    result = await runtime.send(agent_id, '继续')
    assert result.ok is True
    delivered = await broker.consume(agent_id)
    assert [message.content for message in delivered] == ['继续']
    assert delivered[0].receiver == agent_id
    assert result.message_id == delivered[0].id
    await runtime.shutdown()


async def test_send_reaches_only_the_addressed_agents_mailbox():
    broker = InMemoryMessageBroker()
    runtime = Runtime(broker=broker)
    first = await runtime.spawn(_factory(_done()), query='go')
    second = await runtime.spawn(_factory(_done()), query='go')
    await runtime.wait(first)
    await runtime.wait(second)

    assert (await runtime.send(second, 'for second')).ok is True
    assert [message.content for message in await broker.consume(first)] == []
    assert [message.content for message in await broker.consume(second)] == ['for second']
    await runtime.shutdown()


async def test_exit_mode_reaps_only_its_own_agent_from_every_per_agent_structure():
    broker = InMemoryMessageBroker()
    runtime = Runtime(broker=broker)
    reaped = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.EXIT)
    kept = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.ZOMBIE)
    with pytest.raises(ConfigError):
        await runtime.wait(reaped)
    await runtime.wait(kept)

    assert await runtime.find(reaped) is None
    assert await _mailbox_exists(broker, reaped) is False
    assert runtime._table.get(reaped) is None
    assert reaped not in runtime._contexts

    assert await runtime.find(kept) is not None
    assert await _mailbox_exists(broker, kept) is True
    assert runtime._table.get(kept) is not None
    assert kept in runtime._contexts
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
    with pytest.raises(ConfigError):
        await runtime.wait('nope')
    await runtime.shutdown()


async def test_wait_tells_a_never_started_drive_apart_from_an_agent_it_never_saw():
    denied = Runtime(registry=_RecordingRegistry(deny=(AgentStatus.IDLE, AgentStatus.RUNNING)))
    never_started = await denied.spawn(_factory(_done()), query='go')
    with pytest.raises(ConfigError) as started:
        await denied.wait(never_started)
    with pytest.raises(ConfigError) as unknown:
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
    with pytest.raises(ConfigError):
        await runtime.wait(agent_id)

    assert llm.calls == []
    assert registry.of(agent_id) == [(AgentStatus.IDLE, AgentStatus.RUNNING)]
    assert await _status(runtime, agent_id) is AgentStatus.IDLE
    # 转换被拒意味着这个 Agent 归别人所有，替它清理就是拆别人的台。
    assert await _mailbox_exists(broker, agent_id) is True
    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert agent_id in caplog.text
    await runtime.shutdown()


async def test_lost_terminal_transition_is_reported_and_exit_still_releases_resources(
    caplog: pytest.LogCaptureFixture,
):
    registry = _RecordingRegistry(deny=(AgentStatus.RUNNING, AgentStatus.ZOMBIE))
    broker = InMemoryMessageBroker()
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.EXIT)
    with pytest.raises(ConfigError):
        await runtime.wait(agent_id)

    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert agent_id in caplog.text
    assert await runtime.find(agent_id) is None
    assert await _mailbox_exists(broker, agent_id) is False
    await runtime.shutdown()


async def test_shutdown_returns_only_after_every_cancelled_drive_has_finished_unwinding():
    runtime = Runtime()
    blockers = [_Block(), _Block()]
    ids = [await runtime.spawn(_blocking(blocker), query='go') for blocker in blockers]
    for blocker in blockers:
        await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    assert [await _status(runtime, agent_id) for agent_id in ids] == [AgentStatus.RUNNING] * 2

    await runtime.shutdown()

    # 终态由每个任务自己的 finally 写：shutdown 返回时它已经是终态，说明 shutdown 等到了它。
    assert [await _status(runtime, agent_id) for agent_id in ids] == [AgentStatus.ZOMBIE] * 2


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


async def test_shutdown_is_idempotent():
    runtime = Runtime()
    agent_id = await runtime.spawn(_factory(_done()), query='go')
    await runtime.wait(agent_id)
    await runtime.shutdown()
    await runtime.shutdown()
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE


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
