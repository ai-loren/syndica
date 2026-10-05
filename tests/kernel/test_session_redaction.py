"""会话脱敏收口（M6 T0）：落盘前逐叶脱敏，绝不动结构字段，类型只由收口函数构造。"""

from __future__ import annotations

import copy

import pytest

from syndica.kernel.context import ContextBlock, ToolCall, TrustLevel
from syndica.kernel.errors import ContractViolation
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.session_redaction import (
    RedactedContent,
    RedactedSessionRecord,
    redacted_content,
    redacted_record,
)
from syndica.kernel.session_types import SessionMeta, SessionRecord

_SECRET = 'quiz-secret-value-6'


def _redactor() -> RuleRedactor:
    return RuleRedactor(secrets=[_SECRET])


def _record() -> SessionRecord:
    return SessionRecord(
        meta=SessionMeta(sid='s1', title=f'title {_SECRET}', agent='Worker'),
        content=[
            ContextBlock(role='user', content=f'please use {_SECRET} now'),
            ContextBlock(
                role='assistant',
                content='calling',
                tool_calls=[ToolCall(id='call-1', name='Bash', arguments=f'{{"k": "{_SECRET}"}}')],
            ),
            ContextBlock(role='tool', content=f'output has {_SECRET}', tool_call_id='call-1'),
        ],
        system_prompt=f'system {_SECRET}',
    )


def test_redacts_text_leaves_but_not_structural_fields() -> None:
    wrapped = redacted_record(_record(), _redactor())
    record = wrapped.record

    blob = '|'.join(
        str(block.content)
        + '|'.join(call.arguments for call in block.tool_calls)
        + (block.tool_call_id or '')
        for block in record.content
    )
    assert _SECRET not in blob
    assert _SECRET not in record.system_prompt
    assert _SECRET not in record.meta.title
    # 结构字段原样：role、tool_call id、tool_call_id、配对不动。
    assert [block.role for block in record.content] == ['user', 'assistant', 'tool']
    assert record.content[1].tool_calls[0].id == 'call-1'
    assert record.content[2].tool_call_id == 'call-1'


def test_redaction_does_not_mutate_the_caller_record() -> None:
    original = _record()
    snapshot = copy.deepcopy(original)
    redacted_record(original, _redactor())
    assert original.content[0].content == snapshot.content[0].content
    assert original.meta.title == snapshot.meta.title


def test_trust_markers_survive_redaction() -> None:
    record = SessionRecord(
        meta=SessionMeta(sid='s1'),
        content=[
            ContextBlock(role='user', content=f'web said {_SECRET}', trust=TrustLevel.UNTRUSTED)
        ],
    )
    wrapped = redacted_record(record, _redactor())
    assert wrapped.record.content[0].trust is TrustLevel.UNTRUSTED


def test_redacted_content_only_redacts_leaves() -> None:
    wrapped = redacted_content([ContextBlock(role='user', content=f'x {_SECRET} y')], _redactor())
    assert _SECRET not in str(wrapped.content[0].content)


def test_nested_multimodal_text_is_redacted_but_base64_is_kept() -> None:
    # 深层文本（tool_result.content[].text、image_url.url 的 userinfo）也脱；base64 图片数据原样留。
    blob = 'iVBORw0KGgo' + _SECRET + 'AAAA'
    block = ContextBlock(
        role='tool',
        content=[
            {'type': 'text', 'text': f'saw {_SECRET}'},
            {'type': 'tool_result', 'content': [{'type': 'text', 'text': f'deep {_SECRET}'}]},
            {'type': 'image_url', 'image_url': {'url': f'https://u:{_SECRET}@h/x.png'}},
            {
                'type': 'image',
                'source': {'type': 'base64', 'media_type': 'image/png', 'data': blob},
            },
        ],
    )
    wrapped = redacted_content([block], _redactor())
    parts = wrapped.content[0].content
    assert isinstance(parts, list)
    assert _SECRET not in str(parts[0])
    assert _SECRET not in str(parts[1])  # 嵌套一层的 text
    assert _SECRET not in str(parts[2])  # image_url.url 的 userinfo
    source = parts[3]['source']  # type: ignore[index]
    assert source['data'] == blob  # base64 原样保留
    assert source['type'] == 'base64'


def test_the_wrappers_reject_direct_construction() -> None:
    with pytest.raises(ContractViolation):
        RedactedSessionRecord(_record())
    with pytest.raises(ContractViolation):
        RedactedContent([ContextBlock(role='user', content='x')])
