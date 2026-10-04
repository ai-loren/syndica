"""本地伪终端（M5 D7）：会话的收尾、上限、参数校验与输出的清理——契约套件之外只有本地实现
才有的部分。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from syndica.kernel.errors import ConfigError
from syndica.kernel.local_pty import _plain  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.local_workspace import LocalWorkspace


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / 'ws'
    folder.mkdir()
    return folder


def _pid_of(workspace: LocalWorkspace, handle: str) -> int:
    sessions = workspace._terminals._sessions  # pyright: ignore[reportPrivateUsage]
    return sessions[handle].process.pid


def _group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def test_close_ends_every_terminal_session_and_its_process_group(root: Path) -> None:
    workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
    portal = workspace.portal()
    handles = [await portal.pty_create('sleep 300 & sleep 300', cwd='.') for _ in range(3)]
    pids = [_pid_of(workspace, handle) for handle in handles]

    await workspace.close()

    assert [_group_alive(pid) for pid in pids] == [False, False, False]


async def test_a_ninth_session_is_refused_until_one_is_killed(root: Path) -> None:
    workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
    portal = workspace.portal()
    try:
        handles = [await portal.pty_create('sleep 300', cwd='.') for _ in range(8)]
        with pytest.raises(ConfigError) as refused:
            await portal.pty_create('sleep 300', cwd='.')
        assert 'kill' in refused.value.next_action
        await portal.pty_kill(handles[0])
        await portal.pty_kill(await portal.pty_create('true', cwd='.'))
    finally:
        await workspace.close()


@pytest.mark.parametrize(
    ('call', 'anchor'),
    [
        (lambda portal: portal.pty_create('true', cwd='.', cols=0), 'cols'),
        (lambda portal: portal.pty_create('true', cwd='.', rows=True), 'rows'),
        (lambda portal: portal.pty_create('true', cwd='../outside'), '根目录'),
    ],
    ids=['zero_columns', 'boolean_rows', 'cwd_outside_the_root'],
)
async def test_a_bad_terminal_request_says_what_to_pass(root: Path, call, anchor: str) -> None:  # type: ignore[no-untyped-def]
    workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
    try:
        with pytest.raises(ConfigError) as refused:
            await call(workspace.portal())
        message = refused.value.expected + refused.value.received + refused.value.next_action
        assert anchor in message
    finally:
        await workspace.close()


@pytest.mark.parametrize(
    ('since', 'wait', 'anchor'),
    [(-1, 0.0, 'since'), (0, -1.0, 'wait'), (0, 61.0, 'wait'), (True, 0.0, 'since')],
    ids=['negative_since', 'negative_wait', 'wait_above_the_cap', 'boolean_since'],
)
async def test_capture_rejects_offsets_and_waits_it_cannot_honour(
    root: Path, since: int, wait: float, anchor: str
) -> None:
    workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
    try:
        handle = await workspace.pty_create('sleep 30', cwd='.')
        with pytest.raises(ConfigError) as refused:
            await workspace.pty_capture(handle, since=since, wait=wait)
        assert anchor in refused.value.expected
    finally:
        await workspace.close()


@pytest.mark.parametrize(
    ('raw', 'plain'),
    [
        (b'\x1b[31mred\x1b[0m\r\n', 'red\n'),
        (b'\x1b]0;window title\x07prompt$ ', 'prompt$ '),
        (b'\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\', 'link'),
        (b'a\x08b\x07c\td', 'abc\td'),
        (b'\x1b=keypad\x1b>', 'keypad'),
        (b'\x1b(Bascii\x1b7saved\x1b8', 'asciisaved'),
    ],
    ids=[
        'colour',
        'osc_title',
        'osc_hyperlink',
        'c0_controls',
        'two_byte_escape',
        'charset_and_save',
    ],
)
def test_terminal_output_reaches_the_model_without_control_sequences(
    raw: bytes, plain: str
) -> None:
    assert _plain(raw) == plain


async def test_a_capture_after_earlier_output_still_waits_for_new_output(root: Path) -> None:
    workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
    try:
        handle = await workspace.pty_create('echo early; sleep 30', cwd='.')
        first = await workspace.pty_capture(handle, since=0, wait=2)
        for _ in range(100):
            if 'early' in first.text:
                break
            await asyncio.sleep(0.02)
            first = await workspace.pty_capture(handle, since=0)
        loop = asyncio.get_running_loop()
        started = loop.time()
        later = await workspace.pty_capture(handle, since=first.end, wait=0.5)
        waited = loop.time() - started
    finally:
        await workspace.close()

    assert later.text == ''
    assert waited >= 0.4


async def test_a_running_session_holds_back_a_split_character(root: Path) -> None:
    workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
    try:
        handle = await workspace.pty_create(
            "printf 'a\\344\\270'; while [ ! -f go ]; do sleep 0.02; done; printf '\\255b'", cwd='.'
        )
        first = await workspace.pty_capture(handle, since=0, wait=2)
        for _ in range(100):
            if first.text == 'a':
                break
            await asyncio.sleep(0.02)
            first = await workspace.pty_capture(handle, since=0)
        (root / 'go').write_text('')
        rest = await workspace.pty_capture(handle, since=first.end, wait=2)
        for _ in range(100):
            if 'b' in rest.text:
                break
            await asyncio.sleep(0.02)
            rest = await workspace.pty_capture(handle, since=first.end)
    finally:
        await workspace.close()

    assert (first.text, first.end) == ('a', 1)
    assert rest.text == '中b'
