"""SDK 客户端驱动真的 `syndica acp` 子进程。模型是本地假端点，不花钱。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from acp.client.connection import ClientSideConnection
from acp.exceptions import RequestError
from acp.schema import (
    AllowedOutcome,
    DeniedOutcome,
    ImageContentBlock,
    RequestPermissionResponse,
    TextContentBlock,
)

_FAKE = Path(__file__).resolve().parents[1] / 'e2e' / 'fake_openai.py'
_KEY = 'e2e-dummy-key-0123456789'


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


async def _port(fake: asyncio.subprocess.Process) -> int:
    assert fake.stdout is not None
    line = await asyncio.wait_for(fake.stdout.readline(), 10)
    return int(line.strip())


class _Updates:
    def __init__(self, decisions: list[str]) -> None:
        self.chunks: list[str] = []
        self.tools: list[str] = []
        self._decisions = list(decisions)

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        del session_id, kwargs
        title = getattr(update, 'title', None)
        if isinstance(title, str) and title:
            self.tools.append(title)
        content = getattr(update, 'content', None)
        text = getattr(content, 'text', None)
        if isinstance(text, str) and text:
            self.chunks.append(text)

    async def request_permission(
        self, *args: object, **kwargs: object
    ) -> RequestPermissionResponse:
        del args, kwargs
        decision = self._decisions.pop(0)
        if decision == 'allow_once':
            return RequestPermissionResponse(
                outcome=AllowedOutcome(outcome='selected', option_id='allow_once')
            )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome='cancelled'))


async def test_the_sdk_client_drives_syndica_acp(tmp_path: Path) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / '.syndica.yaml').write_text('version: 1\n', encoding='utf-8')
    fake = await asyncio.create_subprocess_exec(
        sys.executable,
        str(_FAKE),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    agent: asyncio.subprocess.Process | None = None
    try:
        port = await _port(fake)
        env = _env(tmp_path / 'config', port)
        _trust(workspace, env)
        agent = await asyncio.create_subprocess_exec(
            sys.executable,
            '-m',
            'syndica',
            'acp',
            cwd=workspace,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert agent.stdin is not None and agent.stdout is not None and agent.stderr is not None
        stderr = asyncio.create_task(agent.stderr.read())
        updates = _Updates(['reject_once', 'allow_once'])
        client = ClientSideConnection(updates, agent.stdin, agent.stdout)  # pyright: ignore[reportArgumentType]

        async def ask(session_id: str, text: str) -> str:
            before = len(updates.chunks)
            result = await asyncio.wait_for(
                client.prompt(
                    session_id=session_id,
                    prompt=[TextContentBlock(type='text', text=text)],
                ),
                40,
            )
            assert result.stop_reason == 'end_turn'
            return ''.join(updates.chunks[before:])

        try:
            with pytest.raises(RequestError, match='protocolVersion'):
                await asyncio.wait_for(client.initialize(protocol_version=0), 20)
            initialized = await asyncio.wait_for(client.initialize(protocol_version=1), 20)
            assert initialized.protocol_version == 1
            opened = await asyncio.wait_for(client.new_session(cwd=str(workspace)), 20)
            await client.set_session_mode(session_id=opened.session_id, mode_id='Worker')
            with pytest.raises(RequestError, match='text'):
                await client.prompt(
                    session_id=opened.session_id,
                    prompt=[ImageContentBlock(type='image', data='aaaa', mime_type='image/png')],
                )
            hello = await ask(opened.session_id, 'subprocess-hello')
            assert '收到：' in hello
            question = await ask(opened.session_id, '问我')
            assert '红' in question and '蓝' in question
            with pytest.raises(RequestError, match='选项'):
                await client.prompt(
                    session_id=opened.session_id,
                    prompt=[TextContentBlock(type='text', text='绿')],
                )
            picked = await ask(opened.session_id, '红')
            assert '收到：' in picked or '红' in picked
            refused = await ask(opened.session_id, '确认一下')
            assert '没有执行' in refused or '拒绝' in refused
            assert 'Bash' in updates.tools
            allowed = await ask(opened.session_id, '再确认一次')
            assert 'exit_code: 0' in allowed
            assert _KEY not in ''.join(updates.chunks)
        finally:
            closer = getattr(client, 'close', None)
            if closer is not None:
                await closer()
            agent.stdin.close()
        err = (await asyncio.wait_for(stderr, 5)).decode()
        assert 'session/prompt' not in err
        assert _KEY not in err
        logs = list((tmp_path / 'config').rglob('acp.log'))
        assert logs
        assert all(workspace not in path.parents for path in logs)
        assert _KEY not in logs[0].read_text(encoding='utf-8')
    finally:
        if agent is not None and agent.returncode is None and agent.pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(agent.pid, signal.SIGTERM)
            await agent.wait()
        if fake.returncode is None and fake.pid is not None:
            fake.terminate()
            await fake.wait()
