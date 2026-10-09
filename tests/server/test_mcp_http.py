"""`/mcp`：Bearer、Host、未知协议版本、两个时代都能列出工具。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from server_fakes import FakeAssembly, running
from test_mcp_serve import _portal, _server

from syndica.kernel import InMemoryTraceStore, RuleRedactor
from syndica.server import ProtocolMount, mcp_serve
from syndica.server.mcp_serve import ACCEPTED_PROTOCOL_VERSIONS, McpTraces, streamable_http


def _mount(
    tmp_path: Path,
    port: int,
    store: InMemoryTraceStore | None = None,
    redactor: RuleRedactor | None = None,
) -> ProtocolMount:
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
            traces = (
                None
                if store is None
                else McpTraces(store, redactor if redactor is not None else RuleRedactor(), router)
            )
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


async def _initialize(client: httpx.AsyncClient, token: str, version: str) -> httpx.Response:
    return await client.post(
        '/mcp',
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/json, text/event-stream',
            'Content-Type': 'application/json',
        },
        json={
            'jsonrpc': '2.0',
            'id': 7,
            'method': 'initialize',
            'params': {
                'protocolVersion': version,
                'capabilities': {},
                'clientInfo': {'name': 'probe', 'version': '0'},
            },
        },
    )


_NEAR_MISSES = ('2099-01-01', '2025-13-40', '2025-06-18 ', '2025-06-180', '2025-06')


async def test_only_the_exact_accepted_versions_get_past_the_gate_before_the_sdk(
    tmp_path: Path,
) -> None:
    def mounts(port: int) -> tuple[ProtocolMount, ...]:
        return (_mount(tmp_path, port),)

    rejected: dict[str, Any] = {}
    accepted: dict[str, httpx.Response] = {}
    async with running(FakeAssembly(), mount_factory=mounts) as server:
        async with httpx.AsyncClient(base_url=server.base, timeout=10) as client:
            for version in _NEAR_MISSES:
                rejected[version] = (await _initialize(client, server.token, version)).json()
            for version in ACCEPTED_PROTOCOL_VERSIONS:
                accepted[version] = await _initialize(client, server.token, version)

    for version, body in rejected.items():
        assert body['id'] == 7
        assert body['error']['code'] == -32022
        data = body['error']['data']
        assert data['requested'] == data['received'] == version
        assert data['supported'] == list(ACCEPTED_PROTOCOL_VERSIONS)
        assert 'supported' in data['next_action']
    for response in accepted.values():
        assert response.status_code == 200
        assert '-32022' not in response.text
        assert 'protocolVersion' in response.text


async def _post(
    client: httpx.AsyncClient,
    token: str,
    payload: dict[str, Any],
    *,
    version: str | None = None,
    session: str | None = None,
) -> httpx.Response:
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json, text/event-stream',
        'Content-Type': 'application/json',
    }
    if version is not None:
        headers['MCP-Protocol-Version'] = version
    if session is not None:
        headers['Mcp-Session-Id'] = session
    return await client.post('/mcp', headers=headers, json=payload)


_LIST = {'jsonrpc': '2.0', 'id': 11, 'method': 'tools/list', 'params': {}}


@pytest.mark.parametrize('version', ['2099-01-01', '2025-13-40', '2025-06-180', '2025-06'])
async def test_tools_list_with_an_unknown_version_header_is_refused_before_the_sdk(
    tmp_path: Path, version: str
) -> None:
    store = InMemoryTraceStore()

    def mounts(port: int) -> tuple[ProtocolMount, ...]:
        return (_mount(tmp_path, port, store),)

    async with running(FakeAssembly(), mount_factory=mounts) as server:
        async with httpx.AsyncClient(base_url=server.base, timeout=10) as client:
            response = await _post(client, server.token, _LIST, version=version)

    assert response.status_code == 400
    body = response.json()
    assert body['id'] == 11
    assert body['error']['code'] == -32022
    data = body['error']['data']
    assert data['supported'] == list(ACCEPTED_PROTOCOL_VERSIONS)
    assert data['requested'] == data['received'] == version
    assert data['expected'].startswith('MCP-Protocol-Version 是 ')
    assert 'MCP-Protocol-Version' in data['next_action']
    assert await store.traces() == []


async def test_a_header_version_the_sdk_speaks_but_the_table_drops_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrowed = tuple(item for item in ACCEPTED_PROTOCOL_VERSIONS if item != '2025-06-18')
    monkeypatch.setattr(mcp_serve, 'ACCEPTED_PROTOCOL_VERSIONS', narrowed)

    def mounts(port: int) -> tuple[ProtocolMount, ...]:
        return (_mount(tmp_path, port),)

    async with running(FakeAssembly(), mount_factory=mounts) as server:
        async with httpx.AsyncClient(base_url=server.base, timeout=10) as client:
            response = await _post(client, server.token, _LIST, version='2025-06-18')

    body = response.json()
    assert body['error']['code'] == -32022
    assert body['error']['data']['supported'] == list(narrowed)
    assert body['error']['data']['requested'] == '2025-06-18'


async def _handshake_without_header(client: httpx.AsyncClient, token: str) -> str:
    opened = await _initialize(client, token, '2025-06-18')
    assert opened.status_code == 200, opened.text
    session = opened.headers['mcp-session-id']
    initialized = await _post(
        client,
        token,
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        session=session,
    )
    assert initialized.status_code == 202, initialized.text
    return session


async def test_an_absent_version_header_is_taken_as_the_spec_default_and_passes(
    tmp_path: Path,
) -> None:
    def mounts(port: int) -> tuple[ProtocolMount, ...]:
        return (_mount(tmp_path, port),)

    async with running(FakeAssembly(), mount_factory=mounts) as server:
        async with httpx.AsyncClient(base_url=server.base, timeout=10) as client:
            session = await _handshake_without_header(client, server.token)
            listed = await _post(client, server.token, _LIST, session=session)

    assert listed.status_code == 200, listed.text
    assert '-32022' not in listed.text
    assert '"Read"' in listed.text


async def test_an_absent_header_is_refused_once_the_spec_default_leaves_the_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrowed = tuple(item for item in ACCEPTED_PROTOCOL_VERSIONS if item != '2025-03-26')

    def mounts(port: int) -> tuple[ProtocolMount, ...]:
        return (_mount(tmp_path, port),)

    async with running(FakeAssembly(), mount_factory=mounts) as server:
        async with httpx.AsyncClient(base_url=server.base, timeout=10) as client:
            session = await _handshake_without_header(client, server.token)
            monkeypatch.setattr(mcp_serve, 'ACCEPTED_PROTOCOL_VERSIONS', narrowed)
            response = await _post(client, server.token, _LIST, session=session)

    assert response.status_code == 400
    data = response.json()['error']['data']
    assert data['requested'] == '2025-03-26'
    assert data['supported'] == list(narrowed)


async def test_a_rejected_version_is_redacted_with_the_connections_redactor(
    tmp_path: Path,
) -> None:
    configured = 'quiz-configured-secret-4471'
    token = 'ghp_' + 'Quiz' * 9

    for name in ('traced', 'bare'):
        (tmp_path / name).mkdir()

    def traced(port: int) -> tuple[ProtocolMount, ...]:
        return (
            _mount(
                tmp_path / 'traced',
                port,
                InMemoryTraceStore(),
                RuleRedactor(secrets=(configured,)),
            ),
        )

    def bare(port: int) -> tuple[ProtocolMount, ...]:
        return (_mount(tmp_path / 'bare', port),)

    for factory, secret in ((traced, configured), (bare, token)):
        async with running(FakeAssembly(), mount_factory=factory) as server:
            async with httpx.AsyncClient(base_url=server.base, timeout=10) as client:
                response = await _initialize(client, server.token, secret)
        body = response.json()
        assert body['error']['code'] == -32022
        assert secret not in response.text
        assert body['error']['data']['requested'].startswith('[REDACTED')


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
