"""ACP agent：版本只答 1，工作区要已信任，提示只收文本。"""

from __future__ import annotations

import asyncio
import socket

import pytest
from acp.exceptions import RequestError
from acp.schema import HttpMcpServer, ImageContentBlock, McpServerStdio, TextContentBlock

from syndica.kernel import ConfigError, RuleRedactor
from syndica.server.acp_agent import SyndicaAcp

_SECRET = 'sekret-VALUE-918273'


def _agent(
    trust: object | None = None,
    runner: object | None = None,
) -> SyndicaAcp:
    def allow(cwd: str) -> None:
        if cwd != '/work':
            raise ConfigError(
                expected='cwd 是已经信任的工作区',
                received=f'{cwd} 的配置没有被信任',
                next_action='在那个目录运行 syndica config trust，再 new_session',
            )

    async def echo(session_id: str, mode: str, text: str) -> str:
        del session_id
        return f'{mode}:{text}:{_SECRET}'

    return SyndicaAcp(
        (('Worker', '干活'), ('Reviewer', '看')),
        allow if trust is None else trust,  # pyright: ignore[reportArgumentType]
        runner=echo if runner is None else runner,  # pyright: ignore[reportArgumentType]
        redactor=RuleRedactor(secrets=(_SECRET,)),
        version='test',
    )


def test_an_empty_role_list_is_refused() -> None:
    with pytest.raises(ConfigError, match='角色名单'):
        SyndicaAcp((), lambda _cwd: None)


async def test_initialize_answers_1_and_rejects_an_older_version() -> None:
    agent = _agent()
    newer = await agent.initialize(protocol_version=2)
    assert newer.protocol_version == 1
    assert newer.agent_capabilities is not None
    assert newer.agent_capabilities.load_session is True
    caps = newer.agent_capabilities
    assert caps.prompt_capabilities is not None and caps.prompt_capabilities.image is False
    assert caps.mcp_capabilities is not None and caps.mcp_capabilities.http is False
    assert caps.mcp_capabilities.sse is False
    with pytest.raises(RequestError) as caught:
        await agent.initialize(protocol_version=0)
    data = caught.value.data
    assert isinstance(data, dict)
    assert data['next_action']
    assert 'protocolVersion' in data['expected']


async def test_new_session_requires_trust_and_stdio_mcp_only() -> None:
    agent = _agent()
    with pytest.raises(RequestError) as caught:
        await agent.new_session('/other')
    data = caught.value.data
    assert isinstance(data, dict)
    assert 'config trust' in data['next_action']
    opened = await agent.new_session(
        '/work',
        mcp_servers=[McpServerStdio(name='files', command='srv', args=[], env=[])],
    )
    assert opened.modes is not None
    assert opened.modes.current_mode_id == 'Worker'
    assert [mode.id for mode in opened.modes.available_modes] == ['Worker', 'Reviewer']
    with pytest.raises(RequestError) as http:
        await agent.new_session(
            '/work',
            mcp_servers=[
                HttpMcpServer(name='remote', url='https://example.com', headers=[], type='http')
            ],
        )
    http_data = http.value.data
    assert isinstance(http_data, dict)
    assert 'stdio' in http_data['expected']


async def test_prompt_is_text_only_redacted_and_mode_locks() -> None:
    seen: list[tuple[str, str, str]] = []

    async def runner(session_id: str, mode: str, text: str) -> str:
        seen.append((session_id, mode, text))
        return f'答 {_SECRET}'

    agent = _agent(runner=runner)
    opened = await agent.new_session('/work')
    session_id = opened.session_id
    await agent.set_session_mode(session_id, 'Reviewer')
    with pytest.raises(RequestError) as image:
        await agent.prompt(
            session_id,
            [ImageContentBlock(type='image', data='aaaa', mime_type='image/png')],
        )
    image_data = image.value.data
    assert isinstance(image_data, dict)
    assert 'text' in image_data['expected']
    assert agent._sessions[session_id].prompted is False  # pyright: ignore[reportPrivateUsage]
    result = await agent.prompt(session_id, [TextContentBlock(type='text', text='你好')])
    assert result.stop_reason == 'end_turn'
    assert seen == [(session_id, 'Reviewer', '你好')]
    with pytest.raises(RequestError, match='第一条提示'):
        await agent.set_session_mode(session_id, 'Worker')


async def test_a_runner_exception_does_not_echo_its_text() -> None:
    async def boom(session_id: str, mode: str, text: str) -> str:
        del session_id, mode, text
        raise RuntimeError(f'blew {_SECRET}')

    agent = _agent(runner=boom)
    opened = await agent.new_session('/work')
    with pytest.raises(RequestError) as caught:
        await agent.prompt(opened.session_id, [TextContentBlock(type='text', text='go')])
    assert _SECRET not in str(caught.value)
    assert _SECRET not in str(caught.value.data)
    assert 'RuntimeError' in str(caught.value.data)


async def test_the_sdk_client_negotiates_version_1() -> None:
    from acp.agent.connection import AgentSideConnection
    from acp.client.connection import ClientSideConnection

    class _Updates:
        def __init__(self) -> None:
            self.chunks: list[str] = []

        async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
            del session_id, kwargs
            content = getattr(update, 'content', None)
            self.chunks.append(str(getattr(content, 'text', '')))

        async def request_permission(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError('permission')

    agent = _agent()
    left, right = socket.socketpair()
    left.setblocking(False)
    right.setblocking(False)
    client_reader, client_writer = await asyncio.open_connection(sock=left)
    agent_reader, agent_writer = await asyncio.open_connection(sock=right)
    updates = _Updates()
    client = ClientSideConnection(
        updates,  # pyright: ignore[reportArgumentType]
        client_writer,
        client_reader,
    )
    server = AgentSideConnection(
        agent,  # pyright: ignore[reportArgumentType]
        agent_writer,
        agent_reader,
        listening=False,
    )
    task = asyncio.create_task(server.listen())
    try:
        initialized = await client.initialize(protocol_version=2)
        assert initialized.protocol_version == 1
        opened = await client.new_session(cwd='/work')
        result = await client.prompt(
            session_id=opened.session_id,
            prompt=[TextContentBlock(type='text', text='ping')],
        )
        assert result.stop_reason == 'end_turn'
        assert updates.chunks and updates.chunks[0].startswith('Worker:ping:')
        assert _SECRET not in ''.join(updates.chunks)
    finally:
        task.cancel()
        await server.close()
        client_writer.close()
        agent_writer.close()
