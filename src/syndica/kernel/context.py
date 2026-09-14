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
    'provenance_of',
    'strictest_trust',
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


def provenance_of(ctx: Context) -> TrustLevel:
    """这份轨迹能不能溯源到不可信内容——spec §12.2 第 3 条那道强制升级的判据。

    当前形态是「轨迹里出现过 `trust=UNTRUSTED` 的 block」，它是**上界**而不是因果链：只会多判，
    不会少判，而少判才是那道升级要防的方向。完整的溯源要接产物溯源（spec §7.3）与 trace 因果链
    （§11.1）。

    **永不交 `None`。** 看完整份轨迹之后报「没溯源过」就是把做过的检查说成没做过，那道升级于是
    恒不命中而调用方以为有它。

    定义只有这一份：闸（这次调用该不该升级处置）与编排工具（派出去的活该不该带着标记）问的是
    同一个问题，两份实现会在其中一处收紧时静默分叉，而分叉出来的那一侧就是洗白路径。
    """
    if any(block.trust is TrustLevel.UNTRUSTED for block in ctx.current_traj):
        return TrustLevel.UNTRUSTED
    return TrustLevel.TRUSTED


def strictest_trust(first: TrustLevel, second: TrustLevel) -> TrustLevel:
    """两个信任级别里更严的那个。

    合并两份溯源结论时**只能往严的方向**走：标记只会因为没查到而丢，不会因为查到了而多出来，
    所以任何一侧报不可信就是不可信。反过来取松的那个等于让其中一次检查作废。
    """
    if TrustLevel.UNTRUSTED in (first, second):
        return TrustLevel.UNTRUSTED
    return TrustLevel.TRUSTED
