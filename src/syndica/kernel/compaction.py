"""上下文压缩的内核契约（阶段三 / spec §8.8）。

策略只提议「留哪些、丢哪些、一句说明」。**五条不变量由内核校验、信任由内核置**：策略无论对错都洗
不白轨迹。压缩在 turn 边界作为一次内核步骤跑（窗口之外）；`compact` 是 `async`（摘要型要发一次
调用）。

五条不变量（spec §8.8）：

1. 工具调用与结果**成对**：每个 assistant 工具批次的结果都在、顺序不变；不留半个批次、不留孤儿 tool
   块（provider 会拒掉整条请求）。
2. System Prompt **不参与**：`CompactionRequest.blocks` 是 SP 之外的轨迹，SP 压根不传进来。
3. `protected` 的块**原样存活**：任务消息、当前周期、`env_fact` 块的下标由内核算好传进来
   （Goal / 未满足 Rubric 到 M7 additively 扩）。字节不同即违反。
4. 已注入的环境事实存活：`env_fact` 块在 `protected` 里，随第 3 条守住。
5. 不可信标记随衍生内容存活：压缩后整体溯源不得比压缩前松。内核把 kept 里**非字节相同**的块（摘要 /
   改写出来的）置成「压缩前整体溯源」那档；纯裁剪若丢了唯一的不可信块、又没有衍生块接住，补一个署名
   UNTRUSTED 标记块（硬约束 5）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from syndica.kernel.context import (
    Context,
    ContextBlock,
    TrustLevel,
    provenance_of,
    strictest_trust,
)
from syndica.kernel.errors import ContractViolation
from syndica.kernel.history_blocks import HistoryKind, signed_block

__all__ = [
    'CompactionRequest',
    'CompactionResult',
    'CompactionStrategy',
    'apply_compaction',
]

# 摘要型压缩的一次性调用句柄：内核给，关缓存写入、记 replay（见阶段五 aux_calls 槽）。
Summarize = Callable[[Sequence[ContextBlock]], Awaitable[str]]


@dataclass(frozen=True)
class CompactionRequest:
    """交给策略的一次压缩请求。`blocks` 是 SP 之外的轨迹；`protected` 是其中要原样存活的块下标。"""

    blocks: list[ContextBlock]
    budget_tokens: int
    protected: frozenset[int] = frozenset()
    summarize: Summarize | None = None


@dataclass
class CompactionResult:
    """策略的提议：留哪些、丢哪些、一句说明。顺序即留下序列的顺序。"""

    kept: list[ContextBlock]
    dropped: list[ContextBlock] = field(default_factory=list[ContextBlock])
    note: str = ''


@runtime_checkable
class CompactionStrategy(Protocol):
    async def compact(self, request: CompactionRequest) -> CompactionResult: ...


def _provenance(blocks: Sequence[ContextBlock]) -> TrustLevel:
    """一段块的整体溯源。借 `provenance_of` 这唯一一份定义，不另写一遍（否则两处会静默分叉）。"""
    context = Context()
    for block in blocks:
        context.append(block)
    return provenance_of(context)


def _pairing_ok(blocks: Sequence[ContextBlock]) -> bool:
    index = 0
    size = len(blocks)
    while index < size:
        block = blocks[index]
        if block.role == 'tool':
            # 工具结果只能紧跟在它那批调用后面被消费；走到这里说明它没有前导的调用批次。
            return False
        if block.role == 'assistant' and block.tool_calls:
            expected = [call.id for call in block.tool_calls]
            following = index + 1
            answered: list[str | None] = []
            while following < size and blocks[following].role == 'tool':
                answered.append(blocks[following].tool_call_id)
                following += 1
            if answered != expected:
                return False
            index = following
        else:
            index += 1
    return True


def _refuse(invariant: str, detail: str) -> ContractViolation:
    return ContractViolation(
        expected=f'压缩结果满足不变量「{invariant}」（spec §8.8）',
        received=detail,
        next_action='改策略让它守住这条；内核不接受违反不变量的压缩结果',
    )


def apply_compaction(request: CompactionRequest, result: CompactionResult) -> list[ContextBlock]:
    """校验五条不变量、由内核置信任与补标记，交回最终留下的块。违反即 `ContractViolation` 点名哪条。

    返回的是一份新列表，不改 `request.blocks` 也不改 `result.kept` 里的块对象（信任置于副本上）。
    """
    before = request.blocks
    if not _pairing_ok(result.kept):
        raise _refuse('工具调用与结果成对', '留下的序列里有半个批次或孤儿 tool 块')

    protected_blocks = [before[index] for index in sorted(request.protected)]
    for block in protected_blocks:
        if block not in result.kept:
            raise _refuse('protected 原样存活', '有 protected 块没有字节相同地留在 kept 里')

    carried = _provenance(before)
    finalized: list[ContextBlock] = []
    for block in result.kept:
        # 非字节相同＝摘要 / 改写出来的衍生块：置「压缩前整体溯源」那一档（往严不往松）。字节相同的
        # 原块原样留（含它自己的 trust）。
        if block in before:
            finalized.append(block)
        else:
            finalized.append(replace(block, trust=strictest_trust(block.trust, carried)))

    if carried is TrustLevel.UNTRUSTED and _provenance(finalized) is not TrustLevel.UNTRUSTED:
        # 纯裁剪把唯一的不可信块丢了、又没有衍生块接住：补一个署名 UNTRUSTED 标记块（硬约束 5）。
        finalized.append(
            signed_block(
                HistoryKind.COMPACTION,
                '（压缩）这一段压掉了一些内容，其中有不可信来源；此后基于它的高风险操作仍按不可信处置。',
                key='compaction:untrusted-carried',
                trust=TrustLevel.UNTRUSTED,
            )
        )
    return finalized
