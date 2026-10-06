"""一个 Agent 的后台调用（spec §5.6 背景化）。

执行器把一次跑得太久、且工具声明了 `__tool_backgroundable__` 的调用转到后台：这一轮先拿到一个占位结
果，真结果出来后在之后某一轮的注入步骤（spec §5.1 第 4 步）以署名 `BACKGROUND` 块进历史。

**一个周期不带着在跑的后台调用结束。** 模型交了最终回复而还有在跑的，内核先等它们；周期因为别的原因
停下（预算、轮次、中止、被杀）时内核取消它们，并把「被取消、副作用未知」记进历史。于是周期之间没有后
台调用在跑，会话存盘（周期收尾时）里也就没有等不到结果的占位。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from syndica.kernel.context import ToolCall
from syndica.kernel.tool import ToolResult

__all__ = ['BackgroundCall', 'BackgroundCalls']


@dataclass
class BackgroundCall:
    """一次转到后台的调用。`state` 是 running / done / cancelled；`why` 是被取消的原因。"""

    id: str
    call: ToolCall
    started: float
    state: str = 'running'
    result: ToolResult | None = None
    why: str = ''
    task: asyncio.Task[None] | None = None
    work: asyncio.Future[object] | None = None


class BackgroundCalls:
    """一个 Agent 的后台调用表：谁在跑、谁有结果还没进历史、等它们的变化。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._calls: dict[str, BackgroundCall] = {}
        self._ready: list[BackgroundCall] = []
        self._changed = asyncio.Event()
        self._issued = 0

    def start(self, call: ToolCall) -> BackgroundCall:
        self._issued += 1
        entry = BackgroundCall(id=f'bg-{self._issued}', call=call, started=self._clock())
        self._calls[entry.id] = entry
        return entry

    def get(self, background_id: str) -> BackgroundCall | None:
        return self._calls.get(background_id)

    def elapsed(self, entry: BackgroundCall) -> float:
        return self._clock() - entry.started

    def finished(self, entry: BackgroundCall, result: ToolResult) -> None:
        """真结果到了。已经被取消的不再收：取消那一刻起它就不归这个 Agent 等了。"""
        if entry.state != 'running':
            return
        entry.state = 'done'
        entry.result = result
        self._ready.append(entry)
        self._changed.set()

    def cancel(self, background_id: str, why: str, *, report: bool) -> BackgroundCall | None:
        """取消一个在跑的；`report` 为真时把这次取消排进历史（Agent 自己要求的不必再告诉它）。"""
        entry = self._calls.get(background_id)
        if entry is None or entry.state != 'running':
            return None
        entry.state = 'cancelled'
        entry.why = why
        # 两个都取消：收尾任务可能还一次都没跑过（事件循环还没轮到它），取消一个没开始的任务不会
        # 执行它的任何代码，工具本身就会一直跑下去。
        for pending in (entry.work, entry.task):
            if pending is not None:
                pending.cancel()
        if report:
            self._ready.append(entry)
        self._changed.set()
        return entry

    def cancel_all(self, why: str) -> None:
        for entry in [entry for entry in self._calls.values() if entry.state == 'running']:
            self.cancel(entry.id, why, report=True)

    @property
    def running(self) -> list[BackgroundCall]:
        return [entry for entry in self._calls.values() if entry.state == 'running']

    @property
    def outstanding(self) -> bool:
        """还有在跑的，或有结果还没进历史的：这时交不了最终回复。"""
        return bool(self._ready) or bool(self.running)

    def take_ready(self) -> list[BackgroundCall]:
        ready, self._ready = self._ready, []
        return ready

    async def wait_for_change(self) -> None:
        """等到有结果可以进历史，或者已经没有在跑的。先 clear 再 wait：这是一个边沿，不是电平。"""
        while not self._ready and self.running:
            self._changed.clear()
            await self._changed.wait()
