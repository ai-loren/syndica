"""OTLP/HTTP JSON 导出（M3 D14 / T16）：发出的 JSON 逐字段对、分批、collector 拒收与连不上、
出去之前再过一遍脱敏、内核与 CLI 不碰这个 backend。"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import random
import re
import string
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from syndica.backends.otel_otlp import OtlpExportError, OtlpHttpExporter, otlp_documents
from syndica.kernel import (
    CausalityEdge,
    EdgeKind,
    InMemoryTraceStore,
    RedactionRule,
    RuleRedactor,
    Span,
    SpanEvent,
    SpanKind,
    SpanStatus,
    TraceHeader,
    TraceLevel,
    TraceRedaction,
    TraceSource,
)
from syndica.kernel.redaction import RULES_VERSION

_TRACE = 'ab' * 16
_LEAD = '1a' * 16
_CHILD = '2b' * 16
_DIGEST = 'cd' * 32
_S = 10**9
_T0 = 1_790_000_000 * _S
_URL = 'http://collector.test:4318/v1/traces'
_PACKAGE = Path(__file__).resolve().parents[2] / 'src' / 'syndica'


def _secret(seed: int) -> str:
    rng = random.Random(seed)
    return 'tok_' + ''.join(rng.choices(string.ascii_letters + string.digits, k=32))


def _header(**changes: Any) -> TraceHeader:
    header = TraceHeader(
        trace_id=_TRACE,
        started_ns=_T0,
        level=TraceLevel.REPLAY,
        redaction=TraceRedaction(RuleRedactor().digest, RULES_VERSION),
        source=TraceSource(argv=('run', 'go'), version='1.2.3'),
        ended_ns=_T0 + 10 * _S,
        status=SpanStatus.OK,
    )
    return dataclasses.replace(header, **changes)


def _span(number: int, start: int, **fields: Any) -> Span:
    values: dict[str, Any] = {
        'span_id': f'{number:016x}',
        'parent_span_id': None,
        'kind': SpanKind.TURN,
        'name': f'turn {number}',
        'agent_id': _LEAD,
        'start_ns': _T0 + start * _S,
        'end_ns': _T0 + (start + 1) * _S,
        'status': SpanStatus.OK,
        'attributes': {'turn': number},
    }
    values.update(fields)
    return Span(**values)


def _spans() -> list[Span]:
    """store 里的顺序（追加的顺序）按开始时间既不升也不降。"""
    return [
        _span(
            3,
            3,
            parent_span_id=f'{1:016x}',
            kind=SpanKind.TOOL_CALL,
            name='Bash',
            status=SpanStatus.CANCELLED,
            attributes={
                'tool.name': 'Bash',
                'tool.index': 0,
                'tool.timed_out': True,
                'danger.rule_ids': ['shell.git_state_change', 'shell.raw_device_write'],
            },
            events=[SpanEvent('message.sent', _T0 + 3 * _S + 5, {'message.to': _CHILD})],
        ),
        _span(
            2,
            1,
            parent_span_id=f'{1:016x}',
            kind=SpanKind.LLM_CALL,
            name='llm',
            status=SpanStatus.ERROR,
            attributes={'llm.attempt': 1, 'llm.status': None, 'llm.response': _DIGEST},
        ),
        _span(
            1,
            0,
            kind=SpanKind.AGENT,
            name='Lead',
            attributes={'agent.role': 'Lead', 'stop_reason': 'completed'},
        ),
        _span(4, 5, kind=SpanKind.AGENT, name='Helper', agent_id=_CHILD, attributes={}),
    ]


def _edges() -> list[CausalityEdge]:
    return [
        CausalityEdge(
            kind=EdgeKind.SPAWN,
            source_span_id=f'{3:016x}',
            target_span_id=f'{4:016x}',
            attributes={'child.agent_id': _CHILD},
        ),
        CausalityEdge(
            kind=EdgeKind.MESSAGE,
            source_span_id=f'{3:016x}',
            target_span_id=f'{255:016x}',
            attributes={'message.id': 'ef' * 16},
        ),
    ]


async def _stored(spans: list[Span], edges: list[CausalityEdge] | None = None, **header: Any):
    store = InMemoryTraceStore()
    await store.begin(dataclasses.replace(_header(**header), ended_ns=None, status=None))
    await store.append(_TRACE, spans, edges or [])
    finished = _header(**header)
    if finished.status is not None and finished.ended_ns is not None:
        await store.finish(_TRACE, finished.status, finished.ended_ns)
    return store


async def _export(
    store: InMemoryTraceStore,
    respond: Callable[[httpx.Request], httpx.Response] | None = None,
    **options: Any,
) -> tuple[int, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200) if respond is None else respond(request)

    options.setdefault('redactor', RuleRedactor())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        count = await OtlpHttpExporter(_URL, client=client, **options).export(store, _TRACE)
    return count, seen


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def _sent_spans(request: httpx.Request) -> list[dict[str, Any]]:
    return _body(request)['resourceSpans'][0]['scopeSpans'][0]['spans']


async def test_every_field_is_sent_in_the_shape_otlp_expects():
    store = await _stored(
        _spans(), _edges(), source=TraceSource(argv=('run', 'go'), version='1.2.3')
    )

    count, [request] = await _export(store, headers={'x-team': 'core'})

    assert count == 4
    assert (request.method, str(request.url)) == ('POST', _URL)
    assert request.headers['content-type'] == 'application/json'
    assert request.headers['x-team'] == 'core'
    [resource_spans] = _body(request)['resourceSpans']
    assert resource_spans['resource'] == {
        'attributes': [
            {'key': 'service.name', 'value': {'stringValue': 'syndica'}},
            {'key': 'service.version', 'value': {'stringValue': '1.2.3'}},
            {'key': 'syndica.trace.level', 'value': {'stringValue': 'replay'}},
            {'key': 'syndica.trace.status', 'value': {'stringValue': 'ok'}},
            {
                'key': 'process.command_args',
                'value': {
                    'arrayValue': {'values': [{'stringValue': 'run'}, {'stringValue': 'go'}]}
                },
            },
        ]
    }
    [scope_spans] = resource_spans['scopeSpans']
    assert scope_spans['scope'] == {'name': 'syndica', 'version': '1.2.3'}
    spans = scope_spans['spans']
    assert [span['spanId'] for span in spans] == [f'{n:016x}' for n in (3, 2, 1, 4)]
    tool, llm, lead, helper = spans
    assert tool == {
        'traceId': _TRACE,
        'spanId': f'{3:016x}',
        'parentSpanId': f'{1:016x}',
        'name': 'Bash',
        'kind': 1,
        'startTimeUnixNano': str(_T0 + 3 * _S),
        'endTimeUnixNano': str(_T0 + 4 * _S),
        'attributes': [
            {'key': 'syndica.span.kind', 'value': {'stringValue': 'tool_call'}},
            {'key': 'syndica.agent.id', 'value': {'stringValue': _LEAD}},
            {'key': 'tool.name', 'value': {'stringValue': 'Bash'}},
            {'key': 'tool.index', 'value': {'intValue': '0'}},
            {'key': 'tool.timed_out', 'value': {'boolValue': True}},
            {
                'key': 'danger.rule_ids',
                'value': {
                    'arrayValue': {
                        'values': [
                            {'stringValue': 'shell.git_state_change'},
                            {'stringValue': 'shell.raw_device_write'},
                        ]
                    }
                },
            },
        ],
        'events': [
            {
                'timeUnixNano': str(_T0 + 3 * _S + 5),
                'name': 'message.sent',
                'attributes': [{'key': 'message.to', 'value': {'stringValue': _CHILD}}],
            }
        ],
        'links': [
            {
                'traceId': _TRACE,
                'spanId': f'{255:016x}',
                'attributes': [
                    {'key': 'syndica.edge.kind', 'value': {'stringValue': 'message'}},
                    {'key': 'syndica.edge.points_at', 'value': {'stringValue': 'target'}},
                    {'key': 'message.id', 'value': {'stringValue': 'ef' * 16}},
                ],
            }
        ],
        'status': {'code': 2, 'message': 'cancelled'},
    }
    assert llm['status'] == {'code': 2, 'message': 'error'}
    assert [item['key'] for item in llm['attributes']] == [
        'syndica.span.kind',
        'syndica.agent.id',
        'llm.attempt',
        'llm.response',
    ]
    assert llm['attributes'][-1]['value'] == {'stringValue': _DIGEST}
    assert 'parentSpanId' not in lead
    assert lead['status'] == {'code': 1}
    assert helper['links'] == [
        {
            'traceId': _TRACE,
            'spanId': f'{3:016x}',
            'attributes': [
                {'key': 'syndica.edge.kind', 'value': {'stringValue': 'spawn'}},
                {'key': 'syndica.edge.points_at', 'value': {'stringValue': 'source'}},
                {'key': 'child.agent_id', 'value': {'stringValue': _CHILD}},
            ],
        }
    ]


async def test_an_unfinished_replayed_trace_says_so_and_names_its_original():
    header = _header(
        source=TraceSource(argv=(), version='', replay_of='ef' * 16), status=None, ended_ns=None
    )

    [document] = otlp_documents(header, _spans()[:1], [], redactor=RuleRedactor())

    [resource_spans] = document['resourceSpans']
    assert resource_spans['resource']['attributes'] == [
        {'key': 'service.name', 'value': {'stringValue': 'syndica'}},
        {'key': 'syndica.trace.level', 'value': {'stringValue': 'replay'}},
        {'key': 'syndica.trace.replay_of', 'value': {'stringValue': 'ef' * 16}},
    ]
    assert resource_spans['scopeSpans'][0]['scope'] == {'name': 'syndica'}


async def test_a_long_trace_goes_out_in_batches_that_each_carry_the_resource():
    spans = [_span(number, number) for number in (5, 1, 4, 2, 3)]
    store = await _stored(spans)

    count, requests = await _export(store, batch=2)

    assert count == 5
    assert [[span['spanId'][-1] for span in _sent_spans(r)] for r in requests] == [
        ['5', '1'],
        ['4', '2'],
        ['3'],
    ]
    resources = [_body(request)['resourceSpans'][0]['resource'] for request in requests]
    assert resources[0] == resources[1] == resources[2]


async def test_a_trace_without_spans_sends_nothing():
    store = await _stored([])

    count, requests = await _export(store)

    assert (count, requests) == (0, [])


@pytest.mark.parametrize(
    ('status', 'said'), [(307, 'moved'), (400, 'unknown path'), (503, 'overloaded')]
)
async def test_a_collector_that_does_not_take_the_trace_says_what_it_answered(
    status: int, said: str
):
    store = await _stored(_spans())

    with pytest.raises(OtlpExportError) as caught:
        await _export(store, lambda _: httpx.Response(status, text=said))

    assert f'HTTP {status}' in caught.value.received and said in caught.value.received
    assert '/v1/traces' in caught.value.next_action


async def test_an_unreachable_collector_is_reported_without_the_credentials_in_its_address():
    store = await _stored(_spans())
    password = _secret(1)
    seen: list[httpx.Request] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ConnectError('connection refused', request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        exporter = OtlpHttpExporter(
            f'http://user:{password}@collector.test:4318/v1/traces',
            redactor=RuleRedactor(),
            client=client,
        )
        with pytest.raises(OtlpExportError) as caught:
            await exporter.export(store, _TRACE)

    assert len(seen) == 1
    assert 'ConnectError' in caught.value.received
    assert password not in str(caught.value)
    assert 'collector.test:4318' in caught.value.expected
    assert '4318' in caught.value.next_action


async def test_spans_the_collector_rejects_count_as_a_failed_export():
    store = await _stored(_spans())
    answer = {'partialSuccess': {'rejectedSpans': '2', 'errorMessage': 'too old'}}

    with pytest.raises(OtlpExportError) as caught:
        await _export(store, lambda _: httpx.Response(200, json=answer))

    assert '拒收了 2 个' in caught.value.received and 'too old' in caught.value.received


@pytest.mark.parametrize(
    'answer',
    [b'', b'{}', b'{"partialSuccess": {}}', b'{"partialSuccess": {"rejectedSpans": "0"}}'],
)
async def test_a_success_answer_without_rejections_is_a_success(answer: bytes):
    store = await _stored(_spans())

    count, _ = await _export(store, lambda _: httpx.Response(200, content=answer))

    assert count == 4


async def test_the_body_is_redacted_on_its_way_out():
    secret = _secret(2)
    spans = [_span(1, 0, kind=SpanKind.TOOL_CALL, name='Bash', attributes={'tool.name': secret})]
    store = await _stored(spans)

    _, [request] = await _export(store, redactor=RuleRedactor(secrets=(secret,)))

    assert secret.encode() not in request.content
    assert b'[REDACTED:' in request.content


async def test_ids_and_digests_survive_a_user_rule_that_recognises_hex():
    store = await _stored(_spans(), _edges())
    hex_rule = RuleRedactor(rules=[RedactionRule('custom.hex', '[0-9a-f]{32,64}')])

    _, [request] = await _export(store, redactor=hex_rule)

    spans = _sent_spans(request)
    assert {span['traceId'] for span in spans} == {_TRACE}
    llm = spans[1]
    assert {'key': 'syndica.agent.id', 'value': {'stringValue': _LEAD}} in llm['attributes']
    assert llm['attributes'][-1]['value'] == {'stringValue': _DIGEST}


async def test_a_trace_the_store_does_not_have_is_refused_before_anything_is_sent():
    store = InMemoryTraceStore()

    with pytest.raises(OtlpExportError) as caught:
        await _export(store)

    assert _TRACE in caught.value.expected


# --- a real collector on this machine ---------------------------------------------------


@pytest.fixture
def collector() -> Iterator[tuple[str, list[tuple[str, dict[str, Any]]]]]:
    received: list[tuple[str, dict[str, Any]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers['Content-Length'])
            received.append((self.path, json.loads(self.rfile.read(length))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}', received
    finally:
        server.shutdown()
        server.server_close()


async def test_a_collector_on_this_machine_receives_and_parses_the_export(
    collector: tuple[str, list[tuple[str, dict[str, Any]]]],
):
    address, received = collector
    store = await _stored(_spans(), _edges())

    count = await OtlpHttpExporter(f'{address}/v1/traces', redactor=RuleRedactor()).export(
        store, _TRACE
    )
    await asyncio.sleep(0)

    assert count == 4
    [(path, document)] = received
    assert path == '/v1/traces'
    spans = document['resourceSpans'][0]['scopeSpans'][0]['spans']
    assert [span['spanId'] for span in spans] == [f'{n:016x}' for n in (3, 2, 1, 4)]


# --- nothing but the composition root may know this backend ----------------------------


def _sources(root: Path) -> list[Path]:
    return sorted(root.rglob('*.py'))


def _walked(root: Path) -> int:
    return sum(name.endswith('.py') for _, _, names in os.walk(root) for name in names)


@pytest.mark.parametrize('layer', ['kernel', 'cli', 'runner'])
def test_no_layer_but_the_composition_root_names_the_exporter_backend(layer: str):
    root = _PACKAGE / layer
    sources = _sources(root)

    naming = [
        str(path.relative_to(_PACKAGE)) for path in sources if 'otel_otlp' in path.read_text()
    ]

    assert naming == []
    assert len(sources) == _walked(root)


def test_the_guard_scans_the_files_that_matter_most():
    scanned = {
        str(path.relative_to(_PACKAGE))
        for layer in ('kernel', 'cli')
        for path in _sources(_PACKAGE / layer)
    }

    assert {'kernel/runtime.py', 'cli/host.py', 'cli/export.py'} <= scanned
    assert 'otel_otlp' in (_PACKAGE / '__main__.py').read_text()


async def test_an_injected_client_stays_open_for_the_next_export():
    store = await _stored(_spans())
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        exporter = OtlpHttpExporter(_URL, redactor=RuleRedactor(), client=client)
        await exporter.export(store, _TRACE)
        await exporter.export(store, _TRACE)

        assert not client.is_closed
    assert len(seen) == 2


# --- what user rules may and may not touch ------------------------------------------------

_HEX16 = re.compile(r'[0-9a-f]{16}')
_HEX32 = re.compile(r'[0-9a-f]{32}')
_NANOS = re.compile(r'[0-9]+')


def _valid_otlp(document: dict[str, Any]) -> None:
    """collector 会拒收的形状：id 不是十六进制、时间与整数不是十进制字符串、枚举不是整数。"""
    for resource_spans in document['resourceSpans']:
        for scope_spans in resource_spans['scopeSpans']:
            for span in scope_spans['spans']:
                assert _HEX32.fullmatch(span['traceId']), span['traceId']
                assert _HEX16.fullmatch(span['spanId']), span['spanId']
                if 'parentSpanId' in span:
                    assert _HEX16.fullmatch(span['parentSpanId']), span['parentSpanId']
                for field in ('startTimeUnixNano', 'endTimeUnixNano'):
                    assert _NANOS.fullmatch(span[field]), span[field]
                assert type(span['kind']) is int and type(span['status']['code']) is int
                for event in span['events']:
                    assert _NANOS.fullmatch(event['timeUnixNano']), event['timeUnixNano']
                for link in span['links']:
                    assert _HEX32.fullmatch(link['traceId']) and _HEX16.fullmatch(link['spanId'])
                for attribute in span['attributes']:
                    value = attribute['value']
                    if 'intValue' in value:
                        assert re.fullmatch(r'-?[0-9]+', value['intValue']), value


async def test_user_rules_redact_free_text_but_never_ids_times_or_numbers():
    phone = '13800138000'
    spans = [
        _span(
            7,
            1_000_000_000 + 13800138,
            name=f'call {phone}',
            parent_span_id='1380013800000000',
            attributes={'tool.name': f'dial {phone}', 'tool.index': 13800138000},
            events=[SpanEvent('message.sent', _T0 + 7 * _S, {'message.to': _CHILD})],
        ),
        *_spans(),
    ]
    store = await _stored(spans, _edges())
    rules = RuleRedactor(
        rules=[
            RedactionRule('custom.cn_mobile', r'1[3-9]\d{9}'),
            RedactionRule('custom.hex16', '[0-9a-f]{16}'),
            RedactionRule('custom.hex32', '[0-9a-f]{32}'),
        ]
    )

    _, [request] = await _export(store, redactor=rules)

    _valid_otlp(_body(request))
    [dialled, tool, *_] = _sent_spans(request)
    assert phone not in dialled['name'] and '[REDACTED:custom.cn_mobile]' in dialled['name']
    assert {'key': 'tool.index', 'value': {'intValue': '13800138000'}} in dialled['attributes']
    assert dialled['events'][0]['attributes'] == [
        {'key': 'message.to', 'value': {'stringValue': _CHILD}}
    ]
    assert tool['links'][0]['attributes'][-1] == {
        'key': 'message.id',
        'value': {'stringValue': 'ef' * 16},
    }


async def test_a_hex_secret_outside_an_identifier_position_is_redacted():
    secret = ''.join(random.Random(3).choices('0123456789abcdef', k=32))
    spans = [_span(1, 0, kind=SpanKind.TOOL_CALL, name='Bash', attributes={'tool.call_id': secret})]
    store = await _stored(spans)

    _, [request] = await _export(store, redactor=RuleRedactor(secrets=(secret,)))

    assert secret.encode() not in request.content


class _Counting:
    def __init__(self) -> None:
        self.seen: list[str] = []
        self._inner = RuleRedactor()

    def redact(self, text: str) -> str:
        self.seen.append(text)
        return self._inner.redact(text)


async def test_each_distinct_piece_of_free_text_is_redacted_once():
    spans = [
        _span(
            number, number, kind=SpanKind.TOOL_CALL, name='Bash', attributes={'tool.name': 'Bash'}
        )
        for number in range(1, 6)
    ]
    store = await _stored(spans)
    counting = _Counting()

    await _export(store, redactor=counting)  # pyright: ignore[reportArgumentType]

    assert sorted(counting.seen) == sorted(set(counting.seen))
    assert 'Bash' in counting.seen


# --- ordering and arguments ---------------------------------------------------------------


async def test_events_and_links_keep_their_order():
    events = [
        SpanEvent('message.sent', _T0 + 9, {'message.to': _CHILD}),
        SpanEvent('agent.despawned', _T0 + 3, {'agent.id': _CHILD}),
        SpanEvent('message.sent', _T0 + 6, {'message.to': _LEAD}),
    ]
    spans = [_span(1, 0, events=events)]
    edges = [
        CausalityEdge(EdgeKind.MESSAGE, f'{1:016x}', f'{9:016x}', {'message.id': 'c' * 32}),
        CausalityEdge(EdgeKind.MESSAGE, f'{1:016x}', f'{5:016x}', {'message.id': 'a' * 32}),
        CausalityEdge(EdgeKind.MESSAGE, f'{1:016x}', f'{7:016x}', {'message.id': 'b' * 32}),
    ]
    store = await _stored(spans, edges)

    _, [request] = await _export(store)

    [span] = _sent_spans(request)
    assert [event['timeUnixNano'][-1] for event in span['events']] == ['9', '3', '6']
    assert [link['spanId'][-1] for link in span['links']] == ['9', '5', '7']


@pytest.mark.parametrize('batch', [0, -1, True, 2.0])
def test_a_batch_size_that_is_not_a_positive_integer_is_refused(batch: Any):
    with pytest.raises(ValueError):
        OtlpHttpExporter(_URL, redactor=RuleRedactor(), batch=batch)
    with pytest.raises(ValueError):
        list(otlp_documents(_header(), _spans(), [], redactor=RuleRedactor(), batch=batch))


# --- the collector's answers --------------------------------------------------------------


@pytest.mark.parametrize('rejected', [2, '2'])
async def test_a_rejection_count_given_as_a_number_or_a_string_fails_the_export(rejected: Any):
    store = await _stored(_spans())
    answer = {'partialSuccess': {'rejectedSpans': rejected}}

    with pytest.raises(OtlpExportError) as caught:
        await _export(store, lambda _: httpx.Response(200, json=answer))

    assert '拒收了 2 个' in caught.value.received
    assert caught.value.resendable is False


async def test_an_answer_nested_too_deep_to_parse_counts_as_accepted():
    store = await _stored(_spans())
    nested = b'[' * 100_000 + b']' * 100_000

    count, _ = await _export(store, lambda _: httpx.Response(200, content=nested))

    assert count == 4


async def test_an_export_that_outlasts_its_deadline_stops_with_a_three_part_error():
    store = await _stored(_spans())

    async def stalled(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(stalled)) as client:
        exporter = OtlpHttpExporter(_URL, redactor=RuleRedactor(), client=client, deadline=0.2)
        with pytest.raises(OtlpExportError) as caught:
            await exporter.export(store, _TRACE)

    assert '0.2 秒' in caught.value.expected
    assert caught.value.resendable is True


@pytest.fixture
def silent() -> Iterator[str]:
    """收下连接、读完请求、一个字节也不回的 collector。"""
    import socket

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except OSError:
                continue
            held.append(connection)

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{listener.getsockname()[1]}/v1/traces'
    finally:
        stop.set()
        thread.join()
        for connection in held:
            connection.close()
        listener.close()


async def test_a_collector_that_never_answers_times_out_per_request(silent: str):
    store = await _stored(_spans())
    started = time.monotonic()

    with pytest.raises(OtlpExportError) as caught:
        await OtlpHttpExporter(silent, redactor=RuleRedactor(), timeout=0.3, deadline=20).export(
            store, _TRACE
        )

    assert time.monotonic() - started < 5
    assert 'Timeout' in caught.value.received


@pytest.mark.parametrize(
    ('url', 'shown', 'hidden'),
    [
        (
            'http://127.0.0.1:9/receiver/SECRETPATHTOKEN/v1/traces',
            'http://127.0.0.1:9',
            'SECRETPATHTOKEN',
        ),
        ('http://[::1]:9/v1/traces?key=SECRETQUERY', 'http://[::1]:9', 'SECRETQUERY'),
    ],
)
async def test_errors_name_the_endpoint_by_scheme_host_and_port_only(
    url: str, shown: str, hidden: str
):
    store = await _stored(_spans())

    with pytest.raises(OtlpExportError) as caught:
        await OtlpHttpExporter(url, redactor=RuleRedactor(), timeout=2).export(store, _TRACE)

    assert f'OTLP collector {shown} ' in caught.value.expected
    assert hidden not in str(caught.value)


async def test_an_address_httpx_cannot_use_is_a_three_part_export_error():
    store = await _stored(_spans())

    with pytest.raises(OtlpExportError) as caught:
        await OtlpHttpExporter('http://\u2488.com:4318/v1/traces', redactor=RuleRedactor()).export(
            store, _TRACE
        )

    assert 'InvalidURL' in caught.value.received


async def test_a_plaintext_export_never_goes_through_a_proxy(
    collector: tuple[str, list[tuple[str, dict[str, Any]]]], monkeypatch: pytest.MonkeyPatch
):
    address, received = collector
    proxied: list[str] = []

    class Proxy(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            proxied.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return None

    proxy = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('HTTP_PROXY', f'http://127.0.0.1:{proxy.server_address[1]}')
    monkeypatch.setenv('http_proxy', f'http://127.0.0.1:{proxy.server_address[1]}')
    monkeypatch.delenv('NO_PROXY', raising=False)
    monkeypatch.delenv('no_proxy', raising=False)
    store = await _stored(_spans())
    try:
        await OtlpHttpExporter(
            f'{address}/v1/traces', redactor=RuleRedactor(), headers={'x-token': 'abc'}
        ).export(store, _TRACE)
    finally:
        proxy.shutdown()
        proxy.server_close()

    assert (len(received), proxied) == (1, [])


async def test_an_unfinished_trace_is_refused_before_anything_is_sent_and_not_offered_again():
    store = await _stored(_spans(), status=None, ended_ns=None)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        exporter = OtlpHttpExporter(_URL, client=client, redactor=RuleRedactor())
        with pytest.raises(OtlpExportError) as caught:
            await exporter.export(store, _TRACE)

    assert seen == []
    assert '记录到一半就断了' in caught.value.received
    assert caught.value.resendable is False


@pytest.mark.parametrize(
    ('outcome', 'code'),
    [
        ('error', 2),
        ('aborted', 2),
        ('judge_failed', 2),
        ('unknown_tool', 2),
        ('bad_arguments', 2),
        ('no_channel', 2),
        ('ok', 1),
        ('blocked', 1),
        ('rejected', 1),
        ('skipped', 1),
        ('not_executed', 1),
    ],
)
async def test_a_tool_call_that_failed_by_itself_is_an_error_and_a_refused_one_is_not(
    outcome: str, code: int
):
    tool = _span(
        1,
        0,
        kind=SpanKind.TOOL_CALL,
        name='Bash',
        attributes={'tool.name': 'Bash', 'tool.outcome': outcome},
    )
    store = await _stored([tool])

    _, [request] = await _export(store)

    [span] = _sent_spans(request)
    assert span['status'] == (
        {'code': 2, 'message': f'tool.outcome={outcome}'} if code == 2 else {'code': 1}
    )
