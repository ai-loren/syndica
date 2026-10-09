"""协议路由始终在；缺 extra 是 501，机器令牌与浏览器令牌同一套校验且不落盘。"""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from server_fakes import FakeAssembly, running
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from syndica.server.app import AppAdapter
from syndica.server.auth import Credentials


def test_an_api_token_authorizes_like_a_browser_token_and_is_not_a_login_code() -> None:
    credentials = Credentials()
    token = credentials.issue_api_token()
    credentials.require(f'Bearer {token}')
    second = credentials.issue_api_token()
    assert second != token
    credentials.require(f'Bearer {second}')
    credentials.require(f'Bearer {token}')
    with pytest.raises(Exception, match='登录码'):
        credentials.exchange(token)


@pytest.mark.parametrize(
    ('path', 'module', 'extra'),
    [
        ('/.well-known/agent-card.json', 'a2a', 'a2a'),
        ('/mcp', 'mcp', 'mcp'),
    ],
)
async def test_a_missing_protocol_extra_answers_501_with_the_install_line(
    monkeypatch: pytest.MonkeyPatch, path: str, module: str, extra: str
) -> None:
    real = importlib.util.find_spec

    def find_spec(name: str, package: str | None = None) -> object:
        if name == module:
            return None
        return real(name, package)

    monkeypatch.setattr(importlib.util, 'find_spec', find_spec)
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        headers = {} if path.endswith('.json') else {'Authorization': f'Bearer {server.token}'}
        response = await client.get(path, headers=headers)
    assert response.status_code == 501
    assert f'syndica[{extra}]' in response.text
    assert 'pip install' in response.text


async def test_a_protocol_route_without_a_token_is_401_before_the_extra_check() -> None:
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        response = await client.get('/v1/models')
    assert response.status_code == 401
    assert 'Bearer' in response.text


async def test_openai_routes_do_not_ask_for_an_extra() -> None:
    async with running(FakeAssembly()) as server, httpx.AsyncClient(base_url=server.base) as client:
        response = await client.get(
            '/v1/models', headers={'Authorization': f'Bearer {server.token}'}
        )
    assert response.status_code == 501
    assert '不需要额外安装' in response.text
    assert 'syndica[openai]' not in response.text


async def test_an_adapter_route_is_served_ahead_of_the_static_fallback() -> None:
    seen: list[str] = []

    @asynccontextmanager
    async def lifespan() -> AsyncIterator[None]:
        seen.append('enter')
        try:
            yield
        finally:
            seen.append('exit')

    async def probe(_request: Request) -> Response:
        return Response(status_code=204)

    adapter = AppAdapter(
        (Route('/probe', probe, methods=['GET']),),
        lifespan=lifespan,
    )
    async with (
        running(FakeAssembly(), adapters=(adapter,)) as server,
        httpx.AsyncClient(base_url=server.base) as client,
    ):
        response = await client.get('/probe')
        assert response.status_code == 204
        assert seen == ['enter']
    assert seen == ['enter', 'exit']
