"""真 SDK 拉起的 stdio 服务：关闭、取消、崩溃之后，它的后代一个不留，含 setsid 离开了进程组的。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from syndica.backends.mcp import process as process_module
from syndica.backends.mcp.client import McpClient, connect_stdio
from syndica.backends.mcp.process import _TREE_POLL_S  # pyright: ignore[reportPrivateUsage]
from syndica.kernel import RuleRedactor, TrustLevel

_SERVER = """
import os, subprocess, sys
marker = sys.argv[1]
plain = subprocess.Popen(['/bin/sleep', '300'])
detached = subprocess.Popen(['/bin/sleep', '300'], start_new_session=True)
with open(marker + '.tmp', 'w') as handle:
    handle.write(f'{plain.pid} {detached.pid}')
os.replace(marker + '.tmp', marker)

import anyio
import mcp_types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server


async def on_list_tools(ctx, params):
    return types.ListToolsResult(
        tools=[
            types.Tool(name='crash', description='crash', input_schema={'type': 'object'}),
            types.Tool(name='spawn', description='spawn', input_schema={'type': 'object'}),
        ]
    )


async def on_call_tool(ctx, params):
    if params.name == 'crash':
        os._exit(3)
    child = subprocess.Popen(['/bin/sleep', '300'], start_new_session=True)
    return types.CallToolResult(content=[types.TextContent(type='text', text=str(child.pid))])


server = Server('leaky', on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


anyio.run(main)
"""


def _client(tmp_path: Path) -> tuple[McpClient, Path]:
    script = tmp_path / 'srv.py'
    script.write_text(_SERVER, encoding='utf-8')
    marker = tmp_path / 'pids'
    client = connect_stdio(
        'leaky',
        [sys.executable, str(script), str(marker)],
        env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(tmp_path)},
        cwd=str(tmp_path),
        redactor=RuleRedactor(),
        timeout=5,
    )
    return client, marker


def _pids_in(marker: Path) -> list[int]:
    if not marker.exists():
        return []
    return [int(part) for part in marker.read_text(encoding='utf-8').split()]


async def _spawned(marker: Path) -> list[int]:
    deadline = time.monotonic() + 10
    while not _pids_in(marker) and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.02)
    pids = _pids_in(marker)
    assert len(pids) == 2
    assert all(_running(pid) for pid in pids)
    return pids


def _running(pid: int) -> bool:
    state = subprocess.run(
        ['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(state) and not state.startswith('Z')


def _survivors(pids: list[int]) -> list[int]:
    return [pid for pid in pids if _running(pid)]


def _kill_all(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            continue


async def test_closing_leaves_neither_a_group_member_nor_a_setsid_grandchild(
    tmp_path: Path,
) -> None:
    client, marker = _client(tmp_path)
    pids: list[int] = []
    try:
        async with client:
            pids = await _spawned(marker)
        assert _survivors(pids) == []
    finally:
        _kill_all(pids)


async def test_a_cancelled_session_leaves_no_descendant(tmp_path: Path) -> None:
    client, marker = _client(tmp_path)
    entered = asyncio.Event()

    async def own() -> None:
        async with client:
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(own())
    pids: list[int] = []
    try:
        await asyncio.wait_for(entered.wait(), 10)
        pids = await _spawned(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _survivors(pids) == []
    finally:
        task.cancel()
        _kill_all(pids)


async def test_a_crashed_servers_descendants_are_gone_before_and_after_close(
    tmp_path: Path,
) -> None:
    client, marker = _client(tmp_path)
    pids: list[int] = []
    try:
        async with client:
            pids = await _spawned(marker)
            await asyncio.sleep(2 * _TREE_POLL_S)
            result = await client.tools['leaky__crash'].execute()
            assert result.trust is TrustLevel.UNTRUSTED
            assert '不会自动重连' in result.output
            deadline = time.monotonic() + 2 * _TREE_POLL_S + 2
            while _survivors(pids) and time.monotonic() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(0.02)
            assert _survivors(pids) == []
        assert _survivors(pids) == []
    finally:
        _kill_all(pids)


async def test_a_setsid_child_spawned_after_the_last_poll_is_reaped_on_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process_module, '_TREE_POLL_S', 60.0)
    client, marker = _client(tmp_path)
    pids: list[int] = []
    try:
        async with client:
            pids = await _spawned(marker)
            result = await client.tools['leaky__spawn'].execute()
            assert isinstance(result.output, str)
            pids.append(int(result.output.strip()))
            assert _running(pids[-1])
        assert _survivors(pids) == []
    finally:
        _kill_all(pids)
