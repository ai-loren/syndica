from __future__ import annotations

import asyncio
import dataclasses
from typing import cast

import pytest

from syndica.kernel.control import (
    BrokerTurnGate,
    ControlBlock,
    NullTurnGate,
    TurnGate,
    TurnGateDecision,
)
from syndica.kernel.message import Message, Trigger
from syndica.kernel.stores import InMemoryMessageBroker

# 挂死类回归应当变红而不是把 CI 挂住；1s 对纯内存操作是极宽裕的上界。
_TIMEOUT = 1.0
# 「现在没人停着」这个答案要等满上限才拿得到，因此它必须短；它不参与任何耗时断言。
_NO_PARK_TIMEOUT = 0.05
_AGENT = 'a1'


def _message(content: str, *, trigger: Trigger = Trigger.PENDING) -> Message:
    return Message(sender='user', receiver=_AGENT, content=content, trigger=trigger)


@dataclasses.dataclass
class _MessageWithNestedField(Message):
    """带嵌套容器的消息。

    `Message` 现在每个字段都是标量（`test_message_carries_no_mutable_container_field` 守着这一点），
    浅拷贝在它上面与深拷贝逐字节相同；而 `defer` 承诺的是消息的快照，不是「顶层字段的快照」。
    """

    payload: dict[str, list[str]] = dataclasses.field(default_factory=dict[str, list[str]])


async def _publish(broker: InMemoryMessageBroker, *contents: str, trigger: Trigger) -> None:
    for content in contents:
        await broker.publish(_AGENT, _message(content, trigger=trigger))


async def _park_after_signalling(control: ControlBlock, arrived: asyncio.Event) -> bool:
    """置位与入园之间没有挂起点，所以看到信号就等于这条执行体已经在园里。"""
    arrived.set()
    return await control.hold_until_runnable()


class _CountingBroker:
    """记下 consume 被调用过几次的 broker 包装。

    「挂起时没有消费消息」只看邮箱剩几条是不够的：一个先取走再放回的实现同样能让邮箱看起来
    没动。计数是能区分两者的那个观测点。

    `hold_until` 让 consume 停到第 n 次调用到齐为止。内存 broker 的 consume 一次都不挂起，两次
    并发的 `before_turn` 因此永远不会真的交错，而远端 broker 的 consume 必然是一次真实等待。
    """

    def __init__(self, inner: InMemoryMessageBroker, *, hold_until: int = 0) -> None:
        self.inner = inner
        self.consume_calls = 0
        self._hold_until = hold_until
        self._released = asyncio.Event()

    async def ensure_queue(self, agent_id: str) -> None:
        await self.inner.ensure_queue(agent_id)

    async def publish(self, agent_id: str, message: Message):
        return await self.inner.publish(agent_id, message)

    async def consume(self, agent_id: str, *, max_n: int = 0) -> list[Message]:
        self.consume_calls += 1
        if self._hold_until:
            if self.consume_calls >= self._hold_until:
                self._released.set()
            await self._released.wait()
        return await self.inner.consume(agent_id, max_n=max_n)

    async def wait_for_messages(self, agent_id: str, timeout: float | None) -> bool:  # noqa: ASYNC109
        return await self.inner.wait_for_messages(agent_id, timeout)

    async def take_by_correlation(
        self,
        agent_id: str,
        correlation_id: str,
        timeout: float | None,  # noqa: ASYNC109
    ):
        return await self.inner.take_by_correlation(agent_id, correlation_id, timeout)

    async def remove_queue(self, agent_id: str) -> None:
        await self.inner.remove_queue(agent_id)

    def on_missing_queue(self, cb) -> None:
        self.inner.on_missing_queue(cb)

    def off_missing_queue(self, cb) -> None:
        self.inner.off_missing_queue(cb)


async def test_a_fresh_control_block_lets_the_turn_loop_through():
    control = ControlBlock()
    assert control.paused is False
    assert await asyncio.wait_for(control.hold_until_runnable(), timeout=_TIMEOUT) is False


async def test_pause_parks_the_turn_loop_until_resume():
    control = ControlBlock()
    control.pause()
    held = asyncio.create_task(control.hold_until_runnable())
    # 确定性握手：wait_until_parked 返回 True 就证明它真的停在门上了，
    # 不是「还没被调度到」，因此下面的断言不会由空转恒成立。
    assert await control.wait_until_parked(timeout=_TIMEOUT) is True
    assert control.paused is True
    control.resume()
    assert await asyncio.wait_for(held, timeout=_TIMEOUT) is False
    assert control.parked == 0


async def test_the_pause_gate_can_be_entered_and_left_repeatedly():
    """挂起是电平，唤醒是边沿：唤醒信号若当电平用，第二次挂起就再也停不住。

    那时循环每次醒来都发现自己仍被挂起、又立刻重等一个恒为高的信号，退化成不让出控制权的空转，
    因此这条用例的回归表现是撞上全局超时，而不是某一句断言为假。
    """
    control = ControlBlock()
    for _ in range(3):
        control.pause()
        held = asyncio.create_task(control.hold_until_runnable())
        assert await control.wait_until_parked(timeout=_TIMEOUT) is True
        control.resume()
        assert await asyncio.wait_for(held, timeout=_TIMEOUT) is False
    assert control.parked == 0


async def test_cancelling_a_parked_holder_hands_the_park_slot_back():
    """园计数一旦泄漏，`parked` 与 `wait_until_parked` 这两个观测点从此永久说谎。"""
    control = ControlBlock()
    control.pause()
    held = asyncio.create_task(control.hold_until_runnable())
    assert await control.wait_until_parked(timeout=_TIMEOUT) is True

    held.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(held, timeout=_TIMEOUT)

    assert control.parked == 0
    assert await control.wait_until_parked(timeout=_NO_PARK_TIMEOUT) is False


async def test_parked_counts_the_execution_bodies_not_just_whether_any_is_parked():
    control = ControlBlock()
    control.pause()
    first_arrived = asyncio.Event()
    second_arrived = asyncio.Event()
    first = asyncio.create_task(_park_after_signalling(control, first_arrived))
    second = asyncio.create_task(_park_after_signalling(control, second_arrived))

    await asyncio.wait_for(second_arrived.wait(), timeout=_TIMEOUT)

    assert first_arrived.is_set() is True
    assert control.parked == 2
    control.resume()
    assert await asyncio.wait_for(asyncio.gather(first, second), timeout=_TIMEOUT) == [False, False]
    assert control.parked == 0


async def test_cancelling_one_of_two_parked_holders_leaves_the_other_in_the_park():
    control = ControlBlock()
    control.pause()
    first_arrived = asyncio.Event()
    second_arrived = asyncio.Event()
    first = asyncio.create_task(_park_after_signalling(control, first_arrived))
    second = asyncio.create_task(_park_after_signalling(control, second_arrived))
    await asyncio.wait_for(second_arrived.wait(), timeout=_TIMEOUT)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(first, timeout=_TIMEOUT)

    assert control.parked == 1
    assert await control.wait_until_parked(timeout=_TIMEOUT) is True
    control.resume()
    assert await asyncio.wait_for(second, timeout=_TIMEOUT) is False
    assert control.parked == 0


async def test_pause_is_a_level_not_a_counter():
    control = ControlBlock()
    control.pause()
    control.pause()
    control.resume()
    assert control.paused is False
    assert await asyncio.wait_for(control.hold_until_runnable(), timeout=_TIMEOUT) is False


async def test_abort_pierces_the_pause_gate():
    """先挂起再中止是真实顺序；中止若要等解除挂起才被看见，这个 Agent 就永远停在那里。"""
    control = ControlBlock()
    control.pause()
    held = asyncio.create_task(control.hold_until_runnable())
    assert await control.wait_until_parked(timeout=_TIMEOUT) is True
    control.request_abort()
    assert await asyncio.wait_for(held, timeout=_TIMEOUT) is True
    assert control.paused is True


async def test_abort_is_consumed_once_so_the_next_cycle_still_runs():
    control = ControlBlock()
    control.request_abort()
    assert await asyncio.wait_for(control.hold_until_runnable(), timeout=_TIMEOUT) is True
    assert await asyncio.wait_for(control.hold_until_runnable(), timeout=_TIMEOUT) is False


async def test_abort_requested_reports_the_bit_without_consuming_it():
    control = ControlBlock()
    assert control.abort_requested is False
    control.request_abort()
    assert control.abort_requested is True
    assert control.abort_requested is True
    assert control.take_abort() is True
    assert control.abort_requested is False
    assert control.take_abort() is False


async def test_in_run_tracks_entering_and_leaving_a_run():
    control = ControlBlock()
    assert control.in_run is False
    control.enter_run()
    assert control.in_run is True
    control.leave_run()
    assert control.in_run is False


async def test_deferred_messages_come_back_in_arrival_order():
    """基数 3 且顺序既非升序亦非降序：n<=2 时排序类变异必有一个是恒等变换。"""
    control = ControlBlock()
    for content in ('beta', 'alpha', 'gamma'):
        control.defer(_message(content, trigger=Trigger.DEFERRED))
    assert control.deferred_count == 3
    assert [message.content for message in control.take_deferred()] == ['beta', 'alpha', 'gamma']
    assert control.deferred_count == 0


async def test_deferring_snapshots_the_message_all_the_way_down():
    """交进来的消息此后被改动——连它的嵌套容器一起——不得改到已经暂存的那一份。"""
    control = ControlBlock()
    message = _MessageWithNestedField(
        sender='user',
        receiver=_AGENT,
        content='original',
        trigger=Trigger.DEFERRED,
        payload={'k': ['v']},
    )
    control.defer(message)
    message.content = 'mutated'
    message.payload['k'].append('injected')

    stashed = [cast(_MessageWithNestedField, taken) for taken in control.take_deferred()]
    assert [taken.content for taken in stashed] == ['original']
    assert [taken.payload for taken in stashed] == [{'k': ['v']}]


async def test_taking_deferred_hands_over_a_list_the_block_no_longer_owns():
    control = ControlBlock()
    control.defer(_message('one', trigger=Trigger.DEFERRED))
    taken = control.take_deferred()
    taken.append(_message('injected', trigger=Trigger.DEFERRED))
    assert control.deferred_count == 0


async def test_both_gates_fit_where_a_turn_gate_is_expected():
    """门是 turn 循环与控制面之间的唯一接缝，两个实现都必须顶得上它的位置。

    `list[TurnGate]` 这个标注由 pyright 兑现签名一致；isinstance 则要求 TurnGate 保持
    runtime_checkable，去掉那个装饰器这里会直接抛 TypeError。
    """
    gates: list[TurnGate] = [
        NullTurnGate(),
        BrokerTurnGate(_AGENT, InMemoryMessageBroker(), ControlBlock()),
    ]
    assert [isinstance(gate, TurnGate) for gate in gates] == [True, True]


async def test_null_gate_never_aborts_and_never_injects():
    gate = NullTurnGate()
    decision = await asyncio.wait_for(gate.before_turn(1), timeout=_TIMEOUT)
    assert decision == TurnGateDecision()
    assert await asyncio.wait_for(gate.check_abort(), timeout=_TIMEOUT) is False


async def test_check_abort_consumes_the_abort_without_consuming_the_mailbox():
    """第 9 步必须消费中止位：peek 会让下一轮 before_turn 立刻再中止一次。"""
    broker = InMemoryMessageBroker()
    await broker.ensure_queue(_AGENT)
    control = ControlBlock()
    gate = BrokerTurnGate(_AGENT, broker, control)
    await _publish(broker, 'pending', trigger=Trigger.PENDING)
    control.request_abort()

    assert await asyncio.wait_for(gate.check_abort(), timeout=_TIMEOUT) is True
    assert await asyncio.wait_for(gate.check_abort(), timeout=_TIMEOUT) is False
    remaining = await broker.consume(_AGENT)
    assert [message.content for message in remaining] == ['pending']


async def test_gate_injects_pending_and_stashes_deferred_after_the_cycle_started():
    broker = InMemoryMessageBroker()
    await broker.ensure_queue(_AGENT)
    control = ControlBlock()
    gate = BrokerTurnGate(_AGENT, broker, control)
    gate.begin_cycle()
    decision = await asyncio.wait_for(gate.before_turn(1), timeout=_TIMEOUT)
    assert decision.messages == ()

    await _publish(broker, 'p1', trigger=Trigger.PENDING)
    await _publish(broker, 'd1', trigger=Trigger.DEFERRED)
    await _publish(broker, 'p2', trigger=Trigger.PENDING)
    decision = await asyncio.wait_for(gate.before_turn(2), timeout=_TIMEOUT)
    assert [message.content for message in decision.messages] == ['p1', 'p2']
    assert control.deferred_count == 1


async def test_the_gate_stashes_mid_cycle_deferred_messages_in_arrival_order():
    """门是暂存队列在生产里唯一的填充者；基数 3 且顺序既非升序亦非降序，n<=2 挡不住排序变异。"""
    broker = InMemoryMessageBroker()
    await broker.ensure_queue(_AGENT)
    control = ControlBlock()
    gate = BrokerTurnGate(_AGENT, broker, control)
    gate.begin_cycle()
    await asyncio.wait_for(gate.before_turn(1), timeout=_TIMEOUT)

    await _publish(broker, 'beta', 'alpha', 'gamma', trigger=Trigger.DEFERRED)
    decision = await asyncio.wait_for(gate.before_turn(2), timeout=_TIMEOUT)
    assert decision.messages == ()
    assert control.deferred_count == 3

    gate.begin_cycle()
    decision = await asyncio.wait_for(gate.before_turn(3), timeout=_TIMEOUT)
    assert [message.content for message in decision.messages] == ['beta', 'alpha', 'gamma']


async def test_two_turns_overlapping_on_one_gate_still_start_exactly_one_cycle():
    """读「周期开始」标志与写回它之间不能隔着挂起点：读到旧值的那一轮会把周期中到达的 DEFERRED
    当成周期开头的消息直接注入，DEFERRED 「先把手上的事做完」的语义就此失效。
    """
    inner = InMemoryMessageBroker()
    await inner.ensure_queue(_AGENT)
    broker = _CountingBroker(inner, hold_until=2)
    control = ControlBlock()
    gate = BrokerTurnGate(_AGENT, broker, control)
    await _publish(inner, 'mid-cycle-deferred', trigger=Trigger.DEFERRED)

    first, second = await asyncio.wait_for(
        asyncio.gather(gate.before_turn(1), gate.before_turn(2)), timeout=_TIMEOUT
    )

    assert first.messages == ()
    assert second.messages == ()
    assert control.deferred_count == 1


async def test_the_first_turn_of_a_cycle_injects_the_stash_before_fresh_arrivals():
    """暂存的那些先到，所以它们排在前面——整条注入序列仍是先来先到。"""
    broker = InMemoryMessageBroker()
    await broker.ensure_queue(_AGENT)
    control = ControlBlock()
    gate = BrokerTurnGate(_AGENT, broker, control)
    gate.begin_cycle()
    await gate.before_turn(1)

    await _publish(broker, 'stashed', trigger=Trigger.DEFERRED)
    await gate.before_turn(2)
    await _publish(broker, 'fresh-pending', trigger=Trigger.PENDING)
    await _publish(broker, 'fresh-deferred', trigger=Trigger.DEFERRED)

    gate.begin_cycle()
    decision = await asyncio.wait_for(gate.before_turn(3), timeout=_TIMEOUT)
    assert [message.content for message in decision.messages] == [
        'stashed',
        'fresh-pending',
        'fresh-deferred',
    ]
    assert control.deferred_count == 0


async def test_a_fresh_gate_treats_its_first_turn_as_a_cycle_start():
    broker = InMemoryMessageBroker()
    await broker.ensure_queue(_AGENT)
    control = ControlBlock()
    control.defer(_message('stashed', trigger=Trigger.DEFERRED))
    gate = BrokerTurnGate(_AGENT, broker, control)
    decision = await asyncio.wait_for(gate.before_turn(1), timeout=_TIMEOUT)
    assert [message.content for message in decision.messages] == ['stashed']


async def test_a_paused_gate_does_not_consume_the_message_it_was_about_to_take():
    inner = InMemoryMessageBroker()
    await inner.ensure_queue(_AGENT)
    broker = _CountingBroker(inner)
    control = ControlBlock()
    gate = BrokerTurnGate(_AGENT, broker, control)
    gate.begin_cycle()
    await _publish(inner, 'do-not-eat-me', trigger=Trigger.PENDING)

    control.pause()
    pending = asyncio.create_task(gate.before_turn(1))
    assert await control.wait_until_parked(timeout=_TIMEOUT) is True
    assert broker.consume_calls == 0

    control.resume()
    decision = await asyncio.wait_for(pending, timeout=_TIMEOUT)
    assert [message.content for message in decision.messages] == ['do-not-eat-me']


async def test_an_aborting_gate_reports_abort_and_takes_no_message():
    broker = InMemoryMessageBroker()
    await broker.ensure_queue(_AGENT)
    control = ControlBlock()
    gate = BrokerTurnGate(_AGENT, broker, control)
    await _publish(broker, 'still-queued', trigger=Trigger.PENDING)
    control.request_abort()

    decision = await asyncio.wait_for(gate.before_turn(1), timeout=_TIMEOUT)
    assert decision.abort is True
    assert decision.messages == ()
    assert [message.content for message in await broker.consume(_AGENT)] == ['still-queued']


async def test_aborting_does_not_touch_the_deferred_stash():
    """abort 停的是手上这一件事；暂存的消息排的是下一件事的队，不在被停的范围里。"""
    broker = InMemoryMessageBroker()
    await broker.ensure_queue(_AGENT)
    control = ControlBlock()
    gate = BrokerTurnGate(_AGENT, broker, control)
    for content in ('beta', 'alpha', 'gamma'):
        control.defer(_message(content, trigger=Trigger.DEFERRED))
    control.request_abort()

    decision = await asyncio.wait_for(gate.before_turn(1), timeout=_TIMEOUT)

    assert decision.abort is True
    assert control.deferred_count == 3
    assert [message.content for message in control.take_deferred()] == ['beta', 'alpha', 'gamma']


async def test_an_aborted_turn_does_not_burn_the_cycle_start_flag():
    """被中止的一轮什么都没干，下一轮仍然是这个周期的第一轮，暂存的消息此刻才该交出去。"""
    broker = InMemoryMessageBroker()
    await broker.ensure_queue(_AGENT)
    control = ControlBlock()
    gate = BrokerTurnGate(_AGENT, broker, control)
    control.defer(_message('stashed', trigger=Trigger.DEFERRED))
    control.request_abort()

    aborted = await asyncio.wait_for(gate.before_turn(1), timeout=_TIMEOUT)
    assert aborted.abort is True

    decision = await asyncio.wait_for(gate.before_turn(2), timeout=_TIMEOUT)
    assert [message.content for message in decision.messages] == ['stashed']


async def test_a_gate_whose_mailbox_vanished_reports_no_messages():
    """despawn 可以在一轮进行中把邮箱摘掉；门对此答「没有消息」而不是抛异常。"""
    broker = InMemoryMessageBroker()
    await broker.ensure_queue(_AGENT)
    control = ControlBlock()
    gate = BrokerTurnGate(_AGENT, broker, control)
    gate.begin_cycle()
    await _publish(broker, 'lost-with-the-mailbox', trigger=Trigger.PENDING)
    await broker.remove_queue(_AGENT)

    decision = await asyncio.wait_for(gate.before_turn(1), timeout=_TIMEOUT)
    assert decision == TurnGateDecision()


async def test_wait_until_parked_reports_false_when_nobody_parks():
    """守卫自身必须能答「没有」，否则上面那些握手断言可能只是恒真。"""
    control = ControlBlock()
    assert await control.wait_until_parked(timeout=_NO_PARK_TIMEOUT) is False


async def test_wait_until_parked_answers_about_now_not_about_the_wake_up():
    """公园在一次等待在途时被进入又离开：答案描述的必须是复检那一刻，不是「曾经有人停过」。

    `_parked_event` 被置位与本方法续跑之间隔着若干就绪队列槽位，先于它入队的任务能在这期间把
    公园腾空——这里的 resumer 就是那样一个任务，它入队早于置位，因此无需任何调度小动作。
    """
    control = ControlBlock()
    control.pause()
    holder_arrived = asyncio.Event()
    parked_mid_flight: list[int] = []

    async def resume_once_parked() -> None:
        await holder_arrived.wait()
        parked_mid_flight.append(control.parked)
        control.resume()

    resumer = asyncio.create_task(resume_once_parked())
    holder = asyncio.create_task(_park_after_signalling(control, holder_arrived))

    answer = await control.wait_until_parked(timeout=_TIMEOUT)

    assert parked_mid_flight == [1]
    assert answer is (control.parked > 0)
    assert await asyncio.wait_for(holder, timeout=_TIMEOUT) is False
    await asyncio.wait_for(resumer, timeout=_TIMEOUT)
    assert control.parked == 0


def test_decision_defaults_are_immutable():
    decision = TurnGateDecision()
    assert decision.abort is False
    assert decision.messages == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.abort = True  # pyright: ignore[reportAttributeAccessIssue]
