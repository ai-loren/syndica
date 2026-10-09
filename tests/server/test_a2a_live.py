"""A2A 对真会话：SDK 客户端走 1.0 与 0.3、contextId 就是会话 id、流里逐条交根 Agent 的文本。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import socket
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Role,
    SendMessageRequest,
    TaskState,
)
from google.protobuf.struct_pb2 import Struct

from syndica.backends.a2a import A2AAgentModel
from syndica.backends.a2a.model import HttpxA2A
from syndica.cli.assembly import ServerAssembly
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import ContextBlock, ScriptedLLM, ScriptedTurn, ToolCall, TrustLevel
from syndica.serve import a2a_http_mounts
from syndica.server import Credentials, Live, ServerSettings, create_app

_TIMEOUT = 20
_GOLDEN = Path(__file__).parent / 'testdata' / 'a2a_session_stream.jsonl'


@dataclass
class _Served:
    base: str
    token: str
    live: Live

    @property
    def rpc(self) -> str:
        return f'{self.base}/a2a/'


def _host(tmp_path: Path, llm: ScriptedLLM) -> tuple[RuntimeCliHost, Live, Any]:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    config = workspace / '.syndica.yaml'
    config.write_text('version: 1\n', encoding='utf-8')
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=workspace,
    )
    loaded = host.trusted_config(str(config))
    return host, Live(ServerAssembly(host, path=str(config), config=loaded)), loaded


@contextlib.asynccontextmanager
async def _serving(live: Live, loaded: Any) -> AsyncIterator[_Served]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    sock.listen(16)
    port = int(sock.getsockname()[1])
    credentials = Credentials()
    token = credentials.issue_api_token()
    app = create_app(
        live,
        credentials,
        ServerSettings(port=port, nonce='nonce', version='test'),
        protocol_mounts=a2a_http_mounts(loaded, port, live, 'ws'),
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning', lifespan='on'))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(1000):
        if server.started:
            break
        await asyncio.sleep(0.01)
    try:
        yield _Served(f'http://127.0.0.1:{port}', token, live)
    finally:
        await live.shutdown()
        server.should_exit = True
        await asyncio.wait_for(task, 10)


def _run(tmp_path: Path, llm: ScriptedLLM, body: Callable[[_Served], Any]) -> None:
    host, live, loaded = _host(tmp_path, llm)

    async def wrapped() -> None:
        async with _serving(live, loaded) as served:
            async with asyncio.timeout(_TIMEOUT):
                await body(served)

    try:
        asyncio.run(wrapped())
    finally:
        host.close()


def _request(text: str, role: str, *, context_id: str = '') -> SendMessageRequest:
    message = Message(message_id='m-sdk', role=Role.ROLE_USER, context_id=context_id)
    message.parts.add(text=text)
    metadata = Struct()
    metadata.update({'syndica.role': role})
    message.metadata.CopyFrom(metadata)
    return SendMessageRequest(message=message)


def _text(holder: Any) -> str:
    parts = list(holder.parts)
    return str(parts[0].text) if parts else ''


def _delegate(content: str) -> ScriptedTurn:
    return ScriptedTurn(
        content=content,
        tool_calls=[
            ToolCall(
                id='d1',
                name='Delegate',
                arguments=json.dumps({'agent': 'Worker', 'query': 'do it'}),
            )
        ],
    )


class _HoldsTheChild(ScriptedLLM):
    """第二次调用（派出去的子 Agent）等到放行才答。"""

    def __init__(self, script: list[ScriptedTurn]) -> None:
        super().__init__(script)
        self.release: asyncio.Event | None = None

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        if len(self.calls) == 1 and self.release is not None:
            await self.release.wait()
        return await super().create(messages, **kwargs)


def _events(event: Any) -> tuple[str, int, str, str]:
    kind = str(event.WhichOneof('payload'))
    if kind == 'task':
        status = event.task.status
        return ('task', status.state, _text(status.message), event.task.context_id)
    if kind == 'status_update':
        update = event.status_update
        return ('status', update.status.state, _text(update.status.message), update.context_id)
    if kind == 'artifact_update':
        update = event.artifact_update
        return ('artifact', 0, _text(update.artifact), update.context_id)
    return (kind, 0, '', '')


def test_the_sdk_stream_is_the_task_then_root_texts_as_produced_then_the_final_state(
    tmp_path: Path,
) -> None:
    llm = _HoldsTheChild(
        [
            _delegate('ROOT-PLAN'),
            ScriptedTurn(content='CHILD-ANSWER'),
            ScriptedTurn(content='ROOT-FINAL'),
        ]
    )
    seen: list[tuple[str, int, str, str]] = []

    async def body(served: _Served) -> None:
        release = asyncio.Event()
        llm.release = release
        http = httpx.AsyncClient(headers={'Authorization': f'Bearer {served.token}'}, timeout=10)
        client = await create_client(served.base, ClientConfig(streaming=True, httpx_client=http))
        try:
            async for event in client.send_message(_request('go', 'Orchestrator')):
                seen.append(_events(event))
                if seen[-1][:3] == ('status', TaskState.TASK_STATE_WORKING, 'ROOT-PLAN'):
                    release.set()
        finally:
            await client.close()
        [sid] = served.live.sessions
        assert {context for *_, context in seen} == {sid}

    _run(tmp_path, llm, body)
    final = 'ROOT-PLAN\nROOT-FINAL'
    assert [row[:3] for row in seen] == [
        ('task', TaskState.TASK_STATE_WORKING, ''),
        ('status', TaskState.TASK_STATE_WORKING, 'ROOT-PLAN'),
        ('status', TaskState.TASK_STATE_WORKING, 'ROOT-FINAL'),
        ('artifact', 0, final),
        ('status', TaskState.TASK_STATE_COMPLETED, final),
    ]


def test_the_sdk_client_continues_a_session_by_context_id_and_an_unknown_one_is_refused(
    tmp_path: Path,
) -> None:
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])

    async def body(served: _Served) -> None:
        http = httpx.AsyncClient(headers={'Authorization': f'Bearer {served.token}'}, timeout=10)
        client = await create_client(served.base, ClientConfig(streaming=False, httpx_client=http))
        try:
            [first] = [event async for event in client.send_message(_request('one', 'Worker'))]
            context_id = first.task.context_id
            assert list(served.live.sessions) == [context_id]
            [second] = [
                event
                async for event in client.send_message(
                    _request('two', 'Worker', context_id=context_id)
                )
            ]
            assert second.task.context_id == context_id
            assert _text(second.task.status.message) == 'second'
            [refused] = [
                event
                async for event in client.send_message(
                    _request('three', 'Worker', context_id='made-up-context')
                )
            ]
        finally:
            await client.close()
        assert refused.task.status.state == TaskState.TASK_STATE_FAILED
        text = _text(refused.task.status.message)
        assert "contextId='made-up-context'" in text
        assert '去掉 contextId 开一个新会话' in text
        assert list(served.live.sessions) == [context_id]

    _run(tmp_path, llm, body)
    users = [str(block.content) for block in llm.calls[1] if block.role == 'user']
    assert any('one' in text for text in users) and any('two' in text for text in users)


def test_a_streamed_message_with_an_unknown_context_id_is_one_failed_task(tmp_path: Path) -> None:
    async def body(served: _Served) -> None:
        http = httpx.AsyncClient(headers={'Authorization': f'Bearer {served.token}'}, timeout=10)
        client = await create_client(served.base, ClientConfig(streaming=True, httpx_client=http))
        try:
            events = [
                _events(event)
                async for event in client.send_message(
                    _request('hi', 'Worker', context_id='made-up-context')
                )
            ]
        finally:
            await client.close()
        assert [row[:2] for row in events] == [('task', TaskState.TASK_STATE_FAILED)]
        assert '去掉 contextId 开一个新会话' in events[0][2]
        assert served.live.sessions == {}

    _run(tmp_path, ScriptedLLM([]), body)


def test_the_sdk_client_speaking_0_3_reaches_the_session_mapping(tmp_path: Path) -> None:
    llm = ScriptedLLM([ScriptedTurn(content='zero-three'), ScriptedTurn(content='again')])
    wire: list[tuple[str, str | None]] = []

    async def record(request: httpx.Request) -> None:
        if request.method == 'POST':
            wire.append((json.loads(request.content)['method'], request.headers.get('a2a-version')))

    async def body(served: _Served) -> None:
        http = httpx.AsyncClient(
            headers={'Authorization': f'Bearer {served.token}'},
            timeout=10,
            event_hooks={'request': [record]},
        )
        card = await A2ACardResolver(http, served.base).get_agent_card()
        legacy = AgentCard()
        legacy.CopyFrom(card)
        del legacy.supported_interfaces[:]
        legacy.supported_interfaces.add(
            url=served.rpc, protocol_binding='JSONRPC', protocol_version='0.3'
        )
        client = await create_client(legacy, ClientConfig(streaming=True, httpx_client=http))
        try:
            first = [
                _events(event) async for event in client.send_message(_request('ping', 'Worker'))
            ]
            context_id = first[0][3]
            task_id = ''
            fetched_text = ''
            [sid] = served.live.sessions
            assert context_id == sid
            async for event in client.send_message(
                _request('pong', 'Worker', context_id=context_id)
            ):
                if event.WhichOneof('payload') == 'task':
                    task_id = event.task.id
            fetched = await client.get_task(GetTaskRequest(id=task_id))
            fetched_text = _text(fetched.status.message)
            canceled = await client.cancel_task(CancelTaskRequest(id=task_id))
        finally:
            await client.close()
        assert [row[:3] for row in first][-1] == (
            'status',
            TaskState.TASK_STATE_COMPLETED,
            'zero-three',
        )
        assert fetched.context_id == sid
        assert fetched_text == 'again'
        assert canceled.status.state == TaskState.TASK_STATE_CANCELED
        assert list(served.live.sessions) == [sid]

    _run(tmp_path, llm, body)
    assert wire == [
        ('message/stream', '0.3'),
        ('message/stream', '0.3'),
        ('tasks/get', '0.3'),
        ('tasks/cancel', '0.3'),
    ]


class _Recording:
    def __init__(self) -> None:
        self.inner = HttpxA2A()
        self.sent: list[dict[str, Any]] = []
        self.received: list[Any] = []

    async def get_json(self, url: str, headers: Mapping[str, str], deadline: float | None) -> Any:
        return await self.inner.get_json(url, headers, deadline)

    async def post_json(
        self,
        url: str,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
        deadline: float | None,
    ) -> Any:
        self.sent.append(dict(body))
        reply = await self.inner.post_json(url, body, headers, deadline)
        self.received.append(reply)
        return reply

    async def aclose(self) -> None:
        await self.inner.aclose()


def test_syndica_talks_to_syndica_on_the_session_the_server_returned(tmp_path: Path) -> None:
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
            ScriptedTurn(content='third turn'),
        ]
    )

    async def body(served: _Served) -> None:
        recording = _Recording()
        model = A2AAgentModel('ws', served.rpc, bearer=served.token, transport=recording)
        try:
            history = [ContextBlock(role='user', content='ask')]
            asked = await model.create(history)
            assert asked.trust is TrustLevel.UNTRUSTED
            assert '选哪个？' in str(asked.content)
            history += [asked, ContextBlock(role='user', content='红')]
            answered = await model.create(history)
            assert answered.content == 'picked red'
            history += [answered, ContextBlock(role='user', content='more')]
            third = await model.create(history)
            assert third.content == 'third turn'
            assert third.trust is TrustLevel.UNTRUSTED
        finally:
            await recording.aclose()
        [sid] = served.live.sessions
        messages = [post['params']['message'] for post in recording.sent]
        assert 'contextId' not in messages[0]
        assert [message.get('contextId') for message in messages[1:]] == [sid, sid]
        task = recording.received[0]['result']['task']
        assert task['contextId'] == sid
        assert messages[1]['taskId'] == task['id']
        assert 'taskId' not in messages[2]

    _run(tmp_path, llm, body)
    users = [str(block.content) for block in llm.calls[2] if block.role == 'user']
    assert any('ask' in text for text in users) and any('more' in text for text in users)


_ID_PREFIX = {
    'id': 'TASK',
    'taskId': 'TASK',
    'contextId': 'CONTEXT',
    'artifactId': 'ARTIFACT',
    'messageId': 'MESSAGE',
}


def _normalized(value: Any, names: dict[str, str], key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: _normalized(v, names, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalized(item, names) for item in value]
    if isinstance(value, str):
        prefix = _ID_PREFIX.get(key or '')
        if prefix is not None:
            if value not in names:
                names[value] = f'{prefix}-{sum(n.startswith(prefix) for n in names.values()) + 1}'
            return names[value]
        return re.sub(r'127\.0\.0\.1:\d+', '127.0.0.1:PORT', value)
    return value


def _line(value: Any, names: dict[str, str]) -> str:
    return json.dumps(_normalized(value, names), ensure_ascii=False, sort_keys=True)


def test_one_streamed_session_turn_matches_the_golden_wire(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [
            _delegate('ROOT-PLAN'),
            ScriptedTurn(content='CHILD-ANSWER'),
            ScriptedTurn(content='ROOT-FINAL'),
        ]
    )
    request = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'SendStreamingMessage',
        'params': {
            'message': {
                'messageId': 'm-golden',
                'role': 'ROLE_USER',
                'parts': [{'text': 'go'}],
                'metadata': {'syndica.role': 'Orchestrator'},
            }
        },
    }
    lines: list[str] = []

    async def body(served: _Served) -> None:
        names: dict[str, str] = {}
        headers = {'Authorization': f'Bearer {served.token}', 'A2A-Version': '1.0'}
        events: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=10) as http:
            async with http.stream('POST', served.rpc, json=request, headers=headers) as response:
                assert response.status_code == 200
                assert response.headers['content-type'].startswith('text/event-stream')
                async for line in response.aiter_lines():
                    if line.startswith('data:'):
                        events.append(json.loads(line[5:]))
            task_id = events[0]['result']['task']['id']
            fetch = {'jsonrpc': '2.0', 'id': 2, 'method': 'GetTask', 'params': {'id': task_id}}
            fetched = await http.post(served.rpc, json=fetch, headers=headers)
        lines.append(_line(request, names))
        lines.extend(_line(event, names) for event in events)
        lines.append(_line(fetch, names))
        lines.append(_line(fetched.json(), names))

    _run(tmp_path, llm, body)
    assert '\n'.join(lines) + '\n' == _GOLDEN.read_text(encoding='utf-8')
