"""Portal 契约。任何 Workspace 实现交出的 Portal 都必须通过。

「可替换」若无测试证明即是空话（spec §13.3）。本套件随发行版发布，第三方写远程工作区时继承它并
提供 `workspace` fixture 即可验证合规。命令用例假定 POSIX shell。

**运行前提**见 `syndica.kernel.contracts.base`。

**带版本前提的写必须跨过「读然后写」那次必然的让出仍然恰好一个赢家。** 远程实现在取版本与写入
之间必有一次往返；本地实现把两者放在同一段不让出的代码里就能过，一个在中间让出的实现会让两个
Agent 都以为自己写成了，后写的那个静默盖掉先写的。基数取 3：n=2 下有退化解。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from collections.abc import Awaitable, Callable

from syndica.kernel.contracts.base import ContractSuite
from syndica.kernel.errors import SyndicaError
from syndica.kernel.workspace import (
    ExecResult,
    Portal,
    PtyCapture,
    PtyStatus,
    Workspace,
    WriteConflict,
)

__all__ = ['WorkspaceContract']

# 等后台命令产出第一段输出的上限。命令自己不睡，只等一个文件出现，所以这只是兜底。
_SETTLE_SECONDS = 10.0


def _digest(data: bytes) -> str:
    return f'sha256:{hashlib.sha256(data).hexdigest()}'


async def _rejects(action: str, call: Callable[[], Awaitable[object]]) -> None:
    try:
        await call()
    except SyndicaError:
        return
    raise AssertionError(
        f'期望 {action} 抛出 SyndicaError，实际调用成功返回。'
        ' 让该实现在这条路径上抛 SyndicaError 子类，调用方才拿得到可自助修复的三件套。'
    )


async def _conflict(action: str, call: Callable[[], Awaitable[object]]) -> WriteConflict:
    try:
        await call()
    except WriteConflict as conflict:
        return conflict
    raise AssertionError(
        f'期望 {action} 抛出 WriteConflict，实际调用成功返回。'
        ' 带版本前提的写在版本对不上时必须拒绝：静默写成就是后写方盖掉了别人的修改。'
    )


async def _until(portal: Portal, handle: str, done: Callable[[ExecResult], bool]) -> ExecResult:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SETTLE_SECONDS
    while True:
        result = await portal.poll(handle)
        if done(result):
            return result
        if loop.time() > deadline:
            raise AssertionError(
                f'期望后台命令在 {_SETTLE_SECONDS} 秒内到达预期状态，'
                f'实际最后一次查询是 {result!r}。'
                ' 检查这个实现的 poll 是否交回到目前为止收到的输出。'
            )
        await asyncio.sleep(0.02)


class WorkspaceContract(ContractSuite):
    """子类必须提供一个 `workspace` fixture：根是一个空目录，用例结束后由 fixture 关闭它。"""

    async def test_written_text_reads_back_and_stat_reports_its_digest(
        self, workspace: Workspace
    ) -> None:
        portal = workspace.portal()
        await portal.write_text('notes/a.txt', 'alpha\nbravo')
        assert await portal.read_text('notes/a.txt', offset=1, limit=10) == 'alpha\nbravo'
        stat = await portal.stat('notes/a.txt')
        assert stat is not None
        assert (stat.version, stat.size) == (_digest(b'alpha\nbravo'), len(b'alpha\nbravo'))

    async def test_a_missing_file_has_no_stat(self, workspace: Workspace) -> None:
        assert await workspace.portal().stat('absent.txt') is None

    async def test_the_version_follows_the_content_not_the_write(
        self, workspace: Workspace
    ) -> None:
        portal = workspace.portal()
        await portal.write_text('a.txt', 'same')
        first = await portal.stat('a.txt')
        await portal.write_text('a.txt', 'same')
        again = await portal.stat('a.txt')
        await portal.write_text('a.txt', 'other')
        changed = await portal.stat('a.txt')
        assert first is not None and again is not None and changed is not None
        assert first.version == again.version != changed.version

    async def test_expecting_absence_creates_once_and_then_conflicts(
        self, workspace: Workspace
    ) -> None:
        portal = workspace.portal()
        await portal.write_text('new.txt', 'first', expected=None)
        created = await portal.stat('new.txt')
        assert created is not None
        conflict = await _conflict(
            'write_text(expected=None) 写一个已存在的文件',
            lambda: portal.write_text('new.txt', 'second', expected=None),
        )
        assert (conflict.path, conflict.expected, conflict.current) == (
            'new.txt',
            None,
            created.version,
        )
        assert await portal.read_text('new.txt', offset=1, limit=5) == 'first'

    async def test_a_stale_version_is_a_conflict_and_leaves_the_file_alone(
        self, workspace: Workspace
    ) -> None:
        portal = workspace.portal()
        await portal.write_text('a.txt', 'one')
        one = await portal.stat('a.txt')
        assert one is not None
        await portal.write_text('a.txt', 'two', expected=one.version)
        two = await portal.stat('a.txt')
        assert two is not None
        conflict = await _conflict(
            'write_text 拿着过期的版本',
            lambda: portal.write_text('a.txt', 'three', expected=one.version),
        )
        assert (conflict.expected, conflict.current) == (one.version, two.version)
        assert await portal.read_text('a.txt', offset=1, limit=5) == 'two'

    async def test_three_concurrent_writes_on_one_version_have_one_winner(
        self, workspace: Workspace
    ) -> None:
        portal = workspace.portal()
        await portal.write_text('shared.txt', 'base')
        base = await portal.stat('shared.txt')
        assert base is not None
        contents = ['delta', 'alpha', 'charlie']
        outcomes = await asyncio.gather(
            portal.write_text('shared.txt', 'delta', expected=base.version),
            portal.write_text('shared.txt', 'alpha', expected=base.version),
            portal.write_text('shared.txt', 'charlie', expected=base.version),
            return_exceptions=True,
        )
        winners = [
            text for text, outcome in zip(contents, outcomes, strict=True) if outcome is None
        ]
        losers = [outcome for outcome in outcomes if isinstance(outcome, WriteConflict)]
        assert len(winners) == 1, f'期望恰好一个写入成功，实际 {outcomes!r}'
        assert len(losers) == 2, f'期望另外两个收到 WriteConflict，实际 {outcomes!r}'
        assert await portal.read_text('shared.txt', offset=1, limit=5) == winners[0]

    async def test_an_edit_on_a_stale_version_is_a_conflict(self, workspace: Workspace) -> None:
        portal = workspace.portal()
        await portal.write_text('a.txt', 'alpha bravo')
        before = await portal.stat('a.txt')
        assert before is not None
        await portal.edit_text('a.txt', 'alpha', 'ALPHA', expected=before.version)
        await _conflict(
            'edit_text 拿着过期的版本',
            lambda: portal.edit_text('a.txt', 'bravo', 'BRAVO', expected=before.version),
        )
        assert await portal.read_text('a.txt', offset=1, limit=5) == 'ALPHA bravo'

    async def test_bytes_round_trip_and_carry_the_same_kind_of_version(
        self, workspace: Workspace
    ) -> None:
        portal = workspace.portal()
        data = b'\xff\x00 not utf-8 \xfe'
        await portal.write_bytes('blob.bin', b'\xff\x00 not utf-8 \xfe', expected=None)
        assert await portal.read_bytes('blob.bin') == data
        stat = await portal.stat('blob.bin')
        assert stat is not None
        assert stat.version == _digest(data)

    async def test_remove_honours_the_version_precondition(self, workspace: Workspace) -> None:
        portal = workspace.portal()
        await portal.write_text('a.txt', 'one')
        one = await portal.stat('a.txt')
        assert one is not None
        await portal.write_text('a.txt', 'two')
        await _conflict(
            'remove 拿着过期的版本', lambda: portal.remove('a.txt', expected=one.version)
        )
        two = await portal.stat('a.txt')
        assert two is not None
        await portal.remove('a.txt', expected=two.version)
        assert await portal.stat('a.txt') is None

    async def test_paths_outside_the_root_are_refused(self, workspace: Workspace) -> None:
        portal = workspace.portal()
        outside = '../outside-the-root.txt'
        await _rejects('越界的 read_text', lambda: portal.read_text(outside, offset=1, limit=1))
        await _rejects(
            '越界的 write_text', lambda: portal.write_text('../outside-the-root.txt', 'x')
        )
        await _rejects(
            '越界的 write_bytes', lambda: portal.write_bytes('../outside-the-root.txt', b'x')
        )
        await _rejects('越界的 read_bytes', lambda: portal.read_bytes(outside))
        await _rejects('越界的 stat', lambda: portal.stat(outside))
        await _rejects('越界的 remove', lambda: portal.remove(outside))

    async def test_poll_since_hands_over_only_the_new_output(self, workspace: Workspace) -> None:
        portal = workspace.portal()
        handle = await portal.start(
            'printf first; while [ ! -f go ]; do sleep 0.02; done; printf second', cwd='.'
        )
        early = await _until(portal, handle, lambda result: 'first' in result.stdout)
        await portal.write_text('go', '')
        await _until(portal, handle, lambda result: not result.running)
        late = await portal.poll(handle, since=(early.stdout_end, early.stderr_end))
        assert (late.stdout, late.running, late.exit_code) == ('second', False, 0)
        assert late.stdout_end == early.stdout_end + len(b'second')

    async def test_a_timed_out_foreground_command_reports_it_without_raising(
        self, workspace: Workspace
    ) -> None:
        result = await workspace.portal().run('sleep 5', timeout=0.2, cwd='.')
        assert (result.timed_out, result.exit_code, result.running) == (True, None, False)

    async def test_kill_stops_a_background_command(self, workspace: Workspace) -> None:
        portal = workspace.portal()
        handle = await portal.start('sleep 30', cwd='.')
        await portal.kill(handle)
        assert (await portal.poll(handle)).running is False

    async def test_process_operations_are_refused_after_close(self, workspace: Workspace) -> None:
        portal = workspace.portal()
        await portal.close()
        await _rejects('close 之后的 start', lambda: portal.start('true', cwd='.'))
        await _rejects('close 之后的 pty_create', lambda: portal.pty_create('true', cwd='.'))

    async def test_a_terminal_session_reads_what_is_sent_and_reports_a_tty(
        self, workspace: Workspace
    ) -> None:
        portal = workspace.portal()
        handle = await portal.pty_create(
            '[ -t 0 ] && [ -t 1 ] && echo tty-yes; read line; echo "got:$line"', cwd='.'
        )
        first = await _captured(portal, handle, 'tty-yes', since=0)
        await portal.pty_send(handle, 'hello\n')
        later = await _captured(portal, handle, 'got:hello', since=first.end)
        assert '\x1b' not in later.text and '\r' not in later.text
        status = await _settled_terminal(portal, handle)
        assert (status.running, status.exit_code) == (False, 0)
        await portal.pty_kill(handle)

    async def test_a_terminal_capture_that_times_out_returns_nothing_instead_of_raising(
        self, workspace: Workspace
    ) -> None:
        portal = workspace.portal()
        handle = await portal.pty_create('sleep 30', cwd='.')
        captured = await portal.pty_capture(handle, since=0, wait=0.2)
        assert (captured.text, captured.running) == ('', True)
        await portal.pty_kill(handle)

    async def test_cancelling_a_waiting_capture_leaves_the_session_usable(
        self, workspace: Workspace
    ) -> None:
        portal = workspace.portal()
        handle = await portal.pty_create('read line; echo "after:$line"', cwd='.')
        waiting = asyncio.ensure_future(portal.pty_capture(handle, since=0, wait=30))
        await asyncio.sleep(0.05)
        waiting.cancel()
        try:
            await waiting
        except asyncio.CancelledError:
            pass
        await portal.pty_send(handle, 'x\n')
        await _captured(portal, handle, 'after:x', since=0)
        await portal.pty_kill(handle)

    async def test_resizing_a_terminal_reaches_the_program(self, workspace: Workspace) -> None:
        portal = workspace.portal()
        handle = await portal.pty_create(
            "trap 'stty size; echo resized' WINCH; echo trap-set; while :; do sleep 0.05; done",
            cwd='.',
            cols=80,
            rows=24,
        )
        ready = await _captured(portal, handle, 'trap-set', since=0)
        await portal.pty_resize(handle, cols=101, rows=33)
        resized = await _captured(portal, handle, 'resized', since=ready.end)
        assert '33 101' in resized.text
        status = await portal.pty_status(handle)
        assert (status.cols, status.rows) == (101, 33)
        await portal.pty_kill(handle)

    async def test_killing_a_terminal_leaves_none_of_its_processes_behind(
        self, workspace: Workspace
    ) -> None:
        portal = workspace.portal()
        handle = await portal.pty_create('sleep 300 & echo $! > child.pid; sleep 300', cwd='.')
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SETTLE_SECONDS
        while await portal.stat('child.pid') is None:
            assert loop.time() < deadline, '期望会话在宽限内写出子进程号'
            await asyncio.sleep(0.02)
        pid = (await portal.read_text('child.pid', offset=1, limit=1)).strip()
        await portal.pty_kill(handle)
        probe = await portal.run(f'kill -0 {pid} 2>/dev/null', timeout=5, cwd='.')
        assert probe.exit_code != 0, f'期望 kill 之后会话里的子进程 {pid} 不在了，实际还在'
        await _rejects('kill 之后的 pty_send', lambda: portal.pty_send(handle, 'x\n'))

    async def test_killing_a_terminal_that_ignores_term_and_hup_still_leaves_nothing_behind(
        self, workspace: Workspace
    ) -> None:
        portal = workspace.portal()
        handle = await portal.pty_create(
            "trap '' TERM HUP; sleep 300 & echo $! > stubborn.pid; sleep 300", cwd='.'
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SETTLE_SECONDS
        while await portal.stat('stubborn.pid') is None:
            assert loop.time() < deadline, '期望会话在宽限内写出子进程号'
            await asyncio.sleep(0.02)
        pid = (await portal.read_text('stubborn.pid', offset=1, limit=1)).strip()
        await portal.pty_kill(handle)
        probe = await portal.run(f'kill -0 {pid} 2>/dev/null', timeout=5, cwd='.')
        assert probe.exit_code != 0, f'期望忽略 SIGTERM 的子进程 {pid} 也被收掉，实际还在'

    async def test_sending_to_a_finished_terminal_is_refused(self, workspace: Workspace) -> None:
        portal = workspace.portal()
        handle = await portal.pty_create('exit 3', cwd='.')
        status = await _settled_terminal(portal, handle)
        assert (status.running, status.exit_code) == (False, 3)
        await _rejects('结束之后的 pty_send', lambda: portal.pty_send(handle, 'x\n'))
        await portal.pty_kill(handle)


async def _captured(portal: Portal, handle: str, needle: str, *, since: int) -> PtyCapture:
    """读到 `needle` 为止，交回从 `since` 起的全部文字与最后的 `end`。

    每次从上一次的 `end` 接着读：一直传同一个 `since` 时只要有过一点输出，`wait` 就再也不等，
    循环变成不让出事件循环的空转，读输出的回调永远轮不到。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SETTLE_SECONDS
    seen = ''
    end = since
    while True:
        captured = await portal.pty_capture(handle, since=end, wait=0.2)
        seen += captured.text
        end = captured.end
        if needle in seen:
            return dataclasses.replace(captured, text=seen)
        if loop.time() > deadline:
            raise AssertionError(
                f'期望终端在 {_SETTLE_SECONDS} 秒内输出 {needle!r}，实际看到 {seen!r}。'
                ' 检查这个实现的 pty_capture 是否交回 since 之后的全部输出。'
            )
        if not captured.text:
            await asyncio.sleep(0.02)


async def _settled_terminal(portal: Portal, handle: str) -> PtyStatus:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SETTLE_SECONDS
    while True:
        status = await portal.pty_status(handle)
        if not status.running:
            return status
        if loop.time() > deadline:
            raise AssertionError(f'期望终端里的程序在 {_SETTLE_SECONDS} 秒内结束，实际仍在运行')
        await asyncio.sleep(0.02)
