"""内核署名的历史块（M6 T0 / spec §5.2、§8.8）。

动态环境事实、恢复提示、记忆注入、压缩标记都作为**历史块**进轨迹，不进 System Prompt：它们随
Agent 或会话而变，进 SP 会每轮改掉请求前缀、击穿前缀缓存（spec §5.2）。每个这样的块在 `extra`
里带一个内核私有的署名：

- `syndica.kind` —— 它是哪一类（环境事实 / 恢复提示 / 记忆 / 压缩标记）。压缩步骤按它决定哪些块
  必须存活（spec §8.8：已注入的环境事实、Goal 与未满足 Rubric 存活）。
- `syndica.key` —— 幂等键。注入前先看轨迹里有没有同键的块，有就不重复注入（spec §5.2「spawn 时
  一次性且幂等」；恢复沿用存下的那一份，而不是再拼一遍）。

署名只用 `syndica.` 前缀的键，与 provider 私有键（message_id、thought_signature 等）同放 `extra`
但不相撞。脱敏绝不动 `extra`、序列化原样 round-trip，所以这个署名跨存盘与恢复都存活。
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from syndica.kernel.context import ContextBlock, Role, TrustLevel

__all__ = ['IDEMPOTENCY_KEY', 'KIND_KEY', 'HistoryKind', 'has_block', 'kind_of', 'signed_block']

KIND_KEY = 'syndica.kind'
IDEMPOTENCY_KEY = 'syndica.key'


class HistoryKind(Enum):
    """内核署名历史块的类别。取值逐字进 `extra`、跨存盘存活，改一个即破坏性变更。"""

    ENV_FACT = 'env_fact'
    RESUME_NOTE = 'resume_note'
    MEMORY = 'memory'
    COMPACTION = 'compaction'
    GOAL = 'goal'
    RUBRIC = 'rubric'
    VERDICT = 'verdict'
    BUDGET = 'budget'
    PROGRESS = 'progress'
    STRATEGY = 'strategy'
    BACKGROUND = 'background'


def signed_block(
    kind: HistoryKind,
    text: str,
    *,
    key: str,
    role: Role = 'user',
    trust: TrustLevel = TrustLevel.TRUSTED,
) -> ContextBlock:
    """造一个带内核署名的历史块。`key` 是幂等键；注入方注入前应先 `has_block` 查重。

    署名写进 `extra` 的 `syndica.kind` / `syndica.key`。`trust` 由调用方给：环境事实与恢复提示由
    内核自撰（TRUSTED），而记忆与压缩衍生内容可能带不可信来源，必须显式传 UNTRUSTED。
    """
    return ContextBlock(
        role=role,
        content=text,
        trust=trust,
        extra={KIND_KEY: kind.value, IDEMPOTENCY_KEY: key},
    )


def kind_of(block: ContextBlock) -> HistoryKind | None:
    """这个块是哪一类内核署名块；没有署名或署名不认识时是 None。"""
    raw = block.extra.get(KIND_KEY)
    if not isinstance(raw, str):
        return None
    return _BY_VALUE.get(raw)


def has_block(blocks: Iterable[ContextBlock], key: str) -> bool:
    """轨迹里有没有署名幂等键为 `key` 的块。注入方靠它做到「一次性且幂等」。"""
    return any(block.extra.get(IDEMPOTENCY_KEY) == key for block in blocks)


_BY_VALUE: dict[str, HistoryKind] = {kind.value: kind for kind in HistoryKind}
