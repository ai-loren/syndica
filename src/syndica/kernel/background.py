"""一个 Agent 的后台调用（spec §5.6 背景化）。

执行器把一次跑得太久、且工具声明了 `__tool_backgroundable__` 的调用转到后台：这一轮先拿到一个占位结
果，真结果出来后在之后某一轮的注入步骤（spec §5.1 第 4 步）以署名 `BACKGROUND` 块进历史。

**一个周期不带着在跑的后台调用结束。** 模型交了最终回复而还有在跑的，内核先等它们；周期因为别的原因
停下（预算、轮次、中止、被杀）时内核取消它们，并把「被取消、副作用未知」记进历史。于是周期之间没有后
台调用在跑，会话存盘（周期收尾时）里也就没有等不到结果的占位。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from syndica.kernel.context import ContextBlock, ToolCall
from syndica.kernel.tool import ToolResult

__all__ = ['BackgroundCall', 'BackgroundCalls', 'drain_cancelled']

logger = logging.getLogger(__name__)

# 取消之后等工具收完尾的上限：模块关着时 despawn 返回那一刻工具的取消处理已经跑完，转到后台不能
# 改变这一点；一个吞掉取消的工具也不能让收尾永远等下去。
CANCEL_DRAIN_S = 5.0
_ISSUED = re.compile(r'\bbg-(\d+)\b')


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
    # 工具抛出的 `SyndicaError`：三件套写给运维，Agent 注入结果时把它抛出来，周期停在 ERROR。
    error: BaseException | None = None


class BackgroundCalls:
    """一个 Agent 的后台调用表：谁在跑、谁有结果还没进历史、等它们的变化。"""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        history: Callable[[], Iterable[ContextBlock]] = tuple,
    ) -> None:
        self._clock = clock
        self._history = history
        self._calls: dict[str, BackgroundCall] = {}
        self._ready: list[BackgroundCall] = []
        self._changed = asyncio.Event()
        self._issued: int | None = None

    def start(self, call: ToolCall) -> BackgroundCall:
        # 编号在这个 Agent 的历史里唯一，不只在这张表里：恢复的会话与分叉出来的 Agent 带着上一张表
        # 发过的编号，撞上了，新结果就被当成已经进过历史而丢掉，模型读到的是旧的那一条。
        if self._issued is None:
            self._issued = _highest_issued(self._history())
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

    def cancel_all(self, why: str) -> list[BackgroundCall]:
        cancelled = [entry for entry in self._calls.values() if entry.state == 'running']
        for entry in cancelled:
            self.cancel(entry.id, why, report=True)
        return cancelled

    def lost(self, entry: BackgroundCall, why: str) -> None:
        """收尾任务没交回结果就退出了：按取消记进历史，不再等它。"""
        if entry.state != 'running':
            return
        entry.state = 'cancelled'
        entry.why = why
        self._ready.append(entry)
        self._changed.set()

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

    async def wait_idle(self) -> None:
        """等到没有在跑的。有结果待注入不算：那是电平，拿它当条件会在别的还在跑时空转。"""
        while self.running:
            self._changed.clear()
            await self._changed.wait()


def _highest_issued(blocks: Iterable[ContextBlock]) -> int:
    highest = 0
    for block in blocks:
        for found in _ISSUED.finditer(str(block.content or '')):
            highest = max(highest, int(found.group(1)))
    return highest


async def drain_cancelled(futures: Iterable[asyncio.Future[Any] | None]) -> None:
    """等被取消的工具与收尾任务收完尾，至多 `CANCEL_DRAIN_S` 秒；到时还没收完的记一条 WARNING。"""
    pending = {future for future in futures if future is not None and not future.done()}
    if not pending:
        return
    _, unsettled = await asyncio.wait(pending, timeout=CANCEL_DRAIN_S)
    if unsettled:
        logger.warning(
            '期望: 被取消的后台调用在 %s 秒内收完尾。 实际: 还有 %d 个没收完，内核不再等它们。'
            ' 下一步: 检查对应工具的取消处理（它可能吞掉了取消，或在清理里一直等）。',
            CANCEL_DRAIN_S,
            len(unsettled),
        )
