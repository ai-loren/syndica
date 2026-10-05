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
