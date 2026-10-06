"""背景化（spec §5.6）：跑得太久的可后台化调用先交回占位，真结果过完 POST hook 之后在之后某一轮进历
史；周期不带着在跑的后台调用结束——交最终回复时先等，别的原因停下时取消并记下。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.background_module import BackgroundModule
from syndica.kernel.context import ArtifactRef, ContextBlock, StopReason, ToolCall, TrustLevel
from syndica.kernel.events import EventBus
from syndica.kernel.history_blocks import HistoryKind, kind_of
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import LLMConfig
from syndica.kernel.runtime import Runtime
from syndica.kernel.tool import Tool, ToolResult

_TIMEOUT = 5.0


async def _now(seconds: float) -> None:
    await asyncio.sleep(0)


class _Slow(Tool):
    """等测试放行才交回。"""

    __tool_name__ = 'Slow'
    __tool_command_args__ = ()
    __tool_backgroundable__ = True

    def __init__(self, *, trust: TrustLevel = TrustLevel.TRUSTED, fail: bool = False) -> None:
        self.release = asyncio.Event()
        self.cancelled = False
        self._trust = trust
        self._fail = fail

    async def execute(self, job: str = 'build') -> ToolResult:
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self._fail:
            raise RuntimeError('build broke')
        artifact = ArtifactRef(
            id='x',
            path='out/build.log',
            kind='document',
            mime='text/plain',
            version='v1',
            producer='',
        )
        return ToolResult(output=f'{job} finished', trust=self._trust, artifacts=[artifact])


class _SlowButNotDeclared(_Slow):
    __tool_name__ = 'Stuck'
    __tool_backgroundable__ = False


class _Releases(Tool):
    """调用时放行另一个工具。"""

    __tool_name__ = 'Release'
    __tool_command_args__ = ()

    def __init__(self, target: _Slow) -> None:
        self._target = target

    async def execute(self) -> ToolResult:
        self._target.release.set()
        await asyncio.sleep(0.05)
        return ToolResult(output='released')


def _call(name: str, call_id: str, **arguments: object) -> ContextBlock:
    return ContextBlock(
        role='assistant',
        tool_calls=[ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))],
    )


def _say(text: str) -> ContextBlock:
    return ContextBlock(role='assistant', content=text)


class _Script:
    def __init__(self, steps: list[ContextBlock]) -> None:
        self.config = LLMConfig(provider='scripted', model='m')
        self._steps = steps
        self.calls: list[list[ContextBlock]] = []
        self.tool_names: list[list[str]] = []

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        await asyncio.sleep(0)
        self.calls.append(list(messages))
        self.tool_names.append([schema['name'] for schema in kwargs.get('tools', [])])
        return self._steps[len(self.calls) - 1]


def _runtime(after: int = 1, **options: Any) -> Runtime:
    return Runtime(
        modules=[BackgroundModule(timer=_now)], flags={'background_after_seconds': after}, **options
    )


def _factory(llm: _Script, tools: dict[str, Tool], max_turns: int = 10) -> Any:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=llm,
                tools=dict(tools),
                system_prompt='SP',
                max_turns=max_turns,  # type: ignore[arg-type]
            ),
            hooks=hooks,
            events=events,
        )

    return build


def _background_blocks(ctx: Any) -> list[ContextBlock]:
    return [b for b in ctx.current_traj if kind_of(b) is HistoryKind.BACKGROUND]


def _tool_answer(ctx: Any, call_id: str) -> str:
    return str(next(b for b in ctx.current_traj if b.tool_call_id == call_id).content)


async def _run(runtime: Runtime, factory: Any) -> tuple[str, Any]:
    agent_id = await runtime.spawn(factory, query='go')
    return agent_id, await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)


async def test_a_slow_call_hands_back_a_placeholder_and_its_result_arrives_in_a_later_turn() -> (
    None
):
    slow = _Slow()
    llm = _Script([_call('Slow', 's'), _call('Release', 'r'), _say('done')])
    runtime = _runtime()
    try:
        _, ctx = await _run(runtime, _factory(llm, {'Slow': slow, 'Release': _Releases(slow)}))
    finally:
        await runtime.shutdown()
    [result] = _background_blocks(ctx)
    assert 'bg-1' in _tool_answer(ctx, 's') and 'QueryBackgroundTool(bg-1)' in _tool_answer(
        ctx, 's'
    )
    assert 'build finished' in str(result.content)
    assert result in llm.calls[2]
    assert ctx.stop_reason is StopReason.COMPLETED


async def test_a_final_answer_waits_for_a_running_background_call() -> None:
    slow = _Slow()
    llm = _Script([_call('Slow', 's'), _say('early'), _say('done')])
    runtime = _runtime()
    try:
        agent_id = await runtime.spawn(_factory(llm, {'Slow': slow}), query='go')
        for _ in range(100):
            if len(llm.calls) == 2:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert len(llm.calls) == 2
        slow.release.set()
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()
    assert len(llm.calls) == 3
    assert ctx.current_traj[-1].content == 'done'
    assert ctx.stop_reason is StopReason.COMPLETED


async def test_post_hooks_and_both_junctions_see_the_real_result() -> None:
    slow = _Slow()
    seen: list[tuple[str, object]] = []

    async def post(**kwargs: Any) -> HookResult:
        result = kwargs['result']
        seen.append(('post', result.output))
        return HookResult(modified={'result': ToolResult(output=f'{result.output} [checked]')})

    async def moved(**kwargs: Any) -> HookResult:
        seen.append(('moved', kwargs['background_id']))
        return HookResult()

    async def completed(**kwargs: Any) -> HookResult:
        seen.append(('completed', kwargs['result'].output))
        return HookResult()

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _factory(llm, {'Slow': slow, 'Release': _Releases(slow)})(hooks, events)
        agent.register_hook(Junction.POST_TOOL_USE, post)
        agent.register_hook(Junction.TOOL_MOVED_TO_BACKGROUND, moved)
        agent.register_hook(Junction.BACKGROUND_TOOL_COMPLETED, completed)
        return agent

    llm = _Script([_call('Slow', 's'), _call('Release', 'r'), _say('done')])
    runtime = _runtime()
    try:
        _, ctx = await _run(runtime, build)
    finally:
        await runtime.shutdown()
    [result] = _background_blocks(ctx)
    assert ('moved', 'bg-1') in seen
    assert ('post', 'build finished') in seen
    assert ('completed', 'build finished [checked]') in seen
    assert 'build finished [checked]' in str(result.content)
    assert not any(kind == 'post' and 'bg-1' in str(value) for kind, value in seen)


async def test_an_untrusted_background_result_stays_untrusted() -> None:
    slow = _Slow(trust=TrustLevel.UNTRUSTED)
    llm = _Script([_call('Slow', 's'), _call('Release', 'r'), _say('done')])
    runtime = _runtime()
    try:
        _, ctx = await _run(runtime, _factory(llm, {'Slow': slow, 'Release': _Releases(slow)}))
    finally:
        await runtime.shutdown()
    [result] = _background_blocks(ctx)
    assert result.trust is TrustLevel.UNTRUSTED


async def test_a_tool_that_does_not_declare_it_is_never_moved() -> None:
    stuck = _SlowButNotDeclared()
    llm = _Script([_call('Stuck', 's'), _say('done')])
    runtime = _runtime()
    try:
        agent_id = await runtime.spawn(_factory(llm, {'Stuck': stuck}), query='go')
        await asyncio.sleep(0.05)
        assert len(llm.calls) == 1
        stuck.release.set()
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()
    assert _tool_answer(ctx, 's') == 'build finished'
    assert _background_blocks(ctx) == []


async def test_with_the_flag_at_zero_nothing_moves_and_no_tools_are_added() -> None:
    slow = _Slow()
    llm = _Script([_call('Slow', 's'), _say('done')])
    runtime = _runtime(after=0)
    try:
        agent_id = await runtime.spawn(_factory(llm, {'Slow': slow}), query='go')
        await asyncio.sleep(0.05)
        slow.release.set()
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()
    assert _tool_answer(ctx, 's') == 'build finished'
    assert not {'QueryBackgroundTool', 'CancelBackgroundTool'} & set(llm.tool_names[0])


async def test_query_reports_a_running_call_and_cancel_stops_it_without_waiting() -> None:
    slow = _Slow()
    llm = _Script(
        [
            _call('Slow', 's'),
            _call('QueryBackgroundTool', 'q', background_id='bg-1'),
            _call('CancelBackgroundTool', 'c', background_id='bg-1'),
            _call('QueryBackgroundTool', 'u', background_id='bg-9'),
            _say('done'),
        ]
    )
    runtime = _runtime()
    try:
        _, ctx = await _run(runtime, _factory(llm, {'Slow': slow}))
    finally:
        await runtime.shutdown()
    assert '还在跑' in _tool_answer(ctx, 'q')
    assert '已取消' in _tool_answer(ctx, 'c')
    assert '期望' in _tool_answer(ctx, 'u') and 'bg-9' in _tool_answer(ctx, 'u')
    await asyncio.sleep(0)
    assert slow.cancelled
    assert _background_blocks(ctx) == []
    assert ctx.stop_reason is StopReason.COMPLETED


async def test_aborting_an_agent_waiting_on_background_cancels_it_and_records_it() -> None:
    slow = _Slow()
    llm = _Script([_call('Slow', 's'), _say('early')])
    runtime = _runtime()
    try:
        agent_id = await runtime.spawn(_factory(llm, {'Slow': slow}), query='go')
        for _ in range(100):
            if len(llm.calls) == 2:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)
        await runtime.abort(agent_id)
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()
    [record] = _background_blocks(ctx)
    assert ctx.stop_reason is StopReason.ABORTED
    assert '被中止了' in str(record.content) and '副作用未知' in str(record.content)
    assert slow.cancelled


async def test_running_out_of_turns_cancels_background_and_records_it() -> None:
    slow = _Slow()
    llm = _Script([_call('Slow', 's'), _call('Slow', 's2')])
    runtime = _runtime()
    try:
        _, ctx = await _run(runtime, _factory(llm, {'Slow': slow}, max_turns=2))
    finally:
        await runtime.shutdown()
    records = _background_blocks(ctx)
    assert ctx.stop_reason is StopReason.MAX_TURNS
    assert len(records) == 2 and all('轮次用完了' in str(b.content) for b in records)


async def test_killing_an_agent_cancels_its_background_calls() -> None:
    slow = _Slow()
    llm = _Script([_call('Slow', 's'), _say('early')])
    runtime = _runtime()
    try:
        agent_id = await runtime.spawn(_factory(llm, {'Slow': slow}), query='go')
        for _ in range(100):
            if len(llm.calls) == 2:
                break
            await asyncio.sleep(0.01)
        await runtime.kill(agent_id)
        for _ in range(100):
            if slow.cancelled:
                break
            await asyncio.sleep(0.01)
    finally:
        await runtime.shutdown()
    assert slow.cancelled


async def test_a_background_call_that_raises_still_reports_back() -> None:
    slow = _Slow(fail=True)
    llm = _Script([_call('Slow', 's'), _call('Release', 'r'), _say('done')])
    runtime = _runtime()
    try:
        _, ctx = await _run(runtime, _factory(llm, {'Slow': slow, 'Release': _Releases(slow)}))
    finally:
        await runtime.shutdown()
    [result] = _background_blocks(ctx)
    assert 'build broke' in str(result.content)
    assert ctx.stop_reason is StopReason.COMPLETED


async def test_a_background_results_artifacts_are_registered() -> None:
    from syndica.kernel.stores.memory_artifact import InMemoryArtifactStore

    store = InMemoryArtifactStore()
    slow = _Slow()
    llm = _Script([_call('Slow', 's'), _call('Release', 'r'), _say('done')])
    runtime = _runtime(artifacts=store)
    try:
        await _run(runtime, _factory(llm, {'Slow': slow, 'Release': _Releases(slow)}))
    finally:
        await runtime.shutdown()
    assert [ref.path for ref in await store.list()] == ['out/build.log']


async def test_the_trace_marks_the_placeholder_and_records_the_real_result() -> None:
    from syndica.kernel.redaction import RuleRedactor
    from syndica.kernel.stores.memory_trace import InMemoryTraceStore
    from syndica.kernel.trace_recorder import TraceRecorder

    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    slow = _Slow()
    llm = _Script([_call('Slow', 's'), _call('Release', 'r'), _say('done')])
    runtime = _runtime(tracer=recorder)
    try:
        await _run(runtime, _factory(llm, {'Slow': slow, 'Release': _Releases(slow)}))
    finally:
        await runtime.shutdown()
    await recorder.finish()
    spans = [
        s for s in await store.spans(recorder.trace_id) if s.attributes.get('tool.name') == 'Slow'
    ]
    outcomes = sorted(str(s.attributes.get('tool.outcome')) for s in spans)
    assert outcomes == ['backgrounded', 'ok']
    assert [s.attributes.get('background.id') for s in spans] == ['bg-1', 'bg-1']


@pytest.mark.parametrize('value', [-1, True])
async def test_the_threshold_must_be_a_non_negative_integer(value: object) -> None:
    from syndica.kernel.errors import ConfigError

    with pytest.raises(ConfigError):
        runtime = Runtime(
            modules=[BackgroundModule()], flags={'background_after_seconds': cast(Any, value)}
        )
        await runtime.shutdown()


async def test_a_human_approved_call_is_never_moved() -> None:
    from syndica.kernel.background import BackgroundCalls
    from syndica.kernel.context import Context
    from syndica.kernel.danger import (
        ConfirmationOutcome,
        ConfirmationRequest,
        DangerRequest,
        Disposition,
    )
    from syndica.kernel.tool_executor import ToolExecutor

    class _Confirm:
        def decide(self, request: DangerRequest) -> Disposition:
            return Disposition.CONFIRM

    async def approve(request: ConfirmationRequest) -> ConfirmationOutcome:
        return ConfirmationOutcome.APPROVED

    slow = _Slow()
    executor = ToolExecutor(tools={'Slow': slow}, hooks=HooksCenter(), policy=_Confirm())
    executor._configure_confirmation('agent-1', approve)  # pyright: ignore[reportPrivateUsage]
    executor._configure_background(BackgroundCalls(), 1.0, _now)  # pyright: ignore[reportPrivateUsage]
    ctx = Context()
    running = asyncio.create_task(
        executor.execute_tool_calls(ctx, [ToolCall(id='s', name='Slow', arguments='{}')])
    )
    await asyncio.sleep(0.05)
    assert not running.done()
    slow.release.set()
    await asyncio.wait_for(running, _TIMEOUT)
    assert ctx.current_traj[-1].content == 'build finished'


class _Stubborn(_Slow):
    """被取消时吞掉取消、照样交回一个结果。"""

    async def execute(self, job: str = 'build') -> ToolResult:
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
        return ToolResult(output='finished anyway')


async def test_a_cancelled_call_that_finishes_anyway_is_not_reported_as_done() -> None:
    stubborn = _Stubborn()
    llm = _Script(
        [
            _call('Slow', 's'),
            _call('CancelBackgroundTool', 'c', background_id='bg-1'),
            _call('QueryBackgroundTool', 'q', background_id='bg-1'),
            _say('done'),
        ]
    )
    runtime = _runtime()
    try:
        _, ctx = await _run(runtime, _factory(llm, {'Slow': stubborn}))
    finally:
        await runtime.shutdown()
    assert stubborn.cancelled
    assert '已被取消' in _tool_answer(ctx, 'q')
    assert _background_blocks(ctx) == []
