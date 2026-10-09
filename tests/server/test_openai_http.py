"""OpenAI SDK 对兼容 HTTP：模型列表、一轮回答、续接、流、拒绝 n>1、客户端工具。"""

from __future__ import annotations

import asyncio
import json
import re
import socket
from io import StringIO
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI, AuthenticationError, BadRequestError, NotFoundError

from syndica.cli.assembly import ServerAssembly
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import ScriptedLLM, ScriptedTurn, ToolCall, TrustLevel
from syndica.kernel.context import ContextBlock
from syndica.server import Credentials, Live, ProtocolMount, ServerSettings, create_app
from syndica.server.openai_http import OpenAIHttp, chat_app, models_app, responses_app


def _port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _app(tmp_path: Path, llm: ScriptedLLM) -> tuple[RuntimeCliHost, Live, object, int, Credentials]:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    config_path = workspace / '.syndica.yaml'
    config_path.write_text('version: 1\n', encoding='utf-8')
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=workspace,
    )
    loaded = host.trusted_config(str(config_path))
    live = Live(ServerAssembly(host, path=str(config_path), config=loaded))
    api = OpenAIHttp(live, tuple(loaded.catalog.names()), live.assembly.redactor())
    port = _port()
    credentials = Credentials()
    app = create_app(
        live,
        credentials,
        ServerSettings(port=port, nonce='nonce', version='test'),
        protocol_mounts=(
            ProtocolMount('/v1/models', models_app(api)),
            ProtocolMount('/v1/chat/completions', chat_app(api)),
            ProtocolMount('/v1/responses', responses_app(api)),
        ),
    )
    return host, live, app, port, credentials


async def _client(app: object, port: int, credentials: Credentials) -> AsyncOpenAI:
    base = f'http://127.0.0.1:{port}'
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base)  # pyright: ignore[reportArgumentType]
    login = await http.post('/api/v1/login', json={'code': credentials.issue_code()})
    token = login.json()['token']
    return AsyncOpenAI(api_key=token, base_url=f'{base}/v1', http_client=http, max_retries=0)  # pyright: ignore[reportArgumentType]


def test_models_and_one_completion_and_a_rejected_n(tmp_path: Path) -> None:
    host, _live, app, port, credentials = _app(
        tmp_path, ScriptedLLM([ScriptedTurn(content='from the worker')])
    )

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            models = await client.models.list()
            assert 'Worker' in {item.id for item in models.data}
            completion = await client.chat.completions.create(
                model='Worker', messages=[{'role': 'user', 'content': 'hello'}]
            )
            assert completion.choices[0].message.content == 'from the worker'
            assert completion.choices[0].finish_reason == 'stop'
            with pytest.raises(BadRequestError, match='n 是 1'):
                await client.chat.completions.create(
                    model='Worker',
                    messages=[{'role': 'user', 'content': 'again'}],
                    n=2,
                )
            with pytest.raises(BadRequestError, match='logprobs'):
                await client.chat.completions.create(
                    model='Worker',
                    messages=[{'role': 'user', 'content': 'again'}],
                    logprobs=True,
                )
            with pytest.raises(BadRequestError, match='json_schema'):
                await client.chat.completions.create(
                    model='Worker',
                    messages=[{'role': 'user', 'content': 'again'}],
                    response_format={'type': 'json_schema', 'json_schema': {'name': 'x'}},  # pyright: ignore[reportArgumentType]
                )
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_second_request_continues_the_session(tmp_path: Path) -> None:
    host, _live, app, port, credentials = _app(
        tmp_path,
        ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')]),
    )

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            first = await client.with_raw_response.chat.completions.create(
                model='Worker', messages=[{'role': 'user', 'content': 'one'}]
            )
            parsed = first.parse()
            sid = first.headers['x-syndica-session']
            assert parsed.choices[0].message.content == 'first'
            second = await client.chat.completions.create(
                model='Worker',
                messages=[
                    {'role': 'user', 'content': 'one'},
                    {'role': 'assistant', 'content': 'first'},
                    {'role': 'user', 'content': 'two'},
                ],
                extra_headers={'X-Syndica-Session': sid},
            )
            assert second.choices[0].message.content == 'second'
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_client_tool_round_trip(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='call_1', name='lookup', arguments='{"q": "x"}')]),
            ScriptedTurn(content='used lookup'),
        ]
    )
    host, _live, app, port, credentials = _app(tmp_path, llm)
    tools = [
        {
            'type': 'function',
            'function': {
                'name': 'lookup',
                'description': 'look up',
                'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}},
            },
        }
    ]

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            first = await client.with_raw_response.chat.completions.create(
                model='Worker',
                messages=[{'role': 'user', 'content': 'find x'}],
                tools=tools,  # pyright: ignore[reportArgumentType]
            )
            sid = first.headers['x-syndica-session']
            asked = first.parse()
            assert asked.choices[0].finish_reason == 'tool_calls'
            calls = asked.choices[0].message.tool_calls
            assert calls is not None
            call = calls[0]
            assert call.type == 'function'
            assert call.function.name == 'lookup'
            second = await client.chat.completions.create(  # pyright: ignore[reportArgumentType]
                model='Worker',
                messages=[
                    {'role': 'user', 'content': 'find x'},
                    asked.choices[0].message,  # pyright: ignore[reportArgumentType]
                    {'role': 'tool', 'tool_call_id': call.id, 'content': 'found'},
                ],
                tools=tools,  # pyright: ignore[reportArgumentType]
                extra_headers={'X-Syndica-Session': sid},
            )
            assert second.choices[0].message.content == 'used lookup'
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()
    [result] = [block for block in llm.calls[1] if block.tool_call_id == 'call_1']
    assert result.content == 'found'
    assert result.trust is TrustLevel.UNTRUSTED


def test_a_client_tool_named_like_a_syndica_tool_is_a_bad_request(tmp_path: Path) -> None:
    host, _live, app, port, credentials = _app(
        tmp_path, ScriptedLLM([ScriptedTurn(content='never')])
    )
    tools = [
        {
            'type': 'function',
            'function': {'name': 'Read', 'parameters': {'type': 'object', 'properties': {}}},
        }
    ]

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            with pytest.raises(BadRequestError, match='client_lookup'):
                await client.chat.completions.create(
                    model='Worker',
                    messages=[{'role': 'user', 'content': 'read'}],
                    tools=tools,  # pyright: ignore[reportArgumentType]
                )
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_missing_token_is_an_authentication_error(tmp_path: Path) -> None:
    host, _live, app, port, _credentials = _app(tmp_path, ScriptedLLM([ScriptedTurn(content='x')]))

    async def body() -> None:
        base = f'http://127.0.0.1:{port}'
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base)  # pyright: ignore[reportArgumentType]
        client = AsyncOpenAI(api_key='nope', base_url=f'{base}/v1', http_client=http, max_retries=0)  # pyright: ignore[reportArgumentType]
        try:
            with pytest.raises(AuthenticationError):
                await client.models.list()
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_stream_lists_ignored_sampling_and_usage(tmp_path: Path) -> None:
    host, _live, app, port, credentials = _app(
        tmp_path, ScriptedLLM([ScriptedTurn(content='streamed')])
    )

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            raw = await client.with_raw_response.chat.completions.create(
                model='Worker',
                messages=[{'role': 'user', 'content': 'hello'}],
                stream=True,
                temperature=0,
                stream_options={'include_usage': True},
            )
            assert raw.headers['x-syndica-ignored'] == 'temperature'
            assert 'x-should-retry' not in {name.lower() for name in raw.headers}
            pieces: list[str] = []
            saw_usage = False
            async for chunk in raw.parse():
                if chunk.usage is not None:
                    saw_usage = True
                    assert chunk.usage.completion_tokens == len('streamed')
                for choice in chunk.choices:
                    if choice.delta.content:
                        pieces.append(choice.delta.content)
                    if choice.finish_reason is not None:
                        assert choice.finish_reason == 'stop'
            assert ''.join(pieces) == 'streamed'
            assert saw_usage
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_responses_continue_with_previous_response_id(tmp_path: Path) -> None:
    host, _live, app, port, credentials = _app(
        tmp_path,
        ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')]),
    )

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            first = await client.responses.create(model='Worker', input='one')
            assert first.output_text == 'first'
            assert first.id.startswith('resp_')
            second = await client.responses.create(
                model='Worker', input='two', previous_response_id=first.id
            )
            assert second.output_text == 'second'
            assert second.id != first.id
            sid = first.id.removeprefix('resp_').rsplit('_', 1)[0]
            assert second.id == f'resp_{sid}_2'
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_responses_stream_is_output_text_deltas(tmp_path: Path) -> None:
    host, _live, app, port, credentials = _app(
        tmp_path, ScriptedLLM([ScriptedTurn(content='streamed')])
    )

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            stream = await client.responses.create(model='Worker', input='hello', stream=True)
            text = ''
            completed = False
            async for event in stream:
                if event.type == 'response.output_text.delta':
                    text += event.delta
                if event.type == 'response.completed':
                    completed = True
                    assert event.response.output_text == 'streamed'
            assert text == 'streamed'
            assert completed
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_an_unknown_model_and_session_are_not_found(tmp_path: Path) -> None:
    host, _live, app, port, credentials = _app(tmp_path, ScriptedLLM([ScriptedTurn(content='x')]))

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            with pytest.raises(NotFoundError, match='model'):
                await client.chat.completions.create(
                    model='Missing', messages=[{'role': 'user', 'content': 'hi'}]
                )
            with pytest.raises(NotFoundError, match='X-Syndica-Session'):
                await client.chat.completions.create(
                    model='Worker',
                    messages=[{'role': 'user', 'content': 'hi'}],
                    extra_headers={'X-Syndica-Session': 'no-such-session'},
                )
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


_GOLDEN = Path(__file__).parent / 'testdata' / 'openai_chat.jsonl'


def test_one_completion_matches_the_golden_transcript(tmp_path: Path) -> None:
    host, _live, app, port, credentials = _app(
        tmp_path, ScriptedLLM([ScriptedTurn(content='from the worker')])
    )

    async def body() -> None:
        base = f'http://127.0.0.1:{port}'
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base)  # pyright: ignore[reportArgumentType]
        try:
            login = await http.post('/api/v1/login', json={'code': credentials.issue_code()})
            token = login.json()['token']
            response = await http.post(
                '/v1/chat/completions',
                headers={'Authorization': f'Bearer {token}'},
                json={
                    'model': 'Worker',
                    'messages': [{'role': 'user', 'content': 'hello'}],
                    'temperature': 0,
                },
            )
            body_json = response.json()
            body_json['id'] = re.sub(r'chatcmpl-[0-9a-f]+', 'chatcmpl-ID', body_json['id'])
            lines = [
                json.dumps(
                    {
                        'model': 'Worker',
                        'messages': [{'role': 'user', 'content': 'hello'}],
                        'temperature': 0,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        'status': response.status_code,
                        'x-syndica-session': 'SID',
                        'x-syndica-ignored': response.headers.get('x-syndica-ignored'),
                        'x-should-retry': response.headers.get('x-should-retry'),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                json.dumps(body_json, ensure_ascii=False, sort_keys=True),
            ]
            assert '\n'.join(lines) + '\n' == _GOLDEN.read_text(encoding='utf-8')
        finally:
            await http.aclose()

    try:
        asyncio.run(body())
    finally:
        host.close()


class _Hold(ScriptedLLM):
    def __init__(
        self, script: list[ScriptedTurn], started: asyncio.Event, release: asyncio.Event
    ) -> None:
        super().__init__(script)
        self._started = started
        self._release = release

    async def create(self, messages: list[ContextBlock], **kwargs: object) -> ContextBlock:
        self._started.set()
        await self._release.wait()
        return await super().create(messages, **kwargs)


def test_a_disconnected_client_leaves_the_turn_in_the_session(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    host, live, app, port, credentials = _app(
        tmp_path, _Hold([ScriptedTurn(content='kept')], started, release)
    )

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            task = asyncio.create_task(
                client.chat.completions.create(
                    model='Worker', messages=[{'role': 'user', 'content': 'go'}]
                )
            )
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            for _ in range(200):
                for session in list(live.sessions.values()):
                    state = await session.snapshot()
                    if 'kept' in json.dumps(state):
                        return
                await asyncio.sleep(0.01)
            raise AssertionError('断开之后这一轮没有在会话里留下 kept')
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_an_attached_ui_approves_the_confirmation(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(id='c1', name='Bash', arguments='{"command": "cat /etc/hosts"}')
                ]
            ),
            ScriptedTurn(content='after'),
        ]
    )
    host, live, app, port, credentials = _app(tmp_path, llm)

    async def body() -> None:
        live.attach()
        client = await _client(app, port, credentials)
        try:
            task = asyncio.create_task(
                client.chat.completions.create(
                    model='Worker', messages=[{'role': 'user', 'content': 'read hosts'}]
                )
            )
            approved = False
            for _ in range(400):
                for session in list(live.sessions.values()):
                    for pending in list(session.pending.values()):
                        if pending.kind == 'confirmation':
                            await session.answer(pending.request_id, 'approve')
                            approved = True
                if task.done():
                    break
                await asyncio.sleep(0.01)
            result = await task
            assert approved
            assert result.choices[0].message.content == 'after'
            seen = json.dumps(
                [[block.content for block in call] for call in llm.calls], default=str
            )
            assert 'localhost' in seen
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_responses_client_tool_round_trip(tmp_path: Path) -> None:
    host, _live, app, port, credentials = _app(
        tmp_path,
        ScriptedLLM(
            [
                ScriptedTurn(
                    tool_calls=[ToolCall(id='call_1', name='lookup', arguments='{"q": "x"}')]
                ),
                ScriptedTurn(content='used lookup'),
            ]
        ),
    )
    tools = [
        {
            'type': 'function',
            'name': 'lookup',
            'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}},
        }
    ]

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            first = await client.responses.create(
                model='Worker',
                input='find x',
                tools=tools,  # pyright: ignore[reportArgumentType]
            )
            call = first.output[0]
            assert call.type == 'function_call'
            assert call.name == 'lookup'
            second = await client.responses.create(
                model='Worker',
                previous_response_id=first.id,
                tools=tools,  # pyright: ignore[reportArgumentType]
                input=[
                    {'type': 'function_call_output', 'call_id': call.call_id, 'output': 'found'}
                ],
            )
            assert second.output_text == 'used lookup'
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()
