"""`run --handoff` 的判定与落盘（spec D13、D14、D15）：纯函数与文件操作这一层。"""

from __future__ import annotations

import json
import os
import stat
from io import StringIO
from pathlib import Path

import pytest

from syndica.cli.dev_module import AgentRun, DevLogSnapshot, Fingerprint, ToolEntry
from syndica.cli.handoff import (
    HandoffFacts,
    HandoffLocked,
    HandoffWriteError,
    RedactingWriter,
    VerifyResult,
    commit_title,
    exit_code_of,
    front_matter,
    lock_workspace,
    marker_verdict,
    path_key,
    protected_matcher,
    render,
    reviewer_verdict,
    rewrite_handoff,
    status_of,
    terminal_safe,
    write_handoff,
)
from syndica.cli.host_git import Change
from syndica.cli.redaction import host_redactor
from syndica.cli.terminal import one_line
from syndica.kernel.redaction import Redactor

_SECRET = 'Zq9' + 'Lm4Xw7Pk2Rt8Vb5Nc3Jd6'


# --- 转义 -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('raw', 'shown'),
    [
        ('\x1b[2J', '\\x1b[2J'),
        ('\x9b31m', '\\x9b31m'),
        ('a\rb', 'a\\x0db'),
        ('v\x0bf\x0c', 'v\\x0bf\\x0c'),
        ('del\x7f', 'del\\x7f'),
        ('\x00', '\\x00'),
        ('\u202eevil', '\\u202eevil'),
        ('\u2066x\u2069', '\\u2066x\\u2069'),
        ('\u061c', '\\u061c'),
        ('\u200e\u200f', '\\u200e\\u200f'),
        ('line\u2028sep\u2029', 'line\\u2028sep\\u2029'),
    ],
)
def test_control_and_bidi_characters_become_visible_escapes(raw: str, shown: str):
    assert terminal_safe(raw) == shown


def test_newlines_tabs_and_ordinary_text_are_kept():
    text = 'a\tb\n中文 — ✓ ok'

    assert terminal_safe(text) == text


def test_a_lone_surrogate_becomes_a_visible_escape():
    assert terminal_safe(json.loads('"x\\ud83dy\\udc00"')) == 'x\\ud83dy\\udc00'


def test_a_single_line_field_escapes_newlines_and_tabs_too():
    assert one_line('a\nb\tc\x1b') == 'a\\nb\\tc\\x1b'


# --- 标记行 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('text', 'verdict'),
    [
        ('looks good\nverdict: pass', 'pass'),
        ('  verdict：PARTIAL  ', 'partial'),
        ('verdict: Block\nmore text', 'block'),
        ('verdict:unverified', 'unverified'),
        ('no marker here', None),
        ('verdict: pass\nverdict: pass', None),
        ('Verdict: pass', None),
        ('verdict: pass because tests pass', None),
        ('the verdict: pass', None),
        ('verdict: maybe', None),
        (None, None),
        ('verdict: block\r\nverdict: pass', None),
        ('verdict: block\x0c\nverdict: pass', None),
        ('verdict: block\xa0\nverdict: pass', None),
        ('verdict: block\u2028verdict: pass', None),
        ('verdict:\xa0pass', 'pass'),
        ('verdict：\u3000pass', 'pass'),
        ('review done\r\nverdict: partial\r\n', 'partial'),
        ('looks fine\u2028verdict: pass', 'pass'),
        ('verdict: pa\u017f\u017f', None),
        ('verdict: part\u0131al', None),
    ],
)
def test_the_reviewer_marker_counts_only_when_exactly_one_line_matches(
    text: str | None, verdict: str | None
):
    assert marker_verdict(text) == verdict


@pytest.mark.parametrize(
    ('text', 'title'),
    [
        ('done\ncommit_title: fix the parser  ', 'fix the parser'),
        ('commit_title：修复解析器', '修复解析器'),
        ('commit_title: a\ncommit_title: b', None),
        ('commit_title:   ', None),
        ('no title', None),
    ],
)
def test_the_commit_title_counts_only_when_exactly_one_line_matches(text: str, title: str | None):
    assert commit_title(text) == title


# --- 受保护路径 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('path', 'protected'),
    [
        ('.gitignore', True),
        ('src/deep/.gitignore', True),
        ('.gitattributes', True),
        ('not.gitignore', False),
        ('pyproject.toml', True),
        ('sub/pyproject.toml', False),
        ('scripts/check_links.py', True),
        ('scripts/nested/tool.py', True),
        ('myscripts/tool.py', False),
        ('conftest.py', True),
        ('tests/cli/conftest.py', True),
        ('tests/cli/conftest.pyc', False),
        ('a1.txt', True),
        ('a12.txt', False),
        ('a/1.txt', False),
        ('a/.txt', False),
        ('c++.txt', True),
        ('cxx.txt', False),
        ('setup.cfg', True),
        ('sub/setup.cfg', False),
        ('src/.GitIgnore', True),
        ('.GITATTRIBUTES', True),
        ('PyProject.TOML', True),
        ('Scripts/tool.py', True),
        ('tests/CONFTEST.PY', True),
    ],
)
def test_protected_globs_follow_the_d15_rules(path: str, protected: bool):
    matches = protected_matcher(
        ['pyproject.toml', 'scripts/**', '**/conftest.py', 'a?.txt', 'c++.txt', '*.cfg']
    )

    assert matches(path) is protected


# --- Reviewer 结论 ----------------------------------------------------------------------------

_A = (Change('src/a.py', ' M', 'blob-a'),)
_OTHER = (Change('src/a.py', ' M', 'blob-changed-later'),)


def _run(
    text: str = 'verdict: pass',
    *,
    agent: str = 'reviewer-1',
    stop: str | None = 'completed',
    start: tuple[Change, ...] | None = _A,
    end: tuple[Change, ...] | None = _A,
    started: int = 1,
    ended: int = 10,
) -> AgentRun:
    finished = None if stop is None else ended
    return AgentRun(
        agent, 'Reviewer', stop, text, Fingerprint(start), Fingerprint(end), started, finished
    )


def _log(
    *runs: AgentRun, writers: tuple[str, ...] = (), background: tuple[str, ...] = ()
) -> DevLogSnapshot:
    tools = tuple(
        ToolEntry(i, agent, 'Reviewer', 'Write', 'x', None, None) for i, agent in enumerate(writers)
    ) + tuple(
        ToolEntry(100 + i, agent, 'Reviewer', 'Bash', 'sleep 9', None, None, f'h{i}')
        for i, agent in enumerate(background)
    )
    return DevLogSnapshot((), tools, runs)


def test_a_clean_completed_review_that_saw_snapshot_a_gives_its_verdict():
    assert reviewer_verdict(_log(_run()), _A) == 'pass'


@pytest.mark.parametrize(
    'run',
    [
        _run(stop='max_turns'),
        _run(stop=None),
        _run(stop='error'),
        _run(start=_OTHER),
        _run(end=_OTHER),
        _run(start=_OTHER, end=_OTHER),
        _run(start=None),
        _run(end=None),
        _run(text='verdict: pass\nverdict: block'),
        _run(text='all good'),
    ],
    ids=[
        'max-turns',
        'unfinished',
        'error',
        'start-differs',
        'end-differs',
        'clean-but-not-a',
        'start-missing',
        'end-missing',
        'two-markers',
        'no-marker',
    ],
)
def test_a_review_that_fails_any_condition_is_unverified(run: AgentRun):
    assert reviewer_verdict(_log(run), _A) == 'unverified'


def test_a_reviewer_that_wrote_files_itself_is_unverified():
    assert reviewer_verdict(_log(_run(), writers=('reviewer-1',)), _A) == 'unverified'


def test_a_reviewer_that_edited_a_file_itself_is_unverified():
    edit = ToolEntry(1, 'reviewer-1', 'Reviewer', 'Edit', 'scratch.txt', None, None)
    log = DevLogSnapshot((), (edit,), (_run(),))

    assert reviewer_verdict(log, _A) == 'unverified'


def test_a_write_by_another_agent_does_not_disqualify_the_reviewer():
    assert reviewer_verdict(_log(_run(), writers=('worker-1',)), _A) == 'pass'


def test_without_snapshot_a_or_any_review_the_verdict_is_unverified():
    assert reviewer_verdict(_log(_run()), None) == 'unverified'
    assert reviewer_verdict(_log(), _A) == 'unverified'


def test_a_run_that_is_not_a_review_is_skipped():
    worker = AgentRun('worker-1', 'Worker', 'completed', 'verdict: pass', None, None)

    assert reviewer_verdict(_log(worker), _A) == 'unverified'


def test_the_qualifying_review_that_ended_last_wins():
    earlier = _run('verdict: pass', agent='reviewer-1', started=1, ended=5)
    later = _run('verdict: block', agent='reviewer-2', started=2, ended=6)

    assert reviewer_verdict(_log(earlier, later), _A) == 'block'


def test_a_review_that_started_first_but_ended_last_wins():
    outer = _run('verdict: block', agent='reviewer-1', started=1, ended=9)
    forked = _run('verdict: pass', agent='reviewer-fork', started=2, ended=5)

    assert reviewer_verdict(_log(outer, forked), _A) == 'block'


def test_a_later_review_that_does_not_qualify_leaves_the_earlier_one():
    earlier = _run('verdict: partial', agent='reviewer-1', ended=5)
    later = _run('verdict: pass', agent='reviewer-2', end=_OTHER, ended=6)

    assert reviewer_verdict(_log(earlier, later), _A) == 'partial'


def test_a_later_review_without_a_marker_leaves_the_earlier_verdict():
    earlier = _run('verdict: pass', agent='reviewer-1', ended=5)
    later = _run('I looked at it.', agent='reviewer-2', ended=6)

    assert reviewer_verdict(_log(earlier, later), _A) == 'pass'


def test_a_reviewer_that_changed_files_in_an_earlier_run_does_not_count_later():
    edited = _run('editing', started=1, ended=2, start=_OTHER, end=_A)
    woken = _run('verdict: pass', started=3, ended=4)

    assert reviewer_verdict(_log(edited, woken), _A) == 'unverified'


def test_a_review_fix_and_re_review_loop_still_counts():
    first = _run('verdict: block', started=1, ended=2, start=_OTHER, end=_OTHER)
    second = _run('verdict: pass', started=5, ended=6)

    assert reviewer_verdict(_log(first, second), _A) == 'pass'


def test_a_reviewer_that_started_a_background_command_does_not_count():
    assert reviewer_verdict(_log(_run(), background=('reviewer-1',)), _A) == 'unverified'


def test_a_reviewer_whose_fingerprint_went_missing_in_any_run_does_not_count():
    missing = _run('x', started=1, ended=2, start=None, end=None)
    later = _run('verdict: pass', started=3, ended=4)

    assert reviewer_verdict(_log(missing, later), _A) == 'unverified'


# --- 状态、退出码、front matter ---------------------------------------------------------------


@pytest.mark.parametrize(
    ('run_failed', 'read_only', 'host_verify', 'verdict', 'status'),
    [
        (True, True, 'failed', 'pass', 'run_failed'),
        (False, True, 'failed', 'pass', 'read_only'),
        (False, False, 'failed', 'pass', 'verification_failed'),
        (False, False, 'passed', 'pass', 'verified'),
        (False, False, 'passed', 'block', 'unverified'),
        (False, False, 'passed', 'partial', 'unverified'),
        (False, False, 'passed', 'unverified', 'unverified'),
        (False, False, 'not_configured', 'pass', 'unverified'),
        (False, False, 'skipped', 'pass', 'unverified'),
    ],
)
def test_the_status_is_the_first_condition_that_holds(
    run_failed: bool, read_only: bool, host_verify: str, verdict: str, status: str
):
    got = status_of(
        run_failed=run_failed, read_only=read_only, host_verify=host_verify, verdict=verdict
    )

    assert got == status


@pytest.mark.parametrize(
    ('status', 'git', 'code'),
    [
        ('verified', 'not_offered', 0),
        ('unverified', 'declined', 0),
        ('read_only', 'not_offered', 0),
        ('verification_failed', 'not_offered', 1),
        ('run_failed', 'not_offered', 1),
        ('verified', 'committed', 0),
        ('verified', 'pushed', 0),
        ('verified', 'push_failed', 1),
        ('verified', 'failed', 1),
    ],
)
def test_the_exit_code_follows_the_status_and_the_git_outcome(status: str, git: str, code: int):
    assert exit_code_of(status, git) == code


def test_the_front_matter_has_fixed_keys_in_order_and_ascii_values():
    text = front_matter(
        status='verified',
        host_verify='passed',
        verdict='pass',
        git='pending',
        protected_changed=['.gitignore', 'docs/中文.md'],
        exit_code=None,
    )

    assert text == (
        '---\n'
        'schema: syndica.handoff/1\n'
        'status: verified\n'
        'host_verify: passed\n'
        'reviewer_verdict: pass\n'
        'git: pending\n'
        'protected_changed: [".gitignore", "docs/\\u4e2d\\u6587.md"]\n'
        'exit_code: null\n'
        '---\n'
    )
    assert text.isascii()


def test_the_final_front_matter_carries_the_exit_code():
    text = front_matter(
        status='run_failed',
        host_verify='skipped',
        verdict='unverified',
        git='not_offered',
        protected_changed=[],
        exit_code=1,
    )

    assert 'protected_changed: []\nexit_code: 1\n' in text


# --- 锁 ---------------------------------------------------------------------------------------


def test_a_second_handoff_run_on_the_same_workspace_is_refused_until_the_first_ends(tmp_path: Path):
    first = lock_workspace(tmp_path)
    try:
        with pytest.raises(HandoffLocked) as caught:
            lock_workspace(tmp_path)
        assert '另一个 run 锁着' in str(caught.value)
    finally:
        os.close(first)

    os.close(lock_workspace(tmp_path))


def test_the_lock_survives_the_agent_deleting_the_syndica_directory(tmp_path: Path):
    (tmp_path / '.syndica').mkdir()
    first = lock_workspace(tmp_path)
    try:
        (tmp_path / '.syndica').rmdir()
        with pytest.raises(HandoffLocked):
            lock_workspace(tmp_path)
    finally:
        os.close(first)


# --- 落盘 -------------------------------------------------------------------------------------


def _redactor() -> Redactor:
    return host_redactor({'SERVICE_TOKEN': _SECRET})


def test_the_handoff_is_written_redacted_with_mode_0600(tmp_path: Path):
    path = write_handoff(tmp_path, 'h.md', f'token {_SECRET}\n', _redactor())

    assert path == tmp_path / '.syndica' / 'handoffs' / 'h.md'
    assert path.read_text(encoding='utf-8') == 'token [REDACTED:secret.env_value]\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_gitignore_is_written_only_when_the_directory_is_first_created(tmp_path: Path):
    write_handoff(tmp_path, 'one.md', 'x\n', _redactor())
    gitignore = tmp_path / '.syndica' / '.gitignore'
    assert gitignore.read_text(encoding='utf-8') == '*\n'
    gitignore.write_text('# mine\n*\n', encoding='utf-8')

    write_handoff(tmp_path, 'two.md', 'y\n', _redactor())

    assert gitignore.read_text(encoding='utf-8') == '# mine\n*\n'


@pytest.mark.parametrize('content', [(), ('notes.txt',)], ids=['empty', 'with-a-file'])
def test_a_syndica_directory_the_agent_created_first_still_gets_a_gitignore(
    tmp_path: Path, content: tuple[str, ...]
):
    (tmp_path / '.syndica').mkdir()
    for name in content:
        (tmp_path / '.syndica' / name).write_text('agent was here\n', encoding='utf-8')

    write_handoff(tmp_path, 'h.md', 'x\n', _redactor())

    assert (tmp_path / '.syndica' / '.gitignore').read_text(encoding='utf-8') == '*\n'


def test_paths_that_name_the_same_file_on_macos_compare_equal():
    assert path_key('SRC/Cafe\u0301.py') == path_key('src/café.py')
    assert path_key('src/a.py') != path_key('src/b.py')


def test_a_symlinked_gitignore_is_left_alone_and_its_target_untouched(tmp_path: Path):
    victim = tmp_path / 'victim.txt'
    victim.write_text('precious\n', encoding='utf-8')
    root = tmp_path / 'ws'
    (root / '.syndica').mkdir(parents=True)
    (root / '.syndica' / '.gitignore').symlink_to(victim)

    write_handoff(root, 'h.md', 'x\n', _redactor())

    assert victim.read_text(encoding='utf-8') == 'precious\n'


@pytest.mark.parametrize('link', ['.syndica', '.syndica/handoffs'])
def test_a_symlinked_directory_is_refused_and_nothing_is_written_behind_it(
    tmp_path: Path, link: str
):
    root = tmp_path / 'ws'
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / '.syndica').mkdir(parents=True, exist_ok=True)
    if link == '.syndica':
        (root / '.syndica').rmdir()
    (root / link).symlink_to(outside, target_is_directory=True)

    with pytest.raises(HandoffWriteError) as caught:
        write_handoff(root, 'h.md', 'x\n', _redactor())

    assert '符号链接' in str(caught.value)
    assert list(outside.iterdir()) == []


def test_a_file_where_the_directory_should_be_is_refused(tmp_path: Path):
    (tmp_path / '.syndica').write_text('not a directory\n', encoding='utf-8')

    with pytest.raises(HandoffWriteError) as caught:
        write_handoff(tmp_path, 'h.md', 'x\n', _redactor())

    assert '实际: 它不是目录' in str(caught.value)
    assert '链接' not in str(caught.value)


def test_a_lone_surrogate_in_the_text_is_written_as_a_visible_escape(tmp_path: Path):
    truncated_emoji = json.loads('"cut \\ud83d here"')

    path = write_handoff(tmp_path, 'h.md', truncated_emoji, _redactor())

    assert path.read_text(encoding='utf-8') == 'cut \\ud83d here'


def test_a_first_write_that_fails_leaves_no_empty_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / '.syndica' / 'handoffs').mkdir(parents=True)
    (tmp_path / '.syndica' / '.gitignore').write_text('*\n', encoding='utf-8')

    def broken(descriptor: int, data: bytes) -> int:
        raise OSError('disk full')

    with monkeypatch.context() as patch:
        patch.setattr(os, 'write', broken)
        with pytest.raises(OSError):
            write_handoff(tmp_path, 'h.md', 'x\n', _redactor())

    assert list((tmp_path / '.syndica' / 'handoffs').iterdir()) == []


def test_an_existing_handoff_file_is_never_overwritten_by_the_first_write(tmp_path: Path):
    write_handoff(tmp_path, 'h.md', 'first\n', _redactor())

    with pytest.raises(FileExistsError):
        write_handoff(tmp_path, 'h.md', 'second\n', _redactor())


def test_the_final_rewrite_replaces_the_file_atomically_and_stays_private(tmp_path: Path):
    path = write_handoff(tmp_path, 'h.md', 'git: pending\n', _redactor())

    rewrite_handoff(tmp_path, path, f'git: committed {_SECRET}\n', _redactor())

    assert path.read_text(encoding='utf-8') == 'git: committed [REDACTED:secret.env_value]\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in path.parent.iterdir()) == ['h.md']


def test_the_rewrite_checks_the_directory_again_and_never_writes_behind_a_new_symlink(
    tmp_path: Path,
):
    root = tmp_path / 'ws'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    path = write_handoff(root, 'h.md', 'git: pending\n', _redactor())
    (root / '.syndica' / 'handoffs').rename(tmp_path / 'moved')
    (root / '.syndica' / 'handoffs').symlink_to(outside, target_is_directory=True)

    with pytest.raises(HandoffWriteError):
        rewrite_handoff(root, path, 'git: committed\n', _redactor())

    assert list(outside.iterdir()) == []


def test_a_short_write_still_leaves_the_whole_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`os.write` 可以只写进一部分而不报错（文件大小上限、磁盘将满）。"""
    real = os.write

    def short(descriptor: int, data: bytes) -> int:
        return real(descriptor, bytes(data)[:5])

    text = 'git: pending\nthe rest of the handoff\n'
    with monkeypatch.context() as patch:
        patch.setattr(os, 'write', short)
        path = write_handoff(tmp_path, 'h.md', text, _redactor())
        first = path.read_text(encoding='utf-8')
        rewrite_handoff(tmp_path, path, f'{text}git: committed\n', _redactor())

    assert first == text
    assert path.read_text(encoding='utf-8') == f'{text}git: committed\n'


def test_a_rewrite_that_fails_midway_leaves_the_file_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = write_handoff(tmp_path, 'h.md', 'git: pending\n', _redactor())

    def broken(descriptor: int, data: bytes) -> int:
        raise OSError('disk full')

    with monkeypatch.context() as patch:
        patch.setattr(os, 'write', broken)
        with pytest.raises(OSError):
            rewrite_handoff(tmp_path, path, 'git: committed\n', _redactor())

    assert path.read_text(encoding='utf-8') == 'git: pending\n'
    assert sorted(p.name for p in path.parent.iterdir()) == ['h.md']
    rewrite_handoff(tmp_path, path, 'git: committed\n', _redactor())
    assert path.read_text(encoding='utf-8') == 'git: committed\n'


# --- 流写入器 ---------------------------------------------------------------------------------


def test_the_writer_redacts_a_secret_split_across_two_writes():
    out = StringIO()
    writer = RedactingWriter(out, _redactor())

    writer.write(f'key {_SECRET[:10]}')
    writer.write(f'{_SECRET[10:]} end\nnext')

    assert out.getvalue() == 'key [REDACTED:secret.env_value] end\n'


def test_flush_writes_the_unfinished_line_so_prompts_are_visible():
    out = StringIO()
    writer = RedactingWriter(out, _redactor())

    writer.write(f'Approve {_SECRET}? [approve/reject]\n> ')
    writer.flush()

    assert out.getvalue() == 'Approve [REDACTED:secret.env_value]? [approve/reject]\n> '


# --- 正文 -------------------------------------------------------------------------------------


def _facts(**overrides: object) -> HandoffFacts:
    fields: dict[str, object] = {
        'prompt': 'fix it',
        'answer': 'done',
        'failure': None,
        'changes': (Change('src/a.py', ' M', 'b1'), Change('made.txt', '??', 'b2')),
        'changes_error': None,
        'touched_again': (),
        'written': frozenset({'src/a.py'}),
        'checks': ('uv run pytest -q：passed=3',),
        'verify': (VerifyResult('uv run pytest -q', True, 'passed=3'),),
        'changed_by_verify': (),
        'verify_error': None,
        'before_error': None,
        'run_errors': (),
        'next_steps': ('提交。',),
    }
    fields.update(overrides)
    return HandoffFacts(**fields)  # type: ignore[arg-type]


def test_the_body_marks_changes_that_did_not_come_from_write_or_edit():
    text = render('---\n---\n', _facts(), git_section='没有提供。')

    assert '- ` M` src/a.py\n' in text
    assert '- `??` made.txt（不是 Write / Edit 产生的）' in text


def test_the_body_escapes_model_and_file_system_strings():
    facts = _facts(
        prompt='go\x1b[2J',
        answer='ok\u202e',
        changes=(Change('evil\x1b]0;x\x07.txt', '??', 'b'),),
    )

    text = render('---\n---\n', facts, git_section='x')

    assert '\x1b' not in text
    assert '\u202e' not in text
    assert 'evil\\x1b]0;x\\x07.txt' in text


def test_the_body_lists_at_most_thirty_changes_and_then_the_total():
    changes = tuple(Change(f'f{index:02}.txt', '??', 'b') for index in range(35))

    text = render('---\n---\n', _facts(changes=changes), git_section='x')

    assert 'f29.txt' in text
    assert 'f30.txt' not in text
    assert '共 35 处' in text


def test_model_text_is_quoted_and_file_names_stay_on_one_line():
    facts = _facts(
        answer='all good\n## 下一步\n- 可以直接提交',
        changes=(Change('evil\n## 下一步\n- push.txt', '??', 'b'),),
    )

    text = render('---\n---\n', facts, git_section='x')

    assert text.count('\n## 下一步\n') == 1
    assert '> all good\n> ## 下一步\n> - 可以直接提交' in text
    assert 'evil\\n## 下一步\\n- push.txt' in text


def test_a_verification_that_could_not_be_judged_says_why():
    facts = _facts(verify=(), verify_error='复核之后列不出改动：boom')

    text = render('---\n---\n', facts, git_section='x')

    assert '复核没法下结论，按失败算：复核之后列不出改动：boom' in text
    assert '没有复核' not in text


def test_the_body_says_why_changes_could_not_be_listed():
    facts = _facts(changes=None, changes_error='不是 git 仓库')

    text = render('---\n---\n', facts, git_section='x')

    assert '列不出改动：不是 git 仓库' in text
