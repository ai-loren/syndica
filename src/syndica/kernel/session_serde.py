"""会话记录的 JSON 序列化（M6 T2）。`FileSessionStore` 用它把 `SessionRecord` 落成一行 JSON 再读回。

round-trip 全字段（含 `extra` 嵌套值、`artifacts`、`ToolCall` 次序、`trust` 枚举值）。读回时：版本比
内核认得的新就拒（与 `FileTraceStore` 的 `NewerTraceSchema` 同理）；`trust` 缺失或不认识按 UNTRUSTED
（fail-closed，spec §12.2 第 2 条、评审 P4）；坏块跳过并由调用方记录（坏记录尽量修复、跳过坏子项）。
"""

from __future__ import annotations

from typing import Any, cast

from syndica.kernel.context import ArtifactRef, ContextBlock, ToolCall, TrustLevel
from syndica.kernel.errors import ConfigError
from syndica.kernel.session_types import SessionMeta, SessionRecord, SessionStatus

__all__ = [
    'SESSION_SCHEMA',
    'NewerSessionSchema',
    'meta_from_json',
    'meta_to_json',
    'record_from_json',
    'record_to_json',
]

SESSION_SCHEMA = 'syndica.session/1'
_KNOWN_VERSION = 1


class NewerSessionSchema(ConfigError):
    """会话记录的格式版本比这个 syndica 认得的新：更新版本写下的记录不猜着读。"""


def _call_to(call: ToolCall) -> dict[str, object]:
    return {'id': call.id, 'name': call.name, 'arguments': call.arguments}


def _artifact_to(ref: ArtifactRef) -> dict[str, object]:
    return {
        'id': ref.id,
        'path': ref.path,
        'kind': ref.kind,
        'mime': ref.mime,
        'version': ref.version,
        'producer': ref.producer,
        'is_deliverable': ref.is_deliverable,
    }


def _block_to(block: ContextBlock) -> dict[str, object]:
    return {
        'role': block.role,
        'content': block.content,
        'tool_calls': [_call_to(call) for call in block.tool_calls],
        'tool_call_id': block.tool_call_id,
        'artifacts': [_artifact_to(ref) for ref in block.artifacts],
        'trust': block.trust.value,
        'extra': block.extra,
    }


def _meta_to(meta: SessionMeta) -> dict[str, object]:
    return {
        'sid': meta.sid,
        'title': meta.title,
        'agent': meta.agent,
        'workspace': meta.workspace,
        'status': meta.status.value,
        'turns': meta.turns,
        'archived': meta.archived,
        'last_trace': meta.last_trace,
        'created_at': meta.created_at,
        'updated_at': meta.updated_at,
    }


def record_to_json(record: SessionRecord) -> dict[str, object]:
    return {
        'schema': SESSION_SCHEMA,
        'meta': _meta_to(record.meta),
        'content': [_block_to(block) for block in record.content],
        'system_prompt': record.system_prompt,
        'model': record.model,
        'tools': list(record.tools),
        'flags': dict(record.flags),
    }


def meta_to_json(meta: SessionMeta) -> dict[str, object]:
    return {'schema': SESSION_SCHEMA, 'meta': _meta_to(meta)}


def _version_of(schema: object) -> int:
    if not isinstance(schema, str) or '/' not in schema:
        raise ConfigError(
            expected=f'记录带一个像 {SESSION_SCHEMA!r} 的 schema',
            received=f'schema={schema!r}',
            next_action='这不是一份会话记录，或者它被改坏了；删掉它所在的目录',
        )
    try:
        return int(schema.rsplit('/', 1)[1])
    except ValueError:
        return -1


def _checked_schema(data: dict[str, Any]) -> None:
    version = _version_of(data.get('schema'))
    if version > _KNOWN_VERSION:
        raise NewerSessionSchema(
            expected=f'会话记录的格式版本不高于 {_KNOWN_VERSION}',
            received=f'记录是 {data.get("schema")!r}',
            next_action='用新版 syndica 读它，或让写它的那个版本来读；旧版本不猜着读新格式',
        )


def _call_from(data: dict[str, Any]) -> ToolCall:
    return ToolCall(id=data['id'], name=data['name'], arguments=data['arguments'])


def _artifact_from(data: dict[str, Any]) -> ArtifactRef:
    return ArtifactRef(
        id=data['id'],
        path=data['path'],
        kind=data['kind'],
        mime=data['mime'],
        version=data['version'],
        producer=data['producer'],
        is_deliverable=bool(data.get('is_deliverable', False)),
    )


def _block_from(data: dict[str, Any]) -> ContextBlock:
    # trust 缺失或不认识一律 UNTRUSTED：反序列化不把未知当可信（spec §12.2 第 2 条、评审 P4）。
    trust = TrustLevel.UNTRUSTED
    if data.get('trust') == TrustLevel.TRUSTED.value:
        trust = TrustLevel.TRUSTED
    return ContextBlock(
        role=data['role'],
        content=data.get('content'),
        tool_calls=[_call_from(cast('dict[str, Any]', c)) for c in data.get('tool_calls', [])],
        tool_call_id=data.get('tool_call_id'),
        artifacts=[_artifact_from(cast('dict[str, Any]', a)) for a in data.get('artifacts', [])],
        trust=trust,
        extra=dict(cast('dict[str, Any]', data.get('extra', {}))),
    )


def _meta_from(data: dict[str, Any]) -> SessionMeta:
    return SessionMeta(
        sid=data['sid'],
        title=data.get('title', ''),
        agent=data.get('agent', ''),
        workspace=data.get('workspace'),
        status=SessionStatus(data.get('status', SessionStatus.ACTIVE.value)),
        turns=data.get('turns', 0),
        archived=data.get('archived', 0),
        last_trace=data.get('last_trace'),
        created_at=data.get('created_at', 0.0),
        updated_at=data.get('updated_at', 0.0),
    )


def meta_from_json(data: dict[str, Any]) -> SessionMeta:
    _checked_schema(data)
    return _meta_from(cast('dict[str, Any]', data['meta']))


def record_from_json(data: dict[str, Any], *, dropped: list[str] | None = None) -> SessionRecord:
    """坏块跳过（记进 `dropped`），其余照读：一个坏子项不该让整份会话打不开。"""
    _checked_schema(data)
    content: list[ContextBlock] = []
    for index, raw in enumerate(data.get('content', [])):
        try:
            content.append(_block_from(cast('dict[str, Any]', raw)))
        except (KeyError, TypeError, ValueError) as error:
            if dropped is None:
                raise
            dropped.append(f'content[{index}]: {type(error).__name__}: {error}')
    return SessionRecord(
        meta=_meta_from(cast('dict[str, Any]', data['meta'])),
        content=content,
        system_prompt=data.get('system_prompt', ''),
        model=data.get('model', ''),
        tools=list(data.get('tools', [])),
        flags=dict(cast('dict[str, Any]', data.get('flags', {}))),
    )
