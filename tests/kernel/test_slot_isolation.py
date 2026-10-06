"""拓扑槽的隔离（M5 后续）：每个周期在共享工作区的拷贝里干活，周期结束时合并，冲突在下一个周期开头
以内核署名块告诉它（信任随合并那一刻的溯源），隔离的事实按周期键注入。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import ContextBlock, ToolCall, TrustLevel
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.history_blocks import HistoryKind, kind_of
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.isolation import LocalIsolator
from syndica.kernel.llm import LLMConfig
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.runtime import Runtime
from syndica.kernel.tools.local import Write
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


async def test_every_cycle_is_told_it_works_in_a_copy_under_its_own_key(tmp_path: Path) -> None:
    router, _ = _router(tmp_path)
    llm = _Script([_say('one'), _say('two')])
    runtime, agent_id = await _slot_agent(router, llm)
    try:
        await _cycle(runtime, agent_id, 'first')
        ctx = await _cycle(runtime, agent_id, 'second')
    finally:
        await runtime.shutdown()
    keys = [str(b.extra['syndica.key']) for b in _facts(ctx)]
    assert keys == [f'isolation:{agent_id}:1', f'isolation:{agent_id}:2']
    assert all('隔离的工作副本' in str(b.content) for b in _facts(ctx))


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
    capture._refuse_redacted_slot_tools(shared)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(Unreplayable) as caught:
        capture._refuse_redacted_slot_tools(isolated)  # pyright: ignore[reportPrivateUsage]
    assert 'isolation' in caught.value.received
