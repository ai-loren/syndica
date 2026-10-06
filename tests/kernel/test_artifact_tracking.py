"""内核隐式产物追踪（§7.4）：工具写出的产物自动登记进索引、触发 ARTIFACT_REGISTERED；没装索引不崩。

本模块保留 `from __future__ import annotations` 与仓库其余模块一致。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import ArtifactRef, ToolCall
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.stores.memory_artifact import InMemoryArtifactStore
from syndica.kernel.tool import Tool, ToolResult

_TIMEOUT = 2.0


class _Make(Tool):
    """写一个产物：回报一个 ArtifactRef，模拟写 / 编辑类工具（§7.4）。"""

    def __init__(self, version: str = 'v1') -> None:
        self._version = version

    async def execute(self, path: str) -> ToolResult:
        """在 path 写出内容，回报它这一版的 ArtifactRef。"""
        ref = ArtifactRef(
            id=f'file:{path}',
            path=path,
            kind='document',
            mime='text/markdown',
            version=self._version,
            producer='worker',
        )
        return ToolResult(output=f'wrote {path}', artifacts=[ref])


def _factory(llm: ScriptedLLM, tool: Tool) -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name='Worker', llm=llm, tools={'Make': tool}, system_prompt='SP'),
            hooks=hooks,
            events=events,
        )

    return build


def _writes_then_done(path: str) -> ScriptedLLM:
    return ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[ToolCall(id='c1', name='Make', arguments=json.dumps({'path': path}))]
            ),
            ScriptedTurn(content='done'),
        ]
    )


async def test_a_tools_artifact_is_registered_and_announced() -> None:
    store = InMemoryArtifactStore()
    runtime = Runtime(artifacts=store)
    announced: list[ArtifactRef] = []

    async def on_registered(**kwargs: Any) -> HookResult:
        announced.append(kwargs['artifact'])
        return HookResult()

    runtime.hooks.register(Junction.ARTIFACT_REGISTERED, on_registered)
    try:
        factory = _factory(_writes_then_done('report.md'), _Make())
        agent_id = await runtime.spawn(factory, query='go')
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        registered = await store.get('file:report.md')
        assert registered is not None and registered.version == 'v1'
        # 切点在内核登记之后触发，L1 验收据此读产物（§7.5）。
        assert [ref.id for ref in announced] == ['file:report.md']
    finally:
        await runtime.shutdown()


async def test_without_an_artifact_store_the_run_still_completes() -> None:
    # 宿主没给产物索引：隐式追踪什么都不做，工具照常回报产物、run 照常收尾（§7.4 可选）。
    runtime = Runtime()
    announced = 0

    async def on_registered(**kwargs: Any) -> HookResult:
        nonlocal announced
        announced += 1
        return HookResult()

    runtime.hooks.register(Junction.ARTIFACT_REGISTERED, on_registered)
    try:
        factory = _factory(_writes_then_done('report.md'), _Make())
        agent_id = await runtime.spawn(factory, query='go')
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        assert ctx.stop_reason is not None  # 跑完了
        assert announced == 0  # 没装索引就不触发
    finally:
        await runtime.shutdown()


class _Forger(Tool):
    """不是 Deliver，却在回报的产物上自称交付物。"""

    async def execute(self, path: str) -> ToolResult:
        """写一个产物并谎称它是交付物。"""
        ref = ArtifactRef(
            id='file:somewhere-else.md',
            path=path,
            kind='document',
            mime='text/markdown',
            version='v1',
            producer='worker',
            is_deliverable=True,
        )
        return ToolResult(output='wrote', artifacts=[ref])


class _Pair(Tool):
    """一次调用回报两个产物。"""

    async def execute(self, path: str) -> ToolResult:
        """写两个产物。"""
        refs = [
            ArtifactRef(
                id=f'file:{name}',
                path=name,
                kind='document',
                mime='text/markdown',
                version='v1',
                producer='worker',
            )
            for name in (f'{path}.a.md', f'{path}.b.md')
        ]
        return ToolResult(output='wrote two', artifacts=refs)


def _agent_with(tools: dict[str, Tool], script: list[ScriptedTurn]) -> BaseAgent:
    options = AgentOptions(name='Worker', llm=ScriptedLLM(script), tools=tools, system_prompt='SP')
    return BaseAgent(options, hooks=HooksCenter(), events=EventBus())


def _call(cid: str, name: str, path: str) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=json.dumps({'path': path}))


async def test_an_isolated_agents_artifact_id_carries_its_scope() -> None:
    # 两个各在自己副本里写 report.md 的 Agent 是两个产物（评审 H5）：id 由内核按作用域派生。
    store = InMemoryArtifactStore()
    agent = _agent_with(
        {'Make': _Make()},
        [ScriptedTurn(tool_calls=[_call('c1', 'Make', 'report.md')]), ScriptedTurn(content='done')],
    )
    agent._attach_artifact_store(store, lambda: 'alpha')  # pyright: ignore[reportPrivateUsage]
    await agent.run('go')
    assert [r.id for r in await store.list()] == ['file@alpha:report.md']


async def test_only_deliver_can_mark_a_deliverable_and_the_id_is_rederived() -> None:
    # 别的工具自称交付物、自报一个和路径不符的 id：内核两样都不认（评审 M11）。
    store = InMemoryArtifactStore()
    agent = _agent_with(
        {'Forger': _Forger()},
        [ScriptedTurn(tool_calls=[_call('c1', 'Forger', 'real.md')]), ScriptedTurn(content='done')],
    )
    agent._attach_artifact_store(store)  # pyright: ignore[reportPrivateUsage]
    await agent.run('go')
    assert await store.list(deliverable_only=True) == []
    assert [r.id for r in await store.list()] == ['file:real.md']


async def test_every_artifact_of_every_call_in_a_batch_is_registered() -> None:
    # 一批两次调用、其中一次回报两个产物：三个都登记（只登记最后一块、或只登记第一个都会漏）。
    store = InMemoryArtifactStore()
    agent = _agent_with(
        {'Make': _Make(), 'Pair': _Pair()},
        [
            ScriptedTurn(tool_calls=[_call('c1', 'Make', 'one.md'), _call('c2', 'Pair', 'two')]),
            ScriptedTurn(content='done'),
        ],
    )
    agent._attach_artifact_store(store)  # pyright: ignore[reportPrivateUsage]
    await agent.run('go')
    assert {r.id for r in await store.list()} == {
        'file:one.md',
        'file:two.a.md',
        'file:two.b.md',
    }


async def test_the_announcement_comes_after_the_artifact_is_in_the_store() -> None:
    # L1 挂在切点上读产物：切点触发时 store 里必须已经有它（评审 M10：先触发后登记会读空）。
    store = InMemoryArtifactStore()
    hooks = HooksCenter()
    present: list[bool] = []

    async def on_registered(**kwargs: Any) -> HookResult:
        present.append(await store.get(kwargs['artifact'].id) is not None)
        return HookResult()

    hooks.register(Junction.ARTIFACT_REGISTERED, on_registered)
    options = AgentOptions(
        name='Worker',
        llm=ScriptedLLM(
            [ScriptedTurn(tool_calls=[_call('c1', 'Make', 'r.md')]), ScriptedTurn(content='done')]
        ),
        tools={'Make': _Make()},
        system_prompt='SP',
    )
    agent = BaseAgent(options, hooks=hooks, events=EventBus())
    agent._attach_artifact_store(store)  # pyright: ignore[reportPrivateUsage]
    await agent.run('go')
    assert present == [True]


async def test_a_registered_path_is_normalised() -> None:
    # 同一个文件的两种写法是同一个产物：id 与 path 都按规范化后的路径（变异 M08）。
    store = InMemoryArtifactStore()
    agent = _agent_with(
        {'Make': _Make()},
        [
            ScriptedTurn(tool_calls=[_call('c1', 'Make', './drafts/../report.md')]),
            ScriptedTurn(content='done'),
        ],
    )
    agent._attach_artifact_store(store)  # pyright: ignore[reportPrivateUsage]
    await agent.run('go')
    assert [(r.id, r.path) for r in await store.list()] == [('file:report.md', 'report.md')]
