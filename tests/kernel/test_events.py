import asyncio
import dataclasses
import gc
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import pytest

from syndica.kernel.errors import ConfigError
from syndica.kernel.events import Event, EventBus, Subscription

# 每个等待都设上限：回归应当让用例变红，而不是把 CI 挂住。
_TIMEOUT = 1.0
# 默认容量必须撑得住一整段无人消费的突发，否则「有界」在默认配置下就等于「一直在丢」。
_BURST = 1000
# 停在空缓冲上的观测窗口，与该窗口内允许烧掉的 CPU 上限。忙轮询会把窗口的两成以上烧成 CPU，
# 真正的阻塞等待只花十分之一毫秒；上限必须留在两者之间。time.process_time() 统计进程内所有
# 线程，因此这条断言只对本实现成立。
_PARKED_WINDOW = 0.1
_MAX_PARKED_CPU = 0.005


def _event(kind: str = 'turn_start', **payload: Any) -> Event:
    return Event(kind=kind, agent_id='a1', payload=payload)


async def _collect(events: Subscription, parked: asyncio.Event | None = None) -> list[Event]:
    """收到订阅结束为止。给了 parked 就在每次取下一条之前置位它。

    置位与进入 __anext__ 之间没有挂起点，所以用例等到 parked 返回时，消费者必定已经停在空缓
    冲上，「唤醒」才有可言。
    """
    seen: list[Event] = []
    while True:
        if parked is not None:
            parked.set()
        try:
            seen.append(await anext(events))
        except StopAsyncIteration:
            return seen


async def _cpu_burned_while_idle() -> float:
    """让出一段墙钟时间，回报这段时间里本进程烧掉的 CPU。"""
    started = time.process_time()
    await asyncio.sleep(_PARKED_WINDOW)
    return time.process_time() - started


@pytest.fixture
async def consumers() -> AsyncIterator[list[asyncio.Task[Any]]]:
    """用例中途失败时收干净还挂着的消费者，否则事件循环关闭时报 Task was destroyed。"""
    tasks: list[asyncio.Task[Any]] = []
    yield tasks
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


def test_event_payload_defaults_to_empty_and_is_not_shared_between_instances():
    first = Event(kind='turn_start', agent_id='a1')
    second = Event(kind='turn_start', agent_id='a1')
    first.payload['turn'] = 1
    assert second.payload == {}


def test_event_rejects_field_rebinding():
    event = Event(kind='turn_start', agent_id='a1')
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.kind = 'turn_end'  # pyright: ignore[reportAttributeAccessIssue]


async def test_subscriber_receives_the_emitted_event():
    bus = EventBus()
    async with bus.subscribe() as events:
        await bus.emit(_event('turn_start', turn=1))
        received = await asyncio.wait_for(anext(events), timeout=_TIMEOUT)
    assert received.kind == 'turn_start'
    assert received.payload == {'turn': 1}


async def test_every_subscriber_receives_the_same_event():
    """CLI 与 server 会同时订阅同一条流，不能互相抢消息。"""
    bus = EventBus()
    async with bus.subscribe() as cli, bus.subscribe() as server:
        await bus.emit(_event('turn_end'))
        assert (await asyncio.wait_for(anext(cli), timeout=_TIMEOUT)).kind == 'turn_end'
        assert (await asyncio.wait_for(anext(server), timeout=_TIMEOUT)).kind == 'turn_end'


async def test_events_are_delivered_in_emission_order():
    bus = EventBus()
    events = bus.subscribe()
    for kind in ('agent_start', 'turn_start', 'turn_end'):
        await bus.emit(_event(kind))
    await bus.close()
    delivered = await asyncio.wait_for(_collect(events), timeout=_TIMEOUT)
    assert [event.kind for event in delivered] == ['agent_start', 'turn_start', 'turn_end']


async def test_emit_without_subscribers_does_not_block():
    bus = EventBus()
    await asyncio.wait_for(bus.emit(_event()), timeout=_TIMEOUT)


async def test_mutating_a_nested_payload_container_after_emit_does_not_reach_subscribers():
    bus = EventBus()
    payload: dict[str, Any] = {'tools': ['read']}
    async with bus.subscribe() as events:
        await bus.emit(Event(kind='turn_start', agent_id='a1', payload=payload))
        payload['tools'].append('write')
        received = await asyncio.wait_for(anext(events), timeout=_TIMEOUT)
    assert received.payload == {'tools': ['read']}


async def test_one_subscriber_mutating_its_event_does_not_reach_another():
    bus = EventBus()
    async with bus.subscribe() as cli, bus.subscribe() as server:
        await bus.emit(Event(kind='turn_start', agent_id='a1', payload={'tools': ['read']}))
        mine = await asyncio.wait_for(anext(cli), timeout=_TIMEOUT)
        mine.payload['tools'].append('write')
        theirs = await asyncio.wait_for(anext(server), timeout=_TIMEOUT)
    assert theirs.payload == {'tools': ['read']}


async def test_overflow_keeps_the_newest_events_and_drops_the_oldest():
    bus = EventBus(capacity=3)
    events = bus.subscribe()
    for index in range(10):
        await bus.emit(_event('turn_end', index=index))
    await bus.close()
    delivered = await asyncio.wait_for(_collect(events), timeout=_TIMEOUT)
    assert [event.payload['index'] for event in delivered] == [7, 8, 9]


async def test_dropped_counts_every_event_the_buffer_could_not_hold():
    bus = EventBus(capacity=3)
    async with bus.subscribe() as events:
        for index in range(10):
            await bus.emit(_event('turn_end', index=index))
        assert events.dropped == 7


async def test_dropped_stays_zero_while_the_subscriber_keeps_up():
    bus = EventBus(capacity=2)
    async with bus.subscribe() as events:
        for index in range(10):
            await bus.emit(_event('turn_end', index=index))
            await asyncio.wait_for(anext(events), timeout=_TIMEOUT)
        assert events.dropped == 0


async def test_each_subscriber_counts_drops_on_its_own_buffer():
    bus = EventBus(capacity=1)
    async with bus.subscribe() as slow, bus.subscribe() as fast:
        for index in range(4):
            await bus.emit(_event('turn_end', index=index))
            await asyncio.wait_for(anext(fast), timeout=_TIMEOUT)
        assert (slow.dropped, fast.dropped) == (3, 0)


async def test_emit_does_not_block_when_a_subscribers_buffer_is_full():
    bus = EventBus(capacity=1)
    async with bus.subscribe():
        await bus.emit(_event())
        await asyncio.wait_for(bus.emit(_event()), timeout=_TIMEOUT)


async def test_the_default_capacity_absorbs_a_long_burst_without_dropping():
    bus = EventBus()
    async with bus.subscribe() as events:
        for _ in range(_BURST):
            await bus.emit(_event())
        assert events.dropped == 0


def test_bus_rejects_a_capacity_that_cannot_hold_anything():
    """capacity=0 会让每条事件入队即被丢掉，安静得像总线坏了。"""
    with pytest.raises(ConfigError) as excinfo:
        EventBus(capacity=0)
    assert 'capacity' in excinfo.value.next_action


async def test_close_wakes_a_subscriber_parked_on_an_empty_buffer(
    consumers: list[asyncio.Task[Any]],
):
    bus = EventBus()
    events = bus.subscribe()
    parked = asyncio.Event()
    consumer = asyncio.create_task(_collect(events, parked))
    consumers.append(consumer)
    await asyncio.wait_for(parked.wait(), timeout=_TIMEOUT)
    await bus.close()
    assert await asyncio.wait_for(consumer, timeout=_TIMEOUT) == []


async def test_a_subscriber_parked_on_a_never_used_buffer_does_not_burn_cpu(
    consumers: list[asyncio.Task[Any]],
):
    """忙轮询与阻塞等待拿到同样的事件、耗掉同样的墙钟时间，只有 CPU 时间能区分两者。"""
    bus = EventBus()
    events = bus.subscribe()
    parked = asyncio.Event()
    consumer = asyncio.create_task(_collect(events, parked))
    consumers.append(consumer)
    await asyncio.wait_for(parked.wait(), timeout=_TIMEOUT)
    burned = await _cpu_burned_while_idle()
    await bus.close()
    assert await asyncio.wait_for(consumer, timeout=_TIMEOUT) == []
    assert burned < _MAX_PARKED_CPU


async def test_a_subscriber_parked_after_draining_its_buffer_does_not_burn_cpu(
    consumers: list[asyncio.Task[Any]],
):
    """轮次之间的空闲是常态：取空之后仍停在空缓冲上，一样不能空转。"""
    bus = EventBus()
    events = bus.subscribe()
    await bus.emit(_event())
    parked = asyncio.Event()
    consumer = asyncio.create_task(_collect(events, parked))
    consumers.append(consumer)
    await asyncio.wait_for(parked.wait(), timeout=_TIMEOUT)
    burned = await _cpu_burned_while_idle()
    await bus.close()
    assert len(await asyncio.wait_for(consumer, timeout=_TIMEOUT)) == 1
    assert burned < _MAX_PARKED_CPU


async def test_close_lets_a_subscriber_drain_what_was_already_buffered():
    bus = EventBus()
    events = bus.subscribe()
    await bus.emit(_event('turn_end'))
    await bus.close()
    delivered = await asyncio.wait_for(_collect(events), timeout=_TIMEOUT)
    assert [event.kind for event in delivered] == ['turn_end']


async def test_emit_after_close_reaches_nobody():
    bus = EventBus()
    events = bus.subscribe()
    await bus.close()
    await bus.emit(_event())
    assert await asyncio.wait_for(_collect(events), timeout=_TIMEOUT) == []


async def test_subscribe_after_close_ends_immediately():
    """否则迟到的订阅者会永久挂在空缓冲上等一个永不到来的事件。"""
    bus = EventBus()
    await bus.close()
    assert await asyncio.wait_for(_collect(bus.subscribe()), timeout=_TIMEOUT) == []


async def test_closing_the_bus_twice_is_a_noop():
    bus = EventBus()
    events = bus.subscribe()
    await bus.close()
    await bus.close()
    assert await asyncio.wait_for(_collect(events), timeout=_TIMEOUT) == []


async def test_leaving_an_async_with_block_deregisters_the_subscription():
    # events 在块外仍被引用，缓冲因此不会被回收：这里量的是 __aexit__ 真的退了订，
    # 而不是垃圾回收替它把痕迹擦掉了。
    bus = EventBus()
    async with bus.subscribe() as events:
        assert len(bus._buffers) == 1
    assert len(bus._buffers) == 0
    assert await asyncio.wait_for(_collect(events), timeout=_TIMEOUT) == []


async def test_closing_a_subscription_ends_a_parked_consumer(
    consumers: list[asyncio.Task[Any]],
):
    bus = EventBus()
    events = bus.subscribe()
    parked = asyncio.Event()
    consumer = asyncio.create_task(_collect(events, parked))
    consumers.append(consumer)
    await asyncio.wait_for(parked.wait(), timeout=_TIMEOUT)
    events.close()
    assert await asyncio.wait_for(consumer, timeout=_TIMEOUT) == []


async def test_closing_a_subscription_discards_what_it_had_not_taken():
    bus = EventBus()
    events = bus.subscribe()
    await bus.emit(_event('turn_end'))
    events.close()
    assert await asyncio.wait_for(_collect(events), timeout=_TIMEOUT) == []


async def test_closing_one_subscription_stops_only_that_one():
    bus = EventBus()
    async with bus.subscribe() as kept:
        leaving = bus.subscribe()
        leaving.close()
        await bus.emit(_event('turn_end'))
        assert (await asyncio.wait_for(anext(kept), timeout=_TIMEOUT)).kind == 'turn_end'
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(leaving), timeout=_TIMEOUT)


async def test_closing_a_subscription_twice_is_a_noop():
    bus = EventBus()
    events = bus.subscribe()
    events.close()
    events.close()
    assert await asyncio.wait_for(_collect(events), timeout=_TIMEOUT) == []
    assert len(bus._buffers) == 0


async def test_breaking_out_of_async_for_leaves_no_buffer_behind(
    consumers: list[asyncio.Task[Any]],
):
    """Task 13 与 CLI 都靠 break 退出订阅；缓冲不能因此永远挂在总线上。"""
    bus = EventBus()
    subscribed = asyncio.Event()

    async def walk_away() -> None:
        events = bus.subscribe()
        subscribed.set()
        async for _ in events:
            break

    task = asyncio.create_task(walk_away())
    consumers.append(task)
    await asyncio.wait_for(subscribed.wait(), timeout=_TIMEOUT)
    await bus.emit(_event())
    await asyncio.wait_for(task, timeout=_TIMEOUT)
    gc.collect()
    assert len(bus._buffers) == 0


async def test_a_subscription_the_consumer_dropped_stops_being_tracked():
    """忘了退订是兜底路径：缓冲随 Subscription 一并被回收，而不是留到总线关闭。"""
    bus = EventBus()
    subscription = bus.subscribe()
    assert len(bus._buffers) == 1
    del subscription
    gc.collect()
    assert len(bus._buffers) == 0
