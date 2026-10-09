"""FileTraceStore（M3 D7）：落盘安全——符号链接、篡改、权限、写到一半的行、写不进去时 run 照常。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest

from syndica.kernel import (
    AgentOptions,
    BaseAgent,
    CausalityEdge,
    ConfigError,
    ContractViolation,
    EdgeKind,
    EventBus,
    HooksCenter,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    StopReason,
    TraceRecorder,
)
from syndica.kernel.redaction import RuleRedactor, encode_redacted
from syndica.kernel.stores import file_trace, guarded_io
from syndica.kernel.stores.file_trace import FileTraceStore
from syndica.kernel.trace import (
    PayloadRef,
    Span,
    SpanKind,
    SpanStatus,
    TraceHeader,
    TraceLevel,
    TraceRedaction,
)

_TIMEOUT = 5.0
_TRACE = 'ab12' + '0' * 28


def _header(trace_id: str = _TRACE, started_ns: int = 1_700_000_000_000_000_000) -> TraceHeader:
    return TraceHeader(trace_id, started_ns, TraceLevel.REPLAY, TraceRedaction('0' * 64, 1))


def _span(number: int) -> Span:
    return Span(
        span_id=f'{number:016x}',
        parent_span_id=None,
        kind=SpanKind.TURN,
        name=f'turn {number}',
        agent_id='a1',
        start_ns=number,
        end_ns=number + 1,
        status=SpanStatus.OK,
        attributes={'turn': number},
    )


def _trace_dir(root: Path) -> Path:
    [directory] = [path for path in root.iterdir() if path.is_dir()]
    return directory


async def _begun(tmp_path: Path) -> tuple[FileTraceStore, Path]:
    root = tmp_path / 'traces'
    store = FileTraceStore(root)
    await store.begin(_header())
    return store, _trace_dir(root)


async def test_a_trace_is_found_by_the_directory_name_it_was_given(tmp_path: Path):
    store, directory = await _begun(tmp_path)

    assert store.names_a_trace(directory.name)
    assert await store.trace_named(directory.name) == _TRACE
    assert not store.names_a_trace('notes')
    assert await store.trace_named('notes') is None
    assert await store.trace_named('20200101T000000Z-' + '0' * 8) is None


def test_the_root_must_be_a_path():
    with pytest.raises(ConfigError) as caught:
        FileTraceStore(b'/tmp/traces')  # pyright: ignore[reportArgumentType]

    assert 'Path' in caught.value.next_action


async def test_the_layout_is_one_private_directory_per_trace(tmp_path: Path):
    store, directory = await _begun(tmp_path)
    await store.append(_TRACE, [_span(1)])
    ref = await store.put_payload(_TRACE, encode_redacted('output', RuleRedactor()))
    await store.put_replay(_TRACE, encode_redacted({'version': 2}, RuleRedactor()))

    assert directory.name == '20231114T221320Z-ab120000'
    assert sorted(path.name for path in directory.iterdir()) == [
        'payloads',
        'replay.json',
        'spans.jsonl',
        'trace.json',
    ]
    assert (directory / 'payloads' / f'{ref.digest}.txt').read_text() == '"output"'
    for path in (tmp_path / 'traces', directory, directory / 'payloads'):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path
    for path in (*directory.glob('*.json*'), *(directory / 'payloads').iterdir()):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


async def test_two_traces_sharing_the_id_prefix_in_one_second_get_separate_directories(
    tmp_path: Path,
):
    store, first = await _begun(tmp_path)
    other = 'ab120000' + 'f' * 24
    await store.begin(_header(other))
    await store.append(other, [_span(7)])

    names = sorted(path.name for path in (tmp_path / 'traces').iterdir())
    reader = FileTraceStore(tmp_path / 'traces')
    assert names == ['20231114T221320Z-ab120000', f'20231114T221320Z-{other}']
    assert [span.span_id for span in await reader.spans(other)] == [f'{7:016x}']
    assert await reader.spans(_TRACE) == []
    assert await reader.directory(other) == tmp_path / 'traces' / names[1]
    assert first.name == names[0]


async def test_a_trace_is_found_by_its_full_id_not_by_the_prefix_in_its_directory_name(
    tmp_path: Path,
):
    store, _ = await _begun(tmp_path)
    other = 'ab120000' + 'f' * 24
    await store.begin(_header(other, 1_700_000_100_000_000_000))
    await store.append(other, [_span(7)])
    await store.finish(other, SpanStatus.ERROR, 1_700_000_100_000_000_001)

    reader = FileTraceStore(tmp_path / 'traces')
    header = await reader.get(other)
    directory = await reader.directory(other)
    assert header is not None and header.trace_id == other
    assert directory == tmp_path / 'traces' / '20231114T221500Z-ab120000'
    assert [span.span_id for span in await reader.spans(other)] == [f'{7:016x}']
    assert await reader.spans(_TRACE) == []
    first = await reader.get(_TRACE)
    assert first is not None and first.status is None


async def test_a_symlinked_root_is_refused_and_nothing_lands_where_it_points(tmp_path: Path):
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (tmp_path / 'traces').symlink_to(elsewhere)

    with pytest.raises(ContractViolation) as caught:
        await FileTraceStore(tmp_path / 'traces').begin(_header())

    assert '符号链接' in caught.value.next_action
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize('swapped', ['trace', 'payloads'])
async def test_a_directory_swapped_for_a_symlink_mid_run_refuses_writes(
    tmp_path: Path, swapped: str
):
    store, directory = await _begun(tmp_path)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    target = directory if swapped == 'trace' else directory / 'payloads'
    shutil.copytree(target, elsewhere / target.name)
    shutil.rmtree(target)
    target.symlink_to(elsewhere / target.name)
    before = sorted(str(path) for path in elsewhere.rglob('*'))

    with pytest.raises(ContractViolation):
        await store.put_payload(_TRACE, encode_redacted('leak', RuleRedactor()))
    if swapped == 'trace':
        with pytest.raises(ContractViolation):
            await store.append(_TRACE, [_span(1)])

    assert sorted(str(path) for path in elsewhere.rglob('*')) == before


@pytest.mark.parametrize('name', ['spans.jsonl', 'payload'])
async def test_a_file_planted_as_a_symlink_is_refused_and_not_followed(tmp_path: Path, name: str):
    store, directory = await _begun(tmp_path)
    text = encode_redacted('output', RuleRedactor())
    outside = tmp_path / 'outside.txt'
    outside.write_text('untouched')
    digest = hashlib.sha256(text.encode()).hexdigest()
    payload = directory / 'payloads' / f'{digest}.txt'
    planted = directory / 'spans.jsonl' if name == 'spans.jsonl' else payload
    planted.symlink_to(outside)

    with pytest.raises(ContractViolation) as caught:
        if name == 'spans.jsonl':
            await store.append(_TRACE, [_span(1)])
        else:
            await store.put_payload(_TRACE, text)

    assert outside.read_text() == 'untouched'
    assert '不可信' in caught.value.next_action


async def test_a_payload_whose_content_was_changed_is_refused_on_read_and_on_rewrite(
    tmp_path: Path,
):
    store, directory = await _begun(tmp_path)
    text = encode_redacted('tool output', RuleRedactor())
    ref = await store.put_payload(_TRACE, text)
    path = directory / 'payloads' / f'{ref.digest}.txt'
    path.chmod(0o600)
    path.write_text('"forged"')

    with pytest.raises(ContractViolation) as read:
        await store.payload(ref)
    with pytest.raises(ContractViolation) as rewritten:
        await store.put_payload(_TRACE, text)

    assert 'sha256' in read.value.received and 'sha256' in rewritten.value.received


async def test_a_payload_larger_than_one_read_comes_back_whole(tmp_path: Path):
    store, _ = await _begun(tmp_path)
    text = encode_redacted('x' * 200_000 + 'end', RuleRedactor())

    ref = await store.put_payload(_TRACE, text)

    assert await store.payload(ref) == text


async def test_a_write_that_fails_halfway_leaves_no_file_so_a_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, directory = await _begun(tmp_path)
    text = encode_redacted('tool output', RuleRedactor())
    original = guarded_io.write_redacted

    def torn(fd: int, value: Any, *, newline: bool = False) -> None:
        os.write(fd, b'"tool')
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(guarded_io, 'write_redacted', torn)
    with pytest.raises(OSError):
        await store.put_payload(_TRACE, text)
    with pytest.raises(OSError):
        await store.put_replay(_TRACE, text)
    leftovers = sorted(path.name for path in directory.rglob('*') if path.is_file())
    monkeypatch.setattr(guarded_io, 'write_redacted', original)
    ref = await store.put_payload(_TRACE, text)
    await store.put_replay(_TRACE, text)

    assert leftovers == ['trace.json']
    assert await store.payload(ref) == text
    assert await store.replay(_TRACE) == text


async def test_a_trace_removed_from_under_the_store_refuses_further_writes(tmp_path: Path):
    store, directory = await _begun(tmp_path)
    shutil.rmtree(directory)

    with pytest.raises(ContractViolation) as caught:
        await store.append(_TRACE, [_span(1)])

    assert '不存在' in caught.value.received
    assert not directory.exists()


async def test_a_line_cut_off_by_a_crash_is_dropped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    store, directory = await _begun(tmp_path)
    await store.append(_TRACE, [_span(1), _span(2)])
    with (directory / 'spans.jsonl').open('a') as file:
        file.write('{"span_id": "00000000000')

    spans = await store.spans(_TRACE)

    assert [span.span_id for span in spans] == [f'{1:016x}', f'{2:016x}']
    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.stores.file_trace']
    assert record.levelno == logging.WARNING and record.args == (_TRACE, 'spans.jsonl')


async def test_a_broken_line_in_the_middle_means_the_file_was_changed(tmp_path: Path):
    store, directory = await _begun(tmp_path)
    await store.append(_TRACE, [_span(1)])
    with (directory / 'spans.jsonl').open('a') as file:
        file.write('not json\n')
    await store.append(_TRACE, [_span(2)])

    with pytest.raises(ContractViolation) as caught:
        await store.spans(_TRACE)

    assert '第 2 行' in caught.value.received


async def test_a_trace_with_an_unreadable_header_is_left_out_of_the_list_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    store, directory = await _begun(tmp_path)
    other = 'cd34' + '0' * 28
    await store.begin(_header(other, 1_700_000_100_000_000_000))
    header = directory / 'trace.json'
    header.chmod(0o600)
    header.write_text('{"broken": ')

    listed = await store.traces()
    with pytest.raises(ContractViolation) as caught:
        await store.get(_TRACE)

    assert [h.trace_id for h in listed] == [other]
    assert '读不成 trace 头' in caught.value.received
    assert any(
        r.levelno == logging.WARNING and str(directory) in r.getMessage() for r in caplog.records
    )


@pytest.mark.parametrize('method', ['append', 'put_payload', 'put_replay', 'finish'])
async def test_every_write_after_delete_is_refused_and_recreates_nothing(
    tmp_path: Path, method: str
):
    store, directory = await _begun(tmp_path)
    await store.delete(_TRACE)
    text = encode_redacted('late', RuleRedactor())
    writes = {
        'append': lambda: store.append(_TRACE, [_span(1)]),
        'put_payload': lambda: store.put_payload(_TRACE, text),
        'put_replay': lambda: store.put_replay(_TRACE, text),
        'finish': lambda: store.finish(_TRACE, SpanStatus.OK, 1_700_000_000_000_000_001),
    }

    with pytest.raises(ContractViolation) as caught:
        await writes[method]()

    assert '不存在' in caught.value.received
    assert list((tmp_path / 'traces').iterdir()) == []
    assert not directory.exists()


@pytest.mark.parametrize('reader', ['writer', 'fresh'])
async def test_a_lookup_reads_only_the_header_of_its_own_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reader: str
):
    store, _ = await _begun(tmp_path)
    for number in range(5):
        await store.begin(_header(f'{number:04x}' + 'e' * 28, 1_700_000_000_000_000_000))
    broken = tmp_path / 'traces' / '20231114T221320Z-ffff0000'
    broken.mkdir()
    (broken / 'trace.json').write_text('{"broken": ')
    reads: list[str] = []
    original = file_trace._read_header

    def counted(directory: int, where: str) -> TraceHeader:
        reads.append(where)
        return original(directory, where)

    monkeypatch.setattr(file_trace, '_read_header', counted)
    looking = store if reader == 'writer' else FileTraceStore(tmp_path / 'traces')

    await looking.append(_TRACE, [_span(1)])

    assert [Path(where).parent.name for where in reads] == ['20231114T221320Z-ab120000']
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_writes_cost_the_same_however_many_traces_are_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, _ = await _begun(tmp_path)
    for number in range(3):
        await store.begin(_header(f'{number:04x}' + 'e' * 28, 1_700_000_000_000_000_000))
    listed: list[int] = []
    original = os.listdir

    def counted(path: Any = '.') -> list[str]:
        listed.append(1)
        return original(path)

    monkeypatch.setattr(file_trace.os, 'listdir', counted)

    await store.append(_TRACE, [_span(1)])
    await store.put_payload(_TRACE, encode_redacted('x', RuleRedactor()))

    assert listed == []


async def test_a_copied_directory_cannot_take_over_the_trace(tmp_path: Path):
    store, directory = await _begun(tmp_path)
    decoy = tmp_path / 'traces' / '00000000T000000Z-ab120000'
    shutil.copytree(directory, decoy)

    await store.append(_TRACE, [_span(1)])
    with pytest.raises(ContractViolation) as caught:
        await FileTraceStore(tmp_path / 'traces').get(_TRACE)

    assert (directory / 'spans.jsonl').exists() and not (decoy / 'spans.jsonl').exists()
    assert await store.directory(_TRACE) == directory
    assert '有 2 个' in caught.value.received
    assert '复制或伪造' in caught.value.next_action


def _factory(llm: ScriptedLLM) -> Any:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(AgentOptions(name='Root', llm=llm), hooks=hooks, events=events)

    return build


async def test_a_recorded_run_reads_back_from_the_files(tmp_path: Path):
    store = FileTraceStore(tmp_path / 'traces')
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    runtime = Runtime(tracer=recorder)
    try:
        agent_id = await runtime.spawn(_factory(ScriptedLLM([ScriptedTurn('done')])), query='go')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()
    await recorder.finish()

    header = await store.get(recorder.trace_id)
    spans = await store.spans(recorder.trace_id)
    [llm] = [span for span in spans if span.kind is SpanKind.LLM_CALL]
    reply = await store.payload(PayloadRef(recorder.trace_id, str(llm.attributes['llm.response'])))
    assert header is not None and header.status is SpanStatus.OK
    assert sorted(span.kind.value for span in spans) == ['agent', 'llm_call', 'turn']
    assert reply is not None and '"done"' in reply


@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory permissions')
async def test_a_directory_that_turns_read_only_mid_run_leaves_the_run_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    store = FileTraceStore(tmp_path / 'traces')
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    directory = _trace_dir(tmp_path / 'traces')
    directory.chmod(0o500)
    runtime = Runtime(tracer=recorder)
    try:
        agent_id = await runtime.spawn(_factory(ScriptedLLM([ScriptedTurn('done')])), query='go')
        context = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
    finally:
        await runtime.shutdown()
        await recorder.finish()
        directory.chmod(0o700)

    assert context.stop_reason is StopReason.COMPLETED
    [report] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    assert report.levelno == logging.ERROR and recorder.dropped > 0


async def _bounded(awaitable: Any) -> Any:
    return await asyncio.wait_for(awaitable, _TIMEOUT)


async def test_a_fifo_planted_as_a_trace_file_is_refused_without_hanging(tmp_path: Path):
    store, directory = await _begun(tmp_path)
    text = encode_redacted('output', RuleRedactor())
    digest = hashlib.sha256(text.encode()).hexdigest()
    os.mkfifo(directory / 'spans.jsonl')
    os.mkfifo(directory / 'payloads' / f'{digest}.txt')

    with pytest.raises(ContractViolation) as appended:
        await _bounded(store.append(_TRACE, [_span(1)]))
    with pytest.raises(ContractViolation) as stored:
        await _bounded(store.put_payload(_TRACE, text))
    with pytest.raises(ContractViolation) as read:
        await _bounded(store.spans(_TRACE))

    assert '普通文件' in appended.value.expected
    assert 'FIFO' in stored.value.received and 'FIFO' in read.value.received


async def test_a_decoy_that_cannot_be_read_does_not_hide_or_hang_the_real_trace(tmp_path: Path):
    store, _ = await _begun(tmp_path)
    fifo = tmp_path / 'traces' / '0-ab120000'
    fifo.mkdir()
    os.mkfifo(fifo / 'trace.json')
    huge = tmp_path / 'traces' / '1-ab120000'
    huge.mkdir()
    with (huge / 'trace.json').open('wb') as file:
        file.truncate(64 * 1024 * 1024)

    header = await _bounded(FileTraceStore(tmp_path / 'traces').get(_TRACE))

    assert header is not None and header.trace_id == _TRACE


async def test_an_oversized_header_is_refused_before_it_is_read(tmp_path: Path):
    store, directory = await _begun(tmp_path)
    header = directory / 'trace.json'
    header.chmod(0o600)
    with header.open('r+b') as file:
        file.truncate(64 * 1024 * 1024)

    with pytest.raises(ContractViolation) as caught:
        await FileTraceStore(tmp_path / 'traces').get(_TRACE)

    assert '超过这种文件的上限' in caught.value.received


async def test_a_payload_already_there_with_another_size_is_refused_without_reading_it(
    tmp_path: Path,
):
    store, directory = await _begun(tmp_path)
    text = encode_redacted('output', RuleRedactor())
    digest = hashlib.sha256(text.encode()).hexdigest()
    with (directory / 'payloads' / f'{digest}.txt').open('wb') as file:
        file.truncate(64 * 1024 * 1024)

    with pytest.raises(ContractViolation) as caught:
        await store.put_payload(_TRACE, text)

    assert f'上限 {len(text.encode())}' in caught.value.received


async def test_a_hard_link_to_an_outside_file_is_never_written_through(tmp_path: Path):
    store, directory = await _begun(tmp_path)
    outside = tmp_path / 'victim.txt'
    outside.write_text('untouched')
    os.link(outside, directory / 'spans.jsonl')

    with pytest.raises(ContractViolation) as caught:
        await store.append(_TRACE, [_span(1)])

    assert outside.read_text() == 'untouched'
    assert '2 个硬链接' in caught.value.received
    assert await store.spans(_TRACE) == []


async def test_a_root_swapped_for_another_directory_refuses_further_writes(tmp_path: Path):
    workspace = tmp_path / 'ws'
    (workspace / '.syndica').mkdir(parents=True)
    store = FileTraceStore(workspace / '.syndica' / 'traces')
    await store.begin(_header())
    elsewhere = tmp_path / 'elsewhere'
    (elsewhere / 'traces').mkdir(parents=True)
    (workspace / '.syndica').rename(tmp_path / 'moved')
    (workspace / '.syndica').symlink_to(elsewhere)

    with pytest.raises(ContractViolation) as caught:
        await store.append(_TRACE, [_span(1)])

    assert list((elsewhere / 'traces').iterdir()) == []
    assert '换成了别的目录' in caught.value.received


async def test_a_regular_file_where_a_directory_belongs_is_refused(tmp_path: Path):
    store, directory = await _begun(tmp_path)
    shutil.rmtree(directory / 'payloads')
    (directory / 'payloads').write_text('not a directory')

    with pytest.raises(ContractViolation) as caught:
        await store.put_payload(_TRACE, encode_redacted('x', RuleRedactor()))

    assert '真目录' in caught.value.expected


@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores file permissions')
async def test_an_unreadable_header_is_reported_for_its_trace_and_skipped_in_the_list(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    store, directory = await _begun(tmp_path)
    other = 'cd34' + '0' * 28
    await store.begin(_header(other, 1_700_000_100_000_000_000))
    (directory / 'trace.json').chmod(0o000)
    try:
        listed = await store.traces()
        with pytest.raises(ContractViolation) as caught:
            await FileTraceStore(tmp_path / 'traces').get(_TRACE)
    finally:
        (directory / 'trace.json').chmod(0o600)

    assert [header.trace_id for header in listed] == [other]
    assert 'PermissionError' in caught.value.received
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.parametrize(
    'header',
    [
        'rule',
        'nesting',
    ],
)
async def test_a_header_the_model_types_reject_is_reported_as_corrupt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, header: str
):
    store, directory = await _begun(tmp_path)
    other = 'cd34' + '0' * 28
    await store.begin(_header(other, 1_700_000_100_000_000_000))
    path = directory / 'trace.json'
    value = json.loads(path.read_text())
    if header == 'rule':
        value['redaction']['rules'] = [{'rule_id': 'not-custom', 'pattern': 'x'}]
        text = json.dumps(value)
    else:
        text = '[' * 100_000 + ']' * 100_000
    path.chmod(0o600)
    path.write_text(text)

    listed = await store.traces()
    with pytest.raises(ContractViolation) as caught:
        await FileTraceStore(tmp_path / 'traces').get(_TRACE)

    assert [h.trace_id for h in listed] == [other]
    assert '读不成 trace 头' in caught.value.received


async def test_a_span_line_that_is_not_utf8_is_reported_as_corrupt(tmp_path: Path):
    store, directory = await _begun(tmp_path)
    line = encode_redacted(file_trace._span_value(_span(1)), RuleRedactor()).encode()
    with (directory / 'spans.jsonl').open('wb') as file:
        file.write(line.replace(b'turn 1', b'turn \xff') + b'\n')

    with pytest.raises(ContractViolation) as caught:
        await store.spans(_TRACE)

    assert '不是 UTF-8' in caught.value.received


async def test_a_header_that_cannot_be_replaced_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, directory = await _begun(tmp_path)

    def refused(*args: Any, **kwargs: Any) -> None:
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(file_trace.os, 'replace', refused)
    with pytest.raises(OSError):
        await store.finish(_TRACE, SpanStatus.OK, 1_700_000_000_000_000_001)

    assert sorted(path.name for path in directory.iterdir()) == ['payloads', 'trace.json']


async def test_a_directory_without_a_header_is_left_out_of_the_list_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    store, _ = await _begun(tmp_path)
    (tmp_path / 'traces' / '20231114T000000Z-00000000').mkdir()

    listed = await store.traces()

    assert [header.trace_id for header in listed] == [_TRACE]
    assert any(
        r.levelno == logging.WARNING and '20231114T000000Z-00000000' in r.getMessage()
        for r in caplog.records
    )


async def test_a_span_line_the_model_rejects_is_reported_as_corrupt(tmp_path: Path):
    store, directory = await _begun(tmp_path)
    value = file_trace._span_value(_span(1))
    value['attributes'] = {'not.an.attribute': 'x'}
    with (directory / 'spans.jsonl').open('w') as file:
        file.write(json.dumps(value) + '\n')

    with pytest.raises(ContractViolation) as caught:
        await store.spans(_TRACE)

    assert '读不成记录' in caught.value.received
    assert 'spans.jsonl 第 1 行' in caught.value.expected


async def test_a_file_that_grows_while_it_is_read_stops_at_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, _ = await _begun(tmp_path)
    ref = await store.put_payload(_TRACE, encode_redacted('x' * 5000, RuleRedactor()))
    real = os.fstat

    def understated(descriptor: int) -> os.stat_result:
        info = real(descriptor)
        return os.stat_result((*info[:6], 10, *info[7:10]))

    monkeypatch.setattr(file_trace, '_FILE_LIMIT', 1000)
    monkeypatch.setattr(file_trace.os, 'fstat', understated)

    with pytest.raises(ContractViolation) as caught:
        await store.payload(ref)

    assert '变大了' in caught.value.received


def _on_disk(directory: Path) -> int:
    total = 0
    for parent, _, files in os.walk(directory):
        total += sum(os.lstat(Path(parent, name)).st_size for name in files)
    return total


async def test_the_size_counts_every_file_of_the_trace_including_its_payloads(tmp_path: Path):
    store, directory = await _begun(tmp_path)
    await store.append(_TRACE, [_span(1), _span(2)], [])
    await store.put_payload(_TRACE, encode_redacted('x' * 5000, RuleRedactor()))
    await store.put_replay(_TRACE, encode_redacted({'version': 2}, RuleRedactor()))

    size = await store.size(_TRACE)

    assert size == _on_disk(directory)
    assert size is not None and size > 5000


async def test_the_size_follows_no_symlink_and_enters_no_other_directory(tmp_path: Path):
    store, directory = await _begun(tmp_path)
    before = await store.size(_TRACE)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'big.bin').write_bytes(b'x' * 10_000)
    (directory / 'linked.bin').symlink_to(elsewhere / 'big.bin')
    (directory / 'payloads').rmdir()
    (directory / 'payloads').symlink_to(elsewhere, target_is_directory=True)
    (directory / 'nested').mkdir()
    (directory / 'nested' / 'inner.bin').write_bytes(b'x' * 10_000)

    assert await store.size(_TRACE) == before


async def test_the_payload_directory_is_counted_but_nothing_below_it(tmp_path: Path):
    store, directory = await _begun(tmp_path)
    await store.put_payload(_TRACE, encode_redacted('x' * 5000, RuleRedactor()))
    before = await store.size(_TRACE)
    (directory / 'payloads' / 'deeper').mkdir()
    (directory / 'payloads' / 'deeper' / 'inner.bin').write_bytes(b'x' * 10_000)

    assert await store.size(_TRACE) == before


async def test_a_file_that_vanishes_while_the_size_is_taken_is_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, directory = await _begun(tmp_path)
    real_listdir = os.listdir

    def with_a_vanished_entry(path: Any) -> list[str]:
        return [*real_listdir(path), '.trace.json.vanished']

    monkeypatch.setattr(file_trace.os, 'listdir', with_a_vanished_entry)

    assert await store.size(_TRACE) == _on_disk(directory)


async def test_the_size_of_a_trace_that_is_not_there_is_none(tmp_path: Path):
    store, _ = await _begun(tmp_path)

    assert await store.size('cd' * 16) is None


async def test_a_tail_cut_inside_a_multibyte_character_is_dropped_like_any_cut_off_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    store, directory = await _begun(tmp_path)
    await store.append(_TRACE, [_span(1)])
    whole = ('{"name": "中文"' + '}').encode()
    with (directory / 'spans.jsonl').open('ab') as file:
        file.write(whole[: whole.index('中'.encode()) + 1])

    spans = await store.spans(_TRACE)

    assert [span.span_id for span in spans] == [f'{1:016x}']
    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.stores.file_trace']
    assert record.levelno == logging.WARNING and record.args == (_TRACE, 'spans.jsonl')


async def test_a_line_nested_too_deep_to_parse_is_reported_as_changed(tmp_path: Path):
    store, directory = await _begun(tmp_path)
    with (directory / 'spans.jsonl').open('a') as file:
        file.write('[' * 100_000 + ']' * 100_000 + '\n')

    with pytest.raises(ContractViolation) as caught:
        await store.spans(_TRACE)

    assert '第 1 行不是 JSON' in caught.value.received


@pytest.mark.parametrize('name', ['backup', 'renamed-by-hand'])
async def test_a_directory_whose_name_does_not_carry_its_trace_id_is_not_listed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, name: str
):
    store, directory = await _begun(tmp_path)
    shutil.copytree(directory, directory.parent / name)

    headers = await FileTraceStore(tmp_path / 'traces').traces()

    assert [header.trace_id for header in headers] == [_TRACE]
    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.stores.file_trace']
    assert record.levelno == logging.WARNING
    assert record.args == (tmp_path / 'traces' / name, _TRACE)


async def test_a_delete_that_fails_partway_leaves_the_trace_listed_for_another_try(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, directory = await _begun(tmp_path)
    await store.append(_TRACE, [_span(1)])
    await store.put_payload(_TRACE, encode_redacted('output', RuleRedactor()))
    real_unlink = os.unlink

    def refusing(path: Any, *args: Any, **kwargs: Any) -> None:
        if path == 'spans.jsonl':
            raise PermissionError('not now')
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(file_trace.os, 'unlink', refusing)
    with pytest.raises(PermissionError):
        await store.delete(_TRACE)
    monkeypatch.setattr(file_trace.os, 'unlink', real_unlink)
    fresh = FileTraceStore(tmp_path / 'traces')

    assert [header.trace_id for header in await fresh.traces()] == [_TRACE]
    await fresh.delete(_TRACE)
    assert await fresh.traces() == []
    assert not directory.exists()


async def test_a_span_file_over_the_read_limit_is_too_large_rather_than_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, _ = await _begun(tmp_path)
    await store.append(_TRACE, [_span(number) for number in range(1, 20)])
    monkeypatch.setattr(file_trace, '_FILE_LIMIT', 512)

    with pytest.raises(ContractViolation) as caught:
        await store.spans(_TRACE)

    assert '读回的上限 512' in caught.value.expected
    assert '长跑' in caught.value.next_action


async def test_unreadable_names_each_directory_traces_skips_with_why_and_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    root = tmp_path / 'traces'
    store = FileTraceStore(root)
    newer, broken = 'cd' * 16, 'ef' * 16
    for trace_id in (_TRACE, newer, broken):
        await store.begin(_header(trace_id))
    directories = {path.name.rsplit('-', 1)[1]: path for path in root.iterdir()}

    def rewrite(trace_id: str, change: dict[str, object]) -> None:
        path = directories[trace_id[:8]] / 'trace.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value.update(change)
        path.write_text(json.dumps(value), encoding='utf-8')

    rewrite(newer, {'schema': 'syndica.trace/9'})
    rewrite(broken, {'started_ns': 'later'})
    shutil.copytree(directories[_TRACE[:8]], root / 'copy')
    caplog.clear()

    skipped = await store.unreadable()
    quiet = list(caplog.records)
    listed = await store.traces()

    names = [directories[newer[:8]].name, directories[broken[:8]].name, 'copy']
    reasons = {name: (received, next_action) for name, received, next_action in skipped}
    assert [header.trace_id for header in listed] == [_TRACE]
    assert [name for name, _, _ in skipped] == sorted(names)
    assert 'syndica.trace/9' in reasons[names[0]][0] and '不要删掉' in reasons[names[0]][1]
    assert '读不进来' in reasons[names[1]][0] and '删掉这个目录' in reasons[names[1]][1]
    assert '被改名或复制出来的' in reasons['copy'][0]
    assert quiet == []
    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 3


async def test_a_finished_trace_whose_last_line_was_torn_is_refused_as_changed_after_it_finished(
    tmp_path: Path,
):
    store, directory = await _begun(tmp_path)
    await store.append(_TRACE, [_span(1), _span(2)])
    await store.finish(_TRACE, SpanStatus.OK, 1_700_000_000_000_000_001)
    spans = directory / 'spans.jsonl'
    spans.write_bytes(spans.read_bytes()[:-5])

    with pytest.raises(ContractViolation) as caught:
        await store.spans(_TRACE)

    assert '已经写下了结局' in caught.value.received


async def test_finishing_puts_the_records_on_disk_before_the_header_that_vouches_for_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, directory = await _begun(tmp_path)
    await store.append(
        _TRACE,
        [_span(1)],
        [CausalityEdge(EdgeKind.SPAWN, '1' * 16, '2' * 16, {'child.agent_id': 'a2'})],
    )
    await store.put_payload(_TRACE, encode_redacted('a payload', RuleRedactor()))
    events: list[tuple[str, int]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(descriptor: int) -> None:
        events.append(('fsync', os.fstat(descriptor).st_ino))
        real_fsync(descriptor)

    def replace(*args: Any, **kwargs: Any) -> None:
        real_replace(*args, **kwargs)
        events.append(('replace', 0))

    monkeypatch.setattr(os, 'fsync', fsync)
    monkeypatch.setattr(os, 'replace', replace)

    await store.finish(_TRACE, SpanStatus.OK, 1_700_000_000_000_000_001)

    inode = {name: (directory / name).stat().st_ino for name in ('spans.jsonl', 'edges.jsonl')}
    inode['payloads'] = (directory / 'payloads').stat().st_ino
    inode['trace'] = directory.stat().st_ino
    inode['header'] = (directory / 'trace.json').stat().st_ino
    replaced = events.index(('replace', 0))
    before = {number for kind, number in events[:replaced] if kind == 'fsync'}
    after = {number for kind, number in events[replaced:] if kind == 'fsync'}
    assert set(inode.values()) <= before
    assert inode['trace'] in after


async def test_a_payload_is_on_disk_before_put_payload_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, directory = await _begun(tmp_path)
    synced: list[int] = []
    real_fsync = os.fsync

    def fsync(descriptor: int) -> None:
        synced.append(os.fstat(descriptor).st_ino)
        real_fsync(descriptor)

    monkeypatch.setattr(os, 'fsync', fsync)

    ref = await store.put_payload(_TRACE, encode_redacted('a payload', RuleRedactor()))

    assert (directory / 'payloads' / f'{ref.digest}.txt').stat().st_ino in synced


@pytest.mark.parametrize(('code', 'finishes'), [(errno.EINVAL, True), (errno.EIO, False)])
async def test_a_directory_that_cannot_be_synced_is_tolerated_only_where_syncing_it_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, finishes: bool
):
    store, directory = await _begun(tmp_path)
    await store.append(_TRACE, [_span(1)])
    real_fsync = os.fsync

    def fsync(descriptor: int) -> None:
        if os.path.isdir(f'/dev/fd/{descriptor}') or os.fstat(descriptor).st_ino == (
            directory.stat().st_ino
        ):
            raise OSError(code, os.strerror(code))
        real_fsync(descriptor)

    monkeypatch.setattr(os, 'fsync', fsync)

    if finishes:
        await store.finish(_TRACE, SpanStatus.OK, 1_700_000_000_000_000_001)
        header = await store.get(_TRACE)
        assert header is not None and header.status is SpanStatus.OK
    else:
        with pytest.raises(OSError):
            await store.finish(_TRACE, SpanStatus.OK, 1_700_000_000_000_000_001)
        header = await store.get(_TRACE)
        assert header is not None and header.status is None
