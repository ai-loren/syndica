"""回放的 broker：没有记录时是一个合格的 MessageBroker；有记录时收件人在每个决定点上看到的与原始 run
那一刻看到的相同——门取走记录里那一轮注入的那几条，周期刚结束时答记录里的答案，停下来之后按邮箱
答；邮箱摘掉时记录里属于以后的消息留着。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection
from typing import TypeVar

import pytest

from syndica.kernel.contracts.broker import MessageBrokerContract
from syndica.kernel.message import Message
from syndica.kernel.protocols.broker import MessageBroker
from syndica.runner.replay_broker import GIVE_UP, Plan, ScheduledBroker

_T = TypeVar('_T')


def _no_hold(
    agent_id: str, turn: int, missing: tuple[str, ...], waiter: asyncio.Future[object]
) -> Callable[[], None]:
    raise AssertionError(f'没有记录的邮箱不该登记等待：{agent_id} 第 {turn} 轮 {missing}')


class TestUnscheduledReplayBroker(MessageBrokerContract):
    @pytest.fixture
    def broker(self) -> MessageBroker:
        return ScheduledBroker(lambda _: None, lambda _: 1, _no_hold)


class _Plan:
    """收件人 `a1` 的记录与它此刻要开的那一轮；`holds` 是 broker 登记过的等待；发送方是 `host` 的
    消息算宿主发的；`asleep` 是它进没进 zombie。"""

    def __init__(
        self,
        messages: dict[int, tuple[str, ...]],
        *,
        turn: int = 1,
        continued: Collection[int] = (),
    ) -> None:
        self.messages = messages
        self.continued = frozenset(continued)
        self.turn = turn
        self.asleep = False
        self.holds: list[tuple[str, int, tuple[str, ...]]] = []
        self.waiters: list[asyncio.Future[object]] = []
        self.released = 0

    def broker(self) -> ScheduledBroker:
        return ScheduledBroker(
            lambda agent_id: Plan(self.messages, self.continued) if agent_id == 'a1' else None,
            lambda _: self.turn,
            self.hold,
            from_host=lambda message: message.sender == 'host',
            asleep=self.is_asleep,
        )

    def hold(
        self, agent_id: str, turn: int, missing: tuple[str, ...], waiter: asyncio.Future[object]
    ) -> Callable[[], None]:
        self.holds.append((agent_id, turn, missing))
        self.waiters.append(waiter)

        def release() -> None:
            self.released += 1

        return release

    async def is_asleep(self, agent_id: str) -> bool:
        return self.asleep


def _message(message_id: str, sender: str = 's') -> Message:
    return Message(sender=sender, receiver='a1', content=f'body of {message_id}', id=message_id)


async def _ready(broker: ScheduledBroker, *ids: str) -> None:
    await broker.ensure_queue('a1')
    for message_id in ids:
        assert (await broker.publish('a1', _message(message_id))).ok


async def _parked(task: asyncio.Task[object]) -> None:
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done(), task


async def _ids(broker: ScheduledBroker) -> list[str]:
    async with asyncio.timeout(5):
        return [message.id for message in await broker.consume('a1')]


async def _done(task: asyncio.Task[_T]) -> _T:
    async with asyncio.timeout(5):
        return await task


async def test_the_gate_takes_exactly_the_messages_recorded_for_that_turn_in_recorded_order():
    plan = _Plan({1: ('m2', 'm3', 'm1'), 3: ('m4',)})
    broker = plan.broker()
    await _ready(broker, 'm3', 'm1', 'm4', 'm2')

    first = await _ids(broker)
    plan.turn = 2
    second = await _ids(broker)
    plan.turn = 3
    third = await _ids(broker)

    assert (first, second, third) == (['m2', 'm3', 'm1'], [], ['m4'])
    assert plan.holds == []


async def test_a_message_recorded_for_this_turn_that_was_not_sent_yet_holds_the_gate_until_it_is():
    plan = _Plan({1: ('m1', 'm2')})
    broker = plan.broker()
    await _ready(broker, 'm1')

    gate = asyncio.create_task(broker.consume('a1'))
    await _parked(gate)
    assert plan.holds == [('a1', 1, ('m2',))]
    await broker.publish('a1', _message('m2'))

    assert [message.id for message in await _done(gate)] == ['m1', 'm2']
    assert plan.released == 1


async def test_giving_up_hands_the_gate_what_is_there():
    plan = _Plan({1: ('m1', 'm2')})
    broker = plan.broker()
    await _ready(broker, 'm1')

    gate = asyncio.create_task(broker.consume('a1'))
    await _parked(gate)
    plan.waiters[0].set_result(GIVE_UP)

    assert [message.id for message in await _done(gate)] == ['m1']
    assert plan.released == 1


async def test_a_host_message_is_handed_over_on_arrival_ahead_of_the_recorded_ones():
    plan = _Plan({1: ('m1',)})
    broker = plan.broker()
    await _ready(broker, 'm1')
    await broker.publish('a1', _message('h1', sender='host'))

    assert await _ids(broker) == ['h1', 'm1']


async def test_right_after_a_cycle_the_record_says_it_stopped_even_with_its_next_message_there():
    plan = _Plan({2: ('m2',)})
    broker = plan.broker()
    await _ready(broker)
    plan.turn = 2
    await broker.publish('a1', _message('m2'))

    assert await broker.wait_for_messages('a1', 0) is False
    assert plan.holds == []


async def test_right_after_a_cycle_the_record_says_it_went_on_so_the_poll_waits_for_the_message():
    plan = _Plan({2: ('m2', 'm3')}, continued={2})
    broker = plan.broker()
    await _ready(broker)
    plan.turn = 2

    poll = asyncio.create_task(broker.wait_for_messages('a1', 0))
    await _parked(poll)
    assert plan.holds == [('a1', 2, ('m2', 'm3'))]
    await broker.publish('a1', _message('m3'))

    assert await _done(poll) is True
    assert plan.released == 1


async def test_right_after_a_cycle_a_host_message_there_means_it_goes_on():
    plan = _Plan({2: ('m2',)})
    broker = plan.broker()
    await _ready(broker)
    plan.turn = 2
    await broker.publish('a1', _message('h1', sender='host'))

    assert await broker.wait_for_messages('a1', 0) is True


async def test_once_parked_the_poll_before_its_next_cycle_answers_by_what_is_in_the_mailbox():
    plan = _Plan({2: ('m2',)})
    broker = plan.broker()
    await _ready(broker)
    plan.turn = 2

    parked = asyncio.create_task(broker.wait_for_messages('a1', None))
    await _parked(parked)
    assert plan.holds == [('a1', 2, ('m2',))]
    await broker.publish('a1', _message('m2'))

    assert await _done(parked) is True
    assert await broker.wait_for_messages('a1', 0) is True


async def test_an_agent_parked_on_its_mailbox_wakes_for_its_next_recorded_message_only():
    plan = _Plan({2: ('m2',)}, turn=2)
    broker = plan.broker()
    await _ready(broker)

    parked = asyncio.create_task(broker.wait_for_messages('a1', None))
    await broker.publish('a1', _message('m9'))
    await _parked(parked)
    await broker.publish('a1', _message('m2'))

    assert await _done(parked) is True


async def test_a_parked_agent_with_nothing_left_to_inject_stays_parked_until_its_mailbox_goes():
    plan = _Plan({1: ('m1',)}, turn=2)
    broker = plan.broker()
    await _ready(broker)

    parked = asyncio.create_task(broker.wait_for_messages('a1', None))
    await broker.publish('a1', _message('m9'))
    await _parked(parked)
    assert plan.holds == []
    await broker.remove_queue('a1')

    assert await _done(parked) is False


async def test_a_parked_agent_waiting_for_its_next_recorded_message_wakes_for_a_host_message():
    plan = _Plan({2: ('m2',)}, turn=2)
    broker = plan.broker()
    await _ready(broker)

    parked = asyncio.create_task(broker.wait_for_messages('a1', None))
    await _parked(parked)
    await broker.publish('a1', _message('h1', sender='host'))

    assert await _done(parked) is True
    assert plan.released == 1


async def test_a_parked_agent_with_nothing_left_to_inject_wakes_for_a_host_message():
    plan = _Plan({1: ('m1',)}, turn=2)
    broker = plan.broker()
    await _ready(broker)

    parked = asyncio.create_task(broker.wait_for_messages('a1', None))
    await _parked(parked)
    await broker.publish('a1', _message('h1', sender='host'))

    assert await _done(parked) is True


async def test_giving_up_on_a_parked_agent_lets_it_answer_by_the_mailbox():
    plan = _Plan({2: ('m2',)}, turn=2)
    broker = plan.broker()
    await _ready(broker)

    parked = asyncio.create_task(broker.wait_for_messages('a1', None))
    await _parked(parked)
    plan.waiters[0].set_result(GIVE_UP)

    assert await _done(parked) is False


async def test_removing_the_mailbox_lets_a_waiting_gate_go_empty_handed():
    plan = _Plan({1: ('m1', 'm2')})
    broker = plan.broker()
    await _ready(broker, 'm1')

    gate = asyncio.create_task(broker.consume('a1'))
    await _parked(gate)
    await broker.remove_queue('a1')

    assert await _done(gate) == []
    assert plan.released == 1


async def test_a_waiting_gate_cancels_on_the_spot_and_the_message_is_still_there_afterwards():
    plan = _Plan({1: ('m1', 'm2')})
    broker = plan.broker()
    await _ready(broker, 'm1')

    gate = asyncio.create_task(broker.consume('a1'))
    await _parked(gate)
    gate.cancel()
    with pytest.raises(asyncio.CancelledError):
        await _done(gate)
    await broker.publish('a1', _message('m2'))

    assert await _ids(broker) == ['m1', 'm2']
    assert plan.released == 1


async def test_messages_recorded_for_later_turns_outlive_the_mailbox_and_come_back_with_it():
    plan = _Plan({1: ('m1',), 3: ('m3',), 4: ('m4',)}, turn=2)
    broker = plan.broker()
    await _ready(broker, 'm4', 'm1', 'm9', 'm3')
    await broker.publish('a1', _message('h1', sender='host'))

    await broker.remove_queue('a1')
    await broker.ensure_queue('a1')
    plan.turn = 3
    third = await _ids(broker)
    plan.turn = 4
    fourth = await _ids(broker)

    assert (third, fourth) == (['m3'], ['m4'])
    assert await broker.wait_for_messages('a1', 0) is False


async def test_a_message_due_next_turn_that_came_before_the_mailbox_went_wakes_the_zombie():
    plan = _Plan({2: ('m2',)}, turn=2)
    broker = plan.broker()
    revived: list[str] = []

    async def revive(agent_id: str) -> bool:
        revived.append(agent_id)
        await broker.ensure_queue(agent_id)
        return True

    broker.on_missing_queue(revive)
    await _ready(broker, 'm2')
    await broker.remove_queue('a1')
    for _ in range(3):
        await asyncio.sleep(0)
    assert revived == []
    plan.asleep = True
    for _ in range(5):
        await asyncio.sleep(0)

    assert revived == ['a1']
    assert await _ids(broker) == ['m2']


async def test_a_message_for_a_later_turn_does_not_wake_the_zombie():
    plan = _Plan({3: ('m3',)}, turn=2)
    broker = plan.broker()
    revived: list[str] = []

    async def revive(agent_id: str) -> bool:
        revived.append(agent_id)
        return True

    broker.on_missing_queue(revive)
    plan.asleep = True
    await _ready(broker, 'm3')
    await broker.remove_queue('a1')
    for _ in range(60):
        await asyncio.sleep(0)

    assert revived == []


async def test_a_zombie_already_woken_by_another_delivery_is_not_woken_again():
    plan = _Plan({2: ('m2', 'm5')}, turn=2)
    broker = plan.broker()
    revived: list[str] = []

    async def revive(agent_id: str) -> bool:
        revived.append(agent_id)
        await broker.ensure_queue(agent_id)
        return True

    broker.on_missing_queue(revive)
    await _ready(broker, 'm2')
    await broker.remove_queue('a1')
    assert (await broker.publish('a1', _message('m5'))).ok
    plan.asleep = True
    for _ in range(60):
        await asyncio.sleep(0)

    assert revived == ['a1']
    assert await _ids(broker) == ['m2', 'm5']


async def test_other_agents_mailboxes_are_plain_while_one_agent_has_a_record():
    plan = _Plan({1: ('m1',)})
    broker = plan.broker()
    await broker.ensure_queue('b1')
    for message_id in ('x2', 'x3', 'x1'):
        await broker.publish('b1', Message(sender='s', receiver='b1', content='x', id=message_id))

    assert [message.id for message in await broker.consume('b1')] == ['x2', 'x3', 'x1']
    assert await broker.wait_for_messages('b1', 0) is False
