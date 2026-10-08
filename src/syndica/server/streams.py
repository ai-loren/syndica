"""会话流与全局流：每个流一个随机 epoch、自己的单调序号、一个有界环形缓冲。

事件 id 是 `<epoch>:<seq>`。重连带 `Last-Event-ID`：同一个 epoch、且下一条还在缓冲里就补发；否则先发
一条 `reset`，客户端重取快照（快照带 `as_of`，用它重新开流）。epoch 每个流各取一个：server 重启或
会话重新打开之后序号从头数，数字相同的 id 指的是另一条事件。
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

__all__ = ['PING', 'RESET', 'RING', 'Entry', 'Stream']

RING = 2048
# 流读到这两个哨兵时分别发 reset 与心跳注释。心跳也是发现客户端已断开的唯一时机：写失败才知道。
RESET = 'reset'
PING = 'ping'


@dataclass(frozen=True)
class Entry:
    seq: int
    kind: str
    data: dict[str, Any]


class Stream:
    def __init__(self, *, capacity: int = RING) -> None:
        self.epoch = secrets.token_hex(8)
        self._seq = 0
        self._ring: deque[Entry] = deque(maxlen=capacity)
        self._changed = asyncio.Event()
        self._closed = False
        self._resets = 0

    @property
    def position(self) -> str:
        """此刻的位置：快照的 `as_of`，客户端用它开流就不会漏掉快照之后的事件。"""
        return f'{self.epoch}:{self._seq}'

    def append(self, kind: str, data: dict[str, Any]) -> Entry:
        self._seq += 1
        entry = Entry(self._seq, kind, data)
        self._ring.append(entry)
        self._wake()
        return entry

    def close(self) -> None:
        self._closed = True
        self._wake()

    def reset(self) -> None:
        """告诉此刻的每个读者先发一条 reset（重取快照）。reset 不是流里的一条事件，没有序号。"""
        self._resets += 1
        self._wake()

    def _wake(self) -> None:
        # 边沿信号：唤醒此刻在等的读者，再换一个新的事件给下一批。不能 set 了不清——那就成了电平，
        # 读者醒来发现没有新条目会立刻再醒，空转不让出。
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def _after(self, last_event_id: str | None) -> tuple[bool, int]:
        """（要不要先发 reset，从哪个序号之后补发）。"""
        if last_event_id is None:
            return False, self._seq
        epoch, _, raw = last_event_id.partition(':')
        if epoch != self.epoch or not (raw.isascii() and raw.isdigit()):
            return True, self._seq
        seq = int(raw)
        oldest = self._ring[0].seq if self._ring else self._seq + 1
        if seq > self._seq or seq + 1 < oldest:
            return True, self._seq
        return False, seq

    async def follow(
        self, last_event_id: str | None, *, heartbeat_s: float = 15.0
    ) -> AsyncIterator[Entry | str]:
        """从 `last_event_id` 之后开始交条目，中间穿插 `RESET` 与 `PING`。流关闭后结束。"""
        reset, after = self._after(last_event_id)
        resets = self._resets
        if reset:
            yield RESET
        while True:
            changed = self._changed
            if self._resets != resets:
                resets = self._resets
                yield RESET
            pending = [entry for entry in self._ring if entry.seq > after]
            if pending and pending[0].seq != after + 1:
                # 读得太慢，要的那一条已经被挤出缓冲：先 reset，从此刻接着读。位置在交出 reset
                # 之前取：消费者处理 reset 的这段时间里进来的条目照样要交给它。
                after = self._seq
                yield RESET
                continue
            for entry in pending:
                yield entry
                after = entry.seq
            if self._closed:
                return
            if pending:
                continue
            timed_out = True
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(heartbeat_s):
                    await changed.wait()
                    timed_out = False
            if timed_out:
                yield PING
