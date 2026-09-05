"""上下文数据类型。

Context 只承载产出与终态：轨迹块，以及一个终止原因（含 ABORTED）。
**运行中的控制状态不在这里** —— 当前状态、是否被挂起、中止是否已置位都归
Registry（spec §4.3）。理由是 Agent 阻塞或崩溃时无法自报状态，所以状态必须
在它之外可读。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

__all__ = [
    'ArtifactRef',
    'Context',
    'ContextBlock',
    'Role',
    'StopReason',
    'ToolCall',
    'TrustLevel',
]

Role = Literal['system', 'user', 'assistant', 'tool']


class TrustLevel(Enum):
    """内容可信度。UNTRUSTED 必须随衍生内容传递（spec §12.2 第 2 条）。"""

    TRUSTED = 'trusted'
    UNTRUSTED = 'untrusted'


class StopReason(Enum):
    COMPLETED = 'completed'
    ABORTED = 'aborted'
    ERROR = 'error'
    MAX_TURNS = 'max_turns'


@dataclass(frozen=True)
class ArtifactRef:
    """产物引用。version 是不透明字符串，含义由隔离策略决定（spec §7.2）。"""

    id: str
    path: str
    kind: str
    mime: str
    version: str
    producer: str
    is_deliverable: bool = False


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class ContextBlock:
    role: Role
    content: str | list[Any] | None = None
    # 工厂写成带参数的泛型别名而非裸 list / dict：strict 模式下裸工厂推不出元素类型。
    tool_calls: list[ToolCall] = field(default_factory=list[ToolCall])
    tool_call_id: str | None = None
    artifacts: list[ArtifactRef] = field(default_factory=list[ArtifactRef])
    trust: TrustLevel = TrustLevel.TRUSTED
    extra: dict[str, Any] = field(default_factory=dict[str, Any])


class Context:
    def __init__(self) -> None:
        self._blocks: list[ContextBlock] = []
        self.stop_reason: StopReason | None = None

    @property
    def current_traj(self) -> list[ContextBlock]:
        return list(self._blocks)

    def append(self, block: ContextBlock) -> None:
        self._blocks.append(block)
