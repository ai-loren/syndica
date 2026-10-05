"""记忆的 JSON 序列化（M6 §8.9）。`FileMemoryStore` 用它把短期日志与长期记录落盘再读回。

round-trip 全字段（含 `trust` 枚举值）。读回时：版本比内核认得的新就拒（与会话 serde 同理）；`trust`
缺失或不认识按 UNTRUSTED（fail-closed，spec §12.2 第 2 条）；`scope` 不认识按坏文件报错，不猜着读。
"""

from __future__ import annotations

from typing import Any, cast

from syndica.kernel.context import TrustLevel
from syndica.kernel.errors import ConfigError
from syndica.kernel.memory_types import MemoryEntry, MemoryRecord, MemoryScope, MemorySection

__all__ = [
    'MEMORY_SCHEMA',
    'NewerMemorySchema',
    'long_from_json',
    'long_to_json',
    'short_from_json',
    'short_to_json',
]

MEMORY_SCHEMA = 'syndica.memory/1'
_SCHEMA_PREFIX = 'syndica.memory/'
_KNOWN_VERSION = 1
_SCOPES = {scope.value: scope for scope in MemoryScope}


class NewerMemorySchema(ConfigError):
    """记忆文件的格式版本比这个 syndica 认得的新：更新版本写下的记录不猜着读。"""


def _corrupt(where: str, detail: str) -> ConfigError:
    return ConfigError(
        expected=f'{where} 是一份格式正确的记忆文件',
        received=detail,
        next_action='它被改坏了或不是记忆文件；删掉它让它重建，或从备份恢复',
    )


def _version_of(schema: object) -> int:
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
    if _version_of(data.get('schema')) > _KNOWN_VERSION:
        raise NewerMemorySchema(
            expected=f'记忆文件的格式版本不高于 {_KNOWN_VERSION}',
            received=f'文件是 {data.get("schema")!r}',
            next_action='用新版 syndica 读它；旧版本不猜着读新格式',
        )


def _scope_from(value: object, where: str) -> MemoryScope:
    scope = _SCOPES.get(value) if isinstance(value, str) else None
    if scope is None:
        raise _corrupt(where, f'scope={value!r} 不是 {sorted(_SCOPES)} 之一')
    return scope


def _trust_from(value: object) -> TrustLevel:
    # 缺失或不认识一律 UNTRUSTED：反序列化不把未知当可信（spec §12.2 第 2 条）。
    return TrustLevel.TRUSTED if value == TrustLevel.TRUSTED.value else TrustLevel.UNTRUSTED


def _entry_to(entry: MemoryEntry) -> dict[str, object]:
    return {
        'seq': entry.seq,
        'scope': entry.scope.value,
        'workspace': entry.workspace,
        'text': entry.text,
        'trust': entry.trust.value,
        'created_at': entry.created_at,
    }


def _entry_from(data: object) -> MemoryEntry:
    if not isinstance(data, dict):
        raise _corrupt('memory entry', f'条目不是对象，是 {type(data).__name__}')
    entry = cast('dict[str, Any]', data)
    seq = entry.get('seq')
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise _corrupt('memory entry', f'seq={seq!r} 不是正整数')
    workspace = entry.get('workspace')
    if workspace is not None and not isinstance(workspace, str):
        raise _corrupt('memory entry', f'workspace={workspace!r} 不是字符串或 null')
    return MemoryEntry(
        seq=seq,
        scope=_scope_from(entry.get('scope'), 'memory entry'),
        workspace=workspace,
        text=str(entry.get('text', '')),
        trust=_trust_from(entry.get('trust')),
        created_at=float(entry.get('created_at', 0.0)),
    )


def short_to_json(
    scope: MemoryScope, workspace: str | None, entries: list[MemoryEntry]
) -> dict[str, object]:
    return {
        'schema': MEMORY_SCHEMA,
        'scope': scope.value,
        'workspace': workspace,
        'entries': [_entry_to(entry) for entry in entries],
    }


def short_from_json(data: dict[str, Any]) -> list[MemoryEntry]:
    _checked_schema(data)
    raw = data.get('entries')
    if not isinstance(raw, list):
        raise _corrupt('short-term memory', f'entries 不是数组，是 {type(raw).__name__}')
    return [_entry_from(item) for item in cast('list[object]', raw)]


def _section_to(section: MemorySection) -> dict[str, object]:
    return {'heading': section.heading, 'text': section.text, 'trust': section.trust.value}


def _section_from(data: object) -> MemorySection:
    if not isinstance(data, dict):
        raise _corrupt('memory section', f'段落不是对象，是 {type(data).__name__}')
    section = cast('dict[str, Any]', data)
    return MemorySection(
        heading=str(section.get('heading', '')),
        text=str(section.get('text', '')),
        trust=_trust_from(section.get('trust')),
    )


def long_to_json(record: MemoryRecord) -> dict[str, object]:
    return {
        'schema': MEMORY_SCHEMA,
        'scope': record.scope.value,
        'workspace': record.workspace,
        'sections': [_section_to(section) for section in record.sections],
        'through_seq': record.through_seq,
        'version': record.version,
        'updated_at': record.updated_at,
    }


def long_from_json(data: dict[str, Any]) -> MemoryRecord:
    _checked_schema(data)
    raw = data.get('sections')
    if not isinstance(raw, list):
        raise _corrupt('long-term memory', f'sections 不是数组，是 {type(raw).__name__}')
    through = data.get('through_seq')
    version = data.get('version')
    if not isinstance(through, int) or isinstance(through, bool) or through < 0:
        raise _corrupt('long-term memory', f'through_seq={through!r} 不是非负整数')
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise _corrupt('long-term memory', f'version={version!r} 不是非负整数')
    return MemoryRecord(
        scope=_scope_from(data.get('scope'), 'long-term memory'),
        workspace=cast('str | None', data.get('workspace')),
        sections=[_section_from(item) for item in cast('list[object]', raw)],
        through_seq=through,
        version=version,
        updated_at=float(data.get('updated_at', 0.0)),
    )
