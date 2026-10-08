"""工作区隔离（M5 D6）：副本、逐文件合并、冲突另存、git 三方合并的隔离、Runtime 与派活的接线。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

import pytest

import syndica.kernel.isolation as isolation_module
from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import ToolCall
from syndica.kernel.driving import _DRIVING_AGENT  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.isolation import CopyTicket, LocalIsolator
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tools import Delegate
from syndica.kernel.tools.local import Write
from syndica.kernel.versions import VersionStore
from syndica.kernel.workspace_router import WorkspaceRouter

T = TypeVar('T')
_GIT = shutil.which('git')
_TIMEOUT = 5.0
needs_git = pytest.mark.skipif(_GIT is None, reason='需要 git 做三方合并')


def _digest(data: bytes) -> str:
    return f'sha256:{hashlib.sha256(data).hexdigest()}'


async def _as(agent: str, call: Callable[[], Awaitable[T]]) -> T:
    async def driven() -> T:
        _DRIVING_AGENT.set(agent)
        return await call()

    return await asyncio.create_task(driven())


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / 'ws'
    folder.mkdir()
    (folder / 'notes.md').write_text('one\ntwo\nthree\n')
    (folder / 'src').mkdir()
    (folder / 'src' / 'app.py').write_text('print(1)\n')
    return folder


def _isolator(tmp_path: Path, *, git: str | None = _GIT) -> LocalIsolator:
    scratch = tmp_path / 'scratch'
    scratch.mkdir(exist_ok=True)
    return LocalIsolator(shell='/bin/sh', env={}, git=git, scratch=str(scratch) if git else None)


@pytest.fixture
async def router(root: Path, tmp_path: Path) -> AsyncIterator[WorkspaceRouter]:
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    routed = WorkspaceRouter(
        local,
        versions=VersionStore(root, redactor=RuleRedactor()),
        isolator=_isolator(tmp_path),
    )
    yield routed
    await routed.close()


def test_a_copy_holds_the_parents_files_but_not_git_syndica_or_symlinks(
    root: Path, tmp_path: Path
) -> None:
    (root / '.git').mkdir()
    (root / '.git' / 'config').write_text('[core]\n')
    (root / '.syndica' / 'traces').mkdir(parents=True)
    (root / '.syndica' / 'traces' / 't.jsonl').write_text('{}\n')
    (root / 'link.md').symlink_to(root / 'notes.md')

    copy = _isolator(tmp_path, git=None).isolate(root, 'alpha', 'snapshot')

    copied = sorted(
        path.relative_to(copy.location).as_posix()
        for path in copy.location.rglob('*')
        if path.is_file()
    )
    assert copied == ['notes.md', 'src/app.py']
    assert copy.base == {
        'notes.md': _digest(b'one\ntwo\nthree\n'),
        'src/app.py': _digest(b'print(1)\n'),
    }


async def test_an_isolated_agent_writes_into_its_copy_and_the_parent_waits_for_the_merge(
    router: WorkspaceRouter, root: Path
) -> None:
    copy = router.isolate('alpha', 'worktree')
    assert copy is not None

    await _as('alpha', lambda: router.write_text('fresh.md', 'from alpha'))

    assert (copy.location / 'fresh.md').read_text() == 'from alpha'
    assert not (root / 'fresh.md').exists()
    report = await router.merge_isolated('alpha')
    assert (root / 'fresh.md').read_text() == 'from alpha'
    assert report.merged == ('fresh.md',)
    assert not copy.location.exists()


async def test_a_task_started_inside_an_isolated_agent_still_lands_in_its_copy(
    router: WorkspaceRouter, root: Path
) -> None:
    copy = router.isolate('alpha', 'snapshot')
    assert copy is not None

    async def spawns_a_task() -> None:
        await asyncio.create_task(router.write_text('side.md', 'from a child task'))

    await _as('alpha', spawns_a_task)

    assert (copy.location / 'side.md').exists()
    assert not (root / 'side.md').exists()


async def test_the_host_reads_each_agents_terminal_in_the_workspace_that_agent_works_in(
    router: WorkspaceRouter,
) -> None:
    router.isolate('alpha', 'snapshot')
    router.adopt('bravo', None)
    opened = {
        agent: await _as(
            agent, lambda agent=agent: router.pty_create(f'echo from-{agent}; sleep 5', cwd='.')
        )
        for agent in ('alpha', 'bravo')
    }

    for agent, handle in opened.items():
        captured = await router.pty_capture_of(agent, handle, wait=2.0)
        assert f'from-{agent}' in captured.text
    with pytest.raises(ConfigError):
        await router.pty_capture_of('bravo', opened['alpha'])


async def test_merge_applies_edits_creations_and_deletions_the_parent_left_alone(
    router: WorkspaceRouter, root: Path
) -> None:
    router.isolate('alpha', 'snapshot')
    await _as('alpha', lambda: router.read_text('notes.md', offset=1, limit=9))
    await _as('alpha', lambda: router.write_text('notes.md', 'rewritten\n'))
    await _as('alpha', lambda: router.write_text('src/new.py', 'x = 1\n'))
    await _as('alpha', lambda: router.read_text('src/app.py', offset=1, limit=9))
    await _as('alpha', lambda: router.remove('src/app.py'))

    report = await router.merge_isolated('alpha')

    assert report.merged == ('notes.md', 'src/app.py', 'src/new.py')
    assert report.conflicts == ()
    assert (root / 'notes.md').read_text() == 'rewritten\n'
    assert (root / 'src' / 'new.py').read_text() == 'x = 1\n'
    assert not (root / 'src' / 'app.py').exists()


async def test_merged_files_are_recorded_in_the_version_layer_under_the_child(
    router: WorkspaceRouter,
) -> None:
    router.isolate('alpha', 'snapshot')
    await _as('alpha', lambda: router.write_text('fresh.md', 'from alpha'))

    await router.merge_isolated('alpha')

    assert router.versions is not None
    assert [entry.producer for entry in router.versions.history('fresh.md')] == ['alpha']


@needs_git
async def test_both_sides_changing_different_lines_merge_cleanly(
    router: WorkspaceRouter, root: Path
) -> None:
    router.isolate('alpha', 'worktree')
    await _as('alpha', lambda: router.read_text('notes.md', offset=1, limit=9))
    await _as('alpha', lambda: router.write_text('notes.md', 'ONE\ntwo\nthree\n'))
    (root / 'notes.md').write_text('one\ntwo\nTHREE\n')

    report = await router.merge_isolated('alpha')

    assert (root / 'notes.md').read_text() == 'ONE\ntwo\nTHREE\n'
    assert (report.merged, report.conflicts) == (('notes.md',), ())


@needs_git
async def test_both_sides_changing_the_same_line_is_a_conflict_that_keeps_theirs_beside_it(
    router: WorkspaceRouter, root: Path
) -> None:
    router.isolate('alpha', 'worktree')
    await _as('alpha', lambda: router.read_text('notes.md', offset=1, limit=9))
    await _as('alpha', lambda: router.write_text('notes.md', 'one\nCHILD\nthree\n'))
    (root / 'notes.md').write_text('one\nPARENT\nthree\n')

    report = await router.merge_isolated('alpha')

    [conflict] = report.conflicts
    assert (conflict.path, conflict.theirs) == ('notes.md', 'notes.md.alpha.theirs')
    assert (root / 'notes.md').read_text() == 'one\nPARENT\nthree\n'
    assert (root / 'notes.md.alpha.theirs').read_text() == 'one\nCHILD\nthree\n'
    assert '.theirs' in report.summary()


async def test_a_snapshot_treats_any_two_sided_change_as_a_conflict(
    router: WorkspaceRouter, root: Path
) -> None:
    router.isolate('alpha', 'snapshot')
    await _as('alpha', lambda: router.read_text('notes.md', offset=1, limit=9))
    await _as('alpha', lambda: router.write_text('notes.md', 'ONE\ntwo\nthree\n'))
    (root / 'notes.md').write_text('one\ntwo\nTHREE\n')

    report = await router.merge_isolated('alpha')

    assert [conflict.path for conflict in report.conflicts] == ['notes.md']
    assert (root / 'notes.md').read_text() == 'one\ntwo\nTHREE\n'


async def test_without_git_a_worktree_falls_back_to_a_snapshot_and_says_so(
    root: Path, tmp_path: Path
) -> None:
    copy = _isolator(tmp_path, git=None).isolate(root, 'alpha', 'worktree')

    assert copy.strategy == 'snapshot'
    assert copy.base_dir is None
    assert '没有 git' in copy.note
    assert '.syndica/snapshots/alpha' in copy.location.as_posix()


@pytest.mark.parametrize('child_deletes', [False, True], ids=['child_rewrote', 'child_deleted'])
async def test_a_parent_change_landing_during_the_merge_is_a_conflict_not_an_overwrite(
    root: Path, tmp_path: Path, child_deletes: bool
) -> None:
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    isolator = _isolator(tmp_path, git=None)
    copy = isolator.isolate(root, 'alpha', 'snapshot')
    if child_deletes:
        (copy.location / 'notes.md').unlink()
    else:
        (copy.location / 'notes.md').write_text('child\n')

    class _RacingParent:
        def __getattr__(self, name: str) -> Any:
            return getattr(local, name)

        async def stat(self, path: str) -> Any:
            answer = await local.stat(path)
            (root / path).write_text('parent got there first\n')
            return answer

    try:
        report = await isolator.merge(copy, _RacingParent(), lambda *_: None)  # pyright: ignore[reportArgumentType]
    finally:
        await copy.discard()
        await local.close()

    assert [conflict.path for conflict in report.conflicts] == ['notes.md']
    assert (root / 'notes.md').read_text() == 'parent got there first\n'


@needs_git
async def test_the_three_way_merge_runs_outside_the_workspace_with_no_config_or_git_variables(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    real = asyncio.create_subprocess_exec

    async def spy(*arguments: Any, **options: Any) -> Any:
        seen['arguments'] = arguments
        seen['options'] = options
        return await real(*arguments, **options)

    monkeypatch.setattr(isolation_module.asyncio, 'create_subprocess_exec', spy)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    isolator = LocalIsolator(
        shell='/bin/sh',
        env={'GIT_DIR': str(root / '.git'), 'GIT_CONFIG_PARAMETERS': "'core.x=y'", 'LANG': 'C'},
        git=_GIT,
        scratch=str(scratch),
    )

    merged = await isolator._merge_file(b'a\nb\nc\n', b'A\nb\nc\n', b'a\nb\nC\n')  # pyright: ignore[reportPrivateUsage]

    environment = seen['options']['env']
    cwd = Path(seen['options']['cwd'])
    assert merged == b'A\nb\nC\n'
    assert seen['arguments'][1:] == ('merge-file', '-p', 'ours', 'base', 'theirs')
    assert cwd.parent == scratch and not cwd.is_relative_to(root)
    assert not [
        key
        for key in environment
        if key.startswith('GIT_')
        and key
        not in {
            'GIT_CONFIG_NOSYSTEM',
            'GIT_CONFIG_GLOBAL',
            'GIT_CEILING_DIRECTORIES',
        }
    ]
    assert (
        environment['GIT_CONFIG_NOSYSTEM'],
        environment['GIT_CONFIG_GLOBAL'],
        environment['GIT_CEILING_DIRECTORIES'],
        environment['HOME'],
    ) == ('1', os.devnull, str(cwd), str(cwd))
    assert environment['LANG'] == 'C'
    assert _gone(cwd)


def _gone(path: Path) -> bool:
    return not path.exists()


async def test_discard_ends_processes_running_in_the_copy_and_removes_it(
    router: WorkspaceRouter,
) -> None:
    copy = router.isolate('alpha', 'worktree')
    assert copy is not None
    await _as('alpha', lambda: router.start('echo $$ > pid; sleep 300', cwd='.'))
    for _ in range(100):
        if (copy.location / 'pid').is_file() and (copy.location / 'pid').read_text().strip():
            break
        await asyncio.sleep(0.02)
    pid = int((copy.location / 'pid').read_text())

    await router.discard_isolated('alpha')

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not copy.location.exists()
    assert copy.base_dir is None or not copy.base_dir.exists()


async def test_releasing_an_agent_with_unmerged_changes_logs_what_was_lost(
    router: WorkspaceRouter, caplog: pytest.LogCaptureFixture
) -> None:
    router.isolate('alpha', 'snapshot')
    await _as('alpha', lambda: router.write_text('lost.md', 'never merged'))
    await _as('alpha', lambda: router.read_text('lost.md', offset=1, limit=1))

    with caplog.at_level(logging.WARNING, logger='syndica.kernel.workspace_router'):
        await router.release('alpha')

    [record] = caplog.records
    assert record.args == ('alpha', 1, ['lost.md'])
    assert router.isolated('alpha') is None


async def test_release_forgets_what_the_agent_had_read(router: WorkspaceRouter) -> None:
    await _as('alpha', lambda: router.read_text('notes.md', offset=1, limit=1))

    await router.release('alpha')
    refused = await _as('alpha', lambda: _conflict(router.write_text('notes.md', 'x')))

    assert refused.unread is True


async def _conflict(call: Awaitable[object]) -> Any:
    from syndica.kernel.workspace import WriteConflict

    try:
        await call
    except WriteConflict as conflict:
        return conflict
    raise AssertionError('期望 WriteConflict，实际写成了')


def test_a_tree_over_the_cap_is_refused_with_a_way_out(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(isolation_module, '_MAX_FILES', 1)

    with pytest.raises(ConfigError) as refused:
        _isolator(tmp_path, git=None).isolate(root, 'alpha', 'snapshot')

    assert 'shared' in refused.value.next_action


async def test_isolating_through_a_router_without_an_isolator_says_how_to_add_one(
    root: Path,
) -> None:
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    try:
        with pytest.raises(ConfigError) as refused:
            WorkspaceRouter(local).isolate('alpha', 'worktree')
    finally:
        await local.close()

    assert 'isolator' in refused.value.next_action


@pytest.mark.parametrize(
    ('options', 'anchor'),
    [
        ({'git': 'git', 'scratch': '/tmp'}, 'git'),
        ({'git': '/usr/bin/git'}, 'scratch'),
        ({'git': '/usr/bin/git', 'scratch': 'tmp'}, 'scratch'),
    ],
    ids=['relative_git', 'git_without_scratch', 'relative_scratch'],
)
def test_the_isolator_refuses_paths_it_cannot_trust(options: dict[str, str], anchor: str) -> None:
    with pytest.raises(ConfigError) as refused:
        LocalIsolator(shell='/bin/sh', env={}, **options)

    assert anchor in refused.value.expected


def _writer(router: WorkspaceRouter, path: str, content: str) -> AgentFactory:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='w1',
                        name='Write',
                        arguments=f'{{"path": "{path}", "content": "{content}"}}',
                    )
                ]
            ),
            ScriptedTurn(content='wrote it'),
        ]
    )

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        tools: dict[str, Tool] = {'Write': Write(router)}
        return BaseAgent(
            AgentOptions(name='Writer', llm=llm, tools=tools, system_prompt='SP'),
            hooks=hooks,
            events=events,
        )

    return build


def _idle(router: WorkspaceRouter) -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Boss', llm=ScriptedLLM([ScriptedTurn(content='idle')]), system_prompt='SP'
            ),
            hooks=hooks,
            events=events,
        )

    return build


async def test_spawn_with_isolation_routes_writes_into_the_copy_and_says_so_in_the_first_message(
    router: WorkspaceRouter, root: Path
) -> None:
    runtime = Runtime(workspace=router)
    try:
        agent_id = await runtime.spawn(
            _writer(router, 'out.md', 'isolated'), query='write it', isolation='worktree'
        )
        await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
        copy = router.isolated(agent_id)
        assert copy is not None
        first = runtime._table.get(agent_id).context.current_traj[0]  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]

        assert not (root / 'out.md').exists()
        assert (copy.location / 'out.md').read_text() == 'isolated'
        assert isinstance(first.content, str)
        assert first.content.startswith('（环境）你在一份隔离的工作副本里')
        assert first.content.endswith('write it')
        report = await runtime.merge_isolated(agent_id)
        assert (root / 'out.md').read_text() == 'isolated'
        assert report.merged == ('out.md',)
    finally:
        await runtime.shutdown()


async def test_merging_an_agent_that_is_still_running_is_refused(router: WorkspaceRouter) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class _Hold(Tool):
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            """挂住这一轮。"""
            entered.set()
            await release.wait()
            return ToolResult(output='released')

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        llm = ScriptedLLM(
            [
                ScriptedTurn(tool_calls=[ToolCall(id='h', name='Hold', arguments='{}')]),
                ScriptedTurn(content='done'),
            ]
        )
        return BaseAgent(
            AgentOptions(name='Holder', llm=llm, tools={'Hold': _Hold()}, system_prompt='SP'),
            hooks=hooks,
            events=events,
        )

    runtime = Runtime(workspace=router)
    try:
        agent_id = await runtime.spawn(build, query='go', isolation='snapshot')
        await asyncio.wait_for(entered.wait(), timeout=_TIMEOUT)

        with pytest.raises(ConfigError) as refused:
            await runtime.merge_isolated(agent_id)

        assert 'wait_for' in refused.value.next_action
        assert router.isolated(agent_id) is not None
        release.set()
    finally:
        await runtime.shutdown()


async def test_spawn_copies_in_the_runtimes_own_bounded_pool(
    router: WorkspaceRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[str] = []
    real = LocalIsolator.prepare

    def recording(self: LocalIsolator, *args: Any, **kwargs: Any) -> Any:
        threads.append(threading.current_thread().name)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(LocalIsolator, 'prepare', recording)
    runtime = Runtime(workspace=router)
    try:
        for _ in range(3):
            agent_id = await runtime.spawn(
                _writer(router, 'out.md', 'x'), query='go', isolation='snapshot'
            )
            await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
    finally:
        await runtime.shutdown()
    assert len(threads) == 3
    assert all(name.startswith('syndica-copy') for name in threads), threads


async def test_despawn_discards_the_copy(router: WorkspaceRouter) -> None:
    runtime = Runtime(workspace=router)
    try:
        agent_id = await runtime.spawn(
            _writer(router, 'out.md', 'x'), query='go', isolation='snapshot'
        )
        await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
        copy = router.isolated(agent_id)
        assert copy is not None

        await runtime.despawn(agent_id)

        assert router.isolated(agent_id) is None
        assert not copy.location.exists()
    finally:
        await runtime.shutdown()


async def test_spawn_with_isolation_on_a_runtime_that_cannot_isolate_says_what_to_pass(
    root: Path,
) -> None:
    runtime = Runtime()
    try:
        with pytest.raises(ConfigError) as refused:
            await runtime.spawn(
                _idle(WorkspaceRouter(LocalWorkspace(str(root), shell='/bin/sh', env={}))),
                isolation='worktree',
            )
    finally:
        await runtime.shutdown()

    assert 'isolator' in refused.value.next_action


async def test_delegate_with_isolation_merges_the_childs_work_back_and_reports_it(
    router: WorkspaceRouter, root: Path
) -> None:
    runtime = Runtime(workspace=router, agents={'Writer': _writer(router, 'report.md', 'done')})
    try:
        parent = await runtime.spawn(_idle(router), query='go')
        await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

        result = await asyncio.wait_for(
            Delegate(runtime, parent).execute(agent='Writer', query='write', isolation='worktree'),
            timeout=_TIMEOUT,
        )

        assert (root / 'report.md').read_text() == 'done'
        assert result.meta['merged'] == ['report.md']
        assert '合并回 1 个文件：report.md' in str(result.output)
        assert router.isolated(result.meta['agent_id']) is None
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize(
    ('isolation', 'anchor'),
    [('sandbox', 'shared / worktree / snapshot'), ('worktree', '省略 isolation')],
    ids=['unknown_value', 'runtime_cannot_isolate'],
)
async def test_delegate_refuses_an_isolation_it_cannot_give_and_says_what_to_do(
    root: Path, isolation: str, anchor: str
) -> None:
    runtime = Runtime(
        agents={
            'Writer': _idle(WorkspaceRouter(LocalWorkspace(str(root), shell='/bin/sh', env={})))
        }
    )
    try:
        parent = await runtime.spawn(
            _idle(WorkspaceRouter(LocalWorkspace(str(root), shell='/bin/sh', env={}))), query='go'
        )
        await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

        refused = await Delegate(runtime, parent).execute(
            agent='Writer', query='x', isolation=isolation
        )
    finally:
        await runtime.shutdown()

    assert anchor in str(refused.output)


async def test_a_case_only_rename_in_the_copy_is_a_conflict_and_deletes_nothing(
    router: WorkspaceRouter, root: Path
) -> None:
    copy = router.isolate('alpha', 'snapshot')
    assert copy is not None
    os.rename(copy.location / 'notes.md', copy.location / 'NOTES.md')

    report = await router.merge_isolated('alpha')

    assert {conflict.path.casefold() for conflict in report.conflicts} == {'notes.md'}
    assert report.merged == ()
    assert (root / 'notes.md').read_text() == 'one\ntwo\nthree\n'


async def test_calls_from_an_agent_whose_copy_is_merging_are_refused(
    router: WorkspaceRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    router.isolate('alpha', 'snapshot')
    await _as('alpha', lambda: router.write_text('fresh.md', 'from alpha'))
    refusals: list[str] = []
    isolator = router._isolator  # pyright: ignore[reportPrivateUsage]
    assert isolator is not None
    real_merge = isolator.merge

    async def merge_with_a_late_write(*args: Any, **kwargs: Any) -> Any:
        try:
            await _as('alpha', lambda: router.write_text('late.md', 'too late'))
        except ConfigError as exc:
            refusals.append(exc.received)
        return await real_merge(*args, **kwargs)

    monkeypatch.setattr(isolator, 'merge', merge_with_a_late_write)
    report = await router.merge_isolated('alpha')

    assert report.merged == ('fresh.md',)
    assert len(refusals) == 1 and '正在合并' in refusals[0]


async def test_a_change_landing_in_the_copy_after_the_snapshot_is_reported_as_lost(
    router: WorkspaceRouter, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = router.isolate('alpha', 'snapshot')
    assert copy is not None
    (copy.location / 'fresh.md').write_text('merged')
    isolator = router._isolator  # pyright: ignore[reportPrivateUsage]
    assert isolator is not None
    real_apply = isolator._apply  # pyright: ignore[reportPrivateUsage]

    async def apply_then_something_writes(*args: Any, **kwargs: Any) -> Any:
        await real_apply(*args, **kwargs)
        (copy.location / 'stray.md').write_text('written by an escaped process')

    monkeypatch.setattr(isolator, '_apply', apply_then_something_writes)
    with caplog.at_level(logging.WARNING, logger='syndica.kernel.workspace_router'):
        await router.merge_isolated('alpha')

    [record] = caplog.records
    assert record.args == ('alpha', 1, ['stray.md'])


async def test_a_taken_side_file_name_moves_to_the_next_one_and_keeps_the_old_file(
    router: WorkspaceRouter, root: Path
) -> None:
    router.isolate('alpha', 'snapshot')
    await _as('alpha', lambda: router.read_text('notes.md', offset=1, limit=9))
    await _as('alpha', lambda: router.write_text('notes.md', 'child\n'))
    (root / 'notes.md').write_text('parent\n')
    (root / 'notes.md.alpha.theirs').write_text('left over\n')

    report = await router.merge_isolated('alpha')

    [conflict] = report.conflicts
    assert conflict.theirs == 'notes.md.alpha.2.theirs'
    assert (root / 'notes.md.alpha.2.theirs').read_text() == 'child\n'
    assert (root / 'notes.md.alpha.theirs').read_text() == 'left over\n'


async def test_a_conflict_whose_child_version_cannot_be_saved_keeps_the_copy(
    router: WorkspaceRouter, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = router.isolate('alpha', 'snapshot')
    assert copy is not None
    await _as('alpha', lambda: router.read_text('notes.md', offset=1, limit=9))
    await _as('alpha', lambda: router.write_text('notes.md', 'child\n'))
    (root / 'notes.md').write_text('parent\n')
    monkeypatch.setattr(isolation_module, '_SIDE_ATTEMPTS', 1)
    (root / 'notes.md.alpha.theirs').write_text('left over\n')

    report = await router.merge_isolated('alpha')

    assert report.kept == str(copy.location)
    assert (copy.location / 'notes.md').read_text() == 'child\n'
    assert router.isolated('alpha') is copy
    assert '副本留在' in report.summary()


async def test_a_path_that_cannot_be_written_back_is_a_conflict_and_the_rest_still_merge(
    router: WorkspaceRouter, root: Path
) -> None:
    copy = router.isolate('alpha', 'snapshot')
    assert copy is not None
    shutil.rmtree(copy.location / 'src')
    (copy.location / 'src').write_text('now a file\n')
    (copy.location / 'aa.md').write_text('a')
    (copy.location / 'zz.md').write_text('z')

    report = await router.merge_isolated('alpha')

    assert report.merged == ('aa.md', 'src/app.py', 'zz.md')
    [conflict] = report.conflicts
    assert (conflict.path, conflict.theirs) == ('src', 'src.alpha.theirs')
    assert (root / 'src.alpha.theirs').read_text() == 'now a file\n'
    assert (root / 'zz.md').read_text() == 'z'


def test_a_git_file_is_not_copied_like_a_git_directory(root: Path, tmp_path: Path) -> None:
    (root / 'sub').mkdir()
    (root / 'sub' / '.git').write_text('gitdir: ../.git/modules/sub\n')

    copy = _isolator(tmp_path, git=None).isolate(root, 'alpha', 'snapshot')

    assert 'sub/.git' not in copy.base


def test_the_copy_keeps_executable_bits(root: Path, tmp_path: Path) -> None:
    (root / 'run.sh').write_text('#!/bin/sh\necho ok\n')
    os.chmod(root / 'run.sh', 0o755)

    copy = _isolator(tmp_path, git=None).isolate(root, 'alpha', 'snapshot')

    assert os.stat(copy.location / 'run.sh').st_mode & 0o777 == 0o755


def test_a_failed_copy_leaves_nothing_behind(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Path.read_bytes

    def failing(self: Path) -> bytes:
        if self.name == 'app.py':
            raise PermissionError('denied')
        return real(self)

    monkeypatch.setattr(Path, 'read_bytes', failing)
    with pytest.raises(PermissionError):
        _isolator(tmp_path).isolate(root, 'alpha', 'worktree')

    monkeypatch.setattr(Path, 'read_bytes', real)
    assert sorted(path.name for path in (root / '.syndica').rglob('*')) in (
        [],
        ['.gitignore', 'worktrees'],
    )


def test_isolating_an_id_again_does_not_stack_on_a_stale_copy(root: Path, tmp_path: Path) -> None:
    stale = root / '.syndica' / 'snapshots' / 'alpha'
    stale.mkdir(parents=True)
    (stale / 'leftover.md').write_text('from an earlier run')

    copy = _isolator(tmp_path, git=None).isolate(root, 'alpha', 'snapshot')

    assert not (copy.location / 'leftover.md').exists()
    assert copy.changed() == []


async def test_a_shared_child_of_an_isolated_agent_works_in_its_parents_copy(
    router: WorkspaceRouter, root: Path
) -> None:
    copy = router.isolate('parent', 'snapshot')
    assert copy is not None
    router.adopt('child', 'parent')

    await _as('child', lambda: router.write_text('by-child.md', 'x'))

    assert (copy.location / 'by-child.md').exists()
    assert not (root / 'by-child.md').exists()


async def test_a_nested_copy_is_taken_from_and_merged_into_the_parents_copy(
    router: WorkspaceRouter, root: Path
) -> None:
    outer = router.isolate('parent', 'snapshot')
    assert outer is not None
    await _as('parent', lambda: router.write_text('parent-only.md', 'p'))
    router.adopt('child', 'parent')
    inner = router.isolate('child', 'snapshot')
    assert inner is not None

    await _as('child', lambda: router.write_text('child-only.md', 'c'))
    await router.merge_isolated('child')

    assert 'parent-only.md' in inner.base
    assert (outer.location / 'child-only.md').read_text() == 'c'
    assert not (root / 'child-only.md').exists()


@pytest.mark.parametrize('status', ['paused', 'waiting', 'aborting'])
async def test_merging_an_agent_that_is_mid_run_in_any_form_is_refused(
    router: WorkspaceRouter, status: str
) -> None:
    from syndica.kernel.agent_types import AgentStatus

    runtime = Runtime(workspace=router)
    try:
        agent_id = await runtime.spawn(
            _writer(router, 'out.md', 'x'), query='go', isolation='snapshot'
        )
        await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
        moved = await runtime.registry.compare_and_set_status(
            agent_id, expected=AgentStatus.ZOMBIE, new=AgentStatus(status)
        )
        assert moved

        with pytest.raises(ConfigError) as refused:
            await runtime.merge_isolated(agent_id)
    finally:
        await runtime.shutdown()

    assert status in refused.value.received
    assert 'pause' not in refused.value.next_action


async def test_spawn_rolls_back_when_the_copy_cannot_be_made(
    router: WorkspaceRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(workspace=router)
    try:
        parent = await runtime.spawn(_idle(router), query='go')
        await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)
        monkeypatch.setattr(isolation_module, '_MAX_FILES', 0)
        with pytest.raises(ConfigError):
            await runtime.spawn(
                _writer(router, 'out.md', 'x'),
                query='go',
                isolation='snapshot',
                parent_id=parent,
            )
        listed = [info.agent_id for info in await runtime.registry.list()]
    finally:
        await runtime.shutdown()

    assert listed == [parent]
    assert list(router._parents) == []  # pyright: ignore[reportPrivateUsage]


async def test_a_process_running_in_the_copy_is_stopped_before_the_merge_reads_it(
    router: WorkspaceRouter,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy = router.isolate('alpha', 'snapshot')
    assert copy is not None
    (copy.location / 'fresh.md').write_text('merged')
    await _as(
        'alpha', lambda: router.start('while :; do echo x >> ticker; sleep 0.01; done', cwd='.')
    )
    for _ in range(100):
        if (copy.location / 'ticker').exists():
            break
        await asyncio.sleep(0.02)
    isolator = router._isolator  # pyright: ignore[reportPrivateUsage]
    assert isolator is not None
    real_apply = isolator._apply  # pyright: ignore[reportPrivateUsage]

    async def slow_apply(*args: Any, **kwargs: Any) -> Any:
        await real_apply(*args, **kwargs)
        await asyncio.sleep(0.2)

    monkeypatch.setattr(isolator, '_apply', slow_apply)
    with caplog.at_level(logging.WARNING, logger='syndica.kernel.workspace_router'):
        await router.merge_isolated('alpha')

    assert caplog.records == []


async def test_the_three_way_write_back_keeps_its_precondition_across_the_merge(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    isolator = LocalIsolator(shell='/bin/sh', env={}, git='/usr/bin/false', scratch=str(scratch))
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    copy = isolator.isolate(root, 'alpha', 'worktree')
    (copy.location / 'notes.md').write_text('ONE\ntwo\nthree\n')
    (root / 'notes.md').write_text('one\ntwo\nTHREE\n')

    async def merge_while_the_parent_changes(*_: bytes) -> bytes:
        (root / 'notes.md').write_text('changed during the merge\n')
        return b'ONE\ntwo\nTHREE\n'

    monkeypatch.setattr(isolator, '_merge_file', merge_while_the_parent_changes)
    try:
        report = await isolator.merge(copy, local, lambda *_: None)
    finally:
        await copy.discard()
        await local.close()

    assert [conflict.path for conflict in report.conflicts] == ['notes.md']
    assert (root / 'notes.md').read_text() == 'changed during the merge\n'


def test_a_symlinked_copy_directory_is_refused_and_nothing_is_copied_outside(
    root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    (root / '.syndica').mkdir()
    (root / '.syndica' / 'snapshots').symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigError) as refused:
        _isolator(tmp_path, git=None).isolate(root, 'alpha', 'snapshot')

    assert '符号链接' in refused.value.received
    assert list(outside.iterdir()) == []


def _executable(path: Path) -> bool:
    return bool(path.stat().st_mode & 0o111)


async def test_a_chmod_in_the_copy_is_merged_back_even_though_the_content_is_unchanged(
    router: WorkspaceRouter, root: Path
) -> None:
    copy = router.isolate('alpha', 'snapshot')
    assert copy is not None
    await _as('alpha', lambda: router.set_executable('src/app.py', True))

    report = await router.merge_isolated('alpha')

    assert report.merged == ('src/app.py',)
    assert _executable(root / 'src' / 'app.py')


async def test_a_new_executable_script_lands_executable(
    router: WorkspaceRouter, root: Path
) -> None:
    copy = router.isolate('alpha', 'worktree')
    assert copy is not None
    await _as('alpha', lambda: router.write_text('run.sh', 'echo ran\n'))
    await _as('alpha', lambda: router.set_executable('run.sh', True))

    await router.merge_isolated('alpha')

    assert _executable(root / 'run.sh')


async def test_clearing_the_bit_in_the_copy_clears_it_in_the_parent(
    router: WorkspaceRouter, root: Path
) -> None:
    os.chmod(root / 'notes.md', 0o755)
    copy = router.isolate('alpha', 'snapshot')
    assert copy is not None
    await _as('alpha', lambda: router.set_executable('notes.md', False))

    await router.merge_isolated('alpha')

    assert not _executable(root / 'notes.md')


async def test_the_parents_own_chmod_survives_a_content_merge(
    router: WorkspaceRouter, root: Path
) -> None:
    # 可执行位只在子 Agent 改过时写回：父那边自己 `chmod +x` 过、子 Agent 只改内容，合并不撤销它。
    copy = router.isolate('alpha', 'worktree')
    assert copy is not None
    os.chmod(root / 'src' / 'app.py', 0o755)
    await _as('alpha', lambda: router.read_text('src/app.py', offset=1, limit=10))
    await _as('alpha', lambda: router.write_text('src/app.py', 'print(2)\n'))

    await router.merge_isolated('alpha')

    assert (root / 'src' / 'app.py').read_text() == 'print(2)\n'
    assert _executable(root / 'src' / 'app.py')


def test_a_grandchild_of_an_isolated_agent_shares_its_scope(
    router: WorkspaceRouter, root: Path
) -> None:
    # 产物 id 的作用域沿父链找最近一个有副本的祖先：孙 Agent 不能落回根的空作用域（变异 M16）。
    assert router.isolate('alpha', 'snapshot') is not None
    router.adopt('beta', 'alpha')
    router.adopt('gamma', 'beta')
    assert (router.scope_of('gamma'), router.scope_of('root-agent')) == ('alpha', '')


def test_a_file_that_disappears_during_the_copy_is_absent_not_an_error(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.read_bytes

    def flaky(self: Path) -> bytes:
        if self.name == 'notes.md':
            raise FileNotFoundError(self)
        return original(self)

    monkeypatch.setattr(Path, 'read_bytes', flaky)
    copy = _isolator(tmp_path).isolate(root, 'alpha', 'snapshot')
    assert 'notes.md' not in copy.base
    assert 'src/app.py' in copy.base
    assert not (copy.location / 'notes.md').exists()
    assert (copy.location / 'src' / 'app.py').is_file()


def test_a_file_that_disappears_between_listing_and_stat_is_absent_not_an_error(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / 'ghost.md').write_text('boo')
    original = Path.is_file

    def vanishing(self: Path) -> bool:
        present = original(self)
        if present and self.name == 'ghost.md':
            self.unlink()
        return present

    monkeypatch.setattr(Path, 'is_file', vanishing)
    copy = _isolator(tmp_path).isolate(root, 'alpha', 'snapshot')
    assert 'ghost.md' not in copy.base
    assert 'notes.md' in copy.base


async def test_a_cancelled_copy_leaves_no_scratch_and_no_route(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    isolator = _isolator(tmp_path)
    routed = WorkspaceRouter(
        local, versions=VersionStore(root, redactor=RuleRedactor()), isolator=isolator
    )
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = LocalIsolator.prepare

    def blocking(self: LocalIsolator, *args: Any, **kwargs: Any) -> Any:
        started.set()
        release.wait(2)
        try:
            return original(self, *args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(LocalIsolator, 'prepare', blocking)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        task = asyncio.create_task(routed.stage_isolation('alpha', 'snapshot', pool))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not list((root / '.syndica' / 'snapshots').glob('.alpha.building-*')):
                break
        assert list((root / '.syndica' / 'snapshots').glob('.alpha.building-*')) == []
        assert routed.isolated('alpha') is None
        monkeypatch.setattr(LocalIsolator, 'prepare', original)
        staged = await routed.stage_isolation('alpha', 'snapshot', pool)
        assert staged is not None and routed.finish_isolation(staged) is not None
    finally:
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)
        await routed.close()


def test_a_copy_cancelled_after_its_loop_is_gone_still_removes_its_scratch(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    original = LocalIsolator.prepare

    def blocking(self: LocalIsolator, *args: Any) -> Any:
        started.set()
        assert release.wait(5)
        return original(self, *args)

    monkeypatch.setattr(LocalIsolator, 'prepare', blocking)
    pool = ThreadPoolExecutor(max_workers=1)

    async def cancelled_while_copying() -> None:
        routed = WorkspaceRouter(
            LocalWorkspace(str(root), shell='/bin/sh', env={}),
            versions=VersionStore(root, redactor=RuleRedactor()),
            isolator=_isolator(tmp_path),
        )
        task = asyncio.create_task(routed.stage_isolation('alpha', 'snapshot', pool))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await routed.close()

    # 事件循环在拷贝线程拷完之前就结束了（Ctrl-C、关机）：线程自己负责删。
    asyncio.run(cancelled_while_copying())
    release.set()
    pool.shutdown(wait=True)
    assert list((root / '.syndica' / 'snapshots').glob('.alpha.building-*')) == []


def test_a_copy_handed_over_before_the_cancel_is_removed_by_the_canceller(
    root: Path, tmp_path: Path
) -> None:
    isolator = _isolator(tmp_path)
    ticket = CopyTicket()
    prepared = isolator.prepare(root, 'alpha', 'snapshot', isolator.begin('alpha'), None, ticket)
    assert all(scratch.exists() for _, scratch in prepared.building)
    ticket.cancel(isolator)
    assert all(not scratch.exists() for _, scratch in prepared.building)


def test_a_superseded_copy_is_deleted_instead_of_installed(root: Path, tmp_path: Path) -> None:
    isolator = _isolator(tmp_path)
    generation = isolator.begin('alpha')
    prepared = isolator.prepare(root, 'alpha', 'snapshot', generation)
    scratches = [scratch for _, scratch in prepared.building]
    isolator.supersede('alpha', generation)
    assert isolator.commit(prepared) is None
    assert all(not scratch.exists() for scratch in scratches)


async def test_the_copy_runs_off_the_event_loop(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    routed = WorkspaceRouter(
        local,
        versions=VersionStore(root, redactor=RuleRedactor()),
        isolator=_isolator(tmp_path),
    )
    started = threading.Event()
    release = threading.Event()
    original = LocalIsolator.prepare

    def blocking(self: LocalIsolator, *args: Any, **kwargs: Any) -> Any:
        started.set()
        assert release.wait(2)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(LocalIsolator, 'prepare', blocking)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        task = asyncio.create_task(routed.stage_isolation('alpha', 'snapshot', pool))
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        release.set()
        staged = await asyncio.wait_for(task, _TIMEOUT)
        assert staged is not None
        copy = routed.finish_isolation(staged)
        assert copy is not None
        assert (copy.location / 'src' / 'app.py').is_file()
    finally:
        release.set()
        pool.shutdown(wait=False, cancel_futures=True)
        await routed.close()
