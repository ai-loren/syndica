"""MCP SDK 客户端驱动真的 `syndica mcp` 子进程。缺省只读，读到的密钥被换掉。"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

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
