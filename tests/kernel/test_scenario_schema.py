"""场景文档的 schema（M3 D12）：v1 照旧、v2 的每个键各有合法与不合法的写法、有序字段按写下的
顺序。"""

from __future__ import annotations

import copy
import importlib
from types import MappingProxyType
from typing import Any, cast

import pytest

from syndica.kernel import DEFAULT_RULES, BaseAgent, ConfigError, HostFacts, scenario_schema
from syndica.kernel.danger import (
    git_state_change_rule,
    path_outside_workspace_rule,
    workspace_root_removal_rule,
)
from syndica.kernel.scenario_schema import (
    FROZEN_IMPLS,
    RULE_PARAMETERS,
    DangerRuleSpec,
    GateRecord,
    HostAction,
    PayloadText,
    ScenarioCall,
    ScenarioError,
    SkippedCall,
    ToolOutcome,
    parse_scenario_document,
)

_LEAD = 'f0' * 16
_REQUEST = ['tool_call', _LEAD, 1, 1, 0, 'request', 0]
_DELETE = object()


def _v2() -> dict[str, Any]:
    return {
        'version': 2,
        'name': 'captured',
        'source': {
            'trace_id': 'ab12' * 8,
            'argv': ['run', 'go', '--agent', 'Lead'],
            'version': '0.3.0',
            'config_sha256': 'c' * 64,
            'git_commit': None,
        },
        'host': {
            'flags': {'retry_enabled': True, 'attempts': 3, 'untrusted_high_risk': 'ask'},
            'danger': {
                'rules': [
                    {'id': 'shell.git_state_change'},
                    {'id': 'shell.privilege_escalation', 'params': {}},
                    {
                        'id': 'shell.path_outside_workspace',
                        'params': {'root': '/w', 'real': '/private/w'},
                    },
                ],
                'table': {
                    'shell.privilege_escalation': 'block',
                    'shell.path_outside_workspace': 'confirm',
                    'shell.git_state_change': 'block',
                },
            },
            'output_limit': 30000,
            'host_facts': {'os': 'darwin', 'platform': 'arm64', 'shell': '/bin/sh'},
            'max_turns': 40,
            'no_write': False,
            'modules': ['syndica.dev', 'retry', 'safety'],
        },
        'agents': {
            'Lead': {
                'frozen': {
                    'system_prompt': {'payload': 'a' * 64},
                    'tools': ['Write', 'Bash', 'Delegate'],
                    'max_turns': 20,
                    'impl': None,
                    'model': 'lead-model',
                },
                'instances': [
                    {
                        'key': ['host', 'spawn', 0],
                        'turns': [
                            {
                                'turn': 1,
                                'attempt': 1,
                                'raises': {
                                    'type': 'syndica.kernel.errors.LLMResponseError',
                                    'message': 'rate limited',
                                    'status_code': 429,
                                    'retry_after': 2,
                                },
                            },
                            {
                                'turn': 1,
                                'attempt': 2,
                                'content': None,
                                'tool_calls': [
                                    {'id': 'c1', 'name': 'Bash', 'arguments': {'command': 'ls'}},
                                    {'id': 'c1', 'name': 'Write', 'arguments_raw': '{"path": "a'},
                                ],
                                'request_digests': ['b' * 64, 'd' * 64],
                                'extra': {
                                    'usage': {'input_tokens': 10},
                                    'finish_reason': 'tool_calls',
                                    'llm_route': 'main',
                                },
                                'trust': 'trusted',
                            },
                            {'turn': 2, 'attempt': 1, 'content': {'payload': 'e' * 64}},
                        ],
                        'tool_results': [
                            {
                                'call': [1, 1, 0],
                                'output': {'payload': '1' * 64},
                                'meta': {'exit_code': 0},
                                'trust': 'untrusted',
                            },
                        ],
                        'skipped': [
                            {'call': [1, 1, 1], 'output': 'refused by the git guard'},
                        ],
                        'gate': [
                            {
                                'call': [1, 1, 0],
                                'rule_ids': [],
                                'risk': 'none',
                                'provenance': 'trusted',
                                'decided_by': 'policy',
                                'disposition': 'allow',
                            },
                        ],
                    },
                ],
            },
        },
        'entry': {'mode': 'query', 'agent': 'Lead', 'prompt': 'go', 'trust': 'trusted'},
        'responses': [{'key': _REQUEST, 'kind': 'user_input', 'answer': 'yes'}],
        'host_actions': [{'kind': 'abort', 'key': _REQUEST}, {'kind': 'interrupt'}],
        'ids': [{'key': ['host', 'spawn', 0], 'id': _LEAD}],
        'expect': {'stop_reasons': {'Lead[0]': 'aborted'}},
    }


def _v1() -> dict[str, Any]:
    return {
        'version': 1,
        'name': 'handwritten',
        'agents': {
            'Root': {
                'manifest': {'name': 'Root', 'description': 'root'},
                'instances': [{'turns': [{'content': 'done'}]}],
            }
        },
        'topology': {
            'slots': [
                {
                    'name': 'root',
                    'agent': 'Root',
                    'is_root': True,
                    'cardinality': 'singleton',
                    'boot': 'eager',
                }
            ]
        },
        'entry': {'slot': 'root', 'prompt': 'go'},
        'responses': [],
        'expect': {},
    }


def _with(document: dict[str, Any], path: tuple[Any, ...], value: object) -> dict[str, Any]:
    changed = copy.deepcopy(document)
    target: Any = changed
    for step in path[:-1]:
        target = target[step]
    if value is _DELETE:
        del target[path[-1]]
    elif isinstance(target, list) and path[-1] == len(cast(list[object], target)):
        cast(list[object], target).append(value)
    else:
        target[path[-1]] = value
    return changed


_INSTANCE = ('agents', 'Lead', 'instances', 0)
_TURN = (*_INSTANCE, 'turns', 1)


def test_a_full_v2_document_reads_into_typed_values():
    document = parse_scenario_document(_v2(), source='captured.json')

    lead = document.roles['Lead']
    [instance] = lead.instances
    first, second, third = instance.replies
    host = document.host
    assert host is not None and lead.frozen is not None and document.origin is not None
    assert (document.version, document.name, document.source) == (2, 'captured', 'captured.json')
    assert document.origin.argv == ('run', 'go', '--agent', 'Lead')
    assert dict(host.flags) == {'retry_enabled': True, 'attempts': 3, 'untrusted_high_risk': 'ask'}
    assert host.host_facts == HostFacts(os='darwin', platform='arm64', shell='/bin/sh')
    assert (host.output_limit, host.max_turns, host.no_write) == (30000, 40, False)
    assert lead.manifest is None and lead.frozen.system_prompt == PayloadText('a' * 64)
    assert (lead.frozen.max_turns, lead.frozen.model, lead.frozen.impl) == (20, 'lead-model', None)
    assert instance.key == ('host', 'spawn', 0)
    assert first.raises == ScenarioError(
        'syndica.kernel.errors.LLMResponseError', 'rate limited', status_code=429, retry_after=2.0
    )
    assert (first.turn, first.attempt, second.turn, second.attempt) == (1, 1, 1, 2)
    assert second.tool_calls == (
        ScenarioCall('c1', 'Bash', '{"command":"ls"}'),
        ScenarioCall('c1', 'Write', '{"path": "a'),
    )
    assert second.request_digests == ('b' * 64, 'd' * 64)
    assert second.extra == {
        'usage': {'input_tokens': 10},
        'finish_reason': 'tool_calls',
        'llm_route': 'main',
    }
    assert list(second.extra or {}) == ['usage', 'finish_reason', 'llm_route']
    assert isinstance(first.raises, ScenarioError) and type(first.raises.retry_after) is float
    assert third.content == PayloadText('e' * 64)
    assert instance.tool_results == (
        ToolOutcome((1, 1, 0), PayloadText('1' * 64), {'exit_code': 0}, 'untrusted'),
    )
    assert instance.skipped == (SkippedCall((1, 1, 1), 'refused by the git guard'),)
    assert instance.gate == (GateRecord((1, 1, 0), (), 'none', 'trusted', 'policy', 'allow'),)
    assert (document.entry.mode, document.entry.agent, document.entry.slot) == (
        'query',
        'Lead',
        None,
    )
    [response] = document.responses
    assert (response.key, response.kind, response.answer) == (tuple(_REQUEST), 'user_input', 'yes')
    assert document.host_actions == (HostAction('abort', tuple(_REQUEST)), HostAction('interrupt'))
    assert document.ids == ((('host', 'spawn', 0), _LEAD),)
    assert document.topology is None


def test_ordered_fields_keep_the_order_they_were_written_in():
    document = parse_scenario_document(_v2(), source='captured.json')

    host = document.host
    frozen = document.roles['Lead'].frozen
    assert host is not None and frozen is not None
    assert host.danger_rules == (
        DangerRuleSpec('shell.git_state_change'),
        DangerRuleSpec('shell.privilege_escalation'),
        DangerRuleSpec('shell.path_outside_workspace', {'root': '/w', 'real': '/private/w'}),
    )
    assert frozen.tools == ('Write', 'Bash', 'Delegate')
    assert host.modules == ('syndica.dev', 'retry', 'safety')
    assert list(host.flags) == ['retry_enabled', 'attempts', 'untrusted_high_risk']
    assert list(host.danger_table or {}) == [
        'shell.privilege_escalation',
        'shell.path_outside_workspace',
        'shell.git_state_change',
    ]


def _hosted(**fields: object) -> dict[str, Any]:
    document = _v2()
    document['host'].update(fields)
    return document


def test_a_host_that_says_nothing_about_its_runtime_was_assembled_like_the_cli():
    host = parse_scenario_document(_v2(), source='captured.json').host

    assert host is not None
    assert (host.orchestration, host.confirmation_timeout) == (True, 300.0)
    assert host.danger_policy == 'table'


def test_the_runtime_assembly_and_the_policy_kind_are_read_from_the_host():
    document = _hosted(
        orchestration=False,
        confirmation_timeout=None,
        danger={'rules': [{'id': 'shell.privilege_escalation'}], 'policy': 'allow_all'},
    )

    host = parse_scenario_document(document, source='captured.json').host

    assert host is not None
    assert (host.orchestration, host.confirmation_timeout) == (False, None)
    assert (host.danger_policy, host.danger_table) == ('allow_all', None)


def test_a_host_without_a_policy_or_a_table_leaves_the_kernel_default():
    document = _hosted(danger={'rules': [{'id': 'shell.privilege_escalation'}]})

    host = parse_scenario_document(document, source='captured.json').host

    assert host is not None and host.danger_policy is None


@pytest.mark.parametrize(
    ('fields', 'received', 'next_action'),
    [
        ({'orchestration': 'yes'}, "'yes'", 'true / false'),
        ({'confirmation_timeout': True}, 'True', '没有确认通道就写 null'),
        ({'confirmation_timeout': 0}, '0', '没有确认通道就写 null'),
        ({'confirmation_timeout': float('inf')}, 'inf', '没有确认通道就写 null'),
        ({'confirmation_timeout': '300'}, "'300'", '没有确认通道就写 null'),
        ({'danger': {'policy': 'mine:Policy'}}, "'mine:Policy'", '改成其中之一'),
        ({'danger': {'policy': 'table'}}, "policy='table'，table 没有", '不写表'),
        (
            {'danger': {'policy': 'allow_all', 'table': {'shell.git_state_change': 'block'}}},
            "policy='allow_all'，table 有",
            '不写表',
        ),
    ],
    ids=[
        'orchestration-text',
        'timeout-bool',
        'timeout-zero',
        'timeout-infinite',
        'timeout-text',
        'policy-unknown',
        'table-without-table',
        'allow-all-with-table',
    ],
)
def test_a_malformed_runtime_assembly_or_policy_is_refused_with_what_to_change(
    fields: dict[str, object], received: str, next_action: str
):
    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(_hosted(**fields), source='captured.json')

    assert received in caught.value.received
    assert next_action in caught.value.next_action


def test_a_v1_document_reads_as_before_with_slot_entry_and_ordered_responses():
    document = parse_scenario_document(_v1(), source='scenario.yaml')

    assert document.version == 1 and document.host is None
    assert (document.entry.mode, document.entry.slot, document.entry.prompt) == (
        'slot',
        'root',
        'go',
    )
    assert document.roles['Root'].manifest == {'name': 'Root', 'description': 'root'}


@pytest.mark.parametrize(
    ('path', 'value'),
    [
        (('topology',), {'slots': [{'name': 'lead', 'agent': 'Lead', 'cardinality': 'on-demand'}]}),
        (('entry',), {'mode': 'send', 'agent': 'Lead', 'prompt': {'payload': '2' * 64}}),
        ((*_TURN, 'content'), [{'type': 'text', 'text': 'hi'}]),
        (
            (*_INSTANCE, 'tool_results', 0),
            {'call': [1, 1, 0], 'raised': {'type': 'a.B', 'message': 'x'}},
        ),
        (('responses',), [{'key': _REQUEST, 'kind': 'timeout'}]),
        (('responses',), [{'kind': 'user_input', 'agent': 'Lead[0]', 'answer': 'yes'}]),
        (('agents', 'Lead', 'frozen', 'impl'), sorted(FROZEN_IMPLS)[0]),
        (('host', 'flags'), {}),
        (('host_actions',), [{'kind': 'shutdown'}, {'kind': 'interrupt'}]),
        ((*_TURN, 'tool_calls', 0, 'id'), ''),
        ((*_INSTANCE, 'tool_results', 0, 'arguments_digest'), 'f' * 64),
        (
            (*_INSTANCE, 'tool_results', 0),
            {
                'call': [1, 1, 0],
                'raised': {'type': 'a.B', 'message': 'x'},
                'arguments_digest': 'e' * 64,
            },
        ),
        ((*_INSTANCE, 'gate', 0, 'outcome'), 'confirmation_aborted'),
        (
            ('host_actions',),
            [{'kind': 'interrupt', 'waiting': [['llm_call', _LEAD, 2, 1], _REQUEST]}],
        ),
        (('host_actions',), [{'kind': 'shutdown', 'waiting': []}]),
        (('expect', 'events'), {'Lead[0]': ['agent_start'], 'Helper[0]': []}),
    ],
    ids=[
        'unrestricted-topology',
        'send-entry-with-payload-prompt',
        'content-parts',
        'tool-raised',
        'timeout-response',
        'ordered-response',
        'builtin-impl',
        'no-flags',
        'shutdown-and-interrupt',
        'empty-call-id',
        'executed-arguments',
        'raised-with-executed-arguments',
        'gate-outcome',
        'interrupt-at-its-waits',
        'shutdown-with-nothing-in-flight',
        'events-by-agent',
    ],
)
def test_each_v2_form_is_accepted(path: tuple[Any, ...], value: object):
    document = _with(_v2(), path, value)
    if path == ('topology',):
        document['entry'] = {'slot': 'lead', 'prompt': 'go'}

    assert parse_scenario_document(document, source='captured.json').version == 2


_INVALID: list[tuple[str, tuple[Any, ...], object, str, str, str]] = [
    ('version', ('version',), 3, '整数 1 或 2', 'version=3', 'version: 2'),
    ('source-trace-id', ('source', 'trace_id'), 'XYZ', '32 个小写十六进制', 'XYZ', 'trace list'),
    ('source-config', ('source', 'config_sha256'), 'abc', 'sha256', 'abc', '不要截断'),
    ('source-argv', ('source', 'argv'), [1], '是字符串', '1', '带引号'),
    ('source-unknown', ('source', 'commit'), 'x', '只含', "'commit'", '删除未知键'),
    ('host-unknown', ('host', 'flag'), {}, '只含', "'flag'", '删除未知键'),
    ('flag-float', ('host', 'flags', 'attempts'), 1.5, '布尔值、整数或字符串', '1.5', 'flags show'),
    (
        'rule-unknown',
        ('host', 'danger', 'rules', 0, 'id'),
        'shell.unknown',
        'shell.git_state_change',
        'shell.unknown',
        '逐字匹配',
    ),
    (
        'rule-duplicate',
        ('host', 'danger', 'rules', 1, 'id'),
        'shell.git_state_change',
        '只出现一次',
        'rules[1]',
        '后一条永远轮不到',
    ),
    (
        'rule-missing-root',
        ('host', 'danger', 'rules', 2, 'params'),
        {'real': '/private/w'},
        "且含 ['root']",
        "缺少 ['root']",
        '字面值',
    ),
    (
        'rule-stray-param',
        ('host', 'danger', 'rules', 0, 'params'),
        {'root': '/w'},
        '只含 []',
        "未知 ['root']",
        '不写 params',
    ),
    (
        'table-value',
        ('host', 'danger', 'table', 'shell.git_state_change'),
        'deny',
        'allow / block',
        'deny',
        '小写',
    ),
    (
        'table-key',
        ('host', 'danger', 'table', ' shell.x'),
        'block',
        '首尾空白',
        "' shell.x'",
        'rule_id',
    ),
    ('output-limit-zero', ('host', 'output_limit'), 0, '不小于 1', '0', '布尔值'),
    ('output-limit-bool', ('host', 'output_limit'), True, '不小于 1', 'True', '布尔值'),
    ('host-facts', ('host', 'host_facts', 'shell'), _DELETE, '必填键', "['shell']", '补齐'),
    ('max-turns', ('host', 'max_turns'), -1, '不小于 1', '-1', '更大的整数'),
    ('no-write', ('host', 'no_write'), 'yes', 'true 或 false', "'yes'", '不带引号'),
    ('modules-repeat', ('host', 'modules'), ['a', 'b', 'a'], '只出现一次', "'a'", '删掉重复'),
    (
        'manifest-and-frozen',
        ('agents', 'Lead', 'manifest'),
        {'name': 'Lead', 'description': 'lead'},
        '恰好有 manifest 与 frozen 之一',
        "['frozen', 'manifest']",
        '捕获的冻结角色用 frozen',
    ),
    ('neither', ('agents', 'Lead', 'frozen'), _DELETE, '恰好有', '[]', '手写的角色用 manifest'),
    (
        'tools-repeat',
        ('agents', 'Lead', 'frozen', 'tools'),
        ['Bash', 'Write', 'Bash'],
        '只出现一次',
        "'Bash'",
        '删掉重复',
    ),
    (
        'impl-path',
        ('agents', 'Lead', 'frozen', 'impl'),
        'os:system',
        '内核内置的类式角色',
        'os:system',
        '不 import',
    ),
    (
        'prompt-ref',
        ('agents', 'Lead', 'frozen', 'system_prompt'),
        {'payload': 'short'},
        'sha256',
        'short',
        '不要截断',
    ),
    ('frozen-turns', ('agents', 'Lead', 'frozen', 'max_turns'), 0, '不小于 1', '0', '更大的整数'),
    ('frozen-tools', ('agents', 'Lead', 'frozen', 'tools'), _DELETE, '必填键', "['tools']", '补齐'),
    ('key-empty', (*_INSTANCE, 'key'), [], '稳定键', '[]', "['host', 'spawn', 0]"),
    ('key-bool', (*_INSTANCE, 'key'), ['host', True], '字符串或整数', 'True', '不是整数'),
    ('turn-alone', (*_TURN, 'attempt'), _DELETE, '一起给', '只给了 turn', '首次是 1'),
    ('turn-order', (*_INSTANCE, 'turns', 2, 'turn'), 1, '严格递增', '[1, 1]', '不要重复'),
    ('turn-mixed', (*_INSTANCE, 'turns', 2), {'content': 'x'}, '都给', '只有 2 项', '按先后消费'),
    ('digest', (*_TURN, 'request_digests'), ['x'], 'sha256', "'x'", '不要截断'),
    ('extra', (*_TURN, 'extra'), [], 'Mapping', 'list', 'JSON 对象'),
    ('trust', (*_TURN, 'trust'), 'maybe', 'trusted / untrusted', 'maybe', '小写'),
    (
        'raise-type',
        (*_INSTANCE, 'turns', 0, 'raises', 'type'),
        'not qualified',
        '模块.限定名',
        'not qualified',
        'LLMResponseError',
    ),
    (
        'raise-status',
        (*_INSTANCE, 'turns', 0, 'raises', 'status_code'),
        True,
        '不小于 0',
        'True',
        '布尔值',
    ),
    (
        'raise-retry',
        (*_INSTANCE, 'turns', 0, 'raises', 'retry_after'),
        'soon',
        '秒数或 null',
        "'soon'",
        '写成数字',
    ),
    ('content', (*_TURN, 'content'), 5, '字符串或 null', '5', '模型会返回的文本'),
    (
        'arguments-both',
        (*_TURN, 'tool_calls', 0, 'arguments_raw'),
        'ls',
        '恰好有 arguments 与 arguments_raw 之一',
        "['arguments', 'arguments_raw']",
        '解析不了时写进 arguments_raw',
    ),
    (
        'arguments-nan',
        (*_TURN, 'tool_calls', 0, 'arguments'),
        {'x': float('nan')},
        '只含 JSON 值',
        'nan',
        'NaN',
    ),
    (
        'arguments-number-key',
        (*_TURN, 'tool_calls', 0, 'arguments'),
        {1: 'one', '1': 'uno'},
        '对象的键是字符串',
        '[1]',
        '数字键',
    ),
    (
        'call-position',
        (*_INSTANCE, 'tool_results', 0, 'call'),
        [1, 1],
        '三个整数',
        '[1, 1]',
        '从 1 数起',
    ),
    (
        'call-cycle',
        (*_INSTANCE, 'tool_results', 0, 'call'),
        [0, 1, 0],
        '不小于 1',
        '0',
        '更大的整数',
    ),
    (
        'both',
        (*_INSTANCE, 'tool_results', 0, 'raised'),
        {'type': 'a.B', 'message': 'x'},
        '不是两者都有',
        'output',
        '只写 call 与 raised',
    ),
    (
        'neither-outcome',
        (*_INSTANCE, 'tool_results', 0, 'output'),
        _DELETE,
        'output',
        "['call', 'meta', 'trust']",
        'tool.result',
    ),
    (
        'outcome-repeat',
        (*_INSTANCE, 'tool_results'),
        [{'call': [1, 1, 0], 'output': 'a'}, {'call': [1, 1, 0], 'output': 'b'}],
        '只出现一次',
        'tool_results[1]',
        '只有一个结局',
    ),
    (
        'skipped-and-run',
        (*_INSTANCE, 'skipped', 0, 'call'),
        [1, 1, 0],
        '要么执行了',
        'skipped[0]',
        '工具没有跑',
    ),
    ('gate-risk', (*_INSTANCE, 'gate', 0, 'risk'), 'medium', 'high / low / none', 'medium', '小写'),
    (
        'gate-missing',
        (*_INSTANCE, 'gate', 0, 'disposition'),
        _DELETE,
        '必填键',
        "['disposition']",
        '补齐',
    ),
    (
        'artifact',
        (*_INSTANCE, 'tool_results', 0, 'artifacts'),
        [{'id': 'a', 'path': 'p', 'kind': 'k', 'mime': 'm', 'version': 'v'}],
        '必填键',
        "['producer']",
        '补齐',
    ),
    ('entry-mode', ('entry', 'mode'), 'spawn', 'slot / query / send', 'spawn', '小写'),
    ('entry-slot', ('entry', 'slot'), 'root', '不给 entry.slot', "'slot'", '直接建 entry.agent'),
    (
        'entry-agent',
        ('entry', 'agent'),
        'Ghost',
        'scenario.agents 里的角色名',
        "'Ghost'；现有角色",
        '改成现有角色名',
    ),
    ('entry-trust', ('entry', 'trust'), 'mostly', 'trusted / untrusted', 'mostly', '小写'),
    ('entry-prompt', ('entry', 'prompt'), ' ', '非空白字符串', "' '", 'payload'),
    (
        'responses-mixed',
        ('responses',),
        [
            {'key': _REQUEST, 'kind': 'user_input', 'answer': 'a'},
            {'kind': 'user_input', 'agent': 'Lead[0]', 'answer': 'b'},
        ],
        '全部按请求的稳定键',
        'responses[1]',
        '不要混用',
    ),
    (
        'responses-repeat',
        ('responses',),
        [
            {'key': _REQUEST, 'kind': 'user_input', 'answer': 'a'},
            {'key': _REQUEST, 'kind': 'user_question', 'answer': 'b'},
        ],
        '只有一条答复',
        'responses[1]',
        '删掉重复',
    ),
    (
        'timeout-answer',
        ('responses', 0, 'kind'),
        'timeout',
        'timeout 没有 answer',
        '有 answer',
        '问题过期',
    ),
    (
        'response-kind',
        ('responses', 0, 'kind'),
        'approve',
        'user_input / user_question',
        'approve',
        '小写',
    ),
    ('abort-key', ('host_actions', 0, 'key'), _DELETE, 'abort 带', '没有 key', '不需要位置'),
    ('interrupt-key', ('host_actions', 1, 'key'), _REQUEST, 'abort 带', '有 key', '不需要位置'),
    (
        'interrupt-twice',
        ('host_actions',),
        [{'kind': 'interrupt'}, {'kind': 'interrupt'}],
        '至多一条 interrupt',
        'host_actions[1]',
        '只会被中断或关闭一次',
    ),
    (
        'abort-twice',
        ('host_actions',),
        [{'kind': 'abort', 'key': _REQUEST}, {'kind': 'abort', 'key': _REQUEST}],
        '至多被中止一次',
        'host_actions[1]',
        '删掉重复',
    ),
    (
        'ids-key',
        ('ids',),
        [{'key': ['host', 'spawn', 0], 'id': 'a'}, {'key': ['host', 'spawn', 0], 'id': 'b'}],
        '稳定键各不相同',
        'ids[1]',
        '注册表也拒绝',
    ),
    (
        'ids-id',
        ('ids',),
        [{'key': ['host', 'spawn', 0], 'id': 'a'}, {'key': ['host', 'spawn', 1], 'id': 'a'}],
        'id 各不相同',
        "id='a'",
        '注册表也拒绝',
    ),
    ('ids-blank', ('ids', 0, 'id'), '', '非空白字符串', "''", '逐字匹配'),
    ('extra-nan', (*_TURN, 'extra'), {'x': float('nan')}, '只含 JSON 值', 'nan', 'NaN'),
    (
        'call-four',
        (*_INSTANCE, 'tool_results', 0, 'call'),
        [1, 1, 0, 0],
        '三个整数',
        '[1, 1, 0, 0]',
        '从 1 数起',
    ),
    (
        'turn-repeat',
        (*_INSTANCE, 'turns', 2),
        {'turn': 1, 'attempt': 2, 'content': 'x'},
        '严格递增',
        '[1, 2]',
        '不要重复',
    ),
    (
        'retry-bool',
        (*_INSTANCE, 'turns', 0, 'raises', 'retry_after'),
        True,
        '秒数或 null',
        'True',
        '写成数字',
    ),
    (
        'answer-missing',
        ('responses', 0, 'answer'),
        _DELETE,
        'timeout 没有 answer',
        '没有 answer',
        '问题过期',
    ),
    ('expect-missing', ('expect',), _DELETE, '必填键', "['expect']", '补齐'),
    ('version-bool', ('version',), True, '整数 1 或 2', 'True', 'version: 2'),
    (
        'payload-suffix',
        ('agents', 'Lead', 'frozen', 'system_prompt'),
        {'payload': 'a' * 64 + '/../x'},
        'sha256',
        '/../x',
        '不要截断',
    ),
    (
        'payload-upper',
        ('agents', 'Lead', 'frozen', 'system_prompt'),
        {'payload': 'A' * 64},
        'sha256',
        'AAAA',
        '大写',
    ),
    (
        'raise-type-tail',
        (*_INSTANCE, 'turns', 0, 'raises', 'type'),
        'a.B c',
        '模块.限定名',
        'a.B c',
        'LLMResponseError',
    ),
    ('extra-number-key', (*_TURN, 'extra'), {1: 'x'}, '对象的键是字符串', '[1]', '数字键'),
    (
        'gate-repeat',
        (*_INSTANCE, 'gate', 1),
        {
            'call': [1, 1, 0],
            'rule_ids': [],
            'risk': 'none',
            'provenance': 'trusted',
            'decided_by': 'policy',
            'disposition': 'allow',
        },
        '只出现一次',
        'gate[1]',
        '只有一个结局',
    ),
    (
        'skipped-repeat',
        (*_INSTANCE, 'skipped', 1),
        {'call': [1, 1, 1], 'output': 'again'},
        '只出现一次',
        'skipped[1]',
        '只有一个结局',
    ),
    ('key-blank-origin', (*_INSTANCE, 'key'), ['', 1], '稳定键', "['', 1]", "['host', 'spawn', 0]"),
    (
        'key-number-origin',
        (*_INSTANCE, 'key'),
        [1, 'spawn'],
        '稳定键',
        "[1, 'spawn']",
        "['host', 'spawn', 0]",
    ),
    (
        'entry-query-agentless',
        ('entry', 'agent'),
        _DELETE,
        '给 entry.agent',
        "['mode', 'prompt', 'trust']",
        '直接建 entry.agent',
    ),
    ('attempt-alone', (*_TURN, 'turn'), _DELETE, '一起给', '只给了 attempt', '首次是 1'),
    (
        'artifact-field-type',
        (*_INSTANCE, 'tool_results', 0, 'artifacts'),
        [{'id': 5, 'path': 'p', 'kind': 'k', 'mime': 'm', 'version': 'v', 'producer': 'x'}],
        '是字符串',
        '5',
        '带引号',
    ),
    (
        'trace-id-upper',
        ('source', 'trace_id'),
        'AB12' * 8,
        '32 个小写十六进制',
        'AB12',
        'trace list',
    ),
    ('source-version', ('source', 'version'), 5, '是字符串', '5', '带引号'),
    (
        'decided-by',
        (*_INSTANCE, 'gate', 0, 'decided_by'),
        'human',
        'flag / policy',
        'human',
        '小写',
    ),
    (
        'raises-and-content',
        (*_INSTANCE, 'turns', 0, 'content'),
        'partial',
        '不是两者都有',
        "['content']",
        '只留 raises',
    ),
    ('attempt-gap', (*_TURN, 'attempt'), 3, '从 1 起逐个加 1', '[1, 3]', '不要跳号'),
    (
        'retry-after-reply',
        (*_INSTANCE, 'turns', 0),
        {'turn': 1, 'attempt': 1, 'content': 'early'},
        '只有失败之后才有下一次调用',
        '[1, 2]',
        '不再重试',
    ),
    (
        'position-no-turn',
        (*_INSTANCE, 'tool_results', 0, 'call'),
        [1, 9, 0],
        '第 9 轮',
        '没有回复',
        '批内位置从 0 数起',
    ),
    (
        'position-past-calls',
        (*_INSTANCE, 'tool_results', 0, 'call'),
        [1, 1, 5],
        '第 1 轮',
        '只有 2 个工具调用',
        '批内位置从 0 数起',
    ),
    (
        'gate-on-text-reply',
        (*_INSTANCE, 'gate', 0, 'call'),
        [1, 2, 0],
        '第 2 轮',
        '只有 0 个工具调用',
        '批内位置从 0 数起',
    ),
    (
        'retry-negative',
        (*_INSTANCE, 'turns', 0, 'raises', 'retry_after'),
        -5,
        '有限秒数',
        '-5',
        '写成数字',
    ),
    (
        'retry-infinite',
        (*_INSTANCE, 'turns', 0, 'raises', 'retry_after'),
        float('inf'),
        '有限秒数',
        'inf',
        '写成数字',
    ),
    (
        'raises-typo',
        (*_INSTANCE, 'turns', 0, 'raises'),
        {'typ': 'a.B', 'message': 'x'},
        '{type, message',
        "['message', 'typ']",
        '写 type',
    ),
    (
        'arguments-digest',
        (*_INSTANCE, 'tool_results', 0, 'arguments_digest'),
        'ABC',
        'arguments_digest 是 64 个小写十六进制',
        'ABC',
        '不要截断',
    ),
    (
        'gate-outcome',
        (*_INSTANCE, 'gate', 0, 'outcome'),
        'done',
        'outcome 是',
        "'done'",
        '逐字匹配',
    ),
    (
        'abort-waiting',
        ('host_actions', 0, 'waiting'),
        [['llm_call', _LEAD, 1, 1]],
        'waiting 只属于 interrupt / shutdown',
        'abort 带了 waiting',
        '删掉 waiting',
    ),
    (
        'waiting-twice',
        ('host_actions', 1, 'waiting'),
        [_REQUEST, _REQUEST],
        'waiting 里的键各不相同',
        '重复',
        '删掉重复',
    ),
    (
        'waiting-not-a-key',
        ('host_actions', 1, 'waiting'),
        [[1, 2]],
        '内核的稳定键',
        '[1, 2]',
        '原样抄写',
    ),
    (
        'events-by-agent',
        ('expect', 'events'),
        {'Lead[0]': 'agent_start'},
        'expect.events.Lead[0]',
        'agent_start',
        '',
    ),
]


@pytest.mark.parametrize(
    ('path', 'value', 'expected', 'received', 'next_action'),
    [case[1:] for case in _INVALID],
    ids=[case[0] for case in _INVALID],
)
def test_each_malformed_v2_value_is_refused_with_where_and_what_to_do(
    path: tuple[Any, ...], value: object, expected: str, received: str, next_action: str
):
    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(_with(_v2(), path, value), source='captured.json')

    assert expected in caught.value.expected
    assert received in caught.value.received
    assert next_action in caught.value.next_action


def test_a_slot_entry_without_a_topology_says_to_add_one():
    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(
            _with(_v2(), ('entry',), {'slot': 'root', 'prompt': 'go'}), source='captured.json'
        )

    assert '文档有 topology' in caught.value.expected
    assert 'topology.slots' in caught.value.next_action


def test_an_instance_key_is_unique_across_roles():
    document = _v2()
    document['agents']['Helper'] = copy.deepcopy(document['agents']['Lead'])

    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(document, source='captured.json')

    assert 'agents.Lead.instances[0].key' in caught.value.received
    assert '不要复制实例' in caught.value.next_action


@pytest.mark.parametrize(
    ('path', 'value', 'newer'),
    [
        (('host',), {}, "['host']"),
        (('agents', 'Root', 'frozen'), {}, "['frozen']"),
        (('agents', 'Root', 'instances', 0, 'key'), ['host', 'spawn', 0], "['key']"),
        (('agents', 'Root', 'instances', 0, 'messages'), [], "['messages']"),
        (('agents', 'Root', 'instances', 0, 'continued'), [], "['continued']"),
        (('agents', 'Root', 'instances', 0, 'cut'), [], "['cut']"),
        (('agents', 'Root', 'instances', 0, 'turns', 0, 'turn'), 1, "['turn']"),
        (('agents', 'Root', 'instances', 0, 'turns', 0, 'request_prefix'), 1, "['request_prefix']"),
        (('entry', 'mode'), 'slot', "['mode']"),
    ],
    ids=['root', 'agent', 'instance', 'messages', 'continued', 'cut', 'turn', 'prefix', 'entry'],
)
def test_a_v2_key_in_a_v1_document_points_at_version_two(
    path: tuple[Any, ...], value: object, newer: str
):
    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(_with(_v1(), path, value), source='scenario.yaml')

    assert newer in caught.value.received
    assert '把 version 改成 2' in caught.value.next_action


_FIRST = 'ab' * 16
_SECOND = 'cd' * 16
_THIRD = 'ef' * 16


def _injecting(messages: object) -> dict[str, Any]:
    document = _with(_v2(), (*_INSTANCE, 'messages'), messages)
    document['ids'] += [
        {'key': ['host', 'message', 0], 'id': _FIRST},
        {'key': ['tool_call', _LEAD, 1, 1, 0, 'message', 0], 'id': _SECOND},
        {'key': ['tool_call', _LEAD, 1, 1, 0, 'message', 1], 'id': _THIRD},
    ]
    return document


def test_messages_read_into_injections_per_turn_sorted_by_turn_in_the_order_written():
    document = parse_scenario_document(
        _injecting([{'turn': 4, 'ids': [_THIRD]}, {'turn': 2, 'ids': [_SECOND, _FIRST]}]),
        source='captured.json',
    )

    [instance] = document.roles['Lead'].instances
    assert instance.messages == {2: (_SECOND, _FIRST), 4: (_THIRD,)}
    assert list(instance.messages or {}) == [2, 4]


def test_an_instance_without_messages_has_no_record_and_an_empty_list_records_none():
    absent = parse_scenario_document(_v2(), source='x.json').roles['Lead'].instances[0]
    empty = parse_scenario_document(_injecting([]), source='x.json').roles['Lead'].instances[0]

    assert absent.messages is None
    assert empty.messages == {}


@pytest.mark.parametrize(
    ('messages', 'received', 'next_action'),
    [
        ([{'turn': 0, 'ids': [_FIRST]}], '0', '1 或更大'),
        ([{'turn': True, 'ids': [_FIRST]}], 'True', 'yes / on / true'),
        ([{'turn': 1, 'ids': []}], '空列表', '删掉这一项'),
        ([{'turn': 1, 'ids': [_FIRST]}, {'turn': 1, 'ids': [_SECOND]}], '第 1 轮', '合进一项'),
        ([{'turn': 1, 'ids': [_FIRST]}, {'turn': 2, 'ids': [_FIRST]}], _FIRST, '只在一轮'),
        ([{'turn': 1, 'ids': ['f' * 32]}], 'f' * 32, '原样抄写 ids 与 messages'),
        ([{'turn': 1, 'ids': [_FIRST], 'when': 3}], "['when']", 'messages[0]'),
    ],
    ids=['turn-zero', 'turn-bool', 'no-ids', 'turn-twice', 'id-twice', 'unknown-id', 'extra-key'],
)
def test_malformed_messages_are_refused_with_what_to_change(
    messages: object, received: str, next_action: str
):
    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(_injecting(messages), source='captured.json')

    assert received in caught.value.received
    assert next_action in caught.value.next_action + caught.value.expected


def _continuing(continued: object, messages: object = None) -> dict[str, Any]:
    if messages is None:
        messages = [{'turn': 2, 'ids': [_FIRST]}, {'turn': 4, 'ids': [_SECOND, _THIRD]}]
    document = _injecting(messages)
    document['agents']['Lead']['instances'][0]['continued'] = continued
    return document


def test_continued_turns_read_into_a_set_of_turns_that_opened_a_cycle_without_a_pause():
    document = parse_scenario_document(_continuing([4, 2]), source='captured.json')

    [instance] = document.roles['Lead'].instances
    assert instance.continued == {2, 4}


@pytest.mark.parametrize(
    ('continued', 'messages', 'received', 'next_action'),
    [
        ([1], None, '1', '2 或更大'),
        ([True], None, 'True', 'yes / on / true'),
        ([2, 2], None, '第 2 轮出现了不止一次', '删掉重复的那一项'),
        ([3], None, '第 3 轮在 messages 里没有', '原样抄写 messages 与 continued'),
        ([2], [], '第 2 轮在 messages 里没有', '原样抄写 messages 与 continued'),
    ],
    ids=['first-cycle', 'bool', 'twice', 'no-injection', 'nothing-injected'],
)
def test_malformed_continued_turns_are_refused_with_what_to_change(
    continued: object, messages: object, received: str, next_action: str
):
    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(_continuing(continued, messages), source='captured.json')

    assert received in caught.value.received
    assert next_action in caught.value.next_action + caught.value.expected


def test_continued_turns_without_any_messages_record_are_refused():
    document = _v2()
    document['agents']['Lead']['instances'][0]['continued'] = [2]

    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(document, source='captured.json')

    assert '原样抄写 messages 与 continued' in caught.value.next_action


def _cutting(cut: object, **instance: object) -> dict[str, Any]:
    document = _v2()
    [lead] = document['agents']['Lead']['instances']
    turn = {
        'turn': 1,
        'attempt': 1,
        'tool_calls': [
            {'id': 'c1', 'name': 'Bash', 'arguments': {'command': 'sleep 9'}},
            {'id': 'c2', 'name': 'Bash', 'arguments': {'command': 'sleep 9'}},
        ],
    }
    document['agents']['Lead']['instances'] = [
        {'key': lead['key'], 'turns': [turn], 'cut': cut, **instance}
    ]
    return document


def test_cut_calls_read_into_positions_in_the_order_written():
    document = parse_scenario_document(_cutting([[1, 1, 1], [1, 1, 0]]), source='captured.json')

    [instance] = document.roles['Lead'].instances
    assert instance.cut == ((1, 1, 1), (1, 1, 0))


@pytest.mark.parametrize(
    ('cut', 'instance', 'received', 'next_action'),
    [
        ([[1, 1, 0], [1, 1, 0]], {}, '[1, 1, 0] 重复', '删掉重复的那一项'),
        ([[1, 1, 2]], {}, '[1, 1, 2]', '照捕获的文档'),
        (
            [[1, 1, 0]],
            {'tool_results': [{'call': [1, 1, 0], 'output': 'done'}]},
            'cut[0]=[1, 1, 0] 也有结局',
            '什么也没有交回',
        ),
        (
            [[1, 1, 1]],
            {'skipped': [{'call': [1, 1, 1], 'output': 'refused'}]},
            'cut[0]=[1, 1, 1] 也有结局',
            '什么也没有交回',
        ),
    ],
    ids=['twice', 'no-such-call', 'with-an-outcome', 'also-skipped'],
)
def test_malformed_cut_calls_are_refused_with_what_to_change(
    cut: object, instance: dict[str, object], received: str, next_action: str
):
    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(_cutting(cut, **instance), source='captured.json')

    assert received in caught.value.received
    assert next_action in caught.value.next_action + caught.value.expected


def _prefixed(**turns: dict[str, object]) -> dict[str, Any]:
    """在基准文档的三项回复（重试前失败的、带两个指纹的、最后那一项）上改：键是它们的下标。"""
    document = _v2()
    [lead] = document['agents']['Lead']['instances']
    for index, fields in turns.items():
        lead['turns'][int(index.removeprefix('t'))].update(fields)
    return document


def _digests_of(document: dict[str, Any]) -> list[tuple[str, ...] | None]:
    parsed = parse_scenario_document(document, source='captured.json')
    return [reply.request_digests for reply in parsed.roles['Lead'].instances[0].replies]


def test_a_request_prefix_expands_into_the_first_units_of_the_previous_request():
    document = _prefixed(t2={'request_prefix': 2, 'request_digests': ['f' * 64]})

    assert _digests_of(document) == [None, ('b' * 64, 'd' * 64), ('b' * 64, 'd' * 64, 'f' * 64)]


def test_a_request_prefix_counts_from_the_last_request_that_had_units():
    document = _prefixed(
        t0={'request_digests': ['a' * 64, 'c' * 64, 'e' * 64]},
        t1={'request_prefix': 1, 'request_digests': []},
        t2={'request_prefix': 1},
    )
    document['agents']['Lead']['instances'][0]['turns'][2]['request_digests'] = ['0' * 64]

    assert _digests_of(document) == [
        ('a' * 64, 'c' * 64, 'e' * 64),
        ('a' * 64,),
        ('a' * 64, '0' * 64),
    ]


@pytest.mark.parametrize(
    ('turns', 'received', 'next_action'),
    [
        ({'t2': {'request_prefix': 2}}, '只有 request_prefix', '原样抄写这一项'),
        (
            {'t2': {'request_prefix': 3, 'request_digests': []}},
            'request_prefix=3，上一次只有 2 个单元',
            '前缀就对不上了',
        ),
        (
            {'t1': {'request_prefix': 1}},
            'request_prefix=1，上一次只有 0 个单元',
            '前缀就对不上了',
        ),
        ({'t2': {'request_prefix': 0, 'request_digests': []}}, '0', '1 或更大'),
        ({'t2': {'request_prefix': True, 'request_digests': []}}, 'True', 'yes / on / true'),
    ],
    ids=['without-digests', 'longer-than-before', 'nothing-before', 'zero', 'bool'],
)
def test_a_malformed_request_prefix_is_refused_with_what_to_change(
    turns: dict[str, dict[str, object]], received: str, next_action: str
):
    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(_prefixed(**turns), source='captured.json')

    assert received in caught.value.received
    assert next_action in caught.value.next_action + caught.value.expected


_ROOT_TURN = ('agents', 'Root', 'instances', 0, 'turns', 0)


@pytest.mark.parametrize(
    ('path', 'value', 'expected', 'next_action'),
    [
        ((*_ROOT_TURN, 'content'), {'payload': 'a' * 64}, '字符串或 null', '模型会返回的文本'),
        (
            (*_ROOT_TURN, 'content'),
            [{'type': 'text', 'text': 'x'}],
            '字符串或 null',
            '模型会返回的文本',
        ),
        (('entry', 'prompt'), {'payload': 'a' * 64}, '非空白字符串', '逐字匹配'),
        (
            (*_ROOT_TURN, 'raises'),
            {'type': 'builtins.RuntimeError', 'message': 'x'},
            "['kind', 'message']",
            '把 version 改成 2',
        ),
        (
            (*_ROOT_TURN, 'tool_calls'),
            [{'id': 'c', 'name': 'Read', 'arguments': '{"path": "a'}],
            'arguments 是 Mapping',
            'JSON 对象',
        ),
        (('agents', 'Root', 'manifest', 'impl'), 'pkg.mod:Agent', '声明式 manifest', '删除 impl'),
        (
            ('topology', 'slots', 0, 'exit_mode'),
            'idle',
            'singleton + zombie',
            'version: 2',
        ),
    ],
    ids=[
        'payload-content',
        'content-parts',
        'payload-prompt',
        'typed-raise',
        'raw-arguments',
        'manifest-impl',
        'unrestricted-slot',
    ],
)
def test_v2_value_forms_are_refused_in_a_v1_document(
    path: tuple[Any, ...], value: object, expected: str, next_action: str
):
    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(_with(_v1(), path, value), source='scenario.yaml')

    assert expected in caught.value.expected
    assert next_action in caught.value.next_action


def test_v1_call_ids_stay_unique_while_captured_ones_may_repeat():
    v1 = _with(
        _v1(),
        ('agents', 'Root', 'instances', 0, 'turns'),
        [
            {'tool_calls': [{'id': 'same', 'name': 'Read', 'arguments': {}}]},
            {'tool_calls': [{'id': 'same', 'name': 'Read', 'arguments': {}}]},
        ],
    )

    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(v1, source='scenario.yaml')

    assert "'same' 重复" in caught.value.received
    assert parse_scenario_document(_v2(), source='captured.json').version == 2


def test_every_whitelisted_impl_is_a_kernel_agent_class():
    for path in sorted(FROZEN_IMPLS):
        module, _, name = path.partition(':')
        found = getattr(importlib.import_module(module), name)
        assert module.startswith('syndica.kernel.') and issubclass(found, BaseAgent), path


def test_the_rebuildable_rules_are_exactly_the_kernels_rules():
    factories = (
        git_state_change_rule(),
        path_outside_workspace_rule('/'),
        workspace_root_removal_rule('/'),
    )
    kernel_rules = {rule.rule_id for rule in (*DEFAULT_RULES, *factories)}

    assert set(RULE_PARAMETERS) == kernel_rules
    assert {rule_id for rule_id, params in RULE_PARAMETERS.items() if params} == {
        'shell.path_outside_workspace',
        'shell.workspace_root_removal',
    }


def test_keyed_and_unkeyed_instances_do_not_mix():
    document = _v2()
    document['agents']['Helper'] = {
        'manifest': {'name': 'Helper', 'description': 'helps'},
        'instances': [{'turns': [{'content': 'done'}]}],
    }

    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(document, source='captured.json')

    assert '都给稳定键 key' in caught.value.expected
    assert '2 个实例里只有 1 个' in caught.value.received


def test_outcomes_by_call_position_need_keyed_turns():
    document = _v2()
    instance = document['agents']['Lead']['instances'][0]
    for reply in instance['turns']:
        del reply['turn'], reply['attempt']
    del instance['key'], document['ids']

    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(document, source='captured.json')

    assert 'turns 每一项都给 turn 与 attempt' in caught.value.expected


def test_the_document_cannot_be_changed_after_parsing_nor_through_its_input():
    v1 = _v1()
    v1['agents']['Root']['manifest']['tools'] = ['Read']
    v2 = _with(_v2(), (*_INSTANCE, 'turns', 2, 'content'), [{'type': 'text', 'text': 'hi'}])

    first = parse_scenario_document(v1, source='scenario.yaml')
    second = parse_scenario_document(v2, source='captured.json')
    v1['agents']['Root']['manifest']['tools'].append('Bash')

    manifest = first.roles['Root'].manifest
    reply = second.roles['Lead'].instances[0].replies[1]
    parts = second.roles['Lead'].instances[0].replies[2].content
    assert manifest is not None and manifest['tools'] == ('Read',)
    assert isinstance(parts, tuple) and isinstance(parts[0], MappingProxyType)
    with pytest.raises(TypeError):
        cast(Any, reply.extra)['usage'] = {}
    with pytest.raises(TypeError):
        cast(Any, manifest)['tools'] = ()


def test_a_value_that_contains_itself_is_refused_instead_of_recursing():
    loop: list[object] = []
    loop.append(loop)

    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(_with(_v2(), (*_TURN, 'extra'), {'loop': loop}), source='x.json')

    assert '嵌套不超过 64 层' in caught.value.expected


def test_a_document_with_too_many_json_nodes_is_refused(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(scenario_schema, '_MAX_NODES', 50)

    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(
            _with(_v2(), (*_TURN, 'extra'), {'many': list(range(60))}), source='x.json'
        )

    assert '不超过 50 个节点' in caught.value.expected
    assert parse_scenario_document(_v2(), source='x.json').version == 2


def _v1_roles(*names: str) -> dict[str, Any]:
    document = _v1()
    document['agents'] = {
        name: {
            'manifest': {'name': name, 'description': name},
            'instances': [{'turns': [{'content': 'done'}]}],
        }
        for name in names
    }
    document['topology']['slots'][0]['agent'] = names[0]
    return document


@pytest.mark.parametrize(
    ('change', 'expected', 'received'),
    [
        (
            lambda d: d['agents']['Root']['instances'].__setitem__(
                0,
                {
                    'turns': [
                        {'tool_calls': [{'id': '', 'name': 'Read', 'arguments': {}}]},
                    ]
                },
            ),
            '非空白字符串',
            "''",
        ),
        (lambda d: d.pop('responses'), '必填键', "['responses']"),
        (lambda d: d['entry'].__setitem__('slot', 'ghost'), 'topology.slots 中的槽名', "'ghost'"),
        (lambda d: d['expect'].__setitem__('final_output', {'Root[0]': 5}), '值都是字符串', '5'),
        (lambda d: d.__setitem__('agents', {}), '至少声明一个角色', '空 Mapping'),
        (lambda d: d['agents']['Root'].__setitem__('instances', []), '至少有一份脚本', '空列表'),
        (
            lambda d: d['topology']['slots'][0].__setitem__('agent', 'Ghost'),
            '提供 instances 脚本',
            "['Ghost']",
        ),
        (
            lambda d: d['topology']['slots'][0].__setitem__('extra_tools', ['Bash']),
            'singleton + zombie',
            "extra_tools=('Bash',)",
        ),
        (
            lambda d: d['topology']['slots'][0].update(cardinality='on-demand', boot='lazy'),
            'singleton + zombie',
            "cardinality='on-demand'",
        ),
    ],
    ids=[
        'blank-call-id',
        'no-responses',
        'unknown-entry-slot',
        'expect-not-text',
        'no-agents',
        'no-instances',
        'slot-without-script',
        'slot-extra-tools',
        'slot-on-demand',
    ],
)
def test_each_v1_rule_that_moved_into_the_kernel_still_holds(
    change: Any, expected: str, received: str
):
    document = _v1()
    change(document)

    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(document, source='scenario.yaml')

    assert expected in caught.value.expected
    assert received in caught.value.received


def test_v1_call_ids_are_unique_across_roles():
    document = _v1_roles('Root', 'Other')
    for role in ('Root', 'Other'):
        document['agents'][role]['instances'][0]['turns'] = [
            {'tool_calls': [{'id': 'same', 'name': 'Read', 'arguments': {}}]}
        ]

    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(document, source='scenario.yaml')

    assert 'agents.Other' in caught.value.received and "'same' 重复" in caught.value.received


def test_roles_and_responses_keep_the_order_they_were_written_in():
    document = _v1_roles('Zeta', 'Alpha', 'Mid')
    document['responses'] = [
        {'kind': 'user_question', 'agent': 'Zeta[0]', 'answer': 'b'},
        {'kind': 'confirmation', 'agent': 'Zeta[0]', 'answer': 'approve', 'tool_call_id': 'x'},
        {'kind': 'user_input', 'agent': 'Zeta[0]', 'answer': 'a'},
    ]

    parsed = parse_scenario_document(document, source='scenario.yaml')

    assert list(parsed.roles) == ['Zeta', 'Alpha', 'Mid']
    assert [response.kind for response in parsed.responses] == [
        'user_question',
        'confirmation',
        'user_input',
    ]


def test_the_version_hint_appears_only_when_every_unknown_key_is_a_v2_key():
    with pytest.raises(ConfigError) as mixed:
        parse_scenario_document(_with(_with(_v1(), ('host',), {}), ('bogus',), 1), source='x.yaml')
    with pytest.raises(ConfigError) as keyed:
        parse_scenario_document(
            _with(
                _v1(),
                ('responses',),
                [{'key': ['host', 'request', 0], 'kind': 'user_input', 'answer': 'a'}],
            ),
            source='x.yaml',
        )

    assert '删除未知键' in mixed.value.next_action
    assert '把 version 改成 2' in keyed.value.next_action


def test_the_v2_additions_read_into_their_fields():
    document = _with(_v2(), (*_INSTANCE, 'tool_results', 0, 'arguments_digest'), 'f' * 64)
    document = _with(document, (*_INSTANCE, 'gate', 0, 'outcome'), 'rejected')
    document = _with(
        document, ('host_actions', 1), {'kind': 'interrupt', 'waiting': [_REQUEST, ['b', 1]]}
    )
    document = _with(document, ('expect', 'events'), {'Lead[0]': ['agent_start', 'turn_start']})

    parsed = parse_scenario_document(document, source='captured.json')

    [instance] = parsed.roles['Lead'].instances
    assert instance.tool_results[0].arguments_digest == 'f' * 64
    assert instance.gate[0].outcome == 'rejected'
    assert parsed.host_actions[1] == HostAction('interrupt', None, (tuple(_REQUEST), ('b', 1)))
    assert parsed.expect.events_by_agent == {'Lead[0]': ('agent_start', 'turn_start')}
    assert parsed.expect.events == () and 'events' in parsed.expect.specified


def test_events_by_agent_are_a_v2_form():
    document = _with(_v1(), ('expect',), {'events': {'Root[0]': ['agent_start']}})

    with pytest.raises(ConfigError) as caught:
        parse_scenario_document(document, source='scenario.yaml')

    assert 'expect.events' in caught.value.expected


def test_v1_error_kinds_name_the_classes_the_kernel_actually_raises():
    from syndica.kernel import ContextOverflowError, LLMResponseError

    named = {
        kind: scenario_schema.parse_scenario_document(
            _with(
                _v1(),
                ('agents', 'Root', 'instances', 0, 'turns', 0),
                {'raises': {'kind': kind, 'message': 'x'}},
            ),
            source='scenario.yaml',
        )
        .roles['Root']
        .instances[0]
        .replies[0]
        .raises
        for kind in ('context_overflow', 'llm_response_error', 'runtime_error')
    }

    assert {kind: None if error is None else error.type for kind, error in named.items()} == {
        kind: f'{cls.__module__}.{cls.__qualname__}'
        for kind, cls in (
            ('context_overflow', ContextOverflowError),
            ('llm_response_error', LLMResponseError),
            ('runtime_error', RuntimeError),
        )
    }
