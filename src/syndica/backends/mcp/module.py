"""把已经连上的 MCP 工具按角色装进 Runtime。

连接在 Runtime 建起来之前完成。这里只负责装工具、绑墙钟，并在 Runtime 关闭时关掉连接。
服务中途退出不会在这里重连。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from syndica.backends.mcp.client import McpClient
from syndica.kernel import ConfigError, Runtime, RuntimeModule, Tool
from syndica.kernel.agent import BaseAgent

__all__ = ['assign_roles', 'excluded_reports', 'mcp_module']

WallRemaining = Callable[[Runtime, str], float | None]


def assign_roles(
    clients: Sequence[McpClient], roles_of: Mapping[str, Sequence[str]]
) -> dict[str, dict[str, Tool]]:
    """每个服务的工具装到它声明的角色上。同一个角色上规整后的名字撞了就拒绝。"""
    assigned: dict[str, dict[str, Tool]] = {}
    origins: dict[tuple[str, str], str] = {}
    for client in clients:
        for role in roles_of.get(client.name, ()):
            bucket = assigned.setdefault(role, {})
            for name, tool in client.tools.items():
                previous = origins.get((role, name))
                if previous is not None:
                    raise ConfigError(
                        expected=f'角色 {role!r} 上的 MCP 工具名互不相同',
                        received=f'服务 {previous!r} 与 {client.name!r} 都提供 {name!r}',
                        next_action='给其中一个服务换名；工具名带着服务名前缀，撞名说明前缀规整后重叠',
                    )
                origins[(role, name)] = client.name
                bucket[name] = tool
    return assigned


def excluded_reports(clients: Sequence[McpClient]) -> tuple[str, ...]:
    """doctor 列出没装上的工具。空元组表示没有。"""
    return tuple(
        f'{item.server} {item.name}: {item.reason}'
        for client in clients
        for item in client.excluded
    )


def mcp_module(
    clients: Sequence[McpClient],
    tools_by_role: Mapping[str, Mapping[str, Tool]],
    *,
    wall_remaining: WallRemaining | None = None,
    protocol: str = '',
) -> RuntimeModule:
    """Runtime Module `mcp`。没列在角色表里的 Agent 得不到这些工具。"""
    by_role = {role: dict(tools) for role, tools in tools_by_role.items()}
    held = tuple(clients)

    def factory(runtime: Runtime, agent: BaseAgent, flags: Mapping[str, object]) -> dict[str, Tool]:
        del flags
        if wall_remaining is not None:
            agent_id = agent.agent_id

            def probe(runtime: Runtime = runtime, agent_id: str = agent_id) -> float | None:
                assert wall_remaining is not None
                return wall_remaining(runtime, agent_id)

            for client in held:
                client.bind_wall(agent_id, probe)
        return dict(by_role.get(agent.options.name, {}))

    async def shutdown(runtime: Runtime) -> None:
        del runtime
        first: BaseException | None = None
        for client in held:
            try:
                await client.aclose()
            except Exception as exc:
                if first is None:
                    first = exc
        if first is not None:
            raise first

    module = RuntimeModule(
        'mcp',
        tool_factories=(factory,),
        on_shutdown=shutdown,
        # 协商版本写进 module.version：trace 的属性闭集里有这一项，没有单独的协议字段。
        # 没连上时仍是 1。连上之后是 `<服务>=<版本>`，多个服务用逗号接。
        version=protocol if protocol else '1',
        description='把已连接的 MCP 服务的工具按角色装进 Agent',
        capabilities=('network', 'subprocess'),
        replay_safe=False,
        security_notes=(
            '外部工具的结果一律不可信。轨迹里一旦有过不可信内容，HIGH 的外部工具按 '
            'untrusted_high_risk 拒绝，与 Bash 同一套。服务中途退出不会自动重连。'
        ),
    )
    return module
