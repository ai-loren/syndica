"""会话装配时连上声明的 MCP 服务；没有服务时不导入额外依赖。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from io import StringIO
from pathlib import Path

import pytest
from capture_http import capturing

from syndica.__main__ import _open_mcp
from syndica.backends.mcp.client import ACCEPTED_PROTOCOL_VERSIONS, connect_http, connect_stdio
from syndica.backends.mcp.module import assign_roles, mcp_module
from syndica.backends.mcp.tools import McpJudge
from syndica.cli.config import config_from_text
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, RunRequest
from syndica.cli.mcp_session import McpPorts, open_mcp_clients
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import (
    ConfigError,
    DangerJudge,
    FileTraceStore,
    RuleRedactor,
    ScriptedLLM,
    ScriptedTurn,
    SpanKind,
    ToolCall,
    TrustLevel,
)

_PORTS = McpPorts(
    connect_stdio=connect_stdio,
    connect_http=connect_http,
    assign_roles=assign_roles,
    make_module=mcp_module,
    wrap_judge=McpJudge,
)

_SERVER = """\
import asyncio

import mcp_types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server


async def on_list_tools(ctx, params):
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="echo",
                description="echo",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]
    )


async def on_call_tool(ctx, params):
    text = (params.arguments or {}).get("text", "")
    return types.CallToolResult(content=[types.TextContent(type="text", text=str(text))])


async def main():
    server = Server("demo", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


asyncio.run(main())
"""


def _config(path: Path, body: str):
    path.write_text(body, encoding='utf-8')
    return config_from_text(path, path.read_text(encoding='utf-8'))


async def test_no_mcp_servers_skips_the_client(tmp_path: Path):
    config = _config(tmp_path / '.syndica.yaml', 'version: 1\n')
    opened = await open_mcp_clients(
        config,
        {},
        cwd=str(tmp_path),
        redactor=RuleRedactor(),
        environ={'HOME': str(tmp_path)},
        judge=DangerJudge(),
        workspace=str(tmp_path),
        ports=_PORTS,
    )
    assert opened is None


async def test_a_declared_stdio_server_is_called_then_closed(tmp_path: Path):
    script = tmp_path / 'srv.py'
    script.write_text(_SERVER, encoding='utf-8')
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    config = _config(
        workspace / '.syndica.yaml',
        'version: 1\n'
        'interop:\n'
        '  mcp:\n'
        '    servers:\n'
        '      - name: files\n'
        f'        command: [{sys.executable!r}, {str(script)!r}]\n'
        '        roles: [Worker]\n',
    )
    opened = await open_mcp_clients(
        config,
        {'PATH': os.environ.get('PATH', '')},
        cwd=str(workspace),
        redactor=RuleRedactor(),
        environ={'HOME': str(tmp_path / 'home'), 'PATH': os.environ.get('PATH', '')},
        judge=DangerJudge(),
        workspace=str(workspace),
        ports=_PORTS,
    )
    assert opened is not None
    try:
        tool = opened.module.tool_factories[0]
        assert opened.judge.rules[-1].rule_id == 'mcp.external_tool'
        from types import SimpleNamespace
        from typing import Any, cast

        agent = SimpleNamespace(options=SimpleNamespace(name='Worker'), agent_id='w')
        tools = tool(cast(Any, SimpleNamespace()), cast(Any, agent), {})
        result = await tools['files__echo'].execute(text='ping')
        assert result.output == 'ping'
        assert result.trust.name == 'UNTRUSTED'
    finally:
        await opened.aclose()


def _user_credentials(home: Path, text: str) -> None:
    creds = home / '.config' / 'syndica' / 'interop.yaml'
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(text, encoding='utf-8')
    os.chmod(creds, 0o600)


async def test_a_missing_bearer_names_the_variable(tmp_path: Path):
    home = tmp_path / 'home'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    _user_credentials(
        home,
        'credentials:\n'
        '  mcp:tracker:\n'
        '    url: http://127.0.0.1:9/mcp\n'
        '    bearer_env: TRACKER_TOKEN\n',
    )
    config = _config(
        workspace / '.syndica.yaml',
        'version: 1\n'
        'interop:\n'
        '  mcp:\n'
        '    servers:\n'
        '      - name: tracker\n'
        '        url: http://127.0.0.1:9/mcp\n'
        '        roles: [Worker]\n',
    )
    with pytest.raises(ConfigError, match='TRACKER_TOKEN') as caught:
        await open_mcp_clients(
            config,
            {},
            cwd=str(workspace),
            redactor=RuleRedactor(),
            environ={'HOME': str(home)},
            judge=DangerJudge(),
            workspace=str(workspace),
            ports=_PORTS,
        )
    assert caught.value.expected
    assert '没有设置' in caught.value.received
    assert 'bearer_env' in caught.value.next_action


_ENV_SERVER = """\
import asyncio
import os

import mcp_types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server


async def on_list_tools(ctx, params):
    return types.ListToolsResult(
        tools=[types.Tool(name="env", description="env", input_schema={"type": "object"})]
    )


async def on_call_tool(ctx, params):
    value = os.environ.get("FILES_TOKEN", "<absent>")
    return types.CallToolResult(content=[types.TextContent(type="text", text=value)])


async def main():
    server = Server("env", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


asyncio.run(main())
"""

_FILES_SECRET = 'files-secret-0123456789'
_TRACKER_SECRET = 'tracker-secret-0123456789'


async def test_a_stdio_server_whose_command_matches_the_pin_sees_the_pinned_variable(
    tmp_path: Path,
):
    script = tmp_path / 'env_srv.py'
    script.write_text(_ENV_SERVER, encoding='utf-8')
    home = tmp_path / 'home'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    command = json.dumps([sys.executable, str(script)])
    _user_credentials(
        home, f'credentials:\n  mcp:files:\n    command: {command}\n    env: [FILES_TOKEN]\n'
    )
    config = _config(
        workspace / '.syndica.yaml',
        'version: 1\n'
        'interop:\n'
        '  mcp:\n'
        '    servers:\n'
        '      - name: files\n'
        f'        command: {command}\n'
        '        roles: [Worker]\n',
    )
    path = os.environ.get('PATH', '')
    opened = await open_mcp_clients(
        config,
        {'PATH': path},
        cwd=str(workspace),
        redactor=RuleRedactor(),
        environ={'HOME': str(home), 'PATH': path, 'FILES_TOKEN': _FILES_SECRET},
        judge=DangerJudge(),
        workspace=str(workspace),
        ports=_PORTS,
    )
    assert opened is not None
    try:
        from types import SimpleNamespace
        from typing import Any, cast

        agent = SimpleNamespace(options=SimpleNamespace(name='Worker'), agent_id='w')
        tools = opened.module.tool_factories[0](cast(Any, SimpleNamespace()), cast(Any, agent), {})
        result = await tools['files__env'].execute()
        assert result.output == _FILES_SECRET
    finally:
        await opened.aclose()


class _Link:
    def __init__(self, name: str) -> None:
        self.name = name
        self.tools: dict[str, object] = {}

    async def __aenter__(self) -> _Link:
        return self

    async def aclose(self) -> None:
        return None


class _Connector:
    """记下组合根交给 backend 的每一次连接：名字、目的地、环境与 bearer。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object, dict[str, object]]] = []

    def stdio(self, name: str, command: object, **kwargs: object) -> _Link:
        self.calls.append(('stdio', name, command, kwargs))
        return _Link(name)

    def http(self, name: str, url: object, **kwargs: object) -> _Link:
        self.calls.append(('http', name, url, kwargs))
        return _Link(name)

    def ports(self) -> McpPorts:
        return McpPorts(
            connect_stdio=self.stdio,
            connect_http=self.http,
            assign_roles=assign_roles,
            make_module=mcp_module,
            wrap_judge=McpJudge,
        )


async def _open_with(connector: _Connector, workspace: Path, home: Path, body: str):
    config = _config(workspace / '.syndica.yaml', f'version: 1\ninterop:\n  mcp:\n{body}')
    return await open_mcp_clients(
        config,
        {'PATH': '/usr/bin'},
        cwd=str(workspace),
        redactor=RuleRedactor(),
        environ={
            'HOME': str(home),
            'FILES_TOKEN': _FILES_SECRET,
            'TRACKER_TOKEN': _TRACKER_SECRET,
        },
        judge=DangerJudge(),
        workspace=str(workspace),
        ports=connector.ports(),
    )


async def test_a_stdio_server_whose_command_differs_from_the_pin_is_never_started(
    tmp_path: Path,
):
    home = tmp_path / 'home'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    _user_credentials(
        home, 'credentials:\n  mcp:files:\n    command: [files-server]\n    env: [FILES_TOKEN]\n'
    )
    connector = _Connector()

    with pytest.raises(ConfigError) as caught:
        await _open_with(
            connector,
            workspace,
            home,
            '    servers:\n      - name: files\n        command: [/tmp/evil, files-server]\n',
        )

    assert connector.calls == []
    assert 'mcp:files' in caught.value.expected
    assert 'FILES_TOKEN' in caught.value.expected
    assert 'command ["files-server"]' in caught.value.expected
    assert 'command ["/tmp/evil", "files-server"]' in caught.value.received
    assert 'mcp:files 的 command' in caught.value.next_action


async def test_a_stdio_server_whose_command_matches_the_pin_is_handed_the_variable(
    tmp_path: Path,
):
    home = tmp_path / 'home'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    _user_credentials(
        home, 'credentials:\n  mcp:files:\n    command: [files-server]\n    env: [FILES_TOKEN]\n'
    )
    connector = _Connector()

    opened = await _open_with(
        connector,
        workspace,
        home,
        '    servers:\n      - name: files\n        command: [files-server]\n',
    )

    assert opened is not None
    [(kind, name, command, kwargs)] = connector.calls
    assert (kind, name, command) == ('stdio', 'files', ('files-server',))
    assert kwargs['env'] == {'PATH': '/usr/bin', 'FILES_TOKEN': _FILES_SECRET}


async def test_an_http_server_whose_url_matches_the_pin_is_handed_the_bearer(tmp_path: Path):
    home = tmp_path / 'home'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    _user_credentials(
        home,
        'credentials:\n'
        '  mcp:tracker:\n'
        '    url: https://mcp.example.com/mcp\n'
        '    bearer_env: TRACKER_TOKEN\n',
    )
    connector = _Connector()

    opened = await _open_with(
        connector,
        workspace,
        home,
        '    servers:\n      - name: tracker\n        url: https://mcp.example.com/mcp\n',
    )

    assert opened is not None
    [(kind, name, url, kwargs)] = connector.calls
    assert (kind, name, url) == ('http', 'tracker', 'https://mcp.example.com/mcp')
    assert kwargs['bearer'] == _TRACKER_SECRET


async def test_one_mismatched_pin_starts_none_of_the_servers(tmp_path: Path):
    home = tmp_path / 'home'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    _user_credentials(
        home,
        'credentials:\n'
        '  mcp:tracker:\n'
        '    url: https://mcp.example.com/mcp\n'
        '    bearer_env: TRACKER_TOKEN\n',
    )
    connector = _Connector()

    with pytest.raises(ConfigError, match='mcp:tracker'):
        await _open_with(
            connector,
            workspace,
            home,
            '    servers:\n'
            '      - name: files\n'
            '        command: [files-server]\n'
            '      - name: tracker\n'
            '        url: https://attacker.example/mcp\n',
        )

    assert connector.calls == []


async def test_a_project_url_other_than_the_pin_never_receives_a_request(tmp_path: Path):
    home = tmp_path / 'home'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    with capturing() as pinned, capturing() as attacker:
        _user_credentials(
            home,
            'credentials:\n'
            '  mcp:tracker:\n'
            f'    url: {pinned.url}/mcp\n'
            '    bearer_env: TRACKER_TOKEN\n',
        )
        config = _config(
            workspace / '.syndica.yaml',
            'version: 1\n'
            'interop:\n'
            '  mcp:\n'
            '    servers:\n'
            '      - name: tracker\n'
            f'        url: {attacker.url}/mcp\n'
            '        roles: [Worker]\n',
        )

        with pytest.raises(ConfigError) as caught:
            await open_mcp_clients(
                config,
                {},
                cwd=str(workspace),
                redactor=RuleRedactor(),
                environ={'HOME': str(home), 'TRACKER_TOKEN': _TRACKER_SECRET},
                judge=DangerJudge(),
                workspace=str(workspace),
                ports=_PORTS,
            )

        assert attacker.seen == []
        assert pinned.seen == []
    assert f'url {pinned.url}/mcp' in caught.value.expected
    assert f'url {attacker.url}/mcp' in caught.value.received


async def test_the_pinned_url_receives_the_bearer_on_every_request(tmp_path: Path):
    home = tmp_path / 'home'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    with capturing() as pinned:
        _user_credentials(
            home,
            'credentials:\n'
            '  mcp:tracker:\n'
            f'    url: {pinned.url}/mcp\n'
            '    bearer_env: TRACKER_TOKEN\n',
        )
        config = _config(
            workspace / '.syndica.yaml',
            'version: 1\n'
            'interop:\n'
            '  mcp:\n'
            '    servers:\n'
            '      - name: tracker\n'
            f'        url: {pinned.url}/mcp\n'
            '        roles: [Worker]\n',
        )

        with contextlib.suppress(Exception):
            await open_mcp_clients(
                config,
                {},
                cwd=str(workspace),
                redactor=RuleRedactor(),
                environ={'HOME': str(home), 'TRACKER_TOKEN': _TRACKER_SECRET},
                judge=DangerJudge(),
                workspace=str(workspace),
                ports=_PORTS,
            )

        assert pinned.seen
        assert {seen.authorization for seen in pinned.seen} == {f'Bearer {_TRACKER_SECRET}'}


def _spans(root: Path):
    store = FileTraceStore(root / '.syndica' / 'traces')

    async def read():
        found = []
        for header in await store.traces():
            found.extend(await store.spans(header.trace_id))
        return found

    return asyncio.run(read())


def test_doctor_lists_the_negotiated_version(tmp_path: Path):
    script = tmp_path / 'srv.py'
    script.write_text(_SERVER, encoding='utf-8')
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    config = _config(
        workspace / '.syndica.yaml',
        'version: 1\n'
        'interop:\n'
        '  mcp:\n'
        '    servers:\n'
        '      - name: files\n'
        f'        command: [{sys.executable!r}, {str(script)!r}]\n'
        '        roles: [Worker]\n',
    )
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: ScriptedLLM([ScriptedTurn(content='idle')]),
        workspace_root=workspace,
        mcp_opener=_open_mcp,
    )
    try:
        outcome = host.doctor(str(config.source))
    finally:
        host.close()
    assert outcome.exit_code == 0
    line = next(item for item in outcome.output.splitlines() if item.startswith('mcp: ok files '))
    version = line.removeprefix('mcp: ok files ')
    assert version in ACCEPTED_PROTOCOL_VERSIONS


def test_a_scripted_worker_records_the_external_call_and_blocks_the_next_high_risk_one(
    tmp_path: Path,
):
    script = tmp_path / 'srv.py'
    script.write_text(_SERVER, encoding='utf-8')
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    config = _config(
        workspace / '.syndica.yaml',
        'version: 1\n'
        'interop:\n'
        '  mcp:\n'
        '    servers:\n'
        '      - name: files\n'
        f'        command: [{sys.executable!r}, {str(script)!r}]\n'
        '        roles: [Worker]\n',
    )
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
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='e2',
                        name='files__echo',
                        arguments=json.dumps({'text': 'again'}),
                    )
                ]
            ),
            ScriptedTurn(content='done'),
        ]
    )
    stdout = StringIO()
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO('approve\n'), stdout=stdout, stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=workspace,
        mcp_opener=_open_mcp,
    )
    try:
        answer = host.run_request(
            RunRequest(prompt='work', agent='Worker', config=str(config.source))
        )
    finally:
        host.close()
    assert answer == 'done'
    blocks = {
        block.tool_call_id: block
        for call in llm.calls
        for block in call
        if block.role == 'tool' and block.tool_call_id
    }
    assert blocks['e1'].content == 'ping'
    assert blocks['e1'].trust is TrustLevel.UNTRUSTED
    assert 'mcp.external_tool' in str(blocks['e2'].content)
    assert '已被阻断' in str(blocks['e2'].content)
    spans = _spans(workspace)
    calls = [
        span
        for span in spans
        if span.kind is SpanKind.TOOL_CALL and span.attributes.get('tool.name') == 'files__echo'
    ]
    assert [span.attributes.get('tool.outcome') for span in calls] == ['ok', 'blocked']
    assert calls[1].attributes.get('danger.decided_by') == 'flag'
    assert calls[1].attributes.get('danger.provenance') == 'untrusted'
    versions = [
        span.attributes.get('module.version')
        for span in spans
        if span.kind is SpanKind.MODULE and span.attributes.get('module.name') == 'mcp'
    ]
    assert versions
    recorded = versions[0]
    assert isinstance(recorded, str)
    name, _, version = recorded.partition('=')
    assert name == 'files'
    assert version in ACCEPTED_PROTOCOL_VERSIONS
