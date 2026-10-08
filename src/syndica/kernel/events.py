"""进程内事件流。

事件流首先是进程内可订阅的，HTTP SSE 只是它的一个出口（spec §9.1 末条）。若只在 HTTP 层
存在，CLI 的交互模式就只能自己再实现一套，那正是「chat 必须是 run --session 的外壳」所要
禁止的分叉。

多订阅者各自持有独立缓冲：CLI 与 server 会同时订阅，不能互相抢消息。
"""

from __future__ import annotations

import asyncio
import copy
import time
import weakref
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType, TracebackType
from typing import Any, Self

from syndica.kernel.errors import ConfigError, ContractViolation

__all__ = ['KERNEL_EVENTS', 'Event', 'EventBus', 'Subscription']


def validate_positive_int(value: object, field: str) -> int:
    """公开入口的类型轴：先拒 bool（YAML 1.1 把 yes 读成 True），再判类型，再判值域。

    bool 是 int 的子类：`True < 1` 为 False，于是 `capacity=True` 会静默退化成容量 1。
    """
    if isinstance(value, bool):
        raise ConfigError(
            expected=f'{field} 是正整数',
            received=(
                f'{field}={value!r}：bool 是 int 的子类，YAML 1.1 会把 yes 读成 True，'
                'True 在数值比较里等于 1，于是缓冲容量静默退化成 1 条'
            ),
            next_action=f'传一个 >= 1 的整数 {field}，不要传布尔值',
        )
    if not isinstance(value, int):
        raise ConfigError(
            expected=f'{field} 是正整数',
            received=f'{field}={value!r}，类型是 {type(value).__name__}',
            next_action=f'传一个 >= 1 的整数 {field}',
        )
    if value < 1:
        raise ConfigError(
            expected=f'{field} 是正整数：每个订阅者最多缓冲多少条事件',
            received=(
                f'{field}={value!r}：容量不足 1 时每条事件入队即被挤掉，'
                '订阅者永远收不到任何东西，且没有任何报错'
            ),
            next_action=f'传一个 >= 1 的 {field}，或省略它用默认容量',
        )
    return value


@dataclass(frozen=True)
class Event:
    """一条事件记录。

    `payload` 必须是**纯数据**：dict / list / str / 数字 / bool / None 及其嵌套，能被
    `copy.deepcopy` 复制。锁、生成器、模块、文件、`asyncio.Task` 这类活对象不行，`EventBus.emit`
    会当场抛 `ContractViolation`——事件要能同样地走进程内出口与序列化的 SSE 出口，两边都得能复制它。

    投递出去的事件**只读**：所有订阅者拿到的是同一份快照，改动 payload 就改到了别人的那一份。
    """

    kind: str
    agent_id: str
    payload: dict[str, Any] = field(default_factory=dict[str, Any])
    sequence: int = 0
    # 墙钟纳秒，总线投递时打；0 是未知。不参与相等比较：同一件事两次运行的时刻不同。
    time_ns: int = field(default=0, compare=False)


# 内核自己发的事件种类与各自 payload 里必有的键。宿主与 Module 可以发别的种类；SSE 出口按这张表
# 给每种内核事件生成 schema，所以内核新增一种事件必须同时在这里加一行。
KERNEL_EVENTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        'agent_start': (),
        'agent_end': ('stop_reason',),
        'agent_error': ('error_type', 'expected', 'received', 'next_action'),
        'turn_start': ('turn',),
        'turn_end': ('turn',),
        'compaction': ('trigger', 'blocks_before', 'blocks_after', 'tokens_before', 'tokens_after'),
        'budget_exhausted': ('dimensions', 'disposition'),
        'budget_changed': ('view',),
        'progress_measured': (
            'satisfied',
            'mandatory',
            'pending_human',
            'measured_turn',
            'verdict',
        ),
        'progress_regressed': ('rubrics',),
        'progress_stalled': ('turns',),
        'goal_drift': ('confirmed',),
        'user_input_requested': ('request_id', 'question'),
        'user_question_requested': ('request_id', 'question', 'options'),
        'tool_confirmation_requested': (
            'request_id',
            'tool_call_id',
            'tool_name',
            'arguments',
            'verdict',
            'untrusted',
        ),
        'human_request_resolved': ('request_id', 'outcome'),
        'message_appended': ('generation', 'index', 'block'),
        'transcript_reset': ('generation', 'reason'),
        'tool_call_started': ('tool_call_id', 'name', 'arguments'),
        'tool_call_finished': ('tool_call_id', 'outcome'),
        'agent_spawned': ('parent_id', 'name'),
        'agent_status': ('status',),
        'agent_removed': (),
        'artifact_registered': ('ref', 'note'),
    }
)


def _require_unassigned_sequence(event: Event) -> None:
    """事件序号归总线所有；调用方只能交未分配的 0。"""
    if type(event.sequence) is int and event.sequence == 0:
        return
    raise ContractViolation(
        expected='Event.sequence=0，表示尚未由 EventBus 分配序号',
        received=f'收到 sequence={event.sequence!r}，类型是 {type(event.sequence).__name__}',
        next_action=(
            '删掉 sequence 参数或传 0；EventBus.emit 会按投递顺序分配从 1 开始的序号，'
            '调用方自填会制造重复或倒退的事件流'
        ),
    )


def _snapshot(event: Event) -> Event:
    """事件的独立副本。

    存副本而不是调用方的对象：否则调用方 emit 之后改动 payload 里的嵌套容器就改到了已入队的
    内容，而序列化的 SSE 出口不会，换出口即行为分叉。
    """
    try:
        return copy.deepcopy(event)
    except Exception as exc:
        raise ContractViolation(
            expected=(
                'event.payload 是纯数据：dict / list / str / 数字 / bool / None 及其嵌套，可深拷贝'
            ),
            received=f'{_why_copying_failed(event.payload, exc)}；事件 kind={event.kind!r}',
            next_action='把这个值换成可序列化的表示再 emit，例如它的 id、名字或 repr 字符串',
        ) from exc


def _why_copying_failed(
    payload: dict[str, Any], exc: Exception, *, container: str = 'payload'
) -> str:
    """指出是哪个键拷不动。逐键重试是拿到键名的唯一途径：copy 抛的异常里只有类型名。"""
    for key, value in payload.items():
        try:
            copy.deepcopy(value)
        except Exception:
            return f'{container}[{key!r}] 是 {type(value).__name__}，深拷贝失败：{exc}'
    return f'{container} 是 {type(payload).__name__}，深拷贝失败：{exc}'


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
        """收下一条；缓冲已满则先挤掉最旧的一条，并记一次丢弃。"""
        if len(self._events) == self._capacity:
            self.dropped += 1
        self._events.append(event)
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
        """本订阅溢出丢掉的事件数，单调递增，退订与总线关闭都不清零。

        缓冲满时 emit 丢最旧的一条，既不阻塞也不报错；消费者靠这个数把「什么都没发生」与
        「我漏了 37 条」区分开——渲染这条流的界面否则只会显示一段没有解释的空白。它只告诉总数、
        不告诉漏在流的哪个位置，要位置信息得由消费方自己在流里补标记。
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

    def __init__(self, *, capacity: object = 1024) -> None:
        self._capacity = validate_positive_int(capacity, 'capacity')
        # 弱引用：消费者忘了退订时，缓冲随 Subscription 一并被回收，emit 不会一直往一个
        # 再也没人读的缓冲里写。它是兜底，确定的退订时机由 Subscription.close 给。
        self._buffers: weakref.WeakSet[_Buffer] = weakref.WeakSet()
        self._closed = False
        self._next_sequence = 1

    async def emit(self, event: Event) -> None:
        """把同一份快照投给每个订阅者。

        **不阻塞，也不会因为订阅者读得慢而失败**：可观测性不得拖垮主流程，所以缓冲满了只是挤掉
        该订阅者最旧的一条并给它的 `dropped` 加一。关闭后与无订阅者时事件直接丢弃。

        `event.payload` 不是纯数据时抛 `ContractViolation`（见 `Event`）：拿不到副本的事件本来
        就不能既进内存队列又出 SSE，静默降级成一条残缺的可观测记录更糟。拷贝只发生一次、在投递
        之前，因此要么所有订阅者都收到，要么谁都没收到。
        """
        self._publish(event)

    def _publish(self, event: Event) -> None:
        """`emit` 的同步本体。轨迹的观察者在 `Context.append` 里调它：追加与它的事件之间不能有
        挂起点，否则别的任务在两者之间发的事件会排到这一块前面，而客户端按事件顺序重建轨迹。"""
        # 无订阅者、总线已关闭时也照样拷：否则 payload 合不合约要看当时有没有人在听，一个确定
        # 的编程错误会退化成只在特定时序下现身的 heisenbug。
        _require_unassigned_sequence(event)
        snapshot = _snapshot(event)
        if self._closed:
            return
        snapshot = replace(
            snapshot, sequence=self._next_sequence, time_ns=snapshot.time_ns or time.time_ns()
        )
        self._next_sequence += 1
        # 必须先快照订阅者集合：缓冲满时被挤掉的那条事件在 append 里当场回收，它 payload 的
        # __del__ 于是运行在本循环中途，而 __del__ 里 subscribe / close 会改动 _buffers——直接
        # 迭代 WeakSet 会撞 RuntimeError: Set changed size during iteration。
        for buffer in list(self._buffers):
            buffer.offer(snapshot)

    @property
    def subscriber_count(self) -> int:
        """当前还在收事件的订阅数。

        兜底回收把「忘了持有 Subscription」变成一次无声的空订阅，这个数是唯一能看见它的地方。
        """
        return len(self._buffers)

    def subscribe(self) -> Subscription:
        """订阅。**同步注册**：返回时已在收事件，此后 emit 的每一条都不会漏。

        规范写法是 `async with bus.subscribe() as sub:`——退订时机确定，且 `sub` 有名字可以读
        `dropped`。裸 `async for event in bus.subscribe():` 两样都没有：匿名订阅看不到丢弃数，
        清理也只能等兜底回收。

        返回的 Subscription 必须一直被引用：放手即停止收事件，且不报错，只有
        `subscriber_count` 会显示它已经不在了。

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
