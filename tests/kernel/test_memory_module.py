"""分层记忆 Runtime Module（阶段四 / §8.9）：Remember 记短期 / 阈值整理、召回缓存注入、根才写全局。

本模块保留 `from __future__ import annotations` 与仓库其余模块一致。
"""

from __future__ import annotations

import asyncio

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import Context
from syndica.kernel.events import EventBus
from syndica.kernel.history_blocks import HistoryKind, kind_of
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.memory_module import MemoryModule
from syndica.kernel.memory_redaction import redacted_memory_sections
from syndica.kernel.memory_types import MemoryScope, MemorySection
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.stores.memory_memory import InMemoryMemoryStore

_TIMEOUT = 2.0
_REDACTOR = RuleRedactor()


def _factory(llm: ScriptedLLM) -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name='Worker', llm=llm, tools={}, system_prompt='SP'),
            hooks=hooks,
            events=events,
        )

    return build


def _agent_of(runtime: Runtime, agent_id: str) -> BaseAgent:
    agent = runtime._table.get(agent_id)  # pyright: ignore[reportPrivateUsage]
    assert agent is not None
    return agent


async def _spawn(runtime: Runtime) -> str:
    return await runtime.spawn(_factory(ScriptedLLM([ScriptedTurn(content='ok')])), query='t')


async def _seed(store: InMemoryMemoryStore, scope: MemoryScope, workspace: str | None) -> None:
    sections = redacted_memory_sections(
        scope, workspace, [MemorySection(heading='约定', text='用 tab 缩进')], 1, _REDACTOR
    )
    await store.consolidate(sections, expected_version=0)


async def test_recall_injects_long_term_memory_into_history() -> None:
    store = InMemoryMemoryStore()
    await _seed(store, MemoryScope.WORKSPACE, '/w')
    runtime = Runtime(modules=[MemoryModule(workspace='/w', store=store)])
    try:
        agent_id = await _spawn(runtime)
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        memory_blocks = [b for b in ctx.current_traj if kind_of(b) is HistoryKind.MEMORY]
        assert len(memory_blocks) == 1
        assert '用 tab 缩进' in str(memory_blocks[0].content)
    finally:
        await runtime.shutdown()


async def test_recall_is_idempotent_within_a_context() -> None:
    from syndica.kernel.memory_module import _recaller  # pyright: ignore[reportPrivateUsage]

    store = InMemoryMemoryStore()
    await _seed(store, MemoryScope.WORKSPACE, '/w')
    recall = _recaller(store, '/w')
    ctx = Context()
    await recall(ctx)
    await recall(ctx)  # 第二次不该再加——幂等键挡住（否则前缀逐轮变、缓存失效，spec §8.9）
    assert sum(kind_of(b) is HistoryKind.MEMORY for b in ctx.current_traj) == 1


async def test_recall_omits_workspace_memory_the_agent_cannot_see() -> None:
    store = InMemoryMemoryStore()
    await _seed(store, MemoryScope.WORKSPACE, '/other')  # 别的工作区
    runtime = Runtime(modules=[MemoryModule(workspace='/mine', store=store)])
    try:
        agent_id = await _spawn(runtime)
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        assert not any(kind_of(b) is HistoryKind.MEMORY for b in ctx.current_traj)
    finally:
        await runtime.shutdown()


async def test_remember_consolidates_short_term_at_the_threshold() -> None:
    store = InMemoryMemoryStore()
    runtime = Runtime(
        modules=[MemoryModule(workspace='/w', store=store)],
        flags={'memory_consolidate_after': 2},
    )
    try:
        agent_id = await _spawn(runtime)
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        remember = _agent_of(runtime, agent_id).options.tools['Remember']
        await remember.execute(text='踩坑一')
        assert await store.read(MemoryScope.WORKSPACE, '/w') is None  # 还没到阈值
        await remember.execute(text='踩坑二')
        record = await store.read(MemoryScope.WORKSPACE, '/w')
        assert record is not None
        assert record.through_seq == 2  # 两条都整理进去了
        assert '踩坑一' in record.sections[0].text and '踩坑二' in record.sections[0].text
    finally:
        await runtime.shutdown()


async def test_only_the_root_coordinator_writes_global_memory() -> None:
    store = InMemoryMemoryStore()
    runtime = Runtime(modules=[MemoryModule(workspace='/w', store=store)])
    try:
        root = await runtime.spawn(_factory(ScriptedLLM([ScriptedTurn(content='ok')])), query='t')
        await asyncio.wait_for(runtime.wait(root), _TIMEOUT)
        child = await runtime.spawn(
            _factory(ScriptedLLM([ScriptedTurn(content='ok')])), parent_id=root
        )

        root_tool = _agent_of(runtime, root).options.tools['Remember']
        child_tool = _agent_of(runtime, child).options.tools['Remember']
        root_result = await root_tool.execute(text='全局约定', scope='global')
        child_result = await child_tool.execute(text='想写全局', scope='global')

        assert '已记住' in str(root_result.output)
        assert '根协调者' in str(child_result.output)  # 工人写全局被拒
        entries = await store.entries(MemoryScope.GLOBAL, None)
        assert [e.text for e in entries] == ['全局约定']  # 只有根写进去了
    finally:
        await runtime.shutdown()


async def test_an_unknown_scope_is_rejected() -> None:
    store = InMemoryMemoryStore()
    runtime = Runtime(modules=[MemoryModule(workspace='/w', store=store)])
    try:
        agent_id = await _spawn(runtime)
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        tool = _agent_of(runtime, agent_id).options.tools['Remember']
        result = await tool.execute(text='x', scope='nonsense')
        assert 'workspace' in str(result.output) and 'global' in str(result.output)
    finally:
        await runtime.shutdown()


async def test_recall_off_omits_memory() -> None:
    store = InMemoryMemoryStore()
    await _seed(store, MemoryScope.WORKSPACE, '/w')
    runtime = Runtime(
        modules=[MemoryModule(workspace='/w', store=store)],
        flags={'memory_recall': False},
    )
    try:
        agent_id = await runtime.spawn(
            _factory(ScriptedLLM([ScriptedTurn(content='ok')])), query='t'
        )
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        assert not any(kind_of(b) is HistoryKind.MEMORY for b in ctx.current_traj)
    finally:
        await runtime.shutdown()
