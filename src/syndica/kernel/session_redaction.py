"""会话记录的脱敏收口（M6 T0 / spec §12.3、§12.5）。

落盘的会话正文一律先过这里。脱敏**逐叶**只动会出现密钥的文本——块内容、`ToolCall.arguments`、工具输出
（`role='tool'` 的块内容）、system prompt、标题——**绝不**改 role / `ToolCall.id` / `tool_call_id` /
`extra`（它放着 `message_id`、`thought_signature` 等 provider 私有键，改了会撞坏配对）。多模态部件里
只脱 `text`，图片 / base64 原样留着。

`SessionStore` 的写入面只收 `RedactedSessionRecord` / `RedactedContent`——它们只由本模块的
`redacted_record` / `redacted_content` 构造，于是第三方 store（Redis / Postgres）也绕不过脱敏（与
`TraceStore` 只收 `RedactedText` 同一招）。脱敏后交给 store 前复校工具调用 id 唯一（脱敏不该动 id，
复校是防回归的那道闸）。
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import cast, final

from syndica.kernel.context import ContextBlock
from syndica.kernel.errors import ContractViolation
from syndica.kernel.redaction import Redactor
from syndica.kernel.session_types import SessionRecord

__all__ = ['RedactedContent', 'RedactedSessionRecord', 'redacted_content', 'redacted_record']

_MINT = object()


@final
class RedactedSessionRecord:
    """`redacted_record` 的输出：正文逐叶脱敏过的 `SessionRecord`。只有 `redacted_record` 构造它，
    收持久化内容的接口靠这个类型认出「已经脱敏」，所以直接构造报 `ContractViolation`。"""

    __slots__ = ('record',)

    def __init__(self, record: SessionRecord, *, _mint: object = None) -> None:
        if _mint is not _MINT:
            raise ContractViolation(
                expected='RedactedSessionRecord 只由 redacted_record 构造',
                received='有代码直接构造了它',
                next_action='把记录交给 redacted_record(record, redactor)：它先逐叶脱敏再复校',
            )
        self.record = record


@final
class RedactedContent:
    """`redacted_content` 的输出：逐叶脱敏过的一段轨迹，`replace_content` 只收它。"""

    __slots__ = ('content',)

    def __init__(self, content: list[ContextBlock], *, _mint: object = None) -> None:
        if _mint is not _MINT:
            raise ContractViolation(
                expected='RedactedContent 只由 redacted_content 构造',
                received='有代码直接构造了它',
                next_action='把内容交给 redacted_content(content, redactor)',
            )
        self.content = content


_BINARY_KEYS = frozenset({'data'})


def _redact_text(value: object, redactor: Redactor) -> object:
    """块内容的叶子脱敏：递归进列表与字典，逐个字符串叶子脱敏。嵌套 `tool_result.content[].text`、
    `image_url.url` 的 userinfo 也脱；`data` 键下 base64 原样留（脱敏会匹配一段密钥样式改坏它）；
    字典的键是结构、不脱。"""
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, list):
        return [_redact_text(item, redactor) for item in cast('list[object]', value)]
    if isinstance(value, dict):
        return {
            key: (val if key in _BINARY_KEYS else _redact_text(val, redactor))
            for key, val in cast('dict[str, object]', value).items()
        }
    return value


def _redact_block(block: ContextBlock, redactor: Redactor) -> ContextBlock:
    return replace(
        block,
        content=_redact_text(block.content, redactor),
        tool_calls=[
            replace(call, arguments=redactor.redact(call.arguments)) for call in block.tool_calls
        ],
    )


def _tool_call_ids(content: list[ContextBlock]) -> list[str]:
    return [call.id for block in content for call in block.tool_calls]


def _checked(before: list[ContextBlock], after: list[ContextBlock]) -> None:
    # 脱敏前后的 tool_call id 必须逐个相同。不是查「全局唯一」：模型在更早的轮里用过同一个 id
    # （`call_0`、空串）是合法的，执行器按位置配对（见 tool_executor.append_unexecuted），查全局唯一
    # 会让这类轨迹每次存盘都报错并把失败误报成「磁盘满」。真正要守的是「脱敏不碰 id」。
    if _tool_call_ids(before) != _tool_call_ids(after):
        raise ContractViolation(
            expected='脱敏之后工具调用 id 与脱敏前逐个相同（顺序与多重性都不变）',
            received='脱敏改动了某个 tool_call id',
            next_action='脱敏只该动文本叶子，不该碰 id；检查用户规则是否命中了 id 字段',
        )


def redacted_content(content: list[ContextBlock], redactor: Redactor) -> RedactedContent:
    redacted = [_redact_block(copy.deepcopy(block), redactor) for block in content]
    _checked(content, redacted)
    return RedactedContent(redacted, _mint=_MINT)


def redacted_record(record: SessionRecord, redactor: Redactor) -> RedactedSessionRecord:
    copied = copy.deepcopy(record)
    before = [copy.deepcopy(block) for block in copied.content]
    copied.content = [_redact_block(block, redactor) for block in copied.content]
    copied.system_prompt = redactor.redact(copied.system_prompt)
    copied.meta.title = redactor.redact(copied.meta.title)
    _checked(before, copied.content)
    return RedactedSessionRecord(copied, _mint=_MINT)
