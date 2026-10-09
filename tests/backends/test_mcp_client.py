"""MCP 连接：分页、版本、串行、超时、取消、断连、stdio 环境。"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import mcp_types as types
import pytest
from mcp.client.client import Client
from mcp.server.lowlevel.server import Server

from syndica.backends.mcp.client import (
    ACCEPTED_PROTOCOL_VERSIONS,
    McpClient,
    connect_http,
    connect_stdio,
)
from syndica.backends.mcp.module import assign_roles, excluded_reports, mcp_module
from syndica.backends.mcp.tools import ExcludedTool
from syndica.kernel import ConfigError, RuleRedactor, TrustLevel
from syndica.kernel.driving import _DRIVING_AGENT  # pyright: ignore[reportPrivateUsage]

_SECRET = 'sekret-VALUE-918273'


class _Page:
    def __init__(self, tools: list[object], cursor: str | None) -> None:
        self.tools = tools
        self.next_cursor = cursor


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name
        self.input_schema = {'type': 'object', 'properties': {'text': {'type': 'string'}}}
        self.annotations = None


class _Result:
    def __init__(self, text: str = 'ok', *, is_error: bool = False) -> None:
        self.content = [SimpleNamespace(type='text', text=text)]
        self.structured_content = None
        self.is_error = is_error


class _Fake:
    def __init__(
        self,
        version: object = '2025-11-25',
        pages: list[_Page] | None = None,
    ) -> None:
        self.session = SimpleNamespace(protocol_version=version)
        self.pages = list(pages or [_Page([], None)])
        self.cache_modes: list[object] = []
        self.calls: list[tuple[object, object, object]] = []
        self.entered = False
        self.exited = False
        self.inflight = 0
        self.max_inflight = 0
        self.behavior: object = None

    async def __aenter__(self) -> _Fake:
        self.entered = True
        return self

    async def __aexit__(self, *args: object) -> None:
        del args
        self.exited = True
        self.entered = False

    async def list_tools(self, *, cursor: str | None = None, cache_mode: str = 'use') -> _Page:
        del cursor
        self.cache_modes.append(cache_mode)
        if not self.pages:
            return _Page([], None)
        return self.pages.pop(0)

    async def call_tool(
        self,
        name: str,
        arguments: object = None,
        read_timeout_seconds: float | None = None,
    ) -> object:
        self.calls.append((name, arguments, read_timeout_seconds))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            behavior = self.behavior
            if behavior is not None:
                return await behavior(name, arguments, read_timeout_seconds)  # type: ignore[operator]
            return _Result()
        finally:
            self.inflight -= 1


def _client(fake: _Fake, **kwargs: object) -> McpClient:
    fields: dict[str, object] = {'timeout': 30}
    fields.update(kwargs)
    return McpClient(
        'docs',
        fake,
        RuleRedactor(secrets=(_SECRET,)),
        fields.pop('timeout'),
        **fields,  # type: ignore[arg-type]
    )


def test_only_the_sdk_five_protocol_versions_are_accepted():
    from mcp_types.version import SUPPORTED_PROTOCOL_VERSIONS

    assert ACCEPTED_PROTOCOL_VERSIONS == (
        '2024-11-05',
        '2025-03-26',
        '2025-06-18',
        '2025-11-25',
        '2026-07-28',
    )
    assert ACCEPTED_PROTOCOL_VERSIONS == SUPPORTED_PROTOCOL_VERSIONS


def test_stdio_and_http_reject_the_shapes_that_would_skip_the_gate():
    redactor = RuleRedactor()
    with pytest.raises(ConfigError, match='shell'):
        connect_stdio('docs', 'bash -lc echo', env={}, cwd='.', redactor=redactor, timeout=30)
    with pytest.raises(ConfigError, match='True'):
        connect_stdio(
            'docs',
            [sys.executable, '-c', 'pass'],
            env={},
            cwd='.',
            redactor=redactor,
            timeout=True,
        )
    with pytest.raises(ConfigError, match='http://'):
        connect_http('docs', 'ftp://example.com/mcp', redactor=redactor, timeout=30)
    with pytest.raises(ConfigError, match='bearer'):
        connect_http('docs', 'http://127.0.0.1/mcp', redactor=redactor, timeout=30, bearer='')


async def test_pages_are_fetched_without_the_response_cache():
    fake = _Fake(pages=[_Page([_Tool('one')], 'next'), _Page([_Tool('two')], None)])
    async with _client(fake) as client:
        assert client.protocol_version == '2025-11-25'
        assert list(client.tools) == ['docs__one', 'docs__two']
    assert fake.cache_modes == ['bypass', 'bypass']
    assert fake.exited


async def test_a_repeated_cursor_and_an_unbounded_listing_are_refused():
    repeated = _Fake(pages=[_Page([], 'same'), _Page([], 'same')])
    with pytest.raises(ConfigError, match='next_cursor'):
        async with _client(repeated):
            pass
    pages = [_Page([_Tool(f't{index}')], f'c{index}') for index in range(100)]
    unbounded = _Fake(pages=pages)
    with pytest.raises(ConfigError, match='100'):
        async with _client(unbounded):
            pass


async def test_a_version_outside_the_five_closes_the_connection():
    fake = _Fake(version='1999-01-01')
    with pytest.raises(ConfigError, match='1999-01-01') as exc:
        async with _client(fake):
            pass
    assert '2024-11-05' in str(exc.value)
    assert fake.exited


async def test_calls_are_serial_and_a_closed_client_does_not_call_again():
    fake = _Fake()

    async def hold(name: object, arguments: object, read_timeout: object) -> _Result:
        del name, arguments, read_timeout
        await asyncio.sleep(0.05)
        return _Result()

    fake.behavior = hold
    async with _client(fake) as client:
        await asyncio.gather(client.invoke('echo', {}), client.invoke('echo', {}))
        assert fake.max_inflight == 1
    result = await client.invoke('echo', {})
    assert '已经关闭' in result.output
    assert '不会自动重连' in result.output
    assert len(fake.calls) == 2


async def test_timeout_cancel_and_disconnect_do_not_reconnect(caplog: pytest.LogCaptureFixture):
    del caplog
    fake = _Fake()

    async def slow(name: object, arguments: object, read_timeout: object) -> _Result:
        del name, arguments, read_timeout
        await asyncio.sleep(5)
        return _Result()

    fake.behavior = slow
    async with _client(fake, timeout=0.05) as client:
        timed = await client.invoke('echo', {})
        assert '超时' in timed.output
        assert '不会自动重连' in timed.output
        task = asyncio.create_task(client.invoke('echo', {}))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    broken = _Fake()

    async def boom(name: object, arguments: object, read_timeout: object) -> _Result:
        del name, arguments, read_timeout
        raise ConnectionError(f'bearer {_SECRET}')

    broken.behavior = boom
    async with _client(broken) as client:
        result = await client.invoke('echo', {})
    assert _SECRET not in result.output
    assert '不会自动重连' in result.output
    assert result.trust is TrustLevel.UNTRUSTED
    assert 'docs' in result.output


async def test_wall_remaining_caps_the_server_timeout_and_rejects_bool():
    fake = _Fake()
    async with _client(fake, timeout=30) as client:
        client.bind_wall('alpha', lambda: 5)
        token = _DRIVING_AGENT.set('alpha')
        try:
            await client.invoke('echo', {})
            assert fake.calls[-1][2] == 5
            client.bind_wall('alpha', lambda: 0)
            timed = await client.invoke('echo', {})
            assert '超时' in timed.output
            assert len(fake.calls) == 1
            client.bind_wall('alpha', lambda: True)  # type: ignore[arg-type, return-value]
            with pytest.raises(ConfigError, match='墙钟'):
                await client.invoke('echo', {})
        finally:
            _DRIVING_AGENT.reset(token)


async def test_an_in_process_server_round_trips_both_protocol_eras():
    async def on_list_tools(ctx: object, params: object) -> types.ListToolsResult:
        del ctx, params
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

    async def on_call_tool(ctx: object, params: object) -> types.CallToolResult:
        del ctx
        arguments = getattr(params, 'arguments', None) or {}
        text = arguments.get('text', '')
        failed = text == 'fail'
        body = 'nope' if failed else str(text)
        return types.CallToolResult(
            content=[types.TextContent(type='text', text=body)],
            is_error=failed,
        )

    for mode in ('legacy', 'auto'):
        server = Server('demo', on_list_tools=on_list_tools, on_call_tool=on_call_tool)
        protocol = Client(server, mode=mode, cache=None)
        async with McpClient('docs', protocol, RuleRedactor(), 30) as client:
            assert client.protocol_version in ACCEPTED_PROTOCOL_VERSIONS
            tool = client.tools['docs__echo']
            ok = await tool.execute(text='ping')
            assert ok.output == 'ping'
            assert ok.trust is TrustLevel.UNTRUSTED
            failed = await tool.execute(text='fail')
            assert failed.meta['isError'] is True
            assert 'nope' in failed.output
            assert failed.trust is TrustLevel.UNTRUSTED


async def test_stdio_child_env_overrides_inherited_names_and_stderr_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    probe = tmp_path / 'probe.txt'
    script = tmp_path / 'srv.py'
    script.write_text(
        'import os, sys\n'
        'open(sys.argv[1], "w").write(os.environ.get("PATH", "") + "\\n" + '
        '("yes" if "SUPER_SECRET" in os.environ else "no"))\n'
        f'sys.stderr.write("{_SECRET}\\n" + ("x" * 80) + "\\n")\n'
        'sys.stderr.flush()\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('SUPER_SECRET', 'present')
    marker = 'syndica-mcp-path'
    env = {'PATH': marker, 'HOME': os.environ.get('HOME', '/tmp')}
    client = connect_stdio(
        'docs',
        [sys.executable, str(script), str(probe)],
        env=env,
        cwd=str(tmp_path),
        redactor=RuleRedactor(secrets=(_SECRET,)),
        timeout=2,
        stderr_limit=64,
    )
    failed = False
    with caplog.at_level(logging.INFO):
        try:
            async with client:
                pass
        except asyncio.CancelledError:
            raise
        except BaseException:
            failed = True
    assert failed
    text = probe.read_text(encoding='utf-8')
    assert text.splitlines()[0] == marker
    assert text.splitlines()[1] == 'no'
    assert _SECRET not in caplog.text
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.args is not None
    ]
    assert warnings
    assert isinstance(warnings[0].args, tuple)
    assert warnings[0].args[1] == 64


_STRADDLER = 'sk-live-QuizWaltzVex9876JUMP'


def _fragments(secret: str, size: int = 6) -> list[str]:
    return [secret[start : start + size] for start in range(len(secret) - size + 1)]


async def _stdio_stderr(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, payload: str, limit: int
) -> tuple[list[str], list[tuple[object, ...]]]:
    script = tmp_path / 'srv.py'
    script.write_text(
        f'import sys\nsys.stderr.write({payload!r})\nsys.stderr.flush()\n', encoding='utf-8'
    )
    client = connect_stdio(
        'docs',
        [sys.executable, str(script)],
        env={'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
        cwd=str(tmp_path),
        redactor=RuleRedactor(secrets=(_STRADDLER,)),
        timeout=2,
        stderr_limit=limit,
    )
    failed = False
    with caplog.at_level(logging.INFO, logger='syndica.backends.mcp.client'):
        try:
            async with client:
                pass
        except asyncio.CancelledError:
            raise
        except BaseException:
            failed = True
    assert failed
    texts: list[str] = []
    warnings: list[tuple[object, ...]] = []
    for record in caplog.records:
        if record.name != 'syndica.backends.mcp.client' or not isinstance(record.args, tuple):
            continue
        if record.levelno == logging.WARNING:
            warnings.append(record.args)
        elif 'stderr' in str(record.msg):
            texts.append(str(record.args[1]))
    return texts, warnings


async def test_stdio_stderr_straddling_the_limit_leaves_no_secret_fragment(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    texts, warnings = await _stdio_stderr(
        tmp_path, caplog, 'x' * 50 + _STRADDLER + '\nafter\n', limit=64
    )
    assert not [piece for piece in _fragments(_STRADDLER) if piece in caplog.text]
    assert texts[0].startswith('x' * 50)
    assert sum(len(text.encode('utf-8')) for text in texts) <= 64
    assert [args[1] for args in warnings] == [64]


async def test_stdio_stderr_straddling_the_line_cap_leaves_no_secret_fragment(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    line_cap = 65_536
    head = 'word ' * (line_cap // 5 - 2)
    payload = head + _STRADDLER + 'y' * 1000 + '\ntail line\n'
    texts, warnings = await _stdio_stderr(tmp_path, caplog, payload, limit=1_048_576)
    assert not [piece for piece in _fragments(_STRADDLER) if piece in caplog.text]
    assert texts[0].startswith('word word')
    assert texts[-1] == 'tail line'
    assert [args[1] for args in warnings] == [line_cap]


def test_an_sdk_that_hides_the_server_process_is_refused():
    from syndica.backends.mcp.client import _server_pid  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(ConfigError, match='process') as exc:
        _server_pid(SimpleNamespace(gen=SimpleNamespace(ag_frame=SimpleNamespace(f_locals={}))))
    assert 'HTTP' in str(exc.value)


def test_roles_receive_only_their_tools_and_shutdown_closes_clients():
    echo = SimpleNamespace(name='docs__echo')
    other = SimpleNamespace(name='docs__other')

    class _Held:
        def __init__(self, name: str, tools: dict[str, object]) -> None:
            self.name = name
            self.tools = tools
            self.excluded = (ExcludedTool(name, 'bad', 'cyclic-$ref'),)
            self.bound: list[str] = []
            self.closed = False

        def bind_wall(self, agent_id: str, probe: object) -> None:
            self.bound.append(agent_id)
            self.probe = probe

        async def aclose(self) -> None:
            self.closed = True

    docs = _Held('docs', {'docs__echo': echo})
    files = _Held('files', {'files__read': other})
    assigned = assign_roles([docs, files], {'docs': ('researcher',), 'files': ('researcher',)})  # type: ignore[list-item]
    assert set(assigned['researcher']) == {'docs__echo', 'files__read'}
    module = mcp_module(
        [docs, files],  # type: ignore[list-item]
        assigned,
        wall_remaining=lambda runtime, agent_id: 1.5,
    )
    researcher = SimpleNamespace(options=SimpleNamespace(name='researcher'), agent_id='a1')
    stranger = SimpleNamespace(options=SimpleNamespace(name='stranger'), agent_id='a2')
    factory = module.tool_factories[0]
    installed = factory(cast(Any, SimpleNamespace()), cast(Any, researcher), {})
    assert set(installed) == {'docs__echo', 'files__read'}
    assert factory(cast(Any, SimpleNamespace()), cast(Any, stranger), {}) == {}
    assert docs.bound == ['a1', 'a2']
    assert docs.probe(SimpleNamespace(), 'a1') == 1.5  # type: ignore[attr-defined]
    assert excluded_reports([docs, files]) == (  # type: ignore[list-item]
        'docs bad: cyclic-$ref',
        'files bad: cyclic-$ref',
    )

    async def close() -> None:
        assert module.on_shutdown is not None
        await module.on_shutdown(SimpleNamespace())  # type: ignore[arg-type]

    asyncio.run(close())
    assert docs.closed
    assert files.closed


def test_two_servers_cannot_install_the_same_public_name_on_one_role():
    tool = SimpleNamespace()
    left = SimpleNamespace(name='ab', tools={'ab___x': tool})
    right = SimpleNamespace(name='a_b', tools={'ab___x': tool})
    with pytest.raises(ConfigError, match='ab___x'):
        assign_roles([left, right], {'ab': ('researcher',), 'a_b': ('researcher',)})  # type: ignore[list-item]
