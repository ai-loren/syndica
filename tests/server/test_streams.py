"""流：补发、过期 reset、落后太多 reset、空闲心跳。"""

from __future__ import annotations

import asyncio

from syndica.server.streams import PING, RESET, Entry, Stream


async def _take(stream: Stream, last: str | None, count: int, **options: float) -> list[object]:
    frames: list[object] = []
    async for item in stream.follow(last, **options):
        frames.append(item.seq if isinstance(item, Entry) else item)
        if len(frames) >= count:
            break
    return frames


async def test_a_reader_that_falls_behind_the_ring_gets_a_reset_and_continues_from_now():
    stream = Stream(capacity=3)
    reader = stream.follow(f'{stream.epoch}:0')
    for _ in range(2):
        stream.append('k', {})
    first = await anext(reader)
    for _ in range(5):
        stream.append('k', {})
    second = await anext(reader)
    assert isinstance(first, Entry) and isinstance(second, Entry)
    assert (first.seq, second.seq) == (1, 2)
    assert await anext(reader) == RESET
    stream.append('k', {})
    after = await anext(reader)
    assert isinstance(after, Entry) and after.seq == 8


async def test_an_idle_stream_sends_heartbeats():
    stream = Stream()
    frames = await asyncio.wait_for(_take(stream, None, 2, heartbeat_s=0.01), 2)
    assert frames == [PING, PING]


async def test_a_position_from_the_future_or_garbage_gets_a_reset():
    stream = Stream()
    stream.append('k', {})
    for last in (f'{stream.epoch}:9', f'{stream.epoch}:x', 'nonsense'):
        assert await asyncio.wait_for(_take(stream, last, 1, heartbeat_s=0.01), 2) == [RESET]


async def test_a_closed_stream_ends_its_readers_after_the_backlog():
    stream = Stream()
    stream.append('k', {})
    stream.append('k', {})
    stream.close()
    frames = [item async for item in stream.follow(f'{stream.epoch}:0')]
    assert [item.seq for item in frames if isinstance(item, Entry)] == [1, 2]
