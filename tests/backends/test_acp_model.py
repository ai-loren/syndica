"""ACP 模型：版本必须是 1，读写和终端走 call_external，回答不可信，取消不留进程。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from acp.agent.connection import AgentSideConnection
from acp.client.connection import ClientSideConnection
from acp.schema import (
    AgentMessageChunk,
    EnvVariable,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    TextContentBlock,
    ToolCallStart,
    ToolCallUpdate,
)

from syndica.backends.acp.model import AcpAgentModel
from syndica.cli.policy import dev_guard_judge
from syndica.kernel import (
    AgentCatalog,
    BlockHighRisk,
    Budget,
    BudgetPolicy,
    ConfigError,
    ContextBlock,
    DangerJudge,
    ExternalCallPolicy,
    HostFacts,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    ToolCall,
    TrustLevel,
    local_tools,
    register_builtins,
)
from syndica.kernel.call_limit import (
    BudgetDeadline,
    arm_model_call_limit,
    reset_model_call_limit,
)
from syndica.kernel.file_taint import FileTaint
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.tools.local import Bash, BashOutput, KillBash, Read, Write
from syndica.kernel.workspace_router import WorkspaceRouter

_SECRET = 'sk-hang-secret'
_HANG = Path(__file__).parent / 'testdata' / 'acp_hang_agent.py'
_GOLDEN = Path(__file__).parent / 'testdata' / 'acp_activity.json'


def _policy(root: Path) -> ExternalCallPolicy:
    return ExternalCallPolicy(
        judge=DangerJudge(),
        policy=BlockHighRisk(),
        guard=dev_guard_judge(root),
    )


async def _router(tmp_path: Path) -> tuple[WorkspaceRouter, Path]:
    root = tmp_path / 'ws'
    root.mkdir()
    (root / 'notes.md').write_text(f'alpha {_SECRET}\n', encoding='utf-8')
    (root / 'doomed.txt').write_text('keep\n', encoding='utf-8')
    local = LocalWorkspace(str(root), shell='/bin/sh', env={'PATH': os.environ.get('PATH', '')})
    return WorkspaceRouter(local, taint=FileTaint(root)), root


def _tools(router: WorkspaceRouter) -> dict[str, Any]:
    return {
        'Read': Read(router),
        'Write': Write(router),
        'Bash': Bash(router),
        'BashOutput': BashOutput(router),
        'KillBash': KillBash(router),
    }


class _Reads:
    def __init__(self) -> None:
        self.conn: Any = None

    def on_connect(self, conn: object) -> None:
        self.conn = conn

    async def initialize(self, protocol_version: int, **kwargs: object) -> InitializeResponse:
        del kwargs
        version = 1 if isinstance(protocol_version, int) and protocol_version >= 1 else 0
        return InitializeResponse(
            protocol_version=version,
            agent_info=Implementation(name='fake', version='0'),
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
        got = await self.conn.read_text_file(session_id=session_id, path='notes.md')
        await self.conn.session_update(
            session_id=session_id,
            update=ToolCallStart(
                session_update='tool_call',
                tool_call_id='read-1',
                title='read notes',
                kind='read',
                status='completed',
            ),
        )
        await self.conn.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update='agent_message_chunk',
                content=TextContentBlock(type='text', text=f'read:{got.content}'),
            ),
        )
        return PromptResponse(stop_reason='end_turn')


def _link(agent: _Reads):
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


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def test_bind_runtime_overlays_only_the_named_host_variables(tmp_path: Path) -> None:
    router, root = await _router(tmp_path)
    runtime = Runtime(workspace=router)
    model = AcpAgentModel(
        'gemini',
        command=('/bin/echo', 'acp'),
        pass_env=('GEMINI_API_KEY',),
    )
    try:
        model.bind_runtime(
            runtime,
            _tools(router),
            _policy(root),
            agent_env={'PATH': '/usr/bin'},
            host_env={'GEMINI_API_KEY': 'secret-value', 'OTHER': 'no'},
            redactor=RuleRedactor(secrets=['secret-value']),
        )
        child = model._env  # pyright: ignore[reportPrivateUsage]
        assert child['PATH'] == '/usr/bin'
        assert child['GEMINI_API_KEY'] == 'secret-value'
        assert 'OTHER' not in child
        assert model._sessions == {}  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()
        await router.close()


async def test_a_hosted_agent_reads_through_call_external_and_is_untrusted(
    tmp_path: Path,
) -> None:
    router, root = await _router(tmp_path)
    runtime = Runtime()
    model = AcpAgentModel(
        'reader',
        connect=_link(_Reads()),
        tools=_tools(router),
        policy=_policy(root),
        cwd=str(root),
        runtime=runtime,
        redactor=RuleRedactor(secrets=[_SECRET]),
    )
    subscription = runtime.events.subscribe()
    try:
        reply = await model.create(
            [ContextBlock(role='user', content='read the notes', trust=TrustLevel.TRUSTED)]
        )
        event = await asyncio.wait_for(anext(subscription), 1)
        assert reply.trust is TrustLevel.UNTRUSTED
        assert isinstance(reply.content, str)
        assert 'alpha' in reply.content
        assert _SECRET not in reply.content
        assert event.kind == 'external_activity'
        assert event.payload == json.loads(_GOLDEN.read_text(encoding='utf-8'))
    finally:
        subscription.close()
        await model.aclose()
        await runtime.shutdown()
        await router.close()


@pytest.mark.parametrize('answered', [0, 2], ids=['older', 'newer'])
async def test_a_hosted_agent_that_does_not_speak_version_1_is_refused(
    tmp_path: Path, answered: int
) -> None:
    class _Old(_Reads):
        async def initialize(self, protocol_version: int, **kwargs: object) -> InitializeResponse:
            del protocol_version, kwargs
            return InitializeResponse(
                protocol_version=answered,
                agent_info=Implementation(name='old', version='0'),
            )

    router, root = await _router(tmp_path)
    model = AcpAgentModel(
        'old',
        connect=_link(_Old()),
        tools=_tools(router),
        policy=_policy(root),
        cwd=str(root),
    )
    try:
        with pytest.raises(ConfigError, match='协议版本 1'):
            await model.create([ContextBlock(role='user', content='hi')])
    finally:
        await model.aclose()
        await router.close()


async def test_an_external_write_is_untrusted_when_read_back(tmp_path: Path) -> None:
    class _Writes(_Reads):
        async def prompt(
            self, session_id: str, prompt: list[object], **kwargs: object
        ) -> PromptResponse:
            del prompt, kwargs
            await self.conn.write_text_file(
                session_id=session_id, path='planted.md', content='from-outside'
            )
            await self.conn.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update='agent_message_chunk',
                    content=TextContentBlock(type='text', text='wrote'),
                ),
            )
            return PromptResponse(stop_reason='end_turn')

    router, root = await _router(tmp_path)
    model = AcpAgentModel(
        'writer',
        connect=_link(_Writes()),
        tools=_tools(router),
        policy=_policy(root),
        cwd=str(root),
    )
    try:
        await model.create([ContextBlock(role='user', content='write')])
        read = await Read(router).execute('planted.md')
        assert read.trust is TrustLevel.UNTRUSTED
        assert read.output == 'from-outside'
    finally:
        await model.aclose()
        await router.close()


async def test_execute_permission_is_rejected_without_a_channel(tmp_path: Path) -> None:
    class _Asks(_Reads):
        async def prompt(
            self, session_id: str, prompt: list[object], **kwargs: object
        ) -> PromptResponse:
            del prompt, kwargs
            decision = await self.conn.request_permission(
                session_id=session_id,
                tool_call=ToolCallUpdate(tool_call_id='c1', kind='execute', title='rm'),
                options=[
                    PermissionOption(option_id='allow', name='Allow', kind='allow_once'),
                    PermissionOption(option_id='no', name='Reject', kind='reject_once'),
                ],
            )
            chosen = getattr(decision.outcome, 'option_id', '')
            await self.conn.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update='agent_message_chunk',
                    content=TextContentBlock(type='text', text=f'perm:{chosen}'),
                ),
            )
            return PromptResponse(stop_reason='end_turn')

    router, root = await _router(tmp_path)
    model = AcpAgentModel(
        'asker',
        connect=_link(_Asks()),
        tools=_tools(router),
        policy=_policy(root),
        cwd=str(root),
    )
    try:
        reply = await model.create([ContextBlock(role='user', content='ask')])
        assert reply.content == 'perm:no'
    finally:
        await model.aclose()
        await router.close()


async def test_a_terminal_echo_is_returned_and_a_high_risk_command_is_not_run(
    tmp_path: Path,
) -> None:
    class _Shell(_Reads):
        async def prompt(
            self, session_id: str, prompt: list[object], **kwargs: object
        ) -> PromptResponse:
            del prompt, kwargs
            created = await self.conn.create_terminal(
                session_id=session_id, command='echo', args=['marker-term']
            )
            await self.conn.wait_for_terminal_exit(
                session_id=session_id, terminal_id=created.terminal_id
            )
            output = await self.conn.terminal_output(
                session_id=session_id, terminal_id=created.terminal_id
            )
            try:
                await self.conn.create_terminal(
                    session_id=session_id,
                    command='rm',
                    args=['-rf', '--', 'doomed.txt'],
                )
                refused = 'ran'
            except Exception as exc:
                refused = str(exc)
            try:
                await self.conn.create_terminal(
                    session_id=session_id,
                    command='echo',
                    args=['nope'],
                    env=[EnvVariable(name='API_KEY', value='super-secret')],
                )
                secret = 'ran'
            except Exception as exc:
                secret = str(exc)
            await self.conn.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update='agent_message_chunk',
                    content=TextContentBlock(
                        type='text', text=f'{output.output}\n{refused}\n{secret}'
                    ),
                ),
            )
            return PromptResponse(stop_reason='end_turn')

    router, root = await _router(tmp_path)
    model = AcpAgentModel(
        'shell',
        connect=_link(_Shell()),
        tools=_tools(router),
        policy=_policy(root),
        cwd=str(root),
    )
    try:
        reply = await model.create([ContextBlock(role='user', content='shell')])
        assert isinstance(reply.content, str)
        assert 'marker-term' in reply.content
        assert '没有执行' in reply.content
        assert (root / 'doomed.txt').read_text(encoding='utf-8') == 'keep\n'
        assert 'super-secret' not in reply.content
        assert 'API_KEY' in reply.content
    finally:
        await model.aclose()
        await router.close()


async def test_a_crashed_agent_does_not_echo_the_exception(tmp_path: Path) -> None:
    class _Boom(_Reads):
        async def prompt(
            self, session_id: str, prompt: list[object], **kwargs: object
        ) -> PromptResponse:
            del session_id, prompt, kwargs
            raise RuntimeError('sk-crash-secret')

    router, root = await _router(tmp_path)
    model = AcpAgentModel(
        'boom',
        connect=_link(_Boom()),
        tools=_tools(router),
        policy=_policy(root),
        cwd=str(root),
    )
    try:
        with pytest.raises(ConfigError, match='不会自动重连') as caught:
            await model.create([ContextBlock(role='user', content='go')])
        text = f'{caught.value.expected} {caught.value.received} {caught.value.next_action}'
        assert caught.value.received.startswith('连接中断：')
        assert 'sk-crash-secret' not in text
    finally:
        await model.aclose()
        await router.close()


async def test_an_orchestrator_delegates_to_a_hosted_acp_role(tmp_path: Path) -> None:
    class _Writes(_Reads):
        async def prompt(
            self, session_id: str, prompt: list[object], **kwargs: object
        ) -> PromptResponse:
            del prompt, kwargs
            await self.conn.write_text_file(
                session_id=session_id, path='planted.md', content='from-outside'
            )
            try:
                await self.conn.create_terminal(
                    session_id=session_id, command='rm', args=['-rf', '--', 'doomed.txt']
                )
                refused = 'ran'
            except Exception as exc:
                refused = str(exc)
            await self.conn.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update='agent_message_chunk',
                    content=TextContentBlock(type='text', text=f'wrote\n{refused}'),
                ),
            )
            return PromptResponse(stop_reason='end_turn')

    router, root = await _router(tmp_path)
    tools = local_tools(router.portal())
    model = AcpAgentModel('reader', connect=_link(_Writes()))
    catalog = AgentCatalog()
    register_builtins(catalog)
    catalog.load_mapping(
        {
            'name': 'Worker',
            'description': 'hosted acp agent',
            'system_prompt': 'hosted',
            'model': 'acp:reader',
            'tools': ['Read', 'Write', 'Edit', 'Bash'],
        }
    )
    parent = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='d1',
                        name='Delegate',
                        arguments=json.dumps({'agent': 'Worker', 'query': 'write the note'}),
                    )
                ]
            ),
            ScriptedTurn(content='parent done'),
        ]
    )
    factories = catalog.freeze(
        tools,
        {'default': parent, 'acp:reader': model},
        HostFacts(os='darwin', platform='test', shell='/bin/sh'),
        danger_judge=DangerJudge(),
        danger_policy=BlockHighRisk(),
    )
    runtime = Runtime(
        agents=factories,
        orchestration=True,
        workspace=router,
        budget=BudgetPolicy(root=Budget(turns=8)),
    )
    model.bind_runtime(
        runtime,
        tools,
        _policy(root),
        agent_env={'PATH': os.environ.get('PATH', '')},
        host_env={},
        redactor=RuleRedactor(),
    )
    try:
        agent_id = await runtime.spawn(factories['Orchestrator'], query='go')
        ctx = await asyncio.wait_for(runtime.wait(agent_id), 10)
        handed = next(block for block in ctx.current_traj if block.tool_call_id == 'd1')
        assert handed.trust is TrustLevel.UNTRUSTED
        assert isinstance(handed.content, str)
        assert 'wrote' in handed.content
        assert '没有执行' in handed.content
        read = await Read(router).execute('planted.md')
        assert read.trust is TrustLevel.UNTRUSTED
        assert read.output == 'from-outside'
        assert (root / 'doomed.txt').read_text(encoding='utf-8') == 'keep\n'
    finally:
        await model.aclose()
        await runtime.shutdown()
        await router.close()


async def test_a_hung_agent_is_killed_with_its_grandchild(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    router, root = await _router(tmp_path)
    marker = tmp_path / 'grand.pid'
    model = AcpAgentModel(
        'hang',
        command=(sys.executable, str(_HANG), str(marker)),
        tools=_tools(router),
        policy=_policy(root),
        cwd=str(root),
        env={'PATH': os.environ.get('PATH', '')},
        redactor=RuleRedactor(secrets=[_SECRET]),
    )
    token = arm_model_call_limit(5.0)
    try:
        with caplog.at_level(logging.INFO, logger='syndica.backends.acp.process'):
            with pytest.raises(BudgetDeadline):
                await model.create([ContextBlock(role='user', content='hang')])
        text = marker.read_text(encoding='utf-8')
        assert 'init' in text
        pid = int(text.splitlines()[0])
        assert not _alive(pid)
        assert _SECRET not in caplog.text
    finally:
        reset_model_call_limit(token)
        await model.aclose()
        await router.close()
