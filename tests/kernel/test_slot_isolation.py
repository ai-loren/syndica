"""拓扑槽的隔离（M5 后续）：每个周期在共享工作区的拷贝里干活，周期结束时合并，冲突在下一个周期开头
以内核署名块告诉它（信任随合并那一刻的溯源），隔离的事实按周期键注入。"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import ContextBlock, StopReason, ToolCall, TrustLevel
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.history_blocks import HistoryKind, kind_of
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.isolation import LocalIsolator, MergeConflict, MergeReport
from syndica.kernel.llm import LLMConfig
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.runtime import Runtime
from syndica.kernel.tools.local import Bash, Write
from syndica.kernel.topology import AgentSlot, Topology
from syndica.kernel.workspace_router import WorkspaceRouter

_TIMEOUT = 10.0


class _Script:
    """按调用次序交回脚本里的一步；`gate` 给出的那一步之前等它放行。"""

    def __init__(self, steps: list[ContextBlock], gates: dict[int, asyncio.Event] | None = None):
        self.config = LLMConfig(provider='scripted', model='m')
        self._steps = steps
        self._gates = gates or {}
        self.calls = 0

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        index = self.calls
        self.calls += 1
        gate = self._gates.get(index)
        if gate is not None:
            await gate.wait()
        return self._steps[index]


def _write(call_id: str, path: str, content: str) -> ContextBlock:
    arguments = json.dumps({'path': path, 'content': content})
    return ContextBlock(
        role='assistant', tool_calls=[ToolCall(id=call_id, name='Write', arguments=arguments)]
    )


def _say(text: str) -> ContextBlock:
    return ContextBlock(role='assistant', content=text)


def _router(tmp_path: Path, *, isolator: bool = True) -> tuple[WorkspaceRouter, Path]:
    root = tmp_path / 'ws'
    root.mkdir()
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    if not isolator:
        return WorkspaceRouter(local), root
    return WorkspaceRouter(local, isolator=LocalIsolator(shell='/bin/sh', env={})), root


async def _slot_agent(
    router: WorkspaceRouter, llm: _Script, isolation: str = 'snapshot'
) -> tuple[Runtime, str]:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=llm,  # type: ignore[arg-type]
                tools={'Write': Write(router.portal())},
                system_prompt='SP',
            ),
            hooks=hooks,
            events=events,
        )

    runtime = Runtime(workspace=router)
    slot = AgentSlot(name='w', agent='Worker', cardinality='singleton', isolation=isolation)
    await runtime.assemble(Topology(slots=(slot,)), {'Worker': build}, tools={})
    return runtime, await runtime.occupy('w')


async def _cycle(runtime: Runtime, agent_id: str, text: str, **options: Any) -> Any:
    assert (await runtime.send(agent_id, text, **options)).ok
    return await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)


def _facts(ctx: Any) -> list[ContextBlock]:
    return [b for b in ctx.current_traj if kind_of(b) is HistoryKind.ENV_FACT]


async def test_a_cycles_work_lands_in_the_shared_workspace_only_when_the_cycle_ends(
    tmp_path: Path,
) -> None:
    router, root = _router(tmp_path)
    gate = asyncio.Event()
    llm = _Script([_write('w1', 'notes.md', 'mine\n'), _say('done')], gates={1: gate})
    runtime, agent_id = await _slot_agent(router, llm)
    try:
        assert (await runtime.send(agent_id, 'write the notes')).ok
        for _ in range(100):
            if llm.calls == 2:
                break
            await asyncio.sleep(0.01)
        during = (root / 'notes.md').exists()
        gate.set()
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        after = (root / 'notes.md').read_text(encoding='utf-8')
    finally:
        await runtime.shutdown()
    assert during is False
    assert after == 'mine\n'


async def test_the_copy_fact_is_told_once_while_it_stays_the_same(tmp_path: Path) -> None:
    router, _ = _router(tmp_path)
    llm = _Script([_say('one'), _say('two'), _say('three')])
    runtime, agent_id = await _slot_agent(router, llm)
    try:
        await _cycle(runtime, agent_id, 'first')
        await _cycle(runtime, agent_id, 'second')
        ctx = await _cycle(runtime, agent_id, 'third')
    finally:
        await runtime.shutdown()
    [fact] = _facts(ctx)
    assert '隔离的工作副本' in str(fact.content)
    assert str(fact.extra['syndica.key']).startswith(f'isolation:{agent_id}:')


async def test_a_conflict_is_reported_at_the_start_of_the_next_cycle_with_the_cycles_trust(
    tmp_path: Path,
) -> None:
    router, root = _router(tmp_path)
    gate = asyncio.Event()
    llm = _Script(
        [_write('w1', 'notes.md', 'mine\n'), _say('done'), _say('again')], gates={1: gate}
    )
    runtime, agent_id = await _slot_agent(router, llm)
    try:
        assert (await runtime.send(agent_id, 'edit', trust=TrustLevel.UNTRUSTED)).ok
        for _ in range(100):
            if llm.calls == 2:
                break
            await asyncio.sleep(0.01)
        (root / 'notes.md').write_text('host edit\n', encoding='utf-8')
        gate.set()
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        shared = (root / 'notes.md').read_text(encoding='utf-8')
        ctx = await _cycle(runtime, agent_id, 'next')
    finally:
        await runtime.shutdown()
    [report] = [b for b in _facts(ctx) if ':merge:' in str(b.extra['syndica.key'])]
    assert shared == 'host edit\n'
    assert 'notes.md' in str(report.content)
    assert report.trust is TrustLevel.UNTRUSTED
    assert str(report.extra['syndica.key']) == f'isolation:{agent_id}:merge:2'


async def test_a_failed_merge_keeps_the_copy_and_says_so_next_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    router, root = _router(tmp_path)
    llm = _Script([_write('w1', 'notes.md', 'mine\n'), _say('done'), _say('again')])
    runtime, agent_id = await _slot_agent(router, llm)

    async def broken(agent: str) -> Any:
        raise OSError('disk full')

    monkeypatch.setattr(router, 'merge_isolated', broken)
    try:
        await _cycle(runtime, agent_id, 'write')
        kept = router.isolated(agent_id) is not None
        ctx = await _cycle(runtime, agent_id, 'next')
    finally:
        await runtime.shutdown()
    assert kept and not (root / 'notes.md').exists()
    assert any('合并失败' in str(b.content) for b in _facts(ctx))


async def test_an_isolated_slot_needs_a_workspace_that_can_isolate(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, isolator=False)
    with pytest.raises(ConfigError) as caught:
        await _slot_agent(router, _Script([]))
    assert 'isolation' in caught.value.next_action and 'isolator=' in caught.value.next_action


def test_a_slot_isolation_outside_the_known_strategies_is_refused() -> None:
    with pytest.raises(ConfigError):
        AgentSlot(name='w', agent='Worker', isolation='copy')


def test_a_capture_of_an_isolated_slot_is_not_replayable() -> None:
    from syndica.kernel.replay_capture import (
        _topology_document,  # pyright: ignore[reportPrivateUsage]
    )

    shared = _topology_document(Topology(slots=(AgentSlot(name='a', agent='W'),)))
    isolated = _topology_document(
        Topology(slots=(AgentSlot(name='a', agent='W', isolation='worktree'),))
    )
    assert 'isolation' not in shared['slots'][0]  # type: ignore[index]
    assert isolated['slots'][0]['isolation'] == 'worktree'  # type: ignore[index]


def test_the_capture_refuses_a_topology_with_an_isolated_slot() -> None:
    from syndica.kernel.redaction import RuleRedactor
    from syndica.kernel.replay_capture import (
        ReplayCapture,
        Unreplayable,
        _topology_document,  # pyright: ignore[reportPrivateUsage]
    )

    capture = ReplayCapture(lambda value: value, RuleRedactor())
    shared = {'topology': _topology_document(Topology(slots=(AgentSlot(name='a', agent='W'),)))}
    isolated = {
        'topology': _topology_document(
            Topology(slots=(AgentSlot(name='a', agent='W', isolation='snapshot'),))
        )
    }
    capture._refuse_unreplayable_slots(shared)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(Unreplayable) as caught:
        capture._refuse_unreplayable_slots(isolated)  # pyright: ignore[reportPrivateUsage]
    assert 'isolation' in caught.value.received


def _bash(call_id: str, command: str) -> ContextBlock:
    arguments = json.dumps({'command': command})
    return ContextBlock(
        role='assistant', tool_calls=[ToolCall(id=call_id, name='Bash', arguments=arguments)]
    )


class _MergeOnceFails(LocalIsolator):
    def __init__(self, *, how: str) -> None:
        super().__init__(shell='/bin/sh', env={})
        self._how = how
        self._done = False

    async def merge(self, copy: Any, parent: Any, written: Any) -> MergeReport:
        if self._done:
            return await super().merge(copy, parent, written)
        self._done = True
        if self._how == 'raises':
            raise OSError('No space left on device')
        report = await super().merge(copy, parent, written)
        return replace(report, conflicts=(MergeConflict(path='a.txt', reason='both changed'),))


@pytest.mark.parametrize('how', ['raises', 'keeps'])
async def test_a_copy_kept_after_a_merge_still_runs_commands_next_cycle(
    tmp_path: Path, how: str
) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    router = WorkspaceRouter(
        LocalWorkspace(str(root), shell='/bin/sh', env={}), isolator=_MergeOnceFails(how=how)
    )
    llm = _Script(
        [_bash('b1', 'echo one > a.txt'), _say('one'), _bash('b2', 'cat a.txt'), _say('two')]
    )

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=llm,  # type: ignore[arg-type]
                tools={'Bash': Bash(router.portal())},
                system_prompt='SP',
            ),
            hooks=hooks,
            events=events,
        )

    runtime = Runtime(workspace=router)
    slot = AgentSlot(name='w', agent='Worker', cardinality='singleton', isolation='snapshot')
    await runtime.assemble(Topology(slots=(slot,)), {'Worker': build}, tools={})
    agent_id = await runtime.occupy('w')
    try:
        await _cycle(runtime, agent_id, 'first')
        ctx = await _cycle(runtime, agent_id, 'second')
    finally:
        await runtime.shutdown()
    answer = str(next(b for b in ctx.current_traj if b.tool_call_id == 'b2').content)
    assert 'one' in answer
    assert '已关闭' not in answer


async def test_a_slot_copy_a_delegated_copy_was_cut_from_is_kept_until_that_copy_merges(
    tmp_path: Path,
) -> None:
    router, root = _router(tmp_path)
    gate = asyncio.Event()
    delegate = json.dumps({'agent': 'Child', 'query': 'write result.txt', 'isolation': 'snapshot'})
    worker_llm = _Script(
        [
            ContextBlock(
                role='assistant', tool_calls=[ToolCall(id='d', name='Delegate', arguments=delegate)]
            ),
            _say('a again'),
        ]
    )
    child_llm = _Script([_write('w', 'result.txt', 'child work\n'), _say('child done')], {0: gate})

    def worker(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name='Worker', llm=worker_llm, system_prompt='SP'),  # type: ignore[arg-type]
            hooks=hooks,
            events=events,
        )

    def child(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Child',
                llm=child_llm,  # type: ignore[arg-type]
                tools={'Write': Write(router.portal())},
                system_prompt='SP',
            ),
            hooks=hooks,
            events=events,
        )

    runtime = Runtime(workspace=router, orchestration=True)
    slot = AgentSlot(name='w', agent='Worker', isolation='snapshot')
    await runtime.assemble(Topology(slots=(slot,)), {'Worker': worker, 'Child': child}, tools={})
    agent_id = await runtime.occupy('w')
    try:
        assert (await runtime.send(agent_id, 'go')).ok
        children: list[str] = []
        for _ in range(300):
            info = await runtime.find(agent_id)
            children = [] if info is None else list(info.children)
            if children:
                break
            await asyncio.sleep(0.01)
        [child_id] = children
        await runtime.abort(agent_id)
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        kept = router.isolated(agent_id) is not None
        gate.set()
        await asyncio.wait_for(runtime.wait(child_id), _TIMEOUT)
        await runtime.merge_isolated(child_id)
        ctx = await _cycle(runtime, agent_id, 'next')
    finally:
        await runtime.shutdown()
    assert kept
    assert (root / 'result.txt').read_text(encoding='utf-8') == 'child work\n'
    assert any(child_id in str(b.content) for b in _facts(ctx))


async def test_a_copy_another_copy_was_cut_from_is_neither_merged_nor_discarded(
    tmp_path: Path,
) -> None:
    router, _ = _router(tmp_path)
    router.isolate('a', 'snapshot')
    router.adopt('c', 'a')
    router.isolate('c', 'snapshot')
    for operation in (router.merge_isolated, router.discard_isolated):
        with pytest.raises(ConfigError) as caught:
            await operation('a')
        assert 'c' in caught.value.received and 'c' in caught.value.next_action
    assert router.isolated('a') is not None and router.isolated('c') is not None
    await router.discard_isolated('c')
    await router.discard_isolated('a')


async def test_merging_a_copy_whose_base_copy_is_gone_is_refused_and_its_work_kept(
    tmp_path: Path,
) -> None:
    router, _ = _router(tmp_path)
    router.isolate('a', 'snapshot')
    router.adopt('c', 'a')
    copy = router.isolate('c', 'snapshot')
    assert copy is not None
    (copy.location / 'report.md').write_text('child work\n', encoding='utf-8')
    await router.release('a')
    with pytest.raises(ConfigError) as caught:
        await router.merge_isolated('c')
    assert str(copy.location) in caught.value.next_action
    assert (copy.location / 'report.md').read_text(encoding='utf-8') == 'child work\n'
    await router.discard_isolated('c')


class _IsolatesOnce(LocalIsolator):
    def __init__(self) -> None:
        super().__init__(shell='/bin/sh', env={})
        self.calls = 0

    def prepare(
        self,
        root: Path,
        agent_id: str,
        requested: str,
        generation: int,
        under: Path | None = None,
    ) -> Any:
        self.calls += 1
        if self.calls > 1:
            raise OSError('No space left on device')
        return super().prepare(root, agent_id, requested, generation, under)


async def test_a_cycle_whose_copy_cannot_be_made_stops_with_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    router = WorkspaceRouter(
        LocalWorkspace(str(root), shell='/bin/sh', env={}), isolator=_IsolatesOnce()
    )
    llm = _Script([_say('answer to one'), _say('answer to two')])
    runtime, agent_id = await _slot_agent(router, llm)
    try:
        await _cycle(runtime, agent_id, 'one')
        with caplog.at_level(logging.ERROR, logger='syndica.kernel.runtime'):
            ctx = await _cycle(runtime, agent_id, 'two')
    finally:
        await runtime.shutdown()
    assert ctx.stop_reason is StopReason.ERROR
    assert llm.calls == 1
    logged = [cast(tuple[object, ...], r.args) for r in caplog.records if r.args]
    assert any(args[0] == agent_id and 'OSError' in args for args in logged)


async def test_a_delegated_copy_survives_its_middle_agent_being_collected_first(
    tmp_path: Path,
) -> None:
    router, root = _router(tmp_path)
    gate = asyncio.Event()

    def delegate(call_id: str, agent: str, query: str) -> ContextBlock:
        arguments = json.dumps({'agent': agent, 'query': query, 'isolation': 'snapshot'})
        return ContextBlock(
            role='assistant',
            tool_calls=[ToolCall(id=call_id, name='Delegate', arguments=arguments)],
        )

    parent_llm = _Script([delegate('d1', 'Middle', 'organise'), _say('parent done')])
    middle_llm = _Script([delegate('d2', 'Child', 'write result.txt'), _say('middle stopped')])
    child_llm = _Script([_write('w', 'result.txt', 'child work\n'), _say('child done')], {0: gate})

    def role(name: str, llm: _Script, tools: dict[str, Any] | None = None) -> Any:
        def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
            return BaseAgent(
                AgentOptions(name=name, llm=llm, tools=tools or {}, system_prompt='SP'),  # type: ignore[arg-type]
                hooks=hooks,
                events=events,
            )

        return build

    runtime = Runtime(
        workspace=router,
        orchestration=True,
        agents={
            'Middle': role('Middle', middle_llm),
            'Child': role('Child', child_llm, {'Write': Write(router.portal())}),
        },
    )
    try:
        parent = await runtime.spawn(role('Parent', parent_llm), query='go')
        middle = child = None
        for _ in range(300):
            info = await runtime.find(parent)
            if info is not None and info.children:
                middle = info.children[0]
                found = await runtime.find(middle)
                if found is not None and found.children:
                    child = found.children[0]
                    break
            await asyncio.sleep(0.01)
        assert middle is not None and child is not None
        await runtime.abort(middle)
        ctx = await asyncio.wait_for(runtime.wait(parent), _TIMEOUT)
        gate.set()
        await asyncio.wait_for(runtime.wait(child), _TIMEOUT)
        collected = str(next(b.content for b in ctx.current_traj if b.tool_call_id == 'd1'))
        await runtime.merge_isolated(child)
        await runtime.merge_isolated(middle)
    finally:
        await runtime.shutdown()
    assert '副本还留着' in collected
    assert (root / 'result.txt').read_text(encoding='utf-8') == 'child work\n'
