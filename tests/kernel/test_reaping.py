"""派活树的回收（D-46）：每个父 Agent 至多留 `keep_finished_children` 个做完、结果已交回的子 Agent，
按进 zombie 的先后回收最早的；活着的后代、留着的隔离副本、正在被等的、宿主自己建的，一律不收。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.budget import Budget, BudgetPolicy
from syndica.kernel.context import ContextBlock, ToolCall
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import LLMConfig, ScriptedLLM, ScriptedTurn
from syndica.kernel.runtime import AgentFactory, Runtime

_TIMEOUT = 5.0


class _Gated:
    """放行之前一直等着，放行后交回一句话。"""

    def __init__(self, gate: asyncio.Event) -> None:
        self.config = LLMConfig(provider='scripted', model='m')
        self._gate = gate

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        await self._gate.wait()
        return ContextBlock(role='assistant', content='child done')


def _agent(llm: Any, name: str = 'Child') -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name=name, llm=llm, system_prompt='SP'), hooks=hooks, events=events
        )

    return build


async def _children(runtime: Runtime, parent: str, count: int, order: list[int]) -> list[str]:
    """`count` 个子 Agent，按 `order` 的先后做完，每个的结果都被派活等待取走。"""
    gates = [asyncio.Event() for _ in range(count)]
    children = [
        await runtime.spawn(_agent(_Gated(gate)), query='work', parent_id=parent) for gate in gates
    ]
    slot = runtime._slots[parent]  # pyright: ignore[reportPrivateUsage]
    for index in order:
        gates[index].set()
        await asyncio.wait_for(runtime.wait(children[index]), _TIMEOUT)
    for child in children:
        result = await runtime._waited_in_turn(slot, child)  # pyright: ignore[reportPrivateUsage]
        assert result.state.value == 'answered'
    return children


async def _alive(runtime: Runtime, ids: list[str]) -> list[bool]:
    return [await runtime.find(agent_id) is not None for agent_id in ids]


async def test_the_earliest_finished_children_beyond_the_limit_are_reaped_on_the_next_spawn() -> (
    None
):
    runtime = Runtime(keep_finished_children=2)
    try:
        parent = await runtime.spawn(_agent(ScriptedLLM([]), 'Parent'))
        children = await _children(runtime, parent, 3, order=[2, 0, 1])
        await runtime.spawn(_agent(ScriptedLLM([])), parent_id=parent)
        alive = await _alive(runtime, children)
        reason = (await runtime.send(children[2], 'more')).reason or ''
    finally:
        await runtime.shutdown()
    assert alive == [True, True, False]
    assert '被回收' in reason and 'Delegate' in reason


async def test_children_whose_result_nobody_collected_are_kept() -> None:
    runtime = Runtime(keep_finished_children=1)
    try:
        parent = await runtime.spawn(_agent(ScriptedLLM([]), 'Parent'))
        children = [
            await runtime.spawn(
                _agent(ScriptedLLM([ScriptedTurn(content='x')])), query='w', parent_id=parent
            )
            for _ in range(3)
        ]
        for child in children:
            await asyncio.wait_for(runtime.wait(child), _TIMEOUT)
        await runtime.spawn(_agent(ScriptedLLM([])), parent_id=parent)
        alive = await _alive(runtime, children)
    finally:
        await runtime.shutdown()
    assert alive == [True, True, True]


async def test_a_child_with_a_live_descendant_is_kept() -> None:
    runtime = Runtime(keep_finished_children=1)
    try:
        parent = await runtime.spawn(_agent(ScriptedLLM([]), 'Parent'))
        children = await _children(runtime, parent, 2, order=[0, 1])
        daemon = await runtime.spawn(_agent(ScriptedLLM([])), parent_id=children[0])
        await runtime.spawn(_agent(ScriptedLLM([])), parent_id=parent)
        alive = await _alive(runtime, [*children, daemon])
    finally:
        await runtime.shutdown()
    assert alive == [True, True, True]


async def test_a_child_something_is_still_waiting_on_is_kept() -> None:
    runtime = Runtime(keep_finished_children=1)
    try:
        parent = await runtime.spawn(_agent(ScriptedLLM([]), 'Parent'))
        children = await _children(runtime, parent, 2, order=[0, 1])
        runtime._await_count[children[0]] = 1  # pyright: ignore[reportPrivateUsage]
        await runtime.spawn(_agent(ScriptedLLM([])), parent_id=parent)
        alive = await _alive(runtime, children)
    finally:
        await runtime.shutdown()
    assert alive == [True, True]


async def test_a_child_with_a_kept_isolated_copy_is_kept(tmp_path: Path) -> None:
    from syndica.kernel.isolation import LocalIsolator
    from syndica.kernel.local_workspace import LocalWorkspace
    from syndica.kernel.workspace_router import WorkspaceRouter

    root = tmp_path / 'ws'
    root.mkdir()
    router = WorkspaceRouter(
        LocalWorkspace(str(root), shell='/bin/sh', env={}),
        isolator=LocalIsolator(shell='/bin/sh', env={}),
    )
    runtime = Runtime(workspace=router, keep_finished_children=1)
    try:
        parent = await runtime.spawn(_agent(ScriptedLLM([]), 'Parent'))
        isolated = await runtime.spawn(
            _agent(ScriptedLLM([ScriptedTurn(content='x')])),
            query='w',
            parent_id=parent,
            isolation='snapshot',
        )
        await asyncio.wait_for(runtime.wait(isolated), _TIMEOUT)
        slot = runtime._slots[parent]  # pyright: ignore[reportPrivateUsage]
        await runtime._waited_in_turn(slot, isolated)  # pyright: ignore[reportPrivateUsage]
        others = await _children(runtime, parent, 2, order=[0, 1])
        await runtime.spawn(_agent(ScriptedLLM([])), parent_id=parent)
        alive = await _alive(runtime, [isolated, *others])
    finally:
        await runtime.shutdown()
    assert alive == [True, False, True]


async def test_a_woken_child_is_not_counted_as_finished_until_collected_again() -> None:
    runtime = Runtime(keep_finished_children=1)
    try:
        parent = await runtime.spawn(_agent(ScriptedLLM([]), 'Parent'))
        children = await _children(runtime, parent, 2, order=[0, 1])
        assert await runtime.ensure_active(children[0])
        await runtime.spawn(_agent(ScriptedLLM([])), parent_id=parent)
        alive = await _alive(runtime, children)
    finally:
        await runtime.shutdown()
    assert alive == [True, True]


async def test_a_reaped_childs_spend_stays_with_its_parent() -> None:
    runtime = Runtime(keep_finished_children=1, budget=BudgetPolicy(root=Budget(turns=50)))
    try:
        parent = await runtime.spawn(_agent(ScriptedLLM([]), 'Parent'))
        children = await _children(runtime, parent, 2, order=[0, 1])
        before = runtime._budget_of(parent)  # pyright: ignore[reportPrivateUsage]
        await runtime.spawn(_agent(ScriptedLLM([])), parent_id=parent)
        after = runtime._budget_of(parent)  # pyright: ignore[reportPrivateUsage]
        alive = await _alive(runtime, children)
    finally:
        await runtime.shutdown()
    assert alive == [False, True]
    assert before is not None and after is not None
    assert after.used.turns == before.used.turns == 2


@pytest.mark.parametrize('value', [0, True, '3'])
def test_the_limit_must_be_a_positive_integer(value: object) -> None:
    with pytest.raises(ConfigError) as caught:
        Runtime(keep_finished_children=cast(Any, value))
    assert 'keep_finished_children' in caught.value.expected


async def test_a_parent_that_asks_about_a_reaped_child_is_told_what_happened() -> None:
    def delegate(call_id: str) -> ScriptedTurn:
        arguments = {'agent': 'Child', 'query': 'sub task'}
        return ScriptedTurn(
            tool_calls=[ToolCall(id=call_id, name='Delegate', arguments=json.dumps(arguments))]
        )

    first_child: list[str] = []

    class _Parent(ScriptedLLM):
        async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
            if len(self.calls) == 3:
                answer = next(b for b in messages if b.tool_call_id == 'd1')
                first_child.append(cast(dict[str, Any], answer.extra['tool_meta'])['agent_id'])
                self._script[3] = ScriptedTurn(
                    tool_calls=[
                        ToolCall(
                            id='q',
                            name='QueryAgent',
                            arguments=json.dumps({'agent_id': first_child[0]}),
                        )
                    ]
                )
            return await super().create(messages, **kwargs)

    parent = _Parent(
        [
            delegate('d1'),
            delegate('d2'),
            delegate('d3'),
            ScriptedTurn(content='x'),
            ScriptedTurn(content='done'),
        ]
    )
    runtime = Runtime(
        orchestration=True,
        keep_finished_children=1,
        agents={'Child': _agent(ScriptedLLM([ScriptedTurn(content=f'r{i}') for i in range(3)]))},
    )
    try:
        root = await runtime.spawn(_agent(parent, 'Parent'), query='go')
        ctx = await asyncio.wait_for(runtime.wait(root), _TIMEOUT)
    finally:
        await runtime.shutdown()
    answer = str(next(b for b in ctx.current_traj if b.tool_call_id == 'q').content)
    assert '被回收' in answer and '最近结束的 1 个' in answer
