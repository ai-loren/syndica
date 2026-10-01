"""可复现的 id（M3 D10）：id 源、稳定键与「当前工具调用」。

内核向 id 源要两样 id：Agent 的与消息的。默认的源交回 uuid4 的十六进制，与不注入时一样；回放时宿主
换上一个按稳定键交回记录的 id 的源。

稳定键说的是「这个东西是怎么来的」，不是「它是第几个」：几个 Agent 并发时，创建的先后取决于调度。
一个键由它的出处加上出处里的第几个组成。出处依次是：当前工具调用；否则是当前的属主——一个 Agent 自己
的事件循环（它的 hook 派的活、发的消息）、一次收尾、一次关机，它们各自串行，序号因此与别的 Agent
的快慢无关；都没有时才是宿主。经拓扑槽创建的 Agent 在出处前面再加上槽名与键；对一个请求的答复用那个
请求的键。键只由字符串与整数组成，可以原样写进 JSON。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    'IdSource',
    'RandomIds',
    'StableKey',
    'ToolCallKey',
    'assigned_agent_id',
    'assigning_agent_id',
    'becoming_owner',
    'current_tool_call',
    'entering_tool_call',
    'leaving_tool_call',
    'next_key',
    'peek_key',
    'releasing_agent_id',
]

StableKey = tuple[str | int, ...]


@runtime_checkable
class IdSource(Protocol):
    """按稳定键交出 id。交回的必须是非空字符串，同一个 Runtime 里各不相同。"""

    def agent_id(self, key: StableKey) -> str: ...

    def message_id(self, key: StableKey) -> str: ...


class RandomIds:
    """默认的源：uuid4 的十六进制，全量不截断（注册表拒绝重复的 id，撞上的那个就注册不进去）。
    键不参与。"""

    def agent_id(self, key: StableKey) -> str:
        del key
        return uuid.uuid4().hex

    def message_id(self, key: StableKey) -> str:
        del key
        return uuid.uuid4().hex


@dataclass(frozen=True)
class ToolCallKey:
    """一次工具调用：哪个 Agent 的第几个工作周期、第几轮、批内第几个，以及模型给的调用 id。模型给的
    id 只作记录、不进键：它可以重复、可以是空串。"""

    agent_id: str
    cycle: int
    turn: int
    index: int
    call_id: str

    @property
    def key(self) -> StableKey:
        return ('tool_call', self.agent_id, self.cycle, self.turn, self.index)


@dataclass
class _Scope:
    call: ToolCallKey
    issued: dict[str, int] = field(default_factory=dict[str, int])


@dataclass
class _Owner:
    prefix: StableKey
    issued: dict[str, int]


_CURRENT: ContextVar[_Scope | None] = ContextVar('syndica_current_tool_call', default=None)
_OWNER: ContextVar[_Owner | None] = ContextVar('syndica_key_owner', default=None)
_ASSIGNED: ContextVar[str | None] = ContextVar('syndica_assigned_agent_id', default=None)


def current_tool_call() -> ToolCallKey | None:
    """此刻在跑的那次工具调用；不在任何工具调用里时是 None。同一段里并发的调用各在自己的任务里，
    互相看不见。"""
    scope = _CURRENT.get()
    return None if scope is None else scope.call


def entering_tool_call(call: ToolCallKey) -> Token[_Scope | None]:
    return _CURRENT.set(_Scope(call))


def leaving_tool_call(token: Token[_Scope | None]) -> None:
    _CURRENT.reset(token)


def becoming_owner(prefix: StableKey, issued: dict[str, int]) -> None:
    """在当前上下文里换成一个新的属主，并清掉「当前工具调用」：内核自己起的任务（驱动、收尾、关机）
    经 `create_task` 建在调用方的上下文里，不清就继承了调用方那次工具调用。`issued` 是属主自己的
    计数，由调用方持有：同一个 Agent 的事件循环被唤醒好几次，序号要接着数。"""
    _CURRENT.set(None)
    _OWNER.set(_Owner(prefix, issued))


def _counters() -> tuple[StableKey, dict[str, int]] | None:
    scope = _CURRENT.get()
    if scope is not None:
        return scope.call.key, scope.issued
    owner = _OWNER.get()
    return None if owner is None else (owner.prefix, owner.issued)


def peek_key(what: str) -> StableKey | None:
    """当前出处里下一个 `what`（`spawn`、`message`、`request`）的键，不占用它；既不在工具调用里、
    也没有属主（宿主自己的代码）时是 None。"""
    counters = _counters()
    if counters is None:
        return None
    prefix, issued = counters
    return (*prefix, what, issued.get(what, 0))


def next_key(what: str) -> StableKey | None:
    """同 `peek_key`，并占用它：同一出处里的下一个 `what` 拿到下一个序号。"""
    counters = _counters()
    if counters is None:
        return None
    prefix, issued = counters
    number = issued.get(what, 0)
    issued[what] = number + 1
    return (*prefix, what, number)


def assigning_agent_id(agent_id: str) -> Token[str | None]:
    """`Runtime.spawn` 在调用工厂之前设下的 id：工厂里就已经有东西绑定 `agent.agent_id`（派活工具、
    选择器表），工厂返回之后再改就晚了。"""
    return _ASSIGNED.set(agent_id)


def releasing_agent_id(token: Token[str | None]) -> None:
    _ASSIGNED.reset(token)


def assigned_agent_id() -> str | None:
    """取走 spawn 设下的 id：只有工厂里构造的第一个 Agent 拿到它，同一个工厂再构造的拿各自的。"""
    agent_id = _ASSIGNED.get()
    if agent_id is not None:
        _ASSIGNED.set(None)
    return agent_id
