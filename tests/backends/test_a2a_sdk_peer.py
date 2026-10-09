"""Syndica 的 A2A 客户端对一个用 a2a SDK 写的远端 Agent。提问停在同一个任务上。"""

from __future__ import annotations

import asyncio
import socket

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


async def test_syndica_answers_an_sdk_agent_on_the_same_task() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    sock.listen(16)
    port = int(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(_app(port), log_level='warning', lifespan='on'))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(1000):
        if server.started:
            break
        await asyncio.sleep(0.01)
    model = A2AAgentModel('remote', f'http://127.0.0.1:{port}/a2a/')
    try:
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
    finally:
        await model.aclose()
        server.should_exit = True
        await asyncio.wait_for(task, 10)
