"""会话装配时连上声明的 MCP 服务；没有服务时不导入额外依赖。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from io import StringIO
from pathlib import Path

import pytest

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


async def test_a_missing_bearer_names_the_variable(tmp_path: Path):
    home = tmp_path / 'home'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    creds = home / '.config' / 'syndica' / 'interop.yaml'
    creds.parent.mkdir(parents=True)
    creds.write_text(
        'credentials:\n  mcp:tracker: {bearer_env: TRACKER_TOKEN}\n',
        encoding='utf-8',
    )
    os.chmod(creds, 0o600)
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
