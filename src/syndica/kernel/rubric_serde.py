"""Goal / Rubric 的 JSON 序列化。`FileRubricStore` 用它把 Goal 与 Rubric 落盘再读回。

round-trip 全字段。读回时：版本比内核认得的新就拒（与会话 / 记忆 / 产物 serde 同理，不猜着读）；
`source` / `kind` / `status` 枚举不认识按坏文件报错（不猜着读）；`trust` 缺失或不认识按 UNTRUSTED
（fail-closed，spec §12.2 第 2 条——反序列化不把未知当可信）。
"""

from __future__ import annotations

from typing import Any, cast

from syndica.kernel.acceptance import (
    Anchor,
    AnchorSource,
    Goal,
    Rubric,
    RubricKind,
    RubricOrigin,
    RubricStatus,
)
from syndica.kernel.context import TrustLevel
from syndica.kernel.errors import ConfigError

__all__ = [
    'RUBRIC_SCHEMA',
    'NewerRubricSchema',
    'goal_from_json',
    'goal_to_json',
    'rubrics_from_json',
    'rubrics_to_json',
]

RUBRIC_SCHEMA = 'syndica.rubrics/1'
_SCHEMA_PREFIX = 'syndica.rubrics/'
_KNOWN_VERSION = 1
_SOURCES = {m.value: m for m in AnchorSource}
_KINDS = {m.value: m for m in RubricKind}
_STATUSES = {m.value: m for m in RubricStatus}
_ORIGINS = {m.value: m for m in RubricOrigin}


class NewerRubricSchema(ConfigError):
    """Goal / Rubric 文件的格式版本比这个 syndica 认得的新：更新版本写下的记录不猜着读。"""


def _corrupt(where: str, detail: str) -> ConfigError:
    return ConfigError(
        expected=f'{where} 是一份格式正确的 Goal / Rubric 文件',
        received=detail,
        next_action='它被改坏了或不是 Goal / Rubric 文件；删掉它让它重建，或从备份恢复',
    )


def _checked_schema(data: dict[str, Any]) -> None:
    schema = data.get('schema')
    if not isinstance(schema, str) or not schema.startswith(_SCHEMA_PREFIX):
        raise _corrupt('schema', f'schema={schema!r}，不是 {_SCHEMA_PREFIX}N')
    try:
        version = int(schema[len(_SCHEMA_PREFIX) :])
    except ValueError:
        raise _corrupt('schema', f'schema={schema!r} 的版本不是整数') from None
    if version <= 0:
        raise _corrupt('schema', f'schema={schema!r} 的版本不是正整数')
    if version > _KNOWN_VERSION:
        raise NewerRubricSchema(
            expected=f'Goal / Rubric 文件的格式版本不高于 {_KNOWN_VERSION}',
            received=f'文件是 {schema!r}',
            next_action='用新版 syndica 读它；旧版本不猜着读新格式',
        )


def _str(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise _corrupt(where, f'{where}={value!r} 不是字符串')
    return value


def _trust_from(value: object) -> TrustLevel:
    return TrustLevel.TRUSTED if value == TrustLevel.TRUSTED.value else TrustLevel.UNTRUSTED


def _anchor_to(anchor: Anchor) -> dict[str, object]:
    return {'source': anchor.source.value, 'locator': anchor.locator, 'quote': anchor.quote}


def _anchor_from(data: object, where: str) -> Anchor:
    if not isinstance(data, dict):
        raise _corrupt(where, f'{where} 不是对象')
    item = cast('dict[str, Any]', data)
    raw_source = item.get('source')
    source = _SOURCES.get(raw_source) if isinstance(raw_source, str) else None
    if source is None:
        raise _corrupt(f'{where}.source', f'source={raw_source!r} 不是 {sorted(_SOURCES)}')
    return Anchor(
        source=source,
        locator=_str(item.get('locator'), f'{where}.locator'),
        quote=_str(item.get('quote'), f'{where}.quote'),
    )


def _anchors_from(value: object, where: str) -> tuple[Anchor, ...]:
    if not isinstance(value, list) or not value:
        raise _corrupt(where, f'{where} 不是非空数组')
    return tuple(
        _anchor_from(a, f'{where}[{i}]') for i, a in enumerate(cast('list[object]', value))
    )


def goal_to_json(goal: Goal) -> dict[str, object]:
    return {
        'schema': RUBRIC_SCHEMA,
        'intent': goal.intent,
        'anchors': [_anchor_to(a) for a in goal.anchors],
        'trust': goal.trust.value,
    }


def goal_from_json(data: dict[str, Any]) -> Goal:
    _checked_schema(data)
    raw = data.get('anchors')
    anchors = () if not raw else _anchors_from(raw, 'anchors')
    return Goal(
        intent=_str(data.get('intent'), 'intent'),
        anchors=anchors,
        trust=_trust_from(data.get('trust')),
    )


def _rubric_to(rubric: Rubric) -> dict[str, object]:
    return {
        'id': rubric.id,
        'statement': rubric.statement,
        'kind': rubric.kind.value,
        'anchors': [_anchor_to(a) for a in rubric.anchors],
        'mandatory': rubric.mandatory,
        'status': rubric.status.value,
        'trust': rubric.trust.value,
        'origin': rubric.origin.value,
    }


def _rubric_from(data: object, where: str) -> Rubric:
    if not isinstance(data, dict):
        raise _corrupt(where, f'{where} 不是对象')
    item = cast('dict[str, Any]', data)
    raw_kind = item.get('kind')
    kind = _KINDS.get(raw_kind) if isinstance(raw_kind, str) else None
    if kind is None:
        raise _corrupt(f'{where}.kind', f'kind={raw_kind!r} 不是 {sorted(_KINDS)}')
    raw_status = item.get('status')
    status = _STATUSES.get(raw_status) if isinstance(raw_status, str) else None
    if status is None:
        raise _corrupt(f'{where}.status', f'status={raw_status!r} 不是 {sorted(_STATUSES)}')
    raw_origin = item.get('origin')
    origin = _ORIGINS.get(raw_origin) if isinstance(raw_origin, str) else None
    if origin is None:
        raise _corrupt(f'{where}.origin', f'origin={raw_origin!r} 不是 {sorted(_ORIGINS)}')
    return Rubric(
        id=_str(item.get('id'), f'{where}.id'),
        statement=_str(item.get('statement'), f'{where}.statement'),
        kind=kind,
        anchors=_anchors_from(item.get('anchors'), f'{where}.anchors'),
        mandatory=item.get('mandatory') is not False,
        status=status,
        trust=_trust_from(item.get('trust')),
        origin=origin,
    )


def rubrics_to_json(rubrics: list[Rubric]) -> dict[str, object]:
    return {'schema': RUBRIC_SCHEMA, 'rubrics': [_rubric_to(r) for r in rubrics]}


def rubrics_from_json(data: dict[str, Any]) -> list[Rubric]:
    _checked_schema(data)
    raw = data.get('rubrics')
    if not isinstance(raw, list):
        raise _corrupt('rubrics', f'rubrics 不是数组，是 {type(raw).__name__}')
    return [_rubric_from(r, f'rubrics[{i}]') for i, r in enumerate(cast('list[object]', raw))]
