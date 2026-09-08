"""本模块**必须**保留 `from __future__ import annotations`：仓库每个模块都带它，于是标注在运行时
是字符串。少了它，这里的工具类持有真类型对象，而 schema 反射按字符串取标注的缺陷在这里全绿、在每个
真实工具上必炸。
"""

from __future__ import annotations

import asyncio
import logging
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
# 五个子代跨两代。广度优先的答案（bravo / alpha / charlie / delta / echo）与深度优先
# （bravo / delta / alpha / echo / charlie）、升序、降序、以及这四者的反转全都互不相同。
_TREE: tuple[tuple[str, str | None], ...] = (
    ('root', None),
    ('bravo', 'root'),
    ('alpha', 'root'),
    ('charlie', 'root'),
    ('delta', 'bravo'),
    ('echo', 'alpha'),
)


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

    `hold_before` 把「转去某个状态」的 CAS 停在 `switches` 这道闸上：`_finish` 摘完队列、还没
    转 ZOMBIE 的那一刻，在 Runtime 外面没有别的观测点。

    `deny_for` 把拒绝收窄到一个 agent。递归操作对「被点名的那一个」与「遍历到的子代」处理不同，
    而只按 (expected, new) 拒绝会同时打中两者，那条子代分支于是永远走不到。
    """

    def __init__(
        self,
        *,
        deny: tuple[AgentStatus, AgentStatus] | None = None,
        deny_for: str | None = None,
        hold_before: AgentStatus | None = None,
    ) -> None:
        self._inner = InMemoryAgentRegistry()
        self._deny = deny
        self._hold_before = hold_before
        self.deny_for = deny_for
        self.switches = _Hold()
        self.transitions: list[tuple[str, AgentStatus, AgentStatus]] = []
        self.applied: list[tuple[str, AgentStatus, AgentStatus]] = []

    async def register(self, info: AgentInfo) -> None:
        await self._inner.register(info)

    async def get(self, agent_id: str) -> AgentInfo | None:
        return await self._inner.get(agent_id)

    async def compare_and_set_status(
        self, agent_id: str, *, expected: AgentStatus, new: AgentStatus
    ) -> bool:
        if new is self._hold_before:
            await self.switches.passing(agent_id)
        self.transitions.append((agent_id, expected, new))
        if self._deny == (expected, new) and self.deny_for in (None, agent_id):
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
    """观测 broker 这一侧的接缝：谁停在邮箱上等消息、谁取走过消息，以及摘队列的那一刻注册表怎么说。

    「事件循环在等消息」（spec §4.1 给 IDLE 的定义）在 Runtime 外面没有别的观测点——注册表里的
    `IDLE` 与「刚注册、驱动任务还没跑起来」是同一个取值，拿它当握手信号分不出这两件事。

    「挂起期间没有取走消息」同理只有 `consumes` 答得出：邮箱剩几条、轨迹里有没有那一轮，对
    「从来没取走」与「取走了又放回去 / 取走了暂存起来」是同一个答案。
    """

    def __init__(self, registry: AgentRegistry | None = None) -> None:
        self._inner = InMemoryMessageBroker()
        self._registry = registry
        # 边沿信号：入园即置位，等待者扫描前清位。「已入园 n 次」是电平，等的是「又入了一次」。
        self._parked = asyncio.Event()
        self._parks: dict[str, int] = {}
        self._consumes: dict[str, int] = {}
        self.removals: list[tuple[str, AgentStatus | None]] = []

    def parks(self, agent_id: str) -> int:
        return self._parks.get(agent_id, 0)

    def consumes(self, agent_id: str) -> int:
        return self._consumes.get(agent_id, 0)

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
        self._consumes[agent_id] = self.consumes(agent_id) + 1
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

    def off_missing_queue(self, cb: MissingQueueCallback) -> None:
        self._inner.off_missing_queue(cb)

    async def _seen_status(self, agent_id: str) -> AgentStatus | None:
        if self._registry is None:
            return None
        info = await self._registry.get(agent_id)
        return None if info is None else info.status


class _Hold:
    """一道可开合的闸，外加「已经有几个执行体停在上面」这个边沿信号。

    闸开着时 `passing` 不挂起（`asyncio.Event.wait` 在已置位时直接返回），因此没被 `close` 的
    闸对被测代码完全透明。
    """

    def __init__(self) -> None:
        self.arrivals: list[str] = []
        self._arrived = asyncio.Event()
        self._open = asyncio.Event()
        self._open.set()

    def close(self) -> None:
        self._open.clear()
        self.arrivals.clear()

    def open(self) -> None:
        self._open.set()

    async def reach(self, count: int) -> None:
        """等到有 `count` 个执行体停在闸上。上界由调用方套一层 `wait_for` 给。"""
        while True:
            self._arrived.clear()
            if len(self.arrivals) >= count:
                return
            await self._arrived.wait()

    async def passing(self, agent_id: str) -> None:
        self.arrivals.append(agent_id)
        self._arrived.set()
        await self._open.wait()


class _HeldBroker:
    """能把 publish 或 ensure_queue 停在半路的 broker，用来制造真正的交错。

    内存实现下 `send` 与 `ensure_active` 全程不挂起，两次调用总是一前一后各自跑完：那时「按调用
    作用域的抑制位」与「放在 Runtime 上的一个字段」是同一个答案，「同一个 Agent 只有一个驱动」
    也永远轮不到被检验，两条断言测的都成了调度顺序。
    """

    def __init__(self) -> None:
        self._inner = InMemoryMessageBroker()
        self.publishes = _Hold()
        self.ensures = _Hold()

    async def ensure_queue(self, agent_id: str) -> None:
        await self.ensures.passing(agent_id)
        await self._inner.ensure_queue(agent_id)

    async def publish(self, agent_id: str, message: Message) -> DeliveryResult:
        await self.publishes.passing(agent_id)
        return await self._inner.publish(agent_id, message)

    async def consume(self, agent_id: str, *, max_n: int = 0) -> list[Message]:
        return await self._inner.consume(agent_id, max_n=max_n)

    async def wait_for_messages(
        self,
        agent_id: str,
        timeout: float | None,  # noqa: ASYNC109
    ) -> bool:
        return await self._inner.wait_for_messages(agent_id, timeout)

    async def take_by_correlation(
        self,
        agent_id: str,
        correlation_id: str,
        timeout: float | None,  # noqa: ASYNC109
    ) -> Message | None:
        return await self._inner.take_by_correlation(agent_id, correlation_id, timeout)

    async def remove_queue(self, agent_id: str) -> None:
        await self._inner.remove_queue(agent_id)

    def on_missing_queue(self, cb: MissingQueueCallback) -> None:
        self._inner.on_missing_queue(cb)

    def off_missing_queue(self, cb: MissingQueueCallback) -> None:
        self._inner.off_missing_queue(cb)


class _LoopingRegistry:
    """`children_of` 报出一个环，并给遍历的提问次数设上界。

    环是远端注册表的真实可能：条目可以被别的进程写过。上界不能用 `asyncio.wait_for` 给——环里
    的 `children_of` 从不挂起，取消永远送不到，那个超时作用域整个被击穿。
    """

    def __init__(self, children: dict[str, list[str]], *, budget: int = 16) -> None:
        self._children = children
        self._budget = budget
        self._inner = InMemoryAgentRegistry()
        self.asked: list[str] = []

    async def register(self, info: AgentInfo) -> None:
        await self._inner.register(info)

    async def get(self, agent_id: str) -> AgentInfo | None:
        return await self._inner.get(agent_id)

    async def compare_and_set_status(
        self, agent_id: str, *, expected: AgentStatus, new: AgentStatus
    ) -> bool:
        return await self._inner.compare_and_set_status(agent_id, expected=expected, new=new)

    async def update(self, agent_id: str, **fields: Any) -> None:
        await self._inner.update(agent_id, **fields)

    async def children_of(self, agent_id: str) -> list[str]:
        self.asked.append(agent_id)
        if len(self.asked) > self._budget:
            raise AssertionError(
                f'children_of 已被问了 {len(self.asked)} 次，超过上界 {self._budget}：'
                '遍历没有在环上收敛'
            )
        return list(self._children.get(agent_id, []))

    async def list(
        self, *, status: AgentStatus | None = None, parent_id: str | None = None
    ) -> list[AgentInfo]:
        return await self._inner.list(status=status, parent_id=parent_id)

    async def unregister(self, agent_id: str) -> None:
        await self._inner.unregister(agent_id)


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


def _declining(name: str, seen: list[str]) -> MissingQueueCallback:
    """一个记名后放弃的 on_missing_queue 回调：用来观测本 Runtime 在回调链上的位次。"""

    async def decline(agent_id: str) -> bool:
        seen.append(name)
        return False

    return decline


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


async def _register_tree(registry: AgentRegistry) -> None:
    for agent_id, parent_id in _TREE:
        await registry.register(
            AgentInfo(agent_id=agent_id, name=agent_id, agent_type='BaseAgent', parent_id=parent_id)
        )


async def _resident(runtime: Runtime, broker: _SpyBroker, *, parent_id: str | None = None) -> str:
    """起一个常驻 Agent，等它真的停在邮箱上。脚本给足三轮，够本文件里任何一个用例用。

    握手用入园而不是「注册表里是 IDLE」：刚注册、驱动任务还没跑起来时状态也是 IDLE，拿它当信号
    会让 pause 赶在接管之前写掉状态，IDLE -> RUNNING 于是被拒——这个 Agent 从头到尾没有事件循环，
    而后面每一条断言看上去都还成立。
    """
    agent_id = await runtime.spawn(
        _factory(ScriptedLLM([ScriptedTurn(content=f'turn-{index}') for index in range(3)])),
        exit_mode=ExitMode.IDLE,
        parent_id=parent_id,
    )
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)
    return agent_id


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
    # 抑制激活才问得出「邮箱还在不在」：默认的 send 会把它叫醒并顺手把邮箱建回来。
    refused = await runtime.send(agent_id, '继续', activate=False)
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


async def test_a_mailbox_that_vanishes_while_paused_ends_the_loop_instead_of_opening_a_cycle(
    caplog: pytest.LogCaptureFixture,
):
    """零输入的周期会写下一块没有前置 user 块的 assistant 块，而轨迹是要持久化的。

    第一轮的请求此时只有 system prompt 一条消息；provider 会拒掉这样的消息，而故障要等到下一次
    resume 读那份轨迹时才现身。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    llm = _done('never asked')
    agent_id = await runtime.spawn(_factory(llm), exit_mode=ExitMode.IDLE)
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)
    control = runtime._slots[agent_id].control
    control.pause()

    assert (await runtime.send(agent_id, 'wake up')).ok is True
    assert await control.wait_until_parked(_TIMEOUT) is True
    await broker.remove_queue(agent_id)
    control.resume()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert llm.calls == []
    assert ctx.current_traj == []
    assert ctx.stop_reason is None
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    assert caplog.records == []
    await runtime.shutdown()


async def test_a_refused_oscillation_at_a_cycle_boundary_is_reported(
    caplog: pytest.LogCaptureFixture,
):
    """周期边界的两次 CAS 是 compare-and-set 而不是赋值；丢掉它的结论就是丢掉它的理由。

    `allowed` 覆盖控制面能留下的全部活状态，所以被拒意味着注册表已经不认本进程的转换了，而
    循环还在跑——零痕迹的话，注册表与本进程各说一套且无从查起。
    """
    registry = _RecordingRegistry(deny=(AgentStatus.RUNNING, AgentStatus.IDLE))
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.IDLE)
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)

    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert caplog.records[0].args == (agent_id, AgentStatus.IDLE.value)
    assert await _status(runtime, agent_id) is AgentStatus.RUNNING
    await runtime.shutdown()


async def test_a_cycle_starts_from_every_state_the_control_plane_can_leave_between_cycles(
    caplog: pytest.LogCaptureFixture,
):
    """PAUSED / WAITING / ABORTING 都是后续任务会在周期之间留下的状态。

    不在 `allowed` 里的那些会让循环照常跑而注册表停在旧状态上——静默转不过去。三个而非一个，
    顺序既非声明序亦非其反转。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    for status in (AgentStatus.WAITING, AgentStatus.ABORTING, AgentStatus.PAUSED):
        agent_id = await runtime.spawn(_factory(_done()), exit_mode=ExitMode.IDLE)
        await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)
        assert await registry.compare_and_set_status(
            agent_id, expected=AgentStatus.IDLE, new=status
        )

        assert (await runtime.send(agent_id, 'go ahead')).ok is True
        await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)

        assert registry.applied_of(agent_id) == [
            (AgentStatus.IDLE, AgentStatus.RUNNING),
            (AgentStatus.RUNNING, AgentStatus.IDLE),
            (AgentStatus.IDLE, status),
            (status, AgentStatus.RUNNING),
            (AgentStatus.RUNNING, AgentStatus.IDLE),
        ]
    assert caplog.records == []
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


async def test_a_message_revives_a_zombie_with_its_context_intact():
    """ZOMBIE 留在注册表可寻址，就是为了「一条消息带着上下文继续干」（spec §4.1）。"""
    runtime = Runtime()
    built: list[BaseAgent] = []
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    agent_id = await runtime.spawn(_capturing(built, llm), query='go')
    await runtime.wait(agent_id)
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE

    delivered = await runtime.send(agent_id, 'keep going')

    assert delivered.ok is True
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert [block.content for block in ctx.current_traj if block.role == 'user'] == [
        'go',
        'keep going',
    ]
    assert [block.content for block in ctx.current_traj if block.role == 'assistant'] == [
        'first',
        'second',
    ]
    # 续用的是原来那个实例与它那份 Context：新建一个就等于把「一条消息带着上下文继续干」丢了。
    assert [agent.agent_id for agent in built] == [agent_id]
    assert ctx is built[0].context
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    await runtime.shutdown()


async def test_send_without_activation_neither_revives_nor_silently_succeeds():
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    agent_id = await runtime.spawn(_factory(llm), query='go')
    await runtime.wait(agent_id)

    delivered = await runtime.send(agent_id, 'queue only', activate=False)

    assert delivered.ok is False
    assert delivered.message_id is None
    assert delivered.reason is not None
    _assert_three_part(delivered.reason)
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    assert len(llm.calls) == 1
    assert broker.parks(agent_id) == 0
    await runtime.shutdown()


async def test_suppressing_activation_on_one_send_does_not_leak_to_a_concurrent_one():
    """抑制位是按调用的：两个并发 send 各自的意图不得互相污染。"""
    broker = _HeldBroker()
    runtime = Runtime(broker=broker)
    llms = [
        ScriptedLLM([ScriptedTurn(content='a1'), ScriptedTurn(content='a2')]),
        ScriptedLLM([ScriptedTurn(content='b1'), ScriptedTurn(content='b2')]),
    ]
    first, second = [await runtime.spawn(_factory(llm), query='go') for llm in llms]
    await runtime.wait(first)
    await runtime.wait(second)

    broker.publishes.close()
    quiet = asyncio.create_task(runtime.send(first, 'queue only', activate=False))
    await asyncio.wait_for(broker.publishes.reach(1), _TIMEOUT)
    loud = asyncio.create_task(runtime.send(second, 'wake up'))
    await asyncio.wait_for(broker.publishes.reach(2), _TIMEOUT)
    broker.publishes.open()

    assert (await asyncio.wait_for(quiet, _TIMEOUT)).ok is False
    assert (await asyncio.wait_for(loud, _TIMEOUT)).ok is True
    await asyncio.wait_for(runtime.wait(second), _TIMEOUT)
    assert [len(llm.calls) for llm in llms] == [1, 2]
    await runtime.shutdown()


async def test_suppressing_activation_does_not_outlive_the_send_that_asked_for_it():
    """抑制位按调用生效，不残留在这条任务的上下文里。

    残留下来的话，此后每一次投递都静默失去激活能力——而「队列不存在」是复活的唯一触发点，
    投递方拿到的仍是一句「没人把邮箱建起来」，看不出激活是被上一次调用关掉的。
    """
    runtime = Runtime()
    llms = [
        ScriptedLLM([ScriptedTurn(content='a1'), ScriptedTurn(content='a2')]),
        ScriptedLLM([ScriptedTurn(content='b1'), ScriptedTurn(content='b2')]),
    ]
    quiet, loud = [await runtime.spawn(_factory(llm), query='go') for llm in llms]
    await runtime.wait(quiet)
    await runtime.wait(loud)

    assert (await runtime.send(quiet, 'queue only', activate=False)).ok is False
    # 同一条任务里紧接着的一次投递照常激活：回调链上的「按需激活」不是本 Runtime 的一个模式位。
    delivered = await runtime.broker.publish(
        loud, Message(sender='user', receiver=loud, content='wake up')
    )

    assert delivered.ok is True
    await asyncio.wait_for(runtime.wait(loud), _TIMEOUT)
    assert [len(llm.calls) for llm in llms] == [1, 2]
    assert await _status(runtime, quiet) is AgentStatus.ZOMBIE
    await runtime.shutdown()


async def test_send_stamps_each_calls_own_sender_and_correlation_id_on_its_message():
    """两个参数各有下游消费者（`wait_for` 认 correlation_id，编排工具认 sender）。

    发不出去的参数就是一个静默什么都不做的旋钮。三条而非一条，且发件人顺序既非升序
    （alpha / bravo / charlie）亦非降序，反转后同样两者都不是。
    """
    broker = InMemoryMessageBroker()
    runtime = Runtime(broker=broker)
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking(blocker), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)

    plan = [('bravo', 'c-bravo'), ('alpha', None), ('charlie', 'c-charlie')]
    for sender, correlation_id in plan:
        result = await runtime.send(
            agent_id, f'from {sender}', sender=sender, correlation_id=correlation_id
        )
        assert result.ok is True
    assert (await runtime.send(agent_id, 'from the runtime')).ok is True

    delivered = await broker.consume(agent_id)
    assert [(message.sender, message.correlation_id) for message in delivered] == [
        *plan,
        ('runtime', None),
    ]
    assert [message.content for message in delivered] == [
        'from bravo',
        'from alpha',
        'from charlie',
        'from the runtime',
    ]
    await runtime.shutdown()


async def test_ensure_active_is_a_question_and_answers_false_for_an_unknown_agent(
    caplog: pytest.LogCaptureFixture,
):
    """答不出就答 False，不抛异常，也不留下一个没人消费的邮箱。

    「这个 Agent 不归我」是回调链上设计好的一支（后面的回调接手），因此它不记日志。
    """
    broker = InMemoryMessageBroker()
    runtime = Runtime(broker=broker)

    assert await runtime.ensure_active('nope') is False

    assert await _mailbox_exists(broker, 'nope') is False
    assert caplog.records == []
    await runtime.shutdown()


async def test_ensure_active_on_a_parked_agent_answers_true_without_starting_a_second_driver(
    caplog: pytest.LogCaptureFixture,
):
    """停在邮箱上的 Agent 状态是 IDLE，而 IDLE -> RUNNING 的 CAS 对第二个驱动任务照样成功。

    所有权凭据因此只能是驱动任务本身：两道门会各自 consume 同一个邮箱，而 consume 恰好一次，
    输的那个静默收不到任何消息。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    llm = ScriptedLLM([ScriptedTurn(content='woken'), ScriptedTurn(content='woken again')])
    agent_id = await runtime.spawn(_factory(llm), exit_mode=ExitMode.IDLE)
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)
    parked = runtime._table.get_task(agent_id)

    assert await runtime.ensure_active(agent_id) is True

    assert runtime._table.get_task(agent_id) is parked
    assert await _status(runtime, agent_id) is AgentStatus.IDLE
    assert broker.parks(agent_id) == 1

    assert (await runtime.send(agent_id, 'wake up')).ok is True
    await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
    assert [block.content for block in await broker.consume(agent_id)] == []
    assert len(llm.calls) == 1
    assert caplog.records == []
    await runtime.shutdown()


async def test_ensure_active_answers_true_for_a_live_agent_whatever_the_registry_calls_it(
    caplog: pytest.LogCaptureFixture,
):
    """判据是驱动任务活着，不是状态恰好落在某个可复活的取值上。

    控制面在周期内能留下 RUNNING / PAUSED / WAITING，拿状态当判据会让「它在跑吗」对这三个
    一律答 False，而它们每一个都真的在跑。三个而非一个，顺序既非声明序亦非其反转。
    """
    registry = _RecordingRegistry()
    runtime = Runtime(registry=registry)
    for status in (AgentStatus.PAUSED, AgentStatus.WAITING, AgentStatus.RUNNING):
        blocker = _Block()
        agent_id = await runtime.spawn(_blocking(blocker), query='go')
        await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
        assert await registry.compare_and_set_status(
            agent_id, expected=AgentStatus.RUNNING, new=status
        )
        driving = runtime._table.get_task(agent_id)

        assert await runtime.ensure_active(agent_id) is True

        assert runtime._table.get_task(agent_id) is driving
        assert await _status(runtime, agent_id) is status
        blocker.release.set()
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert caplog.records == []
    await runtime.shutdown()


async def test_ensure_active_refuses_an_agent_that_is_winding_down(
    caplog: pytest.LogCaptureFixture,
):
    """ABORTING 是「中止已置位、正在收尾」：此刻重开事件循环就是和收尾抢同一份轨迹。"""
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go')
    await runtime.wait(agent_id)
    assert await registry.compare_and_set_status(
        agent_id, expected=AgentStatus.ZOMBIE, new=AgentStatus.ABORTING
    )

    assert await runtime.ensure_active(agent_id) is False

    assert await _status(runtime, agent_id) is AgentStatus.ABORTING
    assert broker.parks(agent_id) == 0
    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert caplog.records[0].args == (agent_id,)
    _assert_three_part(caplog.records[0].getMessage())
    await runtime.shutdown()


async def test_ensure_active_refuses_an_agent_the_registry_says_someone_else_is_running(
    caplog: pytest.LogCaptureFixture,
):
    """注册表说 RUNNING 而本进程手里的驱动任务已经结束，就是 spec §4.3 说的那种孤儿条目。

    本进程既不知道谁在跑它，也没有租约可以判它已死，因此答不出就答 False；而「读到的是什么」
    是这条日志与「它正在收尾」唯一的区别，少报这一项，两种情形的下一步动作就分不开了。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go')
    await runtime.wait(agent_id)
    assert await registry.compare_and_set_status(
        agent_id, expected=AgentStatus.ZOMBIE, new=AgentStatus.RUNNING
    )

    assert await runtime.ensure_active(agent_id) is False

    assert await _status(runtime, agent_id) is AgentStatus.RUNNING
    assert broker.parks(agent_id) == 0
    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert caplog.records[0].args == (agent_id, AgentStatus.RUNNING.value)
    _assert_three_part(caplog.records[0].getMessage())
    await runtime.shutdown()


async def test_ensure_active_refuses_an_agent_this_process_does_not_hold(
    caplog: pytest.LogCaptureFixture,
):
    """注册表可以共享，实例表不行：本进程手里没有实例就重开不了它的事件循环。"""
    runtime = Runtime()
    agent_id = await runtime.spawn(_factory(_done()), query='go')
    await runtime.wait(agent_id)
    second = Runtime(registry=runtime.registry, broker=runtime.broker)

    assert await second.ensure_active(agent_id) is False

    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    assert second._table.agent_ids() == []
    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert caplog.records[0].args == (agent_id,)
    _assert_three_part(caplog.records[0].getMessage())
    await second.shutdown()
    await runtime.shutdown()


async def test_ensure_active_refuses_after_shutdown(caplog: pytest.LogCaptureFixture):
    """shutdown 是终态：此后 spawn 报错，激活同样不再发生。

    两种停驻分开断言，否则「投递也失败了」看起来像 `send` 守住了终态，实际是队列已经不在。
    ZOMBIE 的队列在 `_finish` 里摘过，投递因此拿到诚实的失败；停在邮箱上的那个是被取消的，
    而取消不做收尾，邮箱还在，投递照答 `ok=True`——消息落进一个再没有消费者的地方。
    `ensure_active` 两边都答 False，是 `send` 不看它的答案。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    zombied = await runtime.spawn(_factory(_done()), query='go')
    parked = await runtime.spawn(_factory(_done()), exit_mode=ExitMode.IDLE)
    await runtime.wait(zombied)
    await asyncio.wait_for(broker.reach(parked, 1), _TIMEOUT)
    await runtime.shutdown()

    assert [await runtime.ensure_active(agent_id) for agent_id in (zombied, parked)] == [
        False,
        False,
    ]

    assert [(await runtime.send(agent_id, 'wake up')).ok for agent_id in (zombied, parked)] == [
        False,
        True,
    ]
    assert [message.content for message in await broker.consume(zombied)] == []
    assert [message.content for message in await broker.consume(parked)] == ['wake up']
    assert [broker.parks(agent_id) for agent_id in (zombied, parked)] == [0, 1]
    assert caplog.records == []


async def test_two_concurrent_activations_of_one_zombie_start_exactly_one_driver():
    """激活是唯一入口，因此「一个 Agent 只有一个驱动」这条也在它这里成立。

    复检若隔着挂起点，第二次会撞上实例表拒绝替换未结束任务，把一个只答不抛的问句变成抛异常，
    同时丢下一个无人认领的驱动任务。

    第二次激活要等第一次已经钉在 `ensure_queue` 上再发起：那时状态稳定在 idle，它的 `_switch`
    与注册表挂不挂起无关。两个一起 gather 的话，换一个同样合规、但 CAS 会挂起的注册表
    （任何远端实现都挂起）就只有一个到得了 `ensure_queue`，握手在上界处超时——而它要断言的那条
    不变式仍然成立。
    """
    broker = _HeldBroker()
    runtime = Runtime(broker=broker)
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    agent_id = await runtime.spawn(_factory(llm), query='go')
    await runtime.wait(agent_id)

    broker.ensures.close()
    first = asyncio.create_task(runtime.ensure_active(agent_id))
    await asyncio.wait_for(broker.ensures.reach(1), _TIMEOUT)
    second = asyncio.create_task(runtime.ensure_active(agent_id))
    await asyncio.wait_for(broker.ensures.reach(2), _TIMEOUT)
    broker.ensures.open()

    assert [
        await asyncio.wait_for(first, _TIMEOUT),
        await asyncio.wait_for(second, _TIMEOUT),
    ] == [True, True]
    # 快照取在两次激活都返回的那一刻：多出来的那个驱动没人持有，只会停在同一个邮箱上不动。
    live = [task for task in asyncio.all_tasks() if task.get_name() == f'agent:{agent_id}']
    assert live == [runtime._table.get_task(agent_id)]

    assert await _status(runtime, agent_id) is AgentStatus.IDLE
    assert (await runtime.send(agent_id, 'keep going')).ok is True
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert [block.content for block in ctx.current_traj if block.role == 'user'] == [
        'go',
        'keep going',
    ]
    assert len(llm.calls) == 2
    await runtime.shutdown()


async def test_find_still_never_activates():
    """只读路径与激活路径必须分离，否则查询类工具无法把「不存在」与「存在但未运行」分开答。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    agent_id = await runtime.spawn(_factory(llm), query='go')
    await runtime.wait(agent_id)
    finished = runtime._table.get_task(agent_id)

    known = await runtime.find(agent_id)
    unknown = await runtime.find('nope')

    assert known is not None and known.status is AgentStatus.ZOMBIE
    assert unknown is None
    # 邮箱仍然不存在：不激活的投递失败当且仅当邮箱不在。
    assert (await runtime.send(agent_id, 'x', activate=False)).ok is False
    assert runtime._table.get_task(agent_id) is finished
    assert broker.parks(agent_id) == 0
    assert len(llm.calls) == 1
    await runtime.shutdown()


async def test_the_runtimes_callback_takes_its_turn_in_the_chain_and_only_stops_it_by_reviving():
    """回调链按注册顺序走、停在第一个认领者上；本 Runtime 只是链上的一个。

    四个而非一个：只有一个回调时「按顺序走」「停在认领者上」「放弃时让后面的接手」是同一个
    答案。三个陪跑回调的顺序既非升序（alpha / bravo / charlie）亦非降序，反转后同样两者都不是。
    """
    broker = InMemoryMessageBroker()
    seen: list[str] = []
    broker.on_missing_queue(_declining('bravo', seen))
    runtime = Runtime(broker=broker)
    broker.on_missing_queue(_declining('alpha', seen))
    broker.on_missing_queue(_declining('charlie', seen))
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    agent_id = await runtime.spawn(_factory(llm), query='go')
    await runtime.wait(agent_id)

    assert (await runtime.send(agent_id, 'wake up')).ok is True
    assert seen == ['bravo']
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    seen.clear()
    assert (await runtime.send(agent_id, 'quiet', activate=False)).ok is False
    assert seen == ['bravo', 'alpha', 'charlie']
    await runtime.shutdown()


async def test_the_callback_declines_rather_than_claim_a_mailbox_it_did_not_build(
    caplog: pytest.LogCaptureFixture,
):
    """回调答 True 的含义是「已认领**并把邮箱建好**」（见 `MessageBroker.on_missing_queue`）。

    活驱动那一支不建邮箱，而 `_finish` 摘完队列、还没转 ZOMBIE 的那一刻正落在这里。谎报认领会
    掐断回调链：后面那些本来能把邮箱建起来的回调根本不会被问到。
    """
    registry = _RecordingRegistry(hold_before=AgentStatus.ZOMBIE)
    broker = InMemoryMessageBroker()
    runtime = Runtime(registry=registry, broker=broker)
    seen: list[str] = []
    for name in ('bravo', 'alpha', 'charlie'):
        broker.on_missing_queue(_declining(name, seen))
    registry.switches.close()
    agent_id = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.ZOMBIE)
    await asyncio.wait_for(registry.switches.reach(1), _TIMEOUT)

    delivered = await runtime.send(agent_id, 'while it winds down')

    assert seen == ['bravo', 'alpha', 'charlie']
    # 收据仍然诚实：没人把邮箱建起来，投递就该失败——代价本来只在链上，不在数据上。
    assert delivered.ok is False
    assert delivered.reason is not None
    _assert_three_part(delivered.reason)
    assert caplog.records == []

    registry.switches.open()
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    await runtime.shutdown()


async def test_shutdown_takes_its_own_callback_off_a_shared_brokers_chain_and_only_its_own(
    caplog: pytest.LogCaptureFixture,
):
    """回调链归 broker，活得比任何一个 Runtime 长。

    不摘，共享 broker 上每个关停的 Runtime 都在链上留一条永远答 False 的死回调，并靠那条引用
    把自己连同持有的 Agent 实例钉在内存里；摘多了，别的 Runtime 的 ZOMBIE 再也复活不了。
    三个而非一个、关掉的是中间那个：只关首尾时「摘掉自己那条」与「摘掉第一条」「摘掉最后一条」
    是同一个答案。
    """
    broker = InMemoryMessageBroker()
    runtimes = [Runtime(broker=broker) for _ in range(3)]
    llms = [
        ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
        for _ in range(3)
    ]
    ids = [await runtimes[index].spawn(_factory(llms[index]), query='go') for index in range(3)]
    for index in range(3):
        await runtimes[index].wait(ids[index])

    await runtimes[1].shutdown()

    assert broker._callbacks == [runtimes[0]._on_missing_queue, runtimes[2]._on_missing_queue]
    assert (await runtimes[1].send(ids[1], 'too late')).ok is False
    for index in (0, 2):
        assert (await runtimes[index].send(ids[index], 'keep going')).ok is True
        await asyncio.wait_for(runtimes[index].wait(ids[index]), _TIMEOUT)
    assert [len(llm.calls) for llm in llms] == [2, 1, 2]
    # 「这个 Agent 不归我」是链上设计好的一支，转手不该报错。
    assert caplog.records == []
    for index in (0, 2):
        await runtimes[index].shutdown()


async def test_a_peer_runtime_on_the_same_chain_passes_along_an_agent_it_does_not_hold():
    """回调链存在的理由就是一个 broker 上挂着多个 Runtime；「这个 Agent 不归我」必须让位。

    共享注册表下每个 Runtime 都查得到这个条目，只有实例表分得出谁真的持有它。认领了却复活不
    了，收件人的 ZOMBIE 就永远醒不过来，而投递方拿到的只是一句「没人把邮箱建起来」。

    持有者排在链上第四位，前后各夹一个记名回调：只有一个陪跑者时「让位」与「链上只有我」是
    同一个答案，持有者排在末位时「停在持有者上」与「走完整条链」也是同一个答案。
    """
    registry = InMemoryAgentRegistry()
    broker = InMemoryMessageBroker()
    seen: list[str] = []
    bravo = Runtime(registry=registry, broker=broker)
    alpha = Runtime(registry=registry, broker=broker)
    broker.on_missing_queue(_declining('past the peers', seen))
    owner = Runtime(registry=registry, broker=broker)
    broker.on_missing_queue(_declining('past the owner', seen))
    charlie = Runtime(registry=registry, broker=broker)
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    agent_id = await owner.spawn(_factory(llm), query='go')
    await owner.wait(agent_id)

    assert (await owner.send(agent_id, 'keep going')).ok is True

    ctx = await asyncio.wait_for(owner.wait(agent_id), _TIMEOUT)
    assert [block.content for block in ctx.current_traj if block.role == 'user'] == [
        'go',
        'keep going',
    ]
    assert len(llm.calls) == 2
    assert seen == ['past the peers']
    for runtime in (bravo, alpha, owner, charlie):
        await runtime.shutdown()


async def test_suppressing_activation_reaches_the_runtime_that_actually_holds_the_agent(
    caplog: pytest.LogCaptureFixture,
):
    """抑制位的作用域是**这一次 publish**，不是发起它的那个 Runtime。

    共享注册表 + 共享 broker 下，投递方与持有者可以是两个不同的 Runtime。抑制位若挂在实例上，
    持有者的回调读不到别人设的那一位，`activate=False` 就静默把它叫醒了。
    """
    registry = InMemoryAgentRegistry()
    broker = InMemoryMessageBroker()
    owner = Runtime(registry=registry, broker=broker)
    peer = Runtime(registry=registry, broker=broker)
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    agent_id = await owner.spawn(_factory(llm), query='go')
    await owner.wait(agent_id)

    quiet = await peer.send(agent_id, 'queue only', activate=False)

    assert quiet.ok is False
    assert await _status(owner, agent_id) is AgentStatus.ZOMBIE
    assert len(llm.calls) == 1
    # 同一个投递方、同一条链，只摘掉抑制位：持有者照常复活，上面那条因此不是「反正都醒不了」。
    assert (await peer.send(agent_id, 'wake up')).ok is True
    await asyncio.wait_for(owner.wait(agent_id), _TIMEOUT)
    assert len(llm.calls) == 2
    assert caplog.records == []
    await peer.shutdown()
    await owner.shutdown()


async def test_descendants_lists_every_generation_breadth_first_and_never_the_named_agent():
    """pause / abort / despawn 三条路的公共前置：浅的在前，被点名的那一个不在其中。"""
    runtime = Runtime()
    await _register_tree(runtime.registry)

    assert await runtime._descendants('root') == ['bravo', 'alpha', 'charlie', 'delta', 'echo']
    # 从被点名的那一个出发，不是从树根出发。
    assert await runtime._descendants('bravo') == ['delta']
    assert await runtime._descendants('echo') == []
    assert await runtime._descendants('nope') == []
    await runtime.shutdown()


async def test_descendants_converges_on_a_registry_that_reports_a_cycle():
    """`visited` 不是防御性编程：注册表可以是远端的、可以被别的进程写过。

    每个节点恰好被问一次正是 `visited` 的职责；少了它，这个遍历在环上永不返回，而三条控制面
    路径都要先经过它。
    """
    registry = _LoopingRegistry(
        {
            'root': ['bravo'],
            'bravo': ['alpha', 'root'],
            'alpha': ['charlie', 'bravo'],
            'charlie': ['alpha'],
        }
    )
    runtime = Runtime(registry=registry)

    assert await runtime._descendants('root') == ['bravo', 'alpha', 'charlie']

    assert registry.asked == ['root', 'bravo', 'alpha', 'charlie']
    await runtime.shutdown()


async def test_a_paused_agent_does_not_consume_its_next_message_and_resume_does_not_lose_it():
    """门坐在「有消息」与「取走消息」之间：醒来照常 consume 的话，resume 之后它已经不在了。

    判据是 consume 的**次数**，不是邮箱里最后剩几条：一个取走再放回、或取走进暂存的门，邮箱与
    轨迹上给的都是同一个答案。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    agent_id = await _resident(runtime, broker)
    agent = runtime._table.get(agent_id)
    assert agent is not None
    control = runtime._slots[agent_id].control

    await runtime.pause(agent_id)
    assert (await runtime.send(agent_id, 'arrived while paused')).ok is True
    assert await control.wait_until_parked(_TIMEOUT) is True
    assert agent.context.current_traj == []
    assert broker.consumes(agent_id) == 0

    await runtime.resume(agent_id)
    await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)

    assert broker.consumes(agent_id) == 1
    assert [(block.role, block.content) for block in agent.context.current_traj] == [
        ('user', 'arrived while paused'),
        ('assistant', 'turn-0'),
    ]
    # 恰好一次：邮箱现在是空的，那条消息既没丢也没被注入两遍。
    assert await broker.consume(agent_id) == []
    await runtime.shutdown()


async def test_a_pause_taken_inside_a_turn_consumes_nothing_before_the_next_turns_gate_opens():
    """挂起门有两道：周期之间那道在 `_cycles` 里，轮与轮之间那道在 `BrokerTurnGate` 里。

    周期之间挂起的用例根本走不到第二道门——它停在开周期之前，`before_turn` 一次都没被调用。要让
    「取走了又放回去 / 取走了暂存起来」有机会发生，消息必须在门被抬起**之前**就躺在邮箱里。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking(blocker), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    control = runtime._slots[agent_id].control
    # 第一轮的门已经取过一次空邮箱：每一轮恰好一次 consume。
    assert broker.consumes(agent_id) == 1

    await runtime.pause(agent_id)
    assert (await runtime.send(agent_id, 'arrived while paused')).ok is True
    blocker.release.set()
    assert await control.wait_until_parked(_TIMEOUT) is True

    assert broker.consumes(agent_id) == 1

    await runtime.resume(agent_id)
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert broker.consumes(agent_id) == 2
    assert [(block.role, block.content) for block in ctx.current_traj] == [
        ('user', 'go'),
        ('assistant', None),
        ('tool', 'released'),
        ('user', 'arrived while paused'),
        ('assistant', 'done'),
    ]
    await runtime.shutdown()


async def test_pause_and_resume_survive_three_full_round_trips_on_one_agent():
    """挂起是电平、唤醒是边沿：把唤醒当电平用，第二次入园就退化成不让出控制权的空转。

    这条性质要跨**第二次**往返才现身，只挂起 / 恢复一次的用例对它完全无感；而空转击穿每一层
    asyncio 超时作用域，表现为整套测试挂死而不是一条红色断言。三轮而非两轮，消息顺序既非升序
    亦非降序，反转后同样两者都不是。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    agent_id = await _resident(runtime, broker)
    agent = runtime._table.get(agent_id)
    assert agent is not None
    control = runtime._slots[agent_id].control

    for parks, content in enumerate(('bravo', 'alpha', 'charlie'), start=2):
        await runtime.pause(agent_id)
        assert (await runtime.send(agent_id, content)).ok is True
        assert await control.wait_until_parked(_TIMEOUT) is True
        await runtime.resume(agent_id)
        await asyncio.wait_for(broker.reach(agent_id, parks), _TIMEOUT)

    assert [block.content for block in agent.context.current_traj] == [
        'bravo',
        'turn-0',
        'alpha',
        'turn-1',
        'charlie',
        'turn-2',
    ]
    assert await _status(runtime, agent_id) is AgentStatus.IDLE
    assert control.paused is False
    await runtime.shutdown()


async def test_pause_and_resume_are_both_idempotent_on_an_agent_between_cycles():
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await _resident(runtime, broker)
    control = runtime._slots[agent_id].control

    await runtime.pause(agent_id)
    await runtime.pause(agent_id)
    assert await _status(runtime, agent_id) is AgentStatus.PAUSED
    assert control.paused is True

    await runtime.resume(agent_id)
    await runtime.resume(agent_id)
    assert await _status(runtime, agent_id) is AgentStatus.IDLE
    assert control.paused is False

    # 周期之间挂起的那个回 IDLE，不回 RUNNING；第二次的后置条件已成立，走的是 new -> new。
    assert registry.applied_of(agent_id) == [
        (AgentStatus.IDLE, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.IDLE),
        (AgentStatus.IDLE, AgentStatus.PAUSED),
        (AgentStatus.PAUSED, AgentStatus.PAUSED),
        (AgentStatus.PAUSED, AgentStatus.IDLE),
        (AgentStatus.IDLE, AgentStatus.IDLE),
    ]
    await runtime.shutdown()


async def test_pausing_an_agent_inside_a_turn_returns_at_once_and_stops_it_at_the_next_boundary():
    """「已请求挂起」与「真的停下来了」是两件事：turn 循环可能正卡在一次模型调用里。

    挂起的那段时间不计入任何一轮——门在计数之前，所以这一轮既不会被切成两半，也不多花一次调用。
    """
    registry = _RecordingRegistry()
    runtime = Runtime(registry=registry)
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking(blocker), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    control = runtime._slots[agent_id].control

    await runtime.pause(agent_id)

    assert await _status(runtime, agent_id) is AgentStatus.PAUSED
    assert control.parked == 0

    blocker.release.set()
    assert await control.wait_until_parked(_TIMEOUT) is True
    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert [block.role for block in agent.context.current_traj] == ['user', 'assistant', 'tool']

    await runtime.resume(agent_id)
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert [(block.role, block.content) for block in ctx.current_traj] == [
        ('user', 'go'),
        ('assistant', None),
        ('tool', 'released'),
        ('assistant', 'done'),
    ]
    # 在一次 run() 之内挂起就回 RUNNING：注册表上不记「挂起之前是什么」，控制块说了算。
    assert registry.applied_of(agent_id) == [
        (AgentStatus.IDLE, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.PAUSED),
        (AgentStatus.PAUSED, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.ZOMBIE),
    ]
    await runtime.shutdown()


async def test_pause_enters_from_every_status_that_still_has_an_event_loop_to_stop():
    """不在可挂起集合里的活状态会静默转不过去：拒绝的理由说得通，而 CAS 根本没生效。

    WAITING 由后续任务产出。三个而非一个，顺序既非声明序亦非其反转。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    for status in (AgentStatus.WAITING, AgentStatus.IDLE, AgentStatus.RUNNING):
        agent_id = await _resident(runtime, broker)
        assert await registry.compare_and_set_status(
            agent_id, expected=AgentStatus.IDLE, new=status
        )

        await runtime.pause(agent_id)

        assert await _status(runtime, agent_id) is AgentStatus.PAUSED
        assert runtime._slots[agent_id].control.paused is True
        assert registry.applied_of(agent_id)[-1] == (status, AgentStatus.PAUSED)
    await runtime.shutdown()


async def test_pausing_a_zombie_is_refused_and_leaves_it_revivable():
    """ZOMBIE 的注册表状态正是 `ensure_active` 复活它的凭据；写上 PAUSED 它就再也叫不醒。"""
    runtime = Runtime()
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    agent_id = await runtime.spawn(_factory(llm), query='go')
    await runtime.wait(agent_id)
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE

    with pytest.raises(ContractViolation) as excinfo:
        await runtime.pause(agent_id)

    assert agent_id in excinfo.value.expected
    assert AgentStatus.ZOMBIE.value in excinfo.value.received
    assert runtime._slots[agent_id].control.paused is False
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE

    assert (await runtime.send(agent_id, 'keep going')).ok is True
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert [block.content for block in ctx.current_traj if block.role == 'assistant'] == [
        'first',
        'second',
    ]
    await runtime.shutdown()


async def test_pausing_an_agent_that_is_winding_down_is_refused():
    """ABORTING 正在收尾，挂起一个正在拆的东西没有可表达的意图。"""
    registry = _RecordingRegistry()
    runtime = Runtime(registry=registry)
    agent_id = await runtime.spawn(_factory(_done()), query='go')
    await runtime.wait(agent_id)
    assert await registry.compare_and_set_status(
        agent_id, expected=AgentStatus.ZOMBIE, new=AgentStatus.ABORTING
    )

    with pytest.raises(ContractViolation) as excinfo:
        await runtime.pause(agent_id)

    assert AgentStatus.ABORTING.value in excinfo.value.received
    assert runtime._slots[agent_id].control.paused is False
    assert await _status(runtime, agent_id) is AgentStatus.ABORTING
    await runtime.shutdown()


async def test_each_control_operation_names_itself_when_it_refuses_an_unknown_agent():
    """裸 KeyError 违反错误三件套；而说错了操作名，调用方会去查另一条路径。"""
    runtime = Runtime()

    with pytest.raises(ContractViolation) as paused:
        await runtime.pause('nope')
    with pytest.raises(ContractViolation) as resumed:
        await runtime.resume('nope')

    assert 'pause' in paused.value.expected
    assert 'resume' in resumed.value.expected
    assert 'nope' in paused.value.expected and 'nope' in resumed.value.expected
    assert paused.value.next_action == resumed.value.next_action
    await runtime.shutdown()


async def test_pause_tells_an_unknown_agent_apart_from_one_another_process_holds():
    """两种「取不到」的下一步动作不同：一个要重新 spawn，一个要去持有它的进程上操作。"""
    broker = _SpyBroker()
    owner = Runtime(broker=broker)
    agent_id = await _resident(owner, broker)
    peer = Runtime(registry=owner.registry, broker=owner.broker)

    with pytest.raises(ContractViolation) as unknown:
        await peer.pause('nope')
    with pytest.raises(ContractViolation) as not_mine:
        await peer.pause(agent_id)

    assert 'nope' in unknown.value.expected
    assert agent_id in not_mine.value.expected
    assert unknown.value.received != not_mine.value.received
    # 作用在错的主体上：peer 既不该按住持有者的 Agent，也不该改它的状态。
    assert await _status(owner, agent_id) is AgentStatus.IDLE
    assert owner._slots[agent_id].control.paused is False
    await peer.shutdown()
    await owner.shutdown()


async def test_recursive_pause_reaches_every_descendant_and_nothing_outside_the_subtree():
    """六个 Agent 跨三代，另有一个不在这棵树上：按住整个 Runtime 与按住一棵子树都能让树内全绿。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    root = await _resident(runtime, broker)
    bravo, alpha, charlie = [await _resident(runtime, broker, parent_id=root) for _ in range(3)]
    delta = await _resident(runtime, broker, parent_id=bravo)
    echo = await _resident(runtime, broker, parent_id=alpha)
    outsider = await _resident(runtime, broker)

    await runtime.pause(root, recursive=True)

    tree = (root, bravo, alpha, charlie, delta, echo)
    assert [await _status(runtime, agent_id) for agent_id in tree] == [AgentStatus.PAUSED] * 6
    assert [runtime._slots[agent_id].control.paused for agent_id in tree] == [True] * 6

    assert await _status(runtime, outsider) is AgentStatus.IDLE
    assert runtime._slots[outsider].control.paused is False
    assert (await runtime.send(outsider, 'go ahead')).ok is True
    await asyncio.wait_for(broker.reach(outsider, 2), _TIMEOUT)
    outside_agent = runtime._table.get(outsider)
    assert outside_agent is not None
    assert [block.content for block in outside_agent.context.current_traj] == [
        'go ahead',
        'turn-0',
    ]
    await runtime.shutdown()


async def test_recursive_pause_from_a_node_with_a_parent_never_climbs_to_its_ancestors():
    """`recursive` 说的是**子树**，不是整个家族。

    既有的每一条递归用例都作用在一个**没有父**的根上，于是「只往下走」与「先爬到森林根再往下走」
    是同一个答案，连「顺带把直接父也挂起」都看不出来。两个深度上各三个子代，注册顺序既非升序
    亦非降序，反转后同样两者都不是。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    root = await _resident(runtime, broker)
    bravo, alpha, charlie = [await _resident(runtime, broker, parent_id=root) for _ in range(3)]
    echo, foxtrot, delta = [await _resident(runtime, broker, parent_id=bravo) for _ in range(3)]

    await runtime.pause(bravo, recursive=True)

    subtree = (bravo, echo, foxtrot, delta)
    assert [await _status(runtime, agent_id) for agent_id in subtree] == [AgentStatus.PAUSED] * 4
    assert [runtime._slots[agent_id].control.paused for agent_id in subtree] == [True] * 4

    above = (root, alpha, charlie)
    assert [await _status(runtime, agent_id) for agent_id in above] == [AgentStatus.IDLE] * 3
    assert [runtime._slots[agent_id].control.paused for agent_id in above] == [False] * 3
    # 标志位为假也可能是「按住了又放开」；跑完一个周期才证明这道门从头到尾没落下来过。
    for index, agent_id in enumerate(above):
        assert (await runtime.send(agent_id, f'still working {index}')).ok is True
        await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
        agent = runtime._table.get(agent_id)
        assert agent is not None
        assert [block.content for block in agent.context.current_traj] == [
            f'still working {index}',
            'turn-0',
        ]
    await runtime.shutdown()


async def test_recursive_resume_of_an_interior_subtree_leaves_the_rest_of_the_tree_paused():
    """正操作作用在超集、逆操作作用在一棵**真子树**：差集必须原样停着。

    pause 与 resume 施加在同一个根上时，「只恢复这棵子树」与「恢复整棵树」是同一个答案。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    root = await _resident(runtime, broker)
    bravo, alpha, charlie = [await _resident(runtime, broker, parent_id=root) for _ in range(3)]
    echo, foxtrot, delta = [await _resident(runtime, broker, parent_id=bravo) for _ in range(3)]
    await runtime.pause(root, recursive=True)

    await runtime.resume(bravo, recursive=True)

    freed = (bravo, echo, foxtrot, delta)
    assert [await _status(runtime, agent_id) for agent_id in freed] == [AgentStatus.IDLE] * 4
    assert [runtime._slots[agent_id].control.paused for agent_id in freed] == [False] * 4

    held = (root, alpha, charlie)
    assert [await _status(runtime, agent_id) for agent_id in held] == [AgentStatus.PAUSED] * 3
    assert [runtime._slots[agent_id].control.paused for agent_id in held] == [True] * 3
    # 同一条消息把两边分开：恢复的那些跑完一个周期，其余的停在门上，一轮都不跑。
    for agent_id in (*freed, *held):
        assert (await runtime.send(agent_id, 'go ahead')).ok is True
    for agent_id in freed:
        await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
    for agent_id in held:
        assert await runtime._slots[agent_id].control.wait_until_parked(_TIMEOUT) is True
        agent = runtime._table.get(agent_id)
        assert agent is not None
        assert agent.context.current_traj == []
    await runtime.shutdown()


async def test_recursive_pause_goes_parent_first_while_recursive_resume_goes_children_first():
    """方向不是对称性：pause 先掐掉还会生出新子代的那一头，resume 先让子代就位。

    父恢复之后可能立刻要用子代，先恢复子代，它就不会看见一棵只恢复了一半的树。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    root = await _resident(runtime, broker)
    bravo, alpha, charlie = [await _resident(runtime, broker, parent_id=root) for _ in range(3)]
    delta = await _resident(runtime, broker, parent_id=bravo)

    await runtime.pause(root, recursive=True)
    await runtime.resume(root, recursive=True)

    assert [agent_id for agent_id, _, new in registry.applied if new is AgentStatus.PAUSED] == [
        root,
        bravo,
        alpha,
        charlie,
        delta,
    ]
    assert [
        agent_id
        for agent_id, expected, new in registry.applied
        if expected is AgentStatus.PAUSED and new is AgentStatus.IDLE
    ] == [delta, charlie, alpha, bravo, root]
    assert [
        await _status(runtime, agent_id) for agent_id in (root, bravo, alpha, charlie, delta)
    ] == [AgentStatus.IDLE] * 5
    await runtime.shutdown()


async def test_recursive_resume_hands_every_descendant_back_its_own_pending_message():
    """恢复是逐个 Agent 的：共用一个控制块时，抬一次门会把整棵树一起放走。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    root = await _resident(runtime, broker)
    bravo, alpha = [await _resident(runtime, broker, parent_id=root) for _ in range(2)]
    delta = await _resident(runtime, broker, parent_id=bravo)
    await runtime.pause(root, recursive=True)

    plan = {root: 'for root', bravo: 'for bravo', alpha: 'for alpha', delta: 'for delta'}
    for agent_id, content in plan.items():
        assert (await runtime.send(agent_id, content)).ok is True
        assert await runtime._slots[agent_id].control.wait_until_parked(_TIMEOUT) is True

    await runtime.resume(root, recursive=True)

    for agent_id, content in plan.items():
        await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
        agent = runtime._table.get(agent_id)
        assert agent is not None
        assert [block.content for block in agent.context.current_traj] == [content, 'turn-0']
    await runtime.shutdown()


async def test_a_descendant_that_cannot_be_paused_is_skipped_without_stopping_the_walk(
    caplog: pytest.LogCaptureFixture,
):
    """递归是尽力而为的：为第一个冲突抛异常只会留下一棵改了一半的树。"""
    caplog.set_level(logging.INFO)
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    root = await _resident(runtime, broker)
    finished = await runtime.spawn(_factory(_done()), query='go', parent_id=root)
    await runtime.wait(finished)
    still_running = await _resident(runtime, broker, parent_id=root)

    await runtime.pause(root, recursive=True)

    assert await _status(runtime, finished) is AgentStatus.ZOMBIE
    assert [await _status(runtime, agent_id) for agent_id in (root, still_running)] == [
        AgentStatus.PAUSED,
        AgentStatus.PAUSED,
    ]
    assert runtime._slots[finished].control.paused is False

    assert [record.levelname for record in caplog.records] == ['INFO']
    skipped = caplog.records[0].getMessage()
    assert 'pause' in skipped
    assert finished in skipped
    assert AgentStatus.ZOMBIE.value in skipped
    assert root not in skipped and still_running not in skipped
    await runtime.shutdown()


async def test_a_descendant_whose_transition_is_refused_is_skipped_without_stopping_the_walk(
    caplog: pytest.LogCaptureFixture,
):
    """CAS 失败按同一条规则处理：子代只记日志，遍历继续走到后面那些。"""
    caplog.set_level(logging.INFO)
    registry = _RecordingRegistry(deny=(AgentStatus.IDLE, AgentStatus.PAUSED))
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    root = await _resident(runtime, broker)
    refused = await _resident(runtime, broker, parent_id=root)
    later = await _resident(runtime, broker, parent_id=root)
    registry.deny_for = refused

    await runtime.pause(root, recursive=True)

    assert [await _status(runtime, agent_id) for agent_id in (root, refused, later)] == [
        AgentStatus.PAUSED,
        AgentStatus.IDLE,
        AgentStatus.PAUSED,
    ]
    assert runtime._slots[refused].control.paused is False
    assert [record.levelname for record in caplog.records] == ['INFO']
    skipped = caplog.records[0].getMessage()
    assert refused in skipped
    assert root not in skipped and later not in skipped
    await runtime.shutdown()


async def test_a_pause_the_registry_refuses_is_reported_and_parks_nothing():
    """记不下 PAUSED 就不要真的停——那会停在一个谁都看不见的地方，而调用方以为按住了它。"""
    registry = _RecordingRegistry(deny=(AgentStatus.IDLE, AgentStatus.PAUSED))
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await _resident(runtime, broker)
    child = await _resident(runtime, broker, parent_id=agent_id)
    registry.deny_for = agent_id

    with pytest.raises(ContractViolation) as excinfo:
        await runtime.pause(agent_id, recursive=True)

    assert agent_id in excinfo.value.expected
    assert runtime._slots[agent_id].control.paused is False
    # 父先于子：被点名的那一个抛出来时，这棵树还没被走过。
    assert await _status(runtime, child) is AgentStatus.IDLE
    assert runtime._slots[child].control.paused is False

    assert (await runtime.send(agent_id, 'still working')).ok is True
    await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert [block.content for block in agent.context.current_traj] == ['still working', 'turn-0']
    await runtime.shutdown()


async def test_a_resume_the_registry_refuses_still_lifts_the_gate(
    caplog: pytest.LogCaptureFixture,
):
    """只有 pause / resume 能抬门，所以记不下就不抬会把它永久停在门上。

    一个过时的状态可以用 find() 重读，一个没人能抬起的门谁都救不回来。
    """
    caplog.set_level(logging.INFO)
    registry = _RecordingRegistry(deny=(AgentStatus.PAUSED, AgentStatus.IDLE))
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await _resident(runtime, broker)
    control = runtime._slots[agent_id].control
    await runtime.pause(agent_id)
    assert (await runtime.send(agent_id, 'arrived while paused')).ok is True
    assert await control.wait_until_parked(_TIMEOUT) is True

    await runtime.resume(agent_id)

    await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert [block.content for block in agent.context.current_traj] == [
        'arrived while paused',
        'turn-0',
    ]
    assert control.paused is False
    assert [record.levelname for record in caplog.records] == ['INFO']
    assert caplog.records[0].args == (agent_id, AgentStatus.IDLE.value)
    _assert_three_part(caplog.records[0].getMessage())
    await runtime.shutdown()


async def test_resuming_an_agent_that_was_never_paused_leaves_it_where_it_is():
    """后置条件已经成立，所以它成功；而它不得把一个从没被挂起的 Agent 挪到别的状态上。"""
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await _resident(runtime, broker)

    await runtime.resume(agent_id)

    assert await _status(runtime, agent_id) is AgentStatus.IDLE
    assert runtime._slots[agent_id].control.paused is False
    assert (await runtime.send(agent_id, 'go ahead')).ok is True
    await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
    assert registry.applied_of(agent_id) == [
        (AgentStatus.IDLE, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.IDLE),
        (AgentStatus.IDLE, AgentStatus.IDLE),
        (AgentStatus.IDLE, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.IDLE),
    ]
    await runtime.shutdown()
