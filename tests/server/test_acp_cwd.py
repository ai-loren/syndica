"""真的 `syndica acp` 子进程只在启动它的那个工作区里开会话。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path

import pytest
from acp.client.connection import ClientSideConnection
from acp.exceptions import RequestError
from acp.schema import RequestPermissionResponse


class _Silent:
    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        del session_id, update, kwargs

    async def request_permission(
        self, *args: object, **kwargs: object
    ) -> RequestPermissionResponse:
        raise AssertionError(f'no permission request expected: {args!r} {kwargs!r}')


def _env(config_home: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(('SYNDICA_', 'OPENAI_', 'GEMINI_', 'OPENROUTER_', 'XDG_'))
    }
    env['XDG_CONFIG_HOME'] = str(config_home)
    return env


async def test_syndica_acp_opens_sessions_only_in_its_own_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / 'ws'
    (workspace / 'sub').mkdir(parents=True)
    other = tmp_path / 'other'
    other.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(workspace, target_is_directory=True)
    agent = await asyncio.create_subprocess_exec(
        sys.executable,
        '-m',
        'syndica',
        'acp',
        cwd=workspace,
        env=_env(tmp_path / 'config'),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert agent.stdin is not None and agent.stdout is not None and agent.stderr is not None
        stderr = asyncio.create_task(agent.stderr.read())
        client = ClientSideConnection(_Silent(), agent.stdin, agent.stdout)  # pyright: ignore[reportArgumentType]
        try:
            await asyncio.wait_for(client.initialize(protocol_version=1), 20)
            for refused in (other, workspace / 'sub'):
                with pytest.raises(RequestError) as caught:
                    await asyncio.wait_for(client.new_session(cwd=str(refused)), 20)
                assert caught.value.data == {
                    'expected': f'cwd 是启动 syndica acp 的工作区 {workspace.resolve()}',
                    'received': str(refused.resolve()),
                    'next_action': '在那个目录里运行 syndica acp',
                }
            for accepted in (workspace, alias):
                opened = await asyncio.wait_for(client.new_session(cwd=str(accepted)), 20)
                assert opened.session_id
        finally:
            closer = getattr(client, 'close', None)
            if closer is not None:
                await closer()
            agent.stdin.close()
        await asyncio.wait_for(stderr, 10)
    finally:
        if agent.returncode is None and agent.pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(agent.pid, signal.SIGTERM)
            await agent.wait()
