"""A2A JSON-RPC。鉴权仍由 `create_app` 做。

缺 `A2A-Version` 在交给 SDK 之前拒绝：SDK 会把缺头当成 0.3。只收 `1.0` 与 `0.3`。
执行器抛错时任务失败，原文不进响应。
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.server.routes.jsonrpc_dispatcher import JsonRpcDispatcher
from a2a.types.a2a_pb2 import (
    AgentCard,
    CancelTaskRequest,
    DeleteTaskPushNotificationConfigRequest,
    GetExtendedAgentCardRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    ListTaskPushNotificationConfigsRequest,
    ListTaskPushNotificationConfigsResponse,
    ListTasksRequest,
    ListTasksResponse,
    SendMessageRequest,
    Task,
    TaskPushNotificationConfig,
    TaskState,
    TaskStatusUpdateEvent,
)
from a2a.utils.errors import InternalError, TaskNotFoundError, UnsupportedOperationError
from google.protobuf.json_format import MessageToDict
from starlette.requests import Request
from starlette.responses import Response

from syndica.kernel import ConfigError, Redactor, SyndicaError
from syndica.kernel.redaction import RuleRedactor
from syndica.server.errors import ApiError
from syndica.server.live import Live, LiveSession
from syndica.server.outbound import json_response
from syndica.server.streams import Entry

__all__ = [
    'ACCEPTED_PROTOCOL_VERSIONS',
    'PromptRunner',
    'A2ADesk',
    'SyndicaA2A',
    'build_agent_card',
    'card_app',
    'jsonrpc_app',
]

logger = logging.getLogger(__name__)

ACCEPTED_PROTOCOL_VERSIONS = ('1.0', '0.3')
PromptRunner = Callable[[str, str], Awaitable[str]]
_VERSION_CODE = -32022


class SyndicaA2A(RequestHandler):
    """一次消息一个任务。`runner` 收到文本和角色，交回最终回答。"""

    def __init__(
        self,
        runner: PromptRunner | None = None,
        *,
        card: AgentCard,
        redactor: Redactor | None = None,
        desk: A2ADesk | None = None,
    ) -> None:
        if runner is None and desk is None:
            raise ConfigError(
                expected='SyndicaA2A 有 runner 或 desk',
                received='两个都没有',
                next_action='协议测试传 runner；挂上会话时传 A2ADesk',
            )
        self._runner = runner
        self._desk = desk
        self._card = card
        self._redactor = redactor if redactor is not None else RuleRedactor()
        self._tasks: dict[str, Task] = {}

    def redactor(self) -> Redactor:
        return self._redactor

    async def on_message_send(self, params: SendMessageRequest, context: object) -> Task:
        del context
        return await self._run(params)

    async def on_message_send_stream(
        self, params: SendMessageRequest, context: object
    ) -> AsyncGenerator[Task | TaskStatusUpdateEvent]:
        del context
        task = await self._run(params)
        yield TaskStatusUpdateEvent(
            task_id=task.id,
            context_id=task.context_id,
            status=task.status,
        )

    async def on_get_task(self, params: GetTaskRequest, context: object) -> Task | None:
        del context
        return self._tasks.get(params.id)

    async def on_cancel_task(self, params: CancelTaskRequest, context: object) -> Task | None:
        del context
        if self._desk is not None:
            try:
                task = await self._desk.cancel(params.id)
            except Exception as exc:
                # SDK 的分派器会把异常原文放进 -32603 交给对端；这里只留类型名。
                logger.exception('a2a cancel failed')
                raise InternalError(message=_cancel_failed(type(exc).__name__)) from None
            if task is None:
                raise TaskNotFoundError()
            self._tasks[params.id] = task
            return task
        task = self._tasks.get(params.id)
        if task is None:
            raise TaskNotFoundError()
        task.status.state = TaskState.TASK_STATE_CANCELED
        return task

    async def on_list_tasks(self, params: ListTasksRequest, context: object) -> ListTasksResponse:
        del params, context
        return ListTasksResponse()

    async def on_get_extended_agent_card(
        self, params: GetExtendedAgentCardRequest, context: object
    ) -> AgentCard:
        del params, context
        return self._card

    async def on_create_task_push_notification_config(
        self, params: TaskPushNotificationConfig, context: object
    ) -> TaskPushNotificationConfig:
        del params, context
        raise _no_push()

    async def on_get_task_push_notification_config(
        self, params: GetTaskPushNotificationConfigRequest, context: object
    ) -> TaskPushNotificationConfig:
        del params, context
        raise _no_push()

    async def on_list_task_push_notification_configs(
        self, params: ListTaskPushNotificationConfigsRequest, context: object
    ) -> ListTaskPushNotificationConfigsResponse:
        del params, context
        raise _no_push()

    async def on_delete_task_push_notification_config(
        self, params: DeleteTaskPushNotificationConfigRequest, context: object
    ) -> None:
        del params, context
        raise _no_push()

    async def on_subscribe_to_task(self, params: object, context: object) -> AsyncGenerator[Task]:
        del params, context
        raise _no_push()
        yield Task()

    async def _run(self, params: SendMessageRequest) -> Task:
        message = params.message
        text = '\n'.join(part.text for part in message.parts if part.text)
        role = _role(message)
        task_id = message.task_id or secrets.token_hex(8)
        context_id = message.context_id or secrets.token_hex(8)
        # 漏出这里的异常由 SDK 换成 -32603，`str(exc)` 原样进响应：两条路径都必须在这里收住。
        try:
            state, answer = await self._answer(context_id, task_id, text, role)
        except SyndicaError as exc:
            answer = f'期望: {exc.expected}。实际: {exc.received}。下一步: {exc.next_action}'
            state = TaskState.TASK_STATE_FAILED
        except Exception as exc:
            logger.error('a2a executor failed: %s', type(exc).__name__)
            answer = _executor_failed(type(exc).__name__)
            state = TaskState.TASK_STATE_FAILED
        return self._remember(task_id, context_id, state, answer)

    async def _answer(self, context_id: str, task_id: str, text: str, role: str) -> tuple[int, str]:
        if self._desk is not None:
            outcome = await self._desk.turn(
                context_id=context_id, task_id=task_id, text=text, role=role
            )
            return outcome.state, outcome.text
        assert self._runner is not None
        return TaskState.TASK_STATE_COMPLETED, await self._runner(text, role)

    def _remember(self, task_id: str, context_id: str, state: int, answer: str) -> Task:
        task = Task(id=task_id, context_id=context_id)
        task.status.state = cast(Any, state)
        task.status.message.parts.add(text=self._redactor.redact(answer))
        if state == TaskState.TASK_STATE_COMPLETED and answer:
            artifact = task.artifacts.add()
            artifact.artifact_id = secrets.token_hex(4)
            artifact.name = 'answer'
            artifact.parts.add(text=self._redactor.redact(answer))
        self._tasks[task_id] = task
        return task


def build_agent_card(
    name: str,
    skills: Sequence[tuple[str, str]],
    *,
    port: object,
) -> AgentCard:
    """名字是工作区目录名。技能是角色。接口同时声明 JSON-RPC 1.0 与 0.3。"""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        from syndica.kernel import ConfigError

        raise ConfigError(
            expected='A2A 卡片上的端口是 1 到 65535 的整数',
            received=f'收到 {port!r}',
            next_action='用 syndica up 正在听的那个端口；不要传 true',
        )
    url = f'http://127.0.0.1:{port}/a2a/'
    card = AgentCard(
        name=name or 'syndica',
        description='Syndica workspace agent',
        version='1',
        default_input_modes=['text/plain'],
        default_output_modes=['text/plain'],
    )
    card.capabilities.streaming = True
    for version in ACCEPTED_PROTOCOL_VERSIONS:
        card.supported_interfaces.add(url=url, protocol_binding='JSONRPC', protocol_version=version)
    scheme = card.security_schemes['bearer']
    scheme.http_auth_security_scheme.scheme = 'bearer'
    scheme.http_auth_security_scheme.bearer_format = 'Syndica'
    card.security_requirements.add().schemes['bearer']
    for skill_id, description in skills:
        skill = card.skills.add()
        skill.id = skill_id
        skill.name = skill_id
        skill.description = description
    return card


def card_app(card: AgentCard, redactor: Redactor | None = None) -> Callable[..., Any]:
    body = MessageToDict(card)
    active = redactor if redactor is not None else RuleRedactor()

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        response = json_response(body, active)
        await response(scope, receive, send)

    return app


def jsonrpc_app(handler: SyndicaA2A) -> Callable[..., Any]:
    dispatcher = JsonRpcDispatcher(handler, enable_v0_3_compat=True)

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        request = Request(scope, receive)
        version = request.headers.get('a2a-version')
        if not _accepted(version):
            response = _version_error(await _rpc_id(request), version, handler.redactor())
            await response(scope, receive, send)
            return
        response = await dispatcher.handle_requests(request)
        await response(scope, receive, send)

    return app


def _accepted(version: str | None) -> bool:
    if version is None or not version.strip():
        return False
    if version in ACCEPTED_PROTOCOL_VERSIONS:
        return True
    parts = version.split('.')
    if len(parts) < 2 or not all(part.isdigit() for part in parts[:2]):
        return False
    return (int(parts[0]), int(parts[1])) in {(1, 0), (0, 3)}


def _version_error(
    request_id: str | int | None, version: str | None, redactor: Redactor
) -> Response:
    received = '没有 A2A-Version 头' if not version else f'A2A-Version={version!r}'
    expected = 'A2A-Version 是 1.0 或 0.3'
    next_action = '带上 A2A-Version: 1.0 或 0.3；缺头不会按 0.3 收下'
    return json_response(
        {
            'jsonrpc': '2.0',
            'id': request_id,
            'error': {
                'code': _VERSION_CODE,
                'message': f'期望: {expected}。实际: {received}。下一步: {next_action}',
                'data': {
                    'expected': expected,
                    'received': received,
                    'next_action': next_action,
                    'supported': list(ACCEPTED_PROTOCOL_VERSIONS),
                },
            },
        },
        redactor,
    )


async def _rpc_id(request: Request) -> str | int | None:
    try:
        body = await request.json()
    except Exception:
        return None
    parsed = cast(object, body)
    if not isinstance(parsed, dict):
        return None
    value = cast(dict[str, object], parsed).get('id')
    if type(value) is int or isinstance(value, str):
        return value
    return None


def _role(message: Any) -> str:
    try:
        raw = cast(dict[str, object], MessageToDict(message.metadata))
    except Exception:
        return 'Orchestrator'
    role = raw.get('syndica.role')
    if isinstance(role, str) and role.strip():
        return role.strip()
    return 'Orchestrator'


@dataclass(frozen=True)
class _Outcome:
    state: int
    text: str


class A2ADesk:
    """`contextId` 对上一个打开的会话。一次消息一轮。提问停成 INPUT_REQUIRED。"""

    def __init__(self, live: Live, *, redactor: Redactor | None = None) -> None:
        self._live = live
        self._redactor = redactor if redactor is not None else RuleRedactor()
        self._rooms: dict[str, LiveSession] = {}
        self._pending: dict[str, tuple[str, str, tuple[str, ...]]] = {}
        self._task_room: dict[str, str] = {}

    async def turn(self, *, context_id: str, task_id: str, text: str, role: str) -> _Outcome:
        try:
            session = await self._session(context_id, role)
        except SyndicaError as exc:
            return _Outcome(
                TaskState.TASK_STATE_FAILED,
                f'期望: {exc.expected}。实际: {exc.received}。下一步: {exc.next_action}',
            )
        self._task_room[task_id] = context_id
        position = session.stream.position
        pending = self._pending.get(task_id)
        if pending is not None:
            _agent_id, request_id, options = pending
            if options and text not in options:
                return _Outcome(
                    TaskState.TASK_STATE_INPUT_REQUIRED,
                    (
                        f'期望: 答复是列出的选项之一：{list(options)}。'
                        f'实际: 收到 {text!r}。'
                        '下一步: 原样回复其中一个选项；这个问题还挂着。'
                    ),
                )
            try:
                await session.answer(request_id, text)
            except ApiError as exc:
                return _Outcome(
                    TaskState.TASK_STATE_INPUT_REQUIRED,
                    f'期望: {exc.expected}。实际: {exc.received}。下一步: {exc.next_action}',
                )
            self._pending.pop(task_id, None)
        else:
            delivered = await session.handle.deliver(session.handle.root_id, text)
            if not delivered.ok:
                return _Outcome(
                    TaskState.TASK_STATE_FAILED,
                    (
                        '期望: 根 Agent 接受这条消息。'
                        f'实际: {delivered.reason or "投递没有给出原因"}。'
                        '下一步: 用同一个 contextId 再发；会话已经结束就换一个 contextId。'
                    ),
                )
        return await self._wait(session, task_id, position)

    async def cancel(self, task_id: str) -> Task | None:
        context_id = self._task_room.get(task_id)
        if context_id is None:
            return None
        session = self._rooms[context_id]
        await session.handle.abort(session.handle.root_id)
        task = Task(id=task_id, context_id=context_id)
        task.status.state = TaskState.TASK_STATE_CANCELED
        return task

    async def _session(self, context_id: str, role: str) -> LiveSession:
        session = self._rooms.get(context_id)
        if session is not None:
            return session
        session = await self._live.open_session(resume=None, role=role)
        self._live.hold_questions.add(session.sid)
        self._rooms[context_id] = session
        return session

    async def _wait(self, session: LiveSession, task_id: str, position: str) -> _Outcome:
        texts: list[str] = []
        root_id = session.handle.root_id
        follower: AsyncIterator[Entry | str] = session.stream.follow(position)
        try:
            async for entry in follower:
                outcome = self._entry(root_id, task_id, entry, texts)
                if outcome is not None:
                    return outcome
        finally:
            close = getattr(follower, 'aclose', None)
            if close is not None:
                await close()
        return _Outcome(TaskState.TASK_STATE_FAILED, _executor_failed('流已关闭'))

    def _entry(
        self, root_id: str, task_id: str, entry: Entry | str, texts: list[str]
    ) -> _Outcome | None:
        if isinstance(entry, str):
            return None
        payload = _payload(entry.data)
        agent_id = str(entry.data.get('agent_id') or '')
        # 子 Agent 的提问照样停成 INPUT_REQUIRED：根停在派活里等它，丢掉它这一轮就永远不结束。
        # 答复按待答表交给提问的那个 Agent。
        if entry.kind in {'user_question_requested', 'user_input_requested'}:
            raw_options = payload.get('options')
            items = cast(list[object], raw_options) if isinstance(raw_options, list) else []
            options = tuple(str(item) for item in items)
            question = str(payload.get('question') or '')
            lines = [question, *(f'{index}. {option}' for index, option in enumerate(options, 1))]
            shown = '\n'.join(line for line in lines if line)
            self._pending[task_id] = (agent_id, str(payload.get('request_id') or ''), options)
            return _Outcome(TaskState.TASK_STATE_INPUT_REQUIRED, self._redact(shown))
        # 回答、结束与失败只认根：子 Agent 先结束、先说话，这一轮还没完。
        if agent_id != root_id:
            return None
        if entry.kind == 'message_appended':
            raw_block = payload.get('block')
            block = cast(dict[str, object], raw_block) if isinstance(raw_block, dict) else None
            if block is not None and block.get('role') == 'assistant':
                text = block.get('text')
                if isinstance(text, str) and text:
                    texts.append(self._redact(text))
            return None
        if entry.kind == 'agent_error':
            return _Outcome(TaskState.TASK_STATE_FAILED, _from_payload(payload))
        if entry.kind == 'agent_end':
            reason = str(payload.get('stop_reason') or '')
            if reason == 'aborted':
                return _Outcome(TaskState.TASK_STATE_CANCELED, '')
            if reason == 'error':
                return _Outcome(TaskState.TASK_STATE_FAILED, _from_payload(payload))
            return _Outcome(TaskState.TASK_STATE_COMPLETED, '\n'.join(texts))
        return None

    def _redact(self, text: str) -> str:
        return self._redactor.redact(text)


def _payload(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get('payload')
    if isinstance(raw, dict):
        return cast(dict[str, Any], raw)
    return {}


def _from_payload(payload: dict[str, Any]) -> str:
    """根 Agent 的失败只报异常类型：内核事件里的 `received` 是异常原文，脱敏规则认不全它。"""
    error_type = str(payload.get('error_type') or '未知类型')
    return (
        '期望: 这一轮执行完并交回回答。'
        f'实际: 根 Agent 报错 {error_type}，原文不在任务里。'
        '下一步: 看 server 日志或 trace 里根 Agent 的 agent span 拿到原因；修好之后换一条消息再发。'
    )


def _cancel_failed(kind: str) -> str:
    return (
        '期望: 取消这个任务。'
        f'实际: 中止根 Agent 时抛出 {kind}。'
        '下一步: 看 server 日志；异常原文不会回给对端。用 GetTask 查任务此刻的状态。'
    )


def _executor_failed(kind: str) -> str:
    return (
        '期望: 这一轮执行完并交回回答。'
        f'实际: 执行器抛出 {kind}。'
        '下一步: 看 server 日志；异常原文不会写进任务，也不会自动重试。'
    )


def _no_push() -> UnsupportedOperationError:
    return UnsupportedOperationError(
        message=(
            '期望: 这一版只做 JSON-RPC 的消息、取任务和取消。'
            '实际: 收到推送或订阅。'
            '下一步: 不要配 push notification；结果在这次响应里。'
        )
    )
