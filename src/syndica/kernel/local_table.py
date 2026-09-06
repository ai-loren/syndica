"""进程内的 Agent 实例表。

与 AgentRegistry 的分工：Registry 存可序列化状态、可远程共享；本表存运行时
对象（Agent 实例、asyncio 任务），只在持有它们的进程内有意义，因此不进
Protocol（spec §4.3）。多进程部署时前者共享，后者各进程只有自己那部分。

本表保证的是同一个 id 上的时序：**重复 `put`、给没有实例的 id `set_task`、替换未结束的
任务都被拒绝**，实例与任务描述不同 Agent 的一对不会从这三处产生；`_tasks` 的键恒为 `_agents`
的子集——teardown 只遍历 `agent_ids()`，落在它之外的任务永远轮不到被取消。未结束的任务不会
失去句柄，`pop` 除外（EXIT 模式下 Agent 在自己的任务里摘掉自己），要那个任务就先 `get_task`。
跨 slot 的别名不查：同一个任务对象装进两个 slot 不会被拒绝。
"""

from __future__ import annotations

import asyncio
from typing import Generic, TypeVar

from syndica.kernel.errors import ConfigError

__all__ = ['LocalAgentTable']

AgentT = TypeVar('AgentT')


class LocalAgentTable(Generic[AgentT]):
    def __init__(self) -> None:
        self._agents: dict[str, AgentT] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def put(self, agent_id: str, agent: AgentT, task: asyncio.Task[None] | None = None) -> None:
        """写入新条目。agent_id 已存在时抛异常，与 `AgentRegistry.register` 同一前提。"""
        if agent_id in self._agents:
            raise ConfigError(
                expected=f'agent_id {agent_id!r} 尚未在本表中',
                received=(
                    '该 agent_id 已有 Agent 实例：覆写会让 slot 里的实例与任务描述两个不同的'
                    ' Agent，丢掉旧任务则失去取消它的唯一句柄'
                ),
                next_action='要换实例先 get_task 取走任务、再 pop 掉旧条目；否则换一个 agent_id',
            )
        self._agents[agent_id] = agent
        if task is not None:
            self._tasks[agent_id] = task

    def get(self, agent_id: str) -> AgentT | None:
        return self._agents.get(agent_id)

    def set_task(self, agent_id: str, task: asyncio.Task[None]) -> None:
        """挂上驱动该 Agent 的任务。已有未结束的任务时抛异常；替换已结束的任务是重新激活。"""
        if agent_id not in self._agents:
            raise ConfigError(
                expected=f'agent_id {agent_id!r} 已经 put 过 Agent 实例',
                received=(
                    '本表没有该条目：agent_ids() 只列 Agent 实例，挂在它之外的任务 teardown'
                    ' 永远取不到，只能泄漏'
                ),
                next_action='先 put 该 Agent 实例，再 set_task',
            )
        existing = self._tasks.get(agent_id)
        if existing is not None and not existing.done():
            raise ConfigError(
                expected=f'agent_id {agent_id!r} 没有未结束的任务',
                received='该条目已有未结束的任务：替换它就失去了取消它的唯一句柄',
                next_action=(
                    '先 get_task 取走旧任务并等它结束（取消后仍要 await），再 set_task；'
                    '替换已结束的任务不受此限'
                ),
            )
        self._tasks[agent_id] = task

    def get_task(self, agent_id: str) -> asyncio.Task[None] | None:
        return self._tasks.get(agent_id)

    def pop(self, agent_id: str) -> None:
        """摘掉条目，任务一并丢弃——还需要它就先 get_task。未知 id 是 no-op。"""
        self._agents.pop(agent_id, None)
        self._tasks.pop(agent_id, None)

    def agent_ids(self) -> list[str]:
        """快照。teardown 边遍历边 pop，返回 dict 视图会在迭代中途抛 RuntimeError。"""
        return list(self._agents)
