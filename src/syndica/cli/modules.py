"""Module 的发现与启用（M4 D15）。

发现只读包元数据（入口点名、目标、发行版名与版本），**不 import**：没启用的 Module 必须不可见，
import 就是在跑它的代码。入口点的来源可注入，组合根接上 `importlib.metadata`，用例不碰真实环境。
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from syndica import __version__
from syndica.kernel import ConfigError
from syndica.kernel.hooks.junctions import MODULE_NAME

__all__ = [
    'DEVELOPMENT',
    'ENTRY_POINT_GROUP',
    'MANDATORY',
    'OPTIONAL_BUILTINS',
    'Discovered',
    'EntryPointSource',
    'ModuleRow',
    'discover',
    'installed_entry_points',
    'module_rows',
    'no_entry_points',
    'validated_modules',
]

ENTRY_POINT_GROUP = 'syndica.modules'
# 宿主每次都装、排在最前的。
MANDATORY: tuple[str, ...] = ('safety', 'human-interaction')
# 配置里点名才装的内置 Module。
OPTIONAL_BUILTINS: tuple[str, ...] = ('retry', 'prefix-drift')
# 命令行决定的开发期 Module：`--no-write` 装 read-only，dev 每次都装、排在最后。
DEVELOPMENT: tuple[str, ...] = ('read-only', 'dev')
_BUILTINS = frozenset((*MANDATORY, *OPTIONAL_BUILTINS, *DEVELOPMENT))


class _Distribution(Protocol):
    @property
    def metadata(self) -> object: ...

    @property
    def version(self) -> str: ...


class _EntryPoint(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def value(self) -> str: ...

    @property
    def dist(self) -> _Distribution | None: ...


EntryPointSource = Callable[[], Iterable[_EntryPoint]]


def installed_entry_points() -> Iterable[_EntryPoint]:
    """当前环境里 `syndica.modules` 组的入口点。只读元数据。"""
    return cast(Iterable[_EntryPoint], importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))


def no_entry_points() -> Iterable[_EntryPoint]:
    return ()


@dataclass(frozen=True)
class Discovered:
    """一个第三方 Module 的元数据。`problem` 不为 None 时它装不上，说明为什么。"""

    name: str
    target: str
    distribution: str | None
    version: str | None
    problem: str | None = None


def _distribution(entry: _EntryPoint) -> tuple[str | None, str | None]:
    dist = entry.dist
    if dist is None:
        return None, None
    metadata = dist.metadata
    getter = getattr(metadata, 'get', None)
    name = getter('Name') if callable(getter) else None
    return (name if isinstance(name, str) else None), dist.version


def discover(source: EntryPointSource) -> tuple[Discovered, ...]:
    """按名字排序的发现结果。与内置 Module 重名、名字不合拼法、两个发行版提供同一个名字的，都标成
    装不上，而不是挑一个：挑哪个取决于安装顺序，同一份配置在两台机器上会装上不同的代码。"""
    found: dict[str, list[Discovered]] = {}
    for entry in source():
        distribution, version = _distribution(entry)
        found.setdefault(entry.name, []).append(
            Discovered(entry.name, entry.value, distribution, version)
        )
    results: list[Discovered] = []
    for name in sorted(found):
        entries = found[name]
        problem: str | None = None
        if name in _BUILTINS:
            problem = '与内置 Module 重名，不收'
        elif MODULE_NAME.fullmatch(name) is None:
            problem = '入口点名不是 Module 名的规范拼法（小写字母开头、单个连字符分段）'
        elif len(entries) > 1:
            sources = sorted(f'{entry.distribution} {entry.version}' for entry in entries)
            problem = f'有歧义：{sources} 都提供了这个名字'
        results.extend(
            Discovered(entry.name, entry.target, entry.distribution, entry.version, problem)
            for entry in entries
        )
    return tuple(results)


def validated_modules(names: Sequence[str], discovered: Sequence[Discovered]) -> tuple[str, ...]:
    """配置里 `modules:` 点名的、这个版本能装的 Module。"""
    third_party = {entry.name: entry for entry in discovered}
    for name in names:
        if name in OPTIONAL_BUILTINS:
            continue
        if name in MANDATORY:
            raise ConfigError(
                expected='modules 只列可选的 Module',
                received=f'{name!r} 是宿主必装的，总是最先装上',
                next_action=f'从 modules 里删掉 {name!r}',
            )
        if name in DEVELOPMENT:
            raise ConfigError(
                expected='modules 只列可选的 Module',
                received=f'{name!r} 是开发期的 Module，由命令行决定装不装',
                next_action=f'从 modules 里删掉 {name!r}；只读 run 用 --no-write',
            )
        entry = third_party.get(name)
        if entry is not None and entry.problem is not None:
            raise ConfigError(
                expected=f'modules 里的 {name!r} 装得上',
                received=f'{name!r} 装不上：{entry.problem}',
                next_action='运行 syndica modules list 查看来源；卸掉冲突的发行版或换一个名字',
            )
        if entry is not None:
            raise ConfigError(
                expected='这个版本只能启用内置的可选 Module',
                received=f'{name!r} 是第三方 Module（{entry.distribution} {entry.version}）',
                next_action=(
                    f'先从 modules 里删掉 {name!r}；启用第三方 Module 要先有项目配置的信任确认'
                ),
            )
        raise ConfigError(
            expected=(
                f'modules 里的名字是可选的内置 Module {list(OPTIONAL_BUILTINS)}，'
                '或已安装的第三方 Module'
            ),
            received=f'没有名为 {name!r} 的 Module',
            next_action='运行 syndica modules list 查看能启用的 Module，再逐字写它的名字',
        )
    return tuple(names)


@dataclass(frozen=True)
class ModuleRow:
    """`modules list` 的一行：名字、来源、状态。"""

    name: str
    source: str
    status: str


def module_rows(enabled: Sequence[str], discovered: Sequence[Discovered]) -> tuple[ModuleRow, ...]:
    """内置的按装配次序在前，第三方的按名字在后。只读元数据，不 import。"""
    builtin = f'内置 {__version__}'
    rows = [ModuleRow(name, builtin, '必装') for name in MANDATORY]
    rows.extend(
        ModuleRow(name, builtin, '已启用' if name in enabled else '可启用')
        for name in OPTIONAL_BUILTINS
    )
    rows.extend(ModuleRow(name, builtin, '开发期（命令行决定）') for name in DEVELOPMENT)
    for entry in discovered:
        source = (
            '来源不明'
            if entry.distribution is None
            else f'{entry.distribution} {entry.version or "版本不明"}'
        )
        if entry.problem is not None:
            status = f'坏的：{entry.problem}'
        elif entry.name in enabled:
            status = '已启用'
        else:
            status = '可启用'
        rows.append(ModuleRow(entry.name, source, status))
    return tuple(rows)
