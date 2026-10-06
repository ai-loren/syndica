"""后代的单一定义（B-22）：沿 `parent_id` 往上走，O(深度)，带环保护；KillAgent 的授权用它。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.agent_types import AgentInfo
from syndica.kernel.context import ToolCall
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.stores.memory_registry import InMemoryAgentRegistry

_TIMEOUT = 5.0


class _Counting(InMemoryAgentRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.children_calls = 0

    async def children_of(self, agent_id: str) -> list[str]:
        self.children_calls += 1
        return await super().children_of(agent_id)


class _Cyclic(InMemoryAgentRegistry):
    """一个被别的进程写坏的远端注册表：b 的父是 c、c 的父是 b。"""

    async def get(self, agent_id: str) -> AgentInfo | None:
        info = await super().get(agent_id)
        if info is not None and agent_id in ('b', 'c'):
            return replace(info, parent_id='c' if agent_id == 'b' else 'b')
        return info


def _idle(hooks: HooksCenter, events: EventBus) -> BaseAgent:
    return BaseAgent(
        AgentOptions(name='Idle', llm=ScriptedLLM([]), system_prompt='SP'),
        hooks=hooks,
        events=events,
    )


async def _tree(runtime: Runtime) -> dict[str, str]:
    root = await runtime.spawn(_idle)
    child = await runtime.spawn(_idle, parent_id=root)
    grandchild = await runtime.spawn(_idle, parent_id=child)
    sibling = await runtime.spawn(_idle, parent_id=root)
    return {'root': root, 'child': child, 'grandchild': grandchild, 'sibling': sibling}


async def test_a_descendant_is_found_by_walking_up_and_nothing_else_is() -> None:
    runtime = Runtime()
    try:
        ids = await _tree(runtime)
        check = runtime._is_descendant  # pyright: ignore[reportPrivateUsage]
        assert await check(ids['grandchild'], ids['root'])
        assert await check(ids['grandchild'], ids['child'])
        assert not await check(ids['sibling'], ids['child'])
        assert not await check(ids['root'], ids['child'])
        assert not await check(ids['child'], ids['child'])
        assert not await check('nobody', ids['root'])
    finally:
        await runtime.shutdown()


async def test_a_cycle_in_the_registry_ends_the_walk() -> None:
    registry = _Cyclic()
    await registry.register(AgentInfo(agent_id='b', name='b', agent_type='Idle'))
    await registry.register(AgentInfo(agent_id='c', name='c', agent_type='Idle'))
    runtime = Runtime(registry=registry)
    try:
        found = await asyncio.wait_for(
            runtime._is_descendant('b', 'a'),  # pyright: ignore[reportPrivateUsage]
            _TIMEOUT,
        )
    finally:
        await runtime.shutdown()
    assert found is False


async def test_kill_agent_checks_its_target_without_listing_the_subtree() -> None:
    registry = _Counting()
    runtime = Runtime(registry=registry, orchestration=True)
    try:
        ids = await _tree(runtime)
        for _ in range(20):
            await runtime.spawn(_idle, parent_id=ids['child'])
        calls: list[Any] = [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='k', name='KillAgent', arguments=f'{{"agent_id": "{ids["grandchild"]}"}}'
                    )
                ]
            ),
            ScriptedTurn(content='done'),
        ]
        llm = ScriptedLLM(calls)

        def killer(hooks: HooksCenter, events: EventBus) -> BaseAgent:
            return BaseAgent(
                AgentOptions(name='Killer', llm=llm, system_prompt='SP'), hooks=hooks, events=events
            )

        factory: AgentFactory = killer
        before = registry.children_calls
        caller = await runtime.spawn(factory, query='stop it')
        await asyncio.wait_for(runtime.wait(caller), _TIMEOUT)
        answer = str(llm.calls[1][-1].content)
    finally:
        await runtime.shutdown()
    assert '不在你这棵子树里' in answer
    assert registry.children_calls == before
