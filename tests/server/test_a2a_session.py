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
from syndica.kernel import ScriptedLLM, ScriptedTurn, ToolCall
from syndica.server.a2a_serve import A2ADesk
from syndica.server.live import Live


def _desk(tmp_path: Path, llm: ScriptedLLM) -> tuple[RuntimeCliHost, Live, A2ADesk]:
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
    return host, live, A2ADesk(live)


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
