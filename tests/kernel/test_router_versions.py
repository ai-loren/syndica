"""路由接版本层（M5 D5）：经路由的写记版本、回滚写回并再记一版；`Write` / `Edit` 交回产物引用。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TypeVar

import pytest

from syndica.kernel.driving import _DRIVING_AGENT  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.errors import ConfigError
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.tools.local import Edit, Read, Write
from syndica.kernel.versions import VersionStore
from syndica.kernel.workspace import WriteConflict
from syndica.kernel.workspace_router import WorkspaceRouter

T = TypeVar('T')


def _digest(data: bytes) -> str:
    return f'sha256:{hashlib.sha256(data).hexdigest()}'


async def _as(agent: str, call: Callable[[], Awaitable[T]]) -> T:
    async def driven() -> T:
        _DRIVING_AGENT.set(agent)
        return await call()

    return await asyncio.create_task(driven())


@pytest.fixture
def versions(tmp_path: Path) -> VersionStore:
    return VersionStore(tmp_path, redactor=RuleRedactor())


@pytest.fixture
async def router(tmp_path: Path, versions: VersionStore) -> AsyncIterator[WorkspaceRouter]:
    local = LocalWorkspace(str(tmp_path), shell='/bin/sh', env={})
    yield WorkspaceRouter(local, versions=versions)
    await local.close()


async def test_writes_edits_and_removes_through_the_router_are_recorded_with_their_agent(
    router: WorkspaceRouter, versions: VersionStore
) -> None:
    await _as('alpha', lambda: router.write_text('notes.md', 'one'))
    await _as('bravo', lambda: router.edit_text('notes.md', 'one', 'two'))
    await _as('alpha', lambda: router.read_text('notes.md', offset=1, limit=5))
    await _as('alpha', lambda: router.remove('notes.md'))

    assert [
        (entry.producer, entry.removed, entry.version) for entry in versions.history('notes.md')
    ] == [
        ('alpha', True, None),
        ('bravo', False, _digest(b'two')),
        ('alpha', False, _digest(b'one')),
    ]


async def test_a_write_from_host_code_is_recorded_as_the_host(
    router: WorkspaceRouter, versions: VersionStore
) -> None:
    await router.write_bytes('data.bin', b'\x00')

    assert [entry.producer for entry in versions.history('data.bin')] == ['host']


async def test_restore_writes_the_old_version_back_and_records_it_again(
    router: WorkspaceRouter, versions: VersionStore, tmp_path: Path
) -> None:
    await _as('alpha', lambda: router.write_text('notes.md', 'first'))
    await _as('alpha', lambda: router.write_text('notes.md', 'second'))

    restored = await router.restore('notes.md', _digest(b'first'))

    assert restored == _digest(b'first')
    assert (tmp_path / 'notes.md').read_text() == 'first'
    assert [entry.version for entry in versions.history('notes.md')] == [
        _digest(b'first'),
        _digest(b'second'),
        _digest(b'first'),
    ]


async def test_after_a_restore_an_agent_holding_the_old_version_gets_a_conflict(
    router: WorkspaceRouter,
) -> None:
    await _as('alpha', lambda: router.write_text('notes.md', 'first'))
    await _as('alpha', lambda: router.write_text('notes.md', 'second'))
    await router.restore('notes.md', _digest(b'first'))

    conflict = await _as('alpha', lambda: _conflict(router.write_text('notes.md', 'third')))

    assert (conflict.expected, conflict.current) == (_digest(b'second'), _digest(b'first'))


async def _conflict(call: Awaitable[object]) -> WriteConflict:
    try:
        await call
    except WriteConflict as conflict:
        return conflict
    raise AssertionError('期望 WriteConflict，实际写成了')


async def test_restore_without_a_version_layer_says_how_to_turn_it_on(tmp_path: Path) -> None:
    local = LocalWorkspace(str(tmp_path), shell='/bin/sh', env={})
    try:
        with pytest.raises(ConfigError) as refused:
            await WorkspaceRouter(local).restore('notes.md', _digest(b'x'))
    finally:
        await local.close()

    assert 'versions' in refused.value.next_action


async def test_a_version_layer_that_fails_does_not_turn_a_landed_write_into_a_failure(
    router: WorkspaceRouter,
    versions: VersionStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(*_: object, **__: object) -> None:
        raise OSError('disk full')

    monkeypatch.setattr(versions, 'record', broken)
    with caplog.at_level(logging.ERROR, logger='syndica.kernel.workspace_router'):
        written = await _as('alpha', lambda: router.write_text('notes.md', 'kept'))

    assert written == _digest(b'kept')
    assert (tmp_path / 'notes.md').read_text() == 'kept'
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert isinstance(record.args, tuple)
    assert (record.args[0], str(record.args[1])) == ('notes.md', 'disk full')


async def test_write_and_edit_hand_back_an_artifact_carrying_the_written_version(
    router: WorkspaceRouter, tmp_path: Path
) -> None:
    written = await _as('alpha', lambda: Write(router).execute('docs/plan.md', 'alpha'))
    await _as('alpha', lambda: Read(router).execute('docs/plan.md'))
    edited = await _as('bravo', lambda: Edit(router).execute('docs/plan.md', 'alpha', 'bravo'))

    first, second = written.artifacts[0], edited.artifacts[0]
    assert (first.version, second.version) == (_digest(b'alpha'), _digest(b'bravo'))
    assert first.id == second.id == 'file:docs/plan.md'
    assert (first.producer, second.producer) == ('alpha', 'bravo')
    assert (first.kind, first.mime) == ('document', 'text/markdown')


async def test_an_artifact_id_names_the_file_however_its_path_was_spelled(
    router: WorkspaceRouter,
) -> None:
    plain = await Write(router).execute('docs/plan.md', 'one')
    await Read(router).execute('docs/plan.md')
    dotted = await Write(router).execute('./docs/../docs/plan.md', 'two')

    assert plain.artifacts[0].id == dotted.artifacts[0].id == 'file:docs/plan.md'


async def test_a_refused_write_hands_back_no_artifact(router: WorkspaceRouter) -> None:
    await Write(router).execute('notes.md', 'one')

    refused = await _as('bravo', lambda: Write(router).execute('notes.md', 'two'))

    assert refused.artifacts == []
    assert 'conflict' in refused.meta


async def test_an_unknown_extension_is_a_plain_file(router: WorkspaceRouter) -> None:
    written = await Write(router).execute('build/output.xyz', 'x')

    assert (written.artifacts[0].kind, written.artifacts[0].mime) == (
        'file',
        'application/octet-stream',
    )
