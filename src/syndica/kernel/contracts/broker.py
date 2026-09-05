"""MessageBroker 契约。任何实现都必须通过。

「可替换」若无测试证明即是空话（spec §13.3）。本套件随发行版发布，第三方写 backend 时继承它并
提供 broker fixture 即可验证合规。套件依赖的性质逐条写在 `protocols.broker` 的模块 docstring
里；断言只看结果与类型，不看错误文案，措辞因此仍然自由。

**运行前提**见 `syndica.kernel.contracts.base`：async 用例要有插件驱动，
继承自 `ContractSuite` 的守卫会把配置缺失变成显式失败。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from syndica.kernel.contracts.base import ContractSuite
from syndica.kernel.message import Message
from syndica.kernel.protocols.broker import MessageBroker

__all__ = ['MessageBrokerContract']

# 阻塞类用例的超时与「确实阻塞过」的下限。下限只取超时的四成：asyncio 的超时只会因负载变长、
# 不会变短，所以放宽下限不会让用例失去意义，而它要挡的失败是立刻返回（约 0 秒）。
_BLOCK_TIMEOUT = 0.05
_MIN_BLOCKED = 0.02

# 等待者停稳所需的时间，以及「本该醒的等待者没醒」的判定上限。_WAKE_TIMEOUT 必须远大于
# _BLOCK_TIMEOUT，才不会把慢后端误判成挂死；它的作用是让回归表现为失败而不是挂住整个 CI。
_PARK_DELAY = 0.05
_WAKE_TIMEOUT = 5.0

# 非匹配噪声的间隔，必须显著小于 _BLOCK_TIMEOUT，等待者才会在一个超时窗口里被反复唤醒。
_NOISE_INTERVAL = 0.005


def _msg(content: str = 'hi', *, correlation_id: str | None = None) -> Message:
    return Message(sender='s', receiver='a1', content=content, correlation_id=correlation_id)


async def _park(task: asyncio.Task[Any], call: str) -> None:
    """等到 task 真的停在等待点上。没停住的话后面的唤醒断言就是空转，所以这里断掉。"""
    await asyncio.sleep(_PARK_DELAY)
    assert not task.done(), (
        f'期望 {call} 在空邮箱上阻塞住，后面才有「唤醒」可言。'
        f' 实际它在 {_PARK_DELAY}s 内就返回了 {task.result()!r}。'
        ' 先修等待语义：空邮箱上立刻返回会把轮次循环从阻塞等待变成忙轮询。'
    )


class MessageBrokerContract(ContractSuite):
    """子类必须提供一个 `broker` fixture，且每个用例拿到的必须是没有任何邮箱的空 broker。"""

    async def test_publish_then_consume_preserves_order(self, broker: MessageBroker) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg('one'))
        await broker.publish('a1', _msg('two'))
        got = await broker.consume('a1')
        assert [m.content for m in got] == ['one', 'two']

    async def test_publish_returns_the_message_id_as_receipt(self, broker: MessageBroker) -> None:
        await broker.ensure_queue('a1')
        message = _msg()
        result = await broker.publish('a1', message)
        assert result.ok is True
        assert result.message_id == message.id

    async def test_consume_is_exactly_once(self, broker: MessageBroker) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg())
        assert len(await broker.consume('a1')) == 1
        assert await broker.consume('a1') == []

    async def test_consume_respects_max_n(self, broker: MessageBroker) -> None:
        await broker.ensure_queue('a1')
        for i in range(3):
            await broker.publish('a1', _msg(str(i)))
        first = await broker.consume('a1', max_n=2)
        assert [m.content for m in first] == ['0', '1']
        assert [m.content for m in await broker.consume('a1')] == ['2']

    async def test_consume_on_unknown_queue_returns_empty(self, broker: MessageBroker) -> None:
        assert await broker.consume('nobody') == []

    async def test_publish_stores_a_snapshot_of_the_callers_message(
        self, broker: MessageBroker
    ) -> None:
        """内存实现放行调用方改存储、远端实现不放行，分叉只在换后端时才暴露。"""
        message = _msg('original')
        await broker.ensure_queue('a1')
        await broker.publish('a1', message)
        message.content = 'mutated'
        assert [m.content for m in await broker.consume('a1')] == ['original']

    async def test_one_message_published_twice_yields_independent_copies(
        self, broker: MessageBroker
    ) -> None:
        message = _msg('shared')
        await broker.ensure_queue('a1')
        await broker.ensure_queue('a2')
        await broker.publish('a1', message)
        await broker.publish('a2', message)
        taken = await broker.consume('a1')
        taken[0].content = 'mutated'
        assert [m.content for m in await broker.consume('a2')] == ['shared']

    async def test_wait_for_messages_returns_true_when_already_queued(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg())
        assert await broker.wait_for_messages('a1', timeout=0.1) is True

    async def test_wait_for_messages_with_zero_timeout_still_reports_a_full_mailbox(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg())
        assert await broker.wait_for_messages('a1', timeout=0) is True

    async def test_wait_for_messages_wakes_on_publish(self, broker: MessageBroker) -> None:
        await broker.ensure_queue('a1')

        async def publish_soon() -> None:
            await asyncio.sleep(0.01)
            await broker.publish('a1', _msg())

        task = asyncio.create_task(publish_soon())
        assert await broker.wait_for_messages('a1', timeout=1.0) is True
        await task

    async def test_wait_for_messages_returns_false_on_timeout(self, broker: MessageBroker) -> None:
        await broker.ensure_queue('a1')
        assert await broker.wait_for_messages('a1', timeout=0.01) is False

    async def test_wait_for_messages_on_unknown_queue_returns_false(
        self, broker: MessageBroker
    ) -> None:
        assert await broker.wait_for_messages('nobody', timeout=0.01) is False

    async def test_wait_for_messages_still_blocks_after_consume_drains_the_mailbox(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg())
        await broker.consume('a1')
        started = time.monotonic()
        assert await broker.wait_for_messages('a1', timeout=_BLOCK_TIMEOUT) is False
        assert time.monotonic() - started >= _MIN_BLOCKED

    async def test_wait_for_messages_still_blocks_after_take_by_correlation_drains_the_mailbox(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg('answer', correlation_id='c1'))
        assert await broker.take_by_correlation('a1', 'c1', timeout=1.0) is not None
        started = time.monotonic()
        assert await broker.wait_for_messages('a1', timeout=_BLOCK_TIMEOUT) is False
        assert time.monotonic() - started >= _MIN_BLOCKED

    async def test_wait_for_messages_still_blocks_on_a_mailbox_rebuilt_after_removal(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg())
        await broker.remove_queue('a1')
        await broker.ensure_queue('a1')
        started = time.monotonic()
        assert await broker.wait_for_messages('a1', timeout=_BLOCK_TIMEOUT) is False
        assert time.monotonic() - started >= _MIN_BLOCKED

    async def test_take_by_correlation_leaves_others_queued(self, broker: MessageBroker) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg('other'))
        await broker.publish('a1', _msg('answer', correlation_id='c1'))
        got = await broker.take_by_correlation('a1', 'c1', timeout=0.1)
        assert got is not None and got.content == 'answer'
        assert [m.content for m in await broker.consume('a1')] == ['other']

    async def test_take_by_correlation_times_out_without_match(self, broker: MessageBroker) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg('other'))
        assert await broker.take_by_correlation('a1', 'nope', timeout=0.01) is None

    async def test_take_by_correlation_times_out_while_non_matching_traffic_keeps_arriving(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')

        async def publish_noise_forever() -> None:
            while True:
                await asyncio.sleep(_NOISE_INTERVAL)
                await broker.publish('a1', _msg('noise'))

        noise = asyncio.create_task(publish_noise_forever())
        try:
            got = await asyncio.wait_for(
                broker.take_by_correlation('a1', 'c1', timeout=_BLOCK_TIMEOUT),
                timeout=_WAKE_TIMEOUT,
            )
        finally:
            noise.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await noise
        assert got is None

    async def test_take_by_correlation_wakes_on_a_matching_publish(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg('other'))

        async def reply_soon() -> None:
            await asyncio.sleep(0.01)
            await broker.publish('a1', _msg('answer', correlation_id='c1'))

        task = asyncio.create_task(reply_soon())
        got = await broker.take_by_correlation('a1', 'c1', timeout=1.0)
        assert got is not None and got.content == 'answer'
        await task

    async def test_take_by_correlation_on_unknown_queue_returns_none(
        self, broker: MessageBroker
    ) -> None:
        assert await broker.take_by_correlation('nobody', 'c1', timeout=0.01) is None

    async def test_missing_queue_triggers_callback(self, broker: MessageBroker) -> None:
        activated: list[str] = []

        async def activate(agent_id: str) -> bool:
            activated.append(agent_id)
            await broker.ensure_queue(agent_id)
            return True

        broker.on_missing_queue(activate)
        result = await broker.publish('lazy', _msg())
        assert result.ok is True
        assert activated == ['lazy']

    async def test_publish_stops_at_the_first_callback_that_claims_the_agent(
        self, broker: MessageBroker
    ) -> None:
        called: list[str] = []

        async def claim(agent_id: str) -> bool:
            called.append('claim')
            await broker.ensure_queue(agent_id)
            return True

        async def never_reached(agent_id: str) -> bool:
            called.append('never_reached')
            return True

        broker.on_missing_queue(claim)
        broker.on_missing_queue(never_reached)
        assert (await broker.publish('lazy', _msg())).ok is True
        assert called == ['claim']

    async def test_publish_tries_later_callbacks_when_earlier_ones_decline(
        self, broker: MessageBroker
    ) -> None:
        called: list[str] = []

        async def decline(agent_id: str) -> bool:
            called.append('decline')
            return False

        async def claim(agent_id: str) -> bool:
            called.append('claim')
            await broker.ensure_queue(agent_id)
            return True

        broker.on_missing_queue(decline)
        broker.on_missing_queue(claim)
        assert (await broker.publish('lazy', _msg())).ok is True
        assert called == ['decline', 'claim']

    async def test_publish_to_missing_queue_fails_when_no_callback(
        self, broker: MessageBroker
    ) -> None:
        """失败返回结构化结果，不抛异常。"""
        result = await broker.publish('nobody', _msg())
        assert result.ok is False
        assert result.reason is not None

    async def test_publish_fails_when_a_callback_claims_without_building_the_queue(
        self, broker: MessageBroker
    ) -> None:
        async def lie(agent_id: str) -> bool:
            return True

        broker.on_missing_queue(lie)
        result = await broker.publish('lazy', _msg())
        assert result.ok is False
        assert result.reason is not None

    async def test_ensure_queue_keeps_messages_already_queued(self, broker: MessageBroker) -> None:
        """「队列存在」已经成立，重复 ensure 必须成功且什么都不动。"""
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg('kept'))
        await broker.ensure_queue('a1')
        assert [m.content for m in await broker.consume('a1')] == ['kept']

    async def test_remove_queue_discards_pending(self, broker: MessageBroker) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg())
        await broker.remove_queue('a1')
        result = await broker.publish('a1', _msg())
        assert result.ok is False

    async def test_remove_queue_of_unknown_agent_is_a_noop(self, broker: MessageBroker) -> None:
        """「队列不存在」已经成立，无从表达失败；抛异常会逼调用方先探测再删。"""
        await broker.remove_queue('nobody')
        assert await broker.consume('nobody') == []

    async def test_remove_queue_wakes_a_waiter_blocked_in_wait_for_messages(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        waiting = asyncio.create_task(broker.wait_for_messages('a1', timeout=None))
        await _park(waiting, 'wait_for_messages(timeout=None)')
        await broker.remove_queue('a1')
        assert await asyncio.wait_for(waiting, timeout=_WAKE_TIMEOUT) is False

    async def test_remove_queue_wakes_a_waiter_blocked_in_take_by_correlation(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        waiting = asyncio.create_task(broker.take_by_correlation('a1', 'c1', timeout=None))
        await _park(waiting, 'take_by_correlation(timeout=None)')
        await broker.remove_queue('a1')
        assert await asyncio.wait_for(waiting, timeout=_WAKE_TIMEOUT) is None
