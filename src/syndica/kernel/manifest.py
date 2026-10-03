"""声明式 Agent 清单（spec §4.6）。

十个公开字段的名字、顺序、缺省值一经导出就是永久承诺；未知键当场拒绝——丢掉等于
「我改了为什么没生效」。本模块只解析 Mapping，不读 YAML、不读环境变量。

`system_prompt` 何时当文件读：写成 `{file: <相对路径>}` 时一定读，文件不存在或不在 origin 之内
就报错；写成字符串时，source 带 origin 目录、且该字符串作为相对路径在目录内真的是一个文件才读，否则
字符串就是提示词本身，不靠后缀或换行猜测。没有 origin 时一律不读文件：宿主已经读好正文、不要内核再
按路径读一次，就不给 origin（`prompt_file` 告诉宿主该读哪个文件）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

from syndica.kernel.agent import BaseAgent
from syndica.kernel.errors import ConfigError

__all__ = ['AgentManifest', 'ManifestSource', 'parse_mapping', 'prompt_file']

_PUBLIC_FIELDS: tuple[str, ...] = (
    'name',
    'description',
    'system_prompt',
    'tools',
    'subagents',
    'model',
    'extends',
    'skills',
    'budget',
    'artifact_kinds',
)
_KNOWN_KEYS: frozenset[str] = frozenset((*_PUBLIC_FIELDS, 'impl'))
_SOURCE_KINDS: frozenset[str] = frozenset(
    {'mapping', 'json_file', 'json_directory', 'yaml_file', 'builtin', 'class'}
)


@dataclass(frozen=True)
class ManifestSource:
    """清单从哪来的快照。`location` / `origin` 都是 str，不交活的 Path。

    `origin` 是解析 `system_prompt` 相对路径的目录；纯 Mapping 没有 origin，提示词按字面保存。
    """

    kind: Literal['mapping', 'json_file', 'json_directory', 'yaml_file', 'builtin', 'class']
    location: str
    origin: str | None = None


@dataclass(frozen=True)
class AgentManifest:
    """一份声明式（或类式）Agent 清单。字段顺序计入公开 API，新字段只能追加。

    `tools` 是名字元组，不是 Tool 映射。名字到工具的解析在 catalog freeze，不在本类。
    """

    name: str
    description: str
    system_prompt: str = ''
    tools: tuple[str, ...] = ()
    subagents: tuple[str, ...] = ()
    model: str = 'default'
    extends: str | None = None
    skills: tuple[str, ...] = ()
    budget: Mapping[str, int | None] | None = None
    artifact_kinds: tuple[str, ...] = ()
    impl: type[BaseAgent] | str | None = None
    source: ManifestSource = ManifestSource(kind='mapping', location='<mapping>')


def parse_mapping(data: object, *, source: ManifestSource) -> AgentManifest:
    """把已解析的 Mapping 收成清单。YAML → Mapping 是宿主 / 计划 6 的事。"""
    if isinstance(data, (str, bytes)) or not isinstance(data, Mapping):
        raise ConfigError(
            expected='清单是字段名到取值的映射（已解析的 dict / JSON 对象）',
            received=f'收到 {type(data).__name__} 类型的 {data!r}',
            next_action=(
                '先把 YAML / JSON 解析成 dict 再交给 parse_mapping；'
                'kernel 不收 YAML 文本，也不收清单列表'
            ),
        )
    table = cast(Mapping[str, object], data)
    _require_source(source)
    unknown = [key for key in table if key not in _KNOWN_KEYS]
    if unknown:
        raise ConfigError(
            expected=(f'清单只含十个公开字段 {list(_PUBLIC_FIELDS)} 以及判别字段 impl'),
            received=f'未知键 {sorted(str(key) for key in unknown)!r}',
            next_action=(
                '删掉这些键，或确认没有把 YAML 的 `class:` 写成公开字段——'
                '类式 vs 声明式看 impl，误写 class 走未知键。'
                '新字段只能等内核追加，未知键被丢掉等于改了却不生效'
            ),
        )
    return AgentManifest(
        name=_required_nonblank(table, 'name'),
        description=_required_nonblank(table, 'description'),
        system_prompt=_system_prompt(table.get('system_prompt', ''), source.origin),
        tools=_name_tuple(table.get('tools', ()), 'tools'),
        subagents=_name_tuple(table.get('subagents', ()), 'subagents'),
        model=_model(table.get('model', 'default')),
        extends=_optional_extends(table.get('extends', None)),
        skills=_name_tuple(table.get('skills', ()), 'skills'),
        budget=_budget(table.get('budget', None)),
        artifact_kinds=_name_tuple(table.get('artifact_kinds', ()), 'artifact_kinds'),
        impl=_impl(table.get('impl', None)),
        source=source,
    )


def _require_source(source: object) -> None:
    if not isinstance(source, ManifestSource):
        raise ConfigError(
            expected='source 是 ManifestSource 快照（kind + location，可选 origin 目录）',
            received=f'收到 {type(source).__name__} 类型的 {source!r}',
            next_action='构造 ManifestSource(kind=..., location=...) 再传入，不要传活的 Path',
        )
    if source.kind not in _SOURCE_KINDS:
        raise ConfigError(
            expected=f'source.kind 是 {_SOURCE_KINDS} 之一',
            received=f'source.kind={source.kind!r}',
            next_action='改成 mapping / json_file / json_directory / yaml_file / builtin / class',
        )
    if not source.location.strip():
        raise ConfigError(
            expected='source.location 是非空白 str 快照，不是 Path',
            received=f'source.location={source.location!r}',
            next_action='mapping 用调用方标签（缺省 <mapping>），文件用绝对路径字符串',
        )
    if source.origin is not None and not source.origin.strip():
        raise ConfigError(
            expected='source.origin 是目录的 str 快照，或 None',
            received=f'source.origin={source.origin!r}',
            next_action='传入目录路径字符串；纯 Mapping 不要设 origin，提示词按字面保存',
        )


def _required_nonblank(table: Mapping[str, object], field: str) -> str:
    if field not in table:
        raise ConfigError(
            expected=f'{field} 是非空白字符串，且必填',
            received=f'清单里没有 {field}',
            next_action=f'补上 {field}：编排者按 description 决定何时派活，没有名字则寻址不到',
        )
    return _nonblank_str(table[field], field)


def _nonblank_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(
            expected=f'{field} 是非空白字符串',
            received=f'{field}={value!r}，类型是 {type(value).__name__}',
            next_action=f'把 {field} 改成非空白 str',
        )
    if not value.strip():
        raise ConfigError(
            expected=f'{field} 是非空白字符串',
            received=f'{field}={value!r}',
            next_action=f'把 {field} 改成非空白 str；空白名字让 Delegate 寻址落空',
        )
    return value


def _model(value: object) -> str:
    if value is None:
        return 'default'
    return _nonblank_str(value, 'model')


def _optional_extends(value: object) -> str | None:
    if value is None:
        return None
    return _nonblank_str(value, 'extends')


def _name_tuple(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        raise ConfigError(
            expected=f'{field} 是字符串名字的列表或元组',
            received=(
                f'{field}={value!r}：str / bytes 也是 Sequence，按序列收下会把 '
                f'{value!r} 拆成单个字符'
            ),
            next_action=f'写成 [{field} 的名字, ...]，例如 tools: ["Read", "Grep"]',
        )
    if isinstance(value, Mapping):
        keys = list(cast(Mapping[str, object], value))
        raise ConfigError(
            expected=f'{field} 是名字列表，不是「名字 → Tool」的映射',
            received=f'{field} 是 {type(cast(object, value)).__name__}，键为 {keys!r}',
            next_action=(
                '声明式清单的 tools 写成 ["Read", "Grep"] 这样的名字列表；'
                '先经 AgentCatalog 的工具目录解析，不要直接塞进 AgentOptions.tools'
            ),
        )
    if not isinstance(value, Sequence):
        raise ConfigError(
            expected=f'{field} 是字符串名字的列表或元组',
            received=f'{field}={value!r}，类型是 {type(value).__name__}',
            next_action=f'把 {field} 改成名字的 list 或 tuple',
        )
    names: list[str] = []
    for index, item in enumerate(cast(Sequence[object], value)):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(
                expected=f'{field} 的每个元素是非空白字符串名字',
                received=f'{field}[{index}]={item!r}，类型是 {type(item).__name__}',
                next_action=f'把 {field}[{index}] 改成非空白工具 / 角色 / skill 名',
            )
        names.append(item)
    return tuple(names)


def _budget(value: object) -> Mapping[str, int | None] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Mapping):
        raise ConfigError(
            expected='budget 是只含 turns 键的映射，或省略（则 max_turns 保持 50）',
            received=f'budget={value!r}，类型是 {type(value).__name__}',
            next_action='写成 {turns: 40}，或 turns: null 表示不限轮次',
        )
    budget = cast(Mapping[object, object], value)
    extra = [key for key in budget if key != 'turns']
    if extra:
        raise ConfigError(
            expected='budget 只允许键 turns',
            received=f'budget 里还有 {sorted(str(key) for key in extra)!r}',
            next_action=(
                '删掉这些键。本计划能映射的只有 turns → AgentOptions.max_turns；'
                '其它预算字段还没有读者，写了等于改了却不生效'
            ),
        )
    if 'turns' not in budget:
        raise ConfigError(
            expected='budget 若出现，必须有 turns',
            received=f'budget={dict(budget)!r}，没有 turns',
            next_action='写成 {turns: 40}，或不写 budget 以使用默认 50 轮',
        )
    turns = _turns(budget['turns'])
    return MappingProxyType({'turns': turns})


def _turns(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            expected='budget.turns 是整数或 null（不限轮次）',
            received=(
                f'turns={value!r}，类型是 {type(value).__name__}'
                + ('：bool 是 int 的子类，YAML yes 会变成 1 轮' if isinstance(value, bool) else '')
            ),
            next_action='传整数轮次，或 null；不要传布尔值或浮点',
        )
    if value < 0:
        raise ConfigError(
            expected='budget.turns 是 >= 0 的整数或 null',
            received=f'turns={value!r}',
            next_action='传非负整数，或 null 表示不限',
        )
    return value


def _impl(value: object) -> type[BaseAgent] | str | None:
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            raise ConfigError(
                expected='impl 是 BaseAgent 子类、import 路径字符串，或省略（声明式）',
                received=f'impl={value!r}',
                next_action=(
                    '写成 pkg.mod:Class，或传入 type 对象；空白 impl 不是声明式——声明式请省略该键'
                ),
            )
        return value
    if isinstance(value, type) and issubclass(value, BaseAgent):
        return value
    received = (
        f'impl={value!r}，类型是 {type(value).__name__}'
        if not isinstance(value, type)
        else f'impl 是 {value.__name__}，它不是 BaseAgent 的子类'
    )
    raise ConfigError(
        expected='impl 是 BaseAgent 子类、import 路径字符串，或省略（声明式）',
        received=received,
        next_action=(
            '类式传入 BaseAgent 子类或 "pkg.mod:Class"；'
            '声明式不要写 impl，也不要写 class:——class 是未知键'
        ),
    )


def _system_prompt(value: object, origin: str | None) -> str:
    path = prompt_file(value, origin)
    if path is None:
        return cast(str, value)
    try:
        return path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(
            expected=f'system_prompt 指向的文件 {path} 可读，且是 utf-8 文本',
            received=f'读失败：{exc}',
            next_action='修正该文件的权限或编码（utf-8），或改成字面提示词',
        ) from exc


def prompt_file(value: object, origin: str | None) -> Path | None:
    """`system_prompt` 的这个取值会读哪个文件；字面提示词交回 None。

    宿主要先自己读一遍（算摘要、给人看）时调它，读好的正文再以字面提示词交给清单、不给 origin：
    两处共用这一条规则，就不会一处当路径、一处当正文。
    """
    if isinstance(value, Mapping):
        return _explicit_prompt_file(cast(Mapping[object, object], value), origin)
    if not isinstance(value, str):
        raise ConfigError(
            expected=(
                'system_prompt 是字符串（字面提示词，或相对 origin 的文件路径），'
                '或 {file: <相对路径>}'
            ),
            received=f'system_prompt={value!r}，类型是 {type(value).__name__}',
            next_action='写成提示词正文，或写成 {file: docs/agents/x.md} 点名一个文件',
        )
    return _implicit_prompt_file(value, origin)


def _implicit_prompt_file(text: str, origin: str | None) -> Path | None:
    """origin 目录内、相对路径指向真实文件时交回它；否则 text 就是提示词。

    探测失败一律当正文：一段内联提示词里任何一节超过文件系统的名字上限，`is_file` 就抛
    `OSError`，而它本来就不是路径。多行的字符串不去碰文件系统。
    """
    if origin is None or '\n' in text:
        return None
    try:
        relative = Path(text)
        if relative.is_absolute() or '..' in relative.parts:
            return None
        base = Path(origin)
        if not base.is_dir():
            return None
        candidate = (base / relative).resolve()
        candidate.relative_to(base.resolve())
        return candidate if candidate.is_file() else None
    except (OSError, ValueError):
        return None


def _explicit_prompt_file(table: Mapping[object, object], origin: str | None) -> Path:
    if set(table) != {'file'} or not isinstance(table.get('file'), str):
        raise ConfigError(
            expected='system_prompt 写成映射时只有一个键 file，值是相对路径字符串',
            received=f'system_prompt={dict(table)!r}',
            next_action='写成 {file: docs/agents/x.md}；提示词正文直接写成字符串',
        )
    shown = cast(str, table['file'])
    if not shown.strip():
        raise ConfigError(
            expected='system_prompt.file 是非空的相对路径',
            received=f'system_prompt.file={shown!r}',
            next_action='写上提示词文件相对清单所在目录的路径',
        )
    if origin is None:
        raise ConfigError(
            expected='system_prompt.file 相对清单所在的目录解析',
            received='这份清单没有所在目录（从纯 Mapping 载入）',
            next_action='从文件载入清单，或把提示词正文直接写进 system_prompt',
        )
    base = Path(origin)
    relative = Path(shown)
    try:
        rooted = base.resolve()
        candidate = (base / relative).resolve()
    except (OSError, ValueError) as exc:
        raise ConfigError(
            expected=f'system_prompt.file 是 {origin} 之内的一个文件',
            received=f'解析 {shown!r} 失败：{exc}',
            next_action='改成清单所在目录之内的相对路径',
        ) from exc
    try:
        candidate.relative_to(rooted)
    except ValueError:
        raise ConfigError(
            expected=f'system_prompt.file 是 {rooted} 之内的一个文件',
            received=f'{shown!r} 解析到 {candidate}，在它之外',
            next_action='把提示词文件放进清单所在目录之内，写相对路径；不写绝对路径与 ..',
        ) from None
    try:
        exists = candidate.is_file()
    except OSError as exc:
        raise ConfigError(
            expected=f'system_prompt.file 是 {rooted} 之内的一个文件',
            received=f'检查 {shown!r} 失败：{exc}',
            next_action='改成一个更短、真实存在的相对路径',
        ) from exc
    if relative.is_absolute() or not exists:
        raise ConfigError(
            expected=f'system_prompt.file 是 {rooted} 之内的一个文件',
            received=(
                f'{shown!r} 是绝对路径'
                if relative.is_absolute()
                else f'{candidate} 不存在或不是文件'
            ),
            next_action='写清单所在目录之内的相对路径，并确认文件存在',
        )
    return candidate
