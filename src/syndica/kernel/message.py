"""消息类型。

两类消息统一经 Runtime.send 发出，由 trigger 区分消费时机（spec §4.5）：
PENDING 在下一轮 turn 开始前消费；DEFERRED 等整个 Agent 结束后依次消费。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

from syndica.kernel.context import TrustLevel

__all__ = ['DeliveryResult', 'Message', 'Trigger']


class Trigger(Enum):
    PENDING = 'pending'
    DEFERRED = 'deferred'


@dataclass
class Message:
    sender: str
    receiver: str
    content: str
    trigger: Trigger = Trigger.PENDING
    correlation_id: str | None = None
    trust: TrustLevel = TrustLevel.TRUSTED
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class DeliveryResult:
    """投递结果。失败返回结构化结果而不抛异常，调用方才能识别可重试的瞬时错误。

    reason 面向调用方，因此与异常一样带「期望 / 实际 / 下一步」三件套。
    """

    ok: bool
    message_id: str | None = None
    reason: str | None = None
