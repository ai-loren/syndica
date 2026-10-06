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
from typing import Protocol, cast, runtime_checkable

from syndica.kernel.clipping import clip_head
from syndica.kernel.context import (
    Context,
    ContextBlock,
    TrustLevel,
    provenance_of,
    strictest_trust,
)
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.history_blocks import HistoryKind, kind_of, signed_block
from syndica.kernel.lexical import overlap, terms

__all__ = [
    'CompactionController',
    'CompactionRequest',
    'CompactionResult',
    'CompactionStrategy',
    'GoalDirectedCompaction',
    'PruningCompaction',
    'RuleBasedCompaction',
    'SummaryCompaction',
    'apply_compaction',
    'traj_tokens',
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
    # 还需要什么：未满足的 Rubric 与 Goal 的原文（目标导向型压缩据它挑要留的旧轮次）。空 = 由策略
    # 自己从轨迹里最新的 Goal / Rubric 块推。
    needed: tuple[str, ...] = ()


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


def _kept_originals(
    before: list[ContextBlock], kept: list[ContextBlock]
) -> tuple[set[int], list[bool]]:
    """把 kept 的每一块对回 before：原块必须是 before 的**严格递增子序列**（按下标，不乱序不重复）；
    对不上 before 任一块的是衍生块。交回（被对上的 before 下标集合，kept 里逐块「是否衍生」）。

    乱序 / 重复会在这里报：按值的 `in` 对它们无感——两个相等的受保护块用一块就「满足」，摘要把任务
    挪到当前轮之后也照过（评审 M3）。

    从后往前对、每块取能取的最晚那一个：两条相等的块（同样一句「继续」）里留下的那条对的是后一条——
    后一条正是受保护的当前输入，从前往前对会把它判成丢了。
    """
    cursor = len(before)
    matched: set[int] = set()
    derived: list[bool] = []
    for block in reversed(kept):
        index = next((i for i in range(cursor - 1, -1, -1) if before[i] == block), None)
        if index is None:
            # cursor 之前找不到：要么是真衍生块，要么乱序 / 重复的原块（它等于 cursor 之后的某块）。
            if any(before[i] == block for i in range(len(before))):
                raise _refuse('kept 里的原块按 before 的下标严格递增', '留下的原块乱序或重复')
            derived.append(True)
        else:
            matched.add(index)
            cursor = index
            derived.append(False)
    derived.reverse()
    return matched, derived


def apply_compaction(
    request: CompactionRequest,
    result: CompactionResult,
    *,
    before: list[ContextBlock] | None = None,
) -> list[ContextBlock]:
    """校验五条不变量、由内核置信任与补标记，交回最终留下的块。违反即 `ContractViolation` 点名哪条。

    `before` 是**权威的压缩前快照**（内核深拷的那份）：信任、protected、原块判定都对它算，而不是对
    `request.blocks`——策略可能就地改了后者来洗白（评审 H1）。缺省退回 `request.blocks`，供不改输入的
    策略单测直接用。返回新列表，不改入参里的块对象（信任置于副本上）。
    """
    authoritative = request.blocks if before is None else before
    if not _pairing_ok(result.kept):
        raise _refuse('工具调用与结果成对', '留下的序列里有半个批次或孤儿 tool 块')

    matched, derived = _kept_originals(authoritative, result.kept)
    for index in sorted(request.protected):
        if index not in matched:
            raise _refuse('protected 原样存活', f'protected 块（下标 {index}）没按下标留在 kept 里')

    carried = _provenance(authoritative)
    finalized: list[ContextBlock] = []
    for block, is_derived in zip(result.kept, derived, strict=True):
        if not is_derived:
            # 字节相同、且按下标递增对上的原块：原样留（含它自己的 trust）。
            finalized.append(block)
            continue
        # 衍生块（摘要 / 改写）：不得是 system（会被某些 adapter 并进 SP 位、破坏前缀纪律，M3）。
        if block.role == 'system':
            raise _refuse('不得衍生 system 块', '压缩产出里有 role=system 的衍生块')
        # 置「压缩前整体溯源」那一档（往严不往松）。
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


def _text_size(value: object) -> int:
    """一段内容的 token 估算（保守）：ASCII 约 4 字符 1 token，非 ASCII（CJK 等）按每码点 1 token 算
    ——`len // 4` 对中文低估 3–6 倍，而本项目的提示 / 标记大量是中文（评审 H4）。列表 / 字典递归进去
    数文本叶子，不数它的 repr。"""
    if isinstance(value, str):
        ascii_chars = sum(1 for char in value if ord(char) < 128)
        return ascii_chars // 4 + (len(value) - ascii_chars)
    if isinstance(value, list):
        return sum(_text_size(item) for item in cast('list[object]', value))
    if isinstance(value, dict):
        return sum(_text_size(item) for item in cast('dict[object, object]', value).values())
    return 0


def _estimate_tokens(block: ContextBlock) -> int:
    """一块的确定性 token 估算：只用于压缩的相对取舍，用确定值是为了压缩点可回放（spec §11.3），
    真 tokenizer 既慢又随 provider 变。保守估：宁可早压也不要晚到超限（评审 H4）。"""
    size = _text_size(block.content)
    for call in block.tool_calls:
        size += _text_size(call.name) + _text_size(call.arguments)
    return size + 1  # 每块留一点结构开销


def _turn_bounds(blocks: Sequence[ContextBlock]) -> list[tuple[int, int]]:
    """把轨迹切成「轮」：每个 assistant 回复起一段（它的 tool 结果归这一段），每条真实 user 消息也起
    一段。署名块（env_fact / memory / 恢复提示 / 压缩标记）不另起段。这样单条 query 里的多轮工具调用
    也分得开——否则整段只有一条 user 消息、算作一轮，规则型压缩压不动。"""
    if not blocks:
        return []
    starts = sorted(
        {0}
        | {
            index
            for index, block in enumerate(blocks)
            if block.role == 'assistant' or (block.role == 'user' and kind_of(block) is None)
        }
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
    return replace(
        block, content=f'{clip_head(content, max_chars)}…（压缩截断，原长 {len(content)} 字）'
    )


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


def _validate_strategy_args(name: str, min_keep_turns: object, max_output_chars: object) -> None:
    for name_, value in (
        ('min_keep_turns', min_keep_turns),
        ('max_output_chars', max_output_chars),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(
                expected=f'{name} 的 {name_} 是 >= 1 的整数',
                received=f'{name_}={value!r}',
                next_action=f'给 {name_} 一个正整数；最近一轮都不留，压缩之后就没有当前输入',
            )


def _floor_start(blocks: Sequence[ContextBlock], min_keep_turns: int) -> int:
    exchanges = _turn_bounds(blocks)
    if not exchanges:
        return 0
    return exchanges[max(0, len(exchanges) - min_keep_turns)][0]


class RuleBasedCompaction:
    """规则型（零 LLM）：留最近 N 轮 + protected，预算还够就把更早的轮次按新→旧加回来；只有连最近
    N 轮都塞不下时才截断其中的超长工具 / assistant 输出（spec §8.8）。"""

    def __init__(self, *, min_keep_turns: int = 2, max_output_chars: int = 4000) -> None:
        _validate_strategy_args(type(self).__name__, min_keep_turns, max_output_chars)
        self._min_keep_turns = min_keep_turns
        self._max_output_chars = max_output_chars

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        blocks = request.blocks
        protected = request.protected
        floor_from = _floor_start(blocks, self._min_keep_turns)
        keep: set[int] = set(protected) | set(range(floor_from, len(blocks)))
        for start, end in reversed(_turn_bounds(blocks)):
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


class GoalDirectedCompaction:
    """目标导向型（零 LLM 的近似，spec §8.8 第四种）：「最近」与「还需要」是两件事——五十轮前定下的一
    条约束可能仍然关键，刚才三轮的调试输出完全可以丢。留 protected 与最近 N 轮，再把更早的轮次按
    与**还需要的东西**（未满足的 Rubric、Goal）的词面相关度从高到低加回来，同分的新者优先，加到预算
    为止；不相关的轮次最先被丢。没给 `needed` 时按轨迹里最新的 Goal / Rubric 块（没有就按第一条任务
    消息）推，相当于把全部生效的 Rubric 都当成未满足——宁可多留。"""

    def __init__(self, *, min_keep_turns: int = 2, max_output_chars: int = 4000) -> None:
        _validate_strategy_args(type(self).__name__, min_keep_turns, max_output_chars)
        self._min_keep_turns = min_keep_turns
        self._max_output_chars = max_output_chars

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        blocks = request.blocks
        protected = request.protected
        vocabulary: set[str] = set()
        for text in _needed_texts(request):
            vocabulary |= terms(text)
        floor_from = _floor_start(blocks, self._min_keep_turns)
        keep: set[int] = set(protected) | set(range(floor_from, len(blocks)))
        older = [bounds for bounds in _turn_bounds(blocks) if bounds[0] < floor_from]
        scored = [(overlap(_turn_text(blocks, *bounds), vocabulary), bounds) for bounds in older]
        relevant = 0
        for score, (start, end) in sorted(scored, key=lambda item: (-item[0], -item[1][0])):
            trial = keep | set(range(start, end))
            if _projected_tokens(blocks, trial, protected, 0) <= request.budget_tokens:
                keep = trial
                relevant += score > 0
        over = _projected_tokens(blocks, keep, protected, 0) > request.budget_tokens
        cap = self._max_output_chars if over else 0
        kept, dropped = _materialize(blocks, keep, protected, cap)
        note = (
            f'目标导向型压缩：留 {len(kept)} 块（其中 {relevant} 轮旧轮次与未满足的要求相关）、'
            f'丢 {len(dropped)} 块'
        )
        return CompactionResult(kept=kept, dropped=dropped, note=note)


def _needed_texts(request: CompactionRequest) -> tuple[str, ...]:
    if request.needed:
        return request.needed
    latest: dict[HistoryKind, str] = {}
    for block in request.blocks:
        kind = kind_of(block)
        if kind in (HistoryKind.GOAL, HistoryKind.RUBRIC):
            latest[kind] = str(block.content or '')
    if latest:
        return tuple(latest.values())
    first = next((b for b in request.blocks if b.role == 'user' and kind_of(b) is None), None)
    return () if first is None else (str(first.content or ''),)


def _turn_text(blocks: Sequence[ContextBlock], start: int, end: int) -> str:
    pieces: list[str] = []
    for block in blocks[start:end]:
        pieces.append(str(block.content or ''))
        pieces.extend(f'{call.name} {call.arguments}' for call in block.tool_calls)
    return '\n'.join(pieces)


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

    def __init__(
        self,
        *,
        min_keep_turns: int = 2,
        max_summary_input_chars: int = 2000,
        max_summary_input_blocks: int = 200,
    ) -> None:
        self._min_keep_turns = min_keep_turns
        self._max_summary_input_chars = max_summary_input_chars
        self._max_summary_input_blocks = max_summary_input_blocks

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
        # 已经是一块压缩摘要、没有新东西可并时，不要再摘一遍自己——否则超限兜底下每轮重摘、无穷循环
        # （评审 H2）。
        if not to_summarize or (
            len(to_summarize) == 1 and kind_of(blocks[to_summarize[0]]) is HistoryKind.COMPACTION
        ):
            return CompactionResult(
                kept=list(blocks), dropped=[], note='摘要型压缩：没有新的早期内容可摘'
            )
        # 套娃预压缩：摘要器面对的就是最长那份，先按**总量**裁到上限（只摘最早、最该被摘的那些块），
        # 每块再按长度截断，别让摘要器自己超限（评审 M5）。超出上限的早期块这轮不摘、原样留，不丢。
        capped = set(to_summarize[: self._max_summary_input_blocks])
        pre = [_truncated(blocks[index], self._max_summary_input_chars) for index in sorted(capped)]
        summary_text = await request.summarize(pre)
        summary = signed_block(
            HistoryKind.COMPACTION,
            f'（压缩摘要，覆盖此前 {len(pre)} 块）{summary_text}',
            key='compaction:summary',
        )
        # 遍历全部块：被摘的那些整体换成一块摘要（放在它们第一块的位置），其余原样留——顺序不乱、
        # protected 与未摘的早期块都在位（评审 M3 的子序列校验）。
        kept: list[ContextBlock] = []
        inserted = False
        for index, block in enumerate(blocks):
            if index in capped:
                if not inserted:
                    kept.append(summary)
                    inserted = True
                continue
            kept.append(block)
        dropped = [blocks[index] for index in sorted(capped)]
        return CompactionResult(
            kept=kept, dropped=dropped, note=f'摘要型压缩：{len(dropped)} 块早期内容摘成 1 块'
        )


# ---- 触发策略（spec §8.8：三条触发路径）----


def traj_tokens(blocks: Sequence[ContextBlock]) -> int:
    """整段轨迹的确定性 token 估算——阈值触发与预算都用它，同一把尺子。"""
    return sum(_estimate_tokens(block) for block in blocks)


def _protected_indices(blocks: Sequence[ContextBlock]) -> frozenset[int]:
    """压缩必须原样留的块（spec §8.8 不变量 3/4）：

    - 已注入的环境事实（`ENV_FACT`）与恢复提示（`RESUME_NOTE`，它携带整份溯源）；
    - **最新**的那个 Goal 块（`GOAL`，唯一权威）与**最新**的那个 Rubric 块（`RUBRIC`，生效集合）：
      Goal 改了、Rubric 集合变了都会追加替代块，被替代的旧块不再保护——留着它们模型会同时看见两份互相
      矛盾的要求（M7 评审 M6）；
    - 最后一条真实 user 消息（当前这一轮的输入）；
    - 第一条真实 user 消息（任务 / Goal 代理）——**只在没有 Goal 块时**兜底；有 Goal 块时此代理退役，
      否则 Goal 块会把真正的任务消息挤出保护集合（M7 评审 B2）。

    刻意不硬保整条当前轮：超限兜底时要能压掉它，否则兜底对「当前轮自己撑爆」无能为力。
    """
    protected: set[int] = {
        index
        for index, block in enumerate(blocks)
        if kind_of(block) in (HistoryKind.ENV_FACT, HistoryKind.RESUME_NOTE)
    }
    for kind in (HistoryKind.GOAL, HistoryKind.RUBRIC, HistoryKind.BUDGET):
        latest = [index for index, block in enumerate(blocks) if kind_of(block) is kind]
        if latest:
            protected.add(latest[-1])
    has_goal = any(kind_of(block) is HistoryKind.GOAL for block in blocks)
    user_turns = [
        index
        for index, block in enumerate(blocks)
        if block.role == 'user' and kind_of(block) is None
    ]
    if user_turns:
        protected.add(user_turns[-1])
        if not has_goal:
            protected.add(user_turns[0])
    return frozenset(protected)


@dataclass
class CompactionController:
    """一个 Agent 的压缩策略与触发配置。纯配置 + 迟滞 / 退避判定，不碰 Agent——改写由内核在 turn
    边界调（spec §8.8：改写走内核接口）。

    迟滞：`target_tokens < threshold_tokens`，压到 target 以下，下一轮就不会立刻再触发。
    退避：一次压缩压不到阈值以下时记下当时的占用，占用不再增长就不反复空压（P18）。
    """

    strategy: CompactionStrategy
    threshold_tokens: int
    target_tokens: int
    summarize: Summarize | None = None
    on_compacted: Callable[[], None] | None = None
    needed: Callable[[], Sequence[str]] | None = None
    _requested: bool = field(default=False, init=False)
    _stuck_at: int | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.threshold_tokens < 1 or self.target_tokens < 1:
            raise ConfigError(
                expected='compaction 的 threshold_tokens 与 target_tokens 都是正整数',
                received=f'threshold={self.threshold_tokens!r} target={self.target_tokens!r}',
                next_action='给正的 token 预算；百分比在 Module 里换算成 token 再传进来',
            )
        if self.target_tokens >= self.threshold_tokens:
            raise ConfigError(
                expected='compaction 的 target_tokens 严格小于 threshold_tokens（迟滞才成立）',
                received=f'target={self.target_tokens} >= threshold={self.threshold_tokens}',
                next_action='把目标占用调到阈值以下；否则压完仍在阈值上、每轮都重触发',
            )

    def request(self) -> None:
        """Agent 主动要求下一次 turn 边界压缩（`Compact` 工具走这条，只置位）。"""
        self._requested = True

    def should_compact(self, tokens: int) -> bool:
        if self._requested:
            return True
        if tokens <= self.threshold_tokens:
            return False
        if self._stuck_at is not None and tokens <= self._stuck_at + self._rearm_margin():
            # 压过一次但压不到阈值下：只在占用又长出「一次压缩的预期收益」之后才重试，否则每轮都
            # 重写前缀、每轮都失效缓存（spec §8.8 要防的「此后每轮持续漂移」，评审 M1）。
            return False
        return True

    def _rearm_margin(self) -> int:
        return self.threshold_tokens - self.target_tokens

    def build_request(
        self,
        blocks: Sequence[ContextBlock],
        *,
        budget_tokens: int | None = None,
        extra_protected: frozenset[int] = frozenset(),
    ) -> CompactionRequest:
        return CompactionRequest(
            blocks=list(blocks),
            budget_tokens=self.target_tokens if budget_tokens is None else budget_tokens,
            protected=_protected_indices(blocks) | extra_protected,
            summarize=self.summarize,
            needed=tuple(self.needed()) if self.needed is not None else (),
        )

    def overflow_budget(self, tokens_before: int) -> int:
        """超限兜底的预算：超限说明估算失准、真窗口 ≤ 此刻占用，所以按**此刻占用**折半来逼它真的缩
        （不是沿用那个刚失败的 target，评审 H4）。连续超限时占用一路降、预算一路降，直到压不动。"""
        return max(1, min(self.target_tokens, tokens_before // 2))

    def settle(self, *, tokens_now: int, changed: bool) -> None:
        """一次 turn 边界压缩之后更新迟滞 / 退避状态。`changed=False`（策略压不动）也要记，
        免得每一轮都重试一次空压。"""
        self._requested = False
        self._stuck_at = None if changed and tokens_now <= self.threshold_tokens else tokens_now
