"""把一个 trace 从 `TraceStore` 读出来，按 OTLP/HTTP JSON 发给 collector（M3 D14）。

只从 store 读，所以发出去的内容在记录时已经脱敏。导出时自由文本（span 名、字符串属性、事件名、
命令行参数、资源属性）再经宿主的脱敏器逐叶过一遍；结构（id、时间、整数、闭集取值、payload 引用与
内核生成的标识）不过用户规则：这些十进制与十六进制的字符串是导出时才生成的，记录时从没过过用户
规则，一条认得数字的规则会把时间戳换成占位符，collector 整批拒收。发出的正文仍经 `encode_redacted`
（只用内置规则，它们只认凭证的形状）编码，写入点守卫因此不必为这里开例外。

id 是十六进制字符串，时间是 Unix 纳秒的十进制字符串，枚举是整数，`kind` 一律 INTERNAL；因果边
转成 span links。payload 属性只发内容的 sha256：正文可能很大，collector 大多限制属性长度。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from syndica.kernel import (
    AttributeValue,
    CausalityEdge,
    RuleRedactor,
    Span,
    SpanStatus,
    SyndicaError,
    TraceHeader,
    TraceStore,
)
from syndica.kernel.redaction import Redactor, encode_redacted
from syndica.kernel.trace import verbatim_attribute

__all__ = ['OtlpExportError', 'OtlpHttpExporter', 'otlp_documents']

# OTLP 的 SpanKind 与 StatusCode（opentelemetry-proto trace.proto）。
_KIND_INTERNAL = 1
_STATUS_OK = 1
_STATUS_ERROR = 2
# 一次请求最多带的 span 数（OpenTelemetry SDK 批处理的默认值）：一个长跑的 trace 一次发完，
# 会超过 collector 对请求体的上限。
_BATCH = 512
# 单次请求的超时与整次导出的上限：httpx 的超时按每一步算，一个一点点回字节的 collector 能让它
# 一直等下去。
_TIMEOUT = 10.0
_DEADLINE = 120.0
# 编码正文用的脱敏器：只有内置规则（凭证的形状），自由文本在组装时已经过了宿主的脱敏器。
_ENCODER = RuleRedactor()


class OtlpExportError(SyndicaError):
    """collector 没有收下（连不上、拒收、只收了一部分、超时）。`resendable` 为假时重发会让 collector
    收到重复的 span（它已经收下了其余的）。"""

    resendable = True


class _PartlyRejected(OtlpExportError):
    resendable = False


class _Incomplete(OtlpExportError):
    resendable = False


class _PartlySent(OtlpExportError):
    """前面的批次 collector 已经收下：整个重发会重复它们。"""

    resendable = False


# 这次调用自己没成：工具抛了、判定器抛了、没有这个工具、参数解析不了、整批因它停下、要确认却没有
# 通道。阻断、拒绝、跳过这些是处置的结论，不是失败：照样发 `tool.outcome`，状态不改。
_FAILED_OUTCOMES = frozenset(
    {'error', 'aborted', 'judge_failed', 'unknown_tool', 'bad_arguments', 'no_channel'}
)


def otlp_documents(
    header: TraceHeader,
    spans: Sequence[Span],
    edges: Sequence[CausalityEdge],
    *,
    redactor: Redactor,
    resource: Mapping[str, str] | None = None,
    batch: int = _BATCH,
) -> Iterator[dict[str, Any]]:
    """一个 trace 的 OTLP/HTTP JSON 请求体，每份最多 `batch` 个 span，span 保持 store 里的顺序，
    用到时才组装。没有 span 时一份也没有。`resource` 是宿主给的资源属性（用户环境里的
    `OTEL_SERVICE_NAME` 与 `OTEL_RESOURCE_ATTRIBUTES`），同名时盖过 syndica 自己的。"""
    _checked_batch(batch)
    text = _Text(redactor)
    links = _links(header.trace_id, spans, edges, text)
    attributes = _resource_attributes(header, resource or {}, text)
    scope: dict[str, Any] = {'name': 'syndica'}
    if header.source.version:
        scope['version'] = text(header.source.version)
    for start in range(0, len(spans), batch):
        chunk = [_span(header.trace_id, span, links, text) for span in spans[start : start + batch]]
        yield {
            'resourceSpans': [
                {
                    'resource': {'attributes': attributes},
                    'scopeSpans': [{'scope': scope, 'spans': chunk}],
                }
            ]
        }


class OtlpHttpExporter:
    """`url` 是完整的 traces 端点（`.../v1/traces`）；`headers` 原样随每次请求发出。`client` 给了就
    用它且不关它（测试注入 `MockTransport`），没给时每次导出自建一个并在结束时关掉；自建的只在
    https 下走代理：明文 HTTP 经代理，头与正文就以明文交给了代理。"""

    def __init__(
        self,
        url: str,
        *,
        redactor: Redactor,
        headers: Mapping[str, str] | None = None,
        resource: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        batch: int = _BATCH,
        timeout: float = _TIMEOUT,
        deadline: float = _DEADLINE,
    ) -> None:
        _checked_batch(batch)
        self._url = url
        self._redactor = redactor
        self._headers = dict(headers or {})
        self._resource = dict(resource or {})
        self._client = client
        self._batch = batch
        self._timeout = timeout
        self._deadline = deadline

    async def export(self, store: TraceStore, trace_id: str) -> int:
        """发出这个 trace 的全部 span，交回发出的个数。"""
        header = await store.get(trace_id)
        if header is None:
            raise OtlpExportError(
                expected=f'store 里有 trace {trace_id}',
                received='没有',
                next_action='确认 trace id 没写错',
            )
        if header.status is None:
            raise _Incomplete(
                expected='导出的 trace 有结局（记录完整）',
                received=f'trace {trace_id} 记录到一半就断了：写入失败，或者那次 run 还没结束',
                next_action='等它结束，或者换一个完整的 trace；不完整的 trace 不导出',
            )
        spans = await store.spans(trace_id)
        edges = await store.edges(trace_id)
        documents = otlp_documents(
            header,
            spans,
            edges,
            redactor=self._redactor,
            resource=self._resource,
            batch=self._batch,
        )
        client = self._client or httpx.AsyncClient(
            timeout=self._timeout, trust_env=urlsplit(self._url).scheme == 'https'
        )
        sent = 0
        try:
            async with asyncio.timeout(self._deadline):
                for document in documents:
                    try:
                        await self._send(client, document)
                    except OtlpExportError as error:
                        raise _partly_sent(error, sent, len(spans)) from error
                    sent += _spans_in(document)
        except TimeoutError as error:
            raise _partly_sent(
                OtlpExportError(
                    expected=f'OTLP collector {_shown(self._url)} 在 {self._deadline:g} 秒里收完',
                    received='没有收完',
                    next_action='确认 collector 没有过载、网络通畅',
                ),
                sent,
                len(spans),
            ) from error
        finally:
            if self._client is None:
                await client.aclose()
        return len(spans)

    async def _send(self, client: httpx.AsyncClient, document: dict[str, Any]) -> None:
        shown = _shown(self._url)
        try:
            response = await client.post(
                self._url,
                content=encode_redacted(document, _ENCODER),
                headers={**self._headers, 'Content-Type': 'application/json'},
            )
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as error:
            raise OtlpExportError(
                expected=f'OTLP collector {shown} 收下 trace',
                received=f'请求没有完成：{type(error).__name__}: {error}',
                next_action=(
                    '确认 collector 在跑、地址与端口对（OTLP/HTTP 默认 4318，路径 /v1/traces）'
                ),
            ) from error
        if 300 <= response.status_code < 400:
            location = response.headers.get('location', '')
            raise OtlpExportError(
                expected=f'OTLP collector {shown} 以 2xx 收下 trace',
                received=f'HTTP {response.status_code}：它要改去 {location or "（没说去哪）"}',
                next_action='导出不跟着重定向走（鉴权头不该发给另一个地址）：确认那个地址可信，把端点换成它',
            )
        if response.status_code >= 400:
            raise OtlpExportError(
                expected=f'OTLP collector {shown} 以 2xx 收下 trace',
                received=f'HTTP {response.status_code}：{response.text[:300]}',
                next_action=(
                    '4xx 多半是路径或鉴权头不对（OTLP/HTTP 的路径是 /v1/traces）；'
                    '5xx 是 collector 自己出错，等它好了再发'
                ),
            )
        rejected, said = _rejected(response)
        if rejected:
            raise _PartlyRejected(
                expected=f'OTLP collector {shown} 收下每一个 span',
                received=f'它拒收了 {rejected} 个：{said[:300]}',
                next_action=(
                    '按 collector 的说明改它的配置；不要整个重发，其余的 span 它已经收下了'
                ),
            )


class _Text:
    """自由文本经宿主的脱敏器；同一段文字（工具名、角色名、闭集之外的取值）在一个 trace 里反复
    出现，只脱敏一次。"""

    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor
        self._seen: dict[str, str] = {}

    def __call__(self, value: str) -> str:
        done = self._seen.get(value)
        if done is None:
            done = self._seen[value] = str(self._redactor.redact(value))
        return done


def _span(
    trace_id: str,
    span: Span,
    links: Mapping[str, list[dict[str, Any]]],
    text: Callable[[str], str],
) -> dict[str, Any]:
    value: dict[str, Any] = {'traceId': trace_id, 'spanId': span.span_id}
    if span.parent_span_id is not None:
        value['parentSpanId'] = span.parent_span_id
    value.update(
        {
            'name': text(span.name),
            'kind': _KIND_INTERNAL,
            'startTimeUnixNano': str(span.start_ns),
            'endTimeUnixNano': str(span.end_ns),
            'attributes': [
                _attribute('syndica.span.kind', span.kind.value),
                _attribute('syndica.agent.id', span.agent_id),
                *_attributes(span.attributes, text),
            ],
            'events': [
                {
                    'timeUnixNano': str(event.time_ns),
                    'name': text(event.name),
                    'attributes': _attributes(event.attributes, text),
                }
                for event in span.events
            ],
            'links': links.get(span.span_id, []),
            'status': _status(span),
        }
    )
    return value


def _links(
    trace_id: str,
    spans: Sequence[Span],
    edges: Sequence[CausalityEdge],
    text: Callable[[str], str],
) -> dict[str, list[dict[str, Any]]]:
    """边挂在结果那一端（被派出的 Agent、消费消息的那一轮），指向起因；结果那一端不在 trace 里（派了
    出去却没跑起来）时挂在起因上指向它。`syndica.edge.points_at` 说这条 link 指向的是哪一端。"""
    present = {span.span_id for span in spans}
    links: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        if edge.target_span_id in present:
            holder, other, points_at = edge.target_span_id, edge.source_span_id, 'source'
        elif edge.source_span_id in present:
            holder, other, points_at = edge.source_span_id, edge.target_span_id, 'target'
        else:
            continue
        links.setdefault(holder, []).append(
            {
                'traceId': trace_id,
                'spanId': other,
                'attributes': [
                    _attribute('syndica.edge.kind', edge.kind.value),
                    _attribute('syndica.edge.points_at', points_at),
                    *_attributes(edge.attributes, text),
                ],
            }
        )
    return links


def _resource_attributes(
    header: TraceHeader, given: Mapping[str, str], text: Callable[[str], str]
) -> list[dict[str, Any]]:
    own: dict[str, AttributeValue] = {'service.name': 'syndica'}
    if header.source.version:
        own['service.version'] = text(header.source.version)
    own['syndica.trace.level'] = header.level.value
    if header.status is not None:
        own['syndica.trace.status'] = header.status.value
    if header.source.argv:
        own['process.command_args'] = [text(item) for item in header.source.argv]
    if header.source.replay_of is not None:
        own['syndica.trace.replay_of'] = header.source.replay_of
    for key, value in given.items():
        own[key] = text(value)
    return [_attribute(key, value) for key, value in own.items()]


def _attributes(
    attributes: Mapping[str, AttributeValue], text: Callable[[str], str]
) -> list[dict[str, Any]]:
    """OTLP 的属性没有空值：值是 None 的（没量到的 token 数、没有的 HTTP 状态）不发。原样记的
    属性（标识、摘要、闭集取值）原样发，其余的字符串经宿主的脱敏器。"""
    sent: list[dict[str, Any]] = []
    for key, value in attributes.items():
        if value is None:
            continue
        if not verbatim_attribute(key):
            value = _redacted(value, text)
        sent.append(_attribute(key, value))
    return sent


def _redacted(value: AttributeValue, text: Callable[[str], str]) -> AttributeValue:
    if isinstance(value, str):
        return text(value)
    if isinstance(value, list):
        return [text(item) for item in value]
    return value


def _attribute(key: str, value: AttributeValue) -> dict[str, Any]:
    return {'key': key, 'value': _any_value(value)}


def _any_value(value: AttributeValue) -> dict[str, Any]:
    # bool 是 int 的子类：先判它，否则 True 发成 intValue "1"。
    if isinstance(value, bool):
        return {'boolValue': value}
    if isinstance(value, int):
        return {'intValue': str(value)}
    if isinstance(value, str):
        return {'stringValue': value}
    if isinstance(value, list):
        return {'arrayValue': {'values': [_any_value(item) for item in value]}}
    raise TypeError(f'trace 属性的值是 str / int / bool / list[str]，收到 {type(value).__name__}')


def _status(span: Span) -> dict[str, Any]:
    outcome = span.attributes.get('tool.outcome')
    if span.status is SpanStatus.OK and outcome in _FAILED_OUTCOMES:
        return {'code': _STATUS_ERROR, 'message': f'tool.outcome={outcome}'}
    if span.status is SpanStatus.OK:
        return {'code': _STATUS_OK}
    return {'code': _STATUS_ERROR, 'message': span.status.value}


def _partly_sent(error: OtlpExportError, sent: int, total: int) -> OtlpExportError:
    """一批没发成。前面一个 span 都没收下时原样交出；收下过就不能整个重发了，说清收下了多少。"""
    if not sent:
        return error
    return _PartlySent(
        expected=error.expected,
        received=f'{error.received}；在这之前 collector 已经收下了 {sent} 个 span（共 {total} 个）',
        next_action=('不要整个重发：收下的那些会重复。' + error.next_action),
    )


def _spans_in(document: dict[str, Any]) -> int:
    return sum(
        len(scope['spans'])
        for resource in document['resourceSpans']
        for scope in resource['scopeSpans']
    )


def _rejected(response: httpx.Response) -> tuple[int, str]:
    """OTLP 的部分成功：200 里带着拒收了几个 span 与原因。正文不是 JSON（很多 collector 回空）时
    当成全收下。"""
    try:
        body: object = response.json()
    except (ValueError, RecursionError):
        return 0, ''
    if not isinstance(body, dict):
        return 0, ''
    partial = cast(Mapping[str, object], body).get('partialSuccess')
    if not isinstance(partial, dict):
        return 0, ''
    fields = cast(Mapping[str, object], partial)
    rejected = fields.get('rejectedSpans', 0)
    if isinstance(rejected, bool) or not isinstance(rejected, int | str):
        return 0, ''
    try:
        count = int(rejected)
    except ValueError:
        return 0, ''
    message = fields.get('errorMessage', '')
    return count, message if isinstance(message, str) else ''


def _shown(url: str) -> str:
    """报错里的端点只有协议、主机与端口：userinfo 是凭证，路径与查询里也常带着令牌。"""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return '（端点）'
    host = parts.hostname or ''
    if ':' in host:
        host = f'[{host}]'
    return f'{parts.scheme}://{host}' + ('' if port is None else f':{port}')


def _checked_batch(batch: int) -> None:
    if type(batch) is not int or batch < 1:
        raise ValueError(f'batch 是正整数，收到 {batch!r}')
