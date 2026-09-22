"""Safe YAML loading owned by the runner surface."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from syndica.kernel import ConfigError

__all__: list[str] = []


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueSafeLoader, node: MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = cast(
            object,
            loader.construct_object(  # pyright: ignore[reportUnknownMemberType]
                key_node, deep=deep
            ),
        )
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                'while constructing a mapping',
                node.start_mark,
                f'found unhashable key {key!r}',
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                'while constructing a mapping',
                node.start_mark,
                f'found duplicate key {key!r}',
                key_node.start_mark,
            )
        mapping[key] = cast(
            object,
            loader.construct_object(  # pyright: ignore[reportUnknownMemberType]
                value_node, deep=deep
            ),
        )
    return mapping


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_yaml_mapping(path: object) -> tuple[Mapping[str, object], Path]:
    if not isinstance(path, str | Path):
        raise ConfigError(
            expected='场景路径是 .yaml / .yml 文件路径',
            received=f'收到 {path!r}，类型是 {type(path).__name__}',
            next_action='传入 Path 或字符串路径',
        )
    file = Path(path)
    if file.suffix.lower() not in {'.yaml', '.yml'} or not file.is_file():
        raise ConfigError(
            expected='场景路径指向存在的 .yaml / .yml 文件',
            received=f'收到 {str(file)!r}',
            next_action='创建场景 YAML，或把路径改成已有的 .yaml / .yml 文件',
        )
    try:
        text = file.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        raise ConfigError(
            expected='场景文件可按 UTF-8 读取',
            received=f'读取 {str(file)!r} 失败：{type(exc).__name__}: {exc}',
            next_action='检查文件权限、路径与编码后重试；场景必须保存为 UTF-8',
        ) from exc
    try:
        value = yaml.load(text, Loader=_UniqueSafeLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(
            expected='场景是单文档 safe YAML Mapping，且每个映射键只出现一次',
            received=f'{str(file)!r} 解析失败：{exc}',
            next_action='修正 YAML 语法或重复键；不要使用 !!python/object 等非 safe 标签',
        ) from exc
    if not isinstance(value, Mapping):
        raise ConfigError(
            expected='场景根是 Mapping',
            received=f'{str(file)!r} 的根是 {type(value).__name__}：{value!r}',
            next_action=(
                '把场景写成 version / name / agents / topology / entry / responses / expect'
            ),
        )
    raw = cast(Mapping[object, object], value)
    non_string = [key for key in raw if not isinstance(key, str)]
    if non_string:
        raise ConfigError(
            expected='场景根的键都是字符串',
            received=f'非字符串键为 {non_string!r}',
            next_action='把键改成 version / name / agents / topology / entry / responses / expect',
        )
    return cast(Mapping[str, object], raw), file
