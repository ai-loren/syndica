"""进程内的 TraceStore：测试与不落盘的宿主用。语义见 `syndica.kernel.protocols.trace`。"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TypeVar, cast

from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.redaction import RedactedText
from syndica.kernel.trace import CausalityEdge, PayloadRef, Span, SpanStatus, TraceHeader

__all__ = ['InMemoryTraceStore']

_PREFIX = re.compile(r'[0-9a-f]{1,32}')
_T = TypeVar('_T')


@dataclass
class _Trace:
    header: TraceHeader
    spans: list[Span] = field(default_factory=list[Span])
    edges: list[CausalityEdge] = field(default_factory=list[CausalityEdge])
    payloads: dict[str, str] = field(default_factory=dict[str, str])
    replay: str | None = None


class InMemoryTraceStore:
    """所有操作都不挂起，所以并发的写天然是原子的：一次 `append` 整批进去，`begin` 与
    `put_replay` 的检查与写入之间没有别人插进来。"""

    def __init__(self) -> None:
        self._traces: dict[str, _Trace] = {}

    async def begin(self, header: TraceHeader) -> None:
        if not isinstance(cast(object, header), TraceHeader):
            raise ContractViolation(
                expected='begin 收一个 TraceHeader',
                received=f'收到 {type(header).__name__}',
                next_action='用 TraceHeader(trace_id, started_ns, level, redaction) 构造',
            )
        if header.trace_id in self._traces:
            raise ContractViolation(
                expected='每个 trace id 只 begin 一次',
                received=f'trace {header.trace_id} 已经开始过',
                next_action='换一个新生成的 trace id；继续往已有的 trace 里写用 append',
            )
        if header.status is not None:
            raise ContractViolation(
                expected='begin 时 trace 还没有结局（ended_ns 与 status 都是 None）',
                received=f'trace {header.trace_id} 带着 status={header.status!r}',
                next_action='结局由 finish 写入',
            )
        self._traces[header.trace_id] = _Trace(header)

    async def append(
        self,
        trace_id: str,
        spans: Sequence[Span] = (),
        edges: Sequence[CausalityEdge] = (),
    ) -> None:
        trace = self._open(trace_id, 'append')
        new_spans = _checked(spans, Span, 'spans')
        new_edges = _checked(edges, CausalityEdge, 'edges')
        trace.spans += copy.deepcopy(new_spans)
        trace.edges += copy.deepcopy(new_edges)

    async def put_payload(self, trace_id: str, payload: RedactedText) -> PayloadRef:
        trace = self._open(trace_id, 'put_payload')
        text = _redacted(payload, 'put_payload')
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        trace.payloads.setdefault(digest, str(text))
        return PayloadRef(trace_id, digest)

    async def put_replay(self, trace_id: str, document: RedactedText) -> None:
        trace = self._open(trace_id, 'put_replay')
        text = _redacted(document, 'put_replay')
        if trace.replay is not None:
            raise ContractViolation(
                expected='每个 trace 至多一份回放文档',
                received=f'trace {trace_id} 已经存过回放文档',
                next_action='在 finish 之前把回放文档一次写完',
            )
        trace.replay = str(text)

    async def finish(self, trace_id: str, status: SpanStatus, ended_ns: int) -> None:
        trace = self._open(trace_id, 'finish')
        given = cast(object, ended_ns)
        if (
            not isinstance(cast(object, status), SpanStatus)
            or not isinstance(given, int)
            or isinstance(given, bool)
            or given < trace.header.started_ns
        ):
            raise ContractViolation(
                expected='finish 收一个 SpanStatus 与不早于开始时间的整数纳秒',
                received=(
                    f'status={status!r}，ended_ns={ended_ns!r}，'
                    f'trace 开始于 {trace.header.started_ns}'
                ),
                next_action='传 SpanStatus.OK / ERROR / CANCELLED 与 finish 那一刻的时钟',
            )
        trace.header = dataclasses.replace(trace.header, status=status, ended_ns=ended_ns)

    async def traces(self) -> list[TraceHeader]:
        headers = [trace.header for trace in self._traces.values()]
        return sorted(headers, key=lambda header: (header.started_ns, header.trace_id))

    async def find(self, prefix: str) -> TraceHeader | None:
        if not isinstance(cast(object, prefix), str) or not _PREFIX.fullmatch(prefix):
            raise ConfigError(
                expected='trace id 前缀是 1 到 32 个小写十六进制字符',
                received=f'收到 {prefix!r}',
                next_action='从 `syndica trace list` 的 id 列抄一段开头',
            )
        hits = sorted(trace_id for trace_id in self._traces if trace_id.startswith(prefix))
        if len(hits) > 1:
            raise ConfigError(
                expected=f'前缀 {prefix} 只对应一个 trace',
                received=f'对应 {len(hits)} 个：{", ".join(hits)}',
                next_action='多给几个字符，直到只剩一个',
            )
        return self._traces[hits[0]].header if hits else None

    async def get(self, trace_id: str) -> TraceHeader | None:
        trace = self._traces.get(trace_id)
        return None if trace is None else trace.header

    async def spans(self, trace_id: str) -> list[Span]:
        trace = self._traces.get(trace_id)
        return [] if trace is None else copy.deepcopy(trace.spans)

    async def edges(self, trace_id: str) -> list[CausalityEdge]:
        trace = self._traces.get(trace_id)
        return [] if trace is None else copy.deepcopy(trace.edges)

    async def payload(self, ref: PayloadRef) -> str | None:
        trace = self._traces.get(ref.trace_id)
        return None if trace is None else trace.payloads.get(ref.digest)

    async def replay(self, trace_id: str) -> str | None:
        trace = self._traces.get(trace_id)
        return None if trace is None else trace.replay

    async def delete(self, trace_id: str) -> None:
        self._traces.pop(trace_id, None)

    def _open(self, trace_id: str, method: str) -> _Trace:
        trace = self._traces.get(trace_id)
        if trace is None:
            raise ContractViolation(
                expected=f'{method} 写的是一个 begin 过、还没删掉的 trace',
                received=f'trace {trace_id!r} 不存在（没有开始过，或者已经删掉了）',
                next_action='先 begin(header)，再往里写',
            )
        if trace.header.status is not None:
            raise ContractViolation(
                expected=f'{method} 写的是一个还没 finish 的 trace',
                received=f'trace {trace_id} 已经以 {trace.header.status.value} 结束',
                next_action='trace 结束之后只读；新的记录开一个新的 trace',
            )
        return trace


def _checked(items: Sequence[_T], kind: type[_T], name: str) -> list[_T]:
    given = cast(object, items)
    if isinstance(given, str | bytes) or not isinstance(given, Sequence):
        raise ContractViolation(
            expected=f'append 的 {name} 是 {kind.__name__} 的序列',
            received=f'收到 {type(given).__name__}',
            next_action='传一个列表或元组',
        )
    checked = list(items)
    for index, item in enumerate(checked):
        if not isinstance(cast(object, item), kind):
            raise ContractViolation(
                expected=f'append 的 {name} 每一项是 {kind.__name__}',
                received=f'{name}[{index}] 是 {type(item).__name__}',
                next_action=f'用 {kind.__name__}(...) 构造每一项',
            )
    return checked


def _redacted(text: RedactedText, method: str) -> RedactedText:
    if not isinstance(cast(object, text), RedactedText):
        raise ContractViolation(
            expected=f'{method} 只收 RedactedText（逐叶脱敏之后的编码）',
            received=f'收到 {type(text).__name__}',
            next_action='先经 syndica.kernel.redaction.encode_redacted 编码；普通 str 可能带着密钥',
        )
    return text
