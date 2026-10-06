"""本地工作区的路径监狱：越界在解析之后判定，相对根不查当前目录。"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from syndica.kernel.errors import ConfigError
from syndica.kernel.local_workspace import (
    LocalWorkspace,
    _Capture,  # pyright: ignore[reportPrivateUsage]
)
from syndica.kernel.terminal_text import utf8_prefix_length
from syndica.kernel.workspace import ExecResult


def _workspace(root: Path) -> LocalWorkspace:
    return LocalWorkspace(str(root), shell='/bin/sh', env={})


def test_relative_root_is_rejected_without_calling_getcwd(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> str:
        raise AssertionError('getcwd')

    monkeypatch.setattr('os.getcwd', boom)
    with pytest.raises(ConfigError) as caught:
        LocalWorkspace('relative-dir', shell='/bin/sh', env={})
    assert '传入绝对路径' in caught.value.next_action


async def test_symlink_escaping_the_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    secret = tmp_path / 'secret.txt'
    secret.write_text('secret', encoding='utf-8')
    (root / 'link.txt').symlink_to(secret)
    workspace = _workspace(root)
    with pytest.raises(ConfigError):
        await workspace.read_text('link.txt', offset=1, limit=10)
    with pytest.raises(ConfigError):
        await workspace.write_text('link.txt', 'pwned')
    assert secret.read_text(encoding='utf-8') == 'secret'


async def test_sibling_prefix_is_not_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    sibling = tmp_path / 'ws-evil'
    sibling.mkdir()
    secret = sibling / 'secret.txt'
    secret.write_text('nope', encoding='utf-8')
    workspace = _workspace(root)
    with pytest.raises(ConfigError):
        await workspace.read_text(str(secret), offset=1, limit=10)


async def test_dotdot_is_rejected_after_resolve(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    (root / 'sub').mkdir(parents=True)
    secret = tmp_path / 'secret.txt'
    secret.write_text('nope', encoding='utf-8')
    workspace = _workspace(root)
    with pytest.raises(ConfigError):
        await workspace.read_text('sub/../../secret.txt', offset=1, limit=10)


async def test_write_then_read_roundtrips_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    workspace = _workspace(root)
    await workspace.write_text('notes/a.txt', 'hello')
    assert await workspace.read_text('notes/a.txt', offset=1, limit=10) == 'hello'
    assert (root / 'notes' / 'a.txt').read_text(encoding='utf-8') == 'hello'


def test_shell_must_be_an_executable_absolute_path(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    script = tmp_path / 'not-a-shell'
    script.write_text('nope', encoding='utf-8')
    script.chmod(0o644)
    with pytest.raises(ConfigError) as relative:
        LocalWorkspace(str(root), shell='sh', env={})
    assert '传入绝对路径' in relative.value.next_action
    with pytest.raises(ConfigError):
        LocalWorkspace(str(root), shell=str(script), env={})


def test_caller_env_mutation_does_not_change_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    env = {'A': '1'}
    workspace = LocalWorkspace(str(root), shell='/bin/sh', env=env)
    env['A'] = '2'
    assert workspace._env == {'A': '1'}
    assert workspace._shell == '/bin/sh'


def test_source_does_not_read_process_cwd_or_environ() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ('local_workspace.py', 'workspace.py'):
        source = (root / 'src' / 'syndica' / 'kernel' / name).read_text(encoding='utf-8')
        assert 'getcwd' not in source
        assert 'environ' not in source


async def test_absolute_path_inside_the_root_roundtrips(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    workspace = _workspace(root)
    target = root / 'b.txt'
    await workspace.write_text(str(target), 'abs')
    assert await workspace.read_text(str(target), offset=1, limit=5) == 'abs'


async def test_close_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    workspace = _workspace(root)
    await workspace.close()
    await workspace.close()


async def test_exec_methods_run_poll_and_kill_inside_the_workspace(
    tmp_path: Path, shell_path: str
) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    workspace = LocalWorkspace(str(root), shell=shell_path, env={})
    foreground = await workspace.run('printf direct', timeout=1, cwd='.')
    assert foreground.stdout == 'direct'
    assert foreground.running is False

    handle = await workspace.start('sleep 5', cwd='.')
    assert (await workspace.poll(handle)).running is True
    started = time.monotonic()
    await workspace.kill(handle)
    assert time.monotonic() - started < 2
    assert (await workspace.poll(handle)).running is False
    await workspace.close()


# --- 命令执行：stdin、进程组、有上限的等待、超时 --------------------------------------------------


@pytest.fixture
def exec_root(tmp_path: Path) -> Path:
    root = tmp_path / 'ws'
    root.mkdir()
    return root


def test_exec_result_only_appends_fields_and_defaults_them() -> None:
    assert [field.name for field in dataclasses.fields(ExecResult)] == [
        'exit_code',
        'stdout',
        'stderr',
        'truncated',
        'running',
        'timed_out',
        'stdout_end',
        'stderr_end',
    ]
    legacy = ExecResult(exit_code=0, stdout='', stderr='', truncated=False, running=False)
    assert (legacy.timed_out, legacy.stdout_end, legacy.stderr_end) == (False, 0, 0)


async def test_a_command_reading_stdin_gets_eof_at_once_even_when_the_host_stdin_stays_open(
    exec_root: Path, shell_path: str
) -> None:
    read_end, write_end = os.pipe()
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
        started = time.monotonic()
        result = await workspace.run('cat', timeout=3, cwd='.')
        elapsed = time.monotonic() - started
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_end, write_end):
            os.close(fd)

    assert elapsed < 1
    assert (result.exit_code, result.stdout, result.timed_out) == (0, '', False)


async def test_a_background_child_holding_the_pipes_cannot_stretch_a_short_command(
    exec_root: Path, shell_path: str
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})

    started = time.monotonic()
    result = await workspace.run('sleep 4 &', timeout=1, cwd='.')
    elapsed = time.monotonic() - started

    assert elapsed < 2.5
    assert (result.exit_code, result.timed_out) == (0, False)


async def test_a_child_that_finishes_within_the_grace_still_gets_its_output_in(
    exec_root: Path, shell_path: str
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})

    result = await workspace.run('(sleep 0.3; printf late) &', timeout=5, cwd='.')

    assert result.stdout == 'late'


async def test_what_a_foreground_command_leaves_in_its_group_is_killed_after_the_grace(
    exec_root: Path, shell_path: str
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})

    await workspace.run('(sleep 1.6; touch leaked) &', timeout=5, cwd='.')
    await asyncio.sleep(1.2)

    assert not (exec_root / 'leaked').exists()


async def test_a_timeout_returns_the_partial_output_instead_of_raising(
    exec_root: Path, shell_path: str
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})

    started = time.monotonic()
    result = await workspace.run('printf before; printf warn >&2; sleep 5', timeout=0.5, cwd='.')

    assert time.monotonic() - started < 2
    assert result == ExecResult(
        exit_code=None,
        stdout='before',
        stderr='warn',
        truncated=False,
        running=False,
        timed_out=True,
        stdout_end=len(b'before'),
        stderr_end=len(b'warn'),
    )


async def test_a_timeout_kills_the_whole_group_not_just_the_shell(
    exec_root: Path, shell_path: str
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})

    await workspace.run('(sleep 1.2; touch late) & sleep 5', timeout=0.3, cwd='.')
    await asyncio.sleep(1.4)

    assert not (exec_root / 'late').exists()


async def test_killing_a_background_command_whose_shell_already_exited_kills_its_children(
    exec_root: Path, shell_path: str
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})

    handle = await workspace.start('(sleep 1.6; touch orphan) &', cwd='.')
    await asyncio.sleep(0.2)
    await workspace.kill(handle)
    await asyncio.sleep(1.8)

    assert not (exec_root / 'orphan').exists()


async def test_close_kills_an_inflight_foreground_command_and_background_leftovers(
    exec_root: Path, shell_path: str
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
    running = asyncio.create_task(workspace.run('sleep 1; touch fg', timeout=10, cwd='.'))
    await workspace.start('(sleep 1; touch bg-child) & sleep 5', cwd='.')
    await asyncio.sleep(0.3)

    started = time.monotonic()
    await workspace.close()
    result = await running
    closing_took = time.monotonic() - started
    await asyncio.sleep(1.2)

    assert closing_took < 2
    assert result.exit_code is not None and result.exit_code < 0
    assert not (exec_root / 'fg').exists()
    assert not (exec_root / 'bg-child').exists()


async def test_a_process_that_escaped_the_group_cannot_hold_the_call_or_our_pipes(
    exec_root: Path, shell_path: str
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
    escape = f'{sys.executable} -c "import os, time; os.setsid(); time.sleep(4)" &'
    await workspace.run('true', timeout=5, cwd='.')
    await asyncio.sleep(0.1)
    open_before = len(os.listdir('/dev/fd'))

    started = time.monotonic()
    result = await workspace.run(escape, timeout=5, cwd='.')
    elapsed = time.monotonic() - started
    await asyncio.sleep(0.1)

    assert elapsed < 3.5
    assert result.timed_out is False
    assert len(os.listdir('/dev/fd')) == open_before


async def test_cancelling_a_foreground_run_kills_its_group_at_once(
    exec_root: Path, shell_path: str
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
    running = asyncio.create_task(workspace.run('sleep 1; touch leaked', timeout=10, cwd='.'))
    await asyncio.sleep(0.3)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await asyncio.sleep(1.2)

    assert not (exec_root / 'leaked').exists()


async def test_output_is_collected_across_many_reads(exec_root: Path, shell_path: str) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})

    result = await workspace.run(
        'printf a; sleep 0.2; printf b; sleep 0.2; printf c', timeout=5, cwd='.'
    )

    assert result.stdout == 'abc'


async def test_the_command_sees_exactly_the_given_env_and_cwd_under_the_given_shell(
    exec_root: Path, shell_path: str
) -> None:
    (exec_root / 'sub').mkdir()
    fake = exec_root / 'fake-shell'
    fake.write_text(f'#!{shell_path}\nprintf "fake:%s" "$2"\n', encoding='utf-8')
    fake.chmod(0o755)
    real = LocalWorkspace(str(exec_root), shell=shell_path, env={'SYNDICA_PROBE': 'given'})
    faked = LocalWorkspace(str(exec_root), shell=str(fake), env={})

    seen = await real.run('printf "%s|%s|" "$SYNDICA_PROBE" "$HOME"; pwd', timeout=5, cwd='sub')
    via_fake = await faked.run('hello', timeout=5, cwd='.')

    assert seen.stdout.startswith('given||')
    assert seen.stdout.rstrip('\n').endswith('/sub')
    assert via_fake.stdout == 'fake:hello'


async def test_bytes_that_are_not_utf8_are_replaced_rather_than_crashing(
    exec_root: Path, shell_path: str
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})

    result = await workspace.run("printf 'x\\377y'", timeout=5, cwd='.')

    assert result.stdout == 'x\ufffdy'


async def test_running_after_close_is_refused(exec_root: Path, shell_path: str) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
    await workspace.close()

    with pytest.raises(ConfigError) as caught:
        await workspace.run('touch after-close', timeout=5, cwd='.')

    assert '关闭' in caught.value.received
    assert not (exec_root / 'after-close').exists()


async def test_polling_while_the_shell_exited_but_its_children_linger_reports_running(
    exec_root: Path, shell_path: str
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
    handle = await workspace.start('sleep 3 &', cwd='.')
    await asyncio.sleep(0.3)

    polled = await workspace.poll(handle)
    await workspace.close()

    assert (polled.running, polled.exit_code) == (True, None)


async def test_a_run_cancelled_while_its_shell_is_being_spawned_leaves_no_group_behind(
    exec_root: Path, shell_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
    real_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    release = asyncio.Event()

    async def slow_spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await real_spawn(*args, **kwargs)
        spawned.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', slow_spawn)
    running = asyncio.create_task(workspace.run('sleep 0.6; touch leaked', timeout=10, cwd='.'))
    await spawned.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    release.set()
    await asyncio.sleep(1.0)

    assert not (exec_root / 'leaked').exists()


async def test_cancelling_the_settle_task_kills_the_group(exec_root: Path, shell_path: str) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
    handle = await workspace.start('sleep 0.6; touch leaked', cwd='.')
    await asyncio.sleep(0.2)

    workspace._processes[handle].settled.cancel()
    await asyncio.sleep(0.8)

    assert not (exec_root / 'leaked').exists()


async def test_a_finished_command_is_never_signalled_again(
    exec_root: Path, shell_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
    handle = await workspace.start('true', cwd='.')
    await workspace.run('true', timeout=5, cwd='.')
    for _ in range(40):
        if (await workspace.poll(handle)).running is False:
            break
        await asyncio.sleep(0.05)
    signalled: list[int] = []
    monkeypatch.setattr(os, 'killpg', lambda pgid, _signal: signalled.append(pgid))

    await workspace.kill(handle)
    await workspace.close()

    assert signalled == []


async def test_a_stream_past_the_capture_budget_keeps_its_head_and_tail(
    exec_root: Path, shell_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr('syndica.kernel.local_workspace._CAPTURE_HEAD', 10)
    monkeypatch.setattr('syndica.kernel.local_workspace._CAPTURE_TAIL', 40)
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
    body = 'H' * 10 + 'm' * 150 + 'T' * 40

    finished = await workspace.run(f'printf {body}', timeout=5, cwd='.')
    handle = await workspace.start(f'printf {body}; sleep 5', cwd='.')
    await asyncio.sleep(0.3)
    running = await workspace.poll(handle)
    await workspace.close()

    for result in (finished, running):
        assert result.truncated is True
        assert result.stdout.startswith('H' * 10 + '\n…[')
        assert result.stdout.endswith(']…\n' + 'T' * 40)
        assert '150 个字节' in result.stdout


@pytest.mark.parametrize(
    ('since', 'shown', 'truncated'),
    [
        (4, 'H' * 6 + '\n…[内核丢弃了这条流中间 150 个字节', True),
        (60, '\n…[内核丢弃了这条流中间 100 个字节', True),
        (175, 'T' * 25, False),
        (200, '', False),
    ],
    ids=['from_the_head', 'from_the_dropped_middle', 'from_the_tail', 'from_the_end'],
)
def test_reading_since_an_offset_counts_the_dropped_middle_in_the_offsets(
    monkeypatch: pytest.MonkeyPatch, since: int, shown: str, truncated: bool
) -> None:
    monkeypatch.setattr('syndica.kernel.local_workspace._CAPTURE_HEAD', 10)
    monkeypatch.setattr('syndica.kernel.local_workspace._CAPTURE_TAIL', 40)
    capture = _Capture()
    capture.extend(b'H' * 10 + b'm' * 150 + b'T' * 40)

    text, dropped, end = capture.rendered(since, settled=True)

    assert text.startswith(shown) if shown else text == ''
    assert (dropped, end) == (truncated, 200)
    if since < 160:
        assert text.endswith(']…\n' + 'T' * 40)


@pytest.mark.parametrize(
    ('data', 'complete'),
    [
        (b'ab', 2),
        ('aé'.encode(), 3),
        ('aé'.encode()[:2], 1),
        ('a中'.encode()[:3], 1),
        ('a😀'.encode()[:4], 1),
        ('a😀'.encode(), 5),
        (b'', 0),
    ],
    ids=[
        'ascii',
        'two_byte_whole',
        'two_byte_cut',
        'three_byte_cut',
        'four_byte_cut',
        'four_byte',
        'empty',
    ],
)
def test_a_running_stream_holds_back_a_character_cut_in_half(data: bytes, complete: int) -> None:
    assert utf8_prefix_length(data) == complete


async def test_a_running_command_hands_over_a_split_character_whole_on_the_next_poll(
    exec_root: Path, shell_path: str
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
    handle = await workspace.start(
        "printf 'a\\344\\270'; while [ ! -f go ]; do sleep 0.02; done; printf '\\255b'", cwd='.'
    )
    first = await workspace.poll(handle)
    for _ in range(250):
        if first.stdout == 'a':
            break
        await asyncio.sleep(0.02)
        first = await workspace.poll(handle)
    (exec_root / 'go').write_text('')
    for _ in range(250):
        if not (await workspace.poll(handle)).running:
            break
        await asyncio.sleep(0.02)
    rest = await workspace.poll(handle, since=(first.stdout_end, first.stderr_end))
    await workspace.close()

    assert (first.stdout, first.stdout_end) == ('a', 1)
    assert rest.stdout == '中b'


async def test_running_out_of_descriptors_is_a_config_error_without_leaking_the_first_pipe(
    exec_root: Path, shell_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
    await workspace.run('true', timeout=5, cwd='.')
    await asyncio.sleep(0.1)
    open_before = len(os.listdir('/dev/fd'))
    real_pipe = os.pipe
    calls: list[int] = []

    def second_pipe_fails() -> tuple[int, int]:
        calls.append(1)
        if len(calls) % 2 == 0:
            raise OSError(24, 'Too many open files')
        return real_pipe()

    monkeypatch.setattr(os, 'pipe', second_pipe_fails)
    with pytest.raises(ConfigError) as caught:
        await workspace.run('true', timeout=5, cwd='.')
    monkeypatch.setattr(os, 'pipe', real_pipe)

    assert 'OSError' in caught.value.received
    assert 'KillBash' in caught.value.next_action
    assert len(os.listdir('/dev/fd')) == open_before


async def test_a_running_background_command_reports_what_it_printed_so_far(
    exec_root: Path, shell_path: str
) -> None:
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
    handle = await workspace.start('printf early; sleep 5', cwd='.')
    await asyncio.sleep(0.3)

    polled = await workspace.poll(handle)
    await workspace.close()

    assert (polled.running, polled.stdout, polled.exit_code) == (True, 'early', None)


# --- 拒写 .git -----------------------------------------------------------------------------------


@pytest.mark.parametrize('target', ['.git/config', '.GIT/config', '.git/hooks/x', './.Git/HEAD'])
async def test_writing_or_editing_inside_dot_git_is_refused_with_the_reason(
    exec_root: Path, target: str
) -> None:
    (exec_root / '.git' / 'hooks').mkdir(parents=True)
    (exec_root / '.git' / 'config').write_text('[core]\n', encoding='utf-8')
    (exec_root / '.git' / 'HEAD').write_text('ref: x\n', encoding='utf-8')
    workspace = _workspace(exec_root)

    with pytest.raises(ConfigError) as written:
        await workspace.write_text(target, 'pwned')
    with pytest.raises(ConfigError) as edited:
        await workspace.edit_text('.git/config', '[core]', '[pwned]')

    assert '.git' in written.value.expected
    assert '宿主' in written.value.next_action
    assert '.git' in edited.value.received
    assert (exec_root / '.git' / 'config').read_text(encoding='utf-8') == '[core]\n'
    assert not (exec_root / '.git' / 'hooks' / 'x').exists()


async def test_a_worktree_dot_git_file_is_refused_too(exec_root: Path) -> None:
    (exec_root / '.git').write_text('gitdir: /elsewhere\n', encoding='utf-8')
    workspace = _workspace(exec_root)

    with pytest.raises(ConfigError):
        await workspace.write_text('.git', 'gitdir: /evil\n')
    with pytest.raises(ConfigError):
        await workspace.edit_text('.git', '/elsewhere', '/evil')

    assert (exec_root / '.git').read_text(encoding='utf-8') == 'gitdir: /elsewhere\n'


async def test_a_symlink_into_dot_git_and_a_symlinked_dot_git_are_refused(exec_root: Path) -> None:
    (exec_root / '.git' / 'hooks').mkdir(parents=True)
    (exec_root / 'hooks').symlink_to(exec_root / '.git' / 'hooks')
    other = exec_root / 'other'
    (other / 'gitdir').mkdir(parents=True)
    (other / '.git').symlink_to(other / 'gitdir')
    workspace = _workspace(exec_root)

    with pytest.raises(ConfigError):
        await workspace.write_text('hooks/pre-commit', 'pwned')
    with pytest.raises(ConfigError):
        await workspace.write_text('other/.git/config', 'pwned')
    with pytest.raises(ConfigError):
        await workspace.write_text(str(other / '.git' / 'config'), 'pwned')
    with pytest.raises(ConfigError):
        await workspace.write_text(str(exec_root / '.git' / 'x'), 'pwned')

    assert not (exec_root / '.git' / 'hooks' / 'pre-commit').exists()
    assert not (other / 'gitdir' / 'config').exists()


@pytest.mark.parametrize(
    'target', ['.gitignore', '.gitattributes', '.github/workflows/ci.yml', 'src/.gitkeep', 'git/x']
)
async def test_a_neighbour_of_dot_git_is_still_writable(exec_root: Path, target: str) -> None:
    workspace = _workspace(exec_root)

    await workspace.write_text(target, 'ok\n')
    await workspace.edit_text(target, 'ok', 'fine')

    assert (exec_root / target).read_text(encoding='utf-8') == 'fine\n'


def _planted_git_dirs(root: Path) -> None:
    (root / '.git' / 'hooks').mkdir(parents=True)
    (root / '.GIT').mkdir(exist_ok=True)
    (root / 'src').mkdir()
    (root / 'hooks').symlink_to(root / '.git' / 'hooks')
    (root / 'other' / 'gitdir').mkdir(parents=True)
    (root / 'other' / '.git').symlink_to(root / 'other' / 'gitdir')
    (root / 'sub' / '.git').mkdir(parents=True)


_GIT_CWDS = ['.git', '.git/hooks', '.GIT', 'src/../.git', 'hooks', 'other/.git', 'sub/.git']


def _markers(root: Path) -> list[Path]:
    return list(root.rglob('marker'))


@pytest.mark.parametrize('cwd', _GIT_CWDS)
async def test_a_command_cannot_run_inside_dot_git(
    exec_root: Path, shell_path: str, cwd: str
) -> None:
    _planted_git_dirs(exec_root)
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
    try:
        with pytest.raises(ConfigError) as ran:
            await workspace.run('printf x > marker', timeout=5, cwd=cwd)
        with pytest.raises(ConfigError) as started:
            await workspace.start('printf x > marker', cwd=cwd)
    finally:
        await workspace.close()

    assert f'cwd={cwd!r}' in ran.value.received
    assert 'git status' in ran.value.next_action
    assert '.git' in started.value.received
    assert _markers(exec_root) == []


async def test_the_absolute_path_of_dot_git_is_refused_as_a_cwd_too(
    exec_root: Path, shell_path: str
) -> None:
    _planted_git_dirs(exec_root)
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
    try:
        with pytest.raises(ConfigError):
            await workspace.run('printf x > marker', timeout=5, cwd=str(exec_root / '.git'))
    finally:
        await workspace.close()

    assert _markers(exec_root) == []


@pytest.mark.parametrize('cwd', ['.github', 'src/.gitkeep.d', 'git', 'other/gitdir-neighbour'])
async def test_a_command_still_runs_next_to_dot_git(
    exec_root: Path, shell_path: str, cwd: str
) -> None:
    (exec_root / '.git').mkdir()
    (exec_root / cwd).mkdir(parents=True)
    workspace = LocalWorkspace(str(exec_root), shell=shell_path, env={})
    try:
        result = await workspace.run('printf x > marker', timeout=5, cwd=cwd)
    finally:
        await workspace.close()

    assert result.exit_code == 0
    assert (exec_root / cwd / 'marker').read_text(encoding='utf-8') == 'x'


async def test_the_executable_bit_is_not_changed_through_a_symlink(tmp_path: Path) -> None:
    # 符号链接没有自己的可执行位：顺着它 chmod 改的是另一个名字下的文件。
    root = tmp_path / 'ws'
    root.mkdir()
    (root / 'real.sh').write_text('echo hi\n')
    (root / 'link.sh').symlink_to(root / 'real.sh')
    with pytest.raises(ConfigError):
        await _workspace(root).portal().set_executable('link.sh', True)
    assert not (root / 'real.sh').stat().st_mode & 0o111


async def test_setting_the_executable_bit_follows_the_read_bits(tmp_path: Path) -> None:
    # 与默认 umask 下的 `chmod +x` 一样：读得到的那几类都加上执行位（变异 M18）。
    root = tmp_path / 'ws'
    root.mkdir()
    script = root / 'run.sh'
    script.write_text('echo hi\n')
    os.chmod(script, 0o644)
    await _workspace(root).portal().set_executable('run.sh', True)
    assert script.stat().st_mode & 0o777 == 0o755
