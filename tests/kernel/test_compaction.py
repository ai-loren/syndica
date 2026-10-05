"""上下文压缩的内核契约（阶段三 / §8.8）：五条不变量由内核强制、信任由内核置，策略洗不白。"""

from __future__ import annotations

import pytest

from syndica.kernel.compaction import (
    CompactionRequest,
    CompactionResult,
    apply_compaction,
)
from syndica.kernel.context import ContextBlock, ToolCall, TrustLevel
from syndica.kernel.errors import ContractViolation
from syndica.kernel.history_blocks import HistoryKind, kind_of


def _call_batch(call_id: str, output: str) -> list[ContextBlock]:
    return [
        ContextBlock(
            role='assistant', tool_calls=[ToolCall(id=call_id, name='Bash', arguments='{}')]
        ),
        ContextBlock(role='tool', content=output, tool_call_id=call_id),
    ]


def test_keeps_an_intact_paired_prefix() -> None:
    blocks = [
        ContextBlock(role='user', content='task'),
        *_call_batch('c1', 'old output'),
        ContextBlock(role='assistant', content='answer'),
    ]
    request = CompactionRequest(blocks=blocks, budget_tokens=100)
    result = CompactionResult(kept=list(blocks), dropped=[])
    assert apply_compaction(request, result) == blocks


def test_dropping_half_a_tool_batch_is_refused() -> None:
    blocks = [ContextBlock(role='user', content='task'), *_call_batch('c1', 'out')]
    request = CompactionRequest(blocks=blocks, budget_tokens=100)
    # 留下了调用、丢了它的结果：留下序列有半个批次，provider 会拒。
    result = CompactionResult(kept=[blocks[0], blocks[1]], dropped=[blocks[2]])
    with pytest.raises(ContractViolation, match='成对'):
        apply_compaction(request, result)


def test_an_orphan_tool_result_is_refused() -> None:
    blocks = [ContextBlock(role='user', content='task'), *_call_batch('c1', 'out')]
    request = CompactionRequest(blocks=blocks, budget_tokens=100)
    # 只留结果、丢了调用：孤儿 tool 块。
    result = CompactionResult(kept=[blocks[0], blocks[2]], dropped=[blocks[1]])
    with pytest.raises(ContractViolation, match='成对'):
        apply_compaction(request, result)


def test_dropping_a_protected_block_is_refused() -> None:
    blocks = [
        ContextBlock(role='user', content='the task'),
        ContextBlock(role='assistant', content='chatter'),
    ]
    request = CompactionRequest(blocks=blocks, budget_tokens=100, protected=frozenset({0}))
    result = CompactionResult(kept=[blocks[1]], dropped=[blocks[0]])
    with pytest.raises(ContractViolation, match='protected'):
        apply_compaction(request, result)


def test_a_summary_block_inherits_the_strictest_provenance() -> None:
    # 压缩前有不可信块；摘要块是衍生的（非字节相同），内核把它置成不可信——哪怕它只概括了可信部分。
    blocks = [
        ContextBlock(role='user', content='task', trust=TrustLevel.TRUSTED),
        ContextBlock(role='user', content='web said X', trust=TrustLevel.UNTRUSTED),
    ]
    request = CompactionRequest(blocks=blocks, budget_tokens=100)
    summary = ContextBlock(
        role='assistant', content='summary of the above', trust=TrustLevel.TRUSTED
    )
    result = CompactionResult(kept=[blocks[0], summary], dropped=[blocks[1]])
    final = apply_compaction(request, result)
    assert final[0] is blocks[0]  # 字节相同的原块原样留
    assert final[1].content == 'summary of the above'
    assert final[1].trust is TrustLevel.UNTRUSTED  # 衍生块被置严


def test_pruning_away_the_only_untrusted_block_leaves_a_signed_marker() -> None:
    blocks = [
        ContextBlock(role='user', content='task', trust=TrustLevel.TRUSTED),
        ContextBlock(role='user', content='web said X', trust=TrustLevel.UNTRUSTED),
        ContextBlock(role='assistant', content='done', trust=TrustLevel.TRUSTED),
    ]
    request = CompactionRequest(blocks=blocks, budget_tokens=100)
    # 纯裁剪：丢掉唯一的不可信块，kept 全是可信的原块——内核补一个署名 UNTRUSTED 标记，溯源不被洗白。
    result = CompactionResult(kept=[blocks[0], blocks[2]], dropped=[blocks[1]])
    final = apply_compaction(request, result)
    assert any(block.trust is TrustLevel.UNTRUSTED for block in final)
    marker = final[-1]
    assert kind_of(marker) is HistoryKind.COMPACTION
    assert marker.trust is TrustLevel.UNTRUSTED


def test_all_trusted_pruning_adds_no_marker() -> None:
    blocks = [
        ContextBlock(role='user', content='task'),
        ContextBlock(role='assistant', content='chatter'),
        ContextBlock(role='assistant', content='done'),
    ]
    request = CompactionRequest(blocks=blocks, budget_tokens=100)
    result = CompactionResult(kept=[blocks[0], blocks[2]], dropped=[blocks[1]])
    final = apply_compaction(request, result)
    assert final == [blocks[0], blocks[2]]
    assert all(kind_of(block) is None for block in final)


def test_a_kept_original_block_is_not_over_tainted_when_nothing_was_untrusted() -> None:
    blocks = [
        ContextBlock(role='user', content='task', trust=TrustLevel.TRUSTED),
        ContextBlock(role='assistant', content='done', trust=TrustLevel.TRUSTED),
    ]
    request = CompactionRequest(blocks=blocks, budget_tokens=100)
    result = CompactionResult(kept=list(blocks), dropped=[])
    final = apply_compaction(request, result)
    assert all(block.trust is TrustLevel.TRUSTED for block in final)
