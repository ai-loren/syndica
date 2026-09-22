"""编排者：即使 Runtime(orchestration=False) 也自己装上编排工具。

系统提示词在 freeze 时拼：Delegate 给谁，用的是目录里当时的 description。
覆盖内置 Worker 之后再 freeze，提示词里是新描述。
"""

from __future__ import annotations

from syndica.kernel.agent import BaseAgent
from syndica.kernel.catalog import AgentCatalog
from syndica.kernel.errors import ConfigError
from syndica.kernel.tools.orchestration import orchestration_tools

__all__ = ['Orchestrator']

_IMPL = 'syndica.kernel.agents.orchestrator:Orchestrator'


class Orchestrator(BaseAgent):
    """把活派给 Planner、Explorer、Worker。自己不改文件、不跑命令。"""

    @classmethod
    def compose_system_prompt(cls, catalog: AgentCatalog) -> str:
        """freeze 时调用。列出本角色 subagents 在目录里的当前描述。"""
        own = catalog.get(cls.__name__)
        lines = [f'Delegate 给 {name}：{catalog.get(name).description}' for name in own.subagents]
        return '你负责把活派出去，自己不改文件。\n' + '\n'.join(lines)

    def install_own_tools(self, runtime: object) -> None:
        if 'Delegate' in self.options.tools:
            return
        from syndica.kernel.runtime import Runtime

        if not isinstance(runtime, Runtime):
            raise ConfigError(
                expected='install_own_tools 的 runtime 是 Runtime',
                received=f'收到 {type(runtime).__name__}',
                next_action='由 Runtime.spawn 调用，不要自己传别的对象',
            )
        installed = orchestration_tools(runtime, self.agent_id)
        clashes = sorted(set(installed) & set(self.options.tools))
        if clashes:
            raise ConfigError(
                expected='Orchestrator 自己的工具与编排五件套都不重名',
                received=f'角色 {self.options.name!r} 的工具表里已经有 {clashes}',
                next_action='给自己的那个工具换个名字。本次 spawn 什么都没发生',
            )
        self.install_runtime_tools(installed)


def impl_path() -> str:
    """清单 impl 字段要写的 module:Class。不进 __all__：宿主用 Orchestrator 类本身。"""
    return _IMPL
