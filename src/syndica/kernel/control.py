"""进程内控制块，以及 turn 与 turn 之间的那道门。

按操作系统进程模型，本模块是 Agent 的 PCB 里**只在持有它的进程内有意义**的那些位，与
`LocalAgentTable` 同侧：`AgentRegistry` 回答「它现在是什么状态」，控制块提供「怎么让它停下来、
怎么把消息交给它」的机制。状态仍以注册表为准——Agent 阻塞或崩溃时无法自报状态（spec §4.3）。

**挂起是电平，中止是一次性边沿。** 挂起要能反复进出，因此用一个「现在可以往下走吗」的电平信号
表达；中止只兑现一次，`take_abort` 取走即清位——否则一次 abort 会让此后每一个周期都立刻结束，
而 abort 的语义是「停下手上这一件事」，不是「从此不再干活」。

**中止必须能穿透挂起门。** 先挂起再中止是现实中会发生的顺序（发现它跑偏 → 先停下来 → 决定不要
了）。若中止只在挂起解除之后才被看见，这个 Agent 会一直停在挂起态，而 abort 的调用方拿不到任何
反馈。因此等待用一个**边沿**唤醒信号，`resume` 与 `request_abort` 都置位它；等待前先 `clear()`
再 `wait()`，把它当边沿用——拿电平驱动这个循环会退化成不让出控制权的空转。

**「已请求挂起」与「真的停下来了」是两件事。** turn 循环可能正卡在一次 LLM 调用里，此时注册表
已经是 PAUSED 而它还在跑。`parked` / `wait_until_parked` 是这个区别唯一的观测点：界面上的暂停
按钮需要它才能显示「已停」，测试需要它才能在不靠 sleep 的前提下做确定性握手。

**DEFERRED 消息暂存在这里而不是 broker 里。** `MessageBroker` 没有「取回来又放回去」的操作
（spec §4.4：谓词无法下推到远端队列，全量取回本地过滤再放回既慢又有竞态），所以按 trigger 分流
只能发生在消费之后。代价是暂存的消息随进程消失——与已经追加进上下文的那些一样。
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from syndica.kernel.message import Message, Trigger
from syndica.kernel.protocols import MessageBroker

__all__ = [
    'BrokerTurnGate',
    'ControlBlock',
    'NullTurnGate',
    'TurnGate',
    'TurnGateDecision',
]


class ControlBlock:
    """一个 Agent 的进程内控制位：挂起门、协作式中止位、延迟消息暂存。"""

    def __init__(self) -> None:
        # 电平：置位 = 允许往下走。挂起要能反复进出，所以它表达状态而不是事件。
        self._runnable = asyncio.Event()
        self._runnable.set()
        # 边沿：resume 与 request_abort 都置位它，等待者在 wait 之前先 clear。
        self._wakeup = asyncio.Event()
        self._abort_requested = False
        self._parked = 0
        self._parked_event = asyncio.Event()
        self._deferred: list[Message] = []
        self._in_run = False

    @property
    def paused(self) -> bool:
        return not self._runnable.is_set()

    @property
    def parked(self) -> int:
        """此刻真的停在挂起门上的执行体数。"""
        return self._parked

    @property
    def abort_requested(self) -> bool:
        """中止位是否置位。读它不消费它——消费只发生在 `take_abort`。"""
        return self._abort_requested

    @property
    def in_run(self) -> bool:
        """这个 Agent 此刻是否在一次 `run()` 之内。`resume` 靠它决定该回到 RUNNING 还是 IDLE。"""
        return self._in_run

    @property
    def deferred_count(self) -> int:
        """暂存的延迟消息条数。只交出条数不交出消息，避免又开一个活引用的口子。"""
        return len(self._deferred)

    def deferred_ids(self) -> frozenset[str]:
        """暂存的延迟消息的 id。只交出 id（字符串），理由同 `deferred_count`。"""
        return frozenset(message.id for message in self._deferred)

    def enter_run(self) -> None:
        self._in_run = True

    def leave_run(self) -> None:
        self._in_run = False

    def pause(self) -> None:
        """请求挂起。幂等；它只清电平，不动中止位。"""
        self._runnable.clear()

    def resume(self) -> None:
        """解除挂起。幂等。"""
        self._runnable.set()
        self._wakeup.set()

    def request_abort(self) -> None:
        """置位协作式中止。幂等，且**必须唤醒挂起门上的等待者**。"""
        self._abort_requested = True
        self._wakeup.set()

    def take_abort(self) -> bool:
        """取走中止位并清位。一次 abort 只兑现一次。

        `hold_until_runnable` 与 `TurnGate.check_abort` 是预期消费者。中止位是一次性边沿，
        别处抢先取走会让停在挂起门上的执行体收不到它、静默重新入园，而 `request_abort` 的调用方
        拿不到任何反馈。
        """
        taken = self._abort_requested
        self._abort_requested = False
        return taken

    async def hold_until_runnable(self) -> bool:
        """等到可以继续；返回 True 表示应当中止（中止位已被消费）。

        条件检查与 `clear()` 之间没有挂起点，因此这之后的每一次置位都一定能把我们唤醒，不会漏。
        """
        while True:
            if self._abort_requested:
                return self.take_abort()
            if self._runnable.is_set():
                return False
            self._wakeup.clear()
            self._enter_park()
            try:
                await self._wakeup.wait()
            finally:
                self._leave_park()

    async def wait_until_parked(self, timeout: float | None = None) -> bool:  # noqa: ASYNC109
        """等到有执行体真的停在挂起门上；超时返回 False。

        这是一个问句，答不出就答 False，不抛异常——与 `MessageBroker.wait_for_messages` 同一条
        一致性规则，上限因此是本方法的参数。

        **返回前必须复检 `_parked`。** 被 `_parked_event` 唤醒与执行到这里之间隔着若干就绪队列
        槽位（3.11 的 `asyncio.wait_for` 把内层协程包成 Task 再挂 done-callback，实测这段窗口
        容得下先于本任务入队的每一个回调），公园可能已经在这期间被腾空。答案说的是**此刻**有人
        停着，不是「曾经有人停过」。
        """
        if self._parked > 0:
            return True
        try:
            await asyncio.wait_for(self._parked_event.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return self._parked > 0

    def defer(self, message: Message) -> None:
        """把一条 DEFERRED 消息挂进暂存队列。

        存快照而不是调用方的对象：broker 的 `publish` 已经按这条规则做过一次，暂存队列是同一条
        消息的第二个落点，两处行为必须一致。
        """
        self._deferred.append(copy.deepcopy(message))

    def take_deferred(self) -> list[Message]:
        """取走全部暂存消息，按到达顺序。取走之后本块不再持有它们。"""
        taken = self._deferred
        self._deferred = []
        return taken

    def _enter_park(self) -> None:
        self._parked += 1
        self._parked_event.set()

    def _leave_park(self) -> None:
        self._parked -= 1
        if self._parked == 0:
            self._parked_event.clear()


@dataclass(frozen=True)
class TurnGateDecision:
    """一次 turn 前置检查的结论。

    `messages` 是**元组**而不是列表：它跨越了控制面与 turn 循环的边界，交出可变列表等于让接收方
    能改到发送方还在用的东西。

    `deferred` 是这一次从邮箱取走、暂存到下一个周期再注入的那些消息的 id（只交 id，理由同
    `ControlBlock.deferred_ids`）。只给插桩用：回放要在同一轮把它们交给门，否则邮箱里的那份在
    一次 kill 之后的去向与原始 run 不同（暂存的留在控制块里，邮箱里的随队列一起没了）。
    """

    abort: bool = False
    messages: tuple[Message, ...] = ()
    deferred: tuple[str, ...] = ()


@runtime_checkable
class TurnGate(Protocol):
    """turn 循环与控制面之间的唯一接缝。

    spec §5.1 把每一轮的前三步定为「中止检查 → 挂起检查 → 消费待处理消息」，而这三件事的状态
    全都不属于 Agent 自己（§4.3）。把它们收进 `before_turn`，turn 循环就只需要在轮与轮之间问一句
    「我还能往下走吗、有什么要先看的吗」，不必知道注册表、broker 或控制块的存在。

    第 9 步（模型已经产出之后、工具执行之前）再问一次中止，必须走 `check_abort`：`before_turn`
    会顺带 consume 邮箱，而本轮已经不会再用那些消息。
    """

    async def before_turn(self, turn: int) -> TurnGateDecision:
        """在第 `turn` 轮开始之前调用；返回是否应当中止，以及要注入的消息。"""
        ...

    async def check_abort(self) -> bool:
        """第 9 步：模型已经产出之后、工具执行之前，再问一次是否应当中止。

        必须消费中止位而不是偷看：留下的位会让下一周期的 `before_turn` 立刻再中止一次，而本轮
        已经结束、那次中止没有对应的活可停。不得走 `before_turn`：它会顺带 consume 邮箱。
        """
        ...


class NullTurnGate:
    """没有控制面时的门：从不中止、从不挂起、没有消息。

    `BaseAgent` 可以脱离 `Runtime` 单独使用（计划 1 的全部测试都这么用），此时它需要一个不改变
    任何行为的门，而不是到处 `if self._gate is not None`。
    """

    async def before_turn(self, turn: int) -> TurnGateDecision:
        return TurnGateDecision()

    async def check_abort(self) -> bool:
        return False


class BrokerTurnGate:
    """接上 broker 与控制块的门。

    **周期的第一轮注入一切，其后的轮次只注入 PENDING。** spec §4.5 说 DEFERRED 的语义是「先把
    手上的事做完，然后再看这个」——而周期刚开始时手上没有事，此时再把它推迟一个周期只会白跑一次
    模型调用。判据因此是「这个周期已经干过活了吗」，由驱动循环用 `begin_cycle` 划界。

    注入顺序是**先暂存后新到**：暂存的那些消费得更早，整条注入序列因此仍是先来先到。
    """

    def __init__(self, agent_id: str, broker: MessageBroker, control: ControlBlock) -> None:
        self._agent_id = agent_id
        self._broker = broker
        self._control = control
        self._at_cycle_start = True

    def begin_cycle(self) -> None:
        """由驱动循环在每次 `run()` 之前调用，标记「下一轮是这个周期的第一轮」。"""
        self._at_cycle_start = True

    async def before_turn(self, turn: int) -> TurnGateDecision:
        if await self._control.hold_until_runnable():
            return TurnGateDecision(abort=True)
        at_cycle_start = self._at_cycle_start
        self._at_cycle_start = False
        injected: list[Message] = self._control.take_deferred() if at_cycle_start else []
        deferred: list[str] = []
        for message in await self._broker.consume(self._agent_id):
            if message.trigger is Trigger.DEFERRED and not at_cycle_start:
                self._control.defer(message)
                deferred.append(message.id)
            else:
                injected.append(message)
        return TurnGateDecision(messages=tuple(injected), deferred=tuple(deferred))

    async def check_abort(self) -> bool:
        return self._control.take_abort()
