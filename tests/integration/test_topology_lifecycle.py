"""一条拓扑的端到端：只从 syndica.kernel 导入。

Delegate 冷启动角色，不占用 lazy 槽。find 不创建 on-demand；occupy 才交出 id。
文件工具仍是 stub，真工具在计划 4。
"""

from __future__ import annotations

import asyncio

from syndica.kernel import (
    AgentCatalog,
    AgentSlot,
    AgentStatus,
    EventBus,
    HooksCenter,
    HostFacts,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    StopReason,
    Tool,
    ToolCall,
    ToolResult,
    Topology,
    register_builtins,
)

_TIMEOUT = 5.0
_CUSTOM_PLANNER = 'CUSTOM-PLANNER-ONLY'
_BUILTIN_PLANNER = '规划范围 / 决策 / 依赖 / 成功判据时用我'
_STUB_NAMES = ('Read', 'Grep', 'Glob', 'Write', 'Edit', 'Bash')
_ORCHESTRATOR_TOOLS = ('Delegate', 'Fork', 'SendMessage', 'QueryAgent', 'KillAgent')
_WORKER_TOOLS = ('Read', 'Write', 'Edit', 'Bash')
_EXPLORER_LINE = 'Delegate 给 Explorer：定位文件、符号、引用时用我，不要用我改文件'


class _Stub(Tool):
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        return ToolResult(output=self.name)


def _schema_names(kwargs: dict[str, object]) -> list[str]:
    tools = kwargs['tools']
    assert isinstance(tools, list)
    names: list[str] = []
    for item in tools:
        if isinstance(item, dict):
            name = item.get('name')
            if isinstance(name, str):
                names.append(name)
    return names


def _tools() -> dict[str, Tool]:
    table: dict[str, Tool] = {}
    for name in _STUB_NAMES:
        cls = type(
            f'Stub{name}',
            (_Stub,),
            {'__tool_name__': name, '__tool_command_args__': ()},
        )
        table[name] = cls()
    return table


async def test_topology_delegate_creates_worker_and_find_does_not_create_explorer():
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='c1',
                        name='Delegate',
                        arguments='{"agent": "Worker", "query": "改一处"}',
                    )
                ]
            ),
            ScriptedTurn(content='worker-done'),
            ScriptedTurn(content='orchestrator-done'),
        ]
    )
    host = HostFacts(os='linux', platform='x86_64', shell='bash')
    tools = _tools()
    catalog = AgentCatalog()
    register_builtins(catalog)
    catalog.load_mapping(
        {
            'name': 'Planner',
            'description': _CUSTOM_PLANNER,
            'system_prompt': '先划定范围。',
            'tools': ['Read', 'Grep', 'Glob'],
        }
    )
    models = {'default': llm}
    factories = catalog.freeze(tools, models, host)
    preview = factories['Orchestrator'](HooksCenter(), EventBus())
    assert _CUSTOM_PLANNER in preview.options.system_prompt
    assert _BUILTIN_PLANNER not in preview.options.system_prompt
    assert catalog.overrides('Planner') != ()

    topology = Topology(
        slots=(
            AgentSlot(
                name='root',
                agent='Orchestrator',
                is_root=True,
                cardinality='singleton',
                boot='eager',
            ),
            AgentSlot(name='worker', agent='Worker', cardinality='singleton', boot='lazy'),
            AgentSlot(name='explorer', agent='Explorer', cardinality='on-demand'),
        )
    )
    runtime = Runtime(agents=dict(factories), orchestration=False)
    try:
        await runtime.assemble(topology, catalog, tools=tools, models=models, host=host)
        root_id = await runtime.resolve('root', spawn=False)
        assert root_id is not None
        assert await runtime.resolve('worker', spawn=False) is None
        assert await runtime.find('explorer') is None

        await runtime.send_to_slot('root', '把活派给 Worker')
        ctx = await asyncio.wait_for(runtime.wait(root_id), _TIMEOUT)
        assert ctx.stop_reason is StopReason.COMPLETED
        # assemble 会再 freeze 一次。预览工厂看见覆盖不够：真正发出去的那一轮也必须是新描述。
        opened = llm.calls[0]
        opened_text = '\n'.join(block.content for block in opened if isinstance(block.content, str))
        assert f'Delegate 给 Planner：{_CUSTOM_PLANNER}' in opened_text
        assert _EXPLORER_LINE in opened_text
        assert 'Delegate 给 Worker：' in opened_text
        assert _BUILTIN_PLANNER not in opened_text
        assert _schema_names(llm.call_kwargs[0]) == list(_ORCHESTRATOR_TOOLS)
        assert _schema_names(llm.call_kwargs[1]) == list(_WORKER_TOOLS)

        workers = [info for info in await runtime.registry.list() if info.name == 'Worker']
        assert len(workers) == 1
        assert workers[0].parent_id == root_id
        assert workers[0].status is AgentStatus.ZOMBIE
        again_worker = await runtime.find(workers[0].agent_id)
        assert again_worker is not None and again_worker.status is AgentStatus.ZOMBIE
        assert await runtime.resolve('worker', spawn=False) is None
        assert [info for info in await runtime.registry.list() if info.name == 'Explorer'] == []
        assert await runtime.find('explorer') is None
        assert [info for info in await runtime.registry.list() if info.name == 'Explorer'] == []
        explorer_id = await runtime.occupy('explorer')
        again = await runtime.occupy('explorer')
        assert explorer_id and again and explorer_id != again
        assert await runtime.resolve('explorer', spawn=False) is None
        found = await runtime.find(explorer_id)
        assert found is not None and found.name == 'Explorer'
        second = await runtime.find(again)
        assert second is not None and second.agent_id == again
    finally:
        await runtime.shutdown()
