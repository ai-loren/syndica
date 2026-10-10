"""文件污点与产物边（M5 D9、D13）：读到的文件内容可信吗，以及「谁写的、谁读的」进 trace。"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TypeVar

import pytest

from syndica.kernel import file_taint
from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import (  # pyright: ignore[reportPrivateUsage]
    _CALL_TRUST,
    ToolCall,
    TrustLevel,
)
from syndica.kernel.driving import _DRIVING_AGENT  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.events import EventBus
from syndica.kernel.file_taint import FileTaint
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.isolation import LocalIsolator
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tools.local import Glob, Grep, Read, Write
from syndica.kernel.trace import AttributeValue, CausalityEdge, EdgeKind, SpanKind, SpanStatus
from syndica.kernel.tracing import NullTracer
from syndica.kernel.workspace_router import WorkspaceRouter

T = TypeVar('T')
_TIMEOUT = 5.0


async def _as(agent: str, call: Callable[[], Awaitable[T]]) -> T:
    async def driven() -> T:
        _DRIVING_AGENT.set(agent)
        return await call()

    return await asyncio.create_task(driven())


async def _under(trust: TrustLevel, call: Callable[[], Awaitable[T]]) -> T:
    async def tainted() -> T:
        _CALL_TRUST.set(trust)
        return await call()

    return await asyncio.create_task(tainted())


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / 'ws'
    folder.mkdir()
    (folder / 'notes.md').write_text('present at open\n')
    return folder


@pytest.fixture
async def router(root: Path) -> AsyncIterator[WorkspaceRouter]:
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    routed = WorkspaceRouter(local, taint=FileTaint(root))
    yield routed
    await routed.close()


async def test_a_file_present_at_open_and_untouched_reads_as_trusted(
    router: WorkspaceRouter,
) -> None:
    result = await Read(router).execute('notes.md')

    assert result.trust is TrustLevel.TRUSTED


async def test_a_file_changed_outside_the_tools_reads_as_untrusted(
    router: WorkspaceRouter, root: Path
) -> None:
    (root / 'notes.md').write_text('rewritten by a command\n')

    result = await Read(router).execute('notes.md')

    assert result.trust is TrustLevel.UNTRUSTED


async def test_setting_the_modification_time_back_does_not_restore_trust(
    router: WorkspaceRouter, root: Path
) -> None:
    before = os.stat(root / 'notes.md')
    (root / 'notes.md').write_text('PRESENT AT OPEN\n')
    os.utime(root / 'notes.md', ns=(before.st_atime_ns, before.st_mtime_ns))

    result = await Read(router).execute('notes.md')

    assert result.trust is TrustLevel.UNTRUSTED


async def test_a_same_size_rewrite_the_clock_did_not_register_is_judged_by_its_content(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = file_taint._fingerprint  # pyright: ignore[reportPrivateUsage]
    frozen: dict[Path, int] = {}

    def coarse(path: Path) -> tuple[int, int, int] | None:
        found = real(path)
        if found is None:
            return None
        inode, size, ctime = found
        return inode, size, frozen.setdefault(path, ctime)

    monkeypatch.setattr(file_taint, '_fingerprint', coarse)
    router = WorkspaceRouter(
        LocalWorkspace(str(root), shell='/bin/sh', env={}), taint=FileTaint(root)
    )

    async def trust() -> tuple[TrustLevel, ...]:
        read = await Read(router).execute('notes.md')
        listed = await Glob(router).execute('*.md')
        found = await Grep(router).execute('(?i)open')
        return read.trust, listed.trust, found.trust

    try:
        (root / 'notes.md').write_text('PRESENT AT OPEN\n')
        rewritten = await trust()
        (root / 'notes.md').write_text('present at open\n')
        restored = await trust()
    finally:
        await router.close()

    assert (rewritten, restored) == ((TrustLevel.UNTRUSTED,) * 3, (TrustLevel.TRUSTED,) * 3)


async def test_a_file_created_outside_the_tools_is_untrusted_in_read_glob_and_grep(
    router: WorkspaceRouter, root: Path
) -> None:
    (root / 'planted.md').write_text('needle from outside\n')

    read = await Read(router).execute('planted.md')
    listed = await Glob(router).execute('*.md')
    found = await Grep(router).execute('needle')

    assert (read.trust, listed.trust, found.trust) == (TrustLevel.UNTRUSTED,) * 3


async def test_grep_matching_only_trusted_files_stays_trusted(
    router: WorkspaceRouter, root: Path
) -> None:
    (root / 'planted.md').write_text('something else\n')

    found = await Grep(router).execute('present')

    assert found.trust is TrustLevel.TRUSTED


async def test_a_tool_write_carries_the_trust_of_the_call_that_made_it(
    router: WorkspaceRouter,
) -> None:
    await _under(TrustLevel.TRUSTED, lambda: Write(router).execute('clean.md', 'fine'))
    await _under(TrustLevel.UNTRUSTED, lambda: Write(router).execute('dirty.md', 'derived'))

    clean = await Read(router).execute('clean.md')
    dirty = await Read(router).execute('dirty.md')

    assert (clean.trust, dirty.trust) == (TrustLevel.TRUSTED, TrustLevel.UNTRUSTED)


async def test_writing_the_same_content_from_a_trusted_call_does_not_launder_it(
    router: WorkspaceRouter,
) -> None:
    await _under(TrustLevel.UNTRUSTED, lambda: Write(router).execute('derived.md', 'same'))
    await _under(TrustLevel.TRUSTED, lambda: Write(router).execute('derived.md', 'same'))

    result = await Read(router).execute('derived.md')

    assert result.trust is TrustLevel.UNTRUSTED


async def test_a_symlink_carries_the_trust_of_its_real_target(root: Path) -> None:
    # 路径按后端的方式（跟符号链接）解析后再判信任：指向基线文件的链接读到那份基线内容、可信；指向
    # 打开后新写的文件的链接读到新内容、不可信。两边都没有洗白空间（评审 F1）。
    (root / 'link.md').symlink_to('notes.md')
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    router = WorkspaceRouter(local, taint=FileTaint(root))
    (root / 'planted.md').write_text('needle from outside\n')
    (root / 'evil.md').symlink_to('planted.md')
    try:
        to_baseline = await Glob(router).execute('link.md')
        to_planted = await Glob(router).execute('evil.md')
    finally:
        await router.close()

    assert to_baseline.trust is TrustLevel.TRUSTED
    assert to_planted.trust is TrustLevel.UNTRUSTED


@pytest.mark.skipif(shutil.which('git') is None, reason='需要 git 做三方合并')
async def test_a_three_way_merge_with_an_untrusted_parent_side_is_untrusted(
    root: Path, tmp_path: Path
) -> None:
    (root / 'notes.md').write_text('one\ntwo\nthree\n')
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    router = WorkspaceRouter(
        local,
        taint=FileTaint(root),
        isolator=LocalIsolator(
            shell='/bin/sh', env={}, git=shutil.which('git'), scratch=str(scratch)
        ),
    )
    try:
        router.isolate('alpha', 'worktree')
        await _as('alpha', lambda: Read(router).execute('notes.md'))
        await _as('alpha', lambda: Write(router).execute('notes.md', 'ONE\ntwo\nthree\n'))
        (root / 'notes.md').write_text('one\ntwo\nTHREE\n')
        report = await router.merge_isolated('alpha')
        merged = await Read(router).execute('notes.md')
    finally:
        await router.close()

    assert report.merged == ('notes.md',)
    assert merged.trust is TrustLevel.UNTRUSTED


def test_a_workspace_over_the_cap_takes_a_partial_baseline_and_says_so(
    root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (root / 'second.md').write_text('x')

    with caplog.at_level(logging.WARNING, logger='syndica.kernel.file_taint'):
        taint = FileTaint(root, max_files=1)

    assert [record.args for record in caplog.records] == [(1, 1)]
    trusts = {name: taint.trust_of(name, None) for name in ('notes.md', 'second.md')}
    assert sorted(trusts.values(), key=lambda trust: trust.value) == [
        TrustLevel.TRUSTED,
        TrustLevel.UNTRUSTED,
    ]


async def test_a_router_without_taint_keeps_reads_trusted(root: Path) -> None:
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    (root / 'planted.md').write_text('from outside\n')
    try:
        result = await Read(WorkspaceRouter(local)).execute('planted.md')
    finally:
        await local.close()

    assert result.trust is TrustLevel.TRUSTED


async def test_a_merge_carries_the_childs_taint_into_the_shared_workspace(
    root: Path, tmp_path: Path
) -> None:
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    router = WorkspaceRouter(
        local,
        taint=FileTaint(root),
        isolator=LocalIsolator(shell='/bin/sh', env={}),
    )
    try:
        copy = router.isolate('alpha', 'snapshot')
        assert copy is not None
        await _as('alpha', lambda: Write(router).execute('written.md', 'by a tool'))
        (copy.location / 'planted.md').write_text('by a command\n')
        await router.merge_isolated('alpha')

        written = await Read(router).execute('written.md')
        planted = await Read(router).execute('planted.md')
    finally:
        await router.close()

    assert (written.trust, planted.trust) == (TrustLevel.TRUSTED, TrustLevel.UNTRUSTED)


async def test_an_untrusted_file_copied_into_an_isolated_copy_stays_untrusted_there(
    root: Path,
) -> None:
    (root / 'notes.md').write_text('changed before the copy\n')
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    router = WorkspaceRouter(
        local,
        taint=FileTaint(root),
        isolator=LocalIsolator(shell='/bin/sh', env={}),
    )
    (root / 'notes.md').write_text('changed after open\n')
    try:
        router.isolate('alpha', 'snapshot')
        result = await _as('alpha', lambda: Read(router).execute('notes.md'))
    finally:
        await router.close()

    assert result.trust is TrustLevel.UNTRUSTED


class _Tainted(Tool):
    __tool_command_args__ = ()

    async def execute(self, text: str) -> ToolResult:
        """回显 text，并标记为不可信。"""
        return ToolResult(output=text, trust=TrustLevel.UNTRUSTED)


def _agent(router: WorkspaceRouter, calls: list[ToolCall]) -> AgentFactory:
    llm = ScriptedLLM(
        [*(ScriptedTurn(tool_calls=[call]) for call in calls), ScriptedTurn(content='done')]
    )

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        tools: dict[str, Tool] = {
            'Tainted': _Tainted(),
            'Write': Write(router),
            'Read': Read(router),
        }
        return BaseAgent(
            AgentOptions(name='Worker', llm=llm, tools=tools, system_prompt='SP'),
            hooks=hooks,
            events=events,
        )

    return build


_WRITE = ToolCall(id='w', name='Write', arguments='{"path": "out.md", "content": "derived"}')
_READ = ToolCall(id='r', name='Read', arguments='{"path": "out.md"}')
_TAINT = ToolCall(id='t', name='Tainted', arguments='{"text": "from the web"}')


@pytest.mark.parametrize(
    ('calls', 'expected'),
    [([_WRITE, _READ], TrustLevel.TRUSTED), ([_TAINT, _WRITE, _READ], TrustLevel.UNTRUSTED)],
    ids=['clean_window', 'after_untrusted_content'],
)
async def test_a_file_written_after_reading_untrusted_content_reads_back_as_untrusted(
    router: WorkspaceRouter, calls: list[ToolCall], expected: TrustLevel
) -> None:
    runtime = Runtime(workspace=router)
    try:
        agent_id = await runtime.spawn(_agent(router, calls), query='go')
        await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
        agent = runtime._table.get(agent_id)  # pyright: ignore[reportPrivateUsage]
        assert agent is not None
        [read] = [
            block
            for block in agent.context.current_traj
            if block.role == 'tool' and block.tool_call_id == 'r'
        ]
    finally:
        await runtime.shutdown()

    assert read.trust is expected


class _Recorder(NullTracer):
    def __init__(self) -> None:
        self.spans: dict[str, tuple[SpanKind, str, dict[str, AttributeValue]]] = {}
        self.edges: list[CausalityEdge] = []

    def start_span(
        self,
        span_id: str,
        parent_span_id: str | None,
        kind: SpanKind,
        name: str,
        agent_id: str,
        attributes: dict[str, AttributeValue],
    ) -> None:
        self.spans[span_id] = (kind, name, dict(attributes))

    def end_span(
        self, span_id: str, status: SpanStatus, attributes: dict[str, AttributeValue]
    ) -> None:
        self.spans[span_id][2].update(attributes)

    def add_edge(self, edge: CausalityEdge) -> None:
        self.edges.append(edge)


def _tool_spans(recorder: _Recorder) -> dict[str, str]:
    return {
        str(attributes.get('tool.call_id')): span_id
        for span_id, (kind, _, attributes) in recorder.spans.items()
        if kind is SpanKind.TOOL_CALL
    }


async def test_reading_a_version_a_tool_wrote_links_the_two_calls_with_an_artifact_edge(
    router: WorkspaceRouter,
) -> None:
    recorder = _Recorder()
    runtime = Runtime(workspace=router, tracer=recorder)
    try:
        agent_id = await runtime.spawn(_agent(router, [_WRITE, _READ]), query='go')
        await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
    finally:
        await runtime.shutdown()

    spans = _tool_spans(recorder)
    [edge] = [edge for edge in recorder.edges if edge.kind is EdgeKind.ARTIFACT]
    assert (edge.source_span_id, edge.target_span_id) == (spans['w'], spans['r'])
    assert edge.attributes['artifact.path'] == 'out.md'
    assert edge.attributes['artifact.trust'] == 'trusted'
    assert str(edge.attributes['artifact.version']).startswith('sha256:')


async def test_reading_a_file_from_before_the_run_marks_its_origin_on_the_read(
    router: WorkspaceRouter,
) -> None:
    recorder = _Recorder()
    runtime = Runtime(workspace=router, tracer=recorder)
    read = ToolCall(id='r', name='Read', arguments='{"path": "notes.md"}')
    try:
        agent_id = await runtime.spawn(_agent(router, [read]), query='go')
        await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
    finally:
        await runtime.shutdown()

    attributes = recorder.spans[_tool_spans(recorder)['r']][2]
    assert (attributes['provenance.source'], attributes['provenance.trust']) == (
        'baseline',
        'trusted',
    )
    assert [edge for edge in recorder.edges if edge.kind is EdgeKind.ARTIFACT] == []
