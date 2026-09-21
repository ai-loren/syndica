"""工具目录：按清单名字取出 Tool，登记时钉住三条身份轴。

`register_tool` 是往目录插入的唯一正式入口。直接 `table[k]=tool` 绕过三轴检查；
`resolve_tool_names` 对产出立刻跑 `validate_tool_names`，生插分叉在解析出口被拦。
产出 dict 是目录的浅副本：改返回值或改目录互不影响。
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from typing import cast

from syndica.kernel.errors import ConfigError
from syndica.kernel.tool import Tool, tool_name_disagreement, validate_tool_names

__all__ = ['register_tool', 'resolve_tool_names']


def register_tool(table: object, tool: object) -> None:
    """按 `tool.name` 插入。与 schema['name'] 不一致则拒绝，表保持原样。"""
    if not isinstance(table, MutableMapping):
        raise ConfigError(
            expected='table 是可变的「工具名 -> Tool」映射；register_tool 是唯一正式插入口',
            received=f'收到 {type(table).__name__} 类型的 {table!r}',
            next_action='传入一个 dict，不要用 MappingProxyType 或 list',
        )
    if not isinstance(tool, Tool):
        raise ConfigError(
            expected='第二个参数是 Tool 实例',
            received=f'收到 {tool!r}，类型是 {type(tool).__name__}',
            next_action='传入 Tool 子类的实例，不要传类对象或工具名字符串',
        )
    key = tool.name
    error = tool_name_disagreement(key, tool)
    if error is not None:
        raise error
    table[key] = tool


def resolve_tool_names(names: object, tools: object) -> dict[str, Tool]:
    """按清单顺序从目录取出 Tool。缺的每一个名字都点名，键序与清单一致。"""
    if isinstance(names, (str, bytes)) or not isinstance(names, Sequence):
        raise ConfigError(
            expected='names 是工具名的序列（list / tuple），顺序即清单顺序、进请求前缀',
            received=f'收到 {type(names).__name__} 类型的 {names!r}',
            next_action=('传入清单里的 tools 字段（名字元组）。不要把 Tool 映射直接当名字列表'),
        )
    if isinstance(tools, (str, bytes)) or not isinstance(tools, Mapping):
        raise ConfigError(
            expected='tools 是工具目录：「工具名 -> Tool」的映射',
            received=f'收到 {type(tools).__name__} 类型的 {tools!r}，它没有 items()',
            next_action='传入一个 dict；往目录插入只用 register_tool',
        )
    wanted: list[str] = []
    for raw in cast(Sequence[object], names):
        if not isinstance(raw, str) or not raw.strip():
            raise ConfigError(
                expected='names 的每个元素是非空白工具名 str',
                received=f'元素 {raw!r}，类型是 {type(raw).__name__}',
                next_action='改成非空白字符串；空白名寻址不到任何工具',
            )
        wanted.append(raw)
    catalog = cast(Mapping[object, object], tools)
    missing = [name for name in wanted if name not in catalog]
    if missing:
        raise ConfigError(
            expected=f'工具目录含清单点名的每一个工具：{wanted}',
            received=f'目录里没有 {missing!r}',
            next_action=(
                f'用 register_tool 把缺的 {missing} 登记进工具目录，'
                '或从清单 tools 里删掉这些名字。'
                '不要内置空壳工具装样子——目录里没有就是没有'
            ),
        )
    resolved: dict[str, Tool] = {}
    for name in wanted:
        item = catalog[name]
        if not isinstance(item, Tool):
            raise ConfigError(
                expected=f'目录键 {name!r} 的取值是 Tool 实例',
                received=f'收到 {item!r}，类型是 {type(item).__name__}',
                next_action='用 register_tool 登记 Tool 实例，不要把名字字符串当值',
            )
        resolved[name] = item
    validate_tool_names(resolved)
    return resolved
