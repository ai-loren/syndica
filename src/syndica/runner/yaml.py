"""Safe YAML loading owned by the runner surface."""

from __future__ import annotations

import json
import json.decoder
import json.scanner
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml
from yaml.composer import ComposerError
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent
from yaml.nodes import MappingNode

from syndica.kernel import ConfigError

# 标准库里有，typeshed 没写进去的两个：逐个对象解析的入口与纯 Python 的扫描器。
_decoder = cast(Any, json.decoder)
_scanner = cast(Any, json.scanner)

__all__: list[str] = []


class _AliasRefused(ComposerError):
    pass


class _UniqueSafeLoader(yaml.SafeLoader):
    """safe YAML，另外三条：映射的键不许重复；不收别名（`*x`）——它能造出成环或成倍展开的结构，
    场景文档用不着；`0777`、`1:30` 读成字符串，不按 YAML 1.1 读成八进制与六十进制的整数（稳定键里的
    `0777` 读成 511 就对不上了）。"""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):  # pyright: ignore[reportUnknownMemberType]
            event = cast(Any, self.peek_event())  # pyright: ignore[reportUnknownMemberType]
            raise _AliasRefused(
                None, None, f'场景文档不收 YAML 别名 *{event.anchor}', event.start_mark
            )
        return super().compose_node(parent, index)  # pyright: ignore[reportUnknownMemberType]


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

_INT = 'tag:yaml.org,2002:int'
_FLOAT = 'tag:yaml.org,2002:float'
_UniqueSafeLoader.yaml_implicit_resolvers = {
    first: [(tag, pattern) for tag, pattern in resolvers if tag not in (_INT, _FLOAT)]
    for first, resolvers in cast(
        dict[str, list[tuple[str, re.Pattern[str]]]], yaml.SafeLoader.yaml_implicit_resolvers
    ).items()
}
_UniqueSafeLoader.add_implicit_resolver(  # pyright: ignore[reportUnknownMemberType]
    _INT,
    re.compile(r'^(?:[-+]?0b[0-1_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+)$'),
    list('-+0123456789'),
)
_UniqueSafeLoader.add_implicit_resolver(  # pyright: ignore[reportUnknownMemberType]
    _FLOAT,
    re.compile(
        r'^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?'
        r'|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?'
        r'|[-+]?\.(?:inf|Inf|INF)'
        r'|\.(?:nan|NaN|NAN))$'
    ),
    list('-+0123456789.'),
)


def load_document_mapping(path: object) -> tuple[Mapping[str, object], Path]:
    """读一份场景文档的根 Mapping：`.yaml` / `.yml` 按 safe YAML（手写），`.json` 按标准库 `json`
    （捕获）。两种读法都拒绝重复键：后一个静默顶掉前一个，读出来的就不是写下的那份。"""
    if not isinstance(path, str | Path):
        raise ConfigError(
            expected='场景路径是 .yaml / .yml / .json 文件路径',
            received=f'收到 {path!r}，类型是 {type(path).__name__}',
            next_action='传入 Path 或字符串路径',
        )
    file = Path(path)
    suffix = file.suffix.lower()
    if suffix not in {'.yaml', '.yml', '.json'} or not file.is_file():
        raise ConfigError(
            expected='场景路径指向存在的 .yaml / .yml / .json 文件',
            received=f'收到 {str(file)!r}',
            next_action='创建场景 YAML，或把路径改成已有的 .yaml / .yml / .json 文件',
        )
    try:
        text = file.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        raise ConfigError(
            expected='场景文件可按 UTF-8 读取',
            received=f'读取 {str(file)!r} 失败：{type(exc).__name__}: {exc}',
            next_action='检查文件权限、路径与编码后重试；场景必须保存为 UTF-8',
        ) from exc
    value = _json(text, file) if suffix == '.json' else _yaml(text, file)
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


def _yaml(text: str, file: Path) -> object:
    try:
        return cast(object, yaml.load(text, Loader=_UniqueSafeLoader))
    except _AliasRefused as exc:
        raise ConfigError(
            expected='场景文档不用 YAML 锚点的别名',
            received=f'{str(file)!r}：{exc}',
            next_action='把 *别名 换成写出来的值：别名能造出成环或成倍展开的结构',
        ) from exc
    except RecursionError as exc:
        raise ConfigError(
            expected='场景文档的嵌套深度在解析器能处理的范围之内',
            received=f'{str(file)!r} 嵌套太深，解析时超出了递归上限',
            next_action='把深层结构换成字符串，或拆开；捕获的文档不会这么深',
        ) from exc
    except yaml.YAMLError as exc:
        raise ConfigError(
            expected='场景是单文档 safe YAML Mapping，且每个映射键只出现一次',
            received=f'{str(file)!r} 解析失败：{exc}',
            next_action='修正 YAML 语法或重复键；不要使用 !!python/object 等非 safe 标签',
        ) from exc


def _finite(text: str) -> float:
    """`1e400` 这样的字面量不经过 `parse_constant`，按普通小数读出来就是无穷大。"""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f'数 {text} 超出了浮点数的范围')
    return value


def _no_constant(name: str) -> object:
    raise ValueError(f'{name} 不是 JSON 值')


def _object_at(
    text_and_end: tuple[str, int],
    strict: bool,
    scan_once: Any,
    object_hook: Any,
    object_pairs_hook: Any,
    memo: Any = None,
) -> Any:
    """一个 JSON 对象，键重复时报出它在第几行第几列开始：一份捕获里有几百个 `output`，只报键名
    找不到是哪一个。"""
    text, end = text_and_end

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        found: dict[str, object] = {}
        for key, value in pairs:
            if key in found:
                line = text.count('\n', 0, end) + 1
                column = end - text.rfind('\n', 0, end)
                raise ValueError(
                    f'第 {line} 行第 {column} 列开始的对象里，键 {key!r} 出现了不止一次'
                )
            found[key] = value
        return found

    return _decoder.JSONObject(text_and_end, strict, scan_once, object_hook, unique, memo)


def _json(text: str, file: Path) -> object:
    decoder = cast(Any, json.JSONDecoder(parse_float=_finite, parse_constant=_no_constant))
    decoder.parse_object = _object_at
    # 纯 Python 的扫描器才会调 `parse_object`：C 实现自己解析对象，换不进去。
    decoder.scan_once = _scanner.py_make_scanner(decoder)
    try:
        return cast(object, decoder.decode(text))
    except RecursionError as exc:
        raise ConfigError(
            expected='场景文档的嵌套深度在解析器能处理的范围之内',
            received=f'{str(file)!r} 嵌套太深，解析时超出了递归上限',
            next_action='把深层结构换成字符串，或拆开；捕获的文档不会这么深',
        ) from exc
    except ValueError as exc:
        raise ConfigError(
            expected='场景是一个 JSON 对象，每个键只出现一次，数都是有限的，没有 NaN / Infinity',
            received=f'{str(file)!r} 解析失败：{exc}',
            next_action='修正 JSON 语法或重复键；捕获的文档不要手改成别的格式再存回 .json',
        ) from exc
