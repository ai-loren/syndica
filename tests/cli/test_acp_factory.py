"""`model: acp:<名字>` 由组合根解析成还没拉起的 AcpAgentModel。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from syndica.__main__ import _acp_model, _remember_a2a, _remember_acp
from syndica.backends.acp import AcpAgentModel
from syndica.cli.config import CliConfig, load_config
from syndica.kernel import (
    BlockHighRisk,
    ConfigError,
    DangerJudge,
    ExternalCallPolicy,
    Runtime,
)
from syndica.kernel.file_taint import FileTaint
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.tools.local import Bash, BashOutput, KillBash, Read, Write
from syndica.kernel.workspace_router import WorkspaceRouter

_SECRET = 'gemini-secret-0123456789'
_COMMAND = ('/bin/echo', 'acp')


def _config(tmp_path: Path, command: tuple[str, ...] = _COMMAND) -> CliConfig:
    prompt = tmp_path / 'prompt.md'
    prompt.write_text('hosted', encoding='utf-8')
    path = tmp_path / '.syndica.yaml'
    path.write_text(
        f"""
version: 1
agents:
  - name: Worker
    description: hosted worker
    system_prompt: prompt.md
    model: acp:gemini
    tools: [Read]
interop:
  acp:
    agents:
      - name: gemini
        command: {json.dumps(list(command))}
        pass_env: [NO_COLOR]
""",
        encoding='utf-8',
    )
    return load_config(path)


def test_an_acp_model_name_builds_a_hosted_agent_without_starting_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _remember_acp(config, tmp_path, {})
    model = _acp_model('acp:gemini')
    assert model.config.provider == 'acp'
    assert model.config.model == 'gemini'


def test_an_unknown_acp_name_names_the_declared_agents(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _remember_acp(config, tmp_path, {})
    with pytest.raises(ConfigError, match='interop.acp.agents'):
        _acp_model('acp:missing')


def _environ(tmp_path: Path, pinned: tuple[str, ...]) -> dict[str, str]:
    store = tmp_path / 'config' / 'trusted.json'
    store.parent.mkdir(parents=True, exist_ok=True)
    credentials = store.parent / 'interop.yaml'
    credentials.write_text(
        'credentials:\n'
        '  acp:gemini:\n'
        f'    command: {json.dumps(list(pinned))}\n'
        '    env: [GEMINI_API_KEY]\n',
        encoding='utf-8',
    )
    os.chmod(credentials, 0o600)
    return {'SYNDICA_TRUST_STORE': str(store), 'GEMINI_API_KEY': _SECRET, 'NO_COLOR': '1'}


async def test_a_command_matching_the_pin_hands_the_variable_to_the_hosted_process(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    environ = _environ(tmp_path, _COMMAND)
    _remember_acp(_config(workspace), workspace, environ)
    model = _acp_model('acp:gemini')
    assert isinstance(model, AcpAgentModel)
    local = LocalWorkspace(str(workspace), shell='/bin/sh', env={'PATH': '/usr/bin'})
    router = WorkspaceRouter(local, taint=FileTaint(workspace))
    runtime = Runtime(workspace=router)
    try:
        model.bind_runtime(
            runtime,
            {
                'Read': Read(router),
                'Write': Write(router),
                'Bash': Bash(router),
                'BashOutput': BashOutput(router),
                'KillBash': KillBash(router),
            },
            ExternalCallPolicy(judge=DangerJudge(), policy=BlockHighRisk()),
            role='Worker',
            agent_env={'PATH': '/usr/bin'},
            host_env={**environ, 'OTHER_TOKEN': 'not-named'},
            redactor=RuleRedactor(secrets=[_SECRET]),
        )
        assert model._command == _COMMAND  # pyright: ignore[reportPrivateUsage]
        assert model._env == {  # pyright: ignore[reportPrivateUsage]
            'PATH': '/usr/bin',
            'NO_COLOR': '1',
            'GEMINI_API_KEY': _SECRET,
        }
    finally:
        await runtime.shutdown()
        await router.close()


def _loosen(environ: dict[str, str]) -> None:
    os.chmod(Path(environ['SYNDICA_TRUST_STORE']).parent / 'interop.yaml', 0o644)


def test_a_config_without_external_agents_never_reads_the_user_level_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    environ = _environ(tmp_path, _COMMAND)
    _loosen(environ)
    plain = workspace / '.syndica.yaml'
    plain.write_text('version: 1\n', encoding='utf-8')

    _remember_acp(load_config(plain), workspace, environ)
    _remember_a2a(load_config(plain), workspace, environ)


def test_a_config_with_an_external_agent_still_refuses_a_loose_user_level_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    environ = _environ(tmp_path, _COMMAND)
    _loosen(environ)

    with pytest.raises(ConfigError) as caught:
        _remember_acp(_config(workspace), workspace, environ)

    assert '0600' in caught.value.expected
    assert 'chmod 600' in caught.value.next_action


@pytest.mark.parametrize(
    'declared',
    [(*_COMMAND, '--yolo'), ('/tmp/evil', 'acp'), ('/bin/echo acp',)],
    ids=['extra_argument', 'other_program', 'joined_argv'],
)
def test_a_command_other_than_the_pin_is_refused_before_any_process_exists(
    tmp_path: Path, declared: tuple[str, ...]
) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    with pytest.raises(ConfigError) as caught:
        _remember_acp(_config(workspace, declared), workspace, _environ(tmp_path, _COMMAND))

    assert 'acp:gemini' in caught.value.expected
    assert 'GEMINI_API_KEY' in caught.value.expected
    assert f'command {json.dumps(list(_COMMAND))}' in caught.value.expected
    assert f'command {json.dumps(list(declared))}' in caught.value.received
    assert 'acp:gemini 的 command' in caught.value.next_action
    assert _SECRET not in str(caught.value)
