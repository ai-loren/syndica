"""AgentRegistry 的内存实现：单机默认，零依赖。"""

from __future__ import annotations

import copy
from typing import Any

from syndica.kernel.agent_types import AgentInfo, AgentStatus
from syndica.kernel.errors import ContractViolation
from syndica.kernel.protocols.registry import (
    UPDATE_PROTECTED_FIELD_OWNERS,
    UPDATE_PROTECTED_FIELDS,
    UPDATE_WRITABLE_FIELDS,
)

__all__ = ['InMemoryAgentRegistry']


class InMemoryAgentRegistry:
    def __init__(self) -> None:
        self._infos: dict[str, AgentInfo] = {}

    async def register(self, info: AgentInfo) -> None:
        if info.agent_id in self._infos:
            raise ContractViolation(
                expected=f'agent_id {info.agent_id!r} 尚未注册',
                received='该 agent_id 已存在于注册表',
                next_action='换一个 agent_id，或先 unregister 旧条目',
            )
        parent = self._infos.get(info.parent_id) if info.parent_id is not None else None
        if info.parent_id is not None and parent is None:
            raise ContractViolation(
                expected=f'parent_id {info.parent_id!r} 已在注册表中',
                received='注册表中不存在该父条目，父→子索引无处可写',
                next_action='先 register 父 Agent 再注册子 Agent，或把 parent_id 置为 None',
            )
        self._infos[info.agent_id] = copy.deepcopy(info)
        if parent is not None and info.agent_id not in parent.children:
            parent.children.append(info.agent_id)

    async def get(self, agent_id: str) -> AgentInfo | None:
        info = self._infos.get(agent_id)
        return copy.deepcopy(info) if info is not None else None

    async def compare_and_set_status(
        self, agent_id: str, *, expected: AgentStatus, new: AgentStatus
    ) -> bool:
        info = self._infos.get(agent_id)
        if info is None or info.status is not expected:
            return False
        info.status = new
        return True

    async def update(self, agent_id: str, **fields: Any) -> None:
        info = self._infos.get(agent_id)
        if info is None:
            raise ContractViolation(
                expected=f'agent_id {agent_id!r} 已注册',
                received='注册表中不存在该条目',
                next_action='先 register，或检查 agent_id 是否拼错',
            )
        # 先全量校验再写：任一字段被拒时不得留下部分写入。
        for key in fields:
            if key in UPDATE_PROTECTED_FIELDS:
                owner = UPDATE_PROTECTED_FIELD_OWNERS.get(key, '注册表为该字段提供的专用操作')
                raise ContractViolation(
                    expected=f'update 只写单一写者拥有的字段: {sorted(UPDATE_WRITABLE_FIELDS)}',
                    received=f'收到受保护字段 {key!r}，它另有写入者，无条件写会覆盖掉对方的写入',
                    next_action=f'改用 {owner}',
                )
            if key not in UPDATE_WRITABLE_FIELDS:
                raise ContractViolation(
                    expected=f'update 只写单一写者拥有的字段: {sorted(UPDATE_WRITABLE_FIELDS)}',
                    received=f'{key!r} 既不可写也不在受保护字段中，AgentInfo 上没有这个字段',
                    next_action=(
                        '改用可写字段名；若这是 AgentInfo 新增的字段，'
                        '先把它归入 UPDATE_WRITABLE_FIELDS 或 UPDATE_PROTECTED_FIELDS'
                    ),
                )
        owned_fields = copy.deepcopy(fields)
        for key, value in owned_fields.items():
            setattr(info, key, value)

    async def children_of(self, agent_id: str) -> list[str]:
        info = self._infos.get(agent_id)
        return list(info.children) if info is not None else []

    async def list(
        self, *, status: AgentStatus | None = None, parent_id: str | None = None
    ) -> list[AgentInfo]:
        return [
            copy.deepcopy(info)
            for info in self._infos.values()
            if (status is None or info.status is status)
            and (parent_id is None or info.parent_id == parent_id)
        ]

    async def unregister(self, agent_id: str) -> None:
        info = self._infos.pop(agent_id, None)
        if info is not None and info.parent_id in self._infos:
            parent = self._infos[info.parent_id]
            if agent_id in parent.children:
                parent.children.remove(agent_id)
