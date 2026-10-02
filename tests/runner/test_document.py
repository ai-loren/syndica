"""场景文档的两种读法（M3 D12）：手写的 `.yaml` 与捕获的 `.json` 交给同一个校验，读出同样的文档。"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from syndica.kernel import ConfigError, ContextOverflowError, LLMResponseError
from syndica.runner import load_document, load_scenario

FIXTURES = Path(__file__).parent / 'fixtures'

_CAPTURED: dict[str, Any] = {
    'version': 2,
    'name': 'captured',
    'host': {
        'flags': {'untrusted_high_risk': 'ask', 'attempts': 3, 'retry_enabled': True},
        'danger': {
            'rules': [
                {'id': 'shell.workspace_root_removal', 'params': {'root': '/w', 'real': '/w'}},
                {'id': 'shell.raw_device_write'},
                {'id': 'shell.git_state_change'},
            ]
        },
        'modules': ['safety', 'syndica.dev', 'retry'],
    },
    'agents': {
        'Lead': {
            'frozen': {
                'system_prompt': '你是 Lead。回答「yes」或 0777 都按字面',
                'tools': ['Write', 'Bash', 'Read'],
            },
            'instances': [
                {
                    'key': ['host', 'spawn', 0],
                    'turns': [
                        {
                            'turn': 1,
                            'attempt': 1,
                            'tool_calls': [
                                {
                                    'id': 'c1',
                                    'name': 'Bash',
                                    'arguments': {'command': 'echo é', 'cwd': 'src', 'timeout': 5},
                                }
                            ],
                        },
                        {'turn': 2, 'attempt': 1, 'content': 'yes'},
                    ],
                    'tool_results': [{'call': [1, 1, 0], 'output': 'é\n', 'trust': 'untrusted'}],
                }
            ],
        }
    },
    'entry': {'mode': 'query', 'agent': 'Lead', 'prompt': 'go'},
    'ids': [{'key': ['host', 'spawn', 0], 'id': 'f0' * 16}],
    'expect': {'final_output': {'Lead[0]': 'yes'}},
}


def test_the_same_document_as_yaml_and_as_json_reads_the_same(tmp_path: Path):
    as_yaml = tmp_path / 'captured.yaml'
    as_json = tmp_path / 'captured.json'
    as_yaml.write_text(
        yaml.safe_dump(_CAPTURED, allow_unicode=True, sort_keys=False), encoding='utf-8'
    )
    as_json.write_text(json.dumps(_CAPTURED, ensure_ascii=False), encoding='utf-8')

    from_yaml = load_document(as_yaml)
    from_json = load_document(as_json)

    assert (from_yaml.source, from_json.source) == (str(as_yaml), str(as_json))
    assert dataclasses.replace(from_yaml, source='') == dataclasses.replace(from_json, source='')
    for document in (from_yaml, from_json):
        assert document.host is not None
        assert list(document.host.flags) == ['untrusted_high_risk', 'attempts', 'retry_enabled']
        [call] = document.roles['Lead'].instances[0].replies[0].tool_calls
        assert call.arguments == '{"command":"echo é","cwd":"src","timeout":5}'
    frozen = from_json.roles['Lead'].frozen
    assert frozen is not None and frozen.system_prompt == '你是 Lead。回答「yes」或 0777 都按字面'
    assert from_json.roles['Lead'].instances[0].tool_results[0].output == 'é\n'


@pytest.mark.parametrize(
    ('text', 'received'),
    [
        ('{"version": 2, "version": 1}', "'version' 出现了不止一次"),
        ('{"version": NaN}', 'NaN'),
        ('{"version": 2,}', 'Expecting property name'),
    ],
    ids=['duplicate-key', 'nan', 'syntax'],
)
def test_a_json_document_that_is_not_strict_json_is_refused(
    tmp_path: Path, text: str, received: str
):
    path = tmp_path / 'captured.json'
    path.write_text(text, encoding='utf-8')

    with pytest.raises(ConfigError) as caught:
        load_document(path)

    assert received in caught.value.received
    assert '不要手改' in caught.value.next_action


def test_a_json_root_that_is_not_an_object_is_refused(tmp_path: Path):
    path = tmp_path / 'captured.json'
    path.write_text('[1, 2]', encoding='utf-8')

    with pytest.raises(ConfigError) as caught:
        load_document(path)

    assert '场景根是 Mapping' in caught.value.expected


def test_load_scenario_reads_v1_and_points_v2_documents_at_load_document(tmp_path: Path):
    path = tmp_path / 'captured.json'
    path.write_text(json.dumps(_CAPTURED), encoding='utf-8')

    with pytest.raises(ConfigError) as caught:
        load_scenario(path)

    assert 'version: 2' in caught.value.received
    assert 'load_document' in caught.value.next_action
    assert load_scenario(FIXTURES / 'delegated_write.yaml').version == 1


def test_a_v1_scenario_written_as_json_runs_through_the_same_reader(tmp_path: Path):
    document = yaml.safe_load((FIXTURES / 'delegated_write.yaml').read_text(encoding='utf-8'))
    path = tmp_path / 'delegated_write.json'
    path.write_text(json.dumps(document), encoding='utf-8')

    from_json = load_scenario(path)
    from_yaml = load_scenario(FIXTURES / 'delegated_write.yaml')

    assert from_json.agents['Coordinator'].instances == from_yaml.agents['Coordinator'].instances
    assert from_json.expect == from_yaml.expect


def test_each_v1_failure_kind_is_rebuilt_as_its_own_exception_type(tmp_path: Path):
    document = yaml.safe_load((FIXTURES / 'delegated_write.yaml').read_text(encoding='utf-8'))
    turns = document['agents']['Coordinator']['instances'][0]['turns']
    turns[0:0] = [
        {'raises': {'kind': 'context_overflow', 'message': 'too long'}},
        {'raises': {'kind': 'llm_response_error', 'message': 'HTTP 500'}},
        {'raises': {'kind': 'runtime_error', 'message': 'boom'}},
    ]
    path = tmp_path / 'kinds.yaml'
    path.write_text(yaml.safe_dump(document), encoding='utf-8')

    replies = load_scenario(path).agents['Coordinator'].instances[0]

    overflow, failure, runtime = (reply.raises for reply in replies[:3])
    assert type(overflow) is ContextOverflowError and overflow.received == 'too long'
    assert type(failure) is LLMResponseError and failure.received == 'HTTP 500'
    assert type(runtime) is RuntimeError and str(runtime) == 'boom'


def test_yaml_aliases_are_refused(tmp_path: Path):
    path = tmp_path / 'aliased.yaml'
    path.write_text('version: 2\nname: &n x\nalias: *n\n', encoding='utf-8')

    with pytest.raises(ConfigError) as caught:
        load_document(path)

    assert '别名' in caught.value.expected and '*n' in caught.value.received
    assert '写出来的值' in caught.value.next_action


def test_yaml_octal_and_sexagesimal_looking_words_stay_strings(tmp_path: Path):
    path = tmp_path / 'keys.yaml'
    path.write_text(
        """
version: 2
name: keys
agents:
  Lead:
    manifest: {name: Lead, description: lead}
    instances:
      - key: [slot, workers, 0777, 1:30, 7]
        turns: [{content: done}]
entry: {mode: query, agent: Lead, prompt: go}
ids:
  - {key: [slot, workers, 0777, 1:30, 7], id: lead-1}
expect: {}
""",
        encoding='utf-8',
    )

    parsed = load_document(path)

    assert parsed.roles['Lead'].instances[0].key == ('slot', 'workers', '0777', '1:30', 7)
    assert parsed.ids == ((('slot', 'workers', '0777', '1:30', 7), 'lead-1'),)


def test_a_duplicate_json_key_is_reported_with_where_it_is(tmp_path: Path):
    path = tmp_path / 'captured.json'
    path.write_text(
        '{"version": 2,\n "host": {"no_write": false, "no_write": true}}', encoding='utf-8'
    )

    with pytest.raises(ConfigError) as caught:
        load_document(path)

    assert "第 2 行第 11 列开始的对象里，键 'no_write' 出现了不止一次" in caught.value.received


@pytest.mark.parametrize(
    ('text', 'received'),
    [
        ('{"version": 1e400}', '超出了浮点数的范围'),
        ('{"version": ' + '[' * 5000 + ']' * 5000 + '}', '超出了递归上限'),
    ],
    ids=['overflowing-number', 'deep-nesting'],
)
def test_json_that_python_would_accept_loosely_is_refused_with_a_reason(
    tmp_path: Path, text: str, received: str
):
    path = tmp_path / 'captured.json'
    path.write_text(text, encoding='utf-8')

    with pytest.raises(ConfigError) as caught:
        load_document(path)

    assert received in caught.value.received


def test_yaml_nested_beyond_the_parser_recursion_limit_is_refused_with_a_reason(tmp_path: Path):
    path = tmp_path / 'deep.yaml'
    path.write_text('version: ' + '[' * 3000 + ']' * 3000 + '\n', encoding='utf-8')

    with pytest.raises(ConfigError) as caught:
        load_document(path)

    assert '超出了递归上限' in caught.value.received
