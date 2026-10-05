"""把 Goal 与生效 Rubric 注入历史（§7.6 / §5.2），以及锚点「能重新打开」校验（§7.6）。

Goal / Rubric 随任务而非随轮次变，所以进**历史**不进 System Prompt（否则每轮改前缀、击穿前缀缓存）。
经 M6 的署名历史块注入（`HistoryKind.GOAL` / `RUBRIC`）、幂等键做到「只在新增 / 变更时重注」：Goal
固定键；Rubric 一个随**集合内容**变的键，集合变了就追加替代块（旧块留着靠压缩保护，不 rebaseline）。

信任按 §12.2 传递：Rubric 块的信任取被注入各条里最严的（不可信输入投出的 Rubric 仍不可信）。

锚点规则（§7.6）的牙齿是**能重新打开**：locator 能解析、`quote` 确实出现在那处——`verify_anchors` 做
这条二值判断，RubricsBuilder 靠它拒掉编造的锚点（`#99`、引文对不上），而不只是查「有没有锚点」。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from syndica.kernel.acceptance import Anchor, Goal, Rubric, RubricKind
from syndica.kernel.context import ContextBlock, TrustLevel, strictest_trust
from syndica.kernel.history_blocks import HistoryKind, signed_block

__all__ = ['GOAL_KEY', 'goal_block', 'rubric_block', 'rubric_key', 'verify_anchors']

GOAL_KEY = 'goal'


def goal_block(goal: Goal) -> ContextBlock:
    """Goal 的署名历史块（固定幂等键，唯一权威）。Goal 是用户的要求本身，按可信注入。"""
    return signed_block(
        HistoryKind.GOAL, _render_goal(goal), key=GOAL_KEY, trust=TrustLevel.TRUSTED
    )


def rubric_key(rubrics: Sequence[Rubric]) -> str:
    """生效 Rubric 集合的幂等键：集合内容变了键就变、追加替代块；不变则不重注（前缀缓存稳）。"""
    digest = hashlib.sha256()
    for rubric in rubrics:
        digest.update(
            f'{rubric.id}\0{rubric.statement}\0{rubric.kind.value}\0{rubric.status.value}\0'.encode()
        )
    return f'rubric:{digest.hexdigest()[:16]}'


def rubric_block(rubrics: Sequence[Rubric]) -> ContextBlock:
    """生效 Rubric 的署名历史块。信任取各条里最严的（§12.2：不可信输入投出的 Rubric 仍不可信）。"""
    trust = TrustLevel.TRUSTED
    for rubric in rubrics:
        trust = strictest_trust(trust, rubric.trust)
    return signed_block(
        HistoryKind.RUBRIC, _render_rubrics(rubrics), key=rubric_key(rubrics), trust=trust
    )


def verify_anchors(anchors: Sequence[Anchor], sources: Mapping[str, str]) -> list[str]:
    """每条锚点能不能重新打开（§7.6）：locator 能在 `sources` 里解析、`quote` 确实出现在那处。返回
    问题列表（空 = 全部可重开）。`sources` 与 `quote` 都应是脱敏后文本，比对才对脱敏不敏感。"""
    problems: list[str] = []
    for anchor in anchors:
        text = sources.get(anchor.locator)
        if text is None:
            problems.append(f'锚点 {anchor.locator!r} 指向的来源不存在')
        elif anchor.quote and anchor.quote not in text:
            problems.append(f'锚点 {anchor.locator!r} 的引文在来源里找不到（可能是编造的）')
    return problems


def _render_goal(goal: Goal) -> str:
    return f'任务目标（唯一权威）：{goal.intent}'


def _render_rubrics(rubrics: Sequence[Rubric]) -> str:
    lines = ['验收标准（每条都要满足；标「需人工确认」的由人判，不自动通过）：']
    for rubric in rubrics:
        mark = '（需人工确认）' if rubric.kind is RubricKind.JUDGMENT else ''
        lines.append(f'- [{rubric.id}]{mark} {rubric.statement}')
    return '\n'.join(lines)
