import asyncio
import copy
import time
from collections.abc import Awaitable
from typing import TypeVar

import pytest

from syndica.kernel.contracts.broker import MessageBrokerContract
from syndica.kernel.message import DeliveryResult, Message
from syndica.kernel.protocols.broker import MessageBroker
from syndica.kernel.stores.memory_broker import InMemoryMessageBroker

# 等待期间允许消耗的 CPU 时间上限。忙轮询会把等待窗口的两成以上烧成 CPU，真正的阻塞等待只花
# 十分之一毫秒；上限必须留在两者之间，放宽到接近 _SPIN_TIMEOUT 就等于把这条断言改成永真。
# time.process_time() 统计进程内所有线程，带后台连接线程的远端实现会被它误判，所以这条断言只
# 能留在本实现自己的测试里；契约里由 take_by_correlation 的 docstring 用文字声明同一要求。
_SPIN_TIMEOUT = 0.1
_MAX_WAITING_CPU = 0.005

# 被 remove_queue 删掉的邮箱上，等待者必须立刻醒，而不是白等满自己的 _ORPHAN_TIMEOUT。
# 上限取超时的三分之一：唤醒是同步置位，实测在微秒量级，留这么宽只是给调度抖动让路。
_ORPHAN_TIMEOUT = 0.3
_MAX_WAKE_LATENCY = 0.1

_PARK_DELAY = 0.05
_LONG_ENOUGH = 1.0

T = TypeVar('T')


async def _park(task: asyncio.Task, call: str) -> None:
    await asyncio.sleep(_PARK_DELAY)
    assert not task.done(), (
        f'期望 {call} 在空邮箱上阻塞住，后面才有「唤醒」可言。'
        f' 实际它在 {_PARK_DELAY}s 内就返回了 {task.result()!r}。'
        ' 先修等待语义：空邮箱上立刻返回会把轮次循环从阻塞等待变成忙轮询。'
    )


def _msg(content: str = 'hi', *, correlation_id: str | None = None) -> Message:
    return Message(sender='s', receiver='a1', content=content, correlation_id=correlation_id)


class _AcksAfterTaking(InMemoryMessageBroker):
    """刻意不合规：摘掉与返回之间留一个挂起点（ack / 审计写入的位置）。

    取消落进那个窗口就销毁一条答复，而这正是那两条契约用例要挡的东西。摘掉与返回之间不留挂起点的
    实现走不进它们的「被取消」那一支，于是那一支的断言对合规实现从不执行——义务本身还活着没有任何
    东西证明。这个类就是那个证明：它必须让那两条用例变红。
    """

    async def take_by_correlation(
        self,
        agent_id: str,
        correlation_id: str,
        timeout: float | None,  # noqa: ASYNC109
    ) -> Message | None:
        taken = await super().take_by_correlation(agent_id, correlation_id, timeout)
        if taken is not None:
            await asyncio.sleep(0)
        return taken


class _KeepsThePendingEntryAwayFromConsume(InMemoryMessageBroker):
    """刻意不合规，而且是**合法替换**那一型：既不抛也不说谎，只是另一种选择。

    被取消的那次取把消息挪进一份 pending，此后只对 `take_by_correlation` 与 `wait_for_messages`
    可见——Redis 消费组的 pending entry 原样照抄就是这个形状。它满足「取消之后那条消息还能再取一次」
    的每一个字，而门靠 `consume` 把它注入下一轮，于是那条答复静默消失、`publish` 早已回过 ok=True。
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending: dict[str, list[Message]] = {}

    async def take_by_correlation(
        self,
        agent_id: str,
        correlation_id: str,
        timeout: float | None,  # noqa: ASYNC109
    ) -> Message | None:
        held = self._pending.get(agent_id, [])
        for index, message in enumerate(held):
            if message.correlation_id == correlation_id:
                return held.pop(index)
        taken = await super().take_by_correlation(agent_id, correlation_id, timeout)
        if taken is None:
            return None
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            self._pending.setdefault(agent_id, []).append(taken)
            raise
        return taken

    async def wait_for_messages(
        self,
        agent_id: str,
        timeout: float | None,  # noqa: ASYNC109
    ) -> bool:
        if self._pending.get(agent_id):
            return True
        return await super().wait_for_messages(agent_id, timeout)


async def _wait_for_as_in_3_11(awaitable: Awaitable[T], seconds: float | None) -> T:
    """Python 3.11 的 `asyncio.wait_for` 的写法：内层 future 已经就绪时到达的取消被当成「结果已到」
    吞掉。3.12 起 `wait_for` 改用 `asyncio.timeout`，不再吞，所以照抄在这里，每个解释器上都有它。"""
    loop = asyncio.get_running_loop()
    inner = asyncio.ensure_future(awaitable)
    waiter: asyncio.Future[None] = loop.create_future()

    def release(_: object) -> None:
        if not waiter.done():
            waiter.set_result(None)

    inner.add_done_callback(release)
    expiry = None if seconds is None else loop.call_later(seconds, release, None)
    try:
        try:
            await waiter
        except asyncio.CancelledError:
            if inner.done():
                return inner.result()
            inner.remove_done_callback(release)
            inner.cancel()
            raise
        if inner.done():
            return inner.result()
        inner.remove_done_callback(release)
        inner.cancel()
        raise TimeoutError
    finally:
        if expiry is not None:
            expiry.cancel()


class _SwallowsTheCancel(InMemoryMessageBroker):
    """刻意不合规：两个阻塞方法的内层等待都用 3.11 的 `asyncio.wait_for`，即本实现修掉之前的写法。

    内层 future 已经就绪时到达的取消被它当成「结果已到」吃掉：`take_by_correlation` 因此挂满整个
    超时，而 `wait_for_messages` 更隐蔽——它照常交回 True，取消静默失效。
    """

    async def wait_for_messages(
        self,
        agent_id: str,
        timeout: float | None,  # noqa: ASYNC109
    ) -> bool:
        mailbox = self._mailboxes.get(agent_id)
        if mailbox is None:
            return False
        if mailbox.messages:
            return True
        try:
            await _wait_for_as_in_3_11(mailbox.nonempty.wait(), timeout)
        except TimeoutError:
            return False
        if self._mailboxes.get(agent_id) is not mailbox:
            return False
        return bool(mailbox.messages)

    async def take_by_correlation(
        self,
        agent_id: str,
        correlation_id: str,
        timeout: float | None,  # noqa: ASYNC109
    ) -> Message | None:
        mailbox = self._mailboxes.get(agent_id)
        if mailbox is None:
            return None
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            mailbox.arrival.clear()
            for index, message in enumerate(mailbox.messages):
                if message.correlation_id == correlation_id:
                    return mailbox.messages.pop(index)
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return None
            try:
                await _wait_for_as_in_3_11(mailbox.arrival.wait(), remaining)
            except TimeoutError:
                return None
            if self._mailboxes.get(agent_id) is not mailbox:
                return None


class _PublishesIntoAQueueItAlreadyRemoved(InMemoryMessageBroker):
    async def publish(self, agent_id: str, message: Message) -> DeliveryResult:
        mailbox = self._mailboxes.get(agent_id)
        if mailbox is None:
            return await super().publish(agent_id, message)
        checked = asyncio.Event()
        removed = asyncio.Event()

        async def remove_between_check_and_write() -> None:
            checked.set()
            await self.remove_queue(agent_id)
            removed.set()

        removal = asyncio.create_task(remove_between_check_and_write())
        await checked.wait()
        await removed.wait()
        mailbox.messages.append(copy.deepcopy(message))
        self._refresh(mailbox)
        await removal
        return DeliveryResult(ok=True, message_id=message.id)


class TestInMemoryMessageBroker(MessageBrokerContract):
    @pytest.fixture
    def broker(self) -> MessageBroker:
        return InMemoryMessageBroker()

    async def test_take_by_correlation_does_not_burn_cpu_while_waiting(
        self, broker: MessageBroker
    ) -> None:
        """忙轮询与阻塞等待返回同样的结果、耗掉同样的墙钟时间，只有 CPU 时间能区分两者。"""
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg('other'))
        started = time.process_time()
        assert await broker.take_by_correlation('a1', 'nope', timeout=_SPIN_TIMEOUT) is None
        assert time.process_time() - started < _MAX_WAITING_CPU

    async def test_wait_for_messages_does_not_report_a_mailbox_a_consumer_already_drained(
        self, broker: MessageBroker
    ) -> None:
        """publish 与 consume 之间没有挂起点，等待者只能在两者都做完后才醒。

        这条编排能确定地造出「醒来时邮箱已空」，靠的是本实现单线程且两个方法都不挂起；带网络
        往返的后端排不出这个交错，所以它不进契约。
        """
        await broker.ensure_queue('a1')
        waiting = asyncio.create_task(broker.wait_for_messages('a1', timeout=_LONG_ENOUGH))
        await _park(waiting, 'wait_for_messages')
        await broker.publish('a1', _msg())
        await broker.consume('a1')
        assert await waiting is False

    async def test_wait_for_messages_reports_false_when_its_mailbox_is_removed_after_a_publish(
        self, broker: MessageBroker
    ) -> None:
        """publish 让等待者的 future 就绪，remove_queue 在它重新被调度之前就落了地。

        醒来时手里那条记录已经不是 a1 的邮箱，答 True 只会让调用方去 consume 一个不存在的邮箱。
        这个交错同样只在单线程本地实现上确定，所以它不进契约。
        """
        await broker.ensure_queue('a1')
        waiting = asyncio.create_task(broker.wait_for_messages('a1', timeout=_LONG_ENOUGH))
        await _park(waiting, 'wait_for_messages')
        await broker.publish('a1', _msg())
        await broker.remove_queue('a1')
        assert await waiting is False

    async def test_remove_queue_wakes_a_wait_for_messages_waiter_without_burning_its_timeout(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        waiting = asyncio.create_task(broker.wait_for_messages('a1', timeout=_ORPHAN_TIMEOUT))
        await _park(waiting, 'wait_for_messages')
        started = time.monotonic()
        await broker.remove_queue('a1')
        assert await waiting is False
        assert time.monotonic() - started < _MAX_WAKE_LATENCY

    async def test_remove_queue_wakes_a_take_by_correlation_waiter_without_burning_its_timeout(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        waiting = asyncio.create_task(broker.take_by_correlation('a1', 'c1', _ORPHAN_TIMEOUT))
        await _park(waiting, 'take_by_correlation')
        started = time.monotonic()
        await broker.remove_queue('a1')
        assert await waiting is None
        assert time.monotonic() - started < _MAX_WAKE_LATENCY


async def test_the_cancel_sweep_catches_a_broker_that_destroys_the_match():
    with pytest.raises(AssertionError) as info:
        await MessageBrokerContract().test_cancelling_take_by_correlation_leaves_the_match_takeable(
            _AcksAfterTaking()
        )
    assert '仍能再取一次' in str(info.value)


async def test_the_consume_path_case_catches_a_broker_that_destroys_the_match():
    suite = MessageBrokerContract()
    case = suite.test_a_cancelled_take_leaves_the_match_on_the_ordinary_consume_path
    with pytest.raises(AssertionError) as info:
        await case(_AcksAfterTaking())
    assert 'consume' in str(info.value)


async def test_the_prompt_cancel_case_catches_a_broker_that_swallows_the_cancel():
    suite = MessageBrokerContract()
    with pytest.raises(AssertionError) as info:
        await suite.test_cancelling_take_by_correlation_ends_it_instead_of_burning_its_timeout(
            _SwallowsTheCancel()
        )
    assert 'asyncio.timeout' in str(info.value)


async def test_the_prompt_cancel_case_catches_a_wait_for_messages_that_swallows_the_cancel():
    suite = MessageBrokerContract()
    with pytest.raises(AssertionError) as info:
        await suite.test_cancelling_wait_for_messages_ends_it_instead_of_burning_its_timeout(
            _SwallowsTheCancel()
        )
    assert '静默失效' in str(info.value)


async def test_the_consume_path_case_catches_a_pending_entry_consume_cannot_see():
    """「仍在邮箱里」若只定义成「再 take 一次还取得到」，这个实现整套契约全绿。

    它就是评审那条 Critical 的形状：接进 Runtime 之后，一次被中止的等待返回 ABORTED，而那条答复
    既不在 `consume` 里、也不在轨迹里、零 ERROR，`send` 却早已回过 ok=True。
    """
    suite = MessageBrokerContract()
    broker = _KeepsThePendingEntryAwayFromConsume()
    # 先证明它确实是「合法替换」而不是一个坏得显眼的实现：旧的那条义务它满足。
    await suite.test_cancelling_take_by_correlation_leaves_the_match_takeable(broker)

    case = suite.test_a_cancelled_take_leaves_the_match_on_the_ordinary_consume_path
    with pytest.raises(AssertionError) as info:
        await case(_KeepsThePendingEntryAwayFromConsume())
    assert 'consume 交出' in str(info.value)


async def test_the_publish_receipt_case_catches_a_message_written_to_a_removed_queue():
    suite = MessageBrokerContract()
    with pytest.raises(AssertionError) as info:
        await suite.test_publish_ok_means_the_message_is_still_in_the_mailbox(
            _PublishesIntoAQueueItAlreadyRemoved()
        )
    assert '成功回执' in str(info.value)
