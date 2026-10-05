"""会话序列化（M6 T2）：全字段 round-trip、拒更高版本、trust fail-closed、坏块跳过。"""

from __future__ import annotations

import pytest

from syndica.kernel.context import ArtifactRef, ContextBlock, ToolCall, TrustLevel
from syndica.kernel.session_serde import (
    NewerSessionSchema,
    record_from_json,
    record_to_json,
)
from syndica.kernel.session_types import SessionMeta, SessionRecord, SessionStatus


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


def test_a_bad_block_is_skipped_and_recorded() -> None:
    data = record_to_json(_record())
    content = data['content']
    assert isinstance(content, list)
    content[1] = {'role': 'assistant', 'tool_calls': [{'name': 'Bash'}]}  # tool call missing id
    dropped: list[str] = []
    after = record_from_json(data, dropped=dropped)
    assert len(after.content) == 2
    assert len(dropped) == 1 and 'content[1]' in dropped[0]
