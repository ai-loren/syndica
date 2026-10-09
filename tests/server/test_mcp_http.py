"""`/mcp`：Bearer、Host、未知协议版本、两个时代都能列出工具。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import httpx2
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from server_fakes import FakeAssembly, running
from test_mcp_serve import _portal, _server

from syndica.kernel import InMemoryTraceStore, RuleRedactor
from syndica.server import ProtocolMount
from syndica.server.mcp_serve import McpTraces, streamable_http


def _mount(tmp_path: Path, port: int, store: InMemoryTraceStore | None = None) -> ProtocolMount:
    state: dict[str, Any] = {}

    class _App:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            app = state.get('app')
            if app is None:
                from starlette.responses import JSONResponse

                response = JSONResponse(
                    {
                        'expected': '/mcp 的会话管理器已经进入 lifespan',
                        'received': '请求到了，管理器还没准备好',
                        'next_action': '等 server 启动完成再连',
                    },
                    status_code=503,
                )
                await response(scope, receive, send)
                return
            await app(scope, receive, send)

    @asynccontextmanager
    async def lifespan() -> AsyncIterator[None]:
        router, root = await _portal(tmp_path)
        try:
            traces = None if store is None else McpTraces(store, RuleRedactor(), router)
            app, life = streamable_http(
                _server(router, root, traces=traces), port=port, traces=traces
            )
            state['app'] = app
            async with life():
                yield
        finally:
            state.pop('app', None)
            await router.close()

    return ProtocolMount('/mcp', _App(), lifespan=lifespan)


async def test_mcp_http_requires_a_token_and_a_loopback_host_and_a_known_version(
    tmp_path: Path,
) -> None:
    def mounts(port: int) -> tuple[ProtocolMount, ...]:
        return (_mount(tmp_path, port),)

    async with running(FakeAssembly(), mount_factory=mounts) as server:
        async with httpx.AsyncClient(base_url=server.base, timeout=10) as client:
            missing = await client.get('/mcp')
            assert missing.status_code == 401
            assert 'Bearer' in missing.text
            foreign = await client.get(
                '/mcp',
                headers={'Host': 'evil.example', 'Authorization': f'Bearer {server.token}'},
            )
            assert foreign.status_code == 421
            assert 'evil.example' in foreign.text
            rejected = await client.post(
                '/mcp',
                headers={
                    'Authorization': f'Bearer {server.token}',
                    'Accept': 'application/json, text/event-stream',
                    'Content-Type': 'application/json',
                },
                json={
                    'jsonrpc': '2.0',
                    'id': 1,
                    'method': 'initialize',
                    'params': {
                        'protocolVersion': '1999-01-01',
                        'capabilities': {},
                        'clientInfo': {'name': 'probe', 'version': '0'},
                    },
                },
            )
    assert '-32022' in rejected.text
    assert 'supported' in rejected.text
    assert '1999-01-01' in rejected.text


async def test_both_http_eras_list_the_read_only_tools(tmp_path: Path) -> None:
    store = InMemoryTraceStore()

    def mounts(port: int) -> tuple[ProtocolMount, ...]:
        return (_mount(tmp_path, port, store),)

    async with running(FakeAssembly(), mount_factory=mounts) as server:
        for mode in ('legacy', '2026-07-28'):
            http = httpx2.AsyncClient(
                headers={'Authorization': f'Bearer {server.token}'},
                timeout=httpx2.Timeout(10),
            )
            try:
                async with Client(
                    streamable_http_client(f'{server.base}/mcp', http_client=http),
                    mode=mode,  # pyright: ignore[reportArgumentType]
                    cache=None,
                    read_timeout_seconds=10,
                ) as client:
                    listed = {tool.name for tool in (await client.list_tools()).tools}
            finally:
                await http.aclose()
            assert {'Read', 'Grep', 'Glob'} <= listed
            assert 'Write' not in listed and 'Bash' not in listed
    headers = await store.traces()
    assert len(headers) == 2
