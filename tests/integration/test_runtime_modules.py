"""三个内置 Runtime Module 穿过真实 Agent 循环的端到端场景。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

from syndica.kernel import (
    AgentFactory,
    AgentOptions,
    BaseAgent,
    EventBus,
    HooksCenter,
    PrefixDriftModule,
    PrefixDriftService,
    RetryModule,
    Runtime,
    RuntimeModule,
    SafetyModule,
    ScriptedLLM,
    ScriptedTurn,
    StopReason,
    Tool,
    ToolCall,
    ToolResult,
)

_FLAGS: dict[str, bool | int | str] = {
    'max_wait_seconds': 5,
    'untrusted_high_risk': 'ask',
    'llm_retry_attempts': 1,
    'llm_retry_backoff_ms': 0,
    'llm_fallbacks': '',
    'prefix_drift_detection': True,
}
_ROOT_FLAGS = {
    **_FLAGS,
    'max_wait_seconds': 3,
    'untrusted_high_risk': 'allow',
}


class _Record(Tool):
    __tool_name__ = 'Record'
    __tool_command_args__ = ()

    def __init__(self) -> None:
        self.values: list[str] = []

    async def execute(self, value: str) -> ToolResult:
        self.values.append(value)
        return ToolResult(output=value)


class _DynamicPromptAgent(BaseAgent):
    def __init__(
        self,
        options: AgentOptions,
        *,
        hooks: HooksCenter,
        events: EventBus,
    ) -> None:
        super().__init__(options, hooks=hooks, events=events)
        self._prompt_calls = 0

    def get_system_prompt(self) -> str:
        self._prompt_calls += 1
        return f'dynamic prompt {self._prompt_calls}'


def _root_factory(llm: ScriptedLLM, tool: _Record) -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return _DynamicPromptAgent(
            AgentOptions(name='Root', llm=llm, tools={'Record': tool}),
            hooks=hooks,
            events=events,
        )

    return build


def _child_factory(llm: ScriptedLLM) -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name='Child', llm=llm),
            hooks=hooks,
            events=events,
        )

    return build


async def test_runtime_modules_retry_tool_inheritance_drift_and_shutdown_close_end_to_end():
    order: list[tuple[str, bool]] = []
    child_task: asyncio.Task[None] | None = None

    def shutdown_hook(label: str) -> Callable[[Runtime], Awaitable[None]]:
        async def stop(_: Runtime) -> None:
            order.append((label, child_task is not None and child_task.done()))

        return stop

    first_cleanup = RuntimeModule('cleanup-first', on_shutdown=shutdown_hook('first'))
    last_cleanup = RuntimeModule('cleanup-last', on_shutdown=shutdown_hook('last'))
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    llm = ScriptedLLM(
        [
            ScriptedTurn(raises=RuntimeError('transient')),
            ScriptedTurn(
                tool_calls=[ToolCall(id='record-1', name='Record', arguments='{"value":"written"}')]
            ),
            ScriptedTurn(content='root done'),
        ]
    )
    tool = _Record()
    runtime = Runtime(
        modules=[
            first_cleanup,
            SafetyModule(),
            RetryModule({}, sleep=sleep),
            PrefixDriftModule(),
            last_cleanup,
        ],
        flags=_FLAGS,
    )
    closed = False
    try:
        root = await runtime.spawn(
            _root_factory(llm, tool),
            query='run',
            flags={
                'max_wait_seconds': _ROOT_FLAGS['max_wait_seconds'],
                'untrusted_high_risk': _ROOT_FLAGS['untrusted_high_risk'],
            },
        )
        root_context = await asyncio.wait_for(runtime.wait(root), 2)
        assert root_context.stop_reason is StopReason.COMPLETED
        assert root_context.current_traj[-1].content == 'root done'
        assert tool.values == ['written']
        assert sleeps == [0.0]
        assert len(llm.calls) == 3

        service = cast(PrefixDriftService, cast(Any, runtime).prefix_drift)
        assert len(service.records) == 1
        assert service.records[0].agent_id == root
        assert service.records[0].unit == 'system'

        child = await runtime.spawn(
            _child_factory(ScriptedLLM([ScriptedTurn(content='unused')])),
            parent_id=root,
        )
        child_info = await runtime.find(child)
        assert child_info is not None
        assert child_info.flags == _ROOT_FLAGS
        child_task = runtime._table.get_task(child)  # pyright: ignore[reportPrivateUsage]
        assert child_task is not None and not child_task.done()

        await asyncio.wait_for(runtime.shutdown(), 2)
        closed = True

        assert order == [('last', True), ('first', True)]
        assert child_task.done()
        assert runtime._reapers == set()  # pyright: ignore[reportPrivateUsage]
        assert [
            task.get_name()
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and task.get_name().startswith('agent:')
        ] == []
    finally:
        if not closed:
            await runtime.shutdown()
