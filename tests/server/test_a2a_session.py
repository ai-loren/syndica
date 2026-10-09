"""A2A 的一条消息是会话里的一轮。提问留给对端，没人时确认按拒绝。"""

from __future__ import annotations

import asyncio
import json
from io import StringIO
from pathlib import Path

from a2a.types.a2a_pb2 import TaskState

from syndica.cli.assembly import ServerAssembly
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import Redactor, RuleRedactor, ScriptedLLM, ScriptedTurn, ToolCall
from syndica.server.a2a_serve import A2ADesk
from syndica.server.live import Live

_SECRET = 'quizSECRETvalue-918273'
_TIMEOUT = 20


def _desk(
    tmp_path: Path, llm: ScriptedLLM, redactor: Redactor | None = None
) -> tuple[RuntimeCliHost, Live, A2ADesk]:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    config = workspace / '.syndica.yaml'
    config.write_text('version: 1\n', encoding='utf-8')
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=workspace,
    )
    loaded = host.trusted_config(str(config))
    live = Live(ServerAssembly(host, path=str(config), config=loaded))
    return host, live, A2ADesk(live, redactor=redactor)


def _delegate() -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[
            ToolCall(
                id='d1',
                name='Delegate',
                arguments=json.dumps({'agent': 'Worker', 'query': 'do it'}),
            )
        ]
    )


def _ask(question: str = '选哪个？') -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[
            ToolCall(
                id='q1',
                name='AskUserQuestion',
                arguments=json.dumps({'question': question, 'options': ['红', '蓝']}),
            )
        ]
    )


def test_one_message_is_one_turn_on_a_worker_session(tmp_path: Path) -> None:
    host, live, desk = _desk(tmp_path, ScriptedLLM([ScriptedTurn(content='saw the note')]))

    async def body() -> None:
        try:
            outcome = await desk.turn(context_id='c1', task_id='t1', text='hello', role='Worker')
            assert outcome.state == TaskState.TASK_STATE_COMPLETED
            assert outcome.text == 'saw the note'
            assert len(live.sessions) == 1
        finally:
            await live.shutdown()

    try:
        asyncio.run(body())
    finally:
        host.close()


class _Down(ScriptedLLM):
    async def create(self, messages: list[object], **kwargs: object) -> object:  # type: ignore[override]
        del messages, kwargs
        raise RuntimeError(f'model is down {_SECRET} https://user:pw@host')


def test_a_root_error_fails_the_task_naming_only_its_type(tmp_path: Path) -> None:
    host, live, desk = _desk(tmp_path, _Down([]))

    async def body() -> None:
        try:
            async with asyncio.timeout(_TIMEOUT):
                outcome = await desk.turn(context_id='c1', task_id='t1', text='hi', role='Worker')
            assert outcome.state == TaskState.TASK_STATE_FAILED
            assert '报错 RuntimeError' in outcome.text
            assert _SECRET not in outcome.text and 'pw@host' not in outcome.text
            assert 'model is down' not in outcome.text
        finally:
            await live.shutdown()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_question_waits_for_the_next_message_with_the_same_task(tmp_path: Path) -> None:
    host, live, desk = _desk(
        tmp_path,
        ScriptedLLM(
            [
                ScriptedTurn(
                    tool_calls=[
                        ToolCall(
                            id='q1',
                            name='AskUserQuestion',
                            arguments=json.dumps({'question': '选哪个？', 'options': ['红', '蓝']}),
                        )
                    ]
                ),
                ScriptedTurn(content='picked red'),
            ]
        ),
    )

    async def body() -> None:
        try:
            asked = await desk.turn(context_id='c1', task_id='t1', text='ask', role='Worker')
            assert asked.state == TaskState.TASK_STATE_INPUT_REQUIRED
            assert '选哪个？' in asked.text and '红' in asked.text
            wrong = await desk.turn(context_id='c1', task_id='t1', text='绿', role='Worker')
            assert wrong.state == TaskState.TASK_STATE_INPUT_REQUIRED
            assert '绿' in wrong.text
            answered = await desk.turn(context_id='c1', task_id='t1', text='红', role='Worker')
            assert answered.state == TaskState.TASK_STATE_COMPLETED
            assert answered.text == 'picked red'
        finally:
            await live.shutdown()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_an_unattended_confirmation_is_rejected(tmp_path: Path) -> None:
    host, live, desk = _desk(
        tmp_path,
        ScriptedLLM(
            [
                ScriptedTurn(
                    tool_calls=[
                        ToolCall(
                            id='b1',
                            name='Bash',
                            arguments=json.dumps({'command': 'cat /etc/hosts'}),
                        )
                    ]
                ),
                ScriptedTurn(content='after'),
            ]
        ),
    )
    live.unattended = True

    async def body() -> None:
        try:
            outcome = await desk.turn(
                context_id='c1', task_id='t1', text='read hosts', role='Worker'
            )
            assert outcome.state == TaskState.TASK_STATE_COMPLETED
            assert outcome.text == 'after'
            session = next(iter(live.sessions.values()))
            blob = json.dumps(await session.handle.snapshot(), ensure_ascii=False)
            assert '没有执行' in blob or '已被拒绝' in blob
        finally:
            await live.shutdown()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_delegated_task_completes_with_the_root_answer_after_the_root_ends(
    tmp_path: Path,
) -> None:
    host, live, desk = _desk(
        tmp_path,
        ScriptedLLM(
            [_delegate(), ScriptedTurn(content='CHILD-ANSWER'), ScriptedTurn(content='ROOT-FINAL')]
        ),
    )

    async def body() -> None:
        try:
            async with asyncio.timeout(_TIMEOUT):
                outcome = await desk.turn(
                    context_id='c1', task_id='t1', text='go', role='Orchestrator'
                )
            assert outcome.state == TaskState.TASK_STATE_COMPLETED
            assert outcome.text == 'ROOT-FINAL'
        finally:
            await live.shutdown()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_childs_question_is_input_required_and_the_answer_reaches_the_child(
    tmp_path: Path,
) -> None:
    llm = ScriptedLLM(
        [
            _delegate(),
            _ask('子问：选哪个？'),
            ScriptedTurn(content='CHILD-ANSWER'),
            ScriptedTurn(content='ROOT-FINAL'),
        ]
    )
    host, live, desk = _desk(tmp_path, llm)

    async def body() -> None:
        try:
            async with asyncio.timeout(_TIMEOUT):
                asked = await desk.turn(
                    context_id='c1', task_id='t1', text='go', role='Orchestrator'
                )
            assert asked.state == TaskState.TASK_STATE_INPUT_REQUIRED
            assert '子问：选哪个？' in asked.text
            session = next(iter(live.sessions.values()))
            [pending] = session.pending.values()
            assert pending.agent_id != session.handle.root_id
            async with asyncio.timeout(_TIMEOUT):
                answered = await desk.turn(
                    context_id='c1', task_id='t1', text='红', role='Orchestrator'
                )
            assert answered.state == TaskState.TASK_STATE_COMPLETED
            assert answered.text == 'ROOT-FINAL'
        finally:
            await live.shutdown()

    try:
        asyncio.run(body())
    finally:
        host.close()
    child_after_answer = llm.calls[2]
    assert any(block.role == 'tool' and '红' in str(block.content) for block in child_after_answer)


def test_an_unattended_question_is_still_input_required_and_can_be_answered(
    tmp_path: Path,
) -> None:
    host, live, desk = _desk(tmp_path, ScriptedLLM([_ask(), ScriptedTurn(content='picked red')]))
    live.unattended = True

    async def body() -> None:
        try:
            asked = await desk.turn(context_id='c1', task_id='t1', text='ask', role='Worker')
            assert asked.state == TaskState.TASK_STATE_INPUT_REQUIRED
            for _ in range(20):
                await asyncio.sleep(0)
            session = next(iter(live.sessions.values()))
            assert [pending.kind for pending in session.pending.values()] == ['user_question']
            answered = await desk.turn(context_id='c1', task_id='t1', text='红', role='Worker')
            assert answered.state == TaskState.TASK_STATE_COMPLETED
            assert answered.text == 'picked red'
        finally:
            await live.shutdown()

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_the_desk_redacts_the_assistant_text_and_the_question(tmp_path: Path) -> None:
    host, live, desk = _desk(
        tmp_path,
        ScriptedLLM([_ask(f'问 {_SECRET}'), ScriptedTurn(content=f'答 {_SECRET}')]),
        RuleRedactor(secrets=[_SECRET]),
    )

    async def body() -> None:
        try:
            asked = await desk.turn(context_id='c1', task_id='t1', text='ask', role='Worker')
            assert asked.state == TaskState.TASK_STATE_INPUT_REQUIRED
            assert _SECRET not in asked.text
            assert asked.text.startswith('问 [REDACTED')
            answered = await desk.turn(context_id='c1', task_id='t1', text='红', role='Worker')
            assert answered.state == TaskState.TASK_STATE_COMPLETED
            assert _SECRET not in answered.text
            assert answered.text.startswith('答 [REDACTED')
        finally:
            await live.shutdown()

    try:
        asyncio.run(body())
    finally:
        host.close()
