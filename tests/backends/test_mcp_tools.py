"""MCP 工具包装：名字、schema、风险、结果。判定与开发期处置表对得上。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from syndica.backends.mcp.tools import (
    MCP_EXTERNAL_TOOL,
    McpJudge,
    RemoteTool,
    build_tools,
    clean_description,
    public_name,
    render_result,
)
from syndica.cli.policy import dev_table
from syndica.kernel import (
    ConfigError,
    DangerRequest,
    DangerVerdict,
    Disposition,
    RiskLevel,
    ToolCall,
    TrustLevel,
)
from syndica.kernel.danger import RuleTablePolicy, disposition_of, require_own_command_args

_GOLDEN = Path(__file__).parent / 'testdata' / 'mcp_echo_tool.json'


def _echo(**overrides: object) -> RemoteTool:
    schema: dict[str, Any] = {
        'type': 'object',
        'properties': {'text': {'type': 'string'}},
        'required': ['text'],
    }
    fields: dict[str, Any] = {
        'name': 'echo',
        'description': 'echo text',
        'input_schema': schema,
    }
    fields.update(overrides)
    return RemoteTool(**fields)


async def _unused(remote: str, arguments: Mapping[str, object]) -> Any:
    del remote, arguments
    raise AssertionError('invoke')


def test_public_names_are_sanitized_and_capped():
    assert public_name('docs', 'read file') == 'docs__read_file'
    assert public_name('docs', 'a' * 80) == ('docs__' + 'a' * 80)[:64]
    assert len(public_name('docs', 'a' * 80)) == 64


def test_descriptions_drop_controls_then_cap_at_1024():
    assert clean_description('hello\nworld\x00') == 'helloworld'
    assert clean_description(None) == ''
    assert clean_description('a' * 1100) == 'a' * 1024


def test_wrapped_schema_matches_the_golden_and_declares_every_property():
    tools, excluded = build_tools(
        'docs', (_echo(),), high=frozenset(), low=frozenset(), invoke=_unused
    )
    tool = tools['docs__echo']
    golden = json.loads(_GOLDEN.read_text(encoding='utf-8'))
    schema = tool.schema
    assert schema == golden
    assert list(schema) == ['name', 'description', 'parameters']
    assert list(schema['parameters']) == ['type', 'properties', 'required']
    assert tool.__tool_command_args__ == ('text',)
    assert tool.__tool_external_input__ is True
    assert tool.is_concurrency_safe({}) is False
    require_own_command_args(tool, tool.name, 'test')
    assert excluded == ()


def test_local_refs_are_inlined_and_defs_are_dropped():
    remote = RemoteTool(
        name='bag',
        description='bag',
        input_schema={
            'type': 'object',
            'properties': {'item': {'$ref': '#/$defs/Item'}},
            '$defs': {'Item': {'type': 'object', 'properties': {'n': {'type': 'integer'}}}},
        },
    )
    tools, excluded = build_tools(
        'docs', (remote,), high=frozenset(), low=frozenset(), invoke=_unused
    )
    parameters = tools['docs__bag'].schema['parameters']
    assert '$defs' not in parameters
    assert parameters['properties']['item'] == {
        'type': 'object',
        'properties': {'n': {'type': 'integer'}},
    }
    assert excluded == ()


@pytest.mark.parametrize(
    ('schema', 'reason'),
    [
        ({'$ref': '#'}, 'cyclic-$ref'),
        ({'$ref': 'https://example.com/schema.json'}, 'remote-$ref'),
        ({'$ref': '#/$defs/Missing'}, 'missing-$ref'),
        ('not-a-schema', 'bad-schema'),
    ],
)
def test_unrefinable_schemas_are_excluded_and_logged(
    caplog: pytest.LogCaptureFixture, schema: object, reason: str
):
    remote = RemoteTool(name='bad', description='bad', input_schema=schema)
    with caplog.at_level(logging.ERROR):
        tools, excluded = build_tools(
            'docs', (remote,), high=frozenset(), low=frozenset(), invoke=_unused
        )
    assert tools == {}
    assert excluded[0].reason == reason
    assert excluded[0].name == 'bad'
    records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert records
    assert isinstance(records[0].args, tuple)
    assert records[0].args[2] == reason


def test_a_schema_deeper_than_the_cap_is_excluded():
    node: dict[str, Any] = {'type': 'string'}
    for _ in range(40):
        node = {'type': 'object', 'properties': {'x': node}}
    remote = RemoteTool(name='deep', description='deep', input_schema=node)
    tools, excluded = build_tools(
        'docs', (remote,), high=frozenset(), low=frozenset(), invoke=_unused
    )
    assert tools == {}
    assert excluded[0].reason == 'too-deep'


def test_sanitized_names_that_collide_name_both_tools():
    left = RemoteTool(
        name='read file',
        description='',
        input_schema={'type': 'object', 'properties': {}},
    )
    right = RemoteTool(
        name='read_file',
        description='',
        input_schema={'type': 'object', 'properties': {}},
    )
    with pytest.raises(ConfigError, match='docs__read_file') as exc:
        build_tools('docs', (left, right), high=frozenset(), low=frozenset(), invoke=_unused)
    text = str(exc.value)
    assert 'read file' in text
    assert 'read_file' in text


@pytest.mark.parametrize(
    ('high', 'low', 'read_only', 'destructive', 'level'),
    [
        (frozenset({'echo'}), frozenset(), True, None, RiskLevel.HIGH),
        (frozenset(), frozenset({'echo'}), None, True, RiskLevel.LOW),
        (frozenset(), frozenset(), True, None, RiskLevel.LOW),
        (frozenset(), frozenset(), True, True, RiskLevel.HIGH),
        (frozenset(), frozenset(), None, None, RiskLevel.HIGH),
    ],
)
def test_risk_prefers_config_then_the_read_only_annotation(
    high: frozenset[str],
    low: frozenset[str],
    read_only: object,
    destructive: object,
    level: RiskLevel,
):
    remote = _echo(read_only_hint=read_only, destructive_hint=destructive)
    tools, _ = build_tools('docs', (remote,), high=high, low=low, invoke=_unused)
    assert type(tools['docs__echo']).__dict__['__tool_mcp_risk__'] is level


def test_shell_rules_see_arguments_before_the_mcp_rule_and_numbers_do_not_crash_the_judge():
    remote = RemoteTool(
        name='run',
        description='run',
        input_schema={
            'type': 'object',
            'properties': {'command': {'type': 'string'}, 'count': {'type': 'integer'}},
        },
    )
    tools, _ = build_tools('docs', (remote,), high=frozenset(), low=frozenset(), invoke=_unused)
    tool = tools['docs__run']
    judge = McpJudge()
    assert judge.judge(tool, {'command': 'sudo true'}).rule_id == 'shell.privilege_escalation'
    numeric = judge.judge(tool, {'count': 3})
    assert numeric.rule_id == MCP_EXTERNAL_TOOL
    assert numeric.level is RiskLevel.HIGH
    assert judge.rules[-1].rule_id == MCP_EXTERNAL_TOOL


def test_dev_mode_confirms_the_mcp_rule_and_untrusted_high_risk_blocks_first():
    assert dev_table()[MCP_EXTERNAL_TOOL] is Disposition.CONFIRM
    policy = RuleTablePolicy(dev_table())
    call = ToolCall(id='c', name='docs__write', arguments='{}')
    high = DangerVerdict(rule_id=MCP_EXTERNAL_TOOL, level=RiskLevel.HIGH, reason='外部')
    low = DangerVerdict(rule_id=MCP_EXTERNAL_TOOL, level=RiskLevel.LOW, reason='外部')
    trusted = DangerRequest(verdict=high, call=call, provenance=TrustLevel.TRUSTED)
    untrusted = DangerRequest(verdict=high, call=call, provenance=TrustLevel.UNTRUSTED)
    low_untrusted = DangerRequest(verdict=low, call=call, provenance=TrustLevel.UNTRUSTED)
    assert disposition_of(policy, trusted) is Disposition.CONFIRM
    assert disposition_of(policy, untrusted) is Disposition.BLOCK
    assert disposition_of(policy, low_untrusted) is Disposition.CONFIRM


async def test_arguments_are_validated_before_the_call_and_results_stay_untrusted():
    seen: list[dict[str, object]] = []

    async def invoke(remote: str, arguments: Mapping[str, object]) -> Any:
        seen.append(dict(arguments))
        assert remote == 'echo'
        from syndica.kernel import ToolResult

        return ToolResult(output='ok', trust=TrustLevel.TRUSTED)

    tools, _ = build_tools('docs', (_echo(),), high=frozenset(), low=frozenset(), invoke=invoke)
    tool = tools['docs__echo']
    rejected = await tool.execute(text=1)
    assert seen == []
    assert 'inputSchema' in rejected.output
    assert 'docs__echo' in rejected.output
    assert rejected.trust is TrustLevel.UNTRUSTED
    accepted = await tool.execute(text='ping')
    assert seen == [{'text': 'ping'}]
    assert accepted.output == 'ok'


def test_results_keep_text_mark_errors_and_omit_binary_payloads():
    secret = 'AAAASECRETDATA'
    result = render_result(
        (
            {'type': 'text', 'text': 'hello'},
            {'type': 'image', 'mime_type': 'image/png', 'data': secret},
            {'type': 'audio', 'mime_type': 'audio/wav', 'data': secret},
            {'type': 'resource', 'resource': {'text': 'from-resource'}},
            {'type': 'resource', 'resource': {'blob': secret}},
        ),
        {'z': 1, 'a': 'two'},
        is_error=True,
    )
    assert result.trust is TrustLevel.UNTRUSTED
    assert result.output == '\n'.join(
        (
            'hello',
            '[image omitted mime=image/png]',
            '[audio omitted mime=audio/wav]',
            'from-resource',
            '[resource omitted]',
            '{"a": "two", "z": 1}',
        )
    )
    assert secret not in result.output
    assert result.meta['isError'] is True
    assert result.meta['structuredContent'] == {'z': 1, 'a': 'two'}
