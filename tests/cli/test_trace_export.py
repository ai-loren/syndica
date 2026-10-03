"""OTLP 导出的 CLI 面（M3 D14 / T16）：环境里的端点在 run 结束时导出、`trace export`、头只随环境里的
端点且不以明文发到别的机器、失败只告警、项目配置不能选端点。"""

from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import string
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

import syndica.__main__ as entry
import syndica.cli.host as host_module
from syndica.cli.export import TraceExporter
from syndica.cli.handoff import trace_directory
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, TraceRequest, main
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import (
    ConfigError,
    ContextBlock,
    FileTraceStore,
    InMemoryTraceStore,
    LLMConfig,
    RuleRedactor,
    ScriptedLLM,
    ScriptedTurn,
    Span,
    SpanKind,
    SpanStatus,
    SyndicaError,
    ToolCall,
    TraceStore,
)
from syndica.kernel.redaction import Redactor, encode_redacted
from syndica.kernel.stores import file_trace

_NOT_GIVEN: Any = object()
# 解析器挂住的时长：比导出的时限长得多，关宿主要是等它就看得出来。
_RESOLVER_HANGS = 15.0
_WITHIN = 30.0


def _token(seed: int) -> str:
    rng = random.Random(seed)
    return 'tok_' + ''.join(rng.choices(string.ascii_letters + string.digits, k=32))


@dataclass
class _Collector:
    address: str
    status: int = 200
    answer: str = 'collector says no'
    received: list[tuple[str, dict[str, str], dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, str], dict[str, Any]]]
    )


@pytest.fixture
def collector() -> Iterator[_Collector]:
    holder: list[_Collector] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers['Content-Length'])
            body = json.loads(self.rfile.read(length))
            # `self.path` 把开头的 `//` 并成一个（http.server 的防护）：路径按请求行里的原样记。
            raw_path = self.requestline.split()[1]
            holder[0].received.append((raw_path, dict(self.headers.items()), body))
            self.send_response(holder[0].status)
            self.end_headers()
            self.wfile.write(holder[0].answer.encode() if holder[0].status >= 300 else b'')

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    holder.append(_Collector(f'http://127.0.0.1:{server.server_address[1]}'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield holder[0]
    finally:
        server.shutdown()
        server.server_close()


def _closed_port() -> str:
    server = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
    address = f'http://127.0.0.1:{server.server_address[1]}'
    server.server_close()
    return address


def _llm() -> ScriptedLLM:
    return ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[ToolCall(id='c1', name='Bash', arguments='{"command": "printf hi"}')]
            ),
            ScriptedTurn(content='done'),
        ]
    )


def _cli(
    root: Path,
    argv: list[str],
    *,
    environ: Mapping[str, str],
    llm: ScriptedLLM | None = None,
    exporter: TraceExporter | None = _NOT_GIVEN,
) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(''), stdout=stdout, stderr=stderr)
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=io,
        model_factory=lambda _: llm or _llm(),
        workspace_root=root,
        environ=dict(environ),
        exporter=entry._export_trace if exporter is _NOT_GIVEN else exporter,  # pyright: ignore[reportPrivateUsage]
    )
    try:
        code = main(argv, host=host, io=io)
    finally:
        host.close(interrupted=False)
    return code, stdout.getvalue(), stderr.getvalue()


def _run(root: Path, environ: Mapping[str, str], **options: Any) -> tuple[int, str, str]:
    return _cli(root, ['run', 'go', '--agent', 'Worker'], environ=environ, **options)


def _trace_line(root: Path) -> str:
    [directory] = list(trace_directory(root).iterdir())
    return f'trace：{directory.relative_to(root)}\n'


def _only_directory(root: Path) -> str:
    [directory] = [path for path in trace_directory(root).iterdir() if path.is_dir()]
    return directory.name


@dataclass
class _Spy:
    calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    async def __call__(
        self,
        store: TraceStore,
        trace_id: str,
        *,
        url: str,
        headers: Mapping[str, str],
        resource: Mapping[str, str],
        redactor: Redactor,
    ) -> int:
        self.calls.append(
            {'trace_id': trace_id, 'url': url, 'headers': dict(headers), 'resource': dict(resource)}
        )
        return 0


# --- export at the end of a run ---------------------------------------------------------


def test_a_run_exports_its_trace_to_the_endpoint_in_the_environment_with_its_headers(
    tmp_path: Path, collector: _Collector
):
    token = _token(1)
    environ = {
        'OTEL_EXPORTER_OTLP_ENDPOINT': collector.address,
        'OTEL_EXPORTER_OTLP_HEADERS': f'x-token={token}',
    }

    code, _, err = _run(tmp_path, environ)

    assert code == 0
    assert err == _trace_line(tmp_path)
    [(path, headers, document)] = collector.received
    assert path == '/v1/traces'
    assert headers['x-token'] == token
    spans = document['resourceSpans'][0]['scopeSpans'][0]['spans']
    assert {span['traceId'][:8] for span in spans} == {_only_directory(tmp_path)[-8:]}


@pytest.mark.parametrize(
    ('variable', 'given', 'path'),
    [
        ('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', '/custom/traces', '/custom/traces'),
        ('OTEL_EXPORTER_OTLP_ENDPOINT', '/base', '/base/v1/traces'),
        ('OTEL_EXPORTER_OTLP_ENDPOINT', '/v1/traces', '/v1/traces'),
    ],
)
def test_the_endpoint_variables_follow_the_opentelemetry_convention(
    tmp_path: Path, collector: _Collector, variable: str, given: str, path: str
):
    code, _, _ = _run(tmp_path, {variable: f'{collector.address}{given}'})

    assert code == 0
    assert [item[0] for item in collector.received] == [path]


def test_the_traces_specific_endpoint_wins_over_the_generic_one(tmp_path: Path):
    spy = _Spy()
    environ = {
        'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT': 'http://localhost:9/specific',
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9/generic',
    }

    _run(tmp_path, environ, exporter=spy)

    assert [call['url'] for call in spy.calls] == ['http://localhost:9/specific']


def test_a_run_without_an_endpoint_exports_nothing(tmp_path: Path):
    spy = _Spy()

    code, _, err = _run(
        tmp_path, {'OTEL_EXPORTER_OTLP_HEADERS': 'x-token=abcdefgh1234'}, exporter=spy
    )

    assert (code, spy.calls) == (0, [])
    assert err == _trace_line(tmp_path)


@pytest.mark.parametrize('failure', ['refused', 'server error', 'unreachable'])
def test_a_failed_export_at_the_end_of_a_run_warns_once_and_keeps_the_exit_code(
    tmp_path: Path, collector: _Collector, failure: str
):
    address = collector.address
    if failure == 'refused':
        collector.status = 400
    elif failure == 'server error':
        collector.status = 500
    else:
        address = _closed_port()

    code, out, err = _run(tmp_path, {'OTEL_EXPORTER_OTLP_ENDPOINT': address})

    assert (code, out) == (0, 'done\n')
    lines = err.splitlines()
    assert lines[0] == _trace_line(tmp_path).rstrip('\n')
    [warning] = lines[1:]
    assert warning.startswith('trace 没有导出：期望: ')
    assert '下一步: ' in warning
    assert f'syndica trace export {_only_directory(tmp_path)[-8:]}' in warning


def test_a_host_without_an_exporter_says_the_trace_was_not_exported(tmp_path: Path):
    code, _, err = _run(
        tmp_path, {'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9'}, exporter=None
    )

    assert code == 0
    assert '没有接上导出器' in err


# --- headers ------------------------------------------------------------------------------


def test_headers_are_not_sent_in_plaintext_to_another_machine(tmp_path: Path):
    spy = _Spy()
    environ = {
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://collector.example:4318',
        'OTEL_EXPORTER_OTLP_HEADERS': f'authorization=Bearer {_token(2)}',
    }

    code, _, err = _run(tmp_path, environ, exporter=spy)

    assert code == 0
    assert [call['headers'] for call in spy.calls] == [{}]
    assert 'OTEL_EXPORTER_OTLP_TRACES_HEADERS 里的头没有随导出发出' in err and 'https' in err


@pytest.mark.parametrize(
    'endpoint', ['https://collector.example', 'http://localhost:4318', 'http://[::1]:4318']
)
def test_headers_go_with_an_endpoint_that_does_not_expose_them(tmp_path: Path, endpoint: str):
    spy = _Spy()
    token = _token(3)
    environ = {
        'OTEL_EXPORTER_OTLP_ENDPOINT': endpoint,
        'OTEL_EXPORTER_OTLP_HEADERS': f' x-token = {token} , x-team=core%20team ',
    }

    _run(tmp_path, environ, exporter=spy)

    assert [call['headers'] for call in spy.calls] == [{'x-token': token, 'x-team': 'core team'}]


@pytest.mark.parametrize(
    ('headers', 'said'),
    [
        ('x-token', '第 1 项没有「=」'),
        ('x-team=core,bad name=x', '第 2 项头名不是合法的 HTTP 头名'),
        ('x-token=a%0Ab', '第 1 项值里有换行、控制字符或 ASCII 之外的字符'),
    ],
)
def test_malformed_headers_warn_without_echoing_any_value(tmp_path: Path, headers: str, said: str):
    spy = _Spy()
    environ = {
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9',
        'OTEL_EXPORTER_OTLP_HEADERS': headers,
    }

    code, _, err = _run(tmp_path, environ, exporter=spy)

    assert (code, spy.calls) == (0, [])
    assert said in err and 'OTEL_EXPORTER_OTLP_HEADERS' in err
    assert 'core' not in err and 'a%0Ab' not in err


def test_a_malformed_endpoint_in_the_environment_warns_and_the_run_carries_on(tmp_path: Path):
    spy = _Spy()
    secret_url = f'ftp://user:{_token(4)}@collector.example'

    code, _, err = _run(tmp_path, {'OTEL_EXPORTER_OTLP_ENDPOINT': secret_url}, exporter=spy)

    assert (code, spy.calls) == (0, [])
    assert 'OTEL_EXPORTER_OTLP_ENDPOINT 是 http:// 或 https://' in err
    assert _token(4) not in err


def test_header_values_never_reach_the_trace_on_disk(tmp_path: Path):
    token = _token(5)
    environ = {
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9',
        'OTEL_EXPORTER_OTLP_HEADERS': f'x-token={token}',
    }
    llm = ScriptedLLM([ScriptedTurn(content=f'the collector token is {token}')])

    _run(tmp_path, environ, llm=llm, exporter=_Spy())

    files = [path for path in trace_directory(tmp_path).rglob('*') if path.is_file()]
    assert files
    assert all(token not in path.read_text(encoding='utf-8') for path in files)


# --- trace export -------------------------------------------------------------------------


def test_trace_export_to_a_given_endpoint_sends_none_of_the_environment_headers(
    tmp_path: Path, collector: _Collector
):
    _run(tmp_path, {}, exporter=_Spy())
    environ = {
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9',
        'OTEL_EXPORTER_OTLP_HEADERS': f'x-token={_token(6)}',
    }
    name = _only_directory(tmp_path)

    code, out, _ = _cli(
        tmp_path, ['trace', 'export', name, '--otlp', collector.address], environ=environ
    )

    assert code == 0
    assert out.startswith('导出了 trace ') and out.endswith(' 个 span。\n')
    [(path, headers, _)] = collector.received
    assert path == '/v1/traces'
    assert 'x-token' not in {key.lower() for key in headers}


def test_trace_export_without_otlp_uses_the_environment_endpoint_and_its_headers(
    tmp_path: Path, collector: _Collector
):
    _run(tmp_path, {}, exporter=_Spy())
    token = _token(7)
    environ = {
        'OTEL_EXPORTER_OTLP_ENDPOINT': collector.address,
        'OTEL_EXPORTER_OTLP_HEADERS': f'x-token={token}',
    }

    code, _, _ = _cli(tmp_path, ['trace', 'export', _only_directory(tmp_path)], environ=environ)

    assert code == 0
    [(_, headers, _)] = collector.received
    assert headers['x-token'] == token


def test_trace_export_that_the_collector_refuses_exits_1(tmp_path: Path, collector: _Collector):
    _run(tmp_path, {}, exporter=_Spy())
    collector.status = 503

    code, out, err = _cli(
        tmp_path,
        ['trace', 'export', _only_directory(tmp_path), '--otlp', collector.address],
        environ={},
    )

    assert (code, out) == (1, '')
    assert 'HTTP 503' in err and 'collector says no' in err


@pytest.mark.parametrize(
    ('argv', 'said'),
    [
        (['trace', 'export', 'cd', '--otlp', 'http://localhost:9'], '被 prune 掉的找不回来'),
        (['trace', 'export', 'NAME', '--otlp', 'not a url'], 'http:// 或 https://'),
        (
            ['trace', 'export', 'NAME', '--otlp', 'http://user:pw@collector.example'],
            '带凭证时是 https 或本机地址',
        ),
        (['trace', 'export', 'NAME'], '两样都没有'),
    ],
)
def test_trace_export_that_cannot_start_exits_2(tmp_path: Path, argv: list[str], said: str):
    _run(tmp_path, {}, exporter=_Spy())
    spy = _Spy()
    name = _only_directory(tmp_path)

    code, _, err = _cli(
        tmp_path, [name if item == 'NAME' else item for item in argv], environ={}, exporter=spy
    )

    assert (code, spy.calls) == (2, [])
    assert said in err


def test_trace_export_from_a_host_without_an_exporter_says_where_it_works(tmp_path: Path):
    _run(tmp_path, {}, exporter=_Spy())

    code, _, err = _cli(
        tmp_path,
        ['trace', 'export', _only_directory(tmp_path), '--otlp', 'http://localhost:9'],
        environ={},
        exporter=None,
    )

    assert code == 2
    assert '`syndica` 命令行' in err


# --- the project cannot choose where traces go -------------------------------------------


@pytest.mark.parametrize(
    ('yaml', 'where'),
    [
        ('otel: {endpoint: http://evil.example}\n', '配置根'),
        ('otlp_endpoint: http://evil.example\n', '配置根'),
        ('trace: {endpoint: http://evil.example}\n', 'trace '),
        ('trace: {level: full, exporter: otlp}\n', 'trace '),
        ('opentelemetry: {}\n', '配置根'),
        ('trace: {collector: http://evil.example}\n', 'trace '),
    ],
)
def test_a_project_config_cannot_choose_where_traces_are_exported(
    tmp_path: Path, yaml: str, where: str
):
    (tmp_path / '.syndica.yaml').write_text(f'version: 1\n{yaml}', encoding='utf-8')
    spy = _Spy()

    code, _, err = _run(tmp_path, {}, exporter=spy)

    assert (code, spy.calls) == (1, [])
    assert f'实际: {where}里有' in err
    assert 'OTEL_EXPORTER_OTLP_ENDPOINT' in err and '--otlp' in err
    assert '不受信任' in err


# --- Ctrl-C ---------------------------------------------------------------------------------

_DRIVER = """
import json
import sys
from pathlib import Path

import syndica.__main__ as entry
from syndica.kernel import ScriptedLLM, ScriptedTurn, ToolCall

command = 'touch cut.ready && exec sleep 60'
call = ToolCall(id='b1', name='Bash', arguments=json.dumps({'command': command}))
llm = ScriptedLLM([ScriptedTurn(tool_calls=[call])])
entry._configured_model = lambda name: llm
sys.argv = ['syndica', 'run', 'go', '--agent', 'Worker']
entry.entrypoint()
"""


def test_an_interrupted_run_is_not_exported_and_says_how_to_export_it(
    tmp_path: Path, collector: _Collector
):
    root = tmp_path / 'work'
    root.mkdir()
    (tmp_path / 'driver.py').write_text(_DRIVER, encoding='utf-8')
    environ = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environ.update({'HOME': str(tmp_path), 'OTEL_EXPORTER_OTLP_ENDPOINT': collector.address})
    process = subprocess.Popen(
        [sys.executable, str(tmp_path / 'driver.py')],
        cwd=root,
        env=environ,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + _WITHIN
        while not (root / 'cut.ready').exists():
            assert process.poll() is None, process.stderr.read() if process.stderr else ''
            assert time.monotonic() < deadline, 'the agent command never started'
            time.sleep(0.05)
        process.send_signal(signal.SIGINT)
        _, err = process.communicate(timeout=_WITHIN)
    finally:
        if process.poll() is None:
            process.kill()

    assert process.returncode == 130
    assert collector.received == []
    assert '被中断的 run 不导出 trace；要导出就运行 syndica trace export ' in err.decode()


def test_a_percent_encoded_header_value_is_redacted_in_its_decoded_form(tmp_path: Path):
    rng = random.Random(8)
    pieces = [''.join(rng.choices(string.ascii_letters + string.digits, k=4)) for _ in range(6)]
    decoded, encoded = '/'.join(pieces), '%2F'.join(pieces)
    environ = {
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9',
        'OTEL_EXPORTER_OTLP_HEADERS': f'x-token={encoded}',
    }
    llm = ScriptedLLM([ScriptedTurn(content=f'the collector token is {decoded}')])

    _run(tmp_path, environ, llm=llm, exporter=_Spy())

    files = [path for path in trace_directory(tmp_path).rglob('*') if path.is_file()]
    assert all(decoded not in path.read_text(encoding='utf-8') for path in files)


def test_trace_export_to_the_environment_endpoint_says_why_its_headers_stayed_home(
    tmp_path: Path,
):
    _run(tmp_path, {}, exporter=_Spy())
    spy = _Spy()
    environ = {
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://collector.example:4318',
        'OTEL_EXPORTER_OTLP_HEADERS': f'x-token={_token(9)}',
    }

    code, _, err = _cli(
        tmp_path, ['trace', 'export', _only_directory(tmp_path)], environ=environ, exporter=spy
    )

    assert code == 0
    assert [call['headers'] for call in spy.calls] == [{}]
    assert 'OTEL_EXPORTER_OTLP_TRACES_HEADERS 里的头没有随导出发出' in err


def test_trace_export_of_a_trace_whose_files_were_changed_exits_2_before_sending(tmp_path: Path):
    _run(tmp_path, {}, exporter=_Spy())
    name = _only_directory(tmp_path)
    spans = trace_directory(tmp_path) / name / 'spans.jsonl'
    spans.write_text('not json\n' + spans.read_text(encoding='utf-8'), encoding='utf-8')
    spy = _Spy()

    code, _, err = _cli(
        tmp_path,
        ['trace', 'export', name, '--otlp', 'http://localhost:9'],
        environ={},
        exporter=spy,
    )

    assert (code, spy.calls) == (2, [])
    assert '删掉它所在的目录' in err


def test_the_command_line_entry_wires_the_exporter(
    tmp_path: Path, collector: _Collector, monkeypatch: pytest.MonkeyPatch
):
    _run(tmp_path, {}, exporter=_Spy())
    monkeypatch.chdir(tmp_path)
    io = CliIO(stdin=StringIO(''), stdout=StringIO(), stderr=StringIO())
    host = entry._LazyHost(io)  # pyright: ignore[reportPrivateUsage]

    try:
        outcome = host.trace(
            TraceRequest('export', trace_id=_only_directory(tmp_path), otlp=collector.address)
        )
    finally:
        host.close()

    assert not isinstance(outcome, str) and outcome.exit_code == 0
    assert len(collector.received) == 1


async def test_an_exporter_that_cannot_be_imported_says_which_extra_to_install(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setitem(sys.modules, 'syndica.backends.otel_otlp', None)

    with pytest.raises(ConfigError) as caught:
        await entry._export_trace(  # pyright: ignore[reportPrivateUsage]
            InMemoryTraceStore(),
            'ab' * 16,
            url='http://localhost:9',
            headers={},
            resource={},
            redactor=RuleRedactor(),
        )

    assert "pip install 'syndica[otel]'" in caught.value.next_action


def test_an_export_that_outlasts_the_deadline_at_the_end_of_a_run_gives_up_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(host_module, '_EXPORT_DEADLINE', 0.2)

    async def slow(
        store: TraceStore,
        trace_id: str,
        *,
        url: str,
        headers: Mapping[str, str],
        resource: Mapping[str, str],
        redactor: Redactor,
    ) -> int:
        await asyncio.sleep(30)
        return 0

    started = time.monotonic()
    code, _, err = _run(
        tmp_path, {'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9'}, exporter=slow
    )

    assert code == 0
    assert time.monotonic() - started < 10
    assert 'trace 没有导出完：collector 在 0.2 秒里没有收完' in err
    assert f'syndica trace export {_only_directory(tmp_path)[-8:]}' in err


# --- review round: what the environment and the workspace may and may not do -------------


def test_pass_env_cannot_hand_the_export_settings_to_the_agent(tmp_path: Path):
    (tmp_path / '.syndica.yaml').write_text(
        'version: 1\nworkspace:\n  pass_env: [OTEL_EXPORTER_OTLP_TRACES_HEADERS]\n',
        encoding='utf-8',
    )

    code, _, err = _run(tmp_path, {}, exporter=_Spy())

    assert code == 1
    assert 'OTEL_EXPORTER_OTLP_*' in err and 'collector 的凭证' in err


class _Forging:
    """第一轮之后往还在写的 trace 里塞一个 span：Agent 的 Bash 改得到 trace 目录。"""

    def __init__(self, root: Path) -> None:
        self.config = LLMConfig(provider='test', model='forging')
        self._root = root
        self._turns = 0

    async def create(self, messages: list[Any], **kwargs: Any) -> ContextBlock:
        self._turns += 1
        if self._turns == 1:
            return ContextBlock(
                role='assistant',
                tool_calls=[ToolCall(id='c1', name='Bash', arguments='{"command": "true"}')],
            )
        [directory] = list(trace_directory(self._root).iterdir())
        forged = Span(
            span_id='f0' * 8,
            parent_span_id=None,
            kind=SpanKind.TURN,
            name='forged',
            agent_id='f1' * 16,
            start_ns=1,
            end_ns=2,
            status=SpanStatus.OK,
            attributes={'turn': 1},
        )
        line = encode_redacted(file_trace._span_value(forged), RuleRedactor())  # pyright: ignore[reportPrivateUsage]
        with (directory / 'spans.jsonl').open('a', encoding='utf-8') as file:
            file.write(f'{line}\n')
        return ContextBlock(role='assistant', content='done')


def test_spans_planted_in_the_trace_directory_are_not_exported_at_the_end_of_a_run(
    tmp_path: Path, collector: _Collector
):
    code, _, _ = _cli(
        tmp_path,
        ['run', 'go', '--agent', 'Worker'],
        environ={'OTEL_EXPORTER_OTLP_ENDPOINT': collector.address},
        llm=_Forging(tmp_path),  # pyright: ignore[reportArgumentType]
    )

    assert code == 0
    [(_, _, document)] = collector.received
    names = [span['name'] for span in document['resourceSpans'][0]['scopeSpans'][0]['spans']]
    assert names and 'forged' not in names


def test_what_a_collector_echoes_back_is_redacted_before_trace_export_prints_it(
    tmp_path: Path, collector: _Collector
):
    _run(tmp_path, {}, exporter=_Spy())
    token = _token(10)
    collector.status = 401
    collector.answer = f'invalid api key {token}'
    environ = {
        'OTEL_EXPORTER_OTLP_ENDPOINT': collector.address,
        'OTEL_EXPORTER_OTLP_HEADERS': f'x-token={token}',
    }

    code, _, err = _cli(tmp_path, ['trace', 'export', _only_directory(tmp_path)], environ=environ)

    assert code == 1
    assert 'HTTP 401' in err
    assert token not in err


def test_the_credential_after_an_auth_scheme_is_redacted_on_its_own(tmp_path: Path):
    token = _token(11)
    environ = {
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9',
        'OTEL_EXPORTER_OTLP_HEADERS': f'authorization=Bearer {token}',
    }
    llm = ScriptedLLM([ScriptedTurn(content=f'the bare credential is {token}')])

    _run(tmp_path, environ, llm=llm, exporter=_Spy())

    files = [path for path in trace_directory(tmp_path).rglob('*') if path.is_file()]
    assert all(token not in path.read_text(encoding='utf-8') for path in files)


@pytest.mark.parametrize(
    ('environ', 'said'),
    [
        (
            {'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://alice:pw@collector.example:4318'},
            '带凭证时是 https 或本机地址',
        ),
        (
            {'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://alice@collector.example:4318'},
            '带凭证时是 https 或本机地址',
        ),
        (
            {
                'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9',
                'OTEL_EXPORTER_OTLP_PROTOCOL': 'grpc',
            },
            'syndica 只会 OTLP/HTTP',
        ),
        (
            {
                'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9',
                'OTEL_EXPORTER_OTLP_TRACES_PROTOCOL': 'grpc',
                'OTEL_EXPORTER_OTLP_PROTOCOL': 'http/json',
            },
            'OTEL_EXPORTER_OTLP_TRACES_PROTOCOL 是 http/json',
        ),
        (
            {
                'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9',
                'OTEL_EXPORTER_OTLP_HEADERS': 'x-token=quiz%C3%A9',
            },
            'ASCII 之外的字符',
        ),
        (
            {
                'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9',
                'OTEL_EXPORTER_OTLP_HEADERS': 'x-token=a%01b',
            },
            '控制字符',
        ),
        ({'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://collector.example:x'}, 'http:// 或 https://'),
        ({'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://:4318'}, 'http:// 或 https://'),
        (
            {
                'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9',
                'OTEL_RESOURCE_ATTRIBUTES': 'team',
            },
            'OTEL_RESOURCE_ATTRIBUTES 是逗号分隔的 key=value',
        ),
    ],
)
def test_an_environment_the_export_cannot_use_warns_and_sends_nothing(
    tmp_path: Path, environ: dict[str, str], said: str
):
    spy = _Spy()

    code, _, err = _run(tmp_path, environ, exporter=spy)

    assert (code, spy.calls) == (0, [])
    assert said in err and '下一步: ' in err
    assert 'pw@' not in err and 'quiz' not in err


@pytest.mark.parametrize(
    'environ',
    [
        {'OTEL_SDK_DISABLED': 'true'},
        {'OTEL_SDK_DISABLED': 'TRUE'},
        {'OTEL_TRACES_EXPORTER': 'none'},
        {'OTEL_TRACES_EXPORTER': 'console, zipkin'},
    ],
)
def test_the_opentelemetry_switches_turn_the_export_off_quietly(
    tmp_path: Path, environ: dict[str, str]
):
    spy = _Spy()

    code, _, err = _run(
        tmp_path, {'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9', **environ}, exporter=spy
    )

    assert (code, spy.calls) == (0, [])
    assert err == _trace_line(tmp_path)


@pytest.mark.parametrize(
    'environ',
    [
        {'OTEL_SDK_DISABLED': 'false', 'OTEL_TRACES_EXPORTER': 'otlp'},
        {'OTEL_TRACES_EXPORTER': 'console,otlp', 'OTEL_EXPORTER_OTLP_PROTOCOL': 'http/protobuf'},
    ],
)
def test_switches_that_leave_otlp_on_still_export(tmp_path: Path, environ: dict[str, str]):
    spy = _Spy()

    _run(tmp_path, {'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9', **environ}, exporter=spy)

    assert len(spy.calls) == 1


def test_the_service_name_and_resource_attributes_come_from_the_environment(tmp_path: Path):
    spy = _Spy()
    environ = {
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9',
        'OTEL_RESOURCE_ATTRIBUTES': 'service.name=ignored, deployment.environment=dev%20box,,',
        'OTEL_SERVICE_NAME': 'my-agents',
    }

    _run(tmp_path, environ, exporter=spy)

    assert [call['resource'] for call in spy.calls] == [
        {'service.name': 'my-agents', 'deployment.environment': 'dev box'}
    ]


def test_traces_specific_headers_join_and_override_the_general_ones(tmp_path: Path):
    spy = _Spy()
    token = _token(12)
    environ = {
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9',
        'OTEL_EXPORTER_OTLP_HEADERS': 'x-team=core,,x-token=general',
        'OTEL_EXPORTER_OTLP_TRACES_HEADERS': f'x-token={token}',
    }
    llm = ScriptedLLM([ScriptedTurn(content=f'leaking {token}')])

    _run(tmp_path, environ, llm=llm, exporter=spy)

    assert [call['headers'] for call in spy.calls] == [{'x-team': 'core', 'x-token': token}]
    files = [path for path in trace_directory(tmp_path).rglob('*') if path.is_file()]
    assert all(token not in path.read_text(encoding='utf-8') for path in files)


def test_the_encoded_form_of_a_header_value_is_redacted_too(tmp_path: Path):
    rng = random.Random(13)
    pieces = [''.join(rng.choices(string.ascii_letters + string.digits, k=4)) for _ in range(6)]
    encoded = '%2F'.join(pieces)
    environ = {
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9',
        'OTEL_EXPORTER_OTLP_HEADERS': f'x-token={encoded}',
    }
    llm = ScriptedLLM([ScriptedTurn(content=f'the encoded token is {encoded}')])

    _run(tmp_path, environ, llm=llm, exporter=_Spy())

    files = [path for path in trace_directory(tmp_path).rglob('*') if path.is_file()]
    assert all(encoded not in path.read_text(encoding='utf-8') for path in files)


def test_a_plaintext_endpoint_elsewhere_without_headers_is_used_without_a_note(tmp_path: Path):
    spy = _Spy()

    code, _, err = _run(
        tmp_path, {'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://collector.example:4318'}, exporter=spy
    )

    assert code == 0
    assert [call['url'] for call in spy.calls] == ['http://collector.example:4318/v1/traces']
    assert err == _trace_line(tmp_path)


def test_a_generic_endpoint_with_a_trailing_slash_gets_one_traces_path(
    tmp_path: Path, collector: _Collector
):
    _run(tmp_path, {'OTEL_EXPORTER_OTLP_ENDPOINT': f'{collector.address}/'})

    assert [item[0] for item in collector.received] == ['/v1/traces']


class _Refused(SyndicaError):
    resendable = False


@pytest.mark.parametrize(
    ('raised', 'hinted'),
    [
        (RuntimeError('exporter bug'), True),
        (ConfigError(expected='e', received='r', next_action="pip install 'syndica[otel]'"), False),
        (_Refused(expected='e', received='partly rejected', next_action='n'), False),
    ],
)
def test_a_resend_is_suggested_only_when_resending_helps(
    tmp_path: Path, raised: Exception, hinted: bool
):
    async def failing(
        store: TraceStore,
        trace_id: str,
        *,
        url: str,
        headers: Mapping[str, str],
        resource: Mapping[str, str],
        redactor: Redactor,
    ) -> int:
        raise raised

    code, _, err = _run(
        tmp_path, {'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9'}, exporter=failing
    )

    assert code == 0
    assert 'trace 没有导出：' in err
    assert ('重发' in err) is hinted


def test_trace_export_reports_how_many_spans_it_sent(tmp_path: Path, collector: _Collector):
    _run(tmp_path, {}, exporter=_Spy())
    name = _only_directory(tmp_path)

    code, out, _ = _cli(
        tmp_path, ['trace', 'export', name, '--otlp', collector.address], environ={}
    )

    [(_, _, document)] = collector.received
    sent = len(document['resourceSpans'][0]['scopeSpans'][0]['spans'])
    assert (code, sent > 0) == (0, True)
    assert out.endswith(f' 的 {sent} 个 span。\n')


def _unfinished(root: Path) -> str:
    name = _only_directory(root)
    header = trace_directory(root) / name / 'trace.json'
    value = json.loads(header.read_text(encoding='utf-8'))
    value['status'] = None
    value['ended_ns'] = None
    header.write_text(json.dumps(value), encoding='utf-8')
    return name


def _broken_edges(root: Path) -> str:
    name = _only_directory(root)
    (trace_directory(root) / name / 'edges.jsonl').write_text('not json\n', encoding='utf-8')
    return name


@pytest.mark.parametrize(
    ('make', 'said'),
    [(_unfinished, '记录到一半就断了'), (_broken_edges, '删掉它所在的目录')],
    ids=['unfinished', 'broken-edges'],
)
def test_trace_export_of_a_trace_it_cannot_send_exits_2_before_sending(
    tmp_path: Path, make: Any, said: str
):
    _run(tmp_path, {}, exporter=_Spy())
    name = make(tmp_path)
    spy = _Spy()

    code, _, err = _cli(
        tmp_path,
        ['trace', 'export', name, '--otlp', 'http://localhost:9'],
        environ={},
        exporter=spy,
    )

    assert (code, spy.calls) == (2, [])
    assert said in err


def test_trace_export_to_an_address_with_an_overlong_label_exits_2(tmp_path: Path):
    _run(tmp_path, {}, exporter=_Spy())
    spy = _Spy()

    code, _, _ = _cli(
        tmp_path,
        ['trace', 'export', _only_directory(tmp_path), '--otlp', f'http://{"a" * 64}.example:4318'],
        environ={},
        exporter=spy,
    )

    assert (code, spy.calls) == (2, [])


def test_trace_export_to_an_address_httpx_refuses_is_a_three_part_failure(tmp_path: Path):
    _run(tmp_path, {}, exporter=_Spy())

    code, _, err = _cli(
        tmp_path,
        ['trace', 'export', _only_directory(tmp_path), '--otlp', 'http://\u2488.com:4318'],
        environ={},
    )

    assert code == 1
    assert 'InvalidURL' in err and 'CLI host 完成当前命令' not in err


def test_an_upper_case_export_key_in_the_project_config_is_refused_with_the_reason(tmp_path: Path):
    (tmp_path / '.syndica.yaml').write_text('version: 1\nOTLP: {}\n', encoding='utf-8')

    code, _, err = _run(tmp_path, {}, exporter=_Spy())

    assert code == 1
    assert '不受信任' in err


_SLOW_DRIVER = """
import sys

import syndica.__main__ as entry
from syndica.kernel import ScriptedLLM, ScriptedTurn

llm = ScriptedLLM([ScriptedTurn(content='the answer is 42')])
entry._configured_model = lambda name: llm
sys.argv = ['syndica', 'run', 'go', '--agent', 'Worker']
entry.entrypoint()
"""


def test_ctrl_c_during_the_export_keeps_the_outcome_of_a_finished_run(tmp_path: Path):
    arrived = threading.Event()

    class Holding(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            arrived.set()
            time.sleep(20)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(('127.0.0.1', 0), Holding)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = tmp_path / 'work'
    root.mkdir()
    (tmp_path / 'driver.py').write_text(_SLOW_DRIVER, encoding='utf-8')
    environ = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environ.update(
        {
            'HOME': str(tmp_path),
            'OTEL_EXPORTER_OTLP_ENDPOINT': f'http://127.0.0.1:{server.server_address[1]}',
        }
    )
    process = subprocess.Popen(
        [sys.executable, str(tmp_path / 'driver.py')],
        cwd=root,
        env=environ,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert arrived.wait(_WITHIN), 'the export never reached the collector'
        process.send_signal(signal.SIGINT)
        out, err = process.communicate(timeout=_WITHIN)
    finally:
        if process.poll() is None:
            process.kill()
        server.shutdown()
        server.server_close()

    assert process.returncode == 0
    assert out.decode() == 'the answer is 42\n'
    assert '导出被中断；要导出就运行 syndica trace export ' in err.decode()


def test_an_incomplete_trace_is_not_exported_at_the_end_of_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    async def refusing(self: Any, trace_id: str, spans: Any = (), edges: Any = ()) -> None:
        raise OSError('disk full')

    monkeypatch.setattr(FileTraceStore, 'append', refusing)
    spy = _Spy()

    code, _, err = _run(
        tmp_path, {'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9'}, exporter=spy
    )

    assert (code, spy.calls) == (0, [])
    assert '不完整的 trace 不导出' in err


def test_trace_export_carries_the_resource_attributes_from_the_environment(tmp_path: Path):
    _run(tmp_path, {}, exporter=_Spy())
    spy = _Spy()
    environ = {'OTEL_SERVICE_NAME': 'my-agents', 'OTEL_RESOURCE_ATTRIBUTES': 'team=core'}

    code, _, _ = _cli(
        tmp_path,
        ['trace', 'export', _only_directory(tmp_path), '--otlp', 'http://localhost:9'],
        environ=environ,
        exporter=spy,
    )

    assert code == 0
    assert [call['resource'] for call in spy.calls] == [
        {'team': 'core', 'service.name': 'my-agents'}
    ]


def test_a_chat_session_is_exported_when_the_host_closes(tmp_path: Path):
    spy = _Spy()
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO('hello\n'), stdout=stdout, stderr=stderr)
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=io,
        model_factory=lambda _: ScriptedLLM([ScriptedTurn(content='hi')]),
        workspace_root=tmp_path,
        environ={'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:9'},
        exporter=spy,
    )
    try:
        code = main(['chat'], host=host, io=io)
        assert spy.calls == []
    finally:
        host.close(interrupted=False)

    assert code == 0
    assert [call['trace_id'][:8] for call in spy.calls] == [_only_directory(tmp_path)[-8:]]


def test_an_export_refuses_with_2_when_the_config_it_redacts_with_cannot_be_read(tmp_path: Path):
    spy = _Spy()
    _run(tmp_path, {}, exporter=spy)
    (tmp_path / '.syndica.yaml').write_text('trace: [never closed\n', encoding='utf-8')
    [header] = asyncio.run(FileTraceStore(trace_directory(tmp_path)).traces())

    code, out, err = _cli(
        tmp_path,
        ['trace', 'export', header.trace_id[:8], '--otlp', 'http://localhost:9'],
        environ={},
        exporter=spy,
    )

    assert (code, out, spy.calls) == (2, '', [])
    assert '修好配置再导出' in err


def test_a_collector_name_that_takes_forever_to_resolve_cannot_keep_the_host_from_closing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    release = threading.Event()

    def unresolvable(*args: object, **kwargs: object) -> object:
        release.wait(_RESOLVER_HANGS)
        raise OSError('resolver gave up')

    monkeypatch.setattr('socket.getaddrinfo', unresolvable)
    monkeypatch.setattr(host_module, '_EXPORT_DEADLINE', 0.3)
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(''), stdout=stdout, stderr=stderr)
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=io,
        model_factory=lambda _: _llm(),
        workspace_root=tmp_path,
        environ={'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://collector.never-resolves.test:4318'},
        exporter=entry._export_trace,  # pyright: ignore[reportPrivateUsage]
    )
    try:
        code = main(['run', 'go', '--agent', 'Worker'], host=host, io=io)
        started = time.monotonic()
        host.close(interrupted=False)
        closing = time.monotonic() - started
    finally:
        release.set()

    assert code == 0 and stdout.getvalue().rstrip().endswith('done')
    assert closing < _RESOLVER_HANGS / 3
    assert 'trace 没有导出完' in stderr.getvalue()


def test_a_finished_trace_whose_spans_were_zeroed_is_refused_with_2_and_nothing_is_sent(
    tmp_path: Path,
):
    spy = _Spy()
    _run(tmp_path, {}, exporter=spy)
    store = FileTraceStore(trace_directory(tmp_path))
    [header] = asyncio.run(store.traces())
    directory = asyncio.run(store.directory(header.trace_id))
    assert directory is not None
    spans = directory / 'spans.jsonl'
    spans.write_bytes(b'\0' * spans.stat().st_size)

    code, out, err = _cli(
        tmp_path,
        ['trace', 'export', header.trace_id[:8], '--otlp', 'http://localhost:9'],
        environ={},
        exporter=spy,
    )

    assert (code, out, spy.calls) == (2, '', [])
    assert '已经写下了结局' in err
