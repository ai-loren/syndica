"""托管 ACP Agent 的客户端回调：拒绝、没做成与异常都回带三件套的 RequestError；终端按 ACP 的语义
截输出、报退出状态、结束与释放；活动事件按 SSE schema 的契约取值。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from acp.agent.connection import AgentSideConnection
from acp.client.connection import ClientSideConnection
from acp.exceptions import RequestError
from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PermissionOption,
    PlanEntry,
    PromptResponse,
    TextContentBlock,
    ToolCallStart,
    ToolCallUpdate,
)

from syndica.backends.acp.model import AcpAgentModel
from syndica.cli.policy import dev_guard_judge
from syndica.kernel import (
    BlockHighRisk,
    ConfigError,
    ConfirmationOutcome,
    ConfirmationRequest,
    ContextBlock,
    DangerJudge,
    DangerVerdict,
    Disposition,
    ExternalCallPolicy,
    Runtime,
    Tool,
)
from syndica.kernel.danger import RuleTablePolicy
from syndica.kernel.driving import _DRIVING_AGENT  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.file_taint import FileTaint
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.tools.local import Bash, BashOutput, KillBash, Read, Write
from syndica.kernel.workspace_router import WorkspaceRouter
from syndica.server.openapi import openapi

_SECRET = 'quizSECRETvalue-5512'
_THREE_PART = {'expected', 'received', 'next_action'}

Step = Callable[[Any, str, Path], Awaitable[object]]


class _Scripted:
    """按顺序跑 `steps`：每一步交回的值、或它收到的 RequestError，按名字记进 `outcome`。

    断言读 `outcome` 而不是托管模型交回的回答：回答会再脱敏一遍，盖住线上漏过来的原文。
    """

    def __init__(self, steps: Mapping[str, Step], root: Path) -> None:
        self.conn: Any = None
        self.outcome: dict[str, Any] = {}
        self._steps = steps
        self._root = root

    def on_connect(self, conn: object) -> None:
        self.conn = conn

    async def initialize(self, protocol_version: int, **kwargs: object) -> InitializeResponse:
        del protocol_version, kwargs
        return InitializeResponse(
            protocol_version=1, agent_info=Implementation(name='scripted', version='0')
        )

    async def new_session(self, cwd: str, **kwargs: object) -> NewSessionResponse:
        del cwd, kwargs
        return NewSessionResponse(session_id='sess')

    async def cancel(self, session_id: str, **kwargs: object) -> None:
        del session_id, kwargs

    async def prompt(
        self, session_id: str, prompt: list[object], **kwargs: object
    ) -> PromptResponse:
        del prompt, kwargs
        for name, step in self._steps.items():
            try:
                self.outcome[name] = await step(self.conn, session_id, self._root)
            except RequestError as exc:
                self.outcome[name] = {'code': exc.code, 'message': str(exc), 'data': exc.data}
        await self.conn.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update='agent_message_chunk',
                content=TextContentBlock(type='text', text='done'),
            ),
        )
        return PromptResponse(stop_reason='end_turn')


def _link(agent: _Scripted):
    @asynccontextmanager
    async def connect(client: object) -> AsyncIterator[ClientSideConnection]:
        left, right = socket.socketpair()
        left.setblocking(False)
        right.setblocking(False)
        client_reader, client_writer = await asyncio.open_connection(sock=left)
        agent_reader, agent_writer = await asyncio.open_connection(sock=right)
        server = AgentSideConnection(agent, agent_writer, agent_reader, listening=False)  # pyright: ignore[reportArgumentType]
        task = asyncio.create_task(server.listen())
        conn = ClientSideConnection(client, client_writer, client_reader)  # pyright: ignore[reportArgumentType]
        try:
            yield conn
        finally:
            task.cancel()
            await server.close()
            client_writer.close()
            agent_writer.close()

    return connect


def _workspace(tmp_path: Path) -> tuple[WorkspaceRouter, Path]:
    root = tmp_path / 'ws'
    root.mkdir()
    (root / 'notes.md').write_text('alpha notes\n', encoding='utf-8')
    (root / 'doomed.txt').write_text('keep\n', encoding='utf-8')
    local = LocalWorkspace(str(root), shell='/bin/sh', env={'PATH': os.environ.get('PATH', '')})
    return WorkspaceRouter(local, taint=FileTaint(root)), root


def _tools(router: WorkspaceRouter) -> dict[str, Tool]:
    return {
        'Read': Read(router),
        'Write': Write(router),
        'Bash': Bash(router),
        'BashOutput': BashOutput(router),
        'KillBash': KillBash(router),
    }


def _guarded(root: Path, **changes: Any) -> ExternalCallPolicy:
    fields: dict[str, Any] = {
        'judge': DangerJudge(),
        'policy': BlockHighRisk(),
        'guard': dev_guard_judge(root),
    }
    fields.update(changes)
    return ExternalCallPolicy(**fields)


async def _run(
    tmp_path: Path,
    steps: Mapping[str, Step],
    *,
    policy: Callable[[Path], ExternalCallPolicy] = _guarded,
    runtime: Runtime | None = None,
) -> tuple[dict[str, Any], Path]:
    router, root = _workspace(tmp_path)
    agent = _Scripted(steps, root)
    model = AcpAgentModel(
        'scripted',
        connect=_link(agent),
        tools=_tools(router),
        policy=policy(root),
        cwd=str(root),
        runtime=runtime,
        redactor=RuleRedactor(secrets=[_SECRET]),
    )

    async def as_worker() -> ContextBlock:
        _DRIVING_AGENT.set('worker')
        return await model.create([ContextBlock(role='user', content='go')])

    try:
        reply = await asyncio.create_task(as_worker())
    finally:
        await model.aclose()
        await router.close()
    assert reply.content == 'done'
    return agent.outcome, root


def _assert_three_part(answer: object) -> dict[str, str]:
    assert isinstance(answer, dict)
    data = answer['data']  # pyright: ignore[reportUnknownVariableType]
    assert isinstance(data, dict)
    assert set(data) == _THREE_PART  # pyright: ignore[reportUnknownArgumentType]
    return data  # pyright: ignore[reportUnknownVariableType]


def _pid_in(path: Path) -> int | None:
    text = path.read_text(encoding='utf-8') if path.exists() else ''
    return int(text) if text.strip() else None


async def _written_pid(path: Path) -> int:
    deadline = time.monotonic() + 10
    while _pid_in(path) is None and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.02)
    pid = _pid_in(path)
    assert pid is not None, f'{path} 十秒内没有写出 pid'
    return pid


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _stderr_then_output(
    conn: Any, session_id: str, text: str, limit: int | None
) -> dict[str, object]:
    created = await conn.create_terminal(
        session_id=session_id,
        command='sh',
        args=['-c', f"printf '{text}' >&2"],
        output_byte_limit=limit,
    )
    await conn.wait_for_terminal_exit(session_id=session_id, terminal_id=created.terminal_id)
    output = await conn.terminal_output(session_id=session_id, terminal_id=created.terminal_id)
    return {'output': output.output, 'truncated': output.truncated}


async def test_an_output_byte_limit_keeps_the_last_bytes_and_says_it_truncated(
    tmp_path: Path,
) -> None:
    async def whole(conn: Any, session_id: str, root: Path) -> object:
        del root
        return await _stderr_then_output(conn, session_id, 'head-0123456789', None)

    async def last_8(conn: Any, session_id: str, root: Path) -> object:
        del root
        return await _stderr_then_output(conn, session_id, 'head-0123456789', 8)

    outcome, _ = await _run(tmp_path, {'whole': whole, 'last_8': last_8})

    assert outcome['whole']['output'].endswith('head-0123456789')
    assert outcome['whole']['truncated'] is False
    assert outcome['last_8'] == {'output': '23456789', 'truncated': True}


async def test_an_output_byte_limit_cuts_at_a_character_boundary(tmp_path: Path) -> None:
    async def euros(conn: Any, session_id: str, root: Path) -> object:
        del root
        return await _stderr_then_output(conn, session_id, '€€€', 8)

    outcome, _ = await _run(tmp_path, {'euros': euros})

    assert outcome['euros'] == {'output': '€€', 'truncated': True}


async def test_an_output_byte_limit_of_zero_is_accepted_and_keeps_nothing(
    tmp_path: Path,
) -> None:
    async def nothing(conn: Any, session_id: str, root: Path) -> object:
        del root
        return await _stderr_then_output(conn, session_id, 'anything', 0)

    outcome, _ = await _run(tmp_path, {'nothing': nothing})

    assert outcome['nothing'] == {'output': '', 'truncated': True}


async def test_wait_for_exit_returns_the_exit_code(tmp_path: Path) -> None:
    async def exits_3(conn: Any, session_id: str, root: Path) -> object:
        del root
        created = await conn.create_terminal(
            session_id=session_id, command='sh', args=['-c', 'exit 3']
        )
        waited = await conn.wait_for_terminal_exit(
            session_id=session_id, terminal_id=created.terminal_id
        )
        return {'exit_code': waited.exit_code, 'signal': waited.signal}

    outcome, _ = await _run(tmp_path, {'exits_3': exits_3})

    assert outcome['exits_3'] == {'exit_code': 3, 'signal': None}


async def test_a_killed_terminal_ends_promptly_and_reports_the_signal(tmp_path: Path) -> None:
    async def killed(conn: Any, session_id: str, root: Path) -> object:
        del root
        created = await conn.create_terminal(session_id=session_id, command='sleep', args=['30'])
        started = time.monotonic()
        await conn.kill_terminal(session_id=session_id, terminal_id=created.terminal_id)
        waited = await asyncio.wait_for(
            conn.wait_for_terminal_exit(session_id=session_id, terminal_id=created.terminal_id),
            20,
        )
        output = await conn.terminal_output(session_id=session_id, terminal_id=created.terminal_id)
        status = output.exit_status
        return {
            'seconds': time.monotonic() - started,
            'waited': {'exit_code': waited.exit_code, 'signal': waited.signal},
            'status': None
            if status is None
            else {'exit_code': status.exit_code, 'signal': status.signal},
        }

    outcome, _ = await _run(tmp_path, {'killed': killed})

    assert outcome['killed']['seconds'] < 10
    assert outcome['killed']['waited'] == {'exit_code': None, 'signal': 'SIGKILL'}
    assert outcome['killed']['status'] == {'exit_code': None, 'signal': 'SIGKILL'}


async def test_a_released_terminal_is_killed_and_its_id_is_refused_afterwards(
    tmp_path: Path,
) -> None:
    async def released(conn: Any, session_id: str, root: Path) -> object:
        created = await conn.create_terminal(
            session_id=session_id, command='sh', args=['-c', 'echo $$ > term.pid; exec sleep 30']
        )
        pid = await _written_pid(root / 'term.pid')
        await conn.release_terminal(session_id=session_id, terminal_id=created.terminal_id)
        return pid

    async def output_after(conn: Any, session_id: str, root: Path) -> object:
        del root
        created = await conn.create_terminal(session_id=session_id, command='echo', args=['x'])
        await conn.release_terminal(session_id=session_id, terminal_id=created.terminal_id)
        return await conn.terminal_output(session_id=session_id, terminal_id=created.terminal_id)

    outcome, _ = await _run(tmp_path, {'released': released, 'output_after': output_after})

    assert not _alive(outcome['released'])
    answer = outcome['output_after']
    data = _assert_three_part(answer)
    assert answer['code'] == -32602
    assert 'terminal_id' in data['expected']
    assert 'release 之后的 id 不能再查' in data['next_action']


def _refusing(disposition: Disposition) -> Callable[[Path], ExternalCallPolicy]:
    def policy(root: Path) -> ExternalCallPolicy:
        return _guarded(root, policy=RuleTablePolicy({'danger.no_rule_matched': disposition}))

    return policy


class _Rejects:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def confirm(self, runtime: object, request: ConfirmationRequest) -> ConfirmationOutcome:
        del runtime
        self.asked.append(request.tool_name)
        return ConfirmationOutcome.REJECTED


async def _overwrite_doomed(conn: Any, session_id: str, root: Path) -> object:
    del root
    await conn.write_text_file(session_id=session_id, path='doomed.txt', content='planted')
    return 'ok'


@pytest.mark.parametrize(
    'disposition', [Disposition.BLOCK, Disposition.CONFIRM], ids=['blocked', 'no-channel']
)
async def test_a_write_the_policy_refuses_is_a_request_error_and_the_file_is_unchanged(
    tmp_path: Path, disposition: Disposition
) -> None:
    outcome, root = await _run(
        tmp_path, {'write': _overwrite_doomed}, policy=_refusing(disposition)
    )

    data = _assert_three_part(outcome['write'])
    assert 'fs/write_text_file' in data['expected']
    assert '没有执行' in data['received']
    assert 'danger.no_rule_matched' in data['received']
    assert '不要当成已经做了' in data['next_action']
    assert (root / 'doomed.txt').read_text(encoding='utf-8') == 'keep\n'


async def test_a_write_the_human_rejects_is_a_request_error_and_the_file_is_unchanged(
    tmp_path: Path,
) -> None:
    rejects = _Rejects()

    def policy(root: Path) -> ExternalCallPolicy:
        return _guarded(
            root,
            policy=RuleTablePolicy({'danger.no_rule_matched': Disposition.CONFIRM}),
            confirm=rejects,
        )

    runtime = Runtime()
    try:
        outcome, root = await _run(
            tmp_path, {'write': _overwrite_doomed}, policy=policy, runtime=runtime
        )
    finally:
        await runtime.shutdown()

    assert rejects.asked == ['Write']
    data = _assert_three_part(outcome['write'])
    assert '没有执行' in data['received']
    assert (root / 'doomed.txt').read_text(encoding='utf-8') == 'keep\n'


async def test_a_write_the_write_tool_itself_refuses_is_a_request_error(tmp_path: Path) -> None:
    async def into_git(conn: Any, session_id: str, root: Path) -> object:
        del root
        await conn.write_text_file(
            session_id=session_id, path='.git/hooks/pre-commit', content='#!/bin/sh\n'
        )
        return 'ok'

    outcome, root = await _run(tmp_path, {'into_git': into_git})

    data = _assert_three_part(outcome['into_git'])
    assert 'fs/write_text_file' in data['expected']
    assert '.git' in data['received']
    assert '这一次没有做成' in data['next_action']
    assert not (root / '.git').exists()


async def test_a_refused_read_is_a_request_error_not_file_content(tmp_path: Path) -> None:
    async def read(conn: Any, session_id: str, root: Path) -> object:
        del root
        got = await conn.read_text_file(session_id=session_id, path='notes.md')
        return got.content

    outcome, _ = await _run(tmp_path, {'read': read}, policy=_refusing(Disposition.BLOCK))

    data = _assert_three_part(outcome['read'])
    assert 'fs/read_text_file' in data['expected']
    assert '没有执行' in data['received']
    assert 'alpha notes' not in json.dumps(outcome, ensure_ascii=False)


async def test_reading_a_missing_file_is_a_request_error_not_its_explanation_as_content(
    tmp_path: Path,
) -> None:
    async def read(conn: Any, session_id: str, root: Path) -> object:
        del root
        got = await conn.read_text_file(session_id=session_id, path='absent.md')
        return got.content

    outcome, _ = await _run(tmp_path, {'read': read})

    data = _assert_three_part(outcome['read'])
    assert 'fs/read_text_file' in data['expected']
    assert '没有做成' in data['received']
    assert 'absent.md' in data['received']


async def test_a_refused_terminal_carries_three_part_data_and_runs_nothing(
    tmp_path: Path,
) -> None:
    async def rm(conn: Any, session_id: str, root: Path) -> object:
        del root
        await conn.create_terminal(
            session_id=session_id, command='rm', args=['-rf', '--', 'doomed.txt']
        )
        return 'ok'

    outcome, root = await _run(tmp_path, {'rm': rm})

    data = _assert_three_part(outcome['rm'])
    assert 'terminal/create' in data['expected']
    assert '没有执行' in data['received']
    assert (root / 'doomed.txt').read_text(encoding='utf-8') == 'keep\n'


class _Raises(DangerJudge):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    def judge(self, tool: Tool, args: Mapping[str, Any]) -> DangerVerdict:
        del tool, args
        raise self._exc


def _judged_by(exc: Exception) -> Callable[[Path], ExternalCallPolicy]:
    def policy(root: Path) -> ExternalCallPolicy:
        return _guarded(root, judge=_Raises(exc))

    return policy


async def _read_notes(conn: Any, session_id: str, root: Path) -> object:
    del root
    return (await conn.read_text_file(session_id=session_id, path='notes.md')).content


async def test_an_exception_inside_a_callback_is_answered_with_its_type_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        outcome, _ = await _run(
            tmp_path,
            {'read': _read_notes},
            policy=_judged_by(RuntimeError(f'boom {_SECRET}')),
        )

    answer = outcome['read']
    data = _assert_three_part(answer)
    assert answer['code'] == -32603
    assert 'RuntimeError' in data['received']
    assert _SECRET not in json.dumps(outcome)
    assert 'boom' not in json.dumps(outcome)
    assert 'RuntimeError' in caplog.text
    assert _SECRET not in caplog.text


async def test_a_syndica_error_inside_a_callback_is_three_part_and_redacted(
    tmp_path: Path,
) -> None:
    raised = ConfigError(
        expected='the judge accepts this call',
        received=f'the judge choked on {_SECRET}',
        next_action='fix the judge',
    )

    outcome, _ = await _run(tmp_path, {'read': _read_notes}, policy=_judged_by(raised))

    answer = outcome['read']
    data = _assert_three_part(answer)
    assert answer['code'] == -32602
    assert data['expected'] == 'the judge accepts this call'
    assert data['received'].startswith('the judge choked on [REDACTED')
    assert data['next_action'] == 'fix the judge'
    assert _SECRET not in json.dumps(outcome)


async def test_an_exception_while_asking_permission_is_answered_with_its_type_only(
    tmp_path: Path,
) -> None:
    class _Breaks:
        async def confirm(
            self, runtime: object, request: ConfirmationRequest
        ) -> ConfirmationOutcome:
            del runtime, request
            raise RuntimeError(_SECRET)

    async def ask(conn: Any, session_id: str, root: Path) -> object:
        del root
        decision = await conn.request_permission(
            session_id=session_id,
            tool_call=ToolCallUpdate(tool_call_id='c1', kind='edit', title='edit it'),
            options=[
                PermissionOption(option_id='allow', name='Allow', kind='allow_once'),
                PermissionOption(option_id='no', name='Reject', kind='reject_once'),
            ],
        )
        return decision.outcome.model_dump()

    runtime = Runtime()
    try:
        outcome, _ = await _run(
            tmp_path,
            {'ask': ask},
            policy=lambda root: _guarded(root, confirm=_Breaks()),
            runtime=runtime,
        )
    finally:
        await runtime.shutdown()

    answer = outcome['ask']
    data = _assert_three_part(answer)
    assert answer['code'] == -32603
    assert 'session/request_permission' in data['expected']
    assert 'RuntimeError' in data['received']
    assert _SECRET not in json.dumps(outcome)


async def test_activities_name_the_hosted_agent_under_a_source_the_event_schema_lists(
    tmp_path: Path,
) -> None:
    async def report(conn: Any, session_id: str, root: Path) -> object:
        del root
        await conn.session_update(
            session_id=session_id,
            update=ToolCallStart(
                session_update='tool_call',
                tool_call_id='call-7',
                title='edit notes',
                kind='edit',
                status='pending',
            ),
        )
        await conn.session_update(
            session_id=session_id,
            update=AgentPlanUpdate(
                session_update='plan',
                entries=[PlanEntry(content='write it', priority='high', status='pending')],
            ),
        )
        return 'ok'

    runtime = Runtime()
    subscription = runtime.events.subscribe()
    try:
        await _run(tmp_path, {'report': report}, runtime=runtime)
        events = [await asyncio.wait_for(anext(subscription), 1) for _ in range(2)]
    finally:
        subscription.close()
        await runtime.shutdown()

    sources = openapi()['x-syndica-events']['external_activity']['properties']['source']['enum']
    assert [event.kind for event in events] == ['external_activity', 'external_activity']
    assert [
        (event.payload['source'], event.payload['name'], event.payload['activity_id'])
        for event in events
    ] == [('acp', 'scripted', 'call-7'), ('acp', 'scripted', 'plan')]
    assert all(event.payload['source'] in sources for event in events)
