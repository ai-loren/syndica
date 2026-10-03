"""Module 的发现与启用（M4 D15）。

发现只读包元数据（入口点名、目标、发行版名与版本），**不 import**：没启用的 Module 必须不可见，
import 就是在跑它的代码。入口点的来源可注入，组合根接上 `importlib.metadata`，用例不碰真实环境。
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, cast

from syndica import __version__
from syndica.kernel import ConfigError, ModuleHost, RuntimeModule
from syndica.kernel.hooks.junctions import MODULE_NAME
from syndica.kernel.redaction import Redactor, looks_secret

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
    'load_module',
    'module_rows',
    'no_entry_points',
    'third_party',
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

    def load(self) -> object: ...


EntryPointSource = Callable[[], Iterable[_EntryPoint]]


def installed_entry_points() -> Iterable[_EntryPoint]:
    """当前环境里 `syndica.modules` 组的入口点。只读元数据。"""
    return cast(Iterable[_EntryPoint], importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))


def no_entry_points() -> Iterable[_EntryPoint]:
    return ()


@dataclass(frozen=True)
class Discovered:
    """一个第三方 Module 的元数据。`problem` 不为 None 时它装不上，说明为什么。`load` 是
    import 它的入口点：只在配置受信任、且点名要用它时才调。"""

    name: str
    target: str
    distribution: str | None
    version: str | None
    problem: str | None = None
    load: Callable[[], object] | None = field(default=None, compare=False, repr=False)
    dist: object | None = field(default=None, compare=False, repr=False)


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
            Discovered(
                entry.name,
                entry.value,
                distribution,
                version,
                load=cast('Callable[[], object] | None', getattr(entry, 'load', None)),
                dist=entry.dist,
            )
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
            Discovered(
                entry.name,
                entry.target,
                entry.distribution,
                entry.version,
                problem,
                load=entry.load,
                dist=entry.dist,
            )
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
            continue
        raise ConfigError(
            expected=(
                f'modules 里的名字是可选的内置 Module {list(OPTIONAL_BUILTINS)}，'
                '或已安装的第三方 Module'
            ),
            received=f'没有名为 {name!r} 的 Module',
            next_action='运行 syndica modules list 查看能启用的 Module，再逐字写它的名字',
        )
    return tuple(names)


def third_party(names: Sequence[str]) -> tuple[str, ...]:
    """`modules:` 里的第三方 Module（内置的可选 Module 之外的那些），按写的次序。"""
    return tuple(name for name in names if name not in OPTIONAL_BUILTINS)


def _load_failed(
    entry: Discovered, what: str, exc: BaseException, redactor: Redactor
) -> ConfigError:
    reason = redactor.redact(f'{type(exc).__name__}: {exc}')
    return ConfigError(
        expected=f'第三方 Module {entry.name!r} 的入口点 {entry.target} {what}',
        received=f'失败：{reason}',
        next_action=(
            f'检查发行版 {entry.distribution} {entry.version} 是否装好、与这个 syndica 兼容；'
            f'暂时用不上就从 modules 里删掉 {entry.name!r}'
        ),
    )


def load_module(entry: Discovered, host: ModuleHost, redactor: Redactor) -> RuntimeModule:
    """import 一个启用的第三方 Module 并用 `ModuleHost` 调它的入口点。只在配置受信任之后调：
    import 就是在跑它的代码。失败的原因过宿主的脱敏器再显示——它是第三方代码写的文本。"""
    if entry.load is None:
        raise ConfigError(
            expected=f'第三方 Module {entry.name!r} 有可加载的入口点',
            received='发现结果里没有加载它的办法',
            next_action='重新安装提供它的发行版，或从 modules 里删掉它',
        )
    try:
        factory = entry.load()
    except Exception as exc:
        raise _load_failed(entry, '能 import', exc, redactor) from exc
    if not callable(factory):
        raise ConfigError(
            expected=f'入口点 {entry.target} 是一个收 ModuleHost 的可调用对象',
            received=f'它是 {type(factory).__name__}',
            next_action='让入口点指向 factory(host: ModuleHost) -> RuntimeModule',
        )
    try:
        module: object = factory(host)
    except Exception as exc:
        raise _load_failed(entry, '用 ModuleHost 调用时交回 Module', exc, redactor) from exc
    if not isinstance(module, RuntimeModule):
        raise ConfigError(
            expected=f'入口点 {entry.target} 交回一个 RuntimeModule',
            received=f'交回的是 {type(module).__name__}',
            next_action='让工厂 return RuntimeModule(...)',
        )
    if module.name != entry.name:
        raise ConfigError(
            expected=f'入口点名 {entry.name!r} 等于它交回的 Module 的名字',
            received=f'Module 叫 {module.name!r}',
            next_action='把入口点名或 Module 名改成一样；配置按入口点名启用，trace 按 Module 名记',
        )
    _check_flag_keys(module)
    return module


def _check_flag_keys(module: RuntimeModule) -> None:
    """第三方 Flag 的键带着 Module 名做前缀，且不像密钥：Flag 的值会写进宿主档案与回放文档，
    凭证要从环境来。"""
    prefix = module.name.replace('-', '_') + '_'
    for declaration in module.flags:
        if not declaration.key.startswith(prefix):
            raise ConfigError(
                expected=f'Module {module.name!r} 的 Flag 键以 {prefix!r} 开头',
                received=f'它声明了 {declaration.key!r}',
                next_action=f'改成 {prefix}{declaration.key}；前缀让两个 Module 的 Flag 撞不了名',
            )
        if looks_secret(declaration.key):
            raise ConfigError(
                expected=f'Module {module.name!r} 的 Flag 键不像密钥名',
                received=f'它声明了 {declaration.key!r}',
                next_action='凭证从环境变量读，不进 Flag：Flag 的值会写进宿主档案与回放文档',
            )


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
