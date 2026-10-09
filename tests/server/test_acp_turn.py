"""ACP 的一轮提示跑在真会话上：工具调用和助手文字都推回去。"""

from __future__ import annotations

import asyncio
import json
import sys
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from acp.exceptions import RequestError
from acp.schema import EnvVariable, McpServerStdio, TextContentBlock

from syndica.__main__ import _open_mcp
from syndica.cli.config import config_from_text
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import (
    AgentStatus,
    ConfigError,
    ContextBlock,
    RuleRedactor,
    ScriptedLLM,
    ScriptedTurn,
    ToolCall,
    TrustLevel,
)
from syndica.serve import with_editor_mcp
from syndica.server.acp_agent import SyndicaAcp, TurnBinding

_CONFIG = (
    'version: 1\n'
    'agents:\n'
    '  - name: Worker\n'
    '    description: 干活\n'
    '    system_prompt: 读文件\n'
    '    tools: [Read]\n'
)


class _Recording(SyndicaAcp):
    def __init__(
        self, open_turn: object, roles: tuple[tuple[str, str], ...] = (('Worker', '干活'),)
    ) -> None:
        super().__init__(
            roles,
            lambda _cwd: None,
            open_turn=open_turn,  # pyright: ignore[reportArgumentType]
            redactor=RuleRedactor(),
        )
        self.messages: list[str] = []
        self.users: list[str] = []
        self.tools: list[tuple[str, str]] = []
        self.permissions: list[str] = []
        self.decision = 'approve'
        self.hold: asyncio.Event | None = None
        self.entered: asyncio.Event | None = None

    async def _say(self, session_id: str, text: str) -> None:
        del session_id
        self.messages.append(text)

    async def _user(self, session_id: str, text: str) -> None:
        del session_id
        self.users.append(text)

    async def _tool(
        self,
        session_id: str,
        tool_call_id: str,
        name: str,
        status: str,
        raw_input: str,
        raw_output: str,
    ) -> None:
        del session_id, tool_call_id, raw_input, raw_output
        self.tools.append((name, status))

    async def _permission(
        self, session_id: str, tool_call_id: str, name: str, arguments: str
    ) -> str:
        del session_id, tool_call_id, arguments
        self.permissions.append(name)
        if self.entered is not None:
            self.entered.set()
        if self.hold is not None:
            await self.hold.wait()
        return self.decision


def test_a_scripted_read_is_a_tool_call_and_then_the_answer(tmp_path: Path) -> None:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'notes.md').write_text('alpha marker\n', encoding='utf-8')
    config = workspace / '.syndica.yaml'
    config.write_text(_CONFIG, encoding='utf-8')
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(id='r1', name='Read', arguments=json.dumps({'path': 'notes.md'}))
                ]
            ),
            ScriptedTurn(content='saw alpha'),
        ]
    )
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=workspace,
    )
    opened: list[object] = []

    async def open_turn(cwd: str, mode: str, servers: tuple[object, ...] = ()) -> TurnBinding:
        del cwd, servers
        parts = await host.assemble_session(
            str(config),
            max_turns=4,
            trace=None,
            argv=('syndica', 'acp'),
            trusted_output=(),
            role=mode,
        )
        opened.append(parts)
        return TurnBinding(parts.runtime, parts.agent_id, parts.subscription)

    agent = _Recording(open_turn)

    async def body() -> None:
        session_id = ''
        try:
            session = await agent.new_session(str(workspace))
            session_id = session.session_id
            result = await agent.prompt(
                session.session_id,
                [TextContentBlock(type='text', text='read notes')],
            )
            assert result.stop_reason == 'end_turn'
            assert ('Read', 'in_progress') in agent.tools
            assert ('', 'completed') in agent.tools or ('Read', 'completed') in agent.tools
            assert any('saw alpha' in text for text in agent.messages), agent.messages
            await agent.load_session(str(workspace), session_id)
            assert any('read notes' in text for text in agent.users)
            assert sum('saw alpha' in text for text in agent.messages) >= 2
        finally:
            stored = agent._sessions.get(session_id)  # pyright: ignore[reportPrivateUsage]
            bridge = None if stored is None else stored.bridge
            if bridge is not None:
                await bridge.close()
            for parts in opened:
                await host.close_session(parts, None)  # pyright: ignore[reportArgumentType]

    try:
        asyncio.run(body())
    finally:
        host.close()


def _config(tools: str) -> str:
    return (
        'version: 1\n'
        'agents:\n'
        '  - name: Worker\n'
        '    description: 干活\n'
        '    system_prompt: 干活\n'
        f'    tools: [{tools}]\n'
    )


def _drive(
    tmp_path: Path, tools: str, turns: list[ScriptedTurn]
) -> tuple[RuntimeCliHost, _Recording, list[object]]:
    workspace = tmp_path / 'ws'
    workspace.mkdir(parents=True)
    (workspace / 'doomed.txt').write_text('remove me', encoding='utf-8')
    config = workspace / '.syndica.yaml'
    config.write_text(_config(tools), encoding='utf-8')
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: ScriptedLLM(turns),
        workspace_root=workspace,
    )
    opened: list[object] = []

    async def open_turn(cwd: str, mode: str, servers: tuple[object, ...] = ()) -> TurnBinding:
        del cwd, servers
        parts = await host.assemble_session(
            str(config),
            max_turns=6,
            trace=None,
            argv=('syndica', 'acp'),
            trusted_output=(),
            role=mode,
        )
        opened.append(parts)
        return TurnBinding(parts.runtime, parts.agent_id, parts.subscription)

    return host, _Recording(open_turn), opened


async def _finish(
    host: RuntimeCliHost, agent: _Recording, session_id: str, opened: list[object]
) -> None:
    stored = agent._sessions.get(session_id)  # pyright: ignore[reportPrivateUsage]
    bridge = None if stored is None else stored.bridge
    if bridge is not None:
        await bridge.close()
    for parts in opened:
        await host.close_session(parts, None)  # pyright: ignore[reportArgumentType]


def test_an_approved_bash_runs_and_a_rejected_one_does_not(tmp_path: Path) -> None:
    command = json.dumps({'command': 'cat /etc/hosts'})
    host, agent, opened = _drive(
        tmp_path,
        'Bash',
        [
            ScriptedTurn(tool_calls=[ToolCall(id='b1', name='Bash', arguments=command)]),
            ScriptedTurn(content='after'),
        ],
    )
    agent.decision = 'approve'

    async def body() -> None:
        session_id = ''
        try:
            session = await agent.new_session(str(tmp_path / 'ws'))
            session_id = session.session_id
            result = await agent.prompt(
                session_id, [TextContentBlock(type='text', text='delete it')]
            )
            assert result.stop_reason == 'end_turn'
            assert agent.permissions == ['Bash']
            assert ('Bash', 'in_progress') in agent.tools
            assert ('', 'completed') in agent.tools
            assert any('after' in text for text in agent.messages)
        finally:
            await _finish(host, agent, session_id, opened)

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_rejected_confirmation_fails_the_tool_call(tmp_path: Path) -> None:
    command = json.dumps({'command': 'cat /etc/hosts'})
    host, agent, opened = _drive(
        tmp_path / 'reject',
        'Bash',
        [
            ScriptedTurn(tool_calls=[ToolCall(id='b1', name='Bash', arguments=command)]),
            ScriptedTurn(content='kept'),
        ],
    )
    agent.decision = 'reject'

    async def body() -> None:
        session_id = ''
        try:
            session = await agent.new_session(str(tmp_path / 'reject' / 'ws'))
            session_id = session.session_id
            result = await agent.prompt(session_id, [TextContentBlock(type='text', text='delete')])
            assert result.stop_reason == 'end_turn'
            assert ('', 'failed') in agent.tools
            assert any('kept' in text for text in agent.messages)
        finally:
            await _finish(host, agent, session_id, opened)

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_a_question_ends_the_prompt_and_the_next_prompt_is_the_answer(tmp_path: Path) -> None:
    host, agent, opened = _drive(
        tmp_path / 'ask',
        'Read',
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
        ],
    )

    async def body() -> None:
        session_id = ''
        try:
            session = await agent.new_session(str(tmp_path / 'ask' / 'ws'))
            session_id = session.session_id
            asked = await agent.prompt(session_id, [TextContentBlock(type='text', text='ask')])
            assert asked.stop_reason == 'end_turn'
            asked_text = '\n'.join(agent.messages)
            assert '选哪个？' in asked_text and '红' in asked_text and '蓝' in asked_text
            with pytest.raises(RequestError, match='红'):
                await agent.prompt(session_id, [TextContentBlock(type='text', text='绿')])
            answered = await agent.prompt(session_id, [TextContentBlock(type='text', text='红')])
            assert answered.stop_reason == 'end_turn'
            assert any('picked red' in text for text in agent.messages)
        finally:
            await _finish(host, agent, session_id, opened)

    try:
        asyncio.run(body())
    finally:
        host.close()


def _default_host(
    tmp_path: Path, llm: ScriptedLLM
) -> tuple[RuntimeCliHost, _Recording, list[object], Path]:
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
    opened: list[object] = []

    async def open_turn(cwd: str, mode: str, servers: tuple[object, ...] = ()) -> TurnBinding:
        del cwd, servers
        parts = await host.assemble_session(
            str(config),
            max_turns=8,
            trace=None,
            argv=('syndica', 'acp'),
            trusted_output=(),
            role=mode,
        )
        opened.append(parts)
        return TurnBinding(parts.runtime, parts.agent_id, parts.subscription)

    agent = _Recording(open_turn, (('Orchestrator', '派活'), ('Worker', '干活')))
    return host, agent, opened, workspace


def test_a_delegated_prompt_returns_after_the_root_with_only_the_root_answer(
    tmp_path: Path,
) -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='d1',
                        name='Delegate',
                        arguments=json.dumps({'agent': 'Worker', 'query': 'do it'}),
                    )
                ]
            ),
            ScriptedTurn(content='CHILD-ANSWER'),
            ScriptedTurn(content='ROOT-FINAL'),
        ]
    )
    host, agent, opened, workspace = _default_host(tmp_path, llm)

    async def body() -> None:
        session_id = ''
        try:
            session = await agent.new_session(str(workspace))
            session_id = session.session_id
            async with asyncio.timeout(20):
                result = await agent.prompt(session_id, [TextContentBlock(type='text', text='go')])
            at_return = list(agent.messages)
            assert result.stop_reason == 'end_turn'
            assert at_return == ['ROOT-FINAL']
        finally:
            await _finish(host, agent, session_id, opened)

    try:
        asyncio.run(body())
    finally:
        host.close()


async def _until_zombie(parts: Any) -> None:
    """等根 Agent 拆完这个周期再发下一条：拆邮箱期间送达的提示会随邮箱一起丢掉。"""
    async with asyncio.timeout(20):
        while True:
            info = await parts.runtime.find(parts.agent_id)
            if info is not None and info.status is AgentStatus.ZOMBIE:
                return
            await asyncio.sleep(0.01)


class _FailsOnce(ScriptedLLM):
    def __init__(self, script: list[ScriptedTurn]) -> None:
        super().__init__(script)
        self.failed = False

    async def create(self, messages: list[ContextBlock], **kwargs: object) -> ContextBlock:
        if not self.failed:
            self.failed = True
            raise RuntimeError('model is down')
        return await super().create(messages, **kwargs)


def test_a_root_error_fails_one_prompt_and_the_next_prompt_is_answered(tmp_path: Path) -> None:
    host, agent, opened, workspace = _default_host(
        tmp_path, _FailsOnce([ScriptedTurn(content='recovered')])
    )

    async def body() -> None:
        session_id = ''
        try:
            session = await agent.new_session(str(workspace))
            session_id = session.session_id
            await agent.set_session_mode(session_id, 'Worker')
            with pytest.raises(RequestError) as failed:
                async with asyncio.timeout(20):
                    await agent.prompt(session_id, [TextContentBlock(type='text', text='one')])
            assert isinstance(failed.value.data, dict)
            assert failed.value.data['next_action']
            [parts] = opened
            await _until_zombie(parts)
            async with asyncio.timeout(20):
                again = await agent.prompt(session_id, [TextContentBlock(type='text', text='two')])
            assert again.stop_reason == 'end_turn'
            assert agent.messages == ['recovered']
        finally:
            await _finish(host, agent, session_id, opened)

    try:
        asyncio.run(body())
    finally:
        host.close()


def test_cancel_returns_while_a_confirmation_is_waiting(tmp_path: Path) -> None:
    command = json.dumps({'command': 'cat /etc/hosts'})
    host, agent, opened = _drive(
        tmp_path / 'cancel',
        'Bash',
        [ScriptedTurn(tool_calls=[ToolCall(id='b1', name='Bash', arguments=command)])],
    )
    hold = asyncio.Event()
    entered = asyncio.Event()
    agent.hold = hold
    agent.entered = entered

    async def body() -> None:
        session_id = ''
        try:
            session = await agent.new_session(str(tmp_path / 'cancel' / 'ws'))
            session_id = session.session_id
            prompt = asyncio.create_task(
                agent.prompt(session_id, [TextContentBlock(type='text', text='delete')])
            )
            await entered.wait()
            await agent.cancel(session_id)
            result = await prompt
            assert result.stop_reason == 'cancelled'
        finally:
            hold.set()
            await _finish(host, agent, session_id, opened)

    try:
        asyncio.run(body())
    finally:
        host.close()


_ECHO = """\
import asyncio

import mcp_types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server


async def on_list_tools(ctx, params):
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name='echo',
                description='echo',
                input_schema={
                    'type': 'object',
                    'properties': {'text': {'type': 'string'}},
                    'required': ['text'],
                },
            )
        ]
    )


async def on_call_tool(ctx, params):
    text = (params.arguments or {}).get('text', '')
    return types.CallToolResult(content=[types.TextContent(type='text', text=str(text))])


async def main():
    server = Server('demo', on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


asyncio.run(main())
"""


def test_an_editor_mcp_server_refuses_a_secret_env_name(tmp_path: Path) -> None:
    text = _config('Read')
    path = tmp_path / '.syndica.yaml'
    path.write_text(text, encoding='utf-8')
    config = config_from_text(path, text)
    server = McpServerStdio(
        name='files',
        command=sys.executable,
        args=['srv.py'],
        env=[EnvVariable(name='API_KEY', value='sekret')],
    )
    with pytest.raises(ConfigError, match='API_KEY'):
        with_editor_mcp(config, (server,), 'Worker')


def test_an_editor_stdio_mcp_tool_runs_untrusted(tmp_path: Path) -> None:
    script = tmp_path / 'srv.py'
    script.write_text(_ECHO, encoding='utf-8')
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    config_path = workspace / '.syndica.yaml'
    config_path.write_text(_config('Read'), encoding='utf-8')
    loaded = config_from_text(config_path, config_path.read_text(encoding='utf-8'))
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='e1',
                        name='files__echo',
                        arguments=json.dumps({'text': 'ping'}),
                    )
                ]
            ),
            ScriptedTurn(content='echoed'),
        ]
    )
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=workspace,
        mcp_opener=_open_mcp,
    )
    opened: list[object] = []

    async def open_turn(cwd: str, mode: str, servers: tuple[object, ...] = ()) -> TurnBinding:
        del cwd
        parts = await host.assemble_session(
            str(config_path),
            max_turns=4,
            trace=None,
            argv=('syndica', 'acp'),
            trusted_output=(),
            role=mode,
            config=with_editor_mcp(loaded, servers, mode),
        )
        opened.append(parts)
        return TurnBinding(parts.runtime, parts.agent_id, parts.subscription)

    agent = _Recording(open_turn)
    agent.decision = 'approve'

    async def body() -> None:
        session_id = ''
        try:
            session = await agent.new_session(
                str(workspace),
                mcp_servers=[
                    McpServerStdio(
                        name='files',
                        command=sys.executable,
                        args=[str(script)],
                        env=[],
                    )
                ],
            )
            session_id = session.session_id
            result = await agent.prompt(
                session_id, [TextContentBlock(type='text', text='echo ping')]
            )
            assert result.stop_reason == 'end_turn'
            blocks = [
                block
                for call in llm.calls
                for block in call
                if block.role == 'tool' and block.tool_call_id == 'e1'
            ]
            assert blocks and blocks[-1].content == 'ping'
            assert blocks[-1].trust is TrustLevel.UNTRUSTED
        finally:
            await _finish(host, agent, session_id, opened)

    try:
        asyncio.run(body())
    finally:
        host.close()
