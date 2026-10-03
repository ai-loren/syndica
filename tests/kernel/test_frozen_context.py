"""B-19（M3 D9）：轨迹只由内核写。内核替一个 Agent 跑的 hook、工具与它们起的任务往它的轨迹里追加
都被拒；窗口跟着代码走，不跟着对象走。"""

from __future__ import annotations

import asyncio
import copy
import json
import pickle
from collections.abc import Callable
from typing import Any, cast

import pytest

from syndica.kernel import (
    AgentOptions,
    AgentStatus,
    BaseAgent,
    ConfirmationOutcome,
    Context,
    ContextBlock,
    ContractViolation,
    Disposition,
    EventBus,
    HookResult,
    HooksCenter,
    Junction,
    JunctionKind,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    StopReason,
    Tool,
    ToolCall,
    ToolResult,
)
from syndica.kernel.context import _frozen  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.llm import LLMConfig
from syndica.kernel.tool_executor import ToolExecutor

_TIMEOUT = 5.0


def _note(text: str, role: str = 'user') -> ContextBlock:
    return ContextBlock(role=cast(Any, role), content=text)


def _shape(ctx: Context) -> list[tuple[str, object]]:
    return [(block.role, block.tool_call_id or block.content) for block in ctx.current_traj]


def _refused(ctx: Context, refusals: list[str], text: str = 'x') -> None:
    try:
        ctx.append(_note(text))
    except ContractViolation as error:
        refusals.append(error.received)


# --- the windows themselves --------------------------------------------------------------------


def test_an_append_inside_a_window_is_refused_naming_the_window_and_the_way_out():
    ctx = Context()
    with _frozen(ctx, '窗口甲'):
        with _frozen(ctx, '窗口乙'):
            with pytest.raises(ContractViolation) as inner:
                ctx.append(_note('x'))
        with pytest.raises(ContractViolation) as outer:
            ctx.append(_note('y'))
    ctx.append(_note('z'))

    assert inner.value.received == '窗口乙期间收到一次追加（role=user）'
    assert outer.value.received == '窗口甲期间收到一次追加（role=user）'
    assert '提供方会拒掉下一次请求' in inner.value.expected
    assert "modified['result']" in inner.value.next_action
    assert 'Runtime.send' in inner.value.next_action
    assert '怎么等都写不进' in inner.value.next_action
    assert _shape(ctx) == [('user', 'z')]


def test_two_nested_windows_with_the_same_name_still_count_twice():
    ctx = Context()
    with _frozen(ctx, '同名'):
        with _frozen(ctx, '同名'):
            pass
        with pytest.raises(ContractViolation):
            ctx.append(_note('x'))
    ctx.append(_note('y'))

    assert _shape(ctx) == [('user', 'y')]


@pytest.mark.parametrize('role', ['system', 'user', 'assistant', 'tool'])
def test_a_window_refuses_every_role(role: str):
    ctx = Context()

    with _frozen(ctx, '甲'), pytest.raises(ContractViolation):
        ctx.append(_note('x', role))

    assert ctx.current_traj == []


def test_a_window_on_one_trajectory_leaves_another_writable():
    frozen, other = Context(), Context()

    with _frozen(frozen, '甲'):
        other.append(_note('fine'))

    assert _shape(other) == [('user', 'fine')]


async def test_a_window_closes_when_its_code_raises_or_is_cancelled():
    ctx = Context()

    with pytest.raises(RuntimeError), _frozen(ctx, '甲'):
        raise RuntimeError('boom')
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            with _frozen(ctx, '乙'):
                await asyncio.Event().wait()
    ctx.append(_note('after'))

    assert _shape(ctx) == [('user', 'after')]


async def test_a_task_started_inside_a_window_stays_refused_after_the_window_closes():
    ctx = Context()
    refusals: list[str] = []
    started = asyncio.Event()

    async def writer() -> None:
        started.set()
        await asyncio.sleep(0.01)
        _refused(ctx, refusals)

    with _frozen(ctx, '甲'):
        task = asyncio.get_running_loop().create_task(writer())
        await started.wait()
    await asyncio.wait_for(task, _TIMEOUT)

    assert refusals == ['甲期间收到一次追加（role=user）']
    assert ctx.current_traj == []


async def test_a_window_opened_in_another_task_does_not_freeze_this_one():
    ctx = Context()
    opened = asyncio.Event()
    release = asyncio.Event()

    async def elsewhere() -> None:
        with _frozen(ctx, '别的任务的窗口'):
            opened.set()
            await release.wait()

    task = asyncio.create_task(elsewhere())
    await asyncio.wait_for(opened.wait(), _TIMEOUT)
    ctx.append(_note('mine'))
    release.set()
    await asyncio.wait_for(task, _TIMEOUT)

    assert _shape(ctx) == [('user', 'mine')]


def test_copies_of_a_trajectory_carry_no_window():
    ctx = Context()
    ctx.append(_note('before'))
    with _frozen(ctx, '甲'):
        copied = copy.deepcopy(ctx)
        restored = cast(Context, pickle.loads(pickle.dumps(ctx)))
        copied.append(_note('into the copy'))
    restored.append(_note('into the restored one'))

    assert _shape(copied) == [('user', 'before'), ('user', 'into the copy')]
    assert _shape(restored) == [('user', 'before'), ('user', 'into the restored one')]


# --- tool segments -------------------------------------------------------------------------------


class _Writer(Tool):
    __tool_command_args__ = ()

    def __init__(self, ctx: Context, writes: str | None, refusals: list[str]) -> None:
        self._ctx = ctx
        self._writes = writes
        self._refusals = refusals
        self.spawned: list[asyncio.Task[None]] = []

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        return True

    async def execute(self, label: str = '') -> ToolResult:
        """回显 label；按构造时说的方式试着往轨迹里写。
        label: 标签
        """
        await asyncio.sleep(0)
        if self._writes == 'tool':
            _refused(self._ctx, self._refusals)
        elif self._writes == 'spawned':

            async def later() -> None:
                await asyncio.sleep(0.01)
                _refused(self._ctx, self._refusals)

            self.spawned.append(asyncio.get_running_loop().create_task(later()))
        return ToolResult(output=label)


def _named(name: str, ctx: Context, writes: str | None, refusals: list[str]) -> _Writer:
    kind = cast(type[_Writer], type(name, (_Writer,), {'__tool_name__': name}))
    return kind(ctx, writes, refusals)


def _executor(
    ctx: Context, tools: dict[str, Tool], writes: str | None, refusals: list[str]
) -> ToolExecutor:
    hooks = HooksCenter()

    async def write(**kwargs: Any) -> HookResult:
        _refused(ctx, refusals)
        return HookResult()

    if writes == 'pre':
        hooks.register(Junction.PRE_TOOL_USE, write)
    elif writes == 'post':
        hooks.register(Junction.POST_TOOL_USE, write)
    return ToolExecutor(tools=tools, hooks=hooks, max_concurrency=4)


def _calls(names: list[str]) -> list[ToolCall]:
    return [
        ToolCall(f'c{index}', name, json.dumps({'label': f'c{index}'}))
        for index, name in enumerate(names)
    ]


@pytest.mark.parametrize('count', [1, 2, 3])
@pytest.mark.parametrize('writes', ['pre', 'post', 'tool', 'spawned'])
async def test_nothing_a_tool_segment_runs_can_write_the_trajectory(writes: str, count: int):
    ctx = Context()
    refusals: list[str] = []
    tool = _named('Safe', ctx, writes if writes in ('tool', 'spawned') else None, refusals)

    await _executor(ctx, {'Safe': tool}, writes, refusals).execute_tool_calls(
        ctx, _calls(['Safe'] * count)
    )
    await asyncio.wait_for(asyncio.gather(*tool.spawned), _TIMEOUT)

    window = (
        '工具调用 Safe 执行'
        if count == 1
        else f'同一段里 {count} 个调用（{", ".join(["Safe"] * count)}）并发执行'
    )
    assert refusals == [f'{window}期间收到一次追加（role=user）'] * count
    assert _shape(ctx) == [('tool', f'c{index}') for index in range(count)]


async def test_a_segment_window_names_its_calls_in_call_order():
    ctx = Context()
    refusals: list[str] = []
    tools: dict[str, Tool] = {
        name: _named(name, ctx, 'tool', refusals) for name in ('Alpha', 'Beta', 'Gamma')
    }

    await _executor(ctx, tools, None, refusals).execute_tool_calls(
        ctx, _calls(['Gamma', 'Alpha', 'Beta'])
    )

    assert set(refusals) == {
        '同一段里 3 个调用（Gamma, Alpha, Beta）并发执行期间收到一次追加（role=user）'
    }


async def test_a_cancelled_segment_closes_its_window():
    ctx = Context()

    class _Blocks(Tool):
        __tool_name__ = 'Blocks'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """挂住。"""
            await asyncio.Event().wait()
            return ToolResult(output='never')

    executor = ToolExecutor(tools={'Blocks': _Blocks()}, hooks=HooksCenter())
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await executor.execute_tool_calls(ctx, [ToolCall('c', 'Blocks', '{}')])
    ctx.append(_note('after'))

    assert _shape(ctx) == [('user', 'after')]


class _Runner(Tool):
    __tool_name__ = 'Runner'
    __tool_command_args__ = ('command',)

    async def execute(self, command: str) -> ToolResult:
        """跑一条命令。
        command: 命令
        """
        return ToolResult(output='ran')


class _AlwaysConfirm:
    def decide(self, request: Any) -> Disposition:
        return Disposition.CONFIRM


@pytest.mark.parametrize('channel', [False, True], ids=['no-channel', 'approved'])
async def test_a_confirmation_observer_cannot_write_whether_or_not_someone_answers(channel: bool):
    ctx = Context()
    refusals: list[str] = []

    async def write(**kwargs: Any) -> HookResult:
        _refused(ctx, refusals)
        return HookResult()

    async def approve(request: Any) -> ConfirmationOutcome:
        return ConfirmationOutcome.APPROVED

    hooks = HooksCenter()
    hooks.register(Junction.TOOL_CONFIRMATION_REQUIRED, write)
    executor = ToolExecutor(tools={'Runner': _Runner()}, hooks=hooks, policy=_AlwaysConfirm())
    if channel:
        executor._configure_confirmation('agent', approve)  # pyright: ignore[reportPrivateUsage]

    await executor.execute_tool_calls(
        ctx, [ToolCall('c1', 'Runner', json.dumps({'command': 'sudo rm -rf /'}))]
    )

    assert refusals == ['工具调用 Runner 执行期间收到一次追加（role=user）']
    assert _shape(ctx) == [('tool', 'c1')]
    assert (ctx.current_traj[0].content == 'ran') is channel


# --- junction dispatch ---------------------------------------------------------------------------


_WITH_CTX = [
    Junction.AGENT_START,
    Junction.AGENT_END,
    Junction.AGENT_ERROR,
    Junction.TURN_START,
    Junction.TURN_END,
    Junction.MESSAGE_INJECTED,
    Junction.PRE_LLM_REQUEST,
    Junction.POST_LLM_RESPONSE,
    Junction.CONTEXT_OVERFLOW,
    Junction.LLM_FAILURE,
]


@pytest.mark.parametrize('junction', _WITH_CTX, ids=[junction.value for junction in _WITH_CTX])
async def test_every_dispatch_that_carries_the_trajectory_freezes_it(junction: Junction):
    center = HooksCenter()
    ctx = Context()
    refusals: list[str] = []

    async def write(**kwargs: Any) -> HookResult:
        _refused(cast(Context, kwargs['ctx']), refusals)
        return HookResult()

    center.register(junction, write)
    await center._dispatch(junction, ctx=ctx)
    ctx.append(_note('after'))

    assert refusals == [f'切点 {junction.value} 分发期间收到一次追加（role=user）']
    assert _shape(ctx) == [('user', 'after')]


async def test_an_observable_extension_junction_that_carries_the_trajectory_freezes_it():
    center = HooksCenter()
    consolidated = center.declare_extension_junction('memory:consolidated', JunctionKind.OBSERVABLE)
    ctx = Context()
    refusals: list[str] = []

    async def write(**kwargs: Any) -> HookResult:
        _refused(ctx, refusals)
        return HookResult()

    center.register('memory:consolidated', write)
    await center.trigger(consolidated, ctx=ctx)

    assert refusals == ['切点 memory:consolidated 分发期间收到一次追加（role=user）']


async def test_a_slow_observer_is_still_frozen_after_a_fast_one_finished():
    center = HooksCenter()
    ctx = Context()
    refusals: list[str] = []
    fast_done = asyncio.Event()

    async def fast(**kwargs: Any) -> HookResult:
        fast_done.set()
        return HookResult()

    async def slow(**kwargs: Any) -> HookResult:
        await fast_done.wait()
        await asyncio.sleep(0)
        _refused(ctx, refusals)
        return HookResult()

    center.register(Junction.TURN_END, fast)
    center.register(Junction.TURN_END, slow)
    await center._dispatch(Junction.TURN_END, ctx=ctx)

    assert refusals == ['切点 turn_end 分发期间收到一次追加（role=user）']


async def test_nested_dispatches_each_freeze_and_all_unwind():
    center = HooksCenter()
    ctx = Context()
    refusals: list[str] = []

    async def inner(**kwargs: Any) -> HookResult:
        _refused(ctx, refusals)
        return HookResult()

    async def outer(**kwargs: Any) -> HookResult:
        await center._dispatch(Junction.TURN_START, ctx=ctx, turn=1)
        _refused(ctx, refusals)
        return HookResult()

    center.register(Junction.TURN_END, outer)
    center.register(Junction.TURN_START, inner)
    await center._dispatch(Junction.TURN_END, ctx=ctx, turn=1, message=None)
    ctx.append(_note('after'))

    assert refusals == [
        '切点 turn_start 分发期间收到一次追加（role=user）',
        '切点 turn_end 分发期间收到一次追加（role=user）',
    ]
    assert _shape(ctx) == [('user', 'after')]


async def test_a_cancelled_dispatch_closes_its_window():
    center = HooksCenter()
    ctx = Context()

    async def stuck(**kwargs: Any) -> HookResult:
        await asyncio.Event().wait()
        return HookResult()

    center.register(Junction.TURN_END, stuck)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await center._dispatch(Junction.TURN_END, ctx=ctx)
    ctx.append(_note('after'))

    assert _shape(ctx) == [('user', 'after')]


# --- through the runtime -----------------------------------------------------------------------


def _agent(name: str, llm: object, **options: Any) -> Callable[[HooksCenter, EventBus], BaseAgent]:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name=name, llm=cast(Any, llm), **options), hooks=hooks, events=events
        )

    return build


async def test_an_observer_cannot_write_the_trajectory_even_when_it_is_alone(
    caplog: pytest.LogCaptureFixture,
):
    llm = ScriptedLLM([ScriptedTurn(content='done')])

    async def write(**kwargs: Any) -> HookResult:
        cast(Context, kwargs['ctx']).append(_note('from the observer'))
        return HookResult()

    runtime = Runtime()
    runtime.hooks.register(Junction.TURN_END, write)
    try:
        agent_id = await runtime.spawn(_agent('Root', llm), query='go')
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert context.stop_reason is StopReason.COMPLETED
    assert [block.content for block in context.current_traj] == ['go', 'done']
    [record] = [r for r in caplog.records if '挂在可观察切点 turn_end 上的 hook' in r.getMessage()]
    assert record.exc_info is not None
    error = record.exc_info[1]
    assert isinstance(error, ContractViolation)
    assert error.received == '切点 turn_end 分发期间收到一次追加（role=user）'


async def test_a_dispatch_running_in_another_task_does_not_stop_the_agent_writing():
    llm = ScriptedLLM([ScriptedTurn(content='done')])
    runtime = Runtime()
    consolidated = runtime.hooks.declare_extension_junction(
        'memory:consolidated', JunctionKind.OBSERVABLE
    )
    side: list[asyncio.Task[HookResult]] = []
    finished_reading = asyncio.Event()

    async def read_slowly(**kwargs: Any) -> HookResult:
        await asyncio.sleep(0.05)
        finished_reading.set()
        return HookResult()

    async def consolidate(**kwargs: Any) -> HookResult:
        ctx = kwargs['ctx']
        side.append(asyncio.create_task(runtime.hooks.trigger(consolidated, ctx=ctx)))
        return HookResult()

    runtime.hooks.register('memory:consolidated', read_slowly)
    runtime.hooks.register(Junction.TURN_START, consolidate)
    try:
        agent_id = await runtime.spawn(_agent('Root', llm), query='go')
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        await asyncio.wait_for(asyncio.gather(*side), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert context.stop_reason is StopReason.COMPLETED
    assert [block.content for block in context.current_traj] == ['go', 'done']
    assert finished_reading.is_set()


async def test_an_agent_woken_from_inside_one_of_its_own_windows_still_writes_its_trajectory():
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    runtime = Runtime()
    agent: list[str] = []
    wakers: list[asyncio.Task[None]] = []

    async def wake_it_later() -> None:
        async def zombie() -> bool:
            info = await runtime.registry.get(agent[0])
            return info is not None and info.status is AgentStatus.ZOMBIE

        while True:
            if await zombie():
                break
            await asyncio.sleep(0.001)
        await runtime.send(agent[0], 'again')

    async def on_end(**kwargs: Any) -> HookResult:
        if not wakers:
            wakers.append(asyncio.get_running_loop().create_task(wake_it_later()))
        return HookResult()

    runtime.hooks.register(Junction.AGENT_END, on_end)
    try:
        agent.append(await runtime.spawn(_agent('Root', llm), query='go'))
        await asyncio.wait_for(runtime.wait(agent[0]), _TIMEOUT)
        await asyncio.wait_for(wakers[0], _TIMEOUT)
        context = await asyncio.wait_for(runtime.wait(agent[0]), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert context.stop_reason is StopReason.COMPLETED
    assert [block.content for block in context.current_traj] == ['go', 'first', 'again', 'second']


class _Entering:
    """记下第一次被调用：子 Agent 走到这里时已经往自己的轨迹里写过 query 那一块。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.config = LLMConfig(provider='fake', model='entering')

    async def create(self, messages: list[Any], **kwargs: Any) -> ContextBlock:
        self.entered.set()
        return ContextBlock(role='assistant', content='child done')


async def test_a_runtime_level_dispatch_leaves_the_new_childs_trajectory_writable():
    llm = _Entering()

    async def watch(**kwargs: Any) -> HookResult:
        await asyncio.wait_for(llm.entered.wait(), _TIMEOUT)
        return HookResult()

    runtime = Runtime()
    runtime.hooks.register(Junction.AGENT_SPAWN, watch)
    try:
        child = await asyncio.wait_for(runtime.spawn(_agent('Child', llm), query='work'), _TIMEOUT)
        context = await asyncio.wait_for(runtime.wait(child), _TIMEOUT)
    finally:
        await runtime.shutdown()

    assert context.stop_reason is StopReason.COMPLETED
    assert [block.content for block in context.current_traj] == ['work', 'child done']
