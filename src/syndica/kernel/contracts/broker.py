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
from contextvars import ContextVar
from typing import Any, TypeVar

from syndica.kernel.contracts.base import ContractSuite
from syndica.kernel.message import Message
from syndica.kernel.protocols.broker import MessageBroker, MissingQueueCallback

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

# 「取消不吃掉消息」扫多少个取消点。取到个位数即可：要打的窗口紧跟在「把消息摘出邮箱」之后，而
# 那一步之前的挂起点是取回消息所需的网络往返，一两个就到。
_CANCEL_SWEEP = 8

# 邮箱不存在时必须「答而不等」。交给调用的超时取 _TIMEOUT_NEVER_REACHED，白等满窗口的实现会
# 远远越过 _MAX_PROMPT_REPLY；上限取秒级而非微秒级，一次网络往返与负载抖动都远在它之下。
_TIMEOUT_NEVER_REACHED = 30.0
_MAX_PROMPT_REPLY = 1.0

# 回调链上每个回调都必须读得到调用方在 publish 之前设下的这一位。名字带前缀是因为 ContextVar
# 是进程全局的，宿主自己的同名变量会与它撞车。
_PUBLISHER_MARK: ContextVar[str] = ContextVar('syndica_contract_publisher_mark', default='')

# 陪跑回调的名字，顺序既非升序（alpha / bravo / charlie）亦非降序，反转后同样两者都不是：
# 回调链是一个顺序与多重性都承重的集合，基数取到 3 才挡得住排序类实现。
_CHAIN_NAMES = ('bravo', 'alpha', 'charlie')


def _msg(content: str = 'hi', *, correlation_id: str | None = None) -> Message:
    return Message(sender='s', receiver='a1', content=content, correlation_id=correlation_id)


async def _cancel_must_take_effect(task: asyncio.Task[Any], call: str, budget: float) -> None:
    """取消一个还停在等待点上的调用，它必须当场以取消收场。

    「已经先返回了」也算合格：`cancel()` 对一个已完成的调用本来就是 no-op，而带网络往返的后端
    可以在取消到达之前就拿到答案。不合格的只有第三种——取消到了、却换来一个正常的返回值。
    """
    answered_first = task.done()
    task.cancel()
    ended, _ = await asyncio.wait({task}, timeout=_MAX_PROMPT_REPLY)
    # 先收干净再断言：断言红的时候那次调用还在跑，留着它就是一个悬空任务。
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert ended, (
        f'期望 {call} 被 cancel() 之后 {_MAX_PROMPT_REPLY}s 内结束。'
        f' 实际它还在跑，而交给它的超时是 {budget}s——取消被吃掉了。'
        ' 内层等待改用 `asyncio.timeout` 而不是 `asyncio.wait_for`：'
        '后者在内层 future 已经就绪时把到达的取消当成「结果已到」吞掉，'
        '而调用方正是靠取消让一次被中止的等待当场返回。'
    )
    assert answered_first or task.cancelled(), (
        f'期望 {call} 要么在取消到达之前就已经答完，要么以取消收场。'
        f' 实际取消到了、它却交回了 {task.result()!r}——这次 cancel() 静默失效了，'
        '调用方以为自己停掉了它，而它接着往下跑。'
        ' 内层等待改用 `asyncio.timeout` 而不是 `asyncio.wait_for`。'
    )


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


def _declining(name: str, asked: list[str]) -> MissingQueueCallback:
    """记名后放弃的回调：一条回调在链上的位次、以及它到底有没有被问到，只有它记得下来。"""

    async def callback(agent_id: str) -> bool:
        asked.append(name)
        return False

    return callback


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

    async def test_publish_ok_means_the_message_is_still_in_the_mailbox(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        message = _msg('receipt-visible')
        result = await broker.publish('a1', message)
        queued = await broker.consume('a1')
        assert not result.ok or [item.id for item in queued] == [message.id], (
            '期望 publish 返回 ok=True 时，这条消息已经进入此刻仍属于目标 Agent 的邮箱。'
            f' 实际回执是 ok={result.ok} message_id={result.message_id!r}，'
            f'随后 consume 看到 {[item.id for item in queued]}。'
            ' 把最终邮箱检查、写入与成功回执放进同一个不让 remove_queue 插入的临界区；'
            '远端实现使用事务或原子脚本。'
        )

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

    async def test_wait_timeout_is_not_extended_by_nonmatching_messages(
        self, broker: MessageBroker
    ) -> None:
        await broker.ensure_queue('a1')
        await broker.publish('a1', _msg('already queued'))

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

    async def test_cancelling_take_by_correlation_ends_it_instead_of_burning_its_timeout(
        self, broker: MessageBroker
    ) -> None:
        """取消要当场生效：调用方靠它让一次被中止的等待立刻返回。

        噪声消息是关键——它让实现内层那个「等新消息入队」的 future 就绪，而这次取还得继续等。
        `asyncio.wait_for` 恰在这个形状下把到达的取消当成「结果已到」吃掉，于是这次取此后不可
        取消，`abort` / `shutdown` / `despawn` 都要挂满交给它的整个超时。
        """
        await broker.ensure_queue('a1')
        taking = asyncio.create_task(
            broker.take_by_correlation('a1', 'c1', timeout=_TIMEOUT_NEVER_REACHED)
        )
        await _park(taking, 'take_by_correlation')
        await broker.publish('a1', _msg('noise'))
        await _cancel_must_take_effect(taking, 'take_by_correlation', _TIMEOUT_NEVER_REACHED)

    async def test_cancelling_wait_for_messages_ends_it_instead_of_burning_its_timeout(
        self, broker: MessageBroker
    ) -> None:
        """同一条义务落在另一个阻塞方法上。

        `asyncio.wait_for` 的那个陷阱与方法无关：内层 future 已经就绪时到达的取消一律被它吃掉，
        而这里吃掉的后果不是挂满超时而是**更隐蔽的那一种**——这次调用照常交回 True，于是
        `kill` / `shutdown` 取消不掉停在这里的执行体，它接着往下跑。
        """
        await broker.ensure_queue('a1')
        waiting = asyncio.create_task(
            broker.wait_for_messages('a1', timeout=_TIMEOUT_NEVER_REACHED)
        )
        await _park(waiting, 'wait_for_messages')
        await broker.publish('a1', _msg())
        await _cancel_must_take_effect(waiting, 'wait_for_messages', _TIMEOUT_NEVER_REACHED)

    async def test_cancelling_take_by_correlation_leaves_the_match_takeable(
        self, broker: MessageBroker
    ) -> None:
        """取消一次取，那条匹配的消息要么已经交回，要么还能再取一次——不存在第三种结局。

        逐个取消点扫一遍而不是只取消一次：能吃掉消息的窗口在「已摘出邮箱、还没返回」之间，而它落在
        第几个调度步上是实现的自由度。这里数的步数只决定这一遍**扫到哪里**，每一步上的断言本身与
        步数无关，因此结论不随解释器版本变化。

        摘掉与返回之间不留挂起点的实现走不进「被取消」那一支（取消永远落在一个已完成的任务上），
        因此本用例对它只执行另一支；这条义务真的还活着由一个刻意在那个窗口里挂起的实现来证明。
        """
        await broker.ensure_queue('a1')
        for steps in range(1, _CANCEL_SWEEP + 1):
            await broker.publish('a1', _msg('answer', correlation_id='c1'))
            taking = asyncio.create_task(
                broker.take_by_correlation('a1', 'c1', timeout=_WAKE_TIMEOUT)
            )
            for _ in range(steps):
                if taking.done():
                    break
                await asyncio.sleep(0)
            taking.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await taking
            if taking.cancelled():
                assert await broker.take_by_correlation('a1', 'c1', timeout=_WAKE_TIMEOUT), (
                    f'期望 take_by_correlation 被取消（第 {steps} 个调度步）之后，'
                    'correlation_id=c1 的那条消息仍能再取一次。'
                    ' 实际它消失了：这次取把消息摘出了邮箱，却在返回之前被取消，谁都没收到它。'
                    ' 让「摘掉」与「返回」之间不留挂起点，或改用读了未确认即可重取的语义；'
                    '调用方会在等待被中止或被顶掉时取消这次取。'
                )
            else:
                assert taking.result() is not None
            assert await broker.wait_for_messages('a1', 0) is False, (
                f'期望第 {steps} 轮结束时邮箱回到空：一条消息只该被交付一次。'
                ' 实际邮箱里还有东西，说明这一轮既交回了消息、又把它留在了邮箱里。'
                ' 让取出即移出邮箱（consume 恰好一次）。'
            )

    async def test_a_cancelled_take_leaves_the_match_on_the_ordinary_consume_path(
        self, broker: MessageBroker
    ) -> None:
        """「仍在邮箱里」指的是 `consume` 与 `wait_for_messages` 看得见它，不只是「再 take 一次」。

        内核依赖的正是这一条：被中止的那次等待返回之后，这条答复退回成一条普通消息，由门在下一轮
        开始前 `consume` 进上下文。只对 take 可见的实现（未确认条目不进普通消费路径）会让
        `wait_for_messages` 答 False、`consume` 交出空列表，那条答复于是静默消失，而 `publish`
        早已回过 `ok=True`。
        """
        await broker.ensure_queue('a1')
        for steps in range(1, _CANCEL_SWEEP + 1):
            await broker.publish('a1', _msg('answer', correlation_id='c1'))
            taking = asyncio.create_task(
                broker.take_by_correlation('a1', 'c1', timeout=_WAKE_TIMEOUT)
            )
            for _ in range(steps):
                if taking.done():
                    break
                await asyncio.sleep(0)
            taking.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await taking
            if not taking.cancelled():
                assert taking.result() is not None
                continue
            assert await broker.wait_for_messages('a1', 0) is True, (
                f'期望 take_by_correlation 被取消（第 {steps} 个调度步）之后，'
                'wait_for_messages 看得见那条没人收下的答复。'
                ' 实际它答 False：这条消息只对 take 可见（未确认条目没回到普通消费路径），'
                '而门靠 wait_for_messages 与 consume 把它注入下一轮。'
                ' 让 consume / wait_for_messages 先读自己的 pending'
                '（`XREADGROUP ... STREAMS key 0` 或 `XAUTOCLAIM`）。'
            )
            assert [message.content for message in await broker.consume('a1')] == ['answer'], (
                f'期望 take_by_correlation 被取消（第 {steps} 个调度步）之后，'
                'consume 交出那条没人收下的答复。'
                ' 实际它没交出来：那条答复静默消失了，而 publish 早已回过 ok=True。'
                ' 让 consume 先读自己的 pending，未确认的条目才真的回到了普通消费路径上。'
            )

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

    async def test_every_missing_queue_callback_runs_in_the_publishers_context(
        self, broker: MessageBroker
    ) -> None:
        seen: list[tuple[str, str]] = []

        def watching(name: str) -> MissingQueueCallback:
            async def callback(agent_id: str) -> bool:
                seen.append((name, _PUBLISHER_MARK.get()))
                if name != _CHAIN_NAMES[-1]:
                    return False
                await broker.ensure_queue(agent_id)
                return True

            return callback

        for name in _CHAIN_NAMES:
            broker.on_missing_queue(watching(name))
        token = _PUBLISHER_MARK.set('set-before-publish')
        try:
            assert (await broker.publish('lazy', _msg())).ok is True
        finally:
            _PUBLISHER_MARK.reset(token)

        expected = [(name, 'set-before-publish') for name in _CHAIN_NAMES]
        assert seen == expected, (
            '期望链上每个回调都跑在 publish 那次调用的上下文里，因而读得到调用方在 publish 之前'
            f' 设下的 ContextVar: {expected}。'
            f' 实际 (回调, 它读到的值) = {seen}。'
            ' 在 publish 自己的任务里 await 回调，或用 create_task 派生（新任务复制当前 Context）。'
            ' 交给一个早已存在的后台任务派发就读不到这一位，调用方「这次投递不要叫醒它」的意图'
            '会静默失效：一条消息叫醒了一个本该继续睡的 Agent，而没有任何错误被报出来。'
        )

    async def test_a_callback_registered_after_an_earlier_publish_still_joins_the_chain(
        self, broker: MessageBroker
    ) -> None:
        asked: list[str] = []
        first, *rest = _CHAIN_NAMES
        broker.on_missing_queue(_declining(first, asked))
        assert (await broker.publish('early', _msg())).ok is False
        assert asked == [first]

        for name in rest:
            broker.on_missing_queue(_declining(name, asked))
        asked.clear()
        assert (await broker.publish('late', _msg())).ok is False

        assert asked == list(_CHAIN_NAMES), (
            f'期望回调列表每次 publish 现读，因此第一次 publish 之后注册的回调照样进链: '
            f'{list(_CHAIN_NAMES)}。'
            f' 实际这一轮问到的是 {asked}。'
            ' 别在首次 publish 时把列表冻结成快照：那之后注册的回调永远不会被调用，'
            '而共享 broker 上这意味着此后构造的 Runtime 复活不了自己的 ZOMBIE——'
            '整套契约在这种实现上仍然全绿。'
        )

    async def test_a_detached_callback_is_no_longer_asked(self, broker: MessageBroker) -> None:
        asked: list[str] = []
        chain = {name: _declining(name, asked) for name in _CHAIN_NAMES}
        for callback in chain.values():
            broker.on_missing_queue(callback)
        # 摘中间那条：只摘首尾时，「摘掉点名的那条」与「摘掉第一条」「摘掉最后一条」
        # 是同一个答案。
        head, detached, tail = _CHAIN_NAMES
        broker.off_missing_queue(chain[detached])

        assert (await broker.publish('lazy', _msg())).ok is False
        assert asked == [head, tail]

    async def test_off_missing_queue_for_a_callback_never_registered_is_a_noop(
        self, broker: MessageBroker
    ) -> None:
        """「它不在链上」已经成立，无从表达失败；抛异常会逼调用方先探测再摘。"""
        asked: list[str] = []
        for name in _CHAIN_NAMES:
            broker.on_missing_queue(_declining(name, asked))
        broker.off_missing_queue(_declining('stranger', asked))

        assert (await broker.publish('lazy', _msg())).ok is False
        assert asked == list(_CHAIN_NAMES)

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
