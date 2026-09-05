import asyncio
import time

import pytest

from syndica.kernel.contracts.broker import MessageBrokerContract
from syndica.kernel.message import Message
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


async def _park(task: asyncio.Task, call: str) -> None:
    await asyncio.sleep(_PARK_DELAY)
    assert not task.done(), (
        f'期望 {call} 在空邮箱上阻塞住，后面才有「唤醒」可言。'
        f' 实际它在 {_PARK_DELAY}s 内就返回了 {task.result()!r}。'
        ' 先修等待语义：空邮箱上立刻返回会把轮次循环从阻塞等待变成忙轮询。'
    )


def _msg(content: str = 'hi', *, correlation_id: str | None = None) -> Message:
    return Message(sender='s', receiver='a1', content=content, correlation_id=correlation_id)


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
