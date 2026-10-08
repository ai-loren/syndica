"""墙钟截止时刻对在飞调用的信号。

模型调用的上限交给路由器：到点取消这次调用，并且不把它当成平台故障去换平台。内核不在
`llm.create` 外包超时——那会把取消域从 provider 手里拿走，远端连接留在那里。

已经带超时的工具与人的等待、派活等待读 `wall_seconds_left()`，把上限收成 `min(自己的, 剩余)`。
没有超时的工具不读它，也不因此被取消。
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token

__all__ = [
    'WRAP_UP_GRACE_S',
    'BudgetDeadline',
    'arm_model_call_limit',
    'arm_wrap_grace',
    'bind_wall_clock',
    'model_call_limit',
    'reset_model_call_limit',
    'reset_wall_clock',
    'reset_wrap_grace',
    'wall_seconds_left',
]

# 计划中的收尾轮可以越过截止时刻，但只多这一段：挂住的模型调用不能把墙钟预算变成无限。
WRAP_UP_GRACE_S = 30.0

_LIMIT: ContextVar[float | None] = ContextVar('syndica_model_call_limit_s', default=None)
_GRACE: ContextVar[float] = ContextVar('syndica_wrap_up_grace_s', default=0.0)
_WALL: ContextVar[Callable[[], float | None] | None] = ContextVar(
    'syndica_wall_seconds_left', default=None
)


class BudgetDeadline(Exception):
    """这次模型调用因为预算的墙钟到了被取消。不是平台故障：路由器不得因此换平台。"""


def model_call_limit() -> float | None:
    """这次模型调用最多再跑多少秒；None 表示不另加墙钟上限。"""
    return _LIMIT.get()


def arm_model_call_limit(seconds: float | None) -> Token[float | None]:
    return _LIMIT.set(seconds)


def reset_model_call_limit(token: Token[float | None]) -> None:
    _LIMIT.reset(token)


def arm_wrap_grace(seconds: float) -> Token[float]:
    return _GRACE.set(seconds)


def wrap_grace() -> float:
    return _GRACE.get()


def reset_wrap_grace(token: Token[float]) -> None:
    _GRACE.reset(token)


def bind_wall_clock(reader: Callable[[], float | None]) -> Token[Callable[[], float | None] | None]:
    return _WALL.set(reader)


def reset_wall_clock(token: Token[Callable[[], float | None] | None]) -> None:
    _WALL.reset(token)


def wall_seconds_left() -> float | None:
    """当前 Agent 的墙钟还剩多少秒；没设墙钟上限、或不在它的任务里时是 None。"""
    reader = _WALL.get()
    if reader is None:
        return None
    return reader()
