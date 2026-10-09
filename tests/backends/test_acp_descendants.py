"""托管的 ACP Agent：关闭、超时、取消、崩溃之后，它的后代一个不留，含 setsid 离开了进程组的。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from syndica.backends.acp import process as process_module
from syndica.backends.acp.model import AcpAgentModel
from syndica.backends.acp.process import (
    _TREE_POLL_S,  # pyright: ignore[reportPrivateUsage]
    ExternalProcess,
    _ProcessTree,  # pyright: ignore[reportPrivateUsage]
)
from syndica.kernel import BlockHighRisk, ContextBlock, DangerJudge, ExternalCallPolicy
from syndica.kernel.call_limit import (
    BudgetDeadline,
    arm_model_call_limit,
    reset_model_call_limit,
)
from syndica.kernel.redaction import RuleRedactor

_AGENT = """
import asyncio, os, subprocess, sys

marker, mode = sys.argv[1], sys.argv[2]
pending = set()


def spawn():
    plain = subprocess.Popen(['/bin/sleep', '300'])
    detached = subprocess.Popen(['/bin/sleep', '300'], start_new_session=True)
    with open(marker + '.tmp', 'w') as handle:
        handle.write(f'{plain.pid} {detached.pid}')
    os.replace(marker + '.tmp', marker)


async def crash_when_told():
    while not os.path.exists(marker + '.crash'):
        await asyncio.sleep(0.02)
    os._exit(3)


async def main():
    from acp.agent.connection import AgentSideConnection
    from acp.schema import (
        AgentMessageChunk,
        Implementation,
        InitializeResponse,
        NewSessionResponse,
        PromptResponse,
        TextContentBlock,
    )
    from acp.stdio import stdio_streams

    class Leaky:
        def on_connect(self, conn):
            self.conn = conn

        async def initialize(self, protocol_version, **kwargs):
            return InitializeResponse(
                protocol_version=1, agent_info=Implementation(name='leaky', version='0')
            )

        async def new_session(self, cwd, **kwargs):
            return NewSessionResponse(session_id='leaky')

        async def prompt(self, session_id, prompt, **kwargs):
            spawn()
            if mode == 'hang':
                await asyncio.Event().wait()
            if mode == 'crash':
                task = asyncio.get_running_loop().create_task(crash_when_told())
                pending.add(task)
            await self.conn.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update='agent_message_chunk',
                    content=TextContentBlock(type='text', text='spawned'),
                ),
            )
            return PromptResponse(stop_reason='end_turn')

        async def cancel(self, session_id, **kwargs):
            pass

    reader, writer = await stdio_streams()
    await AgentSideConnection(Leaky(), writer, reader, listening=False).listen()
    if mode == 'hang':
        await asyncio.Event().wait()


asyncio.run(main())
"""


def _model(tmp_path: Path, mode: str) -> tuple[AcpAgentModel, Path]:
    script = tmp_path / 'leaky_agent.py'
    script.write_text(_AGENT, encoding='utf-8')
    marker = tmp_path / 'pids'
    model = AcpAgentModel(
        'leaky',
        command=(sys.executable, str(script), str(marker), mode),
        tools={},
        policy=ExternalCallPolicy(judge=DangerJudge(), policy=BlockHighRisk()),
        cwd=str(tmp_path),
        env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(tmp_path)},
        redactor=RuleRedactor(),
    )
    return model, marker


def _pids_in(marker: Path) -> list[int]:
    if not marker.exists():
        return []
    return [int(part) for part in marker.read_text(encoding='utf-8').split()]


async def _spawned(marker: Path) -> list[int]:
    deadline = time.monotonic() + 10
    while not _pids_in(marker) and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.02)
    pids = _pids_in(marker)
    assert len(pids) == 2
    assert all(_running(pid) for pid in pids)
    return pids


def _running(pid: int) -> bool:
    state = subprocess.run(
        ['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(state) and not state.startswith('Z')


def _survivors(pids: list[int]) -> list[int]:
    return [pid for pid in pids if _running(pid)]


def _kill_all(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            continue


async def test_closing_the_model_leaves_neither_a_group_member_nor_a_setsid_grandchild(
    tmp_path: Path,
) -> None:
    model, marker = _model(tmp_path, 'answer')
    pids: list[int] = []
    try:
        reply = await model.create([ContextBlock(role='user', content='go')])
        assert reply.content == 'spawned'
        pids = await _spawned(marker)
        await model.aclose()
        assert _survivors(pids) == []
    finally:
        await model.aclose()
        _kill_all(pids)


async def test_a_setsid_grandchild_spawned_after_the_last_poll_is_reaped_on_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process_module, '_TREE_POLL_S', 60.0)
    model, marker = _model(tmp_path, 'answer')
    pids: list[int] = []
    try:
        await model.create([ContextBlock(role='user', content='go')])
        pids = await _spawned(marker)
        await model.aclose()
        assert _survivors(pids) == []
    finally:
        await model.aclose()
        _kill_all(pids)


async def test_a_timed_out_call_leaves_no_descendant_before_the_model_is_closed(
    tmp_path: Path,
) -> None:
    model, marker = _model(tmp_path, 'hang')
    pids: list[int] = []
    token = arm_model_call_limit(2.0)
    try:
        with pytest.raises(BudgetDeadline):
            await model.create([ContextBlock(role='user', content='hang')])
        pids = _pids_in(marker)
        assert len(pids) == 2
        assert _survivors(pids) == []
        await model.aclose()
        assert _survivors(pids) == []
    finally:
        reset_model_call_limit(token)
        await model.aclose()
        _kill_all(_pids_in(marker))


async def test_a_cancelled_call_leaves_no_descendant_before_the_model_is_closed(
    tmp_path: Path,
) -> None:
    model, marker = _model(tmp_path, 'hang')
    task = asyncio.create_task(model.create([ContextBlock(role='user', content='hang')]))
    pids: list[int] = []
    try:
        pids = await _spawned(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _survivors(pids) == []
        await model.aclose()
        assert _survivors(pids) == []
    finally:
        task.cancel()
        await model.aclose()
        _kill_all(pids)


async def test_a_crashed_agents_descendants_are_gone_before_and_after_close(
    tmp_path: Path,
) -> None:
    model, marker = _model(tmp_path, 'crash')
    pids: list[int] = []
    try:
        await model.create([ContextBlock(role='user', content='go')])
        pids = await _spawned(marker)
        await asyncio.sleep(4 * _TREE_POLL_S)
        marker.with_name(marker.name + '.crash').write_text('', encoding='utf-8')
        deadline = time.monotonic() + 10
        while _survivors(pids) and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.02)
        assert _survivors(pids) == []
        await model.aclose()
        assert _survivors(pids) == []
    finally:
        await model.aclose()
        _kill_all(pids)


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


async def test_the_watcher_reaps_a_crashed_roots_descendants_without_anyone_closing(
    tmp_path: Path,
) -> None:
    root = await asyncio.create_subprocess_exec(
        sys.executable,
        '-c',
        _ROOT,
        cwd=str(tmp_path),
        env={'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    tree = _ProcessTree(root.pid, grace=0.05)
    pids: list[int] = []
    try:
        tree.start()
        assert root.stdin is not None
        assert root.stdout is not None
        line = await asyncio.wait_for(root.stdout.readline(), 5)
        pids = [int(part) for part in line.split()]
        assert all(_running(pid) for pid in pids)
        await asyncio.sleep(4 * _TREE_POLL_S)
        root.stdin.close()
        await asyncio.wait_for(root.wait(), 5)
        deadline = time.monotonic() + 2 * _TREE_POLL_S + 5
        while _survivors(pids) and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.02)
        assert _survivors(pids) == []
    finally:
        tree.stop()
        _kill_all(pids)
        if root.returncode is None:
            root.kill()
            await root.wait()


async def test_a_close_cancelled_in_the_middle_of_the_ladder_still_reaps_the_tree(
    tmp_path: Path,
) -> None:
    process = ExternalProcess(
        [sys.executable, '-c', _ROOT],
        env={'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
        cwd=str(tmp_path),
        redactor=RuleRedactor(),
        grace=1.0,
    )
    await process.start()
    pids: list[int] = []
    try:
        line = await asyncio.wait_for(process.read_stdout_line(), 5)
        pids = [int(part) for part in line.split()]
        assert all(_running(pid) for pid in pids)
        closing = asyncio.create_task(process.aclose())
        await asyncio.sleep(0.2)
        assert not closing.done()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert _survivors(pids) == []
    finally:
        _kill_all(pids)
        await process.aclose()
        for task in (process._watch, process._reader):  # pyright: ignore[reportPrivateUsage]
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


_STRANGER_START = 'Thu Jan  1 00:00:00 1970'


async def test_a_recorded_pid_whose_start_time_no_longer_matches_is_not_signalled() -> None:
    root = await asyncio.create_subprocess_exec('/bin/sleep', '300', start_new_session=True)
    stranger = await asyncio.create_subprocess_exec('/bin/sleep', '300', start_new_session=True)
    tree = _ProcessTree(root.pid, grace=0.05)
    try:
        tree.start()
        tree.stop()
        tree._seen[stranger.pid] = _STRANGER_START  # pyright: ignore[reportPrivateUsage]
        root.kill()
        await root.wait()
        await tree.reap()
        assert _running(stranger.pid)
    finally:
        tree.stop()
        for process in (root, stranger):
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_a_recorded_pid_whose_start_time_still_matches_is_signalled() -> None:
    root = await asyncio.create_subprocess_exec('/bin/sleep', '300', start_new_session=True)
    adopted = await asyncio.create_subprocess_exec('/bin/sleep', '300', start_new_session=True)
    tree = _ProcessTree(root.pid, grace=0.05)
    try:
        tree.start()
        tree.stop()
        table = process_module._process_table()  # pyright: ignore[reportPrivateUsage]
        assert table is not None
        tree._seen[adopted.pid] = table[adopted.pid].started  # pyright: ignore[reportPrivateUsage]
        root.kill()
        await root.wait()
        await tree.reap()
        await asyncio.wait_for(adopted.wait(), 5)
        assert not _running(adopted.pid)
    finally:
        tree.stop()
        for process in (root, adopted):
            if process.returncode is None:
                process.kill()
                await process.wait()
