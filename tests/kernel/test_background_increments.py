"""后台命令与终端的增量读取（M5 D8）：每个 Agent 只拿到它上次之后的新输出，互不串线。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TypeVar

import pytest

from syndica.kernel.driving import _DRIVING_AGENT  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.tool import ToolResult
from syndica.kernel.tools.local import Bash, BashOutput, Terminal

T = TypeVar('T')
_TWO_PHASES = 'printf first; while [ ! -f go ]; do sleep 0.02; done; printf second'


async def _as(agent: str, call: Callable[[], Awaitable[T]]) -> T:
    async def driven() -> T:
        _DRIVING_AGENT.set(agent)
        return await call()

    return await asyncio.create_task(driven())


@pytest.fixture
async def workspace(tmp_path: Path) -> AsyncIterator[LocalWorkspace]:
    local = LocalWorkspace(str(tmp_path), shell='/bin/sh', env={})
    yield local
    await local.close()


async def _started(workspace: LocalWorkspace) -> tuple[str, BashOutput]:
    started = await Bash(workspace).execute(_TWO_PHASES, run_in_background=True)
    return started.meta['handle'], BashOutput(workspace)


async def _until(
    poll: Callable[[], Awaitable[ToolResult]], done: Callable[[ToolResult], bool]
) -> ToolResult:
    result = await poll()
    for _ in range(250):
        if done(result):
            return result
        await asyncio.sleep(0.02)
        result = await poll()
    raise AssertionError(f'期望后台命令到达预期状态，实际最后一次是 {result.output!r}')


async def _finish(workspace: LocalWorkspace, tmp_path: Path) -> None:
    (tmp_path / 'go').write_text('')
    for _ in range(250):
        handle_results = list(workspace._processes.values())  # pyright: ignore[reportPrivateUsage]
        if all(record.settled.done() for record in handle_results):
            return
        await asyncio.sleep(0.02)


def _shown(result: ToolResult, stream: str) -> str:
    """模型看到的那一段流（`meta` 在命令跑完后给的是全部输出，不是这次的增量）。"""
    shown = result.output
    assert isinstance(shown, str)
    after = shown.split(f'\n{stream}:\n', 1)[1]
    return after.split('\nstderr:\n', 1)[0] if stream == 'stdout' else after


async def test_a_second_poll_hands_over_only_what_arrived_since_the_first(
    workspace: LocalWorkspace, tmp_path: Path
) -> None:
    handle, output = await _started(workspace)
    early = await _until(lambda: output.execute(handle), lambda r: _shown(r, 'stdout') == 'first')
    await _finish(workspace, tmp_path)
    late = await output.execute(handle)

    assert _shown(late, 'stdout') == 'second'
    assert '上次查询之后的新输出' in late.output
    assert '上次查询' not in early.output


async def test_stderr_is_handed_over_in_increments_too(
    workspace: LocalWorkspace, tmp_path: Path
) -> None:
    started = await Bash(workspace).execute(
        'printf one >&2; while [ ! -f go ]; do sleep 0.02; done; printf two >&2',
        run_in_background=True,
    )
    handle, output = started.meta['handle'], BashOutput(workspace)
    await _until(lambda: output.execute(handle), lambda r: _shown(r, 'stderr') == 'one')
    await _finish(workspace, tmp_path)
    late = await output.execute(handle)

    assert _shown(late, 'stderr') == 'two'


async def test_once_finished_meta_carries_the_whole_output_for_readers_like_the_dev_log(
    workspace: LocalWorkspace, tmp_path: Path
) -> None:
    handle, output = await _started(workspace)
    early = await _until(lambda: output.execute(handle), lambda r: _shown(r, 'stdout') == 'first')
    await _finish(workspace, tmp_path)
    late = await output.execute(handle)

    assert early.meta['stdout'] == 'first'
    assert (late.meta['running'], late.meta['stdout']) == (False, 'firstsecond')


async def test_full_hands_over_everything_again(workspace: LocalWorkspace, tmp_path: Path) -> None:
    handle, output = await _started(workspace)
    await _until(lambda: output.execute(handle), lambda r: _shown(r, 'stdout') == 'first')
    await _finish(workspace, tmp_path)

    full = await output.execute(handle, full=True)
    after = await output.execute(handle)

    assert _shown(full, 'stdout') == 'firstsecond'
    assert _shown(after, 'stdout') == ''


async def test_two_agents_polling_one_command_each_get_their_own_increments(
    workspace: LocalWorkspace, tmp_path: Path
) -> None:
    handle, output = await _started(workspace)
    await _until(
        lambda: _as('alpha', lambda: output.execute(handle)),
        lambda r: _shown(r, 'stdout') == 'first',
    )
    await _finish(workspace, tmp_path)

    bravo = await _as('bravo', lambda: output.execute(handle))
    alpha = await _as('alpha', lambda: output.execute(handle))

    assert _shown(bravo, 'stdout') == 'firstsecond'
    assert _shown(alpha, 'stdout') == 'second'


async def test_full_must_be_a_boolean(workspace: LocalWorkspace) -> None:
    handle, output = await _started(workspace)

    told = await output.execute(handle, full='yes')  # pyright: ignore[reportArgumentType]

    assert '期望: full 是 true 或 false' in told.output
    assert "实际: 'yes'" in told.output
    assert '下一步:' in told.output


async def test_two_agents_reading_one_terminal_each_keep_their_own_place(
    workspace: LocalWorkspace,
) -> None:
    terminal = Terminal(workspace)
    created = await _as(
        'alpha', lambda: terminal.execute(action='create', command='echo hello; sleep 30', wait=3)
    )
    handle = created.meta['handle']
    shown = created.meta['output']
    for _ in range(50):
        if 'hello' in shown:
            break
        later = await _as(
            'alpha', lambda: terminal.execute(action='capture', handle=handle, wait=0.2)
        )
        shown += later.meta['output']

    bravo = await _as('bravo', lambda: terminal.execute(action='capture', handle=handle, wait=0))
    again = await _as('alpha', lambda: terminal.execute(action='capture', handle=handle, wait=0))

    assert 'hello' in shown
    assert 'hello' in bravo.meta['output']
    assert again.meta['output'] == ''
