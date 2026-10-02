"""CLI 记录 trace（M3 D15 / T11）：run 与 chat 默认记录、四个级别、配置快照、脱敏、chat 的上限、
写不进去与写到一半时 run 照常。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import string
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest

from syndica import __version__
from syndica.cli.handoff import trace_directory
from syndica.cli.host import (
    RuntimeCliHost,
    _despite_cancellation,  # pyright: ignore[reportPrivateUsage]
)
from syndica.cli.main import CliIO, RunRequest, main
from syndica.cli.recording import run_status, trace_level
from syndica.kernel import (
    ConfigError,
    ContextBlock,
    FileTraceStore,
    LLMResponseError,
    RuleRedactor,
    ScriptedLLM,
    ScriptedTurn,
    Span,
    SpanKind,
    SpanStatus,
    SyndicaError,
    ToolCall,
    TraceHeader,
    TraceLevel,
)

_PAYLOAD_KEYS = ('llm.request_units', 'llm.response', 'llm.request')


def _key(seed: int) -> str:
    rng = random.Random(seed)
    return ''.join(rng.choices(string.ascii_letters + string.digits, k=40))


def _cli(
    tmp_path: Path,
    llm: ScriptedLLM,
    argv: list[str],
    *,
    stdin: str = '',
    environ: dict[str, str] | None = None,
    interrupted: bool = False,
) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(stdin), stdout=stdout, stderr=stderr)
    host = RuntimeCliHost(
        io=io,
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
        environ=dict(os.environ) if environ is None else environ,
    )
    try:
        code = main(argv, host=host, io=io)
    finally:
        host.close(interrupted=interrupted)
    return code, stdout.getvalue(), stderr.getvalue()


def _recorded(root: Path) -> list[tuple[TraceHeader, Path, list[Span]]]:
    store = FileTraceStore(root / '.syndica' / 'traces')

    async def read() -> list[tuple[TraceHeader, Path, list[Span]]]:
        found: list[tuple[TraceHeader, Path, list[Span]]] = []
        for header in await store.traces():
            directory = await store.directory(header.trace_id)
            assert directory is not None
            found.append((header, directory, await store.spans(header.trace_id)))
        return found

    return asyncio.run(read())


def _files(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): path.read_text(encoding='utf-8')
        for path in sorted(directory.rglob('*'))
        if path.is_file()
    }


def _llm_span(spans: list[Span]) -> Span:
    [span] = [span for span in spans if span.kind is SpanKind.LLM_CALL]
    return span


def _rule(rule_id: str, pattern: str) -> str:
    return f"version: 1\nredaction:\n  rules:\n    - {{id: {rule_id}, pattern: '{pattern}'}}\n"


def _done() -> ScriptedLLM:
    return ScriptedLLM([ScriptedTurn(content='done')])


def test_a_run_is_recorded_at_replay_by_default_and_its_directory_is_printed(tmp_path: Path):
    code, stdout, stderr = _cli(tmp_path, _done(), ['run', 'go', '--agent', 'Worker'])

    [(header, directory, spans)] = _recorded(tmp_path)
    span = _llm_span(spans)
    assert (code, stdout) == (0, 'done\n')
    assert (header.level, header.status) == (TraceLevel.REPLAY, SpanStatus.OK)
    assert header.source.argv == ('run', 'go', '--agent', 'Worker')
    assert (header.source.version, header.source.config_sha256) == (__version__, None)
    assert directory.parent == tmp_path / '.syndica' / 'traces'
    assert stderr == f'trace：.syndica/traces/{directory.name}\n'
    assert [key for key in _PAYLOAD_KEYS if key in span.attributes] == [
        'llm.request_units',
        'llm.response',
    ]
    assert (tmp_path / '.syndica' / '.gitignore').read_text(encoding='utf-8') == '*\n'


@pytest.mark.parametrize(
    ('level', 'payloads'),
    [
        ('structure', []),
        ('replay', ['llm.request_units', 'llm.response']),
        ('full', ['llm.request_units', 'llm.response', 'llm.request']),
    ],
)
def test_the_trace_flag_picks_what_is_recorded(tmp_path: Path, level: str, payloads: list[str]):
    code, _, _ = _cli(tmp_path, _done(), ['run', 'go', '--agent', 'Worker', '--trace', level])

    [(header, directory, spans)] = _recorded(tmp_path)
    span = _llm_span(spans)
    assert code == 0
    assert header.level is TraceLevel(level)
    assert [key for key in _PAYLOAD_KEYS if key in span.attributes] == payloads
    assert bool(list((directory / 'payloads').iterdir())) == bool(payloads)


def test_tracing_off_writes_no_directory_and_nothing_on_stderr(tmp_path: Path):
    code, stdout, stderr = _cli(
        tmp_path, _done(), ['run', 'go', '--agent', 'Worker', '--trace', 'off']
    )

    assert (code, stdout, stderr) == (0, 'done\n', '')
    assert not (tmp_path / '.syndica').exists()


def test_the_config_sets_the_level_and_the_flag_overrides_it(tmp_path: Path):
    config = tmp_path / '.syndica.yaml'
    config.write_text('version: 1\ntrace:\n  level: structure\n', encoding='utf-8')

    _cli(tmp_path, _done(), ['run', 'go', '--agent', 'Worker'])
    _cli(tmp_path, _done(), ['run', 'go', '--agent', 'Worker', '--trace', 'full'])

    headers = [header for header, _, _ in _recorded(tmp_path)]
    assert [header.level for header in headers] == [TraceLevel.STRUCTURE, TraceLevel.FULL]
    assert {header.source.config_sha256 for header in headers} == {
        hashlib.sha256(config.read_bytes()).hexdigest()
    }


def test_an_unquoted_off_in_the_config_turns_recording_off(tmp_path: Path):
    (tmp_path / '.syndica.yaml').write_text('version: 1\ntrace: {level: off}\n', encoding='utf-8')

    code, _, stderr = _cli(tmp_path, _done(), ['run', 'go', '--agent', 'Worker'])

    assert (code, stderr) == (0, '')
    assert not (tmp_path / '.syndica').exists()


class _Rewriting(ScriptedLLM):
    """每次被调用都把配置改成「不记录、没有用户规则」：Agent 在 run 中途改 `.syndica.yaml`。"""

    def __init__(self, turns: list[ScriptedTurn], config: Path) -> None:
        super().__init__(turns)
        self._config = config

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        self._config.write_text('version: 1\ntrace: {level: off}\n', encoding='utf-8')
        return await super().create(messages, **kwargs)


def test_the_level_and_the_rules_are_the_ones_in_force_when_the_run_started(tmp_path: Path):
    config = tmp_path / '.syndica.yaml'
    config.write_text(
        'version: 1\ntrace: {level: full}\nredaction:\n  rules:\n'
        "    - {id: custom.ticket, pattern: 'TICKET-[0-9]{4,}'}\n",
        encoding='utf-8',
    )
    llm = _Rewriting([ScriptedTurn(content='closed TICKET-48213')], config)

    code, _, _ = _cli(tmp_path, llm, ['run', 'go', '--agent', 'Worker'])

    [(header, directory, _)] = _recorded(tmp_path)
    payloads = [text for name, text in _files(directory).items() if name.startswith('payloads/')]
    assert code == 0
    assert header.level is TraceLevel.FULL
    assert [rule.rule_id for rule in header.redaction.rules] == ['custom.ticket']
    assert 'TICKET-48213' not in ''.join(_files(directory).values())
    assert any('closed [REDACTED:custom.ticket]' in text for text in payloads)


def test_no_file_of_the_trace_holds_an_environment_secret_or_a_user_rule_match(tmp_path: Path):
    secret = _key(1)
    (tmp_path / 'notes.txt').write_text(f'token {secret} for TICKET-90817\n', encoding='utf-8')
    (tmp_path / '.syndica.yaml').write_text(
        _rule('custom.ticket', 'TICKET-[0-9]{4,}'), encoding='utf-8'
    )
    read = ToolCall(id='read-1', name='Read', arguments=json.dumps({'path': 'notes.txt'}))
    llm = ScriptedLLM(
        [ScriptedTurn(tool_calls=[read]), ScriptedTurn(content=f'saw {secret} in TICKET-90817')]
    )
    environ = {**os.environ, 'SYNDICA_TEST_API_KEY': secret}

    code, _, stderr = _cli(
        tmp_path,
        llm,
        ['run', f'find {secret} and TICKET-90817', '--agent', 'Worker', '--trace', 'full'],
        environ=environ,
    )

    [(header, directory, _)] = _recorded(tmp_path)
    files = _files(directory)
    payloads = ''.join(text for name, text in files.items() if name.startswith('payloads/'))
    assert code == 0
    assert header.source.argv[1] == (
        'find [REDACTED:secret.env_value] and [REDACTED:custom.ticket]'
    )
    for text in (*files.values(), stderr):
        assert secret not in text
        assert 'TICKET-90817' not in text
    assert payloads.count('[REDACTED:secret.env_value]') >= 3
    assert payloads.count('[REDACTED:custom.ticket]') >= 3


@pytest.mark.parametrize(
    ('extra', 'config', 'noted'),
    [
        ([], None, False),
        (['--trace', 'structure'], None, False),
        (['--trace', 'full'], None, True),
        ([], 'version: 1\ntrace: {level: replay}\n', True),
    ],
    ids=['default', 'structure-flag', 'full-flag', 'replay-config'],
)
def test_chat_records_structure_and_says_so_only_when_more_was_asked_for(
    tmp_path: Path, extra: list[str], config: str | None, noted: bool
):
    if config is not None:
        (tmp_path / '.syndica.yaml').write_text(config, encoding='utf-8')

    code, stdout, stderr = _cli(
        tmp_path, ScriptedLLM([ScriptedTurn(content='hi')]), ['chat', *extra], stdin='hello\n'
    )

    [(header, directory, spans)] = _recorded(tmp_path)
    assert code == 0 and 'hi' in stdout
    assert (header.level, header.status) == (TraceLevel.STRUCTURE, SpanStatus.OK)
    assert header.source.argv == ('chat', *extra)
    assert ('chat 的一个会话最高只记 structure' in stderr) is noted
    assert stderr.endswith(f'trace：.syndica/traces/{directory.name}\n')
    assert not [key for key in _PAYLOAD_KEYS if key in _llm_span(spans).attributes]


def test_chat_with_tracing_off_records_nothing(tmp_path: Path):
    code, _, stderr = _cli(
        tmp_path,
        ScriptedLLM([ScriptedTurn(content='hi')]),
        ['chat', '--trace', 'off'],
        stdin='hello\n',
    )

    assert (code, stderr) == (0, '')
    assert not (tmp_path / '.syndica').exists()


@pytest.mark.parametrize(('interrupted', 'status'), [(False, 'ok'), (True, 'cancelled')])
def test_a_chat_session_trace_ends_when_the_host_closes(
    tmp_path: Path, interrupted: bool, status: str
):
    _cli(
        tmp_path,
        ScriptedLLM([ScriptedTurn(content='hi')]),
        ['chat'],
        stdin='hello\n',
        interrupted=interrupted,
    )

    [(header, _, _)] = _recorded(tmp_path)
    assert header.status is SpanStatus(status)


def test_a_turn_in_an_open_session_cannot_change_the_trace_level(tmp_path: Path):
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: _done(),
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(ConfigError) as caught:
            host.run_request(RunRequest(prompt='go', session='a' * 32, trace='full'))
    finally:
        host.close()

    assert "'trace'" in caught.value.received


@pytest.mark.parametrize('planted', ['file', 'symlink'])
def test_a_trace_directory_that_cannot_be_made_is_reported_once_and_the_run_goes_on(
    tmp_path: Path, planted: str
):
    elsewhere = tmp_path.parent / f'{tmp_path.name}-elsewhere'
    elsewhere.mkdir()
    if planted == 'file':
        (tmp_path / '.syndica').write_text('made by the agent', encoding='utf-8')
    else:
        (tmp_path / '.syndica').mkdir()
        (tmp_path / '.syndica' / 'traces').symlink_to(elsewhere)

    code, stdout, stderr = _cli(tmp_path, _done(), ['run', 'go', '--agent', 'Worker'])

    [note] = stderr.splitlines()
    assert (code, stdout) == (0, 'done\n')
    assert note.startswith('这次 run 不记录 trace：期望: ')
    assert ('不是目录' if planted == 'file' else '符号链接') in note
    assert '--trace off' in note
    assert list(elsewhere.iterdir()) == []
    if planted == 'file':
        assert (tmp_path / '.syndica').read_text(encoding='utf-8') == 'made by the agent'


class _Locking(ScriptedLLM):
    """第一次被调用时把这次 run 的 trace 目录改成只读：Agent 的 Bash 能做到的事。"""

    def __init__(self, turns: list[ScriptedTurn], root: Path) -> None:
        super().__init__(turns)
        self._root = root
        self.locked: list[Path] = []

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        if not self.locked:
            [directory] = (self._root / '.syndica' / 'traces').iterdir()
            directory.chmod(0o500)
            self.locked.append(directory)
        return await super().create(messages, **kwargs)


@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory permissions')
def test_a_trace_that_loses_records_mid_run_is_flagged_incomplete_and_the_run_goes_on(
    tmp_path: Path,
):
    llm = _Locking([ScriptedTurn(content='done')], tmp_path)
    try:
        code, stdout, stderr = _cli(tmp_path, llm, ['run', 'go', '--agent', 'Worker'])
    finally:
        for directory in llm.locked:
            directory.chmod(0o700)

    [(header, directory, _)] = _recorded(tmp_path)
    lines = stderr.splitlines()
    shown = f'.syndica/traces/{directory.name}'
    assert (code, stdout) == (0, 'done\n')
    assert header.status is None
    assert len([line for line in lines if line.startswith('期望: trace ')]) == 1
    assert lines[-1] == f'trace（不完整，原因见上面的 ERROR；不要拿它回放）：{shown}'


def test_an_existing_gitignore_in_the_hosts_directory_is_left_alone(tmp_path: Path):
    (tmp_path / '.syndica').mkdir()
    (tmp_path / '.syndica' / '.gitignore').write_text('handoffs/\n', encoding='utf-8')

    _cli(tmp_path, _done(), ['run', 'go', '--agent', 'Worker'])

    assert (tmp_path / '.syndica' / '.gitignore').read_text(encoding='utf-8') == 'handoffs/\n'
    assert len(_recorded(tmp_path)) == 1


@pytest.mark.parametrize(
    ('requested', 'configured', 'chat', 'expected', 'noted'),
    [
        (None, None, False, TraceLevel.REPLAY, False),
        (None, TraceLevel.FULL, False, TraceLevel.FULL, False),
        ('structure', TraceLevel.FULL, False, TraceLevel.STRUCTURE, False),
        ('off', TraceLevel.FULL, False, TraceLevel.OFF, False),
        (None, None, True, TraceLevel.STRUCTURE, False),
        (None, TraceLevel.OFF, True, TraceLevel.OFF, False),
        ('replay', None, True, TraceLevel.STRUCTURE, True),
        (None, TraceLevel.FULL, True, TraceLevel.STRUCTURE, True),
    ],
)
def test_the_level_is_the_flag_then_the_config_then_replay_and_chat_is_capped(
    requested: str | None,
    configured: TraceLevel | None,
    chat: bool,
    expected: TraceLevel,
    noted: bool,
):
    notes: list[str] = []

    level = trace_level(requested, configured, chat=chat, warn=notes.append)

    assert level is expected
    assert bool(notes) is noted


@pytest.mark.parametrize(
    ('error', 'status'),
    [
        (None, SpanStatus.OK),
        (asyncio.CancelledError(), SpanStatus.CANCELLED),
        (KeyboardInterrupt(), SpanStatus.CANCELLED),
        (RuntimeError('boom'), SpanStatus.ERROR),
        (ConfigError(expected='e', received='r', next_action='n'), SpanStatus.ERROR),
    ],
)
def test_a_run_ends_ok_cancelled_or_in_error(error: BaseException | None, status: SpanStatus):
    assert run_status(error) is status


@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory permissions')
def test_a_chat_trace_that_fails_as_the_host_closes_reports_on_the_hosts_stderr(tmp_path: Path):
    stderr = StringIO()
    io = CliIO(stdin=StringIO('hello\n'), stdout=StringIO(), stderr=stderr)
    llm = ScriptedLLM([ScriptedTurn(content='hi')])
    host = RuntimeCliHost(io=io, model_factory=lambda _: llm, workspace_root=tmp_path)
    locked: list[Path] = []
    try:
        code = main(['chat'], host=host, io=io)
        locked.extend((tmp_path / '.syndica' / 'traces').iterdir())
        for directory in locked:
            directory.chmod(0o500)
        host.close()
    finally:
        for directory in locked:
            directory.chmod(0o700)
        host.close()

    lines = stderr.getvalue().splitlines()
    assert code == 0
    assert len([line for line in lines if line.startswith('期望: trace ')]) == 1
    assert lines[-1].startswith('trace（不完整')


def test_a_chat_whose_workspace_cannot_open_still_ends_its_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def unopenable(self: RuntimeCliHost, config: object) -> object:
        raise ConfigError(expected='a workspace', received='none', next_action='fix it')

    monkeypatch.setattr(RuntimeCliHost, '_open_workspace', unopenable)

    code, _, stderr = _cli(tmp_path, ScriptedLLM([]), ['chat'], stdin='hello\n')

    [(header, directory, _)] = _recorded(tmp_path)
    assert code != 0
    assert header.status is SpanStatus.ERROR
    assert f'trace：.syndica/traces/{directory.name}\n' in stderr


def test_the_note_about_an_unrecordable_run_goes_through_the_hosts_rules(tmp_path: Path):
    root = tmp_path / 'corp-db7.internal'
    root.mkdir()
    (root / '.syndica.yaml').write_text(
        _rule('custom.host', 'corp-[a-z0-9]+\\.internal'), encoding='utf-8'
    )
    (root / '.syndica').write_text('made by the agent', encoding='utf-8')

    code, stdout, stderr = _cli(root, _done(), ['run', 'go', '--agent', 'Worker'])

    assert (code, stdout) == (0, 'done\n')
    assert '这次 run 不记录 trace' in stderr
    assert 'corp-db7.internal' not in stderr
    assert '[REDACTED:custom.host]' in stderr


def test_the_printed_trace_directory_goes_through_the_hosts_rules(tmp_path: Path):
    (tmp_path / '.syndica.yaml').write_text(
        _rule('custom.stamp', '[0-9]{8}T[0-9]{6}Z'), encoding='utf-8'
    )

    _, _, stderr = _cli(tmp_path, _done(), ['run', 'go', '--agent', 'Worker'])

    [(_, directory, _)] = _recorded(tmp_path)
    prefix = directory.name.split('-')[1]
    assert stderr == f'trace：.syndica/traces/[REDACTED:custom.stamp]-{prefix}\n'


def test_a_run_that_fails_ends_its_trace_in_error(tmp_path: Path):
    failing = ScriptedLLM(
        [
            ScriptedTurn(
                raises=LLMResponseError(
                    expected='a reply', received='HTTP 500', next_action='try again later'
                )
            )
        ]
    )

    code, _, stderr = _cli(tmp_path, failing, ['run', 'go', '--agent', 'Worker'])

    [(header, directory, _)] = _recorded(tmp_path)
    assert code == 1
    assert header.status is SpanStatus.ERROR
    assert f'trace：.syndica/traces/{directory.name}\n' in stderr


def test_a_chat_that_ends_on_a_failed_turn_ends_its_trace_in_error(tmp_path: Path):
    failing = ScriptedLLM(
        [
            ScriptedTurn(
                raises=LLMResponseError(
                    expected='a reply', received='HTTP 500', next_action='try again later'
                )
            )
        ]
    )

    code, _, _ = _cli(tmp_path, failing, ['chat'], stdin='hello\n')

    [(header, _, _)] = _recorded(tmp_path)
    assert code == 1
    assert header.status is SpanStatus.ERROR


def test_a_trace_level_the_host_does_not_know_is_refused_before_the_run(tmp_path: Path):
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: _done(),
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(ConfigError) as caught:
            host.run_request(RunRequest(prompt='go', agent='Worker', trace='verbose'))
    finally:
        host.close()

    assert 'off / structure / replay / full' in caught.value.expected
    assert "'verbose'" in caught.value.received
    assert '--trace' in caught.value.next_action
    assert not (tmp_path / '.syndica').exists()


def test_a_host_directory_another_run_just_made_is_used_rather_than_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    original = Path.mkdir

    def racing(self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False):
        original(self, mode=0o700)
        raise FileExistsError(17, 'File exists', str(self))

    monkeypatch.setattr(Path, 'mkdir', racing)

    directory = trace_directory(tmp_path)

    assert directory == tmp_path / '.syndica' / 'traces' and directory.is_dir()
    assert (tmp_path / '.syndica' / '.gitignore').read_text(encoding='utf-8') == '*\n'


async def test_finishing_a_trace_outlasts_cancellation_and_then_reports_it():
    release = asyncio.Event()
    finished: list[str] = []

    async def finish() -> str:
        await release.wait()
        finished.append('done')
        return 'value'

    task = asyncio.ensure_future(_despite_cancellation(finish()))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == ['done']
    assert await _despite_cancellation(asyncio.sleep(0, 'plain')) == 'plain'


async def test_a_failure_while_finishing_still_surfaces_when_nothing_cancelled():
    async def broken() -> None:
        raise OSError('disk full')

    with pytest.raises(OSError, match='disk full'):
        await _despite_cancellation(broken())


class _SlowFinish:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.statuses: list[SpanStatus] = []

    async def finish(self, status: SpanStatus) -> Any:
        await self.release.wait()
        self.statuses.append(status)
        return None


async def test_a_second_interruption_while_the_trace_finishes_does_not_lose_its_outcome(
    tmp_path: Path,
):
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: _done(),
        workspace_root=tmp_path,
    )
    recording = _SlowFinish()
    finishing = asyncio.ensure_future(
        host._finish_recording(  # pyright: ignore[reportPrivateUsage]
            cast(Any, recording), asyncio.CancelledError(), RuleRedactor()
        )
    )
    await asyncio.sleep(0)
    finishing.cancel()
    await asyncio.sleep(0)
    recording.release.set()

    with pytest.raises(asyncio.CancelledError):
        await finishing
    assert recording.statuses == [SpanStatus.CANCELLED]


def test_a_failed_turn_followed_by_a_good_one_ends_the_chat_trace_ok(tmp_path: Path):
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                raises=LLMResponseError(
                    expected='a reply', received='HTTP 500', next_action='retry'
                )
            ),
            ScriptedTurn(content='recovered'),
        ]
    )
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        target = host.open_chat('.syndica.yaml')
        with pytest.raises(SyndicaError):
            host.run_request(RunRequest(prompt='first', session=target))
        answer = host.run_request(RunRequest(prompt='second', session=target))
    finally:
        host.close()

    [(header, _, _)] = _recorded(tmp_path)
    assert answer == 'recovered'
    assert header.status is SpanStatus.OK
