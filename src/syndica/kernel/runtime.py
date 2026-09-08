"""Runtime：Agent 生命周期与控制面。

按操作系统进程模型设计：Runtime 类比 init 系统，Agent 类比进程。Agent 不管理自己的生命周期——
外层由本类驱动，内层只跑 LLM + 工具循环。

**驱动任务是一个事件循环，不是一次调用。** spec §4.1 把 IDLE 定义成「已注册，事件循环在等消息」，
所以一个 Agent 的常态是**等**，一条消息触发一个周期。周期与周期之间 Context 不重建、轮次不重置
（spec §5.1）——ZOMBIE 留在注册表可寻址，正是为了「一条消息就能让它带着上下文继续干」。

**只有一条消息消费路径。** 中途插话与唤醒都由同一道门（`BrokerTurnGate`）在轮开始前消费。两条
路径分开写必然分叉，而分叉出来的差异只在有人插话时才现身。

**每一处状态转换都只走 CAS，且失败都必须留痕。** `_switch` 逐个试 CAS 而不是「读一次再写」：读与
写之间的窗口正是 CAS 存在的理由。周期边界上的转换一律走 `_oscillate`（开周期是 RUNNING，停在邮箱
上报什么状态见 `_parked_status`），它的 `allowed` 覆盖控制面能在周期之间留下的**每一个**活状态
（pause / wait_for / abort 各自留下的那些），因此被拒只剩
一种解释：条目已被摘掉，或已被别人送进终态。接管失败意味着这个 Agent 已归别人所有，本进程既不跑它
也不替它清理；其余各处失败意味着循环还在跑而注册表说着另一套——丢掉这个结论就是丢掉「它为什么是
compare-and-set」，而那次真的丢失的转换零痕迹。

**`_finish` 先摘队列再改状态。** 这两步之间落进来的 `send` 会拿到 `ok=False`（队列没了），调用方
于是收到一次诚实的失败；反过来先改状态，那条消息会成功投进一个即将消失的队列，静默丢掉。
**丢数据是静默的、灾难性的；失败是显式的、可恢复的。**

**拿过 `ok=True` 收据的输入不得随队列一起消失。** 收据的用途是让调用方分辨可重试的瞬时失败与永久
失败，而一条「投递成功了但永远不会被处理」的消息把这个能力整个抹掉。因此非 IDLE 退出模式只在
**没有未处理输入**时才结束循环——邮箱里剩下的消息与暂存的延迟消息是同一件事的两个落点。唯一处理不
了的是预算耗尽：一个没有轮次的 Agent 产不出新的一轮，再开周期只会被同一条消息反复唤醒又立刻返回，
一个不让出控制权的空转。那条路因此无条件退出，代价由 `_finish` 记成 ERROR——**丢数据可以，静默不
可以**。

**没有输入就不开周期。** 邮箱可能在「有消息」与门的 `consume` 之间被摘掉，此时周期的第一轮会发出
一个只有 system prompt 的请求，在轨迹里留下一块没有前置 user 块的 assistant 块。轨迹是要持久化的，
这块坏前缀会让此后每一次 provider 请求都非法，而故障在下一次 resume 才现身。

**取消不做收尾工作。** 驱动任务被取消时，`finally` 里既不改注册表也不摘资源：取消可能落在轮的
中间，那时轨迹里留着不配对的调用，而 spec 把「收尾」定义为一个**有状态**的动作。收尾由发起取消
的那一方在取消之外完成，因此它不必在一个正在被取消的任务里 await。`kill` 因此把收尾放进一个独立
的收割任务——它也是那条路上**唯一**保证得了归宿的地方。

**只有一条拆除顺序。** `kill` 的收尾、`despawn`、`ExitMode.EXIT` 三条路都走 `_destroy_tree` →
`_destroy`。各写一份清理迟早会在「先摘队列还是先注销」这种地方分叉，而分叉出来的差异只在并发投递
恰好落进那个窗口时才现身。分工按「删不删注册表条目」切开：`kill` 不删（送进 ZOMBIE，条目还在，
子代不会指向空洞，因此不需要级联）；`despawn` 一定删，所以**必须**级联到全部子代，子先于父。

**条目要么和它的子树一起走，要么整棵树都留着。** `AgentRegistry.unregister` 只把自己从父的
children 上摘掉、不动自己的子代，于是删掉一个有子代的父会留下一串 `parent_id` 指着不存在的条目：
`children_of` 从此永远漏掉它们，而它们自己看起来还有个父。这是数据完整性问题不是策略，所以级联
没有开关。**级联先把整棵子树钉住再动手**：边走边拆的话，中间节点一旦注销，它下面的条目就再也走不
到（`children_of` 只能从父往下问）。钉住期间 `spawn` 拒绝往树上挂新子代、`ensure_active` 拒绝复活
树里的节点——后者若放行，复活出来的驱动会在条目被摘掉时失去句柄，成为永不被取消的孤儿。

**`_slots` 的键恒为实例表键的子集。** EXIT 把队列、注册表条目、实例表条目与 slot 一起摘掉，于是
本类持有的每一份 Context 都对应一个仍可寻址的 Agent，随时间无界增长的只有活着的 Agent 数。代价
写在 `wait` 的契约里：被 reap 的 Agent 没有 waitpid 可言。

**递归的控制面操作对被点名的那一个抛，对遍历到的子代只记日志。** 子代可能是 ZOMBIE、可能正在
被拆，为第一个冲突抛异常只会留下一棵改了一半的树，而调用方无从得知改到哪了；被点名的那一个意图
无歧义，照常抛。CAS 失败同样按这条处理。遍历（`_descendants`）广度优先且带环保护——注册表可以是
远端的，一个环会让这个公共前置永不返回。**遍历穿过本进程不持有的节点，但不作用在它身上**：树在
共享注册表里，本进程持有哪些实例与树的形状无关，在第一个外人那里截断会漏掉它下面属于我们的整片
子树。

**控制面只作用于本进程接管过的 Agent。** 状态写进共享注册表，而这里的门只按得住本进程的事件循环。
替一个接管失败的条目写下 PAUSED，真正的持有者的 `ensure_active` 从此转不回 IDLE，而投递方仍在拿
`ok=True` 的收据——一次谁都叫不醒的静默失联。

**激活只有一个入口。** 复活 ZOMBIE、重开事件循环、日后的「从落盘会话恢复」在语义上是同一件事，
因此都走 `ensure_active`；`send` 的按需激活经 broker 的 `on_missing_queue` 回到同一个方法。让每个
调用方自己「先判断再激活」会让复活有 N 份实现。**判定「它已经活着」看驱动任务，不看注册表状态**：
停在邮箱上的 Agent 是 IDLE，而 IDLE -> RUNNING 的 CAS 对第二个驱动任务照样成功，拿状态当所有权凭据
会让同一个邮箱被两道门 consume，而 consume 恰好一次——输的那道门静默收不到任何消息。

**`shutdown` 是终态。** 关闭后的事件总线静默丢弃 emit，此后 spawn 出来的 Agent 会照常干完活却完全
不可观测，因此 spawn 在关闭后报错。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from syndica.kernel.agent import BaseAgent
from syndica.kernel.agent_types import AgentInfo, AgentStatus, ExitMode
from syndica.kernel.context import Context, StopReason
from syndica.kernel.control import BrokerTurnGate, ControlBlock
from syndica.kernel.errors import ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter, Junction
from syndica.kernel.local_table import LocalAgentTable
from syndica.kernel.message import DeliveryResult, Message, Trigger
from syndica.kernel.protocols import AgentRegistry, MessageBroker
from syndica.kernel.stores import InMemoryAgentRegistry, InMemoryMessageBroker

__all__ = ['AgentFactory', 'Runtime']

logger = logging.getLogger(__name__)

AgentFactory = Callable[[HooksCenter, EventBus], BaseAgent]

# 「这一次 send 允不允许把目标叫醒」。用 ContextVar 而不是 Runtime 上的一个字段：它的作用域是
# **这一次调用**，两个并发 send 的意图因此互不污染；放在实例上则第二个 send 会读到第一个的开关。
# 代价是 broker 必须让回调看得见调用方 publish 时的上下文；这条已经是 MessageBroker 的明文契约
# 并由契约套件强制，见 protocols/broker.py。
_ACTIVATION_ALLOWED: ContextVar[bool] = ContextVar('syndica_activation_allowed', default=True)

# 本进程的事件循环认得的活状态：从其中任何一个都可以被它转去另一个活状态或 ZOMBIE。不在这里的
# 就是终态，本进程不得把它带回来。周期边界的 `allowed` 由本元组派生，漏一个就是「某个状态下
# 静默转不过去」。
_LIVE_STATUSES: tuple[AgentStatus, ...] = (
    AgentStatus.IDLE,
    AgentStatus.RUNNING,
    AgentStatus.PAUSED,
    AgentStatus.WAITING,
    AgentStatus.ABORTING,
)

# 挂起要求「有一个事件循环可以停下来」。这一个元组同时决定 `pause` 的早退拒绝与它那次 CAS 的
# expected 集合：拆成两份写，两份就会各自漂移，而漂移的表现是「拒绝的理由说得通、CAS 却静默转不
# 过去」，或者反过来——放行了一个转换不进去的状态，报出的却是一句关于 CAS 的话。
_PAUSABLE_STATUSES: tuple[AgentStatus, ...] = (
    AgentStatus.IDLE,
    AgentStatus.RUNNING,
    AgentStatus.WAITING,
)


@dataclass
class _Slot:
    """本进程为一个 Agent 额外持有的东西。

    实例与任务在 `LocalAgentTable`，可序列化状态在 `AgentRegistry`；这里放的是两边都不该管的
    第三样：这轮工作结束后该去哪（`exit_mode`）、控制块与门，以及**本进程的驱动任务到底接管过
    它没有**（`owned`——接管失败与从未 spawn 是两回事，`wait` 要分开答）。
    """

    exit_mode: ExitMode
    control: ControlBlock
    gate: BrokerTurnGate
    owned: bool = False
    # 事件循环此刻停在邮箱上（>0）还是在往前走（0）。注册表答不出这件事：停在邮箱上的 PAUSED
    # 与停在挂起门上的 PAUSED 是同一个取值，而只有后者收得到唤醒边沿。读它与改它都是同步的，
    # 因此控制面在一段不含 await 的代码里读到的答案与驱动任务此刻的位置严格一致。
    parked_on_mailbox: int = 0


def _parked_status(slot: _Slot) -> AgentStatus:
    """事件循环停到邮箱上时，注册表该说的那个状态。

    门落着就说 PAUSED：一次穿透挂起门的 `abort` 会把状态从 ABORTING 带回这一点，而此刻写 IDLE
    等于宣布「它能处理消息」——门还落着，它一条都不会处理，而 `send` 照答 ok=True。
    """
    return AgentStatus.PAUSED if slot.control.paused else AgentStatus.IDLE


def _parent_being_destroyed(parent_id: str) -> ContractViolation:
    return ContractViolation(
        expected=f'parent_id {parent_id!r} 不在被拆的过程中',
        received=(
            '它正在被 despawn（或以 ExitMode.EXIT 收尾），它的整棵子树都要消失：'
            '此刻挂上去的子代会带着一个指向已注销条目的 parent_id 留在注册表里，'
            'children_of 从此永远漏掉它，而它自己看起来还有个父'
        ),
        next_action=(
            '等这次拆除结束——用 find() 确认那个父已经不在注册表里——再决定把这个 Agent 挂到哪个'
            '父下面；它不需要父就把 parent_id 置为 None'
        ),
    )


def _already_shut_down() -> ContractViolation:
    return ContractViolation(
        expected='Runtime 尚未 shutdown，spawn 出来的 Agent 事件仍有订阅者能收到',
        received=(
            'shutdown() 已经跑过：事件总线已关闭，此后 emit 一律静默丢弃，'
            '这个 Agent 会照常干完活却完全不可观测'
        ),
        next_action='要再跑 Agent 就新建一个 Runtime；shutdown 是终态，同一个 Runtime 不重开',
    )


class Runtime:
    """Agent 的生命周期驱动与控制面。"""

    def __init__(
        self,
        *,
        registry: AgentRegistry | None = None,
        broker: MessageBroker | None = None,
    ) -> None:
        self.registry: AgentRegistry = registry or InMemoryAgentRegistry()
        self.broker: MessageBroker = broker or InMemoryMessageBroker()
        self.events: EventBus = EventBus()
        self.hooks: HooksCenter = HooksCenter()
        self._table: LocalAgentTable[BaseAgent] = LocalAgentTable()
        self._slots: dict[str, _Slot] = {}
        # 收尾在事件循环之外做，因此它自己是一个任务；不在实例表里，`shutdown` 的取消名单看不见
        # 它们，只能另等一次。
        self._reapers: set[asyncio.Task[None]] = set()
        # 此刻有收尾在飞的 Agent。一个 Agent 同时只有一次收尾：两次并发的收尾会各自走一遍级联，
        # 而它们在拆同一个节点时都会先看到「它还在」，于是同一个 Agent 被报两遍 despawn。
        self._reaping: set[str] = set()
        self._doomed: set[str] = set()
        self._closed = False
        # 「队列不存在」是懒启动与 ZOMBIE 复活的唯一触发点（spec §4.4）。接在这里，而不是让每个
        # 调用方自己先判断再激活——那样「复活」就有了 N 份实现。
        self.broker.on_missing_queue(self._on_missing_queue)

    async def spawn(
        self,
        factory: AgentFactory,
        *,
        query: str | None = None,
        exit_mode: ExitMode = ExitMode.ZOMBIE,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """建一个 Agent 并启动它的事件循环，返回它的 agent_id。

        `query=None` 就是「起来等消息」（spec §4.7 的 lazy boot）：事件循环直接进 IDLE，第一条
        消息才是它的第一个周期。

        工厂收下本 Runtime 的 hooks 与事件总线：Agent 自己新建一份，宿主注册的切点与订阅就都落空了。
        """
        if self._closed:
            raise _already_shut_down()
        if parent_id is not None and parent_id in self._doomed:
            raise _parent_being_destroyed(parent_id)
        agent = factory(self.hooks, self.events)
        agent_id = agent.agent_id
        control = ControlBlock()
        gate = BrokerTurnGate(agent_id, self.broker, control)
        agent.attach_gate(gate)
        await self.registry.register(
            AgentInfo(
                agent_id=agent_id,
                name=agent.options.name,
                agent_type=type(agent).__name__,
                parent_id=parent_id,
                metadata=metadata or {},
            )
        )
        await self.broker.ensure_queue(agent_id)
        # `_closed` 在方法开头读过一次，此后隔着 register / ensure_queue 两个 Protocol await。任何
        # 会挂起的 store 下，并发的 shutdown 会在 gather 返回之后才走到这里，于是建出一个永不被
        # 取消、永不被 await 的驱动，并把一个再没有订阅者的 Agent 留在注册表里。复检与 create_task
        # 之间没有挂起点，因此这道守卫按得住；建好的两样按逆序还回去，不留半个 Agent。
        if self._closed:
            await self.broker.remove_queue(agent_id)
            await self.registry.unregister(agent_id)
            raise _already_shut_down()
        # put 先于 create_task：put 被拒时还没有任务可泄漏。两者之间没有挂起点，任务因此不会在
        # set_task 之前就跑起来。
        self._table.put(agent_id, agent)
        self._slots[agent_id] = _Slot(exit_mode=exit_mode, control=control, gate=gate)
        task = asyncio.create_task(self._drive(agent_id, agent, query), name=f'agent:{agent_id}')
        self._table.set_task(agent_id, task)
        await self.hooks.trigger(Junction.AGENT_SPAWN, agent_id=agent_id, parent_id=parent_id)
        return agent_id

    async def _drive(self, agent_id: str, agent: BaseAgent, query: str | None) -> None:
        # `get` 而不是下标：任务是 create_task 排上去的，在它真正跑起来之前 slot 可能已经被摘掉。
        # 那时该静默退出，而不是让一个 KeyError 变成无人认领的任务异常。
        slot = self._slots.get(agent_id)
        if slot is None:
            return
        if not await self.registry.compare_and_set_status(
            agent_id, expected=AgentStatus.IDLE, new=AgentStatus.RUNNING
        ):
            logger.error(
                '期望: agent %s 在注册表里是 IDLE，可由本进程接管。'
                ' 实际: IDLE -> RUNNING 被拒，它当前的状态不是 IDLE，说明已被别人拿走。'
                ' 下一步: 用 find() 读它的 status 与 owner；本进程不跑它，也不清理它。',
                agent_id,
            )
            return
        slot.owned = True
        cancelled = False
        try:
            await self._cycles(agent_id, agent, query, slot)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            logger.exception(
                '期望: agent %s 的事件循环正常结束。'
                ' 实际: 它以异常结束，Context 的 stop_reason 已置为 error，循环不再继续。'
                ' 下一步: 读 wait() 交回的 Context 定位最后一轮；'
                '要让它自愈就挂一个 llm_failure / post_tool_use hook。',
                agent_id,
            )
        finally:
            if not cancelled:
                await self._finish(agent_id, slot)

    async def _cycles(
        self, agent_id: str, agent: BaseAgent, query: str | None, slot: _Slot
    ) -> None:
        """IDLE↔RUNNING：等一条消息，跑一个周期，再等下一条。"""
        while True:
            if query is None and slot.control.deferred_count == 0:
                # 这三句之间没有挂起点，因此它们对控制面是一次原子的「我要去停在邮箱上了」：
                # 到这里手上已经没有在飞的轮，一个还没被消费的中止位没有东西可停，作废它；
                # 紧随其后的 `_oscillate` 把注册表从 ABORTING 带回停泊态。置位在两个 await
                # **之前**，`abort` 因此不会在这段窗口里写下一个没人会取走的 ABORTING。
                slot.control.take_abort()
                slot.parked_on_mailbox += 1
                try:
                    await self._oscillate(agent_id, new=_parked_status(slot))
                    # timeout=None 是刻意的：这个循环的退出路径是取消（kill / shutdown）与队列
                    # 消失，不是超时。答 False 只有一个原因——邮箱已经被摘掉了。
                    if not await self.broker.wait_for_messages(agent_id, None):
                        return
                finally:
                    slot.parked_on_mailbox -= 1
            if await slot.control.hold_until_runnable():
                # 中止落在周期之间：这一轮不开始，位已消费，回去继续等。
                query = None
                continue
            # 挂起窗口或并发的 despawn 可能已经把邮箱摘掉：此刻开周期就会写下一块没有前置
            # user 块的 assistant 块。回去等，让「邮箱没了」在它唯一的观测点上被答出来。
            if query is None and not await self._has_unhandled_input(agent_id, slot):
                continue
            await self._oscillate(agent_id, new=AgentStatus.RUNNING)
            slot.gate.begin_cycle()
            slot.control.enter_run()
            try:
                await agent.run(query)
            finally:
                slot.control.leave_run()
            query = None
            # 预算耗尽这条路无条件退出，即便还有已收下的输入：没有轮次可用，再开一个周期只会
            # 被同一条消息反复唤醒又立刻返回。丢下的那些由 `_finish` 记成 ERROR。
            if agent.context.stop_reason is StopReason.MAX_TURNS:
                return
            if slot.exit_mode is not ExitMode.IDLE and not await self._has_unhandled_input(
                agent_id, slot
            ):
                return

    async def _has_unhandled_input(self, agent_id: str, slot: _Slot) -> bool:
        """还有已经收下、却还没被处理的输入吗？

        邮箱里剩下的消息与暂存的延迟消息是同一件事的两个落点，两边的发送方都拿到过 `ok=True`。
        """
        if slot.control.deferred_count:
            return True
        return await self._has_queued_message(agent_id)

    async def _oscillate(self, agent_id: str, *, new: AgentStatus) -> None:
        """周期边界上的状态转换（停在邮箱上、开周期，见 `_parked_status`）。

        失败不改变循环的走向，但必须留痕。

        `allowed` 是除目标状态之外的全部活状态：控制面在周期之间能留下的每一个状态都在内，所以
        被拒只剩一种解释——条目已被摘掉，或已被别人送进终态。
        """
        allowed = tuple(status for status in _LIVE_STATUSES if status is not new)
        if not await self._switch(agent_id, new=new, allowed=allowed):
            logger.error(
                '期望: agent %s 在周期边界上仍是本进程认得的活状态，可由本进程转去 %s。'
                ' 实际: 转换被拒——它现在既不是目标状态，也不在本进程认得的活状态里，'
                '说明注册表条目已被摘掉或已被别人送进终态，而本进程的事件循环还在跑。'
                ' 下一步: 用 find() 读它当前的状态；要真的停下这个循环就 kill 它。',
                agent_id,
                new.value,
            )

    async def _switch(
        self, agent_id: str, *, new: AgentStatus, allowed: tuple[AgentStatus, ...]
    ) -> bool:
        """把状态 CAS 到 `new`；当前已经是 `new` 也算成功（后置条件已成立）。

        逐个试 CAS 而不是「读一次再决定写什么」：读与写之间的窗口正是 CAS 存在的理由，远端实现
        下另一个执行体可以在这个窗口里改掉状态，而那次转换会被无声吞掉。
        """
        for expected in (new, *allowed):
            if await self.registry.compare_and_set_status(agent_id, expected=expected, new=new):
                return True
        return False

    async def _finish(self, agent_id: str, slot: _Slot) -> None:
        """事件循环退出后的归宿（spec §4.1 的 ExitMode）。

        **状态与资源的拆除是幂等的**：`kill` 的收割任务与驱动任务自己的 finally 都可能走到这里，
        而两者谁先谁后取决于取消是否赶上。
        """
        await self._report_abandoned_input(agent_id, slot)
        await self.broker.remove_queue(agent_id)
        if slot.exit_mode is ExitMode.EXIT:
            await self._destroy_tree(agent_id, operation='exit')
            return
        if not await self._switch(agent_id, new=AgentStatus.ZOMBIE, allowed=_LIVE_STATUSES):
            logger.error(
                '期望: agent %s 在注册表里仍是本进程认得的活状态，可由本进程转去 zombie。'
                ' 实际: 转换被拒，它的状态在事件循环退出前被别人改成了别的东西。'
                ' 下一步: 用 find() 读它当前的状态；本进程已经不再驱动它。',
                agent_id,
            )

    async def _report_abandoned_input(self, agent_id: str, slot: _Slot) -> None:
        """摘队列之前，把「拿过 ok=True 收据、却不会再被处理」的输入记下来。

        循环只在没有未处理输入时退出，所以这条日志出现即意味着一次真的丢失：预算耗尽、周期抛
        异常，或摘队列与最后一次门检查之间的那个窗口。暂存的那些还留在控制块里（slot 在 ZOMBIE
        之后不摘），邮箱里的随队列一起没了。
        """
        stashed = slot.control.deferred_count
        queued = await self._has_queued_message(agent_id)
        if not stashed and not queued:
            return
        logger.error(
            '期望: agent %s 的事件循环退出时，已经收下的输入都已处理——'
            'publish 答过 ok=True 的消息不该随队列一起消失。'
            ' 实际: 还剩 %d 条暂存的延迟消息，邮箱非空=%s；队列即将被摘掉，'
            '邮箱里那些就此丢失，暂存的那些留在控制块里但这个循环不会再读。'
            ' 下一步: 读 wait() 交回的 Context 看它停在哪一轮（预算耗尽就提高 max_turns），'
            '再把这些消息重投一次——在发送方看来它们是投递成功的。',
            agent_id,
            stashed,
            queued,
        )

    async def _has_queued_message(self, agent_id: str) -> bool:
        """邮箱此刻非空吗。

        `timeout=0` 是纯轮询（见 `MessageBroker.wait_for_messages`）：它不取走消息，因此不与门
        那唯一一条消费路径分叉；也不构成一次「停在邮箱上等」。
        """
        return await self.broker.wait_for_messages(agent_id, 0)

    async def wait(self, agent_id: str) -> Context:
        """类比 waitpid：等事件循环结束，交回它的轨迹。

        交回的是 Agent 自己那份 Context，不是快照：ZOMBIE 靠一条消息复活时续用的就是它，拿到副本
        的调用方会与 Agent 将来读写的那一份静默分叉。

        **`ExitMode.IDLE` 的 Agent 事件循环常驻**，因此 `wait` 只在它被 shutdown / kill / despawn
        之后才返回——对守护型 Agent 调 `wait` 等于故意把自己挂住。

        Agent 的异常与取消都是**被等待者**的终态，不冒充成本次调用自己的失败：崩溃在
        `ctx.stop_reason` 上是 ERROR，被取消的运行没有 stop_reason。
        """
        task = self._table.get_task(agent_id)
        if task is not None:
            await asyncio.wait({task})
        slot = self._slots.get(agent_id)
        agent = self._table.get(agent_id)
        if slot is None or agent is None:
            raise self._nothing_to_wait_for(agent_id)
        if not slot.owned:
            raise ContractViolation(
                expected=f'agent {agent_id!r} 的事件循环由本进程接管过',
                received=(
                    '本进程 spawn 过它，但驱动任务没有接管成功：'
                    'IDLE -> RUNNING 的转换被拒（它已被别人拿走），或任务在这之前就被取消'
                ),
                next_action='用 find() 读它当前的 status 与 owner；确认它空闲后重新 spawn 一个',
            )
        return agent.context

    def _nothing_to_wait_for(self, agent_id: str) -> ContractViolation:
        return ContractViolation(
            expected=f'agent {agent_id!r} 由本进程 spawn，且结束后仍留在本进程里',
            received=(
                f'本进程没有它的运行记录（已知: {sorted(self._slots)}）：'
                '要么它不是本进程 spawn 的，'
                '要么它以 ExitMode.EXIT 结束，已连同 Context 一起被销毁'
            ),
            next_action=(
                '确认 agent_id 是 spawn 的返回值；'
                '要在结束后读轨迹就用 ExitMode.ZOMBIE 或 ExitMode.IDLE spawn；'
                '若它由其他进程持有，本进程无法 wait 它'
            ),
        )

    async def send(
        self,
        agent_id: str,
        content: str,
        *,
        trigger: Trigger = Trigger.PENDING,
        activate: bool = True,
        sender: str = 'runtime',
        correlation_id: str | None = None,
    ) -> DeliveryResult:
        """投递一条消息到目标邮箱。

        **默认激活目标**：绝大多数调用方要的语义是「发消息，它没在跑就叫醒它」（spec §4.2）。
        要严格的「仅入队」语义就传 `activate=False`——此时目标若已经进了 ZOMBIE（队列已摘），
        投递返回 `ok=False`，而不是把消息塞进一个没人消费的地方。

        **投递失败返回结构化结果而不抛异常**，调用方才能识别可重试的瞬时错误。

        可寻址性以注册表为准，不以邮箱在不在为准：残留邮箱（远端 broker 的 remove_queue 失败、
        或 ensure_queue 之后注册失败）会让投递看起来成功，而再没有人会去消费它。
        """
        if await self.registry.get(agent_id) is None:
            return DeliveryResult(
                ok=False,
                reason=(
                    f'期望: agent {agent_id!r} 已注册，因而可寻址。'
                    ' 实际: 注册表里没有这个条目。'
                    ' 下一步: 先 spawn 它；以 ExitMode.EXIT 结束的 Agent 已被销毁，只能重新 spawn。'
                ),
            )
        message = Message(
            sender=sender,
            receiver=agent_id,
            content=content,
            trigger=trigger,
            correlation_id=correlation_id,
        )
        token = _ACTIVATION_ALLOWED.set(activate)
        try:
            return await self.broker.publish(agent_id, message)
        finally:
            _ACTIVATION_ALLOWED.reset(token)

    async def ensure_active(self, agent_id: str) -> bool:
        """唯一的激活入口：复活 ZOMBIE、重开事件循环（spec §4.2）。

        复活、重开、从落盘会话恢复、恢复子会话在语义上是同一件事，不该暴露成四个方法让用户猜。

        返回 True 表示「此刻它有一个活着的事件循环」，False 表示本进程做不到——**两者都是回答，
        都不抛异常**（问句只答不抛）。三种本进程无法自助诊断的 False 各记一条日志：手里没有它的
        实例、它正在收尾、状态转换被拒。注册表里没有它不记——那与 `find` 答 None 是同一个正常
        答案，而且「这个 Agent 不归我」正是回调链上设计好的一支，记下来只会让每一次转手都报错。
        """
        if self._closed:
            return False
        info = await self.registry.get(agent_id)
        if info is None:
            return False
        slot = self._slots.get(agent_id)
        agent = self._table.get(agent_id)
        if slot is None or agent is None:
            logger.error(
                '期望: agent %s 的实例在本进程的实例表里，可以直接重开它的事件循环。'
                ' 实际: 注册表里有它，本进程手里没有——它由别的进程持有，'
                '或者只剩落盘会话而恢复能力尚未实现。'
                ' 下一步: 在持有它的进程上激活它；'
                '要在本进程续跑就重新 spawn 一个并把会话交给它。',
                agent_id,
            )
            return False
        if self._has_live_driver(agent_id):
            return True
        if info.status is AgentStatus.ABORTING:
            logger.error(
                '期望: agent %s 处在可以重开事件循环的状态。'
                ' 实际: 它正在收尾（aborting），此刻重开会与收尾抢同一份轨迹。'
                ' 下一步: 等它进 zombie 之后再激活；用 find() 读它当前的状态。',
                agent_id,
            )
            return False
        if not await self._switch(agent_id, new=AgentStatus.IDLE, allowed=(AgentStatus.ZOMBIE,)):
            logger.error(
                '期望: agent %s 在注册表里是 idle 或 zombie，可由本进程重开事件循环。'
                ' 实际: 转换被拒，本进程读到它是 %s，而本进程手里的驱动任务已经结束——'
                '它由别人在跑，或已被送进别的状态。'
                ' 下一步: 用 find() 读它当前的状态；条目已被 despawn 就只能重新 spawn。',
                agent_id,
                info.status.value,
            )
            return False
        await self.broker.ensure_queue(agent_id)
        # 上面那次 has_live_driver 之后隔着两个挂起点，并发的另一次激活可能已经把驱动挂上去了。
        # 复检与 create_task / set_task 之间没有挂起点，这三步因此对事件循环是原子的；少了复检，
        # 第二次会撞上实例表拒绝替换未结束任务，把只答不抛的问句变成抛异常并丢下一个孤儿任务。
        if self._has_live_driver(agent_id):
            return True
        if self._closed or agent_id in self._doomed:
            # ZOMBIE -> IDLE 已经写下去了，而这里不会再有驱动接着：留着 idle 就是宣布「它能处理
            # 消息」，而 zombie 才是复活它的凭据（`ensure_active` 的 `allowed` 只有它）。
            await self._switch(agent_id, new=AgentStatus.ZOMBIE, allowed=(AgentStatus.IDLE,))
            logger.error(
                '期望: agent %s 可以重开事件循环。'
                ' 实际: 复活走到一半时本 Runtime 已经 shutdown、或它已经在被拆——此刻建出来的驱动'
                '没人会取消也没人会 await，只会在解释器退出时变成一行「Task was destroyed but it'
                ' is pending」。'
                ' 下一步: 注册表状态已经写回 zombie；被拆的等它拆完（find() 会答 None），'
                '关机的新建一个 Runtime 重新 spawn。',
                agent_id,
            )
            return False
        # 复活的循环没有 query：它要干什么由那条把它叫醒的消息说，门会在第一轮开始前消费它。
        revived = asyncio.create_task(self._drive(agent_id, agent, None), name=f'agent:{agent_id}')
        self._table.set_task(agent_id, revived)
        return True

    def _has_live_driver(self, agent_id: str) -> bool:
        task = self._table.get_task(agent_id)
        return task is not None and not task.done()

    async def _on_missing_queue(self, agent_id: str) -> bool:
        """回调答的是「邮箱建好了没有」，不是「它活着没有」；两者只在活驱动这一支上分叉。

        活驱动而邮箱不在，只可能落在 `_finish` 摘掉队列之后。此刻把邮箱建回来，消息会落进一个
        再没有消费者的地方；答 True 却没建好，则掐断回调链——本来能把它建起来的下一个回调不会
        被问到。
        """
        if not _ACTIVATION_ALLOWED.get():
            return False
        if self._has_live_driver(agent_id):
            return False
        return await self.ensure_active(agent_id)

    async def pause(self, agent_id: str, *, recursive: bool = False) -> None:
        """挂起：让它在下一个轮的边界上停下来（spec §5.1 第 2 步），注册表转成 PAUSED。

        挂起的那段时间**不计入任何一轮**——门在计数之前，因此挂起既不消耗预算，也不会把一轮切成
        两半。**返回不等于它已经停下来**：turn 循环可能正卡在一次 LLM 调用里，「真的停了」只有
        `ControlBlock.parked` 答得出。

        允许从 IDLE / RUNNING / WAITING 进入；已经是 PAUSED 是成功（后置条件已成立）。ZOMBIE 与
        ABORTING 报错：前者没有事件循环可停，而它的注册表状态正是 `ensure_active` 复活它的凭据，
        写上 PAUSED 它就再也叫不醒；后者正在收尾，挂起一个正在拆的东西没有可表达的意图。

        `recursive=True` 时**父先于子**：父只在下一个轮边界停下，因此它仍可能在遍历期间生出新的
        子代——父先于子把这个窗口从无界收窄到**一次在飞的轮**，并不是消除它。那一轮里出生的子代
        遍历看不到，也就不会被挂起；要一棵完整挂起的子树只能再走一遍。父先于子另外保证：被点名
        的那一个被拒时，异常是在走这棵树**之前**抛出的，不会留下一棵改了一半的树。
        """
        await self._pause_one(agent_id, named=True)
        if recursive:
            for descendant in await self._descendants(agent_id):
                await self._pause_one(descendant, named=False)

    async def resume(self, agent_id: str, *, recursive: bool = False) -> None:
        """解除挂起，注册表从 PAUSED 转回它该在的地方。

        **回到哪里由控制块说了算**：它在一次 `run()` 之内就回 RUNNING，否则回 IDLE。只有持有
        事件循环的进程知道这件事，而注册表上没有「挂起之前是什么」——记一份就多出一个必须与真相
        保持一致的东西，且它在挂起期间还会被 `wait_for` 改。

        **门无条件抬起，注册表那一步尽力而为。** 只有本组方法抬得起这道门，因此「记不下就不抬」
        会把这个 Agent 永久停在门上；过时的状态可以用 `find` 重读，没人能抬起的门谁都救不回来。

        `recursive=True` 时**子先于父**：父恢复之后可能立刻要用子代，先恢复子代，它就不会看见
        一棵只恢复了一半的树。
        """
        if recursive:
            for descendant in reversed(await self._descendants(agent_id)):
                await self._resume_one(descendant, named=False)
        await self._resume_one(agent_id, named=True)

    async def abort(self, agent_id: str, *, recursive: bool = False) -> None:
        """协作式中止：置位中止位、把状态转成 ABORTING，然后立刻返回。

        中止**在轮与轮之间兑现**（spec §5.1 第 1 步）：这一次工作以 `StopReason.ABORTED` 收场，
        轨迹配对完整、可以直接 resume。要立刻停下、不惜留下不配对的调用，那是 `kill`——两者的
        区别正是 `ABORTED` 存在的理由，不要合并。

        位是**一次性**的：这一个周期结束，事件循环照常回去等下一条消息（`exit_mode` 决定它还在
        不在）。abort 是「停下手上这一件事」，不是「从此不再干活」。**暂存的延迟消息不在被停的
        范围里**——它们排的是下一件事的队，那一件还没开始。

        **手上没有活时它成功，并且什么都不写。** 事件循环停在邮箱上（IDLE，或挂起着而邮箱空）、
        或已经结束（ZOMBIE），此刻轨迹已经配对完整——那正是协作式中止要保住的东西，后置条件已经
        成立。判据是**驱动任务此刻在哪**，不是注册表说什么：注册表分不出停在邮箱上的 PAUSED 与
        停在挂起门上的 PAUSED，而只有后者收得到唤醒边沿。这条判断在一段不含 await 的代码里做完，
        因此与「一个周期开始」之间没有交错窗口。之后才到达的消息开的是**下一件事**，不在本次调用
        的范围里——它本来也停不住：被中止的周期不 consume 邮箱，那条消息照样换来一个完整的周期。

        因此 abort **不因为「它在做什么」而拒绝**。拒绝只剩下「本进程答不了这个问题」：注册表里
        没有这个条目、本进程手里没有它的实例、驱动任务没有接管过它，以及转 ABORTING 的 CAS 输给
        了并发的写者。

        对 PAUSED 允许，且中止**穿透挂起门**：先挂起再中止是真实顺序，若中止要等解除挂起才被
        看见，这个 Agent 会永远停在那里而调用方毫无反馈。**穿透不等于解除**：门原样落着，兑现
        之后它回到挂起态等 `resume`。

        `recursive=True` 时**父先于子**，理由同 `pause`：先掐掉还会派出新活的那一头。父只在下一个
        轮边界停下，因此它仍可能在遍历期间生出新的子代——父先于子把这个窗口收窄到**一次在飞的
        轮**，并不是消除它。要一棵完整中止的子树只能再走一遍。
        """
        await self._abort_one(agent_id, named=True)
        if recursive:
            for descendant in await self._descendants(agent_id):
                await self._abort_one(descendant, named=False)

    async def kill(self, agent_id: str) -> None:
        """硬终止：取消事件循环并**立刻返回**，收尾在后台完成。

        spec §4.1 给 ABORTING 的理由就是这个——它是中间态，好让 `kill` 不必等收尾。

        取消可能落在轮的中间，因此这条路**不产生 `stop_reason`**，并且会留下一个声明了 N 个调用却
        不足 N 条结果的 assistant block。那不是缺陷：`BaseAgent.run` 在入口按「中断、副作用未知」
        把它补成配对完整的轨迹，于是被杀掉的 Agent 仍然可以被一条消息复活并继续干。要一次配对完整、
        带 `StopReason.ABORTED` 的收场，那是 `abort`——两者的区别正是 `ABORTED` 存在的理由。

        **`kill` 自己不注销注册表条目**，它把 Agent 送进 ZOMBIE：条目还在，因此它的子代不会指向一
        个已被删掉的父，`kill` 也就不需要级联。条目的去留仍由 `exit_mode` 决定——`ExitMode.EXIT` 的
        Agent 走的是同一条级联拆除，条目连同整棵子树一起消失。要无条件连条目一起去掉是 `despawn`。

        **刻意不置中止位。** 取消已经足够；而中止位是会留下来的状态，此后一次 `ensure_active` 复活会
        在第一轮就把它消费掉，让复活出来的 Agent 立刻结束——一次 kill 变成了永久的诅咒。

        `shutdown` 之后是成功的 no-op：事件循环那时都已经停下（后置条件已成立），而收尾与 `shutdown`
        自己一样不做；此刻再排一个收割任务，那是个永不被 await 的孤儿。
        """
        slot, info = await self._control_target('kill', agent_id, named=True)
        if slot is None or info is None:
            return
        if self._closed:
            return
        # 转换失败不记：那只意味着它已经进了终态（killing 一个 zombie 是成功的 no-op），而真正
        # 要保证的归宿由收割任务给出，那一步失败会自己留痕。
        await self._switch(agent_id, new=AgentStatus.ABORTING, allowed=_LIVE_STATUSES)
        # 复检与 create_task 之间没有挂起点：并发的 shutdown 已经等过收割任务，此刻再建一个就是
        # 永不被 await 的孤儿。
        if self._closed:
            return
        task = self._table.get_task(agent_id)
        if task is not None and not task.done():
            task.cancel()
        # 已经有一次收尾在飞就不再排一个：那一次会把它送到同一个归宿，后置条件已经成立。
        if agent_id in self._reaping:
            return
        self._reaping.add(agent_id)
        reaper = asyncio.create_task(self._reap(agent_id, slot), name=f'reap:{agent_id}')
        self._reapers.add(reaper)
        reaper.add_done_callback(self._reapers.discard)

    async def despawn(self, agent_id: str) -> None:
        """销毁：连注册表条目一起去掉，**并且级联到全部子代**。

        级联不是可选项，所以这里没有 `recursive` 开关：**要么条目还在，要么它和它的子树一起不在**
        （理由见模块文档）。拆除顺序是**子先于父**——任何一刻都不存在「父已经没了而子还活着」的中
        间态。

        **不因为「本进程没接管过它」而拒绝。** 树在共享注册表里，而 `pause` / `resume` / `abort`
        只作用于本进程按得住的 Agent；`despawn` 照搬那条规则就会留下一个别人持有的子代，它的
        `parent_id` 指向一个已被注销的父。因此本方法作用在**条目**上，不作用在事件循环的所有权上。

        `despawn` 等**本进程持有的**每一个事件循环真的停下来再返回——它对调用方的承诺是「回来之后
        这些条目都不在了」，而这个承诺只有等到才作数。别的进程持有的那些停不了，它们自己会在下一个
        周期边界发现条目已经不在。要立刻返回就用 `kill`。

        未知 agent 是成功的 no-op：「它不在注册表里」这个后置条件已经成立。
        """
        self._refuse_self('despawn', agent_id)
        if agent_id not in self._slots and await self.registry.get(agent_id) is None:
            return
        await self._destroy_tree(agent_id, operation='despawn')

    async def _destroy_tree(self, agent_id: str, *, operation: str) -> None:
        """`kill` 的收尾、`despawn` 与 `ExitMode.EXIT` 共用的唯一拆除顺序。

        **先把整棵子树钉住，再拆**（理由见模块文档）。反复走而不是走一遍：第一遍的 `children_of`
        与钉住之间有窗口，那一刻出生的子代只有下一遍才看得见；钉住之后树上再挂不进新子代，所以走到
        没有新节点为止这棵树的形状就冻住了。

        拆除顺序是**发现顺序的逆序**：广度优先只能经由父发现子，因此逆序里子一定在父之前被拆。

        遍历到的子代拆不掉只记日志、不中断：递归操作是尽力而为的，为第一个冲突抛异常会留下一棵改了
        一半的树，而调用方无从得知改到哪了。被点名的那一个照常抛。
        """
        descendants: list[str] = []
        seen = {agent_id}
        self._doomed.add(agent_id)
        try:
            while fresh := [
                found for found in await self._descendants(agent_id) if found not in seen
            ]:
                seen.update(fresh)
                descendants.extend(fresh)
                self._doomed.update(fresh)
            # 抛在动手之前：等自己就是永久挂死，而半棵拆掉的树没有人能收拾。
            for descendant in descendants:
                self._refuse_self(operation, descendant)
            for descendant in reversed(descendants):
                try:
                    await self._destroy(descendant)
                except Exception:
                    logger.exception(
                        '期望: agent %s 作为 %s 的子代被拆掉，条目随子树一起消失。'
                        ' 实际: 拆它时抛了异常，条目留在注册表里，而它的父即将被注销——'
                        '它的 parent_id 从此指向一个不存在的条目，children_of 再也找不到它。'
                        ' 下一步: 用 find() 确认它还在，再按 agent_id 单独 despawn 它；'
                        '它的父已经不在，只能这样够得着它。',
                        descendant,
                        agent_id,
                    )
            await self._destroy(agent_id)
        finally:
            self._doomed.difference_update(seen)

    async def _destroy(self, agent_id: str) -> None:
        """拆掉一个 Agent 的全部痕迹。幂等。

        本进程没有它的实例时只摘条目与队列：树的形状与本进程持有哪些实例无关。
        """
        # 已经没有痕迹可拆就什么都不做。`AGENT_DESPAWN` 报的是「这个条目刚刚消失」这个既成事实，
        # 对一个早已不在的条目再报一次是个假事件，而本方法会被走到两次——`kill` 的收割任务与驱动
        # 任务自己的 finally 都通向它。
        if (
            agent_id not in self._slots
            and self._table.get(agent_id) is None
            and await self.registry.get(agent_id) is None
        ):
            return
        task = self._table.get_task(agent_id)
        # `is not current_task()`：`ExitMode.EXIT` 是在驱动任务自己的 finally 里走到这条路的，
        # 取消并 await 自己就是永久挂死。
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.wait({task})
        await self.broker.remove_queue(agent_id)
        await self.registry.unregister(agent_id)
        self._table.pop(agent_id)
        self._slots.pop(agent_id, None)
        await self.hooks.trigger(Junction.AGENT_DESPAWN, agent_id=agent_id)

    async def _reap(self, agent_id: str, slot: _Slot) -> None:
        """在事件循环之外收尾。

        `_drive` 的 finally 在取消路径上刻意不收尾，因此**被取消的那一次退出只有这里保证得了归宿**
        ——少了它，Agent 停在 ABORTING，而 `ensure_active` 明文拒绝复活一个正在收尾的 Agent。

        收尾要写注册表、摘队列，而这些 await 落在一个正在被取消的任务里会再次被取消——内存实现没有
        挂起点所以看不出来，远端实现下就是收了一半的尾。放进独立任务，`kill` 因此也能立刻返回。

        异常在这里收口：收割任务没有调用方，逃出去的异常无人取回，只会在 GC 时变成一行看不出主体
        的日志。
        """
        try:
            task = self._table.get_task(agent_id)
            if task is not None:
                await asyncio.wait({task})
            await self._finish(agent_id, slot)
        except Exception:
            logger.exception(
                '期望: agent %s 被取消之后的收尾走完，它落到 exit_mode 说的那个归宿上。'
                ' 实际: 收尾自己抛了异常，它可能停在 aborting——而 aborting 的 Agent 复活不了。'
                ' 下一步: 用 find() 读它当前的状态；停在 aborting 就 despawn 它再重新 spawn。',
                agent_id,
            )
        finally:
            self._reaping.discard(agent_id)

    def _refuse_self(self, operation: str, agent_id: str) -> None:
        """拒绝一个要等目标停下、而目标就是调用者自己的操作。

        这不是「冲突」而是意图无法表达，所以遍历找到的主体上照样抛：等自己就是永久挂死。
        """
        task = self._table.get_task(agent_id)
        if task is None or task is not asyncio.current_task():
            return
        raise ContractViolation(
            expected=f'{operation} 的目标子树里不含发起这次调用的那个 Agent 自己',
            received=f'这次 {operation} 落在 agent {agent_id!r} 自己的事件循环里',
            next_action=(
                f'{operation} 要等目标的事件循环停下来，等自己就是永久挂死。'
                '要让自己停下来用 abort（它在下一轮边界上兑现，轨迹保持一致）；'
                '要让别人把你销毁，就把这件事交给你的父 Agent'
            ),
        )

    async def _abort_one(self, agent_id: str, *, named: bool) -> None:
        slot, info = await self._control_target('abort', agent_id, named=named)
        if slot is None or info is None:
            return
        # 两个读都是同步的，且此后到 `request_abort()` 为止只隔着一次 CAS：驱动任务在本函数让出
        # 控制权之前动不了，因此「它此刻没有在飞的轮」这个判断不会被一个并发开始的周期推翻。
        if not self._has_live_driver(agent_id) or slot.parked_on_mailbox:
            return
        # 先落注册表再落位：记不下 ABORTING 就不要真的置位——那会停掉一件谁都看不出被停了的活。
        # 反过来这两句之间也没有挂起点，所以驱动任务看到的永远是「状态与位一起到」。
        if not await self._switch(agent_id, new=AgentStatus.ABORTING, allowed=_LIVE_STATUSES):
            self._refuse_control(
                'abort',
                agent_id,
                named=named,
                received='转到 aborting 的 CAS 被拒，它的状态刚刚被别人改过',
                next_action='用 find() 重读它的状态再决定；不要在读到的旧状态上重试',
            )
            return
        slot.control.request_abort()

    async def _pause_one(self, agent_id: str, *, named: bool) -> None:
        slot, info = await self._control_target('pause', agent_id, named=named)
        if slot is None or info is None:
            return
        # 已经是 PAUSED 的一并放行：后置条件已经成立的操作成功返回。
        if info.status not in (*_PAUSABLE_STATUSES, AgentStatus.PAUSED):
            self._refuse_control(
                'pause',
                agent_id,
                named=named,
                received=f'它当前是 {info.status.value}',
                next_action=(
                    f'只有 {" / ".join(status.value for status in _PAUSABLE_STATUSES)} 能挂起。'
                    'zombie 没有事件循环可停，而它的注册表状态正是复活它的凭据；'
                    'aborting 正在收尾。要让它彻底不再被叫醒就用 despawn'
                ),
            )
            return
        # 先落注册表再落门：记不下 PAUSED 就不要真的停——那会停在一个谁都看不见的地方。
        if not await self._switch(agent_id, new=AgentStatus.PAUSED, allowed=_PAUSABLE_STATUSES):
            self._refuse_control(
                'pause',
                agent_id,
                named=named,
                received='转到 paused 的 CAS 被拒，它的状态刚刚被别人改过',
                next_action='用 find() 重读它的状态再决定；不要在读到的旧状态上重试',
            )
            return
        slot.control.pause()

    async def _resume_one(self, agent_id: str, *, named: bool) -> None:
        slot, info = await self._control_target('resume', agent_id, named=named)
        if slot is None or info is None:
            return
        slot.control.resume()
        back_to = AgentStatus.RUNNING if slot.control.in_run else AgentStatus.IDLE
        if not await self._switch(agent_id, new=back_to, allowed=(AgentStatus.PAUSED,)):
            # WARNING 而不是 INFO：root logger 默认停在 WARNING，INFO 的记录根本不会被创建，而
            # 本模块的不变式是「每一处状态转换失败都必须留痕」。也不是 ERROR：门已经抬起、这个
            # Agent 照常跑，任何远端注册表上「它自己先往下走了」都会让 ERROR 变成常态误报。
            logger.warning(
                '期望: agent %s 在注册表里是 paused，可由本进程转回 %s。'
                ' 实际: 这次 CAS 被拒，它此刻不是 paused——'
                '它可能已经自己往下走了，也可能状态归了别人。'
                ' 下一步: 门已经抬起，这个 Agent 可以继续跑；'
                '用 find() 重读它的状态，不要在读到的旧状态上重试。',
                agent_id,
                back_to.value,
            )

    async def _control_target(
        self, operation: str, agent_id: str, *, named: bool
    ) -> tuple[_Slot | None, AgentInfo | None]:
        """取出控制面操作的目标；取不到时按 named 决定抛还是记日志。

        **驱动任务没有接管过它，就不算取到。** 控制面写的是共享注册表里的状态，而这里的门只按得住
        本进程的事件循环；接管失败的那个条目归别人所有，替它写下 PAUSED 之后谁的 `ensure_active`
        都转不回 IDLE（`allowed` 只有 ZOMBIE），而 `send` 仍旧照答 ok=True——一张通往没有消费者的
        邮箱的收据。
        """
        slot = self._slots.get(agent_id)
        info = await self.registry.get(agent_id)
        if slot is None or info is None:
            self._refuse_control(
                operation,
                agent_id,
                named=named,
                received=(
                    '注册表里没有这个条目'
                    if info is None
                    else '本进程没有它的实例——它由别的进程持有'
                ),
                next_action=(
                    '确认 agent_id 是 spawn 的返回值；'
                    '被 despawn 或以 ExitMode.EXIT 结束的 Agent 已被销毁，只能重新 spawn。'
                    '若它由其他进程持有，在那个进程上操作它'
                ),
            )
            return None, None
        if not slot.owned:
            self._refuse_control(
                operation,
                agent_id,
                named=named,
                received=(
                    '本进程 spawn 过它，但驱动任务还没有接管：'
                    'spawn 只是把任务排上去，它可能还没被调度到；'
                    '也可能 IDLE -> RUNNING 的转换被拒（它已被别人拿走），或任务在这之前就被取消'
                    '——本进程没有它的事件循环可以停'
                ),
                next_action=(
                    '刚 spawn 完就调用的话先重试一次：每一次 await 都给驱动任务一个接管的机会。'
                    '重试仍是这一条就用 find() 读它当前的 status 与 owner，在持有它的进程上操作它；'
                    '确认它空闲后重新 spawn 一个，才能在本进程里控制它'
                ),
            )
            return None, None
        return slot, info

    def _refuse_control(
        self, operation: str, agent_id: str, *, named: bool, received: str, next_action: str
    ) -> None:
        """点名的目标抛异常，遍历到的子代只记日志。"""
        if not named:
            logger.info(
                '%s 跳过子代 %s：%s。这是尽力而为的遍历，不因此中断。',
                operation,
                agent_id,
                received,
            )
            return
        raise ContractViolation(
            expected=f'agent {agent_id!r} 处在可以 {operation} 的状态',
            received=received,
            next_action=next_action,
        )

    async def _descendants(self, agent_id: str) -> list[str]:
        """广度优先列出全部子代，浅的在前。

        `visited` 不是防御性编程：`children_of` 读的是注册表，而注册表可以是远端的、可以被别的
        进程写过；一个环会让这个遍历永不返回，而它是 pause / abort / despawn 三条路的公共前置。
        """
        found: list[str] = []
        visited = {agent_id}
        frontier = [agent_id]
        while frontier:
            following: list[str] = []
            for parent in frontier:
                for child in await self.registry.children_of(parent):
                    if child in visited:
                        continue
                    visited.add(child)
                    found.append(child)
                    following.append(child)
            frontier = following
        return found

    async def find(self, agent_id: str) -> AgentInfo | None:
        """只读路径，绝不激活任何 Agent。"""
        return await self.registry.get(agent_id)

    async def shutdown(self) -> None:
        """取消所有事件循环，**等它们停下来**再关事件总线。幂等，且是终态：此后 spawn 报错。

        取消不等于停下：任务还要一路展开出来。不等它们停下，未结束的任务会在解释器退出时留下
        一串 "Task was destroyed but it is pending"。
        """
        # 置位在 gather 之前：与本次关机并发的 spawn 既赶不上取消名单、事件又发不出去。
        self._closed = True
        # 摘回调也在 gather 之前：链是 broker 的，活得比本 Runtime 长。不摘，共享 broker 上每个
        # 关停的 Runtime 都留一条永远答 False 的死回调，并靠那条引用把自己整个钉在内存里。
        self.broker.off_missing_queue(self._on_missing_queue)
        tasks = [
            task
            for agent_id in self._table.agent_ids()
            if (task := self._table.get_task(agent_id)) is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # 收割任务不在实例表里，上面那份取消名单看不见它们；不等掉就是一串 "Task was destroyed but
        # it is pending"。清空之后再等：done-callback 要过一次事件循环才把它们摘掉，照着集合等会
        # 白转一圈。收割任务自己不会再排新的收割任务。
        while self._reapers:
            reapers = list(self._reapers)
            self._reapers.clear()
            await asyncio.gather(*reapers, return_exceptions=True)
        await self.hooks.trigger(Junction.RUNTIME_SHUTDOWN)
        await self.events.close()
