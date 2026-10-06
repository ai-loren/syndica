"""外发载荷的唯一出口：REST 响应、SSE 事件、WebSocket 帧都经这里交出。

每个字符串叶子先过脱敏器、再截断（`kernel.clipping`）：截断可能切在一个密钥中间，剩下的那截不一定
还认得出来，所以顺序不能反。字典的键不脱敏（键是 server 自己定的字段名）。
"""

from __future__ import annotations

import json
from typing import Any, cast

from starlette.responses import Response

from syndica.kernel import Redactor
from syndica.kernel.clipping import clip_head
from syndica.server.streams import PING, RESET, Entry

__all__ = ['FIELD_LIMIT', 'encoded', 'json_response', 'redacted', 'redacted_text', 'sse_frame']

# 单个字符串字段的上限：工具输出已经按工具自己的上限截过，这里兜住宿主没截的那些（产物文本）。
FIELD_LIMIT = 200_000


def redacted_text(text: str, redactor: Redactor) -> str:
    return clip_head(redactor.redact(text), FIELD_LIMIT)


def redacted(value: object, redactor: Redactor) -> Any:
    """纯数据的脱敏副本：每个字符串叶子先脱敏再截断。"""
    if isinstance(value, str):
        return redacted_text(value, redactor)
    if isinstance(value, dict):
        return {
            key: redacted(item, redactor) for key, item in cast('dict[str, object]', value).items()
        }
    if isinstance(value, list | tuple):
        return [redacted(item, redactor) for item in cast('list[object]', value)]
    return value


def encoded(value: object, redactor: Redactor) -> str:
    return json.dumps(redacted(value, redactor), ensure_ascii=False, separators=(',', ':'))


def json_response(value: object, redactor: Redactor, *, status: int = 200) -> Response:
    return Response(encoded(value, redactor), status_code=status, media_type='application/json')


def sse_frame(epoch: str, item: Entry | str, redactor: Redactor) -> str:
    """一条 SSE 帧。reset 不带 id：它不是流里的一条事件，客户端不该拿它续读。"""
    if item == PING:
        return ': ping\n\n'
    if item == RESET:
        return 'event: reset\ndata: {}\n\n'
    assert isinstance(item, Entry)
    return f'id: {epoch}:{item.seq}\nevent: {item.kind}\ndata: {encoded(item.data, redactor)}\n\n'
