"""宿主的每条收尾路径之后，外部 MCP 服务和它的孙进程都不在：会话关闭、Runtime 关机、Ctrl-C。

服务是真的 SDK stdio 服务，起来时留下两个孙进程：一个留在它的进程组里，一个自己 setsid 离开。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from syndica.__main__ import _open_mcp
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, RunRequest
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import ScriptedLLM, ScriptedTurn

_SERVER = Path(__file__).resolve().parents[1] / 'fixtures' / 'mcp_spawning_server.py'
# 子进程里的事都有上限：机器忙时起一个 Python、建一个 Runtime 就要好几秒。
_WITHIN = 30.0


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    config = workspace / '.syndica.yaml'
    # 服务的工作目录是工作区根。marker 写成相对路径：tmp 路径里带着用例名，名字里的 session 这类
    # 词会让 command 被当成点名了密钥而拒收。
    config.write_text(
        'version: 1\n'
        'interop:\n'
        '  mcp:\n'
        '    servers:\n'
        '      - name: files\n'
        f'        command: {json.dumps([sys.executable, str(_SERVER), "../mcp.pids"])}\n'
        '        roles: [Worker]\n',
        encoding='utf-8',
    )
    return workspace, config, tmp_path / 'mcp.pids'


def _pids(marker: Path, alive: Any = None) -> dict[str, int]:
    deadline = time.monotonic() + _WITHIN
    while not marker.exists():
        if alive is not None and not alive():
            pytest.fail('the host exited before the MCP server reported its grandchildren')
        if time.monotonic() > deadline:
            pytest.fail(f'the MCP server did not report its grandchildren within {_WITHIN:g}s')
        time.sleep(0.05)
    pids = json.loads(marker.read_text(encoding='utf-8'))
    assert set(pids) == {'server', 'plain', 'detached'}
    assert _survivors(pids) == sorted(pids)
    assert os.getpgid(pids['plain']) == os.getpgid(pids['server'])
    assert os.getpgid(pids['detached']) == pids['detached']
    return pids


def _running(pid: int) -> bool:
    state = subprocess.run(
        ['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(state) and not state.startswith('Z')


def _survivors(pids: dict[str, int]) -> list[str]:
    return sorted(name for name, pid in pids.items() if _running(pid))


def _kill_all(pids: dict[str, int]) -> None:
    for pid in pids.values():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue


def _host(workspace: Path, llm: ScriptedLLM) -> RuntimeCliHost:
    return RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=workspace,
        mcp_opener=_open_mcp,
    )


def _assembled(tmp_path: Path, *, runtime_only: bool) -> tuple[dict[str, int], list[str]]:
    """装一个会话、收掉它，在事件循环还活着时查进程表：循环收尾会取消挂着的连接任务，
    在那之后才查就分不出是谁收的。`runtime_only` 时只关 Runtime，会话的其余部分查完再收。"""
    workspace, config, marker = _workspace(tmp_path)
    host = _host(workspace, ScriptedLLM([ScriptedTurn(content='idle')]))
    pids: dict[str, int] = {}

    async def body() -> list[str]:
        parts = await host.assemble_session(
            str(config), max_turns=None, trace=None, argv=(), trusted_output=(), role='Worker'
        )
        closed = False
        try:
            pids.update(_pids(marker))
            if runtime_only:
                await parts.runtime.shutdown()
            else:
                closed = True
                await host.close_session(parts, None)
            return _survivors(pids)
        finally:
            if not closed:
                await host.close_session(parts, None)

    try:
        return pids, asyncio.run(body())
    finally:
        host.close()
        _kill_all(pids)


def test_close_session_leaves_neither_the_server_nor_its_grandchildren(tmp_path: Path):
    pids, survivors = _assembled(tmp_path, runtime_only=False)

    assert set(pids) == {'server', 'plain', 'detached'}
    assert survivors == []


def test_runtime_shutdown_alone_leaves_neither_the_server_nor_its_grandchildren(tmp_path: Path):
    pids, survivors = _assembled(tmp_path, runtime_only=True)

    assert set(pids) == {'server', 'plain', 'detached'}
    assert survivors == []


@pytest.mark.parametrize('interrupted', [False, True], ids=['closed', 'interrupted'])
def test_closing_a_chat_leaves_neither_the_server_nor_its_grandchildren(
    tmp_path: Path, interrupted: bool
):
    workspace, config, marker = _workspace(tmp_path)
    host = _host(workspace, ScriptedLLM([ScriptedTurn(content='idle')]))
    pids: dict[str, int] = {}
    try:
        host.open_chat(str(config))
        pids = _pids(marker)
        host.close(interrupted=interrupted)
        assert _survivors(pids) == []
    finally:
        host.close()
        _kill_all(pids)


def test_a_finished_run_shuts_the_runtime_down_without_leaving_a_grandchild(tmp_path: Path):
    workspace, config, marker = _workspace(tmp_path)
    host = _host(workspace, ScriptedLLM([ScriptedTurn(content='done')]))
    pids: dict[str, int] = {}
    try:
        answer = host.run_request(RunRequest(prompt='go', agent='Worker', config=str(config)))
        assert answer == 'done'
        pids = json.loads(marker.read_text(encoding='utf-8'))
        assert set(pids) == {'server', 'plain', 'detached'}
        assert _survivors(pids) == []
    finally:
        host.close()
        _kill_all(pids)


class _Model:
    """OpenAI 兼容端点：第一次让 Worker 调 `files__hold`，之后只答一句。"""

    def __init__(self) -> None:
        self.calls = 0
        model = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                model.calls += 1
                if model.calls == 1:
                    message: dict[str, Any] = {
                        'role': 'assistant',
                        'content': None,
                        'tool_calls': [
                            {
                                'id': 'call-1',
                                'type': 'function',
                                'function': {'name': 'files__hold', 'arguments': '{}'},
                            }
                        ],
                    }
                    finish = 'tool_calls'
                else:
                    message, finish = {'role': 'assistant', 'content': 'done'}, 'stop'
                data = json.dumps(
                    {
                        'id': f'fake-{model.calls}',
                        'object': 'chat.completion',
                        'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
                        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
                    }
                ).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def model() -> Iterator[_Model]:
    served = _Model()
    try:
        yield served
    finally:
        served.close()


def _env(tmp_path: Path, port: int) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(('SYNDICA_', 'OPENAI_', 'GEMINI_', 'OPENROUTER_', 'XDG_'))
    }
    env.update(
        {
            'XDG_CONFIG_HOME': str(tmp_path / 'config'),
            'SYNDICA_LLM_ORDER': 'openai',
            'OPENAI_BASE_URL': f'http://127.0.0.1:{port}/v1',
            'OPENAI_MODEL': 'fake',
            'OPENAI_API_KEY': 'e2e-dummy-key-0123456789',
        }
    )
    return env


def _trust(workspace: Path, env: dict[str, str]) -> None:
    command = [sys.executable, '-P', '-m', 'syndica', 'config', 'trust']
    probe = subprocess.run(command, cwd=workspace, env=env, capture_output=True, text=True)
    digest = re.search(r'--sha256 ([0-9a-f]{64})', probe.stdout + probe.stderr)
    assert digest is not None, probe.stdout + probe.stderr
    trusted = subprocess.run(
        [*command, '--sha256', digest.group(1)],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )
    assert trusted.returncode == 0, trusted.stdout + trusted.stderr


class _Cli:
    """`python -m syndica run` 起在自己的进程组里：Ctrl-C 发给整组，与终端按键一样。"""

    def __init__(self, workspace: Path, env: dict[str, str], argv: list[str]) -> None:
        self.process = subprocess.Popen(
            [sys.executable, '-P', '-m', 'syndica', *argv],
            cwd=workspace,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self._chunks: list[bytes] = []
        self._readers = [
            threading.Thread(target=self._drain, args=(stream,), daemon=True)
            for stream in (self.process.stdout, self.process.stderr)
        ]
        for reader in self._readers:
            reader.start()

    def _drain(self, stream: Any) -> None:
        for chunk in iter(lambda: stream.read1(4096), b''):
            self._chunks.append(chunk)

    @property
    def output(self) -> str:
        return b''.join(self._chunks).decode('utf-8', 'replace')

    def alive(self) -> bool:
        return self.process.poll() is None

    def wait_for(self, ready: Any, what: str) -> None:
        deadline = time.monotonic() + _WITHIN
        while not ready():
            if not self.alive():
                pytest.fail(f'the CLI exited before {what}:\n{self.output}')
            if time.monotonic() > deadline:
                pytest.fail(f'{what} did not happen within {_WITHIN:g}s:\n{self.output}')
            time.sleep(0.05)

    def answer(self, line: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(line.encode())
        self.process.stdin.flush()

    def ctrl_c(self) -> int:
        os.killpg(self.process.pid, signal.SIGINT)
        try:
            return self.process.wait(timeout=_WITHIN)
        except subprocess.TimeoutExpired:
            pytest.fail(f'the CLI did not exit within {_WITHIN:g}s of Ctrl-C:\n{self.output}')

    def kill(self) -> None:
        if self.alive():
            self.process.kill()
            self.process.wait()
        for reader in self._readers:
            reader.join(5)


@pytest.mark.timeout(120)
def test_ctrl_c_during_an_mcp_call_leaves_neither_the_server_nor_its_grandchildren(
    tmp_path: Path, model: _Model
):
    workspace, _config, marker = _workspace(tmp_path)
    env = _env(tmp_path, model.port)
    _trust(workspace, env)
    cli = _Cli(workspace, env, ['run', '--agent', 'Worker', 'go'])
    pids: dict[str, int] = {}
    try:
        pids = _pids(marker, cli.alive)
        cli.wait_for(lambda: '[approve/reject]' in cli.output, 'the confirmation question')
        cli.answer('approve\n')
        held = marker.with_name(marker.name + '.held')
        cli.wait_for(held.exists, 'the MCP call started')

        code = cli.ctrl_c()

        assert code == 130, cli.output
        assert _survivors(pids) == []
        assert 'Traceback' not in cli.output
    finally:
        cli.kill()
        _kill_all(pids)
