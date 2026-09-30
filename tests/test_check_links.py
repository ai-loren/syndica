"""Markdown 相对链接检查（spec D18）：经子进程跑脚本，与 CI 里的用法一样。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from syndica.cli.checks import parse_check

REPO_ROOT = Path(__file__).parent.parent
CHECKER = REPO_ROOT / 'scripts' / 'check_links.py'
C_LOCALE = {'LC_ALL': 'C', 'LANG': 'C', 'PYTHONCOERCECLOCALE': '0', 'PYTHONUTF8': '0'}


def _run(
    root: Path | None, *, cwd: Path = REPO_ROOT, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """临时目录的上一级设成 git 的天花板：临时目录不会被当成某个外层仓库的一部分。"""
    arguments = [] if root is None else ['--root', str(root)]
    ceiling = (root or cwd).resolve().parent
    return subprocess.run(
        [sys.executable, str(CHECKER), *arguments],
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ, 'GIT_CEILING_DIRECTORIES': str(ceiling), **(env or {})},
    )


def _tree(root: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    return root


def test_the_repository_has_no_broken_relative_link():
    result = subprocess.run(
        [sys.executable, str(CHECKER)], capture_output=True, text=True, cwd=REPO_ROOT
    )

    assert (result.returncode, result.stdout, result.stderr) == (0, '', '')


def test_the_agents_guide_lists_the_command():
    agents = (REPO_ROOT / 'AGENTS.md').read_text(encoding='utf-8')

    assert 'uv run python scripts/check_links.py' in agents


def test_a_broken_link_is_reported_with_its_file_and_line(tmp_path: Path):
    _tree(tmp_path, {'docs/guide.md': '# Guide\n\nSee [the spec](spec.md).\n'})

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "docs/guide.md:3: 链接指向的 'spec.md' 不存在。" in result.stdout
    assert '改成存在的相对路径' in result.stdout
    assert result.stdout.rstrip().endswith('共 1 处断链。')


# --- 认得出的链接 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'line',
    [
        '[spec](spec.md)',
        '[spec](spec.md#section)',
        '[spec](./spec.md "The spec")',
        "[spec](spec.md 'The spec')",
        '[spec](spec.md (The spec))',
        '[spec](<spec.md>)',
        '[docs](../docs)',
        '[spec](/docs/spec.md)',
        '[a file](my%20file.md)',
        '[a file](<my file.md>)',
        '![diagram](diagram.svg)',
        '![diagram](diagram.svg?raw=true)',
        '[nested [brackets] text](spec.md)',
        '[parens](foo(bar).md)',
        '[brackets](foo[1].md)',
        '[escaped](foo\\_bar.md)',
        '[entity](a&amp;b.md)',
        '[hash](a%23b.md)',
        '[r]: spec.md',
        '[r]: <my file.md> "title"',
    ],
)
def test_links_that_resolve_are_accepted(tmp_path: Path, line: str):
    _tree(
        tmp_path,
        {
            'docs/spec.md': '# Spec\n',
            'docs/my file.md': 'x\n',
            'docs/diagram.svg': '<svg/>\n',
            'docs/foo(bar).md': 'x\n',
            'docs/foo[1].md': 'x\n',
            'docs/foo_bar.md': 'x\n',
            'docs/a&b.md': 'x\n',
            'docs/a#b.md': 'x\n',
            'docs/guide.md': f'{line}\n',
        },
    )

    result = _run(tmp_path)

    assert (result.returncode, result.stdout) == (0, '')


@pytest.mark.parametrize(
    ('text', 'shown'),
    [
        ('[spec](missing.md#section)', 'missing.md#section'),
        ('![diagram](missing.svg)', 'missing.svg'),
        ('[root](/missing.md)', '/missing.md'),
        ('[a file](my%20missing.md)', 'my%20missing.md'),
        ('text `code` then [spec](missing.md)', 'missing.md'),
        ('Run `a` then [x](missing.md) then `b`.', 'missing.md'),
        ('An escaped \\` then [x](missing.md) then `code`.', 'missing.md'),
        ('``code`` then [x](missing.md)', 'missing.md'),
        ('[spec](missing.md "The spec")', 'missing.md'),
        ('[spec](missing.md (The spec))', 'missing.md'),
        ('[spec](missing.md "a \\" b")', 'missing.md'),
        ('[a\\]b](missing.md)', 'missing.md'),
        ('[nested [brackets] text](missing.md)', 'missing.md'),
        ('[parens](missing(bar).md)', 'missing(bar).md'),
        ('``text\n\n[x](missing.md)', 'missing.md'),
        ('[x](<missing file.md>)', 'missing file.md'),
        ('[x](docs/a:b.md)', 'docs/a:b.md'),
        ('[r]: missing.md', 'missing.md'),
        ('[![logo](missing-logo.svg)](https://example.com)', 'missing-logo.svg'),
        ('[![logo](missing-logo.svg)](guide.md)', 'missing-logo.svg'),
        ('See [the\nguide](missing.md).', 'missing.md'),
        ('[the guide](\nmissing.md)', 'missing.md'),
        ('Run `uv run\nfoo` and [g](missing.md) and `bar`.', 'missing.md'),
        ('```uv run pytest```\n\n[x](missing.md)', 'missing.md'),
        ('```\ncode\n````\n[x](missing.md)', 'missing.md'),
        ('```markdown\n```python\nprint(1)\n```\n[y](missing.md)\n```', 'missing.md'),
        ('    ```\n    code\n    ```\n[x](missing.md)', 'missing.md'),
    ],
)
def test_links_that_do_not_resolve_are_reported(tmp_path: Path, text: str, shown: str):
    _tree(tmp_path, {'guide.md': f'{text}\n'})

    result = _run(tmp_path)

    assert result.returncode == 1
    assert f'{shown!r} 不存在' in result.stdout
    assert result.stdout.rstrip().endswith('共 1 处断链。')


def test_a_fence_after_a_byte_order_mark_is_still_a_fence(tmp_path: Path):
    (tmp_path / 'guide.md').write_bytes(
        '\ufeff```\n[a](missing-a.md)\n```\n[b](missing-b.md)\n'.encode()
    )

    result = _run(tmp_path)

    assert 'missing-a.md' not in result.stdout
    assert 'guide.md:4:' in result.stdout


def test_a_crlf_fence_closes(tmp_path: Path):
    (tmp_path / 'guide.md').write_bytes(b'```\r\n[a](missing-a.md)\r\n```\r\n[b](missing-b.md)\r\n')

    result = _run(tmp_path)

    assert 'missing-a.md' not in result.stdout
    assert 'guide.md:4:' in result.stdout


def test_the_line_of_a_wrapped_link_is_where_it_starts(tmp_path: Path):
    _tree(tmp_path, {'guide.md': 'intro\n\nSee [the\nguide](missing.md) and\n[b](missing-b.md)\n'})

    result = _run(tmp_path)

    assert 'guide.md:3:' in result.stdout
    assert 'guide.md:5:' in result.stdout


@pytest.mark.parametrize('name', ['notes.markdown', 'README.MD'])
def test_every_markdown_suffix_is_scanned(tmp_path: Path, name: str):
    _tree(tmp_path, {name: '[x](missing.md)\n'})

    assert _run(tmp_path).returncode == 1


@pytest.mark.parametrize(
    'text',
    [
        '[site](https://example.com/missing.md)',
        '[mail](mailto:someone@example.com)',
        '[repo](git+ssh://example.com/r.git)',
        '![cdn](//cdn.example.com/a.png)',
        '[section](#missing-section)',
        'Write links like `[text](missing.md)` in docs.',
        'Double ``[text](missing.md)`` too.',
        '`` a `[x](missing.md)` b ``',
        'Use `[t](missing.md)\nmore` here.',
        '[text]`code`(missing.md)',
        '\\[x\\](missing.md)',
        '\\[x](missing.md)',
        '<!-- [x](missing.md) -->',
        '<!--\n[x](missing.md)\n\nstill a comment [y](missing.md)\n-->',
        '```markdown\n[text](missing.md)\n```',
        '~~~\n[text](missing.md)\n~~~',
        '````\n```\n[text](missing.md)\n```\n````',
        '~~~\n```\n[text](missing.md)\n```\n~~~',
        '> ```\n> [text](missing.md)\n> ```',
        '> ~~~\n> [text](missing.md)\n> ~~~',
        '1. Step:\n\n    ```\n    [text](missing.md)\n    ```',
    ],
)
def test_external_links_anchors_comments_and_code_are_not_checked(tmp_path: Path, text: str):
    _tree(tmp_path, {'guide.md': f'{text}\n'})

    result = _run(tmp_path)

    assert (result.returncode, result.stdout) == (0, '')


def test_a_link_after_a_closed_fence_is_checked_again(tmp_path: Path):
    _tree(tmp_path, {'guide.md': '```\n[a](missing-a.md)\n```\n[b](missing-b.md)\n'})

    result = _run(tmp_path)

    assert result.returncode == 1
    assert 'missing-a.md' not in result.stdout
    assert 'guide.md:4:' in result.stdout


def test_a_fence_left_open_in_one_file_does_not_hide_the_next_file(tmp_path: Path):
    _tree(tmp_path, {'a.md': '```\nnever closed\n', 'b.md': '[x](missing.md)\n'})

    result = _run(tmp_path)

    assert result.returncode == 1
    assert 'b.md:1:' in result.stdout


# --- 与别人克隆下来的逐字对得上 ---------------------------------------------------------------


def test_a_case_mismatch_is_broken_and_the_real_name_is_suggested(tmp_path: Path):
    _tree(tmp_path, {'docs/spec.md': 'x\n', 'guide.md': '[s](docs/SPEC.md)\n'})

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "大小写对不上：仓库里是 'docs/spec.md'" in result.stdout


def test_a_link_that_leaves_the_root_is_broken_even_if_the_file_exists(tmp_path: Path):
    _tree(tmp_path, {'outside.md': 'x\n', 'repo/guide.md': '[o](../outside.md)\n'})

    result = _run(tmp_path / 'repo')

    assert result.returncode == 1
    assert '跑出了 --root' in result.stdout


def test_a_root_relative_link_is_explained_as_relative_to_the_root(tmp_path: Path):
    _tree(tmp_path, {'docs/guide.md': '[r](/missing.md)\n'})

    result = _run(tmp_path)

    assert '以 / 开头的路径从 --root 算起' in result.stdout


def test_a_component_too_long_for_the_file_system_is_broken_not_a_crash(tmp_path: Path):
    _tree(tmp_path, {'guide.md': f'[x]({"长" * 100}.md)\n'})

    result = _run(tmp_path)

    assert result.returncode == 1
    assert 'Traceback' not in result.stderr


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ['git', *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'},
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / 'repo'
    root.mkdir()
    _git(root, 'init', '-q')
    return root


def test_in_a_git_work_tree_a_committed_or_untracked_file_is_found(repo: Path):
    _tree(
        repo,
        {'docs/spec.md': 'x\n', 'new.md': 'x\n', 'guide.md': '[s](docs/spec.md) [n](new.md)\n'},
    )
    _git(repo, 'add', 'docs/spec.md', 'guide.md')

    result = _run(repo)

    assert (result.returncode, result.stdout) == (0, '')


def test_in_a_git_work_tree_an_ignored_file_is_broken(repo: Path):
    _tree(repo, {'.gitignore': 'secret-notes.md\n', 'secret-notes.md': 'x\n'})
    _tree(repo, {'guide.md': '[n](secret-notes.md)\n'})

    result = _run(repo)

    assert result.returncode == 1
    assert '被 .gitignore 忽略' in result.stdout


def test_in_a_git_work_tree_a_tracked_file_deleted_from_disk_is_broken(repo: Path):
    _tree(repo, {'gone.md': 'x\n', 'guide.md': '[g](gone.md)\n'})
    _git(repo, 'add', 'gone.md')
    (repo / 'gone.md').unlink()

    result = _run(repo)

    assert result.returncode == 1
    assert "'gone.md' 不存在" in result.stdout


def test_in_a_git_work_tree_a_case_mismatch_is_broken(repo: Path):
    _tree(repo, {'Spec.md': 'x\n', 'guide.md': '[s](spec.md) [d](DOCS)\n', 'docs/a.md': 'x\n'})

    result = _run(repo)

    assert "大小写对不上：仓库里是 'Spec.md'" in result.stdout
    assert "大小写对不上：仓库里是 'docs'" in result.stdout


# --- 读不了、跑不了 ---------------------------------------------------------------------------


def test_a_file_that_is_not_utf8_is_an_error_naming_it(tmp_path: Path):
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'docs' / 'latin.md').write_bytes(b'caf\xe9 [x](y.md)\n')

    result = _run(tmp_path)

    assert result.returncode == 2
    assert 'docs/latin.md' in result.stderr
    assert '转成 UTF-8' in result.stderr
    assert 'Traceback' not in result.stderr


def test_a_dangling_symlink_and_a_fifo_are_not_documents(tmp_path: Path):
    (tmp_path / 'dangling.md').symlink_to(tmp_path / 'nowhere.md')
    os.mkfifo(tmp_path / 'pipe.md')

    result = subprocess.run(
        [sys.executable, str(CHECKER), '--root', str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert (result.returncode, result.stdout, result.stderr) == (0, '', '')


@pytest.mark.skipif(os.geteuid() == 0, reason='root 不受目录权限约束')
def test_a_directory_that_cannot_be_listed_is_an_error(tmp_path: Path):
    _tree(tmp_path, {'locked/a.md': 'x\n', 'guide.md': '[a](locked/a.md)\n'})
    (tmp_path / 'locked').chmod(0)
    try:
        result = _run(tmp_path)
    finally:
        (tmp_path / 'locked').chmod(0o755)

    assert result.returncode == 2
    assert '加上读与执行权限' in result.stderr


def test_a_strict_c_locale_reads_utf8_documents_and_prints_the_report(tmp_path: Path):
    _tree(tmp_path, {'指南.md': '# 中文\n\n[x](缺失.md)\n', 'clean.md': '# 中文文档\n'})

    broken = _run(tmp_path, env=C_LOCALE)
    clean = _run(_tree(tmp_path / 'ok', {'文档.md': '# 中文文档\n'}), env=C_LOCALE)

    assert broken.returncode == 1
    assert '共 1 处断链。' in broken.stdout
    assert 'Traceback' not in broken.stderr
    assert (clean.returncode, clean.stderr) == (0, '')


@pytest.mark.parametrize(
    'directory',
    [
        '.git',
        '.mypy_cache',
        '.pytest_cache',
        '.ruff_cache',
        '.syndica',
        '.venv',
        '__pycache__',
        'fixtures',
        'node_modules',
    ],
)
def test_data_and_tool_directories_are_not_scanned(tmp_path: Path, directory: str):
    _tree(tmp_path, {f'tests/{directory}/notes.md': '[x](missing.md)\n'})

    assert _run(tmp_path).returncode == 0


def test_a_missing_root_is_an_error_not_a_pass(tmp_path: Path):
    missing = tmp_path / 'no-such-dir'

    result = _run(missing)

    assert result.returncode == 2
    assert str(missing) in result.stderr
    assert '请从仓库根目录运行' in result.stderr


def test_a_file_given_as_the_root_is_an_error_not_a_pass(tmp_path: Path):
    readme = _tree(tmp_path, {'README.md': '# x\n'}) / 'README.md'

    assert _run(readme).returncode == 2


def test_without_a_root_the_current_directory_is_checked(tmp_path: Path):
    _tree(tmp_path, {'README.md': '[x](missing.md)\n'})

    result = _run(None, cwd=tmp_path)

    assert result.returncode == 1
    assert 'README.md:1:' in result.stdout


@pytest.mark.parametrize(
    'text',
    [
        '`' * 20_000,
        '[x](' * 20_000,
        '[x](a "' * 10_000,
        '<!--' * 20_000,
        '` a ' * 20_000,
        '[' * 40_000,
        ' ' * 50_000 + 'x',
        'a[' * 20_000,
        '[x](a (' * 10_000,
        '[x](' + 'b[' * 20_000,
    ],
    ids=[
        'backticks',
        'link-openers',
        'unclosed-titles',
        'comment-openers',
        'spans',
        'brackets',
        'indentation',
        'unclosed-texts',
        'unclosed-paren-titles',
        'long-target',
    ],
)
def test_pathological_lines_are_checked_in_linear_time(tmp_path: Path, text: str):
    _tree(tmp_path, {'guide.md': f'{text}\n'})
    started = time.monotonic()

    _run(tmp_path)

    assert time.monotonic() - started < 5


def test_the_output_is_what_the_check_parser_reads(tmp_path: Path):
    """检查结果解析器认 `共 N 处断链。` 与成功时不出声：两边的约定改了一边，这里就红。"""
    _tree(tmp_path, {'a.md': '[x](one.md)\n[y](two.md)\n'})
    failed = _run(tmp_path)
    passed = _run(_tree(tmp_path / 'clean', {'a.md': '# nothing\n'}))

    command = 'uv run python scripts/check_links.py'
    fields = {'command': command, 'timed_out': False}
    failed_check = parse_check(
        **fields, exit_code=failed.returncode, stdout=failed.stdout, stderr=failed.stderr
    )
    passed_check = parse_check(
        **fields, exit_code=passed.returncode, stdout=passed.stdout, stderr=passed.stderr
    )

    assert (failed_check.tool, failed_check.status, dict(failed_check.counts)) == (
        'check_links',
        'failed',
        {'broken': 2},
    )
    assert (passed_check.status, dict(passed_check.counts)) == ('passed', {'broken': 0})
