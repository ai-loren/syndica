"""MCP server：工具清单、call_external、产物脱敏、异常不回显。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from io import StringIO
from pathlib import Path

import mcp_types as types
import pytest
from mcp.client.client import Client
from mcp.server.lowlevel.server import Server

from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO
from syndica.cli.policy import dev_guard_judge
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import (
    AllowAll,
    ArtifactRef,
    BlockHighRisk,
    ConfigError,
    DangerJudge,
    ExternalCallPolicy,
    InMemoryTraceStore,
    RuleRedactor,
    ScriptedLLM,
    Span,
    Tool,
    ToolResult,
    TrustLevel,
)
from syndica.kernel.file_taint import FileTaint
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.tools.local import Bash, Glob, Grep, Read, Write
from syndica.kernel.workspace_router import WorkspaceRouter
from syndica.server.mcp_serve import (
    McpPrompt,
    McpResource,
    McpTraces,
    build_mcp_server,
    read_artifact_bytes,
    resources_of,
    select_serve_tools,
)

_SECRET = 'sekret-VALUE-918273'
_READ_ONLY = frozenset({'Read', 'Grep', 'Glob'})


class _Boom(Tool):
    __tool_name__ = 'Boom'
    __tool_command_args__ = ()
    __tool_external_input__ = True

    async def execute(self) -> ToolResult:
        """会失败的工具。"""
        raise RuntimeError(f'blew up {_SECRET}')


def _policy(root: Path) -> ExternalCallPolicy:
    return ExternalCallPolicy(
        judge=DangerJudge(),
        policy=BlockHighRisk(),
        guard=dev_guard_judge(root),
    )


def _server(
    portal: WorkspaceRouter,
    root: Path,
    *,
    extra: dict[str, Tool] | None = None,
    traces: McpTraces | None = None,
) -> Server:
    tools: dict[str, Tool] = {
        'Read': Read(portal),
        'Grep': Grep(portal),
        'Glob': Glob(portal),
    }
    if extra:
        tools.update(extra)
    return build_mcp_server(
        tools,
        policy=_policy(root),
        read_only=_READ_ONLY,
        redactor=RuleRedactor(secrets=(_SECRET,)),
        prompts=(McpPrompt('Worker', '干活的角色', f'系统提示 {_SECRET}'),),
        resources=(
            McpResource('syndica://artifact/note', 'note', text=f'正文 {_SECRET}'),
            McpResource(
                'syndica://artifact/bin', 'bin', text=None, mime='application/octet-stream'
            ),
        ),
        traces=traces,
    )


async def _portal(tmp_path: Path) -> tuple[WorkspaceRouter, Path]:
    root = tmp_path / 'ws'
    root.mkdir()
    (root / 'notes.md').write_text('alpha marker\n', encoding='utf-8')
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    return WorkspaceRouter(local, taint=FileTaint(root)), root


def _text(result: types.CallToolResult) -> str:
    block = result.content[0]
    return str(getattr(block, 'text', ''))


async def test_both_eras_list_read_grep_prompt_and_redacted_resources(tmp_path: Path) -> None:
    portal, root = await _portal(tmp_path)
    golden = json.loads(
        (Path(__file__).parent / 'testdata' / 'mcp_serve_tools.json').read_text(encoding='utf-8')
    )
    try:
        for mode in ('legacy', 'auto'):
            server = _server(portal, root)
            async with Client(server, mode=mode, cache=None) as client:
                listed = await client.list_tools(cache_mode='bypass')
                names = [tool.name for tool in listed.tools]
                assert names == ['Read', 'Grep', 'Glob']
                hints = [
                    {
                        'name': tool.name,
                        'readOnlyHint': bool(tool.annotations and tool.annotations.read_only_hint),
                    }
                    for tool in listed.tools
                ]
                assert hints == golden
                assert 'Write' not in names
                read = await client.call_tool('Read', {'path': 'notes.md'})
                assert 'alpha marker' in _text(read)
                assert read.is_error is not True
                found = await client.call_tool('Grep', {'pattern': 'marker', 'path': '.'})
                assert 'notes.md' in _text(found)
                prompts = await client.list_prompts()
                assert [item.name for item in prompts.prompts] == ['Worker']
                prompt = await client.get_prompt('Worker')
                body = str(getattr(prompt.messages[0].content, 'text', ''))
                assert _SECRET not in body
                assert '系统提示' in body
                resources = await client.list_resources()
                uris = [str(item.uri) for item in resources.resources]
                assert uris == ['syndica://artifact/note', 'syndica://artifact/bin']
                note = await client.read_resource('syndica://artifact/note')
                note_text = str(getattr(note.contents[0], 'text', ''))
                assert _SECRET not in note_text
                binary = await client.read_resource('syndica://artifact/bin')
                binary_text = str(getattr(binary.contents[0], 'text', ''))
                assert binary_text == '[binary omitted mime=application/octet-stream]'
                assert _SECRET not in binary_text
    finally:
        await portal.close()


async def test_bash_and_git_are_refused_and_an_external_write_reads_back_untrusted(
    tmp_path: Path,
) -> None:
    portal, root = await _portal(tmp_path)
    try:
        server = _server(
            portal, root, extra={'Bash': Bash(portal), 'Write': Write(portal), 'Boom': _Boom()}
        )
        async with Client(server, mode='legacy', cache=None) as client:
            tools = (await client.list_tools(cache_mode='bypass')).tools
            listed = {tool.name for tool in tools}
            assert 'Bash' in listed and 'Write' in listed
            bash_tool = next(tool for tool in tools if tool.name == 'Bash')
            assert bash_tool.annotations is not None
            assert bash_tool.annotations.read_only_hint is not True
            removed = await client.call_tool('Bash', {'command': 'rm -rf /'})
            assert removed.is_error is True
            assert '已被阻断' in _text(removed) or '没有执行' in _text(removed)
            git = await client.call_tool('Bash', {'command': 'git commit -m x'})
            assert git.is_error is True
            assert 'git' in _text(git)
            wrote = await client.call_tool('Write', {'path': 'out.md', 'content': 'from-outside'})
            assert wrote.is_error is not True
            wrote_meta = wrote.meta
            assert wrote_meta is None or wrote_meta.get('trust') == 'untrusted'
            again = await client.call_tool('Read', {'path': 'out.md'})
            assert 'from-outside' in _text(again)
            assert again.meta is not None and again.meta.get('trust') == 'untrusted'
            boom = await client.call_tool('Boom', {})
            assert _SECRET not in _text(boom)
            assert 'RuntimeError' in _text(boom) or '原文不回显' in _text(boom)
    finally:
        await portal.close()


async def test_a_tool_call_is_a_span_naming_the_mcp_client(tmp_path: Path) -> None:
    router, root = await _portal(tmp_path)
    store = InMemoryTraceStore()
    traces = McpTraces(store, RuleRedactor(), router)
    try:
        server = _server(router, root, traces=traces)
        async with Client(server, mode='legacy', cache=None) as client:
            await client.call_tool('Read', {'path': 'notes.md'})
        await traces.aclose()
        headers = await store.traces()
        assert len(headers) == 1
        spans = await store.spans(headers[0].trace_id)
        callers = [
            span.attributes.get('tool.caller')
            for span in spans
            if isinstance(span.attributes.get('tool.caller'), str)
        ]
        assert callers
        assert all(str(item).startswith('mcp-client:') for item in callers)
    finally:
        await router.close()


def test_the_server_accepts_the_same_protocol_versions_as_the_client() -> None:
    from syndica.backends.mcp.client import ACCEPTED_PROTOCOL_VERSIONS as client_versions
    from syndica.server.mcp_serve import ACCEPTED_PROTOCOL_VERSIONS as server_versions

    assert client_versions == server_versions


async def test_artifact_resources_omit_binary_bytes_and_failed_read_text() -> None:
    async def read(ref: ArtifactRef) -> bytes:
        if ref.mime == 'text/markdown':
            return f'hello {_SECRET}'.encode()
        if ref.mime == 'image/png':
            return b'\xff\xfe' + _SECRET.encode()
        raise RuntimeError(f'boom {_SECRET}')

    items = await resources_of(
        (
            ArtifactRef('file:note.md', 'note.md', 'document', 'text/markdown', 'v', 'worker'),
            ArtifactRef('file:pic.bin', 'pic.bin', 'image', 'image/png', 'v', 'worker'),
            ArtifactRef('file:gone.md', 'gone.md', 'document', 'text/plain', 'v', 'worker'),
        ),
        read,
    )
    assert items[0].uri == 'syndica://artifact/file%3Anote.md'
    assert items[0].text is not None and _SECRET in items[0].text
    assert items[1].text is None
    assert items[2].text is not None
    assert _SECRET not in items[2].text and 'RuntimeError' not in items[2].text
    assert '原文不回显' in items[2].text


async def test_a_resource_source_is_reread_and_redacted(tmp_path: Path) -> None:
    router, root = await _portal(tmp_path)
    current: list[McpResource] = []

    async def source() -> tuple[McpResource, ...]:
        return tuple(current)

    try:
        server = build_mcp_server(
            {'Read': Read(router)},
            policy=_policy(root),
            read_only=_READ_ONLY,
            redactor=RuleRedactor(secrets=(_SECRET,)),
            resource_source=source,
        )
        async with Client(server, mode='legacy', cache=None) as client:
            assert (await client.list_resources()).resources == []
            current.append(McpResource('syndica://artifact/note', 'note', text=f'正文 {_SECRET}'))
            listed = await client.list_resources()
            assert [str(item.uri) for item in listed.resources] == ['syndica://artifact/note']
            note = await client.read_resource('syndica://artifact/note')
            text = str(getattr(note.contents[0], 'text', ''))
            assert _SECRET not in text and '正文' in text
    finally:
        await router.close()


async def test_read_artifact_bytes_uses_the_file_while_the_version_matches(
    tmp_path: Path,
) -> None:
    router, root = await _portal(tmp_path)
    try:
        data = (root / 'notes.md').read_bytes()
        version = 'sha256:' + hashlib.sha256(data).hexdigest()
        current = ArtifactRef(
            'file:notes.md', 'notes.md', 'document', 'text/markdown', version, 'w'
        )
        assert await read_artifact_bytes(router, current) == data
        stale = ArtifactRef(
            'file:notes.md', 'notes.md', 'document', 'text/markdown', 'sha256:' + 'ab' * 32, 'w'
        )
        with pytest.raises(ConfigError, match='workspace.versions'):
            await read_artifact_bytes(router, stale)
    finally:
        await router.close()


class _Shell(Tool):
    __tool_name__ = 'Shell'
    __tool_command_args__ = ('command',)

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def execute(self, command: str) -> ToolResult:
        """记下命令，不真的执行。"""
        self.ran.append(command)
        return ToolResult(output=f'ran {command}', trust=TrustLevel.TRUSTED)


async def _shell_calls(
    tmp_path: Path, calls: list[dict[str, object]]
) -> tuple[_Shell, list[types.CallToolResult], list[Span]]:
    router, _ = await _portal(tmp_path)
    store = InMemoryTraceStore()
    traces = McpTraces(store, RuleRedactor(), router)
    shell = _Shell()
    results: list[types.CallToolResult] = []
    try:
        server = build_mcp_server(
            {'Shell': shell},
            policy=ExternalCallPolicy(judge=DangerJudge(), policy=AllowAll()),
            read_only=frozenset(),
            traces=traces,
        )
        async with Client(server, mode='legacy', cache=None) as client:
            for arguments in calls:
                results.append(await client.call_tool('Shell', arguments))
        await traces.aclose()
        spans = [
            span
            for header in await store.traces()
            for span in await store.spans(header.trace_id)
            if span.attributes.get('tool.name') == 'Shell'
        ]
    finally:
        await router.close()
    return shell, results, spans


async def test_a_high_risk_call_is_refused_under_allow_all_because_the_mcp_client_is_untrusted(
    tmp_path: Path,
) -> None:
    shell, (refused, allowed), spans = await _shell_calls(
        tmp_path, [{'command': 'sudo true'}, {'command': 'echo hi'}]
    )

    assert shell.ran == ['echo hi']
    assert refused.is_error is True
    assert 'shell.privilege_escalation' in _text(refused)
    assert allowed.is_error is not True
    assert [
        (
            span.attributes.get('danger.provenance'),
            span.attributes.get('danger.decided_by'),
            span.attributes.get('danger.disposition'),
        )
        for span in spans
    ] == [('untrusted', 'flag', 'block'), ('untrusted', 'policy', 'allow')]


@pytest.mark.parametrize(
    ('arguments', 'received'),
    [({}, "'command' is a required property"), ({'command': 7}, "is not of type 'string'")],
    ids=['missing', 'wrong_type'],
)
async def test_arguments_outside_the_input_schema_are_refused_before_the_tool_is_judged(
    tmp_path: Path, arguments: dict[str, object], received: str
) -> None:
    shell, [result], spans = await _shell_calls(tmp_path, [arguments])
    text = _text(result)

    assert shell.ran == []
    assert spans == []
    assert result.is_error is True
    assert '期望实参符合 Shell 的 inputSchema' in text
    assert received in text
    assert '下一步：按 inputSchema 改参数后再次调用 Shell' in text


class _Misconfigured(Tool):
    __tool_name__ = 'Misconfigured'
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        """抛一个调用方配置错误。"""
        raise ConfigError(
            expected='quiz-expected',
            received=f'quiz-received {_SECRET}',
            next_action='quiz-next',
        )


async def test_an_error_raised_past_call_external_is_neither_echoed_nor_leaks_a_secret(
    tmp_path: Path,
) -> None:
    portal, root = await _portal(tmp_path)
    try:
        server = _server(portal, root, extra={'Misconfigured': _Misconfigured()})
        async with Client(server, mode='legacy', cache=None) as client:
            result = await client.call_tool('Misconfigured', {})
    finally:
        await portal.close()
    text = _text(result)

    assert result.is_error is True
    assert 'ConfigError' in text
    assert '原文不回显' in text
    assert '下一步：看 server 的 stderr 日志' in text
    assert _SECRET not in text
    assert 'quiz' not in text


def test_the_composed_server_marks_only_read_grep_glob_read_only(tmp_path: Path) -> None:
    from syndica.serve import _bind_mcp  # pyright: ignore[reportPrivateUsage]

    (tmp_path / '.syndica.yaml').write_text(
        'version: 1\ninterop:\n  mcp:\n    serve:\n      tools: [Write, Read, Bash, Glob]\n',
        encoding='utf-8',
    )
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(''), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: ScriptedLLM([]),
        workspace_root=tmp_path,
        environ={},
    )

    async def body() -> list[types.Tool]:
        config = host.trusted_config('.syndica.yaml')
        server, traces, router, environment = await _bind_mcp(host, config, None)
        try:
            async with Client(server, mode='legacy', cache=None) as client:
                return (await client.list_tools(cache_mode='bypass')).tools
        finally:
            await traces.aclose()
            await router.close()
            environment.remove()

    try:
        listed = asyncio.run(body())
    finally:
        host.close()

    assert [
        (
            tool.name,
            tool.annotations is not None and tool.annotations.read_only_hint is True,
            tool.annotations is not None and tool.annotations.destructive_hint is True,
        )
        for tool in listed
    ] == [
        ('Write', False, True),
        ('Read', True, False),
        ('Bash', False, True),
        ('Glob', True, False),
    ]


def test_an_unknown_serve_tool_names_the_missing_one() -> None:
    with pytest.raises(ConfigError, match='Nope') as caught:
        select_serve_tools({}, ('Read', 'Nope'))
    assert 'mcp.serve.tools' in caught.value.expected
    assert '已经装上' in caught.value.next_action
