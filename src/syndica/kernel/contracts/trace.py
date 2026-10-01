"""TraceStore 的契约套件（M3 D6）。语义见 `syndica.kernel.protocols.trace`；运行前提见
`syndica.kernel.contracts.base`。

用法：子类提供一个名为 `store` 的 fixture，交回一个空的 TraceStore 实现。

固定装置刻意让每个字段都有不止一种取值（有父与无父、三种结局、三个时间与名字都不单调的事件、三项
既非升序也非降序的列表、布尔与 None 属性、每个字段都不是默认值的头）：一个字段只有一种取值时，把它
写死、排序、丢掉的实现照样过。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any, cast

from syndica.kernel.contracts.base import ContractSuite
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.protocols.trace import TraceStore
from syndica.kernel.redaction import RedactedText, RedactionRule, RuleRedactor, encode_redacted
from syndica.kernel.trace import (
    CausalityEdge,
    EdgeKind,
    PayloadRef,
    Span,
    SpanEvent,
    SpanKind,
    SpanStatus,
    TraceHeader,
    TraceLevel,
    TraceRedaction,
    TraceSource,
)

__all__ = ['TraceStoreContract']

_TRACE = 'ab12' + '0' * 28
_OTHER = 'ab34' + '0' * 28
_THIRD = 'cd56' + '0' * 28


def _header(trace_id: str = _TRACE, started_ns: int = 1) -> TraceHeader:
    return TraceHeader(trace_id, started_ns, TraceLevel.REPLAY, TraceRedaction('0' * 64, 1))


def _rich_header() -> TraceHeader:
    return TraceHeader(
        _TRACE,
        1_700_000_000_000_000_000,
        TraceLevel.FULL,
        TraceRedaction(
            '7f' * 32,
            3,
            (
                RedactionRule('custom.api_token', r'tok_[A-Za-z0-9]{32}'),
                RedactionRule('custom.host', r'corp-[a-z]+'),
            ),
        ),
        TraceSource(
            argv=('run', '--trace', 'full', 'ship it'),
            config_sha256='5e' * 32,
            version='0.3.0',
            git_commit='06cf340',
            replay_of=_THIRD,
        ),
    )


def _span(number: int, **attributes: Any) -> Span:
    return Span(
        span_id=f'{number:016x}',
        parent_span_id=None,
        kind=SpanKind.TURN,
        name=f'turn {number}',
        agent_id='a1',
        start_ns=number,
        end_ns=number + 1,
        status=SpanStatus.OK,
        attributes={'turn': number, **attributes},
    )


def _shaped_spans() -> list[Span]:
    root = 'a0' * 8
    return [
        Span(
            span_id=root,
            parent_span_id=None,
            kind=SpanKind.AGENT,
            name='Worker',
            agent_id='a1',
            start_ns=10,
            end_ns=90,
            status=SpanStatus.OK,
            attributes={'agent.role': 'Worker', 'agent.cycle': 2},
        ),
        Span(
            span_id='b1' * 8,
            parent_span_id=root,
            kind=SpanKind.TOOL_CALL,
            name='Bash',
            agent_id='a1',
            start_ns=20,
            end_ns=40,
            status=SpanStatus.ERROR,
            attributes={
                'tool.name': 'Bash',
                'tool.index': 1,
                'tool.timed_out': True,
                'tool.exit_code': None,
                'danger.rule_ids': ['shell.write', 'git.state', 'path.outside'],
            },
            events=(
                SpanEvent('z.last', 39, {'message.to': 'a3'}),
                SpanEvent('a.first', 21, {'message.id': 'm1'}),
                SpanEvent('m.middle', 30),
            ),
        ),
        Span(
            span_id='c2' * 8,
            parent_span_id=root,
            kind=SpanKind.WAIT,
            name='wait',
            agent_id='a2',
            start_ns=50,
            end_ns=50,
            status=SpanStatus.CANCELLED,
            attributes={
                'wait.kind': 'human',
                'wait.target': 'confirm:1',
                'wait.outcome': 'aborted',
                'tool.timed_out': False,
                'llm.input_tokens': None,
            },
        ),
    ]


def _shaped_edges() -> list[CausalityEdge]:
    return [
        CausalityEdge(EdgeKind.SPAWN, 'a0' * 8, 'c2' * 8, {'child.agent_id': 'a2'}),
        CausalityEdge(EdgeKind.MESSAGE, 'b1' * 8, 'c2' * 8, {'message.id': 'm1'}),
    ]


def _edge(number: int) -> CausalityEdge:
    return CausalityEdge(
        EdgeKind.MESSAGE, f'{number:016x}', f'{number + 1:016x}', {'message.id': f'm{number}'}
    )


def _text(value: str) -> RedactedText:
    return encode_redacted(value, RuleRedactor())


def _numbers(spans: list[Span]) -> list[int]:
    return [int(span.span_id, 16) for span in spans]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _writes(store: TraceStore, trace_id: str) -> list[tuple[str, Callable[[], Awaitable[Any]]]]:
    return [
        ('append', lambda: store.append(trace_id, [_span(9)], [_edge(9)])),
        ('an empty append', lambda: store.append(trace_id)),
        ('put_payload', lambda: store.put_payload(trace_id, _text('late'))),
        ('put_replay', lambda: store.put_replay(trace_id, _text('late'))),
        ('finish', lambda: store.finish(trace_id, SpanStatus.ERROR, 10**6)),
    ]


async def _refused_writes(store: TraceStore, trace_id: str, why: str) -> None:
    for name, write in _writes(store, trace_id):
        try:
            await write()
        except ContractViolation:
            continue
        raise AssertionError(
            f'期望往{why}的 trace 上 {name} 报 ContractViolation；实际没有报错。'
            ' 写之前先确认这个 trace 存在且还开着。'
        )
    assert await store.payload(PayloadRef(trace_id, _digest(_text('late')))) is None, (
        f'期望往{why}的 trace 上被拒的 put_payload 读不回来；实际读回来了。先检查，再写。'
    )


class TraceStoreContract(ContractSuite):
    async def test_spans_and_edges_come_back_in_append_order(self, store: TraceStore) -> None:
        await store.begin(_header())
        await store.append(_TRACE, [_span(5), _span(2)], [_edge(7)])
        await store.append(_TRACE, [_span(9), _span(1)], [_edge(3), _edge(8)])

        spans, edges = await store.spans(_TRACE), await store.edges(_TRACE)

        assert _numbers(spans) == [5, 2, 9, 1], (
            '期望 span 按 append 的调用顺序读回（跨两次 append，不按 id 排序、不丢）；'
            f'实际读回 {_numbers(spans)}。按追加顺序存，读时原样交回。'
        )
        assert [edge.attributes['message.id'] for edge in edges] == ['m7', 'm3', 'm8'], (
            '期望边按 append 的调用顺序读回；实际 '
            f'{[edge.attributes.get("message.id") for edge in edges]}。'
        )

    async def test_every_field_of_a_span_comes_back_as_written(self, store: TraceStore) -> None:
        await store.begin(_header())
        await store.append(_TRACE, _shaped_spans(), _shaped_edges())

        spans, edges = await store.spans(_TRACE), await store.edges(_TRACE)

        assert spans == _shaped_spans(), (
            '期望每个字段原样读回：父 span、三种结局、事件的个数与顺序（时间与名字都不单调）、'
            f'列表属性里各项的顺序、None 属性；实际读回 {spans!r}。'
            '别排序、别丢 None、别写死默认值。'
        )
        assert spans[1].attributes['tool.timed_out'] is True, (
            '期望布尔属性读回来仍是布尔；实际 '
            f'{spans[1].attributes["tool.timed_out"]!r}。存成整数的实现读回时要转回 bool。'
        )
        assert spans[2].attributes['tool.timed_out'] is False, '期望 False 读回来仍是 False。'
        assert edges == _shaped_edges(), f'期望两种边原样读回；实际 {edges!r}。'

    async def test_a_header_comes_back_whole(self, store: TraceStore) -> None:
        header = _rich_header()
        await store.begin(header)

        assert await store.get(_TRACE) == header, (
            '期望 get 读回的头每个字段都与 begin 时一样（级别、规则集、来源，含 replay_of）；'
            f'实际 {await store.get(_TRACE)!r}。'
        )
        assert await store.find('ab12') == header, '期望 find 读回同一份头。'
        assert await store.traces() == [header], '期望 traces() 读回同一份头。'

        await store.finish(_TRACE, SpanStatus.ERROR, header.started_ns + 5)
        finished = dataclasses.replace(
            header, status=SpanStatus.ERROR, ended_ns=header.started_ns + 5
        )
        assert await store.get(_TRACE) == finished, (
            f'期望 finish 之后 get 读回带结局的头、其余不变；实际 {await store.get(_TRACE)!r}。'
        )
        assert await store.find('ab12') == finished, (
            '期望 finish 之后 find 读到结局；按前缀找的索引也要跟着更新。'
        )
        assert await store.traces() == [finished], '期望 finish 之后 traces() 读到结局。'

    async def test_a_read_is_a_snapshot(self, store: TraceStore) -> None:
        await store.begin(_header())
        await store.append(_TRACE, _shaped_spans(), [_edge(1)])

        read, edges = await store.spans(_TRACE), await store.edges(_TRACE)
        read[1].attributes['tool.index'] = 99
        cast(list[str], read[1].attributes['danger.rule_ids']).append('forged')
        read[1].events[0].attributes['message.to'] = 'elsewhere'
        edges[0].attributes['message.id'] = 'forged'
        read.clear()

        again, edges_again = await store.spans(_TRACE), await store.edges(_TRACE)
        assert again == _shaped_spans(), (
            '期望改读回的 span（含嵌套的属性、列表与事件）不影响再读同一个 trace；'
            f'实际再读得到 {again!r}。读时交回深拷贝。'
        )
        assert edges_again == [_edge(1)], (
            f'期望改读回的边不影响再读；实际再读得到 {edges_again!r}。读时交回深拷贝。'
        )

    async def test_the_list_of_traces_is_a_snapshot(self, store: TraceStore) -> None:
        await store.begin(_header(_TRACE))
        await store.begin(_header(_OTHER))

        listed = await store.traces()
        listed.clear()

        assert len(await store.traces()) == 2, (
            '期望清空 traces() 交回的列表不影响再读；实际再读少了。交回一份新列表。'
        )

    async def test_what_was_appended_is_a_copy(self, store: TraceStore) -> None:
        spans = _shaped_spans()
        edge = _edge(1)
        await store.begin(_header())
        await store.append(_TRACE, spans, [edge])

        spans[1].attributes['tool.index'] = 99
        cast(list[str], spans[1].attributes['danger.rule_ids']).append('forged')
        spans[1].events[0].attributes['message.to'] = 'forged'
        edge.attributes['message.id'] = 'forged'

        assert await store.spans(_TRACE) == _shaped_spans(), (
            '期望 append 返回之后调用方再改传进来的 span（含事件的属性），不影响已经存下的；'
            '实际受影响了。append 时存一份深拷贝，dataclasses.replace 不拷事件。'
        )
        assert await store.edges(_TRACE) == [_edge(1)], (
            '期望 append 返回之后调用方再改传进来的边，不影响已经存下的；实际受影响了。'
        )

    async def test_a_second_begin_is_refused(self, store: TraceStore) -> None:
        await store.begin(_header())
        await store.append(_TRACE, [_span(1)])

        try:
            await store.begin(_header(started_ns=5))
        except ContractViolation:
            pass
        else:
            raise AssertionError(
                '期望同一个 trace id 第二次 begin 报 ContractViolation；实际没有报错。'
                ' 第二次 begin 会把前一次记下的东西覆盖或者混进来。'
            )
        header = await store.get(_TRACE)
        assert header is not None and header.started_ns == 1, (
            f'期望被拒的 begin 什么都不改；实际头变成了 {header!r}。'
        )
        assert _numbers(await store.spans(_TRACE)) == [1], (
            '期望被拒的 begin 不清掉已有的 span；实际变了。'
        )

    async def test_concurrent_begins_of_one_trace_let_exactly_one_through(
        self, store: TraceStore
    ) -> None:
        results = await asyncio.gather(
            *(store.begin(_header(started_ns=started)) for started in (3, 1, 2)),
            return_exceptions=True,
        )

        assert results.count(None) == 1, (
            f'期望同一个 trace id 并发 begin 三次恰好一次成功；实际 {results!r}。检查与写入之间'
            '让出过控制权：用事务或条件写（SET NX、INSERT … ON CONFLICT）。'
        )
        assert all(isinstance(r, ContractViolation) for r in results if r is not None), (
            f'期望另外两次报 ContractViolation；实际 {results!r}。'
        )

    async def test_begin_refuses_a_header_that_already_has_an_outcome(
        self, store: TraceStore
    ) -> None:
        finished = dataclasses.replace(_header(), ended_ns=2, status=SpanStatus.OK)
        try:
            await store.begin(finished)
        except ContractViolation:
            pass
        else:
            raise AssertionError(
                '期望 begin 拒收已经带着结局的头（结局只由 finish 写）；实际收下了。'
            )
        assert await store.get(_TRACE) is None, '期望被拒的 begin 什么都不写；实际写进去了。'

    async def test_writes_need_a_begun_unfinished_trace(self, store: TraceStore) -> None:
        await _refused_writes(store, _OTHER, '没 begin 过')
        assert await store.get(_OTHER) is None, '期望被拒的写不会凭空建出一个 trace；实际建出来了。'

        await store.begin(_header())
        await store.append(_TRACE, [_span(1)], [_edge(1)])
        await store.finish(_TRACE, SpanStatus.OK, 5)
        await _refused_writes(store, _TRACE, '已经 finish ')
        header = await store.get(_TRACE)
        assert header is not None and (header.status, header.ended_ns) == (SpanStatus.OK, 5), (
            f'期望被拒的 finish 不改结局；实际头是 {header!r}。'
        )
        assert _numbers(await store.spans(_TRACE)) == [1], '期望被拒的 append 什么都不写。'
        assert len(await store.edges(_TRACE)) == 1, '期望被拒的 append 什么都不写。'
        assert await store.replay(_TRACE) is None, '期望被拒的 put_replay 什么都不写。'

    async def test_writes_after_delete_are_refused(self, store: TraceStore) -> None:
        await store.begin(_header())
        await store.delete(_TRACE)

        await _refused_writes(store, _TRACE, '已经 delete ')
        assert await store.get(_TRACE) is None, (
            '期望 delete 之后被拒的写不会让 trace 复活；实际它又出现了。'
        )

    async def test_finish_refuses_an_outcome_it_cannot_record(self, store: TraceStore) -> None:
        await store.begin(_header(started_ns=10))

        for status, ended in ((None, None), ('ok', 20), (SpanStatus.OK, 5), (SpanStatus.OK, True)):
            try:
                await store.finish(_TRACE, cast(Any, status), cast(Any, ended))
            except ContractViolation:
                continue
            raise AssertionError(
                f'期望 finish(status={status!r}, ended_ns={ended!r}) 报 ContractViolation'
                '（status 不是 SpanStatus，或 ended_ns 不是不早于开始时间的整数）；实际收下了。'
            )
        header = await store.get(_TRACE)
        assert header is not None and header.status is None, (
            f'期望被拒的 finish 让 trace 仍开着；实际头是 {header!r}。'
        )
        await store.append(_TRACE, [_span(1)])
        assert _numbers(await store.spans(_TRACE)) == [1], '期望被拒的 finish 之后仍能追加。'

    async def test_a_refused_append_writes_nothing(self, store: TraceStore) -> None:
        await store.begin(_header())

        for spans, edges in (
            ([_span(1), cast(Span, 'not a span'), _span(2)], []),
            ([_span(3)], [_edge(3), cast(CausalityEdge, 'not an edge')]),
        ):
            try:
                await store.append(_TRACE, spans, edges)
            except ContractViolation:
                continue
            raise AssertionError('期望 append 拒收不是 Span / CausalityEdge 的项；实际收下了。')
        assert await store.spans(_TRACE) == [], (
            '期望被拒的 append 一项都不写（拒绝即无副作用），混着一条坏边的那批 span 也不写；'
            '实际写进了。先校验 span 与边的整批，全部通过再写。'
        )
        assert await store.edges(_TRACE) == [], '期望被拒的 append 一条边都不写。'

    async def test_free_text_must_be_redacted_text(self, store: TraceStore) -> None:
        await store.begin(_header())

        for name, write in (
            ('put_payload', lambda: store.put_payload(_TRACE, cast(Any, 'raw secret'))),
            ('put_replay', lambda: store.put_replay(_TRACE, cast(Any, 'raw secret'))),
        ):
            try:
                await write()
            except ContractViolation:
                continue
            raise AssertionError(
                f'期望 {name} 收到普通 str 时报 ContractViolation；实际收下了。'
                ' 运行时检查 isinstance(text, RedactedText)：普通 str 可能带着密钥。'
            )
        assert await store.replay(_TRACE) is None, '期望被拒的 put_replay 什么都不写。'
        assert await store.payload(PayloadRef(_TRACE, _digest('raw secret'))) is None, (
            '期望被拒的 put_payload 什么都不写。'
        )

    async def test_payloads_are_addressed_by_their_sha256_and_stored_once(
        self, store: TraceStore
    ) -> None:
        await store.begin(_header())
        text, other = _text('tool output ä'), _text('another output')

        first = await store.put_payload(_TRACE, text)
        second = await store.put_payload(_TRACE, other)
        third = await store.put_payload(_TRACE, text)

        assert first == third == PayloadRef(_TRACE, _digest(text)), (
            '期望同一段内容交回同一个引用，引用是内容 UTF-8 编码的 sha256（由 store 算），中间隔着'
            f'别的内容也一样；实际 {first!r} 与 {third!r}。'
        )
        assert second == PayloadRef(_TRACE, _digest(other)), '期望不同的内容交回不同的引用。'
        read = await store.payload(first)
        assert read == text and type(read) is str, (
            f'期望按引用读回原文，类型是普通 str；实际 {read!r}（{type(read).__name__}）。'
        )
        assert await store.payload(second) == other, '期望按引用读回原文；实际不同。'
        unknown = PayloadRef(_TRACE, _digest('never stored'))
        assert await store.payload(unknown) is None, '期望没存过的引用读回 None。'

    async def test_payload_references_belong_to_their_trace(self, store: TraceStore) -> None:
        await store.begin(_header(_TRACE))
        await store.begin(_header(_OTHER))
        ref = await store.put_payload(_TRACE, _text('only in this trace'))

        assert await store.payload(PayloadRef(_OTHER, ref.digest)) is None, (
            '期望拿别的 trace 的 id 读同一个摘要交回 None；实际读到了。payload 按 trace 存，'
            'delete 一个 trace 才删得干净。'
        )

    async def test_the_replay_document_is_written_once(self, store: TraceStore) -> None:
        await store.begin(_header())
        await store.put_replay(_TRACE, _text('first'))

        try:
            await store.put_replay(_TRACE, _text('second'))
        except ContractViolation:
            pass
        else:
            raise AssertionError('期望第二次 put_replay 报 ContractViolation；实际收下了。')
        read = await store.replay(_TRACE)
        assert read == _text('first') and type(read) is str, (
            f'期望被拒的第二次 put_replay 不改已经存下的回放文档，读回的是普通 str；实际 {read!r}。'
        )

    async def test_concurrent_replay_documents_let_exactly_one_through(
        self, store: TraceStore
    ) -> None:
        await store.begin(_header())
        texts = [_text(f'replay {number}') for number in (3, 1, 2)]

        results = await asyncio.gather(
            *(store.put_replay(_TRACE, text) for text in texts), return_exceptions=True
        )

        winners = [text for text, result in zip(texts, results, strict=True) if result is None]
        assert len(winners) == 1, (
            f'期望并发三次 put_replay 恰好一次成功；实际 {results!r}。检查与写入之间让出过控制权。'
        )
        assert await store.replay(_TRACE) == winners[0], (
            '期望存下的回放文档是成功的那一次；实际是别的。'
        )

    async def test_finish_records_the_outcome(self, store: TraceStore) -> None:
        await store.begin(_header())
        await store.finish(_TRACE, SpanStatus.ERROR, 99)

        header = await store.get(_TRACE)
        listed = await store.traces()

        assert header is not None and (header.status, header.ended_ns) == (SpanStatus.ERROR, 99), (
            f'期望 finish 之后 get 读到结局；实际 {header!r}。'
        )
        assert [(h.status, h.ended_ns) for h in listed] == [(SpanStatus.ERROR, 99)], (
            f'期望 traces() 也读到结局；实际 {listed!r}。'
        )

    async def test_traces_are_listed_by_start_time_then_id(self, store: TraceStore) -> None:
        late = 'ff' + '1' * 30
        for trace_id, started in ((late, 30), (_THIRD, 10), (_OTHER, 20), (_TRACE, 20)):
            await store.begin(_header(trace_id, started))

        listed = [(header.started_ns, header.trace_id) for header in await store.traces()]

        assert listed == [(10, _THIRD), (20, _TRACE), (20, _OTHER), (30, late)], (
            f'期望 traces() 按开始时间、再按 trace id 排序（与写入顺序无关）；实际 {listed}。'
        )

    async def test_find_needs_a_unique_prefix(self, store: TraceStore) -> None:
        await store.begin(_header(_TRACE))
        try:
            await store.find('')
        except ConfigError:
            pass
        else:
            raise AssertionError(
                '期望空的前缀报 ConfigError，即使 store 里只有一个 trace；实际交回了它。'
                ' 空前缀不是在点名哪个 trace。'
            )
        for trace_id in (_OTHER, _THIRD):
            await store.begin(_header(trace_id))

        found = await store.find('ab1')
        assert found is not None and found.trace_id == _TRACE, (
            f'期望唯一的前缀 ab1 找到 {_TRACE}；实际 {found!r}。'
        )
        whole = await store.find(_THIRD)
        assert whole is not None and whole.trace_id == _THIRD, '期望完整的 id 也算前缀。'
        long_prefix = await store.find(_TRACE[:24])
        assert long_prefix is not None and long_prefix.trace_id == _TRACE, (
            '期望 24 个字符的前缀照样找得到；实际没找到。'
        )
        assert await store.find('ef') is None, '期望没有命中的前缀交回 None。'
        assert await store.find(_TRACE[:31] + '1') is None, (
            '期望前 31 个字符都对、最后一个不对的完整 id 交回 None；实际找到了。前缀要逐个字符比，'
            '不能只比目录名里的前 8 个。'
        )
        try:
            await store.find('ab')
        except ConfigError as exc:
            assert _TRACE in str(exc) and _OTHER in str(exc), (
                f'期望歧义的报错列出命中的 id；实际 {exc}。'
            )
        else:
            raise AssertionError('期望对应两个 trace 的前缀 ab 报 ConfigError；实际没有报错。')
        for malformed in ('AB12', 'xyz', 'a' * 33):
            try:
                await store.find(malformed)
            except ConfigError:
                continue
            raise AssertionError(
                f'期望前缀 {malformed!r}（不是 1 到 32 个小写十六进制字符）报 ConfigError；'
                '实际没有报错。'
            )

    async def test_delete_removes_the_whole_trace_and_only_it(self, store: TraceStore) -> None:
        refs: dict[str, PayloadRef] = {}
        for trace_id in (_TRACE, _OTHER):
            await store.begin(_header(trace_id))
            await store.append(trace_id, [_span(1)], [_edge(1)])
            refs[trace_id] = await store.put_payload(trace_id, _text('output'))
            await store.put_replay(trace_id, _text('replay'))

        await store.delete(_TRACE)
        await store.delete(_THIRD)

        assert await store.get(_TRACE) is None, '期望 delete 之后头没了。'
        assert await store.spans(_TRACE) == [], '期望 delete 之后 span 没了。'
        assert await store.edges(_TRACE) == [], '期望 delete 之后边没了。'
        assert await store.payload(refs[_TRACE]) is None, '期望 delete 之后 payload 没了。'
        assert await store.replay(_TRACE) is None, '期望 delete 之后回放文档没了。'
        assert [header.trace_id for header in await store.traces()] == [_OTHER], (
            '期望 delete 只删指定的 trace；实际别的 trace 也受影响了。'
        )
        assert await store.payload(refs[_OTHER]) == _text('output'), (
            '期望另一个 trace 里内容相同的 payload 不受影响；实际被一起删了。'
        )
        await store.begin(_header(_TRACE))
        assert await store.spans(_TRACE) == [], (
            '期望删掉的 trace id 可以重新 begin，且不带回旧的 span；实际带回来了。'
        )

    async def test_concurrent_appends_lose_nothing_and_keep_each_batch_whole(
        self, store: TraceStore
    ) -> None:
        await store.begin(_header())

        async def writer(base: int) -> None:
            for step in range(3):
                first = base + step * 2
                await store.append(_TRACE, [_span(first), _span(first + 1)])
                await asyncio.sleep(0)

        await asyncio.gather(writer(100), writer(200), writer(300))

        numbers = _numbers(await store.spans(_TRACE))
        assert sorted(numbers) == sorted(
            base + offset for base in (100, 200, 300) for offset in range(6)
        ), f'期望三个并发写者的 18 个 span 一个不少、一个不多；实际 {numbers}。'
        for base in (100, 200, 300):
            own = [number for number in numbers if base <= number < base + 6]
            assert own == sorted(own), (
                f'期望每个写者的 span 按它 append 的顺序读回；写者 {base} 的是 {own}。'
            )
        for index, number in enumerate(numbers):
            if number % 2 == 0:
                follower = numbers[index + 1] if index + 1 < len(numbers) else None
                assert follower == number + 1, (
                    f'期望一次 append 的那一批连在一起（一次 append 是原子的）；{number} 之后紧跟的'
                    f'是 {follower}。'
                )

    async def test_reads_of_an_unknown_trace_are_empty(self, store: TraceStore) -> None:
        assert await store.get(_TRACE) is None, '期望没有这个 trace 时 get 交回 None。'
        assert await store.spans(_TRACE) == [], '期望没有这个 trace 时 spans 交回空列表。'
        assert await store.edges(_TRACE) == [], '期望没有这个 trace 时 edges 交回空列表。'
        assert await store.replay(_TRACE) is None, '期望没有这个 trace 时 replay 交回 None。'
        assert await store.payload(PayloadRef(_TRACE, _digest('x'))) is None, (
            '期望没有这个 trace 时 payload 交回 None。'
        )
        assert await store.traces() == [], '期望空的 store 列不出 trace。'
