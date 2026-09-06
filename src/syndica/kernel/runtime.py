"""Runtime：Agent 生命周期。

按操作系统进程模型设计：Runtime 类比 init 系统，Agent 类比进程。Agent 不管理自己的生命周期——
外层由本类驱动，内层只跑 LLM + 工具循环。

本计划只做最小闭环（spawn / wait / send / find / shutdown）。事件循环、kill / pause / abort、
按需激活随计划 2 补入。

**两处状态转换都看 CAS 的返回值。** 进 RUNNING 失败 = 这个 Agent 已被别人拿走：既不跑它，也不替
它清理，连终态转换都不试——它此刻的状态不是本进程写的。到终态失败 = 注册表与本进程对「刚才在跑
什么」的看法不一致：记 ERROR，但 EXIT 的资源释放照做——EXIT 对 spawn 方的承诺是销毁，队列与实例
表条目的释放不得取决于一次状态竞争。

**Context 记在 `finally` 里。** `run()` 在取消与失败时不返回，只在成功路径上记录等于恰好丢掉最需
要读的那些轨迹。同一个理由下，Agent 抛出的异常不出驱动任务：init 不因子进程崩溃而倒下，崩溃在
`ctx.stop_reason` 上是 ERROR，`wait` 交回轨迹而不是把它变成调用方自己的异常。

**`_contexts` 的键恒为实例表键的子集。** EXIT 把队列、注册表条目、实例表条目与 Context 一起摘掉，
于是本类持有的每一份 Context 都对应一个仍可寻址的 Agent，随时间无界增长的只有活着的 Agent 数。
代价写在 `wait` 的契约里：被 reap 的 Agent 没有 waitpid 可言。

**ZOMBIE 保留邮箱。** spec §4.1 的 ZOMBIE 靠按需激活重建队列来维持可寻址；按需激活在计划 2，此刻
移除队列会让 ZOMBIE 一条消息都收不到，而「一条消息即可复活」正是它存在的理由。

**`shutdown` 是终态。** 关闭后的事件总线静默丢弃 emit，此后 spawn 出来的 Agent 会照常干完活却完全
不可观测，因此 spawn 在关闭后报错，而不是交出一个不可观测的 Agent。

**取消落在 `finally` 里的注册表写之前时，条目会停在 RUNNING。** 内存实现的那两个 await 不挂起，
单机默认没有这个窗口；远端实现留下的孤儿由 `owner` / `heartbeat_at` 租约回收（spec §4.3）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from syndica.kernel.agent import BaseAgent
from syndica.kernel.agent_types import AgentInfo, AgentStatus, ExitMode
from syndica.kernel.context import Context
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

_TERMINAL_STATUS: Mapping[ExitMode, AgentStatus] = MappingProxyType(
    {
        ExitMode.IDLE: AgentStatus.IDLE,
        ExitMode.ZOMBIE: AgentStatus.ZOMBIE,
        # EXIT 的条目紧接着被 unregister；这一步转换只为把「注册表还认为它在 RUNNING 吗」问出来。
        ExitMode.EXIT: AgentStatus.ZOMBIE,
    }
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
    """Agent 的生命周期驱动。"""

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
        self._contexts: dict[str, Context] = {}
        self._closed = False

    async def spawn(
        self,
        factory: AgentFactory,
        *,
        query: str,
        exit_mode: ExitMode = ExitMode.ZOMBIE,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """建一个 Agent 并让它开始跑 query，返回它的 agent_id。

        工厂收下本 Runtime 的 hooks 与事件总线：Agent 自己新建一份，宿主注册的切点与订阅就都落空了。
        """
        if self._closed:
            raise _already_shut_down()
        agent = factory(self.hooks, self.events)
        agent_id = agent.agent_id
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
        # set_task 之前就跑起来（EXIT 模式下它会摘掉自己的条目）。
        self._table.put(agent_id, agent)
        task = asyncio.create_task(
            self._drive(agent_id, agent, query, exit_mode), name=f'agent:{agent_id}'
        )
        self._table.set_task(agent_id, task)
        return agent_id

    async def _drive(
        self, agent_id: str, agent: BaseAgent, query: str, exit_mode: ExitMode
    ) -> None:
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
        try:
            await agent.run(query)
        except Exception:
            logger.exception(
                '期望: agent %s 的 run 正常结束。'
                ' 实际: 它以异常结束，Context 的 stop_reason 已置为 error。'
                ' 下一步: 读 wait() 交回的 Context 定位最后一轮；'
                '要让它自愈就挂一个 llm_failure / post_tool_use hook。',
                agent_id,
            )
        finally:
            self._contexts[agent_id] = agent.context
            terminal = _TERMINAL_STATUS[exit_mode]
            if not await self.registry.compare_and_set_status(
                agent_id, expected=AgentStatus.RUNNING, new=terminal
            ):
                logger.error(
                    '期望: agent %s 在注册表里仍是 RUNNING，由本进程转去 %s。'
                    ' 实际: RUNNING -> %s 被拒，它的状态在运行期间被别人改过。'
                    ' 下一步: 用 find() 读它当前的状态；本进程已经不再驱动它。',
                    agent_id,
                    terminal.value,
                    terminal.value,
                )
            if exit_mode is ExitMode.EXIT:
                await self.broker.remove_queue(agent_id)
                await self.registry.unregister(agent_id)
                self._table.pop(agent_id)
                self._contexts.pop(agent_id, None)

    async def wait(self, agent_id: str) -> Context:
        """类比 waitpid：等驱动任务结束，交回它的轨迹。

        交回的是 Agent 自己那份 Context，不是快照：ZOMBIE 靠一条消息复活时续用的就是它，拿到副本
        的调用方会与 Agent 将来读写的那一份静默分叉。

        Agent 的异常与取消都是**被等待者**的终态，不冒充成本次调用自己的失败：崩溃在
        `ctx.stop_reason` 上是 ERROR，被取消的运行没有 stop_reason。
        """
        task = self._table.get_task(agent_id)
        if task is not None:
            await asyncio.wait({task})
        context = self._contexts.get(agent_id)
        if context is None:
            raise self._nothing_to_wait_for(agent_id)
        return context

    def _nothing_to_wait_for(self, agent_id: str) -> ContractViolation:
        if self._table.get(agent_id) is not None:
            return ContractViolation(
                expected=f'agent {agent_id!r} 的驱动任务跑过一遍并留下了 Context',
                received=(
                    '本进程 spawn 过它，但驱动任务没有进入 run：'
                    'IDLE -> RUNNING 的转换被拒（它已被别人拿走），或任务在这之前就被取消'
                ),
                next_action='用 find() 读它当前的 status 与 owner；确认它空闲后重新 spawn 一个',
            )
        return ContractViolation(
            expected=f'agent {agent_id!r} 由本进程 spawn，且结束后仍留在本进程里',
            received=(
                f'本进程没有它的运行记录（已知: {sorted(self._contexts)}）：'
                '要么它不是本进程 spawn 的，要么它以 ExitMode.EXIT 结束、已连同 Context 一起被销毁'
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

        **只投递，不激活**；spec §4.2 的 `activate` 随按需激活能力在计划 2 一并加入。

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
        """取消所有驱动任务，**等它们收完尾**再关事件总线。幂等，且是终态：此后 spawn 报错。

        取消不等于结束：任务的 `finally` 还要写注册表、EXIT 还要摘队列。不等它们收完尾，
        未结束的任务会在解释器退出时留下一串 "Task was destroyed but it is pending"。
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
