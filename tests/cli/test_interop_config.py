"""interop 根键与用户级 interop.yaml：项目配置只能按名字引用，密钥变量名只在工作区之外。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from syndica.cli.config import load_config
from syndica.cli.interop_config import (
    A2aAgent,
    AcpAgent,
    InteropCredential,
    McpServer,
    credential_for,
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
        'mcp files: npx -y @modelcontextprotocol/server-filesystem .'
        '；凭证：只交用户级 interop.yaml 里 command 与此逐字相同的 mcp:files 点名的变量',
        'mcp tracker: https://mcp.example.com/mcp'
        '；凭证：只交用户级 interop.yaml 里 url 与此逐字相同的 mcp:tracker 点名的变量',
        'mcp serve: Read, Grep, Glob',
        'acp gemini: npx -y @google/gemini-cli --experimental-acp'
        '；凭证：只交用户级 interop.yaml 里 command 与此逐字相同的 acp:gemini 点名的变量',
        'a2a researcher: http://127.0.0.1:9999/'
        '；凭证：只交用户级 interop.yaml 里 url 与此逐字相同的 a2a:researcher 点名的变量',
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


_PINNED = """\
credentials:
  acp:gemini:
    command: [npx, "-y", "@google/gemini-cli", "--experimental-acp"]
    env: [GEMINI_API_KEY]
  mcp:files:
    command: [npx, "-y", "@modelcontextprotocol/server-filesystem", "."]
    env: [FILES_TOKEN]
  mcp:tracker:
    url: https://mcp.example.com/mcp
    bearer_env: TRACKER_TOKEN
  a2a:researcher:
    url: http://127.0.0.1:9999/
    bearer_env: RESEARCHER_TOKEN
"""


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    return workspace


def test_user_credentials_pin_a_destination_and_keep_only_variable_names(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    path = _user_file(tmp_path / 'config' / 'interop.yaml', _PINNED)

    loaded = load_interop_credentials(path, workspace=workspace)

    assert dict(loaded) == {
        'acp:gemini': InteropCredential(
            command=('npx', '-y', '@google/gemini-cli', '--experimental-acp'),
            env=('GEMINI_API_KEY',),
        ),
        'mcp:files': InteropCredential(
            command=('npx', '-y', '@modelcontextprotocol/server-filesystem', '.'),
            env=('FILES_TOKEN',),
        ),
        'mcp:tracker': InteropCredential(
            url='https://mcp.example.com/mcp', bearer_env='TRACKER_TOKEN'
        ),
        'a2a:researcher': InteropCredential(
            url='http://127.0.0.1:9999/', bearer_env='RESEARCHER_TOKEN'
        ),
    }
    project = load_config(_write(workspace / '.syndica.yaml', _BASE))
    assert unmatched_credentials(loaded, project.interop) == ()
    files, tracker = project.interop.mcp_servers
    assert credential_for(loaded, files) == loaded['mcp:files']
    assert credential_for(loaded, tracker) == loaded['mcp:tracker']
    assert credential_for(loaded, project.interop.acp_agents[0]) == loaded['acp:gemini']
    assert credential_for(loaded, project.interop.a2a_agents[0]) == loaded['a2a:researcher']


def test_the_trust_description_names_which_variables_go_to_which_destination(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    loaded = load_interop_credentials(
        _user_file(tmp_path / 'config' / 'interop.yaml', _PINNED), workspace=workspace
    )
    project = load_config(_write(workspace / '.syndica.yaml', _BASE))

    assert described_targets(project.interop, loaded) == (
        'mcp files: npx -y @modelcontextprotocol/server-filesystem .；凭证：FILES_TOKEN 交给这里',
        'mcp tracker: https://mcp.example.com/mcp'
        '；凭证：TRACKER_TOKEN（Authorization: Bearer） 交给这里',
        'mcp serve: Read, Grep, Glob',
        'acp gemini: npx -y @google/gemini-cli --experimental-acp；凭证：GEMINI_API_KEY 交给这里',
        'a2a researcher: http://127.0.0.1:9999/'
        '；凭证：RESEARCHER_TOKEN（Authorization: Bearer） 交给这里',
    )


def test_the_trust_description_says_a_mismatched_pin_sends_nothing_and_refuses(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    loaded = load_interop_credentials(
        _user_file(tmp_path / 'config' / 'interop.yaml', _PINNED), workspace=workspace
    )
    hostile = _BASE.replace('http://127.0.0.1:9999/', 'https://attacker.example/a2a/').replace(
        '"--experimental-acp"', '"--experimental-acp", "--yolo"'
    )
    project = load_config(_write(workspace / '.syndica.yaml', hostile))

    shown = described_targets(project.interop, loaded)

    assert shown[3] == (
        'acp gemini: npx -y @google/gemini-cli --experimental-acp --yolo'
        '；凭证：不交——用户级 acp:gemini 钉的是 command '
        '["npx", "-y", "@google/gemini-cli", "--experimental-acp"]，对不上，'
        'GEMINI_API_KEY 不发、启动时拒绝'
    )
    assert shown[4] == (
        'a2a researcher: https://attacker.example/a2a/'
        '；凭证：不交——用户级 a2a:researcher 钉的是 url http://127.0.0.1:9999/，对不上，'
        'RESEARCHER_TOKEN（Authorization: Bearer） 不发、启动时拒绝'
    )


def test_the_trust_description_says_an_object_without_a_user_entry_gets_no_credential(
    tmp_path: Path,
) -> None:
    project = load_config(_write(_workspace(tmp_path) / '.syndica.yaml', _BASE))

    shown = described_targets(project.interop, {})

    assert shown[4] == (
        'a2a researcher: http://127.0.0.1:9999/'
        '；凭证：无（用户级 interop.yaml 没有 a2a:researcher）'
    )


def _server(*, url: str | None = None, command: tuple[str, ...] | None = None) -> McpServer:
    return McpServer(
        name='tracker',
        command=command,
        url=url,
        pass_env=(),
        roles=(),
        high_risk=(),
        low_risk=(),
    )


_URL_PIN = 'https://mcp.example.com/mcp'
_COMMAND_PIN = ('npx', '-y', '@google/gemini-cli', '--experimental-acp')


@pytest.mark.parametrize(
    'declared',
    [
        'https://attacker.example/mcp',
        'https://mcp.example.com/mcp/',
        'https://MCP.example.com/mcp',
        'https://mcp.example.com:443/mcp',
        'https://mcp.example.com:8443/mcp',
        'https://mcp.example.com/other',
        'http://mcp.example.com/mcp',
        'https://mcp.example.com/mcp?to=attacker',
        'https://mcp.example.com@attacker.example/mcp',
    ],
)
def test_a_url_that_differs_from_the_pin_in_any_byte_is_refused(declared: str) -> None:
    credentials = {
        'a2a:researcher': InteropCredential(url=_URL_PIN, bearer_env='RESEARCHER_TOKEN'),
        'mcp:tracker': InteropCredential(url=_URL_PIN, bearer_env='TRACKER_TOKEN'),
    }

    for target, key in (
        (A2aAgent(name='researcher', url=declared), 'a2a:researcher'),
        (_server(url=declared), 'mcp:tracker'),
    ):
        with pytest.raises(ConfigError) as caught:
            credential_for(credentials, target)
        assert key in caught.value.expected
        assert f'url {_URL_PIN}' in caught.value.expected
        assert 'interop.yaml' in caught.value.expected
        assert f'url {declared}' in caught.value.received
        assert f'{key} 的 url' in caught.value.next_action
        assert '逐字相同' in caught.value.next_action


@pytest.mark.parametrize(
    'declared',
    [
        (*_COMMAND_PIN, '--yolo'),
        _COMMAND_PIN[:-1],
        ('npx', '@google/gemini-cli', '-y', '--experimental-acp'),
        ('/tmp/evil', '-y', '@google/gemini-cli', '--experimental-acp'),
        ('npx -y', '@google/gemini-cli', '--experimental-acp'),
    ],
)
def test_a_command_that_differs_from_the_pin_in_any_argument_is_refused(
    declared: tuple[str, ...],
) -> None:
    credentials = {
        'acp:gemini': InteropCredential(command=_COMMAND_PIN, env=('GEMINI_API_KEY',)),
        'mcp:tracker': InteropCredential(command=_COMMAND_PIN, env=('GEMINI_API_KEY',)),
    }

    for target, key in (
        (AcpAgent(name='gemini', command=declared, pass_env=()), 'acp:gemini'),
        (_server(command=declared), 'mcp:tracker'),
    ):
        with pytest.raises(ConfigError) as caught:
            credential_for(credentials, target)
        assert 'GEMINI_API_KEY' in caught.value.expected
        assert '"--experimental-acp"]' in caught.value.expected
        assert f'command {json.dumps(list(declared))}' in caught.value.received
        assert f'{key} 的 command' in caught.value.next_action


def test_an_mcp_pin_of_the_other_kind_is_refused() -> None:
    by_command = {'mcp:tracker': InteropCredential(command=('tracker',), env=('A_TOKEN',))}
    by_url = {'mcp:tracker': InteropCredential(url=_URL_PIN, bearer_env='A_TOKEN')}

    with pytest.raises(ConfigError, match='command'):
        credential_for(by_command, _server(url=_URL_PIN))
    with pytest.raises(ConfigError, match='url'):
        credential_for(by_url, _server(command=('tracker',)))


def test_an_object_without_a_user_entry_gets_no_credential() -> None:
    credentials = {'a2a:other': InteropCredential(url=_URL_PIN, bearer_env='A_TOKEN')}

    assert credential_for(credentials, A2aAgent(name='researcher', url=_URL_PIN)) is None


@pytest.mark.parametrize(
    ('key', 'url'),
    [
        ('a2a:researcher', 'http://researcher.example/a2a/'),
        ('mcp:tracker', 'http://mcp.example.com/mcp'),
        ('a2a:researcher', 'http://192.168.1.10:9999/'),
        ('a2a:researcher', 'http://127.0.0.2:9999/'),
        ('a2a:researcher', 'http://127.0.0.1.attacker.example/'),
        ('a2a:researcher', 'http://localhost.attacker.example/'),
        ('a2a:researcher', 'http://127.0.0.1@attacker.example/'),
        ('a2a:researcher', 'http://attacker.example\\@127.0.0.1/'),
        ('a2a:researcher', 'http://LOCALHOST:9999/'),
    ],
)
def test_a_bearer_over_cleartext_http_off_loopback_is_refused(
    tmp_path: Path, key: str, url: str
) -> None:
    path = _user_file(
        tmp_path / 'config' / 'interop.yaml',
        f'credentials:\n  {key}:\n    url: {json.dumps(url)}\n    bearer_env: A_TOKEN\n',
    )

    with pytest.raises(ConfigError) as caught:
        load_interop_credentials(path, workspace=_workspace(tmp_path))

    assert 'https://' in caught.value.expected
    assert '127.0.0.1 / [::1] / localhost' in caught.value.expected
    assert repr(url) in caught.value.received
    assert 'TLS' in caught.value.next_action


@pytest.mark.parametrize(
    'url',
    [
        'http://127.0.0.1:9999/a2a/',
        'http://[::1]:8080/a2a/',
        'http://localhost/mcp',
        'http://localhost:3000',
        'http://127.0.0.1?x=@attacker.example',
        'https://researcher.example/a2a/',
    ],
)
def test_a_bearer_over_loopback_http_or_https_is_accepted(tmp_path: Path, url: str) -> None:
    path = _user_file(
        tmp_path / 'config' / 'interop.yaml',
        f'credentials:\n  a2a:researcher:\n    url: {json.dumps(url)}\n    bearer_env: A_TOKEN\n',
    )

    loaded = load_interop_credentials(path, workspace=_workspace(tmp_path))

    assert loaded['a2a:researcher'] == InteropCredential(url=url, bearer_env='A_TOKEN')


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


@pytest.mark.parametrize(
    'mode',
    [0o644, 0o640, 0o620, 0o604, 0o602, 0o700, 0o400],
    ids=[
        'world_readable',
        'group_readable',
        'group_writable',
        'other_readable',
        'other_writable',
        'owner_executable',
        'owner_read_only',
    ],
)
def test_credentials_whose_mode_is_not_exactly_0600_are_refused(tmp_path: Path, mode: int) -> None:
    workspace = _workspace(tmp_path)
    path = _user_file(tmp_path / 'config' / 'interop.yaml', 'credentials: {}\n', mode=mode)

    with pytest.raises(ConfigError) as caught:
        load_interop_credentials(path, workspace=workspace)

    assert '0600' in caught.value.expected
    assert f'权限是 {mode:04o}' in caught.value.received
    assert 'chmod 600' in caught.value.next_action


_ACP = '  acp:gemini:\n    command: [gemini]\n'
_A2A = '  a2a:researcher:\n    url: https://researcher.example/a2a/\n'


@pytest.mark.parametrize(
    ('text', 'anchor'),
    [
        ('credentials:\n  acp:Gemini: {command: [gemini], env: [GEMINI_API_KEY]}\n', 'mcp:<名字>'),
        (f'credentials:\n{_ACP}    env: ["not a name"]\n', '变量名'),
        (f'credentials:\n{_ACP}    env: [yes]\n', '布尔'),
        (f'credentials:\n{_A2A}    bearer_env: yes\n', 'bearer_env'),
        (f'credentials:\n{_ACP}    env: [A]\n    token: GEMINI_API_KEY\n', 'TOKEN'),
        ('credentials:\n  acp:gemini: {env: [A]}\n  acp:gemini: {env: [B]}\n', '只出现一次'),
        ('credentials: [acp:gemini]\n', 'Mapping'),
        ('credentials:\n  acp:gemini: {}\n', '恰好有 command'),
        ('credentials:\n  acp:gemini: {env: [GEMINI_API_KEY]}\n', '恰好有 command'),
        ('credentials:\n  a2a:researcher: {bearer_env: RESEARCHER_TOKEN}\n', '恰好有 url'),
        ('credentials:\n  mcp:tracker: {bearer_env: TRACKER_TOKEN}\n', 'url（HTTP 服务'),
        (
            'credentials:\n  acp:gemini: {url: "https://gemini.example/", env: [A]}\n',
            '恰好有 command',
        ),
        ('credentials:\n  a2a:researcher: {command: [researcher], bearer_env: A}\n', '恰好有 url'),
        (
            'credentials:\n  mcp:tracker:\n    url: https://mcp.example.com/mcp\n'
            '    command: [tracker]\n    bearer_env: A\n',
            'url（HTTP 服务',
        ),
        (f'credentials:\n{_A2A}    bearer_env: A\n    env: [B]\n', '不含 env'),
        (f'credentials:\n{_ACP}    env: [A]\n    bearer_env: B\n', '不含 bearer_env'),
        (f'credentials:\n{_A2A}', '有 bearer_env'),
        (f'credentials:\n{_ACP}', '有 env'),
        ('credentials:\n  acp:gemini: {command: "npx -y gemini", env: [A]}\n', '不经 shell'),
        ('credentials:\n  acp:gemini: {command: [], env: [A]}\n', '至少有程序名'),
        (
            'credentials:\n  a2a:researcher: {url: "ftp://researcher.example/", bearer_env: A}\n',
            'http://',
        ),
        ('credentials:\n  a2a:researcher: {url: [x], bearer_env: A}\n', 'http://'),
    ],
)
def test_user_credentials_reject_values_and_the_wrong_shape(
    tmp_path: Path, text: str, anchor: str
) -> None:
    workspace = _workspace(tmp_path)
    path = _user_file(tmp_path / 'config' / 'interop.yaml', text)

    with pytest.raises(ConfigError) as caught:
        load_interop_credentials(path, workspace=workspace)

    assert anchor in str(caught.value)
    assert caught.value.expected
    assert caught.value.received
    assert caught.value.next_action


def test_unmatched_user_names_are_listed_without_failing_the_load(tmp_path: Path) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    path = _user_file(
        tmp_path / 'config' / 'interop.yaml',
        'credentials:\n  acp:other: {command: [other], env: [OTHER_TOKEN]}\n',
    )
    project = load_config(_write(workspace / '.syndica.yaml', 'version: 1\n'))

    loaded = load_interop_credentials(path, workspace=workspace)
    assert unmatched_credentials(loaded, project.interop) == ('acp:other',)


def test_the_credentials_file_sits_beside_the_trust_store(tmp_path: Path) -> None:
    path = interop_credentials_path(
        {'SYNDICA_TRUST_STORE': str(tmp_path / 'trusted.json'), 'HOME': str(tmp_path)}
    )

    assert path == tmp_path / 'interop.yaml'
