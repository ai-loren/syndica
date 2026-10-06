"""唤醒重切与切分同一套规则（spec §8.4，B-21）：父是 Agent 时先给它自己留一轮。父只剩最后一轮时
叫醒子 Agent 被如实拒绝，父照常在最后一轮交出回答；被拒的子 Agent 原样留着。"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.budget import Budget, BudgetPolicy
from syndica.kernel.context import ContextBlock, ToolCall
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import LLMConfig
from syndica.kernel.runtime import Runtime
from syndica.kernel.tool import Tool, ToolResult

_TIMEOUT = 5.0


def _call(name: str, call_id: str, **arguments: object) -> ContextBlock:
    return ContextBlock(
        role='assistant',
        tool_calls=[ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))],
    )


class _Parent:
    config = LLMConfig(provider='scripted', model='m')

    def __init__(self, wake: bool, nap: bool = False) -> None:
        self.nap = nap
        self.calls = 0
        self.wake = wake
        self.child: str | None = None
        self.last_messages: list[ContextBlock] = []
        self.child_ctx: Any = None

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        await asyncio.sleep(0)
        self.calls += 1
        self.last_messages = list(messages)
        for block in messages:
            found = re.search(r'agent (\S+?) 交回', str(block.content or ''))
            if found and self.child is None:
                self.child = found.group(1)
        if self.calls == 1:
            return _call('Delegate', 'd', agent='Worker', query='do a small job', turns=3)
        if self.calls in (2, 3):
            return _call('GetBudget', f'g{self.calls}')
        if self.calls == 4 and self.wake:
            assert self.child is not None
            return ContextBlock(
                role='assistant',
                tool_calls=[
                    ToolCall(
                        id='s',
                        name='SendMessage',
                        arguments=json.dumps(
                            {'agent_id': self.child, 'content': 'one more detail'}
                        ),
                    ),
                    ToolCall(id='n', name='Nap', arguments='{}'),
                ]
                if self.nap
                else [
                    ToolCall(
                        id='s',
                        name='SendMessage',
                        arguments=json.dumps(
                            {'agent_id': self.child, 'content': 'one more detail'}
                        ),
                    ),
                ],
            )
        if self.calls == 4:
            return _call('GetBudget', 'g4')
        return ContextBlock(role='assistant', content='PARENT FINAL ANSWER')


class _Child:
    config = LLMConfig(provider='scripted', model='m')

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        await asyncio.sleep(0)
        return ContextBlock(role='assistant', content='child done')


class _Nap(Tool):
    __tool_name__ = 'Nap'
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        """Wait a moment."""
        await asyncio.sleep(0.05)
        return ToolResult(output='rested')


def _factory(name: str, llm: Any, tools: dict[str, Tool] | None = None) -> Any:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name=name, llm=llm, tools=dict(tools or {}), system_prompt='SP', max_turns=None
            ),
            hooks=hooks,
            events=events,
        )

    return build


async def _run(wake: bool, nap: bool = False) -> tuple[Any, _Parent]:
    parent = _Parent(wake, nap)
    runtime = Runtime(
        agents={'Worker': _factory('Worker', _Child())},
        budget=BudgetPolicy(root=Budget(turns=6)),
        orchestration=True,
    )
    try:
        agent_id = await runtime.spawn(_factory('Root', parent, {'Nap': _Nap()}), query='go')
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        await asyncio.sleep(0.1)
        child = runtime._table.get(parent.child) if parent.child else None
        parent.child_ctx = None if child is None else child.context
    finally:
        await runtime.shutdown()
    return ctx, parent


async def test_without_a_wake_the_parent_answers_on_its_last_turn() -> None:
    ctx, _ = await _run(wake=False)
    assert ctx.current_traj[-1].content == 'PARENT FINAL ANSWER'


async def test_waking_a_child_with_the_parents_last_turn_is_refused_and_the_parent_answers() -> (
    None
):
    ctx, _ = await _run(wake=True)
    sent = str(next(b for b in ctx.current_traj if b.tool_call_id == 's').content)
    assert '预算' in sent
    assert ctx.current_traj[-1].content == 'PARENT FINAL ANSWER'


async def test_a_child_that_cannot_be_woken_is_left_as_it_was() -> None:
    _, parent = await _run(wake=True)
    child = parent.child_ctx
    assert child.current_traj[-1].role == 'assistant'
    assert not any('one more detail' in str(b.content) for b in child.current_traj)


async def test_a_wake_racing_the_parent_never_takes_the_parents_last_turn() -> None:
    ctx, _ = await _run(wake=True, nap=True)
    assert ctx.current_traj[-1].content == 'PARENT FINAL ANSWER'
