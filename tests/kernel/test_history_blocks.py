from __future__ import annotations

from syndica.kernel.context import ContextBlock, TrustLevel
from syndica.kernel.history_blocks import (
    IDEMPOTENCY_KEY,
    KIND_KEY,
    HistoryKind,
    has_block,
    kind_of,
    signed_block,
)


def test_history_kind_name_to_value_map_is_exactly_this() -> None:
    # 整张成员→取值表：一句同时钉住互异性、成员完备与落盘取值。取值撞车会让一个成员静默变成
    # 另一个的别名（枚举迭代跳过别名，字典少一项即失败），而取值逐字进 extra、跨存盘存活。
    assert {kind.name: kind.value for kind in HistoryKind} == {
        'ENV_FACT': 'env_fact',
        'RESUME_NOTE': 'resume_note',
        'MEMORY': 'memory',
        'COMPACTION': 'compaction',
        'GOAL': 'goal',
        'RUBRIC': 'rubric',
        'VERDICT': 'verdict',
        'BUDGET': 'budget',
        'PROGRESS': 'progress',
        'STRATEGY': 'strategy',
        'BACKGROUND': 'background',
    }


def test_signed_block_carries_kind_and_key_in_extra() -> None:
    block = signed_block(HistoryKind.ENV_FACT, 'you are in a copy', key='env:isolation')
    assert block.extra[KIND_KEY] == 'env_fact'
    assert block.extra[IDEMPOTENCY_KEY] == 'env:isolation'
    assert block.content == 'you are in a copy'
    assert block.role == 'user'
    assert block.trust is TrustLevel.TRUSTED


def test_signed_block_honours_role_and_trust_overrides() -> None:
    # 非不动点：默认 TRUSTED 时「原样传」与「强制 TRUSTED」同解，所以用 UNTRUSTED 证明它真的透传。
    block = signed_block(
        HistoryKind.MEMORY,
        'recalled fact',
        key='mem:7',
        role='assistant',
        trust=TrustLevel.UNTRUSTED,
    )
    assert block.role == 'assistant'
    assert block.trust is TrustLevel.UNTRUSTED
    assert kind_of(block) is HistoryKind.MEMORY


def test_kind_of_reads_each_signed_kind() -> None:
    for kind in HistoryKind:
        assert kind_of(signed_block(kind, 'x', key=f'k:{kind.value}')) is kind


def test_kind_of_is_none_for_unsigned_or_unknown_blocks() -> None:
    assert kind_of(ContextBlock(role='user', content='plain')) is None
    assert kind_of(ContextBlock(role='user', content='x', extra={KIND_KEY: 'not_a_kind'})) is None
    assert kind_of(ContextBlock(role='user', content='x', extra={KIND_KEY: 123})) is None


def test_has_block_distinguishes_keys() -> None:
    # 基数 2：两个不同键的署名块都在轨迹里，查其一不能误命中其二，查不存在的键要答 False。
    blocks = [
        signed_block(HistoryKind.ENV_FACT, 'a', key='env:cwd'),
        ContextBlock(role='assistant', content='some turn'),
        signed_block(HistoryKind.RESUME_NOTE, 'b', key='resume:1'),
    ]
    assert has_block(blocks, 'env:cwd') is True
    assert has_block(blocks, 'resume:1') is True
    assert has_block(blocks, 'mem:absent') is False
    assert has_block([], 'env:cwd') is False
