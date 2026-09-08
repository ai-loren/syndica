from __future__ import annotations

import asyncio
import dataclasses

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
_AGENT = 'a1'


def _message(content: str, *, trigger: Trigger = Trigger.PENDING) -> Message:
    return Message(sender='user', receiver=_AGENT, content=content, trigger=trigger)


async def _publish(broker: InMemoryMessageBroker, *contents: str, trigger: Trigger) -> None:
    for content in contents:
        await broker.publish(_AGENT, _message(content, trigger=trigger))


class _CountingBroker:
    """记下 consume 被调用过几次的 broker 包装。

    「挂起时没有消费消息」只看邮箱剩几条是不够的：一个先取走再放回的实现同样能让邮箱看起来
    没动。计数是能区分两者的那个观测点。

    两处 ASYNC109 豁免与 pyproject 给 `protocols/broker.py`、`stores/memory_broker.py` 的那两条
    同源：等待上限是 `MessageBroker` 签名的一部分，包装必须逐字照抄它才代得了原件。
    """

    def __init__(self, inner: InMemoryMessageBroker) -> None:
        self.inner = inner
        self.consume_calls = 0

    async def ensure_queue(self, agent_id: str) -> None:
        await self.inner.ensure_queue(agent_id)

    async def publish(self, agent_id: str, message: Message):
        return await self.inner.publish(agent_id, message)

    async def consume(self, agent_id: str, *, max_n: int = 0) -> list[Message]:
        self.consume_calls += 1
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


async def test_deferring_snapshots_the_message():
    """交进来的消息此后被改动，不得改到已经暂存的那一份。"""
    control = ControlBlock()
    message = _message('original', trigger=Trigger.DEFERRED)
    control.defer(message)
    message.content = 'mutated'
    assert [taken.content for taken in control.take_deferred()] == ['original']


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
    decision = await asyncio.wait_for(NullTurnGate().before_turn(1), timeout=_TIMEOUT)
    assert decision == TurnGateDecision()


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
    """刚建出来的门还没干过活，所以它的第一轮就是一个周期的第一轮，暂存的消息此刻该交出去。"""
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
    assert await control.wait_until_parked(timeout=0.05) is False


def test_decision_defaults_are_immutable():
    decision = TurnGateDecision()
    assert decision.abort is False
    assert decision.messages == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.abort = True  # pyright: ignore[reportAttributeAccessIssue]
