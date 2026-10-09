"""项目配置里的 `interop:` 与用户级 `interop.yaml`。

项目配置只按名字引用外部对象。点名形似密钥的环境变量在这里拒收：那些名字只能出现在工作区之外的
用户级文件里，而且那里也只存变量名、不存值。用户级文件权限必须是 0600，放进工作区就拒——Agent
改得到工作区里的文件。

用户级的每一项凭证都钉住自己的目的地（`url` 或 `command`），只交给项目配置里同名、且目的地与它
逐字相同的外部对象：名字和目的地都写在项目配置里，只按名字绑定，等于让项目配置决定你的凭证发往
哪里。
"""

from __future__ import annotations

import json
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
    'credential_for',
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
_CREDENTIAL_FIELDS = frozenset({'url', 'command', 'env', 'bearer_env'})
_PINS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {'a2a': ('url',), 'acp': ('command',), 'mcp': ('url', 'command')}
)
_LOOPBACK = re.compile(r'(?:127\.0\.0\.1|\[::1\]|localhost)(?::[0-9]{1,5})?')


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
    """用户级文件里的一条：钉住的目的地，加上要读的变量名。

    `url` 与 `bearer_env` 成对（A2A Agent、HTTP 的 MCP 服务：值放进 Authorization: Bearer），
    `command` 与 `env` 成对（托管的 ACP Agent、stdio 的 MCP 服务：值透传进那个进程的环境）。
    """

    url: str | None = None
    command: tuple[str, ...] | None = None
    env: tuple[str, ...] = ()
    bearer_env: str | None = None


_Target = McpServer | AcpAgent | A2aAgent
_Pin = tuple[str, str | tuple[str, ...] | None]


def bind_interop(value: object, catalog: AgentCatalog) -> InteropConfig:
    """解析 `interop:` 并核对角色名与 `model: acp:<名>` / `a2a:<名>`。"""
    interop = InteropConfig() if value == {} else _interop(value)
    _check_roles(interop, catalog)
    _check_models(catalog, interop)
    return interop


def described_targets(
    interop: InteropConfig, credentials: Mapping[str, InteropCredential] | None = None
) -> tuple[str, ...]:
    """给人看的 command / url，逐字，各带一句哪些凭证变量会交给它。整段省略时不列。

    `credentials` 是读好的用户级名单；不给时只写明哪一项、在什么条件下会交给它。
    """
    if not interop.present:
        return ()
    rows: list[str] = []
    for server in interop.mcp_servers:
        target = server.url if server.url is not None else _shown(server.command)
        rows.append(f'mcp {server.name}: {target}{_sending(server, credentials)}')
    rows.append('mcp serve: ' + ', '.join(interop.mcp_serve_tools))
    for agent in interop.acp_agents:
        rows.append(f'acp {agent.name}: {_shown(agent.command)}{_sending(agent, credentials)}')
    for agent in interop.a2a_agents:
        rows.append(f'a2a {agent.name}: {agent.url}{_sending(agent, credentials)}')
    return tuple(rows)


def credential_for(
    credentials: Mapping[str, InteropCredential], target: McpServer | AcpAgent | A2aAgent
) -> InteropCredential | None:
    """项目配置里的 `target` 可用的那一项用户级凭证。用户级文件里没有它的名字时是 None。

    目的地逐字比较：`url` 比整个字符串，`command` 比整个参数列表。大小写、默认端口、末尾斜杠都
    不归一化：归一化要挑一个 URL 解析器，而真正发请求的客户端可能把同一个字符串解析到别的主机。
    对不上时三件套拒绝，这个外部对象不启动。
    """
    key = _key_of(target)
    credential = credentials.get(key)
    if credential is None:
        return None
    pinned = _pin_of(credential)
    declared = _target_pin(target)
    if pinned != declared:
        raise _error(
            (
                f'用户级 interop.yaml 的 {key} 只把 {_names(credential)} '
                f'交给它钉住的 {_pin_text(pinned)}'
            ),
            f'项目配置里的 {key} 指向 {_pin_text(declared)}，与钉住的不逐字相同',
            (
                f'项目配置里的 {declared[0]} 就是你要连的，就把 interop.yaml 里 {key} 的 '
                f'{declared[0]} 改成与它逐字相同；不是的话别用这份项目配置。改好之前 {key} 不启动：'
                '项目配置选的目的地拿不到你的凭证，不带凭证连过去也只会换来说不清原因的鉴权失败'
            ),
        )
    return credential


def _key_of(target: _Target) -> str:
    if isinstance(target, McpServer):
        return f'mcp:{target.name}'
    if isinstance(target, AcpAgent):
        return f'acp:{target.name}'
    return f'a2a:{target.name}'


def _target_pin(target: _Target) -> _Pin:
    if isinstance(target, A2aAgent):
        return ('url', target.url)
    if isinstance(target, McpServer) and target.url is not None:
        return ('url', target.url)
    return ('command', target.command)


def _pin_of(credential: InteropCredential) -> _Pin:
    if credential.url is not None:
        return ('url', credential.url)
    return ('command', credential.command)


def _pin_text(pin: _Pin) -> str:
    field, value = pin
    if isinstance(value, tuple):
        return f'{field} {json.dumps(list(value), ensure_ascii=False)}'
    return f'{field} {value}'


def _names(credential: InteropCredential) -> str:
    if credential.bearer_env is not None:
        return f'{credential.bearer_env}（Authorization: Bearer）'
    if not credential.env:
        return '（没有变量）'
    return '、'.join(credential.env)


def _sending(target: _Target, credentials: Mapping[str, InteropCredential] | None) -> str:
    key = _key_of(target)
    declared = _target_pin(target)
    if credentials is None:
        return f'；凭证：只交用户级 interop.yaml 里 {declared[0]} 与此逐字相同的 {key} 点名的变量'
    credential = credentials.get(key)
    if credential is None:
        return f'；凭证：无（用户级 interop.yaml 没有 {key}）'
    pinned = _pin_of(credential)
    if pinned != declared:
        return (
            f'；凭证：不交——用户级 {key} 钉的是 {_pin_text(pinned)}，对不上，'
            f'{_names(credential)} 不发、启动时拒绝'
        )
    return f'；凭证：{_names(credential)} 交给这里'


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
    """读用户级凭证名单。文件不存在是空表；在工作区之内、权限不是 0600、某一项没钉住目的地、或
    bearer 要走明文离开本机，三件套拒绝。"""
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
    field = f'credentials.{key}'
    raw = _mapping(value, field, _CREDENTIAL_FIELDS)
    if _pin_field(key, raw, field) == 'url':
        _absent(
            raw,
            'env',
            field,
            'env 只透传给本机拉起的进程，url 那头是远端，到不了它；远端的凭证写 bearer_env',
        )
        url = _url(raw.get('url'), f'{field}.url')
        bearer = _one_env_name(
            _required(raw, 'bearer_env', field, '写上 bearer_env: <变量名>，或删掉这一项'),
            f'{field}.bearer_env',
        )
        _refuse_cleartext_bearer(url, f'{field}.url')
        return InteropCredential(url=url, bearer_env=bearer)
    _absent(
        raw,
        'bearer_env',
        field,
        'bearer_env 只发给 url 那头的远端；command 拉起的进程用 env 透传变量',
    )
    command = _command(raw.get('command'), f'{field}.command')
    env = _env_var_names(
        _required(raw, 'env', field, '写上 env: [<变量名>, ...]，或删掉这一项'),
        f'{field}.env',
    )
    return InteropCredential(command=command, env=env)


def _pin_field(key: str, raw: Mapping[str, object], field: str) -> str:
    kind, name = key.split(':', 1)
    allowed = _PINS[kind]
    given = [pin for pin in ('url', 'command') if pin in raw]
    if len(given) == 1 and given[0] in allowed:
        return given[0]
    section = {'a2a': 'a2a.agents', 'acp': 'acp.agents', 'mcp': 'mcp.servers'}[kind]
    if kind == 'mcp':
        wanted = 'url（HTTP 服务，配 bearer_env）或 command（stdio 服务，配 env）其中之一'
        copy = 'url 或 command'
    else:
        wanted = allowed[0]
        copy = allowed[0]
    raise _error(
        (
            f'{field} 恰好有 {wanted}，钉住凭证的目的地：与项目配置 interop.{section} 里 {name} '
            '的同名字段逐字相同'
        ),
        f'收到键 {sorted(raw)!r}',
        (
            f'把项目配置里 {name} 的 {copy} 原样抄进来；凭证只交给目的地与它逐字相同的外部对象，'
            '项目配置换了目的地就不再交'
        ),
    )


def _required(raw: Mapping[str, object], key: str, field: str, next_action: str) -> object:
    if key not in raw:
        raise _error(
            f'{field} 有 {key}',
            f'没写；收到键 {sorted(raw)!r}，只钉了目的地、没有凭证',
            next_action,
        )
    return raw[key]


def _absent(raw: Mapping[str, object], key: str, field: str, next_action: str) -> None:
    if key in raw:
        raise _error(
            f'{field} 不含 {key}',
            f'收到键 {sorted(raw)!r}',
            next_action,
        )


def _refuse_cleartext_bearer(url: str, field: str) -> None:
    """明文 http 只在本机回环上带 bearer：出了本机，路上任何一跳都读得到这个头。

    主机部分取 `http://` 之后到第一个 `/`、`?`、`#` 为止，整段与回环名单逐字匹配：带 userinfo、
    反斜杠或别的写法的一律拒绝，不靠解析器猜它最终连到哪里。
    """
    if url.startswith('https://'):
        return
    host = re.split(r'[/?#]', url.removeprefix('http://'), maxsplit=1)[0]
    if _LOOPBACK.fullmatch(host):
        return
    raise _error(
        (
            f'{field} 是 https://，或主机是 127.0.0.1 / [::1] / localhost 的 http://：'
            'bearer 不走明文离开本机'
        ),
        f'收到 {url!r}，主机部分是 {host!r}',
        '改用对方的 https:// 地址；对方只有 http 时，在本机起 TLS 隧道或反向代理并钉住本机那一头',
    )


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
