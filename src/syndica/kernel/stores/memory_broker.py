"""MessageBroker 的内存实现：asyncio 队列，单机默认。"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field

from syndica.kernel.message import DeliveryResult, Message
from syndica.kernel.protocols.broker import MissingQueueCallback

__all__ = ['InMemoryMessageBroker']


@dataclass
class _Mailbox:
    """一个 agent 的消息与两个唤醒信号。

    三者共存于一条记录而不是三个平行 dict：平行 dict 要靠每个调用点维持同键，而 remove_queue
    漏掉任何一个就会留下残留信号，让下一个等待者立刻被唤醒。
    """

    # 工厂写成带参数的泛型别名而非裸 list：strict 模式下裸工厂推不出元素类型。
    messages: list[Message] = field(default_factory=list[Message])
    # 电平信号，在还挂在 _mailboxes 上的记录里恒等于 messages 非空，只由 _refresh 派生。
    # wait_for_messages 的谓词就是它，因此它不需要在等待前清位。remove_queue 摘下记录后会连同
    # arrival 一起置位把等待者赶醒，此时两个信号只表示「别等了」，等待者必须复检记录还在不在。
    nonempty: asyncio.Event = field(default_factory=asyncio.Event)
    # 边沿信号，只由 publish 置位、由等待者在扫描前清位。take_by_correlation 不能等电平信号：
    # 邮箱里留着不匹配的消息时电平恒为高，等它会退化成忙轮询；它要等的是「又入队了一条」。
    arrival: asyncio.Event = field(default_factory=asyncio.Event)


class InMemoryMessageBroker:
    def __init__(self) -> None:
        self._mailboxes: dict[str, _Mailbox] = {}
        self._callbacks: list[MissingQueueCallback] = []

    def _refresh(self, mailbox: _Mailbox) -> None:
        """由队列是否为空派生电平信号；每个动过 messages 的地方都必须调它。

        队列内容与信号是同一个事实的两种表示。若改成在各个调用点分别 set / clear，漏掉一处
        就会让 wait_for_messages 在空邮箱上立刻返回，把阻塞等待变成忙轮询。
        """
        if mailbox.messages:
            mailbox.nonempty.set()
        else:
            mailbox.nonempty.clear()

    def _is_live(self, agent_id: str, mailbox: _Mailbox) -> bool:
        """等待者醒来后判断自己等的还是不是当前邮箱。

        remove_queue 摘下记录时会置位它的信号来赶醒等待者，所以醒来不等于有消息；重建的同名
        邮箱是另一条记录，旧等待者也不该继续等它。
        """
        return self._mailboxes.get(agent_id) is mailbox

    async def ensure_queue(self, agent_id: str) -> None:
        self._mailboxes.setdefault(agent_id, _Mailbox())

    async def publish(self, agent_id: str, message: Message) -> DeliveryResult:
        mailbox = self._mailboxes.get(agent_id)
        if mailbox is None:
            for cb in self._callbacks:
                if await cb(agent_id):
                    break
            mailbox = self._mailboxes.get(agent_id)
        if mailbox is None:
            return DeliveryResult(
                ok=False,
                reason=(
                    f'期望: agent {agent_id!r} 有可投递的邮箱。'
                    f' 实际: 邮箱不存在，且没有 on_missing_queue 回调把它建起来。'
                    f' 下一步: 先 ensure_queue，或注册一个能激活该 agent 的回调。'
                ),
            )
        # 存快照而不是调用方的对象：否则调用方 publish 之后改动它就改到了已入队的内容，
        # 而序列化的远端实现不会，换后端即行为分叉。
        mailbox.messages.append(copy.deepcopy(message))
        self._refresh(mailbox)
        mailbox.arrival.set()
        return DeliveryResult(ok=True, message_id=message.id)

    async def consume(self, agent_id: str, *, max_n: int = 0) -> list[Message]:
        mailbox = self._mailboxes.get(agent_id)
        if mailbox is None:
            return []
        count = len(mailbox.messages) if max_n <= 0 else min(max_n, len(mailbox.messages))
        taken = mailbox.messages[:count]
        del mailbox.messages[:count]
        self._refresh(mailbox)
        return taken

    async def wait_for_messages(self, agent_id: str, timeout: float | None) -> bool:
        mailbox = self._mailboxes.get(agent_id)
        if mailbox is None:
            return False
        # 非空时不能落到下面等：超时域对 timeout <= 0 一律当即到期，纯轮询（timeout=0）会因此
        # 误报空邮箱。
        if mailbox.messages:
            return True
        try:
            # `asyncio.timeout` 而不是 `asyncio.wait_for`：后者在 3.11 上把内层协程包成 Task，
            # 内层已经就绪时到达的取消会被它当成「结果已到」吃掉（`if fut.done(): return
            # fut.result()`），于是 `task.cancel()` 静默失效、调用方再也取消不掉这次等待。
            async with asyncio.timeout(timeout):
                await mailbox.nonempty.wait()
        except TimeoutError:
            return False
        if not self._is_live(agent_id, mailbox):
            return False
        return bool(mailbox.messages)

    async def take_by_correlation(
        self, agent_id: str, correlation_id: str, timeout: float | None
    ) -> Message | None:
        mailbox = self._mailboxes.get(agent_id)
        if mailbox is None:
            return None
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            # 先清边沿信号再扫描：扫描到下面 await 之间没有挂起点，所以这之后入队的消息一定会
            # 重新置位并唤醒等待，不会漏掉唤醒。
            mailbox.arrival.clear()
            for index, message in enumerate(mailbox.messages):
                if message.correlation_id == correlation_id:
                    taken = mailbox.messages.pop(index)
                    self._refresh(mailbox)
                    return taken
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return None
            try:
                # 取消必须当场生效（见 protocols/broker.py 的模块 docstring）。`asyncio.wait_for`
                # 在 3.11 上做不到：它把内层协程包成 Task，内层那个 future 已经就绪时（一条**不
                # 匹配**的消息 publish 之后 arrival 即已置位）到达的取消被它吃掉并返回内层结果，
                # 于是 `while True` 照常往下转，这次取此后不可取消——`abort` / `shutdown` /
                # `despawn` 全都要挂满整个 waiter 超时，而超时可以是几小时。
                async with asyncio.timeout(remaining):
                    await mailbox.arrival.wait()
            except TimeoutError:
                return None
            if not self._is_live(agent_id, mailbox):
                return None

    async def remove_queue(self, agent_id: str) -> None:
        mailbox = self._mailboxes.pop(agent_id, None)
        if mailbox is None:
            return
        # 摘下来的记录再没人会置位它的信号，停在上面的等待者会一直等下去——timeout=None 时是
        # 永久挂死。置位只为把它们赶醒，醒来后 _is_live 会判定邮箱已消失。
        mailbox.nonempty.set()
        mailbox.arrival.set()

    def on_missing_queue(self, cb: MissingQueueCallback) -> None:
        self._callbacks.append(cb)

    def off_missing_queue(self, cb: MissingQueueCallback) -> None:
        if cb in self._callbacks:
            self._callbacks.remove(cb)
