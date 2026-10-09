"""Syndica 的 A2A 客户端对 a2a SDK 写的远端 Agent：提问停在同一个任务上，线上往返有黄金文件。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import socket
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import uvicorn
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers.default_request_handler_v2 import DefaultRequestHandlerV2
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import AgentCard, Message, Role, Task, TaskState
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from syndica.backends.a2a import A2AAgentModel
from syndica.kernel import ContextBlock, TrustLevel


class _Asks(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = context.context_id
        if not task_id or not context_id:
            raise RuntimeError('sdk did not assign a task id')
        opened = Task(id=task_id, context_id=context_id)
        opened.status.state = TaskState.TASK_STATE_SUBMITTED
        await event_queue.enqueue_event(opened)
        updater = TaskUpdater(event_queue, task_id, context_id)
        message = Message(message_id='reply', role=Role.ROLE_AGENT)
        text = context.get_user_input().strip()
        if text == '红':
            message.parts.add(text='picked red')
            await updater.complete(message)
            return
        message.parts.add(text='选哪个？\n1. 红')
        await updater.requires_input(message)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ''
        context_id = context.context_id or ''
        opened = Task(id=task_id, context_id=context_id)
        opened.status.state = TaskState.TASK_STATE_SUBMITTED
        await event_queue.enqueue_event(opened)
        await TaskUpdater(event_queue, task_id, context_id).cancel()


def _card(port: int) -> AgentCard:
    url = f'http://127.0.0.1:{port}/a2a/'
    card = AgentCard(
        name='remote',
        description='sdk peer',
        version='1',
        default_input_modes=['text/plain'],
        default_output_modes=['text/plain'],
    )
    card.supported_interfaces.add(url=url, protocol_binding='JSONRPC', protocol_version='1.0')
    return card


def _app(port: int) -> Starlette:
    card = _card(port)
    handler = DefaultRequestHandlerV2(_Asks(), InMemoryTaskStore(), card)

    def card_endpoint(_: object) -> JSONResponse:
        return JSONResponse(MessageToDict(card))

    return Starlette(
        routes=[
            Route('/.well-known/agent-card.json', card_endpoint),
            *create_jsonrpc_routes(handler, '/a2a/'),
        ]
    )


class _Wire:
    """ASGI 外层：逐个请求记下方法、路径、A2A 相关的头与两边的 JSON。"""

    def __init__(self, app: Starlette) -> None:
        self.app = app
        self.exchanges: list[dict[str, Any]] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        inbound = bytearray()
        outbound = bytearray()
        status: list[int] = []

        async def reading() -> Any:
            message = await receive()
            if message['type'] == 'http.request':
                inbound.extend(message.get('body', b''))
            return message

        async def writing(message: Any) -> None:
            if message['type'] == 'http.response.start':
                status.append(message['status'])
            elif message['type'] == 'http.response.body':
                outbound.extend(message.get('body', b''))
            await send(message)

        await self.app(scope, reading, writing)
        headers = {key.decode(): value.decode() for key, value in scope['headers']}
        shown = {key: headers[key] for key in ('a2a-version', 'content-type') if key in headers}
        if headers.get('authorization', '').startswith('Bearer '):
            shown['authorization'] = 'Bearer TOKEN'
        self.exchanges.append(
            {
                'method': scope['method'],
                'path': scope['path'],
                'headers': shown,
                'body': json.loads(inbound) if inbound else None,
            }
        )
        self.exchanges.append({'status': status[0], 'body': json.loads(outbound)})


@contextlib.asynccontextmanager
async def _serving(wrap: Callable[[Starlette], Any] = lambda app: app) -> AsyncIterator[int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    sock.listen(16)
    port = int(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(wrap(_app(port)), log_level='warning', lifespan='on'))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(1000):
        if server.started:
            break
        await asyncio.sleep(0.01)
    try:
        yield port
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


async def _ask_then_answer(model: A2AAgentModel) -> None:
    asked = await model.create([ContextBlock(role='user', content='ask')])
    assert asked.trust is TrustLevel.UNTRUSTED
    assert asked.content == '选哪个？\n1. 红'
    answered = await model.create(
        [
            ContextBlock(role='user', content='ask'),
            ContextBlock(role='assistant', content='选哪个？\n1. 红'),
            ContextBlock(role='user', content='红'),
        ]
    )
    assert answered.trust is TrustLevel.UNTRUSTED
    assert answered.content == 'picked red'


async def test_syndica_answers_an_sdk_agent_on_the_same_task() -> None:
    async with _serving() as port:
        model = A2AAgentModel('remote', f'http://127.0.0.1:{port}/a2a/')
        try:
            await _ask_then_answer(model)
        finally:
            await model.aclose()


_GOLDEN = Path(__file__).parent / 'testdata' / 'a2a_client_wire.jsonl'
_ID_PREFIX = {
    'id': 'TASK',
    'taskId': 'TASK',
    'contextId': 'CONTEXT',
    'messageId': 'MESSAGE',
    'artifactId': 'ARTIFACT',
}


def _normalized(value: Any, names: dict[str, str], key: str = '') -> Any:
    if isinstance(value, dict):
        return {k: _normalized(v, names, k) for k, v in cast(dict[str, Any], value).items()}
    if isinstance(value, list):
        return [_normalized(item, names) for item in cast(list[Any], value)]
    if isinstance(value, str):
        if key == 'timestamp':
            return 'TIMESTAMP'
        prefix = _ID_PREFIX.get(key)
        if prefix is not None:
            if value not in names:
                count = sum(name.startswith(prefix) for name in names.values())
                names[value] = f'{prefix}-{count + 1}'
            return names[value]
        return re.sub(r'127\.0\.0\.1:\d+', '127.0.0.1:PORT', value)
    return value


async def test_the_client_wire_round_trip_matches_the_golden() -> None:
    wire: list[_Wire] = []

    def wrap(app: Starlette) -> _Wire:
        wire.append(_Wire(app))
        return wire[0]

    async with _serving(wrap) as port:
        model = A2AAgentModel('remote', f'http://127.0.0.1:{port}/a2a/', bearer='machine-token')
        try:
            await _ask_then_answer(model)
        finally:
            await model.aclose()
    names: dict[str, str] = {}
    lines = [
        json.dumps(_normalized(exchange, names), ensure_ascii=False, sort_keys=True)
        for exchange in wire[0].exchanges
    ]
    assert '\n'.join(lines) + '\n' == _GOLDEN.read_text(encoding='utf-8')
