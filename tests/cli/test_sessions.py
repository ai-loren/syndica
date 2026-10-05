"""CLI 会话：run / chat 落盘，`sessions list/show/rm`，`sessions resume` 续跑（M6 T5）。"""

import os
from io import StringIO
from pathlib import Path

from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, main
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn


def _cli(
    tmp_path: Path,
    llm: ScriptedLLM,
    argv: list[str],
    *,
    stdin: str = '',
) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(stdin), stdout=stdout, stderr=stderr)
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=io,
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
        environ=dict(os.environ),
    )
    try:
        code = main(argv, host=host, io=io)
    finally:
        host.close()
    return code, stdout.getvalue(), stderr.getvalue()


def _done(content: str = 'done') -> ScriptedLLM:
    return ScriptedLLM([ScriptedTurn(content=content)])


def _session_ids(tmp_path: Path) -> list[str]:
    sessions = tmp_path / '.syndica' / 'sessions'
    if not sessions.is_dir():
        return []
    return sorted(p.name for p in sessions.iterdir() if p.is_dir())


def test_a_run_persists_a_session_that_list_and_show_report(tmp_path: Path) -> None:
    code, _, _ = _cli(tmp_path, _done(), ['run', 'go', '--agent', 'Worker'])
    assert code == 0

    sids = _session_ids(tmp_path)
    assert len(sids) == 1
    sid = sids[0]

    code, listing, _ = _cli(tmp_path, _done(), ['sessions', 'list'])
    assert code == 0
    assert sid in listing
    assert 'Worker' in listing

    code, detail, _ = _cli(tmp_path, _done(), ['sessions', 'show', sid])
    assert code == 0
    assert sid in detail
    assert 'Worker' in detail


def test_sessions_rm_deletes_the_session(tmp_path: Path) -> None:
    _cli(tmp_path, _done(), ['run', 'go', '--agent', 'Worker'])
    [sid] = _session_ids(tmp_path)

    code, message, _ = _cli(tmp_path, _done(), ['sessions', 'rm', sid])
    assert code == 0
    assert sid in message
    assert _session_ids(tmp_path) == []


def test_show_and_rm_report_an_absent_session(tmp_path: Path) -> None:
    code, _, stderr = _cli(tmp_path, _done(), ['sessions', 'show', 'nope'])
    assert code == 1
    assert 'nope' in stderr

    code, _, stderr = _cli(tmp_path, _done(), ['sessions', 'rm', 'nope'])
    assert code == 1
    assert 'nope' in stderr


def test_sessions_list_is_empty_before_any_run(tmp_path: Path) -> None:
    code, listing, _ = _cli(tmp_path, _done(), ['sessions', 'list'])
    assert code == 0
    assert '没有会话' in listing


def test_sessions_resume_continues_the_conversation(tmp_path: Path) -> None:
    _cli(tmp_path, _done('first answer'), ['run', 'remember 42', '--agent', 'Worker'])
    [sid] = _session_ids(tmp_path)

    code, stdout, _ = _cli(
        tmp_path,
        _done('second answer'),
        ['sessions', 'resume', sid],
        stdin='what did I say\n',
    )
    assert code == 0
    assert 'second answer' in stdout


def test_run_session_resumes_one_shot_across_processes(tmp_path: Path) -> None:
    _cli(tmp_path, _done('first answer'), ['run', 'remember 42', '--agent', 'Worker'])
    [sid] = _session_ids(tmp_path)

    code, stdout, _ = _cli(
        tmp_path, _done('resumed answer'), ['run', 'what did I say', '--session', sid]
    )
    assert code == 0
    assert 'resumed answer' in stdout
