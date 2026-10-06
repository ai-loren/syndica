"""trace 的数据模型（M3 D3 / D6）：枚举取值、属性闭集与每个字段的校验。"""

from __future__ import annotations

import dataclasses
import json
from enum import Enum
from typing import Any

import pytest

from syndica.kernel.acceptance import VerdictStatus
from syndica.kernel.budget import Exhaustion
from syndica.kernel.context import StopReason, TrustLevel
from syndica.kernel.danger import ConfirmationOutcome, Disposition, RiskLevel
from syndica.kernel.errors import ContractViolation
from syndica.kernel.long_horizon import BudgetMove, DriftMove, StallMove
from syndica.kernel.redaction import RedactionRule, RuleRedactor, encode_redacted, looks_secret
from syndica.kernel.trace import (
    ATTRIBUTE_DOMAINS,
    ATTRIBUTE_KINDS,
    TRACE_SCHEMA,
    CausalityEdge,
    EdgeKind,
    NewerTraceSchema,
    PayloadRef,
    Span,
    SpanEvent,
    SpanKind,
    SpanStatus,
    TraceHeader,
    TraceLevel,
    TraceRedaction,
    TraceSource,
    readable_schema,
)
from syndica.kernel.waiting import WaitState

_SPAN_ID = '0123456789abcdef'
_TRACE_ID = '0123456789abcdef' * 2
_DIGEST = 'ab' * 32
_MODELS = (
    Span,
    SpanEvent,
    CausalityEdge,
    TraceHeader,
    TraceRedaction,
    TraceSource,
    PayloadRef,
    RedactionRule,
)


def _span(**changes: Any) -> Span:
    fields: dict[str, Any] = {
        'span_id': _SPAN_ID,
        'parent_span_id': None,
        'kind': SpanKind.TURN,
        'name': 'turn 1',
        'agent_id': 'a1',
        'start_ns': 1,
        'end_ns': 2,
        'status': SpanStatus.OK,
        **changes,
    }
    return Span(**fields)


def _header(**changes: Any) -> TraceHeader:
    fields: dict[str, Any] = {
        'trace_id': _TRACE_ID,
        'started_ns': 10,
        'level': TraceLevel.REPLAY,
        'redaction': TraceRedaction(_DIGEST, 1),
        **changes,
    }
    return TraceHeader(**fields)


def _plain(value: object) -> object:
    """模型交给编码器之前的样子：dataclass 成映射、枚举取值、元组成列表。"""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _plain(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def test_the_enum_values_are_the_schema():
    assert {member.name: member.value for member in SpanKind} == {
        'AGENT': 'agent',
        'TURN': 'turn',
        'LLM_CALL': 'llm_call',
        'TOOL_CALL': 'tool_call',
        'CONFIRMATION': 'confirmation',
        'WAIT': 'wait',
        'MODULE': 'module',
    }
    assert {member.name: member.value for member in EdgeKind} == {
        'SPAWN': 'spawn',
        'MESSAGE': 'message',
        'ARTIFACT': 'artifact',
        'VERIFICATION': 'verification',
    }
    assert {member.name: member.value for member in SpanStatus} == {
        'OK': 'ok',
        'ERROR': 'error',
        'CANCELLED': 'cancelled',
    }
    assert {member.name: member.value for member in TraceLevel} == {
        'OFF': 'off',
        'STRUCTURE': 'structure',
        'REPLAY': 'replay',
        'FULL': 'full',
    }
    assert TRACE_SCHEMA == 'syndica.trace/6'


def test_the_attribute_set_is_exactly_the_schema():
    assert dict(ATTRIBUTE_KINDS) == {
        'agent.role': 'str',
        'agent.cycle': 'int',
        'turn': 'int',
        'stop_reason': 'str',
        'llm.platform': 'str',
        'llm.error': 'str',
        'llm.status': 'int?',
        'llm.model': 'str',
        'llm.attempt': 'int',
        'llm.input_tokens': 'int?',
        'llm.output_tokens': 'int?',
        'llm.cached_input_tokens': 'int?',
        'llm.request_digest': 'str',
        'llm.request_units': 'payload',
        'llm.request': 'payload',
        'llm.response': 'payload',
        'tool.name': 'str',
        'tool.call_id': 'str',
        'tool.index': 'int',
        'tool.outcome': 'str',
        'tool.exit_code': 'int?',
        'tool.timed_out': 'bool',
        'tool.result': 'payload',
        'danger.rule_ids': 'str[]',
        'danger.risk': 'str',
        'danger.provenance': 'str',
        'danger.decided_by': 'str',
        'danger.disposition': 'str',
        'confirmation.outcome': 'str',
        'wait.kind': 'str',
        'wait.target': 'str',
        'wait.outcome': 'str',
        'wait.answer': 'payload',
        'message.id': 'str',
        'message.to': 'str',
        'child.agent_id': 'str',
        'agent.id': 'str',
        'module.name': 'str',
        'module.version': 'str',
        'module.tools': 'str[]',
        'module.plugins': 'str[]',
        'module.flags': 'str[]',
        'junction.name': 'str',
        'artifact.path': 'str',
        'artifact.version': 'str',
        'artifact.trust': 'str',
        'provenance.source': 'str',
        'provenance.trust': 'str',
        'verdict.status': 'str',
        'verdict.unverified_cause': 'str',
        'verdict.needs_human': 'str[]',
        'verdict.outcomes': 'str[]',
        'compaction.trigger': 'str',
        'compaction.blocks_before': 'int',
        'compaction.blocks_after': 'int',
        'compaction.tokens_before': 'int',
        'compaction.tokens_after': 'int',
        'compaction.note': 'str',
        'compaction.archive': 'int?',
        'budget.input_tokens': 'int',
        'budget.output_tokens': 'int',
        'budget.cached_input_tokens': 'int',
        'budget.turns': 'int',
        'budget.cost': 'str?',
        'budget.estimated': 'bool',
        'budget.limit_tokens': 'int?',
        'budget.limit_cost': 'str?',
        'budget.limit_turns': 'int?',
        'budget.limit_wall_clock_s': 'int?',
        'budget.percent_left': 'int',
        'budget.dimensions': 'str[]',
        'budget.disposition': 'str',
        'agent.serves': 'str',
        'progress.satisfied': 'int',
        'progress.mandatory': 'int',
        'progress.pending_human': 'int',
        'progress.regressed': 'str[]',
        'progress.stalled_turns': 'int',
        'drift.anchored': 'bool',
        'drift.confirmed': 'bool?',
        'strategy.hook': 'str',
        'strategy.move': 'str',
    }


def test_the_attributes_with_a_fixed_set_of_values_follow_the_kernel_enums():
    assert ATTRIBUTE_DOMAINS['stop_reason'] == {member.value for member in StopReason}
    assert ATTRIBUTE_DOMAINS['verdict.status'] == {member.value for member in VerdictStatus}
    assert ATTRIBUTE_DOMAINS['confirmation.outcome'] == {
        member.value for member in ConfirmationOutcome
    }
    assert ATTRIBUTE_DOMAINS['wait.outcome'] == {member.value for member in WaitState}
    assert ATTRIBUTE_DOMAINS['danger.risk'] == {member.value for member in RiskLevel}
    assert ATTRIBUTE_DOMAINS['danger.provenance'] == {member.value for member in TrustLevel}
    assert ATTRIBUTE_DOMAINS['artifact.trust'] == {member.value for member in TrustLevel}
    assert ATTRIBUTE_DOMAINS['provenance.trust'] == {member.value for member in TrustLevel}
    assert ATTRIBUTE_DOMAINS['provenance.source'] == {'baseline', 'tool', 'external'}
    assert ATTRIBUTE_DOMAINS['danger.disposition'] == {member.value for member in Disposition}
    assert ATTRIBUTE_DOMAINS['wait.kind'] == {'agent', 'human'}
    assert ATTRIBUTE_DOMAINS['danger.decided_by'] == {'flag', 'policy'}
    assert ATTRIBUTE_DOMAINS['budget.disposition'] == {member.value for member in Exhaustion}
    assert ATTRIBUTE_DOMAINS['compaction.trigger'] == {'threshold', 'overflow'}
    assert ATTRIBUTE_DOMAINS['strategy.hook'] == {
        'on_progress_stall',
        'on_budget_low',
        'on_goal_drift',
        'should_stop',
    }
    assert ATTRIBUTE_DOMAINS['strategy.move'] == (
        {m.value for m in StallMove}
        | {m.value for m in BudgetMove}
        | {m.value for m in DriftMove}
        | {'stop', 'continue'}
    )
    assert ATTRIBUTE_DOMAINS['tool.outcome'] == {
        'ok',
        'error',
        'aborted',
        'judge_failed',
        'not_executed',
        'skipped',
        'blocked',
        'rejected',
        'timeout',
        'preempted',
        'confirmation_aborted',
        'no_channel',
        'unknown_tool',
        'bad_arguments',
        'interrupted',
    }
    assert set(ATTRIBUTE_DOMAINS) <= set(ATTRIBUTE_KINDS)


@pytest.mark.parametrize(
    ('key', 'value'),
    [(key, value) for key, domain in sorted(ATTRIBUTE_DOMAINS.items()) for value in sorted(domain)],
)
def test_every_value_in_a_fixed_set_is_accepted(key: str, value: str):
    assert _span(attributes={key: value}).attributes == {key: value}


@pytest.mark.parametrize('key', sorted(ATTRIBUTE_DOMAINS))
def test_a_value_outside_a_fixed_set_is_refused(key: str):
    with pytest.raises(ContractViolation) as caught:
        _span(attributes={key: 'rejcted'})

    assert key in caught.value.expected
    assert 'ATTRIBUTE_DOMAINS' in caught.value.next_action


@pytest.mark.parametrize(
    ('key', 'value'),
    [
        ('agent.role', 'Worker'),
        ('turn', 0),
        ('turn', 2**63 - 1),
        ('turn', -(2**63)),
        ('llm.input_tokens', None),
        ('llm.input_tokens', 12),
        ('tool.timed_out', False),
        ('danger.rule_ids', []),
        ('danger.rule_ids', ['shell.write', 'git.state', 'path.outside']),
        ('danger.rule_ids', ['shell.write', 'shell.write']),
        ('danger.rule_ids', ('shell.write',)),
        ('tool.result', '0f' * 32),
        ('wait.answer', '9' * 64),
    ],
)
def test_an_attribute_of_the_right_kind_is_kept(key: str, value: object):
    span = _span(attributes={key: value})

    assert span.attributes == {key: list(value) if isinstance(value, tuple) else value}


@pytest.mark.parametrize(
    ('key', 'value'),
    [
        ('agent.role', 3),
        ('turn', True),
        ('turn', None),
        ('turn', 1.0),
        ('turn', '1'),
        ('turn', 2**63),
        ('turn', -(2**63) - 1),
        ('llm.input_tokens', False),
        ('tool.timed_out', 1),
        ('tool.timed_out', None),
        ('danger.rule_ids', 'shell.write'),
        ('danger.rule_ids', ['shell.write', 3]),
        ('danger.rule_ids', None),
    ],
)
def test_an_attribute_of_the_wrong_kind_is_refused(key: str, value: object):
    with pytest.raises(ContractViolation) as caught:
        _span(attributes={key: value})

    assert key in caught.value.expected
    assert '布尔值不算整数' in caught.value.next_action


@pytest.mark.parametrize(
    'value',
    ['0F' * 32, '0f' * 31, '0f' * 33, ' ' + '0f' * 32, 'see the attached reply', None, 64, True],
    ids=['uppercase', 'short', 'long', 'padded', 'text', 'none', 'int', 'bool'],
)
def test_a_payload_attribute_takes_only_a_digest_and_never_echoes_what_it_got(value: object):
    with pytest.raises(ContractViolation) as caught:
        _span(attributes={'llm.response': value})

    assert 'llm.response' in caught.value.expected and 'sha256' in caught.value.expected
    assert 'put_payload' in caught.value.next_action
    if isinstance(value, str):
        assert value not in caught.value.received


@pytest.mark.parametrize('key', ['tool.nmae', 'Turn', 3])
def test_an_attribute_outside_the_closed_set_is_refused(key: object):
    with pytest.raises(ContractViolation) as caught:
        _span(attributes={key: 'x'})

    assert 'ATTRIBUTE_KINDS' in caught.value.next_action


def test_the_closed_set_applies_to_events_and_edges_too():
    with pytest.raises(ContractViolation):
        SpanEvent('message.sent', 1, {'message.too': 'a2'})
    with pytest.raises(ContractViolation):
        CausalityEdge(EdgeKind.SPAWN, _SPAN_ID, _SPAN_ID, {'child.agent_id': 7})


def test_attributes_are_copied_on_construction():
    rules = ['shell.write']
    given: dict[str, object] = {'danger.rule_ids': rules, 'turn': 1}

    span = _span(attributes=given)
    given['turn'] = 2
    rules.append('forged')

    assert span.attributes == {'danger.rule_ids': ['shell.write'], 'turn': 1}


@pytest.mark.parametrize(
    'changes',
    [
        {'span_id': _SPAN_ID[:-1]},
        {'span_id': _SPAN_ID + '0'},
        {'span_id': _SPAN_ID.upper()},
        {'parent_span_id': 'parent'},
        {'kind': 'turn'},
        {'status': 'ok'},
        {'start_ns': True},
        {'start_ns': -1},
        {'start_ns': 2**63, 'end_ns': 2**63},
        {'start_ns': 5, 'end_ns': 4},
        {'name': None},
        {'agent_id': 7},
        {'attributes': [('turn', 1)]},
        {'events': ('message.sent',)},
        {'events': None},
        {'events': 'message.sent'},
    ],
    ids=[
        'short-id',
        'long-id',
        'uppercase-id',
        'parent-id',
        'kind-as-text',
        'status-as-text',
        'boolean-time',
        'negative-time',
        'time-beyond-int64',
        'ends-before-it-starts',
        'no-name',
        'agent-id-not-text',
        'attributes-not-a-mapping',
        'event-not-a-span-event',
        'events-none',
        'events-as-text',
    ],
)
def test_a_malformed_span_is_refused(changes: dict[str, Any]):
    with pytest.raises(ContractViolation):
        _span(**changes)


def test_a_span_may_start_at_zero_and_take_no_time():
    span = _span(start_ns=0, end_ns=0)
    longest = _span(start_ns=2**63 - 1, end_ns=2**63 - 1)

    assert (span.start_ns, span.end_ns, longest.end_ns) == (0, 0, 2**63 - 1)


def test_events_are_kept_as_a_tuple_in_order():
    events = [SpanEvent('z', 9), SpanEvent('a', 3), SpanEvent('m', 5)]

    assert _span(events=events).events == tuple(events)


@pytest.mark.parametrize(
    'arguments',
    [
        {'name': 7, 'time_ns': 1},
        {'name': 'sent', 'time_ns': -1},
        {'name': 'sent', 'time_ns': True},
        {'name': 'sent', 'time_ns': 1, 'attributes': {'turn': 'one'}},
    ],
    ids=['name-not-text', 'negative-time', 'boolean-time', 'attribute-kind'],
)
def test_a_malformed_event_is_refused(arguments: dict[str, Any]):
    with pytest.raises(ContractViolation):
        SpanEvent(**arguments)


@pytest.mark.parametrize(
    'arguments',
    [
        ('spawn', _SPAN_ID, _SPAN_ID),
        (EdgeKind.SPAWN, 'source', _SPAN_ID),
        (EdgeKind.SPAWN, _SPAN_ID, 'target'),
    ],
    ids=['kind-as-text', 'source-id', 'target-id'],
)
def test_a_malformed_edge_is_refused(arguments: tuple[Any, str, str]):
    with pytest.raises(ContractViolation):
        CausalityEdge(*arguments)


@pytest.mark.parametrize(
    'changes',
    [
        {'trace_id': _TRACE_ID[:-1]},
        {'started_ns': -1},
        {'level': 'replay'},
        {'redaction': {'digest': _DIGEST}},
        {'source': {'argv': []}},
        {'status': SpanStatus.OK},
        {'ended_ns': 20},
        {'ended_ns': 5, 'status': SpanStatus.OK},
        {'ended_ns': 20, 'status': 'ok'},
        {'started_ns': 0, 'ended_ns': 1.5, 'status': SpanStatus.OK},
        {'schema': 'syndica.trace/0'},
    ],
    ids=[
        'short-trace-id',
        'negative-start',
        'level-as-text',
        'redaction-not-typed',
        'source-not-typed',
        'status-without-end',
        'end-without-status',
        'ends-before-it-starts',
        'status-as-text',
        'end-not-an-integer',
        'other-schema',
    ],
)
def test_a_malformed_header_is_refused(changes: dict[str, Any]):
    with pytest.raises(ContractViolation):
        _header(**changes)


@pytest.mark.parametrize('schema', ['syndica.trace/7', 'syndica.trace/10'])
def test_a_header_from_a_newer_version_says_so_and_that_it_is_not_broken(schema: str):
    with pytest.raises(NewerTraceSchema) as caught:
        _header(schema=schema)

    assert schema in caught.value.received
    assert '不要删掉' in caught.value.next_action


@pytest.mark.parametrize(
    'schema', ['syndica.trace/0', 'syndica.trace/01', 'syndica.trace/1.1', 'other/2', 2, None]
)
def test_a_schema_that_is_not_a_newer_version_is_refused_as_unknown(schema: object):
    with pytest.raises(ContractViolation) as caught:
        readable_schema(schema)

    assert not isinstance(caught.value, NewerTraceSchema)
    assert '删掉它所在的目录' in caught.value.next_action


def test_the_current_schema_reads():
    readable_schema(TRACE_SCHEMA)


def test_a_finished_header_carries_both_end_and_status():
    header = _header(ended_ns=20, status=SpanStatus.CANCELLED)
    instant = _header(ended_ns=10, status=SpanStatus.OK)

    assert (header.ended_ns, header.status, header.schema) == (
        20,
        SpanStatus.CANCELLED,
        TRACE_SCHEMA,
    )
    assert instant.ended_ns == instant.started_ns


@pytest.mark.parametrize(
    'arguments',
    [
        {'digest': 'ab' * 31, 'rules_version': 1},
        {'digest': _DIGEST, 'rules_version': True},
        {'digest': _DIGEST, 'rules_version': 1, 'rules': (('custom.host', 'corp'),)},
        {'digest': _DIGEST, 'rules_version': 1, 'rules': None},
        {'digest': _DIGEST, 'rules_version': 1, 'rules': 'custom.host'},
    ],
    ids=['short-digest', 'boolean-version', 'pair-not-a-rule', 'rules-none', 'rules-as-text'],
)
def test_a_malformed_ruleset_record_is_refused(arguments: dict[str, Any]):
    with pytest.raises(ContractViolation):
        TraceRedaction(**arguments)


def test_a_ruleset_record_keeps_rules_as_records_and_reads_back_from_json():
    rules = [RedactionRule('custom.api_token', r'tok_[A-Za-z0-9]{32}')]
    record = TraceRedaction(_DIGEST, 1, rules)  # pyright: ignore[reportArgumentType]
    stored = json.loads(json.dumps([dataclasses.asdict(rule) for rule in record.rules]))

    assert record.rules == tuple(rules)
    assert tuple(RedactionRule(**item) for item in stored) == record.rules


@pytest.mark.parametrize(
    'arguments',
    [
        {'argv': 'run --trace full'},
        {'argv': None},
        {'argv': ['run', 3]},
        {'config_sha256': 'abc'},
        {'version': None},
        {'git_commit': 7},
        {'replay_of': 'abc'},
    ],
    ids=[
        'argv-as-text',
        'argv-none',
        'argv-not-text',
        'config-digest',
        'version',
        'commit',
        'replay-of',
    ],
)
def test_a_malformed_source_record_is_refused(arguments: dict[str, Any]):
    with pytest.raises(ContractViolation):
        TraceSource(**arguments)


def test_a_source_record_keeps_argv_as_a_tuple():
    assert TraceSource(argv=['run', '--trace', 'full']).argv == ('run', '--trace', 'full')  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    'arguments',
    [(_TRACE_ID[:-1], _DIGEST), (_TRACE_ID, _DIGEST[:-1]), (_TRACE_ID, _DIGEST.upper())],
    ids=['short-trace-id', 'short-digest', 'uppercase-digest'],
)
def test_a_malformed_payload_reference_is_refused(arguments: tuple[str, str]):
    with pytest.raises(ContractViolation):
        PayloadRef(*arguments)


def test_no_name_that_looks_secret_holds_text():
    """trace 落盘前经 `encode_redacted`：名字像密钥的键下面、像凭证的字符串会被换成占位符，于是摘要
    与 id 都会丢。整数与布尔它不碰，`llm.input_tokens` 这类计数可以叫这个名字。"""
    fields = {field.name for model in _MODELS for field in dataclasses.fields(model)}
    textual = {
        key
        for key, kind in ATTRIBUTE_KINDS.items()
        if kind.rstrip('?') in {'str', 'str[]', 'payload'}
    }

    assert sorted(name for name in fields | textual if looks_secret(name)) == []
    assert sorted(key for key in ATTRIBUTE_KINDS if looks_secret(key)) == [
        'budget.cached_input_tokens',
        'budget.input_tokens',
        'budget.limit_tokens',
        'budget.output_tokens',
        'compaction.tokens_after',
        'compaction.tokens_before',
        'llm.cached_input_tokens',
        'llm.input_tokens',
        'llm.output_tokens',
    ]


def test_a_whole_header_and_span_survive_leaf_redaction_unchanged():
    header = TraceHeader(
        _TRACE_ID,
        1_700_000_000_000_000_000,
        TraceLevel.FULL,
        TraceRedaction(
            '7f' * 32,
            3,
            (
                RedactionRule('custom.api_token', r'tok_[A-Za-z0-9]{32}'),
                RedactionRule('custom.session_key', r'sess-[0-9a-f]{24}'),
            ),
        ),
        TraceSource(argv=('run', 'ship it'), config_sha256='5e' * 32, version='0.3.0'),
    )
    span = _span(
        attributes={
            'llm.request_digest': 'c3' * 32,
            'tool.call_id': 'call_9f8a7b6c5d4e3f2a1b0c',
            'danger.rule_ids': ['shell.ssh_key', 'path.outside_workspace'],
            'tool.exit_code': None,
            'tool.timed_out': True,
            'llm.input_tokens': 123456789,
            'llm.cached_input_tokens': None,
        },
        events=(SpanEvent('message.sent', 1, {'message.to': 'a2', 'message.id': 'm' * 32}),),
    )

    for value in (header, span):
        assert json.loads(encode_redacted(_plain(value), RuleRedactor())) == _plain(value)


def test_a_header_from_an_older_version_is_still_read_as_written():
    header = _header(schema='syndica.trace/1')
    assert header.schema == 'syndica.trace/1'
