"""AgentRegistry：Agent 可序列化状态的存储契约。

它只回答「这个 Agent 处于什么状态」；「这个 Agent 的对象在哪」由进程内的
LocalAgentTable 回答。两者分离是存储层可替换的地基（spec §4.3）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from syndica.kernel.agent_types import AgentInfo, AgentStatus

__all__ = ['AgentRegistry']


@runtime_checkable
class AgentRegistry(Protocol):
    async def register(self, info: AgentInfo) -> None: ...

    async def get(self, agent_id: str) -> AgentInfo | None:
        """返回快照。调用方修改返回值不得影响存储。"""
        ...

    async def compare_and_set_status(
        self, agent_id: str, *, expected: AgentStatus, new: AgentStatus
    ) -> bool:
        """状态转换。expected 不匹配或 agent 不存在时返回 False，不抛异常。"""
        ...

    async def update(self, agent_id: str, **fields: Any) -> None:
        """无条件写。仅限单一写者拥有的字段；多方可写的集合须用专用原子操作。"""
        ...

    async def children_of(self, agent_id: str) -> list[str]: ...

    async def list(
        self, *, status: AgentStatus | None = None, parent_id: str | None = None
    ) -> list[AgentInfo]: ...

    async def unregister(self, agent_id: str) -> None: ...
