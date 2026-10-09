"""会话开始前连上项目里声明、且有角色用到的 MCP 服务。

backend 由组合根注入。没有角色用到的服务不拉起、不核对凭证：拉起它只会多一个谁也调不到的外部
进程。一个都不用连时不调用注入的连接函数。环境交给子进程的是完整的 Agent 环境，
再叠上用户级文件点名的变量，不是宿主环境的增量。用户级凭证钉住的目的地与哪个服务对不上，就
一个服务都不连：先核对完全部，再拉起第一个。配置里没有每服务超时，调用先限 30 秒，
再和这个 Agent 的墙钟剩余取较小者。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from syndica.cli.config import CliConfig
from syndica.cli.environment import refuse_env_name
from syndica.cli.interop_config import (
    InteropCredential,
    McpServer,
    credential_for,
    interop_credentials_path,
    load_interop_credentials,
)
from syndica.cli.terminal import one_line
from syndica.kernel import ConfigError, DangerJudge, Redactor, Runtime, RuntimeModule, Tool

_CALL_TIMEOUT_S = 30.0


class _Closeable(Protocol):
    async def aclose(self) -> None: ...


class _Link(_Closeable, Protocol):
    name: str
    tools: Mapping[str, Tool]

    def __aenter__(self) -> Awaitable[object]: ...


@dataclass(frozen=True)
class McpPorts:
    """组合根交进来的 backend。surface 不 import 它们。"""

    connect_stdio: Callable[..., object]
    connect_http: Callable[..., object]
    assign_roles: Callable[..., object]
    make_module: Callable[..., RuntimeModule]
    wrap_judge: Callable[[DangerJudge], DangerJudge]


@dataclass
class OpenedMcp:
    """已经握手的连接，以及装进 Runtime 的模块和判定器。"""

    module: RuntimeModule
    judge: DangerJudge
    clients: tuple[_Closeable, ...]

    async def aclose(self) -> None:
        await _close_all(self.clients)


class McpOpener(Protocol):
    def __call__(
        self,
        config: CliConfig,
        agent_env: Mapping[str, str],
        *,
        cwd: str,
        redactor: Redactor,
        environ: Mapping[str, str],
        judge: DangerJudge,
        workspace: str,
    ) -> Awaitable[OpenedMcp | None]: ...


async def open_mcp_clients(
    config: CliConfig,
    agent_env: Mapping[str, str],
    *,
    cwd: str,
    redactor: Redactor,
    environ: Mapping[str, str],
    judge: DangerJudge,
    workspace: str,
    ports: McpPorts,
) -> OpenedMcp | None:
    """连上 `interop.mcp.servers` 里 `roles` 非空的服务。一个都没有时返回 None，不碰 `ports`。"""
    servers = tuple(server for server in config.interop.mcp_servers if server.roles)
    if not servers:
        return None
    credentials = _credentials(environ, workspace)
    bound = [(server, credential_for(credentials, server)) for server in servers]
    live: list[_Link] = []
    try:
        for server, credential in bound:
            client = _as_link(
                _connect(
                    server,
                    credential,
                    agent_env,
                    cwd=cwd,
                    redactor=redactor,
                    environ=environ,
                    ports=ports,
                )
            )
            try:
                await client.__aenter__()
            except BaseException:
                await client.aclose()
                raise
            live.append(client)
    except BaseException as exc:
        await _close_opened(live, exc)
        raise
    roles = {server.name: server.roles for server in servers}
    assigned = ports.assign_roles(live, roles)
    if not isinstance(assigned, Mapping):
        raise ConfigError(
            expected='assign_roles 返回角色到工具表的映射',
            received=f'收到 {assigned!r}，类型是 {type(assigned).__name__}',
            next_action='组合根传入 backends.mcp 的 assign_roles',
        )
    protocol = ','.join(
        f'{client.name}={version}'
        for client in live
        if isinstance(version := getattr(client, 'protocol_version', None), str) and version
    )
    return OpenedMcp(
        module=ports.make_module(live, assigned, wall_remaining=_wall_remaining, protocol=protocol),
        judge=ports.wrap_judge(judge),
        clients=tuple(live),
    )


def doctor_lines(opened: OpenedMcp) -> tuple[str, ...]:
    """doctor 里每个已连接服务一行版本、一行装上的工具（Agent 看到的名字，按服务列出的顺序），
    每个没装上的工具一行。"""
    lines: list[str] = []
    for client in opened.clients:
        name = getattr(client, 'name', '')
        version = getattr(client, 'protocol_version', None)
        lines.append(f'mcp: ok {one_line(str(name))} {one_line(str(version))}')
        tools = getattr(client, 'tools', None)
        if isinstance(tools, Mapping):
            listed = ', '.join(one_line(str(key)) for key in cast(Mapping[object, object], tools))
            lines.append(f'mcp: tools {one_line(str(name))} {listed or "（没有工具）"}')
        excluded = getattr(client, 'excluded', ())
        if isinstance(excluded, tuple | list):
            for item in cast(Sequence[object], excluded):
                server = getattr(item, 'server', '')
                tool = getattr(item, 'name', '')
                reason = getattr(item, 'reason', '')
                lines.append(
                    f'mcp: excluded {one_line(str(server))} {one_line(str(tool))} '
                    f'{one_line(str(reason))}'
                )
    return tuple(lines)


def _credentials(environ: Mapping[str, str], workspace: str) -> Mapping[str, InteropCredential]:
    path = interop_credentials_path(environ)
    if path is None:
        return {}
    return load_interop_credentials(path, workspace=Path(workspace))


def _wall_remaining(runtime: Runtime, agent_id: str) -> float | None:
    view = runtime._budget_of(agent_id)  # pyright: ignore[reportPrivateUsage]
    if view is None:
        return None
    return view.remaining.wall_clock_s


def _connect(
    server: McpServer,
    credential: InteropCredential | None,
    agent_env: Mapping[str, str],
    *,
    cwd: str,
    redactor: Redactor,
    environ: Mapping[str, str],
    ports: McpPorts,
) -> object:
    extra = () if credential is None else credential.env
    env = _child_env(agent_env, (*server.pass_env, *extra), environ)
    for name, value in server.env_values:
        refuse_env_name(name, f'mcp:{server.name} env')
        env[name] = value
    if server.command is not None:
        return ports.connect_stdio(
            server.name,
            server.command,
            env=env,
            cwd=cwd,
            redactor=redactor,
            timeout=_CALL_TIMEOUT_S,
            high=server.high_risk,
            low=server.low_risk,
        )
    return ports.connect_http(
        server.name,
        server.url,
        redactor=redactor,
        timeout=_CALL_TIMEOUT_S,
        bearer=_bearer(server.name, credential, environ),
        high=server.high_risk,
        low=server.low_risk,
    )


def _as_link(client: object) -> _Link:
    enter = getattr(client, '__aenter__', None)
    close = getattr(client, 'aclose', None)
    name = getattr(client, 'name', None)
    if not callable(enter) or not callable(close) or not isinstance(name, str):
        raise ConfigError(
            expected='MCP 连接有 name、__aenter__ 和 aclose',
            received=f'收到 {client!r}，类型是 {type(client).__name__}',
            next_action='组合根传入 backends.mcp 的 connect_stdio / connect_http',
        )
    return cast(_Link, client)


def _child_env(
    agent_env: Mapping[str, str], names: Sequence[str], host: Mapping[str, str]
) -> dict[str, str]:
    """Agent 环境的整份副本，加上用户级点名且宿主环境里确实有的变量。"""
    env = dict(agent_env)
    for name in names:
        value = host.get(name)
        if value is not None:
            env[name] = value
    return env


def _bearer(name: str, credential: InteropCredential | None, host: Mapping[str, str]) -> str | None:
    if credential is None or credential.bearer_env is None:
        return None
    variable = credential.bearer_env
    value = host.get(variable)
    if value is None or not value:
        raise ConfigError(
            expected=f'环境变量 {variable} 有值，用作 MCP 服务 {name!r} 的 bearer',
            received=f'{variable} 没有设置',
            next_action=(
                f'在启动 Syndica 的环境里设置 {variable}，或从用户级 interop.yaml 删掉 '
                f'mcp:{name} 这一项（它的 bearer_env 点名了 {variable}）'
            ),
        )
    return value


async def _close_opened(clients: Sequence[_Closeable], exc: BaseException) -> None:
    task = asyncio.current_task()
    cancelled = isinstance(exc, asyncio.CancelledError)
    if cancelled and task is not None:
        while task.cancelling():
            task.uncancel()
    try:
        await _close_all(clients)
    finally:
        if cancelled:
            raise asyncio.CancelledError from exc


async def _close_all(clients: Sequence[_Closeable]) -> None:
    first: BaseException | None = None
    for client in clients:
        try:
            await client.aclose()
        except Exception as exc:
            if first is None:
                first = exc
    if first is not None:
        raise first
