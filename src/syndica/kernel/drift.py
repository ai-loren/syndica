"""目标漂移的锚点检查（spec §8.2 第一层，零 LLM）：最近几轮的动作还锚不锚得回 Goal 与 Rubric。

动作 = 工具调用的参数（不含写进文件的正文：一段长正文几乎和什么都有词面重叠，会把漂移盖住）与工具回
报的产物路径。锚 = Goal 的原文与锚点引文、每条生效 Rubric 的陈述与引文。两边按 `lexical.terms` 切成
词面单位，一个都不重叠就是**可疑**——只是可疑：认不出同义改写，所以确认要交给第二层的语义比对，而第二
层缺省关着（贵的能力按需启用，§11.3）。窗口里没有任何动作不可疑：没做事不是做偏了。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import cast

from syndica.kernel.acceptance import Goal, Rubric, RubricStatus
from syndica.kernel.clipping import clip_head
from syndica.kernel.context import ContextBlock, ToolCall
from syndica.kernel.lexical import terms

__all__ = ['action_terms', 'anchor_terms', 'anchored', 'call_lines']

# 这些参数装的是要写下的正文，不是「在动哪里」：不进动作的词面。
_CONTENT_ARGUMENTS = frozenset(
    {'content', 'new_string', 'old_string', 'text', 'body', 'patch', 'diff', 'answer'}
)


def _values(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in cast(Mapping[object, object], value).items():
            if key not in _CONTENT_ARGUMENTS:
                yield from _values(item)
    elif isinstance(value, list | tuple):
        for item in cast(Sequence[object], value):
            yield from _values(item)


def _without_content(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: _without_content(item)
            for key, item in cast(Mapping[object, object], value).items()
            if key not in _CONTENT_ARGUMENTS
        }
    if isinstance(value, list | tuple):
        return [_without_content(item) for item in cast(Sequence[object], value)]
    return value


def _arguments(call: ToolCall) -> object:
    try:
        return cast(object, json.loads(call.arguments or '{}'))
    except (TypeError, ValueError):
        return call.arguments


def call_lines(blocks: Sequence[ContextBlock], *, line_chars: int) -> tuple[str, ...]:
    """交给语义比对的动作：工具名与参数（正文除外，与词面单位同一个视图），每行截到
    `line_chars`。"""
    lines: list[str] = []
    for block in blocks:
        for call in block.tool_calls:
            shown = _without_content(_arguments(call))
            text = shown if isinstance(shown, str) else json.dumps(shown, ensure_ascii=False)
            line = f'- {call.name} {text}'
            lines.append(line if len(line) <= line_chars else clip_head(line, line_chars) + '…')
    return tuple(lines)


def action_terms(blocks: Sequence[ContextBlock]) -> frozenset[str]:
    """这段轨迹里的动作的词面：每次工具调用的参数（正文除外）与工具回报的产物路径。"""
    found: set[str] = set()
    for block in blocks:
        for call in block.tool_calls:
            for text in _values(_arguments(call)):
                found |= terms(text)
        for artifact in block.artifacts:
            found |= terms(artifact.path)
    return frozenset(found)


def anchor_terms(goal: Goal, rubrics: Sequence[Rubric]) -> frozenset[str]:
    found = set(terms(goal.intent))
    for anchor in goal.anchors:
        found |= terms(anchor.quote)
    for rubric in rubrics:
        if rubric.status is not RubricStatus.ACTIVE:
            continue
        found |= terms(rubric.statement)
        for anchor in rubric.anchors:
            found |= terms(anchor.quote)
    return frozenset(found)


def anchored(actions: frozenset[str], anchors: frozenset[str]) -> bool:
    """动作锚得回去吗。没有动作、或者没有锚（没有 Goal 可对照）都不算可疑。"""
    if not actions or not anchors:
        return True
    return bool(actions & anchors)
