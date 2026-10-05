"""产物索引的 JSON 序列化。`FileArtifactStore` 用它把 id → 版本列表 + 交付物标记落盘再读回。

round-trip 全字段。读回时：版本比内核认得的新就拒（与会话 / 记忆 serde 同理，不猜着读）；字段缺失 /
类型不对按坏文件报错。产物只存路径 / 版本元数据、无正文，所以没有 `trust` 字段要兜。
"""

from __future__ import annotations

from typing import Any, cast

from syndica.kernel.context import ArtifactRef
from syndica.kernel.errors import ConfigError

__all__ = ['ARTIFACT_SCHEMA', 'NewerArtifactSchema', 'index_from_json', 'index_to_json']

ARTIFACT_SCHEMA = 'syndica.artifacts/1'
_SCHEMA_PREFIX = 'syndica.artifacts/'
_KNOWN_VERSION = 1


class NewerArtifactSchema(ConfigError):
    """产物索引的格式版本比这个 syndica 认得的新：更新版本写下的记录不猜着读。"""


def _corrupt(where: str, detail: str) -> ConfigError:
    return ConfigError(
        expected=f'{where} 是一份格式正确的产物索引',
        received=detail,
        next_action='它被改坏了或不是产物索引；删掉它让它重建，或从备份恢复',
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
        raise NewerArtifactSchema(
            expected=f'产物索引的格式版本不高于 {_KNOWN_VERSION}',
            received=f'文件是 {schema!r}',
            next_action='用新版 syndica 读它；旧版本不猜着读新格式',
        )


def _str(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise _corrupt(where, f'{where}={value!r} 不是字符串')
    return value


def _ref_to(ref: ArtifactRef) -> dict[str, object]:
    return {
        'id': ref.id,
        'path': ref.path,
        'kind': ref.kind,
        'mime': ref.mime,
        'version': ref.version,
        'producer': ref.producer,
        'is_deliverable': ref.is_deliverable,
    }


def _ref_from(data: object, where: str) -> ArtifactRef:
    if not isinstance(data, dict):
        raise _corrupt(where, f'{where} 不是对象，是 {type(data).__name__}')
    ref = cast('dict[str, Any]', data)
    return ArtifactRef(
        id=_str(ref.get('id'), f'{where}.id'),
        path=_str(ref.get('path'), f'{where}.path'),
        kind=_str(ref.get('kind'), f'{where}.kind'),
        mime=_str(ref.get('mime'), f'{where}.mime'),
        version=_str(ref.get('version'), f'{where}.version'),
        producer=_str(ref.get('producer'), f'{where}.producer'),
        is_deliverable=ref.get('is_deliverable') is True,
    )


def index_to_json(order: list[str], versions: dict[str, list[ArtifactRef]]) -> dict[str, object]:
    return {
        'schema': ARTIFACT_SCHEMA,
        'artifacts': [
            {
                'id': artifact_id,
                'deliverable': any(ref.is_deliverable for ref in versions[artifact_id]),
                'versions': [_ref_to(ref) for ref in versions[artifact_id]],
            }
            for artifact_id in order
        ],
    }


def index_from_json(data: dict[str, Any]) -> tuple[list[str], dict[str, list[ArtifactRef]]]:
    """返回 (登记顺序的 id 列表, id → 版本列表)。交付物标记由各版本的 `is_deliverable` 粘性推出，
    不单独存——落盘的 `deliverable` 只是便于人读。"""
    _checked_schema(data)
    raw = data.get('artifacts')
    if not isinstance(raw, list):
        raise _corrupt('artifacts', f'artifacts 不是数组，是 {type(raw).__name__}')
    order: list[str] = []
    versions: dict[str, list[ArtifactRef]] = {}
    for i, entry in enumerate(cast('list[object]', raw)):
        if not isinstance(entry, dict):
            raise _corrupt(f'artifacts[{i}]', '不是对象')
        item = cast('dict[str, Any]', entry)
        artifact_id = _str(item.get('id'), f'artifacts[{i}].id')
        vraw = item.get('versions')
        if not isinstance(vraw, list) or not vraw:
            raise _corrupt(f'artifacts[{i}].versions', '不是非空数组')
        vlist = cast('list[object]', vraw)
        refs = [_ref_from(v, f'artifacts[{i}].versions[{j}]') for j, v in enumerate(vlist)]
        order.append(artifact_id)
        versions[artifact_id] = refs
    return order, versions
