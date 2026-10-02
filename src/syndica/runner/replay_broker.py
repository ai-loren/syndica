"""回放的 broker（M3 D11）：消息照常投递，Agent 在每个决定点上看到的与原始 run 那一刻看到的相同。

原始 run 里，一条消息落在收件人的哪一刻，取决于收件人与发件人各自跑了多久；回放里外部世界都不花
时间，同一条消息可能早到也可能晚到——请求与记录对不上，派活的一方拿到的结果也可能不同，而内核
什么都没改。所以收件人有记录时（`plan` 交回非 None），按记录在三个决定点上答：

- **门在第 N 轮开始前 `consume`：** 交回记录里第 N 轮从邮箱取走的那几条——注入的（按注入的先后；
  早先暂存的从控制块里出来，不在其中）与暂存的（内核照样把它们暂存到下一个周期）。还没到的等它到，
  记录里属于以后的留在邮箱里。
- **一个周期刚结束时问「还有没有没处理的输入」：** 答记录里的那个答案。紧接着开了下一个周期的
  （`continued`），等它第一轮要注入的消息有一条到了，答有；别的答没有，Agent 照常停下来——原始
  run 里它停了，派活的一方此刻拿到了它的结果。手上有暂存的消息时内核不问，直接开下一个周期。
- **停下来之后（停在邮箱上、或进了 zombie 又被叫醒）：** 它第一轮要注入的消息有一条到了就醒。

邮箱随 Agent 停下被摘掉时，记录里属于以后几轮的消息留着，邮箱重建时放回去；下一轮要注入的那些
已经到了时，等 Agent 进了 zombie 替它们把它叫醒：原始 run 里它们是之后才到的（之前到的要么在那一
轮被注入，要么被门暂存、不在邮箱里），到的那一刻叫醒了它。

宿主发的消息（入口、人的答复）不在记录里（它们没有发送方的 span，消费时没有因果边），到了就照常
投递。

等一条还没到的消息时，这次等待交给回放登记（`hold`），连同它是不是周期刚结束、记录说紧接着开了
下一个周期的那一次（原始 run 里 Agent 没有停在那里，kill 落不到那里）：run 安静下来时还在等，就是
一处分叉。

没有记录的邮箱就是一个普通的内存 broker，契约与 `InMemoryMessageBroker` 相同。
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field

from syndica.kernel import DeliveryResult, Message, MissingQueueCallback

__all__ = ['GIVE_UP', 'Hold', 'Plan', 'ScheduledBroker']

# 回放不再等了（允许分叉、run 已经安静下来）时交给等待者的值：拿邮箱里有的接着跑。
GIVE_UP = object()

# 登记一次等待：（收件人、第几轮、还没到的 id、等待者、是不是记录说紧接着开下一个周期的那次周期
# 边界）→ 撤销这次登记。
Hold = Callable[[str, int, tuple[str, ...], 'asyncio.Future[object]', bool], Callable[[], None]]

# Agent 进 zombie 要走完它的收尾；替它叫醒之前最多等这么多轮事件循环。
_REVIVE_TRIES = 50


@dataclass(frozen=True)
class Plan:
    """一个收件人的记录：每一轮开始前注入的消息（轮 → id，按注入的先后）、哪些轮开了一个紧接着
    上一个周期的新周期，以及门在哪一轮暂存了哪些延迟消息（轮 → id，按取走的先后）。"""

    messages: Mapping[int, tuple[str, ...]]
    continued: Collection[int] = frozenset()
    deferred: Mapping[int, tuple[str, ...]] = field(default_factory=dict[int, tuple[str, ...]])

    def taken(self, turn: int) -> tuple[str, ...]:
        """门在第 `turn` 轮开始前从邮箱取走的：那一轮注入的（去掉早先暂存的，它们从控制块里出来），
        加上那一轮暂存的。"""
        stashed = {message_id for ids in self.deferred.values() for message_id in ids}
        injected = tuple(
            message_id for message_id in self.messages.get(turn, ()) if message_id not in stashed
        )
        return injected + self.deferred.get(turn, ())


@dataclass
class _Mailbox:
    messages: list[Message] = field(default_factory=list[Message])
    # 每次入队与摘邮箱都把它们全部叫醒；醒来不等于等到了，等待者自己复检。
    waiters: list[asyncio.Future[object]] = field(default_factory=list[asyncio.Future[object]])

    def wake(self) -> None:
        for waiter in self.waiters:
            if not waiter.done():
                waiter.set_result(None)
        self.waiters.clear()


class ScheduledBroker:
    """`plan(agent_id)` 是这个收件人的记录，None 是没有记录；`next_turn(agent_id)` 是它下一个要开的
    轮；`from_host(message)` 说一条消息是不是宿主发的；`asleep(agent_id)` 说它是不是已经停在
    zombie 里、叫得醒了。"""

    def __init__(
        self,
        plan: Callable[[str], Plan | None],
        next_turn: Callable[[str], int],
        hold: Hold,
        *,
        from_host: Callable[[Message], bool] = lambda message: False,
        asleep: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._plan = plan
        self._next_turn = next_turn
        self._hold = hold
        self._from_host = from_host
        self._asleep = asleep
        self._boxes: dict[str, _Mailbox] = {}
        self._callbacks: list[MissingQueueCallback] = []
        # 邮箱摘掉时留下的、记录里属于以后几轮的消息；邮箱重建时放回去。
        self._kept: dict[str, list[Message]] = {}
        # Agent 最近一次停下来时要开的那一轮：停下来之后，问「有没有输入」答的是邮箱里有什么。
        self._slept: dict[str, int] = {}
        self._reviving: set[asyncio.Task[None]] = set()

    async def ensure_queue(self, agent_id: str) -> None:
        if agent_id in self._boxes:
            return
        box = self._boxes[agent_id] = _Mailbox()
        kept = self._kept.pop(agent_id, None)
        if kept:
            box.messages.extend(kept)
        self._slept[agent_id] = self._next_turn(agent_id)

    async def publish(self, agent_id: str, message: Message) -> DeliveryResult:
        box = self._boxes.get(agent_id)
        if box is None:
            for callback in list(self._callbacks):
                if await callback(agent_id):
                    break
            box = self._boxes.get(agent_id)
        if box is None:
            return DeliveryResult(
                ok=False,
                reason=(
                    f'期望: agent {agent_id!r} 有可投递的邮箱。'
                    ' 实际: 邮箱不存在，且没有 on_missing_queue 回调把它建起来。'
                    ' 下一步: 先 ensure_queue，或注册一个能激活该 agent 的回调。'
                ),
            )
        # 确认邮箱与入队之间没有挂起点：remove_queue 插不进来。
        box.messages.append(copy.deepcopy(message))
        box.wake()
        return DeliveryResult(ok=True, message_id=message.id)

    async def consume(self, agent_id: str, *, max_n: int = 0) -> list[Message]:
        box = self._boxes.get(agent_id)
        if box is None:
            return []
        plan = self._plan(agent_id)
        if plan is None:
            return _taken(box, box.messages, max_n)
        turn = self._next_turn(agent_id)
        wanted = plan.taken(turn)
        await self._until(agent_id, box, turn, wanted, every=True)
        if self._boxes.get(agent_id) is not box:
            return []
        by_id = {message.id: message for message in box.messages}
        chosen = [message for message in box.messages if self._from_host(message)] + [
            by_id[message_id] for message_id in wanted if message_id in by_id
        ]
        return _taken(box, chosen, max_n)

    async def wait_for_messages(self, agent_id: str, timeout: float | None) -> bool:
        box = self._boxes.get(agent_id)
        if box is None:
            return False
        plan = self._plan(agent_id)
        if plan is None:
            if not box.messages:
                await self._woken_until(agent_id, box, timeout, lambda: bool(box.messages))
            return self._boxes.get(agent_id) is box and bool(box.messages)
        turn = self._next_turn(agent_id)
        recorded = plan.taken(turn)
        wanted = frozenset(recorded)

        def present() -> bool:
            return any(m.id in wanted or self._from_host(m) for m in box.messages)

        if timeout is None or timeout > 0:
            # 停在邮箱上等：它停下来了。
            self._slept[agent_id] = turn
            if not present():
                if wanted:
                    await self._until(agent_id, box, turn, recorded, every=False)
                else:
                    await self._woken_until(agent_id, box, timeout, present)
            return self._boxes.get(agent_id) is box and present()
        if self._slept.get(agent_id) == turn:
            return present()
        # 一个周期刚结束：答原始 run 在这一刻的答案。
        if any(self._from_host(message) for message in box.messages):
            return True
        if turn not in plan.continued:
            return False
        await self._until(agent_id, box, turn, recorded, every=False, boundary=True)
        return self._boxes.get(agent_id) is box and present()

    async def take_by_correlation(
        self, agent_id: str, correlation_id: str, timeout: float | None
    ) -> Message | None:
        box = self._boxes.get(agent_id)
        if box is None:
            return None
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            # 找到与交回之间没有挂起点：取消落不进去，匹配的那条要么交回、要么还在邮箱里。
            for index, message in enumerate(box.messages):
                if message.correlation_id == correlation_id:
                    return box.messages.pop(index)
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return None
            if not await _woken(box, remaining):
                return None
            if self._boxes.get(agent_id) is not box:
                return None

    async def remove_queue(self, agent_id: str) -> None:
        box = self._boxes.pop(agent_id, None)
        if box is None:
            return
        plan = self._plan(agent_id)
        if plan is not None:
            turn = self._next_turn(agent_id)
            later = {
                message_id
                for at, ids in (*plan.messages.items(), *plan.deferred.items())
                if at >= turn
                for message_id in ids
            }
            kept = [
                message
                for message in box.messages
                if message.id in later and not self._from_host(message)
            ]
            if kept:
                self._kept[agent_id] = [*self._kept.get(agent_id, []), *kept]
                due = set(plan.taken(turn))
                if self._asleep is not None and any(message.id in due for message in kept):
                    task = asyncio.get_running_loop().create_task(self._revive(agent_id))
                    self._reviving.add(task)
                    task.add_done_callback(self._reviving.discard)
        box.wake()

    def on_missing_queue(self, cb: MissingQueueCallback) -> None:
        self._callbacks.append(cb)

    def off_missing_queue(self, cb: MissingQueueCallback) -> None:
        if cb in self._callbacks:
            self._callbacks.remove(cb)

    async def _revive(self, agent_id: str) -> None:
        """下一轮要注入的消息在邮箱摘掉之前就到了：等它进了 zombie，替它们叫醒它（原始 run 里是它们
        之后才到、到的那一刻叫醒了它）。邮箱已经被别的投递重建了就不用叫。"""
        assert self._asleep is not None
        for _ in range(_REVIVE_TRIES):
            await asyncio.sleep(0)
            if agent_id in self._boxes or agent_id not in self._kept:
                return
            if await self._asleep(agent_id):
                for callback in list(self._callbacks):
                    if await callback(agent_id):
                        return
                return

    async def _until(
        self,
        agent_id: str,
        box: _Mailbox,
        turn: int,
        wanted: tuple[str, ...],
        *,
        every: bool,
        boundary: bool = False,
    ) -> None:
        """等记录里第 `turn` 轮要从邮箱取走的消息到（`every` 时全部到，否则到一条——宿主的消息也
        算），或邮箱被摘掉，或回放放弃。不看超时：原始 run 在这一刻已经收到了它们，它们到不到只取决
        于别的 Agent。`boundary`：这是周期刚结束、记录说紧接着开了下一个周期的那次等待。"""
        loop = asyncio.get_running_loop()
        while self._boxes.get(agent_id) is box:
            present = {message.id for message in box.messages}
            missing = tuple(message_id for message_id in wanted if message_id not in present)
            if every and not missing:
                return
            if not every and (
                len(missing) < len(wanted)
                or any(self._from_host(message) for message in box.messages)
            ):
                return
            waiter: asyncio.Future[object] = loop.create_future()
            box.waiters.append(waiter)
            release = self._hold(agent_id, turn, missing, waiter, boundary)
            try:
                if await waiter is GIVE_UP:
                    return
            finally:
                release()
                if waiter in box.waiters:
                    box.waiters.remove(waiter)

    async def _woken_until(
        self, agent_id: str, box: _Mailbox, timeout: float | None, ready: Callable[[], bool]
    ) -> None:
        """等到 `ready()`、邮箱被摘掉或超时；`timeout <= 0` 只看一眼。"""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while self._boxes.get(agent_id) is box and not ready():
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return
            if not await _woken(box, remaining):
                return


def _taken(box: _Mailbox, chosen: list[Message], max_n: int) -> list[Message]:
    taken = list(chosen) if max_n <= 0 else chosen[:max_n]
    gone = {id(message) for message in taken}
    box.messages[:] = [message for message in box.messages if id(message) not in gone]
    return taken


async def _woken(box: _Mailbox, remaining: float | None) -> bool:
    """等下一次入队或摘邮箱；超时交回 False。`asyncio.timeout` 而不是 `asyncio.wait_for`：后者在
    3.11 上会吃掉内层已经就绪时到达的取消。"""
    waiter: asyncio.Future[object] = asyncio.get_running_loop().create_future()
    box.waiters.append(waiter)
    try:
        async with asyncio.timeout(remaining):
            await waiter
    except TimeoutError:
        return False
    finally:
        if waiter in box.waiters:
            box.waiters.remove(waiter)
    return True
