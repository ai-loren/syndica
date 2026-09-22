"""四个内置角色。`register_builtins` 可重复调用：第二次是同名覆盖，留下 OverrideRecord。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from syndica.kernel.agents.orchestrator import Orchestrator, impl_path
from syndica.kernel.catalog import AgentCatalog
from syndica.kernel.errors import ConfigError

__all__ = ['Orchestrator', 'register_builtins']

_DECLARATIVE: tuple[str, ...] = ('planner.json', 'explorer.json', 'worker.json')
_SUBAGENTS: tuple[str, ...] = ('Planner', 'Explorer', 'Worker')


def register_builtins(catalog: object) -> None:
    """先装三个声明式角色，再装 Orchestrator。顺序反了，freeze 会因 subagents 缺失失败。"""
    if not isinstance(catalog, AgentCatalog):
        raise ConfigError(
            expected='catalog 是 AgentCatalog',
            received=f'收到 {type(catalog).__name__}',
            next_action='传入 AgentCatalog()，不要传带同名方法的别的对象',
        )
    directory = Path(__file__).parent
    for filename in _DECLARATIVE:
        loaded = json.loads((directory / filename).read_text(encoding='utf-8'))
        if not isinstance(loaded, dict):
            raise ConfigError(
                expected=f'{filename} 是带 name 的 JSON 对象',
                received=f'收到 {type(loaded).__name__}',
                next_action='内置 JSON 必须是对象，且有字符串 name',
            )
        raw: dict[str, object] = {}
        for key, value in cast('dict[object, object]', loaded).items():
            if not isinstance(key, str):
                raise ConfigError(
                    expected=f'{filename} 的键都是 str',
                    received=f'键 {key!r} 的类型是 {type(key).__name__}',
                    next_action='内置 JSON 只用字符串键',
                )
            raw[key] = value
        name = raw.get('name')
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(
                expected=f'{filename} 有非空白字符串 name',
                received=f'name={name!r}',
                next_action='内置 JSON 必须有字符串 name',
            )
        catalog.register_builtin(raw, location=name)
    catalog.register_builtin(
        {
            'name': 'Orchestrator',
            'description': '把工作拆给 Planner、Explorer、Worker，自己不改文件',
            'impl': impl_path(),
            'subagents': list(_SUBAGENTS),
        },
        location='Orchestrator',
    )
