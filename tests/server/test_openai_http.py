"""OpenAI SDK 对兼容 HTTP：模型列表、一轮回答、续接、流、拒绝 n>1、客户端工具。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
from io import StringIO
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from openai import (
    APIStatusError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
)
from server_fakes import FakeAssembly

from syndica.cli.assembly import ServerAssembly
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import RuleRedactor, ScriptedLLM, ScriptedTurn, ToolCall, TrustLevel
from syndica.kernel.context import ContextBlock
from syndica.server import Credentials, Live, ProtocolMount, ServerSettings, create_app, openai_http
from syndica.server.openai_http import OpenAIHttp, chat_app, models_app, responses_app

_LEAK = 'Bearer sk-live-quizzical0123456789 postgres://quizuser:quizpw@db.internal/x'
_TIMEOUT = 20


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


def _fields(error: APIStatusError) -> tuple[object, object, object]:
    return error.type, error.param, error.code


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
    host, live, app, port, credentials = _app(
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
            with pytest.raises(BadRequestError, match='client_lookup') as caught:
                await client.chat.completions.create(
                    model='Worker',
                    messages=[{'role': 'user', 'content': 'read'}],
                    tools=tools,  # pyright: ignore[reportArgumentType]
                )
            assert _fields(caught.value) == ('invalid_request_error', 'tools', 'invalid_value')
            assert 'x-syndica-session' not in caught.value.response.headers
            assert live.sessions == {}
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
            with pytest.raises(NotFoundError, match='GET /v1/models') as model:
                await client.chat.completions.create(
                    model='Missing', messages=[{'role': 'user', 'content': 'hi'}]
                )
            assert _fields(model.value) == ('invalid_request_error', None, 'model_not_found')
            with pytest.raises(NotFoundError, match='X-Syndica-Session') as session:
                await client.chat.completions.create(
                    model='Worker',
                    messages=[{'role': 'user', 'content': 'hi'}],
                    extra_headers={'X-Syndica-Session': 'no-such-session'},
                )
            assert _fields(session.value) == ('invalid_request_error', None, 'session_not_found')
            with pytest.raises(NotFoundError, match='previous_response_id') as previous:
                await client.responses.create(
                    model='Worker', input='hi', previous_response_id='resp_no-such-session_1'
                )
            assert _fields(previous.value) == (
                'invalid_request_error',
                'previous_response_id',
                'previous_response_not_found',
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


def test_a_response_object_echoes_its_client_tools_and_lists_tool_choice_as_ignored(
    tmp_path: Path,
) -> None:
    host, _live, app, port, credentials = _app(
        tmp_path,
        ScriptedLLM([ScriptedTurn(content='no tools here'), ScriptedTurn(content='with tools')]),
    )
    tools = [
        {
            'type': 'function',
            'name': 'lookup',
            'description': 'find a thing',
            'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}},
        },
        {'type': 'function', 'name': 'zap', 'parameters': {'type': 'object', 'properties': {}}},
    ]

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            bare = await client.responses.create(model='Worker', input='plain')
            assert (bare.tool_choice, bare.tools) == ('none', [])
            raw = await client.responses.with_raw_response.create(
                model='Worker',
                input='use them',
                tools=tools,  # pyright: ignore[reportArgumentType]
                tool_choice='required',
            )
            tooled = raw.parse()
            assert tooled.tool_choice == 'auto'
            assert [(tool.name, tool.description) for tool in tooled.tools] == [  # pyright: ignore[reportAttributeAccessIssue]
                ('lookup', 'find a thing'),
                ('zap', None),
            ]
            assert 'tool_choice' in raw.headers['X-Syndica-Ignored'].split(',')
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def _function(name: str) -> dict[str, object]:
    return {
        'type': 'function',
        'function': {
            'name': name,
            'description': f'{name} on the client',
            'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}}},
        },
    }


def test_a_delegated_turn_answers_with_the_root_and_keeps_a_childs_bash_on_the_server(
    tmp_path: Path,
) -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='d1',
                        name='Delegate',
                        arguments=json.dumps({'agent': 'Worker', 'query': 'do it'}),
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='b1',
                        name='Bash',
                        arguments=json.dumps({'command': 'echo CHILD-SECRET-CMD'}),
                    )
                ]
            ),
            ScriptedTurn(content='CHILD-ANSWER'),
            ScriptedTurn(content='ROOT-FINAL'),
        ]
    )
    host, live, app, port, credentials = _app(tmp_path, llm)

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            async with asyncio.timeout(_TIMEOUT):
                raw = await client.with_raw_response.chat.completions.create(
                    model='Orchestrator',
                    messages=[{'role': 'user', 'content': 'go'}],
                    tools=[_function('Bash')],  # pyright: ignore[reportArgumentType]
                )
            assert 'CHILD-SECRET-CMD' not in raw.text
            choice = raw.parse().choices[0]
            assert choice.finish_reason == 'stop'
            assert choice.message.tool_calls is None
            assert choice.message.content == 'ROOT-FINAL'
        finally:
            await client.close()
            async with asyncio.timeout(_TIMEOUT):
                await live.shutdown()

    try:
        asyncio.run(body())
    finally:
        host.close()
    ran = [block for call in llm.calls for block in call if block.tool_call_id == 'b1']
    assert ran and 'CHILD-SECRET-CMD' in str(ran[-1].content)


def test_a_childs_question_is_returned_and_the_next_request_answers_the_child(
    tmp_path: Path,
) -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='d1',
                        name='Delegate',
                        arguments=json.dumps({'agent': 'Worker', 'query': 'do it'}),
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='q1',
                        name='AskUserQuestion',
                        arguments=json.dumps(
                            {'question': '子问：选哪个？', 'options': ['红', '蓝']}
                        ),
                    )
                ]
            ),
            ScriptedTurn(content='CHILD-ANSWER'),
            ScriptedTurn(content='ROOT-FINAL'),
        ]
    )
    host, live, app, port, credentials = _app(tmp_path, llm)

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            async with asyncio.timeout(_TIMEOUT):
                first = await client.with_raw_response.chat.completions.create(
                    model='Orchestrator', messages=[{'role': 'user', 'content': 'go'}]
                )
            sid = first.headers['x-syndica-session']
            asked = first.parse().choices[0].message.content or ''
            assert '子问：选哪个？' in asked
            async with asyncio.timeout(_TIMEOUT):
                answered = await client.chat.completions.create(
                    model='Orchestrator',
                    messages=[
                        {'role': 'user', 'content': 'go'},
                        {'role': 'assistant', 'content': asked},
                        {'role': 'user', 'content': '红'},
                    ],
                    extra_headers={'X-Syndica-Session': sid},
                )
            assert answered.choices[0].message.content == 'ROOT-FINAL'
        finally:
            await client.close()
            async with asyncio.timeout(_TIMEOUT):
                await live.shutdown()

    try:
        asyncio.run(body())
    finally:
        host.close()
    child_after_answer = llm.calls[2]
    assert any(block.role == 'tool' and '红' in str(block.content) for block in child_after_answer)


class _Raises(ScriptedLLM):
    async def create(self, messages: list[ContextBlock], **kwargs: object) -> ContextBlock:
        del messages, kwargs
        raise RuntimeError(_LEAK)


def test_a_root_error_is_a_500_that_names_only_its_type(tmp_path: Path) -> None:
    host, _live, app, port, credentials = _app(tmp_path, _Raises([]))

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            with pytest.raises(InternalServerError) as caught:
                async with asyncio.timeout(_TIMEOUT):
                    await client.chat.completions.create(
                        model='Worker', messages=[{'role': 'user', 'content': 'go'}]
                    )
            text = caught.value.response.text
            assert 'quiz' not in text
            assert '根 Agent 报错 RuntimeError' in text
            assert 'agent span' in text
            assert _fields(caught.value) == ('server_error', None, None)
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_audio_and_audio_modalities_are_bad_requests(tmp_path: Path) -> None:
    host, live, app, port, credentials = _app(tmp_path, ScriptedLLM([ScriptedTurn(content='x')]))

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            with pytest.raises(BadRequestError, match='音频'):
                await client.chat.completions.create(
                    model='Worker',
                    messages=[{'role': 'user', 'content': 'hi'}],
                    audio={'voice': 'alloy', 'format': 'wav'},
                )
            with pytest.raises(BadRequestError, match='音频'):
                await client.chat.completions.create(
                    model='Worker',
                    messages=[{'role': 'user', 'content': 'hi'}],
                    modalities=['text', 'audio'],
                )
            assert live.sessions == {}
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_changing_the_tools_on_a_continued_session_is_a_bad_request(tmp_path: Path) -> None:
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    host, _live, app, port, credentials = _app(tmp_path, llm)
    history = [{'role': 'user', 'content': 'one'}, {'role': 'assistant', 'content': 'first'}]

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            first = await client.with_raw_response.chat.completions.create(
                model='Worker',
                messages=[{'role': 'user', 'content': 'one'}],
                tools=[_function('lookup')],  # pyright: ignore[reportArgumentType]
            )
            sid = first.headers['x-syndica-session']
            with pytest.raises(BadRequestError, match='工具集') as caught:
                await client.chat.completions.create(
                    model='Worker',
                    messages=[*history, {'role': 'user', 'content': 'two'}],  # pyright: ignore[reportArgumentType]
                    tools=[_function('fetch')],  # pyright: ignore[reportArgumentType]
                    extra_headers={'X-Syndica-Session': sid},
                )
            assert _fields(caught.value) == ('invalid_request_error', 'tools', 'invalid_value')
            assert caught.value.response.headers['x-syndica-session'] == sid
            assert len(llm.calls) == 1
            second = await client.chat.completions.create(
                model='Worker',
                messages=[*history, {'role': 'user', 'content': 'two'}],  # pyright: ignore[reportArgumentType]
                tools=[_function('lookup')],  # pyright: ignore[reportArgumentType]
                extra_headers={'X-Syndica-Session': sid},
            )
            assert second.choices[0].message.content == 'second'
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_question_answer_outside_the_options_is_a_bad_request(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='q1',
                        name='AskUserQuestion',
                        arguments=json.dumps({'question': '选哪个？', 'options': ['红', '蓝']}),
                    )
                ]
            ),
            ScriptedTurn(content='picked red'),
        ]
    )
    host, _live, app, port, credentials = _app(tmp_path, llm)

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            first = await client.with_raw_response.chat.completions.create(
                model='Worker', messages=[{'role': 'user', 'content': 'ask'}]
            )
            sid = first.headers['x-syndica-session']
            asked = first.parse().choices[0].message.content or ''
            assert '选哪个？' in asked and '红' in asked
            history = [
                {'role': 'user', 'content': 'ask'},
                {'role': 'assistant', 'content': asked},
            ]
            with pytest.raises(BadRequestError, match='选项之一') as caught:
                await client.chat.completions.create(
                    model='Worker',
                    messages=[*history, {'role': 'user', 'content': '绿'}],  # pyright: ignore[reportArgumentType]
                    extra_headers={'X-Syndica-Session': sid},
                )
            assert _fields(caught.value) == ('invalid_request_error', None, 'invalid_value')
            assert caught.value.response.headers['x-syndica-session'] == sid
            answered = await client.chat.completions.create(
                model='Worker',
                messages=[*history, {'role': 'user', 'content': '红'}],  # pyright: ignore[reportArgumentType]
                extra_headers={'X-Syndica-Session': sid},
            )
            assert answered.choices[0].message.content == 'picked red'
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_question_keeps_the_text_the_root_said_before_it(tmp_path: Path) -> None:
    ask = ToolCall(
        id='q1',
        name='AskUserQuestion',
        arguments=json.dumps({'question': '选哪个？', 'options': ['红', '蓝']}),
    )
    llm = ScriptedLLM([ScriptedTurn(content='先确认一下', tool_calls=[ask])])
    host, live, app, port, credentials = _app(tmp_path, llm)

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            async with asyncio.timeout(_TIMEOUT):
                asked = await client.chat.completions.create(
                    model='Worker', messages=[{'role': 'user', 'content': 'ask'}]
                )
            assert asked.choices[0].message.content == '先确认一下\n选哪个？\n1. 红\n2. 蓝'
        finally:
            await client.close()
            async with asyncio.timeout(_TIMEOUT):
                await live.shutdown()

    try:
        asyncio.run(body())
    finally:
        host.close()


class _Explodes(OpenAIHttp):
    async def chat(self, body: object, exchange: object) -> Any:
        del body, exchange
        raise RuntimeError(_LEAK)


async def test_an_unexpected_exception_is_a_500_that_names_only_its_type() -> None:
    live = Live(FakeAssembly())
    app = chat_app(_Explodes(live, ('Worker',), RuleRedactor()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='http://openai', timeout=10
    ) as client:
        response = await client.post(
            '/', json={'model': 'Worker', 'messages': [{'role': 'user', 'content': 'hi'}]}
        )
    assert response.status_code == 500
    assert 'quiz' not in response.text
    assert '抛出 RuntimeError' in response.text
    assert 'POST 不要重放' in response.text
    error = response.json()['error']
    assert (error['type'], error['param'], error['code']) == ('server_error', None, None)
    assert 'x-syndica-session' not in response.headers


async def test_an_unexpected_exception_leaves_its_stack_in_the_server_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    live = Live(FakeAssembly())
    app = chat_app(_Explodes(live, ('Worker',), RuleRedactor()))
    with caplog.at_level(logging.ERROR, logger='syndica.server.openai_http'):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://openai', timeout=10
        ) as client:
            response = await client.post(
                '/', json={'model': 'Worker', 'messages': [{'role': 'user', 'content': 'hi'}]}
            )
    assert response.status_code == 500
    [record] = [r for r in caplog.records if r.name == 'syndica.server.openai_http']
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError


_REFUSALS: list[tuple[str, dict[str, object], str, str]] = [
    ('chat', {'logprobs': True}, 'logprobs', 'unsupported_parameter'),
    ('chat', {'logprobs': 1}, 'logprobs', 'invalid_type'),
    ('chat', {'logprobs': 0}, 'logprobs', 'invalid_type'),
    (
        'chat',
        {'response_format': {'type': 'json_schema', 'json_schema': {'name': 'x'}}},
        'response_format',
        'unsupported_value',
    ),
    ('chat', {'audio': {'voice': 'alloy', 'format': 'wav'}}, 'audio', 'unsupported_parameter'),
    ('chat', {'modalities': ['text', 'audio']}, 'modalities', 'unsupported_value'),
    ('chat', {'tools': 'lookup'}, 'tools', 'invalid_type'),
    ('chat', {'tools': [_function('lookup'), 'fetch']}, 'tools[1]', 'invalid_type'),
    (
        'chat',
        {'tools': [_function('lookup'), {'type': 'custom', 'custom': {'name': 'grammar'}}]},
        'tools[1].type',
        'invalid_value',
    ),
    (
        'chat',
        {'tools': [{'type': 'function', 'function': {'parameters': {}}}]},
        'tools[0].function.name',
        'missing_required_parameter',
    ),
    ('chat', {'messages': None}, 'messages', 'missing_required_parameter'),
    ('chat', {'messages': 'hi'}, 'messages', 'invalid_type'),
    ('chat', {'messages': []}, 'messages', 'invalid_value'),
    ('chat', {'messages': [{'role': 'user', 'content': 'a'}, 'b']}, 'messages[1]', 'invalid_type'),
    ('chat', {'messages': [{'content': 'a'}]}, 'messages[0].role', 'missing_required_parameter'),
    ('chat', {'model': 7}, 'model', 'invalid_type'),
    ('chat', {'model': ' '}, 'model', 'invalid_value'),
    ('responses', {'tools': [{'type': 'web_search'}]}, 'tools[0].type', 'invalid_value'),
    (
        'responses',
        {'tools': [{'type': 'function', 'parameters': {}}]},
        'tools[0].name',
        'missing_required_parameter',
    ),
    ('responses', {'input': []}, 'input', 'invalid_value'),
    ('responses', {'input': [3]}, 'input[0]', 'invalid_type'),
    ('responses', {'input': [{'content': 'x'}]}, 'input[0].role', 'missing_required_parameter'),
]


async def _refused(
    client: AsyncOpenAI, endpoint: str, overrides: dict[str, object]
) -> BadRequestError:
    with pytest.raises(BadRequestError) as caught:
        if endpoint == 'chat':
            await client.chat.completions.create(
                model='Worker', messages=[{'role': 'user', 'content': 'hi'}], extra_body=overrides
            )
        else:
            await client.responses.create(model='Worker', input='hi', extra_body=overrides)
    return caught.value


@pytest.mark.parametrize(
    ('endpoint', 'overrides', 'param', 'code'),
    _REFUSALS,
    ids=[
        f'{endpoint}-{param}-{code}-{json.dumps(overrides)[:24]}'
        for endpoint, overrides, param, code in _REFUSALS
    ],
)
def test_a_refused_request_names_the_openai_param_and_code_and_opens_no_session(
    tmp_path: Path, endpoint: str, overrides: dict[str, object], param: str, code: str
) -> None:
    host, live, app, port, credentials = _app(tmp_path, ScriptedLLM([ScriptedTurn(content='x')]))

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            refused = await _refused(client, endpoint, overrides)
            assert _fields(refused) == ('invalid_request_error', param, code)
            assert isinstance(refused.body, dict)
            assert str(cast(dict[str, Any], refused.body)['message']).startswith('期望: ')
            assert live.sessions == {}
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


@pytest.mark.parametrize(
    ('n', 'code'),
    [
        (True, 'invalid_type'),
        (False, 'invalid_type'),
        ('1', 'invalid_type'),
        (1.0, 'invalid_type'),
        (2.5, 'invalid_type'),
        (0, 'unsupported_value'),
        (2, 'unsupported_value'),
        (-1, 'unsupported_value'),
    ],
    ids=['true', 'false', 'text-1', 'float-1.0', 'float-2.5', 'zero', 'two', 'minus-one'],
)
def test_n_refuses_bool_text_and_fractions_as_a_type_and_other_integers_as_unsupported(
    tmp_path: Path, n: object, code: str
) -> None:
    host, live, app, port, credentials = _app(tmp_path, ScriptedLLM([ScriptedTurn(content='x')]))

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            refused = await _refused(client, 'chat', {'n': n})
            assert _fields(refused) == ('invalid_request_error', 'n', code)
            assert live.sessions == {}
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_n_of_one_or_null_is_accepted(tmp_path: Path) -> None:
    host, _live, app, port, credentials = _app(
        tmp_path, ScriptedLLM([ScriptedTurn(content='one'), ScriptedTurn(content='null')])
    )

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            for n, expected in ((1, 'one'), (None, 'null')):
                completion = await client.chat.completions.create(
                    model='Worker',
                    messages=[{'role': 'user', 'content': 'hi'}],
                    extra_body={'n': n},
                )
                assert completion.choices[0].message.content == expected
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_refusal_on_a_continued_session_carries_its_session_header(tmp_path: Path) -> None:
    host, _live, app, port, credentials = _app(
        tmp_path, ScriptedLLM([ScriptedTurn(content='chat'), ScriptedTurn(content='responses')])
    )

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            first = await client.with_raw_response.chat.completions.create(
                model='Worker', messages=[{'role': 'user', 'content': 'one'}]
            )
            sid = first.headers['x-syndica-session']
            with pytest.raises(BadRequestError) as chat:
                await client.chat.completions.create(
                    model='Worker',
                    messages=[{'role': 'user', 'content': 'two'}],
                    extra_headers={'X-Syndica-Session': sid},
                    extra_body={'n': True},
                )
            assert chat.value.response.headers['x-syndica-session'] == sid
            opened = await client.responses.create(model='Worker', input='one')
            with pytest.raises(BadRequestError) as responses:
                await client.responses.create(
                    model='Worker',
                    input='two',
                    previous_response_id=opened.id,
                    extra_body={'n': True},
                )
            other = opened.id.removeprefix('resp_').rsplit('_', 1)[0]
            assert other != sid
            assert responses.value.response.headers['x-syndica-session'] == other
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_root_error_in_a_new_session_carries_the_session_it_opened(tmp_path: Path) -> None:
    host, live, app, port, credentials = _app(tmp_path, _Raises([]))

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            with pytest.raises(InternalServerError) as caught:
                async with asyncio.timeout(_TIMEOUT):
                    await client.chat.completions.create(
                        model='Worker', messages=[{'role': 'user', 'content': 'go'}]
                    )
            sid = caught.value.response.headers['x-syndica-session']
            assert list(live.sessions) == [sid]
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_errors_outside_any_open_session_carry_no_session_header(tmp_path: Path) -> None:
    host, live, app, port, credentials = _app(tmp_path, ScriptedLLM([ScriptedTurn(content='x')]))

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            refusals: list[APIStatusError] = [await _refused(client, 'chat', {'n': True})]
            with pytest.raises(NotFoundError) as model:
                await client.chat.completions.create(
                    model='Missing', messages=[{'role': 'user', 'content': 'hi'}]
                )
            refusals.append(model.value)
            with pytest.raises(NotFoundError) as session:
                await client.chat.completions.create(
                    model='Worker',
                    messages=[{'role': 'user', 'content': 'hi'}],
                    extra_headers={'X-Syndica-Session': 'no-such-session'},
                )
            refusals.append(session.value)
            with pytest.raises(NotFoundError) as previous:
                await client.responses.create(
                    model='Worker', input='hi', previous_response_id='resp_no-such-session_1'
                )
            refusals.append(previous.value)
            assert [error.response.headers.get('x-syndica-session') for error in refusals] == [
                None,
                None,
                None,
                None,
            ]
            assert live.sessions == {}
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


@pytest.mark.parametrize(
    'raw',
    [b'{"model": ', b'\xff\xfe', b'[1, 2]', b'"Worker"'],
    ids=['truncated', 'not-utf8', 'array', 'string'],
)
def test_a_body_that_is_not_a_json_object_is_a_bad_request_not_a_server_error(
    tmp_path: Path, raw: bytes
) -> None:
    host, live, app, port, credentials = _app(tmp_path, ScriptedLLM([ScriptedTurn(content='x')]))

    async def body() -> None:
        base = f'http://127.0.0.1:{port}'
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base)  # pyright: ignore[reportArgumentType]
        try:
            login = await http.post('/api/v1/login', json={'code': credentials.issue_code()})
            token = login.json()['token']
            response = await http.post(
                '/v1/chat/completions',
                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                content=raw,
            )
            assert response.status_code == 400
            error = response.json()['error']
            assert (error['type'], error['param'], error['code']) == (
                'invalid_request_error',
                None,
                None,
            )
            assert error['message'].startswith('期望: 请求体是')
            assert 'x-syndica-session' not in response.headers
            assert live.sessions == {}
        finally:
            await http.aclose()

    try:
        asyncio.run(body())
    finally:
        host.close()


class _Second(ScriptedLLM):
    def __init__(self, script: list[ScriptedTurn], reached: asyncio.Event) -> None:
        super().__init__(script)
        self._reached = reached

    async def create(self, messages: list[ContextBlock], **kwargs: object) -> ContextBlock:
        reply = await super().create(messages, **kwargs)
        if len(self.calls) == 2:
            self._reached.set()
        return reply


def test_a_client_tool_result_that_never_comes_hands_the_model_a_missed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(openai_http, '_TOOL_WAIT_S', 0.05)
    reached = asyncio.Event()
    llm = _Second(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='call_1', name='lookup', arguments='{"q": "x"}')]),
            ScriptedTurn(content='gave up'),
        ],
        reached,
    )
    host, _live, app, port, credentials = _app(tmp_path, llm)

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            asked = await client.chat.completions.create(
                model='Worker',
                messages=[{'role': 'user', 'content': 'find x'}],
                tools=[_function('lookup')],  # pyright: ignore[reportArgumentType]
            )
            assert asked.choices[0].finish_reason == 'tool_calls'
            async with asyncio.timeout(_TIMEOUT):
                await reached.wait()
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()
    [missed] = [block for block in llm.calls[1] if block.tool_call_id == 'call_1']
    assert '实际: timeout' in str(missed.content)
    assert '带上这个 tool_call_id 的 tool 结果' in str(missed.content)


def _blocks(*texts: str) -> list[ScriptedTurn]:
    """每段文本一个 assistant 块；除最后一块外各带一次服务端的 Glob，于是一轮里有多块回答。"""
    glob = '{"pattern": "*.yaml"}'
    served = [
        ScriptedTurn(
            content=text, tool_calls=[ToolCall(id=f'g{index}', name='Glob', arguments=glob)]
        )
        for index, text in enumerate(texts[:-1])
    ]
    return [*served, ScriptedTurn(content=texts[-1])]


def test_a_chat_stream_sends_each_assistant_block_as_its_own_delta(tmp_path: Path) -> None:
    llm = ScriptedLLM([*_blocks('mid', 'zeta', 'alpha'), *_blocks('mid', 'zeta', 'alpha')])
    host, _live, app, port, credentials = _app(tmp_path, llm)

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            whole = await client.chat.completions.create(
                model='Worker', messages=[{'role': 'user', 'content': 'go'}]
            )
            assert whole.choices[0].message.content == 'mid\nzeta\nalpha'
            stream = await client.chat.completions.create(
                model='Worker', messages=[{'role': 'user', 'content': 'go'}], stream=True
            )
            deltas: list[str] = []
            roles: list[str] = []
            async for chunk in stream:
                for choice in chunk.choices:
                    if choice.delta.content is not None:
                        deltas.append(choice.delta.content)
                    if choice.delta.role is not None:
                        roles.append(choice.delta.role)
            assert deltas == ['mid', '\nzeta', '\nalpha']
            assert ''.join(deltas) == whole.choices[0].message.content
            assert roles == ['assistant']
        finally:
            await client.close()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_turn_that_ends_in_client_tool_calls_keeps_the_text_before_them(tmp_path: Path) -> None:
    glob = ToolCall(id='g0', name='Glob', arguments='{"pattern": "*.yaml"}')
    lookup = ToolCall(id='call_1', name='lookup', arguments='{"q": "x"}')
    script = [
        ScriptedTurn(content='mid', tool_calls=[glob]),
        ScriptedTurn(content='zeta', tool_calls=[lookup]),
    ]
    host, live, app, port, credentials = _app(tmp_path, ScriptedLLM(script * 3))
    tools: Any = [_function('lookup')]

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            async with asyncio.timeout(_TIMEOUT):
                whole = await client.chat.completions.create(
                    model='Worker', messages=[{'role': 'user', 'content': 'go'}], tools=tools
                )
            message = whole.choices[0].message
            assert whole.choices[0].finish_reason == 'tool_calls'
            assert message.content == 'mid\nzeta'
            assert [call.id for call in message.tool_calls or []] == ['call_1']
            async with asyncio.timeout(_TIMEOUT):
                stream = await client.chat.completions.create(
                    model='Worker',
                    messages=[{'role': 'user', 'content': 'go'}],
                    tools=tools,
                    stream=True,
                )
                kinds: list[str] = []
                async for chunk in stream:
                    for choice in chunk.choices:
                        if choice.delta.content is not None:
                            kinds.append(f'content:{choice.delta.content}')
                        for call in choice.delta.tool_calls or []:
                            kinds.append(f'tool_call:{call.id}')
            assert kinds == ['content:mid', 'content:\nzeta', 'tool_call:call_1']
            async with asyncio.timeout(_TIMEOUT):
                response = await client.responses.create(
                    model='Worker',
                    input='go',
                    tools=[{'type': 'function', 'name': 'lookup', 'parameters': {}}],  # pyright: ignore[reportArgumentType]
                )
            assert [item.type for item in response.output] == ['message', 'function_call']
            assert response.output_text == 'mid\nzeta'
        finally:
            await client.close()
            async with asyncio.timeout(_TIMEOUT):
                await live.shutdown()

    try:
        asyncio.run(body())
    finally:
        host.close()


_CALLS = [
    ToolCall(id='call_2', name='lookup', arguments='{"q": "two"}'),
    ToolCall(id='call_3', name='zap', arguments='{"q": "three"}'),
    ToolCall(id='call_1', name='fetch', arguments='{"q": "one"}'),
]
_RESPONSE_TOOLS: list[dict[str, Any]] = [
    {'type': 'function', 'name': name, 'parameters': {'type': 'object', 'properties': {}}}
    for name in ('lookup', 'zap', 'fetch')
]


def test_a_responses_stream_emits_every_function_call_in_order(tmp_path: Path) -> None:
    host, live, app, port, credentials = _app(
        tmp_path, ScriptedLLM([ScriptedTurn(tool_calls=list(_CALLS))])
    )
    expected = [(0, 'call_2'), (1, 'call_3'), (2, 'call_1')]

    async def body() -> None:
        client = await _client(app, port, credentials)
        try:
            added: list[tuple[int, str]] = []
            argued: list[tuple[int, str]] = []
            done: list[tuple[int, str]] = []
            async with asyncio.timeout(_TIMEOUT):
                async with client.responses.stream(
                    model='Worker',
                    input='go',
                    tools=_RESPONSE_TOOLS,  # pyright: ignore[reportArgumentType]
                ) as stream:
                    async for event in stream:
                        if event.type == 'response.output_item.added':
                            assert event.item.type == 'function_call'
                            added.append((event.output_index, event.item.call_id))
                        elif event.type == 'response.function_call_arguments.done':
                            argued.append((event.output_index, event.arguments))
                        elif event.type == 'response.output_item.done':
                            assert event.item.type == 'function_call'
                            done.append((event.output_index, event.item.call_id))
                    final = await stream.get_final_response()
            assert added == expected
            assert done == expected
            assert argued == [(0, '{"q": "two"}'), (1, '{"q": "three"}'), (2, '{"q": "one"}')]
            assert [getattr(item, 'call_id', None) for item in final.output] == [
                'call_2',
                'call_3',
                'call_1',
            ]
        finally:
            await client.close()
            async with asyncio.timeout(_TIMEOUT):
                await live.shutdown()

    try:
        asyncio.run(body())
    finally:
        host.close()


_ERRORS_GOLDEN = Path(__file__).parent / 'testdata' / 'openai_errors.jsonl'
_STREAMS_GOLDEN = Path(__file__).parent / 'testdata' / 'openai_streams.jsonl'


class _Wire:
    """原样记下线上往返：请求、状态与协议头、响应体（SSE 逐条）。会话 id 按出现顺序换成 SID-n。"""

    def __init__(self, http: httpx.AsyncClient, token: str) -> None:
        self._http = http
        self._token = token
        self._sids: list[str] = []
        self.lines: list[str] = []

    async def post(
        self, path: str, payload: dict[str, object], *, session: str | None = None
    ) -> str | None:
        headers = {'Authorization': f'Bearer {self._token}'}
        if session is not None:
            headers['X-Syndica-Session'] = session
        response = await self._http.post(path, headers=headers, json=payload)
        sid = response.headers.get('x-syndica-session')
        if sid is not None and sid not in self._sids:
            self._sids.append(sid)
        self._line({'path': path, 'x-syndica-session': session, 'body': payload})
        self._line(
            {
                'status': response.status_code,
                'content-type': response.headers.get('content-type'),
                'x-syndica-session': sid,
                'x-should-retry': response.headers.get('x-should-retry'),
            }
        )
        if response.headers.get('content-type', '').startswith('text/event-stream'):
            for line in response.text.splitlines():
                if line.startswith('data: '):
                    data = line.removeprefix('data: ')
                    self._line(data if data == '[DONE]' else json.loads(data))
        else:
            self._line(response.json())
        return sid

    def _line(self, value: object) -> None:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        text = re.sub(r'chatcmpl-[0-9a-f]+', 'chatcmpl-ID', text)
        for index, sid in enumerate(self._sids, 1):
            text = text.replace(sid, f'SID-{index}')
        self.lines.append(text)


async def _wire(
    app: object, port: int, credentials: Credentials
) -> tuple[httpx.AsyncClient, _Wire]:
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # pyright: ignore[reportArgumentType]
        base_url=f'http://127.0.0.1:{port}',
    )
    login = await http.post('/api/v1/login', json={'code': credentials.issue_code()})
    return http, _Wire(http, login.json()['token'])


def test_the_error_mapping_matches_the_golden_transcript(tmp_path: Path) -> None:
    host, _live, app, port, credentials = _app(tmp_path, _Raises([]))
    hello = [{'role': 'user', 'content': 'hello'}]

    async def body() -> None:
        http, wire = await _wire(app, port, credentials)
        try:
            await wire.post('/v1/chat/completions', {'model': 'Missing', 'messages': hello})
            await wire.post(
                '/v1/chat/completions', {'model': 'Worker', 'messages': hello, 'n': True}
            )
            await wire.post(
                '/v1/chat/completions',
                {'model': 'Worker', 'messages': hello},
                session='no-such-session',
            )
            await wire.post(
                '/v1/responses',
                {
                    'model': 'Worker',
                    'input': 'hello',
                    'previous_response_id': 'resp_no-such-session_1',
                },
            )
            async with asyncio.timeout(_TIMEOUT):
                sid = await wire.post(
                    '/v1/chat/completions', {'model': 'Worker', 'messages': hello}
                )
            assert sid is not None
            await wire.post(
                '/v1/chat/completions', {'model': 'Worker', 'messages': hello, 'n': 2}, session=sid
            )
            assert '\n'.join(wire.lines) + '\n' == _ERRORS_GOLDEN.read_text(encoding='utf-8')
        finally:
            await http.aclose()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_the_stream_mapping_matches_the_golden_transcript(tmp_path: Path) -> None:
    host, live, app, port, credentials = _app(
        tmp_path,
        ScriptedLLM([*_blocks('mid', 'zeta', 'alpha'), ScriptedTurn(tool_calls=list(_CALLS))]),
    )

    async def body() -> None:
        http, wire = await _wire(app, port, credentials)
        try:
            async with asyncio.timeout(_TIMEOUT):
                await wire.post(
                    '/v1/chat/completions',
                    {
                        'model': 'Worker',
                        'messages': [{'role': 'user', 'content': 'go'}],
                        'stream': True,
                        'stream_options': {'include_usage': True},
                    },
                )
                await wire.post(
                    '/v1/responses',
                    {'model': 'Worker', 'input': 'go', 'stream': True, 'tools': _RESPONSE_TOOLS},
                )
            assert '\n'.join(wire.lines) + '\n' == _STREAMS_GOLDEN.read_text(encoding='utf-8')
        finally:
            await http.aclose()
            async with asyncio.timeout(_TIMEOUT):
                await live.shutdown()

    try:
        asyncio.run(body())
    finally:
        host.close()
