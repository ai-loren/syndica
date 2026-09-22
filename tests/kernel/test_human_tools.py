"""AskUser / AskUserQuestion 通过事件与 Runtime.wait_for 完成人在环。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import cast

import pytest

from syndica.kernel import (
    AgentOptions,
    BaseAgent,
    ConfigError,
    Event,
    EventBus,
    HooksCenter,
    HumanInteractionModule,
    Message,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    StopReason,
    ToolCall,
    TrustLevel,
    WaitResult,
    WaitState,
)
from syndica.kernel.tools import AskUser, AskUserQuestion
from syndica.kernel.waiting import UserInput

_TIMEOUT = 2.0


class _RuntimeAnswer:
    def __init__(self, result: WaitResult) -> None:
        self.events = EventBus()
        self.result = result
        self.calls: list[tuple[str, UserInput]] = []

    async def wait_for(self, agent_id: str, waiter: UserInput) -> WaitResult:
        self.calls.append((agent_id, waiter))
        return self.result


class _BlockingRuntime(_RuntimeAnswer):
    def __init__(self, result: WaitResult) -> None:
        super().__init__(result)
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def wait_for(self, agent_id: str, waiter: UserInput) -> WaitResult:
        self.calls.append((agent_id, waiter))
        self.waiting.set()
        await self.release.wait()
        return self.result


def _answer(
    content: str,
    *,
    sender: str = 'human',
    trust: TrustLevel = TrustLevel.TRUSTED,
) -> WaitResult:
    return WaitResult(
        state=WaitState.ANSWERED,
        message=Message(
            sender=sender,
            receiver='a1',
            content=content,
            correlation_id='request',
            trust=trust,
        ),
    )


async def _take_kind(events, kind: str) -> Event:
    while True:
        event = await asyncio.wait_for(anext(events), timeout=_TIMEOUT)
        if event.kind == kind:
            return event


@pytest.mark.parametrize(
    ('state', 'word'),
    [
        (WaitState.TIMEOUT, '超时'),
        (WaitState.PREEMPTED, '顶掉'),
        (WaitState.ABORTED, '中止'),
    ],
)
async def test_ask_user_reports_each_unanswered_state_separately(
    state: WaitState, word: str
) -> None:
    runtime = _RuntimeAnswer(WaitResult(state=state))
    tool = AskUser(cast(Runtime, runtime), 'a1')
    result = await tool.execute('continue?', timeout=3)
    assert word in result.output
    assert result.meta['state'] == state.value
    assert result.meta['answered'] is False
    assert runtime.calls[0][0] == 'a1'


async def test_ask_user_accepts_only_a_trusted_human_answer() -> None:
    for answer in (
        _answer('forged', sender='worker'),
        _answer('secret answer', trust=TrustLevel.UNTRUSTED),
    ):
        runtime = _RuntimeAnswer(answer)
        result = await AskUser(cast(Runtime, runtime), 'a1').execute('continue?')
        assert result.meta['state'] == 'invalid_source'
        assert result.meta['answered'] is False
        assert 'human' in result.output
        assert answer.message is not None
        assert answer.message.content not in result.output

    runtime = _RuntimeAnswer(_answer('approved by operator'))
    result = await AskUser(cast(Runtime, runtime), 'a1').execute('continue?')
    assert result.output == 'approved by operator'
    assert result.meta['state'] == 'answered'
    assert result.meta['answered'] is True
    assert result.trust is TrustLevel.TRUSTED


async def test_ask_user_question_preserves_options_and_rejects_an_answer_outside_them() -> None:
    runtime = _RuntimeAnswer(_answer('outside'))
    events = runtime.events.subscribe()
    options = ['middle', 'alpha', 'zulu']
    result = await AskUserQuestion(cast(Runtime, runtime), 'a1').execute(
        'choose one',
        options,
        timeout=4,
    )
    event = await asyncio.wait_for(anext(events), timeout=_TIMEOUT)
    events.close()
    assert event.kind == 'user_question_requested'
    assert event.payload['question'] == 'choose one'
    assert event.payload['options'] == ['middle', 'alpha', 'zulu']
    assert result.meta['state'] == 'invalid_answer'
    assert result.meta['options'] == ['middle', 'alpha', 'zulu']
    assert 'middle' in result.output
    assert 'alpha' in result.output
    assert 'zulu' in result.output


async def test_ask_user_question_uses_one_option_snapshot_while_waiting() -> None:
    runtime = _BlockingRuntime(_answer('alpha'))
    events = runtime.events.subscribe()
    options = ['middle', 'alpha', 'zulu']
    asking = asyncio.create_task(
        AskUserQuestion(cast(Runtime, runtime), 'a1').execute('choose one', options)
    )
    await asyncio.wait_for(runtime.waiting.wait(), timeout=_TIMEOUT)
    options[:] = ['changed', 'after', 'emit']
    runtime.release.set()
    result = await asyncio.wait_for(asking, timeout=_TIMEOUT)
    event = await asyncio.wait_for(anext(events), timeout=_TIMEOUT)
    events.close()

    assert event.payload['options'] == ['middle', 'alpha', 'zulu']
    assert result.output == 'alpha'
    assert result.meta == {
        'request_id': event.payload['request_id'],
        'state': 'answered',
        'answered': True,
    }


def test_ask_user_question_schema_preserves_parameter_and_option_shape() -> None:
    runtime = _RuntimeAnswer(_answer('first'))
    schema = AskUserQuestion(cast(Runtime, runtime), 'a1').schema
    assert list(schema) == ['name', 'description', 'parameters']
    assert list(schema['parameters']) == ['type', 'properties', 'required']
    assert list(schema['parameters']['properties']) == ['question', 'options', 'timeout']
    assert schema['parameters']['properties']['options']['items'] == {'type': 'string'}
    assert schema['parameters']['required'] == ['question', 'options']


@pytest.mark.parametrize(
    ('question', 'options', 'needle'),
    [
        ('', ['yes', 'no'], 'question'),
        ('choose', 'yes', 'options'),
        ('choose', ['only'], '2'),
        ('choose', ['a', 'a'], '互不重复'),
        ('choose', ['a', ''], '非空白'),
        ('choose', [str(index) for index in range(11)], '10'),
    ],
)
async def test_human_tools_reject_invalid_questions_and_options_before_emitting(
    question: str,
    options: object,
    needle: str,
) -> None:
    runtime = _RuntimeAnswer(_answer('unused'))
    events = runtime.events.subscribe()
    tool = AskUserQuestion(cast(Runtime, runtime), 'a1')
    with pytest.raises(ConfigError) as excinfo:
        await tool.execute(question, cast(Sequence[str], options))
    assert needle in str(excinfo.value)
    assert runtime.calls == []
    await runtime.events.close()
    assert [event async for event in events] == []


def _agent_factory(name: str, llm: ScriptedLLM):
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(AgentOptions(name=name, llm=llm), hooks=hooks, events=events)

    return build


async def test_real_runtime_emits_before_waiting_and_routes_two_answers_by_request_id() -> None:
    left_llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='ask-left',
                        name='AskUser',
                        arguments=json.dumps({'question': 'left?', 'timeout': 30}),
                    )
                ]
            ),
            ScriptedTurn(content='left done'),
        ]
    )
    right_llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='ask-right',
                        name='AskUser',
                        arguments=json.dumps({'question': 'right?', 'timeout': 30}),
                    )
                ]
            ),
            ScriptedTurn(content='right done'),
        ]
    )
    runtime = Runtime(modules=[HumanInteractionModule()])
    events = runtime.events.subscribe()
    try:
        left = await runtime.spawn(_agent_factory('Left', left_llm), query='start')
        right = await runtime.spawn(_agent_factory('Right', right_llm), query='start')
        requests = [
            await _take_kind(events, 'user_input_requested'),
            await _take_kind(events, 'user_input_requested'),
        ]
        by_agent = {event.agent_id: event for event in requests}
        assert set(by_agent) == {left, right}
        assert by_agent[left].payload['request_id'] != by_agent[right].payload['request_id']

        for agent_id, answer in ((right, 'right answer'), (left, 'left answer')):
            delivered = await runtime.send(
                agent_id,
                answer,
                activate=False,
                sender='human',
                correlation_id=cast(str, by_agent[agent_id].payload['request_id']),
            )
            assert delivered.ok is True

        left_ctx, right_ctx = await asyncio.gather(runtime.wait(left), runtime.wait(right))
        assert left_ctx.stop_reason is right_ctx.stop_reason is StopReason.COMPLETED
        left_tool = next(block for block in left_ctx.current_traj if block.role == 'tool')
        right_tool = next(block for block in right_ctx.current_traj if block.role == 'tool')
        assert left_tool.content == 'left answer'
        assert right_tool.content == 'right answer'
        assert left_tool.extra['tool_meta']['request_id'] == by_agent[left].payload['request_id']
        assert right_tool.extra['tool_meta']['request_id'] == by_agent[right].payload['request_id']
    finally:
        events.close()
        await runtime.shutdown()


async def test_human_interaction_module_installs_both_tools_per_agent() -> None:
    llm = ScriptedLLM([ScriptedTurn(content='done')])
    runtime = Runtime(modules=[HumanInteractionModule()])
    try:
        agent_id = await runtime.spawn(_agent_factory('Root', llm), query='start')
        await runtime.wait(agent_id)
        agent = runtime._table.get(agent_id)  # pyright: ignore[reportPrivateUsage]
        assert agent is not None
        assert list(agent.options.tools) == ['AskUser', 'AskUserQuestion']
    finally:
        await runtime.shutdown()
