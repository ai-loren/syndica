"""会话记录的 JSON 序列化（M6 T2）。`FileSessionStore` 用它把 `SessionRecord` 落成一行 JSON 再读回。

round-trip 全字段（含 `extra` 嵌套值、`artifacts`、`ToolCall` 次序、`trust` 枚举值）。读回时：版本比
内核认得的新就拒（与 `FileTraceStore` 的 `NewerTraceSchema` 同理）；`trust` 缺失或不认识按 UNTRUSTED
（fail-closed，spec §12.2 第 2 条、评审 P4）；坏块跳过并由调用方记录（坏记录尽量修复、跳过坏子项）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from syndica.kernel.budget import Usage
from syndica.kernel.context import ArtifactRef, ContextBlock, ToolCall, TrustLevel
from syndica.kernel.errors import ConfigError
from syndica.kernel.session_types import SessionBudget, SessionMeta, SessionRecord, SessionStatus

__all__ = [
    'SESSION_SCHEMA',
    'NewerSessionSchema',
    'meta_from_json',
    'meta_to_json',
    'record_from_json',
    'record_to_json',
]

SESSION_SCHEMA = 'syndica.session/1'
_SCHEMA_PREFIX = 'syndica.session/'
_KNOWN_VERSION = 1
_ROLES = frozenset({'system', 'user', 'assistant', 'tool'})


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


def _money(value: Decimal | None) -> str | None:
    return None if value is None else format(value, 'f')


def _usage_to(used: Usage) -> dict[str, object]:
    return {
        'input_tokens': used.input_tokens,
        'output_tokens': used.output_tokens,
        'cached_input_tokens': used.cached_input_tokens,
        'cost': _money(used.cost),
        'turns': used.turns,
        'estimated': used.estimated,
    }


def _budget_to(budget: SessionBudget) -> dict[str, object]:
    return {
        'tokens': budget.tokens,
        'cost': _money(budget.cost),
        'turns': budget.turns,
        'used': _usage_to(budget.used),
    }


def record_to_json(record: SessionRecord) -> dict[str, object]:
    body: dict[str, object] = {
        'schema': SESSION_SCHEMA,
        'meta': _meta_to(record.meta),
        'content': [_block_to(block) for block in record.content],
        'system_prompt': record.system_prompt,
        'model': record.model,
        'tools': list(record.tools),
        'flags': dict(record.flags),
    }
    # 旧记录没有这一键。写出 None 会让「没记预算」和「记了一份空预算」分不清，读回时也没法
    # 把缺键当成从零开始。
    if record.budget is not None:
        body['budget'] = _budget_to(record.budget)
    return body


def meta_to_json(meta: SessionMeta) -> dict[str, object]:
    return {'schema': SESSION_SCHEMA, 'meta': _meta_to(meta)}


def _corrupt(where: str, detail: str) -> ConfigError:
    return ConfigError(
        expected=f'{where} 是一份格式正确的会话记录',
        received=detail,
        next_action='它被改坏了或不是会话记录；用 sessions rm 删掉它，或从备份恢复',
    )


def _version_of(schema: object) -> int:
    # 前缀必须逐字是 syndica.session/：'syndica.trace/1' 这类别的记录不许当会话读。版本是正整数，
    # 'syndica.session/abc' 这种解析不出数字的不许当 v1 收下（否则更新 / 乱码格式被猜着读）。
    if not isinstance(schema, str) or not schema.startswith(_SCHEMA_PREFIX):
        raise _corrupt('schema', f'schema={schema!r}，不是 {_SCHEMA_PREFIX}N')
    try:
        version = int(schema[len(_SCHEMA_PREFIX) :])
    except ValueError:
        raise _corrupt('schema', f'schema={schema!r} 的版本不是整数') from None
    if version <= 0:
        raise _corrupt('schema', f'schema={schema!r} 的版本不是正整数')
    return version


def _checked_schema(data: dict[str, Any]) -> None:
    version = _version_of(data.get('schema'))
    if version > _KNOWN_VERSION:
        raise NewerSessionSchema(
            expected=f'会话记录的格式版本不高于 {_KNOWN_VERSION}',
            received=f'记录是 {data.get("schema")!r}',
            next_action='用新版 syndica 读它，或让写它的那个版本来读；旧版本不猜着读新格式',
        )


def _require(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise _corrupt(where, f'缺少字段 {key!r}')
    return data[key]


def _call_from(data: object) -> ToolCall:
    if not isinstance(data, dict):
        raise _corrupt('tool_call', f'tool_call 不是对象，是 {type(data).__name__}')
    call = cast('dict[str, Any]', data)
    return ToolCall(
        id=_require(call, 'id', 'tool_call'),
        name=_require(call, 'name', 'tool_call'),
        arguments=_require(call, 'arguments', 'tool_call'),
    )


def _artifact_from(data: object) -> ArtifactRef:
    if not isinstance(data, dict):
        raise _corrupt('artifact', f'artifact 不是对象，是 {type(data).__name__}')
    ref = cast('dict[str, Any]', data)
    return ArtifactRef(
        id=_require(ref, 'id', 'artifact'),
        path=_require(ref, 'path', 'artifact'),
        kind=_require(ref, 'kind', 'artifact'),
        mime=_require(ref, 'mime', 'artifact'),
        version=_require(ref, 'version', 'artifact'),
        producer=_require(ref, 'producer', 'artifact'),
        is_deliverable=bool(ref.get('is_deliverable', False)),
    )


def _block_from(data: object) -> ContextBlock:
    # trust 缺失或不认识一律 UNTRUSTED：反序列化不把未知当可信（spec §12.2 第 2 条、评审 P4）。
    if not isinstance(data, dict):
        raise _corrupt('content block', f'块不是对象，是 {type(data).__name__}')
    block = cast('dict[str, Any]', data)
    role = block.get('role')
    if role not in _ROLES:
        raise _corrupt('content block', f'role={role!r} 不是 {sorted(_ROLES)} 之一')
    calls = block.get('tool_calls', [])
    arts = block.get('artifacts', [])
    extra = block.get('extra', {})
    if not isinstance(calls, list) or not isinstance(arts, list) or not isinstance(extra, dict):
        raise _corrupt('content block', 'tool_calls / artifacts 必须是数组、extra 必须是对象')
    trust = (
        TrustLevel.TRUSTED
        if block.get('trust') == TrustLevel.TRUSTED.value
        else (TrustLevel.UNTRUSTED)
    )
    return ContextBlock(
        role=role,
        content=block.get('content'),
        tool_calls=[_call_from(c) for c in cast('list[object]', calls)],
        tool_call_id=block.get('tool_call_id'),
        artifacts=[_artifact_from(a) for a in cast('list[object]', arts)],
        trust=trust,
        extra=dict(cast('dict[str, Any]', extra)),
    )


def _int(value: object, field: str) -> int:
    # bool 是 int 的子类；turns / archived 收到 True 当成 1 是静默的坏值，单独拒。
    if isinstance(value, bool) or not isinstance(value, int):
        raise _corrupt(field, f'{field}={value!r} 不是整数')
    return value


def _meta_from(data: object) -> SessionMeta:
    if not isinstance(data, dict):
        raise _corrupt('meta', f'meta 不是对象，是 {type(data).__name__}')
    meta = cast('dict[str, Any]', data)
    sid = meta.get('sid')
    if not isinstance(sid, str) or not sid:
        raise _corrupt('meta', f'sid={sid!r} 不是非空字符串')
    status_value = meta.get('status', SessionStatus.ACTIVE.value)
    try:
        status = SessionStatus(status_value)
    except ValueError:
        raise _corrupt('meta', f'status={status_value!r} 不是已知状态') from None
    return SessionMeta(
        sid=sid,
        title=meta.get('title', ''),
        agent=meta.get('agent', ''),
        workspace=meta.get('workspace'),
        status=status,
        turns=_int(meta.get('turns', 0), 'turns'),
        archived=_int(meta.get('archived', 0), 'archived'),
        last_trace=meta.get('last_trace'),
        created_at=meta.get('created_at', 0.0),
        updated_at=meta.get('updated_at', 0.0),
    )


def meta_from_json(data: dict[str, Any]) -> SessionMeta:
    _checked_schema(data)
    return _meta_from(data.get('meta'))


def record_from_json(data: dict[str, Any]) -> SessionRecord:
    """整份读回，**坏块即坏记录**（fail-closed）：一个解析不出的块可能正是这份轨迹里唯一的不可信
    块，静默跳过它会把 provenance 洗成 TRUSTED，还可能在历史中段留下未配对的工具调用（评审 F3）。
    任何坏子项抛三件套 `ConfigError`，由存储按 corrupt 对待；恢复宁可拒绝也不续一份被洗过的轨迹。
    """
    _checked_schema(data)
    raw_content = data.get('content', [])
    raw_tools = data.get('tools', [])
    raw_flags = data.get('flags', {})
    if not isinstance(raw_content, list):
        raise _corrupt('content', f'content 不是数组，是 {type(raw_content).__name__}')
    if not isinstance(raw_tools, list):
        raise _corrupt('tools', f'tools 不是数组，是 {type(raw_tools).__name__}')
    if not isinstance(raw_flags, dict):
        raise _corrupt('flags', f'flags 不是对象，是 {type(raw_flags).__name__}')
    return SessionRecord(
        meta=_meta_from(data.get('meta')),
        content=[_block_from(raw) for raw in cast('list[object]', raw_content)],
        system_prompt=data.get('system_prompt', ''),
        model=data.get('model', ''),
        tools=[str(tool) for tool in cast('list[object]', raw_tools)],
        flags=dict(cast('dict[str, Any]', raw_flags)),
        budget=None if 'budget' not in data else _budget_from(data.get('budget')),
    )


def _count(value: object, field: str, *, allow_none: bool) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _corrupt('budget', f'{field}={value!r}，要的是 >= 0 的整数')
    return value


def _decimal(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise _corrupt('budget', f'{field}={value!r}，要的是十进制字符串或 null')
    try:
        amount = Decimal(value)
    except Exception:
        raise _corrupt('budget', f'{field}={value!r} 不是十进制金额') from None
    if not amount.is_finite() or amount < 0:
        raise _corrupt('budget', f'{field}={value!r} 不是有限的非负金额')
    return amount


def _budget_from(data: object) -> SessionBudget:
    if not isinstance(data, dict):
        raise _corrupt('budget', f'budget 不是对象，是 {type(data).__name__}')
    body = cast('dict[str, Any]', data)
    used = body.get('used')
    if not isinstance(used, dict):
        raise _corrupt('budget', f'budget.used 不是对象，是 {type(used).__name__}')
    spent = cast('dict[str, Any]', used)
    try:
        usage = Usage(
            input_tokens=cast(
                int, _count(spent.get('input_tokens', 0), 'used.input_tokens', allow_none=False)
            ),
            output_tokens=cast(
                int, _count(spent.get('output_tokens', 0), 'used.output_tokens', allow_none=False)
            ),
            cached_input_tokens=cast(
                int,
                _count(
                    spent.get('cached_input_tokens', 0),
                    'used.cached_input_tokens',
                    allow_none=False,
                ),
            ),
            cost=_decimal(spent.get('cost', '0'), 'used.cost'),
            turns=cast(int, _count(spent.get('turns', 0), 'used.turns', allow_none=False)),
            estimated=spent.get('estimated') is True,
        )
    except ConfigError as exc:
        raise _corrupt('budget', exc.received) from None
    return SessionBudget(
        tokens=_count(body.get('tokens'), 'tokens', allow_none=True),
        cost=_decimal(body.get('cost'), 'cost'),
        turns=_count(body.get('turns'), 'turns', allow_none=True),
        used=usage,
    )
