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
from syndica.kernel.history_blocks import HistoryKind, kind_of, signed_block

__all__ = [
    'CompactionRequest',
    'CompactionResult',
    'CompactionStrategy',
    'PruningCompaction',
    'RuleBasedCompaction',
    'SummaryCompaction',
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


# ---- 基线策略（spec §8.8：四种基线，目标导向型依赖 M7 的 Rubric，推迟）----
#
# 策略只提议「留哪些、丢哪些」。它们必须自己守住配对与 protected（否则 `apply_compaction` 退回），
# 信任由内核在 `apply_compaction` 里置——策略无须也无权碰 trust。


def _estimate_tokens(block: ContextBlock) -> int:
    """一块的粗略 token 估算，确定性：`len(序列化) // 4`。只用于压缩的相对取舍，不求精确；用确定值
    是因为压缩点要能回放（spec §11.3），真 tokenizer 既慢又随 provider 变。"""
    size = 0
    content = block.content
    if isinstance(content, str):
        size += len(content)
    elif content is not None:
        size += len(str(content))
    for call in block.tool_calls:
        size += len(call.name) + len(call.arguments)
    return (size + 3) // 4


def _exchange_bounds(blocks: Sequence[ContextBlock]) -> list[tuple[int, int]]:
    """把轨迹切成「交流」：每个真实 user 消息（非内核署名历史块）起一段，到下一个这样的消息前。
    署名块（env_fact / memory / 恢复提示 / 压缩标记）不另起段，归前一段——它们不是对话轮次。"""
    if not blocks:
        return []
    starts = sorted(
        {0} | {i for i, b in enumerate(blocks) if b.role == 'user' and kind_of(b) is None}
    )
    return [
        (start, starts[index + 1] if index + 1 < len(starts) else len(blocks))
        for index, start in enumerate(starts)
    ]


def _tool_batches(blocks: Sequence[ContextBlock]) -> list[tuple[int, ...]]:
    """每个 assistant 工具调用批次连同它的结果块的下标范围。剪枝按整批丢，不拆配对。"""
    batches: list[tuple[int, ...]] = []
    index = 0
    size = len(blocks)
    while index < size:
        block = blocks[index]
        if block.role == 'assistant' and block.tool_calls:
            following = index + 1
            while following < size and blocks[following].role == 'tool':
                following += 1
            batches.append(tuple(range(index, following)))
            index = following
        else:
            index += 1
    return batches


def _truncated(block: ContextBlock, max_chars: int) -> ContextBlock:
    """超长文本块截到 `max_chars`、留一句说明。非字符串内容不动。截出来的是衍生块，
    `apply_compaction` 会给它置「压缩前整体溯源」那一档信任。"""
    content = block.content
    if max_chars <= 0 or not isinstance(content, str) or len(content) <= max_chars:
        return block
    return replace(block, content=f'{content[:max_chars]}…（压缩截断，原长 {len(content)} 字）')


def _projected_tokens(
    blocks: Sequence[ContextBlock],
    keep: set[int],
    protected: frozenset[int],
    max_output_chars: int,
) -> int:
    total = 0
    for index in keep:
        block = blocks[index]
        if index not in protected and block.role in ('tool', 'assistant'):
            block = _truncated(block, max_output_chars)
        total += _estimate_tokens(block)
    return total


def _materialize(
    blocks: Sequence[ContextBlock],
    keep: set[int],
    protected: frozenset[int],
    max_output_chars: int,
) -> tuple[list[ContextBlock], list[ContextBlock]]:
    kept: list[ContextBlock] = []
    dropped: list[ContextBlock] = []
    for index, block in enumerate(blocks):
        if index not in keep:
            dropped.append(block)
        elif index not in protected and block.role in ('tool', 'assistant'):
            kept.append(_truncated(block, max_output_chars))
        else:
            kept.append(block)
    return kept, dropped


def _floor_start(blocks: Sequence[ContextBlock], min_keep_turns: int) -> int:
    exchanges = _exchange_bounds(blocks)
    if not exchanges:
        return 0
    return exchanges[max(0, len(exchanges) - min_keep_turns)][0]


class RuleBasedCompaction:
    """规则型（零 LLM）：留最近 N 轮 + protected，预算还够就把更早的轮次按新→旧加回来；只有连最近
    N 轮都塞不下时才截断其中的超长工具 / assistant 输出（spec §8.8）。"""

    def __init__(self, *, min_keep_turns: int = 2, max_output_chars: int = 4000) -> None:
        self._min_keep_turns = min_keep_turns
        self._max_output_chars = max_output_chars

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        blocks = request.blocks
        protected = request.protected
        floor_from = _floor_start(blocks, self._min_keep_turns)
        keep: set[int] = set(protected) | set(range(floor_from, len(blocks)))
        for start, end in reversed(_exchange_bounds(blocks)):
            if start >= floor_from:
                continue
            trial = keep | set(range(start, end))
            if _projected_tokens(blocks, trial, protected, 0) <= request.budget_tokens:
                keep = trial
            else:
                break
        over = _projected_tokens(blocks, keep, protected, 0) > request.budget_tokens
        cap = self._max_output_chars if over else 0
        kept, dropped = _materialize(blocks, keep, protected, cap)
        note = f'规则型压缩：留 {len(kept)} 块、丢 {len(dropped)} 块'
        if over:
            note += '（最近轮次仍超预算，截断其中的超长输出）'
        return CompactionResult(kept=kept, dropped=dropped, note=note)


class PruningCompaction:
    """剪枝型（低成本）：按已完成工具批次丢执行细节——整批丢（assistant 调用 + 结果），保住配对与
    文本推理；protected 的批次不丢。丢到预算内为止（spec §8.8）。"""

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        blocks = request.blocks
        protected = request.protected
        keep: set[int] = set(range(len(blocks)))
        for batch in _tool_batches(blocks):
            if _projected_tokens(blocks, keep, protected, 0) <= request.budget_tokens:
                break
            if any(index in protected for index in batch):
                continue
            keep -= set(batch)
        kept, dropped = _materialize(blocks, keep, protected, 0)
        return CompactionResult(
            kept=kept,
            dropped=dropped,
            note=f'剪枝型压缩：丢 {len(dropped)} 块已完成工具批次的执行细节',
        )


class SummaryCompaction:
    """摘要型（高成本）：把最近 N 轮之前、非 protected 的内容用一次性调用摘成一块。摘要器面对的就是
    最长那份，所以先对每块做廉价的规则型预截断再喂给它（spec §8.8 的套娃问题）。"""

    def __init__(self, *, min_keep_turns: int = 2, max_summary_input_chars: int = 2000) -> None:
        self._min_keep_turns = min_keep_turns
        self._max_summary_input_chars = max_summary_input_chars

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        if request.summarize is None:
            raise ContractViolation(
                expected='摘要型压缩拿到一个 summarize 调用句柄',
                received='request.summarize 是 None',
                next_action='给压缩步骤配一个关了缓存写入的一次性调用，或改用规则型 / 剪枝型策略',
            )
        blocks = request.blocks
        protected = request.protected
        floor_from = _floor_start(blocks, self._min_keep_turns)
        to_summarize = [index for index in range(floor_from) if index not in protected]
        if not to_summarize:
            return CompactionResult(
                kept=list(blocks), dropped=[], note='摘要型压缩：无可摘要的早期内容'
            )
        pre = [_truncated(blocks[index], self._max_summary_input_chars) for index in to_summarize]
        summary_text = await request.summarize(pre)
        summary = signed_block(
            HistoryKind.COMPACTION,
            f'（压缩摘要，覆盖此前 {len(pre)} 块）{summary_text}',
            key='compaction:summary',
        )
        kept = [blocks[index] for index in sorted(protected) if index < floor_from]
        kept.append(summary)
        kept.extend(blocks[floor_from:])
        dropped = [blocks[index] for index in to_summarize]
        return CompactionResult(
            kept=kept, dropped=dropped, note=f'摘要型压缩：{len(dropped)} 块早期内容摘成 1 块'
        )
