"""`doctor` 报告声明的外部对象：ACP Agent 能否起来与协商到的版本，A2A 卡片上的版本与技能，
MCP 的工具。

全部经组合根的 `build_host` 与 `main` 跑：注入的探测、凭证的钉住与 Agent 环境都与 run 是同一套。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from capture_http import Seen, capturing

import syndica.cli.host as host_module
from syndica.__main__ import build_host
from syndica.cli.environment import AGENT_MARKER
from syndica.cli.main import CliIO, main

_MODEL_ENV = (
    'GEMINI_API_KEY',
    'GEMINI_MODEL',
    'OPENAI_API_KEY',
    'OPENROUTER_API_KEY',
    'OPENROUTER_MODEL',
    'SYNDICA_LLM_ORDER',
)
_PINNED = 'pinned-' + 'Zq9Lm4Xw7Pk2Rt8Vb5Nc3Jd6'
_UNNAMED = 'other-' + 'Hq3Tz8Lw1Mv6Rb9Xc2Kd5Pf'
_BEARER = 'ghp_' + 'researcher0123456789abcdef'
_CARD_PATH = '/.well-known/agent-card.json'

_AGENT = """
import asyncio, json, os, subprocess, sys

marker, version, mode = sys.argv[1], int(sys.argv[2]), sys.argv[3]


def record():
    plain = subprocess.Popen(['/bin/sleep', '300'])
    detached = subprocess.Popen(['/bin/sleep', '300'], start_new_session=True)
    with open(marker + '.tmp', 'w') as handle:
        json.dump({'pids': [os.getpid(), plain.pid, detached.pid], 'env': dict(os.environ)}, handle)
    os.replace(marker + '.tmp', marker)


async def main():
    from acp.agent.connection import AgentSideConnection
    from acp.schema import Implementation, InitializeResponse
    from acp.stdio import stdio_streams

    record()

    class Probed:
        async def initialize(self, protocol_version, **kwargs):
            if mode == 'silent':
                await asyncio.Event().wait()
            return InitializeResponse(
                protocol_version=version,
                agent_info=Implementation(name='fake-acp', version='0.3.1'),
            )

    reader, writer = await stdio_streams()
    await AgentSideConnection(Probed(), writer, reader, listening=False).listen()


asyncio.run(main())
"""

_MCP_SERVER = """\
import asyncio

import mcp_types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

_SCHEMA = {"type": "object", "properties": {}}


async def on_list_tools(ctx, params):
    return types.ListToolsResult(
        tools=[
            types.Tool(name=name, description=name, input_schema=_SCHEMA)
            for name in ("zeta", "alpha", "mid")
        ]
    )


async def on_call_tool(ctx, params):
    return types.CallToolResult(content=[types.TextContent(type="text", text="")])


async def main():
    server = Server("demo", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


asyncio.run(main())
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个与开发者自己的配置、模型 key 和 git 设置都隔开的宿主环境。"""
    for name in _MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    for name in [key for key in os.environ if key.startswith('GIT_')]:
        monkeypatch.delenv(name)
    home = tmp_path / 'home'
    home.mkdir()
    (tmp_path / 'config').mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('SYNDICA_TRUST_STORE', str(tmp_path / 'config' / 'trusted.json'))
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://llm.example.test/v1')
    monkeypatch.setenv('OPENAI_MODEL', 'm')
    root = tmp_path / 'ws'
    root.mkdir()
    return root


def _interop(tmp_path: Path, body: str) -> Path:
    path = tmp_path / 'syndica.yaml'
    path.write_text(f'version: 1\ninterop:\n{body}', encoding='utf-8')
    return path


def _credentials(tmp_path: Path, body: str) -> None:
    path = tmp_path / 'config' / 'interop.yaml'
    path.write_text(f'credentials:\n{body}', encoding='utf-8')
    os.chmod(path, 0o600)


def _agent_command(tmp_path: Path, name: str, version: int = 1, mode: str = 'answer') -> list[str]:
    script = tmp_path / 'fake_acp_agent.py'
    script.write_text(_AGENT, encoding='utf-8')
    return [sys.executable, str(script), str(tmp_path / f'{name}.json'), str(version), mode]


def _acp_agents(*agents: tuple[str, list[str]]) -> str:
    rows = ''.join(
        f'      - name: {name}\n        command: {json.dumps(command)}\n'
        for name, command in agents
    )
    return f'  acp:\n    agents:\n{rows}'


def _a2a_agents(*agents: tuple[str, str]) -> str:
    rows = ''.join(f'      - name: {name}\n        url: {url}\n' for name, url in agents)
    return f'  a2a:\n    agents:\n{rows}'


def _recorded(tmp_path: Path, name: str) -> dict[str, Any]:
    return json.loads((tmp_path / f'{name}.json').read_text(encoding='utf-8'))


def _running(pid: int) -> bool:
    state = subprocess.run(
        ['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(state) and not state.startswith('Z')


def _kill_all(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            continue


class _Doctor:
    def __init__(self, root: Path) -> None:
        self.stdout = StringIO()
        self.stderr = StringIO()
        self.io = CliIO(stdin=StringIO(), stdout=self.stdout, stderr=self.stderr)
        self.host = build_host(self.io, root)
        self.leftovers: list[int] = []

    def run(self, config: Path) -> int:
        return main(['--config', str(config), 'doctor'], host=self.host, io=self.io)

    def lines(self, prefix: str) -> list[str]:
        return [line for line in self.stdout.getvalue().splitlines() if line.startswith(prefix)]

    def prefixes(self) -> list[str]:
        return [line.split(':', 1)[0] for line in self.stdout.getvalue().splitlines()]

    def survivors(self, tmp_path: Path, name: str) -> list[int]:
        """`doctor` 返回的这一刻、宿主还没关时，那个 Agent 和它的孙进程里还在跑的。"""
        pids = [int(pid) for pid in _recorded(tmp_path, name)['pids']]
        self.leftovers.extend(pids)
        return [pid for pid in pids if _running(pid)]

    def close(self) -> None:
        self.host.close()
        _kill_all(self.leftovers)


@pytest.fixture
def doctor(workspace: Path) -> Iterator[_Doctor]:
    opened = _Doctor(workspace)
    try:
        yield opened
    finally:
        opened.close()


# ---- ACP ----


def test_a_hosted_acp_agent_reports_protocol_1_and_leaves_no_process_or_grandchild(
    tmp_path: Path, doctor: _Doctor
):
    config = _interop(tmp_path, _acp_agents(('fake', _agent_command(tmp_path, 'fake'))))

    code = doctor.run(config)

    assert doctor.lines('acp:') == ['acp: ok fake（协议 1；Agent fake-acp 0.3.1）']
    assert doctor.survivors(tmp_path, 'fake') == []
    assert code == 0


def test_an_acp_agent_answering_protocol_2_fails_its_own_line_and_is_reaped(
    tmp_path: Path, doctor: _Doctor
):
    config = _interop(tmp_path, _acp_agents(('v2', _agent_command(tmp_path, 'v2', version=2))))

    code = doctor.run(config)

    (line,) = doctor.lines('acp:')
    assert line.startswith('acp: failed v2（期望: acp:v2 协商到 ACP 协议版本 1。')
    assert '实际: 对端给出 protocolVersion=2。' in line
    assert '下一步: 换一个说协议版本 1 的 ACP Agent' in line
    assert doctor.survivors(tmp_path, 'v2') == []
    assert code == 1


def test_an_acp_agent_that_never_answers_times_out_and_is_reaped(
    tmp_path: Path, doctor: _Doctor, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(host_module, '_EXTERNAL_PROBE_TIMEOUT', 1.0)
    command = _agent_command(tmp_path, 'mute', mode='silent')
    config = _interop(tmp_path, _acp_agents(('mute', command)))

    code = doctor.run(config)

    (line,) = doctor.lines('acp:')
    assert line.startswith('acp: failed mute（期望: acp:mute 在 1 秒内答复 initialize。')
    assert '下一步: 在终端里单独跑这条 command' in line
    assert doctor.survivors(tmp_path, 'mute') == []
    assert code == 1


def test_an_acp_command_that_cannot_start_fails_its_own_line_and_the_next_agent_is_still_probed(
    tmp_path: Path, doctor: _Doctor
):
    missing = str(tmp_path / 'no-such-agent')
    config = _interop(
        tmp_path,
        _acp_agents(('gone', [missing]), ('fake', _agent_command(tmp_path, 'fake'))),
    )

    code = doctor.run(config)

    gone, fake = doctor.lines('acp:')
    assert gone.startswith(f"acp: failed gone（期望: 能拉起 '{missing}'。实际: 找不到这个程序")
    assert fake == 'acp: ok fake（协议 1；Agent fake-acp 0.3.1）'
    assert doctor.survivors(tmp_path, 'fake') == []
    assert code == 1


def test_the_hosted_agent_gets_the_agent_environment_plus_only_the_pinned_variable(
    tmp_path: Path, doctor: _Doctor, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv('DOCTOR_PINNED_TOKEN', _PINNED)
    monkeypatch.setenv('DOCTOR_UNNAMED_TOKEN', _UNNAMED)
    command = _agent_command(tmp_path, 'fake')
    _credentials(
        tmp_path,
        f'  acp:fake:\n    command: {json.dumps(command)}\n    env: [DOCTOR_PINNED_TOKEN]\n',
    )
    config = _interop(tmp_path, _acp_agents(('fake', command)))

    code = doctor.run(config)

    env = _recorded(tmp_path, 'fake')['env']
    assert isinstance(env, dict)
    assert env.get('DOCTOR_PINNED_TOKEN') == _PINNED
    assert 'DOCTOR_UNNAMED_TOKEN' not in env
    assert env.get(AGENT_MARKER) == '1'
    assert doctor.lines('acp:') == ['acp: ok fake（协议 1；Agent fake-acp 0.3.1）']
    assert _PINNED not in doctor.stdout.getvalue() + doctor.stderr.getvalue()
    assert doctor.survivors(tmp_path, 'fake') == []
    assert code == 0


def test_a_hosted_agent_without_a_pin_does_not_see_the_host_variable(
    tmp_path: Path, doctor: _Doctor, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv('DOCTOR_PINNED_TOKEN', _PINNED)
    config = _interop(tmp_path, _acp_agents(('fake', _agent_command(tmp_path, 'fake'))))

    doctor.run(config)

    env = _recorded(tmp_path, 'fake')['env']
    assert isinstance(env, dict)
    assert 'DOCTOR_PINNED_TOKEN' not in env
    assert doctor.survivors(tmp_path, 'fake') == []


# ---- A2A ----


def _card(*, versions: tuple[str, ...], skills: tuple[str, ...]):
    def reply(method: str, path: str, body: bytes) -> tuple[int, object]:
        del body
        if method == 'GET' and path == _CARD_PATH:
            return 200, {
                'name': 'remote',
                'supportedInterfaces': [
                    {'url': 'x', 'protocolBinding': 'JSONRPC', 'protocolVersion': version}
                    for version in versions
                ],
                'skills': [{'id': f'id-{name}', 'name': name} for name in skills],
            }
        return 404, {'error': 'not here'}

    return reply


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def test_the_a2a_card_lists_versions_and_skills_and_is_fetched_without_the_pinned_bearer(
    tmp_path: Path, doctor: _Doctor, monkeypatch: pytest.MonkeyPatch
):
    reply = _card(versions=('1.0', '0.3'), skills=('Worker', 'Analyst', 'Reviewer'))
    with capturing(reply) as remote:
        url = f'{remote.url}/a2a/'
        monkeypatch.setenv('RESEARCHER_TOKEN', _BEARER)
        _credentials(tmp_path, f'  a2a:remote:\n    url: {url}\n    bearer_env: RESEARCHER_TOKEN\n')
        config = _interop(tmp_path, _a2a_agents(('remote', url)))

        code = doctor.run(config)

    assert remote.seen == [Seen('GET', _CARD_PATH, None)]
    assert doctor.lines('a2a:') == [
        'a2a: ok remote（协议 1.0, 0.3；技能 Worker, Analyst, Reviewer）'
    ]
    assert _BEARER not in doctor.stdout.getvalue() + doctor.stderr.getvalue()
    assert code == 0


def test_an_unreachable_a2a_agent_fails_its_own_line_and_doctor_goes_on(
    tmp_path: Path, doctor: _Doctor
):
    down = f'http://127.0.0.1:{_closed_port()}/a2a/'
    with capturing(_card(versions=('1.0',), skills=('Worker',))) as remote:
        config = _interop(tmp_path, _a2a_agents(('down', down), ('up', f'{remote.url}/a2a/')))

        code = doctor.run(config)

    failed, ok = doctor.lines('a2a:')
    assert failed.startswith(
        f'a2a: failed down（期望: a2a:down 的 agent card 能从 '
        f'{down.removesuffix("a2a/")}.well-known/agent-card.json 取到。实际: 连不上：'
    )
    assert '下一步: 确认远端 A2A Agent 在跑' in failed
    assert ok == 'a2a: ok up（协议 1.0；技能 Worker）'
    assert doctor.prefixes()[-4:] == ['workspace', 'env', 'git', 'python']
    assert code == 1


def test_an_a2a_card_declaring_only_an_unknown_version_fails_its_line(
    tmp_path: Path, doctor: _Doctor
):
    with capturing(_card(versions=('2.0',), skills=('Worker',))) as remote:
        config = _interop(tmp_path, _a2a_agents(('next', f'{remote.url}/a2a/')))

        code = doctor.run(config)

    (line,) = doctor.lines('a2a:')
    assert line.startswith('a2a: failed next（期望: a2a:next 的卡片在 supportedInterfaces 里')
    assert "实际: 声明了 ['2.0']。" in line
    assert code == 1


def test_skills_past_the_cap_are_counted_instead_of_listed(tmp_path: Path, doctor: _Doctor):
    skills = tuple(f's{index}' for index in range(40))
    with capturing(_card(versions=('1.0',), skills=skills)) as remote:
        config = _interop(tmp_path, _a2a_agents(('many', f'{remote.url}/a2a/')))

        doctor.run(config)

    (line,) = doctor.lines('a2a:')
    listed = ', '.join(skills[:32])
    assert line == f'a2a: ok many（协议 1.0；技能 {listed}，另有 8 个没列出）'


# ---- MCP ----


def test_doctor_lists_every_tool_an_mcp_server_exposes_in_its_order(
    tmp_path: Path, doctor: _Doctor
):
    script = tmp_path / 'srv.py'
    script.write_text(_MCP_SERVER, encoding='utf-8')
    command = json.dumps([sys.executable, str(script)])
    config = _interop(
        tmp_path,
        f'  mcp:\n    servers:\n      - name: files\n        command: {command}\n'
        '        roles: [Worker]\n',
    )

    code = doctor.run(config)

    assert doctor.lines('mcp: tools') == ['mcp: tools files files__zeta, files__alpha, files__mid']
    assert code == 0
