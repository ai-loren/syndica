"""OpenAPI 3.1 文档：由 `models` 里的 pydantic 模型生成，不引 OpenAPI 框架。

SSE 事件与 WebSocket 帧不是请求 / 响应体，放在扩展字段 `x-syndica-events` / `x-syndica-rpc`
里，前端类型同样从这份文档生成。快照在 `docs/reference/openapi.json`，改动要在
`docs/reference/api-changes.md` 记一条（用例按快照摘要判）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from syndica import __version__
from syndica.kernel.events import KERNEL_EVENTS
from syndica.server.models import (
    CreateSession,
    DoctorReport,
    ErrorBody,
    EventEnvelope,
    Health,
    LoginRequest,
    LoginResponse,
    Roles,
    RpcReply,
    RpcRequest,
    SessionList,
    Snapshot,
)

__all__ = ['PATHS', 'SERVER_EVENTS', 'openapi']

_REF = '#/components/schemas/{model}'

# (方法, 路径, 请求体模型, 成功状态, 响应模型, 是否要令牌, 说明)
PATHS: tuple[
    tuple[str, str, type[BaseModel] | None, int, type[BaseModel] | None, bool, str], ...
] = (
    ('get', '/api/v1/health', None, 200, Health, False, '进程 nonce 与版本；公开'),
    ('post', '/api/v1/login', LoginRequest, 200, LoginResponse, False, '一次性登录码换会话令牌'),
    ('get', '/api/v1/doctor', None, 200, DoctorReport, True, '逐项自检（缺 key、配置改过）'),
    ('get', '/api/v1/roles', None, 200, Roles, True, '可以建会话的角色'),
    ('get', '/api/v1/sessions', None, 200, SessionList, True, '落盘的与打开着的会话'),
    ('post', '/api/v1/sessions', CreateSession, 201, Snapshot, True, '建会话，交回快照'),
    ('post', '/api/v1/sessions/{sid}/resume', None, 200, Snapshot, True, '打开落盘的会话'),
    ('post', '/api/v1/sessions/{sid}/close', None, 204, None, True, '关闭（存盘、放锁）'),
    ('delete', '/api/v1/sessions/{sid}', None, 204, None, True, '关闭并删除落盘的会话'),
    ('get', '/api/v1/sessions/{sid}/snapshot', None, 200, Snapshot, True, '快照，带 as_of'),
    ('get', '/api/v1/sessions/{sid}/events', None, 200, None, True, '会话事件流（SSE）'),
    ('get', '/api/v1/events', None, 200, None, True, '全局事件流（SSE）'),
)

# server 自己发的事件种类（不在内核事件表里）与各自 payload 里必有的键。
SERVER_EVENTS: dict[str, tuple[str, ...]] = {
    'reset': (),
    'budget_question': ('request_id', 'view'),
    'budget_question_resolved': ('request_id',),
    'session_opened': (),
    'session_closed': (),
}

_RPC_OPS: dict[str, dict[str, Any]] = {
    'auth': {'token': {'type': 'string'}},
    'send': {'text': {'type': 'string'}, 'target': {'type': 'string'}},
    'answer': {'request_id': {'type': 'string'}, 'text': {'type': 'string'}},
    'abort': {'agent_id': {'type': 'string'}},
    'grant': {
        'agent_id': {'type': 'string'},
        'budget': {'$ref': _REF.format(model='BudgetRequest')},
    },
    'budget_answer': {
        'request_id': {'type': 'string'},
        'budget': {'anyOf': [{'$ref': _REF.format(model='BudgetRequest')}, {'type': 'null'}]},
    },
}

_MODELS: tuple[type[BaseModel], ...] = (
    CreateSession,
    DoctorReport,
    ErrorBody,
    EventEnvelope,
    Health,
    LoginRequest,
    LoginResponse,
    Roles,
    RpcReply,
    RpcRequest,
    SessionList,
    Snapshot,
)


def _schemas() -> dict[str, Any]:
    schemas: dict[str, Any] = {}
    for model in _MODELS:
        schema = model.model_json_schema(ref_template=_REF)
        for name, definition in schema.pop('$defs', {}).items():
            schemas[name] = definition
        schemas[model.__name__] = schema
    return dict(sorted(schemas.items()))


def _json(model: type[BaseModel]) -> dict[str, Any]:
    return {'application/json': {'schema': {'$ref': _REF.format(model=model.__name__)}}}


def _operation(
    request: type[BaseModel] | None,
    status: int,
    response: type[BaseModel] | None,
    auth: bool,
    summary: str,
    path: str,
) -> dict[str, Any]:
    operation: dict[str, Any] = {'summary': summary}
    if '{sid}' in path:
        operation['parameters'] = [
            {'name': 'sid', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}
        ]
    if request is not None:
        operation['requestBody'] = {'required': True, 'content': _json(request)}
    responses: dict[str, Any] = {}
    if response is not None:
        responses[str(status)] = {'description': summary, 'content': _json(response)}
    elif path.endswith('events'):
        responses[str(status)] = {
            'description': '`text/event-stream`；每帧 `id: <epoch>:<seq>`、`event: <种类>`、'
            '`data:` 是 EventEnvelope；`reset` 帧没有 id，收到后重取快照',
            'content': {'text/event-stream': {'schema': {'type': 'string'}}},
        }
    else:
        responses[str(status)] = {'description': summary}
    responses['default'] = {'description': '三件套错误', 'content': _json(ErrorBody)}
    operation['responses'] = responses
    if auth:
        operation['security'] = [{'bearer': []}]
    return operation


def _events() -> dict[str, Any]:
    kinds = {**dict(KERNEL_EVENTS), **SERVER_EVENTS}
    return {
        kind: {
            'type': 'object',
            'required': list(required),
            'properties': {key: {} for key in required},
            'additionalProperties': True,
        }
        for kind, required in sorted(kinds.items())
    }


def openapi() -> dict[str, Any]:
    paths: dict[str, dict[str, Any]] = {}
    for method, path, request, status, response, auth, summary in PATHS:
        paths.setdefault(path, {})[method] = _operation(
            request, status, response, auth, summary, path
        )
    return {
        'openapi': '3.1.0',
        'info': {
            'title': 'Syndica',
            'version': '1',
            'description': (
                f'syndica {__version__} 的 HTTP / SSE / WebSocket 面。标 x-syndica-stability: '
                'provisional 的字段来自内核里 M12 才冻结的类型，形状仍可能变。'
            ),
        },
        'paths': paths,
        'components': {
            'schemas': _schemas(),
            'securitySchemes': {'bearer': {'type': 'http', 'scheme': 'bearer'}},
        },
        'x-syndica-events': _events(),
        'x-syndica-rpc': {
            'path': '/api/v1/sessions/{sid}/rpc',
            'first_frame': 'auth',
            'request': {'$ref': _REF.format(model='RpcRequest')},
            'reply': {'$ref': _REF.format(model='RpcReply')},
            'ops': {
                op: {'type': 'object', 'properties': fields}
                for op, fields in sorted(_RPC_OPS.items())
            },
        },
    }
