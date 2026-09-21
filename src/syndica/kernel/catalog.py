"""工具目录与 Agent 目录。

工具侧：`register_tool` 是往目录插入的唯一正式入口。直接 `table[k]=tool` 绕过三轴检查；
`resolve_tool_names` 对产出立刻跑 `validate_tool_names`，生插分叉在解析出口被拦。
产出 dict 是目录的浅副本：改返回值或改目录互不影响。

Agent 侧：`AgentCatalog` 加载清单、同名覆盖可查询。覆盖不改 `names()` 第一次见到的顺序。
同一次 `load_json_directory` 内部撞名不是覆盖，整次加载都不写入。freeze 不在本模块这一层。
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from syndica.kernel.agent import BaseAgent
from syndica.kernel.errors import ConfigError
from syndica.kernel.manifest import AgentManifest, ManifestSource, parse_mapping
from syndica.kernel.tool import Tool, tool_name_disagreement, validate_tool_names

__all__ = [
    'AgentCatalog',
    'OverrideRecord',
    'register_tool',
    'resolve_tool_names',
]


@dataclass(frozen=True)
class OverrideRecord:
    """一次同名覆盖留下的快照。`overrides(name)` 从旧到新，从未覆盖则空元组。

    `displaced_at_index` 是该名字第几次被挤掉（从 0 起），不是 `names()` 下标——
    第一次见到的顺序覆盖之后也不改。
    """

    name: str
    displaced_source: ManifestSource
    displaced_at_index: int
    by_source: ManifestSource


class AgentCatalog:
    """角色清单目录：加载源、同名覆盖可查询、按第一次见到的顺序报名字。"""

    def __init__(self) -> None:
        self._entries: dict[str, AgentManifest] = {}
        self._order: list[str] = []
        self._overrides: dict[str, list[OverrideRecord]] = {}

    def load_mapping(self, data: object, *, source: object = None) -> AgentManifest:
        """收下已经解析的 Mapping。YAML → Mapping 是宿主 / 计划 6 的事。"""
        if source is None:
            snapshot = ManifestSource(kind='mapping', location='<mapping>')
        elif isinstance(source, ManifestSource):
            snapshot = source
        else:
            raise ConfigError(
                expected='source 是 ManifestSource 快照（kind + location）',
                received=f'收到 {type(source).__name__} 类型的 {source!r}',
                next_action='构造 ManifestSource(kind=..., location=...) 再传入，不要传活的 Path',
            )
        manifest = parse_mapping(data, source=snapshot)
        self._put(manifest)
        return manifest

    def load_json_file(self, path: object) -> AgentManifest:
        """读一个 JSON 对象文件。`origin` 是父目录，给 system_prompt 相对路径用。"""
        file = _require_file(path)
        origin = str(file.parent.resolve())
        manifest = _manifest_from_json_file(file, kind='json_file', origin=origin)
        self._put(manifest)
        return manifest

    def load_json_directory(self, path: object) -> tuple[AgentManifest, ...]:
        """只收该目录下一层 `*.json`，不递归。同一次内部撞名整批拒绝，目录里已有的条目不动。"""
        directory = _require_directory(path)
        origin = str(directory.resolve())
        files = [
            candidate
            for candidate in sorted(directory.iterdir(), key=lambda item: item.name)
            if candidate.is_file() and candidate.suffix == '.json'
        ]
        parsed: list[AgentManifest] = []
        for file in files:
            parsed.append(_manifest_from_json_file(file, kind='json_directory', origin=origin))
        _reject_duplicate_names_in_one_directory(parsed, directory=directory)
        for manifest in parsed:
            self._put(manifest)
        return tuple(parsed)

    def register_class(
        self,
        name: object,
        cls: object,
        *,
        source: object = None,
    ) -> AgentManifest:
        """按名字登记一个 BaseAgent 子类。description 取该类 docstring 首行。"""
        role = _role_name(name)
        agent_cls = _base_agent_subclass(cls)
        if source is None:
            snapshot = ManifestSource(
                kind='class',
                location=f'{agent_cls.__module__}:{agent_cls.__qualname__}',
            )
        elif not isinstance(source, ManifestSource):
            raise ConfigError(
                expected='source 是 ManifestSource 快照（kind + location）',
                received=f'收到 {type(source).__name__} 类型的 {source!r}',
                next_action='构造 ManifestSource(kind="class", location=...) 再传入',
            )
        else:
            snapshot = source
        manifest = AgentManifest(
            name=role,
            description=_class_description(agent_cls),
            impl=agent_cls,
            source=snapshot,
        )
        self._put(manifest)
        return manifest

    def register_builtin(self, data: object, *, location: object) -> AgentManifest:
        """以内置源登记一份清单。location 是内置角色的稳定标签，不是路径。"""
        if not isinstance(location, str) or not location.strip():
            raise ConfigError(
                expected='location 是非空白 str，标识这份内置清单',
                received=f'location={location!r}，类型是 {type(location).__name__}',
                next_action='传入内置角色标签，例如 "Planner"',
            )
        return self.load_mapping(data, source=ManifestSource(kind='builtin', location=location))

    def get(self, name: object) -> AgentManifest:
        """当前生效的清单。没有这个名字就报，不要返回 None。"""
        role = _role_name(name)
        manifest = self._entries.get(role)
        if manifest is None:
            known = list(self._order)
            raise ConfigError(
                expected=f'目录里有名为 {role!r} 的清单',
                received=f'目录现有 {known!r}，没有 {role!r}',
                next_action=(
                    f'用 load_mapping / load_json_file / load_json_directory / '
                    f'register_class / register_builtin 登记 {role!r}，'
                    '或核对拼写。names() 给出第一次见到的顺序'
                ),
            )
        return manifest

    def names(self) -> tuple[str, ...]:
        """第一次见到该名的顺序。覆盖不把名字挪到末尾。"""
        return tuple(self._order)

    def overrides(self, name: object) -> tuple[OverrideRecord, ...]:
        """该名被挤掉的每一份，从旧到新。从未覆盖或从未见过则 `()`。"""
        role = _role_name(name)
        return tuple(self._overrides.get(role, ()))

    def source_of(self, name: object) -> ManifestSource:
        """当前生效条目的 source。"""
        return self.get(name).source

    def _put(self, manifest: AgentManifest) -> None:
        name = manifest.name
        current = self._entries.get(name)
        if current is not None:
            chain = self._overrides.setdefault(name, [])
            chain.append(
                OverrideRecord(
                    name=name,
                    displaced_source=current.source,
                    displaced_at_index=len(chain),
                    by_source=manifest.source,
                )
            )
            self._entries[name] = manifest
            return
        self._entries[name] = manifest
        self._order.append(name)


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


def _role_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(
            expected='角色名是非空白 str',
            received=f'收到 {name!r}，类型是 {type(name).__name__}',
            next_action='传入清单里的 name，不要传空串或非字符串',
        )
    return name


def _base_agent_subclass(cls: object) -> type[BaseAgent]:
    if isinstance(cls, type) and issubclass(cls, BaseAgent):
        return cls
    received = (
        f'收到实例 {cls!r}，类型是 {type(cls).__name__}'
        if not isinstance(cls, type)
        else f'收到 {cls.__name__}，它不是 BaseAgent 的子类'
    )
    raise ConfigError(
        expected='第二个参数是 BaseAgent 的子类（类对象，不是实例）',
        received=received,
        next_action='传入 class Planner(BaseAgent): ... 这样的类，不要传入实例或无关 type',
    )


def _class_description(cls: type[BaseAgent]) -> str:
    doc = cls.__doc__
    if not isinstance(doc, str) or not doc.strip():
        raise ConfigError(
            expected='register_class 的类有非空白 docstring，作为清单 description',
            received=f'{cls.__name__}.__doc__={doc!r}',
            next_action=(
                '给类写一行「什么时候用我」的 docstring，'
                '或改用 load_mapping 自己填 description 和 impl'
            ),
        )
    return doc.strip().splitlines()[0].strip()


def _as_path(path: object) -> Path:
    if isinstance(path, Path):
        return path
    if isinstance(path, str) and path.strip():
        return Path(path)
    raise ConfigError(
        expected='path 是现存文件或目录的 Path / 非空白 str',
        received=f'收到 {type(path).__name__} 类型的 {path!r}',
        next_action='传入路径字符串或 pathlib.Path，不要传 bytes 或 None',
    )


def _require_file(path: object) -> Path:
    candidate = _as_path(path)
    if not candidate.is_file():
        raise ConfigError(
            expected=f'{candidate} 是一个 JSON 文件',
            received=('它是目录' if candidate.is_dir() else '它不是现存文件'),
            next_action='传入单个 .json 文件；整目录用 load_json_directory',
        )
    return candidate


def _require_directory(path: object) -> Path:
    candidate = _as_path(path)
    if not candidate.is_dir():
        raise ConfigError(
            expected=f'{candidate} 是一个目录',
            received=('它是文件' if candidate.is_file() else '它不是现存目录'),
            next_action='传入目录路径；单个文件用 load_json_file',
        )
    return candidate


def _read_json(path: Path) -> object:
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as exc:
        raise ConfigError(
            expected=f'{path} 可读且是 utf-8 文本',
            received=f'读失败：{exc}',
            next_action='修正权限或编码后再 load',
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            expected=f'{path} 是一个 JSON 对象（清单字段的映射）',
            received=f'JSON 解析失败：{exc}',
            next_action='改成单个 JSON 对象，不要交 YAML 或清单数组',
        ) from exc


def _manifest_from_json_file(
    path: Path,
    *,
    kind: Literal['json_file', 'json_directory'],
    origin: str,
) -> AgentManifest:
    data = _read_json(path)
    source = ManifestSource(kind=kind, location=str(path.resolve()), origin=origin)
    return parse_mapping(data, source=source)


def _reject_duplicate_names_in_one_directory(
    parsed: Sequence[AgentManifest], *, directory: Path
) -> None:
    grouped: dict[str, list[str]] = defaultdict(list)
    for manifest in parsed:
        grouped[manifest.name].append(manifest.source.location)
    conflicts = {name: locations for name, locations in grouped.items() if len(locations) > 1}
    if not conflicts:
        return
    raise ConfigError(
        expected='同一目录里每个清单的 name 唯一；一次 load_json_directory 内部撞名不是覆盖',
        received=f'目录 {directory.resolve()} 里同名文件 {dict(conflicts)!r}',
        next_action=(
            '改其中一个文件的 name，或把它们放到不同目录分两次加载——'
            '后一次加载才是覆盖，overrides() 才能查出被挤掉的那份'
        ),
    )
