"""外部进程组：会话关闭、关机、取消、组长先退出，孙进程都不留。"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

import pytest

from syndica.backends.mcp.process import (
    _LINE_CAP,  # pyright: ignore[reportPrivateUsage]
    _TREE_POLL_S,  # pyright: ignore[reportPrivateUsage]
    ExternalProcess,
    ProcessSupervisor,
    ProcessTree,
    StderrLines,
)
from syndica.kernel import ConfigError, RuleRedactor

_SECRET = 'sekret-VALUE-918273'
_STRADDLER = 'sk-live-QuizWaltzVex9876JUMP'
_GRACE = 0.05


def _members(pgid: int) -> set[int]:
    listing = subprocess.run(
        ['ps', '-ax', '-o', 'pid=', '-o', 'pgid='],
        check=False,
        capture_output=True,
        text=True,
    )
    found: set[int] = set()
    for line in listing.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        pid, group = int(parts[0]), int(parts[1])
        if group == pgid:
            found.add(pid)
    return found


async def _gone(pgid: int, grandchild: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if grandchild not in _members(pgid) and not _alive(grandchild):
            return
        await asyncio.sleep(0.02)
    assert grandchild not in _members(pgid)
    assert not _alive(grandchild)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _env() -> dict[str, str]:
    return {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}


_STUBBORN = """
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
    os._exit(0)
sys.stdout.write(f"{pid}\\n")
sys.stdout.flush()
time.sleep(60)
"""

_ORPHAN = """
import os, sys, time
pid = os.fork()
if pid == 0:
    time.sleep(60)
    os._exit(0)
sys.stdout.write(f"{pid}\\n")
sys.stdout.flush()
os._exit(0)
"""


async def _stubborn(tmp_path: Path) -> ExternalProcess:
    process = ExternalProcess(
        [sys.executable, '-c', _STUBBORN],
        env=_env(),
        cwd=str(tmp_path),
        redactor=RuleRedactor(),
        grace=_GRACE,
    )
    await process.start()
    return process


async def _grandchild(process: ExternalProcess) -> tuple[int, int]:
    line = await asyncio.wait_for(process.read_stdout_line(), 2)
    grandchild = int(line.strip())
    assert process._pgid is not None  # pyright: ignore[reportPrivateUsage]
    pgid = process._pgid  # pyright: ignore[reportPrivateUsage]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and grandchild not in _members(pgid):  # noqa: ASYNC110
        await asyncio.sleep(0.02)
    assert grandchild in _members(pgid)
    assert _alive(grandchild)
    return pgid, grandchild


async def test_session_close_kills_the_grandchild_and_leaves_the_sibling(tmp_path: Path) -> None:
    supervisor = ProcessSupervisor()
    closing = await supervisor.spawn(
        [sys.executable, '-c', _STUBBORN],
        env=_env(),
        cwd=str(tmp_path),
        redactor=RuleRedactor(),
        grace=_GRACE,
    )
    staying = await supervisor.spawn(
        [sys.executable, '-c', _STUBBORN],
        env=_env(),
        cwd=str(tmp_path),
        redactor=RuleRedactor(),
        grace=_GRACE,
    )
    try:
        closing_pgid, closing_child = await _grandchild(closing)
        staying_pgid, staying_child = await _grandchild(staying)
        await closing.aclose()
        await _gone(closing_pgid, closing_child)
        assert staying_child in _members(staying_pgid)
        assert _alive(staying_child)
    finally:
        await supervisor.shutdown()


async def test_shutdown_kills_every_group(tmp_path: Path) -> None:
    supervisor = ProcessSupervisor()
    first = await supervisor.spawn(
        [sys.executable, '-c', _STUBBORN],
        env=_env(),
        cwd=str(tmp_path),
        redactor=RuleRedactor(),
        grace=_GRACE,
    )
    second = await supervisor.spawn(
        [sys.executable, '-c', _STUBBORN],
        env=_env(),
        cwd=str(tmp_path),
        redactor=RuleRedactor(),
        grace=_GRACE,
    )
    groups = [await _grandchild(first), await _grandchild(second)]
    await supervisor.shutdown()
    for pgid, grandchild in groups:
        await _gone(pgid, grandchild)


async def test_cancelling_the_owner_kills_the_group(tmp_path: Path) -> None:
    process = ExternalProcess(
        [sys.executable, '-c', _STUBBORN],
        env=_env(),
        cwd=str(tmp_path),
        redactor=RuleRedactor(),
        grace=_GRACE,
    )

    async def own() -> None:
        async with process:
            await asyncio.sleep(30)

    task = asyncio.create_task(own())
    await asyncio.sleep(0.05)
    pgid, grandchild = await _grandchild(process)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _gone(pgid, grandchild)


async def test_a_leader_that_exits_first_does_not_leave_its_grandchild(tmp_path: Path) -> None:
    process = ExternalProcess(
        [sys.executable, '-c', _ORPHAN],
        env=_env(),
        cwd=str(tmp_path),
        redactor=RuleRedactor(),
        grace=_GRACE,
    )
    await process.start()
    try:
        line = await asyncio.wait_for(process.read_stdout_line(), 2)
        grandchild = int(line.strip())
        assert process._pgid is not None  # pyright: ignore[reportPrivateUsage]
        await _gone(process._pgid, grandchild)  # pyright: ignore[reportPrivateUsage]
    finally:
        await process.aclose()


async def test_stderr_is_redacted_and_the_cap_does_not_stop_the_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    script = '\n'.join(
        [
            'import signal, sys, time',
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)',
            'secret = sys.argv[1]',
            'for _ in range(20):',
            '    sys.stderr.write(secret + "\\n")',
            '    sys.stderr.flush()',
            'while True:',
            '    sys.stderr.write("x" * 100 + "\\n")',
            '    sys.stderr.flush()',
        ]
    )
    process = ExternalProcess(
        [sys.executable, '-c', script, _SECRET],
        env=_env(),
        cwd=str(tmp_path),
        redactor=RuleRedactor(secrets=[_SECRET]),
        grace=_GRACE,
        stderr_limit=64,
    )
    with caplog.at_level(logging.INFO, logger='syndica.backends.mcp.process'):
        await process.start()
        try:
            await asyncio.sleep(0.2)
            await process.aclose()
        except BaseException:
            await process.aclose()
            raise
    assert _SECRET not in caplog.text
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.args is not None
    ]
    assert warnings
    assert isinstance(warnings[0].args, tuple)
    assert warnings[0].args[1] == 64


def _fragments(secret: str, size: int = 6) -> list[str]:
    return [secret[start : start + size] for start in range(len(secret) - size + 1)]


def _stderr_texts(caplog: pytest.LogCaptureFixture) -> list[str]:
    texts: list[str] = []
    for record in caplog.records:
        if record.levelno == logging.INFO and 'stderr' in str(record.msg):
            assert isinstance(record.args, tuple)
            texts.append(str(record.args[1]))
    return texts


def _warning_args(caplog: pytest.LogCaptureFixture) -> list[tuple[object, ...]]:
    found: list[tuple[object, ...]] = []
    for record in caplog.records:
        if record.levelno == logging.WARNING and isinstance(record.args, tuple):
            found.append(record.args)
    return found


async def _until_logged(caplog: pytest.LogCaptureFixture, needle: str) -> None:
    deadline = time.monotonic() + 5
    while needle not in caplog.text and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.02)
    assert needle in caplog.text


async def _log_stderr_of(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, payload: str, needle: str, limit: int
) -> None:
    script = '\n'.join(
        [
            'import sys, time',
            f'sys.stderr.write({payload!r})',
            'sys.stderr.flush()',
            'time.sleep(30)',
        ]
    )
    process = ExternalProcess(
        [sys.executable, '-c', script],
        env=_env(),
        cwd=str(tmp_path),
        redactor=RuleRedactor(secrets=[_STRADDLER]),
        grace=_GRACE,
        stderr_limit=limit,
    )
    with caplog.at_level(logging.INFO, logger='syndica.backends.mcp.process'):
        await process.start()
        try:
            await _until_logged(caplog, needle)
        finally:
            await process.aclose()


async def test_a_secret_straddling_the_stderr_limit_leaves_no_fragment_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    payload = 'x' * 50 + _STRADDLER + '\nafter\n'
    await _log_stderr_of(tmp_path, caplog, payload, '不超过', limit=64)
    assert not [piece for piece in _fragments(_STRADDLER) if piece in caplog.text]
    texts = _stderr_texts(caplog)
    assert texts[0].startswith('x' * 50)
    assert sum(len(text.encode('utf-8')) for text in texts) <= 64
    assert [args[1] for args in _warning_args(caplog)] == [64]


async def test_a_secret_straddling_the_line_cap_leaves_no_fragment_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    head = 'word ' * (_LINE_CAP // 5 - 2)
    assert len(head) < _LINE_CAP < len(head) + len(_STRADDLER)
    payload = head + _STRADDLER + 'y' * 1000 + '\ntail line\n'
    await _log_stderr_of(tmp_path, caplog, payload, 'tail line', limit=1_048_576)
    assert not [piece for piece in _fragments(_STRADDLER) if piece in caplog.text]
    texts = _stderr_texts(caplog)
    assert texts[0].startswith('word word')
    assert texts[-1] == 'tail line'
    assert [args[1] for args in _warning_args(caplog)] == [_LINE_CAP]


def test_an_endless_line_holds_at_most_the_line_cap_in_memory() -> None:
    emitted: list[str] = []
    lines = StderrLines(
        RuleRedactor(),
        64 * 1024 * 1024,
        emit=emitted.append,
        capped=lambda: None,
        clipped=lambda cap: None,
    )
    chunk = b'x' * 4096
    tracemalloc.start()
    try:
        for _ in range(4096):
            lines.feed(chunk)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 16 * _LINE_CAP


def test_a_line_that_fits_exactly_is_not_reported_as_dropped() -> None:
    emitted: list[str] = []
    capped: list[bool] = []
    lines = StderrLines(
        RuleRedactor(),
        5,
        emit=emitted.append,
        capped=lambda: capped.append(True),
        clipped=lambda cap: None,
    )
    lines.feed(b'hello\n')
    lines.close()
    assert emitted == ['hello']
    assert capped == []
    lines.feed(b'more\n')
    assert emitted == ['hello']
    assert capped == [True]


_ROOT = """
import os, subprocess, sys
quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
plain = subprocess.Popen(['/bin/sleep', '300'], **quiet)
detached = subprocess.Popen(['/bin/sleep', '300'], start_new_session=True, **quiet)
sys.stdout.write(f'{plain.pid} {detached.pid}\\n')
sys.stdout.flush()
sys.stdin.readline()
os._exit(3)
"""


def _running(pid: int) -> bool:
    state = subprocess.run(
        ['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(state) and not state.startswith('Z')


def _kill_all(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            continue


async def _root(tmp_path: Path) -> tuple[asyncio.subprocess.Process, ProcessTree, list[int]]:
    root = await asyncio.create_subprocess_exec(
        sys.executable,
        '-c',
        _ROOT,
        cwd=str(tmp_path),
        env=_env(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    tree = ProcessTree(root.pid, grace=_GRACE)
    tree.start()
    assert root.stdout is not None
    line = await asyncio.wait_for(root.stdout.readline(), 5)
    pids = [int(part) for part in line.split()]
    assert all(_running(pid) for pid in pids)
    return root, tree, pids


async def _exit_root(root: asyncio.subprocess.Process) -> None:
    assert root.stdin is not None
    root.stdin.close()
    await asyncio.wait_for(root.wait(), 5)


async def _clean_up(
    roots: list[asyncio.subprocess.Process], trees: list[ProcessTree], pids: list[int]
) -> None:
    _kill_all(pids)
    for tree in trees:
        tree.stop()
    for root in roots:
        if root.returncode is None:
            root.kill()
            await root.wait()


async def test_reap_kills_a_setsid_grandchild_and_a_group_member_after_the_root_exited(
    tmp_path: Path,
) -> None:
    root, tree, pids = await _root(tmp_path)
    try:
        tree.stop()
        tree.record()
        await _exit_root(root)
        assert all(_running(pid) for pid in pids)
        await tree.reap()
        assert not [pid for pid in pids if _running(pid)]
    finally:
        await _clean_up([root], [tree], pids)


async def test_the_watcher_reaps_a_crashed_roots_setsid_grandchild_before_anyone_closes(
    tmp_path: Path,
) -> None:
    root, tree, pids = await _root(tmp_path)
    try:
        await asyncio.sleep(2 * _TREE_POLL_S)
        await _exit_root(root)
        deadline = time.monotonic() + 2 * _TREE_POLL_S + 2
        while [pid for pid in pids if _running(pid)] and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.02)
        assert not [pid for pid in pids if _running(pid)]
    finally:
        await _clean_up([root], [tree], pids)


async def test_reaping_one_tree_leaves_a_sibling_trees_descendants_running(
    tmp_path: Path,
) -> None:
    closing_root, closing, closing_pids = await _root(tmp_path)
    staying_root, staying, staying_pids = await _root(tmp_path)
    try:
        closing.stop()
        closing.record()
        await _exit_root(closing_root)
        await closing.reap()
        assert not [pid for pid in closing_pids if _running(pid)]
        assert all(_running(pid) for pid in staying_pids)
    finally:
        await _clean_up(
            [closing_root, staying_root], [closing, staying], closing_pids + staying_pids
        )


async def test_the_child_does_not_inherit_the_host_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('SUPER_SECRET', _SECRET)
    process = ExternalProcess(
        [
            sys.executable,
            '-c',
            'import os, sys; sys.stdout.write(os.environ.get("SUPER_SECRET", "") + "\\n")',
        ],
        env=_env(),
        cwd=str(tmp_path),
        redactor=RuleRedactor(),
        grace=_GRACE,
    )
    await process.start()
    try:
        line = await asyncio.wait_for(process.read_stdout_line(), 2)
    finally:
        await process.aclose()
    assert line.strip() == ''
    assert _SECRET not in line


@pytest.mark.parametrize(
    ('argv', 'grace', 'limit', 'anchor'),
    [
        ('echo hi', _GRACE, 64, '不经 shell'),
        ([sys.executable, ''], _GRACE, 64, '非空字符串'),
        ([sys.executable], True, 64, 'grace'),
        ([sys.executable], _GRACE, True, 'stderr_limit'),
        ([sys.executable], _GRACE, 0, 'stderr_limit'),
    ],
)
async def test_spawn_rejects_the_wrong_type_family(
    tmp_path: Path, argv: object, grace: object, limit: object, anchor: str
) -> None:
    with pytest.raises(ConfigError, match=anchor):
        ExternalProcess(
            argv,  # pyright: ignore[reportArgumentType]
            env=_env(),
            cwd=str(tmp_path),
            redactor=RuleRedactor(),
            grace=grace,  # pyright: ignore[reportArgumentType]
            stderr_limit=limit,  # pyright: ignore[reportArgumentType]
        )
