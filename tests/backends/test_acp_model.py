"""ACP 模型：版本必须是 1，读写和终端走 call_external，回答不可信，取消不留进程。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, get_args

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
    ToolKind,
)

from syndica.backends.acp.model import AcpAgentModel
from syndica.cli.policy import dev_guard_judge
from syndica.kernel import (
    AgentCatalog,
    BlockHighRisk,
    Budget,
    BudgetPolicy,
    ConfigError,
    ConfirmationOutcome,
    ConfirmationRequest,
    ContextBlock,
    DangerJudge,
    ExternalCallPolicy,
    HostFacts,
    LocalIsolator,
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
from syndica.kernel.driving import _DRIVING_AGENT  # pyright: ignore[reportPrivateUsage]
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
            role='Worker',
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
        {name: tools[name] for name in ('Read', 'Write', 'Edit', 'Bash', 'BashOutput', 'KillBash')},
        _policy(root),
        role='Worker',
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


_GRANTED_KINDS: tuple[ToolKind, ...] = ('think', 'read', 'search')
_ASKED_KINDS: tuple[ToolKind, ...] = (
    'fetch',
    'edit',
    'other',
    'execute',
    'move',
    'switch_mode',
    'delete',
)
_ALL_TOOLS = ('Read', 'Write', 'Bash')


def _refusal(exc: Exception) -> dict[str, object]:
    return {'message': str(exc), 'data': getattr(exc, 'data', None)}


class _Tries(_Reads):
    """依次读 notes.md、写 planted.md、开终端 touch made.txt；每一步的结果或拒绝原文写进回答。"""

    async def prompt(
        self, session_id: str, prompt: list[object], **kwargs: object
    ) -> PromptResponse:
        del prompt, kwargs
        outcome: dict[str, object] = {}
        try:
            got = await self.conn.read_text_file(session_id=session_id, path='notes.md')
            outcome['read'] = got.content
        except Exception as exc:
            outcome['read'] = _refusal(exc)
        try:
            await self.conn.write_text_file(
                session_id=session_id, path='planted.md', content='from-outside'
            )
            outcome['write'] = 'ok'
        except Exception as exc:
            outcome['write'] = _refusal(exc)
        try:
            created = await self.conn.create_terminal(
                session_id=session_id, command='touch', args=['made.txt']
            )
            await self.conn.wait_for_terminal_exit(
                session_id=session_id, terminal_id=created.terminal_id
            )
            outcome['terminal'] = 'ok'
        except Exception as exc:
            outcome['terminal'] = _refusal(exc)
        await self.conn.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update='agent_message_chunk',
                content=TextContentBlock(type='text', text=json.dumps(outcome)),
            ),
        )
        return PromptResponse(stop_reason='end_turn')


class _AsksFor(_Reads):
    def __init__(self, kind: ToolKind) -> None:
        super().__init__()
        self.kind: ToolKind = kind

    async def prompt(
        self, session_id: str, prompt: list[object], **kwargs: object
    ) -> PromptResponse:
        del prompt, kwargs
        decision = await self.conn.request_permission(
            session_id=session_id,
            tool_call=ToolCallUpdate(tool_call_id='c1', kind=self.kind, title='do it'),
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


class _Confirm:
    def __init__(self, outcome: ConfirmationOutcome) -> None:
        self.outcome = outcome
        self.kinds: list[str] = []

    async def confirm(self, runtime: object, request: ConfirmationRequest) -> ConfirmationOutcome:
        del runtime
        self.kinds.append(json.loads(request.arguments)['kind'])
        return self.outcome


def _granted(tools: Mapping[str, Any], declared: Sequence[str]) -> dict[str, Any]:
    granted = {name: tools[name] for name in declared}
    if 'Bash' in granted:
        granted['BashOutput'] = tools['BashOutput']
        granted['KillBash'] = tools['KillBash']
    return granted


async def _run_roles(
    tmp_path: Path,
    agent: _Reads,
    roles: Mapping[str, Sequence[str]],
    *,
    read_only: bool = False,
    confirm: _Confirm | None = None,
    bound: Sequence[str] | None = None,
) -> tuple[dict[str, str], Path]:
    """每个角色都用同一个托管模型、各按自己的 tools 绑定（`bound` 给了就只绑这几个）；按声明顺序逐个
    跑完，交回各自的回答。"""
    router, root = await _router(tmp_path)
    tools = local_tools(router.portal())
    model = AcpAgentModel('fake', connect=_link(agent))
    catalog = AgentCatalog()
    for name, declared in roles.items():
        catalog.load_mapping(
            {
                'name': name,
                'description': f'{name} role',
                'system_prompt': 'hosted',
                'model': 'acp:fake',
                'tools': list(declared),
            }
        )
    factories = catalog.freeze(
        tools,
        {'acp:fake': model},
        HostFacts(os='darwin', platform='test', shell='/bin/sh'),
        danger_judge=DangerJudge(),
        danger_policy=BlockHighRisk(),
    )
    runtime = Runtime(agents=factories, workspace=router, budget=BudgetPolicy(root=Budget(turns=4)))
    for name, declared in roles.items():
        if bound is not None and name not in bound:
            continue
        model.bind_runtime(
            runtime,
            _granted(tools, declared),
            replace(_policy(root), confirm=confirm),
            role=name,
            read_only=read_only,
            agent_env={'PATH': os.environ.get('PATH', '')},
            host_env={},
            redactor=RuleRedactor(),
        )
    replies: dict[str, str] = {}
    try:
        for name in roles:
            agent_id = await runtime.spawn(factories[name], query='go')
            ctx = await asyncio.wait_for(runtime.wait(agent_id), 10)
            answers = [block for block in ctx.current_traj if block.role == 'assistant']
            replies[name] = str(answers[-1].content)
    finally:
        await model.aclose()
        await runtime.shutdown()
        await router.close()
    return replies, root


async def test_a_role_with_only_read_cannot_write_or_open_a_terminal(tmp_path: Path) -> None:
    replies, root = await _run_roles(tmp_path, _Tries(), {'Peek': ['Read']})

    outcome = json.loads(replies['Peek'])
    assert 'alpha' in outcome['read']
    write = outcome['write']['data']
    terminal = outcome['terminal']['data']
    assert 'fs/write_text_file' in write['expected']
    assert '角色 Peek' in write['received']
    assert '角色 Peek 的 tools 里加上 Write' in write['next_action']
    assert 'terminal/create' in terminal['expected']
    assert '角色 Peek 的 tools 里加上 Bash' in terminal['next_action']
    assert not (root / 'planted.md').exists()
    assert not (root / 'made.txt').exists()


async def test_a_role_that_declares_write_and_bash_can_write_and_run_a_terminal(
    tmp_path: Path,
) -> None:
    replies, root = await _run_roles(tmp_path, _Tries(), {'Doer': _ALL_TOOLS})

    outcome = json.loads(replies['Doer'])
    assert outcome['write'] == 'ok'
    assert outcome['terminal'] == 'ok'
    assert (root / 'planted.md').read_text(encoding='utf-8') == 'from-outside'
    assert (root / 'made.txt').exists()


async def test_two_roles_sharing_one_hosted_model_each_get_only_their_own_tools(
    tmp_path: Path,
) -> None:
    replies, _ = await _run_roles(
        tmp_path, _Tries(), {'Peek': ['Read'], 'Doer': _ALL_TOOLS, 'Note': ['Read', 'Write']}
    )

    peek = json.loads(replies['Peek'])
    doer = json.loads(replies['Doer'])
    note = json.loads(replies['Note'])
    assert '角色 Peek 的 tools 里加上 Write' in peek['write']['data']['next_action']
    assert doer['write'] == 'ok'
    assert doer['terminal'] == 'ok'
    assert note['write'] == 'ok'
    assert '角色 Note 的 tools 里加上 Bash' in note['terminal']['data']['next_action']


async def test_a_role_the_host_never_bound_gets_no_tools_at_all(tmp_path: Path) -> None:
    replies, root = await _run_roles(
        tmp_path, _Tries(), {'Doer': _ALL_TOOLS, 'Stray': _ALL_TOOLS}, bound=['Doer']
    )

    stray = json.loads(replies['Stray'])
    for step, tool in (('read', 'Read'), ('write', 'Write'), ('terminal', 'Bash')):
        assert f'角色 Stray 的 tools 里加上 {tool}' in stray[step]['data']['next_action']
    assert json.loads(replies['Doer'])['write'] == 'ok'
    assert (root / 'planted.md').exists()


@pytest.mark.parametrize(
    'declared', [['Read'], ['Read', 'Write', 'Bash']], ids=['read', 'read-write-bash']
)
async def test_a_read_only_run_refuses_writes_and_terminals_whatever_the_role_declares(
    tmp_path: Path, declared: list[str]
) -> None:
    replies, root = await _run_roles(tmp_path, _Tries(), {'Peek': declared}, read_only=True)

    outcome = json.loads(replies['Peek'])
    assert 'alpha' in outcome['read']
    for step in ('write', 'terminal'):
        data = outcome[step]['data']
        assert '只读 run' in data['expected']
        assert '--no-write' in data['received']
        assert '去掉 --no-write' in data['next_action']
    assert not (root / 'planted.md').exists()
    assert not (root / 'made.txt').exists()


def test_the_permission_cases_cover_every_acp_tool_kind() -> None:
    assert sorted((*_GRANTED_KINDS, *_ASKED_KINDS)) == sorted(get_args(ToolKind))


@pytest.mark.parametrize('kind', _GRANTED_KINDS)
async def test_a_read_search_or_think_permission_is_granted_without_asking(
    tmp_path: Path, kind: ToolKind
) -> None:
    confirm = _Confirm(ConfirmationOutcome.REJECTED)

    replies, _ = await _run_roles(tmp_path, _AsksFor(kind), {'Doer': _ALL_TOOLS}, confirm=confirm)

    assert replies['Doer'] == 'perm:allow'
    assert confirm.kinds == []


@pytest.mark.parametrize(
    ('outcome', 'chosen'),
    [(ConfirmationOutcome.APPROVED, 'allow'), (ConfirmationOutcome.REJECTED, 'no')],
    ids=['approved', 'rejected'],
)
@pytest.mark.parametrize('kind', _ASKED_KINDS)
async def test_any_other_permission_is_asked_and_follows_the_answer(
    tmp_path: Path, kind: ToolKind, outcome: ConfirmationOutcome, chosen: str
) -> None:
    confirm = _Confirm(outcome)

    replies, _ = await _run_roles(tmp_path, _AsksFor(kind), {'Doer': _ALL_TOOLS}, confirm=confirm)

    assert replies['Doer'] == f'perm:{chosen}'
    assert confirm.kinds == [kind]


@pytest.mark.parametrize(
    ('kind', 'declared'),
    [
        ('edit', ['Read', 'Bash']),
        ('delete', ['Read', 'Bash']),
        ('move', ['Read', 'Bash']),
        ('execute', ['Read', 'Write']),
    ],
)
async def test_a_write_or_execute_permission_is_refused_unasked_when_the_role_lacks_the_tool(
    tmp_path: Path, kind: ToolKind, declared: list[str]
) -> None:
    confirm = _Confirm(ConfirmationOutcome.APPROVED)

    replies, _ = await _run_roles(tmp_path, _AsksFor(kind), {'Peek': declared}, confirm=confirm)

    assert replies['Peek'] == 'perm:no'
    assert confirm.kinds == []


@pytest.mark.parametrize('kind', ['edit', 'delete', 'move', 'execute'])
async def test_a_read_only_run_refuses_write_and_execute_permissions_unasked(
    tmp_path: Path, kind: ToolKind
) -> None:
    confirm = _Confirm(ConfirmationOutcome.APPROVED)

    replies, _ = await _run_roles(
        tmp_path, _AsksFor(kind), {'Doer': _ALL_TOOLS}, read_only=True, confirm=confirm
    )

    assert replies['Doer'] == 'perm:no'
    assert confirm.kinds == []


class _InItsCwd(_Reads):
    """在 session/new 交来的目录里写 iso.md、读 notes.md，用绝对路径（真的 ACP Agent 都这样）。"""

    def __init__(self) -> None:
        super().__init__()
        self.cwd = ''

    async def new_session(self, cwd: str, **kwargs: object) -> NewSessionResponse:
        del kwargs
        self.cwd = cwd
        return NewSessionResponse(session_id='sess')

    async def prompt(
        self, session_id: str, prompt: list[object], **kwargs: object
    ) -> PromptResponse:
        del prompt, kwargs
        await self.conn.write_text_file(
            session_id=session_id, path=os.path.join(self.cwd, 'iso.md'), content='inside'
        )
        got = await self.conn.read_text_file(
            session_id=session_id, path=os.path.join(self.cwd, 'notes.md')
        )
        await self.conn.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update='agent_message_chunk',
                content=TextContentBlock(type='text', text=got.content),
            ),
        )
        return PromptResponse(stop_reason='end_turn')


async def test_inside_an_isolated_copy_reads_and_writes_stay_out_of_the_shared_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    (root / 'notes.md').write_text('alpha copy\n', encoding='utf-8')
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    local = LocalWorkspace(str(root), shell='/bin/sh', env={'PATH': os.environ.get('PATH', '')})
    router = WorkspaceRouter(
        local,
        taint=FileTaint(root),
        isolator=LocalIsolator(shell='/bin/sh', env={}, git=None, scratch=str(scratch)),
    )
    copy = router.isolate('alpha', 'snapshot')
    assert copy is not None
    (root / 'notes.md').write_text('shared after the copy\n', encoding='utf-8')
    runtime = Runtime(workspace=router)
    agent = _InItsCwd()
    model = AcpAgentModel(
        'iso',
        connect=_link(agent),
        tools=_tools(router),
        policy=_policy(root),
        cwd=str(root),
        runtime=runtime,
    )

    async def as_alpha() -> ContextBlock:
        _DRIVING_AGENT.set('alpha')
        return await model.create([ContextBlock(role='user', content='go')])

    try:
        reply = await asyncio.create_task(as_alpha())
        assert agent.cwd == str(copy.location)
        assert reply.content == 'alpha copy'
        assert (copy.location / 'iso.md').read_text(encoding='utf-8') == 'inside'
        assert not (root / 'iso.md').exists()
    finally:
        await model.aclose()
        await runtime.shutdown()
        await router.close()
