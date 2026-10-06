"""预算接线的评审处置（阶段一评审 H1–H3、M1–M7、L1–L12）：每一条都有一条单独施加即变红的用例。"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any, cast

import pytest

from syndica.kernel.agent import (  # pyright: ignore[reportPrivateUsage]
    _SERVED,
    AgentOptions,
    BaseAgent,
)
from syndica.kernel.budget import Budget, BudgetPolicy, BudgetView, Exhaustion, Prices, StaticPrices
from syndica.kernel.budget_tree import BudgetRefused
from syndica.kernel.compaction import _protected_indices  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.compaction_module import CompactionModule
from syndica.kernel.companion import run_companion
from syndica.kernel.context import ContextBlock, StopReason, ToolCall
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.history_blocks import HistoryKind, kind_of, signed_block
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import LLMConfig, ScriptedLLM, ScriptedTurn
from syndica.kernel.retry import RetryModule
from syndica.kernel.runtime import AgentFactory, ExitMode, Runtime
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.trace import AttributeValue, SpanKind, SpanStatus
from syndica.kernel.tracing import NullTracer

_TIMEOUT = 5.0


class _Echo(Tool):
    __tool_name__ = 'Echo'
    __tool_command_args__ = ()

    async def execute(self, text: str = 'x') -> ToolResult:
        return ToolResult(output='echo')


class _Spans(NullTracer):
    def __init__(self) -> None:
        self.spans: dict[str, tuple[str | None, SpanKind, str]] = {}

    def start_span(
        self,
        span_id: str,
        parent_span_id: str | None,
        kind: SpanKind,
        name: str,
        agent_id: str,
        attributes: dict[str, AttributeValue],
    ) -> None:
        self.spans[span_id] = (parent_span_id, kind, agent_id)

    def end_span(
        self, span_id: str, status: SpanStatus, attributes: dict[str, AttributeValue]
    ) -> None:
        return None


class _Usage:
    """每次都答同一段话、报同一份 usage 的 LLM；`fail` 次先抛错。"""

    def __init__(
        self,
        *,
        model: str = 'm',
        usage: tuple[int, int] = (100, 20),
        reply: str = 'done',
        tool: str | None = None,
        fail: int = 0,
        hold: asyncio.Event | None = None,
        **request_args: Any,
    ) -> None:
        self.config = LLMConfig(provider='scripted', model=model, request_args=request_args)
        self.calls = 0
        self._usage = usage
        self._reply = reply
        self._tool = tool
        self._fail = fail
        self._hold = hold

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        self.calls += 1
        if self._hold is not None:
            await self._hold.wait()
        if self._fail:
            self._fail -= 1
            raise RuntimeError('provider hiccup')
        inp, out = self._usage
        calls = (
            []
            if self._tool is None
            else [ToolCall(id=f't{self.calls}', name=self._tool, arguments='{}')]
        )
        return ContextBlock(
            role='assistant',
            content=None if calls else self._reply,
            tool_calls=calls,
            extra={'usage': {'input_tokens': inp, 'output_tokens': out}},
        )


async def _no_sleep(_: float) -> None:
    return None


def _factory(llm: Any, tools: dict[str, Tool] | None = None, name: str = 'Worker') -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name=name, llm=llm, tools=dict(tools or {}), system_prompt='SP'),
            hooks=hooks,
            events=events,
        )

    return build


async def _run(runtime: Runtime, factory: AgentFactory, query: str = 'go') -> Any:
    agent_id = await runtime.spawn(factory, query=query)
    ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    return agent_id, ctx


# ---- H1 ----


async def test_a_siblings_inflated_in_flight_estimate_does_not_stop_the_other_agent() -> None:
    hold = asyncio.Event()
    slow = _Usage(hold=hold)
    quick = _Usage()
    runtime = Runtime(budget=BudgetPolicy(root=Budget(tokens=6000)))
    try:
        first = await runtime.spawn(_factory(slow), query='go')
        await asyncio.sleep(0.01)
        second = await runtime.spawn(_factory(quick), query='go')
        done = await asyncio.wait_for(runtime.wait(second), _TIMEOUT)
        hold.set()
        other = await asyncio.wait_for(runtime.wait(first), _TIMEOUT)
    finally:
        await runtime.shutdown()
    assert (done.stop_reason, other.stop_reason) == (StopReason.COMPLETED, StopReason.COMPLETED)


# ---- H2 ----


async def test_an_unpriced_fallback_is_skipped_for_the_next_priced_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    price = Prices(input=Decimal(1), output=Decimal(1), cached_input=Decimal(1))
    primary = _Usage(model='primary', fail=1)
    unpriced = _Usage(model='mystery')
    priced = _Usage(model='known', reply='from the priced fallback')
    runtime = Runtime(
        modules=[RetryModule({'a': unpriced, 'b': priced}, sleep=_no_sleep)],
        flags={'llm_retry_attempts': 0, 'llm_fallbacks': 'a,b'},
        budget=BudgetPolicy(
            root=Budget(cost=Decimal(10)),
            prices=StaticPrices({'primary': price, 'known': price}),
        ),
    )
    try:
        with caplog.at_level(logging.ERROR):
            _, ctx = await _run(runtime, _factory(primary))
    finally:
        await runtime.shutdown()
    assert ctx.stop_reason is StopReason.COMPLETED
    assert ctx.current_traj[-1].content == 'from the priced fallback'
    assert (unpriced.calls, priced.calls) == (0, 1)
    assert any(r.args and 'mystery' in r.args for r in caplog.records)


# ---- H3 ----


async def test_summary_compaction_does_not_eat_the_planned_wrap_up_call() -> None:
    for tokens in range(1500, 9000, 250):
        llm = _Usage(usage=(150, 30), tool='Echo', max_tokens=40)
        runtime = Runtime(
            modules=[CompactionModule()],
            flags={
                'compaction_strategy': 'summary',
                'compaction_context_tokens': 300,
                'compaction_threshold_percent': 50,
                'compaction_target_percent': 20,
            },
            budget=BudgetPolicy(root=Budget(tokens=tokens)),
        )
        try:
            _, ctx = await _run(runtime, _factory(llm, {'Echo': _Echo()}), query='x' * 200)
        finally:
            await runtime.shutdown()
        traj = ctx.current_traj
        wraps = [i for i, b in enumerate(traj) if '最后一轮' in str(b.content)]
        if wraps:
            assert any(b.role == 'assistant' for b in traj[wraps[-1] :]), tokens


# ---- M7 ----


async def test_a_completion_hook_companion_is_charged_to_the_agent_and_nested_in_its_trace() -> (
    None
):
    judge = _Usage(model='m', usage=(300, 50), reply='{"ok": true}')
    tracer = _Spans()

    async def judging(**kwargs: Any) -> HookResult:
        await run_companion(name='Judge', llm=cast(Any, judge), system_prompt='j', task='t')
        return HookResult()

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = BaseAgent(
            AgentOptions(name='Main', llm=cast(Any, _Usage(usage=(10, 5))), system_prompt='SP'),
            hooks=hooks,
            events=events,
        )
        agent.register_hook(Junction.COMPLETION_PROPOSED, judging)
        return agent

    runtime = Runtime(budget=BudgetPolicy(root=Budget(tokens=10_000, turns=5)))
    runtime._set_tracer(tracer)  # pyright: ignore[reportPrivateUsage]
    try:
        agent_id, ctx = await _run(runtime, build)
        view = runtime._budget_of(agent_id)  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()
    assert ctx.stop_reason is StopReason.COMPLETED
    assert view is not None and view.used.tokens == 15 + 350 and view.used.turns == 1
    [(parent, _, companion)] = [
        v for v in tracer.spans.values() if v[1] is SpanKind.AGENT and v[2] != agent_id
    ]
    assert companion == f'{agent_id}.Judge.1'
    assert parent is not None and tracer.spans[parent][2] == agent_id


async def test_a_failed_call_gives_its_estimate_back() -> None:
    llm = _Usage(fail=1, usage=(10, 5), max_tokens=100)
    runtime = Runtime(
        modules=[RetryModule({}, sleep=_no_sleep)],
        flags={'llm_retry_attempts': 1},
        budget=BudgetPolicy(root=Budget(tokens=10_000)),
    )
    try:
        agent_id, ctx = await _run(runtime, _factory(llm))
        view = runtime._budget_of(agent_id)  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()
    assert ctx.stop_reason is StopReason.COMPLETED
    assert view is not None and view.remaining.tokens == 10_000 - 15


async def test_a_killed_call_keeps_its_input_estimate_and_gives_the_rest_back() -> None:
    hold = asyncio.Event()
    llm = _Usage(hold=hold, max_tokens=100)
    runtime = Runtime(budget=BudgetPolicy(root=Budget(tokens=10_000)))
    try:
        agent_id = await runtime.spawn(_factory(llm), query='go')
        await asyncio.sleep(0.02)
        await runtime.kill(agent_id)
        await asyncio.sleep(0.02)
        view = runtime._budget.pool().view()  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()
    used = view.used
    assert used.input_tokens > 0 and used.output_tokens == 0 and used.estimated
    assert view.remaining.tokens == 10_000 - used.input_tokens


async def test_every_timed_out_companion_call_is_charged() -> None:
    from syndica.kernel.budget_tree import BudgetTree
    from syndica.kernel.tracing import _Tracing, llm_attempt  # pyright: ignore[reportPrivateUsage]

    class _Slow:
        config = LLMConfig(provider='scripted', model='paid')

        async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
            await asyncio.sleep(10)
            return ContextBlock(role='assistant', content='late')

    prices = StaticPrices({'paid': Prices(Decimal('1'), Decimal('1'), Decimal('0'))})
    tree = BudgetTree(BudgetPolicy(root=Budget(tokens=100_000, cost=Decimal('1')), prices=prices))
    account = tree.carve('a', None, Budget(tokens=100_000, cost=Decimal('1')))
    llm = _Slow()
    messages = [ContextBlock(role='user', content='x ' * 2000)]
    tracing = _Tracing()
    for turn in range(3):
        with tracing.llm_call('a', turn, account.for_companion()):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.01):
                    async with llm_attempt(llm, {'messages': messages}):
                        await llm.create(messages)
    used = account.view().used
    each = used.input_tokens // 3
    assert each > 0 and used.input_tokens == 3 * each
    assert used.cost is not None and used.cost > 0


# ---- 收尾轮 ----


async def test_a_cost_cap_also_triggers_the_wrap_up() -> None:
    price = Prices(input=Decimal(1000), output=Decimal(1000), cached_input=Decimal(1000))
    llm = _Usage(usage=(100, 10), tool='Echo', max_tokens=10)
    runtime = Runtime(
        budget=BudgetPolicy(root=Budget(cost=Decimal('0.5')), prices=StaticPrices({'m': price}))
    )
    try:
        _, ctx = await _run(runtime, _factory(llm, {'Echo': _Echo()}))
    finally:
        await runtime.shutdown()
    wraps = [str(b.content) for b in ctx.current_traj if '最后一轮' in str(b.content)]
    assert ctx.stop_reason is StopReason.BUDGET
    assert len(wraps) == 1 and 'cost' in wraps[0]


async def test_a_send_back_does_not_buy_another_turn_after_the_wrap_up() -> None:
    from syndica.kernel.acceptance import Verdict, VerdictStatus

    llm = ScriptedLLM([ScriptedTurn(content='wrapped'), ScriptedTurn(content='never')])

    async def sends_back(**kwargs: Any) -> HookResult:
        verdict = Verdict(status=VerdictStatus.PARTIAL, outcomes=(), needs_human=())
        return HookResult(modified={'verdict': verdict, 'send_back': True})

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = BaseAgent(
            AgentOptions(name='W', llm=llm, system_prompt='SP'), hooks=hooks, events=events
        )
        agent.register_hook(Junction.COMPLETION_PROPOSED, sends_back)
        return agent

    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=1)))
    try:
        _, ctx = await _run(runtime, build)
    finally:
        await runtime.shutdown()
    assert ctx.stop_reason is StopReason.BUDGET
    assert len(llm.calls) == 1
    assert not any(kind_of(b) is HistoryKind.VERDICT for b in ctx.current_traj)
    assert ctx.verdict is not None and ctx.verdict.status is VerdictStatus.PARTIAL


async def test_a_wrap_up_reply_with_text_and_tool_calls_is_still_judged_and_handed_back() -> None:
    from syndica.kernel.acceptance import Verdict, VerdictStatus

    child = ScriptedLLM(
        [
            ScriptedTurn(
                content='here is what I have',
                tool_calls=[ToolCall(id='e', name='Echo', arguments='{}')],
            )
        ]
    )
    parent = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='d',
                        name='Delegate',
                        arguments='{"agent": "Child", "query": "sub", "turns": 1}',
                    )
                ]
            ),
            ScriptedTurn(content='parent done'),
        ]
    )
    judged: list[str] = []

    async def judge(**kwargs: Any) -> HookResult:
        judged.append(str(kwargs['response'].content))
        return HookResult(
            modified={'verdict': Verdict(status=VerdictStatus.PARTIAL, outcomes=(), needs_human=())}
        )

    def build_child(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = BaseAgent(
            AgentOptions(name='Child', llm=child, tools={'Echo': _Echo()}, system_prompt='SP'),
            hooks=hooks,
            events=events,
        )
        agent.register_hook(Junction.COMPLETION_PROPOSED, judge)
        return agent

    runtime = Runtime(
        orchestration=True,
        agents={'Child': build_child},
        budget=BudgetPolicy(root=Budget(turns=10)),
    )
    try:
        _, ctx = await _run(runtime, _factory(parent, name='Parent'))
    finally:
        await runtime.shutdown()
    answer = str(next(b for b in ctx.current_traj if b.tool_call_id == 'd').content)
    assert judged == ['here is what I have']
    assert 'here is what I have' in answer and 'partial' in answer


async def test_a_wrap_up_with_only_tool_calls_hands_back_no_stale_text() -> None:
    child = ScriptedLLM(
        [
            ScriptedTurn(
                content='an old remark', tool_calls=[ToolCall(id='e1', name='Echo', arguments='{}')]
            ),
            ScriptedTurn(tool_calls=[ToolCall(id='e2', name='Echo', arguments='{}')]),
        ]
    )
    parent = ScriptedLLM(
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
        agents={'Child': _factory(child, {'Echo': _Echo()}, name='Child')},
        budget=BudgetPolicy(root=Budget(turns=10)),
    )
    try:
        _, ctx = await _run(runtime, _factory(parent, name='Parent'))
    finally:
        await runtime.shutdown()
    answer = str(next(b for b in ctx.current_traj if b.tool_call_id == 'd').content)
    assert 'an old remark' not in answer and '用完' in answer


async def test_only_one_your_budget_block_is_injected_per_agent() -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id=f'e{i}', name='Echo', arguments='{}')])
            for i in range(3)
        ]
        + [ScriptedTurn(content='done')]
    )
    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=50)))
    try:
        _, ctx = await _run(runtime, _factory(llm, {'Echo': _Echo()}))
    finally:
        await runtime.shutdown()
    starts = [b for b in ctx.current_traj if str(b.content).startswith('你的预算')]
    assert len(starts) == 1


def test_the_latest_budget_block_survives_compaction() -> None:
    blocks = [
        ContextBlock(role='user', content='go'),
        signed_block(HistoryKind.BUDGET, 'old', key='budget:a:start'),
        ContextBlock(role='assistant', content='x'),
        signed_block(HistoryKind.BUDGET, 'new', key='budget:a:0:50'),
    ]
    protected = _protected_indices(blocks)
    assert 3 in protected and 1 not in protected


# ---- ask_human ----


async def test_a_declined_ask_replans_before_using_the_pre_ask_decision() -> None:
    runtime_ref: dict[str, Runtime] = {}
    llm = ScriptedLLM([ScriptedTurn(content='never')])

    async def ask(agent_id: str, view: BudgetView) -> Budget | None:
        runtime_ref['r']._budget.pool().take_turn()  # pyright: ignore[reportPrivateUsage]
        return None

    runtime = Runtime(
        budget=BudgetPolicy(root=Budget(turns=1), on_exhausted=Exhaustion.ASK_HUMAN, ask_human=ask)
    )
    runtime_ref['r'] = runtime
    try:
        _, ctx = await _run(runtime, _factory(llm))
    finally:
        await runtime.shutdown()
    assert ctx.stop_reason is StopReason.BUDGET
    assert llm.calls == []


# ---- Runtime 的接线 ----


async def test_a_carve_refused_after_registration_rolls_the_spawn_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=5)))

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise BudgetRefused(expected='e', received='r', next_action='n')

    monkeypatch.setattr(runtime._budget, 'carve', refuse)  # pyright: ignore[reportPrivateUsage]
    try:
        with pytest.raises(BudgetRefused):
            await runtime.spawn(_factory(ScriptedLLM([])), query='go')
        listed = await runtime.registry.list()
    finally:
        await runtime.shutdown()
    assert listed == []


async def test_despawning_a_child_closes_its_node_and_keeps_its_spend() -> None:
    llm = ScriptedLLM([ScriptedTurn(content='child done')])
    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=10)))
    try:
        parent = await runtime.spawn(_factory(ScriptedLLM([])))
        child = await runtime.spawn(_factory(llm), query='go', parent_id=parent)
        await asyncio.wait_for(runtime.wait(child), _TIMEOUT)
        await runtime.despawn(child)
        gone = runtime._budget.account(child)  # pyright: ignore[reportPrivateUsage]
        view = runtime._budget_of(parent)  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()
    assert gone is None
    assert view is not None and view.used.turns == 1


@pytest.mark.parametrize('value', ['-1', 'true', '1.5'])
async def test_delegate_refuses_a_turns_value_that_is_not_a_non_negative_integer(
    value: str,
) -> None:
    parent = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='d',
                        name='Delegate',
                        arguments=f'{{"agent": "Child", "query": "sub", "turns": {value}}}',
                    )
                ]
            ),
            ScriptedTurn(content='parent done'),
        ]
    )
    runtime = Runtime(orchestration=True, agents={'Child': _factory(ScriptedLLM([]), name='Child')})
    try:
        _, ctx = await _run(runtime, _factory(parent, name='Parent'))
        listed = await runtime.registry.list()
    finally:
        await runtime.shutdown()
    answer = str(next(b for b in ctx.current_traj if b.tool_call_id == 'd').content)
    assert '剩余预算的一半' in answer and len(listed) == 1


async def test_fork_passes_its_turns_to_the_child() -> None:
    seen: list[BudgetView | None] = []
    parent = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[ToolCall(id='f', name='Fork', arguments='{"query": "sub", "turns": 2}')]
            ),
            ScriptedTurn(content='child done'),
            ScriptedTurn(content='parent done'),
        ]
    )
    runtime = Runtime(orchestration=True, budget=BudgetPolicy(root=Budget(turns=20)))
    original = runtime.spawn

    async def spawn(*args: Any, **kwargs: Any) -> str:
        agent_id = await original(*args, **kwargs)
        seen.append(runtime._budget_of(agent_id))  # pyright: ignore[reportPrivateUsage]
        return agent_id

    runtime.spawn = spawn  # type: ignore[method-assign]
    try:
        root = await original(_factory(parent, name='Parent'), query='go')
        await asyncio.wait_for(runtime.wait(root), _TIMEOUT)
    finally:
        await runtime.shutdown()
    assert seen[0] is not None and seen[0].limit.turns == 2


async def test_a_fork_of_an_unbudgeted_agent_gets_no_get_budget() -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[ToolCall(id='f', name='Fork', arguments='{"query": "sub", "turns": 2}')]
            ),
            ScriptedTurn(content='child done'),
            ScriptedTurn(content='parent done'),
        ]
    )
    runtime = Runtime(orchestration=True)
    try:
        await _run(runtime, _factory(llm, name='Parent'))
    finally:
        await runtime.shutdown()
    names = [[schema['name'] for schema in kwargs['tools']] for kwargs in llm.call_kwargs]
    assert names[0] == names[1]
    assert 'GetBudget' not in names[1]


async def test_a_child_torn_down_on_exit_does_not_see_its_creator_as_the_served_agent() -> None:
    seen: list[object] = []
    holder: dict[str, Runtime] = {}

    class _SpawnExiting(Tool):
        __tool_name__ = 'Spawn'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            runtime = holder['r']
            await runtime.spawn(
                _factory(ScriptedLLM([ScriptedTurn(content='bye')])),
                query='go',
                exit_mode=ExitMode.EXIT,
            )
            return ToolResult(output='spawned')

    async def on_despawn(**kwargs: Any) -> HookResult:
        seen.append(_SERVED.get())
        return HookResult()

    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='s', name='Spawn', arguments='{}')]),
            ScriptedTurn(content='parent done'),
        ]
    )
    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=20)))
    holder['r'] = runtime
    runtime.hooks.register(Junction.AGENT_DESPAWN, on_despawn)
    try:
        await _run(runtime, _factory(llm, {'Spawn': _SpawnExiting()}, name='Parent'))
        for _ in range(20):
            if seen:
                break
            await asyncio.sleep(0.01)
    finally:
        await runtime.shutdown()
    assert seen and seen[0] is None


# ---- 错误三件套的锚词 ----


def test_a_runtime_budget_that_is_not_a_policy_is_refused() -> None:
    with pytest.raises(ConfigError) as caught:
        Runtime(budget=cast(Any, Budget(turns=3)))
    assert 'BudgetPolicy(root=' in caught.value.next_action


async def test_a_spawn_budget_that_is_not_a_budget_is_refused() -> None:
    runtime = Runtime()
    try:
        with pytest.raises(ConfigError) as caught:
            await runtime.spawn(_factory(ScriptedLLM([])), budget=cast(Any, {'turns': 3}))
    finally:
        await runtime.shutdown()
    assert 'Budget(turns=' in caught.value.next_action


async def test_granting_to_an_unknown_agent_is_refused_saying_how_to_check() -> None:
    runtime = Runtime()
    try:
        with pytest.raises(ConfigError) as caught:
            runtime._grant_budget('nobody', Budget(turns=1))  # pyright: ignore[reportPrivateUsage]
        agent_id = await runtime.spawn(_factory(ScriptedLLM([])))
        with pytest.raises(ConfigError) as wrong:
            runtime._grant_budget(agent_id, cast(Any, 3))  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()
    assert 'find()' in caught.value.next_action
    assert 'Budget(turns=' in wrong.value.next_action


# ---- 会话与产物 ----


async def test_a_resumed_session_starts_with_a_fresh_budget(tmp_path: Any) -> None:
    from syndica.kernel.redaction import RuleRedactor
    from syndica.kernel.stores.file_session import FileSessionStore

    store = FileSessionStore(tmp_path / 'sessions')
    first = Runtime(
        sessions=store,
        redactor=RuleRedactor(),
        budget=BudgetPolicy(root=Budget(turns=10)),
        agents={'Worker': _factory(ScriptedLLM([ScriptedTurn(content='one')]))},
    )
    try:
        factory = first.agent_factory('Worker')
        assert factory is not None
        agent_id = await first.spawn(factory, query='go', session=True)
        await asyncio.wait_for(first.wait(agent_id), _TIMEOUT)
        assert first._budget_of(agent_id).used.turns == 1  # type: ignore[union-attr]  # pyright: ignore[reportPrivateUsage]
    finally:
        await first.shutdown()
    second = Runtime(
        sessions=store,
        redactor=RuleRedactor(),
        budget=BudgetPolicy(root=Budget(turns=10)),
        agents={'Worker': _factory(ScriptedLLM([ScriptedTurn(content='two')]))},
    )
    try:
        assert await second.ensure_active(agent_id)
        view = second._budget.pool().view()  # pyright: ignore[reportPrivateUsage]
    finally:
        await second.shutdown()
    assert view.used.turns == 0


@pytest.mark.parametrize('disposition', [Exhaustion.HARD_STOP, Exhaustion.GRACEFUL_WRAPUP])
async def test_running_out_of_budget_keeps_what_was_written(
    tmp_path: Any, disposition: Exhaustion
) -> None:
    from syndica.kernel.local_workspace import LocalWorkspace
    from syndica.kernel.tools.local import Write
    from syndica.kernel.workspace_router import WorkspaceRouter

    root = tmp_path / 'ws'
    root.mkdir()
    router = WorkspaceRouter(LocalWorkspace(str(root), shell='/bin/sh', env={}))
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='w',
                        name='Write',
                        arguments=json.dumps({'path': 'report.md', 'content': '# done so far\n'}),
                    )
                ]
            ),
            ScriptedTurn(tool_calls=[ToolCall(id='w2', name='Write', arguments='{}')]),
        ]
    )
    runtime = Runtime(
        workspace=router,
        budget=BudgetPolicy(root=Budget(turns=2), on_exhausted=disposition),
    )
    try:
        _, ctx = await _run(runtime, _factory(llm, {'Write': Write(router.portal())}))
    finally:
        await runtime.shutdown()
    assert ctx.stop_reason is StopReason.BUDGET
    assert (root / 'report.md').read_text(encoding='utf-8') == '# done so far\n'


# ---- 验收与重试里的预算 ----


async def _judged_run(judge: Any, budget: BudgetPolicy, main: Any) -> Any:
    from syndica.kernel.acceptance import Anchor, AnchorSource, Goal, Rubric, RubricKind
    from syndica.kernel.redaction import RuleRedactor
    from syndica.kernel.rubric_redaction import redacted_goal, redacted_rubric
    from syndica.kernel.stores.memory_rubric import InMemoryRubricStore
    from syndica.kernel.verifiers import acceptance_hook

    anchors = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'add section four'),)
    rubric = Rubric(id='R1', statement='has section 4', kind=RubricKind.CHECKABLE, anchors=anchors)
    store = InMemoryRubricStore()
    await store.put_goal('s1', redacted_goal(Goal(intent='write'), RuleRedactor()))
    await store.put_rubrics('s1', [redacted_rubric(rubric, RuleRedactor())])
    hook = acceptance_hook(
        store=store,
        scope='s1',
        llm=judge,
        portal=None,
        output=False,
        outcome=True,
        output_retries=0,
        partial_retries=0,
        block_retries=0,
        max_turns=4,
        timeout=5.0,
    )

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = BaseAgent(
            AgentOptions(name='Main', llm=main, system_prompt='SP'), hooks=hooks, events=events
        )
        agent.register_hook(Junction.COMPLETION_PROPOSED, hook)
        return agent

    runtime = Runtime(budget=budget)
    try:
        _, ctx = await _run(runtime, build)
    finally:
        await runtime.shutdown()
    return ctx


async def test_a_judge_with_no_budget_left_marks_the_verdict_budget_exhausted() -> None:
    judge = _Usage(reply='{"outcomes": [], "reason": "r"}')
    ctx = await _judged_run(
        judge, BudgetPolicy(root=Budget(tokens=3000)), _Usage(usage=(2900, 20), reply='report')
    )
    assert judge.calls == 0
    assert ctx.verdict is not None and ctx.verdict.unverified_cause == 'budget_exhausted'


async def test_an_unpriced_judge_under_a_cost_cap_marks_the_verdict_price_unknown() -> None:
    price = Prices(input=Decimal(1), output=Decimal(1), cached_input=Decimal(1))
    judge = _Usage(model='mystery', reply='{"outcomes": [], "reason": "r"}')
    ctx = await _judged_run(
        judge,
        BudgetPolicy(root=Budget(cost=Decimal(10)), prices=StaticPrices({'m': price})),
        _Usage(reply='report'),
    )
    assert judge.calls == 0
    assert ctx.verdict is not None and ctx.verdict.unverified_cause == 'price_unknown'


async def test_running_out_of_budget_inside_a_retry_stops_for_budget_not_error() -> None:
    cheap = Prices(input=Decimal(1), output=Decimal(1), cached_input=Decimal(1))
    dear = Prices(input=Decimal(10**9), output=Decimal(10**9), cached_input=Decimal(10**9))
    fallback = _Usage(model='dear')
    runtime = Runtime(
        modules=[RetryModule({'dear': fallback}, sleep=_no_sleep)],
        flags={'llm_retry_attempts': 0, 'llm_fallbacks': 'dear'},
        budget=BudgetPolicy(
            root=Budget(cost=Decimal(1)), prices=StaticPrices({'m': cheap, 'dear': dear})
        ),
    )
    try:
        _, ctx = await _run(runtime, _factory(_Usage(fail=1)))
    finally:
        await runtime.shutdown()
    assert fallback.calls == 0
    assert ctx.stop_reason is StopReason.BUDGET


async def test_an_isolation_failure_hands_the_reservation_back(tmp_path: Any) -> None:
    from syndica.kernel.isolation import LocalIsolator
    from syndica.kernel.local_workspace import LocalWorkspace
    from syndica.kernel.workspace_router import WorkspaceRouter

    root = tmp_path / 'ws'
    root.mkdir()
    router = WorkspaceRouter(
        LocalWorkspace(str(root), shell='/bin/sh', env={}),
        isolator=LocalIsolator(shell='/bin/sh', env={}),
    )

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError('copy failed')

    router.isolate = broken  # type: ignore[method-assign]
    runtime = Runtime(workspace=router, budget=BudgetPolicy(root=Budget(turns=10)))
    try:
        with pytest.raises(OSError):
            await runtime.spawn(
                _factory(ScriptedLLM([])), query='go', budget=Budget(turns=4), isolation='snapshot'
            )
        view = runtime._budget.pool().view()  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()
    assert view.remaining.turns == 10


async def test_a_child_reaped_after_a_kill_does_not_see_its_killer_as_the_served_agent() -> None:
    seen: list[object] = []
    holder: dict[str, Runtime] = {}

    class _SpawnThenKill(Tool):
        __tool_name__ = 'SpawnThenKill'
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            runtime = holder['r']
            child = await runtime.spawn(
                _factory(_Usage(hold=asyncio.Event())), query='go', exit_mode=ExitMode.EXIT
            )
            await asyncio.sleep(0.01)
            await runtime.kill(child)
            return ToolResult(output='killed')

    async def on_despawn(**kwargs: Any) -> HookResult:
        seen.append(_SERVED.get())
        return HookResult()

    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='k', name='SpawnThenKill', arguments='{}')]),
            ScriptedTurn(content='parent done'),
        ]
    )
    runtime = Runtime(budget=BudgetPolicy(root=Budget(turns=20)))
    holder['r'] = runtime
    runtime.hooks.register(Junction.AGENT_DESPAWN, on_despawn)
    try:
        await _run(runtime, _factory(llm, {'SpawnThenKill': _SpawnThenKill()}, name='Parent'))
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.01)
    finally:
        await runtime.shutdown()
    assert seen and seen[0] is None


async def test_a_deadline_passed_during_a_failed_call_stops_the_retry_for_budget() -> None:
    now = [0.0]

    class _SlowFailure(_Usage):
        async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
            if self.calls == 0:
                self.calls += 1
                now[0] += 1000.0
                raise RuntimeError('provider hiccup')
            return await super().create(messages, **kwargs)

    llm = _SlowFailure()
    runtime = Runtime(
        modules=[RetryModule({}, sleep=_no_sleep)],
        flags={'llm_retry_attempts': 1},
        budget=BudgetPolicy(root=Budget(wall_clock_s=100.0)),
    )
    runtime._budget._clock = lambda: now[0]  # pyright: ignore[reportPrivateUsage]
    try:
        _, ctx = await _run(runtime, _factory(llm))
    finally:
        await runtime.shutdown()
    assert llm.calls == 1
    assert ctx.stop_reason is StopReason.BUDGET
