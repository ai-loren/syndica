"""远端 A2A：提问记着任务，下一次调用把答复送回去，回答不可信。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from syndica.backends.a2a.model import A2AAgentModel
from syndica.kernel import (
    AgentCatalog,
    BlockHighRisk,
    Budget,
    BudgetPolicy,
    ConfigError,
    ContextBlock,
    DangerJudge,
    HostFacts,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    ToolCall,
    TrustLevel,
    local_tools,
    register_builtins,
)
from syndica.kernel.call_limit import BudgetDeadline, arm_model_call_limit, reset_model_call_limit
from syndica.kernel.file_taint import FileTaint
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.workspace_router import WorkspaceRouter

_CARD = {
    'name': 'remote',
    'supportedInterfaces': [{'protocolVersion': '1.0', 'protocolBinding': 'JSONRPC'}],
}


class _Script:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.posts: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.card_headers: list[dict[str, str]] = []

    async def get_json(self, url: str, headers: Mapping[str, str], deadline: float | None) -> Any:
        del deadline
        assert url.endswith('/.well-known/agent-card.json')
        self.card_headers.append(dict(headers))
        return _CARD

    async def post_json(
        self,
        url: str,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
        deadline: float | None,
    ) -> Any:
        del url, deadline
        self.posts.append(dict(body))
        self.headers.append(dict(headers))
        if body['method'] == 'CancelTask':
            return {
                'jsonrpc': '2.0',
                'id': body['id'],
                'result': {'id': 't', 'status': {'state': 'TASK_STATE_CANCELED'}},
            }
        if not self.replies:
            raise AssertionError('no scripted reply')
        return self.replies.pop(0)

    async def aclose(self) -> None:
        return None


def _task(
    state: str, text: str, *, task_id: str = 'task-1', context_id: str = 'ctx-1'
) -> dict[str, Any]:
    return {
        'jsonrpc': '2.0',
        'id': 1,
        'result': {
            'task': {
                'id': task_id,
                'contextId': context_id,
                'status': {'state': state, 'message': {'parts': [{'text': text}]}},
            }
        },
    }


async def test_input_required_is_answered_on_the_same_task() -> None:
    script = _Script(
        [
            _task('TASK_STATE_INPUT_REQUIRED', '选哪个？\n1. 红'),
            _task('TASK_STATE_COMPLETED', 'picked red', task_id='task-1'),
        ]
    )
    model = A2AAgentModel(
        'remote',
        'http://127.0.0.1:9/a2a/',
        bearer='machine-token',
        transport=script,
        redactor=RuleRedactor(secrets=['sk-a2a-secret']),
    )
    try:
        asked = await model.create([ContextBlock(role='user', content='ask')])
        assert asked.trust is TrustLevel.UNTRUSTED
        assert asked.content == '选哪个？\n1. 红'
        answered = await model.create(
            [
                ContextBlock(role='user', content='ask'),
                ContextBlock(role='assistant', content='选哪个？'),
                ContextBlock(role='user', content='红'),
            ]
        )
        assert answered.trust is TrustLevel.UNTRUSTED
        assert answered.content == 'picked red'
        second = script.posts[1]['params']['message']
        assert second['taskId'] == 'task-1'
        assert second['contextId'] == 'ctx-1'
        assert second['parts'] == [{'text': '红'}]
        assert [headers['A2A-Version'] for headers in script.headers] == ['1.0', '1.0']
        assert [headers['Authorization'] for headers in script.headers] == [
            'Bearer machine-token',
            'Bearer machine-token',
        ]
    finally:
        await model.aclose()


async def test_the_public_agent_card_is_fetched_without_the_bearer() -> None:
    script = _Script([_task('TASK_STATE_COMPLETED', 'done')])
    model = A2AAgentModel(
        'remote',
        'http://127.0.0.1:9/a2a/',
        bearer='machine-token',
        transport=script,
        redactor=RuleRedactor(secrets=[]),
    )
    try:
        await model.create([ContextBlock(role='user', content='go')])
        assert script.card_headers == [{'A2A-Version': '1.0'}]
        assert script.headers[0]['Authorization'] == 'Bearer machine-token'
    finally:
        await model.aclose()


async def test_a_failed_task_becomes_a_three_part_error_without_the_secret() -> None:
    script = _Script([_task('TASK_STATE_FAILED', '执行器抛出 RuntimeError，sk-a2a-secret')])
    model = A2AAgentModel(
        'remote',
        'http://127.0.0.1:9/a2a/',
        transport=script,
        redactor=RuleRedactor(secrets=['sk-a2a-secret']),
    )
    try:
        with pytest.raises(ConfigError, match='任务完成') as caught:
            await model.create([ContextBlock(role='user', content='boom')])
        text = f'{caught.value.expected} {caught.value.received} {caught.value.next_action}'
        assert 'sk-a2a-secret' not in text
        assert '不会自动重试' in caught.value.next_action
    finally:
        await model.aclose()


async def test_a_timeout_cancels_the_open_task() -> None:
    class _ThenSlow(_Script):
        async def post_json(
            self,
            url: str,
            body: Mapping[str, Any],
            headers: Mapping[str, str],
            deadline: float | None,
        ) -> Any:
            self.posts.append(dict(body))
            if body['method'] == 'CancelTask':
                return {'jsonrpc': '2.0', 'id': body['id'], 'result': {}}
            sent = [item for item in self.posts if item['method'] == 'SendMessage']
            if len(sent) == 1:
                return _task('TASK_STATE_INPUT_REQUIRED', 'wait')
            raise httpx.TimeoutException('slow')

    script = _ThenSlow([])
    model = A2AAgentModel('remote', 'http://127.0.0.1:9/a2a/', transport=script)
    token = arm_model_call_limit(1.0)
    try:
        asked = await model.create([ContextBlock(role='user', content='ask')])
        assert asked.content == 'wait'
        with pytest.raises(BudgetDeadline):
            await model.create(
                [
                    ContextBlock(role='user', content='ask'),
                    ContextBlock(role='user', content='go on'),
                ]
            )
        assert script.posts[-1]['method'] == 'CancelTask'
        assert script.posts[-1]['params'] == {'id': 'task-1'}
    finally:
        reset_model_call_limit(token)
        await model.aclose()


class _CapturePeer:
    """真 HTTP 上的远端。`script` 逐条答 SendMessage（None：收尾前不答），记下每个 CancelTask。"""

    def __init__(self, script: list[str | None], *, cancel_hangs: bool = False) -> None:
        self.script = list(script)
        self.cancel_hangs = cancel_hangs
        self.sends: list[dict[str, Any]] = []
        self.cancels: list[tuple[dict[str, Any], dict[str, str | None]]] = []
        self.hanging = asyncio.Event()
        self.done = asyncio.Event()
        self.url = ''

    async def _card(self, request: Request) -> JSONResponse:
        del request
        return JSONResponse(_CARD)

    async def _rpc(self, request: Request) -> JSONResponse:
        body = await request.json()
        if body['method'] == 'CancelTask':
            seen = {key: request.headers.get(key) for key in ('a2a-version', 'authorization')}
            self.cancels.append((body['params'], seen))
            if self.cancel_hangs:
                await self.done.wait()
            canceled = {'id': body['params']['id'], 'status': {'state': 'TASK_STATE_CANCELED'}}
            return JSONResponse({'jsonrpc': '2.0', 'id': body['id'], 'result': canceled})
        self.sends.append(body['params']['message'])
        state = self.script.pop(0)
        if state is None:
            self.hanging.set()
            await self.done.wait()
            state = 'TASK_STATE_COMPLETED'
        return JSONResponse(_task(state, f'{state} answer'))

    @contextlib.asynccontextmanager
    async def serving(self) -> AsyncIterator[_CapturePeer]:
        app = Starlette(
            routes=[
                Route('/.well-known/agent-card.json', self._card),
                Route('/a2a/', self._rpc, methods=['POST']),
            ]
        )
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        sock.listen(16)
        self.url = f'http://127.0.0.1:{sock.getsockname()[1]}/a2a/'
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', lifespan='off'))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        for _ in range(1000):
            if server.started:
                break
            await asyncio.sleep(0.01)
        try:
            yield self
        finally:
            self.done.set()
            server.should_exit = True
            await asyncio.wait_for(task, 10)


async def test_a_cancelled_call_cancels_the_open_remote_task_and_still_raises_cancelled() -> None:
    peer = _CapturePeer(['TASK_STATE_INPUT_REQUIRED', None, 'TASK_STATE_COMPLETED'])
    async with peer.serving():
        model = A2AAgentModel('remote', peer.url, bearer='machine-token', timeout=5.0)
        try:
            asked = await model.create([ContextBlock(role='user', content='ask')])
            assert asked.content == 'TASK_STATE_INPUT_REQUIRED answer'
            call = asyncio.create_task(
                model.create(
                    [
                        ContextBlock(role='user', content='ask'),
                        ContextBlock(role='user', content='go on'),
                    ]
                )
            )
            await asyncio.wait_for(peer.hanging.wait(), 5)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            assert peer.cancels == [
                ({'id': 'task-1'}, {'a2a-version': '1.0', 'authorization': 'Bearer machine-token'})
            ]
            fresh = await model.create([ContextBlock(role='user', content='again')])
            assert fresh.content == 'TASK_STATE_COMPLETED answer'
        finally:
            await model.aclose()
    assert peer.sends[1]['taskId'] == 'task-1'
    assert 'taskId' not in peer.sends[2]


async def test_the_cancel_task_sent_after_a_cancellation_is_bounded() -> None:
    peer = _CapturePeer(['TASK_STATE_INPUT_REQUIRED', None], cancel_hangs=True)
    async with peer.serving():
        model = A2AAgentModel('remote', peer.url, timeout=0.3)
        loop = asyncio.get_running_loop()
        try:
            await model.create([ContextBlock(role='user', content='ask')])
            call = asyncio.create_task(model.create([ContextBlock(role='user', content='go on')]))
            await asyncio.wait_for(peer.hanging.wait(), 5)
            started = loop.time()
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(call, 5)
            elapsed = loop.time() - started
        finally:
            await model.aclose()
    assert [params for params, _ in peer.cancels] == [{'id': 'task-1'}]
    assert elapsed < 2


async def test_a_cancelled_first_call_has_no_task_to_cancel_and_still_raises_cancelled() -> None:
    peer = _CapturePeer([None])
    async with peer.serving():
        model = A2AAgentModel('remote', peer.url, timeout=5.0)
        try:
            call = asyncio.create_task(model.create([ContextBlock(role='user', content='hi')]))
            await asyncio.wait_for(peer.hanging.wait(), 5)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        finally:
            await model.aclose()
    assert peer.cancels == []


async def test_a_peer_that_accepts_and_never_answers_times_out_at_the_config_timeout() -> None:
    accepted: list[asyncio.StreamWriter] = []

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        del reader
        accepted.append(writer)

    server = await asyncio.start_server(silent, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    url = f'http://127.0.0.1:{port}/a2a/'
    model = A2AAgentModel('remote', url, timeout=0.3)
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        with pytest.raises(ConfigError) as caught:
            await asyncio.wait_for(model.create([ContextBlock(role='user', content='hi')]), 5)
        elapsed = loop.time() - started
    finally:
        await model.aclose()
        for writer in accepted:
            writer.close()
        server.close()
        await server.wait_closed()

    assert accepted
    assert elapsed < 3
    assert '0.3 秒内答复' in caught.value.expected
    assert url in caught.value.received
    assert url in caught.value.next_action
    assert '不会自动重试' in caught.value.next_action


async def test_without_a_budget_every_request_and_the_cancel_carry_the_config_timeout() -> None:
    class _ThenSlow(_Script):
        def __init__(self) -> None:
            super().__init__([])
            self.deadlines: list[tuple[str, float | None]] = []

        async def get_json(
            self, url: str, headers: Mapping[str, str], deadline: float | None
        ) -> Any:
            self.deadlines.append(('card', deadline))
            return await super().get_json(url, headers, deadline)

        async def post_json(
            self,
            url: str,
            body: Mapping[str, Any],
            headers: Mapping[str, str],
            deadline: float | None,
        ) -> Any:
            self.deadlines.append((body['method'], deadline))
            self.posts.append(dict(body))
            if body['method'] == 'CancelTask':
                return {'jsonrpc': '2.0', 'id': body['id'], 'result': {}}
            sent = [item for item in self.posts if item['method'] == 'SendMessage']
            if len(sent) == 1:
                return _task('TASK_STATE_INPUT_REQUIRED', 'wait')
            raise httpx.TimeoutException('slow')

    script = _ThenSlow()
    model = A2AAgentModel('remote', 'http://127.0.0.1:9/a2a/', transport=script, timeout=7.0)
    try:
        await model.create([ContextBlock(role='user', content='ask')])
        with pytest.raises(ConfigError, match='7 秒内答复'):
            await model.create(
                [
                    ContextBlock(role='user', content='ask'),
                    ContextBlock(role='user', content='go on'),
                ]
            )
    finally:
        await model.aclose()

    assert script.deadlines == [
        ('card', 7.0),
        ('SendMessage', 7.0),
        ('SendMessage', 7.0),
        ('CancelTask', 7.0),
    ]


async def test_a_card_that_declares_only_an_unknown_version_is_refused() -> None:
    class _Future(_Script):
        async def get_json(
            self, url: str, headers: Mapping[str, str], deadline: float | None
        ) -> Any:
            del url, headers, deadline
            return {
                'name': 'remote',
                'supportedInterfaces': [{'protocolVersion': '2.0', 'protocolBinding': 'JSONRPC'}],
            }

    script = _Future([_task('TASK_STATE_COMPLETED', 'should not be asked')])
    model = A2AAgentModel('remote', 'http://127.0.0.1:9/a2a/', transport=script)
    try:
        with pytest.raises(ConfigError) as caught:
            await model.create([ContextBlock(role='user', content='hi')])
    finally:
        await model.aclose()

    assert '1.0 或 0.3' in caught.value.expected
    assert "['2.0']" in caught.value.received
    assert '卡片里写上 1.0' in caught.value.next_action
    assert script.posts == []


async def test_a_remote_error_message_is_redacted_and_capped() -> None:
    secret = 'sk-a2a-remote-secret'
    script = _Script(
        [
            {
                'jsonrpc': '2.0',
                'id': 1,
                'error': {'code': -32603, 'message': f'quota denied {secret} ' + 'x' * 1000},
            }
        ]
    )
    model = A2AAgentModel(
        'remote',
        'http://127.0.0.1:9/a2a/',
        transport=script,
        redactor=RuleRedactor(secrets=[secret]),
    )
    try:
        with pytest.raises(ConfigError) as caught:
            await model.create([ContextBlock(role='user', content='hi')])
    finally:
        await model.aclose()

    assert 'quota denied' in caught.value.received
    assert secret not in caught.value.received
    assert len(caught.value.received) <= 300
    assert 'A2A-Version' in caught.value.next_action


async def test_a_remote_three_part_error_is_redacted_field_by_field() -> None:
    secret = 'sk-a2a-remote-secret'
    script = _Script(
        [
            {
                'jsonrpc': '2.0',
                'id': 1,
                'error': {
                    'code': -32602,
                    'message': 'invalid',
                    'data': {
                        'expected': f'expected {secret}',
                        'received': f'received {secret} ' + 'y' * 1000,
                        'next_action': f'next {secret}',
                    },
                },
            }
        ]
    )
    model = A2AAgentModel(
        'remote',
        'http://127.0.0.1:9/a2a/',
        transport=script,
        redactor=RuleRedactor(secrets=[secret]),
    )
    try:
        with pytest.raises(ConfigError) as caught:
            await model.create([ContextBlock(role='user', content='hi')])
    finally:
        await model.aclose()

    error = caught.value
    for field, anchor in (
        (error.expected, 'expected'),
        (error.received, 'received'),
        (error.next_action, 'next'),
    ):
        assert field.startswith(anchor)
        assert secret not in field
        assert len(field) <= 300


async def test_an_orchestrator_delegates_to_an_a2a_role_and_answers_it(tmp_path: Path) -> None:
    script = _Script(
        [
            _task('TASK_STATE_INPUT_REQUIRED', '选哪个？\n1. 红'),
            _task('TASK_STATE_COMPLETED', 'picked red'),
        ]
    )
    model = A2AAgentModel('remote', 'http://127.0.0.1:9/a2a/', transport=script)
    catalog = AgentCatalog()
    register_builtins(catalog)
    catalog.load_mapping(
        {
            'name': 'Worker',
            'description': 'remote a2a agent',
            'system_prompt': 'hosted',
            'model': 'a2a:remote',
            'tools': [],
        }
    )
    parent = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='d1',
                        name='Delegate',
                        arguments=json.dumps({'agent': 'Worker', 'query': 'ask'}),
                    )
                ]
            ),
            ScriptedTurn(content='parent saw the question'),
        ]
    )
    root = tmp_path / 'ws'
    root.mkdir()
    local = LocalWorkspace(str(root), shell='/bin/sh', env={'PATH': os.environ.get('PATH', '')})
    router = WorkspaceRouter(local, taint=FileTaint(root))
    factories = catalog.freeze(
        local_tools(router.portal()),
        {'default': parent, 'a2a:remote': model},
        HostFacts(os='darwin', platform='test', shell='/bin/sh'),
        danger_judge=DangerJudge(),
        danger_policy=BlockHighRisk(),
    )
    runtime = Runtime(
        agents=factories,
        orchestration=True,
        workspace=router,
        budget=BudgetPolicy(root=Budget(turns=8)),
    )
    try:
        agent_id = await runtime.spawn(factories['Orchestrator'], query='go')
        ctx = await asyncio.wait_for(runtime.wait(agent_id), 10)
        handed = next(block for block in ctx.current_traj if block.tool_call_id == 'd1')
        assert handed.trust is TrustLevel.UNTRUSTED
        assert isinstance(handed.content, str)
        assert '选哪个？' in handed.content
        child_id = handed.content.split('交回', 1)[0].removeprefix('agent ').strip()
        delivered = await runtime.send(child_id, '红')
        assert delivered.ok is True
        child = await asyncio.wait_for(runtime.wait(child_id), 10)
        answer = next(
            block
            for block in reversed(child.current_traj)
            if block.role == 'assistant' and block.content == 'picked red'
        )
        assert answer.trust is TrustLevel.UNTRUSTED
        sends = [post for post in script.posts if post['method'] == 'SendMessage']
        assert 'taskId' not in sends[0]['params']['message']
        assert sends[1]['params']['message']['taskId'] == 'task-1'
        assert sends[1]['params']['message']['contextId'] == 'ctx-1'
        assert sends[1]['params']['message']['parts'][0]['text'].startswith('红')
    finally:
        await model.aclose()
        await runtime.shutdown()
        await router.close()
