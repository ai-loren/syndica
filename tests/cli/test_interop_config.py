"""interop 根键与用户级 interop.yaml：项目配置只能按名字引用，密钥变量名只在工作区之外。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from syndica.cli.config import load_config
from syndica.cli.interop_config import (
    described_targets,
    interop_credentials_path,
    load_interop_credentials,
    unmatched_credentials,
)
from syndica.cli.trust import describe, read_snapshot
from syndica.kernel import ConfigError

_BASE = """\
version: 1
interop:
  mcp:
    servers:
      - name: files
        command: [npx, "-y", "@modelcontextprotocol/server-filesystem", "."]
        pass_env: [NO_COLOR]
        roles: [Worker]
        high_risk: [write_file]
        low_risk: [read_file]
      - name: tracker
        url: https://mcp.example.com/mcp
  acp:
    agents:
      - name: gemini
        command: [npx, "-y", "@google/gemini-cli", "--experimental-acp"]
  a2a:
    agents:
      - name: researcher
        url: http://127.0.0.1:9999/
agents:
  - name: Gemini
    description: 用 Gemini CLI 做事
    model: acp:gemini
    tools: []
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding='utf-8')
    return path


def test_a_declared_external_agent_is_bound_and_shown_verbatim(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path / '.syndica.yaml', _BASE))

    assert config.interop.present
    assert config.interop.mcp_serve_tools == ('Read', 'Grep', 'Glob')
    files, tracker = config.interop.mcp_servers
    assert files.command == ('npx', '-y', '@modelcontextprotocol/server-filesystem', '.')
    assert files.roles == ('Worker',)
    assert files.pass_env == ('NO_COLOR',)
    assert (files.high_risk, files.low_risk) == (('write_file',), ('read_file',))
    assert tracker.url == 'https://mcp.example.com/mcp'
    assert config.interop.acp_agents[0].name == 'gemini'
    assert config.interop.a2a_agents[0].url == 'http://127.0.0.1:9999/'
    assert described_targets(config.interop) == (
        'mcp files: npx -y @modelcontextprotocol/server-filesystem .',
        'mcp tracker: https://mcp.example.com/mcp',
        'mcp serve: Read, Grep, Glob',
        'acp gemini: npx -y @google/gemini-cli --experimental-acp',
        'a2a researcher: http://127.0.0.1:9999/',
    )


def test_omitted_interop_keeps_the_default_serve_tools_and_adds_no_confirmation_line(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path / '.syndica.yaml', 'version: 1\n')
    config = load_config(path)

    assert config.interop.present is False
    assert config.interop.mcp_serve_tools == ('Read', 'Grep', 'Glob')
    assert described_targets(config.interop) == ()
    shown = describe(read_snapshot(path, tmp_path), config)
    assert 'interop: 无' in shown


def test_an_explicit_serve_list_replaces_the_default(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path / '.syndica.yaml',
            'version: 1\ninterop:\n  mcp:\n    serve:\n      tools: [Read, Write]\n',
        )
    )

    assert config.interop.mcp_serve_tools == ('Read', 'Write')


@pytest.mark.parametrize(
    ('body', 'anchor'),
    [
        ('interop: [files]\n', 'Mapping'),
        ('interop:\n  mcp:\n    servers: files\n', 'YAML 列表'),
        (
            'interop:\n  mcp:\n    servers:\n      - name: Files\n        command: [npx]\n',
            '[a-z]',
        ),
        (
            'interop:\n  mcp:\n    servers:\n      - name: files\n        command: npx -y files\n',
            '不经 shell',
        ),
        (
            'interop:\n  mcp:\n    servers:\n      - name: files\n'
            '        command: [npx]\n        url: https://mcp.example.com/mcp\n',
            '恰好有',
        ),
        (
            'interop:\n  mcp:\n    servers:\n      - name: files\n',
            '恰好有',
        ),
        (
            'interop:\n  mcp:\n    servers:\n      - name: files\n        command: []\n',
            '至少有程序名',
        ),
        (
            'interop:\n  mcp:\n    servers:\n      - name: files\n        url: ftp://example.com/mcp\n',
            'http://',
        ),
        (
            'interop:\n  mcp:\n    servers:\n      - name: files\n        command: [npx]\n'
            '        token: secret\n',
            'TOKEN',
        ),
        (
            'interop:\n  mcp:\n    servers:\n      - name: files\n        command: [npx]\n'
            '        pass_env: [GEMINI_API_KEY]\n',
            '密钥',
        ),
        (
            'interop:\n  mcp:\n    servers:\n'
            '      - name: files\n        command: [GEMINI_API_KEY]\n',
            '密钥',
        ),
        (
            'interop:\n  mcp:\n    servers:\n      - name: files\n        command: [npx]\n'
            '        roles: [NoSuch]\n',
            'NoSuch',
        ),
        (
            'interop:\n  mcp:\n    servers:\n      - name: files\n        command: [npx]\n'
            '        high_risk: [write_file]\n        low_risk: [write_file]\n',
            '重叠',
        ),
        (
            'interop:\n  mcp:\n    servers:\n      - name: files\n        command: [npx]\n'
            '      - name: files\n        url: https://mcp.example.com/mcp\n',
            '互不重复',
        ),
        (
            'interop:\n  mcp:\n    servers:\n      - name: files\n        command: [yes]\n',
            '布尔',
        ),
        (
            'interop:\n  acp:\n    agents:\n      - name: gemini\n',
            'command',
        ),
        (
            'agents:\n  - name: Gemini\n    description: x\n'
            '    model: acp:missing\n    tools: []\n',
            'acp:missing',
        ),
        (
            'agents:\n  - name: Gemini\n    description: x\n'
            '    model: a2a:missing\n    tools: []\n',
            'a2a:missing',
        ),
        ('interop:\n  bearer_token: abc\n', 'bearer_token'),
    ],
)
def test_project_interop_rejects_the_wrong_shape(tmp_path: Path, body: str, anchor: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_config(_write(tmp_path / '.syndica.yaml', f'version: 1\n{body}'))

    assert anchor in str(caught.value)
    assert caught.value.expected
    assert caught.value.received
    assert caught.value.next_action


def _user_file(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    os.chmod(path, mode)
    return path


def test_user_credentials_keep_only_variable_names(tmp_path: Path) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    path = _user_file(
        tmp_path / 'config' / 'interop.yaml',
        'credentials:\n'
        '  acp:gemini: {env: [GEMINI_API_KEY]}\n'
        '  mcp:tracker: {bearer_env: TRACKER_TOKEN}\n'
        '  a2a:researcher: {env: [RESEARCHER_TOKEN], bearer_env: RESEARCHER_TOKEN}\n',
    )

    loaded = load_interop_credentials(path, workspace=workspace)

    assert loaded['acp:gemini'].env == ('GEMINI_API_KEY',)
    assert loaded['acp:gemini'].bearer_env is None
    assert loaded['mcp:tracker'].bearer_env == 'TRACKER_TOKEN'
    assert loaded['a2a:researcher'].env == ('RESEARCHER_TOKEN',)
    project = load_config(_write(workspace / '.syndica.yaml', _BASE))
    assert unmatched_credentials(loaded, project.interop) == ()


def test_a_missing_user_file_is_an_empty_table(tmp_path: Path) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    assert dict(load_interop_credentials(tmp_path / 'missing.yaml', workspace=workspace)) == {}


def test_credentials_inside_the_workspace_are_refused(tmp_path: Path) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    path = _user_file(workspace / 'interop.yaml', 'credentials: {}\n')

    with pytest.raises(ConfigError) as caught:
        load_interop_credentials(path, workspace=workspace)

    assert '工作区之外' in caught.value.expected
    assert 'interop.yaml' in caught.value.next_action


def test_credentials_that_are_not_mode_0600_are_refused(tmp_path: Path) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    path = _user_file(tmp_path / 'config' / 'interop.yaml', 'credentials: {}\n', mode=0o644)

    with pytest.raises(ConfigError) as caught:
        load_interop_credentials(path, workspace=workspace)

    assert '0600' in caught.value.expected
    assert '0644' in caught.value.received
    assert 'chmod 600' in caught.value.next_action


@pytest.mark.parametrize(
    ('text', 'anchor'),
    [
        ('credentials:\n  acp:Gemini: {env: [GEMINI_API_KEY]}\n', 'mcp:<名字>'),
        ('credentials:\n  acp:gemini: {env: ["not a name"]}\n', '变量名'),
        ('credentials:\n  acp:gemini: {env: [yes]}\n', '布尔'),
        ('credentials:\n  acp:gemini: {bearer_env: yes}\n', 'bearer_env'),
        ('credentials:\n  acp:gemini: {token: GEMINI_API_KEY}\n', 'TOKEN'),
        ('credentials:\n  acp:gemini: {}\n', 'env'),
        ('credentials:\n  acp:gemini: {env: [A]}\n  acp:gemini: {env: [B]}\n', '只出现一次'),
        ('credentials: [acp:gemini]\n', 'Mapping'),
    ],
)
def test_user_credentials_reject_values_and_the_wrong_shape(
    tmp_path: Path, text: str, anchor: str
) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    path = _user_file(tmp_path / 'config' / 'interop.yaml', text)

    with pytest.raises(ConfigError) as caught:
        load_interop_credentials(path, workspace=workspace)

    assert anchor in str(caught.value)


def test_unmatched_user_names_are_listed_without_failing_the_load(tmp_path: Path) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    path = _user_file(
        tmp_path / 'config' / 'interop.yaml',
        'credentials:\n  acp:other: {env: [OTHER_TOKEN]}\n',
    )
    project = load_config(_write(workspace / '.syndica.yaml', 'version: 1\n'))

    loaded = load_interop_credentials(path, workspace=workspace)
    assert unmatched_credentials(loaded, project.interop) == ('acp:other',)


def test_the_credentials_file_sits_beside_the_trust_store(tmp_path: Path) -> None:
    path = interop_credentials_path(
        {'SYNDICA_TRUST_STORE': str(tmp_path / 'trusted.json'), 'HOME': str(tmp_path)}
    )

    assert path == tmp_path / 'interop.yaml'
