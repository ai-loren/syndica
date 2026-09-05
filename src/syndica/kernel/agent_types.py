"""Agent 的可序列化状态。

状态放 Registry 而不是 Agent 自身：Agent 阻塞或崩溃时无法自报状态
（spec §4.3），同操作系统把进程状态放在内核而非进程内。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = ['AgentInfo', 'AgentStatus', 'ExitMode']


class AgentStatus(Enum):
    """ZOMBIE 不是终态：事件循环与队列已撤，但条目留在 Registry 因此仍可寻址，
    一条消息即可复活并续用原上下文。消费方不得把它当残留清理掉。
    """

    IDLE = 'idle'
    RUNNING = 'running'
    PAUSED = 'paused'
    WAITING = 'waiting'
    ABORTING = 'aborting'
    ZOMBIE = 'zombie'


class ExitMode(Enum):
    """一轮工作结束后的归宿。"""

    IDLE = 'idle'
    ZOMBIE = 'zombie'
    EXIT = 'exit'


@dataclass
class AgentInfo:
    agent_id: str
    name: str
    agent_type: str
    status: AgentStatus = AgentStatus.IDLE
    parent_id: str | None = None
    # 工厂写成带参数的泛型别名而非裸 list / dict：strict 模式下裸工厂推不出元素类型。
    children: list[str] = field(default_factory=list[str])
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    # 租约，用于回收孤儿状态：owner 为空是「无人认领」，owner 非空而心跳过期是「持有者已崩溃」。
    # 两者混同会让状态为 RUNNING 的孤儿一直「看起来正在运行」，从而挡住重新激活。
    owner: str | None = None
    heartbeat_at: float | None = None
