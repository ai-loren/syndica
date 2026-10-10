"""v1.0 公开 API 的快照与英文参考页。

快照（`tests/testdata/public_api.json`）记下承诺面的每一处形状：公开模块与它们的 `__all__`、
每个名字的种类、可调用对象的签名、类的公开成员、Protocol 的成员、Enum 的取值；以及不在 Python
名字里的承诺：切点名、内置 Module 的 Flag key、`.syndica.yaml` 收的键与 Agent 清单字段、CLI 的
命令树、HTTP 契约的摘要。参考页（`docs/user/reference/api.md`）由同一份快照加
`public_api_descriptions.json` 里每个名字一句英文说明生成。

`tests/test_public_api_snapshot.py` 重新生成两者并逐字比对；`SYNDICA_UPDATE_PUBLIC_API=1` 时重写。
快照的 diff 就是公开面的变化，由人 review：加名字不破坏，删名字、改签名破坏。

输出必须与机器无关：键排序、签名按源码里的字符串标注写、默认值的 repr 去掉内存地址、拒绝绝对路径。
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import hashlib
import importlib
import inspect
import json
import re
import sys
import types
import typing
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePath
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / 'tests' / 'testdata' / 'public_api.json'
REFERENCE = ROOT / 'docs' / 'user' / 'reference' / 'api.md'
DESCRIPTIONS = Path(__file__).resolve().with_name('public_api_descriptions.json')
OPENAPI = ROOT / 'docs' / 'reference' / 'openapi.json'
SCHEMA = 'syndica.public_api/1'

# 整个 `__all__` 都是承诺面的模块，按参考页里的次序。
PUBLIC_MODULES: tuple[str, ...] = (
    'syndica',
    'syndica.kernel',
    'syndica.kernel.contracts',
    'syndica.runner',
    'syndica.backends.llm_openai',
    'syndica.backends.llm_router',
    'syndica.backends.otel_otlp',
    'syndica.backends.trl',
)
# 只承诺列出的名字的模块：写第三方 store、导出器与读回放文档的人从这里取，模块 `__all__` 里其余的
# 名字是内部的。与 `tests/kernel/test_public_api.py` 的 `_PROMISED_VIA_SUBMODULE` 是同一份裁决。
PROMISED_VIA_SUBMODULE: dict[str, tuple[str, ...]] = {
    'syndica.kernel.events': ('KERNEL_EVENTS',),
    'syndica.kernel.file_taint': ('TaintAware',),
    'syndica.kernel.isolation': ('IsolatedCopy',),
    'syndica.kernel.scenario_schema': (
        'SCENARIO_EVENT_KINDS',
        'DangerPolicySpec',
        'DangerRuleSpec',
        'FrozenRole',
        'GateRecord',
        'HostAction',
        'HostProfile',
        'ModuleRecord',
        'ScenarioCall',
        'ScenarioEntry',
        'ScenarioError',
        'ScenarioExpectation',
        'ScenarioInstance',
        'ScenarioReply',
        'ScenarioResponse',
        'ScenarioRole',
        'ScenarioSource',
        'SkippedCall',
        'Text',
        'ToolOutcome',
    ),
    'syndica.kernel.tool_executor': ('ToolOutcomeObserver',),
    'syndica.kernel.trace': ('ATTRIBUTE_DOMAINS', 'ATTRIBUTE_KINDS', 'TRACE_SCHEMA'),
    'syndica.kernel.workspace': ('ANY', 'Expected'),
}
# 参考页里模块的次序。
MODULE_ORDER: tuple[str, ...] = (
    *PUBLIC_MODULES[:3],
    *sorted(PROMISED_VIA_SUBMODULE),
    *PUBLIC_MODULES[3:],
)
# 取值随发布变、不属于形状的常量：只记类型。版本号由打包那一侧改，不该让快照跟着变红。
VOLATILE: frozenset[tuple[str, str]] = frozenset({('syndica', '__version__')})
# 公开契约是命令、文件格式与线上协议而不是 Python 名字的面：它们的模块一律是内部的。
SURFACES: dict[str, str] = {
    'syndica.cli': (
        'The `syndica` command: its subcommands and options (snapshotted under `cli`), exit codes, '
        'the `.syndica.yaml` schema (under `config`) and the handoff file format.'
    ),
    'syndica.server': (
        'The HTTP / SSE / WebSocket API served by `syndica up`, frozen by '
        '`docs/reference/openapi.json` and its change log `docs/reference/api-changes.md` '
        '(digest under `http`).'
    ),
}
_PATH_IN_TEXT = re.compile(r'(?:/Users/|/home/|/tmp/|/private/|[A-Za-z]:\\)')
_ADDRESS = re.compile(r' at 0x[0-9A-Fa-f]+')
# 运行期求值的标注（类型别名）带着定义它的模块路径；源码里的字符串标注不带。去掉模块前缀，两者写法
# 一致，内部模块挪动也不改快照。
_MODULE_PREFIX = re.compile(r'(?<![\'"\w.])(?:[a-z_][a-z0-9_]*\.)+(?=[A-Za-z_])')
_JSONABLE_LIMIT = 4000


class _Raw:
    """在 `inspect.Signature` 里按原文显示的标注或默认值。"""

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


def _checked(text: str) -> str:
    if _PATH_IN_TEXT.search(text):
        raise ValueError(
            f'expected the public API snapshot to be machine independent; got an absolute path in '
            f'{text!r}. Give that default a relative or symbolic value, or render it explicitly in '
            'scripts/public_api.py.'
        )
    return _ADDRESS.sub('', text)


def _annotation(value: object) -> str:
    if isinstance(value, str):
        return ' '.join(value.split())
    if value is type(None):
        return 'None'
    if isinstance(value, type) and not typing.get_args(value):
        return value.__qualname__
    return _checked(_MODULE_PREFIX.sub('', inspect.formatannotation(value)))


def _value(value: object) -> str:
    if isinstance(value, enum.Enum):
        return f'{type(value).__qualname__}.{value.name}'
    if isinstance(value, PurePath):
        return _checked(f'Path({value.as_posix()!r})')
    if isinstance(value, frozenset | set):
        items = sorted(_value(item) for item in cast(Iterable[object], value))
        return (
            f'{type(value).__name__}({{{", ".join(items)}}})'
            if items
            else f'{type(value).__name__}()'
        )
    if inspect.isfunction(value) or inspect.isbuiltin(value):
        return f'<function {value.__qualname__}>'
    if isinstance(value, type):
        return value.__qualname__
    text = repr(value)
    if text.startswith('<') and text.endswith('>'):
        return f'<{type(value).__qualname__}>'
    return _checked(text)


def _signature(target: Callable[..., object], *, drop_first: bool) -> str:
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return '(...)'
    parameters = list(signature.parameters.values())
    if drop_first and parameters:
        parameters = parameters[1:]
    rendered = [
        parameter.replace(
            annotation=(
                parameter.annotation
                if parameter.annotation is inspect.Parameter.empty
                else _Raw(_annotation(parameter.annotation))
            ),
            default=(
                parameter.default
                if parameter.default is inspect.Parameter.empty
                else _Raw(_value(parameter.default))
            ),
        )
        for parameter in parameters
    ]
    returns = signature.return_annotation
    return str(
        signature.replace(
            parameters=rendered,
            return_annotation=(
                returns if returns is inspect.Signature.empty else _Raw(_annotation(returns))
            ),
        )
    )


def _jsonable(value: object, depth: int = 6) -> object:
    """常量的取值：能原样进 JSON 的照写（映射、序列、集合排序后），不能的抛 TypeError。"""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, enum.Enum):
        return _value(value)
    if depth <= 0:
        raise TypeError('too deep')
    if isinstance(value, Mapping):
        items = cast(Mapping[object, object], value)
        if not all(isinstance(key, str) for key in items):
            raise TypeError('non-string key')
        return {cast(str, key): _jsonable(item, depth - 1) for key, item in items.items()}
    if isinstance(value, frozenset | set):
        return sorted(
            (_jsonable(item, depth - 1) for item in cast(Iterable[object], value)),
            key=lambda item: json.dumps(item, sort_keys=True),
        )
    if isinstance(value, list | tuple):
        return [_jsonable(item, depth - 1) for item in cast(Iterable[object], value)]
    raise TypeError(type(value).__name__)


def _constant(value: object) -> dict[str, object]:
    entry: dict[str, object] = {'kind': 'constant', 'type': type(value).__qualname__}
    try:
        data = _jsonable(value)
    except TypeError:
        if isinstance(value, tuple | list | frozenset | set):
            items = list(cast(Iterable[object], value))
            entry['items'] = len(items)
            entry['item_types'] = sorted({type(item).__qualname__ for item in items})
        else:
            entry['repr'] = _value(value)
        return entry
    if len(json.dumps(data, sort_keys=True)) > _JSONABLE_LIMIT:
        raise ValueError(
            f'expected a public constant small enough to review in the snapshot; got '
            f'{type(value).__qualname__} rendering to more than {_JSONABLE_LIMIT} characters. '
            'Summarise it in scripts/public_api.py instead of dumping it.'
        )
    entry['value'] = data
    return entry


def _is_alias(value: object) -> bool:
    return isinstance(value, types.UnionType | types.GenericAlias) or (
        typing.get_origin(value) is not None and not isinstance(value, type)
    )


def _owner(cls: type, name: str) -> type | None:
    return next((klass for klass in cls.__mro__ if name in vars(klass)), None)


def _ours(klass: type) -> bool:
    return klass.__module__ == 'syndica' or klass.__module__.startswith('syndica.')


def _annotations(cls: type) -> dict[str, str]:
    found: dict[str, str] = {}
    for klass in reversed(cls.__mro__):
        if not _ours(klass):
            continue
        for name, annotation in inspect.get_annotations(klass).items():
            if not name.startswith('_') and not _annotation(annotation).startswith('ClassVar'):
                found[name] = _annotation(annotation)
    return found


def _members(cls: type, *, skip: Iterable[str] = ()) -> dict[str, dict[str, object]]:
    skipped = set(skip)
    members: dict[str, dict[str, object]] = {}
    for name in dir(cls):
        if name.startswith('_') or name in skipped:
            continue
        owner = _owner(cls, name)
        if owner is None or not _ours(owner):
            continue
        raw = vars(owner)[name]
        if isinstance(raw, staticmethod):
            function = cast(Callable[..., object], raw.__func__)
            members[name] = {
                'kind': 'staticmethod',
                'signature': _signature(function, drop_first=False),
            }
        elif isinstance(raw, classmethod):
            function = cast(Callable[..., object], raw.__func__)
            members[name] = {
                'kind': 'classmethod',
                'signature': _signature(function, drop_first=True),
            }
        elif isinstance(raw, property):
            getter = raw.fget
            returns = inspect.signature(getter).return_annotation if getter is not None else None
            members[name] = {
                'kind': 'property',
                'type': '' if returns in (None, inspect.Signature.empty) else _annotation(returns),
                'settable': raw.fset is not None,
            }
        elif inspect.isfunction(raw):
            members[name] = {
                'kind': 'async method' if inspect.iscoroutinefunction(raw) else 'method',
                'signature': _signature(raw, drop_first=True),
            }
        elif isinstance(raw, type):
            members[name] = {'kind': 'class'}
        else:
            annotations = _annotations(cls)
            entry: dict[str, object] = {'kind': 'attribute'}
            if name in annotations:
                entry['type'] = annotations[name]
            if not (inspect.ismemberdescriptor(raw) or inspect.isgetsetdescriptor(raw)):
                entry['value'] = _value(raw)
            members[name] = entry
    for name, annotation in _annotations(cls).items():
        if name not in members and name not in skipped:
            members[name] = {'kind': 'attribute', 'type': annotation}
    return dict(sorted(members.items()))


def _bases(cls: type, public_paths: Mapping[int, str]) -> list[str]:
    names: list[str] = []
    for base in cls.__bases__:
        if base is object:
            continue
        if _ours(base):
            names.append(public_paths.get(id(base), f'{base.__module__}.{base.__qualname__}'))
        else:
            names.append(base.__qualname__)
    return names


def _class(cls: type, public_paths: Mapping[int, str]) -> dict[str, object]:
    bases = _bases(cls, public_paths)
    if issubclass(cls, enum.Enum):
        return {
            'kind': 'enum',
            'bases': bases,
            'members': {member.name: _jsonable(member.value) for member in cls},
        }
    if getattr(cls, '_is_protocol', False):
        return {
            'kind': 'protocol',
            'bases': bases,
            'runtime_checkable': bool(getattr(cls, '_is_runtime_protocol', False)),
            'members': _members(cls),
        }
    entry: dict[str, object] = {
        'kind': 'exception' if issubclass(cls, BaseException) else 'class',
        'bases': bases,
    }
    fields: list[str] = []
    if dataclasses.is_dataclass(cls):
        entry['kind'] = 'dataclass'
        params = cast(Any, cls).__dataclass_params__
        entry['frozen'] = bool(params.frozen)
        entry['fields'] = [
            {
                'name': field.name,
                'type': _annotation(field.type),
                'default': (
                    _value(field.default)
                    if field.default is not dataclasses.MISSING
                    else '<factory>'
                    if field.default_factory is not dataclasses.MISSING
                    else None
                ),
                'init': field.init,
                'kw_only': field.kw_only is True,
            }
            for field in dataclasses.fields(cls)
            if not field.name.startswith('_')
        ]
        fields = [field.name for field in dataclasses.fields(cls)]
    if (
        '__init__' in vars(cls)
        or dataclasses.is_dataclass(cls)
        or _owner(cls, '__init__')
        not in (
            object,
            BaseException,
            Exception,
        )
    ):
        entry['init'] = _signature(cls, drop_first=False)
    entry['members'] = _members(cls, skip=fields)
    return entry


def _entry(value: object, public_paths: Mapping[int, str]) -> dict[str, object]:
    if isinstance(value, type):
        return _class(value, public_paths)
    if _is_alias(value):
        return {'kind': 'type alias', 'value': _annotation(value)}
    if inspect.isfunction(value):
        return {
            'kind': 'async function' if inspect.iscoroutinefunction(value) else 'function',
            'signature': _signature(value, drop_first=False),
        }
    return _constant(value)


def public_names() -> dict[str, tuple[str, ...]]:
    """模块 → 它承诺的名字（排序）。"""
    names: dict[str, tuple[str, ...]] = {}
    for module_name in PUBLIC_MODULES:
        module = importlib.import_module(module_name)
        names[module_name] = tuple(sorted(cast(list[str], module.__all__)))
    for module_name, promised in PROMISED_VIA_SUBMODULE.items():
        names[module_name] = tuple(sorted(promised))
    return names


def _modules() -> dict[str, object]:
    names = public_names()
    public_paths: dict[int, str] = {}
    for module_name, module_names in names.items():
        module = importlib.import_module(module_name)
        for name in module_names:
            public_paths.setdefault(id(getattr(module, name)), f'{module_name}.{name}')
    rendered: dict[str, object] = {}
    for module_name, module_names in names.items():
        module = importlib.import_module(module_name)
        entries: dict[str, object] = {}
        for name in module_names:
            value = getattr(module, name)
            entries[name] = (
                {'kind': 'constant', 'type': type(value).__qualname__}
                if (module_name, name) in VOLATILE
                else _entry(value, public_paths)
            )
        rendered[module_name] = {
            'scope': 'module' if module_name in PUBLIC_MODULES else 'listed names',
            'all': list(module_names),
            'names': entries,
        }
    return rendered


def _junctions() -> dict[str, object]:
    from syndica.cli.host import reference_modules
    from syndica.kernel.hooks.junctions import INTERCEPTABLE, Junction

    return {
        'kernel': {
            junction.value: 'interceptable' if junction in INTERCEPTABLE else 'observable'
            for junction in Junction
        },
        'builtin_modules': {
            handle.name: handle.kind.value
            for module in reference_modules()
            for handle in module.junctions
        },
    }


def _flags() -> list[dict[str, object]]:
    from syndica.cli.host import reference_modules
    from syndica.kernel import flag_reference

    return [
        {
            'key': row.key,
            'module': row.module,
            'kind': row.kind,
            'default': row.default,
            'choices': list(row.choices),
            'minimum': row.minimum,
            'maximum': row.maximum,
        }
        for row in flag_reference(reference_modules())
    ]


def _config() -> dict[str, object]:
    from syndica.cli import config, interop_config
    from syndica.kernel import manifest

    manifest_fields = sorted(manifest._KNOWN_KEYS)  # pyright: ignore[reportPrivateUsage]
    syndica_yaml: dict[str, frozenset[str]] = {
        '': config._ROOT_KEYS,  # pyright: ignore[reportPrivateUsage]
        'agents.<name>': frozenset(manifest_fields),
        'danger': config._DANGER_KEYS,  # pyright: ignore[reportPrivateUsage]
        'danger.roles.<role>': config._ROLE_DANGER_KEYS,  # pyright: ignore[reportPrivateUsage]
        'interop': interop_config._INTEROP_KEYS,  # pyright: ignore[reportPrivateUsage]
        'interop.a2a': interop_config._A2A_KEYS,  # pyright: ignore[reportPrivateUsage]
        'interop.a2a.agents[]': interop_config._A2A_AGENT_KEYS,  # pyright: ignore[reportPrivateUsage]
        'interop.acp': interop_config._ACP_KEYS,  # pyright: ignore[reportPrivateUsage]
        'interop.acp.agents[]': interop_config._ACP_AGENT_KEYS,  # pyright: ignore[reportPrivateUsage]
        'interop.mcp': interop_config._MCP_KEYS,  # pyright: ignore[reportPrivateUsage]
        'interop.mcp.serve': interop_config._SERVE_KEYS,  # pyright: ignore[reportPrivateUsage]
        'interop.mcp.servers[]': interop_config._SERVER_KEYS,  # pyright: ignore[reportPrivateUsage]
        'prices.<model>': config._PRICE_KEYS,  # pyright: ignore[reportPrivateUsage]
        'redaction': config._REDACTION_KEYS,  # pyright: ignore[reportPrivateUsage]
        'redaction.rules[]': config._RULE_KEYS,  # pyright: ignore[reportPrivateUsage]
        'topologies.<name>': config._TOPOLOGY_KEYS,  # pyright: ignore[reportPrivateUsage]
        'topologies.<name>.slots[]': config._SLOT_KEYS,  # pyright: ignore[reportPrivateUsage]
        'trace': config._TRACE_KEYS,  # pyright: ignore[reportPrivateUsage]
        'workspace': config._WORKSPACE_KEYS,  # pyright: ignore[reportPrivateUsage]
    }
    interop_yaml: dict[str, frozenset[str]] = {
        '': interop_config._CREDENTIAL_ROOT,  # pyright: ignore[reportPrivateUsage]
        'credentials.<name>': interop_config._CREDENTIAL_FIELDS,  # pyright: ignore[reportPrivateUsage]
    }
    return {
        'agent_manifest': manifest_fields,
        'interop.yaml': {path: sorted(keys) for path, keys in interop_yaml.items()},
        'syndica.yaml': {path: sorted(keys) for path, keys in syndica_yaml.items()},
    }


def _commands(parser: argparse.ArgumentParser, path: str, out: dict[str, object]) -> None:
    options: list[str] = []
    positionals: list[str] = []
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
            positionals.append(f'<{action.dest}>')
            for name, sub in sorted(
                cast(dict[str, argparse.ArgumentParser], action.choices).items()
            ):
                _commands(sub, f'{path} {name}', out)
            continue
        if isinstance(action, argparse._HelpAction):  # pyright: ignore[reportPrivateUsage]
            continue
        choices = '' if action.choices is None else '{' + ','.join(map(str, action.choices)) + '}'
        if action.option_strings:
            options.append(
                ' / '.join(sorted(action.option_strings)) + (f' {choices}' if choices else '')
            )
        else:
            positionals.append(f'{action.dest}{choices}')
    out[path] = {'positionals': positionals, 'options': sorted(options)}


def _cli() -> dict[str, object]:
    from syndica.cli.main import _parser  # pyright: ignore[reportPrivateUsage]

    tree: dict[str, object] = {}
    _commands(_parser(), 'syndica', tree)
    return dict(sorted(tree.items()))


def _http() -> dict[str, object]:
    document = json.loads(OPENAPI.read_text(encoding='utf-8'))
    return {
        'openapi': 'docs/reference/openapi.json',
        'info_version': document['info']['version'],
        'sha256_12': hashlib.sha256(OPENAPI.read_bytes()).hexdigest()[:12],
    }


def snapshot() -> dict[str, object]:
    return {
        'schema': SCHEMA,
        'modules': _modules(),
        'surfaces': dict(SURFACES),
        'junctions': _junctions(),
        'flags': _flags(),
        'config': _config(),
        'cli': _cli(),
        'http': _http(),
    }


def render_snapshot(data: Mapping[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n'


def descriptions() -> dict[str, dict[str, str]]:
    loaded = json.loads(DESCRIPTIONS.read_text(encoding='utf-8'))
    return cast(dict[str, dict[str, str]], loaded)


def missing_descriptions(
    names: Mapping[str, tuple[str, ...]], described: Mapping[str, Mapping[str, str]]
) -> list[str]:
    return sorted(
        f'{module}.{name}'
        for module, module_names in names.items()
        for name in module_names
        if not described.get(module, {}).get(name, '').strip()
    )


def stray_descriptions(
    names: Mapping[str, tuple[str, ...]], described: Mapping[str, Mapping[str, str]]
) -> list[str]:
    return sorted(
        f'{module}.{name}'
        for module, module_descriptions in described.items()
        for name in module_descriptions
        if name not in names.get(module, ())
    )


_INTRO = """\
# Python API reference

<!-- Generated by scripts/public_api.py from tests/testdata/public_api.json. Do not edit by hand:
     run `SYNDICA_UPDATE_PUBLIC_API=1 uv run pytest tests/test_public_api_snapshot.py`. -->

This page lists every Python name Syndica promises to keep compatible in v1.x. A name that is not
listed here is internal, even if you can import it: internal modules may move or change in any
release.

- **Import public names from the module they are listed under.** Everything kernel-side is
  re-exported from `syndica.kernel`; its subpackages (`syndica.kernel.llm`,
  `syndica.kernel.stores`, ...) are internal paths.
- **Adding a name, an optional keyword argument or a class member is not a breaking change; removing
  or renaming one is.** The snapshot in `tests/testdata/public_api.json` records every signature,
  and a change to it is reviewed as an API change.
- **Runtime messages are in Chinese in v1.0.** Error texts, CLI output and code comments stay
  Chinese; every error still carries the expected / received / next-action triple (see
  [Errors](errors.md)). Translating them is planned for v1.x.
"""

_SURFACES_INTRO = """\
## Surfaces whose contract is not their Python names

These packages ship in the wheel, but you program against their commands, files and wire protocol,
not their Python modules:
"""

_PROVISIONAL = """\
## Provisional in v1.0

The following kernel pieces ship and are used by the built-in Modules, but are **not** part of the
v1.0 promise. They may change in a minor release; they will move into this page (without breaking
anyone who used the listed names) once a third-party implementation needs them:

- The memory, rubric and artifact stores (`MemoryStore`, `RubricStore`, `ArtifactStore`), their
  file and in-memory implementations, and their contract suites. The `artifacts=` parameter of
  `Runtime` takes an `ArtifactStore` and is provisional with it.
- `AcceptanceModule` and `LongHorizonModule`, whose constructors take those stores. A
  `LongHorizonStrategy` is installed today through
  `syndica.kernel.long_horizon_module.LongHorizonModule(acceptance, strategy=...)`; the strategy
  protocol, its context and actions, and `LongHorizonStrategyContract` are public.
- Reading and granting the budget tree from host code. Strategies read the budget from
  `LongHorizonContext.budget`; hosts grant more through `BudgetPolicy.ask_human`.
"""

_EXTRA_PROMISES = """\
## Promises that are not Python names

The snapshot also freezes, and changes to them are reviewed the same way:

- the hook junction names and whether each is interceptable or observable (`junctions`);
- the Flag keys the built-in Modules declare, with their types, defaults, choices and bounds
  (`flags`); descriptions live in `docs/reference/flags.md`;
- the keys `.syndica.yaml` and `interop.yaml` accept, and the agent manifest fields (`config`);
- the CLI command tree (`cli`);
- the digest of the HTTP API snapshot (`http`).
"""


def _heading(module: str) -> str:
    return f'`{module}`'


def _signature_of(entry: Mapping[str, object], name: str) -> str | None:
    kind = entry['kind']
    if kind in ('function', 'async function'):
        prefix = 'async def ' if kind == 'async function' else 'def '
        return f'{prefix}{name}{entry["signature"]}'
    if 'init' in entry:
        return f'{name}{entry["init"]}'
    if kind == 'type alias':
        return f'{name} = {entry["value"]}'
    if kind == 'constant' and 'value' in entry:
        rendered = json.dumps(entry['value'], ensure_ascii=False)
        if len(rendered) <= 80:
            return f'{name} = {rendered}'
    return None


def _kind_label(entry: Mapping[str, object]) -> str:
    kind = cast(str, entry['kind'])
    if kind == 'protocol':
        return 'protocol (runtime-checkable)' if entry.get('runtime_checkable') else 'protocol'
    return kind


def render_reference(data: Mapping[str, Any], described: Mapping[str, Mapping[str, str]]) -> str:
    lines: list[str] = [_INTRO, '## Public modules', '']
    modules = cast(Mapping[str, Mapping[str, Any]], data['modules'])
    order = [name for name in MODULE_ORDER if name in modules]
    for module_name in order:
        scope = modules[module_name]['scope']
        note = '' if scope == 'module' else ' (only the names listed here)'
        lines.append(f'- [{_heading(module_name)}](#{_anchor(module_name)}){note}')
    lines.append('')
    for module_name in order:
        module = modules[module_name]
        lines.append(f'## {_heading(module_name)}')
        lines.append('')
        if module['scope'] != 'module':
            lines.append('Only the names below are public; the rest of this module is internal.')
            lines.append('')
        if not module['all']:
            lines.append('Nothing is exported from this module.')
            lines.append('')
        for name in module['all']:
            entry = cast(Mapping[str, object], module['names'][name])
            lines.append(f'### `{name}`')
            lines.append('')
            lines.append(f'*{_kind_label(entry)}* — {described[module_name][name]}')
            lines.append('')
            signature = _signature_of(entry, name)
            if signature is not None:
                lines.extend(['```python', signature, '```', ''])
            if entry['kind'] == 'enum':
                members = cast(Mapping[str, object], entry['members'])
                rendered = ', '.join(
                    f'`{key}` = `{json.dumps(value)}`' for key, value in members.items()
                )
                lines.append(f'Members: {rendered}.')
                lines.append('')
            if entry['kind'] == 'dataclass':
                fields = cast(list[Mapping[str, object]], entry['fields'])
                if fields:
                    rendered = ', '.join(f'`{field["name"]}: {field["type"]}`' for field in fields)
                    lines.append(f'Fields: {rendered}.')
                    lines.append('')
            members = cast(Mapping[str, Mapping[str, object]], entry.get('members', {}))
            if entry['kind'] != 'enum' and members:
                lines.append('Members:')
                lines.append('')
                for member, info in members.items():
                    lines.append(f'- {_member_line(member, info)}')
                lines.append('')
    lines.append(_SURFACES_INTRO)
    for module_name, text in cast(Mapping[str, str], data['surfaces']).items():
        lines.append(f'- `{module_name}`: {text}')
    lines.append('')
    lines.append(_PROVISIONAL)
    lines.append(_EXTRA_PROMISES)
    return '\n'.join(lines).rstrip('\n') + '\n'


def _member_line(member: str, info: Mapping[str, object]) -> str:
    kind = info['kind']
    if kind in ('method', 'async method', 'classmethod', 'staticmethod'):
        prefix = 'async ' if kind == 'async method' else ''
        label = '' if kind in ('method', 'async method') else f' ({kind})'
        return f'`{prefix}{member}{info["signature"]}`{label}'
    if kind == 'property':
        typed = f': {info["type"]}' if info.get('type') else ''
        return f'`{member}{typed}` (property{", settable" if info.get("settable") else ""})'
    if kind == 'attribute':
        typed = f': {info["type"]}' if info.get('type') else ''
        return f'`{member}{typed}`'
    return f'`{member}` ({kind})'


def _anchor(module_name: str) -> str:
    return re.sub(r'[^a-z0-9_-]', '', module_name.lower())


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('--write', action='store_true', help='rewrite the snapshot and the page')
    arguments = parser.parse_args(argv)
    data = snapshot()
    described = descriptions()
    names = public_names()
    missing = missing_descriptions(names, described)
    stray = stray_descriptions(names, described)
    if missing or stray:
        print(
            f'expected one description per public name in {DESCRIPTIONS.name}; '
            f'missing {missing}, not public {stray}. Add or remove those entries and run again.',
            file=sys.stderr,
        )
        return 1
    snapshot_text = render_snapshot(data)
    page = render_reference(json.loads(snapshot_text), described)
    if arguments.write:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(snapshot_text, encoding='utf-8')
        REFERENCE.parent.mkdir(parents=True, exist_ok=True)
        REFERENCE.write_text(page, encoding='utf-8')
        return 0
    stale = [
        str(path.relative_to(ROOT))
        for path, text in ((SNAPSHOT, snapshot_text), (REFERENCE, page))
        if not path.is_file() or path.read_text(encoding='utf-8') != text
    ]
    if stale:
        print(
            f'expected the committed public API files to match the code; {stale} differ. '
            'Review the change, then run `uv run python scripts/public_api.py --write`.',
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
