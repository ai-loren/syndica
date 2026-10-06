"""上下文数据类型。

Context 只承载产出与终态：轨迹块，以及一个终止原因（含 ABORTED）。
**运行中的控制状态不在这里** —— 当前状态、是否被挂起、中止是否已置位都归
Registry（spec §4.3）。理由是 Agent 阻塞或崩溃时无法自报状态，所以状态必须
在它之外可读。
"""

from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from syndica.kernel.errors import ContractViolation

if TYPE_CHECKING:
    from syndica.kernel.acceptance import Verdict

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
    BUDGET = 'budget'


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
        # 本周期的验收裁决（§7.5；COMPLETION_PROPOSED 给出、内核记下）。每个周期开始清空：上一周期的
        # PASS 不能冒充这一周期的结论（评审 H2）。随 `_finish` 的快照一起被 Delegate 读到。
        self.verdict: Verdict | None = None

    @property
    def current_traj(self) -> list[ContextBlock]:
        return list(self._blocks)

    def append(self, block: ContextBlock) -> None:
        """追加一块。轨迹只由内核自己的循环写：在这份轨迹的窗口里（这个 Agent 的切点分发、工具段，
        以及从它们里面起的任务）报 `ContractViolation`，见 `hooks.junctions` 模块文档。"""
        window = next((name for owner, name in reversed(_WINDOWS.get()) if owner is self), None)
        if window is not None:
            raise ContractViolation(
                expected=(
                    '轨迹只由内核写，hook、工具与它们起的任务不往里追加：几方并发时谁先写取决于调度，'
                    '写在一次调用与它的结果之间提供方会拒掉下一次请求'
                ),
                received=f'{window}期间收到一次追加（role={block.role}）',
                next_action=(
                    "要改一次调用的产出，在 POST_TOOL_USE 交回 modified['result']（或由工具"
                    '自己交回 ToolResult）；要告诉模型一件事，用 Runtime.send 发一条消息，'
                    'Agent 下一轮按顺序注入。在这次分发里、以及它起的任务里，怎么等都写不进'
                ),
            )
        self._blocks.append(block)

    def _replace_traj(self, blocks: Sequence[ContextBlock]) -> None:
        """整段重写轨迹（压缩走的内核接口，spec §8.8）。**不是 append**：压缩改历史，不逐块追加
        （那会插进一次调用与它结果之间、被 provider 拒），也不让 hook 就地改。只内核压缩步骤调。"""
        self._blocks[:] = list(blocks)


# 此刻在跑的这次工具调用的溯源结论（执行器在 `tool.execute` 期间设）。工具写下的文件版本记成它：
# 读过不可信内容之后写的文件，此后被读到时仍是不可信的（spec §12.2 第 2 条）。不在工具调用里是
# None。
_CALL_TRUST: ContextVar[TrustLevel | None] = ContextVar('syndica_call_trust', default=None)


def call_trust() -> TrustLevel | None:
    return _CALL_TRUST.get()


# 此刻所在的窗口：（哪份轨迹，哪个窗口）。窗口里跑的代码与它起的任务（任务建在窗口里，继承这份值）
# 不许往那份轨迹里追加。放在 contextvar 里而不是 Context 上：别的任务开的窗口不该冻结 Agent 自己的
# 循环，轨迹的拷贝也不该带着一个早已结束的窗口。
_WINDOWS: ContextVar[tuple[tuple[Context, str], ...]] = ContextVar(
    'syndica_trajectory_windows', default=()
)


@contextmanager
def _frozen(ctx: Context, window: str) -> Generator[None, None, None]:  # pyright: ignore[reportUnusedFunction]
    """`window` 期间（在这段代码与它起的任务里）不许往 `ctx` 追加。`window` 写成接上「期间」读得通
    的一句话。"""
    token = _WINDOWS.set((*_WINDOWS.get(), (ctx, window)))
    try:
        yield
    finally:
        try:
            _WINDOWS.reset(token)
        except ValueError:
            # 协程没被取消就被 GC 关掉时，finally 跑在另一份上下文里；那份上下文随即丢弃。
            pass


def _no_windows() -> None:  # pyright: ignore[reportUnusedFunction]
    """内核自己的循环在任务开头调：它不继承建它的那段代码所在的窗口（一个在自己的观察者里被叫醒的
    Agent，否则从此写不进自己的轨迹）。"""
    _WINDOWS.set(())


def _trailing_unpaired_batch(  # pyright: ignore[reportUnusedFunction]
    blocks: Sequence[ContextBlock],
) -> tuple[int, tuple[ToolCall, ...]] | None:
    """返回轨迹末尾那个未配对批次的位置与未配对调用。"""
    index = len(blocks) - 1
    while index >= 0 and blocks[index].role == 'tool':
        index -= 1
    if index < 0 or blocks[index].role != 'assistant' or not blocks[index].tool_calls:
        return None
    answered = {block.tool_call_id for block in blocks[index + 1 :]}
    unpaired = tuple(call for call in blocks[index].tool_calls if call.id not in answered)
    if not unpaired:
        return None
    return index, unpaired


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
