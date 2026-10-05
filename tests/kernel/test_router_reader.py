"""WorkspaceRouter 给伴生的只读视图（M7 评审 B2）与产物作用域（评审 H5）。

伴生在主 Agent 的驱动任务里跑：经路由读会把读记在主 Agent 名下、刷新它的写前提，主 Agent 下一次
整篇覆盖就把别人的改动静默盖掉。`reader_for` 交出的视图读同一份工作区，但不碰读基线。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

import pytest

from syndica.kernel.context import TrustLevel
from syndica.kernel.driving import _DRIVING_AGENT  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.errors import ConfigError
from syndica.kernel.file_taint import TaintAware
from syndica.kernel.isolation import LocalIsolator
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.workspace import WriteConflict
from syndica.kernel.workspace_router import WorkspaceRouter

T = TypeVar('T')


async def _as(agent: str, call: Callable[[], Awaitable[T]]) -> T:
    async def driven() -> T:
        _DRIVING_AGENT.set(agent)
        return await call()

    return await asyncio.create_task(driven())


def _router(tmp_path: Path) -> WorkspaceRouter:
    (tmp_path / 'f.md').write_text('v1\n', encoding='utf-8')
    return WorkspaceRouter(LocalWorkspace(str(tmp_path), shell='/bin/sh', env={}))


async def _other_agent_edits(router: WorkspaceRouter) -> None:
    await _as('other', lambda: router.read_text('f.md', offset=1, limit=10))
    await _as('other', lambda: router.write_text('f.md', 'v2 from other\n'))


async def test_a_companion_read_through_the_view_keeps_the_main_agents_conflict(
    tmp_path: Path,
) -> None:
    router = _router(tmp_path)
    await _as('main', lambda: router.read_text('f.md', offset=1, limit=10))  # main 看到 v1
    await _other_agent_edits(router)  # 别人改成 v2
    view = router.reader_for('main')
    await _as(
        'main', lambda: view.read_text('f.md', offset=1, limit=10)
    )  # 伴生读（在 main 的任务里）
    with pytest.raises(WriteConflict):
        await _as('main', lambda: router.write_text('f.md', 'main overwrites\n'))
    assert (tmp_path / 'f.md').read_text(encoding='utf-8') == 'v2 from other\n'  # 别人的改动还在


async def test_reading_through_the_router_itself_would_hide_the_conflict(tmp_path: Path) -> None:
    # 对照组：伴生若直接经路由读，主 Agent 的写前提被刷新，覆盖静默成功——这正是视图要防的。
    router = _router(tmp_path)
    await _as('main', lambda: router.read_text('f.md', offset=1, limit=10))
    await _other_agent_edits(router)
    await _as('main', lambda: router.read_text('f.md', offset=1, limit=10))
    await _as('main', lambda: router.write_text('f.md', 'main overwrites\n'))
    assert (tmp_path / 'f.md').read_text(encoding='utf-8') == 'main overwrites\n'


async def test_the_view_reads_the_agents_workspace(tmp_path: Path) -> None:
    view = _router(tmp_path).reader_for('main')
    assert await view.read_text('f.md', offset=1, limit=10) == 'v1'
    assert await view.glob_paths('*.md') == ('f.md',)


async def test_the_view_refuses_writes_and_commands(tmp_path: Path) -> None:
    view = _router(tmp_path).reader_for('main')
    with pytest.raises(ConfigError):
        await view.write_text('f.md', 'forged\n')
    with pytest.raises(ConfigError):
        await view.edit_text('f.md', 'v1', 'forged')
    with pytest.raises(ConfigError):
        await view.remove('f.md')
    with pytest.raises(ConfigError):
        await view.set_executable('f.md', True)
    with pytest.raises(ConfigError):
        await view.run('rm -rf .', timeout=1, cwd='.')
    assert (tmp_path / 'f.md').read_text(encoding='utf-8') == 'v1\n'
    assert not (tmp_path / 'f.md').stat().st_mode & 0o111


def test_the_view_is_taint_aware_so_reads_keep_their_trust(tmp_path: Path) -> None:
    view = _router(tmp_path).reader_for('main')
    assert isinstance(view, TaintAware)
    assert view.file_trust('f.md', None) is TrustLevel.TRUSTED  # 没有污点时可信，与路由同口径


def test_an_agent_on_the_root_workspace_has_an_empty_scope(tmp_path: Path) -> None:
    assert _router(tmp_path).scope_of('main') == ''


async def test_an_isolated_agent_is_scoped_by_its_copy(tmp_path: Path) -> None:
    # 两个各在自己副本里写同一路径的 Agent 必须是两个产物：作用域就是副本属主（评审 H5）。
    root = tmp_path / 'ws'
    root.mkdir()
    router = WorkspaceRouter(
        LocalWorkspace(str(root), shell='/bin/sh', env={}),
        isolator=LocalIsolator(shell='/bin/sh', env={}),
    )
    try:
        assert router.isolate('alpha', 'snapshot') is not None
        assert router.isolate('beta', 'snapshot') is not None
        assert router.scope_of('alpha') == 'alpha'
        assert router.scope_of('beta') == 'beta'
        assert router.scope_of('gamma') == ''  # 没有副本的仍在根工作区
    finally:
        await router.close()
