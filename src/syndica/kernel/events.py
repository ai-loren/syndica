"""进程内事件流。

事件流首先是进程内可订阅的，HTTP SSE 只是它的一个出口（spec §9.1 末条）。若只在 HTTP 层
存在，CLI 的交互模式就只能自己再实现一套，那正是「chat 必须是 run --session 的外壳」所要
禁止的分叉。

多订阅者各自持有独立缓冲：CLI 与 server 会同时订阅，不能互相抢消息。
"""

from __future__ import annotations

import asyncio
import copy
import weakref
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

from syndica.kernel.errors import ConfigError

__all__ = ['Event', 'EventBus', 'Subscription']


@dataclass(frozen=True)
class Event:
    kind: str
    agent_id: str
    payload: dict[str, Any] = field(default_factory=dict[str, Any])


class _Buffer:
    """一个订阅者的独立事件缓冲。

    `_ready` 是边沿信号，只由入队与结束置位、由等待者在扫描前清位。它不表示「有东西可
    取」，醒来必须复检 `_events` 与 `_ended`。
    """

    def __init__(self, capacity: int) -> None:
        self._events: deque[Event] = deque(maxlen=capacity)
        self._capacity = capacity
        self._ready = asyncio.Event()
        self._ended = False
        self.dropped = 0

    def offer(self, event: Event) -> None:
        """收下一份独立副本；缓冲已满则先挤掉最旧的一条，并记一次丢弃。

        存副本而不是调用方的对象：否则调用方 emit 之后改动 payload 里的嵌套容器就改到了
        已入队的内容，而序列化的 SSE 出口不会，换出口即行为分叉；两个订阅者之间同理。
        """
        if len(self._events) == self._capacity:
            self.dropped += 1
        self._events.append(copy.deepcopy(event))
        self._ready.set()

    def end(self) -> None:
        """总线关闭：已缓冲的事件仍可取走，取完即结束。"""
        self._ended = True
        self._ready.set()

    def abandon(self) -> None:
        """消费者退订：未取走的事件一并丢弃。"""
        self._events.clear()
        self._ended = True
        self._ready.set()

    async def take(self) -> Event:
        """取下一条；缓冲已空且订阅已结束时抛 StopAsyncIteration。"""
        while True:
            if self._events:
                return self._events.popleft()
            if self._ended:
                raise StopAsyncIteration
            # 清位与下面的 await 之间不得插入挂起点：这之后的每一次置位才一定能把我们唤
            # 醒。清位也不能省——信号恒为高时 wait() 立即返回，本循环就成了不让出控制权的
            # 死转，连 asyncio.wait_for 都拦不住。
            self._ready.clear()
            await self._ready.wait()


class Subscription:
    """一路订阅：独立缓冲、独立丢弃计数。

    既是 async iterator 也是 async context manager。退订走 `async with`（离开代码块即退
    订）或显式 `close()`，两者幂等；**从 `async for` 里 break 出去不算退订**，此后仍会继
    续收事件，直到本对象被回收。
    """

    def __init__(self, buffer: _Buffer, detach: Callable[[_Buffer], None]) -> None:
        self._buffer = buffer
        self._detach = detach

    @property
    def dropped(self) -> int:
        """本订阅溢出丢掉的事件数，单调递增。

        缓冲满时 emit 丢最旧的一条，既不阻塞也不报错；消费者靠这个数把「什么都没发生」与
        「我漏了 37 条」区分开——渲染这条流的界面否则只会显示一段没有解释的空白。
        """
        return self._buffer.dropped

    def close(self) -> None:
        """退订：从总线摘掉缓冲、丢弃未取走的事件，停在迭代上的消费者立刻结束。幂等。"""
        self._detach(self._buffer)
        self._buffer.abandon()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Event:
        return await self._buffer.take()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class EventBus:
    """进程内事件总线。每个订阅者一个有界缓冲。

    缓冲满时 emit **丢最旧的一条**并给该订阅者的 `dropped` 加一。emit 在主流程的热路径
    上，阻塞或抛错都会让一个不读的订阅者拖垮整轮，而无界队列只是把拖垮方式从阻塞换成吃内
    存；丢最旧则保证慢订阅者恢复消费后看到的是最近的窗口，而不是永远落后一整个缓冲。
    """

    def __init__(self, *, capacity: int = 1024) -> None:
        if capacity < 1:
            raise ConfigError(
                expected='capacity 是正整数：每个订阅者最多缓冲多少条事件',
                received=(
                    f'capacity={capacity!r}：容量不足 1 时每条事件入队即被挤掉，'
                    '订阅者永远收不到任何东西，且没有任何报错'
                ),
                next_action='传一个 >= 1 的 capacity，或省略它用默认容量',
            )
        self._capacity = capacity
        # 弱引用：消费者忘了退订时，缓冲随 Subscription 一并被回收，emit 不会一直往一个
        # 再也没人读的缓冲里写。它是兜底，确定的退订时机由 Subscription.close 给。
        self._buffers: weakref.WeakSet[_Buffer] = weakref.WeakSet()
        self._closed = False

    async def emit(self, event: Event) -> None:
        """给每个订阅者投一份独立副本。

        关闭后与无订阅者时直接丢弃，某个订阅者的缓冲已满时挤掉它最旧的一条：可观测性不得拖
        垮主流程，所以这里既不抛异常也不阻塞。
        """
        if self._closed:
            return
        for buffer in list(self._buffers):
            buffer.offer(event)

    def subscribe(self) -> Subscription:
        """订阅。**同步注册**：返回时已在收事件，此后 emit 的每一条都不会漏。

        总线已关闭时返回一个立刻结束的空订阅，迟到的订阅者不会挂在空缓冲上等一个永不到来的
        事件。
        """
        buffer = _Buffer(self._capacity)
        if self._closed:
            buffer.end()
        else:
            self._buffers.add(buffer)
        return Subscription(buffer, self._buffers.discard)

    async def close(self) -> None:
        """关闭总线：已缓冲的事件仍可取走，取完订阅即结束；此后 emit 是 no-op。幂等。"""
        self._closed = True
        for buffer in list(self._buffers):
            buffer.end()
