"""把 Goal 与生效 Rubric 注入历史（§7.6 / §5.2），以及锚点「能重新打开」校验（§7.6）。

Goal / Rubric 随任务而非随轮次变，所以进**历史**不进 System Prompt（否则每轮改前缀、击穿前缀缓存）。
经 M6 的署名历史块注入（`HistoryKind.GOAL` / `RUBRIC`），幂等键做到「只在新增 / 变更时重注」：两种块
的键都随**内容**变——Goal 改了就注入新的 Goal 块，Rubric 集合变了就追加替代块。压缩只保护**最新**的那
个 Goal 块与 Rubric 块（被替代的旧块不再保护，模型不会看见两份互相矛盾的要求，评审 M6）。键与渲染都
按 id 排序：store 交回的顺序变了不改请求字节。

信任按 §12.2 传递：Goal 块随 Goal 的信任（评审 H6），Rubric 块取被注入各条里最严的。

锚点规则（§7.6）的牙齿是**能重新打开**：locator 能解析、`quote` 是一段有实义的原文且确实出现在那处、
`source` 与 locator 的类别对得上——空引文、一两个字的引文对什么都「命中」，等于没有锚点（评审 H4）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from syndica.kernel.acceptance import Anchor, AnchorSource, Goal, Rubric, RubricKind
from syndica.kernel.context import ContextBlock, TrustLevel, strictest_trust
from syndica.kernel.history_blocks import HistoryKind, signed_block

__all__ = [
    'goal_block',
    'goal_key',
    'meaningful_quote',
    'rubric_block',
    'rubric_key',
    'verify_anchors',
]

_MIN_QUOTE = 4
_MESSAGE_PREFIX = 'msg:'


def meaningful_quote(quote: str) -> bool:
    """引文是不是一段有实义的原文：去掉空白至少 4 个字符。短到这个程度的引文几乎在任何来源里都能「找
    到」，用它做锚点等于没有锚点。"""
    return len(''.join(quote.split())) >= _MIN_QUOTE


def goal_key(goal: Goal) -> str:
    """Goal 块的幂等键：随 intent / 锚点 / 信任变，Goal 改了就是新块（不会因键固定而永不重注）。"""
    digest = hashlib.sha256()
    digest.update(f'{goal.intent}\0{goal.trust.value}\0'.encode())
    for anchor in goal.anchors:
        digest.update(f'{anchor.source.value}\0{anchor.locator}\0{anchor.quote}\0'.encode())
    return f'goal:{digest.hexdigest()[:16]}'


def goal_block(goal: Goal) -> ContextBlock:
    """Goal 的署名历史块（唯一权威）。信任随 Goal：从不可信消息派生的 Goal 仍不可信（评审 H6）。"""
    return signed_block(HistoryKind.GOAL, _render_goal(goal), key=goal_key(goal), trust=goal.trust)


def rubric_key(rubrics: Sequence[Rubric]) -> str:
    """生效 Rubric 集合的幂等键：按 id 排序后对内容取摘要（store 的返回顺序变了键不变），内容含状
    态、类别、是否强制与信任——任何一项变了都是新集合、追加替代块。"""
    digest = hashlib.sha256()
    for rubric in sorted(rubrics, key=lambda r: r.id):
        digest.update(
            f'{rubric.id}\0{rubric.statement}\0{rubric.kind.value}\0{rubric.status.value}\0'
            f'{rubric.mandatory}\0{rubric.trust.value}\0'.encode()
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
    """每条锚点能不能重新打开（§7.6）。返回问题列表（空 = 全部可重开）。三项都要过：引文有实义、
    `source` 与 locator 的类别一致（user_message ↔ `msg:` 开头）、locator 能在 `sources` 里解析且引
    文确实出现在那处。`sources` 与 `quote` 都应是脱敏后文本，比对才对脱敏不敏感。"""
    problems: list[str] = []
    for anchor in anchors:
        if not meaningful_quote(anchor.quote):
            problems.append(f'锚点 {anchor.locator!r} 的引文太短或为空，什么都能「命中」')
            continue
        is_message = anchor.locator.startswith(_MESSAGE_PREFIX)
        if is_message != (anchor.source is AnchorSource.USER_MESSAGE):
            problems.append(
                f'锚点 {anchor.locator!r} 的 source={anchor.source.value} 与 locator 的类别对不上'
            )
            continue
        text = sources.get(anchor.locator)
        if text is None:
            problems.append(f'锚点 {anchor.locator!r} 指向的来源不存在')
        elif anchor.quote not in text:
            problems.append(f'锚点 {anchor.locator!r} 的引文在来源里找不到（可能是编造的）')
    return problems


def _render_goal(goal: Goal) -> str:
    return f'任务目标（唯一权威）：{goal.intent}'


def _render_rubrics(rubrics: Sequence[Rubric]) -> str:
    if not rubrics:
        return '验收标准：当前没有生效的标准（此前列过的那些已不再适用）。'
    lines = ['验收标准（每条都要满足；标「需人工确认」的由人判，不自动通过）：']
    for rubric in sorted(rubrics, key=lambda r: r.id):
        mark = '（需人工确认）' if rubric.kind is RubricKind.JUDGMENT else ''
        lines.append(f'- [{rubric.id}]{mark} {rubric.statement}')
    return '\n'.join(lines)
