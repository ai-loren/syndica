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
from collections.abc import Awaitable
from typing import Any, TypeVar

from syndica.kernel.contracts.base import ContractSuite
from syndica.kernel.message import Message
from syndica.kernel.protocols.broker import MessageBroker

__all__ = ['MessageBrokerContract']

_T = TypeVar('_T')

# 阻塞类用例的超时与「确实阻塞过」的下限。下限只取超时的四成：asyncio 的超时只会因负载变长、
# 不会变短，所以放宽下限不会让用例失去意义，而它要挡的失败是立刻返回（约 0 秒）。
_BLOCK_TIMEOUT = 0.05
_MIN_BLOCKED = 0.02

# 等待者停稳所需的时间，以及「本该返回的调用没返回」的判定上限。_WAKE_TIMEOUT 必须远大于
# _BLOCK_TIMEOUT 与 _MAX_PROMPT_REPLY，才不会把慢后端误判成挂死；它的作用是让回归表现为失败
# 而不是挂住整个 CI。
_PARK_DELAY = 0.05
_WAKE_TIMEOUT = 5.0

# 非匹配噪声的间隔，必须显著小于 _BLOCK_TIMEOUT，等待者才会在一个超时窗口里被反复唤醒。
_NOISE_INTERVAL = 0.005

# 邮箱不存在时必须「答而不等」。交给调用的超时取 _TIMEOUT_NEVER_REACHED，白等满窗口的实现会
# 远远越过 _MAX_PROMPT_REPLY；上限取秒级而非微秒级，一次网络往返与负载抖动都远在它之下。
_TIMEOUT_NEVER_REACHED = 30.0
_MAX_PROMPT_REPLY = 1.0


def _msg(content: str = 'hi', *, correlation_id: str | None = None) -> Message:
    return Message(sender='s', receiver='a1', content=content, correlation_id=correlation_id)


async def _answered_without_waiting(call: Awaitable[_T], label: str) -> _T:
    """断言 call 没有停下来等就给出答案。

    外层必须有界：timeout=None 上的回归是永久挂死，无界地 await 它会把 CI 挂在这里，而回归应当
    表现为失败。
    """
    started = time.monotonic()
    try:
        answer = await asyncio.wait_for(call, timeout=_WAKE_TIMEOUT)
    except TimeoutError:
        raise AssertionError(
            f'期望 {label} 在邮箱不存在时不等待就作答。'
            f' 实际到 {_WAKE_TIMEOUT}s 仍未返回。'
            ' 让「邮箱不存在」这一支在进入等待之前就返回，别挂到一个占位邮箱上：'
            ' timeout=None 是公开签名的一部分，停在那里就是永久挂死。'
        ) from None
    elapsed = time.monotonic() - started
    assert elapsed < _MAX_PROMPT_REPLY, (
        f'期望 {label} 在邮箱不存在时不等待就作答，耗时远低于 {_MAX_PROMPT_REPLY}s。'
        f' 实际等了 {elapsed:.3f}s。'
        ' 让「邮箱不存在」这一支在进入等待之前就返回：这是一个问句，答不出就答，'
        ' 不要占满调用方交来的超时。'
    )
    return answer


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

    async def test_wait_for_messages_on_unknown_queue_answers_false_without_waiting(
        self, broker: MessageBroker
    ) -> None:
        answer = await _answered_without_waiting(
            broker.wait_for_messages('nobody', timeout=_TIMEOUT_NEVER_REACHED),
            f'wait_for_messages(timeout={_TIMEOUT_NEVER_REACHED})',
        )
        assert answer is False

    async def test_wait_for_messages_on_unknown_queue_answers_false_when_timeout_is_none(
        self, broker: MessageBroker
    ) -> None:
        answer = await _answered_without_waiting(
            broker.wait_for_messages('nobody', timeout=None),
            'wait_for_messages(timeout=None)',
        )
        assert answer is False

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

    async def test_take_by_correlation_on_unknown_queue_answers_none_without_waiting(
        self, broker: MessageBroker
    ) -> None:
        answer = await _answered_without_waiting(
            broker.take_by_correlation('nobody', 'c1', timeout=_TIMEOUT_NEVER_REACHED),
            f'take_by_correlation(timeout={_TIMEOUT_NEVER_REACHED})',
        )
        assert answer is None

    async def test_take_by_correlation_on_unknown_queue_answers_none_when_timeout_is_none(
        self, broker: MessageBroker
    ) -> None:
        answer = await _answered_without_waiting(
            broker.take_by_correlation('nobody', 'c1', timeout=None),
            'take_by_correlation(timeout=None)',
        )
        assert answer is None

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
