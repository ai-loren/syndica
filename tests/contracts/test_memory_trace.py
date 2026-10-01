from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from collections.abc import Sequence

import pytest

from syndica.kernel.contracts.trace import TraceStoreContract
from syndica.kernel.protocols.trace import TraceStore
from syndica.kernel.redaction import RedactedText
from syndica.kernel.stores.memory_trace import InMemoryTraceStore
from syndica.kernel.trace import (
    CausalityEdge,
    PayloadRef,
    Span,
    SpanStatus,
    TraceHeader,
    TraceLevel,
    TraceSource,
)


class TestInMemoryTraceStore(TraceStoreContract):
    @pytest.fixture
    def store(self) -> TraceStore:
        return InMemoryTraceStore()


def test_the_memory_store_is_a_trace_store():
    assert isinstance(InMemoryTraceStore(), TraceStore)


class _DropsTheLastSpan(InMemoryTraceStore):
    async def append(
        self, trace_id: str, spans: Sequence[Span] = (), edges: Sequence[CausalityEdge] = ()
    ) -> None:
        await super().append(trace_id, list(spans)[:-1], edges)


class _SharesWhatItStores(InMemoryTraceStore):
    """存下与交回的都是同一批对象：调用方改一处，store 里跟着变。"""

    def __init__(self) -> None:
        super().__init__()
        self._kept: dict[str, list[Span]] = {}
        self._kept_edges: dict[str, list[CausalityEdge]] = {}

    async def append(
        self, trace_id: str, spans: Sequence[Span] = (), edges: Sequence[CausalityEdge] = ()
    ) -> None:
        await super().append(trace_id, spans, edges)
        self._kept.setdefault(trace_id, []).extend(spans)
        self._kept_edges.setdefault(trace_id, []).extend(edges)

    async def spans(self, trace_id: str) -> list[Span]:
        return self._kept.get(trace_id, [])

    async def edges(self, trace_id: str) -> list[CausalityEdge]:
        return self._kept_edges.get(trace_id, [])


class _SharesEvents(_SharesWhatItStores):
    """拿 `dataclasses.replace` 当拷贝：属性重新校验时拷了，事件还是同一批对象。"""

    async def append(
        self, trace_id: str, spans: Sequence[Span] = (), edges: Sequence[CausalityEdge] = ()
    ) -> None:
        await super().append(trace_id, [dataclasses.replace(span) for span in spans], edges)


class _AcceptsAfterFinish(InMemoryTraceStore):
    async def finish(self, trace_id: str, status: SpanStatus, ended_ns: int) -> None:
        """认得 trace，却不关门：之后的写照收。"""
        self._open(trace_id, 'finish')


class _AcceptsPlainText(InMemoryTraceStore):
    def __init__(self) -> None:
        super().__init__()
        self._plain: dict[str, str] = {}

    async def put_payload(self, trace_id: str, payload: RedactedText) -> PayloadRef:
        digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        self._plain[digest] = payload
        return PayloadRef(trace_id, digest)


class _ForgetsHeaderDetails(InMemoryTraceStore):
    async def begin(self, header: TraceHeader) -> None:
        await super().begin(dataclasses.replace(header, level=TraceLevel.OFF, source=TraceSource()))


class _FlattensSpans(InMemoryTraceStore):
    """父 span 一律写成 None、结局一律写成 OK：固定装置只有一种取值时看不出来。"""

    async def append(
        self, trace_id: str, spans: Sequence[Span] = (), edges: Sequence[CausalityEdge] = ()
    ) -> None:
        flat = [
            dataclasses.replace(span, parent_span_id=None, status=SpanStatus.OK) for span in spans
        ]
        await super().append(trace_id, flat, edges)


class _KeepsTheFirstEventAndSortsLists(InMemoryTraceStore):
    async def spans(self, trace_id: str) -> list[Span]:
        return [
            dataclasses.replace(
                span,
                events=span.events[:1],
                attributes={
                    key: sorted(value) if isinstance(value, list) else value
                    for key, value in span.attributes.items()
                },
            )
            for span in await super().spans(trace_id)
        ]


class _DropsNoneAttributes(InMemoryTraceStore):
    async def spans(self, trace_id: str) -> list[Span]:
        return [
            dataclasses.replace(
                span,
                attributes={k: v for k, v in span.attributes.items() if v is not None},
            )
            for span in await super().spans(trace_id)
        ]


class _WritesSpansBeforeCheckingEdges(InMemoryTraceStore):
    async def append(
        self, trace_id: str, spans: Sequence[Span] = (), edges: Sequence[CausalityEdge] = ()
    ) -> None:
        await super().append(trace_id, spans)
        await super().append(trace_id, (), edges)


class _KeepsARefusedPayload(InMemoryTraceStore):
    """先存下，再检查 trace 还开着没有。"""

    def __init__(self) -> None:
        super().__init__()
        self._early: dict[tuple[str, str], str] = {}

    async def put_payload(self, trace_id: str, payload: RedactedText) -> PayloadRef:
        digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        self._early[(trace_id, digest)] = str(payload)
        return await super().put_payload(trace_id, payload)

    async def payload(self, ref: PayloadRef) -> str | None:
        return self._early.get((ref.trace_id, ref.digest))


class _AcceptsWritesAfterDelete(InMemoryTraceStore):
    """手里攥着每个 trace 的写入口：delete 删了目录，写入口还在，写照常返回、数据丢了。"""

    def __init__(self) -> None:
        super().__init__()
        self._deleted: set[str] = set()

    async def delete(self, trace_id: str) -> None:
        if await self.get(trace_id) is not None:
            self._deleted.add(trace_id)
        await super().delete(trace_id)

    async def append(
        self, trace_id: str, spans: Sequence[Span] = (), edges: Sequence[CausalityEdge] = ()
    ) -> None:
        if trace_id in self._deleted:
            return
        await super().append(trace_id, spans, edges)


class _RacyCheckThenWrite(InMemoryTraceStore):
    """检查与写入之间让出一次控制权：远端 store 的往返就在这里。"""

    async def begin(self, header: TraceHeader) -> None:
        exists = await self.get(header.trace_id) is not None
        await asyncio.sleep(0)
        if not exists:
            await InMemoryTraceStore.delete(self, header.trace_id)
        await super().begin(header)

    async def put_replay(self, trace_id: str, document: RedactedText) -> None:
        already = await self.replay(trace_id) is not None
        await asyncio.sleep(0)
        trace = self._open(trace_id, 'put_replay')
        if already:
            await super().put_replay(trace_id, document)
        trace.replay = str(document)


class _StaleFindIndex(InMemoryTraceStore):
    """按前缀找的索引在 begin 时建好，finish 不更新它。"""

    def __init__(self) -> None:
        super().__init__()
        self._index: dict[str, TraceHeader] = {}

    async def begin(self, header: TraceHeader) -> None:
        await super().begin(header)
        self._index[header.trace_id] = header

    async def find(self, prefix: str) -> TraceHeader | None:
        found = await super().find(prefix)
        return None if found is None else self._index[found.trace_id]


class _MatchesEightCharacters(InMemoryTraceStore):
    """目录名里只有 trace id 的前 8 位：按目录名比前缀。"""

    async def find(self, prefix: str) -> TraceHeader | None:
        return await super().find(prefix[:8])


class _SharesTheTraceList(InMemoryTraceStore):
    def __init__(self) -> None:
        super().__init__()
        self._listed: list[TraceHeader] = []

    async def begin(self, header: TraceHeader) -> None:
        await super().begin(header)
        self._listed.append(header)

    async def traces(self) -> list[TraceHeader]:
        return self._listed


class _GlobalPayloadPool(InMemoryTraceStore):
    def __init__(self) -> None:
        super().__init__()
        self._pool: dict[str, str] = {}

    async def put_payload(self, trace_id: str, payload: RedactedText) -> PayloadRef:
        ref = await super().put_payload(trace_id, payload)
        self._pool[ref.digest] = str(payload)
        return ref

    async def payload(self, ref: PayloadRef) -> str | None:
        if await self.get(ref.trace_id) is None:
            return None
        return self._pool.get(ref.digest)


class _DedupesOnlyAgainstTheLast(InMemoryTraceStore):
    """只跟上一次比：A、B、A 的第三次照常去建 `<sha256>.txt`，撞上已有的文件。"""

    def __init__(self) -> None:
        super().__init__()
        self._last: dict[str, str] = {}
        self._files: set[tuple[str, str]] = set()

    async def put_payload(self, trace_id: str, payload: RedactedText) -> PayloadRef:
        digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        if self._last.get(trace_id) == digest:
            return PayloadRef(trace_id, digest)
        self._last[trace_id] = digest
        if (trace_id, digest) in self._files:
            raise FileExistsError(f'{digest}.txt')
        self._files.add((trace_id, digest))
        return await super().put_payload(trace_id, payload)


class _HandsBackRedactedText(InMemoryTraceStore):
    def __init__(self) -> None:
        super().__init__()
        self._kept: dict[tuple[str, str], RedactedText] = {}

    async def put_payload(self, trace_id: str, payload: RedactedText) -> PayloadRef:
        ref = await super().put_payload(trace_id, payload)
        self._kept[(trace_id, ref.digest)] = payload
        return ref

    async def payload(self, ref: PayloadRef) -> str | None:
        return self._kept.get((ref.trace_id, ref.digest))


class _AcceptsAnyOutcome(InMemoryTraceStore):
    async def finish(self, trace_id: str, status: SpanStatus, ended_ns: int) -> None:
        self._open(trace_id, 'finish')
        if isinstance(status, SpanStatus) and isinstance(ended_ns, int):
            await super().finish(trace_id, status, max(ended_ns, 10))


@pytest.mark.parametrize(
    ('broken', 'case', 'anchor'),
    [
        (_DropsTheLastSpan, 'test_spans_and_edges_come_back_in_append_order', 'append 的调用顺序'),
        (_SharesWhatItStores, 'test_a_read_is_a_snapshot', '深拷贝'),
        (_SharesWhatItStores, 'test_what_was_appended_is_a_copy', '深拷贝'),
        (_SharesEvents, 'test_what_was_appended_is_a_copy', 'dataclasses.replace'),
        (_AcceptsAfterFinish, 'test_writes_need_a_begun_unfinished_trace', '已经 finish'),
        (_AcceptsPlainText, 'test_free_text_must_be_redacted_text', 'RedactedText'),
        (_ForgetsHeaderDetails, 'test_a_header_comes_back_whole', 'replay_of'),
        (_FlattensSpans, 'test_every_field_of_a_span_comes_back_as_written', '父 span'),
        (
            _KeepsTheFirstEventAndSortsLists,
            'test_every_field_of_a_span_comes_back_as_written',
            '别排序',
        ),
        (_DropsNoneAttributes, 'test_every_field_of_a_span_comes_back_as_written', 'None'),
        (_WritesSpansBeforeCheckingEdges, 'test_a_refused_append_writes_nothing', '坏边'),
        (_KeepsARefusedPayload, 'test_writes_need_a_begun_unfinished_trace', 'put_payload'),
        (_AcceptsWritesAfterDelete, 'test_writes_after_delete_are_refused', '已经 delete'),
        (
            _RacyCheckThenWrite,
            'test_concurrent_begins_of_one_trace_let_exactly_one_through',
            'SET NX',
        ),
        (
            _RacyCheckThenWrite,
            'test_concurrent_replay_documents_let_exactly_one_through',
            '恰好一次',
        ),
        (_StaleFindIndex, 'test_a_header_comes_back_whole', '索引'),
        (_MatchesEightCharacters, 'test_find_needs_a_unique_prefix', '前 8 个'),
        (_SharesTheTraceList, 'test_the_list_of_traces_is_a_snapshot', '新列表'),
        (_GlobalPayloadPool, 'test_payload_references_belong_to_their_trace', '按 trace 存'),
        (
            _DedupesOnlyAgainstTheLast,
            'test_payloads_are_addressed_by_their_sha256_and_stored_once',
            '.txt',
        ),
        (
            _HandsBackRedactedText,
            'test_payloads_are_addressed_by_their_sha256_and_stored_once',
            '普通 str',
        ),
        (_AcceptsAnyOutcome, 'test_finish_refuses_an_outcome_it_cannot_record', 'SpanStatus'),
    ],
    ids=[
        'drops-the-last-span',
        'shares-reads',
        'shares-writes',
        'shares-events',
        'open-after-finish',
        'plain-str',
        'forgets-header-details',
        'flattens-parents-and-outcomes',
        'first-event-and-sorted-lists',
        'drops-none-attributes',
        'spans-before-edges-are-checked',
        'keeps-a-refused-payload',
        'writes-after-delete',
        'racy-begin',
        'racy-replay',
        'stale-find-index',
        'eight-character-prefix',
        'shares-the-trace-list',
        'global-payload-pool',
        'dedupes-only-against-the-last',
        'hands-back-redacted-text',
        'accepts-any-outcome',
    ],
)
async def test_the_contract_catches_a_store_that_breaks_it(
    broken: type[InMemoryTraceStore], case: str, anchor: str
):
    error = FileExistsError if broken is _DedupesOnlyAgainstTheLast else AssertionError
    with pytest.raises(error) as caught:
        await getattr(TraceStoreContract(), case)(broken())

    assert anchor in str(caught.value)
