"""`syndica trace list / show / replay / prune`（M3 T15）：退出码、回放自己记的 trace 指向原 trace、
prune 按开始时间留、不动没有结局的。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

import syndica.__main__ as entry
from syndica.cli import trace_commands
from syndica.cli.handoff import trace_directory
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, TraceRequest, main
from syndica.cli.trace_commands import Replayer
from syndica.kernel import (
    ContractViolation,
    FileTraceStore,
    LLMResponseError,
    RedactionRule,
    RuleRedactor,
    ScenarioDocument,
    ScriptedLLM,
    ScriptedTurn,
    SpanStatus,
    ToolCall,
    TraceHeader,
    TraceLevel,
    TraceRecorder,
    TraceRedaction,
    TraceSource,
)
from syndica.kernel.redaction import RULES_VERSION, Redactor

_NOT_GIVEN: Any = object()


def _cli(
    root: Path,
    argv: list[str],
    *,
    llm: ScriptedLLM | None = None,
    replayer: Replayer | None = _NOT_GIVEN,
) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(''), stdout=stdout, stderr=stderr)
    host = RuntimeCliHost(
        io=io,
        model_factory=lambda _: _model(llm),
        workspace_root=root,
        environ={key: value for key, value in os.environ.items() if not key.startswith('GIT_')},
        replayer=entry._replay_document if replayer is _NOT_GIVEN else replayer,  # pyright: ignore[reportPrivateUsage]
    )
    try:
        code = main(argv, host=host, io=io)
    finally:
        host.close(interrupted=False)
    return code, stdout.getvalue(), stderr.getvalue()


def _model(llm: ScriptedLLM | None) -> ScriptedLLM:
    assert llm is not None, '这条命令不该要模型'
    return llm


def _worker_run(root: Path, *extra: str) -> TraceHeader:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[ToolCall(id='c1', name='Bash', arguments='{"command": "printf hi"}')]
            ),
            ScriptedTurn(content='done'),
        ]
    )
    before = {header.trace_id for header in _headers(root)}
    code, _, stderr = _cli(root, ['run', 'go', '--agent', 'Worker', *extra], llm=llm)
    assert code == 0, stderr
    [header] = [header for header in _headers(root) if header.trace_id not in before]
    return header


def _store(root: Path) -> FileTraceStore:
    return FileTraceStore(trace_directory(root))


def _headers(root: Path) -> list[TraceHeader]:
    return asyncio.run(_store(root).traces())


def _directory(root: Path, trace_id: str) -> Path:
    directory = asyncio.run(_store(root).directory(trace_id))
    assert directory is not None
    return directory


def _planted(
    root: Path,
    trace_id: str,
    started_s: int,
    *,
    finished: bool = True,
    ended_s: int | None = None,
    level: TraceLevel = TraceLevel.STRUCTURE,
    source: TraceSource | None = None,
) -> None:
    async def plant() -> None:
        store = _store(root)
        redaction = TraceRedaction(RuleRedactor().digest, RULES_VERSION)
        await store.begin(
            TraceHeader(
                trace_id,
                started_s * 10**9,
                level,
                redaction,
                source=TraceSource() if source is None else source,
            )
        )
        if finished:
            ended = started_s * 10**9 + 1 if ended_s is None else ended_s * 10**9
            await store.finish(trace_id, SpanStatus.OK, ended)

    asyncio.run(plant())


def _on_disk(directory: Path) -> int:
    total = 0
    for parent, _, files in os.walk(directory):
        total += sum(os.lstat(Path(parent, name)).st_size for name in files)
    return total


# --- list -----------------------------------------------------------------------------------


def test_list_reports_the_size_the_files_take_on_disk(tmp_path: Path):
    header = _worker_run(tmp_path)

    code, out, _ = _cli(tmp_path, ['trace', 'list'])

    assert code == 0
    total = _on_disk(_directory(tmp_path, header.trace_id))
    assert 1024 <= total < 1024 * 1024
    [line] = out.splitlines()
    assert f'  {header.trace_id[:8]}  ok  1 个 Agent  ' in line
    assert f'  {total / 1024:.1f} KB  run go --agent Worker' in line


def test_a_trace_whose_spans_cannot_be_read_does_not_hide_the_others(tmp_path: Path):
    broken = _worker_run(tmp_path)
    intact = _worker_run(tmp_path)
    spans = _directory(tmp_path, broken.trace_id) / 'spans.jsonl'
    spans.write_text('not json\n' + spans.read_text(encoding='utf-8'), encoding='utf-8')

    code, out, _ = _cli(tmp_path, ['trace', 'list'])

    assert code == 0
    rows = {line.split('  ')[1]: line for line in out.splitlines()}
    assert 'span 读不进来' in rows[broken.trace_id[:8]]
    assert '1 个 Agent' in rows[intact.trace_id[:8]]


# --- show -----------------------------------------------------------------------------------


def test_show_finds_the_trace_by_a_prefix_of_its_id(tmp_path: Path):
    header = _worker_run(tmp_path)

    code, out, _ = _cli(tmp_path, ['trace', 'show', header.trace_id[:6]])

    assert code == 0
    assert out.startswith(f'trace {header.trace_id}  ok  记录级别 replay\n')


@pytest.mark.parametrize('form', ['name', 'printed path', 'trailing slash'])
def test_a_trace_is_found_by_its_directory_name_as_the_run_printed_it(tmp_path: Path, form: str):
    header = _worker_run(tmp_path)
    shown = _directory(tmp_path, header.trace_id).relative_to(tmp_path)
    given = {'name': shown.name, 'printed path': str(shown), 'trailing slash': f'{shown}/'}[form]

    code, out, _ = _cli(tmp_path, ['trace', 'show', given])

    assert code == 0
    assert out.startswith(f'trace {header.trace_id}  ')


def test_two_traces_that_share_a_directory_prefix_are_told_apart_by_name(tmp_path: Path):
    first, second = 'ab' * 16, 'ab' * 4 + 'cd' * 12
    _planted(tmp_path, first, 7)
    _planted(tmp_path, second, 7)
    names = {_directory(tmp_path, trace_id).name: trace_id for trace_id in (first, second)}
    assert len(names) == 2

    for name, trace_id in names.items():
        code, out, _ = _cli(tmp_path, ['trace', 'show', name])
        assert code == 0
        assert out.startswith(f'trace {trace_id}  ')


def test_prune_deletes_a_trace_named_by_its_directory(tmp_path: Path):
    _planted(tmp_path, 'aa' * 16, 1)
    _planted(tmp_path, 'bb' * 16, 2)
    name = _directory(tmp_path, 'bb' * 16).name

    code, _, _ = _cli(tmp_path, ['trace', 'prune', name])

    assert code == 0
    assert _ids(tmp_path) == {'aa'}


def test_a_directory_name_that_is_not_there_exits_2(tmp_path: Path):
    _planted(tmp_path, 'aa' * 16, 1)

    code, _, err = _cli(tmp_path, ['trace', 'show', '20000101T000000Z-abcdef01'])

    assert code == 2
    assert '目录名是 20000101T000000Z-abcdef01' in err


def test_show_draws_who_created_whom_from_the_recorded_edges(tmp_path: Path):
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='d1',
                        name='Delegate',
                        arguments=json.dumps({'agent': 'Explorer', 'query': 'look around'}),
                    )
                ]
            ),
            ScriptedTurn(content='explored'),
            ScriptedTurn(content='done'),
        ]
    )
    code, _, err = _cli(tmp_path, ['run', 'go', '--agent', 'Worker'], llm=llm)
    assert code == 0, err
    [header] = _headers(tmp_path)

    code, out, _ = _cli(tmp_path, ['trace', 'show', header.trace_id[:8]])

    assert code == 0
    lines = out.splitlines()
    tree = lines[lines.index('谁创建了谁：') + 1 : lines.index('谁在等谁：') - 1]
    assert len(tree) == 2
    assert tree[0].startswith('  Worker ')
    assert tree[1].startswith('     └─ Explorer ')


@pytest.mark.parametrize(
    ('given', 'anchor'),
    [
        ('ab', '多给几个字符'),
        ('cd', '被 prune 掉的找不回来'),
        ('AB', '小写十六进制'),
    ],
)
def test_show_of_a_trace_it_cannot_pin_down_exits_2(tmp_path: Path, given: str, anchor: str):
    _planted(tmp_path, 'ab' + '1' * 30, 1)
    _planted(tmp_path, 'ab' + '2' * 30, 2)

    code, out, err = _cli(tmp_path, ['trace', 'show', given])

    assert (code, out) == (2, '')
    assert anchor in err


def test_show_of_a_trace_whose_files_were_changed_exits_2_and_says_what_to_do(tmp_path: Path):
    header = _worker_run(tmp_path)
    spans = _directory(tmp_path, header.trace_id) / 'spans.jsonl'
    spans.write_text('not json\n' + spans.read_text(encoding='utf-8'), encoding='utf-8')

    code, _, err = _cli(tmp_path, ['trace', 'show', header.trace_id[:8]])

    assert code == 2
    assert '删掉它所在的目录' in err


# --- replay ---------------------------------------------------------------------------------


def test_a_replay_that_reproduces_exits_0_and_records_a_trace_pointing_back(tmp_path: Path):
    original = _worker_run(tmp_path)
    config = tmp_path / '.syndica.yaml'
    config.write_text('version: 1\n', encoding='utf-8')
    argv = ['trace', 'replay', original.trace_id[:8]]

    code, out, err = _cli(tmp_path, argv)

    assert code == 0, err
    assert out == (
        f'复现：trace {original.trace_id} 的回放与记录逐项相同。\n'
        '没有重建的宿主 Module 与 Flag：dev（它们的作用已经在记录里）。\n'
    )
    [replayed] = [h for h in _headers(tmp_path) if h.trace_id != original.trace_id]
    assert replayed.source.replay_of == original.trace_id
    assert replayed.source.argv == tuple(argv)
    assert replayed.source.config_sha256 == hashlib.sha256(config.read_bytes()).hexdigest()
    assert (replayed.level, replayed.status) == (TraceLevel.STRUCTURE, SpanStatus.OK)
    shown = _directory(tmp_path, replayed.trace_id).relative_to(tmp_path)
    assert err == f'trace：{shown}\n'


def _tampered(root: Path, header: TraceHeader) -> None:
    replay = _directory(root, header.trace_id) / 'replay.json'
    document = json.loads(replay.read_text(encoding='utf-8'))
    document['agents']['Worker']['instances'][0]['tool_results'][0]['output'] = 'tampered'
    replay.write_text(json.dumps(document), encoding='utf-8')


def test_a_replay_that_diverges_exits_1_and_names_the_turn_and_the_unit(tmp_path: Path):
    original = _worker_run(tmp_path)
    _tampered(tmp_path, original)

    code, out, _ = _cli(tmp_path, ['trace', 'replay', original.trace_id[:8]])

    assert code == 1
    lines = out.splitlines()
    assert lines[0] == f'分叉：trace {original.trace_id} 的回放有 1 处与记录不同：'
    assert lines[1] == '  request  Worker[0] 第 2 轮第 1 次调用：请求的「第 2 条历史」与记录不同'
    [replayed] = [h for h in _headers(tmp_path) if h.trace_id != original.trace_id]
    assert replayed.source.replay_of == original.trace_id


def _unfinished(root: Path) -> str:
    trace_id = 'ef' * 16
    _planted(root, trace_id, 1, finished=False)
    return trace_id[:8]


def _structure_only(root: Path) -> str:
    return _worker_run(root, '--trace', 'structure').trace_id[:8]


def _a_replay(root: Path) -> str:
    original = _worker_run(root)
    assert _cli(root, ['trace', 'replay', original.trace_id[:8]])[0] == 0
    [replayed] = [h for h in _headers(root) if h.trace_id != original.trace_id]
    return replayed.trace_id[:8]


def _not_json(root: Path) -> str:
    header = _worker_run(root)
    (_directory(root, header.trace_id) / 'replay.json').write_text('{', encoding='utf-8')
    return header.trace_id[:8]


def _symlinked_replay(root: Path) -> str:
    header = _worker_run(root)
    replay = _directory(root, header.trace_id) / 'replay.json'
    copy = root / 'copy.json'
    copy.write_bytes(replay.read_bytes())
    replay.unlink()
    replay.symlink_to(copy)
    return header.trace_id[:8]


def _unreadable_replay(root: Path) -> str:
    header = _worker_run(root)
    (_directory(root, header.trace_id) / 'replay.json').chmod(0)
    return header.trace_id[:8]


def _nested_too_deep(root: Path) -> str:
    header = _worker_run(root)
    replay = _directory(root, header.trace_id) / 'replay.json'
    replay.write_text('[' * 100_000 + ']' * 100_000, encoding='utf-8')
    return header.trace_id[:8]


def _rules_that_cannot_coexist(root: Path) -> str:
    header = _worker_run(root)
    path = _directory(root, header.trace_id) / 'trace.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    value['redaction']['rules'] = [
        {'rule_id': 'custom.a', 'pattern': 'x'},
        {'rule_id': 'custom.a', 'pattern': 'y'},
    ]
    path.write_text(json.dumps(value), encoding='utf-8')
    return header.trace_id[:8]


def _no_document(argv: tuple[str, ...], level: TraceLevel) -> Any:
    def make(root: Path) -> str:
        _planted(root, 'ab' * 16, 10, level=level, source=TraceSource(argv=argv))
        return 'abab'

    return make


def _refused_by_the_replay(root: Path) -> str:
    header = _worker_run(root)
    replay = _directory(root, header.trace_id) / 'replay.json'
    document = json.loads(replay.read_text(encoding='utf-8'))
    document['agents']['Worker']['frozen']['system_prompt'] = {'payload': 'a' * 64}
    replay.write_text(json.dumps(document), encoding='utf-8')
    return header.trace_id[:8]


@pytest.mark.parametrize(
    ('make', 'anchor'),
    [
        (lambda root: 'cd', '被 prune 掉的找不回来'),
        (_unfinished, '记录到一半就断了'),
        (_structure_only, '用 --trace replay 重新跑一次'),
        (_a_replay, '回放原来那个：syndica trace replay '),
        (_not_json, '重新记录这次 run'),
        (_symlinked_replay, '已经不可信'),
        (_unreadable_replay, '这个 trace 的权限'),
        (_nested_too_deep, '重新记录这次 run'),
        (_rules_that_cannot_coexist, '这个 trace 的头被改过'),
        (_refused_by_the_replay, '用 --trace replay 重新跑一次那条命令'),
        (_no_document(('chat',), TraceLevel.STRUCTURE), '用 `syndica run` 跑同样的需求'),
        (
            _no_document(('run', '--topology', 'dev', 'go'), TraceLevel.REPLAY),
            '用 --agent <角色> 单独跑那个角色',
        ),
        (
            _no_document(('run', '--topology=dev', 'go'), TraceLevel.STRUCTURE),
            '用 --agent <角色> 单独跑那个角色',
        ),
        (_no_document(('run', 'go'), TraceLevel.REPLAY), 'stderr 上的那条 WARNING'),
    ],
    ids=[
        'unknown',
        'unfinished',
        'structure',
        'replay-of-a-replay',
        'not-json',
        'symlink',
        'unreadable',
        'nested',
        'rules',
        'refused',
        'chat',
        'topology',
        'topology-equals',
        'capture-refused',
    ],
)
def test_a_trace_the_replay_cannot_use_exits_2_and_says_what_to_do(
    tmp_path: Path, make: Any, anchor: str
):
    given = make(tmp_path)
    before = {header.trace_id for header in _headers(tmp_path)}

    code, out, err = _cli(tmp_path, ['trace', 'replay', given])

    assert (code, out) == (2, '')
    assert anchor in err
    assert err.count('下一步') == 1
    assert {header.trace_id for header in _headers(tmp_path)} == before


def test_a_replay_of_a_replay_says_which_trace_it_came_from(tmp_path: Path):
    given = _a_replay(tmp_path)
    [replayed] = [h for h in _headers(tmp_path) if h.trace_id.startswith(given)]
    assert replayed.source.replay_of is not None

    _, _, err = _cli(tmp_path, ['trace', 'replay', given])

    assert f'syndica trace replay {replayed.source.replay_of[:8]}\n' in err


def _with_digest(root: Path, header: TraceHeader, digest: str) -> None:
    path = _directory(root, header.trace_id) / 'trace.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    value['redaction']['digest'] = digest
    path.write_text(json.dumps(value), encoding='utf-8')


@pytest.mark.parametrize(
    ('changed', 'said'),
    [
        ('version', f'记录时第 {RULES_VERSION} 版，现在第 {RULES_VERSION + 1} 版'),
        ('digest', '摘要与记录时不同：版本一样'),
    ],
)
def test_a_rule_set_that_changed_since_recording_is_its_own_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str, said: str
):
    original = _worker_run(tmp_path)
    if changed == 'version':
        monkeypatch.setattr(trace_commands, 'RULES_VERSION', RULES_VERSION + 1)
    else:
        _with_digest(tmp_path, original, 'f' * 64)

    code, out, _ = _cli(tmp_path, ['trace', 'replay', original.trace_id[:8]])

    assert code == 1
    lines = out.splitlines()
    assert lines[0] == f'分叉：trace {original.trace_id} 的回放有 1 处与记录不同：'
    assert lines[1].startswith('  rules  脱敏规则集')
    assert said in lines[1]


def test_the_replay_redacts_its_requests_with_the_rules_recorded_in_the_trace(tmp_path: Path):
    rule = 'version: 1\nredaction:\n  rules:\n    - {id: custom.place, pattern: "工作区"}\n'
    (tmp_path / '.syndica.yaml').write_text(rule, encoding='utf-8')
    original = _worker_run(tmp_path)
    (tmp_path / '.syndica.yaml').write_text('version: 1\n', encoding='utf-8')

    code, out, _ = _cli(tmp_path, ['trace', 'replay', original.trace_id[:8]])

    assert code == 0, out
    assert out.startswith('复现：')


def test_a_replay_with_recording_switched_off_writes_no_trace(tmp_path: Path):
    original = _worker_run(tmp_path)
    (tmp_path / '.syndica.yaml').write_text('version: 1\ntrace: {level: off}\n', encoding='utf-8')

    code, _, err = _cli(tmp_path, ['trace', 'replay', original.trace_id[:8]])

    assert code == 0
    assert [h.trace_id for h in _headers(tmp_path)] == [original.trace_id]
    assert 'trace：' not in err


@dataclass
class _Seen:
    calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


@dataclass(frozen=True)
class _Verdict:
    divergences: Sequence[Any] = ()
    unrebuilt: Sequence[str] = ()


def _spying(seen: _Seen) -> Replayer:
    def replay(
        document: ScenarioDocument,
        *,
        redactor: Redactor,
        allow_divergence: bool,
        recorder: TraceRecorder | None,
    ) -> _Verdict:
        seen.calls.append({'redactor': redactor, 'allow_divergence': allow_divergence})
        return _Verdict()

    return replay


@pytest.mark.parametrize('flag', [True, False])
def test_the_replay_gets_the_recorded_rules_and_the_divergence_flag(tmp_path: Path, flag: bool):
    rules = 'version: 1\nredaction:\n  rules:\n    - {id: custom.ticket, pattern: "TKT-[0-9]+"}\n'
    (tmp_path / '.syndica.yaml').write_text(rules, encoding='utf-8')
    original = _worker_run(tmp_path)
    (tmp_path / '.syndica.yaml').write_text('version: 1\n', encoding='utf-8')
    seen = _Seen()
    argv = ['trace', 'replay', original.trace_id[:8], *(['--allow-divergence'] if flag else [])]

    code, _, _ = _cli(tmp_path, argv, replayer=_spying(seen))

    assert code == 0
    [call] = seen.calls
    assert call['allow_divergence'] is flag
    assert isinstance(call['redactor'], RuleRedactor)
    assert call['redactor'].rules == (RedactionRule('custom.ticket', 'TKT-[0-9]+'),)


def test_a_replay_that_fails_still_reports_where_its_own_trace_went(tmp_path: Path):
    original = _worker_run(tmp_path)

    def failing(
        document: ScenarioDocument,
        *,
        redactor: Redactor,
        allow_divergence: bool,
        recorder: TraceRecorder | None,
    ) -> _Verdict:
        assert recorder is not None

        async def record() -> None:
            await recorder.begin()
            await recorder.finish(SpanStatus.ERROR)

        asyncio.run(record())
        raise ContractViolation(expected='e', received='r', next_action='the replay could not run')

    code, _, err = _cli(tmp_path, ['trace', 'replay', original.trace_id[:8]], replayer=failing)

    assert code == 2
    assert 'the replay could not run' in err
    [replayed] = [h for h in _headers(tmp_path) if h.trace_id != original.trace_id]
    shown = _directory(tmp_path, replayed.trace_id).relative_to(tmp_path)
    assert err.startswith(f'trace：{shown}\n期望: ')


def test_a_host_without_a_replayer_says_where_replay_works(tmp_path: Path):
    original = _worker_run(tmp_path)

    code, _, err = _cli(tmp_path, ['trace', 'replay', original.trace_id[:8]], replayer=None)

    assert code == 2
    assert '`syndica` 命令行' in err


def test_the_command_line_entry_wires_the_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    original = _worker_run(tmp_path)
    monkeypatch.chdir(tmp_path)
    io = CliIO(stdin=StringIO(''), stdout=StringIO(), stderr=StringIO())
    host = entry._LazyHost(io)  # pyright: ignore[reportPrivateUsage]

    try:
        outcome = host.trace(TraceRequest('replay', trace_id=original.trace_id[:8]))
    finally:
        host.close()

    assert not isinstance(outcome, str)
    assert outcome.exit_code == 0
    assert outcome.output.startswith('复现：')


# --- prune ----------------------------------------------------------------------------------


def _ids(root: Path) -> set[str]:
    return {header.trace_id[:2] for header in _headers(root)}


def test_prune_keeps_the_newest_finished_traces_by_start_time(tmp_path: Path):
    for prefix, started in [('aa', 30), ('bb', 10), ('cc', 40), ('dd', 20)]:
        _planted(tmp_path, prefix * 16, started)
    _planted(tmp_path, 'ee' * 16, 5, finished=False)

    code, out, _ = _cli(tmp_path, ['trace', 'prune', '--keep', '2'])

    assert code == 0
    assert _ids(tmp_path) == {'aa', 'cc', 'ee'}
    assert out.splitlines() == [
        f'删掉了 2 个 trace：{"dd" * 4}, {"bb" * 4}。',
        '留下最近 2 个有结局的 trace，连同它们的回放。',
        f'没有结局的 1 个没动：{"ee" * 4}（还在写，或者记录到一半就断了；确认之后点名删：'
        'syndica trace prune <id>）。',
    ]


def test_prune_keeps_the_replays_of_what_it_keeps_and_never_lets_them_crowd_out_an_original(
    tmp_path: Path,
):
    _planted(tmp_path, 'aa' * 16, 10)
    _planted(tmp_path, 'bb' * 16, 20)
    _planted(tmp_path, 'cc' * 16, 30, source=TraceSource(replay_of='aa' * 16))
    _planted(tmp_path, 'dd' * 16, 40, source=TraceSource(replay_of='bb' * 16))
    _planted(tmp_path, 'ee' * 16, 50, source=TraceSource(replay_of='bb' * 16))

    code, out, _ = _cli(tmp_path, ['trace', 'prune', '--keep', '1'])

    assert code == 0
    assert _ids(tmp_path) == {'bb', 'dd', 'ee'}
    assert out.splitlines()[0] == f'删掉了 2 个 trace：{"cc" * 4}, {"aa" * 4}。'


def test_prune_by_default_keeps_fifty(tmp_path: Path):
    for number in range(52):
        _planted(tmp_path, f'{number:02x}' * 16, 100 + number)

    code, _, _ = _cli(tmp_path, ['trace', 'prune'])

    assert code == 0
    assert _ids(tmp_path) == {f'{number:02x}' for number in range(2, 52)}


def test_prune_keep_0_removes_every_finished_trace_and_nothing_else(tmp_path: Path):
    _planted(tmp_path, 'aa' * 16, 1)
    _planted(tmp_path, 'bb' * 16, 2, finished=False)
    traces = trace_directory(tmp_path)
    (traces / 'notes.txt').write_text('mine', encoding='utf-8')
    (traces / 'scratch').mkdir()

    code, _, _ = _cli(tmp_path, ['trace', 'prune', '--keep', '0'])

    assert code == 0
    assert _ids(tmp_path) == {'bb'}
    assert (traces / 'notes.txt').read_text(encoding='utf-8') == 'mine'
    assert (traces / 'scratch').is_dir()


def test_prune_deletes_exactly_the_named_traces_even_unfinished_ones(tmp_path: Path):
    _planted(tmp_path, 'aa' * 16, 1)
    _planted(tmp_path, 'bb' * 16, 2, finished=False)
    _planted(tmp_path, 'cc' * 16, 3)

    code, out, _ = _cli(tmp_path, ['trace', 'prune', 'bb', 'cccc', 'cc'])

    assert code == 0
    assert _ids(tmp_path) == {'aa'}
    assert out == f'删掉了 2 个 trace：{"bb" * 4}, {"cc" * 4}。\n'


def test_prune_naming_a_trace_that_is_not_there_deletes_nothing(tmp_path: Path):
    _planted(tmp_path, 'aa' * 16, 1)

    code, _, err = _cli(tmp_path, ['trace', 'prune', 'aa', 'cd'])

    assert code == 2
    assert '被 prune 掉的找不回来' in err
    assert _ids(tmp_path) == {'aa'}


@pytest.mark.parametrize(
    ('argv', 'anchor'),
    [
        (['trace', 'prune', 'aa', '--keep', '3'], '去掉其中一个'),
        (['trace', 'prune', 'aa', '--keep', '0'], '去掉其中一个'),
        (['trace', 'prune', '--keep', '-1'], 'non-negative integer'),
        (['trace'], '{list,show,replay,export,prune}'),
    ],
)
def test_contradictory_or_malformed_trace_arguments_are_refused(
    tmp_path: Path, argv: list[str], anchor: str
):
    _planted(tmp_path, 'aa' * 16, 1)

    code, out, err = _cli(tmp_path, argv)

    assert (code, out) == (2, '')
    assert anchor in err
    assert _ids(tmp_path) == {'aa'}


@pytest.mark.parametrize(
    'argv',
    [['trace', 'list'], ['trace', 'show', 'ab'], ['trace', 'prune'], ['trace', 'replay', 'ab']],
)
def test_reading_traces_where_none_were_recorded_creates_nothing(tmp_path: Path, argv: list[str]):
    _cli(tmp_path, argv)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('linked', ['.syndica', '.syndica/traces'])
def test_a_trace_directory_that_is_a_symlink_is_refused_before_anything_is_deleted(
    tmp_path: Path, linked: str
):
    elsewhere = tmp_path / 'elsewhere'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    elsewhere.mkdir()
    _planted(elsewhere, 'aa' * 16, 1)
    if linked == '.syndica/traces':
        (workspace / '.syndica').mkdir()
    (workspace / linked).symlink_to(elsewhere / linked, target_is_directory=True)

    code, _, err = _cli(workspace, ['trace', 'prune', '--keep', '0'])

    assert code == 2
    assert '符号链接' in err
    assert _ids(elsewhere) == {'aa'}


def test_allowing_divergence_lets_the_replay_report_every_one(tmp_path: Path):
    original = _worker_run(tmp_path)
    replay = _directory(tmp_path, original.trace_id) / 'replay.json'
    document = json.loads(replay.read_text(encoding='utf-8'))
    document['agents']['Worker']['instances'][0]['tool_results'][0]['output'] = 'tampered'
    document['expect']['stop_reasons'] = {'Worker[0]': 'max_turns'}
    replay.write_text(json.dumps(document), encoding='utf-8')
    argv = ['trace', 'replay', original.trace_id[:8]]

    stopped = _cli(tmp_path, argv)
    continued = _cli(tmp_path, [*argv, '--allow-divergence'])

    assert (stopped[0], continued[0]) == (1, 1)
    assert [line.split('  ')[1] for line in stopped[1].splitlines()[1:2]] == ['request']
    assert [line.split('  ')[1] for line in continued[1].splitlines()[1:3]] == [
        'request',
        'outcome',
    ]


def test_a_replay_trace_that_was_left_unfinished_is_labelled_so(tmp_path: Path):
    original = _worker_run(tmp_path)

    def abandoning(
        document: ScenarioDocument,
        *,
        redactor: Redactor,
        allow_divergence: bool,
        recorder: TraceRecorder | None,
    ) -> _Verdict:
        assert recorder is not None
        asyncio.run(recorder.begin())
        return _Verdict()

    code, _, err = _cli(tmp_path, ['trace', 'replay', original.trace_id[:8]], replayer=abandoning)

    *reasons, label = err.splitlines()
    assert code == 0
    assert label.startswith('trace（不完整')
    assert [line for line in reasons if '写入任务被取消' in line] == reasons[:1]


def test_a_replay_puts_back_the_gitignore_that_keeps_traces_out_of_git(tmp_path: Path):
    original = _worker_run(tmp_path)
    gitignore = tmp_path / '.syndica' / '.gitignore'
    gitignore.unlink()

    code, _, _ = _cli(tmp_path, ['trace', 'replay', original.trace_id[:8]])

    assert code == 0
    assert gitignore.read_text(encoding='utf-8') == '*\n'


@pytest.mark.parametrize('action', ['show', 'replay'])
def test_a_host_asked_about_a_trace_without_its_id_says_what_is_missing(
    tmp_path: Path, action: str
):
    _worker_run(tmp_path)
    io = CliIO(stdin=StringIO(''), stdout=StringIO(), stderr=StringIO())
    host = RuntimeCliHost(
        io=io,
        model_factory=lambda _: _model(None),
        workspace_root=tmp_path,
        replayer=entry._replay_document,  # pyright: ignore[reportPrivateUsage]
    )

    try:
        with pytest.raises(trace_commands.TraceUnusable) as caught:
            host.trace(TraceRequest(action, trace_id=None))  # pyright: ignore[reportArgumentType]
    finally:
        host.close(interrupted=False)

    assert caught.value.received == '没有给'
    assert 'syndica trace list' in caught.value.next_action


def test_names_planted_in_the_trace_directory_reach_the_terminal_escaped(tmp_path: Path):
    _planted(tmp_path, 'aa' * 16, 1)
    (trace_directory(tmp_path) / 'x\x1b[2J\x1b]0;PWNED\x07').mkdir()

    for argv in (['trace', 'list'], ['trace', 'show', 'aa'], ['trace', 'prune']):
        code, out, err = _cli(tmp_path, argv)

        assert code == 0, err
        assert '\x1b' not in out + err and '\x07' not in out + err
        assert '\\x1b[2J' in err


def test_replaying_a_failed_run_reproduces_it_without_printing_the_original_failure(
    tmp_path: Path,
):
    failure = LLMResponseError(
        expected='a reply', received='HTTP 500 \x1b]0;PWNED\x07', next_action='try again later'
    )
    code, _, _ = _cli(
        tmp_path,
        ['run', 'go', '--agent', 'Worker'],
        llm=ScriptedLLM([ScriptedTurn(raises=failure)]),
    )
    assert code == 1
    [original] = _headers(tmp_path)

    code, out, err = _cli(tmp_path, ['trace', 'replay', original.trace_id[:8]])

    assert code == 0, out
    assert out.startswith('复现：')
    [replayed] = [h for h in _headers(tmp_path) if h.trace_id != original.trace_id]
    assert err == f'trace：{_directory(tmp_path, replayed.trace_id).relative_to(tmp_path)}\n'


def test_an_unreadable_span_file_exits_2_for_show_and_leaves_the_rest_of_the_list(
    tmp_path: Path,
):
    broken = _worker_run(tmp_path)
    intact = _worker_run(tmp_path)
    (_directory(tmp_path, broken.trace_id) / 'spans.jsonl').chmod(0)

    shown = _cli(tmp_path, ['trace', 'show', broken.trace_id[:8]])
    listed = _cli(tmp_path, ['trace', 'list'])

    assert shown[0] == 2
    assert '这个 trace 的权限' in shown[2]
    assert listed[0] == 0
    rows = {line.split('  ')[1]: line for line in listed[1].splitlines()}
    assert 'span 读不进来' in rows[broken.trace_id[:8]]
    assert '1 个 Agent' in rows[intact.trace_id[:8]]


def test_prune_reports_the_traces_it_could_not_delete_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _planted(tmp_path, 'aa' * 16, 1)
    _planted(tmp_path, 'bb' * 16, 2)

    async def keeps(self: FileTraceStore, trace_id: str) -> None:
        return None

    monkeypatch.setattr(FileTraceStore, 'delete', keeps)

    code, out, _ = _cli(tmp_path, ['trace', 'prune', '--keep', '0'])

    assert code == 1
    assert out.splitlines() == [
        '删掉了 0 个 trace。',
        '留下最近 0 个有结局的 trace，连同它们的回放。',
        f'没删掉 {"bb" * 4}：删完之后它还在',
        f'没删掉 {"aa" * 4}：删完之后它还在',
    ]


def test_a_copied_trace_directory_is_neither_listed_nor_counted_as_deleted(tmp_path: Path):
    header = _worker_run(tmp_path)
    directory = _directory(tmp_path, header.trace_id)
    shutil.copytree(directory, directory.parent / 'backup')

    listed = _cli(tmp_path, ['trace', 'list'])
    pruned = _cli(tmp_path, ['trace', 'prune', '--keep', '0'])

    [row, skipped] = listed[1].splitlines()
    assert row.split()[2] == header.trace_id[:8]
    assert skipped == '另有 1 个 trace 目录读不进来，没有列出（原因见上面的 WARNING）。'
    assert pruned[0] == 0
    assert pruned[1].splitlines() == [
        f'删掉了 1 个 trace：{header.trace_id[:8]}。',
        '留下最近 0 个有结局的 trace，连同它们的回放。',
    ]
    assert _headers(tmp_path) == []


def test_prune_keeps_by_when_traces_started_not_when_they_ended(tmp_path: Path):
    _planted(tmp_path, 'aa' * 16, 10, ended_s=100)
    _planted(tmp_path, 'bb' * 16, 20, ended_s=21)

    code, _, _ = _cli(tmp_path, ['trace', 'prune', '--keep', '1'])

    assert code == 0
    assert _ids(tmp_path) == {'bb'}


def test_a_replay_whose_trace_directory_cannot_be_prepared_replays_unrecorded(tmp_path: Path):
    original = _worker_run(tmp_path)
    own = tmp_path / '.syndica'
    (own / '.gitignore').unlink()
    own.chmod(0o500)
    try:
        code, out, err = _cli(tmp_path, ['trace', 'replay', original.trace_id[:8]])
    finally:
        own.chmod(0o700)

    assert code == 0
    assert out.startswith('复现：')
    assert '这次回放不记录 trace' in err and '回放照常进行' in err
    assert [h.trace_id for h in _headers(tmp_path)] == [original.trace_id]


def test_a_replay_under_an_explicit_trace_level_prints_nothing_but_its_trace(tmp_path: Path):
    original = _worker_run(tmp_path)
    (tmp_path / '.syndica.yaml').write_text(
        'version: 1\ntrace: {level: replay}\n', encoding='utf-8'
    )

    code, _, err = _cli(tmp_path, ['trace', 'replay', original.trace_id[:8]])

    assert code == 0
    [replayed] = [h for h in _headers(tmp_path) if h.trace_id != original.trace_id]
    assert err == f'trace：{_directory(tmp_path, replayed.trace_id).relative_to(tmp_path)}\n'


def _a_twin(root: Path, header: TraceHeader) -> None:
    directory = _directory(root, header.trace_id)
    shutil.copytree(directory, directory.parent / f'20991231T000000Z-{header.trace_id[:8]}')


def test_two_directories_claiming_one_trace_do_not_break_the_list(tmp_path: Path):
    claimed = _worker_run(tmp_path)
    other = _worker_run(tmp_path)
    _a_twin(tmp_path, claimed)

    code, out, _ = _cli(tmp_path, ['trace', 'list'])

    assert code == 0
    rows = [line for line in out.splitlines() if line.split('  ')[1] == claimed.trace_id[:8]]
    assert len(rows) == 2
    assert all('span 读不进来' in row and '大小不明' in row for row in rows)
    assert any(line.split('  ')[1] == other.trace_id[:8] for line in out.splitlines())


def test_prune_reports_a_trace_two_directories_claim_once(tmp_path: Path):
    claimed = _worker_run(tmp_path)
    _a_twin(tmp_path, claimed)

    code, out, _ = _cli(tmp_path, ['trace', 'prune', '--keep', '0'])

    assert code == 1
    failures = [line for line in out.splitlines() if line.startswith('没删掉')]
    assert len(failures) == 1
    assert claimed.trace_id[:8] in failures[0] and '只有一个目录' in failures[0]


def test_a_directory_name_is_matched_only_against_the_traces_it_could_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    for prefix, started in [('aa', 1), ('bb', 2), ('cc', 3)]:
        _planted(tmp_path, prefix * 16, started)
    name = _directory(tmp_path, 'bb' * 16).name
    asked: list[str] = []
    real = FileTraceStore.directory

    async def counting(self: FileTraceStore, trace_id: str) -> Path | None:
        asked.append(trace_id)
        return await real(self, trace_id)

    monkeypatch.setattr(FileTraceStore, 'directory', counting)

    code, _, _ = _cli(tmp_path, ['trace', 'show', name])

    assert code == 0
    assert asked == ['bb' * 16]


@dataclass(frozen=True)
class _Divergence:
    kind: str
    where: str
    detail: str


def test_divergences_reported_by_the_replay_stay_one_line_each_and_inert(tmp_path: Path):
    original = _worker_run(tmp_path)

    def diverging(
        document: ScenarioDocument,
        *,
        redactor: Redactor,
        allow_divergence: bool,
        recorder: TraceRecorder | None,
    ) -> _Verdict:
        return _Verdict(
            divergences=[_Divergence('tool', 'Worker[0]\x1b[2J', 'line one\nline two\x07')]
        )

    code, out, _ = _cli(tmp_path, ['trace', 'replay', original.trace_id[:8]], replayer=diverging)

    assert code == 1
    lines = out.splitlines()
    assert lines[1] == '  tool  Worker[0]\\x1b[2J：line one\\nline two\\x07'
    assert len(lines) == 2


# --- traces this version cannot read --------------------------------------------------------


def _rewritten_header(root: Path, change: Any) -> tuple[TraceHeader, str]:
    header = _worker_run(root)
    directory = _directory(root, header.trace_id)
    path = directory / 'trace.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    change(value)
    path.write_text(json.dumps(value), encoding='utf-8')
    return header, directory.name


def _from_a_newer_syndica(value: dict[str, Any]) -> None:
    value['schema'] = 'syndica.trace/2'
    value['added_in_2'] = {'anything': True}
    del value['redaction']


def test_a_trace_from_a_newer_syndica_is_reported_as_newer_and_never_as_tampered(tmp_path: Path):
    header, name = _rewritten_header(tmp_path, _from_a_newer_syndica)

    listed = _cli(tmp_path, ['trace', 'list'])
    by_prefix = _cli(tmp_path, ['trace', 'show', header.trace_id[:8]])
    by_name = _cli(tmp_path, ['trace', 'replay', name])

    assert listed[:2] == (0, '另有 1 个 trace 目录读不进来，没有列出（原因见上面的 WARNING）。\n')
    for code, _, err in (by_prefix, by_name):
        assert code == 2
        assert 'syndica.trace/2' in err and '不要删掉' in err
    for _, _, err in (listed, by_prefix, by_name):
        assert '多半是 Agent 的 Bash' not in err and '被 prune 掉的找不回来' not in err


def test_a_trace_whose_header_broke_is_named_as_unreadable_not_as_missing(tmp_path: Path):
    header, _ = _rewritten_header(tmp_path, lambda value: value.update(started_ns='soon'))

    code, _, err = _cli(tmp_path, ['trace', 'show', header.trace_id[:8]])

    assert code == 2
    assert '读不进来' in err and '不要了就删掉这个目录' in err
    assert '被 prune 掉的找不回来' not in err


def test_a_replay_still_runs_when_the_config_cannot_be_read_and_says_whose_rules_it_used(
    tmp_path: Path,
):
    header = _worker_run(tmp_path)
    (tmp_path / '.syndica.yaml').write_text('trace: [never closed\n', encoding='utf-8')

    code, out, err = _cli(tmp_path, ['trace', 'replay', header.trace_id[:8]])

    assert code == 0 and out.startswith('复现：'), err
    assert '配置读不了' in err and '这个 trace 记下的脱敏规则' in err
    [replayed] = [h for h in _headers(tmp_path) if h.source.replay_of == header.trace_id]
    assert replayed.status is SpanStatus.OK
