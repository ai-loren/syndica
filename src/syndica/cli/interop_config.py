"""项目配置里的 `interop:` 与用户级 `interop.yaml`。

项目配置只按名字引用外部对象。点名形似密钥的环境变量在这里拒收：那些名字只能出现在工作区之外的
用户级文件里，而且那里也只存变量名、不存值。用户级文件权限必须是 0600，放进工作区就拒——Agent
改得到工作区里的文件。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import cast

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from syndica.cli.environment import refuse_env_name, valid_env_name
from syndica.kernel import AgentCatalog, ConfigError
from syndica.kernel.redaction import looks_secret

__all__ = [
    'A2aAgent',
    'AcpAgent',
    'InteropConfig',
    'InteropCredential',
    'McpServer',
    'bind_interop',
    'described_targets',
    'interop_credentials_path',
    'load_interop_credentials',
    'unmatched_credentials',
]

_NAME = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')
_CREDENTIAL_KEY = re.compile(r'^(mcp|acp|a2a):[a-z][a-z0-9_-]{0,31}$')
_DEFAULT_SERVE = ('Read', 'Grep', 'Glob')
_INTEROP_KEYS = frozenset({'mcp', 'acp', 'a2a'})
_MCP_KEYS = frozenset({'servers', 'serve'})
_SERVER_KEYS = frozenset({'name', 'command', 'url', 'pass_env', 'roles', 'high_risk', 'low_risk'})
_SERVE_KEYS = frozenset({'tools'})
_ACP_KEYS = frozenset({'agents'})
_ACP_AGENT_KEYS = frozenset({'name', 'command', 'pass_env'})
_A2A_KEYS = frozenset({'agents'})
_A2A_AGENT_KEYS = frozenset({'name', 'url'})
_CREDENTIAL_ROOT = frozenset({'credentials'})
_CREDENTIAL_FIELDS = frozenset({'env', 'bearer_env'})


def _error(expected: str, received: str, next_action: str) -> ConfigError:
    return ConfigError(expected=expected, received=received, next_action=next_action)


class _UniqueLoader(yaml.SafeLoader):
    """拒重复键：后写的那份会静默盖掉前一份，密钥名单因此可以被换掉而配置仍能加载。"""


def _unique_mapping(
    loader: _UniqueLoader, node: MappingNode, deep: bool = False
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


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _unique_mapping,
)


@dataclass(frozen=True)
class McpServer:
    name: str
    command: tuple[str, ...] | None
    url: str | None
    pass_env: tuple[str, ...]
    roles: tuple[str, ...]
    high_risk: tuple[str, ...]
    low_risk: tuple[str, ...]
    # 编辑器在 session/new 里给的环境变量值。项目配置不写这个字段。
    env_values: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class AcpAgent:
    name: str
    command: tuple[str, ...]
    pass_env: tuple[str, ...]


@dataclass(frozen=True)
class A2aAgent:
    name: str
    url: str


@dataclass(frozen=True)
class InteropConfig:
    """`.syndica.yaml` 的 `interop:`。`present` 为假表示整段省略。"""

    mcp_servers: tuple[McpServer, ...] = ()
    mcp_serve_tools: tuple[str, ...] = _DEFAULT_SERVE
    acp_agents: tuple[AcpAgent, ...] = ()
    a2a_agents: tuple[A2aAgent, ...] = ()
    present: bool = False


@dataclass(frozen=True)
class InteropCredential:
    """用户级文件里的一条：只存要读的变量名。`env` 透传给外部进程，`bearer_env` 的值放进鉴权头。"""

    env: tuple[str, ...] = ()
    bearer_env: str | None = None


def bind_interop(value: object, catalog: AgentCatalog) -> InteropConfig:
    """解析 `interop:` 并核对角色名与 `model: acp:<名>` / `a2a:<名>`。"""
    interop = InteropConfig() if value == {} else _interop(value)
    _check_roles(interop, catalog)
    _check_models(catalog, interop)
    return interop


def described_targets(interop: InteropConfig) -> tuple[str, ...]:
    """给人看的 command / url，逐字。整段省略时不列。"""
    if not interop.present:
        return ()
    rows: list[str] = []
    for server in interop.mcp_servers:
        target = server.url if server.url is not None else _shown(server.command)
        rows.append(f'mcp {server.name}: {target}')
    rows.append('mcp serve: ' + ', '.join(interop.mcp_serve_tools))
    for agent in interop.acp_agents:
        rows.append(f'acp {agent.name}: {_shown(agent.command)}')
    for agent in interop.a2a_agents:
        rows.append(f'a2a {agent.name}: {agent.url}')
    return tuple(rows)


def _shown(command: tuple[str, ...] | None) -> str:
    return ' '.join(command or ())


def interop_credentials_path(environ: Mapping[str, str]) -> Path | None:
    """与信任存储同一目录的 `interop.yaml`。没有可放信任存储的目录时是 None。"""
    from syndica.cli.trust import store_path

    store = store_path(environ)
    if store is None:
        return None
    return store.parent / 'interop.yaml'


def load_interop_credentials(path: Path, *, workspace: Path) -> Mapping[str, InteropCredential]:
    """读用户级凭证名单。文件不存在是空表；在工作区之内、或权限不是 0600，三件套拒绝。"""
    if not path.exists():
        return MappingProxyType({})
    if not path.is_file():
        raise _error(
            'interop.yaml 是普通文件',
            f'收到 {str(path)!r}，它存在但不是文件',
            '把用户级凭证名单写成那个路径上的文件，或删掉占着这个名字的目录',
        )
    resolved = path.resolve()
    root = workspace.resolve()
    if _inside(resolved, root):
        raise _error(
            'interop.yaml 在工作区之外（与信任存储同一目录）',
            f'收到 {str(resolved)!r}，它在工作区 {str(root)!r} 之内',
            (
                '把 interop.yaml 挪到信任存储所在的目录；'
                'Agent 改得到工作区里的文件，密钥变量名不能放在那里'
            ),
        )
    mode = resolved.stat().st_mode & 0o777
    if mode != 0o600:
        raise _error(
            'interop.yaml 的权限是 0600（只有本人可读写）',
            f'收到 {str(resolved)!r}，权限是 {mode:04o}',
            (
                f'运行 chmod 600 {resolved} 后再读；别的用户读得到这份名单，'
                '就知道你把哪些密钥交给了外部进程'
            ),
        )
    try:
        text = resolved.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        raise _error(
            'interop.yaml 可按 UTF-8 读取',
            f'读取 {str(resolved)!r} 失败：{type(exc).__name__}: {exc}',
            '检查文件权限与编码后重试',
        ) from exc
    return _credentials(resolved, text)


def unmatched_credentials(
    credentials: Mapping[str, InteropCredential], interop: InteropConfig
) -> tuple[str, ...]:
    """用户级名单里、这份项目配置没有声明的外部对象。

    doctor 报警告，不报错：这份文件跨工作区共用。
    """
    declared = {
        *(f'mcp:{server.name}' for server in interop.mcp_servers),
        *(f'acp:{agent.name}' for agent in interop.acp_agents),
        *(f'a2a:{agent.name}' for agent in interop.a2a_agents),
    }
    return tuple(sorted(key for key in credentials if key not in declared))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _interop(value: object) -> InteropConfig:
    raw = _mapping(value, 'interop', _INTEROP_KEYS)
    mcp = _mapping(raw.get('mcp', {}), 'interop.mcp', _MCP_KEYS)
    servers = tuple(
        _mcp_server(index, item)
        for index, item in enumerate(_sequence(mcp.get('servers', ()), 'interop.mcp.servers'))
    )
    _unique_names([server.name for server in servers], 'interop.mcp.servers')
    if 'serve' in mcp:
        serve = _mapping(mcp.get('serve'), 'interop.mcp.serve', _SERVE_KEYS)
        tools = _strings(serve.get('tools', ()), 'interop.mcp.serve.tools')
    else:
        tools = _DEFAULT_SERVE
    acp = _mapping(raw.get('acp', {}), 'interop.acp', _ACP_KEYS)
    acp_agents = tuple(
        _acp_agent(index, item)
        for index, item in enumerate(_sequence(acp.get('agents', ()), 'interop.acp.agents'))
    )
    _unique_names([agent.name for agent in acp_agents], 'interop.acp.agents')
    a2a = _mapping(raw.get('a2a', {}), 'interop.a2a', _A2A_KEYS)
    a2a_agents = tuple(
        _a2a_agent(index, item)
        for index, item in enumerate(_sequence(a2a.get('agents', ()), 'interop.a2a.agents'))
    )
    _unique_names([agent.name for agent in a2a_agents], 'interop.a2a.agents')
    return InteropConfig(
        mcp_servers=servers,
        mcp_serve_tools=tools,
        acp_agents=acp_agents,
        a2a_agents=a2a_agents,
        present=True,
    )


def _check_roles(interop: InteropConfig, catalog: AgentCatalog) -> None:
    known = set(catalog.names())
    for server in interop.mcp_servers:
        missing = [role for role in server.roles if role not in known]
        if missing:
            raise _error(
                f'interop.mcp.servers 里 {server.name} 的 roles 是已声明的角色',
                f'没有这些角色：{missing!r}；已有 {sorted(known)!r}',
                '改成 agents 里的 name，或内置的 Planner / Explorer / Worker / Orchestrator',
            )


def _check_models(catalog: AgentCatalog, interop: InteropConfig) -> None:
    acp = {agent.name for agent in interop.acp_agents}
    a2a = {agent.name for agent in interop.a2a_agents}
    for role in catalog.names():
        model = catalog.get(role).model
        if model.startswith('acp:'):
            _known_model(role, model, 'acp', acp)
        elif model.startswith('a2a:'):
            _known_model(role, model, 'a2a', a2a)


def _known_model(role: str, model: str, kind: str, declared: set[str]) -> None:
    target = model.split(':', 1)[1]
    if _NAME.fullmatch(target) and target in declared:
        return
    raise _error(
        f'角色 {role} 的 model {model!r} 指向 interop.{kind}.agents 里已声明的名字',
        f'收到 {target!r}，已声明的是 {sorted(declared)!r}',
        (
            f'在 interop.{kind}.agents 里加上 name: {target or "<名字>"}，'
            f'或把 model 改成已经声明的 {kind}:<名字>；名字是 [a-z][a-z0-9_-]{{0,31}}'
        ),
    )


def _mcp_server(index: int, value: object) -> McpServer:
    field = f'interop.mcp.servers[{index}]'
    raw = _mapping(value, field, _SERVER_KEYS)
    command = raw.get('command', None)
    url = raw.get('url', None)
    has_command = 'command' in raw
    has_url = 'url' in raw
    if has_command == has_url:
        raise _error(
            f'{field} 恰好有 command（字符串列表）或 url（http/https）其中之一',
            f'command={"有" if has_command else "无"}，url={"有" if has_url else "无"}',
            (
                'stdio 的 server 写 command: [程序, 参数...]；HTTP 的写 url。'
                '不要两个都写，也不要经 shell'
            ),
        )
    high = _strings(raw.get('high_risk', ()), f'{field}.high_risk')
    low = _strings(raw.get('low_risk', ()), f'{field}.low_risk')
    both = sorted(set(high) & set(low))
    if both:
        raise _error(
            f'{field} 的 high_risk 与 low_risk 没有重叠',
            f'同时出现在两边的有 {both!r}',
            '每个外部工具只归一边：高风险问人（没人就拒绝），低风险放行并记录',
        )
    return McpServer(
        name=_object_name(raw.get('name'), f'{field}.name'),
        command=_command(command, f'{field}.command') if has_command else None,
        url=_url(url, f'{field}.url') if has_url else None,
        pass_env=_env_names(raw.get('pass_env', ()), f'{field}.pass_env'),
        roles=_strings(raw.get('roles', ()), f'{field}.roles'),
        high_risk=high,
        low_risk=low,
    )


def _acp_agent(index: int, value: object) -> AcpAgent:
    field = f'interop.acp.agents[{index}]'
    raw = _mapping(value, field, _ACP_AGENT_KEYS)
    if 'command' not in raw:
        raise _error(
            f'{field} 有 command（字符串列表，不经 shell）',
            f'收到键 {sorted(str(key) for key in raw)!r}',
            '写成 command: [npx, "-y", "@google/gemini-cli", "--experimental-acp"]',
        )
    return AcpAgent(
        name=_object_name(raw.get('name'), f'{field}.name'),
        command=_command(raw.get('command'), f'{field}.command'),
        pass_env=_env_names(raw.get('pass_env', ()), f'{field}.pass_env'),
    )


def _a2a_agent(index: int, value: object) -> A2aAgent:
    field = f'interop.a2a.agents[{index}]'
    raw = _mapping(value, field, _A2A_AGENT_KEYS)
    return A2aAgent(
        name=_object_name(raw.get('name'), f'{field}.name'),
        url=_url(raw.get('url'), f'{field}.url'),
    )


def _credentials(path: Path, text: str) -> Mapping[str, InteropCredential]:
    try:
        raw = yaml.load(text, Loader=_UniqueLoader)
    except yaml.YAMLError as exc:
        raise _error(
            'interop.yaml 是单文档 safe YAML Mapping，且每个映射键只出现一次',
            f'{str(path)!r} 解析失败：{exc}',
            '修正 YAML 语法或重复键；不要把密钥的值写进这个文件',
        ) from exc
    root = _mapping(raw, 'interop.yaml', _CREDENTIAL_ROOT)
    table = _mapping(root.get('credentials', {}), 'interop.yaml credentials', None)
    parsed: dict[str, InteropCredential] = {}
    for key, item in table.items():
        if _CREDENTIAL_KEY.fullmatch(key) is None:
            raise _error(
                'credentials 的键是 mcp:<名字>、acp:<名字> 或 a2a:<名字>',
                f'收到键 {key!r}',
                '名字与项目配置里的 name 相同，且是 [a-z][a-z0-9_-]{0,31}；这里只写变量名',
            )
        parsed[key] = _credential(key, item)
    return MappingProxyType(parsed)


def _credential(key: str, value: object) -> InteropCredential:
    raw = _mapping(value, f'credentials.{key}', _CREDENTIAL_FIELDS)
    env = _env_var_names(raw.get('env', ()), f'credentials.{key}.env')
    bearer = raw.get('bearer_env')
    if 'env' not in raw and 'bearer_env' not in raw:
        raise _error(
            f'credentials.{key} 有 env（变量名列表）或 bearer_env（一个变量名）',
            '两个都没写',
            'env 是额外透传给外部进程的变量；bearer_env 的值放进 Authorization: Bearer',
        )
    parsed_bearer = None
    if 'bearer_env' in raw:
        parsed_bearer = _one_env_name(bearer, f'credentials.{key}.bearer_env')
    return InteropCredential(env=env, bearer_env=parsed_bearer)


def _env_var_names(value: object, field: str) -> tuple[str, ...]:
    """用户级文件允许像密钥的名字，但仍必须是变量名，不能是值。"""
    names = _strings(value, field)
    for name in names:
        if not valid_env_name(name):
            raise _error(
                f'{field} 的每一项是环境变量名，不是密钥的值',
                f'收到 {name!r}',
                '只写变量名，例如 GEMINI_API_KEY；值留在你的环境里，不要写进这个文件',
            )
    return names


def _one_env_name(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise _error(
            f'{field} 是一个环境变量名',
            f'收到 {value!r}，类型是 {type(value).__name__}',
            '写成 bearer_env: TRACKER_TOKEN；YAML 的 yes / true 要加引号，而且那也不是变量名',
        )
    names = _env_var_names((value,), field)
    return names[0]


def _mapping(value: object, field: str, allowed: frozenset[str] | None) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _error(
            f'{field} 是 Mapping',
            f'收到 {value!r}，类型是 {type(value).__name__}',
            f'把 {field} 写成 YAML 对象；不需要就省略',
        )
    raw = cast(Mapping[object, object], value)
    bad = [key for key in raw if not isinstance(key, str)]
    if bad:
        raise _error(
            f'{field} 的键是字符串',
            f'非字符串键为 {bad!r}',
            '改成字段名',
        )
    if allowed is not None:
        unknown = [str(key) for key in raw if str(key) not in allowed]
        if unknown:
            _refuse_secret_keys(unknown, field)
            raise _error(
                f'{field} 只含 {sorted(allowed)!r}',
                f'未知键 {sorted(unknown)!r}',
                '删除这些键。项目配置不能写凭证；变量名放用户级 interop.yaml',
            )
    return cast(Mapping[str, object], raw)


def _refuse_secret_keys(keys: Sequence[str], field: str) -> None:
    named = [key for key in keys if looks_secret(key)]
    if named:
        raise _error(
            f'{field} 不含像密钥的字段（KEY、TOKEN、AUTH 这类）',
            f'收到 {named!r}',
            (
                '删掉这些字段。要交给外部进程的密钥只在用户级 interop.yaml 里点名变量，'
                '不在项目配置里写值'
            ),
        )


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise _error(
            f'{field} 是 YAML 列表',
            f'收到 {field}={value!r}，类型是 {type(value).__name__}',
            f'把 {field} 写成 `- ...` 列表；没有内容就写 [] 或省略',
        )
    return cast(Sequence[object], value)


def _strings(value: object, field: str) -> tuple[str, ...]:
    items = _sequence(value, field)
    seen: set[str] = set()
    result: list[str] = []
    for index, item in enumerate(items):
        if isinstance(item, bool) or not isinstance(item, str) or not item.strip() or '\0' in item:
            raise _error(
                f'{field}[{index}] 是非空白、不含 NUL 字节的字符串',
                f'收到 {item!r}，类型是 {type(item).__name__}',
                (
                    f'YAML 把 on / off / yes / no / true 读成布尔值；要当字符串就给 {field} 加引号'
                    if isinstance(item, bool)
                    else f'把 {field} 写成字符串列表，删掉空项与 NUL 字节'
                ),
            )
        if any(ord(char) < 32 for char in item):
            raise _error(
                f'{field}[{index}] 不含控制字符',
                f'收到 {item!r}',
                '删掉换行和控制字符；command 是参数列表，不是一段脚本',
            )
        if item in seen:
            raise _error(
                f'{field} 的每一项只出现一次',
                f'{item!r} 重复出现',
                '删掉重复的那一项',
            )
        seen.add(item)
        result.append(item)
    return tuple(result)


def _command(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raise _error(
            f'{field} 是字符串列表，不经 shell',
            f'收到字符串 {value!r}',
            '写成 [程序, 参数, ...]；一个字符串会被 shell 拆开，引号和变量展开都由对方解释',
        )
    items = _strings(value, field)
    if not items:
        raise _error(
            f'{field} 至少有程序名',
            '收到空列表',
            '写成 [程序, 参数, ...]',
        )
    for item in items:
        head = item.split('=', 1)[0]
        if looks_secret(item) or looks_secret(head):
            raise _error(
                f'{field} 的参数不像密钥变量名',
                f'收到 {item!r}',
                (
                    '从 command 里删掉它。密钥变量名写在用户级 interop.yaml，'
                    '由宿主读进环境，不写进 argv'
                ),
            )
    return items


def _url(value: object, field: str) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        raise _error(
            f'{field} 是 http:// 或 https:// 开头的字符串',
            f'收到 {value!r}，类型是 {type(value).__name__}',
            '写成 url: https://mcp.example.com/mcp；不要写命令，也不要夹控制字符',
        )
    if not (value.startswith('https://') or value.startswith('http://')):
        raise _error(
            f'{field} 以 http:// 或 https:// 开头',
            f'收到 {value!r}',
            '改成 http 或 https URL。别的 scheme 不会被当 Streamable HTTP 或 A2A 端点去连',
        )
    return value


def _object_name(value: object, field: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise _error(
            f'{field} 是 [a-z][a-z0-9_-]{{0,31}}',
            f'收到 {value!r}，类型是 {type(value).__name__}',
            '用小写名字，例如 files、gemini；它会出现在模型名 acp:<名字> 和用户级凭证的键里',
        )
    return value


def _unique_names(names: list[str], field: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise _error(
                f'{field} 的 name 互不重复',
                f'{name!r} 出现了多次',
                '合并成一份，或改一个名字；model: 前缀只按这个名字找',
            )
        seen.add(name)


def _env_names(value: object, field: str) -> tuple[str, ...]:
    names = _strings(value, field)
    for name in names:
        refuse_env_name(name, field)
    return names
