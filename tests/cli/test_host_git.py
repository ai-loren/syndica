"""宿主 git 环境（spec D14）：改动列表与 blob id。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from syndica.cli.host_git import Change, GitUnavailable, HostGit, host_environment


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    return {
        **os.environ,
        'HOME': str(home),
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_NOSYSTEM': '1',
    }


def _git(root: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _repo(tmp_path: Path, files: dict[str, str], *, object_format: str = 'sha1') -> Path:
    root = tmp_path / 'repo'
    root.mkdir()
    env = _env(tmp_path)
    _git(root, env, 'init', '-q', f'--object-format={object_format}')
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    if files:
        _git(root, env, 'add', '.')
        _git(root, env, 'commit', '-q', '-m', 'init')
    return root


def _hash(root: Path, env: dict[str, str], *paths: str) -> list[str]:
    return _git(root, env, 'hash-object', '--', *paths).split()


def test_the_host_environment_turns_off_prompts_pathspec_magic_and_optional_locks():
    environment = host_environment({'PATH': '/bin', 'GIT_TERMINAL_PROMPT': '1'})

    assert environment == {
        'PATH': '/bin',
        'GIT_TERMINAL_PROMPT': '0',
        'GIT_LITERAL_PATHSPECS': '1',
        'GIT_OPTIONAL_LOCKS': '0',
    }


@pytest.mark.parametrize(
    'name',
    [
        'GIT_DIR',
        'GIT_WORK_TREE',
        'GIT_INDEX_FILE',
        'GIT_OBJECT_DIRECTORY',
        'GIT_ALTERNATE_OBJECT_DIRECTORIES',
        'GIT_COMMON_DIR',
        'GIT_NAMESPACE',
        'GIT_PREFIX',
        'GIT_CONFIG_PARAMETERS',
        'GIT_CONFIG_COUNT',
        'GIT_CONFIG_KEY_0',
        'GIT_CONFIG_VALUE_0',
    ],
)
def test_variables_that_point_git_elsewhere_are_dropped(name: str):
    environment = host_environment({name: 'x', 'GIT_CONFIG_GLOBAL': '/dev/null'})

    assert name not in environment
    assert environment['GIT_CONFIG_GLOBAL'] == '/dev/null'


def test_a_git_dir_in_the_hosts_environment_does_not_redirect_the_snapshot(tmp_path: Path):
    root = _repo(tmp_path, {'keep.txt': 'k\n'})
    (root / 'mine.txt').write_text('mine\n', encoding='utf-8')
    other = tmp_path / 'other'
    other.mkdir()
    env = _env(tmp_path)
    _git(other, env, 'init', '-q')
    (other / 'foreign.txt').write_text('f\n', encoding='utf-8')

    changes = HostGit(
        root, {**env, 'GIT_DIR': str(other / '.git'), 'GIT_WORK_TREE': str(other)}
    ).changes()

    assert [change.path for change in changes] == ['mine.txt']


def test_modified_deleted_renamed_and_untracked_files_are_listed_with_blob_ids(tmp_path: Path):
    root = _repo(tmp_path, {'a.txt': 'a\n', 'b.txt': 'b\n', 'c.txt': 'c\n'})
    env = _env(tmp_path)
    (root / 'a.txt').write_text('changed\n', encoding='utf-8')
    (root / 'b.txt').unlink()
    _git(root, env, 'mv', 'c.txt', 'd.txt')
    (root / 'e.txt').write_text('new\n', encoding='utf-8')
    (root / '0.txt').write_text(
        'untracked, listed after the tracked ones by git\n', encoding='utf-8'
    )

    changes = HostGit(root, env).changes()

    zero, a, d, e = _hash(root, env, '0.txt', 'a.txt', 'd.txt', 'e.txt')
    assert changes == (
        Change('0.txt', '??', zero, mode='100644'),
        Change('a.txt', ' M', a, mode='100644'),
        Change('b.txt', ' D', None),
        Change('d.txt', 'R ', d, origin='c.txt', mode='100644'),
        Change('e.txt', '??', e, mode='100644'),
    )


def test_a_rename_detected_in_the_work_tree_keeps_its_origin(tmp_path: Path):
    root = _repo(tmp_path, {'old.txt': 'same content for rename detection\n'})
    env = _env(tmp_path)
    (root / 'old.txt').rename(root / 'new.txt')
    _git(root, env, 'add', '-N', 'new.txt')

    (change,) = HostGit(root, env).changes()

    assert (change.path, change.status, change.origin) == ('new.txt', ' R', 'old.txt')


def test_a_copy_reported_by_git_keeps_its_origin(tmp_path: Path):
    content = ''.join(f'line {n}\n' for n in range(40))
    root = _repo(tmp_path, {'a.txt': content})
    env = _env(tmp_path)
    _git(root, env, 'config', 'status.renames', 'copies')
    (root / 'a.txt').write_text(content + 'more\n', encoding='utf-8')
    (root / 'b.txt').write_text(content, encoding='utf-8')
    _git(root, env, 'add', 'a.txt', 'b.txt')

    changes = HostGit(root, env).changes()

    assert [(c.path, c.status[0], c.origin) for c in changes] == [
        ('a.txt', 'M', None),
        ('b.txt', 'C', 'a.txt'),
    ]


def test_a_rename_into_the_hosts_directory_shows_up_as_a_deletion(tmp_path: Path):
    root = _repo(tmp_path, {'src/keep.py': 'k\n'})
    env = _env(tmp_path)
    (root / '.syndica').mkdir()
    _git(root, env, 'mv', 'src/keep.py', '.syndica/keep.py')

    changes = HostGit(root, env).changes()

    assert [(c.path, c.status) for c in changes] == [('src/keep.py', 'D ')]


def test_a_plain_file_named_like_the_hosts_directory_is_not_a_change(tmp_path: Path):
    root = _repo(tmp_path, {'keep.txt': 'k\n'})
    (root / '.syndica').write_text('in the way\n', encoding='utf-8')

    assert HostGit(root, _env(tmp_path)).changes() == ()


def test_a_git_that_cannot_be_executed_is_unavailable_with_the_reason(tmp_path: Path):
    tools = tmp_path / 'bin'
    tools.mkdir()
    (tools / 'git').write_text('#!/bin/sh\n', encoding='utf-8')
    (tools / 'git').chmod(0o644)
    env = {**_env(tmp_path), 'PATH': str(tools)}

    with pytest.raises(GitUnavailable) as caught:
        HostGit(tmp_path, env).run('status')

    assert '系统拒绝了' in str(caught.value)


def test_a_file_name_that_starts_with_a_space_keeps_it(tmp_path: Path):
    root = _repo(tmp_path, {'keep.txt': 'k\n'})
    env = _env(tmp_path)
    (root / ' lead.txt').write_text('x\n', encoding='utf-8')

    (change,) = HostGit(root, env).changes()

    assert (change.path, change.blob) == (' lead.txt', _hash(root, env, ' lead.txt')[0])


def test_the_mode_shows_a_permission_change_and_a_file_swapped_for_a_symlink(tmp_path: Path):
    root = _repo(tmp_path, {'run.sh': 'echo hi\n'})
    env = _env(tmp_path)
    (root / 'run.sh').write_text('echo changed\n', encoding='utf-8')
    (root / 'note').write_text('target', encoding='utf-8')
    git = HostGit(root, env)
    before = git.changes()

    (root / 'run.sh').chmod(0o755)
    (root / 'note').unlink()
    (root / 'note').symlink_to('target')
    after = git.changes()

    assert [(c.path, c.mode) for c in before] == [('note', '100644'), ('run.sh', '100644')]
    assert [(c.path, c.mode) for c in after] == [('note', '120000'), ('run.sh', '100755')]
    assert [c.blob for c in before] == [c.blob for c in after]


def test_a_workspace_in_a_subdirectory_sees_only_its_own_files_relative_to_itself(tmp_path: Path):
    root = _repo(tmp_path, {'pkg/a.py': 'a\n', 'top.txt': 'top\n', 'pkg/top.txt': 'inner\n'})
    env = _env(tmp_path)
    (root / 'top.txt').write_text('outside the workspace\n', encoding='utf-8')
    (root / 'pkg' / 'a.py').write_text('changed\n', encoding='utf-8')
    (root / 'pkg' / 'top.txt').write_text('inner changed\n', encoding='utf-8')
    workspace = root / 'pkg'

    changes = HostGit(workspace, env).changes()

    assert [(c.path, c.status) for c in changes] == [('a.py', ' M'), ('top.txt', ' M')]
    assert [c.blob for c in changes] == _hash(workspace, env, 'a.py', 'top.txt')


@pytest.mark.parametrize('kind', ['missing', 'file'])
def test_a_workspace_that_is_not_a_directory_is_unavailable_with_a_next_step(
    tmp_path: Path, kind: str
):
    root = tmp_path / 'ws'
    if kind == 'file':
        root.write_text('x\n', encoding='utf-8')

    with pytest.raises(GitUnavailable) as caught:
        HostGit(root, _env(tmp_path)).changes()

    assert '不存在或不是目录' in str(caught.value)


def test_a_filter_selected_by_an_agent_written_gitattributes_never_runs(tmp_path: Path):
    root = _repo(tmp_path, {'a.txt': 'a\n'})
    env = _env(tmp_path)
    marker = tmp_path / 'filter-ran'
    script = tmp_path / 'clean.sh'
    script.write_text(f'#!/bin/sh\ntouch {marker}\ncat\n', encoding='utf-8')
    script.chmod(0o755)
    _git(root, env, 'config', 'filter.evil.clean', str(script))
    (root / '.gitattributes').write_text('* filter=evil\n', encoding='utf-8')
    (root / 'a.txt').write_text('changed\n', encoding='utf-8')
    _git(root, env, 'hash-object', 'a.txt')
    assert marker.exists()
    marker.unlink()

    HostGit(root, env).changes()

    assert not marker.exists()


def test_a_symlink_to_bytes_that_are_not_utf8_gets_the_blob_git_stores(tmp_path: Path):
    root = _repo(tmp_path, {'keep.txt': 'k\n'})
    env = _env(tmp_path)
    os.symlink(b'caf\xe9', bytes(root / 'link'))

    (change,) = HostGit(root, env).changes()
    _git(root, env, 'add', 'link')

    assert change.blob == _git(root, env, 'ls-files', '-s', 'link').split()[1]


def test_git_reads_nothing_from_the_hosts_stdin(tmp_path: Path):
    root = _repo(tmp_path, {'keep.txt': 'k\n'})
    reader, writer = os.pipe()
    os.write(writer, b'data that must not be read\n')
    os.close(writer)
    saved = os.dup(0)
    os.dup2(reader, 0)
    try:
        blob = HostGit(root, _env(tmp_path)).run('hash-object', '--stdin').decode().strip()
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(reader)

    assert blob == 'e69de29bb2d1d6434b8b29ae775ad8c2e48c5391'


def test_a_git_call_that_hangs_is_cut_off_at_its_timeout(tmp_path: Path):
    tools = tmp_path / 'bin'
    tools.mkdir()
    fake = tools / 'git'
    fake.write_text('#!/bin/sh\nsleep 5\n', encoding='utf-8')
    fake.chmod(0o755)
    env = {**_env(tmp_path), 'PATH': f'{tools}:{os.environ["PATH"]}'}

    with pytest.raises(GitUnavailable) as caught:
        HostGit(tmp_path, env).run('status', timeout=0.5)

    assert '0.5 秒内结束' in str(caught.value)


def test_every_file_in_an_untracked_directory_is_listed_on_its_own(tmp_path: Path):
    root = _repo(tmp_path, {'keep.txt': 'k\n'})
    env = _env(tmp_path)
    (root / 'newdir' / 'sub').mkdir(parents=True)
    (root / 'newdir' / 'a.txt').write_text('a\n', encoding='utf-8')
    (root / 'newdir' / 'sub' / 'b.txt').write_text('b\n', encoding='utf-8')

    changes = HostGit(root, env).changes()

    blobs = _hash(root, env, 'newdir/a.txt', 'newdir/sub/b.txt')
    assert [(change.path, change.blob) for change in changes] == [
        ('newdir/a.txt', blobs[0]),
        ('newdir/sub/b.txt', blobs[1]),
    ]


def test_the_hosts_own_directory_is_not_a_change(tmp_path: Path):
    root = _repo(tmp_path, {'keep.txt': 'k\n'})
    (root / '.syndica' / 'handoffs').mkdir(parents=True)
    (root / '.syndica' / 'handoffs' / 'x.md').write_text('h\n', encoding='utf-8')
    (root / '.syndica-notes.txt').write_text('mine\n', encoding='utf-8')

    changes = HostGit(root, _env(tmp_path)).changes()

    assert [change.path for change in changes] == ['.syndica-notes.txt']


@pytest.mark.parametrize(
    'name',
    ['with space.txt', '中文.txt', '-leading-dash.txt', 'star*.txt', ':(top)x', 'line\nbreak.txt'],
)
def test_unusual_file_names_are_listed_and_hashed(tmp_path: Path, name: str):
    root = _repo(tmp_path, {'keep.txt': 'k\n'})
    env = _env(tmp_path)
    (root / name).write_text('odd\n', encoding='utf-8')

    (change,) = HostGit(root, env).changes()

    assert (change.path, change.status) == (name, '??')
    assert change.blob == _hash(root, env, name)[0]


@pytest.mark.parametrize('object_format', ['sha1', 'sha256'])
def test_a_symlink_gets_the_blob_id_git_stores_for_it(tmp_path: Path, object_format: str):
    root = _repo(tmp_path, {'target.txt': 'content\n'}, object_format=object_format)
    env = _env(tmp_path)
    (root / 'link').symlink_to('target.txt')

    (change,) = HostGit(root, env).changes()
    _git(root, env, 'add', 'link')

    stored = _git(root, env, 'ls-files', '-s', 'link').split()[1]
    assert (change.path, change.blob) == ('link', stored)


def test_a_nested_repository_cannot_be_fingerprinted_and_says_so(tmp_path: Path):
    root = _repo(tmp_path, {'keep.txt': 'k\n'})
    env = _env(tmp_path)
    (root / 'nested').mkdir()
    _git(root / 'nested', env, 'init', '-q')
    (root / 'nested' / 'f.txt').write_text('f\n', encoding='utf-8')

    with pytest.raises(GitUnavailable) as caught:
        HostGit(root, env).changes()

    assert '嵌套仓库或子模块：nested/' in str(caught.value)


def test_more_files_than_one_hash_batch_are_all_hashed(tmp_path: Path):
    root = _repo(tmp_path, {'keep.txt': 'k\n'})
    env = _env(tmp_path)
    names = [f'f{index:03}.txt' for index in range(450)]
    for index, name in enumerate(names):
        (root / name).write_text(f'{index}\n', encoding='utf-8')

    changes = HostGit(root, env).changes()

    assert [change.path for change in changes] == names
    assert [change.blob for change in changes] == _hash(root, env, *names)


def test_an_fsmonitor_configured_in_the_repository_never_runs(tmp_path: Path):
    root = _repo(tmp_path, {'keep.txt': 'k\n'})
    env = _env(tmp_path)
    marker = tmp_path / 'fsmonitor-ran'
    monitor = tmp_path / 'monitor.py'
    monitor.write_text(
        f'import pathlib\npathlib.Path({str(marker)!r}).touch()\nraise SystemExit(1)\n',
        encoding='utf-8',
    )
    script = tmp_path / 'monitor.sh'
    script.write_text(f'#!/bin/sh\nexec {sys.executable} {monitor}\n', encoding='utf-8')
    script.chmod(0o755)
    _git(root, env, 'config', 'core.fsmonitor', str(script))
    (root / 'keep.txt').write_text('changed\n', encoding='utf-8')
    subprocess.run(['git', 'status', '--porcelain'], cwd=root, env=env, capture_output=True)
    assert marker.exists()
    marker.unlink()

    HostGit(root, env).changes()

    assert not marker.exists()


def test_outside_a_repository_git_is_unavailable_with_a_next_step(tmp_path: Path):
    plain = tmp_path / 'plain'
    plain.mkdir()
    env = {**_env(tmp_path), 'GIT_CEILING_DIRECTORIES': str(tmp_path)}

    with pytest.raises(GitUnavailable) as caught:
        HostGit(plain, env).changes()

    assert 'not a git repository' in str(caught.value)
    assert '在 git 仓库里' in str(caught.value)


def test_a_missing_git_is_unavailable_with_a_next_step(tmp_path: Path):
    env = {**_env(tmp_path), 'PATH': str(tmp_path / 'empty-bin')}

    with pytest.raises(GitUnavailable) as caught:
        HostGit(tmp_path, env).run('status')

    assert '加进 PATH' in str(caught.value)
