"""工作区路由的乐观并发（M5 D2、D3）：基准是这个 Agent 上一次看到的版本。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TypeVar, cast

import pytest

from syndica.kernel.driving import _DRIVING_AGENT  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.errors import ConfigError
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.tools.local import Edit, Read, Write
from syndica.kernel.workspace import Workspace, WriteConflict
from syndica.kernel.workspace_router import WorkspaceRouter

T = TypeVar('T')


async def _as(agent: str, call: Callable[[], Awaitable[T]]) -> T:
    async def driven() -> T:
        _DRIVING_AGENT.set(agent)
        return await call()

    return await asyncio.create_task(driven())


@pytest.fixture
async def router(tmp_path: Path) -> AsyncIterator[WorkspaceRouter]:
    routed = WorkspaceRouter(LocalWorkspace(str(tmp_path), shell='/bin/sh', env={}))
    yield routed
    await routed.close()


def _digest(data: bytes) -> str:
    return f'sha256:{hashlib.sha256(data).hexdigest()}'


async def _conflict(call: Callable[[], Awaitable[object]]) -> WriteConflict:
    try:
        await call()
    except WriteConflict as conflict:
        return conflict
    raise AssertionError('期望 WriteConflict，实际写成了')


async def test_the_later_of_two_agents_writing_the_same_file_gets_a_conflict(
    router: WorkspaceRouter, tmp_path: Path
) -> None:
    (tmp_path / 'notes.md').write_text('base')
    await _as('alpha', lambda: router.read_text('notes.md', offset=1, limit=10))
    await _as('bravo', lambda: router.read_text('notes.md', offset=1, limit=10))
    await _as('alpha', lambda: router.write_text('notes.md', 'from alpha'))

    conflict = await _conflict(lambda: _as('bravo', lambda: router.write_text('notes.md', 'b')))

    assert (tmp_path / 'notes.md').read_text() == 'from alpha'
    assert conflict.current == (await router.stat('notes.md')).version  # type: ignore[union-attr]
    assert '重新读它' in str(conflict)


async def test_overwriting_a_file_the_agent_never_read_is_a_conflict(
    router: WorkspaceRouter, tmp_path: Path
) -> None:
    (tmp_path / 'notes.md').write_text('someone else wrote this')

    conflict = await _conflict(lambda: _as('alpha', lambda: router.write_text('notes.md', 'x')))

    assert conflict.unread is True
    assert 'Read' in str(conflict)
    assert (tmp_path / 'notes.md').read_text() == 'someone else wrote this'


async def test_another_agents_read_does_not_count_for_this_one(
    router: WorkspaceRouter, tmp_path: Path
) -> None:
    (tmp_path / 'notes.md').write_text('base')
    await _as('alpha', lambda: router.read_text('notes.md', offset=1, limit=10))

    conflict = await _conflict(lambda: _as('bravo', lambda: router.write_text('notes.md', 'x')))

    assert conflict.unread is True


async def test_a_change_made_outside_the_tools_makes_the_next_write_conflict(
    router: WorkspaceRouter, tmp_path: Path
) -> None:
    (tmp_path / 'notes.md').write_text('base')
    await _as('alpha', lambda: router.read_text('notes.md', offset=1, limit=10))
    (tmp_path / 'notes.md').write_text('a formatter rewrote this')

    await _conflict(lambda: _as('alpha', lambda: router.write_text('notes.md', 'x')))

    assert (tmp_path / 'notes.md').read_text() == 'a formatter rewrote this'


async def test_an_agent_can_keep_writing_what_it_wrote_itself(
    router: WorkspaceRouter, tmp_path: Path
) -> None:
    await _as('alpha', lambda: router.write_text('draft.md', 'one'))
    await _as('alpha', lambda: router.write_text('draft.md', 'two'))
    await _as('alpha', lambda: router.edit_text('draft.md', 'two', 'three'))
    await _as('alpha', lambda: router.write_text('draft.md', 'four'))

    assert (tmp_path / 'draft.md').read_text() == 'four'


async def test_an_edit_on_what_the_agent_saw_conflicts_once_someone_else_changed_it(
    router: WorkspaceRouter, tmp_path: Path
) -> None:
    (tmp_path / 'notes.md').write_text('alpha bravo')
    await _as('alpha', lambda: router.read_text('notes.md', offset=1, limit=10))
    await _as('bravo', lambda: router.read_text('notes.md', offset=1, limit=10))
    await _as('bravo', lambda: router.edit_text('notes.md', 'alpha', 'ALPHA'))

    await _conflict(lambda: _as('alpha', lambda: router.edit_text('notes.md', 'bravo', 'B')))

    assert (tmp_path / 'notes.md').read_text() == 'ALPHA bravo'


async def test_an_edit_without_a_prior_read_applies_to_the_current_content(
    router: WorkspaceRouter, tmp_path: Path
) -> None:
    (tmp_path / 'notes.md').write_text('alpha bravo')

    await _as('alpha', lambda: router.edit_text('notes.md', 'bravo', 'BRAVO'))

    assert (tmp_path / 'notes.md').read_text() == 'alpha BRAVO'


async def test_a_write_racing_the_read_leaves_the_version_that_was_read_on_record(
    router: WorkspaceRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / 'notes.md').write_text('before')
    real_read = LocalWorkspace.read_versioned

    async def read_then_someone_writes(
        self: LocalWorkspace, path: str, **kwargs: int
    ) -> tuple[str, str]:
        answer = await real_read(self, path, **kwargs)
        (tmp_path / 'notes.md').write_text('written while the agent was reading')
        return answer

    monkeypatch.setattr(LocalWorkspace, 'read_versioned', read_then_someone_writes)
    seen = await _as('alpha', lambda: router.read_text('notes.md', offset=1, limit=5))
    monkeypatch.setattr(LocalWorkspace, 'read_versioned', real_read)

    conflict = await _conflict(lambda: _as('alpha', lambda: router.write_text('notes.md', 'x')))

    assert seen == 'before'
    assert (conflict.unread, conflict.expected) == (False, _digest(b'before'))
    assert (tmp_path / 'notes.md').read_text() == 'written while the agent was reading'


async def test_an_edit_without_a_read_does_not_count_as_having_read_the_file(
    router: WorkspaceRouter, tmp_path: Path
) -> None:
    (tmp_path / 'notes.md').write_text('title\nbody nobody read')

    await _as('alpha', lambda: router.edit_text('notes.md', 'title', 'Title'))
    conflict = await _conflict(lambda: _as('alpha', lambda: router.write_text('notes.md', 'x')))

    assert conflict.unread is True
    assert (tmp_path / 'notes.md').read_text() == 'Title\nbody nobody read'


async def test_an_edit_after_a_read_keeps_the_file_on_record_as_seen(
    router: WorkspaceRouter, tmp_path: Path
) -> None:
    (tmp_path / 'notes.md').write_text('title\nbody')

    await _as('alpha', lambda: router.read_text('notes.md', offset=1, limit=5))
    await _as('alpha', lambda: router.edit_text('notes.md', 'title', 'Title'))
    await _as('alpha', lambda: router.write_text('notes.md', 'rewritten'))

    assert (tmp_path / 'notes.md').read_text() == 'rewritten'


async def test_a_read_refused_for_its_encoding_still_lets_the_agent_overwrite_the_file(
    router: WorkspaceRouter, tmp_path: Path
) -> None:
    (tmp_path / 'blob.txt').write_bytes(b'\xff\xfe not utf-8')

    with pytest.raises(ConfigError):
        await _as('alpha', lambda: router.read_text('blob.txt', offset=1, limit=5))
    await _as('alpha', lambda: router.write_text('blob.txt', 'replaced'))

    assert (tmp_path / 'blob.txt').read_text() == 'replaced'


async def test_a_read_that_found_nothing_makes_a_file_created_since_a_conflict(
    router: WorkspaceRouter, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError):
        await _as('alpha', lambda: router.read_text('later.md', offset=1, limit=5))
    (tmp_path / 'later.md').write_text('someone else got here first')

    conflict = await _conflict(lambda: _as('alpha', lambda: router.write_text('later.md', 'x')))

    assert (conflict.unread, conflict.expected) == (False, None)
    assert (tmp_path / 'later.md').read_text() == 'someone else got here first'


class _Yielding:
    """每次调用之前先让出一次的后端：远端后端的每一步都隔着一次往返。"""

    def __init__(self, inner: LocalWorkspace) -> None:
        self._inner = inner

    @property
    def root(self) -> str:
        return self._inner.root

    def portal(self) -> _Yielding:
        return self

    def __getattr__(self, name: str) -> Callable[..., Awaitable[object]]:
        method = getattr(self._inner, name)

        async def yielding(*args: object, **kwargs: object) -> object:
            await asyncio.sleep(0)
            return await method(*args, **kwargs)

        return yielding


async def test_two_agents_creating_one_new_file_over_a_yielding_backend_have_one_winner(
    tmp_path: Path,
) -> None:
    local = LocalWorkspace(str(tmp_path), shell='/bin/sh', env={})
    router = WorkspaceRouter(cast(Workspace, _Yielding(local)))
    try:
        outcomes = await asyncio.gather(
            _as('alpha', lambda: router.write_text('fresh.md', 'alpha')),
            _as('bravo', lambda: router.write_text('fresh.md', 'bravo')),
            _as('charlie', lambda: router.write_text('fresh.md', 'charlie')),
            return_exceptions=True,
        )
    finally:
        await local.close()

    assert sorted(type(outcome).__name__ for outcome in outcomes) == [
        'NoneType',
        'WriteConflict',
        'WriteConflict',
    ]


async def test_removing_an_unread_file_is_a_conflict_and_a_read_one_goes(
    router: WorkspaceRouter, tmp_path: Path
) -> None:
    (tmp_path / 'old.md').write_text('keep me')

    await _conflict(lambda: _as('alpha', lambda: router.remove('old.md')))
    await _as('alpha', lambda: router.read_text('old.md', offset=1, limit=5))
    await _as('alpha', lambda: router.remove('old.md'))

    assert not (tmp_path / 'old.md').exists()


async def test_the_write_tool_hands_a_conflict_back_as_a_recoverable_result(
    router: WorkspaceRouter, tmp_path: Path
) -> None:
    (tmp_path / 'notes.md').write_text('base')
    read, write, edit = Read(router), Write(router), Edit(router)
    await _as('alpha', lambda: read.execute('notes.md'))
    await _as('bravo', lambda: read.execute('notes.md'))
    await _as('alpha', lambda: write.execute('notes.md', 'from alpha'))

    rejected = await _as('bravo', lambda: write.execute('notes.md', 'from bravo'))
    edited = await _as('bravo', lambda: edit.execute('notes.md', 'base', 'x'))
    reread = await _as('bravo', lambda: read.execute('notes.md'))
    merged = await _as('bravo', lambda: write.execute('notes.md', 'from alpha + bravo'))

    assert '期望:' in rejected.output and '实际:' in rejected.output
    assert '下一步:' in rejected.output
    assert rejected.meta['conflict']['path'] == 'notes.md'
    assert 'path' not in rejected.meta
    assert 'conflict' in edited.meta
    assert reread.output == 'from alpha'
    assert merged.meta == {'path': 'notes.md'}
    assert (tmp_path / 'notes.md').read_text() == 'from alpha + bravo'


async def test_spellings_of_the_same_path_share_one_record(
    router: WorkspaceRouter, tmp_path: Path
) -> None:
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'docs' / 'a.md').write_text('base')
    await _as('alpha', lambda: router.read_text('./docs/../docs/a.md', offset=1, limit=5))
    await _as('alpha', lambda: router.write_text(str(tmp_path / 'docs' / 'a.md'), 'x'))

    assert (tmp_path / 'docs' / 'a.md').read_text() == 'x'
