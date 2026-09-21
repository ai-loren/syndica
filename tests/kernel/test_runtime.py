"""本模块**必须**保留 `from __future__ import annotations`：仓库每个模块都带它，于是标注在运行时
是字符串。少了它，这里的工具类持有真类型对象，而 schema 反射按字符串取标注的缺陷在这里全绿、在每个
真实工具上必炸。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from types import MappingProxyType
from typing import Any

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.agent_types import AgentInfo, AgentStatus, ExitMode
from syndica.kernel.context import ContextBlock, StopReason, ToolCall, TrustLevel
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.events import Event, EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import LLMConfig, ScriptedLLM, ScriptedTurn
from syndica.kernel.message import DeliveryResult, Message, Trigger
from syndica.kernel.protocols import AgentRegistry, MissingQueueCallback
from syndica.kernel.runtime import _LIVE_STATUSES, AgentFactory, Runtime, _WaitToken
from syndica.kernel.stores import InMemoryAgentRegistry, InMemoryMessageBroker
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.waiting import (
    Confirmation,
    Reply,
    UserInput,
    Waiter,
    WaitResult,
    WaitState,
)

# 每个等待都设上限：回归应当让用例变红，而不是把 CI 挂住。
_TIMEOUT = 1.0
# 一次要真的跑满的等待上限，以及一次「本次用例期间绝不会到期」的等待上限。后者必须远大于 _TIMEOUT
# ——它要挡的回归是「明明没等到也没被打断，却提前返回了」，而那种返回会被误读成超时。
_BRIEF_WAIT = 0.05
_UNREACHED_WAIT = 30.0
# 一次四小时的人工确认。取到小时量级是为了越过任何「看着合理」的宿主级上限：`wait_for` 不 clamp
# 是明确裁决，而秒级或分钟级的取值挡不住一道 min(timeout, 3600) ——它会把这次确认静默截成一小时。
_LONG_WAIT = 4 * 3600.0
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


class _Wipe(Tool):
    """记录被执行过的路径。第 9 步中止必须让这份清单保持空。"""

    def __init__(self) -> None:
        self.paths: list[str] = []

    async def execute(self, path: str) -> ToolResult:
        """记下 path。"""
        self.paths.append(path)
        return ToolResult(output=path)


class _SlowWipeLLM:
    """进入 create 后挂起，直到测试放行才交出一次 Wipe 调用。

    abort 不取消正在飞的 LLM：create 必须返回，第 9 步才能读到中止位。
    """

    def __init__(self, entered: asyncio.Event, released: asyncio.Event) -> None:
        self.config = LLMConfig(provider='scripted', model='slow-wipe')
        self._entered = entered
        self._released = released

    async def create(self, messages: list[ContextBlock], **kwargs: object) -> ContextBlock:
        self._entered.set()
        await self._released.wait()
        return ContextBlock(
            role='assistant',
            content='will wipe',
            tool_calls=[ToolCall(id='c1', name='Wipe', arguments='{"path":"/data"}')],
        )


class _Retaliate(Tool):
    """停在自己的闸上；被取消时在收尾里拆掉另一棵树。

    「走的时候把我的 helper 一起收掉」是普通的工具代码，而它跑在一个**正在被取消的**驱动任务
    里——于是另一条级联的 `await asyncio.wait({driver})` 落在这段收尾之内，两条级联各自停在对方
    正在拆的节点上。调用方这一侧只是两次普通的 `despawn`。
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.runtime: Runtime | None = None
        self.target: str | None = None

    async def execute(self) -> ToolResult:
        """一直等；被取消时在收尾里拆掉另一棵树。"""
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert self.runtime is not None and self.target is not None
            await self.runtime.despawn(self.target)
        return ToolResult(output='retaliated')


class _Waiting(Tool):
    """在轮的中间发起一次 `wait_for`，那正是它唯一的真实调用点。

    从测试任务里直接调 `wait_for` 只走到同一批状态转换，但 turn 循环并没有真的挂在那次等待上——
    「轮被挂起」与「事件循环在等」的区别正是 `WAITING` 存在的理由，用假停点搭出来的树对它无感。

    等待返回之后停在 `release` 上：这一刻是「轮还在往前走、而等待已经结束」的唯一观测点，注册表
    在它之后就被下一个周期边界改掉了。
    """

    def __init__(self, waiter: Waiter) -> None:
        self.entered = asyncio.Event()
        self.returned = asyncio.Event()
        self.release = asyncio.Event()
        self.waiter = waiter
        self.runtime: Runtime | None = None
        self.agent_id: str | None = None
        self.results: list[WaitResult] = []
        # 在同一次工具调用里、发起等待之前先做的那件事。控制面操作在**这个**位置落下时窗口是整整
        # 一轮（挂起与中止都只在轮边界上兑现），而从测试任务里调 wait_for 走不到这个位置。
        self.before: Callable[[], Awaitable[None]] | None = None

    async def execute(self) -> ToolResult:
        """等一条匹配的答复，把结局记下来，然后等测试放行本轮。"""
        self.entered.set()
        assert self.runtime is not None and self.agent_id is not None
        if self.before is not None:
            await self.before()
        result = await self.runtime.wait_for(self.agent_id, self.waiter)
        self.results.append(result)
        self.returned.set()
        await self.release.wait()
        return ToolResult(output=result.state.value)


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

    `hold_after` 把一次**已经生效**的转换停在 `settles` 这道闸上：远端注册表可以先写进去再返回，
    而「转换已生效、发起它的那一方还没往下走」这段窗口在进程内注册表下根本不存在——一次落在这段
    窗口里的控制面操作看到的是转换之后的状态，却看不到发起方接下来才装上的东西。

    `hold_before` 把「转去某个状态」的 CAS 停在 `switches` 这道闸上：`_finish` 摘完队列、还没
    转 ZOMBIE 的那一刻，在 Runtime 外面没有别的观测点。它可以在构造之后再装上，用来只拦住某个
    时间点之后发生的转换——「驱动任务还活着、但还没接管成功」同样没有别的构造方式。

    `deny_for` 把拒绝收窄到一个 agent。递归操作对「被点名的那一个」与「遍历到的子代」处理不同，
    而只按 (expected, new) 拒绝会同时打中两者，那条子代分支于是永远走不到。

    `hold_children_of` 把遍历停在**某一个节点**的 `children_of` 上：级联拆除先走完整棵树再动手，
    而「走的时候出生的子代」只能在树里某个已经被问过的父下面出现，没有别的构造方式。

    `hold_get_of` 把**某一个** agent 的 `get` 停在半路。`spawn` 的两道复检里带 await 的那一道就是
    这个 `get`，所以「一条级联恰好在这一问的中途钉住整棵树」只有这个停点造得出来——而两道复检
    的先后顺序只在这一刻分叉。

    `fail_unregister` 让一个条目注销不掉。注销失败留下的是永久孤儿（父被注销、它自己留着），
    而这类丢失的唯一证据就是那条日志。

    `silent_unregister` 是同一件事更坏的那一面：**答应了却没真的摘掉**。它是「接缝另一侧的合法
    替换」——不抛、不说谎，只是没兑现 `unregister` 的明文契约；把后置条件从契约里推出来而不去
    复检的实现，在它下面会一声不响地报一次假 `AGENT_DESPAWN`。

    `unregisters` 只记**真的被摘掉的那些**，按摘掉的顺序：重叠的级联会各自对同一个条目调一次
    `unregister`（摘是幂等的），把 no-op 也记下来就分不出「摘了几次」与「消失了几次」。
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
        self.hold_before = hold_before
        self.deny_for = deny_for
        self.hold_after: AgentStatus | None = None
        self.settles = _Hold()
        self.hold_children_of: str | None = None
        self.hold_get_of: str | None = None
        self.fail_unregister: str | None = None
        self.silent_unregister: str | None = None
        self.switches = _Hold()
        self.walks = _Hold()
        self.gets = _Hold()
        self.transitions: list[tuple[str, AgentStatus, AgentStatus]] = []
        self.applied: list[tuple[str, AgentStatus, AgentStatus]] = []
        self.unregisters: list[str] = []

    async def register(self, info: AgentInfo) -> None:
        await self._inner.register(info)

    async def get(self, agent_id: str) -> AgentInfo | None:
        if agent_id == self.hold_get_of:
            await self.gets.passing(agent_id)
        return await self._inner.get(agent_id)

    async def compare_and_set_status(
        self, agent_id: str, *, expected: AgentStatus, new: AgentStatus
    ) -> bool:
        if new is self.hold_before:
            await self.switches.passing(agent_id)
        self.transitions.append((agent_id, expected, new))
        if self._deny == (expected, new) and self.deny_for in (None, agent_id):
            return False
        applied = await self._inner.compare_and_set_status(agent_id, expected=expected, new=new)
        if applied:
            self.applied.append((agent_id, expected, new))
        if applied and new is self.hold_after:
            await self.settles.passing(agent_id)
        return applied

    async def update(self, agent_id: str, **fields: Any) -> None:
        await self._inner.update(agent_id, **fields)

    async def children_of(self, agent_id: str) -> list[str]:
        if agent_id == self.hold_children_of:
            await self.walks.passing(agent_id)
        return await self._inner.children_of(agent_id)

    async def list(
        self, *, status: AgentStatus | None = None, parent_id: str | None = None
    ) -> list[AgentInfo]:
        return await self._inner.list(status=status, parent_id=parent_id)

    async def unregister(self, agent_id: str) -> None:
        if agent_id == self.fail_unregister:
            raise RuntimeError(f'注销 {agent_id} 失败')
        if agent_id == self.silent_unregister:
            return
        if await self._inner.get(agent_id) is not None:
            self.unregisters.append(agent_id)
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

    `hold_removal_of` 把**某一个** agent 的摘队列停在半路。这是拆除写下第一样东西之前的最后一个
    停点，因此「另一条级联此刻正拆着这个节点、什么都还没摘掉」在 Runtime 外面没有别的观测点；
    按 agent 而不是按全局停，才能让一条收尾走完而另一条停着。

    `takes` 与 `reach_take` 是「一次等待真的挂上去了」的观测点：`wait_for` 先把注册表转成 WAITING
    再去问 broker，因此 broker 被问到的那一刻状态与槽里的令牌都已经就位。轮询注册表分不出「还没
    被调度到」与「转换被拒」。`hold_take_of` 把这一问停在**进入 broker 之前**，用来在等待还挂着的
    时候动别的东西（摘掉邮箱、投一条答复、中止它）。
    """

    def __init__(self, registry: AgentRegistry | None = None) -> None:
        self._inner = InMemoryMessageBroker()
        self._registry = registry
        # 边沿信号：入园即置位，等待者扫描前清位。「已入园 n 次」是电平，等的是「又入了一次」。
        self._parked = asyncio.Event()
        self._parks: dict[str, int] = {}
        self._consumes: dict[str, int] = {}
        self._took = asyncio.Event()
        self._takes: dict[str, int] = {}
        # 交给 take_by_correlation 的那个上限，原样记下。`wait_for` 不 clamp 是一条明确裁决，而
        # 它在外面唯一的观测点就是这里：夹一道宿主级上限之后，一次四小时的人工确认会静默变成一
        # 小时，而症状只在长跑里现身。
        self.take_timeouts: list[tuple[str, float | None]] = []
        self.removals: list[tuple[str, AgentStatus | None]] = []
        self.hold_removal_of: str | None = None
        self.removal_hold = _Hold()
        self.hold_take_of: str | None = None
        self.take_hold = _Hold()
        self.fail_take_of: str | None = None

    def parks(self, agent_id: str) -> int:
        return self._parks.get(agent_id, 0)

    def consumes(self, agent_id: str) -> int:
        return self._consumes.get(agent_id, 0)

    def takes(self, agent_id: str) -> int:
        return self._takes.get(agent_id, 0)

    async def reach(self, agent_id: str, parks: int) -> None:
        """等到该 agent 第 `parks` 次停到邮箱上。上界由调用方套一层 `wait_for` 给。"""
        while True:
            self._parked.clear()
            if self.parks(agent_id) >= parks:
                return
            await self._parked.wait()

    async def reach_take(self, agent_id: str, takes: int) -> None:
        """等到该 agent 第 `takes` 次按关联 ID 取消息。上界由调用方套一层 `wait_for` 给。"""
        while True:
            self._took.clear()
            if self.takes(agent_id) >= takes:
                return
            await self._took.wait()

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
        self._takes[agent_id] = self.takes(agent_id) + 1
        self.take_timeouts.append((agent_id, timeout))
        self._took.set()
        if agent_id == self.hold_take_of:
            await self.take_hold.passing(agent_id)
        if agent_id == self.fail_take_of:
            raise RuntimeError(f'取 {agent_id} 的答复失败')
        return await self._inner.take_by_correlation(agent_id, correlation_id, timeout)

    async def remove_queue(self, agent_id: str) -> None:
        if agent_id == self.hold_removal_of:
            await self.removal_hold.passing(agent_id)
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

    `hold_removal_of` 与 `_SpyBroker` 上的那个同义：把**某一个** agent 的摘队列停在半路。级联
    先钉住整棵树再动手，因此「树已经钉住、而一个条目都还没摘掉」只有这个停点看得见——而 `spawn`
    的那道复检要分「级联还在飞」与「级联已经跑完」，两支各需要这棵树处在不同的状态。
    """

    def __init__(self) -> None:
        self._inner = InMemoryMessageBroker()
        self.publishes = _Hold()
        self.ensures = _Hold()
        self.hold_removal_of: str | None = None
        self.removal_hold = _Hold()

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
        if agent_id == self.hold_removal_of:
            await self.removal_hold.passing(agent_id)
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


async def _reaped(runtime: Runtime) -> None:
    """等本 Runtime 已经排出的收割任务收完尾。上界由调用方套一层 `wait_for` 给。

    比轮询注册表状态确定：收尾在一个独立任务里跑，而轮询把「还没被调度到」与「收尾拒绝了这次
    转换」看成同一件事。不吞异常——收割任务里逃出来的异常无人取回时只会在 GC 时变成一行日志。
    """
    while runtime._reapers:
        await asyncio.gather(*list(runtime._reapers))


async def _dangling_parents(registry: AgentRegistry) -> list[str]:
    """`parent_id` 指着一个不在注册表里的条目的那些 agent_id。

    这就是级联拆除要防的那件事本身：父的条目被摘掉而子代留着，`children_of` 从此永远漏掉它们，
    而它们自己看起来还有个父——没有任何后续操作能把这棵树恢复过来。
    """
    infos = await registry.list()
    known = {info.agent_id for info in infos}
    return sorted(
        info.agent_id
        for info in infos
        if info.parent_id is not None and info.parent_id not in known
    )


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


async def _running(
    runtime: Runtime, blockers: list[_Block], *, parent_id: str | None = None
) -> str:
    """起一个真的停在一次工具调用里的 Agent，握手用工具入口而不是注册表。

    把停在邮箱上的 Agent 的注册表改成 RUNNING 只是改了一个字符串：它手上依然没有活，事件循环
    也依然停在邮箱上。`abort` 的判据是驱动任务此刻在哪，用假状态搭出来的树对它完全无感。
    """
    blocker = _Block()
    blockers.append(blocker)
    agent_id = await runtime.spawn(_blocking(blocker), query='go', parent_id=parent_id)
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    return agent_id


async def _armed(
    runtime: Runtime, broker: _SpyBroker, tool: _Waiting, *, parent_id: str | None = None
) -> str:
    """起一个等待型 Agent，停在邮箱上、工具已经拿到自己的 agent_id，但还没开始跑。

    懒启动而不是 spawn 时带 query：工具要拿自己的 agent_id 才能发起等待，而它只在 spawn 返回之后
    才存在——带 query 的 spawn 会让第一轮赶在赋值之前跑起来。
    """
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Waiting', arguments='{}')]),
            ScriptedTurn(content='done'),
        ]
    )
    agent_id = await runtime.spawn(_factory(llm, tools={'Waiting': tool}), parent_id=parent_id)
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)
    tool.runtime = runtime
    tool.agent_id = agent_id
    return agent_id


async def _started(runtime: Runtime, broker: _SpyBroker, tool: _Waiting, agent_id: str) -> None:
    """把它叫起来，等到它真的把 turn 循环挂在自己那次 `wait_for` 上。

    握手用 broker 被问到的那一刻：`wait_for` 先把注册表转成 WAITING 再去取消息，因此这时状态与
    槽里的令牌都已经就位。
    """
    assert (await runtime.send(agent_id, 'go')).ok
    await asyncio.wait_for(tool.entered.wait(), _TIMEOUT)
    await asyncio.wait_for(broker.reach_take(agent_id, 1), _TIMEOUT)


async def _waiting(
    runtime: Runtime, broker: _SpyBroker, tool: _Waiting, *, parent_id: str | None = None
) -> str:
    agent_id = await _armed(runtime, broker, tool, parent_id=parent_id)
    await _started(runtime, broker, tool, agent_id)
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


async def test_send_defaults_to_trusted_and_carries_whatever_trust_it_was_given():
    """`trust` 由写下这段文字的那一方给出：宿主自己写的消息可信，模型复述出来的不一定。

    基数 2 且两档都断言：只钉「传了 UNTRUSTED 就是 UNTRUSTED」会让「恒 UNTRUSTED」活下来，而那
    会让每一条宿主消息都把收件人的 `AllowAll` 永久废掉。
    """
    broker = InMemoryMessageBroker()
    runtime = Runtime(broker=broker)
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking(blocker), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)

    await runtime.send(agent_id, 'from the host')
    await runtime.send(agent_id, 'from a model that read the web', trust=TrustLevel.UNTRUSTED)

    delivered = await broker.consume(agent_id)
    assert [message.trust for message in delivered] == [
        TrustLevel.TRUSTED,
        TrustLevel.UNTRUSTED,
    ]
    await runtime.shutdown()


async def test_the_trust_a_send_carries_lands_on_the_block_the_target_reads():
    """`Message.trust` 到不了轨迹就等于没有：溯源看的是块，不是消息。"""
    broker = InMemoryMessageBroker()
    runtime = Runtime(broker=broker)
    agent_id = await runtime.spawn(
        _factory(ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])),
        query='go',
    )
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    await runtime.send(agent_id, '网页说要提权删除', trust=TrustLevel.UNTRUSTED)
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert [(block.content, block.trust) for block in ctx.current_traj if block.role == 'user'] == [
        ('go', TrustLevel.TRUSTED),
        ('网页说要提权删除', TrustLevel.UNTRUSTED),
    ]
    await runtime.shutdown()


async def test_despawning_yourself_from_inside_a_concurrent_segment_is_still_refused():
    """守卫判的是「这次操作会不会等自己停下来」，而**可并发的工具跑在 `gather` 建的子任务里**。

    按 `asyncio.current_task()` 比对在那里静默答否，于是 `despawn` 去取消并等待一个正阻塞在本
    子任务上的驱动任务——永久挂死。段内基数 2 是触发它的最小形状，段内基数 1 由串行那条路覆盖。
    """
    runtime = Runtime()
    holder: dict[str, str] = {}
    outcomes: list[str] = []

    class _Peer(Tool):
        __tool_command_args__ = ()

        def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
            return True

        async def execute(self) -> ToolResult:
            """什么都不做，只为把这一段撑到两个元素。"""
            return ToolResult(output='peer')

    class _SelfDespawnConcurrently(Tool):
        __tool_command_args__ = ()

        def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
            return True

        async def execute(self) -> ToolResult:
            """试着在一个并发段里销毁自己。"""
            try:
                await runtime.despawn(holder['agent_id'])
            except ContractViolation as error:
                outcomes.append(error.next_action)
                return ToolResult(output='refused')
            return ToolResult(output='not refused')

    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(id='c1', name='Peer', arguments='{}'),
                    ToolCall(id='c2', name='SelfDespawnConcurrently', arguments='{}'),
                ]
            ),
            ScriptedTurn(content='still alive'),
        ]
    )
    agent_id = await runtime.spawn(
        _factory(
            llm, tools={'Peer': _Peer(), 'SelfDespawnConcurrently': _SelfDespawnConcurrently()}
        ),
        query='go',
    )
    holder['agent_id'] = agent_id
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert [block.content for block in ctx.current_traj if block.role == 'tool'] == [
        'peer',
        'refused',
    ]
    assert [action for action in outcomes if 'abort' in action] == outcomes
    assert await runtime.find(agent_id) is not None
    await runtime.shutdown()


async def test_an_agent_spawned_from_inside_a_tool_does_not_inherit_its_parents_identity():
    """`spawn` 从驱动任务内部建子 Agent 的驱动任务，而 `asyncio.Task` 建立时复制 contextvars。

    `_drive` 不**无条件**覆写的话，子 Agent 的守卫指着它的父：它 despawn 自己会被放行，而那次
    拆除要取消并等待它自己正跑在里面的那个驱动任务——于是这次工具调用既没被拒、也没有结果。
    """
    runtime = Runtime()
    holder: dict[str, str] = {}
    outcomes: list[str] = []

    class _SelfDespawn(Tool):
        __tool_command_args__ = ()

        def __init__(self) -> None:
            self.agent_id = ''

        async def execute(self) -> ToolResult:
            """试着销毁自己。"""
            try:
                await runtime.despawn(self.agent_id)
            except ContractViolation:
                outcomes.append('refused')
                return ToolResult(output='refused')
            outcomes.append('not refused')
            return ToolResult(output='not refused')

    def child_factory(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        tool = _SelfDespawn()
        agent = BaseAgent(
            AgentOptions(
                name='Child',
                llm=ScriptedLLM(
                    [
                        ScriptedTurn(
                            tool_calls=[ToolCall(id='c1', name='SelfDespawn', arguments='{}')]
                        ),
                        ScriptedTurn(content='child done'),
                    ]
                ),
                tools={'SelfDespawn': tool},
                system_prompt=_SYSTEM_PROMPT,
            ),
            hooks=hooks,
            events=events,
        )
        tool.agent_id = agent.agent_id
        return agent

    class _SpawnChild(Tool):
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """从这一轮里面派一个子 Agent 出去。"""
            holder['child'] = await runtime.spawn(child_factory, query='go')
            return ToolResult(output='spawned')

    parent_llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='p1', name='SpawnChild', arguments='{}')]),
            ScriptedTurn(content='parent done'),
        ]
    )
    parent = await runtime.spawn(
        _factory(parent_llm, tools={'SpawnChild': _SpawnChild()}), query='go'
    )
    await asyncio.wait_for(runtime.wait(parent), _TIMEOUT)
    await asyncio.wait_for(runtime.wait(holder['child']), _TIMEOUT)

    assert outcomes == ['refused']
    assert await runtime.find(holder['child']) is not None
    await runtime.shutdown()


async def test_a_reaper_does_not_inherit_the_driving_agent_of_whoever_called_kill():
    """收尾刻意跑在事件循环之外，而它是 `kill` 在**调用方的上下文**里建的任务。

    继承来的「我是谁的事件循环」在那里是一句假话。一个 Agent 杀掉自己的祖先时，祖先的
    `ExitMode.EXIT` 级联要拆到这个杀手身上，而带着那份继承值的守卫会把它误判成「等自己」——
    级联在动手之前就抛出，整棵树一个条目都没拆掉，只剩收尾任务里的一条日志。
    """
    runtime = Runtime()
    holder: dict[str, str] = {}
    killed = asyncio.Event()

    class _KillMyAncestor(Tool):
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """终止自己的祖先。"""
            await runtime.kill(holder['ancestor'])
            killed.set()
            return ToolResult(output='killed')

    ancestor = await runtime.spawn(
        _factory(ScriptedLLM([ScriptedTurn(content='ancestor')])), exit_mode=ExitMode.EXIT
    )
    holder['ancestor'] = ancestor
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='KillMyAncestor', arguments='{}')]),
            ScriptedTurn(content='done'),
        ]
    )
    killer = await runtime.spawn(
        _factory(llm, tools={'KillMyAncestor': _KillMyAncestor()}),
        query='go',
        parent_id=ancestor,
    )
    await asyncio.wait_for(killed.wait(), _TIMEOUT)
    await runtime.shutdown()

    assert await runtime.find(ancestor) is None
    assert await runtime.find(killer) is None


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
    # 条目还在，所以「读它的 status 与 owner」这个下一步是够得着的——条目已被注销那一支不是。
    assert 'find()' in caplog.text
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
    with pytest.raises(ContractViolation) as aborted:
        await runtime.abort('nope')

    refusals = (paused, resumed, aborted)
    assert 'pause' in paused.value.expected
    assert 'resume' in resumed.value.expected
    assert 'abort' in aborted.value.expected
    assert all('nope' in refusal.value.expected for refusal in refusals)
    assert len({refusal.value.next_action for refusal in refusals}) == 1
    await runtime.shutdown()


async def test_pausing_an_agent_whose_drive_never_took_over_is_refused_and_leaves_it_revivable():
    """接管失败的条目归别人所有：本进程没有它的事件循环可停，写下的状态却谁都改不回来。

    `ensure_active` 只肯从 idle / zombie 重开，所以一个替它写下的 PAUSED 会把它对**任何**进程
    锁死；而 `send` 照答 `ok=True`——一张通往没有消费者的邮箱的收据，正是收据本要分辨的那件事。
    """
    registry = _RecordingRegistry(deny=(AgentStatus.IDLE, AgentStatus.RUNNING))
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go')
    with pytest.raises(ContractViolation):
        await runtime.wait(agent_id)
    peer = Runtime(registry=registry, broker=broker)

    with pytest.raises(ContractViolation) as stranded:
        await runtime.pause(agent_id)
    with pytest.raises(ContractViolation) as unknown:
        await runtime.pause('nope')
    with pytest.raises(ContractViolation) as not_mine:
        await peer.pause(agent_id)

    # 三种「控制不了」的下一步动作互不相同：重新 spawn / 去持有它的进程上操作 / 它压根没被接管。
    assert len({exc.value.received for exc in (stranded, unknown, not_mine)}) == 3
    assert agent_id in stranded.value.expected
    assert runtime._slots[agent_id].control.paused is False
    assert await _status(runtime, agent_id) is AgentStatus.IDLE
    # 状态没被动过，它就仍然叫得醒；写下 PAUSED 的话这一句答 False。
    assert await runtime.ensure_active(agent_id) is True
    await peer.shutdown()
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


async def test_a_recursive_pause_walks_through_a_descendant_another_process_holds(
    caplog: pytest.LogCaptureFixture,
):
    """树在共享注册表里，本进程持有哪些实例与树的形状无关。

    在第一个外人那里停住、或者干脆不往下走，会漏掉它下面属于我们的整片子树——而遍历是 pause /
    abort / despawn 三条路的公共前置。外人排在三个子代的**中间**：排在末位时「走过它」与「走到
    它为止」是同一个答案。
    """
    caplog.set_level(logging.INFO)
    registry = InMemoryAgentRegistry()
    broker = _SpyBroker(registry)
    owner = Runtime(registry=registry, broker=broker)
    peer = Runtime(registry=registry, broker=broker)
    root = await _resident(owner, broker)
    bravo = await _resident(owner, broker, parent_id=root)
    outsider = await _resident(peer, broker, parent_id=root)
    charlie = await _resident(owner, broker, parent_id=root)
    # 外人下面挂着的仍然是本进程的 Agent：只有走过外人才够得着它。
    delta = await _resident(owner, broker, parent_id=outsider)

    await owner.pause(root, recursive=True)

    ours = (root, bravo, charlie, delta)
    assert [await _status(owner, agent_id) for agent_id in ours] == [AgentStatus.PAUSED] * 4
    assert [owner._slots[agent_id].control.paused for agent_id in ours] == [True] * 4

    assert await _status(owner, outsider) is AgentStatus.IDLE
    assert peer._slots[outsider].control.paused is False
    assert (await peer.send(outsider, 'go ahead')).ok is True
    await asyncio.wait_for(broker.reach(outsider, 2), _TIMEOUT)
    outside_agent = peer._table.get(outsider)
    assert outside_agent is not None
    assert [block.content for block in outside_agent.context.current_traj] == ['go ahead', 'turn-0']

    assert [record.levelname for record in caplog.records] == ['INFO']
    skipped = caplog.records[0].getMessage()
    assert outsider in skipped
    assert delta not in skipped and charlie not in skipped
    await peer.shutdown()
    await owner.shutdown()


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

    **不调 `caplog.set_level`**：这次转换失败的唯一痕迹就是这条记录，而 root logger 默认停在
    WARNING，低于它的级别连记录都不会被创建。
    """
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
    assert [record.levelname for record in caplog.records] == ['WARNING']
    assert caplog.records[0].args == (agent_id, AgentStatus.IDLE.value)
    _assert_three_part(caplog.records[0].getMessage())
    await runtime.shutdown()


async def test_a_refused_resume_inside_a_run_reports_running_as_the_state_it_could_not_write(
    caplog: pytest.LogCaptureFixture,
):
    """`back_to` 只有 RUNNING / IDLE 两个取值，而只走过其中一个的话，另一支的措辞从未被检查。

    这里覆盖的是整个取值域，不是取样：把这一项写死成任一个常量，两条用例里必有一条变红。
    """
    registry = _RecordingRegistry(deny=(AgentStatus.PAUSED, AgentStatus.RUNNING))
    runtime = Runtime(registry=registry)
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking(blocker), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    control = runtime._slots[agent_id].control
    await runtime.pause(agent_id)

    await runtime.resume(agent_id)

    assert control.in_run is True
    assert [record.levelname for record in caplog.records] == ['WARNING']
    assert caplog.records[0].args == (agent_id, AgentStatus.RUNNING.value)
    _assert_three_part(caplog.records[0].getMessage())
    # 门照常抬起：注册表记不下不该把它永久停在门上。
    assert control.paused is False
    blocker.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert [block.content for block in ctx.current_traj if block.role == 'assistant'] == [
        None,
        'done',
    ]
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


async def test_abort_ends_the_current_cycle_with_a_consistent_trajectory():
    """协作式中止兑现在轮与轮之间，因此轨迹配对完整、可以直接 resume。

    这正是 ABORTED 与硬取消的分界：硬取消落在轮中间，可能留下不配对的调用，所以它不设
    stop_reason；合并两者会毁掉 resume 正需要的那个区分。
    """
    registry = _RecordingRegistry()
    runtime = Runtime(registry=registry)
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking(blocker), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)

    await runtime.abort(agent_id)

    # 立刻返回：这一轮还卡在工具里，而状态已经说得出「它正在收尾」。
    assert await _status(runtime, agent_id) is AgentStatus.ABORTING
    blocker.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert ctx.stop_reason is StopReason.ABORTED
    assert [(block.role, block.content) for block in ctx.current_traj] == [
        ('user', 'go'),
        ('assistant', None),
        ('tool', 'released'),
    ]
    assert registry.applied_of(agent_id) == [
        (AgentStatus.IDLE, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.ABORTING),
        (AgentStatus.ABORTING, AgentStatus.ZOMBIE),
    ]
    await runtime.shutdown()


async def test_an_aborted_cycle_puts_its_stop_reason_on_the_bus_as_plain_data():
    """`stop_reason` 走的是事件总线这道序列化出口，而 payload 必须是纯数据。

    在 agent 层用一个固定答案的假门钉住过它，但真实链路是 `Runtime.abort` → 控制块 → 真实
    `BrokerTurnGate` → turn 循环：一个性质在某一层被钉住，不等于在真正依赖它的那道接缝上被钉住。
    """
    runtime = Runtime()
    subscription = runtime.events.subscribe()
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking(blocker), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)

    await runtime.abort(agent_id)

    blocker.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert ctx.stop_reason is StopReason.ABORTED
    await runtime.shutdown()

    ends = [event async for event in subscription if event.kind == 'agent_end']
    assert [(event.agent_id, event.payload) for event in ends] == [
        (agent_id, {'stop_reason': 'aborted'})
    ]
    assert [type(value) for value in ends[0].payload.values()] == [str]


async def test_abort_pierces_a_pause_taken_inside_a_turn_without_lifting_it():
    """先挂起再中止是真实顺序：中止若要等解除挂起才被看见，调用方永远拿不到反馈。

    **穿透不等于解除**：兑现之后这个 Agent 回到挂起态等 `resume`，注册表也得这么说——写 idle
    等于宣布「它能处理消息」，而门还落着，它一条都不会处理。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking(blocker), query='go', exit_mode=ExitMode.IDLE)
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    control = runtime._slots[agent_id].control
    await runtime.pause(agent_id)

    await runtime.abort(agent_id)

    blocker.release.set()
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)
    agent = runtime._table.get(agent_id)
    assert agent is not None

    assert agent.context.stop_reason is StopReason.ABORTED
    assert control.paused is True
    assert await _status(runtime, agent_id) is AgentStatus.PAUSED
    assert control.abort_requested is False

    # 门确实还落着：这条消息一轮都跑不动，直到 resume。
    assert (await runtime.send(agent_id, 'after the abort')).ok is True
    assert await control.wait_until_parked(_TIMEOUT) is True
    assert [(block.role, block.content) for block in agent.context.current_traj] == [
        ('user', 'go'),
        ('assistant', None),
        ('tool', 'released'),
    ]

    await runtime.resume(agent_id)
    await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
    assert [(block.role, block.content) for block in agent.context.current_traj] == [
        ('user', 'go'),
        ('assistant', None),
        ('tool', 'released'),
        ('user', 'after the abort'),
        ('assistant', 'done'),
    ]
    await runtime.shutdown()


async def test_abort_on_an_agent_parked_at_the_pause_gate_is_taken_at_once_and_it_stays_paused():
    """挂起态的执行体停在门上，`request_abort` 的唤醒边沿当场送到——位不会 armed 着等下一条消息。

    与它同名 PAUSED 的另一支（挂起着而邮箱为空）停在邮箱上，收不到这条边沿：注册表把两处叫成
    同一个取值，只有 `parked` 与 `parked_on_mailbox` 分得出。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await _resident(runtime, broker)
    control = runtime._slots[agent_id].control
    await runtime.pause(agent_id)
    assert (await runtime.send(agent_id, 'wake up')).ok is True
    assert await control.wait_until_parked(_TIMEOUT) is True

    await runtime.abort(agent_id)

    await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
    agent = runtime._table.get(agent_id)
    assert agent is not None

    assert control.abort_requested is False
    assert control.paused is True
    assert await _status(runtime, agent_id) is AgentStatus.PAUSED
    # 手上本来就没有活，所以没有周期被收场；那条消息也一直留在邮箱里。
    assert agent.context.current_traj == []
    assert broker.consumes(agent_id) == 0

    await runtime.resume(agent_id)
    await asyncio.wait_for(broker.reach(agent_id, 3), _TIMEOUT)
    assert [block.content for block in agent.context.current_traj] == ['wake up', 'turn-0']
    await runtime.shutdown()


async def test_aborting_an_agent_parked_on_its_mailbox_succeeds_and_writes_nothing():
    """停在邮箱上就没有「手上这一件事」可停：轨迹已经配对完整，后置条件已经成立。

    写下 ABORTING 才是真的坏——邮箱上没有等唤醒边沿的执行体，那个状态要等下一条消息才被纠正，
    在此期间注册表对**所有**进程谎报「它正在收尾」。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await _resident(runtime, broker)
    before = len(registry.transitions)

    await runtime.abort(agent_id)

    # 一次 CAS 都没试过：拒绝与放行之外的第三种答案是「什么都不写」。
    assert registry.transitions[before:] == []
    assert runtime._slots[agent_id].control.abort_requested is False
    assert await _status(runtime, agent_id) is AgentStatus.IDLE

    # 之后到达的消息开的是**下一件事**，不在本次调用的范围里，照常换来一个完整的周期。
    assert (await runtime.send(agent_id, 'still working')).ok is True
    await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert [block.content for block in agent.context.current_traj] == ['still working', 'turn-0']
    assert agent.context.stop_reason is StopReason.COMPLETED
    await runtime.shutdown()


async def test_aborting_a_paused_agent_with_an_empty_mailbox_leaves_it_paused_and_resumable():
    """挂起着而邮箱空的执行体停在**邮箱**上，不在挂起门上——唤醒边沿在那里没有等待者。

    这一支与 IDLE 在事件循环里是同一个位置，只有注册表把它们叫成两个名字；按状态准入的判据分不出
    它与停在挂起门上的 PAUSED。写下 ABORTING 的话，`resume` 的 CAS（expected 只有 paused）落空，
    只剩一条入站消息能改回来——一个没有任何操作纠正得了的假状态。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await _resident(runtime, broker)
    control = runtime._slots[agent_id].control
    await runtime.pause(agent_id)
    assert control.parked == 0
    before = len(registry.transitions)

    await runtime.abort(agent_id)

    assert registry.transitions[before:] == []
    assert control.abort_requested is False
    assert control.paused is True
    assert await _status(runtime, agent_id) is AgentStatus.PAUSED

    # resume 修得回来，这正是写下 ABORTING 之后失效的那一步。
    await runtime.resume(agent_id)
    assert await _status(runtime, agent_id) is AgentStatus.IDLE
    assert (await runtime.send(agent_id, 'carry on')).ok is True
    await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert [block.content for block in agent.context.current_traj] == ['carry on', 'turn-0']
    await runtime.shutdown()


async def test_an_abort_landing_while_the_loop_is_on_its_way_to_the_mailbox_writes_nothing():
    """「它停着」这个事实要在循环**开始**往邮箱走的时候就成立，不是在它到达之后。

    置位晚一步，落在这段窗口里的 abort 就会写下一个 ABORTING、再由循环停到邮箱上——正是那个没有
    等待者、要等下一条消息才被纠正的假状态。窗口本身靠停住注册表那次 CAS 造出来。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await _resident(runtime, broker)
    registry.hold_before = AgentStatus.IDLE
    registry.switches.close()
    assert (await runtime.send(agent_id, 'one')).ok is True
    # 这个周期已经跑完，循环正卡在「转回停泊态」那一次 CAS 上，还没停到邮箱上。
    await asyncio.wait_for(registry.switches.reach(1), _TIMEOUT)
    assert runtime._slots[agent_id].parked_on_mailbox == 1
    before = len(registry.transitions)

    await runtime.abort(agent_id)

    assert registry.transitions[before:] == []
    assert runtime._slots[agent_id].control.abort_requested is False

    registry.switches.open()
    await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
    assert await _status(runtime, agent_id) is AgentStatus.IDLE
    await runtime.shutdown()


async def test_aborting_a_zombie_succeeds_without_writing_the_state_that_would_bury_it():
    """ZOMBIE 的那件事早就停了，后置条件已经成立。

    成功与拒绝的差别不在返回值上，在于它**没有写下 ABORTING**：`ensure_active` 明文拒绝复活一个
    aborting 的 Agent，而 ZOMBIE 的注册表状态正是复活它的凭据。
    """
    registry = _RecordingRegistry()
    runtime = Runtime(registry=registry)
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    agent_id = await runtime.spawn(_factory(llm), query='go')
    await runtime.wait(agent_id)
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    before = len(registry.transitions)

    await runtime.abort(agent_id)

    assert registry.transitions[before:] == []
    assert runtime._slots[agent_id].control.abort_requested is False
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE

    assert (await runtime.send(agent_id, 'keep going')).ok is True
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert [block.content for block in ctx.current_traj if block.role == 'assistant'] == [
        'first',
        'second',
    ]
    await runtime.shutdown()


async def test_a_send_followed_at_once_by_an_abort_is_not_a_refusal():
    """界面上「发出去 → 立刻按停止」那一格：此刻注册表多半还说 idle，驱动任务还没被调度到。

    这条 abort 本来就停不住那条消息——被中止的周期不 consume 邮箱，它照样换来一个完整的周期，
    放行与拒绝的终局逐字相同。既然如此就不该拒绝，更不该把调用方推去 kill 一个本可协作停下的
    Agent：kill 拆的是整个事件循环。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    agent_id = await _resident(runtime, broker)

    assert (await runtime.send(agent_id, 'do the thing')).ok is True
    await runtime.abort(agent_id)

    await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert [block.content for block in agent.context.current_traj] == ['do the thing', 'turn-0']
    assert agent.context.stop_reason is StopReason.COMPLETED
    assert runtime._slots[agent_id].control.abort_requested is False
    await runtime.shutdown()


async def test_an_abort_that_loses_the_race_reads_as_a_race_and_not_as_an_idle_agent():
    """输掉的 CAS 报成「它空闲着」的话，调用方会照着去动一个正在干活的 Agent。

    而它的活本可以协作式地停下来；`kill` 拆的是整个事件循环，远大于「这件事别做了」。
    """
    registry = _RecordingRegistry(deny=(AgentStatus.RUNNING, AgentStatus.ABORTING))
    runtime = Runtime(registry=registry)
    blockers: list[_Block] = []
    contended = await _running(runtime, blockers)

    with pytest.raises(ContractViolation) as lost_the_race:
        await runtime.abort(contended)

    assert contended in lost_the_race.value.expected
    assert AgentStatus.ABORTING.value in lost_the_race.value.received
    assert AgentStatus.IDLE.value not in lost_the_race.value.received
    assert 'kill' not in lost_the_race.value.next_action
    assert 'find()' in lost_the_race.value.next_action
    assert runtime._slots[contended].control.abort_requested is False
    assert await _status(runtime, contended) is AgentStatus.RUNNING
    for blocker in blockers:
        blocker.release.set()
    await runtime.shutdown()


async def test_abort_is_consumed_once_so_the_next_message_still_gets_a_whole_cycle():
    """位若不清，一次 abort 会让此后每一个周期都立刻结束——而它的语义是「停下手上这一件事」。

    中止之后跑**两个**周期而不是一个：只跑一个时，「清一次」与「清了但下次又置上」是同一个答案。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    blocker = _Block()
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Block', arguments='{}')]),
            ScriptedTurn(content='second cycle'),
            ScriptedTurn(content='third cycle'),
        ]
    )
    agent_id = await runtime.spawn(
        _factory(llm, tools={'Block': blocker}), query='go', exit_mode=ExitMode.IDLE
    )
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)

    await runtime.abort(agent_id)

    blocker.release.set()
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)
    for parks, content in enumerate(('bravo', 'alpha'), start=2):
        assert (await runtime.send(agent_id, content)).ok is True
        await asyncio.wait_for(broker.reach(agent_id, parks), _TIMEOUT)

    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert [(block.role, block.content) for block in agent.context.current_traj] == [
        ('user', 'go'),
        ('assistant', None),
        ('tool', 'released'),
        ('user', 'bravo'),
        ('assistant', 'second cycle'),
        ('user', 'alpha'),
        ('assistant', 'third cycle'),
    ]
    assert agent.context.stop_reason is StopReason.COMPLETED
    # 被中止的那一轮一次模型调用都没发：三次调用对应三轮真的跑过的轮。
    assert len(llm.calls) == 3
    await runtime.shutdown()


async def test_an_aborted_turn_spends_no_turn_budget():
    """门在计数之前（spec §5.1 前三步）：被中止的那一轮不消耗预算。

    预算给到 2：第一轮花掉一轮，被中止的那一轮若也记了账，下一个周期一开就撞 MAX_TURNS，而它
    本该还有整整一轮可用。
    """
    runtime = Runtime()
    blocker = _Block()
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Block', arguments='{}')]),
            ScriptedTurn(content='the turn the abort did not spend'),
        ]
    )

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=llm,
                tools={'Block': blocker},
                system_prompt=_SYSTEM_PROMPT,
                max_turns=2,
            ),
            hooks=hooks,
            events=events,
        )

    agent_id = await runtime.spawn(build, query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    await runtime.abort(agent_id)
    blocker.release.set()
    aborted = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert aborted.stop_reason is StopReason.ABORTED

    assert (await runtime.send(agent_id, 'try again')).ok is True
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert ctx.stop_reason is StopReason.COMPLETED
    assert [block.content for block in ctx.current_traj if block.role == 'assistant'] == [
        None,
        'the turn the abort did not spend',
    ]
    await runtime.shutdown()


async def test_an_abort_leaves_the_deferred_stash_for_the_next_cycle():
    """暂存的消息排的是**下一件事**的队，而 abort 停的是手上这一件——它们不在被停的范围里。

    三条而非一条，顺序既非升序亦非降序，反转后同样两者都不是。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    gate = _Gate(2)
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Gate', arguments='{}')]),
            ScriptedTurn(tool_calls=[ToolCall(id='c2', name='Gate', arguments='{}')]),
            ScriptedTurn(content='the cycle that reads the stash'),
        ]
    )
    agent_id = await runtime.spawn(
        _factory(llm, tools={'Gate': gate}), query='go', exit_mode=ExitMode.IDLE
    )
    await asyncio.wait_for(gate.entered[0].wait(), _TIMEOUT)
    for content in ('bravo', 'alpha', 'charlie'):
        assert (await runtime.send(agent_id, content, trigger=Trigger.DEFERRED)).ok is True

    # 放行第一个停点：第二轮的门把三条挪进暂存，随后停在第二个停点上，这个周期还剩第三轮。
    gate.release[0].set()
    await asyncio.wait_for(gate.entered[1].wait(), _TIMEOUT)
    control = runtime._slots[agent_id].control
    assert control.deferred_count == 3

    await runtime.abort(agent_id)

    gate.release[1].set()
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)
    agent = runtime._table.get(agent_id)
    assert agent is not None

    assert [(block.role, block.content) for block in agent.context.current_traj] == [
        ('user', 'go'),
        ('assistant', None),
        ('tool', 'released 0'),
        ('assistant', None),
        ('tool', 'released 1'),
        ('user', 'bravo'),
        ('user', 'alpha'),
        ('user', 'charlie'),
        ('assistant', 'the cycle that reads the stash'),
    ]
    assert control.deferred_count == 0
    assert agent.context.stop_reason is StopReason.COMPLETED
    await runtime.shutdown()


async def test_abort_writes_aborting_from_every_live_status_its_own_loop_can_be_in():
    """不在 CAS `allowed` 里的活状态会静默转不过去：位置上了，注册表却停在旧状态上。

    四个而非一个，顺序既非 `_LIVE_STATUSES` 的声明序亦非其反转，也不是取值的升序或降序。每一个都
    真的停在一次工具调用里——注册表的取值是被改出来的，而准入判据读的是驱动任务此刻在哪。
    """
    registry = _RecordingRegistry()
    runtime = Runtime(registry=registry)
    blockers: list[_Block] = []
    for status in (
        AgentStatus.WAITING,
        AgentStatus.IDLE,
        AgentStatus.PAUSED,
        AgentStatus.RUNNING,
    ):
        agent_id = await _running(runtime, blockers)
        assert await registry.compare_and_set_status(
            agent_id, expected=AgentStatus.RUNNING, new=status
        )

        await runtime.abort(agent_id)

        assert await _status(runtime, agent_id) is AgentStatus.ABORTING
        assert runtime._slots[agent_id].control.abort_requested is True
        assert registry.applied_of(agent_id)[-1] == (status, AgentStatus.ABORTING)
    for blocker in blockers:
        blocker.release.set()
    await runtime.shutdown()


async def test_a_second_abort_on_an_agent_already_winding_down_is_a_no_op():
    """后置条件已经成立的操作成功返回：位已置、状态已是 aborting。"""
    registry = _RecordingRegistry()
    runtime = Runtime(registry=registry)
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking(blocker), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)

    await runtime.abort(agent_id)
    await runtime.abort(agent_id)

    blocker.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert ctx.stop_reason is StopReason.ABORTED
    assert registry.applied_of(agent_id) == [
        (AgentStatus.IDLE, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.ABORTING),
        (AgentStatus.ABORTING, AgentStatus.ABORTING),
        (AgentStatus.ABORTING, AgentStatus.ZOMBIE),
    ]
    await runtime.shutdown()


async def test_an_abort_that_lands_after_the_last_turn_is_spent_before_the_loop_parks():
    """位在停到邮箱上之前就作废：那一刻手上已经没有轮了，它没有东西可停。

    留着它的话下一条消息会先烧掉一次循环迭代——终局一模一样（那条消息照样换来一个完整的周期），
    只有「停着的时候位是干净的」这一句分得出这一位到底是被谁取走的。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    ending = _Hold()
    ending.close()

    async def hold(**kwargs: Any) -> HookResult:
        await ending.passing('agent_end')
        return HookResult()

    runtime.hooks.register(Junction.AGENT_END, hold)
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    agent_id = await runtime.spawn(_factory(llm), query='go', exit_mode=ExitMode.IDLE)
    await asyncio.wait_for(ending.reach(1), _TIMEOUT)
    # 这一轮已经跑完而循环还没回到邮箱上：既不在门上，也不在邮箱上。
    slot = runtime._slots[agent_id]
    assert slot.parked_on_mailbox == 0

    await runtime.abort(agent_id)

    assert slot.control.abort_requested is True
    assert await _status(runtime, agent_id) is AgentStatus.ABORTING

    ending.open()
    await asyncio.wait_for(broker.reach(agent_id, 1), _TIMEOUT)
    assert slot.control.abort_requested is False
    assert await _status(runtime, agent_id) is AgentStatus.IDLE

    assert (await runtime.send(agent_id, 'next')).ok is True
    await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert [block.content for block in agent.context.current_traj] == [
        'go',
        'first',
        'next',
        'second',
    ]
    await runtime.shutdown()


async def test_an_aborted_cycle_comes_back_out_of_aborting_on_both_exits():
    """ABORTING 是 `ensure_active` 明文拒绝复活的状态：进得去出不来，这个 Agent 就对**所有**进程
    失联，而 `send` 照答 ok=True。

    出口不在 abort 手上——它由 `_LIVE_STATUSES` 经 `_finish`（→ zombie）与 `_oscillate`
    （→ idle / paused）提供，而 abort 是第一个真的会写下 ABORTING 的操作。两边一旦不认同一张表，
    这一步就静默变成单向门；三条出口各由不同的 exit_mode / 门状态走到，只走一条时其余的从未被
    检查过。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    resident_block, reaped_block = _Block(), _Block()
    resident = await runtime.spawn(_blocking(resident_block), query='go', exit_mode=ExitMode.IDLE)
    reaped = await runtime.spawn(_blocking(reaped_block), query='go', exit_mode=ExitMode.ZOMBIE)
    for blocker in (resident_block, reaped_block):
        await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)

    for agent_id in (resident, reaped):
        await runtime.abort(agent_id)
    for blocker in (resident_block, reaped_block):
        blocker.release.set()
    await asyncio.wait_for(broker.reach(resident, 1), _TIMEOUT)
    await asyncio.wait_for(runtime.wait(reaped), _TIMEOUT)

    assert [await _status(runtime, agent_id) for agent_id in (resident, reaped)] == [
        AgentStatus.IDLE,
        AgentStatus.ZOMBIE,
    ]

    # 出得来才叫得醒：卡在 aborting 上时 `ensure_active` 答 False，这条消息永远没人消费。
    assert (await runtime.send(resident, 'carry on')).ok is True
    await asyncio.wait_for(broker.reach(resident, 2), _TIMEOUT)
    assert (await runtime.send(reaped, 'carry on')).ok is True
    await asyncio.wait_for(runtime.wait(reaped), _TIMEOUT)

    for agent_id in (resident, reaped):
        agent = runtime._table.get(agent_id)
        assert agent is not None
        assert [(block.role, block.content) for block in agent.context.current_traj] == [
            ('user', 'go'),
            ('assistant', None),
            ('tool', 'released'),
            ('user', 'carry on'),
            ('assistant', 'done'),
        ]
    await runtime.shutdown()


async def _running_tree(runtime: Runtime, blockers: list[_Block]) -> tuple[tuple[str, ...], str]:
    """一棵五个子代跨两代的树，每一个都真的停在一次工具调用里，另有一个不在树上的 Agent。

    子代必须真的可中止，否则「遍历漏了它」与「遍历到了但它不可中止」是同一个答案，而后者只记
    日志——整条递归路径于是可以被一个什么都不做的实现满足。
    """
    root = await _running(runtime, blockers)
    bravo, alpha, charlie = [await _running(runtime, blockers, parent_id=root) for _ in range(3)]
    delta = await _running(runtime, blockers, parent_id=bravo)
    echo = await _running(runtime, blockers, parent_id=alpha)
    outsider = await _running(runtime, blockers)
    return (root, bravo, alpha, charlie, delta, echo), outsider


async def test_recursive_abort_reaches_every_descendant_and_goes_parent_first():
    """父先于子：先掐掉还会派出新活的那一头。

    广度优先的答案（bravo / alpha / charlie / delta / echo）与深度优先、升序、降序及这四者的
    反转全都互不相同。
    """
    registry = _RecordingRegistry()
    runtime = Runtime(registry=registry)
    blockers: list[_Block] = []
    tree, outsider = await _running_tree(runtime, blockers)

    await runtime.abort(tree[0], recursive=True)

    assert [
        agent_id for agent_id, _, new in registry.applied if new is AgentStatus.ABORTING
    ] == list(tree)
    assert [runtime._slots[agent_id].control.abort_requested for agent_id in tree] == [True] * 6

    assert await _status(runtime, outsider) is AgentStatus.RUNNING
    assert runtime._slots[outsider].control.abort_requested is False
    for blocker in blockers:
        blocker.release.set()
    await runtime.shutdown()


async def test_recursive_abort_from_a_node_with_a_parent_never_climbs_to_its_ancestors():
    """`recursive` 说的是**子树**，不是整个家族。

    祖先必须自己也可中止，否则「爬上去了但被拒」与「没爬上去」是同一个答案。两个深度上各三个
    节点，注册顺序既非升序亦非降序，反转后同样两者都不是。
    """
    runtime = Runtime()
    blockers: list[_Block] = []
    root = await _running(runtime, blockers)
    bravo, alpha, charlie = [await _running(runtime, blockers, parent_id=root) for _ in range(3)]
    echo, foxtrot, delta = [await _running(runtime, blockers, parent_id=bravo) for _ in range(3)]

    await runtime.abort(bravo, recursive=True)

    subtree = (bravo, echo, foxtrot, delta)
    assert [await _status(runtime, agent_id) for agent_id in subtree] == [AgentStatus.ABORTING] * 4
    assert [runtime._slots[agent_id].control.abort_requested for agent_id in subtree] == [True] * 4

    above = (root, alpha, charlie)
    assert [await _status(runtime, agent_id) for agent_id in above] == [AgentStatus.RUNNING] * 3
    assert [runtime._slots[agent_id].control.abort_requested for agent_id in above] == [False] * 3
    for blocker in blockers:
        blocker.release.set()
    await runtime.shutdown()


async def test_a_recursive_abort_on_one_tree_leaves_an_independent_tree_alone():
    """本仓是多 Agent 框架，「操作没错但作用在错的主体上」是一等缺陷。

    两棵**独立**的树而不是一棵树的两支：同一棵树上的两支共享一个根，「只走这一支」与「走整棵树」
    可以被同一个实现同时满足。
    """
    runtime = Runtime()
    blockers: list[_Block] = []
    trees: list[tuple[str, ...]] = []
    for _ in range(2):
        root = await _running(runtime, blockers)
        bravo, alpha = [await _running(runtime, blockers, parent_id=root) for _ in range(2)]
        delta = await _running(runtime, blockers, parent_id=bravo)
        trees.append((root, bravo, alpha, delta))

    # 第二棵而不是第一棵：作用在「第一棵」上的实现在只有一棵树时与正确实现无从区分。
    await runtime.abort(trees[1][0], recursive=True)

    assert [await _status(runtime, agent_id) for agent_id in trees[1]] == [AgentStatus.ABORTING] * 4
    assert [runtime._slots[agent_id].control.abort_requested for agent_id in trees[1]] == [True] * 4
    assert [await _status(runtime, agent_id) for agent_id in trees[0]] == [AgentStatus.RUNNING] * 4
    assert [runtime._slots[agent_id].control.abort_requested for agent_id in trees[0]] == [
        False
    ] * 4
    for blocker in blockers:
        blocker.release.set()
    await runtime.shutdown()


async def test_a_descendant_with_nothing_to_stop_is_left_alone_without_a_record(
    caplog: pytest.LogCaptureFixture,
):
    """后置条件已经成立不是一次事件：这两个子代既没被写状态，也没留下记录，而遍历照常往下走。

    两个「手上没有活」的排在四个子代的**中间**：排在末位时「走过它们」与「走到它们为止」是同一个
    答案。它们是两种不同的「没有活」——一个已经收尾（ZOMBIE，连事件循环都没了），一个停在邮箱上。
    """
    caplog.set_level(logging.INFO)
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    blockers: list[_Block] = []
    root = await _running(runtime, blockers)
    bravo = await _running(runtime, blockers, parent_id=root)
    finished = await runtime.spawn(_factory(_done()), query='go', parent_id=root)
    await runtime.wait(finished)
    resting = await _resident(runtime, broker, parent_id=root)
    charlie = await _running(runtime, blockers, parent_id=root)

    await runtime.abort(root, recursive=True)

    assert [await _status(runtime, agent_id) for agent_id in (root, bravo, charlie)] == [
        AgentStatus.ABORTING
    ] * 3
    assert await _status(runtime, finished) is AgentStatus.ZOMBIE
    assert await _status(runtime, resting) is AgentStatus.IDLE
    untouched = (finished, resting)
    assert [runtime._slots[agent_id].control.abort_requested for agent_id in untouched] == [
        False,
        False,
    ]
    assert caplog.records == []
    for blocker in blockers:
        blocker.release.set()
    await runtime.shutdown()


async def test_a_recursive_abort_never_writes_aborting_on_a_descendant_it_never_took_over(
    caplog: pytest.LogCaptureFixture,
):
    """所有权凭据在**遍历找到的**主体上是第二次独立的应用，被点名的那一次替不了它。

    树在共享注册表里，本进程接管过哪些不影响树的形状；而替一个本进程按不住的条目写下 ABORTING
    比写下 PAUSED 更糟——`ensure_active` 对 aborting 有专门的拒绝分支，连真正的持有者也再叫不醒
    它。这个子代的驱动任务**还活着**（停在它的第一次 CAS 上），因此「它已经没有事件循环了」这条
    通道救不了它，只有所有权凭据本身能。三个子代跨两代，被跳过的那一个在广度优先序里排第四、
    后面还有两个，遍历必须走过它继续往下。
    """
    caplog.set_level(logging.INFO)
    registry = _RecordingRegistry()
    runtime = Runtime(registry=registry)
    blockers: list[_Block] = []
    root = await _running(runtime, blockers)
    bravo, alpha, charlie = [await _running(runtime, blockers, parent_id=root) for _ in range(3)]
    delta = await _running(runtime, blockers, parent_id=alpha)
    echo = await _running(runtime, blockers, parent_id=charlie)
    # 闸只拦「转去 RUNNING」，而这六个都已经停在各自的工具里，不会再要这个转换。
    registry.hold_before = AgentStatus.RUNNING
    registry.switches.close()
    stranded = await runtime.spawn(_factory(_done()), query='go', parent_id=bravo)
    await asyncio.wait_for(registry.switches.reach(1), _TIMEOUT)
    assert runtime._slots[stranded].owned is False

    await runtime.abort(root, recursive=True)

    walked = (root, bravo, alpha, charlie, delta, echo)
    assert [await _status(runtime, agent_id) for agent_id in walked] == [AgentStatus.ABORTING] * 6
    assert [runtime._slots[agent_id].control.abort_requested for agent_id in walked] == [True] * 6
    # 状态没被动过，它就仍然叫得醒；写下 aborting 的话这个条目对每一个进程都失联。
    assert await _status(runtime, stranded) is AgentStatus.IDLE
    assert runtime._slots[stranded].control.abort_requested is False

    assert [record.levelname for record in caplog.records] == ['INFO']
    skipped = caplog.records[0].getMessage()
    assert 'abort' in skipped and stranded in skipped
    assert all(agent_id not in skipped for agent_id in walked)

    registry.switches.open()
    for blocker in blockers:
        blocker.release.set()
    await runtime.shutdown()


async def test_an_abort_the_registry_refuses_arms_nothing_and_leaves_the_tree_untouched():
    """记不下 aborting 就不要真的置位——那会停掉一件谁都看不出被停了的活。"""
    registry = _RecordingRegistry(deny=(AgentStatus.RUNNING, AgentStatus.ABORTING))
    runtime = Runtime(registry=registry)
    blockers: list[_Block] = []
    agent_id = await _running(runtime, blockers)
    child = await _running(runtime, blockers, parent_id=agent_id)
    registry.deny_for = agent_id

    with pytest.raises(ContractViolation) as excinfo:
        await runtime.abort(agent_id, recursive=True)

    assert agent_id in excinfo.value.expected
    assert runtime._slots[agent_id].control.abort_requested is False
    # 父先于子：被点名的那一个抛出来时，这棵树还没被走过。
    assert await _status(runtime, child) is AgentStatus.RUNNING
    assert runtime._slots[child].control.abort_requested is False
    for blocker in blockers:
        blocker.release.set()
    await runtime.shutdown()


async def test_aborting_an_agent_whose_drive_never_took_over_is_refused_and_leaves_it_revivable():
    """接管失败的条目归别人所有：本进程没有它的事件循环可停，写下的 aborting 却谁都改不回来。

    `ensure_active` 明文拒绝复活一个 aborting 的 Agent（「它正在收尾」），所以替它写下 aborting
    比写下 paused 更糟——连真正的持有者也再叫不醒它。
    """
    registry = _RecordingRegistry(deny=(AgentStatus.IDLE, AgentStatus.RUNNING))
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go')
    with pytest.raises(ContractViolation):
        await runtime.wait(agent_id)
    peer = Runtime(registry=registry, broker=broker)

    with pytest.raises(ContractViolation) as stranded:
        await runtime.abort(agent_id)
    with pytest.raises(ContractViolation) as not_mine:
        await peer.abort(agent_id)

    assert stranded.value.received != not_mine.value.received
    assert runtime._slots[agent_id].control.abort_requested is False
    assert await _status(runtime, agent_id) is AgentStatus.IDLE
    # 状态没被动过，它就仍然叫得醒；写下 aborting 的话这一句答 False。
    assert await runtime.ensure_active(agent_id) is True
    await peer.shutdown()
    await runtime.shutdown()


async def test_kill_returns_before_its_teardown_has_written_anything():
    """收尾要 await，而在一个正在被取消的任务里 await 只会收一半的尾；所以它在别的任务里跑。

    等自己的收尾就把「立刻返回」变成「等收尾」，spec §4.1 给 ABORTING 的理由随之作废；这里的上界
    让那个变异变成一条 1 秒就红的断言，而不是一次挂死。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    blockers: list[_Block] = []
    agent_id = await _running(runtime, blockers)
    registry.hold_before = AgentStatus.ZOMBIE
    registry.switches.close()

    await asyncio.wait_for(runtime.kill(agent_id), _TIMEOUT)

    await asyncio.wait_for(registry.switches.reach(1), _TIMEOUT)
    assert await _status(runtime, agent_id) is AgentStatus.ABORTING
    registry.switches.open()
    await asyncio.wait_for(_reaped(runtime), _TIMEOUT)

    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    await runtime.shutdown()


async def test_a_killed_agent_comes_back_on_one_message_with_its_batch_paired_up():
    """取消落在轮的中间，留下一个声明了调用却没有结果的 assistant block。

    那不是缺陷：`BaseAgent.run` 在入口按「中断、副作用未知」把它补成配对完整的轨迹，于是被杀掉的
    Agent 仍然能被一条消息复活并继续干。**中止位刻意不置**——位是会留下来的状态，一次复活会在第一
    轮就把它消费掉，让复活出来的 Agent 立刻结束，一次 kill 于是变成永久的诅咒。
    """
    runtime = Runtime()
    blocker = _Block()
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Block', arguments='{}')]),
            ScriptedTurn(content='resumed after the kill'),
        ]
    )
    agent_id = await runtime.spawn(_factory(llm, tools={'Block': blocker}), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)

    await runtime.kill(agent_id)
    await asyncio.wait_for(_reaped(runtime), _TIMEOUT)

    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert agent.context.stop_reason is None
    assert [block.role for block in agent.context.current_traj] == ['user', 'assistant']
    assert runtime._slots[agent_id].control.abort_requested is False

    assert (await runtime.send(agent_id, 'carry on')).ok is True
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert [(block.role, block.tool_call_id) for block in ctx.current_traj] == [
        ('user', None),
        ('assistant', None),
        ('tool', 'c1'),
        ('user', None),
        ('assistant', None),
    ]
    assert ctx.current_traj[2].extra == {'interrupted': True}
    assert ctx.current_traj[4].content == 'resumed after the kill'
    assert ctx.stop_reason is StopReason.COMPLETED
    await runtime.shutdown()


async def test_kill_keeps_the_registry_entry_so_children_never_point_at_a_hole():
    """kill 送它进 ZOMBIE 而不摘条目，因此它根本不会制造悬空 parent_id，也就不需要级联。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    parent = await _resident(runtime, broker)
    child = await _resident(runtime, broker, parent_id=parent)

    await runtime.kill(parent)
    await asyncio.wait_for(_reaped(runtime), _TIMEOUT)

    assert await _status(runtime, parent) is AgentStatus.ZOMBIE
    info = await runtime.find(child)
    assert info is not None and info.parent_id == parent
    assert await runtime.registry.children_of(parent) == [child]
    assert await _status(runtime, child) is AgentStatus.IDLE
    await runtime.shutdown()


async def test_killing_an_exit_mode_agent_takes_its_subtree_with_it_instead_of_orphaning_it():
    """kill 自己不选择注销——去留由 exit_mode 决定，而 EXIT 走的是同一条级联。

    条目会消失的那条路上必须级联，否则「kill 不制造悬空 parent_id」只在 ZOMBIE / IDLE 两种退出
    模式下成立。三个子代跨两个深度，注册顺序既非升序亦非降序，反转后同样两者都不是。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    blocker = _Block()
    parent = await runtime.spawn(_blocking(blocker), query='go', exit_mode=ExitMode.EXIT)
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    bravo = await _resident(runtime, broker, parent_id=parent)
    alpha = await _resident(runtime, broker, parent_id=parent)
    delta = await _resident(runtime, broker, parent_id=bravo)

    await runtime.kill(parent)
    await asyncio.wait_for(_reaped(runtime), _TIMEOUT)

    tree = (parent, bravo, alpha, delta)
    assert [await runtime.find(agent_id) for agent_id in tree] == [None] * 4
    assert await _dangling_parents(registry) == []
    await runtime.shutdown()


async def test_kill_refuses_an_agent_this_process_never_took_over():
    """接管失败的条目归别人所有：本进程没有它的事件循环可停，而 kill 会替它写下 aborting——

    `ensure_active` 明文拒绝复活一个 aborting 的 Agent，所以那一笔连真正的持有者也再叫不醒它。
    """
    registry = _RecordingRegistry(deny=(AgentStatus.IDLE, AgentStatus.RUNNING))
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go')
    with pytest.raises(ContractViolation):
        await runtime.wait(agent_id)
    peer = Runtime(registry=registry, broker=broker)

    with pytest.raises(ContractViolation) as stranded:
        await runtime.kill(agent_id)
    with pytest.raises(ContractViolation) as not_mine:
        await peer.kill(agent_id)

    assert stranded.value.received != not_mine.value.received
    assert runtime._reapers == set()
    assert await _status(runtime, agent_id) is AgentStatus.IDLE
    assert await runtime.ensure_active(agent_id) is True
    await peer.shutdown()
    await runtime.shutdown()


async def test_killing_the_same_agent_twice_lands_on_the_same_place_without_reporting_a_thing(
    caplog: pytest.LogCaptureFixture,
):
    """收尾幂等：两次 kill 排出两个收割任务，两者都会走到同一个归宿。"""
    runtime = Runtime()
    blockers: list[_Block] = []
    agent_id = await _running(runtime, blockers)

    await runtime.kill(agent_id)
    await runtime.kill(agent_id)
    await asyncio.wait_for(_reaped(runtime), _TIMEOUT)

    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    assert caplog.records == []
    await runtime.shutdown()


async def test_kill_after_shutdown_starts_no_reaper_nobody_will_ever_await():
    """shutdown 是终态且已经等掉了收割任务，此后再排一个就是永不被 await 的孤儿。"""
    runtime = Runtime()
    blockers: list[_Block] = []
    agent_id = await _running(runtime, blockers)
    await runtime.shutdown()

    await runtime.kill(agent_id)

    assert runtime._reapers == set()


async def test_despawn_takes_the_whole_subtree_with_it_and_leaves_an_independent_tree_alone():
    """`unregister` 只把自己从父的 children 上摘掉、不动自己的子代。

    五个子代跨两个深度，注册顺序既非升序亦非降序，反转后同样两者都不是；另一棵独立的树必须整棵
    原样活着——「操作没错但作用在错的主体上」在本仓是一等缺陷类。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    root = await _resident(runtime, broker)
    bravo, alpha, charlie = [await _resident(runtime, broker, parent_id=root) for _ in range(3)]
    echo = await _resident(runtime, broker, parent_id=bravo)
    delta = await _resident(runtime, broker, parent_id=alpha)
    other_root = await _resident(runtime, broker)
    other_child = await _resident(runtime, broker, parent_id=other_root)
    other_leaf = await _resident(runtime, broker, parent_id=other_child)

    await runtime.despawn(root)

    doomed = (root, bravo, alpha, charlie, echo, delta)
    assert [await runtime.find(agent_id) for agent_id in doomed] == [None] * 6
    assert [agent_id in runtime._slots for agent_id in doomed] == [False] * 6
    assert await _dangling_parents(registry) == []

    survivors = (other_root, other_child, other_leaf)
    assert [await _status(runtime, agent_id) for agent_id in survivors] == [AgentStatus.IDLE] * 3
    for index, agent_id in enumerate(survivors):
        assert (await runtime.send(agent_id, f'still working {index}')).ok is True
        await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)
        agent = runtime._table.get(agent_id)
        assert agent is not None
        assert [block.content for block in agent.context.current_traj] == [
            f'still working {index}',
            'turn-0',
        ]
    await runtime.shutdown()


async def test_despawn_destroys_every_child_before_its_parent():
    """任何一刻都不存在「父已经没了而子还活着」的中间态。

    子代跨两个深度、注册顺序既非升序亦非降序：n=2 时 `sorted` 与 `sorted(reverse=True)` 里必有
    一个是恒等变换，反转类变异挡不住。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    root = await _resident(runtime, broker)
    bravo, alpha, charlie = [await _resident(runtime, broker, parent_id=root) for _ in range(3)]
    echo, foxtrot, delta = [await _resident(runtime, broker, parent_id=bravo) for _ in range(3)]

    await runtime.despawn(root)

    assert registry.unregisters == [delta, foxtrot, echo, charlie, alpha, bravo, root]
    await runtime.shutdown()


async def test_despawn_returns_only_after_every_loop_it_holds_has_actually_stopped():
    """承诺是「回来之后这些条目都不在了」，而这个承诺只有等到才作数。

    快照取在 `despawn` 返回的那一刻：被取消的任务事件循环转下去照样会跑完，在 `await` 之后断言
    「它停了」测的是调度顺序而不是等待语义。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    blockers: list[_Block] = []
    root = await _running(runtime, blockers)
    bravo = await _running(runtime, blockers, parent_id=root)
    alpha = await _running(runtime, blockers, parent_id=root)
    delta = await _running(runtime, blockers, parent_id=bravo)
    tree = (root, bravo, alpha, delta)
    tasks = [runtime._table.get_task(agent_id) for agent_id in tree]
    assert [task is not None and not task.done() for task in tasks] == [True] * 4

    await runtime.despawn(root)

    assert [task is not None and task.done() for task in tasks] == [True] * 4
    assert [await runtime.find(agent_id) for agent_id in tree] == [None] * 4
    await runtime.shutdown()


async def test_despawning_a_child_leaves_the_parent_and_its_siblings_alone():
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    root = await _resident(runtime, broker)
    doomed = await _resident(runtime, broker, parent_id=root)
    bravo = await _resident(runtime, broker, parent_id=root)
    alpha = await _resident(runtime, broker, parent_id=root)

    await runtime.despawn(doomed)

    assert await runtime.find(doomed) is None
    assert await runtime.registry.children_of(root) == [bravo, alpha]
    assert [await _status(runtime, agent_id) for agent_id in (root, bravo, alpha)] == [
        AgentStatus.IDLE
    ] * 3
    await runtime.shutdown()


async def test_despawning_an_unknown_agent_succeeds_because_the_postcondition_already_holds():
    """意图无法表达才抛；「它不在注册表里」这个后置条件已经成立。"""
    runtime = Runtime()
    await runtime.despawn('nope')
    assert await runtime.find('nope') is None
    await runtime.shutdown()


async def test_exit_mode_exit_uses_the_same_cascade_as_despawn():
    """EXIT 是在驱动任务自己的 finally 里走到这条路的：取消并 await 自己就是永久挂死。

    上界让「自取消守卫被删掉」变成一条会红的断言：那时 `_destroy` 在自己身上等自己，收尾停在
    半路，条目留在注册表里。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    blocker = _Block()
    parent = await runtime.spawn(_blocking(blocker), query='go', exit_mode=ExitMode.EXIT)
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    bravo = await _resident(runtime, broker, parent_id=parent)
    alpha = await _resident(runtime, broker, parent_id=parent)
    delta = await _resident(runtime, broker, parent_id=bravo)
    task = runtime._table.get_task(parent)
    assert task is not None

    blocker.release.set()
    await asyncio.wait_for(asyncio.shield(task), _TIMEOUT)

    tree = (parent, bravo, alpha, delta)
    assert [await runtime.find(agent_id) for agent_id in tree] == [None] * 4
    assert registry.unregisters == [delta, alpha, bravo, parent]
    assert await _dangling_parents(registry) == []
    await runtime.shutdown()


async def test_despawning_yourself_from_inside_your_own_loop_is_refused():
    """despawn 要等目标的事件循环停下来，等自己就是永久挂死。"""
    runtime = Runtime()
    holder: dict[str, str] = {}

    class _SelfDespawn(Tool):
        async def execute(self, note: str) -> ToolResult:
            """记下 note 然后试着销毁自己。"""
            try:
                await runtime.despawn(holder['agent_id'])
            except ContractViolation as error:
                return ToolResult(output=f'refused: {error.next_action}')
            return ToolResult(output='not refused')

    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[ToolCall(id='c1', name='SelfDespawn', arguments='{"note":"bye"}')]
            ),
            ScriptedTurn(content='still alive'),
        ]
    )
    agent_id = await runtime.spawn(_factory(llm, tools={'SelfDespawn': _SelfDespawn()}), query='go')
    holder['agent_id'] = agent_id
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    tool_blocks = [block for block in ctx.current_traj if block.role == 'tool']
    assert len(tool_blocks) == 1
    assert isinstance(tool_blocks[0].content, str)
    assert tool_blocks[0].content.startswith('refused: ')
    assert 'abort' in tool_blocks[0].content
    assert await runtime.find(agent_id) is not None
    await runtime.shutdown()


async def test_despawning_an_ancestor_from_inside_your_own_loop_is_refused():
    """自己在目标的子树里，`despawn` 同样等不到它停下来——而它抛在动手之前，树原样留着。

    只挡住被点名的那一个是不够的：拒绝的判据挂在名字上而不是挂在遍历找到的主体上时，这条路会
    先把祖先的整棵子树拆掉一半，再停在自己身上。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    holder: dict[str, str] = {}

    class _DespawnAncestor(Tool):
        async def execute(self, note: str) -> ToolResult:
            """记下 note 然后试着销毁自己的祖先。"""
            try:
                await runtime.despawn(holder['root'])
            except ContractViolation as error:
                return ToolResult(output=f'refused: {error.received}')
            return ToolResult(output='not refused')

    root = await _resident(runtime, broker)
    middle = await _resident(runtime, broker, parent_id=root)
    sibling = await _resident(runtime, broker, parent_id=root)
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[ToolCall(id='c1', name='DespawnAncestor', arguments='{"note":"bye"}')]
            ),
            ScriptedTurn(content='still alive'),
        ]
    )
    agent_id = await runtime.spawn(
        _factory(llm, tools={'DespawnAncestor': _DespawnAncestor()}),
        query='go',
        parent_id=middle,
    )
    holder['root'] = root
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    tool_blocks = [block for block in ctx.current_traj if block.role == 'tool']
    assert len(tool_blocks) == 1
    assert isinstance(tool_blocks[0].content, str)
    assert tool_blocks[0].content.startswith('refused: ')
    tree = (root, middle, sibling, agent_id)
    assert [await runtime.find(name) is not None for name in tree] == [True] * 4
    await runtime.shutdown()


async def test_a_child_born_while_the_walk_is_under_way_never_outlives_its_parent():
    """走一遍就动手是不行的：中间节点一旦注销，它下面的条目就再也走不到，而它们的 parent_id
    已经指向一个不存在的条目。

    闸装在树里**最后**被问到的那个节点上：那一刻 `children_of(bravo)` 已经答过，第一遍再也不会
    回头看它。落在这个窗口里的子代只有第二遍才发现得了，而钉住期间挂到已发现节点下的意图根本
    表达不出来——两者缺一，这棵树就会留下一个永久孤儿。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    root = await _resident(runtime, broker)
    bravo, alpha, charlie = [await _resident(runtime, broker, parent_id=root) for _ in range(3)]
    delta = await _resident(runtime, broker, parent_id=bravo)
    registry.hold_children_of = delta
    registry.walks.close()

    despawning = asyncio.create_task(runtime.despawn(root))
    await asyncio.wait_for(registry.walks.reach(1), _TIMEOUT)
    latecomer = await _resident(runtime, broker, parent_id=bravo)
    with pytest.raises(ContractViolation) as refused:
        await _resident(runtime, broker, parent_id=root)
    registry.walks.open()
    await asyncio.wait_for(despawning, _TIMEOUT)

    assert root in refused.value.expected
    tree = (root, bravo, alpha, charlie, delta, latecomer)
    assert [await runtime.find(agent_id) for agent_id in tree] == [None] * 6
    assert await _dangling_parents(registry) == []
    # 子先于父仍然成立：后来的那个排在它父亲前面。
    assert registry.unregisters.index(latecomer) < registry.unregisters.index(bravo)
    await runtime.shutdown()


async def test_despawn_removes_a_descendant_another_process_holds_instead_of_orphaning_it(
    caplog: pytest.LogCaptureFixture,
):
    """树在共享注册表里，本进程持有哪些实例与树的形状无关。

    `pause` / `resume` / `abort` 拒绝一个本进程接管不了的 Agent，`despawn` 不能照搬那条规则：
    留下一个别的进程持有的子代，它的 parent_id 就指向一个已被注销的父。外人排在三个子代的
    **中间**，且它下面还挂着本进程的 Agent——排在末位时「走过它」与「走到它为止」是同一个答案。
    """
    caplog.set_level(logging.DEBUG)
    registry = InMemoryAgentRegistry()
    broker = _SpyBroker(registry)
    owner = Runtime(registry=registry, broker=broker)
    peer = Runtime(registry=registry, broker=broker)
    root = await _resident(owner, broker)
    bravo = await _resident(owner, broker, parent_id=root)
    outsider = await _resident(peer, broker, parent_id=root)
    charlie = await _resident(owner, broker, parent_id=root)
    delta = await _resident(owner, broker, parent_id=outsider)
    stranded = peer._table.get_task(outsider)
    assert stranded is not None

    await owner.despawn(root)

    tree = (root, bravo, outsider, charlie, delta)
    assert [await owner.find(agent_id) for agent_id in tree] == [None] * 5
    assert await _dangling_parents(registry) == []

    # 别的进程的循环由它自己发现条目已经不在。条目不在不是丢失，不报 ERROR。
    await asyncio.wait_for(asyncio.wait({stranded}), _TIMEOUT)
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    assert any(
        outsider in record.getMessage() and '已经被 despawn' in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.DEBUG
    )
    # 跳过一个子代留下的是永久孤儿，因此这条路上不该有一条「跳过」记录。
    assert [record.getMessage() for record in caplog.records if record.levelname == 'INFO'] == []
    await peer.shutdown()
    await owner.shutdown()


async def test_a_descendant_that_cannot_be_destroyed_is_reported_and_the_walk_carries_on(
    caplog: pytest.LogCaptureFixture,
):
    """遍历到的子代拆不掉只记日志：为第一个冲突抛异常会留下一棵改了一半的树。

    而这次跳过留下的是一个**永久**孤儿——它的父已经注销，`children_of` 再也找不到它——所以那条
    记录必须在 root logger 的默认级别之上真的被创建出来，并说清是哪一个条目留下来了。
    """
    caplog.set_level(logging.INFO)
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    root = await _resident(runtime, broker)
    bravo, alpha, charlie = [await _resident(runtime, broker, parent_id=root) for _ in range(3)]
    echo = await _resident(runtime, broker, parent_id=bravo)
    registry.fail_unregister = bravo

    await runtime.despawn(root)

    assert [await runtime.find(agent_id) for agent_id in (root, alpha, charlie, echo)] == [None] * 4
    assert await runtime.find(bravo) is not None
    assert await _dangling_parents(registry) == [bravo]
    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert caplog.records[0].args == (bravo, root)

    # 那条记录给的下一步必须真的走得通：留下来的条目既叫得醒、也还能被单独拆掉。若它的「正在
    # 被拆」标记没被撤掉，这个条目就永久地既激活不了也挂不上新子代。
    registry.fail_unregister = None
    assert await runtime.ensure_active(bravo) is True
    await runtime.despawn(bravo)
    assert await runtime.find(bravo) is None
    assert await _dangling_parents(registry) == []
    await runtime.shutdown()


async def test_spawning_under_a_parent_that_is_being_destroyed_is_refused():
    """挂上去的子代会带着一个指向即将消失的条目的 parent_id 留下来，而那是不可逆的。"""
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    root = await _resident(runtime, broker)
    bravo = await _resident(runtime, broker, parent_id=root)
    registry.hold_children_of = bravo
    registry.walks.close()

    despawning = asyncio.create_task(runtime.despawn(root))
    await asyncio.wait_for(registry.walks.reach(1), _TIMEOUT)

    with pytest.raises(ContractViolation) as refused:
        await runtime.spawn(_factory(_done()), query='go', parent_id=root)

    assert root in refused.value.expected
    assert 'parent_id' in refused.value.next_action
    registry.walks.open()
    await asyncio.wait_for(despawning, _TIMEOUT)

    # 拆完就不再是那个理由了：条目已经不在，注册表自己会拒。
    with pytest.raises(ContractViolation) as gone:
        await runtime.spawn(_factory(_done()), query='go', parent_id=root)
    assert gone.value.received != refused.value.received
    assert await _dangling_parents(registry) == []
    await runtime.shutdown()


async def test_a_spawn_that_lands_after_shutdown_leaves_no_orphan_driver():
    """`_closed` 在方法开头读过一次，此后隔着两个 Protocol await。

    任何会挂起的 store 下，并发的 shutdown 会在 gather 返回之后才走到 create_task，于是建出一个
    永不被取消、永不被 await 的驱动，并把一个再没有订阅者的 Agent 留在注册表里。
    """
    registry = InMemoryAgentRegistry()
    broker = _HeldBroker()
    runtime = Runtime(registry=registry, broker=broker)
    broker.ensures.close()
    spawning = asyncio.create_task(runtime.spawn(_factory(_done()), query='go'))
    await asyncio.wait_for(broker.ensures.reach(1), _TIMEOUT)
    await runtime.shutdown()
    broker.ensures.open()

    with pytest.raises(ContractViolation) as refused:
        await asyncio.wait_for(spawning, _TIMEOUT)

    assert 'shutdown' in refused.value.received
    assert await registry.list() == []
    assert runtime._table.agent_ids() == []


async def test_an_activation_that_lands_after_shutdown_leaves_no_orphan_driver(
    caplog: pytest.LogCaptureFixture,
):
    """同一个洞的另一半：`ensure_active` 也隔着三个 await 才 create_task。

    它是问句，所以答 False 而不抛；但注册表已经被它从 zombie 翻成 idle 了，而 idle 宣布的是
    「它能处理消息」——此后再没有驱动会去处理，那条状态必须写回去。
    """
    broker = _HeldBroker()
    runtime = Runtime(broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go')
    await runtime.wait(agent_id)
    original = runtime._table.get_task(agent_id)
    broker.ensures.close()
    activating = asyncio.create_task(runtime.ensure_active(agent_id))
    await asyncio.wait_for(broker.ensures.reach(1), _TIMEOUT)
    await runtime.shutdown()
    broker.ensures.open()

    assert await asyncio.wait_for(activating, _TIMEOUT) is False

    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    assert runtime._table.get_task(agent_id) is original
    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert agent_id in caplog.records[0].getMessage()


async def test_the_lifecycle_junctions_report_every_agent_as_it_arrives_and_as_it_goes():
    """切点名与它的分类都计入公开 API：spawn / despawn / shutdown 报的都是既成事实。

    两代三个 Agent：只有一个 Agent 时「报了自己」与「报了随便一个」是同一个答案。
    """
    seen: list[tuple[str, str]] = []

    async def record_spawn(**kwargs: Any) -> HookResult:
        seen.append(('spawn', kwargs['agent_id']))
        return HookResult()

    async def record_despawn(**kwargs: Any) -> HookResult:
        seen.append(('despawn', kwargs['agent_id']))
        return HookResult()

    async def record_shutdown(**kwargs: Any) -> HookResult:
        seen.append(('shutdown', ''))
        return HookResult()

    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    runtime.hooks.register(Junction.AGENT_SPAWN, record_spawn)
    runtime.hooks.register(Junction.AGENT_DESPAWN, record_despawn)
    runtime.hooks.register(Junction.RUNTIME_SHUTDOWN, record_shutdown)
    root = await _resident(runtime, broker)
    bravo = await _resident(runtime, broker, parent_id=root)
    delta = await _resident(runtime, broker, parent_id=bravo)

    await runtime.despawn(root)
    await runtime.shutdown()

    assert seen == [
        ('spawn', root),
        ('spawn', bravo),
        ('spawn', delta),
        ('despawn', delta),
        ('despawn', bravo),
        ('despawn', root),
        ('shutdown', ''),
    ]


async def test_the_spawn_junction_reports_the_parent_each_child_was_filed_under():
    """父子关系是 spawn 这一刻唯一还看得见的地方：despawn 之后条目已经不在了。"""
    filed: dict[str, str | None] = {}

    async def record(**kwargs: Any) -> HookResult:
        filed[kwargs['agent_id']] = kwargs['parent_id']
        return HookResult()

    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    runtime.hooks.register(Junction.AGENT_SPAWN, record)
    root = await _resident(runtime, broker)
    bravo = await _resident(runtime, broker, parent_id=root)
    delta = await _resident(runtime, broker, parent_id=bravo)

    assert filed == {root: None, bravo: root, delta: bravo}
    await runtime.shutdown()


async def test_shutdown_waits_for_the_reaper_that_kill_left_winding_an_agent_down():
    """收割任务不在实例表里，`shutdown` 的取消名单看不见它们：不另等一次，它们就是一串
    "Task was destroyed but it is pending"，而那个 Agent 停在 aborting——复活不了。

    快照取在 `kill` 返回的那一刻：收尾还没被调度到，因此这条断言问的是 `shutdown` 等不等它，
    不是事件循环碰巧转到了哪里。
    """
    runtime = Runtime()
    blockers: list[_Block] = []
    agent_id = await _running(runtime, blockers)

    await runtime.kill(agent_id)
    reapers = list(runtime._reapers)
    assert [task.done() for task in reapers] == [False]

    await runtime.shutdown()

    assert [task.done() for task in reapers] == [True]
    assert runtime._reapers == set()
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE


async def test_a_reaper_that_cannot_finish_reports_it_instead_of_dropping_it_on_the_floor(
    caplog: pytest.LogCaptureFixture,
):
    """收割任务没有调用方，逃出去的异常无人取回，只会在 GC 时变成一行看不出主体的日志。"""
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking(blocker), query='go', exit_mode=ExitMode.EXIT)
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    registry.fail_unregister = agent_id

    await runtime.kill(agent_id)
    await asyncio.wait_for(_reaped(runtime), _TIMEOUT)

    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert caplog.records[0].args == (agent_id,)
    # 条目还在，所以这次失败是可诊断、可重来的，而不是一次静默消失的收尾。
    assert await runtime.find(agent_id) is not None
    await runtime.shutdown()


async def test_a_teardown_that_runs_twice_reports_each_despawn_exactly_once():
    """`AGENT_DESPAWN` 报的是「这个条目刚刚消失」这个既成事实。

    收尾会被走到两次——`kill` 的收割任务与驱动任务自己的 finally 都通向它——而对一个早已不在的
    条目再报一次是个假事件：挂在这个切点上记账的 hook 会把同一个 Agent 数两遍。
    """
    seen: list[str] = []

    async def record(**kwargs: Any) -> HookResult:
        seen.append(kwargs['agent_id'])
        return HookResult()

    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    runtime.hooks.register(Junction.AGENT_DESPAWN, record)
    blocker = _Block()
    parent = await runtime.spawn(_blocking(blocker), query='go', exit_mode=ExitMode.EXIT)
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    bravo = await _resident(runtime, broker, parent_id=parent)
    delta = await _resident(runtime, broker, parent_id=bravo)

    await runtime.kill(parent)
    await runtime.kill(parent)
    await asyncio.wait_for(_reaped(runtime), _TIMEOUT)

    assert seen == [delta, bravo, parent]
    await runtime.shutdown()


async def test_a_despawn_hook_that_files_a_child_under_the_node_being_torn_down_is_refused():
    """可观察切点跑在拆除**之内**：hook 因此能在一次拆除的中途重入 Runtime。

    这道守卫按的是「这个父正在被拆」，不是「这个父还在不在注册表里」——所以它在条目已经摘掉之后
    仍然答得出为什么，而不是把一句关于注册表的话丢给调用方。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    refusals: list[ContractViolation] = []

    async def reenter(**kwargs: Any) -> HookResult:
        try:
            await runtime.spawn(_factory(_done()), query='go', parent_id=kwargs['agent_id'])
        except ContractViolation as error:
            refusals.append(error)
        return HookResult()

    runtime.hooks.register(Junction.AGENT_DESPAWN, reenter)
    root = await _resident(runtime, broker)
    bravo = await _resident(runtime, broker, parent_id=root)
    delta = await _resident(runtime, broker, parent_id=bravo)

    await runtime.despawn(root)

    assert [error.expected for error in refusals] == [
        f'parent_id {agent_id!r} 不在被拆的过程中' for agent_id in (delta, bravo, root)
    ]
    assert [await runtime.find(agent_id) for agent_id in (root, bravo, delta)] == [None] * 3
    assert await _dangling_parents(registry) == []
    await runtime.shutdown()


async def test_a_despawn_hook_that_despawns_another_node_of_the_same_tree_derails_nothing():
    """重入的拆除与外层的拆除标记同一批节点，而两者各自在 finally 里撤自己那一份。

    撤早了这棵树就重新开放挂子代，而外层还在走它——所以这里既要每个节点恰好被报一次，也要走完
    之后整棵树都不在，且没有一个条目指着空洞。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    seen: list[str] = []
    holder: dict[str, str] = {}

    async def reenter(**kwargs: Any) -> HookResult:
        seen.append(kwargs['agent_id'])
        target = holder.pop('sibling', None)
        if target is not None:
            await runtime.despawn(target)
        return HookResult()

    runtime.hooks.register(Junction.AGENT_DESPAWN, reenter)
    root = await _resident(runtime, broker)
    bravo, alpha, charlie = [await _resident(runtime, broker, parent_id=root) for _ in range(3)]
    delta = await _resident(runtime, broker, parent_id=bravo)
    holder['sibling'] = alpha

    await runtime.despawn(root)

    tree = (root, bravo, alpha, charlie, delta)
    assert sorted(seen) == sorted(tree)
    assert [await runtime.find(agent_id) for agent_id in tree] == [None] * 5
    assert await _dangling_parents(registry) == []
    await runtime.shutdown()


async def test_a_lifecycle_hook_that_raises_derails_neither_the_spawn_nor_the_teardown():
    """可观察切点并行执行、返回值与异常一律丢弃：拆除是既成事实，报告它的人出错改不了它。"""
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)

    async def explode(**kwargs: Any) -> HookResult:
        raise RuntimeError('hook boom')

    runtime.hooks.register(Junction.AGENT_SPAWN, explode)
    runtime.hooks.register(Junction.AGENT_DESPAWN, explode)
    root = await _resident(runtime, broker)
    bravo = await _resident(runtime, broker, parent_id=root)
    delta = await _resident(runtime, broker, parent_id=bravo)

    await runtime.despawn(root)

    assert [await runtime.find(agent_id) for agent_id in (root, bravo, delta)] == [None] * 3
    assert registry.unregisters == [delta, bravo, root]
    await runtime.shutdown()


async def test_an_agent_that_was_killed_and_brought_back_can_be_killed_again():
    """「有收尾在飞」的标记必须随收尾一起撤掉。

    留着它，复活之后的每一次 kill 都排不出收割任务，而那个 Agent 停在 aborting——`ensure_active`
    明文拒绝复活一个正在收尾的 Agent，于是第一次 kill 变成了永久的诅咒。同一个操作在同一个主体
    上施加两次才看得出这件事，单次 kill 对它完全无感。
    """
    runtime = Runtime()
    gate = _Gate(2)
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Gate', arguments='{}')]),
            ScriptedTurn(tool_calls=[ToolCall(id='c2', name='Gate', arguments='{}')]),
            ScriptedTurn(content='done'),
        ]
    )
    agent_id = await runtime.spawn(_factory(llm, tools={'Gate': gate}), query='go')
    await asyncio.wait_for(gate.entered[0].wait(), _TIMEOUT)

    await runtime.kill(agent_id)
    await asyncio.wait_for(_reaped(runtime), _TIMEOUT)
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE

    assert (await runtime.send(agent_id, 'again')).ok is True
    await asyncio.wait_for(gate.entered[1].wait(), _TIMEOUT)
    await runtime.kill(agent_id)
    await asyncio.wait_for(_reaped(runtime), _TIMEOUT)

    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert [block.role for block in agent.context.current_traj] == [
        'user',
        'assistant',
        'tool',
        'user',
        'assistant',
    ]
    await runtime.shutdown()


async def test_a_cascade_that_dies_after_marking_leaves_the_other_cascades_pins_alone():
    """钉住标记按调用计数，否则一条中途夭折的级联会把别人还在拆的树整个放开。

    撤标记写在 `finally` 里，所以它**也在一个节点都没拆就退出时执行**：`_refuse_self` 的预检查在
    一个 Agent 从自己的工具里 despawn 祖先时必然抛，而它在抛之前已经把整棵树标记过一遍。撤标记若
    不分是谁放的，此刻往树上挂的子代就不再被拒——而它不在外层早已冻结好的拆除名单里，于是它的
    parent_id 永久指向一个已被注销的父，`children_of` 从此永远找不到它。

    纯公开路径：`despawn` + Agent 工具里的 `despawn(祖先)` + `spawn`，唯一的额外元素是一个会
    `await` 的可观察 hook（可观察切点本来就允许做异步工作）。另一棵独立的树必须整棵原样活着。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    entered = asyncio.Event()
    released = asyncio.Event()
    meddled = asyncio.Event()
    holder: dict[str, str] = {}
    seen: dict[str, object] = {}

    class _Meddle(Tool):
        async def execute(self, note: str) -> ToolResult:
            """记下 note，等测试放行，然后 despawn 自己的祖先并往树上挂一个新子代。"""
            entered.set()
            await released.wait()
            try:
                await runtime.despawn(holder['root'])
            except ContractViolation as error:
                seen['refusal'] = error.received
            try:
                seen['newborn'] = await _resident(runtime, broker, parent_id=holder['bravo'])
            except ContractViolation as error:
                seen['spawn_refused'] = error.expected
            meddled.set()
            return ToolResult(output='meddled')

    async def open_the_window(**kwargs: Any) -> HookResult:
        if kwargs['agent_id'] == holder.get('first_torn_down'):
            released.set()
            await asyncio.wait_for(meddled.wait(), _TIMEOUT)
        return HookResult()

    runtime.hooks.register(Junction.AGENT_DESPAWN, open_the_window)
    root = await _resident(runtime, broker)
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Meddle', arguments='{"note":"bye"}')]),
            ScriptedTurn(content='still alive'),
        ]
    )
    meddler = await runtime.spawn(
        _factory(llm, tools={'Meddle': _Meddle()}), query='go', parent_id=root
    )
    bravo = await _resident(runtime, broker, parent_id=root)
    charlie = await _resident(runtime, broker, parent_id=root)
    delta = await _resident(runtime, broker, parent_id=bravo)
    other_root = await _resident(runtime, broker)
    other_child = await _resident(runtime, broker, parent_id=other_root)
    other_leaf = await _resident(runtime, broker, parent_id=other_child)
    holder.update(root=root, bravo=bravo, first_torn_down=delta)
    await asyncio.wait_for(entered.wait(), _TIMEOUT)

    await asyncio.wait_for(runtime.despawn(root), _TIMEOUT)

    assert seen['refusal'] is not None
    assert seen.get('newborn') is None
    assert seen['spawn_refused'] == f'parent_id {bravo!r} 不在被拆的过程中'
    doomed = (root, meddler, bravo, charlie, delta)
    assert [await runtime.find(agent_id) for agent_id in doomed] == [None] * 5
    assert await _dangling_parents(registry) == []
    # 每一份钉住都还给了：留一份下去，这棵树的位置从此挂不上任何新子代。
    assert runtime._doomed == {}
    survivors = (other_root, other_child, other_leaf)
    assert [await _status(runtime, agent_id) for agent_id in survivors] == [AgentStatus.IDLE] * 3
    await runtime.shutdown()


async def test_two_overlapping_despawns_report_each_node_exactly_once():
    """`AGENT_DESPAWN` 报的是「这个条目刚刚消失」这个既成事实，重复报就是假事件。

    「还有没有痕迹可拆」自带一个 await，因此它是 check-then-act：两条重叠的级联都会先看到
    「它还在」，然后各自摘一遍、各自报一次。闸装在第一个被拆的节点摘队列那一刻——拆除还什么都
    没写下去，正是两条级联能同时通过那道检查的窗口。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    reported: list[str] = []

    async def record(**kwargs: Any) -> HookResult:
        reported.append(kwargs['agent_id'])
        return HookResult()

    runtime.hooks.register(Junction.AGENT_DESPAWN, record)
    root = await _resident(runtime, broker)
    bravo, alpha, charlie = [await _resident(runtime, broker, parent_id=root) for _ in range(3)]
    delta = await _resident(runtime, broker, parent_id=bravo)
    broker.hold_removal_of = delta
    broker.removal_hold.close()

    outer = asyncio.create_task(runtime.despawn(root))
    await asyncio.wait_for(broker.removal_hold.reach(1), _TIMEOUT)
    registry.hold_children_of = delta
    registry.walks.close()
    inner = asyncio.create_task(runtime.despawn(delta))
    await asyncio.wait_for(registry.walks.reach(1), _TIMEOUT)
    registry.walks.open()
    broker.removal_hold.open()
    await asyncio.wait_for(asyncio.gather(inner, outer), _TIMEOUT)

    tree = (root, bravo, alpha, charlie, delta)
    assert sorted(reported) == sorted(tree)
    assert [await runtime.find(agent_id) for agent_id in tree] == [None] * 5
    assert registry.unregisters == [delta, charlie, alpha, bravo, root]
    assert await _dangling_parents(registry) == []
    assert runtime._destroying == {}
    await runtime.shutdown()


async def test_an_exit_teardown_racing_an_external_despawn_reports_each_node_exactly_once():
    """调用方完全没有并发也能撞上：`ExitMode.EXIT` 的收尾走的是同一条级联。

    一个 Agent 自己收尾时拆整棵子树，而外部一次 `despawn(子代)` 落在同一批节点上——两者是两个
    独立的执行体，谁都不知道对方在拆。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    reported: list[str] = []

    async def record(**kwargs: Any) -> HookResult:
        reported.append(kwargs['agent_id'])
        return HookResult()

    runtime.hooks.register(Junction.AGENT_DESPAWN, record)
    blocker = _Block()
    parent = await runtime.spawn(_blocking(blocker), query='go', exit_mode=ExitMode.EXIT)
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    bravo, alpha, charlie = [await _resident(runtime, broker, parent_id=parent) for _ in range(3)]
    delta = await _resident(runtime, broker, parent_id=bravo)
    exiting = runtime._table.get_task(parent)
    assert exiting is not None
    broker.hold_removal_of = delta
    broker.removal_hold.close()

    blocker.release.set()
    await asyncio.wait_for(broker.removal_hold.reach(1), _TIMEOUT)
    registry.hold_children_of = delta
    registry.walks.close()
    despawning = asyncio.create_task(runtime.despawn(bravo))
    await asyncio.wait_for(registry.walks.reach(1), _TIMEOUT)
    registry.walks.open()
    broker.removal_hold.open()
    await asyncio.wait_for(asyncio.gather(despawning, asyncio.shield(exiting)), _TIMEOUT)

    tree = (parent, bravo, alpha, charlie, delta)
    assert sorted(reported) == sorted(tree)
    assert [await runtime.find(agent_id) for agent_id in tree] == [None] * 5
    assert await _dangling_parents(registry) == []
    assert runtime._destroying == {}
    await runtime.shutdown()


async def test_one_agents_finished_reaping_leaves_another_agents_token_alone():
    """「有收尾在飞」的令牌按 agent 撤，不是撤一整张表。

    撤掉别人的令牌，那个 Agent 会在收尾还没走完时被第二次 kill 排出**第二个**收割任务，于是同一
    次拆除走两遍级联。同一操作的并发度提到 2 才看得出这件事：只有一个在飞的收尾时，「撤自己那
    一份」与「全撤」是同一个答案。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    blockers: list[_Block] = []
    held = await _running(runtime, blockers)
    quick = await _running(runtime, blockers)
    broker.hold_removal_of = held
    broker.removal_hold.close()

    await runtime.kill(held)
    await asyncio.wait_for(broker.removal_hold.reach(1), _TIMEOUT)
    reapers = set(runtime._reapers)
    await runtime.kill(quick)
    reaper_of_quick = (set(runtime._reapers) - reapers).pop()
    await asyncio.wait_for(asyncio.wait({reaper_of_quick}), _TIMEOUT)

    assert runtime._reaping == {held}
    # 令牌还在，所以第二次 kill 不再排收割任务：末态由那个还在飞的收尾给出。
    await runtime.kill(held)
    assert [task.get_name() for task in runtime._reapers] == [f'reap:{held}']

    broker.removal_hold.open()
    await asyncio.wait_for(_reaped(runtime), _TIMEOUT)
    assert [await _status(runtime, agent_id) for agent_id in (held, quick)] == [
        AgentStatus.ZOMBIE
    ] * 2
    assert runtime._reaping == set()
    # 一次收尾就是一次摘队列：第二个收割任务会把同一个 Agent 的收尾整个走第二遍。
    assert [agent_id for agent_id, _ in broker.removals].count(held) == 1
    await runtime.shutdown()


async def test_one_finished_teardown_leaves_another_nodes_in_flight_claim_alone():
    """占位按 agent 撤，不是撤一整张表：一次拆除走完不代表别人那次也走完了。

    撤掉别人的占位，那个还停在半路的节点会被第二次拆除认成「它还在」——两条级联于是各自摘一遍、
    各自报一次 `AGENT_DESPAWN`，正是占位存在的理由被绕过的样子。要看出这件事，得让两次拆除同时
    在飞**且落在不同的 agent 上**：只有一个在飞的时候，「撤自己那一份」与「全撤」是同一个答案。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    reported: list[str] = []

    async def record(**kwargs: Any) -> HookResult:
        reported.append(kwargs['agent_id'])
        return HookResult()

    runtime.hooks.register(Junction.AGENT_DESPAWN, record)
    held = await _resident(runtime, broker)
    other_root = await _resident(runtime, broker)
    other_child = await _resident(runtime, broker, parent_id=other_root)
    broker.hold_removal_of = held
    broker.removal_hold.close()

    first = asyncio.create_task(runtime.despawn(held))
    await asyncio.wait_for(broker.removal_hold.reach(1), _TIMEOUT)
    # 另一棵树整棵拆完：它释放的是它自己的两份占位，`held` 那一份与它无关。
    await asyncio.wait_for(runtime.despawn(other_root), _TIMEOUT)
    registry.hold_children_of = held
    registry.walks.close()
    second = asyncio.create_task(runtime.despawn(held))
    await asyncio.wait_for(registry.walks.reach(1), _TIMEOUT)
    registry.walks.open()
    broker.removal_hold.open()
    await asyncio.wait_for(asyncio.gather(second, first), _TIMEOUT)

    assert reported == [other_child, other_root, held]
    assert [await runtime.find(agent_id) for agent_id in (held, other_root, other_child)] == [
        None
    ] * 3
    assert runtime._destroying == {}
    await runtime.shutdown()


async def test_a_despawn_that_finds_the_node_already_being_torn_down_tears_it_down_itself():
    """去重不能靠早退，也不能靠等对方：`despawn` 的后置条件是它自己回答的。

    答案在**被测调用返回的那一刻**取，且取在它自己的任务里：另一条级联的事件循环转下去照样会
    把这个条目摘掉，在外面 `await` 之后再问，测的是调度顺序而不是等待语义。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    root = await _resident(runtime, broker)
    bravo, alpha, charlie = [await _resident(runtime, broker, parent_id=root) for _ in range(3)]
    delta = await _resident(runtime, broker, parent_id=bravo)

    async def despawn_and_look() -> bool:
        await runtime.despawn(delta)
        return await runtime.find(delta) is None

    broker.hold_removal_of = delta
    broker.removal_hold.close()
    outer = asyncio.create_task(runtime.despawn(root))
    await asyncio.wait_for(broker.removal_hold.reach(1), _TIMEOUT)
    registry.hold_children_of = delta
    registry.walks.close()
    inner = asyncio.create_task(despawn_and_look())
    await asyncio.wait_for(registry.walks.reach(1), _TIMEOUT)
    registry.walks.open()
    broker.removal_hold.open()

    assert await asyncio.wait_for(inner, _TIMEOUT) is True
    await asyncio.wait_for(outer, _TIMEOUT)
    assert await _dangling_parents(registry) == []
    await runtime.shutdown()


async def test_a_spawn_whose_parent_is_doomed_while_it_reads_back_its_own_entry_is_refused():
    """两道复检的**顺序**是承重的：带 await 的那一问在前，两个同步标志位在后。

    反过来写，一条级联可以恰好在「读回自己那条条目」这个 await 期间钉住整棵树——`_doomed` 那一问
    此前已经问过，而条目还没被注销，于是 `spawn` 照常交出一个级联马上就要拆掉的 agent_id。闸装在
    第一个被拆节点的摘队列上：那一刻树已经钉住、一个条目都还没摘掉。
    """
    registry = _RecordingRegistry()
    broker = _HeldBroker()
    runtime = Runtime(registry=registry, broker=broker)
    root = await runtime.spawn(_factory(_done()), query='go')
    await asyncio.wait_for(runtime.wait(root), _TIMEOUT)
    tree: list[str] = []
    for parent in (root, root, root, root):
        agent_id = await runtime.spawn(_factory(_done()), query='go', parent_id=parent)
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        tree.append(agent_id)
    bravo, alpha, charlie, delta = tree
    echo = await runtime.spawn(_factory(_done()), query='go', parent_id=alpha)
    await asyncio.wait_for(runtime.wait(echo), _TIMEOUT)

    built: list[BaseAgent] = []
    broker.ensures.close()
    spawning = asyncio.create_task(
        runtime.spawn(_capturing(built, _done()), query='go', parent_id=bravo)
    )
    await asyncio.wait_for(broker.ensures.reach(1), _TIMEOUT)
    child = built[0].agent_id
    registry.hold_get_of = child
    registry.gets.close()
    broker.ensures.open()
    await asyncio.wait_for(registry.gets.reach(1), _TIMEOUT)
    broker.hold_removal_of = echo
    broker.removal_hold.close()
    despawning = asyncio.create_task(runtime.despawn(root))
    await asyncio.wait_for(broker.removal_hold.reach(1), _TIMEOUT)
    registry.gets.open()

    with pytest.raises(ContractViolation) as refused:
        await asyncio.wait_for(spawning, _TIMEOUT)

    broker.removal_hold.open()
    await asyncio.wait_for(despawning, _TIMEOUT)

    assert bravo in refused.value.expected
    assert child not in runtime._slots
    assert runtime._table.get(child) is None
    assert await _dangling_parents(registry) == []
    assert registry.unregisters == [child, echo, delta, charlie, alpha, bravo, root]
    _ = delta
    await runtime.shutdown()


async def test_despawn_reports_the_disappearance_of_a_descendant_this_process_never_held():
    """`AGENT_DESPAWN` 报的是「这个**条目**刚刚消失」，而条目的去留与本进程持有哪些实例无关。

    只在本进程持有过实例的节点上报，挂在这个切点上记账的 hook 会漏掉整片属于别人的子树——而正是
    这条级联把它们注销掉的。外人排在三个子代的**中间**，且它下面还挂着本进程的 Agent：排在末位
    时「报了它」与「报到它为止」是同一个答案。
    """
    registry = InMemoryAgentRegistry()
    broker = _SpyBroker(registry)
    owner = Runtime(registry=registry, broker=broker)
    peer = Runtime(registry=registry, broker=broker)
    reported: list[str] = []

    async def record(**kwargs: Any) -> HookResult:
        reported.append(kwargs['agent_id'])
        return HookResult()

    owner.hooks.register(Junction.AGENT_DESPAWN, record)
    root = await _resident(owner, broker)
    bravo = await _resident(owner, broker, parent_id=root)
    outsider = await _resident(peer, broker, parent_id=root)
    charlie = await _resident(owner, broker, parent_id=root)
    delta = await _resident(owner, broker, parent_id=outsider)

    await asyncio.wait_for(owner.despawn(root), _TIMEOUT)

    assert reported == [delta, charlie, outsider, bravo, root]
    assert await _dangling_parents(registry) == []
    await peer.shutdown()
    await owner.shutdown()


async def test_a_teardown_cancelled_before_it_removes_anything_leaves_the_peer_nothing_to_trust():
    """一个「拆完了」的信号说的是**持有者离开了**，不是**这个条目已经不在了**。

    持有者被取消时两者分叉：条目还在，而相信那个信号的一方会接着注销这个条目的**父**——永久
    悬空的 `parent_id`，且任何级别的日志都没有一条（取消刻意不留痕，而持有者「正常」返回了）。
    所以第二条级联自己动手，判据是它**也**到了摘队列那道闸上：相信对方的写法在那里永远只有一个
    到达者。另一棵独立的树必须整棵原样活着。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    reported: list[str] = []

    async def record(**kwargs: Any) -> HookResult:
        reported.append(kwargs['agent_id'])
        return HookResult()

    runtime.hooks.register(Junction.AGENT_DESPAWN, record)
    root = await _resident(runtime, broker)
    bravo, alpha, charlie = [await _resident(runtime, broker, parent_id=root) for _ in range(3)]
    delta, echo, foxtrot = [await _resident(runtime, broker, parent_id=bravo) for _ in range(3)]
    other_root = await _resident(runtime, broker)
    other_child = await _resident(runtime, broker, parent_id=other_root)
    other_leaf = await _resident(runtime, broker, parent_id=other_child)
    broker.hold_removal_of = delta
    broker.removal_hold.close()

    holder = asyncio.create_task(runtime.despawn(delta))
    await asyncio.wait_for(broker.removal_hold.reach(1), _TIMEOUT)
    peer = asyncio.create_task(runtime.despawn(root))
    await asyncio.wait_for(broker.removal_hold.reach(2), _TIMEOUT)
    holder.cancel()
    await asyncio.gather(holder, return_exceptions=True)
    broker.removal_hold.open()
    await asyncio.wait_for(peer, _TIMEOUT)

    assert broker.removal_hold.arrivals == [delta, delta]
    doomed = (root, bravo, alpha, charlie, delta, echo, foxtrot)
    assert [await runtime.find(agent_id) for agent_id in doomed] == [None] * 7
    assert sorted(reported) == sorted(doomed)
    assert await _dangling_parents(registry) == []
    assert runtime._doomed == {}
    assert runtime._destroying == {}
    survivors = (other_root, other_child, other_leaf)
    assert [await _status(runtime, agent_id) for agent_id in survivors] == [AgentStatus.IDLE] * 3
    await runtime.shutdown()


async def test_two_cascades_that_each_tear_down_the_others_tree_both_return():
    """新加的阻塞边若落在一个**平级调用方**身上，它可以成环——而 `despawn` 是唯一入口，环上的
    两棵子树因此永久不可拆。

    环的长度是 2，走的不是 hook 那条自环：`_destroy` 在**摘掉任何痕迹之前**就 `await` 子代的
    驱动任务，那时痕迹还在，「痕迹已经没了就当场回答」这个例外够不着它。调用方这一侧只是两次
    普通的 `despawn`，两个 Agent 的工具收尾里各拆掉对方那棵树。上界让死锁变成一条红色的断言。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    reported: list[str] = []

    async def record(**kwargs: Any) -> HookResult:
        reported.append(kwargs['agent_id'])
        return HookResult()

    runtime.hooks.register(Junction.AGENT_DESPAWN, record)
    trees: list[tuple[str, list[str], str]] = []
    tools = [_Retaliate(), _Retaliate()]
    for tool in tools:
        root = await _resident(runtime, broker)
        kids = [await _resident(runtime, broker, parent_id=root) for _ in range(3)]
        llm = ScriptedLLM(
            [
                ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Retaliate', arguments='{}')]),
                ScriptedTurn(content='done'),
            ]
        )
        worker = await runtime.spawn(
            _factory(llm, tools={'Retaliate': tool}), query='go', parent_id=kids[0]
        )
        trees.append((root, kids, worker))
    for tool, (root, _, _) in zip(tools, reversed(trees), strict=True):
        tool.runtime = runtime
        tool.target = root
    await asyncio.wait_for(asyncio.gather(*(tool.entered.wait() for tool in tools)), _TIMEOUT)

    first = asyncio.create_task(runtime.despawn(trees[0][0]))
    second = asyncio.create_task(runtime.despawn(trees[1][0]))
    await asyncio.wait_for(asyncio.gather(first, second), _TIMEOUT)

    everything = [agent_id for root, kids, worker in trees for agent_id in (root, *kids, worker)]
    assert [await runtime.find(agent_id) for agent_id in everything] == [None] * 10
    assert sorted(reported) == sorted(everything)
    assert await _dangling_parents(registry) == []
    assert runtime._doomed == {}
    assert runtime._destroying == {}
    await runtime.shutdown()


async def test_a_despawn_whose_registry_keeps_the_entry_reports_it_instead_of_the_despawn(
    caplog: pytest.LogCaptureFixture,
):
    """后置条件是复检出来的，不是从 `unregister` 的明文契约推出来的。

    一个答应了却没真的摘掉条目的注册表既不该换来一次 `AGENT_DESPAWN`（它没消失，那是假事件），
    也不该一声不响返回——留下的条目此后 `parent_id` 指着空洞，而那条 ERROR 里的下一步是够得着
    它的唯一办法。这是「接缝另一侧的合法替换」：不抛、不说谎，只是没兑现契约。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    reported: list[str] = []

    async def record(**kwargs: Any) -> HookResult:
        reported.append(kwargs['agent_id'])
        return HookResult()

    runtime.hooks.register(Junction.AGENT_DESPAWN, record)
    root = await _resident(runtime, broker)
    bravo, alpha, charlie = [await _resident(runtime, broker, parent_id=root) for _ in range(3)]
    delta = await _resident(runtime, broker, parent_id=bravo)
    registry.silent_unregister = delta

    await asyncio.wait_for(runtime.despawn(root), _TIMEOUT)

    assert reported == [charlie, alpha, bravo, root]
    assert await runtime.find(delta) is not None
    assert await _dangling_parents(registry) == [delta]
    assert [record.levelname for record in caplog.records] == ['ERROR']
    message = caplog.records[0].getMessage()
    assert caplog.records[0].args == (delta,)
    _assert_three_part(message)
    await runtime.shutdown()


async def test_unpinning_a_mark_that_is_no_longer_there_still_takes_the_rest_off():
    """撤钉住跑在 `finally` 里：一个 KeyError 会同时吃掉在飞的异常、并让这一批里剩下的 id 一份
    都撤不掉，那些位置于是永久挂不上新子代。

    基数取到 3，且缺的那一份排在**中间**：排在末位时「剩下的还撤不撤」与「就此中断」是同一个
    答案。
    """
    runtime = Runtime()
    runtime._pin(['bravo', 'charlie'])

    runtime._unpin(['bravo', 'alpha', 'charlie'])

    assert runtime._doomed == {}
    await runtime.shutdown()


async def test_unpinning_a_mark_that_is_no_longer_there_is_reported(
    caplog: pytest.LogCaptureFixture,
):
    """配平被破坏是一次静默的丢失：此后往这些位置 spawn 子代会被误拒或误放行，而没有别的痕迹。"""
    runtime = Runtime()

    runtime._unpin(['alpha'])

    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert caplog.records[0].args == ('alpha',)
    _assert_three_part(caplog.records[0].getMessage())
    await runtime.shutdown()


async def test_a_drive_that_finds_its_entry_gone_says_so_instead_of_blaming_another_owner(
    caplog: pytest.LogCaptureFixture,
):
    """IDLE -> RUNNING 被拒的两种情形下一步不是同一件事。

    条目已被注销时，「用 find() 读它的 status 与 owner」答 `None`，读不出任何东西；而本进程还
    留着它的 slot 与实例，收掉它们的是 `despawn`（它的痕迹检查看的是全部痕迹）。措辞相同的一条
    记录把两种情形合并，等于在其中一种上给了一个够不着的下一步。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go')
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert await runtime.ensure_active(agent_id) is True
    # 这两句之间没有挂起点（内存 store 全程不挂起），复活出来的驱动因此还没跑起来。
    await registry.unregister(agent_id)
    revived = runtime._table.get_task(agent_id)
    assert revived is not None
    await asyncio.wait_for(asyncio.wait({revived}), _TIMEOUT)

    assert [record.levelname for record in caplog.records] == ['ERROR']
    message = caplog.records[0].getMessage()
    assert '条目已经不在注册表里' in message
    assert 'despawn()' in message
    assert 'find()' not in message
    _assert_three_part(message)
    # 那条记录说的下一步真的够得着：痕迹检查看的是全部痕迹，不只是注册表条目。
    await runtime.despawn(agent_id)
    assert agent_id not in runtime._slots
    assert runtime._table.get(agent_id) is None
    await runtime.shutdown()


async def test_a_despawn_hook_that_despawns_an_ancestor_does_not_wait_on_its_own_teardown():
    """可观察切点跑在拆除**之内**，所以从 hook 里发起的级联会重新走到发出这次 hook 的那个节点。

    占着那个节点的正是本任务自己那次拆除，无条件等它就是死锁——痕迹已经没了才直接回答。上界让
    这条断言变成一条红色的用例而不是一次挂死。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    seen: list[str] = []
    holder: dict[str, str] = {}

    async def reenter(**kwargs: Any) -> HookResult:
        seen.append(kwargs['agent_id'])
        target = holder.pop('root', None)
        if target is not None:
            await runtime.despawn(target)
        return HookResult()

    runtime.hooks.register(Junction.AGENT_DESPAWN, reenter)
    root = await _resident(runtime, broker)
    bravo, alpha, charlie = [await _resident(runtime, broker, parent_id=root) for _ in range(3)]
    delta = await _resident(runtime, broker, parent_id=bravo)
    holder['root'] = root

    await asyncio.wait_for(runtime.despawn(root), _TIMEOUT)

    tree = (root, bravo, alpha, charlie, delta)
    assert sorted(seen) == sorted(tree)
    assert [await runtime.find(agent_id) for agent_id in tree] == [None] * 5
    assert await _dangling_parents(registry) == []
    assert runtime._doomed == {}
    assert runtime._destroying == {}
    await runtime.shutdown()


async def test_a_spawn_whose_parent_is_doomed_while_it_waits_hands_back_no_unaddressable_agent():
    """`_doomed` 在方法开头读过一次，此后隔着 register / ensure_queue 两个 Protocol await。

    任何会挂起的 store 下，一条并发的级联会在这段窗口里发现刚注册好的子代并把它拆掉；`spawn`
    若只复检 `_closed`，它会照常返回一个注册表里不存在的 agent_id，留下一份属于不可寻址 Agent 的
    Context，并让同一个 agent_id 先 `AGENT_DESPAWN` 后 `AGENT_SPAWN`——生命周期切点的顺序被倒过来。

    这一支是**级联还在飞**：闸装在第一个被拆节点的摘队列上，此刻整棵树已经钉住、而一个条目都
    还没摘掉，所以「我注册的那一条还在不在」答「还在」，按住它的只能是 `_doomed`。
    """
    registry = InMemoryAgentRegistry()
    broker = _HeldBroker()
    runtime = Runtime(registry=registry, broker=broker)
    log: list[tuple[Junction, str]] = []

    async def note_spawn(**kwargs: Any) -> HookResult:
        log.append((Junction.AGENT_SPAWN, kwargs['agent_id']))
        return HookResult()

    async def note_despawn(**kwargs: Any) -> HookResult:
        log.append((Junction.AGENT_DESPAWN, kwargs['agent_id']))
        return HookResult()

    runtime.hooks.register(Junction.AGENT_SPAWN, note_spawn)
    runtime.hooks.register(Junction.AGENT_DESPAWN, note_despawn)
    root = await runtime.spawn(_factory(_done()), query='go')
    await asyncio.wait_for(runtime.wait(root), _TIMEOUT)
    tree: list[str] = []
    for parent in (root, root, root, root):
        agent_id = await runtime.spawn(_factory(_done()), query='go', parent_id=parent)
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        tree.append(agent_id)
    bravo, alpha, charlie, delta = tree
    echo = await runtime.spawn(_factory(_done()), query='go', parent_id=alpha)
    await asyncio.wait_for(runtime.wait(echo), _TIMEOUT)

    built: list[BaseAgent] = []
    broker.ensures.close()
    spawning = asyncio.create_task(
        runtime.spawn(_capturing(built, _done()), query='go', parent_id=bravo)
    )
    await asyncio.wait_for(broker.ensures.reach(1), _TIMEOUT)
    child = built[0].agent_id
    broker.hold_removal_of = echo
    broker.removal_hold.close()
    despawning = asyncio.create_task(runtime.despawn(root))
    await asyncio.wait_for(broker.removal_hold.reach(1), _TIMEOUT)
    broker.ensures.open()

    with pytest.raises(ContractViolation) as refused:
        await asyncio.wait_for(spawning, _TIMEOUT)

    broker.removal_hold.open()
    await asyncio.wait_for(despawning, _TIMEOUT)

    assert bravo in refused.value.expected
    assert 'parent_id' in refused.value.next_action
    assert await runtime.find(child) is None
    assert child not in runtime._slots
    assert runtime._table.get(child) is None
    despawned = [subject for junction, subject in log if junction is Junction.AGENT_DESPAWN]
    assert despawned == [echo, delta, charlie, alpha, bravo, root]
    assert (Junction.AGENT_SPAWN, child) not in log
    at = {pair: index for index, pair in enumerate(log)}
    assert [
        at[(Junction.AGENT_SPAWN, subject)] < at[(Junction.AGENT_DESPAWN, subject)]
        for subject in (echo, delta, charlie, alpha, bravo, root)
    ] == [True] * 6
    assert await _dangling_parents(registry) == []
    await runtime.shutdown()


async def test_a_spawn_whose_parent_vanishes_while_it_waits_hands_back_no_unaddressable_agent():
    """级联**在这段窗口里跑完**时，「那个父此刻在不在被拆」答「不在」——钉住早已撤掉。

    要按住这一支，复检的对象得是后置条件本身：**我刚注册进去的那一条还在不在**。少了它，`spawn`
    照常返回一个注册表里不存在的 agent_id（`send` 答 ok=False、`wait` 抛），并留下一份属于不可
    寻址 Agent 的 `_Slot` 与 Context，而同一个 agent_id 先 `AGENT_DESPAWN` 后 `AGENT_SPAWN`。
    """
    registry = InMemoryAgentRegistry()
    broker = _HeldBroker()
    runtime = Runtime(registry=registry, broker=broker)
    log: list[tuple[Junction, str]] = []

    async def note_spawn(**kwargs: Any) -> HookResult:
        log.append((Junction.AGENT_SPAWN, kwargs['agent_id']))
        return HookResult()

    async def note_despawn(**kwargs: Any) -> HookResult:
        log.append((Junction.AGENT_DESPAWN, kwargs['agent_id']))
        return HookResult()

    runtime.hooks.register(Junction.AGENT_SPAWN, note_spawn)
    runtime.hooks.register(Junction.AGENT_DESPAWN, note_despawn)
    root = await runtime.spawn(_factory(_done()), query='go')
    await asyncio.wait_for(runtime.wait(root), _TIMEOUT)
    tree: list[str] = []
    for parent in (root, root, root, root):
        agent_id = await runtime.spawn(_factory(_done()), query='go', parent_id=parent)
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        tree.append(agent_id)
    bravo, alpha, charlie, delta = tree
    other_root = await runtime.spawn(_factory(_done()), query='go')
    await asyncio.wait_for(runtime.wait(other_root), _TIMEOUT)
    other_child = await runtime.spawn(_factory(_done()), query='go', parent_id=other_root)
    await asyncio.wait_for(runtime.wait(other_child), _TIMEOUT)

    built: list[BaseAgent] = []
    broker.ensures.close()
    spawning = asyncio.create_task(
        runtime.spawn(_capturing(built, _done()), query='go', parent_id=bravo)
    )
    await asyncio.wait_for(broker.ensures.reach(1), _TIMEOUT)
    child = built[0].agent_id
    # 整条级联跑完：钉住全部撤掉，`_doomed` 从此答不出任何东西。
    await asyncio.wait_for(runtime.despawn(root), _TIMEOUT)
    assert runtime._doomed == {}
    broker.ensures.open()

    with pytest.raises(ContractViolation) as refused:
        await asyncio.wait_for(spawning, _TIMEOUT)

    assert child in refused.value.expected
    assert '钉住' in refused.value.received
    assert 'parent_id' in refused.value.next_action
    assert await runtime.find(child) is None
    assert child not in runtime._slots
    assert runtime._table.get(child) is None
    assert (Junction.AGENT_SPAWN, child) not in log
    assert (await runtime.send(child, 'hello')).ok is False
    assert await _dangling_parents(registry) == []
    assert [await runtime.find(agent_id) is not None for agent_id in (other_root, other_child)] == [
        True
    ] * 2
    _ = (alpha, charlie, delta)
    await runtime.shutdown()


async def test_the_self_refusal_pre_pass_finishes_before_the_first_node_comes_down():
    """预检查是独立的一遍，抛在动手之前：半棵拆掉的树没有人能收拾。

    调用者放在深度 1，另有一条更深的兄弟子树：拆除相是 `reversed(BFS)`，把检查挪进拆除循环时
    那三个更深的节点会先被真的拆掉，再停在调用者身上。调用者在最深处时两种写法给同一个答案。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    holder: dict[str, str] = {}
    seen: list[str] = []
    ready = asyncio.Event()

    class _DespawnAncestor(Tool):
        async def execute(self, note: str) -> ToolResult:
            """等测试搭完这棵树，然后试着销毁自己的祖先。"""
            await ready.wait()
            try:
                await runtime.despawn(holder['root'])
            except ContractViolation as error:
                return ToolResult(output=f'refused: {error.received}')
            return ToolResult(output='not refused')

    async def record(**kwargs: Any) -> HookResult:
        seen.append(kwargs['agent_id'])
        return HookResult()

    runtime.hooks.register(Junction.AGENT_DESPAWN, record)
    root = await _resident(runtime, broker)
    holder['root'] = root
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[ToolCall(id='c1', name='DespawnAncestor', arguments='{"note":"bye"}')]
            ),
            ScriptedTurn(content='still alive'),
        ]
    )
    # 调用者在深度 1 且**先于** bravo 注册：广度优先的答案是 [caller, bravo, deep, deeper]，
    # 因此独立的一遍预检查第一个就撞上它，而拆除相的逆序里它排在最后。
    caller = await runtime.spawn(
        _factory(llm, tools={'DespawnAncestor': _DespawnAncestor()}), query='go', parent_id=root
    )
    bravo = await _resident(runtime, broker, parent_id=root)
    deep = await _resident(runtime, broker, parent_id=bravo)
    deeper = await _resident(runtime, broker, parent_id=deep)
    ready.set()

    ctx = await asyncio.wait_for(runtime.wait(caller), _TIMEOUT)

    tool_blocks = [block for block in ctx.current_traj if block.role == 'tool']
    assert len(tool_blocks) == 1
    assert isinstance(tool_blocks[0].content, str)
    assert tool_blocks[0].content.startswith('refused: ')
    tree = (root, caller, bravo, deep, deeper)
    assert [await runtime.find(agent_id) is not None for agent_id in tree] == [True] * 5
    assert registry.unregisters == []
    assert seen == []
    assert runtime._doomed == {}
    await runtime.shutdown()


async def test_reviving_a_zombie_that_sits_in_a_doomed_subtree_is_refused(
    caplog: pytest.LogCaptureFixture,
):
    """复活一个正在被拆的节点，它的驱动会在条目被摘掉时失去句柄——一个永不被取消的孤儿。

    这道守卫够得着，需要一个「已经是 ZOMBIE、又落在被拆的树里」的节点：钉住相在遍历返回之后
    才落，所以窗口只能架在被点名的那一个身上。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go')
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    task = runtime._table.get_task(agent_id)
    registry.hold_children_of = agent_id
    registry.walks.close()
    despawning = asyncio.create_task(runtime.despawn(agent_id))
    await asyncio.wait_for(registry.walks.reach(1), _TIMEOUT)
    caplog.set_level(logging.INFO)

    assert await runtime.ensure_active(agent_id) is False

    # 复活写下去的那个 IDLE 必须收回：留着它就是宣布「它能处理消息」，而 zombie 才是复活的凭据。
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    assert runtime._table.get_task(agent_id) is task
    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert caplog.records[0].args == (agent_id,)
    registry.walks.open()
    await asyncio.wait_for(despawning, _TIMEOUT)
    assert await runtime.find(agent_id) is None
    await runtime.shutdown()


async def test_the_despawn_junction_fires_only_after_every_trace_is_already_gone():
    """`AGENT_DESPAWN` 报的是既成事实，所以想留下父子关系只能在 `AGENT_SPAWN` 上记。

    这条契约本身就是「hook 里读不到条目」，因此只有在 hook 里当场问才验得出来：在外面事后问，
    触发点挪到摘条目**之前**同样答「不在了」。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    observed: list[tuple[str, bool, bool, bool]] = []

    async def look_around(**kwargs: Any) -> HookResult:
        agent_id = kwargs['agent_id']
        observed.append(
            (
                agent_id,
                await runtime.find(agent_id) is None,
                agent_id not in runtime._slots,
                runtime._table.get(agent_id) is None,
            )
        )
        return HookResult()

    runtime.hooks.register(Junction.AGENT_DESPAWN, look_around)
    root = await _resident(runtime, broker)
    bravo = await _resident(runtime, broker, parent_id=root)
    delta = await _resident(runtime, broker, parent_id=bravo)

    await runtime.despawn(root)

    assert observed == [
        (delta, True, True, True),
        (bravo, True, True, True),
        (root, True, True, True),
    ]
    await runtime.shutdown()


async def test_kill_reaches_aborting_from_every_status_this_process_calls_live():
    """`kill` 的 `allowed` 必须是 `_LIVE_STATUSES` 本体，不是一份手写的子集。

    基数由那个元组自己给出：漏掉的成员今天可能还没有写者（`WAITING`），但手写子集会让它落地那天
    静默转不过去——而 `kill` 刻意不为转换失败留痕，末态又被 `_finish` 那一次 CAS 兜回 zombie，
    于是只有走过的状态路径能把这件事说出来。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    blockers: list[_Block] = []
    agents = [await _running(runtime, blockers) for _ in _LIVE_STATUSES]
    for agent_id, status in zip(agents, _LIVE_STATUSES, strict=True):
        assert await registry.compare_and_set_status(
            agent_id, expected=AgentStatus.RUNNING, new=status
        )

    for agent_id in agents:
        await runtime.kill(agent_id)
    await asyncio.wait_for(_reaped(runtime), _TIMEOUT)

    assert [
        (status, AgentStatus.ABORTING) in registry.applied_of(agent_id)
        for agent_id, status in zip(agents, _LIVE_STATUSES, strict=True)
    ] == [True] * len(_LIVE_STATUSES)
    assert [await _status(runtime, agent_id) for agent_id in agents] == [AgentStatus.ZOMBIE] * len(
        _LIVE_STATUSES
    )
    await runtime.shutdown()


async def test_despawn_clears_the_context_of_an_agent_whose_entry_someone_else_already_removed():
    """痕迹检查看的是**全部**痕迹，不只是注册表条目。

    只看注册表就会在条目已经被别的进程摘掉时早退，把 slot 与实例留在本进程里——而模块文档明写
    「本类持有的每一份 Context 都对应一个仍可寻址的 Agent」。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    seen: list[str] = []

    async def record(**kwargs: Any) -> HookResult:
        seen.append(kwargs['agent_id'])
        return HookResult()

    runtime.hooks.register(Junction.AGENT_DESPAWN, record)
    agent_id = await runtime.spawn(_factory(_done()), query='go')
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    await registry.unregister(agent_id)

    await runtime.despawn(agent_id)

    assert agent_id not in runtime._slots
    assert runtime._table.get(agent_id) is None
    assert seen == [agent_id]
    await runtime.shutdown()


async def test_a_matching_reply_answers_the_wait_and_never_reaches_the_turn_loop():
    """答复由 take_by_correlation 从邮箱取走，因此不会再作为普通消息注入一遍。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(Reply(timeout=_UNREACHED_WAIT, correlation_id='r1'))
    agent_id = await _waiting(runtime, broker, tool)

    assert (await runtime.send(agent_id, 'the answer', correlation_id='r1')).ok
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.ANSWERED]
    answered = tool.results[0].message
    assert answered is not None and answered.content == 'the answer'
    tool.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert [block.content for block in ctx.current_traj if block.role == 'user'] == ['go']
    assert ctx.stop_reason is StopReason.COMPLETED
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_the_registry_says_waiting_while_the_turn_is_suspended_and_running_again_after():
    """WAITING 是「轮被挂起」：注册表要在这一整段里说 waiting，等待一结束就回 running。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(UserInput(timeout=_BRIEF_WAIT))
    agent_id = await _waiting(runtime, broker, tool)

    assert await _status(runtime, agent_id) is AgentStatus.WAITING
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.TIMEOUT]
    assert tool.results[0].message is None
    assert await _status(runtime, agent_id) is AgentStatus.RUNNING
    tool.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert ctx.stop_reason is StopReason.COMPLETED
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_second_wait_preempts_the_first_and_stays_waiting_after_it_returns():
    """恢复 RUNNING 归**当下**那个等待者。

    被顶掉的那次先返回，它若也去恢复，注册表就在顶掉它的那次等待还挂着的时候宣布这个 Agent 在跑
    ——而它悬在一次等待上，只有一条匹配的答复救得回来。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(UserInput(timeout=_UNREACHED_WAIT, request_id='req-1'))
    agent_id = await _waiting(runtime, broker, tool)

    second = asyncio.create_task(
        runtime.wait_for(agent_id, UserInput(timeout=_UNREACHED_WAIT, request_id='req-2'))
    )
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.PREEMPTED]
    assert await _status(runtime, agent_id) is AgentStatus.WAITING
    assert (await runtime.send(agent_id, 'late answer', correlation_id='req-2')).ok
    assert (await asyncio.wait_for(second, _TIMEOUT)).state is WaitState.ANSWERED
    assert await _status(runtime, agent_id) is AgentStatus.RUNNING
    tool.release.set()
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_abort_while_waiting_reports_aborted_rather_than_hanging_to_the_timeout():
    """中止位只在轮的边界上被读到，而这次等待卡在轮的中间，超时可以是几小时。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(UserInput(timeout=_UNREACHED_WAIT))
    agent_id = await _waiting(runtime, broker, tool)

    await runtime.abort(agent_id)
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.ABORTED]
    assert await _status(runtime, agent_id) is AgentStatus.ABORTING
    tool.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert ctx.stop_reason is StopReason.ABORTED
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_an_answer_that_lands_together_with_an_abort_is_reported_answered():
    """消息已经离开邮箱了，此刻丢掉它就是真的丢了；中止照旧在下一个轮边界兑现。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(Reply(timeout=_UNREACHED_WAIT, correlation_id='r1'))
    agent_id = await _armed(runtime, broker, tool)
    broker.hold_take_of = agent_id
    broker.take_hold.close()
    await _started(runtime, broker, tool, agent_id)

    assert (await runtime.send(agent_id, 'the answer', correlation_id='r1')).ok
    broker.take_hold.open()
    await runtime.abort(agent_id)
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.ANSWERED]
    answered = tool.results[0].message
    assert answered is not None and answered.content == 'the answer'
    tool.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert ctx.stop_reason is StopReason.ABORTED
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_an_aborted_wait_leaves_the_unclaimed_answer_in_the_mailbox():
    """被中断的等待取消那次取，而取消不得吃掉消息——它退回成一条普通消息等下一轮。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(Reply(timeout=_UNREACHED_WAIT, correlation_id='r1'))
    agent_id = await _armed(runtime, broker, tool)
    broker.hold_take_of = agent_id
    broker.take_hold.close()
    await _started(runtime, broker, tool, agent_id)

    assert (await runtime.send(agent_id, 'the answer', correlation_id='r1')).ok
    await runtime.abort(agent_id)
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.ABORTED]
    assert await broker.wait_for_messages(agent_id, 0) is True
    assert [
        task.get_name() for task in asyncio.all_tasks() if task.get_name().startswith('wait:')
    ] == []
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_refused_wait_leaves_the_one_already_in_flight_alone():
    """拒绝意味着什么都没发生：被拒的调用不该顶掉在飞的那个等待。

    用一个名单之外的等待类型触发拒绝，因为 `correlation_of` 在任何状态改动之前就抛——而 paused 与
    aborting 都**不**再被拒（拒绝会让 ContractViolation 从工具里上抛、把整个 run 打成 error）。
    """

    class _Unregistered(Waiter):
        """名单之外的等待类型：`correlation_of` 当场报错。"""

    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(Reply(timeout=_UNREACHED_WAIT, correlation_id='r1'))
    agent_id = await _waiting(runtime, broker, tool)
    token = runtime._slots[agent_id].waiter

    with pytest.raises(ContractViolation):
        # 套一层上限：拒绝丢掉的话这次调用会真的等满 _UNREACHED_WAIT，回归表现成挂死而不是红色断言。
        await asyncio.wait_for(
            runtime.wait_for(agent_id, _Unregistered(timeout=_UNREACHED_WAIT)), _TIMEOUT
        )

    # 同步读令牌：这一问答的是「这次调用有没有顶掉它」，等那次等待自己返回再看结局的话，答案取决
    # 于答复与抢占谁先被调度到。
    assert token is not None and not token.raised.done()
    assert runtime._slots[agent_id].waiter is token
    assert tool.results == []
    assert (await runtime.send(agent_id, 'the answer', correlation_id='r1')).ok
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)
    assert [result.state for result in tool.results] == [WaitState.ANSWERED]
    tool.release.set()
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_wait_started_after_an_abort_landed_mid_turn_answers_aborted():
    """abort 落在轮中间、在等待发起之前：这次等待交回 ABORTED，而不是把整个 run 打成 error。

    `abort` 的 docstring 承诺这一次工作以 `StopReason.ABORTED` 收场。从这里抛 `ContractViolation`
    会让它原样穿过 tool_executor，run 以 error 结束、`resume` 也救不回来，而这条路的窗口是整整
    一轮（中止只在轮边界上兑现）。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(UserInput(timeout=_UNREACHED_WAIT))
    agent_id = await _armed(runtime, broker, tool)
    tool.before = lambda: runtime.abort(agent_id)

    assert (await runtime.send(agent_id, 'go')).ok
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.ABORTED]
    assert tool.results[0].message is None
    # 不写注册表：后置条件本来就是 aborting。
    assert await _status(runtime, agent_id) is AgentStatus.ABORTING
    # 也没向 broker 发一次注定要被取消的请求。
    assert broker.takes(agent_id) == 0
    tool.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert ctx.stop_reason is StopReason.ABORTED
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_wait_started_after_a_pause_landed_mid_turn_keeps_the_agent_resumable():
    """pause 落在轮中间、在等待发起之前：照常等，但不写注册表——否则 resume 再也落不下来。

    `resume` 的 CAS 只收 PAUSED。把状态改成 WAITING 会让它失败，用户此后没有任何操作能把这个
    Agent 叫回来；而从这里抛 `ContractViolation` 会把整个 run 打成 error。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(Reply(timeout=_UNREACHED_WAIT, correlation_id='r1'))
    agent_id = await _armed(runtime, broker, tool)
    tool.before = lambda: runtime.pause(agent_id)

    assert (await runtime.send(agent_id, 'go')).ok
    await asyncio.wait_for(broker.reach_take(agent_id, 1), _TIMEOUT)

    # 等待真的挂上去了（broker 被问到了），而注册表仍说 paused。
    assert await _status(runtime, agent_id) is AgentStatus.PAUSED
    assert runtime._slots[agent_id].waiter is not None
    assert tool.results == []
    await runtime.resume(agent_id)
    # 解除挂起要看在飞的等待：它此刻仍挂着，回 RUNNING 等于宣布这一轮在往前走。
    assert await _status(runtime, agent_id) is AgentStatus.WAITING
    assert (await runtime.send(agent_id, 'the answer', correlation_id='r1')).ok
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.ANSWERED]
    assert await _status(runtime, agent_id) is AgentStatus.RUNNING
    tool.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert ctx.stop_reason is StopReason.COMPLETED
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_wait_started_while_paused_still_stops_at_the_next_turn_boundary():
    """不写注册表不等于把挂起请求也丢了：它留在控制块里，这一轮照旧停在下一个轮边界上。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(Reply(timeout=_UNREACHED_WAIT, correlation_id='r1'))
    agent_id = await _armed(runtime, broker, tool)
    tool.before = lambda: runtime.pause(agent_id)

    assert (await runtime.send(agent_id, 'go')).ok
    await asyncio.wait_for(broker.reach_take(agent_id, 1), _TIMEOUT)
    assert (await runtime.send(agent_id, 'the answer', correlation_id='r1')).ok
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)
    tool.release.set()

    control = runtime._slots[agent_id].control
    assert await asyncio.wait_for(control.wait_until_parked(_TIMEOUT), _TIMEOUT) is True
    assert await _status(runtime, agent_id) is AgentStatus.PAUSED
    await runtime.resume(agent_id)
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert ctx.stop_reason is StopReason.COMPLETED
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_wait_for_an_agent_this_process_holds_as_a_zombie_is_refused():
    """zombie 手上没有一轮活可以挂起，而它的注册表状态正是复活它的凭据。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    agent_id = await runtime.spawn(_factory(_done()), query='go', exit_mode=ExitMode.ZOMBIE)
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE

    with pytest.raises(ContractViolation) as info:
        await asyncio.wait_for(
            runtime.wait_for(agent_id, UserInput(timeout=_UNREACHED_WAIT)), _TIMEOUT
        )

    assert AgentStatus.ZOMBIE.value in info.value.received
    assert 'ensure_active' in info.value.next_action
    assert runtime._slots[agent_id].waiter is None
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_wait_for_an_agent_kill_left_in_aborting_answers_aborted():
    """判据是注册表状态，不是本地那一位：`kill` 写下 aborting 却**不**武装中止位。

    拿本地位当判据的话这条路会抛 `ContractViolation`——而 kill 之后手上这一轮确实以中止收场，
    那个异常会把整个 run 打成 error。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    blockers: list[_Block] = []
    agent_id = await _running(runtime, blockers)
    broker.hold_removal_of = agent_id
    broker.removal_hold.close()

    await runtime.kill(agent_id)
    await asyncio.wait_for(broker.removal_hold.reach(1), _TIMEOUT)
    assert await _status(runtime, agent_id) is AgentStatus.ABORTING
    assert runtime._slots[agent_id].control.abort_requested is False

    result = await asyncio.wait_for(
        runtime.wait_for(agent_id, UserInput(timeout=_UNREACHED_WAIT)), _TIMEOUT
    )

    assert result.state is WaitState.ABORTED
    assert runtime._slots[agent_id].waiter is None
    assert broker.takes(agent_id) == 0
    broker.removal_hold.open()
    await asyncio.wait_for(_reaped(runtime), _TIMEOUT)
    for blocker in blockers:
        blocker.release.set()
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_the_waiter_timeout_reaches_the_broker_unclamped():
    """上限来自 `Waiter` 自己（spec §4.2），`wait_for` 不 clamp。

    在这里悄悄夹一道宿主级上限，一次四小时的人工确认会静默变成一小时，而症状只在长跑里现身。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(Reply(timeout=_LONG_WAIT, correlation_id='r1'))
    agent_id = await _waiting(runtime, broker, tool)

    assert broker.take_timeouts == [(agent_id, _LONG_WAIT)]
    assert (await runtime.send(agent_id, 'the answer', correlation_id='r1')).ok
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)
    assert [result.state for result in tool.results] == [WaitState.ANSWERED]
    tool.release.set()
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_pause_does_not_cut_a_wait_short_and_resume_puts_it_back_into_waiting():
    """挂起在轮的边界上兑现，而这次等待在轮的中间；解除挂起要回到 waiting，不是 running。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(Reply(timeout=_UNREACHED_WAIT, correlation_id='r1'))
    agent_id = await _waiting(runtime, broker, tool)

    await runtime.pause(agent_id)
    assert await _status(runtime, agent_id) is AgentStatus.PAUSED
    await runtime.resume(agent_id)

    assert await _status(runtime, agent_id) is AgentStatus.WAITING
    assert tool.results == []
    assert (await runtime.send(agent_id, 'the answer', correlation_id='r1')).ok
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)
    assert [result.state for result in tool.results] == [WaitState.ANSWERED]
    assert await _status(runtime, agent_id) is AgentStatus.RUNNING
    tool.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert ctx.stop_reason is StopReason.COMPLETED
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_wait_that_ends_while_paused_leaves_the_agent_paused():
    """等待结束不代表挂起结束：强行恢复 running 等于抹掉 pause 写下的那次转换。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(Reply(timeout=_UNREACHED_WAIT, correlation_id='r1'))
    agent_id = await _waiting(runtime, broker, tool)

    await runtime.pause(agent_id)
    assert (await runtime.send(agent_id, 'the answer', correlation_id='r1')).ok
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.ANSWERED]
    assert await _status(runtime, agent_id) is AgentStatus.PAUSED
    tool.release.set()
    control = runtime._slots[agent_id].control
    assert await asyncio.wait_for(control.wait_until_parked(_TIMEOUT), _TIMEOUT) is True
    await runtime.resume(agent_id)
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert ctx.stop_reason is StopReason.COMPLETED
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_waiting_for_an_agent_that_is_not_running_is_refused():
    """`wait_for` 是「代表一个正在干活的 Agent 等」；从外面等它结束是 `wait()`。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    agent_id = await _resident(runtime, broker)

    with pytest.raises(ContractViolation) as info:
        await asyncio.wait_for(
            runtime.wait_for(agent_id, UserInput(timeout=_UNREACHED_WAIT)), _TIMEOUT
        )

    assert 'wait()' in info.value.next_action
    assert AgentStatus.IDLE.value in info.value.received
    assert await _status(runtime, agent_id) is AgentStatus.IDLE
    assert runtime._slots[agent_id].waiter is None
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_waiting_for_an_agent_this_process_never_took_over_is_refused():
    """替一个别人持有的条目写下 waiting，谁的 `ensure_active` 都转不回 idle。"""
    registry = _RecordingRegistry()
    runtime = Runtime(registry=registry)
    registry.hold_before = AgentStatus.RUNNING
    registry.switches.close()
    stranded = await runtime.spawn(_factory(_done()), query='go')
    await asyncio.wait_for(registry.switches.reach(1), _TIMEOUT)
    assert runtime._slots[stranded].owned is False
    # 状态摆成 running：所有权凭据是本次拒绝唯一的依据，靠状态挡不住它。写内层注册表是因为外层
    # 那道闸拦的正是「转去 running」，而这里要的就是把状态摆成 running。
    assert await registry._inner.compare_and_set_status(
        stranded, expected=AgentStatus.IDLE, new=AgentStatus.RUNNING
    )

    with pytest.raises(ContractViolation) as info:
        await asyncio.wait_for(
            runtime.wait_for(stranded, UserInput(timeout=_UNREACHED_WAIT)), _TIMEOUT
        )

    assert 'IDLE -> RUNNING' in info.value.received
    assert 'wait()' in info.value.next_action
    assert await _status(runtime, stranded) is AgentStatus.RUNNING
    assert runtime._slots[stranded].waiter is None
    registry.switches.open()
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_waiting_for_an_agent_this_process_does_not_hold_is_refused():
    runtime = Runtime()
    with pytest.raises(ContractViolation) as info:
        await runtime.wait_for('nobody', UserInput(timeout=_UNREACHED_WAIT))
    assert '本进程没有它的实例' in info.value.received
    assert 'wait()' in info.value.next_action
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_waiter_correlation_of_refuses_never_moves_the_agent_into_waiting():
    """校验在改状态之前：非法的 Waiter 换来一次什么都没发生的拒绝，而不是一个停在 waiting 的
    Agent——它此后没有任何答复解得开，只有 abort 救得回来。
    """

    class _Unregistered(Waiter):
        """名单之外的等待类型：`correlation_of` 当场报错，而不是去等一个不会来的答复。"""

    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    blockers: list[_Block] = []
    agent_id = await _running(runtime, blockers)

    with pytest.raises(ContractViolation):
        await asyncio.wait_for(
            runtime.wait_for(agent_id, _Unregistered(timeout=_UNREACHED_WAIT)), _TIMEOUT
        )
    assert await _status(runtime, agent_id) is AgentStatus.RUNNING

    smuggled = Confirmation(timeout=_UNREACHED_WAIT, tool_call_id='call-7')
    object.__setattr__(smuggled, 'tool_call_id', '   ')
    with pytest.raises(ConfigError):
        await asyncio.wait_for(runtime.wait_for(agent_id, smuggled), _TIMEOUT)

    assert await _status(runtime, agent_id) is AgentStatus.RUNNING
    assert runtime._slots[agent_id].waiter is None
    assert broker.takes(agent_id) == 0
    for blocker in blockers:
        blocker.release.set()
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_reply_nobody_is_waiting_for_becomes_an_ordinary_message():
    """关联 ID 只是匹配用的；没人在等它时它就是一条普通消息，不会凭空消失。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    agent_id = await _resident(runtime, broker)

    assert (await runtime.send(agent_id, 'unclaimed answer', correlation_id='r9')).ok
    await asyncio.wait_for(broker.reach(agent_id, 2), _TIMEOUT)

    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert [block.content for block in agent.context.current_traj if block.role == 'user'] == [
        'unclaimed answer'
    ]
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_wait_whose_mailbox_vanished_says_so_instead_of_passing_it_off_as_a_timeout(
    caplog: pytest.LogCaptureFixture,
):
    """`take_by_correlation` 在截止时刻之前答「没有」只有一个原因：邮箱不在了。

    四态由 spec 定死，所以这一支并入 timeout；而交回的 timeout 会被读成「等满了那么久」，一次零秒
    的等待于是看着像一次几十秒的等待——那条 ERROR 是这个区别留下的全部痕迹。
    """
    caplog.set_level(logging.ERROR)
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(UserInput(timeout=_UNREACHED_WAIT, request_id='req-1'))
    agent_id = await _armed(runtime, broker, tool)
    broker.hold_take_of = agent_id
    broker.take_hold.close()
    await _started(runtime, broker, tool, agent_id)

    await broker.remove_queue(agent_id)
    broker.take_hold.open()
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.TIMEOUT]
    assert [record.levelname for record in caplog.records] == ['ERROR']
    reported_args = caplog.records[0].args
    assert isinstance(reported_args, tuple)
    assert reported_args[:3] == (agent_id, 'req-1', _UNREACHED_WAIT)
    remaining = reported_args[3]
    # 严格小于：报出的必须是**还剩多少**，把超时预算原样回声一遍就把这次「零秒等待」说成等满了。
    assert isinstance(remaining, float) and 0 < remaining < _UNREACHED_WAIT
    reported = caplog.records[0].getMessage()
    _assert_three_part(reported)
    assert 'remove_queue' in reported and 'find()' in reported
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_wait_that_runs_its_timeout_out_reports_no_vanished_mailbox(
    caplog: pytest.LogCaptureFixture,
):
    """真的等满超时的那一次不该报「邮箱没了」——否则那条记录在每次超时上都是误报。"""
    caplog.set_level(logging.ERROR)
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(UserInput(timeout=_BRIEF_WAIT))
    agent_id = await _waiting(runtime, broker, tool)

    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.TIMEOUT]
    assert caplog.records == []
    tool.release.set()
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_broker_that_raises_during_a_wait_reaches_the_caller_and_still_restores_running():
    """协作者抛异常这条路也得把状态带回来：留在 waiting 的 Agent 没有任何答复解得开。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    blockers: list[_Block] = []
    agent_id = await _running(runtime, blockers)
    broker.fail_take_of = agent_id

    with pytest.raises(RuntimeError):
        await runtime.wait_for(agent_id, UserInput(timeout=_UNREACHED_WAIT))

    assert await _status(runtime, agent_id) is AgentStatus.RUNNING
    assert runtime._slots[agent_id].waiter is None
    for blocker in blockers:
        blocker.release.set()
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_cancelled_wait_collects_its_helper_task_and_leaves_the_registry_alone():
    """取消路径上不写注册表：归宿由发起取消的那一方在取消之外给出（见 `_reap`）。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    blockers: list[_Block] = []
    agent_id = await _running(runtime, blockers)
    waiting = asyncio.create_task(runtime.wait_for(agent_id, UserInput(timeout=_UNREACHED_WAIT)))
    await asyncio.wait_for(broker.reach_take(agent_id, 1), _TIMEOUT)

    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)

    assert [
        task.get_name() for task in asyncio.all_tasks() if task.get_name().startswith('wait:')
    ] == []
    assert runtime._slots[agent_id].waiter is None
    assert await _status(runtime, agent_id) is AgentStatus.WAITING
    for blocker in blockers:
        blocker.release.set()
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_wait_cut_short_by_shutdown_leaves_the_registry_where_the_cancel_found_it():
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(UserInput(timeout=_UNREACHED_WAIT))
    agent_id = await _waiting(runtime, broker, tool)

    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)

    assert await _status(runtime, agent_id) is AgentStatus.WAITING
    assert tool.results == []
    assert runtime._slots[agent_id].waiter is None


async def test_killing_a_waiting_agent_still_reaches_zombie_and_drops_its_token():
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(UserInput(timeout=_UNREACHED_WAIT))
    agent_id = await _waiting(runtime, broker, tool)

    await asyncio.wait_for(runtime.kill(agent_id), _TIMEOUT)
    await asyncio.wait_for(_reaped(runtime), _TIMEOUT)

    assert await _status(runtime, agent_id) is AgentStatus.ZOMBIE
    assert runtime._slots[agent_id].waiter is None
    assert tool.results == []
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_despawning_a_waiting_agent_removes_the_entry():
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(UserInput(timeout=_UNREACHED_WAIT))
    agent_id = await _waiting(runtime, broker, tool)

    await asyncio.wait_for(runtime.despawn(agent_id), _TIMEOUT)

    assert await runtime.find(agent_id) is None
    assert agent_id not in runtime._slots
    assert runtime._table.get(agent_id) is None
    assert tool.results == []
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_one_agents_preemption_and_abort_leave_another_agents_wait_alone():
    """令牌按 agent 分槽：一次抢占或中止只落在被点名的那个 Agent 上。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    theirs = _Waiting(Reply(timeout=_UNREACHED_WAIT, correlation_id='r1'))
    ours = _Waiting(Reply(timeout=_UNREACHED_WAIT, correlation_id='r2'))
    left = await _waiting(runtime, broker, theirs)
    right = await _waiting(runtime, broker, ours)

    preemptor = asyncio.create_task(runtime.wait_for(left, UserInput(timeout=_UNREACHED_WAIT)))
    await asyncio.wait_for(theirs.returned.wait(), _TIMEOUT)
    await runtime.abort(left)

    assert [result.state for result in theirs.results] == [WaitState.PREEMPTED]
    assert (await asyncio.wait_for(preemptor, _TIMEOUT)).state is WaitState.ABORTED
    assert ours.results == []
    assert await _status(runtime, right) is AgentStatus.WAITING
    assert (await runtime.send(right, 'ours', correlation_id='r2')).ok
    await asyncio.wait_for(ours.returned.wait(), _TIMEOUT)
    assert [result.state for result in ours.results] == [WaitState.ANSWERED]
    assert await _status(runtime, right) is AgentStatus.RUNNING
    theirs.release.set()
    ours.release.set()
    assert (await asyncio.wait_for(runtime.wait(left), _TIMEOUT)).stop_reason is StopReason.ABORTED
    assert (await asyncio.wait_for(runtime.wait(right), _TIMEOUT)).stop_reason is (
        StopReason.COMPLETED
    )
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_wait_token_keeps_the_first_interrupt_and_drops_the_second():
    """直接打令牌：换令牌与打旧令牌之间没有挂起点，所以这条性质只有令牌自己答得出。"""
    token = _WaitToken()
    token.interrupt(WaitState.PREEMPTED)
    token.interrupt(WaitState.ABORTED)
    assert await asyncio.wait_for(token.raised, _TIMEOUT) is WaitState.PREEMPTED


async def test_an_abort_the_registry_refuses_leaves_the_wait_in_flight():
    """记不下 aborting 就不要真的打断那次等待——那会停掉一件谁都看不出被停了的活。"""
    registry = _RecordingRegistry(deny=(AgentStatus.WAITING, AgentStatus.ABORTING))
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    tool = _Waiting(Reply(timeout=_UNREACHED_WAIT, correlation_id='r1'))
    agent_id = await _waiting(runtime, broker, tool)

    with pytest.raises(ContractViolation):
        await runtime.abort(agent_id)

    # 令牌本身没被动过。这一问是同步的，因此它答的是「这次调用有没有打断它」；等那次等待自己
    # 返回再看结局的话，答案取决于答复与中断谁先被调度到。
    still_waiting = runtime._slots[agent_id].waiter
    assert still_waiting is not None and not still_waiting.raised.done()
    assert tool.results == []
    assert runtime._slots[agent_id].control.abort_requested is False
    assert (await runtime.send(agent_id, 'the answer', correlation_id='r1')).ok
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)
    assert [result.state for result in tool.results] == [WaitState.ANSWERED]
    tool.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert ctx.stop_reason is StopReason.COMPLETED
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_an_abort_after_a_non_matching_message_does_not_burn_the_waiters_whole_timeout():
    """取消一次在飞的取消息必须当场生效，否则 abort 要挂满整个 waiter 超时。

    那条**不匹配**的消息是关键：它让 broker 内层那个「等新消息入队」的 future 就绪，而这次取还得
    继续等。内存 store 的 publish 全程不挂起，所以 send 紧跟 abort 是确定命中这个交错，不是概率
    事件——而 `asyncio.wait_for` 恰在这个形状下把到达的取消当成「结果已到」吃掉。

    **这里的 `abort` 不能套 `asyncio.wait_for`**：那会把它包成一个 Task、多出一次调度跳转，broker
    那次取于是在中止到达之前就清掉了 arrival 并重新停稳，窗口不复存在。上界在这里本来也多余——
    内存注册表全程不挂起，`abort` 不可能挂住。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(Reply(timeout=_LONG_WAIT, correlation_id='r1'))
    agent_id = await _waiting(runtime, broker, tool)

    assert (await runtime.send(agent_id, 'not the answer', correlation_id='other')).ok
    await runtime.abort(agent_id)
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.ABORTED]
    # 那条不匹配的消息没被吃掉，仍在普通消费路径上等下一轮。
    assert await broker.wait_for_messages(agent_id, 0) is True
    tool.release.set()
    await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_shutdown_after_a_non_matching_message_does_not_burn_the_waiters_whole_timeout():
    """`shutdown` 等每个驱动任务真的停下来，而驱动任务停在收回那次取消息上。

    取消被吃掉时这条等待边是无界的：它沿自己 create_task 出来的后代走、不可能成环，但时长由
    broker 决定，于是 shutdown 挂满整个 waiter 超时。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(Reply(timeout=_LONG_WAIT, correlation_id='r1'))
    agent_id = await _waiting(runtime, broker, tool)

    assert (await runtime.send(agent_id, 'not the answer', correlation_id='other')).ok
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)

    assert tool.results == []
    assert [
        task.get_name() for task in asyncio.all_tasks() if task.get_name().startswith('wait:')
    ] == []


async def test_a_recursive_abort_cuts_short_a_wait_on_a_descendant():
    """所有权与令牌两道都要在**遍历发现的**主体上生效，不只在被点名的那一个上。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    blockers: list[_Block] = []
    parent = await _running(runtime, blockers)
    tool = _Waiting(Reply(timeout=_LONG_WAIT, correlation_id='r1'))
    child = await _waiting(runtime, broker, tool, parent_id=parent)

    await runtime.abort(parent, recursive=True)
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.ABORTED]
    assert await _status(runtime, child) is AgentStatus.ABORTING
    tool.release.set()
    ctx = await asyncio.wait_for(runtime.wait(child), _TIMEOUT)
    assert ctx.stop_reason is StopReason.ABORTED
    for blocker in blockers:
        blocker.release.set()
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_recursive_resume_puts_a_waiting_descendant_back_into_waiting():
    """递归 resume 同样要看在飞的等待：把子代抹成 running 等于宣布一轮在往前走。"""
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    blockers: list[_Block] = []
    parent = await _running(runtime, blockers)
    tool = _Waiting(Reply(timeout=_UNREACHED_WAIT, correlation_id='r1'))
    child = await _waiting(runtime, broker, tool, parent_id=parent)

    await runtime.pause(parent, recursive=True)
    assert await _status(runtime, child) is AgentStatus.PAUSED
    await runtime.resume(parent, recursive=True)

    assert await _status(runtime, child) is AgentStatus.WAITING
    assert tool.results == []
    assert (await runtime.send(child, 'the answer', correlation_id='r1')).ok
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)
    assert [result.state for result in tool.results] == [WaitState.ANSWERED]
    assert await _status(runtime, child) is AgentStatus.RUNNING
    tool.release.set()
    await asyncio.wait_for(runtime.wait(child), _TIMEOUT)
    for blocker in blockers:
        blocker.release.set()
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_an_abort_interrupts_only_the_named_waiter_out_of_three():
    """基数取到 3 并打中间那个：n=2 时「第一个」与「被点名那个」有一半机会重合。

    令牌逐个同步读，而不是看最终稳定状态——三个等待的最终结局在「打错了主体」下同样各自成立。
    """
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tools = [_Waiting(Reply(timeout=_LONG_WAIT, correlation_id=f'r{index}')) for index in range(3)]
    agents = [await _waiting(runtime, broker, tool) for tool in tools]

    await runtime.abort(agents[1])

    tokens = [runtime._slots[agent_id].waiter for agent_id in agents]
    assert [token is not None and token.raised.done() for token in tokens] == [False, True, False]
    await asyncio.wait_for(tools[1].returned.wait(), _TIMEOUT)
    assert [tool.results for tool in tools] == [[], [WaitResult(state=WaitState.ABORTED)], []]
    for tool in tools:
        tool.release.set()
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_preemption_interrupts_only_the_named_waiters_token_out_of_three():
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tools = [_Waiting(Reply(timeout=_LONG_WAIT, correlation_id=f'r{index}')) for index in range(3)]
    agents = [await _waiting(runtime, broker, tool) for tool in tools]
    first = [runtime._slots[agent_id].waiter for agent_id in agents]

    preemptor = asyncio.create_task(
        runtime.wait_for(agents[1], UserInput(timeout=_LONG_WAIT, request_id='req-1'))
    )
    await asyncio.wait_for(tools[1].returned.wait(), _TIMEOUT)

    assert [token is not None and token.raised.done() for token in first] == [False, True, False]
    assert [tool.results for tool in tools] == [[], [WaitResult(state=WaitState.PREEMPTED)], []]
    preemptor.cancel()
    await asyncio.gather(preemptor, return_exceptions=True)
    for tool in tools:
        tool.release.set()
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_wait_that_was_aborted_and_lost_its_mailbox_reports_aborted_not_timeout(
    caplog: pytest.LogCaptureFixture,
):
    """两件事同时发生时交回的是中断状态：四态的整个意义就是后续动作不同。

    那条「本次交回 timeout」的 ERROR 只属于真的交回 timeout 的那条路——在这里打出来就是让唯一的
    那份证据说谎。
    """
    caplog.set_level(logging.ERROR)
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(UserInput(timeout=_UNREACHED_WAIT, request_id='req-1'))
    agent_id = await _armed(runtime, broker, tool)
    broker.hold_take_of = agent_id
    broker.take_hold.close()
    await _started(runtime, broker, tool, agent_id)

    await broker.remove_queue(agent_id)
    # 开闸与 abort 之间不让出控制权：内存注册表全程不挂起，所以 abort 跑完时那次取已经排上队、
    # 还没跑到「答没有」。两件事于是同时就绪。
    broker.take_hold.open()
    await runtime.abort(agent_id)
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.ABORTED]
    assert caplog.records == []
    tool.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert ctx.stop_reason is StopReason.ABORTED
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_a_broker_failure_a_finished_interrupt_discarded_still_leaves_an_error(
    caplog: pytest.LogCaptureFixture,
):
    """中断胜出而 broker 抛了：异常此后没有调用方，这条 ERROR 是它留下的全部痕迹。

    四态里表示不出「broker 坏了」，而静默丢掉一次真实故障之后，下一次等待会撞上同一个故障。
    """
    caplog.set_level(logging.ERROR)
    broker = _SpyBroker()
    runtime = Runtime(broker=broker)
    tool = _Waiting(UserInput(timeout=_UNREACHED_WAIT, request_id='req-1'))
    agent_id = await _armed(runtime, broker, tool)
    broker.hold_take_of = agent_id
    broker.take_hold.close()
    broker.fail_take_of = agent_id
    await _started(runtime, broker, tool, agent_id)

    broker.take_hold.open()
    await runtime.abort(agent_id)
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.ABORTED]
    assert [record.levelname for record in caplog.records] == ['ERROR']
    reported_args = caplog.records[0].args
    assert isinstance(reported_args, tuple)
    assert reported_args[0] == agent_id
    assert reported_args[1] == 'req-1'
    assert reported_args[2] == 'RuntimeError'
    assert reported_args[4] == WaitState.ABORTED.value
    _assert_three_part(caplog.records[0].getMessage())
    tool.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert ctx.stop_reason is StopReason.ABORTED
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_an_abort_landing_before_a_second_wait_installs_its_token_wins_over_preemption():
    """先到者胜要在集成路径上成立：第二次等待的 CAS 已通过、新令牌还没装上的那段窗口。

    没有这道守卫，被中止的等待会被报成 preempted，而两者的后续动作按 docstring 相反——被顶掉的
    可以重来，而重来会再撞上一次中止。
    """
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    tool = _Waiting(Reply(timeout=_LONG_WAIT, correlation_id='r1'))
    agent_id = await _waiting(runtime, broker, tool)
    first = runtime._slots[agent_id].waiter

    registry.hold_after = AgentStatus.WAITING
    registry.settles.close()
    second = asyncio.create_task(
        runtime.wait_for(agent_id, UserInput(timeout=_LONG_WAIT, request_id='req-2'))
    )
    await asyncio.wait_for(registry.settles.reach(1), _TIMEOUT)
    # CAS 已生效，而槽里装的还是第一个令牌——这段窗口在进程内注册表下根本不存在。
    assert runtime._slots[agent_id].waiter is first

    await runtime.abort(agent_id)
    registry.settles.open()
    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)

    assert [result.state for result in tool.results] == [WaitState.ABORTED]
    assert (await asyncio.wait_for(second, _TIMEOUT)).state is WaitState.ABORTED
    tool.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert ctx.stop_reason is StopReason.ABORTED
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


async def test_an_abort_landing_while_the_wait_installs_itself_still_cuts_it_short():
    """转 waiting 的 CAS 可以在生效之后才返回，那段窗口里的 abort 看不到还没装上的令牌。"""
    registry = _RecordingRegistry()
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    tool = _Waiting(UserInput(timeout=_UNREACHED_WAIT))
    agent_id = await _armed(runtime, broker, tool)
    registry.hold_after = AgentStatus.WAITING
    registry.settles.close()
    assert (await runtime.send(agent_id, 'go')).ok
    await asyncio.wait_for(tool.entered.wait(), _TIMEOUT)
    await asyncio.wait_for(registry.settles.reach(1), _TIMEOUT)
    assert await _status(runtime, agent_id) is AgentStatus.WAITING
    assert runtime._slots[agent_id].waiter is None

    await runtime.abort(agent_id)
    registry.settles.open()

    await asyncio.wait_for(tool.returned.wait(), _TIMEOUT)
    assert [result.state for result in tool.results] == [WaitState.ABORTED]
    # 令牌在装上的那一刻就已经被打过了，因此不向 broker 发那次注定要被取消的请求。
    assert broker.takes(agent_id) == 0
    tool.release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    assert ctx.stop_reason is StopReason.ABORTED
    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)


# ------------------------------------------------- 角色表的运行期校验


@pytest.mark.parametrize(
    ('agents', 'anchor'),
    [
        ({'Worker': 'not-a-factory'}, 'str'),
        ({'Worker': BaseAgent}, 'BaseAgent'),
        ({'Worker': lambda: None}, '<lambda>'),
        ({'Worker': lambda hooks, events, extra: None}, '<lambda>'),
    ],
    ids=['plain-value', 'the-class-itself', 'takes-nothing', 'takes-three'],
)
def test_a_role_whose_value_cannot_be_called_as_a_factory_is_refused_where_it_was_declared(
    agents, anchor
):
    """没有这道校验时，症状是一条指着**别处**的异常：取值要到某次 Delegate 真的派活时才被调用，
    报出的是 runtime.py 里 `factory(hooks, events)` 的 TypeError。
    """
    with pytest.raises(ConfigError) as caught:
        Runtime(agents=agents)

    assert 'Worker' in caught.value.expected
    assert anchor in caught.value.received
    assert 'agents' in caught.value.next_action


@pytest.mark.parametrize(
    'agents',
    [['Worker', 'Planner'], ('Worker',), {'Worker'}, 'Worker'],
    ids=['list', 'tuple', 'set', 'bare-string'],
)
def test_a_role_table_that_is_not_a_mapping_is_refused_instead_of_a_bare_attribute_error(agents):
    """声明式清单里 `agents: [Worker, Planner]` 最顺手，而那里只有名字、没有工厂。

    不拦的话报出的是 `'list' object has no attribute 'items'`——它既不说该写成什么，
    也不说这个参数叫什么。
    """
    with pytest.raises(ConfigError) as caught:
        Runtime(agents=agents)  # pyright: ignore[reportArgumentType]

    assert type(agents).__name__ in caught.value.received
    assert 'agents' in caught.value.next_action


def test_a_role_table_that_is_a_mapping_but_not_a_dict_is_accepted():
    """放宽方向也要钉：收窄成只认 `dict` 会把一个只读映射拒掉，而声明式清单最自然的产物之一
    就是把宿主那份表包成 `MappingProxyType` 交进来。
    """
    runtime = Runtime(agents=MappingProxyType({'Worker': _factory(_done())}))

    assert runtime.agent_names == ('Worker',)


@pytest.mark.parametrize('name', ['', '   '], ids=['empty', 'blank'])
def test_a_blank_role_name_is_refused_because_nothing_can_ever_address_it(name):
    with pytest.raises(ConfigError) as caught:
        Runtime(agents={name: _factory(_done())})

    assert repr(name) in caught.value.received


def test_a_role_name_that_is_not_a_string_is_refused():
    """声明式清单来自 YAML / JSON，那里的键可以是任何标量。"""
    with pytest.raises(ConfigError) as caught:
        Runtime(agents={7: _factory(_done())})  # pyright: ignore[reportArgumentType]

    assert 'int' in caught.value.received


async def test_a_factory_maker_passed_without_calling_it_is_named_at_the_spawn_that_used_it():
    """「忘了加括号」骗得过任何签名检查：一个收两个位置参数的工厂生成器长得就像工厂。

    唯一抓得住它的地方是工厂**交回了什么**，而那条消息必须点名这个工厂——否则报出的是
    `'function' object has no attribute 'agent_id'`，指着 runtime.py。
    """

    def make_worker(name: str, content: str) -> AgentFactory:
        return _factory(_done(content), name=name)

    runtime = Runtime(agents={'Worker': make_worker})  # pyright: ignore[reportArgumentType]
    with pytest.raises(ContractViolation) as caught:
        await runtime.spawn(make_worker, query='go')  # pyright: ignore[reportArgumentType]

    assert 'make_worker' in caught.value.received
    assert 'function' in caught.value.received
    assert 'BaseAgent' in caught.value.expected
    await runtime.shutdown()


async def test_a_factory_that_accepts_two_positional_arguments_is_accepted():
    """放宽方向也要钉：收窄到只认函数会把一个合法的可调用对象拒掉。"""

    class _Callable:
        def __call__(self, hooks: HooksCenter, events: EventBus) -> BaseAgent:
            return _factory(_done())(hooks, events)

    runtime = Runtime(agents={'Worker': _Callable()})

    assert runtime.agent_names == ('Worker',)
    await runtime.shutdown()


async def test_waiting_in_turn_for_an_agent_whose_drive_never_took_over_is_refused():
    """控制面只作用于本进程接管过的 Agent：接管失败的条目归别人所有，本进程手上没有一轮可以挂起。

    放行的话，一次派活会代表一个别人正在跑的 Agent 挂在这里，而没有任何东西能把它叫回来。
    """
    registry = _RecordingRegistry(deny=(AgentStatus.IDLE, AgentStatus.RUNNING))
    broker = _SpyBroker(registry)
    runtime = Runtime(registry=registry, broker=broker)
    stranded = await runtime.spawn(_factory(_done()), query='go')
    with pytest.raises(ContractViolation):
        await runtime.wait(stranded)
    target = await runtime.spawn(_factory(_done()), query='go')

    with pytest.raises(ContractViolation) as caught:
        await runtime._wait_in_turn(stranded, target)

    assert stranded in caught.value.expected
    assert '接管' in caught.value.received
    await runtime.shutdown()


async def test_abort_during_llm_does_not_run_the_declared_tools():
    """第 9 步：模型产出之后、工具执行之前兑现 abort。"""
    entered = asyncio.Event()
    released = asyncio.Event()
    wipe = _Wipe()
    llm = _SlowWipeLLM(entered, released)

    def factory(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=llm,  # pyright: ignore[reportArgumentType]
                tools={'Wipe': wipe},
                system_prompt=_SYSTEM_PROMPT,
            ),
            hooks=hooks,
            events=events,
        )

    runtime = Runtime()
    agent_id = await runtime.spawn(factory, query='go')
    await asyncio.wait_for(entered.wait(), _TIMEOUT)
    await runtime.abort(agent_id)
    released.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)

    assert ctx.stop_reason is StopReason.ABORTED
    assert wipe.paths == []
    await runtime.shutdown()


def test_runtime_rejects_a_registry_class_object():
    """漏括号把构造推迟到第一次 spawn 的裸 TypeError。"""
    with pytest.raises(ConfigError) as caught:
        Runtime(registry=InMemoryAgentRegistry)  # pyright: ignore[reportArgumentType]
    assert '实例' in caught.value.expected
    assert 'InMemoryAgentRegistry' in caught.value.received
    assert '括号' in caught.value.next_action


def test_runtime_rejects_a_broker_that_is_not_a_message_broker():
    with pytest.raises(ConfigError) as caught:
        Runtime(broker=object())  # pyright: ignore[reportArgumentType]
    assert 'MessageBroker' in caught.value.expected
    assert 'object' in caught.value.received
    assert caught.value.next_action


async def test_runtime_accepts_constructed_registry_and_broker_instances():
    runtime = Runtime(registry=InMemoryAgentRegistry(), broker=InMemoryMessageBroker())
    agent_id = await runtime.spawn(_factory(_done()), query='go')
    assert await runtime.find(agent_id) is not None
    await runtime.shutdown()


async def test_spawn_rejects_metadata_that_cannot_be_deepcopied():
    runtime = Runtime()
    try:
        with pytest.raises(ContractViolation) as caught:
            await runtime.spawn(
                _factory(_done()),
                query='go',
                metadata={'conn': threading.Lock()},
            )
        assert "metadata['conn']" in caught.value.received
        assert caught.value.next_action
    finally:
        await runtime.shutdown()


async def test_shutdown_triggers_runtime_shutdown_only_once():
    """拆除每一步都幂等；触发只报一次。"""
    seen: list[str] = []

    async def record(**kwargs: Any) -> HookResult:
        seen.append('shutdown')
        return HookResult()

    runtime = Runtime()
    runtime.hooks.register(Junction.RUNTIME_SHUTDOWN, record)
    await runtime.shutdown()
    await runtime.shutdown()
    assert seen == ['shutdown']


async def test_concurrent_shutdown_triggers_runtime_shutdown_only_once():
    seen: list[str] = []

    async def record(**kwargs: Any) -> HookResult:
        seen.append('shutdown')
        return HookResult()

    runtime = Runtime()
    runtime.hooks.register(Junction.RUNTIME_SHUTDOWN, record)
    await asyncio.gather(runtime.shutdown(), runtime.shutdown(), runtime.shutdown())
    assert seen == ['shutdown']


async def test_second_shutdown_still_closes_the_bus_while_the_first_is_in_the_hook():
    """第二次不得在方法开头见 `_closed` 就返回：那会把关闭总线这一步跳过。"""
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def stall(**kwargs: Any) -> HookResult:
        entered.set()
        await hold.wait()
        return HookResult()

    runtime = Runtime()
    runtime.hooks.register(Junction.RUNTIME_SHUTDOWN, stall)
    first = asyncio.create_task(runtime.shutdown())
    await asyncio.wait_for(entered.wait(), _TIMEOUT)
    await runtime.shutdown()
    probe = runtime.events.subscribe()
    await runtime.events.emit(Event(kind='probe', agent_id='none'))
    got: list[Event] = []
    try:
        got.append(await asyncio.wait_for(anext(probe), timeout=_BRIEF_WAIT))
    except (StopAsyncIteration, TimeoutError):
        pass
    assert got == []
    hold.set()
    await first


async def test_kill_then_despawn_does_not_error_when_the_zombie_transition_loses_the_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """kill 立刻返回、收尾在后台、再 despawn：条目不在是正常顺序。"""
    caplog.set_level(logging.DEBUG, logger='syndica.kernel.runtime')
    blocker = _Block()
    runtime = Runtime()
    agent_id = await runtime.spawn(_blocking(blocker), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    await runtime.kill(agent_id)
    await runtime.despawn(agent_id)
    await asyncio.wait_for(_reaped(runtime), _TIMEOUT)

    assert await runtime.find(agent_id) is None
    messages = [record.getMessage() for record in caplog.records]
    assert any('已经被 despawn 掉了' in message for message in messages)
    assert not any(
        record.levelno >= logging.ERROR and '已经被 despawn' in record.getMessage()
        for record in caplog.records
    )
    await runtime.shutdown()
