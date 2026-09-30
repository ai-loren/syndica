"""CLI YAML 配置：只在 surface 解析，交给 kernel 的永远是 Mapping。"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from syndica.cli.config import CliConfig, load_config
from syndica.kernel import ConfigError, ExitMode


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding='utf-8')
    return path


def test_load_config_registers_builtins_then_user_overrides_and_named_topologies(
    tmp_path: Path,
) -> None:
    prompt = tmp_path / 'prompt.md'
    prompt.write_text('custom worker prompt', encoding='utf-8')
    path = _write(
        tmp_path / 'syndica.yaml',
        """
version: 1
agents:
  - name: Worker
    description: custom worker
    system_prompt: prompt.md
    tools: [Read, Write]
topologies:
  review:
    slots:
      - name: root
        agent: Orchestrator
        is_root: true
        cardinality: singleton
        boot: eager
      - name: worker
        agent: Worker
        cardinality: keyed
        key_field: task
        boot: lazy
        exit_mode: idle
        extra_tools: [Grep]
        budget: {turns: 7}
        metadata: {lane: review}
""",
    )

    config = load_config(path)

    assert isinstance(config, CliConfig)
    assert config.source == str(path)
    assert config.catalog.names() == ('Planner', 'Explorer', 'Worker', 'Orchestrator')
    assert config.catalog.get('Worker').description == 'custom worker'
    assert config.catalog.get('Worker').system_prompt == 'custom worker prompt'
    assert config.catalog.source_of('Worker').kind == 'yaml_file'
    assert config.catalog.source_of('Worker').origin == str(tmp_path)
    assert len(config.catalog.overrides('Worker')) == 1
    assert isinstance(config.topologies, MappingProxyType)
    topology = config.topologies['review']
    assert [slot.name for slot in topology.slots] == ['root', 'worker']
    assert topology.slots[1].exit_mode is ExitMode.IDLE
    assert topology.slots[1].extra_tools == ('Grep',)
    assert topology.slots[1].budget == {'turns': 7}
    assert topology.slots[1].metadata == {'lane': 'review'}


def test_minimal_config_uses_builtin_agents_and_no_topologies(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path / '.syndica.yml', 'version: 1\n'))
    assert config.catalog.names() == ('Planner', 'Explorer', 'Worker', 'Orchestrator')
    assert dict(config.topologies) == {}


@pytest.mark.parametrize(
    ('text', 'needle'),
    [
        ('', 'Mapping'),
        ('- version\n- 1\n', 'Mapping'),
        ('version: true\n', 'version'),
        ('version: 2\n', 'version'),
        ('version: 1\nextra: value\n', 'extra'),
        ('version: 1\nagents: Worker\n', 'agents'),
        ('version: 1\ntopologies: []\n', 'topologies'),
    ],
)
def test_invalid_root_shapes_fail_with_three_part_config_errors(
    tmp_path: Path, text: str, needle: str
) -> None:
    path = _write(tmp_path / 'bad.yaml', text)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert needle in str(excinfo.value)
    assert excinfo.value.expected
    assert excinfo.value.received
    assert excinfo.value.next_action


def test_duplicate_yaml_keys_are_rejected_instead_of_last_wins(tmp_path: Path) -> None:
    path = _write(
        tmp_path / 'duplicate.yaml',
        """
version: 1
agents: []
agents:
  - {name: Hidden, description: must not silently win}
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert 'agents' in excinfo.value.received
    assert '重复' in str(excinfo.value)


def test_unsafe_python_yaml_tags_are_never_constructed(tmp_path: Path) -> None:
    path = _write(
        tmp_path / 'unsafe.yaml',
        """
!!python/object/apply:builtins.dict
  - - [version, 1]
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert 'YAML' in excinfo.value.expected
    assert 'safe' in excinfo.value.next_action


def test_duplicate_agent_names_in_one_file_are_rejected_as_one_bad_batch(tmp_path: Path) -> None:
    path = _write(
        tmp_path / 'duplicate-agent.yaml',
        """
version: 1
agents:
  - {name: Analyst, description: first}
  - {name: Analyst, description: second}
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert 'Analyst' in excinfo.value.received
    assert '重名' in str(excinfo.value)


def test_unknown_topology_and_slot_keys_are_rejected(tmp_path: Path) -> None:
    unknown_topology = _write(
        tmp_path / 'bad-topology.yaml',
        """
version: 1
topologies:
  default:
    slots: []
    behavior: replace-agent
""",
    )
    with pytest.raises(ConfigError) as topology_exc:
        load_config(unknown_topology)
    assert 'behavior' in topology_exc.value.received

    unknown_slot = _write(
        tmp_path / 'bad-slot.yaml',
        """
version: 1
topologies:
  default:
    slots:
      - {name: root, agent: Orchestrator, system_prompt: replace}
""",
    )
    with pytest.raises(ConfigError) as slot_exc:
        load_config(unknown_slot)
    assert 'system_prompt' in slot_exc.value.received
    assert '只增强' in slot_exc.value.next_action


def test_yaml_bool_cannot_be_used_as_a_numeric_budget(tmp_path: Path) -> None:
    path = _write(
        tmp_path / 'bool-budget.yaml',
        """
version: 1
topologies:
  default:
    slots:
      - name: root
        agent: Orchestrator
        budget: {turns: yes}
""",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert 'bool' in str(excinfo.value)
    assert 'turns' in excinfo.value.next_action


def test_missing_or_non_yaml_paths_fail_before_parsing(tmp_path: Path) -> None:
    missing = tmp_path / 'missing.yaml'
    with pytest.raises(ConfigError) as missing_exc:
        load_config(missing)
    assert str(missing) in missing_exc.value.received
    assert '创建' in missing_exc.value.next_action

    json_path = _write(tmp_path / 'config.json', '{"version": 1}')
    with pytest.raises(ConfigError) as suffix_exc:
        load_config(json_path)
    assert '.yaml' in suffix_exc.value.expected
    assert '.json' in suffix_exc.value.received


def test_invalid_utf8_and_multiple_yaml_documents_are_rejected_with_config_errors(
    tmp_path: Path,
) -> None:
    invalid_utf8 = tmp_path / 'invalid.yaml'
    invalid_utf8.write_bytes(b'version: 1\n\xff')
    with pytest.raises(ConfigError) as encoding_exc:
        load_config(invalid_utf8)
    assert 'UTF-8' in encoding_exc.value.expected
    assert str(invalid_utf8) in encoding_exc.value.received
    assert '编码' in encoding_exc.value.next_action

    multiple = _write(tmp_path / 'multiple.yaml', 'version: 1\n---\nversion: 1\n')
    with pytest.raises(ConfigError) as document_exc:
        load_config(multiple)
    assert '单文档' in document_exc.value.expected
    assert str(multiple) in document_exc.value.received


def test_agent_topology_and_slot_names_are_preserved_byte_for_byte(tmp_path: Path) -> None:
    path = _write(
        tmp_path / 'names.yaml',
        """
version: 1
agents:
  - {name: " Worker ", description: exact}
topologies:
  " Review ":
    slots:
      - {name: " root ", agent: " Worker "}
""",
    )
    config = load_config(path)
    assert config.catalog.names()[-1] == ' Worker '
    assert list(config.topologies) == [' Review ']
    assert config.topologies[' Review '].slots[0].name == ' root '
    assert config.topologies[' Review '].slots[0].agent == ' Worker '


def test_topology_names_and_result_mapping_are_immutable(tmp_path: Path) -> None:
    path = _write(
        tmp_path / 'immutable.yaml',
        """
version: 1
topologies:
  default:
    slots: [{name: root, agent: Orchestrator}]
""",
    )
    config = load_config(path)
    with pytest.raises(TypeError):
        config.topologies['other'] = config.topologies['default']  # pyright: ignore[reportIndexIssue]


# --- workspace 段 --------------------------------------------------------------------------------


def _workspace_config(tmp_path: Path, body: str) -> CliConfig:
    return load_config(_write(tmp_path / 'syndica.yaml', f'version: 1\nworkspace:\n{body}'))


def test_the_workspace_section_is_read_in_the_order_written(tmp_path: Path) -> None:
    config = _workspace_config(
        tmp_path,
        '  pass_env: [NO_COLOR, GIT_AUTHOR_NAME]\n'
        '  verify: [uv run ruff check ., uv run pytest -q]\n'
        '  protected: [pyproject.toml, "scripts/**", "**/conftest.py"]\n',
    )

    assert config.workspace.pass_env == ('NO_COLOR', 'GIT_AUTHOR_NAME')
    assert config.workspace.verify == ('uv run ruff check .', 'uv run pytest -q')
    assert config.workspace.protected == ('pyproject.toml', 'scripts/**', '**/conftest.py')


def test_a_config_without_a_workspace_section_has_an_empty_one(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path / 'syndica.yaml', 'version: 1\n'))

    assert (config.workspace.pass_env, config.workspace.verify) == ((), ())
    assert config.workspace.protected == ()


@pytest.mark.parametrize(
    'word',
    [
        'KEY',
        'KEYS',
        'TOKEN',
        'TOKENS',
        'SECRET',
        'SECRETS',
        'PASSWORD',
        'PASSWD',
        'PASS',
        'PAT',
        'CREDENTIAL',
        'CREDENTIALS',
        'AUTH',
        'COOKIE',
    ],
)
def test_every_secret_word_in_a_pass_env_name_is_refused(tmp_path: Path, word: str) -> None:
    with pytest.raises(ConfigError) as caught:
        _workspace_config(tmp_path, f'  pass_env: [MY_{word}_X]\n')

    assert '密钥' in caught.value.expected
    assert f'MY_{word}_X' in caught.value.received
    assert 'Agent' in caught.value.next_action


@pytest.mark.parametrize(
    'name', ['MY_API_KEY', 'SSH_AUTH_SOCK', 'my_token', 'Github_Pat', 'FOO_TOKEN']
)
def test_a_pass_env_name_that_is_a_secret_by_word_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(ConfigError):
        _workspace_config(tmp_path, f'  pass_env: [{name}]\n')


@pytest.mark.parametrize(
    'name', ['GIT_AUTHOR_NAME', 'COMPASS_DIR', 'KEYBOARD_LAYOUT', 'PATHS_EXTRA', 'NO_COLOR']
)
def test_a_name_that_merely_contains_a_secret_word_is_accepted(tmp_path: Path, name: str) -> None:
    assert _workspace_config(tmp_path, f'  pass_env: [{name}]\n').workspace.pass_env == (name,)


@pytest.mark.parametrize(
    'name', ['GIT_TERMINAL_PROMPT', 'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL', 'GH_CONFIG_DIR']
)
def test_a_variable_the_host_sets_itself_cannot_be_passed_through(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(ConfigError) as caught:
        _workspace_config(tmp_path, f'  pass_env: [{name}]\n')

    assert '宿主' in caught.value.expected
    assert name in caught.value.next_action


@pytest.mark.parametrize('name', ['NAME=1', '1ABC', 'A-B', '"A B"'])
def test_a_pass_env_entry_that_is_not_a_variable_name_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(ConfigError) as caught:
        _workspace_config(tmp_path, f'  pass_env: [{name}]\n')

    assert '环境变量名' in caught.value.expected


@pytest.mark.parametrize(
    ('body', 'anchor'),
    [
        ('  verify: uv run pytest\n', 'YAML 列表'),
        ('  verify: [""]\n', '非空白'),
        ('  verify: ["  "]\n', '非空白'),
        ('  verify: [3]\n', '非空白'),
        ('  verify: ["a\\0b"]\n', 'NUL'),
        ('  protected: ["x\\0"]\n', 'NUL'),
        ('  protected: ["a/../b"]\n', '不含 ..'),
        ('  pass_env: ["A\\n"]\n', '环境变量名'),
        ('  verify: [a, a]\n', '只出现一次'),
        ('  protected: [/etc/passwd]\n', '相对工作区根'),
        ('  protected: ["../x/**"]\n', '不含 ..'),
        ('  protected: [a, a]\n', '只出现一次'),
        ('  pass_env: [A, A]\n', '只出现一次'),
        ('  verfy: [x]\n', '只含'),
    ],
)
def test_a_malformed_workspace_entry_is_refused_with_the_fix(
    tmp_path: Path, body: str, anchor: str
) -> None:
    with pytest.raises(ConfigError) as caught:
        _workspace_config(tmp_path, body)

    assert anchor in caught.value.expected


@pytest.mark.parametrize('value', ['[verify]', 'null', 'false', '[]', '"verify"'])
def test_a_workspace_section_that_is_not_a_mapping_is_refused(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_config(_write(tmp_path / 'syndica.yaml', f'version: 1\nworkspace: {value}\n'))

    assert 'Mapping' in caught.value.expected


@pytest.mark.parametrize(
    'name',
    [
        'GIT_CONFIG_PARAMETERS',
        'GIT_CONFIG_COUNT',
        'GIT_CONFIG_VALUE_0',
        'GIT_SSH_COMMAND',
        'GIT_SSH',
        'GIT_ASKPASS',
        'SSH_ASKPASS',
        'GIT_DIR',
        'GIT_WORK_TREE',
        'GIT_EXEC_PATH',
        'GIT_NAMESPACE',
        'GH_HOST',
    ],
)
def test_a_name_that_reopens_git_or_gh_configuration_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(ConfigError) as caught:
        _workspace_config(tmp_path, f'  pass_env: [{name}]\n')

    assert 'git' in caught.value.expected
    assert name in caught.value.next_action


def test_a_yaml_boolean_in_a_list_of_strings_is_refused_with_a_quoting_hint(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError) as caught:
        _workspace_config(tmp_path, '  pass_env: [ON]\n')

    assert 'bool' in caught.value.received
    assert '引号' in caught.value.next_action
