"""Runtime：Agent 生命周期与控制面。

按操作系统进程模型设计：Runtime 类比 init 系统，Agent 类比进程。Agent 不管理自己的生命周期——
外层由本类驱动，内层只跑 LLM + 工具循环。

**驱动任务是一个事件循环，不是一次调用。** spec §4.1 把 IDLE 定义成「已注册，事件循环在等消息」，
所以一个 Agent 的常态是**等**，一条消息触发一个周期。周期与周期之间 Context 不重建、轮次不重置
（spec §5.1）——ZOMBIE 留在注册表可寻址，正是为了「一条消息就能让它带着上下文继续干」。

**只有一条消息消费路径。** 中途插话与唤醒都由同一道门（`BrokerTurnGate`）在轮开始前消费。两条
路径分开写必然分叉，而分叉出来的差异只在有人插话时才现身。

**每一处状态转换都只走 CAS，且失败都必须留痕。** `_switch` 逐个试 CAS 而不是「读一次再写」：读与
写之间的窗口正是 CAS 存在的理由。周期边界上的 IDLE↔RUNNING 走 `_oscillate`，它的 `allowed` 覆盖
控制面能在周期之间留下的**每一个**活状态（pause / wait_for / abort 各自留下的那些），因此被拒只剩
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
的那一方在取消之外完成，因此它不必在一个正在被取消的任务里 await。

**`_slots` 的键恒为实例表键的子集。** EXIT 把队列、注册表条目、实例表条目与 slot 一起摘掉，于是
本类持有的每一份 Context 都对应一个仍可寻址的 Agent，随时间无界增长的只有活着的 Agent 数。代价
写在 `wait` 的契约里：被 reap 的 Agent 没有 waitpid 可言。

**`shutdown` 是终态。** 关闭后的事件总线静默丢弃 emit，此后 spawn 出来的 Agent 会照常干完活却完全
不可观测，因此 spawn 在关闭后报错。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from syndica.kernel.agent import BaseAgent
from syndica.kernel.agent_types import AgentInfo, AgentStatus, ExitMode
from syndica.kernel.context import Context, StopReason
from syndica.kernel.control import BrokerTurnGate, ControlBlock
from syndica.kernel.errors import ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.local_table import LocalAgentTable
from syndica.kernel.message import DeliveryResult, Message, Trigger
from syndica.kernel.protocols import AgentRegistry, MessageBroker
from syndica.kernel.stores import InMemoryAgentRegistry, InMemoryMessageBroker

__all__ = ['AgentFactory', 'Runtime']

logger = logging.getLogger(__name__)

AgentFactory = Callable[[HooksCenter, EventBus], BaseAgent]

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
        self._closed = False

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
        # put 先于 create_task：put 被拒时还没有任务可泄漏。两者之间没有挂起点，任务因此不会在
        # set_task 之前就跑起来。
        self._table.put(agent_id, agent)
        self._slots[agent_id] = _Slot(exit_mode=exit_mode, control=control, gate=gate)
        task = asyncio.create_task(self._drive(agent_id, agent, query), name=f'agent:{agent_id}')
        self._table.set_task(agent_id, task)
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
                await self._oscillate(agent_id, new=AgentStatus.IDLE)
                # timeout=None 是刻意的：这个循环的退出路径是取消（kill / shutdown）与队列消失，
                # 不是超时。答 False 只有一个原因——邮箱已经被摘掉了。
                if not await self.broker.wait_for_messages(agent_id, None):
                    return
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
        """周期边界上的 IDLE↔RUNNING 转换。失败不改变循环的走向，但必须留痕。

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
        """事件循环退出后的归宿（spec §4.1 的 ExitMode）。"""
        await self._report_abandoned_input(agent_id, slot)
        await self.broker.remove_queue(agent_id)
        if slot.exit_mode is ExitMode.EXIT:
            await self.registry.unregister(agent_id)
            self._table.pop(agent_id)
            self._slots.pop(agent_id, None)
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

        **`ExitMode.IDLE` 的 Agent 事件循环常驻**，因此 `wait` 只在它被 shutdown（或后续加入的
        kill / despawn）之后才返回——对守护型 Agent 调 `wait` 等于故意把自己挂住。

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
        self, agent_id: str, content: str, *, trigger: Trigger = Trigger.PENDING
    ) -> DeliveryResult:
        """投递一条消息到目标邮箱。

        **只投递，不激活**；spec §4.2 的按需激活随 `on_missing_queue` 接线在计划里下一个任务加入，
        在那之前 ZOMBIE 的邮箱已经摘掉，投给它会拿到 `ok=False`。

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
        return await self.broker.publish(
            agent_id,
            Message(sender='runtime', receiver=agent_id, content=content, trigger=trigger),
        )

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
        tasks = [
            task
            for agent_id in self._table.agent_ids()
            if (task := self._table.get_task(agent_id)) is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.events.close()
