"""预算接进 Runtime 与 turn 循环（spec §8.4，计划 P1 B1 / B2 / H1 / H3 / H4）：收尾轮在上限之内、
子 Agent 从父切分且不得超额、准入与计量只有一个口子、耗尽的 Agent 拒收消息。"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any, cast

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.budget import Budget, BudgetPolicy, BudgetView, Exhaustion, Prices, StaticPrices
from syndica.kernel.budget_tree import BudgetRefused
from syndica.kernel.context import ContextBlock, StopReason, ToolCall
from syndica.kernel.events import EventBus
from syndica.kernel.history_blocks import HistoryKind, kind_of
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import LLMConfig, ScriptedLLM, ScriptedTurn
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.trace import AttributeValue, SpanKind, SpanStatus
from syndica.kernel.tracing import NullTracer

_TIMEOUT = 5.0


class _Echo(Tool):
    __tool_name__ = 'Echo'
    __tool_command_args__ = ()

    def __init__(self, ran: list[str] | None = None) -> None:
        self.ran = ran if ran is not None else []

    async def execute(self, text: str = 'x') -> ToolResult:
        self.ran.append(text)
        return ToolResult(output=f'echo {text}')


class _Reporting:
    """包一层脚本化 LLM，每次回复都报一份 usage——不变量要从这份独立的账上核，不从树自己的算术核。"""

    def __init__(self, script: list[ScriptedTurn], *, usage: tuple[int, int], model: str = 'm'):
        self.inner = ScriptedLLM(script)
        self.reported: list[tuple[int, int]] = []
        self._usage = usage
        self.config = LLMConfig(provider='scripted', model=model, request_args={'max_tokens': 20})

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        reply = await self.inner.create(messages, **kwargs)
        self.reported.append(self._usage)
        inp, out = self._usage
        return ContextBlock(
            role='assistant',
            content=reply.content,
            tool_calls=reply.tool_calls,
            extra={'usage': {'input_tokens': inp, 'output_tokens': out}},
        )


class _Events(NullTracer):
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, AttributeValue]]] = []
        self.spans: dict[str, tuple[str | None, SpanKind, dict[str, AttributeValue]]] = {}
        self.ended: dict[str, dict[str, AttributeValue]] = {}

    def start_span(
        self,
        span_id: str,
        parent_span_id: str | None,
        kind: SpanKind,
        name: str,
        agent_id: str,
        attributes: dict[str, AttributeValue],
    ) -> None:
        self.spans[span_id] = (parent_span_id, kind, dict(attributes))

    def end_span(
        self, span_id: str, status: SpanStatus, attributes: dict[str, AttributeValue]
    ) -> None:
        self.ended[span_id] = dict(attributes)

    def add_event(self, span_id: str, name: str, attributes: dict[str, AttributeValue]) -> None:
        self.events.append((name, dict(attributes)))


def _echo(call_id: str) -> ScriptedTurn:
    return ScriptedTurn(tool_calls=[ToolCall(id=call_id, name='Echo', arguments='{}')])


def _factory(llm: Any, tools: dict[str, Tool] | None = None, name: str = 'Worker') -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name=name, llm=llm, tools=dict(tools or {}), system_prompt='SP'),
            hooks=hooks,
            events=events,
        )

    return build


def _budget_blocks(blocks: list[ContextBlock]) -> list[str]:
    return [str(b.content) for b in blocks if kind_of(b) is HistoryKind.BUDGET]


async def _run(
    runtime: Runtime, factory: AgentFactory, query: str = 'go'
) -> tuple[str, list[ContextBlock], StopReason | None]:
    agent_id = await runtime.spawn(factory, query=query)
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    return agent_id, ctx.current_traj, ctx.stop_reason


# ---- 收尾 ----


async def test_the_last_turn_is_a_wrap_up_whose_tool_calls_are_not_executed() -> None:
    echo = _Echo()
    llm = ScriptedLLM([_echo('c1'), _echo('c2'), _echo('c3'), ScriptedTurn(content='never')])
    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=3)))
    try:
        _, traj, reason = await _run(runtime, _factory(llm, {'Echo': echo}))
    finally:
        await runtime.shutdown()
    assert reason is StopReason.BUDGET
    assert len(echo.ran) == 2
    assert len(llm.calls) == 3
    assert any('最后一轮' in text for text in _budget_blocks(traj))
    unexecuted = [b for b in traj if b.role == 'tool' and b.tool_call_id == 'c3']
    assert unexecuted and '预算用完' in str(unexecuted[0].content)


async def test_a_wrap_up_reply_without_tool_calls_is_the_final_answer() -> None:
    llm = ScriptedLLM([_echo('c1'), ScriptedTurn(content='partial result')])
    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=2)))
    try:
        _, traj, reason = await _run(runtime, _factory(llm, {'Echo': _Echo()}))
    finally:
        await runtime.shutdown()
    assert reason is StopReason.BUDGET
    assert traj[-1].role == 'assistant' and traj[-1].content == 'partial result'


async def test_hard_stop_uses_every_turn_then_stops_without_a_model_call() -> None:
    echo = _Echo()
    llm = ScriptedLLM([_echo('c1'), _echo('c2'), _echo('c3'), ScriptedTurn(content='never')])
    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=3), on_exhausted=Exhaustion.HARD_STOP))
    try:
        _, traj, reason = await _run(runtime, _factory(llm, {'Echo': echo}))
    finally:
        await runtime.shutdown()
    assert reason is StopReason.BUDGET
    assert (len(echo.ran), len(llm.calls)) == (3, 3)
    assert not any('最后一轮' in text for text in _budget_blocks(traj))


async def test_an_unbudgeted_run_has_no_budget_blocks_and_no_get_budget_tool() -> None:
    llm = ScriptedLLM([ScriptedTurn(content='done')])
    runtime = Runtime()
    try:
        _, traj, reason = await _run(runtime, _factory(llm))
    finally:
        await runtime.shutdown()
    assert reason is StopReason.COMPLETED
    assert _budget_blocks(traj) == []
    assert [t['function']['name'] for t in llm.call_kwargs[0]['tools']] == []


async def test_a_budgeted_agent_gets_get_budget_and_reads_its_remaining_turns() -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='b', name='GetBudget', arguments='{}')]),
            ScriptedTurn(content='done'),
        ]
    )
    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=10)))
    try:
        _, traj, _ = await _run(runtime, _factory(llm))
    finally:
        await runtime.shutdown()
    reading = json.loads(str(next(b for b in traj if b.tool_call_id == 'b').content))
    assert reading['limit']['turns'] == 10
    assert reading['remaining']['turns'] == 9
    assert reading['used']['turns'] == 1


async def test_the_cycle_start_block_and_a_threshold_block_are_appended_not_inserted() -> None:
    llm = ScriptedLLM([_echo(f'c{i}') for i in range(5)] + [ScriptedTurn(content='done')])
    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=8)))
    try:
        _, traj, _ = await _run(runtime, _factory(llm, {'Echo': _Echo()}))
    finally:
        await runtime.shutdown()
    texts = _budget_blocks(traj)
    assert texts[0].startswith('本周期开始时的预算')
    assert any(text.startswith('预算提醒：最紧的那一维只剩不到 50%') for text in texts)
    for earlier, later in zip(llm.calls, llm.calls[1:], strict=False):
        assert later[: len(earlier)] == earlier


# ---- 切分与子 Agent ----


async def test_a_delegated_child_gets_half_of_what_its_parent_has_left() -> None:
    child_llm = ScriptedLLM([ScriptedTurn(content='child done')])
    parent_llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='d', name='Delegate', arguments='{"agent": "Child", "query": "sub"}'
                    )
                ]
            ),
            ScriptedTurn(content='parent done'),
        ]
    )
    runtime = Runtime(
        orchestration=True,
        agents={'Child': _factory(child_llm, name='Child')},
        budget=BudgetPolicy(root=Budget(turns=20)),
    )
    seen: list[BudgetView | None] = []
    original = runtime.spawn

    async def spawn(*args: Any, **kwargs: Any) -> str:
        child = await original(*args, **kwargs)
        seen.append(runtime._budget_of(child))  # pyright: ignore[reportPrivateUsage]
        return child

    runtime.spawn = spawn  # type: ignore[method-assign]
    try:
        root = await original(_factory(parent_llm, name='Parent'), query='go')
        await asyncio.wait_for(runtime.wait(root), _TIMEOUT)
        parent_view = runtime._budget_of(root)  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()
    assert seen[0] is not None and seen[0].limit.turns == 10
    assert parent_view is not None and parent_view.used.turns == 3


async def test_asking_for_more_than_the_parent_has_is_answered_to_the_model() -> None:
    parent_llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='d',
                        name='Delegate',
                        arguments='{"agent": "Child", "query": "sub", "turns": 50}',
                    )
                ]
            ),
            ScriptedTurn(content='parent done'),
        ]
    )
    runtime = Runtime(
        orchestration=True,
        agents={'Child': _factory(ScriptedLLM([]), name='Child')},
        budget=BudgetPolicy(root=Budget(turns=10)),
    )
    try:
        _, traj, reason = await _run(runtime, _factory(parent_llm, name='Parent'))
        listed = await runtime.registry.list()
    finally:
        await runtime.shutdown()
    answer = str(next(b for b in traj if b.tool_call_id == 'd').content)
    assert reason is StopReason.COMPLETED
    assert 'turns=50' in answer and '降到' in answer
    assert [info.name for info in listed] == ['Parent']


async def test_a_child_out_of_budget_hands_back_its_wrap_up_as_a_partial_result() -> None:
    child_llm = ScriptedLLM([_echo('c1'), ScriptedTurn(content='half of it')])
    parent_llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='d',
                        name='Delegate',
                        arguments='{"agent": "Child", "query": "sub", "turns": 2}',
                    )
                ]
            ),
            ScriptedTurn(content='parent done'),
        ]
    )
    runtime = Runtime(
        orchestration=True,
        agents={'Child': _factory(child_llm, {'Echo': _Echo()}, name='Child')},
        budget=BudgetPolicy(root=Budget(turns=10)),
    )
    try:
        _, traj, _ = await _run(runtime, _factory(parent_llm, name='Parent'))
    finally:
        await runtime.shutdown()
    answer = str(next(b for b in traj if b.tool_call_id == 'd').content)
    assert '预算用完了' in answer and 'half of it' in answer
    assert 'SendMessage 叫不醒' in answer


async def test_a_message_to_an_agent_out_of_budget_is_refused_not_dropped() -> None:
    llm = ScriptedLLM([ScriptedTurn(content='only turn')])
    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=1)))
    try:
        agent_id, _, reason = await _run(runtime, _factory(llm))
        delivered = await runtime.send(agent_id, 'more please')
    finally:
        await runtime.shutdown()
    assert reason is StopReason.BUDGET
    assert delivered.ok is False
    assert delivered.reason is not None and 'turns' in delivered.reason
    assert '追加预算' in delivered.reason


async def test_a_finished_child_returns_what_it_did_not_use() -> None:
    child_llm = ScriptedLLM([ScriptedTurn(content='quick')])
    parent_llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='d', name='Delegate', arguments='{"agent": "Child", "query": "sub"}'
                    )
                ]
            ),
            ScriptedTurn(tool_calls=[ToolCall(id='b', name='GetBudget', arguments='{}')]),
            ScriptedTurn(content='parent done'),
        ]
    )
    runtime = Runtime(
        orchestration=True,
        agents={'Child': _factory(child_llm, name='Child')},
        budget=BudgetPolicy(root=Budget(turns=20)),
    )
    try:
        _, traj, _ = await _run(runtime, _factory(parent_llm, name='Parent'))
    finally:
        await runtime.shutdown()
    reading = json.loads(str(next(b for b in traj if b.tool_call_id == 'b').content))
    assert reading['remaining']['turns'] == 20 - 2 - 1


# ---- 计量与准入 ----


async def test_reported_spend_never_exceeds_the_token_budget() -> None:
    llm = _Reporting([_echo(f'c{i}') for i in range(40)], usage=(100, 10))
    runtime = Runtime(budget=BudgetPolicy(root=Budget(tokens=1000)))
    try:
        _, _, reason = await _run(runtime, _factory(llm, {'Echo': _Echo()}))
    finally:
        await runtime.shutdown()
    assert reason is StopReason.BUDGET
    spent = sum(inp + out for inp, out in llm.reported)
    assert 500 <= spent <= 1000


async def test_a_cost_cap_without_a_price_is_refused_at_spawn() -> None:
    runtime = Runtime(budget=BudgetPolicy(root=Budget(cost=Decimal('1')), prices=StaticPrices({})))
    try:
        with pytest.raises(BudgetRefused) as caught:
            await runtime.spawn(_factory(ScriptedLLM([])), query='go')
        listed = await runtime.registry.list()
    finally:
        await runtime.shutdown()
    assert "'scripted'" in caught.value.received and 'prices' in caught.value.next_action
    assert listed == []


async def test_a_priced_cost_cap_stops_the_run_within_budget() -> None:
    price = Prices(input=Decimal(1000), output=Decimal(1000), cached_input=Decimal(1000))
    llm = _Reporting([_echo(f'c{i}') for i in range(40)], usage=(100, 10))
    runtime = Runtime(
        budget=BudgetPolicy(root=Budget(cost=Decimal('1')), prices=StaticPrices({'m': price}))
    )
    try:
        agent_id, _, reason = await _run(runtime, _factory(llm, {'Echo': _Echo()}))
        view = runtime._budget_of(agent_id)  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()
    assert reason is StopReason.BUDGET
    assert view is not None and view.used.cost is not None and view.used.cost <= Decimal(1)
    assert view.used.cost == Decimal(110) * len(llm.reported) * 1000 / 1_000_000


# ---- ask_human ----


async def test_ask_human_granting_more_turns_lets_the_agent_continue() -> None:
    asked: list[int | None] = []

    async def ask(agent_id: str, view: BudgetView) -> Budget | None:
        asked.append(view.remaining.turns)
        return Budget(turns=2) if len(asked) == 1 else None

    echo = _Echo()
    llm = ScriptedLLM([_echo('c1'), _echo('c2'), _echo('c3'), ScriptedTurn(content='done')])
    runtime = Runtime(
        budget=BudgetPolicy(root=Budget(turns=2), on_exhausted=Exhaustion.ASK_HUMAN, ask_human=ask)
    )
    try:
        _, _, reason = await _run(runtime, _factory(llm, {'Echo': echo}))
    finally:
        await runtime.shutdown()
    assert asked == [1, 1]
    assert reason is StopReason.BUDGET
    assert len(echo.ran) == 3


async def test_ask_human_that_times_out_falls_back_to_a_wrap_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def ask(agent_id: str, view: BudgetView) -> Budget | None:
        await asyncio.sleep(10)
        return Budget(turns=5)

    llm = ScriptedLLM([_echo('c1'), ScriptedTurn(content='wrapped')])
    runtime = Runtime(
        budget=BudgetPolicy(
            root=Budget(turns=2),
            on_exhausted=Exhaustion.ASK_HUMAN,
            ask_human=ask,
            ask_timeout_s=0.05,
        )
    )
    try:
        with caplog.at_level(logging.ERROR):
            _, traj, reason = await _run(runtime, _factory(llm, {'Echo': _Echo()}))
    finally:
        await runtime.shutdown()
    assert reason is StopReason.BUDGET
    assert traj[-1].content == 'wrapped'
    [record] = [r for r in caplog.records if 'ask_human' in r.getMessage()]
    args = cast(tuple[object, ...], record.args)
    assert args == (0.05, args[1])


async def test_ask_human_that_raises_falls_back_to_a_wrap_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def ask(agent_id: str, view: BudgetView) -> Budget | None:
        raise RuntimeError('no human here')

    llm = ScriptedLLM([_echo('c1'), ScriptedTurn(content='wrapped')])
    runtime = Runtime(
        budget=BudgetPolicy(root=Budget(turns=2), on_exhausted=Exhaustion.ASK_HUMAN, ask_human=ask)
    )
    try:
        with caplog.at_level(logging.ERROR):
            agent_id, traj, reason = await _run(runtime, _factory(llm, {'Echo': _Echo()}))
    finally:
        await runtime.shutdown()
    assert reason is StopReason.BUDGET and traj[-1].content == 'wrapped'
    [record] = [r for r in caplog.records if 'ask_human' in r.getMessage()]
    args = cast(tuple[object, ...], record.args)
    assert args == (agent_id, 'RuntimeError', args[2])


# ---- 拒绝铸钱、trace ----


async def test_a_grant_from_a_tightened_module_is_refused() -> None:
    from syndica.kernel.hooks.installing import installing
    from syndica.kernel.modules import RuntimeModule

    runtime = Runtime(modules=[RuntimeModule('vendor')], untrusted_output_modules=('vendor',))
    try:
        agent_id = await runtime.spawn(_factory(ScriptedLLM([ScriptedTurn(content='x')])))
        with installing('module', 'vendor'), pytest.raises(Exception) as caught:
            runtime._grant_budget(agent_id, Budget(turns=100))  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()
    assert 'vendor' in str(caught.value) and 'ask_human' in str(caught.value)


async def test_the_agent_span_carries_the_spend_and_the_exhaustion_is_an_event() -> None:
    tracer = _Events()
    llm = ScriptedLLM([_echo('c1'), ScriptedTurn(content='wrapped')])
    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=2)))
    runtime._set_tracer(tracer)  # pyright: ignore[reportPrivateUsage]
    try:
        await _run(runtime, _factory(llm, {'Echo': _Echo()}))
    finally:
        await runtime.shutdown()
    [agent_span] = [sid for sid, (_, kind, _) in tracer.spans.items() if kind is SpanKind.AGENT]
    ended = tracer.ended[agent_span]
    assert (ended['budget.turns'], ended['budget.limit_turns'], ended['stop_reason']) == (
        2,
        2,
        'budget',
    )
    assert (
        'budget.exhausted',
        {
            'budget.dimensions': ['turns'],
            'budget.disposition': 'graceful_wrapup',
        },
    ) in tracer.events


class _Barrier:
    """两个 Agent 的调用都在飞之后才一起返回：检查与记账之间若没有预占，两个都会被放进去。"""

    def __init__(self, parties: int) -> None:
        self.arrived = 0
        self.parties = parties
        self.both = asyncio.Event()
        self.config = LLMConfig(provider='scripted', model='m', request_args={'max_tokens': 20})
        self.calls = 0

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        self.calls += 1
        self.arrived += 1
        if self.arrived >= self.parties:
            self.both.set()
        try:
            async with asyncio.timeout(0.3):
                await self.both.wait()
        except TimeoutError:
            pass
        return ContextBlock(
            role='assistant',
            content='done',
            extra={'usage': {'input_tokens': 600, 'output_tokens': 10}},
        )


async def test_concurrent_agents_cannot_jointly_overrun_the_pool() -> None:
    llm = _Barrier(parties=2)
    runtime = Runtime(budget=BudgetPolicy(root=Budget(tokens=1000)))
    query = 'x' * 2400
    try:
        first = await runtime.spawn(_factory(llm), query=query)
        second = await runtime.spawn(_factory(llm), query=query)
        contexts = [
            await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT) for agent_id in (first, second)
        ]
        pool = runtime._budget.pool().view()  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()
    assert llm.calls == 1
    assert [ctx.stop_reason for ctx in contexts] == [StopReason.BUDGET, StopReason.BUDGET]
    assert pool.used.tokens == 610


async def test_a_companion_spends_the_served_agents_tokens_but_not_its_turns() -> None:
    from syndica.kernel.agent import SERVED, _Served  # pyright: ignore[reportPrivateUsage]
    from syndica.kernel.budget_tree import BudgetTree
    from syndica.kernel.companion import run_companion
    from syndica.kernel.tracing import _Tracing  # pyright: ignore[reportPrivateUsage]

    tree = BudgetTree(BudgetPolicy())
    served = BaseAgent(
        AgentOptions(name='Main', llm=ScriptedLLM([])), hooks=HooksCenter(), events=EventBus()
    )
    account = tree.carve(served.agent_id, None, Budget(tokens=10_000, turns=3))
    served._attach_budget(account)  # pyright: ignore[reportPrivateUsage]
    tracer = _Events()
    tracing = _Tracing(tracer)
    token = SERVED.set(_Served(served, account, tracing))
    try:
        companion = _Reporting([ScriptedTurn(content='{"ok": true}')], usage=(300, 50))
        with tracing.span(SpanKind.TURN, (served.agent_id, 1), name='turn', agent_id='x') as turn:
            result = await run_companion(
                name='OutputVerifier', llm=companion, system_prompt='judge', task='look'
            )
    finally:
        SERVED.reset(token)
    view = account.view()
    assert result.reply == '{"ok": true}'
    assert (view.used.tokens, view.used.turns) == (350, 0)
    [(parent, attributes)] = [
        (p, a) for p, kind, a in tracer.spans.values() if kind is SpanKind.AGENT
    ]
    assert parent == turn.span_id
    assert attributes['agent.serves'] == served.agent_id


async def test_a_message_accepted_during_the_wrap_up_does_not_open_another_cycle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _Clock()
    holder: dict[str, Any] = {}

    class _SendsOnTheLastCall(_Scripted):
        async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
            if not self._turns:
                runtime: Runtime = holder['runtime']
                delivered = await runtime.send(holder['id'], 'one more thing', activate=False)
                holder['delivered'] = delivered.ok
            return await super().create(messages, **kwargs)

    calls = [ToolCall(id='t', name='Tick', arguments='{}')]
    llm = _SendsOnTheLastCall([calls, calls], clock)
    runtime = Runtime(budget=BudgetPolicy(root=Budget(wall_clock_s=100)))
    runtime._budget._clock = clock  # pyright: ignore[reportPrivateUsage]
    holder['runtime'] = runtime
    try:
        with caplog.at_level(logging.ERROR):
            holder['id'] = await runtime.spawn(_factory(llm, {'Tick': _Tick()}), query='go')
            ctx = await asyncio.wait_for(runtime.wait(holder['id']), _TIMEOUT)
            await asyncio.sleep(0.05)
        agent = runtime._table.get(holder['id'])  # pyright: ignore[reportPrivateUsage]
        cycles = None if agent is None else agent._cycle  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()
    assert holder['delivered'] is True
    assert ctx.stop_reason is StopReason.BUDGET
    assert cycles == 1
    [record] = [r for r in caplog.records if '已经收下的输入都已处理' in r.getMessage()]
    assert cast(tuple[object, ...], record.args)[0] == holder['id']


async def test_a_declined_ask_is_not_repeated_when_the_agent_is_woken_again() -> None:
    clock = _Clock()
    asked: list[float | None] = []

    async def ask(agent_id: str, view: BudgetView) -> Budget | None:
        asked.append(view.remaining.wall_clock_s)
        return None

    runtime = Runtime(
        budget=BudgetPolicy(
            root=Budget(wall_clock_s=100), on_exhausted=Exhaustion.ASK_HUMAN, ask_human=ask
        )
    )
    runtime._budget._clock = clock  # pyright: ignore[reportPrivateUsage]

    script = _Scripted(
        [
            [ToolCall(id='t1', name='Tick', arguments='{}')],
            [ToolCall(id='t2', name='Tick', arguments='{}')],
            None,
            None,
        ],
        clock,
    )
    try:
        agent_id = await runtime.spawn(_factory(script, {'Tick': _Tick()}), query='go')
        first = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        delivered = await runtime.send(agent_id, 'again')
        await asyncio.sleep(0.05)
        second = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()
    assert first.stop_reason is StopReason.BUDGET
    assert delivered.ok is True
    assert second.stop_reason is StopReason.BUDGET
    assert asked == [40.0]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Tick(Tool):
    __tool_name__ = 'Tick'
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        return ToolResult(output='tick')


class _Scripted:
    """每次调用把假时钟往前拨 30 秒：wall clock 的判定按上一轮耗时预测下一轮。"""

    config = LLMConfig(provider='scripted', model='m')

    def __init__(self, turns: list[list[ToolCall] | None], clock: Any) -> None:
        self._turns = list(turns)
        self._clock = clock

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        self._clock.now += 30
        calls = self._turns.pop(0) if self._turns else None
        if calls:
            return ContextBlock(role='assistant', tool_calls=calls)
        return ContextBlock(role='assistant', content='wrapped')


async def test_a_wrap_up_block_is_always_followed_by_its_wrap_up_call() -> None:
    for tokens in range(100, 700, 10):
        llm = _Reporting([ScriptedTurn(content='final')], usage=(1, 1))
        runtime = Runtime(budget=BudgetPolicy(root=Budget(tokens=tokens)))
        try:
            _, traj, reason = await _run(runtime, _factory(llm), query='x' * 400)
        finally:
            await runtime.shutdown()
        wraps = [i for i, b in enumerate(traj) if '最后一轮' in str(b.content)]
        if wraps:
            assert traj[-1].role == 'assistant' and traj[-1].content == 'final', tokens
            assert wraps[-1] < len(traj) - 1, tokens
        assert reason in (StopReason.BUDGET, StopReason.COMPLETED), tokens
