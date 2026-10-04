"""YAML configuration adapter for the CLI surface.

YAML is decoded here and handed to the kernel as ordinary mappings. The kernel
must not depend on this module or on PyYAML.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from syndica.cli.environment import HOST_SET_ENV, reopens_git, valid_env_name
from syndica.cli.policy import STRICTNESS, RoleTightening, dev_table
from syndica.kernel import (
    AgentCatalog,
    AgentSlot,
    ConfigError,
    Disposition,
    ExitMode,
    ManifestSource,
    RedactionRule,
    RuleRedactor,
    Topology,
    TraceLevel,
    register_builtins,
)
from syndica.kernel.hooks.junctions import MODULE_NAME
from syndica.kernel.manifest import prompt_file
from syndica.kernel.redaction import looks_secret

__all__ = [
    'CliConfig',
    'WorkspaceConfig',
    'config_from_text',
    'load_config',
    'parse_yaml',
    'yaml_file',
]

logger = logging.getLogger(__name__)

_ROOT_KEYS = frozenset(
    {
        'version',
        'agents',
        'topologies',
        'workspace',
        'redaction',
        'trace',
        'danger',
        'modules',
        'flags',
    }
)
_DANGER_KEYS = frozenset({'roles'})
_ROLE_DANGER_KEYS = frozenset({'rules', 'policy'})
_WORKSPACE_KEYS = frozenset({'pass_env', 'verify', 'protected', 'versions'})
_REDACTION_KEYS = frozenset({'rules'})
_TRACE_KEYS = frozenset({'level'})
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


# 名字里带这些的未知键多半是想设 OTLP 导出：单独说清为什么不行，而不是只说「未知键」（M3 D14）。
_EXPORT_HINTS = ('otlp', 'otel', 'opentelemetry', 'endpoint', 'export', 'collector')


def _refuse_export_settings(unknown: Sequence[str], where: str) -> None:
    named = sorted(key for key in unknown if any(hint in key.lower() for hint in _EXPORT_HINTS))
    if named:
        raise _config(
            'OTLP 导出的端点只从你自己的环境（OTEL_EXPORTER_OTLP_TRACES_ENDPOINT / '
            'OTEL_EXPORTER_OTLP_ENDPOINT）或命令行（syndica trace export <id> --otlp <url>）来',
            f'{where}里有 {named!r}',
            '删掉它，改用环境变量或 --otlp：仓库里的配置可能来自不受信任的人，它选的端点会收到 '
            'trace 的结构（命令、路径、工具名）与你的鉴权头',
        )


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
    """`.syndica.yaml` 的 `workspace:` 段。四项都可省略。

    `pass_env` 是固定白名单之外再交给 Agent 的环境变量名；`verify` 是宿主复核与 `doctor --verify`
    逐条运行的命令；`protected` 是改了就不自动提交的相对路径 glob；`versions` 打开版本层与隔离
    （缺省打开；关掉时没有版本、没有隔离，文件污点照旧，不放松任何安全判据）。
    """

    pass_env: tuple[str, ...] = ()
    verify: tuple[str, ...] = ()
    protected: tuple[str, ...] = ()
    versions: bool = True


@dataclass(frozen=True)
class CliConfig:
    """A validated CLI configuration snapshot.

    `redaction_rules` 是 `redaction.rules` 里用户追加的脱敏规则，按声明顺序排在内置规则之后；
    `trace_level` 是 `trace.level`，命令行没给 `--trace` 时用它，没写时 None（宿主取默认级别）。
    """

    source: str
    catalog: AgentCatalog
    topologies: Mapping[str, Topology]
    workspace: WorkspaceConfig = WorkspaceConfig()
    redaction_rules: tuple[RedactionRule, ...] = ()
    trace_level: TraceLevel | None = None
    danger_roles: Mapping[str, RoleTightening] = field(default_factory=lambda: MappingProxyType({}))
    modules: tuple[str, ...] = ()
    flags: Mapping[str, bool | int | str] = field(default_factory=lambda: MappingProxyType({}))


def load_config(path: str | Path) -> CliConfig:
    """Load one YAML mapping and translate it to kernel values."""
    file = yaml_file(path)
    try:
        text = file.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        raise _config(
            '配置文件可按 UTF-8 读取',
            f'读取 {str(file)!r} 失败：{type(exc).__name__}: {exc}',
            '检查文件权限、路径与编码后重试；配置必须保存为 UTF-8',
        ) from exc
    return config_from_text(file, text)


def parse_yaml(file: Path, text: str) -> Mapping[str, object]:
    """配置文件的根映射（已校验键与版本），不建任何 kernel 对象。"""
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
    return root


def config_from_text(
    file: Path, text: str, *, prompts: Mapping[int, str] | None = None
) -> CliConfig:
    """从已经读出的配置文本建配置。给了 `prompts`（agents 的序号 → 读好的提示词正文）时，提示词一律
    按给出的正文、不再按路径读文件：读文件的那一方已经读过一次，摘要算的就是那一份（M4 D15）。"""
    root = parse_yaml(file, text)
    catalog = AgentCatalog()
    register_builtins(catalog)
    _load_agents(catalog, root.get('agents', ()), file, prompts)
    topologies = _load_topologies(root.get('topologies', {}), catalog, file)
    return CliConfig(
        source=str(file),
        catalog=catalog,
        topologies=MappingProxyType(topologies),
        workspace=_workspace(root.get('workspace', {})),
        redaction_rules=_redaction(root.get('redaction', {})),
        trace_level=_trace(root.get('trace', {})),
        danger_roles=_danger(root.get('danger', {}), catalog),
        modules=_module_names(root.get('modules', ())),
        flags=_flags(root.get('flags', {})),
    )


def _flags(value: object) -> Mapping[str, bool | int | str]:
    """`flags:` 的原样取值。只查形状：哪些 key 合法、取值对不对，要等装好 Module 才知道。"""
    entries = _keyed(value, 'flags', None, '`flags: {retry_max_attempts: 3}`')
    for key, item in entries.items():
        if isinstance(item, float) or not isinstance(item, bool | int | str):
            raise _config(
                f'flags.{key} 是布尔值、整数或字符串',
                f'收到 {item!r}，类型是 {type(item).__name__}',
                '照 syndica flags show <key> 的缺省值写；Flag 的取值只有这三种类型',
            )
    return MappingProxyType(dict(cast(Mapping[str, bool | int | str], entries)))


def _module_names(value: object) -> tuple[str, ...]:
    """`modules:` 的名字，按写的次序装。名字能不能装由宿主对着内置表与发现结果判。"""
    names: list[str] = []
    for index, item in enumerate(_sequence(value, 'modules')):
        if not isinstance(item, str) or MODULE_NAME.fullmatch(item) is None:
            raise _config(
                'modules 的每一项是 Module 名（小写字母开头、单个连字符分段）',
                f'modules[{index}]={item!r}，类型是 {type(item).__name__}',
                '照 syndica modules list 的名字逐字写，例如 retry',
            )
        if item in names:
            raise _config(
                'modules 里每个名字只写一次',
                f'modules[{index}]={item!r} 与前面重复',
                '删掉重复的那一项；装配次序就是写的次序',
            )
        names.append(item)
    return tuple(names)


def _danger(value: object, catalog: AgentCatalog) -> Mapping[str, RoleTightening]:
    """`danger.roles`：项目配置只能收紧开发期的处置（M4 D12）。项目配置是不受信的来源，放宽安全边界
    要问用户，而 M4 没有用户级配置。"""
    section = _keyed(value, 'danger', _DANGER_KEYS, '`danger: {roles: {Worker: {...}}}`')
    roles: dict[str, RoleTightening] = {}
    names = catalog.names()
    raw_roles = _keyed(section.get('roles', {}), 'danger.roles', None, '`roles: {Worker: {...}}`')
    for role, raw in raw_roles.items():
        if role not in names:
            raise _config(
                'danger.roles 的键是已载入的角色名',
                f'没有角色 {role!r}',
                f'改成已有的角色名（{list(names)}），或先在 agents 里声明它',
            )
        field_name = f'danger.roles.{role}'
        entry = _keyed(raw, field_name, _ROLE_DANGER_KEYS, '`{rules: {...}}` 或 `{policy: ...}`')
        if not entry:
            raise _config(
                f'{field_name} 至少写 rules 或 policy 之一',
                f'{field_name} 是空的',
                '写上要收紧的规则，或 policy: block_high_risk；不收紧就删掉这个角色',
            )
        roles[role] = RoleTightening(
            rules=MappingProxyType(_tightened_rules(entry.get('rules', {}), field_name)),
            block_high_risk=_role_policy(entry, field_name),
        )
    return MappingProxyType(roles)


def _keyed(
    value: object, field_name: str, allowed: frozenset[str] | None, example: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _config(
            f'{field_name} 是 Mapping',
            f'收到 {field_name}={value!r}，类型是 {type(value).__name__}',
            f'写成 {example}',
        )
    raw = cast(Mapping[object, object], value)
    bad = [key for key in raw if not isinstance(key, str)]
    unknown = [key for key in raw if allowed is not None and key not in allowed]
    if bad or unknown:
        raise _config(
            f'{field_name} 的键是字符串'
            + ('' if allowed is None else f'，只含 {sorted(allowed)!r}'),
            f'收到键 {sorted(str(key) for key in [*bad, *unknown])!r}',
            f'写成 {example}；未识别的配置不能静默忽略',
        )
    return cast(Mapping[str, object], raw)


def _tightened_rules(value: object, field_name: str) -> dict[str, Disposition]:
    base = dev_table()
    choices = {'confirm': Disposition.CONFIRM, 'block': Disposition.BLOCK}
    rules: dict[str, Disposition] = {}
    for rule_id, raw in _keyed(value, f'{field_name}.rules', None, '`{规则 ID: block}`').items():
        if rule_id not in base:
            raise _config(
                f'{field_name}.rules 的键是开发期判定器里的规则 ID',
                f'没有规则 {rule_id!r}',
                f'改成 {sorted(base)} 之一；规则 ID 拼错时不能静默回落',
            )
        if not isinstance(raw, str) or raw not in choices:
            raise _config(
                f'{field_name}.rules.{rule_id} 是 confirm 或 block：项目配置只能收紧处置',
                f'收到 {raw!r}，类型是 {type(raw).__name__}',
                '写字符串 confirm 或 block；项目配置放不开开发期的规则，当前也没有别的放开入口',
            )
        chosen = choices[raw]
        if STRICTNESS[chosen] < STRICTNESS[base[rule_id]]:
            raise _config(
                f'{field_name}.rules.{rule_id} 只能比开发期的处置更严：项目配置只能收紧处置',
                f'开发期是 {base[rule_id].value}，收到 {raw!r}',
                f'写 {base[rule_id].value} 或更严的值，或删掉这一条',
            )
        rules[rule_id] = chosen
    return rules


def _role_policy(entry: Mapping[str, object], field_name: str) -> bool:
    if 'policy' not in entry:
        return False
    policy = entry['policy']
    if policy == 'block_high_risk':
        return True
    raise _config(
        f'{field_name}.policy 是 block_high_risk：项目配置只能收紧处置',
        f'收到 {policy!r}',
        ('allow_all 会放开每一条规则，项目配置做不到；' if policy == 'allow_all' else '')
        + '要逐条收紧就写 rules，要阻断全部高风险就写 block_high_risk',
    )


def _trace(value: object) -> TraceLevel | None:
    if not isinstance(value, Mapping):
        raise _config(
            'trace 是只含 level 的 Mapping',
            f'收到 trace={value!r}，类型是 {type(value).__name__}',
            '写成 `trace: {level: structure}`；不需要就省略整段（默认 replay）',
        )
    raw = cast(Mapping[object, object], value)
    unknown = [str(key) for key in raw if not isinstance(key, str) or key not in _TRACE_KEYS]
    _refuse_export_settings(unknown, 'trace ')
    if unknown:
        raise _config(
            f'trace 只含 {sorted(_TRACE_KEYS)!r}',
            f'未知键 {sorted(unknown)!r}',
            '删除这些键；未识别的配置不能静默忽略，否则用户会以为它已经生效',
        )
    if 'level' not in raw:
        return None
    level = raw['level']
    # YAML 1.1 把不带引号的 off（还有 no、false）读成 false。
    if level is False:
        return TraceLevel.OFF
    names = [member.value for member in TraceLevel]
    if not isinstance(level, str) or level not in names:
        raise _config(
            f'trace.level 是 {" / ".join(names)} 之一',
            f'收到 {level!r}（{type(level).__name__}）',
            '改成其中之一；yes / on / true 会被 YAML 读成布尔值，不是级别',
        )
    return TraceLevel(level)


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
            'workspace 是只含 pass_env / verify / protected / versions 的 Mapping',
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
        if name.startswith('OTEL_EXPORTER_OTLP_'):
            raise _config(
                'workspace.pass_env 不含 OTLP 导出的配置（OTEL_EXPORTER_OTLP_*）',
                f'收到 {name!r}',
                (
                    f'从 pass_env 删掉 {name}：它们是宿主导出 trace 用的端点与鉴权头，交给 Agent '
                    '等于把 collector 的凭证交给它'
                ),
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
    versions = fields.get('versions', True)
    if not isinstance(versions, bool):
        raise _config(
            'workspace.versions 是 true 或 false',
            f'收到 {versions!r}，类型是 {type(versions).__name__}',
            '写成 `versions: false` 关掉版本层与隔离；要打开就省略它',
        )
    return WorkspaceConfig(
        pass_env=pass_env,
        verify=_strings(fields.get('verify', ()), 'workspace.verify'),
        protected=protected,
        versions=versions,
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


def yaml_file(path: object) -> Path:
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
            f'把文件改成由 {" / ".join(sorted(_ROOT_KEYS))} 组成的单个 YAML 对象',
        )
    root = cast(Mapping[object, object], value)
    non_strings = [key for key in root if not isinstance(key, str)]
    if non_strings:
        raise _config(
            '配置根的键都是字符串',
            f'非字符串键为 {non_strings!r}',
            f'把这些键改成 {" / ".join(sorted(_ROOT_KEYS))} 之一',
        )
    unknown = [str(key) for key in root if key not in _ROOT_KEYS]
    _refuse_export_settings(unknown, '配置根')
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


def _load_agents(
    catalog: AgentCatalog, value: object, file: Path, prompts: Mapping[int, str] | None
) -> None:
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
        location = f'{str(file)}#agents[{index}]'
        origin = str(file.parent.resolve())
        entry = cast(object, item)
        if isinstance(item, Mapping):
            table = cast(Mapping[object, object], item)
            _warn_if_a_path_went_missing(table.get('system_prompt'), origin, location)
            if prompts is not None and index in prompts:
                entry = {**table, 'system_prompt': prompts[index]}
        catalog.load_mapping(
            entry,
            source=ManifestSource(
                kind='yaml_file', location=location, origin=None if prompts is not None else origin
            ),
        )


def _warn_if_a_path_went_missing(prompt: object, origin: str, location: str) -> None:
    """一个长得像路径、却不会被读的字符串会被当成提示词正文原样交给模型，而 run 照常跑完。"""
    if not isinstance(prompt, str) or not prompt or any(ch.isspace() for ch in prompt):
        return
    if not ('/' in prompt or '\\' in prompt or prompt.endswith(('.md', '.txt'))):
        return
    if prompt_file(prompt, origin) is not None:
        return
    reason, fix = _why_not_read(prompt, origin)
    logger.warning(
        '期望: %s 的 system_prompt %r 是 %s 之内的一个文件。'
        ' 实际: %s，它会被当成提示词正文原样交给模型。'
        ' 下一步: %s；本意就是这段正文就不用管。',
        location,
        prompt,
        origin,
        reason,
        fix,
    )


def _why_not_read(prompt: str, origin: str) -> tuple[str, str]:
    """(没读它的原因, 走得通的下一步)。规则同 `prompt_file`：只读 origin 之内、相对路径指向的真实
    文件。文件在 origin 之外时 `{file:}` 同样会拒，下一步只能是把文件挪进来。"""
    move = f'把文件放进 {origin} 之内并写相对路径'
    relative = Path(prompt)
    if relative.is_absolute():
        return '它是绝对路径，只读清单所在目录之内的相对路径', move
    if '..' in relative.parts:
        return '它含 ..，只读清单所在目录之内的相对路径', move
    try:
        candidate = (Path(origin) / relative).resolve()
    except (OSError, RuntimeError) as exc:
        return f'解析它失败（{exc}）', '修正路径'
    try:
        candidate.relative_to(Path(origin).resolve())
    except ValueError:
        return f'它解析到 {candidate}，在 {origin} 之外', move
    if candidate.exists():
        return f'{candidate} 不是文件', '修正路径'
    return '没有这个文件', f'修正路径，或写成 {{file: {prompt}}}，路径写错时直接报错'


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
