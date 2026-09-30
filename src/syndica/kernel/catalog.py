"""工具目录与 Agent 目录。

工具侧：`register_tool` 是往目录插入的唯一正式入口。直接 `table[k]=tool` 绕过三轴检查；
`resolve_tool_names` 对产出立刻跑 `validate_tool_names`，生插分叉在解析出口被拦。
产出 dict 是目录的浅副本：改返回值或改目录互不影响。

Agent 侧：`AgentCatalog` 加载清单、同名覆盖可查询。覆盖不改 `names()` 第一次见到的顺序。
同一次 `load_json_directory` 内部撞名不是覆盖，整次加载都不写入。
`freeze` 在这一层：插值一次、按名字解析工具和模型、把清单收成 `AgentFactory`。
工厂捕获工具表的副本，freeze 之后改目录进不了已冻结的工厂。
"""

from __future__ import annotations

import importlib
import json
from collections import defaultdict
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.danger import DangerJudge, DangerPolicy, validate_danger_options
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import LLM
from syndica.kernel.manifest import AgentManifest, ManifestSource, parse_mapping
from syndica.kernel.runtime import AgentFactory, validate_agent_factories
from syndica.kernel.tool import Tool, tool_name_disagreement, validate_tool_names

__all__ = [
    'AgentCatalog',
    'HostFacts',
    'OverrideRecord',
    'register_tool',
    'resolve_tool_names',
]

_PROMPT_PLACEHOLDERS: tuple[str, ...] = ('os', 'platform', 'shell')
_DEFAULT_MODEL_NAME = 'default'
_DEFAULT_MAX_TURNS = 50


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


@dataclass(frozen=True)
class HostFacts:
    """宿主注入的静态环境事实。内核不读 os.environ / os.getcwd。

    只有 os / platform / shell 能进 System Prompt。工作目录是动态事实，进历史块；
    `{cwd}` 即使宿主后来知道 cwd 也拒绝。
    """

    os: str
    platform: str
    shell: str

    def __post_init__(self) -> None:
        for field_name in _PROMPT_PLACEHOLDERS:
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(
                    expected=f'HostFacts.{field_name} 是非空白 str',
                    received=f'{field_name}={value!r}，类型是 {type(value).__name__}',
                    next_action='由宿主注入真实的 os / platform / shell；内核不会自己读环境',
                )


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

    def freeze(
        self,
        tools: object,
        models: object,
        host: object = None,
        *,
        danger_judge: DangerJudge | None = None,
        danger_policy: DangerPolicy | None = None,
    ) -> Mapping[str, AgentFactory]:
        """插值一次并收成「hooks, events → BaseAgent」工厂。键序与 names() 相同。

        `danger_judge` / `danger_policy` 交给每个建出的 Agent，类式角色也一样；`None` 用内核默认。
        入口先校验：目录里一个角色都没有时不会建任何 `AgentOptions`，错的策略就没有别处能拦下。
        """
        validate_danger_options(danger_judge, danger_policy)
        host_facts = _host_facts(host)
        model_table = _model_table(models)
        self._reject_missing_models(model_table)
        self._reject_missing_subagents()
        factories: dict[str, AgentFactory] = {}
        for name in self._order:
            factories[name] = self._factory_for(
                self._entries[name],
                tools=tools,
                models=model_table,
                host=host_facts,
                danger_judge=danger_judge,
                danger_policy=danger_policy,
            )
        validate_agent_factories(factories)
        return MappingProxyType(factories)

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

    def _factory_for(
        self,
        manifest: AgentManifest,
        *,
        tools: object,
        models: Mapping[str, LLM],
        host: HostFacts | None,
        danger_judge: DangerJudge | None,
        danger_policy: DangerPolicy | None,
    ) -> AgentFactory:
        cls = self._resolve_class(manifest)
        tool_names = self._union_tool_names(manifest)
        captured_tools = dict(resolve_tool_names(tool_names, tools))
        prompt = _role_prompt(
            cls,
            self,
            _interpolate_prompt(manifest.system_prompt, host, role=manifest.name),
            role=manifest.name,
        )
        options = AgentOptions(
            name=manifest.name,
            llm=models[manifest.model],
            tools=captured_tools,
            system_prompt=prompt,
            max_turns=_max_turns(manifest),
            danger_judge=danger_judge,
            danger_policy=danger_policy,
        )

        def factory(hooks: HooksCenter, events: EventBus) -> BaseAgent:
            return cls(options, hooks=hooks, events=events)

        return factory

    def _resolve_class(self, manifest: AgentManifest) -> type[BaseAgent]:
        ancestors = self._walk_extends(manifest)
        if isinstance(manifest.impl, type):
            return _base_agent_subclass(manifest.impl)
        if isinstance(manifest.impl, str):
            return _import_impl(manifest.impl)
        if ancestors:
            parent = ancestors[0]
            if isinstance(parent.impl, type):
                return _base_agent_subclass(parent.impl)
            if isinstance(parent.impl, str):
                return _import_impl(parent.impl)
        return BaseAgent

    def _walk_extends(self, start: AgentManifest) -> list[AgentManifest]:
        chain: list[AgentManifest] = []
        seen: set[str] = {start.name}
        path: list[str] = [start.name]
        current_name = start.extends
        while current_name:
            if current_name in seen:
                cycle = [*path, current_name]
                raise ConfigError(
                    expected='extends 链是从声明式指向已登记的类式角色，不能成环',
                    received=f'{start.name!r} 的 extends 链成环：{cycle}',
                    next_action='打断环：每个声明式角色只 extends 一个已经 register_class 的名字',
                )
            parent = self._entries.get(current_name)
            if parent is None:
                known = list(self._order)
                raise ConfigError(
                    expected=f'{start.name!r} 的 extends={current_name!r} 已在目录里',
                    received=f'目录现有 {known!r}，没有 {current_name!r}',
                    next_action=(
                        f'先 load / register_class 登记 {current_name!r}，再 freeze。'
                        'extends 必须指向已经登记的类式角色'
                    ),
                )
            if parent.impl is None:
                raise ConfigError(
                    expected='extends 只能指向已经登记的类式角色（impl 是 class 或 module:Class）',
                    received=(
                        f'{start.name!r} extends {current_name!r}，'
                        f'而 {current_name!r} 是声明式（impl 为空）'
                    ),
                    next_action=(
                        f'把 {current_name!r} 改成类式（register_class 或 impl 指向'
                        ' BaseAgent 子类），或去掉这条 extends。声明式不能 extends 声明式'
                    ),
                )
            seen.add(current_name)
            path.append(current_name)
            chain.append(parent)
            current_name = parent.extends
        return chain

    def _union_tool_names(self, manifest: AgentManifest) -> tuple[str, ...]:
        ordered: list[str] = []
        seen: set[str] = set()
        for node in (manifest, *self._walk_extends(manifest)):
            for name in node.tools:
                if name not in seen:
                    ordered.append(name)
                    seen.add(name)
        return tuple(ordered)

    def _reject_missing_models(self, models: Mapping[str, LLM]) -> None:
        missing: list[str] = []
        roles: list[str] = []
        seen: set[str] = set()
        for name in self._order:
            model_name = self._entries[name].model
            if model_name in models:
                continue
            roles.append(name)
            if model_name not in seen:
                seen.add(model_name)
                missing.append(model_name)
        if not missing:
            return
        present = list(models)
        raise ConfigError(
            expected=f'模型表含清单点名的每一个模型：缺 {missing}',
            received=f'模型表现有 {present!r}，角色 {roles} 还要点 {missing}',
            next_action=(
                f'把 {missing} 登记进 freeze(models=...)；缺省模型名是 {_DEFAULT_MODEL_NAME!r}。'
                '清单 model 字段写的是名字，不是实例'
            ),
        )

    def _reject_missing_subagents(self) -> None:
        wanted: list[str] = []
        seen: set[str] = set()
        for name in self._order:
            for sub in self._entries[name].subagents:
                if sub not in seen:
                    seen.add(sub)
                    wanted.append(sub)
        missing = [name for name in wanted if name not in self._entries]
        if not missing:
            return
        known = list(self._order)
        raise ConfigError(
            expected=f'subagents 里的每一个名字都已在目录里：{wanted}',
            received=f'目录现有 {known!r}，没有 {missing!r}',
            next_action=f'先登记缺的 {missing}，再 freeze。subagents 不在 freeze 时自动创建',
        )


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


def _host_facts(host: object) -> HostFacts | None:
    if host is None:
        return None
    if isinstance(host, HostFacts):
        return host
    raise ConfigError(
        expected='host 是 HostFacts（os / platform / shell）或省略',
        received=f'收到 {type(host).__name__} 类型的 {host!r}',
        next_action='构造 HostFacts(os=..., platform=..., shell=...)；不要传 os 模块或 cwd',
    )


def _model_table(models: object) -> dict[str, LLM]:
    if isinstance(models, (str, bytes)) or not isinstance(models, Mapping):
        raise ConfigError(
            expected='models 是「模型名 -> LLM」的映射',
            received=f'收到 {type(models).__name__} 类型的 {models!r}，它没有 items()',
            next_action="传入一个 dict，例如 {'default': ScriptedLLM([])}",
        )
    table: dict[str, LLM] = {}
    for raw_key, raw_value in cast(Mapping[object, object], models).items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ConfigError(
                expected='模型表的每个键是非空白 str',
                received=f'键 {raw_key!r}，类型是 {type(raw_key).__name__}',
                next_action='键写成清单 model 字段会引用的名字，缺省是 default',
            )
        if not isinstance(raw_value, LLM):
            raise ConfigError(
                expected=f'模型表键 {raw_key!r} 的取值是 LLM 实例',
                received=f'收到 {raw_value!r}，类型是 {type(raw_value).__name__}',
                next_action='传入 LLM 实例（测试用 ScriptedLLM），不要传类对象或名字字符串',
            )
        table[raw_key] = raw_value
    return table


def _max_turns(manifest: AgentManifest) -> int | None:
    if manifest.budget is None:
        return _DEFAULT_MAX_TURNS
    return manifest.budget['turns']


def _import_impl(path: str) -> type[BaseAgent]:
    module_name, sep, qualname = path.partition(':')
    if not sep or not module_name.strip() or not qualname.strip():
        raise ConfigError(
            expected='impl 字符串是 pkg.mod:Class',
            received=f'impl={path!r}',
            next_action='写成 syndica.kernel.agent:BaseAgent 这种 module:qualname，中间一个冒号',
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(
            expected=f'能 import {module_name!r}',
            received=str(exc),
            next_action='核对模块路径；类式角色也可以直接把 class 对象传给 impl',
        ) from exc
    obj: object = module
    for part in qualname.split('.'):
        if not hasattr(obj, part):
            raise ConfigError(
                expected=f'{module_name}:{qualname} 能解析到 BaseAgent 子类',
                received=f'{type(obj).__name__} 上没有属性 {part!r}',
                next_action='核对 Class 名与模块导出',
            )
        obj = getattr(obj, part)
    return _base_agent_subclass(obj)


def _role_prompt(
    cls: type[BaseAgent],
    catalog: AgentCatalog,
    interpolated: str,
    *,
    role: str,
) -> str:
    """类可以在 freeze 时用目录里当时的 description 拼系统提示词。

    拼在 freeze，不在 register：用户后载入的同名清单要能换掉内置描述，编排者看到的是
    覆盖之后的那一句。没有 `compose_system_prompt` 的角色沿用清单里的提示词。
    """
    composer = cast('object', getattr(cls, 'compose_system_prompt', None))
    if not callable(composer):
        return interpolated
    composed = composer(catalog)
    if not isinstance(composed, str) or not composed.strip():
        raise ConfigError(
            expected=f'{role!r} 的 compose_system_prompt 返回非空白 str',
            received=f'收到 {composed!r}',
            next_action='返回拼好的系统提示词；各角色描述从 catalog.get 取，不要写死覆盖前的文案',
        )
    if not interpolated:
        return composed
    return f'{composed}\n{interpolated}'


def _interpolate_prompt(prompt: object, host: HostFacts | None, *, role: str) -> str:
    if not isinstance(prompt, str):
        raise ConfigError(
            expected=f'{role!r} 的 system_prompt 是 str',
            received=f'收到 {prompt!r}，类型是 {type(prompt).__name__}',
            next_action='清单解析之后 prompt 必须已经是字符串',
        )
    out: list[str] = []
    index = 0
    length = len(prompt)
    while index < length:
        char = prompt[index]
        if char == '{':
            if index + 1 < length and prompt[index + 1] == '{':
                out.append('{')
                index += 2
                continue
            close = prompt.find('}', index + 1)
            if close < 0:
                raise ConfigError(
                    expected=f'{role!r} 的 system_prompt 里每个 {{ 都有配对的 }}',
                    received=f'未闭合的占位从 {prompt[index:]!r} 开始',
                    next_action='写成 {os} / {platform} / {shell}，字面大括号用 {{ / }}',
                )
            name = prompt[index + 1 : close]
            out.append(_placeholder_value(name, host, role=role, raw=prompt[index : close + 1]))
            index = close + 1
            continue
        if char == '}':
            if index + 1 < length and prompt[index + 1] == '}':
                out.append('}')
                index += 2
                continue
            raise ConfigError(
                expected=f'{role!r} 的 system_prompt 里每个 }} 都来自占位或 }}',
                received=f'单独的 }} 出现在 {prompt[index:]!r}',
                next_action='字面右大括号写成 }}，占位写成 {os} / {platform} / {shell}',
            )
        out.append(char)
        index += 1
    return ''.join(out)


def _placeholder_value(
    name: str,
    host: HostFacts | None,
    *,
    role: str,
    raw: str,
) -> str:
    if name == 'cwd':
        raise ConfigError(
            expected='System Prompt 只允许 {os} / {platform} / {shell}，禁止 {cwd}',
            received=f'{role!r} 的 prompt 含 {raw}',
            next_action='把工作目录写进历史块，不要写进 System Prompt',
        )
    if name not in _PROMPT_PLACEHOLDERS:
        allowed = ' / '.join('{' + item + '}' for item in _PROMPT_PLACEHOLDERS)
        raise ConfigError(
            expected=f'System Prompt 占位只允许 {allowed}',
            received=f'{role!r} 的 prompt 含 {raw}',
            next_action=f'改成 {allowed}，或把这段写成普通文本。不要用格式规格（{{os:posix}}）',
        )
    if host is None:
        raise ConfigError(
            expected=f'含占位的 prompt 在 freeze 时注入 HostFacts；{role!r} 用了 {raw}',
            received='freeze(..., host=) 省略了 HostFacts',
            next_action='传入 HostFacts(os=..., platform=..., shell=...)；空 prompt 才可以不传',
        )
    return getattr(host, name)
