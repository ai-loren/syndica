"""YAML configuration adapter for the CLI surface.

YAML is decoded here and handed to the kernel as ordinary mappings. The kernel
must not depend on this module or on PyYAML.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from syndica.cli.environment import HOST_SET_ENV, reopens_git, valid_env_name
from syndica.kernel import (
    AgentCatalog,
    AgentSlot,
    ConfigError,
    ExitMode,
    ManifestSource,
    RedactionRule,
    RuleRedactor,
    Topology,
    register_builtins,
)
from syndica.kernel.redaction import looks_secret

__all__ = ['CliConfig', 'WorkspaceConfig', 'load_config']

_ROOT_KEYS = frozenset({'version', 'agents', 'topologies', 'workspace', 'redaction'})
_WORKSPACE_KEYS = frozenset({'pass_env', 'verify', 'protected'})
_REDACTION_KEYS = frozenset({'rules'})
_RULE_KEYS = frozenset({'id', 'pattern'})
_TOPOLOGY_KEYS = frozenset({'slots'})
_SLOT_KEYS = frozenset(
    {
        'name',
        'agent',
        'is_root',
        'cardinality',
        'key_field',
        'boot',
        'exit_mode',
        'extra_tools',
        'budget',
        'metadata',
    }
)


def _config(expected: str, received: str, next_action: str) -> ConfigError:
    return ConfigError(expected=expected, received=received, next_action=next_action)


class _UniqueSafeLoader(yaml.SafeLoader):
    """SafeLoader variant that refuses duplicate mapping keys."""


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


@dataclass(frozen=True)
class WorkspaceConfig:
    """`.syndica.yaml` 的 `workspace:` 段。三项都可省略。

    `pass_env` 是固定白名单之外再交给 Agent 的环境变量名；`verify` 是宿主复核与 `doctor --verify`
    逐条运行的命令；`protected` 是改了就不自动提交的相对路径 glob。
    """

    pass_env: tuple[str, ...] = ()
    verify: tuple[str, ...] = ()
    protected: tuple[str, ...] = ()


@dataclass(frozen=True)
class CliConfig:
    """A validated CLI configuration snapshot.

    `redaction_rules` 是 `redaction.rules` 里用户追加的脱敏规则，按声明顺序排在内置规则之后。
    """

    source: str
    catalog: AgentCatalog
    topologies: Mapping[str, Topology]
    workspace: WorkspaceConfig = WorkspaceConfig()
    redaction_rules: tuple[RedactionRule, ...] = ()


def load_config(path: str | Path) -> CliConfig:
    """Load one YAML mapping and translate it to kernel values."""
    file = _yaml_file(path)
    try:
        text = file.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        raise _config(
            '配置文件可按 UTF-8 读取',
            f'读取 {str(file)!r} 失败：{type(exc).__name__}: {exc}',
            '检查文件权限、路径与编码后重试；配置必须保存为 UTF-8',
        ) from exc
    try:
        raw = yaml.load(text, Loader=_UniqueSafeLoader)
    except yaml.YAMLError as exc:
        raise _config(
            '配置是单文档 safe YAML Mapping，且每个映射键只出现一次',
            f'{str(file)!r} 解析失败：{exc}',
            '修正 YAML 语法或重复键；不要使用 !!python/object 等非 safe 标签',
        ) from exc
    root = _root_mapping(raw, file)
    _version(root.get('version'), file)

    catalog = AgentCatalog()
    register_builtins(catalog)
    _load_agents(catalog, root.get('agents', ()), file)
    topologies = _load_topologies(root.get('topologies', {}), catalog, file)
    return CliConfig(
        source=str(file),
        catalog=catalog,
        topologies=MappingProxyType(topologies),
        workspace=_workspace(root.get('workspace', {})),
        redaction_rules=_redaction(root.get('redaction', {})),
    )


def _redaction(value: object) -> tuple[RedactionRule, ...]:
    if not isinstance(value, Mapping):
        raise _config(
            'redaction 是只含 rules 的 Mapping',
            f'收到 redaction={value!r}，类型是 {type(value).__name__}',
            '写成 `redaction: {rules: [{id: custom.internal_host, pattern: ...}]}`；'
            '不需要就省略整段',
        )
    raw = cast(Mapping[object, object], value)
    unknown = [str(key) for key in raw if not isinstance(key, str) or key not in _REDACTION_KEYS]
    if unknown:
        raise _config(
            f'redaction 只含 {sorted(_REDACTION_KEYS)!r}',
            f'未知键 {sorted(unknown)!r}',
            '删除这些键；未识别的配置不能静默忽略，否则用户会以为它已经生效',
        )
    rules: list[RedactionRule] = []
    for index, item in enumerate(_sequence(raw.get('rules', ()), 'redaction.rules')):
        field = f'redaction.rules[{index}]'
        entry = cast(Mapping[object, object], item) if isinstance(item, Mapping) else None
        exact = entry is not None and len(entry) == len(_RULE_KEYS)
        if entry is None or not exact or any(key not in entry for key in _RULE_KEYS):
            raise _config(
                '每条脱敏规则是恰好含 id 与 pattern 两个键的 Mapping',
                f'{field} 是 {item!r}',
                '写成 `{id: custom.internal_host, pattern: corp-[a-z0-9]+\\.internal}`',
            )
        try:
            rules.append(RedactionRule(cast(str, entry['id']), cast(str, entry['pattern'])))
        except ConfigError as exc:
            raise _config(exc.expected, f'{field}：{exc.received}', exc.next_action) from exc
    try:
        RuleRedactor(rules=rules)
    except ConfigError as exc:
        raise _config(exc.expected, f'redaction.rules：{exc.received}', exc.next_action) from exc
    return tuple(rules)


def _workspace(value: object) -> WorkspaceConfig:
    if not isinstance(value, Mapping):
        raise _config(
            'workspace 是只含 pass_env / verify / protected 的 Mapping',
            f'收到 workspace={value!r}，类型是 {type(value).__name__}',
            '写成 `workspace: {verify: [...]}`；不需要就省略整段',
        )
    raw = cast(Mapping[object, object], value)
    unknown = [str(key) for key in raw if not isinstance(key, str) or key not in _WORKSPACE_KEYS]
    if unknown:
        raise _config(
            f'workspace 只含 {sorted(_WORKSPACE_KEYS)!r}',
            f'未知键 {sorted(unknown)!r}',
            '删除这些键；未识别的配置不能静默忽略，否则用户会以为它已经生效',
        )
    fields = cast(Mapping[str, object], raw)
    pass_env = _strings(fields.get('pass_env', ()), 'workspace.pass_env')
    for name in pass_env:
        if not valid_env_name(name):
            raise _config(
                'workspace.pass_env 的每一项是环境变量名（字母、数字、下划线，不以数字开头）',
                f'收到 {name!r}',
                '改成变量名本身，例如 NO_COLOR；不要写 NAME=值',
            )
        if looks_secret(name):
            raise _config(
                'workspace.pass_env 不含像密钥的名字（按 _ 切开后没有 KEY、TOKEN、AUTH 这类词）',
                f'收到 {name!r}',
                (
                    f'从 pass_env 删掉 {name}：交给 Agent 的环境变量会出现在它能读、能打印的地方。'
                    'Agent 需要调用的服务如果要凭证，让宿主去调它'
                ),
            )
        if name in HOST_SET_ENV:
            raise _config(
                f'workspace.pass_env 不含宿主自己设的 {list(HOST_SET_ENV)!r}',
                f'收到 {name!r}',
                f'从 pass_env 删掉 {name}：宿主为每次 run 设它，透传会让 Agent 拿回你的 git / gh '
                '配置',
            )
        if reopens_git(name):
            raise _config(
                'workspace.pass_env 不含会重新打开 git / gh 配置的名字'
                '（GIT_CONFIG*、GIT_SSH*、*ASKPASS、GIT_DIR、GIT_WORK_TREE、GIT_EXEC_PATH、GH_*）',
                f'收到 {name!r}',
                (
                    f'从 pass_env 删掉 {name}：它让 Agent 的 git 或 gh 重新用上配置、凭证或另一个'
                    '仓库，绕过宿主为每次 run 准备的空配置'
                ),
            )
    protected = _strings(fields.get('protected', ()), 'workspace.protected')
    for pattern in protected:
        if pattern.startswith('/') or '..' in pattern.split('/'):
            raise _config(
                'workspace.protected 的每一项是相对工作区根、不含 .. 的 glob',
                f'收到 {pattern!r}',
                '改成相对路径，例如 pyproject.toml 或 scripts/**',
            )
    return WorkspaceConfig(
        pass_env=pass_env,
        verify=_strings(fields.get('verify', ()), 'workspace.verify'),
        protected=protected,
    )


def _strings(value: object, field: str) -> tuple[str, ...]:
    items = _sequence(value, field)
    seen: set[str] = set()
    result: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item.strip() or '\0' in item:
            raise _config(
                f'{field}[{index}] 是非空白、不含 NUL 字节的字符串',
                f'收到 {item!r}，类型是 {type(item).__name__}',
                (
                    f'YAML 把 on / off / yes / no / true 读成布尔值；要当字符串就给 {field} 的'
                    '这一项加引号'
                    if isinstance(item, bool)
                    else f'把 {field} 写成字符串列表，删掉空项与 NUL 字节'
                ),
            )
        if item in seen:
            raise _config(
                f'{field} 的每一项只出现一次',
                f'{item!r} 重复出现',
                '删掉重复的那一项',
            )
        seen.add(item)
        result.append(item)
    return tuple(result)


def _yaml_file(path: object) -> Path:
    if not isinstance(path, str | Path):
        raise _config(
            '配置路径是 .yaml / .yml 文件路径',
            f'收到 {path!r}，类型是 {type(path).__name__}',
            '传入 Path 或字符串路径',
        )
    file = Path(path)
    if file.suffix.lower() not in {'.yaml', '.yml'}:
        raise _config(
            '配置路径以 .yaml 或 .yml 结尾',
            f'收到 {str(file)!r}，后缀是 {file.suffix!r}',
            '把配置保存成 YAML 文件并使用 .yaml / .yml 后缀',
        )
    if not file.is_file():
        raise _config(
            '配置路径指向一个存在的普通文件',
            f'收到 {str(file)!r}，它不存在或不是文件',
            '创建该 YAML 配置文件，或把 --config 指向已有文件',
        )
    return file


def _root_mapping(value: object, file: Path) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _config(
            '配置根是 Mapping，至少含 version: 1',
            f'{str(file)!r} 的根是 {type(value).__name__}：{value!r}',
            '把文件改成 version / agents / topologies / workspace / redaction 组成的单个 YAML 对象',
        )
    root = cast(Mapping[object, object], value)
    non_strings = [key for key in root if not isinstance(key, str)]
    if non_strings:
        raise _config(
            '配置根的键都是字符串',
            f'非字符串键为 {non_strings!r}',
            '把这些键改成 version / agents / topologies / workspace / redaction 中的字符串',
        )
    unknown = [str(key) for key in root if key not in _ROOT_KEYS]
    if unknown:
        raise _config(
            f'配置根只含 {sorted(_ROOT_KEYS)!r}',
            f'未知键 {sorted(unknown)!r}',
            '删除未知键；未识别配置不能静默忽略，否则用户会以为它已经生效',
        )
    return cast(Mapping[str, object], root)


def _version(value: object, file: Path) -> None:
    if type(value) is int and value == 1:
        return
    raise _config(
        '配置 version 是整数 1',
        f'{str(file)!r} 收到 version={value!r}，类型是 {type(value).__name__}',
        '写成 `version: 1`；不要写 true、字符串或其他版本',
    )


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise _config(
            f'{field} 是 YAML 列表',
            f'收到 {field}={value!r}，类型是 {type(value).__name__}',
            f'把 {field} 写成 `- ...` 列表；没有内容就写 [] 或省略',
        )
    return cast(Sequence[object], value)


def _load_agents(catalog: AgentCatalog, value: object, file: Path) -> None:
    entries = _sequence(value, 'agents')
    names: list[str] = []
    for index, item in enumerate(entries):
        if isinstance(item, Mapping):
            name = cast(Mapping[object, object], item).get('name')
            if isinstance(name, str) and name in names:
                raise _config(
                    '同一个 YAML 文件里的 Agent name 互不重名',
                    f'agents[{index}] 再次声明了 {name!r}',
                    '合并成一份清单；若要覆盖内置角色，只保留最后希望生效的那一份',
                )
            if isinstance(name, str):
                names.append(name)
        catalog.load_mapping(
            cast(object, item),
            source=ManifestSource(
                kind='yaml_file',
                location=f'{str(file)}#agents[{index}]',
                origin=str(file.parent.resolve()),
            ),
        )


def _load_topologies(value: object, catalog: AgentCatalog, file: Path) -> dict[str, Topology]:
    if not isinstance(value, Mapping):
        raise _config(
            'topologies 是「名字 -> {slots: [...]}」的 Mapping',
            f'收到 topologies={value!r}，类型是 {type(value).__name__}',
            '写成 `topologies: {default: {slots: [...]}}`，没有拓扑就省略',
        )
    topologies: dict[str, Topology] = {}
    for raw_name, raw_topology in cast(Mapping[object, object], value).items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise _config(
                '每个 topology 名是非空白字符串',
                f'收到名字 {raw_name!r}，类型是 {type(raw_name).__name__}',
                '给拓扑一个可由 --topology 逐字匹配的名字',
            )
        topology = _topology(raw_name, raw_topology, file)
        topology.validate(catalog)
        topologies[raw_name] = topology
    return topologies


def _topology(name: str, value: object, file: Path) -> Topology:
    if not isinstance(value, Mapping):
        raise _config(
            f'topology {name!r} 是只含 slots 的 Mapping',
            f'收到 {type(value).__name__} 类型的 {value!r}',
            '写成 `{slots: [...]}`',
        )
    topology = cast(Mapping[object, object], value)
    unknown = [str(key) for key in topology if key not in _TOPOLOGY_KEYS]
    if unknown:
        raise _config(
            f'topology {name!r} 只允许键 slots',
            f'{str(file)!r} 中有未知键 {sorted(unknown)!r}',
            '删除这些键；拓扑只声明结构，不声明行为',
        )
    slots = _sequence(topology.get('slots', ()), f'topologies.{name}.slots')
    return Topology(slots=tuple(_slot(name, index, item) for index, item in enumerate(slots)))


def _slot(topology: str, index: int, value: object) -> AgentSlot:
    field = f'topologies.{topology}.slots[{index}]'
    if not isinstance(value, Mapping):
        raise _config(
            f'{field} 是 AgentSlot 字段的 Mapping',
            f'收到 {type(value).__name__} 类型的 {value!r}',
            '写成 `{name: root, agent: Orchestrator}` 这样的映射',
        )
    raw = cast(Mapping[object, object], value)
    unknown = [str(key) for key in raw if not isinstance(key, str) or key not in _SLOT_KEYS]
    if unknown:
        raise _config(
            f'{field} 只含 AgentSlot 字段 {sorted(_SLOT_KEYS)!r}',
            f'未知键 {sorted(unknown)!r}',
            (
                '删除这些键；slot 只增强清单，只做增量追加，不得放 system_prompt / model / impl '
                '等替换角色行为的字段'
            ),
        )
    values = dict(cast(Mapping[str, object], raw))
    if 'exit_mode' in values:
        raw_exit_mode = values['exit_mode']
        try:
            values['exit_mode'] = ExitMode(raw_exit_mode)
        except (TypeError, ValueError) as exc:
            raise _config(
                f'{field}.exit_mode 是 idle / zombie / exit 之一',
                f'收到 exit_mode={raw_exit_mode!r}，类型是 {type(raw_exit_mode).__name__}',
                '改成小写字符串 idle、zombie 或 exit',
            ) from exc
    try:
        return AgentSlot(**cast(Any, values))
    except TypeError as exc:
        raise _config(
            f'{field} 至少含 name 与 agent，且字段与 AgentSlot 一致',
            f'构造失败：{exc}',
            '补齐 name / agent，并删除不属于 AgentSlot 的字段',
        ) from exc
