"""项目配置的信任确认（M4 D15）。

工作区之内的配置是不受信的来源：Agent 改得到它，而它决定跑哪些命令、装哪些代码。第一次用、或摘要变了
时要人确认，确认之前不执行配置里的任何东西（不 import `impl:`、不跑第三方 Module）。

**只读一次**：配置文件与它引用的每个提示词文件在这里读一遍，摘要算的、给人看的、交给内核的都是这一份；
内核拿到的提示词是正文、不带 origin，不会再按路径读。人答完之后重读一遍再比：检查与动作之间隔着一次
问人，问题挂着的这段时间文件照样能被改。
"""

from __future__ import annotations

import atexit
import base64
import functools
import hashlib
import importlib.machinery
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast

import syndica
from syndica.cli.config import CliConfig, config_from_text, parse_yaml
from syndica.cli.modules import Discovered
from syndica.cli.terminal import one_line
from syndica.kernel import ConfigError
from syndica.kernel.manifest import prompt_file

__all__ = [
    'NO_TRUST_GATE',
    'STORE_ENV',
    'ConfigSnapshot',
    'JsonTrustStore',
    'TrustGate',
    'TrustStore',
    'config_of',
    'describe',
    'private_pycache_prefix',
    'read_snapshot',
    'real_trust_gate',
    'store_path',
    'within',
]

# Agent 的环境里宿主把它设到这次 run 的临时目录：Agent 里嵌套起来的 syndica 写的是那份存储，碰不到
# 用户的。
STORE_ENV = 'SYNDICA_TRUST_STORE'
_DIGEST_FORMAT = 'syndica-trust/1'


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def within(path: Path, root: Path) -> bool:
    """path 解析之后是不是 root 或在它之下。"""
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


@dataclass(frozen=True)
class ImplSource:
    """一个 `impl:` 目标。`files` 是它在工作区之内时整棵顶层包的文件（相对包根的路径 → 内容）；
    工作区之外的、syndica 自己的代码不摘，`where` 说明是哪一种。"""

    index: int
    target: str
    where: str
    root: Path | None
    files: tuple[tuple[str, bytes], ...] = ()


@dataclass(frozen=True)
class ConfigSnapshot:
    """读一遍得到的全部输入，与它们的摘要。`path_entries` 是 `sys.path` 里落在工作区之内的条目：
    只展示、不进摘要——那里的 `.pth` 在任何确认之前就已经跑过了。"""

    path: Path
    text: str
    prompts: Mapping[int, tuple[Path, str]]
    impls: tuple[ImplSource, ...]
    modules: tuple[Discovered, ...]
    digest: str
    path_entries: tuple[str, ...] = ()
    module_sources: tuple[ImplSource, ...] = ()


def _read(path: Path, what: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConfigError(
            expected=f'{what} {str(path)!r} 可读',
            received=f'读取失败：{type(exc).__name__}: {exc}',
            next_action='检查文件的权限与路径后重试',
        ) from exc


def _decoded(data: bytes, path: Path, what: str) -> str:
    """按 `read_text` 的通用换行解码：内核按路径读提示词时就是这样读的，CRLF 的文件交给模型的字节
    不能因为改由这里读而变——那会让前缀缓存整体失效。"""
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ConfigError(
            expected=f'{what} {str(path)!r} 是 utf-8 文本',
            received=f'解码失败：{exc}',
            next_action='把文件保存成 utf-8 后重试',
        ) from exc
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _package_files(root: Path) -> Iterator[tuple[str, bytes]]:
    """包目录下除 `__pycache__` 之外的每个文件，按相对路径排序。不 import 任何东西。"""
    if root.is_file():
        yield root.name, root.read_bytes()
        return
    found: list[Path] = []
    for directory, subdirs, names in os.walk(root):
        subdirs[:] = sorted(name for name in subdirs if name != '__pycache__')
        found.extend(Path(directory) / name for name in names)
    for path in sorted(found):
        if path.is_file():
            yield path.relative_to(root).as_posix(), path.read_bytes()


def _found_spec(top: str) -> ModuleSpec | None:
    """按 import 系统自己的次序逐个问 `sys.meta_path` 上的 finder，不 import。只问 `PathFinder` 时，
    可编辑安装的 finder 认得而它认不出的代码会被当成工作区之外、不进摘要。"""
    for finder in sys.meta_path:
        find_spec = getattr(finder, 'find_spec', None)
        if find_spec is None:
            continue
        try:
            spec = find_spec(top, None)
        except (ImportError, ValueError):
            return None
        if spec is not None:
            return cast(ModuleSpec, spec)
    return None


_NOT_FROM_FILES: tuple[object, ...] = (
    importlib.machinery.BuiltinImporter,
    importlib.machinery.FrozenImporter,
)


def _unpinned(target: str, spec: ModuleSpec | None) -> ConfigError:
    shown = (
        '它已经载入，但没有 __spec__'
        if spec is None
        else f'import 系统找到的是 origin={spec.origin!r}、loader={type(spec.loader).__name__}'
    )
    return ConfigError(
        expected=f'impl: {target!r} 的代码是文件系统里的普通包或模块文件，信任确认才摘得到它',
        received=shown,
        next_action=(
            '把它做成普通的包（目录里有 __init__.py）或模块文件；'
            'zip、命名空间包与自定义加载器摘不到'
        ),
    )


def _spec_root(target: str, spec: ModuleSpec) -> Path | None:
    """spec 指向的代码在文件系统里的根：包是它唯一的目录，模块是它的文件。None 是内置或冻结的代码。
    别的一律报错：摘不到的代码放过去，等于不进摘要就跑。"""
    if not spec.has_location:
        if cast(object, spec.loader) in _NOT_FROM_FILES:
            return None
        raise _unpinned(target, spec)
    if spec.origin is None or not Path(spec.origin).is_file():
        raise _unpinned(target, spec)
    locations = list(spec.submodule_search_locations or ())
    if not locations:
        return Path(spec.origin)
    if len(locations) != 1 or Path(spec.origin).parent != Path(locations[0]):
        raise _unpinned(target, spec)
    return Path(locations[0])


def _impl_source(index: int, target: str, workspace: Path) -> ImplSource:
    """找 `impl:` 的顶层包，不 import：`find_spec('a.b')` 会先 import `a`，那是在确认之前跑代码。
    已经载入的用它自己的 spec（`import_module` 不会再找一遍）。工作区之内的整棵顶层包进摘要：父包的
    `__init__.py` 与它 import 的兄弟模块都会跑。"""
    top = target.partition(':')[0].split('.')[0]
    loaded = sys.modules.get(top)
    if loaded is not None:
        spec = cast('ModuleSpec | None', getattr(loaded, '__spec__', None))
        if spec is None:
            raise _unpinned(target, None)
    else:
        spec = _found_spec(top)
        if spec is None:
            return ImplSource(index, target, '找不到（信任之后 import 时会报错）', None)
    root = _spec_root(target, spec)
    if root is None:
        return ImplSource(index, target, '不是文件里的代码', None)
    if within(root, Path(syndica.__file__).parent):
        return ImplSource(index, target, 'syndica 自己的代码', root)
    if not within(root, workspace):
        return ImplSource(index, target, '工作区之外，不进摘要；信任它就是信任那份代码', root)
    return ImplSource(
        index, target, '工作区之内，整棵包进了摘要', root, tuple(_package_files(root))
    )


def read_snapshot(
    path: Path, workspace: Path, discovered: Sequence[Discovered] = ()
) -> ConfigSnapshot:
    """读配置与它引用的提示词文件，找 `impl:` 的代码，算摘要。`discovered` 是发现的第三方 Module，
    配置启用的那些的元数据进摘要（只读元数据，不 import）。"""
    file = path.resolve()
    raw = _read(file, '配置文件')
    text = _decoded(raw, file, '配置文件')
    root = parse_yaml(file, text)
    named = root.get('modules', ())
    wanted = set(cast(Sequence[object], named)) if isinstance(named, list) else set[object]()
    enabled = tuple(module for module in discovered if module.name in wanted)
    origin = str(file.parent)
    prompts: dict[int, tuple[Path, str]] = {}
    prompt_bytes: dict[int, bytes] = {}
    impls: list[ImplSource] = []
    agents = root.get('agents', ())
    entries = cast(Sequence[object], agents) if isinstance(agents, list) else ()
    for index, item in enumerate(entries):
        if not isinstance(item, Mapping):
            continue
        table = cast(Mapping[object, object], item)
        prompt = prompt_file(table.get('system_prompt', ''), origin)
        if prompt is not None:
            data = _read(prompt, 'system_prompt 指向的文件')
            prompts[index] = (prompt, _decoded(data, prompt, 'system_prompt 指向的文件'))
            prompt_bytes[index] = data
        target = table.get('impl')
        if isinstance(target, str):
            impls.append(_impl_source(index, target, workspace))
    lines = [_DIGEST_FORMAT, json.dumps(['config', str(file), _sha(raw)])]
    for index in sorted(prompts):
        lines.append(
            json.dumps(['prompt', index, str(prompts[index][0]), _sha(prompt_bytes[index])])
        )
    for impl in impls:
        lines.append(json.dumps(['impl', impl.index, impl.target, impl.where]))
        lines.extend(json.dumps(['file', name, _sha(data)]) for name, data in impl.files)
    module_sources: list[ImplSource] = []
    for module in enabled:
        lines.append(
            json.dumps(['module', module.name, module.target, module.distribution, module.version])
        )
        source = _impl_source(-1, module.target, workspace)
        module_sources.append(source)
        lines.append(json.dumps(['module-code', module.name, source.where]))
        lines.extend(
            json.dumps(['module-file', module.name, name, _sha(data)])
            for name, data in source.files
        )
        lines.extend(
            json.dumps(['module-record', module.name, name, sha])
            for name, sha in _verified_record(module, workspace)
        )
    digest = _sha('\n'.join(lines).encode('utf-8'))
    return ConfigSnapshot(
        path=file,
        text=text,
        prompts=MappingProxyType(prompts),
        impls=tuple(impls),
        modules=tuple(enabled),
        digest=digest,
        path_entries=_workspace_path_entries(workspace),
        module_sources=tuple(module_sources),
    )


def _verified_record(module: Discovered, workspace: Path) -> list[tuple[str, str]]:
    """装在工作区之内的发行版：每个文件按它的 `RECORD` 核对哈希，交回 (路径, sha256) 进摘要。
    Agent 写得到工作区里的 `.venv`，第一次确认之前就可能改过那里装好的代码。`__pycache__` 不算：
    Python import 时就会重写它，算进来摘要每次都变。装在工作区之外的不核对。"""
    dist = module.dist
    files = cast('Sequence[object] | None', getattr(dist, 'files', None))
    locate = getattr(dist, 'locate_file', None)
    if not files or not callable(locate):
        return []
    if not within(Path(cast(str, locate(''))), workspace):
        return []
    verified: list[tuple[str, str]] = []
    for entry in files:
        shown = str(entry)
        if '__pycache__' in Path(shown).parts:
            continue
        path = Path(cast(str, locate(entry)))
        data = _read(path, f'发行版 {module.distribution} 的文件')
        recorded = getattr(entry, 'hash', None)
        if recorded is not None:
            mode = str(getattr(recorded, 'mode', ''))
            try:
                digest = hashlib.new(mode, data).digest()
            except ValueError:
                raise _record_mismatch(
                    module, shown, f'RECORD 用了认不出的哈希算法 {mode!r}'
                ) from None
            actual = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
            if actual != getattr(recorded, 'value', None):
                raise _record_mismatch(module, shown, '内容与安装时记下的哈希不符')
        verified.append((shown, _sha(data)))
    return verified


def _record_mismatch(module: Discovered, path: str, why: str) -> ConfigError:
    return ConfigError(
        expected=(
            f'第三方 Module {module.name!r} 的发行版 {module.distribution} {module.version} '
            '在工作区之内的文件与它的 RECORD 一致'
        ),
        received=f'{path}：{why}',
        next_action=(
            '它装好之后被改过（工作区里的东西 Agent 都写得到）：重新安装这个发行版再确认；'
            '把 syndica 与它装在工作区之外更稳妥'
        ),
    )


def _workspace_path_entries(workspace: Path) -> tuple[str, ...]:
    entries = (entry or os.getcwd() for entry in sys.path)
    return tuple(dict.fromkeys(entry for entry in entries if within(Path(entry), workspace)))


@functools.cache
def private_pycache_prefix() -> str:
    """本进程自己的字节码目录：每个进程新建一个（0700），退出时删掉。固定的目录 Agent 的 Bash 写得
    进去，而 unchecked-hash 的 `.pyc` 从不与源文件比对，种在那里的就会被当成缓存。"""
    path = tempfile.mkdtemp(prefix='syndica-pycache-')
    atexit.register(shutil.rmtree, path, True)
    return path


def config_of(snapshot: ConfigSnapshot) -> CliConfig:
    """快照里的那一份配置：提示词按读好的正文，不再碰文件。"""
    return config_from_text(
        snapshot.path,
        snapshot.text,
        prompts={index: text for index, (_, text) in snapshot.prompts.items()},
    )


_GUTTER = '    │ '


def _section(title: str, items: Sequence[str]) -> list[str]:
    if not items:
        return [f'  {title}: 无']
    return [f'  {title}:', *(f'{_GUTTER}{one_line(item)}' for item in items)]


def describe(snapshot: ConfigSnapshot, config: CliConfig) -> str:
    """给人看的确认内容。每一项都过 `one_line` 并带固定的前缀：一个值伪造不出一行确认。"""
    catalog = config.catalog
    lines = [
        '这份项目配置第一次用（或改过了）。确认之前不会执行它里面的任何东西。',
        f'  配置: {one_line(str(snapshot.path))}',
        f'  摘要: {snapshot.digest}',
    ]
    lines += _section('提示词文件', [str(path) for path, _ in snapshot.prompts.values()])
    lines += _section('impl', [f'{impl.target}（{impl.where}）' for impl in snapshot.impls])
    lines += _section(
        'sys.path 里工作区之内的条目（Agent 写得到；那里的 .pth 在任何确认之前就会运行，'
        '摘要盖不住）',
        list(snapshot.path_entries),
    )
    lines += _section(
        '角色的模型', [f'{name}: {catalog.get(name).model}' for name in catalog.names()]
    )
    lines += _section('workspace.verify', list(config.workspace.verify))
    lines += _section('pass_env', list(config.workspace.pass_env))
    lines += _section(
        'modules',
        [
            *config.modules,
            *(
                f'{module.name} 来自 {module.distribution} {module.version}（{module.target}，'
                f'{source.where}）；能力在信任之后用 syndica modules show 查看'
                for module, source in zip(snapshot.modules, snapshot.module_sources, strict=True)
            ),
        ],
    )
    lines += _section('danger.roles（只能收紧）', sorted(config.danger_roles))
    lines += _section(
        'redaction.rules（只能多脱敏）', [rule.rule_id for rule in config.redaction_rules]
    )
    return '\n'.join(lines)


class TrustStore(Protocol):
    @property
    def path(self) -> Path: ...

    def get(self, config: Path) -> str | None: ...

    def put(self, config: Path, digest: str) -> None: ...


class JsonTrustStore:
    """按配置文件的绝对路径记摘要的 JSON 文件。读不出按「未信任」处理；写是原子的（临时文件 +
    rename），写到一半被打断不会留下半份存储。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def _entries(self) -> dict[str, str]:
        try:
            data = json.loads(self._path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        configs = cast(dict[str, object], data).get('configs') if isinstance(data, dict) else None
        if not isinstance(configs, dict):
            return {}
        return {
            str(key): value
            for key, value in cast(dict[object, object], configs).items()
            if isinstance(value, str)
        }

    def get(self, config: Path) -> str | None:
        return self._entries().get(str(config))

    def put(self, config: Path, digest: str) -> None:
        entries = self._entries()
        entries[str(config)] = digest
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f'.{self._path.name}.{os.getpid()}.tmp')
        temporary.write_text(
            json.dumps({'version': 1, 'configs': entries}, indent=2, sort_keys=True) + '\n',
            encoding='utf-8',
        )
        os.replace(temporary, self._path)


def store_path(environ: Mapping[str, str]) -> Path | None:
    """信任存储的位置：`SYNDICA_TRUST_STORE`，否则 `$XDG_CONFIG_HOME/syndica/trusted.json`，否则
    `~/.config/syndica/trusted.json`。都没有时交回 None。"""
    explicit = environ.get(STORE_ENV)
    if explicit:
        return Path(explicit)
    xdg = environ.get('XDG_CONFIG_HOME')
    if xdg:
        return Path(xdg) / 'syndica' / 'trusted.json'
    home = environ.get('HOME')
    if home:
        return Path(home) / '.config' / 'syndica' / 'trusted.json'
    return None


@dataclass(frozen=True)
class TrustGate:
    """宿主的信任闸。`store_for` 交回这个宿主用的存储；为 None 时不设闸（只给用例与嵌入方显式
    选择）。"""

    store_for: Callable[[Mapping[str, str]], TrustStore | None] | None


NO_TRUST_GATE = TrustGate(store_for=None)


def _json_store(environ: Mapping[str, str]) -> TrustStore | None:
    path = store_path(environ)
    return None if path is None else JsonTrustStore(path)


def real_trust_gate() -> TrustGate:
    """组合根用的信任闸：存储在 `store_path` 那里。"""
    return TrustGate(store_for=_json_store)
