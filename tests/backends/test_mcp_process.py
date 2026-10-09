"""外部进程组：会话关闭、关机、取消、组长先退出，孙进程都不留。"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from syndica.backends.mcp.process import ExternalProcess, ProcessSupervisor
from syndica.kernel import ConfigError, RuleRedactor

_SECRET = 'sekret-VALUE-918273'
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
