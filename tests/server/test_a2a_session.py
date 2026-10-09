"""A2A 的一条消息是会话里的一轮。contextId 就是会话 id。提问留给对端，确认跟着 Live。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any

from a2a.types.a2a_pb2 import TaskState

from syndica.cli.assembly import ServerAssembly
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import (
    ContextBlock,
    Redactor,
    RuleRedactor,
    ScriptedLLM,
    ScriptedTurn,
    ToolCall,
)
from syndica.server.a2a_serve import A2ADesk
from syndica.server.live import Live, Pending

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


class _Peer:
    """像对端那样用 contextId：第一条不带，之后带 desk 交回的那个。"""

    def __init__(self, desk: A2ADesk) -> None:
        self.desk = desk
        self.sid = ''

    async def say(
        self,
        text: str,
        *,
        task_id: str = 't1',
        role: str = 'Worker',
        progress: Callable[[str], None] | None = None,
    ) -> Any:
        def started(sid: str) -> None:
            self.sid = sid

        return await self.desk.turn(
            context_id=self.sid,
            task_id=task_id,
            text=text,
            role=role,
            started=started,
            progress=progress,
        )


def _delegate(content: str | None = None) -> ScriptedTurn:
    return ScriptedTurn(
        content=content,
        tool_calls=[
            ToolCall(
                id='d1',
                name='Delegate',
                arguments=json.dumps({'agent': 'Worker', 'query': 'do it'}),
            )
        ],
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


def _hosts() -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ToolCall(id='b1', name='Bash', arguments='{"command": "cat /etc/hosts"}')]
    )


def _run(host: RuntimeCliHost, live: Live, body: Callable[[], Any]) -> None:
    async def wrapped() -> None:
        try:
            async with asyncio.timeout(_TIMEOUT):
                await body()
        finally:
            await live.shutdown()

    try:
        asyncio.run(wrapped())
    finally:
        host.close()


def test_one_message_is_one_turn_on_a_worker_session(tmp_path: Path) -> None:
    host, live, desk = _desk(tmp_path, ScriptedLLM([ScriptedTurn(content='saw the note')]))
    peer = _Peer(desk)

    async def body() -> None:
        outcome = await peer.say('hello')
        assert outcome.state == TaskState.TASK_STATE_COMPLETED
        assert outcome.text == 'saw the note'
        assert len(live.sessions) == 1

    _run(host, live, body)


def test_a_message_without_a_context_id_opens_a_session_and_its_id_is_the_context_id(
    tmp_path: Path,
) -> None:
    llm = ScriptedLLM([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    host, live, desk = _desk(tmp_path, llm)
    peer = _Peer(desk)

    async def body() -> None:
        first = await peer.say('one', task_id='t1')
        assert first.state == TaskState.TASK_STATE_COMPLETED
        assert list(live.sessions) == [peer.sid]
        second = await peer.say('two', task_id='t2')
        assert second.text == 'second'
        assert list(live.sessions) == [peer.sid]

    _run(host, live, body)
    users = [block.content for block in llm.calls[1] if block.role == 'user']
    assert any('one' in str(text) for text in users)
    assert any('two' in str(text) for text in users)


def test_an_unknown_context_id_is_refused_without_opening_a_session(tmp_path: Path) -> None:
    host, live, desk = _desk(tmp_path, ScriptedLLM([]))
    started: list[str] = []

    async def body() -> None:
        outcome = await desk.turn(
            context_id='ctx-from-elsewhere',
            task_id='t1',
            text='hello',
            role='Worker',
            started=started.append,
        )
        assert outcome.state == TaskState.TASK_STATE_FAILED
        assert outcome.text.startswith('期望: contextId 是这个 server 经 A2A 开的')
        assert "'ctx-from-elsewhere'" in outcome.text
        assert '去掉 contextId 开一个新会话' in outcome.text
        assert live.sessions == {}

    _run(host, live, body)
    assert started == []


def test_a_closed_session_id_is_refused(tmp_path: Path) -> None:
    host, live, desk = _desk(tmp_path, ScriptedLLM([ScriptedTurn(content='first')]))
    peer = _Peer(desk)

    async def body() -> None:
        await peer.say('one')
        await live.close(peer.sid)
        refused = await peer.say('two', task_id='t2')
        assert refused.state == TaskState.TASK_STATE_FAILED
        assert repr(peer.sid) in refused.text
        assert '去掉 contextId 开一个新会话' in refused.text
        assert live.sessions == {}

    _run(host, live, body)


def test_a_session_opened_outside_a2a_is_not_continued(tmp_path: Path) -> None:
    host, live, desk = _desk(tmp_path, ScriptedLLM([]))

    async def body() -> None:
        other = await live.open_session(resume=None, role='Worker')
        refused = await desk.turn(context_id=other.sid, task_id='t1', text='hi', role='Worker')
        assert refused.state == TaskState.TASK_STATE_FAILED
        assert repr(other.sid) in refused.text
        assert other.sid not in live.hold_questions

    _run(host, live, body)


def test_a_follow_up_with_only_the_task_id_lands_in_that_tasks_session(tmp_path: Path) -> None:
    host, live, desk = _desk(tmp_path, ScriptedLLM([_ask(), ScriptedTurn(content='picked red')]))
    peer = _Peer(desk)

    async def body() -> None:
        asked = await peer.say('ask', task_id='t1')
        assert asked.state == TaskState.TASK_STATE_INPUT_REQUIRED
        landed: list[str] = []
        answered = await desk.turn(
            context_id='', task_id='t1', text='红', role='Worker', started=landed.append
        )
        assert answered.state == TaskState.TASK_STATE_COMPLETED
        assert landed == [peer.sid]
        assert list(live.sessions) == [peer.sid]

    _run(host, live, body)


class _Down(ScriptedLLM):
    async def create(self, messages: list[object], **kwargs: object) -> object:  # type: ignore[override]
        del messages, kwargs
        raise RuntimeError(f'model is down {_SECRET} https://user:pw@host')


def test_a_root_error_fails_the_task_naming_only_its_type(tmp_path: Path) -> None:
    host, live, desk = _desk(tmp_path, _Down([]))
    peer = _Peer(desk)

    async def body() -> None:
        outcome = await peer.say('hi')
        assert outcome.state == TaskState.TASK_STATE_FAILED
        assert '报错 RuntimeError' in outcome.text
        assert _SECRET not in outcome.text and 'pw@host' not in outcome.text
        assert 'model is down' not in outcome.text

    _run(host, live, body)


def test_a_question_waits_for_the_next_message_with_the_same_task(tmp_path: Path) -> None:
    host, live, desk = _desk(tmp_path, ScriptedLLM([_ask(), ScriptedTurn(content='picked red')]))
    peer = _Peer(desk)

    async def body() -> None:
        asked = await peer.say('ask')
        assert asked.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert '选哪个？' in asked.text and '红' in asked.text
        wrong = await peer.say('绿')
        assert wrong.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert '绿' in wrong.text
        answered = await peer.say('红')
        assert answered.state == TaskState.TASK_STATE_COMPLETED
        assert answered.text == 'picked red'

    _run(host, live, body)


def test_an_unattended_confirmation_is_rejected(tmp_path: Path) -> None:
    host, live, desk = _desk(tmp_path, ScriptedLLM([_hosts(), ScriptedTurn(content='after')]))
    live.unattended = True
    peer = _Peer(desk)

    async def body() -> None:
        outcome = await peer.say('read hosts')
        assert outcome.state == TaskState.TASK_STATE_COMPLETED
        assert outcome.text == 'after'
        session = next(iter(live.sessions.values()))
        blob = json.dumps(await session.handle.snapshot(), ensure_ascii=False)
        assert '没有执行' in blob or '已被拒绝' in blob

    _run(host, live, body)


async def _confirmation(live: Live) -> tuple[str, Pending]:
    while True:
        for sid, session in list(live.sessions.items()):
            for pending in session.pending.values():
                if pending.kind == 'confirmation':
                    return sid, pending
        await asyncio.sleep(0.01)


def test_an_attended_confirmation_waits_for_the_human_and_runs_after_approval(
    tmp_path: Path,
) -> None:
    llm = ScriptedLLM([_hosts(), ScriptedTurn(content='after')])
    host, live, desk = _desk(tmp_path, llm)
    peer = _Peer(desk)

    async def body() -> None:
        live.attach()
        turn = asyncio.create_task(peer.say('read hosts'))
        sid, pending = await _confirmation(live)
        assert sid == peer.sid
        await asyncio.sleep(0.2)
        assert not turn.done()
        await live.sessions[sid].answer(pending.request_id, 'approve')
        outcome = await turn
        assert outcome.state == TaskState.TASK_STATE_COMPLETED
        assert outcome.text == 'after'

    _run(host, live, body)
    tool_results = [str(block.content) for block in llm.calls[1] if block.role == 'tool']
    assert any('localhost' in text for text in tool_results)


def test_a_delegated_task_completes_with_the_root_answer_after_the_root_ends(
    tmp_path: Path,
) -> None:
    host, live, desk = _desk(
        tmp_path,
        ScriptedLLM(
            [_delegate(), ScriptedTurn(content='CHILD-ANSWER'), ScriptedTurn(content='ROOT-FINAL')]
        ),
    )
    peer = _Peer(desk)

    async def body() -> None:
        outcome = await peer.say('go', role='Orchestrator')
        assert outcome.state == TaskState.TASK_STATE_COMPLETED
        assert outcome.text == 'ROOT-FINAL'

    _run(host, live, body)


class _HoldsTheChild(ScriptedLLM):
    """第二次调用（派出去的子 Agent）等到放行才答：先交的根文本必须在这之前送达。"""

    def __init__(self, script: list[ScriptedTurn]) -> None:
        super().__init__(script)
        self.release: asyncio.Event | None = None

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        if len(self.calls) == 1 and self.release is not None:
            await self.release.wait()
        return await super().create(messages, **kwargs)


def test_progress_carries_only_the_root_texts_as_they_are_produced(tmp_path: Path) -> None:
    llm = _HoldsTheChild(
        [
            _delegate('ROOT-PLAN'),
            ScriptedTurn(content='CHILD-ANSWER'),
            ScriptedTurn(content='ROOT-FINAL'),
        ]
    )
    host, live, desk = _desk(tmp_path, llm)
    peer = _Peer(desk)
    seen: list[str] = []

    async def body() -> None:
        release = asyncio.Event()
        llm.release = release

        def progress(text: str) -> None:
            seen.append(text)
            release.set()

        outcome = await peer.say('go', role='Orchestrator', progress=progress)
        assert outcome.state == TaskState.TASK_STATE_COMPLETED
        assert outcome.text == 'ROOT-PLAN\nROOT-FINAL'

    _run(host, live, body)
    assert seen == ['ROOT-PLAN', 'ROOT-FINAL']


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
    peer = _Peer(desk)

    async def body() -> None:
        asked = await peer.say('go', role='Orchestrator')
        assert asked.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert '子问：选哪个？' in asked.text
        session = live.sessions[peer.sid]
        [pending] = session.pending.values()
        assert pending.agent_id != session.handle.root_id
        answered = await peer.say('红', role='Orchestrator')
        assert answered.state == TaskState.TASK_STATE_COMPLETED
        assert answered.text == 'ROOT-FINAL'

    _run(host, live, body)
    child_after_answer = llm.calls[2]
    assert any(block.role == 'tool' and '红' in str(block.content) for block in child_after_answer)


def test_an_unattended_question_is_still_input_required_and_can_be_answered(
    tmp_path: Path,
) -> None:
    host, live, desk = _desk(tmp_path, ScriptedLLM([_ask(), ScriptedTurn(content='picked red')]))
    live.unattended = True
    peer = _Peer(desk)

    async def body() -> None:
        asked = await peer.say('ask')
        assert asked.state == TaskState.TASK_STATE_INPUT_REQUIRED
        for _ in range(20):
            await asyncio.sleep(0)
        session = live.sessions[peer.sid]
        assert [pending.kind for pending in session.pending.values()] == ['user_question']
        answered = await peer.say('红')
        assert answered.state == TaskState.TASK_STATE_COMPLETED
        assert answered.text == 'picked red'

    _run(host, live, body)


def test_the_desk_redacts_the_assistant_text_and_the_question(tmp_path: Path) -> None:
    host, live, desk = _desk(
        tmp_path,
        ScriptedLLM([_ask(f'问 {_SECRET}'), ScriptedTurn(content=f'答 {_SECRET}')]),
        RuleRedactor(secrets=[_SECRET]),
    )
    peer = _Peer(desk)
    seen: list[str] = []

    async def body() -> None:
        asked = await peer.say('ask')
        assert asked.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert _SECRET not in asked.text
        assert asked.text.startswith('问 [REDACTED')
        answered = await peer.say('红', progress=seen.append)
        assert answered.state == TaskState.TASK_STATE_COMPLETED
        assert _SECRET not in answered.text
        assert answered.text.startswith('答 [REDACTED')

    _run(host, live, body)
    assert [text.startswith('答 [REDACTED') for text in seen] == [True]
    assert all(_SECRET not in text for text in seen)
