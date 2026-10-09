"""`syndica up / status / down`：真的子进程、真的端口。模型配成一个用不到的 OpenAI 兼容地址，
不需要 key 也不发请求。"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import threading
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
        [sys.executable, '-P', '-m', 'syndica', *args],
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


@pytest.mark.parametrize('planted', ['uvicorn.py', 'syndica/__init__.py'])
def test_the_server_process_never_imports_code_from_the_workspace_root(
    tmp_path: Path, workspace: Path, planted: str
):
    marker = tmp_path / 'imported.json'
    target = workspace / planted
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        'import json, os\n'
        f'open({str(marker)!r}, "w").write(json.dumps({{"key": "OPENAI_API_KEY" in os.environ}}))\n'
        'raise ImportError("planted")\n'
    )
    environ = _environ(tmp_path)
    started = _syndica(workspace, environ, 'up', '--no-open')
    try:
        assert started.returncode == 0, started.stderr
        assert not marker.exists(), marker.read_text()
    finally:
        _syndica(workspace, environ, 'down')


def test_the_openai_sdk_talks_to_syndica_up(tmp_path: Path, workspace: Path) -> None:
    fake = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parents[1] / 'e2e' / 'fake_openai.py')],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert fake.stdout is not None
    port_line = fake.stdout.readline()
    environ = _environ(tmp_path)
    environ['OPENAI_BASE_URL'] = f'http://127.0.0.1:{int(port_line.strip())}/v1'
    started = _syndica(workspace, environ, 'up', '--no-open', '--api-token')
    try:
        assert started.returncode == 0, started.stderr
        token = re.search(r'机器客户端令牌.*\n([A-Za-z0-9_-]+)\n', started.stdout)
        listen = re.search(r'127\.0\.0\.1:(\d+)', started.stdout)
        assert token is not None and listen is not None, started.stdout
        from openai import OpenAI

        client = OpenAI(
            api_key=token.group(1),
            base_url=f'http://127.0.0.1:{listen.group(1)}/v1',
            max_retries=0,
        )
        completion = client.chat.completions.create(
            model='Worker',
            messages=[{'role': 'user', 'content': 'from-the-sdk'}],
        )
        assert completion.choices[0].message.content is not None
        assert '收到：' in completion.choices[0].message.content
        assert _SECRET not in completion.choices[0].message.content
    finally:
        _syndica(workspace, environ, 'down')
        fake.terminate()
        fake.wait(timeout=5)


def test_api_token_is_printed_once_and_absent_from_the_state_file(tmp_path: Path, workspace: Path):
    environ = _environ(tmp_path)
    started = _syndica(workspace, environ, 'up', '--no-open', '--api-token')
    try:
        assert started.returncode == 0, started.stderr
        match = re.search(r'机器客户端令牌.*\n([A-Za-z0-9_-]+)\n', started.stdout)
        assert match is not None, started.stdout
        token = match.group(1)
        port = int(re.search(r'127\.0\.0\.1:(\d+)', started.stdout).group(1))  # type: ignore[union-attr]
        [state_file] = (tmp_path / 'config' / 'syndica' / 'servers').glob('*/server.json')
        blob = b''.join(
            path.read_bytes() for path in state_file.parent.rglob('*') if path.is_file()
        )
        assert token.encode() not in blob
        request = urllib.request.Request(
            f'http://127.0.0.1:{port}/v1/models',
            headers={'Authorization': f'Bearer {token}'},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            listed = json.loads(response.read())
        assert 'Worker' in {item['id'] for item in listed['data']}
        denied = urllib.request.Request(f'http://127.0.0.1:{port}/v1/models')
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(denied, timeout=5)
        assert caught.value.code == 401
    finally:
        _syndica(workspace, environ, 'down')


@pytest.mark.parametrize('command', ['up', 'down'])
def test_up_and_down_refuse_inside_an_agent_environment(
    tmp_path: Path, workspace: Path, command: str
):
    environ = {**_environ(tmp_path), 'SYNDICA_AGENT_ENV': '1'}
    result = _syndica(workspace, environ, command, *(['--no-open'] if command == 'up' else []))
    assert result.returncode != 0
    assert 'Agent' in result.stderr
    assert not (tmp_path / 'config' / 'syndica' / 'servers').exists()


def test_the_server_module_entry_refuses_inside_an_agent_environment(
    tmp_path: Path, workspace: Path
):
    environ = {**_environ(tmp_path), 'SYNDICA_AGENT_ENV': '1'}
    process = subprocess.Popen(
        [sys.executable, '-m', 'syndica.serve', '--foreground'],
        cwd=workspace,
        env=environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=30)
    finally:
        # 拒绝一旦失效，这里起的是一个真的 server：不管断言怎样，进程都不能留下。
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 2
    assert 'Agent' in stderr and 'login' not in stdout
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


def test_the_server_runs_blocking_work_on_a_bounded_set_of_threads():
    executor = serve._blocking_executor()  # pyright: ignore[reportPrivateUsage]
    started = threading.Semaphore(0)
    release = threading.Event()
    names: set[str] = set()

    def job() -> None:
        names.add(threading.current_thread().name)
        started.release()
        release.wait(10)

    futures = [executor.submit(job) for _ in range(20)]
    try:
        for _ in range(serve._BLOCKING_WORKERS):  # pyright: ignore[reportPrivateUsage]
            assert started.acquire(timeout=5)
        assert not started.acquire(timeout=0.3)
    finally:
        release.set()
    for future in futures:
        future.result(timeout=5)
    assert len(names) == serve._BLOCKING_WORKERS  # pyright: ignore[reportPrivateUsage]


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


def test_a_server_that_holds_its_lock_but_does_not_answer_keeps_its_state_file(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    def no_spawn(*_: object) -> dict[str, object]:
        raise AssertionError('up 不该在已有 server 时再起一个')

    # 判定一旦出错，up 会真的起一个 server 进程（继承真实的 HOME）：挡在这里，失败也不留进程。
    monkeypatch.setattr(serve, '_spawn', no_spawn)
    environ = _environ(tmp_path)
    paths = serve._Paths.of(workspace, environ)  # pyright: ignore[reportPrivateUsage]
    paths.make()
    paths.state.write_text(json.dumps({'pid': os.getpid(), 'port': 9, 'nonce': 'live'}))
    held = os.open(paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = serve.status(workspace, environ)
        with pytest.raises(ConfigError) as refused:
            serve.up(
                serve.CliIO(stdin=None, stdout=None, stderr=None),  # type: ignore[arg-type]
                workspace,
                environ,
                config='.syndica.yaml',
                port=0,
                foreground=False,
                open_browser=False,
            )
    finally:
        os.close(held)
    assert result.exit_code == 1
    assert str(os.getpid()) in result.output and 'syndica down' in result.output
    assert paths.state.exists()
    assert '已经有一个在跑' in refused.value.received


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
