"""真的 `syndica acp` 子进程：线上往返与黄金文件逐字相同、stdout 只有协议、stdin 断开时挂着的确认
按拒绝落定、换一个进程 load 回旧会话接着跑。模型是本地假端点，不花钱。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from acp.client.connection import ClientSideConnection
from acp.exceptions import RequestError
from acp.schema import ImageContentBlock, RequestPermissionResponse, TextContentBlock

from syndica.kernel import FileSessionStore
from syndica.kernel.history_blocks import kind_of

_E2E = Path(__file__).resolve().parents[1] / 'e2e'
_GOLDEN = Path(__file__).parent / 'testdata' / 'acp_agent.jsonl'
_KEY = 'acp-process-dummy-key-0123456789'
_CONFIG = (
    'version: 1\n'
    'agents:\n'
    '  - name: Worker\n'
    '    description: 读文件\n'
    '    system_prompt: 读文件\n'
    '    tools: [Read, Bash]\n'
)
_TIMEOUT = 40

# 包一层 tests/e2e/fake_openai.py，换掉它按关键词作答的那一段：
# `read notes` 读 notes.md；`$ <命令>` 原样跑 Bash；读回工具结果之后一句定稿；其余回声。
_MODEL = """\
import json
import sys

sys.path.insert(0, sys.argv[1])
import fake_openai


def _text(message):
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ' '.join(str(part.get('text', '')) for part in content if isinstance(part, dict))
    return ''


def _human(messages):
    trailing = []
    for message in reversed(messages):
        if message.get('role') != 'user':
            break
        text = _text(message)
        if not text.startswith(('你的预算', '（恢复）')):
            trailing.append(text)
    return trailing[-1] if trailing else ''


def _answer(message):
    return {
        'id': 'fake',
        'object': 'chat.completion',
        'choices': [
            {
                'index': 0,
                'message': message,
                'finish_reason': 'tool_calls' if 'tool_calls' in message else 'stop',
            }
        ],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
    }


def _call(name, arguments):
    call = {
        'id': 'call-1',
        'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments)},
    }
    return _answer({'role': 'assistant', 'content': None, 'tool_calls': [call]})


def reply(body, index):
    messages = body.get('messages', [])
    last = messages[-1] if messages else {}
    if last.get('role') == 'tool':
        answer = 'saw alpha-marker' if 'alpha-marker' in _text(last) else 'no marker'
        return _answer({'role': 'assistant', 'content': answer})
    human = _human(messages)
    if human == 'read notes':
        return _call('Read', {'path': 'notes.md'})
    if human.startswith('$ '):
        return _call('Bash', {'command': human[2:]})
    return _answer({'role': 'assistant', 'content': 'echo: ' + human})


fake_openai.reply = reply
del sys.argv[1:]
fake_openai.main()
"""


def _env(config_home: Path, port: int) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(('SYNDICA_', 'OPENAI_', 'GEMINI_', 'OPENROUTER_', 'XDG_'))
    }
    env['XDG_CONFIG_HOME'] = str(config_home)
    env['SYNDICA_LLM_ORDER'] = 'openai'
    env['OPENAI_BASE_URL'] = f'http://127.0.0.1:{port}/v1'
    env['OPENAI_MODEL'] = 'fake'
    env['OPENAI_API_KEY'] = _KEY
    return env


def _trust(workspace: Path, env: dict[str, str]) -> None:
    command = [sys.executable, '-m', 'syndica', 'config', 'trust']
    probe = subprocess.run(command, cwd=workspace, env=env, capture_output=True, text=True)
    digest = re.search(r'--sha256 ([0-9a-f]{64})', probe.stdout + probe.stderr)
    if digest is None:
        raise AssertionError(probe.stdout + probe.stderr)
    trusted = subprocess.run(
        [*command, '--sha256', digest.group(1)],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
    )
    if trusted.returncode != 0:
        raise AssertionError(trusted.stdout + trusted.stderr)


class _World:
    def __init__(self, workspace: Path, env: dict[str, str], config_home: Path) -> None:
        self.workspace = workspace
        self.env = env
        self.config_home = config_home

    def log(self) -> str:
        logs = list(self.config_home.rglob('acp.log'))
        assert len(logs) == 1
        assert self.workspace not in logs[0].parents
        return logs[0].read_text(encoding='utf-8')

    def sessions(self) -> list[str]:
        root = self.workspace / '.syndica' / 'sessions'
        return sorted(path.name for path in root.iterdir() if path.is_dir())


@asynccontextmanager
async def _world(tmp_path: Path, *, trusted: bool = True) -> AsyncIterator[_World]:
    script = tmp_path / 'model.py'
    script.write_text(_MODEL, encoding='utf-8')
    fake = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        str(_E2E),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        assert fake.stdout is not None
        port = int((await asyncio.wait_for(fake.stdout.readline(), 10)).strip())
        workspace = tmp_path / 'ws'
        workspace.mkdir()
        (workspace / '.syndica.yaml').write_text(_CONFIG, encoding='utf-8')
        (workspace / 'notes.md').write_text('alpha-marker\n', encoding='utf-8')
        config_home = tmp_path / 'config'
        env = _env(config_home, port)
        if trusted:
            _trust(workspace, env)
        yield _World(workspace, env, config_home)
    finally:
        if fake.returncode is None:
            fake.terminate()
            await fake.wait()


@asynccontextmanager
async def _acp(world: _World) -> AsyncIterator[asyncio.subprocess.Process]:
    agent = await asyncio.create_subprocess_exec(
        sys.executable,
        '-m',
        'syndica',
        'acp',
        cwd=world.workspace,
        env=world.env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert agent.stderr is not None
    stderr = asyncio.create_task(agent.stderr.read())
    try:
        yield agent
    finally:
        if agent.returncode is None:
            os.killpg(agent.pid, signal.SIGKILL)
            await agent.wait()
        err = (await asyncio.wait_for(stderr, 10)).decode()
        assert _KEY not in err


class _Editor:
    """编辑器一侧：记下推来的消息块；确认请求按 `hold` 决定是不是一直不答。"""

    def __init__(self, *, hold: bool = False) -> None:
        self.users: list[str] = []
        self.agent: list[str] = []
        self.tools: list[str] = []
        self.asked = asyncio.Event()
        self._hold = hold

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        del session_id, kwargs
        kind = getattr(update, 'session_update', None)
        text = getattr(getattr(update, 'content', None), 'text', None)
        if kind == 'user_message_chunk' and isinstance(text, str):
            self.users.append(text)
        elif kind == 'agent_message_chunk' and isinstance(text, str):
            self.agent.append(text)
        elif kind == 'tool_call':
            self.tools.append(str(getattr(update, 'title', '')))

    async def request_permission(
        self, *args: object, **kwargs: object
    ) -> RequestPermissionResponse:
        del args, kwargs
        self.asked.set()
        if not self._hold:
            raise AssertionError('no permission request expected')
        await asyncio.Event().wait()
        raise AssertionError('unreachable')


class _Wire:
    """`syndica acp` 两个方向上的原始字节，按经过中继的先后逐行记下。"""

    def __init__(self) -> None:
        self.lines: list[tuple[str, bytes]] = []
        self.done = asyncio.Event()

    def from_agent(self) -> list[bytes]:
        return [line for side, line in self.lines if side == 'agent']


@asynccontextmanager
async def _relayed(
    agent: asyncio.subprocess.Process, editor: _Editor
) -> AsyncIterator[tuple[ClientSideConnection, _Wire]]:
    assert agent.stdin is not None and agent.stdout is not None
    stdin, stdout = agent.stdin, agent.stdout
    left, right = socket.socketpair()
    client_reader, client_writer = await asyncio.open_connection(sock=left)
    relay_reader, relay_writer = await asyncio.open_connection(sock=right)
    wire = _Wire()

    async def up() -> None:
        while line := await relay_reader.readline():
            wire.lines.append(('client', line))
            stdin.write(line)
            await stdin.drain()
        stdin.close()

    async def down() -> None:
        while line := await stdout.readline():
            wire.lines.append(('agent', line))
            relay_writer.write(line)
            await relay_writer.drain()
        relay_writer.close()
        wire.done.set()

    relays = [asyncio.create_task(up()), asyncio.create_task(down())]
    client = ClientSideConnection(editor, client_writer, client_reader)  # pyright: ignore[reportArgumentType]
    try:
        yield client, wire
    finally:
        await client.close()
        # 连接的 close 不关它的写端：不关，中继读不到 EOF，`syndica acp` 的 stdin 就一直开着。
        client_writer.close()
        async with asyncio.timeout(_TIMEOUT):
            await wire.done.wait()
        for relay in relays:
            relay.cancel()
        await asyncio.gather(*relays, return_exceptions=True)


async def _until(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(_TIMEOUT):
        while not check():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


async def _ask(client: ClientSideConnection, session_id: str, text: str) -> str:
    result = await asyncio.wait_for(
        client.prompt(session_id=session_id, prompt=[TextContentBlock(type='text', text=text)]),
        _TIMEOUT,
    )
    return result.stop_reason


def _normalized(value: object, workspace: Path) -> object:
    if isinstance(value, dict):
        items = cast(dict[str, object], value)
        out: dict[str, object] = {}
        for key, item in items.items():
            if key == 'sessionId':
                out[key] = 'SESSION'
            elif key == 'toolCallId':
                out[key] = 'TOOL_CALL'
            elif key == 'cwd' and item == str(workspace):
                out[key] = 'CWD'
            elif key == 'agentInfo' and isinstance(item, dict):
                out[key] = {**cast(dict[str, object], item), 'version': 'VERSION'}
            else:
                out[key] = _normalized(item, workspace)
        return out
    if isinstance(value, list):
        return [_normalized(item, workspace) for item in cast(list[object], value)]
    return value


async def test_a_scripted_read_round_trip_matches_the_golden_wire(tmp_path: Path) -> None:
    async with _world(tmp_path) as world, _acp(world) as agent:
        editor = _Editor()
        async with _relayed(agent, editor) as (client, wire):
            await asyncio.wait_for(client.initialize(protocol_version=1), _TIMEOUT)
            opened = await asyncio.wait_for(client.new_session(cwd=str(world.workspace)), _TIMEOUT)
            await client.set_session_mode(session_id=opened.session_id, mode_id='Worker')
            assert await _ask(client, opened.session_id, 'read notes') == 'end_turn'
        assert await asyncio.wait_for(agent.wait(), _TIMEOUT) == 0
    assert editor.agent == ['saw alpha-marker']
    rendered = ''.join(
        json.dumps(
            {'from': side, 'message': _normalized(json.loads(line), world.workspace)},
            ensure_ascii=False,
        )
        + '\n'
        for side, line in wire.lines
    )
    assert rendered == _GOLDEN.read_text(encoding='utf-8')


def _is_json_rpc(line: bytes) -> bool:
    if not line.endswith(b'\n'):
        return False
    try:
        message: object = json.loads(line)
    except ValueError:
        return False
    if not isinstance(message, dict):
        return False
    fields = cast(dict[str, object], message)
    if fields.get('jsonrpc') != '2.0':
        return False
    if 'method' in fields:
        return isinstance(fields['method'], str)
    return 'id' in fields and (('result' in fields) != ('error' in fields))


async def test_an_untrusted_config_is_refused_at_session_new_with_the_trust_command(
    tmp_path: Path,
) -> None:
    async with _world(tmp_path, trusted=False) as world, _acp(world) as agent:
        editor = _Editor()
        async with _relayed(agent, editor) as (client, _wire):
            answered = await asyncio.wait_for(client.initialize(protocol_version=1), _TIMEOUT)
            assert answered.protocol_version == 1
            with pytest.raises(RequestError) as new:
                await asyncio.wait_for(client.new_session(cwd=str(world.workspace)), _TIMEOUT)
            with pytest.raises(RequestError) as load:
                await asyncio.wait_for(
                    client.load_session(cwd=str(world.workspace), session_id='a' * 32), _TIMEOUT
                )
        assert await asyncio.wait_for(agent.wait(), _TIMEOUT) == 0
    for refused in (new.value, load.value):
        data = cast(dict[str, str], refused.data)
        assert '没有被信任' in data['received']
        assert 'syndica config trust --sha256' in data['next_action']
    sessions = world.workspace / '.syndica' / 'sessions'
    assert not sessions.exists() or world.sessions() == []


async def test_every_stdout_line_of_syndica_acp_is_a_json_rpc_message(tmp_path: Path) -> None:
    async with _world(tmp_path) as world, _acp(world) as agent:
        editor = _Editor()
        async with _relayed(agent, editor) as (client, wire):
            with pytest.raises(RequestError):
                await asyncio.wait_for(client.initialize(protocol_version=0), _TIMEOUT)
            await asyncio.wait_for(client.initialize(protocol_version=1), _TIMEOUT)
            opened = await asyncio.wait_for(client.new_session(cwd=str(world.workspace)), _TIMEOUT)
            with pytest.raises(RequestError):
                await client.prompt(
                    session_id=opened.session_id,
                    prompt=[ImageContentBlock(type='image', data='aaaa', mime_type='image/png')],
                )
            with pytest.raises(RequestError):
                await client.load_session(cwd=str(world.workspace), session_id='../../escape')
            assert await _ask(client, opened.session_id, 'read notes') == 'end_turn'
        assert await asyncio.wait_for(agent.wait(), _TIMEOUT) == 0
        log = world.log()
    stdout = wire.from_agent()
    assert len(stdout) >= 8
    assert [line for line in stdout if not _is_json_rpc(line)] == []
    assert log.strip()
    assert _KEY not in log


async def test_closing_stdin_rejects_the_pending_confirmation_and_checkpoints(
    tmp_path: Path,
) -> None:
    outside = tmp_path / 'outside.txt'
    outside.write_text('keep me\n', encoding='utf-8')
    command = f'rm {outside}'
    async with _world(tmp_path) as world, _acp(world) as agent:
        assert agent.stdin is not None and agent.stdout is not None
        editor = _Editor(hold=True)
        client = ClientSideConnection(editor, agent.stdin, agent.stdout)  # pyright: ignore[reportArgumentType]
        await asyncio.wait_for(client.initialize(protocol_version=1), _TIMEOUT)
        opened = await asyncio.wait_for(client.new_session(cwd=str(world.workspace)), _TIMEOUT)
        await client.set_session_mode(session_id=opened.session_id, mode_id='Worker')
        prompt = asyncio.create_task(
            client.prompt(
                session_id=opened.session_id,
                prompt=[TextContentBlock(type='text', text=f'$ {command}')],
            )
        )
        await asyncio.wait_for(editor.asked.wait(), _TIMEOUT)
        agent.stdin.close()
        assert await asyncio.wait_for(agent.wait(), 20) == 0
        prompt.cancel()
        await asyncio.gather(prompt, return_exceptions=True)
        await client.close()
        log = world.log()
        [sid] = world.sessions()
        record = await FileSessionStore(world.workspace / '.syndica' / 'sessions').load(sid)
    assert outside.read_text(encoding='utf-8') == 'keep me\n'
    assert editor.tools == ['Bash']
    assert record is not None
    assert f'$ {command}' in [block.content for block in record.content if block.role == 'user']
    [refused] = [block for block in record.content if block.tool_call_id == 'call-1']
    assert '明确拒绝了这次调用' in str(refused.content)
    assert _KEY not in log


def _said(record_content: list[Any]) -> list[str]:
    return [
        str(block.content)
        for block in record_content
        if block.role == 'user' and kind_of(block) is None
    ]


async def test_a_session_loads_into_a_new_process_and_continues_there(tmp_path: Path) -> None:
    async with _world(tmp_path) as world:
        async with _acp(world) as first:
            assert first.stdin is not None and first.stdout is not None
            before = _Editor()
            client = ClientSideConnection(before, first.stdin, first.stdout)  # pyright: ignore[reportArgumentType]
            await asyncio.wait_for(client.initialize(protocol_version=1), _TIMEOUT)
            opened = await asyncio.wait_for(client.new_session(cwd=str(world.workspace)), _TIMEOUT)
            session_id = opened.session_id
            await client.set_session_mode(session_id=session_id, mode_id='Worker')
            assert await _ask(client, session_id, 'first-prompt') == 'end_turn'
            assert before.agent == ['echo: first-prompt']
            await client.close()
            first.stdin.close()
            assert await asyncio.wait_for(first.wait(), _TIMEOUT) == 0
        async with _acp(world) as second:
            assert second.stdin is not None and second.stdout is not None
            after = _Editor()
            client = ClientSideConnection(after, second.stdin, second.stdout)  # pyright: ignore[reportArgumentType]
            await asyncio.wait_for(client.initialize(protocol_version=1), _TIMEOUT)
            loaded = await asyncio.wait_for(
                client.load_session(cwd=str(world.workspace), session_id=session_id), _TIMEOUT
            )
            assert loaded.modes is not None
            assert loaded.modes.current_mode_id == 'Worker'
            await _until(lambda: len(after.agent) >= 1)
            assert (after.users, after.agent) == (['first-prompt'], ['echo: first-prompt'])
            assert await _ask(client, session_id, 'second-prompt') == 'end_turn'
            assert after.agent[-1] == 'echo: second-prompt'
            await client.close()
            second.stdin.close()
            assert await asyncio.wait_for(second.wait(), _TIMEOUT) == 0
        [sid] = world.sessions()
        record = await FileSessionStore(world.workspace / '.syndica' / 'sessions').load(sid)
        log = world.log()
    assert record is not None
    assert _said(record.content) == ['first-prompt', 'second-prompt']
    assert _KEY not in log
