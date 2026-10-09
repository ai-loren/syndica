"""ACP 桥：回答、结束与失败只认根；工具、确认与提问来自谁都推给编辑器，答复交回发起者。

推给编辑器的每个出口先脱敏、再截断。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from acp.exceptions import RequestError

from syndica.kernel import (
    ContextBlock,
    DeliveryResult,
    Event,
    EventBus,
    RuleRedactor,
    TrustLevel,
)
from syndica.server.acp_agent import AcpBridge, TurnBinding

_SECRET = 'quizSECRETvalue-918273'
_ROOT = 'root-agent'
_CHILD = 'child-agent'
_TIMEOUT = 5


class _Runtime:
    def __init__(self, traj: list[ContextBlock] | None = None) -> None:
        self.sent: list[tuple[str, str, dict[str, object]]] = []
        self.traj = traj or []

    async def send(self, target: str, text: str, **options: object) -> DeliveryResult:
        self.sent.append((target, text, options))
        return DeliveryResult(ok=True)

    async def wait(self, agent_id: str) -> Any:
        del agent_id
        return SimpleNamespace(current_traj=self.traj)

    async def abort(self, agent_id: str) -> None:
        del agent_id

    async def find(self, agent_id: str) -> None:
        del agent_id


class _Emit:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.users: list[str] = []
        self.tools: list[tuple[str, str, str, str]] = []
        self.permissions: list[tuple[str, str, str]] = []

    async def message(self, text: str) -> None:
        self.messages.append(text)

    async def user_message(self, text: str) -> None:
        self.users.append(text)

    async def tool(
        self, tool_call_id: str, name: str, status: str, raw_input: str, raw_output: str
    ) -> None:
        del raw_output
        self.tools.append((tool_call_id, name, status, raw_input))

    async def permission(self, tool_call_id: str, name: str, arguments: str) -> str:
        self.permissions.append((tool_call_id, name, arguments))
        return 'approve'


@asynccontextmanager
async def _bridge(
    runtime: _Runtime | None = None,
) -> AsyncIterator[tuple[AcpBridge, EventBus, _Runtime, _Emit]]:
    bus = EventBus()
    active = runtime if runtime is not None else _Runtime()
    emit = _Emit()
    bridge = AcpBridge(
        TurnBinding(active, _ROOT, bus.subscribe()),  # pyright: ignore[reportArgumentType]
        emit,
        redactor=RuleRedactor(secrets=[_SECRET]),
    )
    try:
        yield bridge, bus, active, emit
    finally:
        await bridge.close()
        await bus.close()


async def _submit(bridge: AcpBridge, runtime: _Runtime, text: str) -> asyncio.Task[str]:
    """提示已经投递出去才返回：此后发的事件都落在这次提示里。"""
    count = len(runtime.sent)
    task = asyncio.create_task(bridge.submit(text))
    async with asyncio.timeout(_TIMEOUT):
        while len(runtime.sent) == count:  # noqa: ASYNC110
            await asyncio.sleep(0)
    return task


async def _drain(bus: EventBus, emit: _Emit, mark: str) -> None:
    """桥按序处理事件：标记被处理时，它之前的事件都已经处理过。"""
    await bus.emit(Event('tool_call_finished', _CHILD, {'tool_call_id': mark, 'outcome': 'ok'}))
    async with asyncio.timeout(_TIMEOUT):
        while not any(call[0] == mark for call in emit.tools):  # noqa: ASYNC110
            await asyncio.sleep(0)
    for _ in range(10):
        await asyncio.sleep(0)


def _said(agent_id: str, text: str) -> Event:
    return Event('message_appended', agent_id, {'block': {'role': 'assistant', 'text': text}})


def _ended(agent_id: str) -> Event:
    return Event('agent_end', agent_id, {'stop_reason': 'completed'})


def _failed(agent_id: str, next_action: str) -> Event:
    return Event(
        'agent_error',
        agent_id,
        {
            'error_type': 'RuntimeError',
            'expected': '这个周期正常跑完',
            'received': 'RuntimeError',
            'next_action': next_action,
        },
    )


async def test_a_childs_answer_and_end_neither_end_the_prompt_nor_reach_the_editor() -> None:
    async with _bridge() as (bridge, bus, runtime, emit):
        prompt = await _submit(bridge, runtime, 'go')
        await bus.emit(_said(_CHILD, 'CHILD-ANSWER'))
        await bus.emit(_ended(_CHILD))
        await _drain(bus, emit, 'mark-1')
        assert not prompt.done()
        assert emit.messages == []
        await bus.emit(_said(_ROOT, 'ROOT-FINAL'))
        await bus.emit(_ended(_ROOT))
        async with asyncio.timeout(_TIMEOUT):
            assert await prompt == 'end_turn'
        assert emit.messages == ['ROOT-FINAL']


async def test_a_childs_error_fails_neither_the_prompt_nor_the_session() -> None:
    async with _bridge() as (bridge, bus, runtime, emit):
        prompt = await _submit(bridge, runtime, 'go')
        await bus.emit(_failed(_CHILD, '看子 Agent 的日志'))
        await _drain(bus, emit, 'mark-1')
        assert not prompt.done()
        await bus.emit(_ended(_ROOT))
        async with asyncio.timeout(_TIMEOUT):
            assert await prompt == 'end_turn'


async def test_a_root_error_fails_that_prompt_and_the_next_prompt_runs() -> None:
    async with _bridge() as (bridge, bus, runtime, _emit):
        prompt = await _submit(bridge, runtime, 'go')
        await bus.emit(_failed(_ROOT, '修好模型配置再发'))
        with pytest.raises(RequestError, match='报错 RuntimeError') as failed:
            async with asyncio.timeout(_TIMEOUT):
                await prompt
        # 内核事件里的三件套可能带着异常原文，只有类型交给编辑器。
        assert '修好模型配置再发' not in str(failed.value)
        again = await _submit(bridge, runtime, 'again')
        await bus.emit(_ended(_ROOT))
        async with asyncio.timeout(_TIMEOUT):
            assert await again == 'end_turn'
        assert [(target, text) for target, text, _ in runtime.sent] == [
            (_ROOT, 'go'),
            (_ROOT, 'again'),
        ]


async def test_a_childs_question_ends_the_prompt_and_its_answer_goes_to_the_child() -> None:
    async with _bridge() as (bridge, bus, runtime, emit):
        prompt = await _submit(bridge, runtime, 'go')
        await bus.emit(
            Event(
                'user_question_requested',
                _CHILD,
                {'request_id': 'r1', 'question': '选哪个？', 'options': ['红', '蓝']},
            )
        )
        async with asyncio.timeout(_TIMEOUT):
            assert await prompt == 'end_turn'
        assert emit.messages == ['选哪个？\n1. 红\n2. 蓝']
        answer = await _submit(bridge, runtime, '红')
        assert runtime.sent[-1] == (
            _CHILD,
            '红',
            {
                'activate': False,
                'sender': 'human',
                'correlation_id': 'r1',
                'trust': TrustLevel.TRUSTED,
            },
        )
        await bus.emit(_ended(_ROOT))
        async with asyncio.timeout(_TIMEOUT):
            assert await answer == 'end_turn'


async def test_a_childs_confirmation_is_asked_and_its_answer_goes_to_the_child() -> None:
    async with _bridge() as (bridge, bus, runtime, emit):
        prompt = await _submit(bridge, runtime, 'go')
        await bus.emit(
            Event(
                'tool_confirmation_requested',
                _CHILD,
                {
                    'request_id': 'c1',
                    'tool_call_id': 'b1',
                    'tool_name': 'Bash',
                    'arguments': '{"command": "ls"}',
                },
            )
        )
        await _drain(bus, emit, 'mark-1')
        assert emit.permissions == [('b1', 'Bash', '{"command": "ls"}')]
        assert runtime.sent[-1] == (
            _CHILD,
            'approve',
            {
                'activate': False,
                'sender': 'human',
                'correlation_id': 'c1',
                'trust': TrustLevel.TRUSTED,
            },
        )
        await bus.emit(_ended(_ROOT))
        async with asyncio.timeout(_TIMEOUT):
            assert await prompt == 'end_turn'


async def test_tool_input_is_redacted_before_it_is_clipped_to_4000_chars() -> None:
    arguments = 'x' * 3990 + _SECRET + 'y' * 5000
    async with _bridge() as (_bridge_, bus, _runtime, emit):
        await bus.emit(
            Event(
                'tool_call_started',
                _CHILD,
                {'tool_call_id': 'b1', 'name': 'Bash', 'arguments': arguments},
            )
        )
        await _drain(bus, emit, 'mark-1')
    [shown] = [raw for call_id, _name, status, raw in emit.tools if call_id == 'b1']
    assert len(shown) == 4000
    assert shown.startswith('x' * 3990 + '[REDACTED')
    assert _SECRET[:8] not in shown


async def test_confirmation_arguments_are_redacted() -> None:
    async with _bridge() as (_bridge_, bus, _runtime, emit):
        await bus.emit(
            Event(
                'tool_confirmation_requested',
                _ROOT,
                {
                    'request_id': 'c1',
                    'tool_call_id': 'b1',
                    'tool_name': 'Bash',
                    'arguments': f'{{"command": "curl -H {_SECRET}"}}',
                },
            )
        )
        await _drain(bus, emit, 'mark-1')
    [(_call_id, _name, arguments)] = emit.permissions
    assert _SECRET not in arguments
    assert arguments.startswith('{"command": "curl -H [REDACTED')


async def test_confirmation_arguments_are_redacted_before_they_are_clipped() -> None:
    arguments = 'x' * 3990 + _SECRET + 'y' * 5000
    async with _bridge() as (_bridge_, bus, _runtime, emit):
        await bus.emit(
            Event(
                'tool_confirmation_requested',
                _ROOT,
                {
                    'request_id': 'c2',
                    'tool_call_id': 'b2',
                    'tool_name': 'Bash',
                    'arguments': arguments,
                },
            )
        )
        await _drain(bus, emit, 'mark-2')
    [(_call_id, _name, shown)] = emit.permissions
    assert len(shown) == 4000
    assert shown.startswith('x' * 3990 + '[REDACTED')
    assert _SECRET[:8] not in shown


async def test_the_assistant_text_and_a_question_are_redacted() -> None:
    async with _bridge() as (_bridge_, bus, _runtime, emit):
        await bus.emit(_said(_ROOT, f'答 {_SECRET}'))
        await bus.emit(
            Event(
                'user_question_requested',
                _ROOT,
                {'request_id': 'r1', 'question': f'问 {_SECRET}', 'options': ['红']},
            )
        )
        await _drain(bus, emit, 'mark-1')
    assert len(emit.messages) == 2
    assert all(_SECRET not in text for text in emit.messages)
    assert emit.messages[0].startswith('答 [REDACTED')
    assert emit.messages[1].startswith('问 [REDACTED')


async def test_history_replay_redacts_both_sides() -> None:
    runtime = _Runtime(
        [
            ContextBlock(role='user', content=f'问 {_SECRET}'),
            ContextBlock(role='assistant', content=f'答 {_SECRET}'),
        ]
    )
    async with _bridge(runtime) as (bridge, _bus, _runtime, emit):
        await bridge.replay()
    assert len(emit.users) == 1 and len(emit.messages) == 1
    assert _SECRET not in emit.users[0] and _SECRET not in emit.messages[0]
    assert emit.users[0].startswith('问 [REDACTED')
    assert emit.messages[0].startswith('答 [REDACTED')
