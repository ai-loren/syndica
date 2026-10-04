"""此刻在驱动的 Agent。

Runtime 在每个 Agent 的驱动任务开头设下它，同一段里并发的工具调用与 hook 建在驱动任务里，继承
同一个值。工作区路由按它把调用分到这个 Agent 的工作区并记它看到过的版本；宿主代码（不在任何
驱动任务里）读到的是 None。
"""

from __future__ import annotations

from contextvars import ContextVar

__all__ = ['driving_agent']

_DRIVING_AGENT: ContextVar[str | None] = ContextVar('syndica_driving_agent', default=None)


def driving_agent() -> str | None:
    """此刻在驱动的 Agent 的 id；宿主代码里是 None。"""
    return _DRIVING_AGENT.get()
