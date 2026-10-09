"""A2A：公开卡片、Bearer、缺版本头、1.0 与 0.3 发消息、异常不回显、SDK 客户端。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from a2a.client import ClientConfig, create_client
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Role,
    SendMessageRequest,
    TaskState,
)
from a2a.utils.constants import VERSION_HEADER
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct
from server_fakes import FakeAssembly, FakeHandle, running

from syndica.backends.a2a import A2AAgentModel
from syndica.kernel import ContextBlock, TrustLevel
from syndica.kernel.redaction import RuleRedactor
from syndica.server import Live, ProtocolMount
from syndica.server.a2a_serve import (
    A2ADesk,
    SyndicaA2A,
    build_agent_card,
    card_app,
    jsonrpc_app,
)

_LEAK = 'Bearer sk-live-quizzical0123456789 postgres://quizuser:quizpw@db.internal/x'


def _mounts(port: int) -> tuple[ProtocolMount, ...]:
    seen: list[tuple[str, str]] = []

    async def runner(text: str, role: str) -> str:
        seen.append((text, role))
        if 'boom' in text:
            raise RuntimeError('sk-a2a-secret')
        return f'{role}:{text}'

    card = build_agent_card('workspace', (('Worker', 'does work'),), port=port)
    handler = SyndicaA2A(runner, card=card, redactor=RuleRedactor(secrets=['sk-a2a-secret']))
    return (
        ProtocolMount('/.well-known/agent-card.json', card_app(card)),
        ProtocolMount('/a2a/', jsonrpc_app(handler)),
    )


def _send(text: str, *, role: str | None = None) -> dict[str, object]:
    message: dict[str, object] = {
        'messageId': 'm1',
        'role': 'ROLE_USER',
        'parts': [{'text': text}],
    }
    if role is not None:
        message['metadata'] = {'syndica.role': role}
    return {
        'jsonrpc': '2.0',
        'id': 7,
        'method': 'SendMessage',
        'params': {'message': message},
    }


async def test_the_card_is_public_and_rpc_needs_a_token_and_a_version() -> None:
    async with running(FakeAssembly(), mount_factory=_mounts) as server:
        async with httpx.AsyncClient(base_url=server.base, timeout=10) as client:
            card = await client.get('/.well-known/agent-card.json')
            assert card.status_code == 200
            body = card.json()
            assert body['name'] == 'workspace'
            assert body['capabilities']['streaming'] is True
            versions = {item['protocolVersion'] for item in body['supportedInterfaces']}
            assert versions == {'1.0', '0.3'}
            assert body['skills'][0]['id'] == 'Worker'
            assert 'bearer' in body['securitySchemes']

            missing = await client.post('/a2a/', json=_send('hi'))
            assert missing.status_code == 401

            bare = await client.post(
                '/a2a/', json=_send('hi'), headers={'Authorization': f'Bearer {server.token}'}
            )
            assert bare.status_code == 200
            assert bare.json()['error']['code'] == -32022
            assert 'A2A-Version' in bare.json()['error']['message']
            assert 'sk-a2a-secret' not in bare.text


async def test_version_1_returns_the_answer_and_hides_executor_exceptions() -> None:
    async with running(FakeAssembly(), mount_factory=_mounts) as server:
        headers = {
            'Authorization': f'Bearer {server.token}',
            'A2A-Version': '1.0',
        }
        async with httpx.AsyncClient(base_url=server.base, timeout=10) as client:
            ok = await client.post('/a2a/', json=_send('hello', role='Worker'), headers=headers)
            assert ok.status_code == 200
            result = ok.json()['result']
            task = result.get('task', result)
            assert task['status']['state'] == 'TASK_STATE_COMPLETED'
            text = task['status']['message']['parts'][0]['text']
            assert text == 'Worker:hello'
            task_id = task['id']

            fetched = await client.post(
                '/a2a/',
                headers=headers,
                json={'jsonrpc': '2.0', 'id': 8, 'method': 'GetTask', 'params': {'id': task_id}},
            )
            assert fetched.json()['result']['id'] == task_id

            failed = await client.post('/a2a/', json=_send('boom'), headers=headers)
            failed_task = failed.json()['result'].get('task', failed.json()['result'])
            assert failed_task['status']['state'] == 'TASK_STATE_FAILED'
            assert 'sk-a2a-secret' not in failed.text
            assert 'RuntimeError' in failed.text

            canceled = await client.post(
                '/a2a/',
                headers=headers,
                json={
                    'jsonrpc': '2.0',
                    'id': 9,
                    'method': 'CancelTask',
                    'params': {'id': task_id},
                },
            )
            assert canceled.json()['result']['status']['state'] == 'TASK_STATE_CANCELED'


async def test_version_0_3_message_send_reaches_the_same_runner() -> None:
    async with running(FakeAssembly(), mount_factory=_mounts) as server:
        headers = {
            'Authorization': f'Bearer {server.token}',
            'A2A-Version': '0.3',
        }
        payload = {
            'jsonrpc': '2.0',
            'id': 3,
            'method': 'message/send',
            'params': {
                'message': {
                    'messageId': 'm3',
                    'role': 'user',
                    'kind': 'message',
                    'parts': [{'kind': 'text', 'text': 'ping'}],
                    'metadata': {'syndica.role': 'Explorer'},
                }
            },
        }
        async with httpx.AsyncClient(base_url=server.base, timeout=10) as client:
            response = await client.post('/a2a/', json=payload, headers=headers)
        assert response.status_code == 200, response.text
        assert 'Explorer:ping' in response.text
        assert 'sk-a2a-secret' not in response.text


class _RaisingAssembly(FakeAssembly):
    async def open(self, **options: object) -> FakeHandle:
        handle = await super().open(**options)
        handle.deliver_error = RuntimeError(_LEAK)
        return handle


@pytest.mark.parametrize('method', ['SendMessage', 'SendStreamingMessage'])
async def test_a_desk_exception_is_a_failed_task_that_names_only_its_type(method: str) -> None:
    live = Live(_RaisingAssembly())
    card = build_agent_card('workspace', (('Worker', 'does work'),), port=8080)
    app = jsonrpc_app(SyndicaA2A(card=card, desk=A2ADesk(live)))
    payload = {**_send('hello', role='Worker'), 'method': method}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://a2a', timeout=10
        ) as client:
            response = await client.post('/', json=payload, headers={'A2A-Version': '1.0'})
    finally:
        await live.shutdown()
    assert response.status_code == 200
    assert 'quiz' not in response.text
    assert '-32603' not in response.text
    assert 'TASK_STATE_FAILED' in response.text
    assert '执行器抛出 RuntimeError' in response.text


class _CancelRaises:
    async def cancel(self, task_id: str) -> object:
        del task_id
        raise RuntimeError(_LEAK)


async def test_a_cancel_that_raises_names_only_the_exception_type() -> None:
    card = build_agent_card('workspace', (('Worker', 'does work'),), port=8080)
    handler = SyndicaA2A(card=card, desk=_CancelRaises())  # pyright: ignore[reportArgumentType]
    payload = {'jsonrpc': '2.0', 'id': 9, 'method': 'CancelTask', 'params': {'id': 't1'}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=jsonrpc_app(handler)), base_url='http://a2a', timeout=10
    ) as client:
        response = await client.post('/', json=payload, headers={'A2A-Version': '1.0'})
    assert response.status_code == 200
    assert 'quiz' not in response.text
    assert '中止根 Agent 时抛出 RuntimeError' in response.text


async def test_the_client_reads_the_card_and_gets_an_untrusted_answer() -> None:
    async with running(FakeAssembly(), mount_factory=_mounts) as server:
        model = A2AAgentModel('workspace', f'{server.base}/a2a/', bearer=server.token)
        try:
            reply = await model.create([ContextBlock(role='user', content='hello')])
            assert reply.trust is TrustLevel.UNTRUSTED
            assert reply.content == 'Orchestrator:hello'
        finally:
            await model.aclose()


_SDK_GOLDEN = Path(__file__).parent / 'testdata' / 'a2a_sdk_task.json'


def _sdk_request(text: str, role: str) -> SendMessageRequest:
    message = Message(message_id='m-sdk', role=Role.ROLE_USER)
    message.parts.add(text=text)
    metadata = Struct()
    metadata.update({'syndica.role': role})
    message.metadata.CopyFrom(metadata)
    return SendMessageRequest(message=message)


def _normalize_task(task: object) -> dict[str, object]:
    raw = MessageToDict(task)  # pyright: ignore[reportArgumentType]
    raw['id'] = 'TASK'
    raw['contextId'] = 'CONTEXT'
    artifacts = raw.get('artifacts')
    if isinstance(artifacts, list):
        for item in artifacts:
            if isinstance(item, dict):
                item['artifactId'] = 'ART'
    return raw


async def test_the_sdk_client_streams_fetches_and_cancels() -> None:
    async with running(FakeAssembly(), mount_factory=_mounts) as server:
        http = httpx.AsyncClient(
            headers={'Authorization': f'Bearer {server.token}'},
            timeout=10,
        )
        client = await create_client(server.base, ClientConfig(streaming=True, httpx_client=http))
        try:
            texts: list[str] = []
            task_id = ''
            async with asyncio.timeout(10):
                async for event in client.send_message(_sdk_request('hello', 'Worker')):
                    kind = event.WhichOneof('payload')
                    if kind == 'task':
                        task_id = event.task.id
                        texts.append(event.task.status.message.parts[0].text)
                    elif kind == 'status_update':
                        task_id = event.status_update.task_id
                        if event.status_update.status.message.parts:
                            texts.append(event.status_update.status.message.parts[0].text)
            assert texts == ['Worker:hello']
            assert task_id
            fetched = await client.get_task(GetTaskRequest(id=task_id))
            assert fetched.status.message.parts[0].text == 'Worker:hello'
            normalized = json.dumps(
                _normalize_task(fetched), ensure_ascii=False, sort_keys=True, indent=2
            )
            assert normalized + '\n' == _SDK_GOLDEN.read_text(encoding='utf-8')
            canceled = await client.cancel_task(CancelTaskRequest(id=task_id))
            assert canceled.status.state == TaskState.TASK_STATE_CANCELED
        finally:
            await client.close()
            await http.aclose()


async def test_the_sdk_client_hides_executor_exceptions_and_rejects_a_blank_version() -> None:
    async with running(FakeAssembly(), mount_factory=_mounts) as server:
        http = httpx.AsyncClient(
            headers={'Authorization': f'Bearer {server.token}'},
            timeout=10,
        )
        client = await create_client(server.base, ClientConfig(streaming=False, httpx_client=http))
        try:
            failed = [event async for event in client.send_message(_sdk_request('boom', 'Worker'))]
            blob = MessageToDict(failed[0])
            text = json.dumps(blob)
            assert 'RuntimeError' in text
            assert 'sk-a2a-secret' not in text
        finally:
            await client.close()
            await http.aclose()

        denied = httpx.AsyncClient(
            headers={'Authorization': 'Bearer wrong-token', VERSION_HEADER: '1.0'},
            timeout=10,
        )
        try:
            rejected = await create_client(server.base, ClientConfig(httpx_client=denied))
            with pytest.raises(Exception, match='401'):
                async for _event in rejected.send_message(_sdk_request('hi', 'Worker')):
                    pass
            await rejected.close()
        finally:
            await denied.aclose()

        blank = httpx.AsyncClient(
            headers={
                'Authorization': f'Bearer {server.token}',
                VERSION_HEADER: '',
            },
            timeout=10,
        )
        try:
            bare = await create_client(
                server.base, ClientConfig(streaming=False, httpx_client=blank)
            )
            with pytest.raises(Exception, match='A2A-Version'):
                async for _event in bare.send_message(_sdk_request('hi', 'Worker')):
                    pass
            await bare.close()
        finally:
            await blank.aclose()
