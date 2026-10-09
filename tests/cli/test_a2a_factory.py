"""`model: a2a:<名字>` 由组合根解析成还没连上的 A2AAgentModel。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from capture_http import capturing

from syndica.__main__ import _a2a_model, _remember_a2a
from syndica.backends.a2a import A2AAgentModel
from syndica.cli.config import CliConfig, load_config
from syndica.kernel import ConfigError, ContextBlock

_TOKEN = 'ghp_researcher0123456789abcdef'


def _config(tmp_path: Path, url: str = 'http://127.0.0.1:9/a2a/') -> CliConfig:
    prompt = tmp_path / 'prompt.md'
    prompt.write_text('remote', encoding='utf-8')
    path = tmp_path / '.syndica.yaml'
    path.write_text(
        f"""
version: 1
agents:
  - name: Worker
    description: remote worker
    system_prompt: prompt.md
    model: a2a:remote
    tools: [Read]
interop:
  a2a:
    agents:
      - name: remote
        url: {url}
""",
        encoding='utf-8',
    )
    return load_config(path)


def test_an_a2a_model_name_points_at_the_declared_url(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _remember_a2a(config, tmp_path, {})
    model = _a2a_model('a2a:remote')
    assert model.config.provider == 'a2a'
    assert model.config.model == 'remote'


def test_an_unknown_a2a_name_names_the_declared_agents(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _remember_a2a(config, tmp_path, {})
    with pytest.raises(ConfigError, match='interop.a2a.agents'):
        _a2a_model('a2a:missing')


def _environ(tmp_path: Path, pinned: str) -> dict[str, str]:
    store = tmp_path / 'config' / 'trusted.json'
    store.parent.mkdir(parents=True, exist_ok=True)
    credentials = store.parent / 'interop.yaml'
    credentials.write_text(
        'credentials:\n'
        '  a2a:remote:\n'
        f'    url: {json.dumps(pinned)}\n'
        '    bearer_env: RESEARCHER_TOKEN\n',
        encoding='utf-8',
    )
    os.chmod(credentials, 0o600)
    return {'SYNDICA_TRUST_STORE': str(store), 'RESEARCHER_TOKEN': _TOKEN}


def _remote_agent(method: str, path: str, body: bytes) -> tuple[int, object]:
    if method == 'GET' and path == '/.well-known/agent-card.json':
        return 200, {
            'name': 'remote',
            'supportedInterfaces': [{'protocolVersion': '1.0', 'protocolBinding': 'JSONRPC'}],
        }
    request = json.loads(body)
    return 200, {
        'jsonrpc': '2.0',
        'id': request['id'],
        'result': {
            'task': {
                'id': 'task-1',
                'contextId': 'ctx-1',
                'status': {
                    'state': 'TASK_STATE_COMPLETED',
                    'message': {'parts': [{'text': 'done remotely'}]},
                },
            }
        },
    }


async def test_a_url_matching_the_pin_carries_the_bearer_on_rpc_but_not_on_the_public_card(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    with capturing(_remote_agent) as remote:
        url = f'{remote.url}/a2a/'
        _remember_a2a(_config(workspace, url), workspace, _environ(tmp_path, url))
        model = _a2a_model('a2a:remote')
        assert isinstance(model, A2AAgentModel)
        try:
            reply = await model.create([ContextBlock(role='user', content='research this')])
        finally:
            await model.aclose()

        assert reply.content == 'done remotely'
        assert [(seen.method, seen.path, seen.authorization) for seen in remote.seen] == [
            ('GET', '/.well-known/agent-card.json', None),
            ('POST', '/a2a/', f'Bearer {_TOKEN}'),
        ]


def test_a_project_url_other_than_the_pin_gets_no_request_and_names_both(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    with capturing(_remote_agent) as pinned, capturing(_remote_agent) as attacker:
        environ = _environ(tmp_path, f'{pinned.url}/a2a/')

        with pytest.raises(ConfigError) as caught:
            _remember_a2a(_config(workspace, f'{attacker.url}/a2a/'), workspace, environ)

        assert attacker.seen == []
        assert pinned.seen == []
    assert 'a2a:remote' in caught.value.expected
    assert 'RESEARCHER_TOKEN' in caught.value.expected
    assert f'url {pinned.url}/a2a/' in caught.value.expected
    assert f'url {attacker.url}/a2a/' in caught.value.received
    assert 'a2a:remote 的 url' in caught.value.next_action
    assert _TOKEN not in str(caught.value)


def test_an_agent_without_a_user_entry_is_built_without_a_bearer(tmp_path: Path) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    environ = _environ(tmp_path, 'http://127.0.0.1:9/a2a/')
    environ['SYNDICA_TRUST_STORE'] = str(tmp_path / 'elsewhere' / 'trusted.json')

    _remember_a2a(_config(workspace), workspace, environ)

    model = _a2a_model('a2a:remote')
    assert isinstance(model, A2AAgentModel)
    assert model._bearer is None  # pyright: ignore[reportPrivateUsage]
