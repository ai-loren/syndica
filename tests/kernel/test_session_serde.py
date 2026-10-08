"""会话序列化（M6 T2）：全字段 round-trip、拒更高版本 / 外来 / 乱码 schema、trust fail-closed、
坏块即坏记录（fail-closed）、类型校验。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from syndica.kernel.budget import Usage
from syndica.kernel.context import ArtifactRef, ContextBlock, ToolCall, TrustLevel
from syndica.kernel.errors import ConfigError
from syndica.kernel.session_serde import (
    NewerSessionSchema,
    record_from_json,
    record_to_json,
)
from syndica.kernel.session_types import SessionBudget, SessionMeta, SessionRecord, SessionStatus


def _record() -> SessionRecord:
    return SessionRecord(
        meta=SessionMeta(
            sid='s1',
            title='t',
            agent='Worker',
            workspace='/w',
            status=SessionStatus.ACTIVE,
            turns=3,
            archived=2,
            last_trace='trace-9',
        ),
        content=[
            ContextBlock(role='user', content='hi', trust=TrustLevel.UNTRUSTED),
            ContextBlock(
                role='assistant',
                content=['part', {'type': 'text', 'text': 'more'}],
                tool_calls=[ToolCall(id='c1', name='Bash', arguments='{}')],
                artifacts=[
                    ArtifactRef(
                        id='file:a',
                        path='a.py',
                        kind='file',
                        mime='text/x-python',
                        version='sha256:1',
                        producer='Worker',
                        is_deliverable=True,
                    )
                ],
                extra={'message_id': 'm1', 'nested': {'k': [1, 2]}},
            ),
            ContextBlock(role='tool', content='out', tool_call_id='c1'),
        ],
        system_prompt='SP',
        model='gpt',
        tools=['Read', 'Write'],
        flags={'mode': 'dev', 'n': 2, 'on': True},
    )


def test_record_round_trips_every_field() -> None:
    before = _record()
    after = record_from_json(record_to_json(before))

    assert after.meta == before.meta
    assert after.system_prompt == before.system_prompt
    assert after.model == before.model
    assert after.tools == before.tools
    assert after.flags == before.flags
    assert len(after.content) == 3
    assert after.content[0].trust is TrustLevel.UNTRUSTED
    assert after.content[1].content == ['part', {'type': 'text', 'text': 'more'}]
    assert after.content[1].tool_calls == before.content[1].tool_calls
    assert after.content[1].artifacts == before.content[1].artifacts
    assert after.content[1].extra == {'message_id': 'm1', 'nested': {'k': [1, 2]}}
    assert after.content[2].tool_call_id == 'c1'


def test_a_newer_schema_is_refused() -> None:
    data = record_to_json(_record())
    data['schema'] = 'syndica.session/2'
    with pytest.raises(NewerSessionSchema):
        record_from_json(data)


def test_missing_or_unknown_trust_reads_as_untrusted() -> None:
    data = record_to_json(_record())
    content = data['content']
    assert isinstance(content, list)
    content[0].pop('trust')
    content[1]['trust'] = 'bogus'
    after = record_from_json(data)
    assert after.content[0].trust is TrustLevel.UNTRUSTED
    assert after.content[1].trust is TrustLevel.UNTRUSTED


def test_a_bad_block_fails_the_whole_load_closed() -> None:
    # 坏块即坏记录（fail-closed，评审 F3）：静默跳过一个块会把唯一的不可信块一起丢掉、洗白
    # provenance，还可能在历史中段留下未配对的工具调用。恢复宁可拒绝整份。
    data = record_to_json(_record())
    content = data['content']
    assert isinstance(content, list)
    content[1] = {'role': 'assistant', 'tool_calls': [{'name': 'Bash'}]}  # tool call missing id
    with pytest.raises(ConfigError):
        record_from_json(data)


def test_foreign_or_garbled_schema_is_refused() -> None:
    # 外来前缀（trace 记录）不许当会话读，乱码版本不许当 v1 收下（评审 F12）。
    for schema in ('syndica.trace/1', 'syndica.session/abc', 'syndica.session/0', 'nonsense'):
        data = record_to_json(_record())
        data['schema'] = schema
        with pytest.raises(ConfigError):
            record_from_json(data)


def test_wrong_types_are_refused_not_silently_coerced() -> None:
    # tools: "abc" 不该静默变成 ['a','b','c']；turns=True 不该当 1；status 乱码不该抛裸 ValueError。
    bad_tools = record_to_json(_record())
    bad_tools['tools'] = 'abc'
    with pytest.raises(ConfigError):
        record_from_json(bad_tools)

    bad_turns = record_to_json(_record())
    meta = bad_turns['meta']
    assert isinstance(meta, dict)
    meta['turns'] = True
    with pytest.raises(ConfigError):
        record_from_json(bad_turns)

    bad_status = record_to_json(_record())
    meta2 = bad_status['meta']
    assert isinstance(meta2, dict)
    meta2['status'] = 'bogus'
    with pytest.raises(ConfigError):
        record_from_json(bad_status)


def test_a_disallowed_role_is_refused() -> None:
    data = record_to_json(_record())
    content = data['content']
    assert isinstance(content, list)
    content[0]['role'] = 'root'
    with pytest.raises(ConfigError):
        record_from_json(data)


def test_a_record_without_a_budget_reads_as_none() -> None:
    data = record_to_json(_record())
    assert 'budget' not in data
    assert record_from_json(data).budget is None


def test_budget_limits_and_usage_round_trip_as_decimals() -> None:
    before = _record()
    before.budget = SessionBudget(
        tokens=100,
        cost=Decimal('1.50'),
        turns=4,
        used=Usage(
            input_tokens=8,
            output_tokens=2,
            cached_input_tokens=3,
            cost=Decimal('0.25'),
            turns=1,
            estimated=True,
        ),
    )
    after = record_from_json(record_to_json(before))
    assert after.budget == before.budget


def test_a_budget_whose_cost_is_not_a_decimal_string_is_refused() -> None:
    data = record_to_json(_record())
    data['budget'] = {'tokens': 1, 'cost': 1.5, 'turns': 1, 'used': {'turns': 0}}
    with pytest.raises(ConfigError) as caught:
        record_from_json(data)
    assert 'cost' in caught.value.received
