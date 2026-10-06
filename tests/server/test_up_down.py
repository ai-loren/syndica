"""`syndica up / status / down`：真的子进程、真的端口。模型配成一个用不到的 OpenAI 兼容地址，
不需要 key 也不发请求。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from syndica import serve
from syndica.kernel import ConfigError

_SECRET = 'sk-test-abcdefghijklmnopqrstuv'


def _environ(tmp_path: Path) -> dict[str, str]:
    environ = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(('SYNDICA_', 'OPENAI_', 'GEMINI_', 'OPENROUTER_', 'XDG_'))
    }
    environ.update(
        {
            'XDG_CONFIG_HOME': str(tmp_path / 'config'),
            'SYNDICA_LLM_ORDER': 'openai',
            'OPENAI_BASE_URL': 'http://127.0.0.1:9/v1',
            'OPENAI_MODEL': 'unused',
            'OPENAI_API_KEY': _SECRET,
        }
    )
    return environ


def _syndica(
    workspace: Path, environ: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, '-m', 'syndica', *args],
        cwd=workspace,
        env=environ,
        capture_output=True,
        text=True,
        timeout=90,
    )


def _get(url: str, token: str | None = None) -> dict[str, object]:
    request = urllib.request.Request(url)
    if token is not None:
        request.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def _login(port: int, code: str) -> str:
    request = urllib.request.Request(
        f'http://127.0.0.1:{port}/api/v1/login',
        data=json.dumps({'code': code}).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())['token']


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / 'ws'
    root.mkdir()
    return root


def test_up_status_down_round_trip_without_leaving_a_credential_anywhere(
    tmp_path: Path, workspace: Path
):
    environ = _environ(tmp_path)
    started = _syndica(workspace, environ, 'up', '--no-open')
    try:
        assert started.returncode == 0, started.stderr
        match = re.search(r'http://127\.0\.0\.1:(\d+)/#login=([A-Za-z0-9_-]+)', started.stdout)
        assert match is not None, started.stdout
        port, code = int(match.group(1)), match.group(2)
        [state_file] = (tmp_path / 'config' / 'syndica' / 'servers').glob('*/server.json')
        state = json.loads(state_file.read_text())
        assert set(state) == {'pid', 'port', 'nonce', 'workspace', 'started_at'}
        assert _get(f'http://127.0.0.1:{port}/api/v1/health')['nonce'] == state['nonce']
        token = _login(port, code)
        assert _get(f'http://127.0.0.1:{port}/api/v1/roles', token)['roles']
        with pytest.raises(urllib.error.HTTPError):
            _login(port, code)
        for path in [*workspace.rglob('*'), *state_file.parent.rglob('*')]:
            if path.is_file():
                text = path.read_bytes()
                assert code.encode() not in text and token.encode() not in text, path
                assert _SECRET.encode() not in text, path

        status = _syndica(workspace, environ, 'status')
        assert status.returncode == 0 and f'127.0.0.1:{port}' in status.stdout

        again = _syndica(workspace, environ, 'up', '--no-open')
        assert again.returncode != 0
        assert f'127.0.0.1:{port}' in again.stderr and 'syndica down' in again.stderr

        state_file.rename(state_file.with_suffix('.moved'))
        locked = _syndica(workspace, environ, 'up', '--no-open')
        state_file.with_suffix('.moved').rename(state_file)
        assert locked.returncode != 0
        assert 'server 锁' in locked.stderr
    finally:
        stopped = _syndica(workspace, environ, 'down')
    assert stopped.returncode == 0, stopped.stderr
    assert not state_file.exists()
    with pytest.raises(urllib.error.URLError):
        _get(f'http://127.0.0.1:{port}/api/v1/health')
    after = _syndica(workspace, environ, 'status')
    assert after.returncode == 3


@pytest.mark.parametrize('command', ['up', 'down'])
def test_up_and_down_refuse_inside_an_agent_environment(
    tmp_path: Path, workspace: Path, command: str
):
    environ = {**_environ(tmp_path), 'SYNDICA_AGENT_ENV': '1'}
    result = _syndica(workspace, environ, command, *(['--no-open'] if command == 'up' else []))
    assert result.returncode != 0
    assert 'Agent' in result.stderr
    assert not (tmp_path / 'config' / 'syndica' / 'servers').exists()


def test_a_port_in_use_is_reported_with_three_parts(tmp_path: Path, workspace: Path):
    import socket

    blocker = socket.socket()
    blocker.bind(('127.0.0.1', 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    try:
        result = _syndica(workspace, _environ(tmp_path), 'up', '--no-open', '--port', str(port))
    finally:
        blocker.close()
    assert result.returncode != 0
    assert '--port' in result.stderr


def test_missing_server_dependencies_point_at_the_extra(monkeypatch: pytest.MonkeyPatch):
    real = serve.importlib.util.find_spec
    monkeypatch.setattr(
        serve.importlib.util,
        'find_spec',
        lambda name: None if name == 'uvicorn' else real(name),
    )
    with pytest.raises(ConfigError) as caught:
        serve._require_dependencies()  # pyright: ignore[reportPrivateUsage]
    assert 'uvicorn' in caught.value.received
    assert 'syndica[server]' in caught.value.next_action


def test_the_state_directory_must_be_outside_the_workspace(tmp_path: Path):
    with pytest.raises(ConfigError) as caught:
        serve._Paths.of(tmp_path, {'XDG_CONFIG_HOME': str(tmp_path / 'inside')})  # pyright: ignore[reportPrivateUsage]
    assert '工作区之外' in caught.value.expected


def test_a_state_file_whose_server_is_gone_is_reported_and_removed(tmp_path: Path, workspace: Path):
    environ = _environ(tmp_path)
    paths = serve._Paths.of(workspace, environ)  # pyright: ignore[reportPrivateUsage]
    paths.make()
    paths.state.write_text(json.dumps({'pid': 1, 'port': 9, 'nonce': 'old'}))
    result = serve.status(workspace, environ)
    assert result.exit_code == 3
    assert not paths.state.exists()


def test_down_stops_gracefully_with_a_stream_open_and_the_session_saved(
    tmp_path: Path, workspace: Path
):
    import threading

    environ = _environ(tmp_path)
    started = _syndica(workspace, environ, 'up', '--no-open')
    assert started.returncode == 0, started.stderr
    match = re.search(r'http://127\.0\.0\.1:(\d+)/#login=([A-Za-z0-9_-]+)', started.stdout)
    assert match is not None
    port, token = int(match.group(1)), _login(int(match.group(1)), match.group(2))
    request = urllib.request.Request(
        f'http://127.0.0.1:{port}/api/v1/sessions',
        data=b'{"role": "Worker"}',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        sid = json.loads(response.read())['sid']
    ended = threading.Event()

    def read_stream() -> None:
        stream = urllib.request.Request(
            f'http://127.0.0.1:{port}/api/v1/events', headers={'Authorization': f'Bearer {token}'}
        )
        try:
            with urllib.request.urlopen(stream, timeout=30) as response:
                while response.read(1024):
                    pass
        except Exception:
            pass
        ended.set()

    reader = threading.Thread(target=read_stream, daemon=True)
    reader.start()
    began = time.monotonic()
    stopped = _syndica(workspace, environ, 'down')
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert '已停止' in stopped.stdout
    assert time.monotonic() - began < serve._GRACEFUL_S - 1  # pyright: ignore[reportPrivateUsage]
    assert ended.wait(5)
    assert (workspace / '.syndica' / 'sessions' / sid / 'record.json').is_file()
