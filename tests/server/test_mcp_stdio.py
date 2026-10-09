"""MCP SDK 客户端驱动真的 `syndica mcp` 子进程。缺省只读，读到的密钥被换掉。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import cast

from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

_SECRET = 'sk-mcp-stdio-secret'
_MARKER = 'mcp-stdio-marker'


def _env(config_home: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(('SYNDICA_', 'OPENAI_', 'GEMINI_', 'OPENROUTER_', 'XDG_'))
    }
    env['XDG_CONFIG_HOME'] = str(config_home)
    env['MCP_PROBE_TOKEN'] = _SECRET
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


def _text(result: object) -> str:
    content = getattr(result, 'content', ())
    return ''.join(str(getattr(block, 'text', '')) for block in content)


async def test_the_sdk_client_drives_syndica_mcp(tmp_path: Path) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / '.syndica.yaml').write_text('version: 1\n', encoding='utf-8')
    (workspace / 'notes.md').write_text(f'{_MARKER}\n{_SECRET}\n', encoding='utf-8')
    env = _env(tmp_path / 'config')
    _trust(workspace, env)
    params = StdioServerParameters(
        command=sys.executable,
        args=['-m', 'syndica', 'mcp'],
        cwd=workspace,
        env=env,
    )
    async with Client(params, cache=None, read_timeout_seconds=20) as client:
        listed = await client.list_tools(cache_mode='bypass')
        names = [tool.name for tool in listed.tools]
        assert names == ['Read', 'Grep', 'Glob']
        assert all(tool.annotations and tool.annotations.read_only_hint for tool in listed.tools)
        read = await client.call_tool('Read', {'path': 'notes.md'})
        body = _text(read)
        assert _MARKER in body
        assert _SECRET not in body
        found = await client.call_tool('Grep', {'pattern': _MARKER, 'path': '.'})
        assert 'notes.md' in _text(found)
        assert _SECRET not in _text(found)
    logs = list((tmp_path / 'config').rglob('mcp.log'))
    assert logs
    assert all(workspace not in path.parents for path in logs)
    assert _SECRET not in logs[0].read_text(encoding='utf-8')


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


async def test_every_stdout_line_of_syndica_mcp_is_a_json_rpc_message(tmp_path: Path) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / '.syndica.yaml').write_text('version: 1\n', encoding='utf-8')
    (workspace / 'notes.md').write_text(f'{_MARKER}\n{_SECRET}\n', encoding='utf-8')
    env = _env(tmp_path / 'config')
    _trust(workspace, env)
    server = await asyncio.create_subprocess_exec(
        sys.executable,
        '-m',
        'syndica',
        'mcp',
        cwd=workspace,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert server.stdin is not None and server.stdout is not None and server.stderr is not None
    stdin, stdout = server.stdin, server.stdout
    stderr = asyncio.create_task(server.stderr.read())
    lines: list[bytes] = []

    async def call(message: dict[str, object]) -> None:
        stdin.write(json.dumps(message).encode() + b'\n')
        await stdin.drain()
        if 'id' not in message:
            return
        async with asyncio.timeout(20):
            while True:
                line = await stdout.readline()
                assert line, b''.join(lines)
                lines.append(line)
                if json.loads(line).get('id') == message['id']:
                    return

    try:
        await call(
            {
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'initialize',
                'params': {
                    'protocolVersion': '2025-11-25',
                    'capabilities': {},
                    'clientInfo': {'name': 'raw', 'version': '0'},
                },
            }
        )
        await call({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        await call({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        await call(
            {
                'jsonrpc': '2.0',
                'id': 3,
                'method': 'tools/call',
                'params': {'name': 'Read', 'arguments': {'path': 'notes.md'}},
            }
        )
        await call(
            {
                'jsonrpc': '2.0',
                'id': 4,
                'method': 'tools/call',
                'params': {'name': 'Write', 'arguments': {'path': 'x', 'content': 'y'}},
            }
        )
        await call({'jsonrpc': '2.0', 'id': 5, 'method': 'no/such/method'})
        stdin.close()
        async with asyncio.timeout(20):
            lines.extend([line async for line in stdout])
            assert await server.wait() == 0
    finally:
        if server.returncode is None:
            os.killpg(server.pid, signal.SIGKILL)
            await server.wait()
        await asyncio.wait_for(stderr, 10)
    assert len(lines) >= 5
    assert [line for line in lines if not _is_json_rpc(line)] == []
    assert _MARKER.encode() in b''.join(lines)
    assert _SECRET.encode() not in b''.join(lines)
