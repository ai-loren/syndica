"""内核对象的纯数据表示：事件 payload 与宿主的快照用同一份，两边才认得同一样东西。

块在轨迹里的位置只在同一代里稳定：压缩整段重写一次轨迹，代号加一（`transcript_reset`），客户端
按（代，位置）认块，代号变了就整份重取。
"""

from __future__ import annotations

from typing import Any, cast

from syndica.kernel.budget import Budget, BudgetView
from syndica.kernel.context import ArtifactRef, ContextBlock
from syndica.kernel.history_blocks import kind_of

__all__ = ['artifact_payload', 'block_payload', 'budget_payload', 'plain_data']

_DEPTH = 8


def block_payload(block: ContextBlock) -> dict[str, Any]:
    """一块的纯数据表示。文本原样交出、不截断：截断必须在脱敏之后，而脱敏是出口的事。"""
    # backend 交回的 `extra` 可能读不出：只丢署名与 meta，事件不能让这一轮崩掉。读不出这件事由
    # tracing 记 ERROR。
    try:
        kind = kind_of(block)
        meta: object = block.extra.get('tool_meta')
    except Exception:
        kind, meta = None, None
    payload: dict[str, Any] = {
        'role': block.role,
        'text': _text_of(block.content),
        'trust': block.trust.value,
        'kind': None if kind is None else kind.value,
        'tool_calls': [
            {'id': call.id, 'name': call.name, 'arguments': call.arguments}
            for call in block.tool_calls
        ],
        'tool_call_id': block.tool_call_id,
        'artifacts': [artifact_payload(ref) for ref in block.artifacts],
    }
    payload['tool_meta'] = plain_data(cast(object, meta)) if isinstance(meta, dict) else {}
    return payload


def artifact_payload(ref: ArtifactRef) -> dict[str, Any]:
    return {
        'id': ref.id,
        'path': ref.path,
        'kind': ref.kind,
        'mime': ref.mime,
        'version': ref.version,
        'producer': ref.producer,
        'is_deliverable': ref.is_deliverable,
    }


def plain_data(value: object, depth: int = _DEPTH) -> Any:
    """只留下能进 JSON 的部分：工具的 meta 由工具自己定，里面可能有活对象，而事件 payload 必须能
    深拷贝（不能的会让 emit 抛，Agent 的这一轮就崩在一次可观测上）。认不出的值换成 None。"""
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if depth <= 0:
        return None
    if isinstance(value, list | tuple):
        return [plain_data(item, depth - 1) for item in cast('list[object]', value)]
    if isinstance(value, dict):
        return {
            key: plain_data(item, depth - 1)
            for key, item in cast('dict[object, object]', value).items()
            if isinstance(key, str)
        }
    return None


def _text_of(content: object) -> str:
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(part, dict) and part.get('type') == 'text':  # pyright: ignore[reportUnknownMemberType]
                text = part.get('text')  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                parts.append(text if isinstance(text, str) else '')
            elif isinstance(part, dict):
                parts.append(f'[{part.get("type", "part")}]')  # pyright: ignore[reportUnknownMemberType]
            elif isinstance(part, str):
                parts.append(part)
        return '\n'.join(parts)
    return str(content)


def budget_payload(view: BudgetView) -> dict[str, Any]:
    """预算视图的纯数据表示。金额是十进制字符串（浮点会把 0.1 记成 0.1000000000000000055…）；
    `None` 是那一维不限，或成本里有价格未知的调用。"""
    return {
        'limit': _budget(view.limit),
        'used': {
            'input_tokens': view.used.input_tokens,
            'output_tokens': view.used.output_tokens,
            'cached_input_tokens': view.used.cached_input_tokens,
            'cost': None if view.used.cost is None else str(view.used.cost),
            'turns': view.used.turns,
            'estimated': view.used.estimated,
        },
        'remaining': _budget(view.remaining),
        'exhausted': list(view.exhausted),
    }


def _budget(budget: Budget) -> dict[str, Any]:
    return {
        'tokens': budget.tokens,
        'cost': None if budget.cost is None else str(budget.cost),
        'wall_clock_s': budget.wall_clock_s,
        'turns': budget.turns,
    }
