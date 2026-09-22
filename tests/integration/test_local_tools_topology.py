"""真实本地工具穿过声明式目录、拓扑和 Delegate 完成一轮协作。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from syndica.kernel import (
    AgentCatalog,
    AgentSlot,
    HostFacts,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    StopReason,
    ToolCall,
    Topology,
    register_builtins,
)
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.tools.local import local_tools

_TIMEOUT = 5.0


async def test_worker_write_and_bash_touch_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='delegate-1',
                        name='Delegate',
                        arguments='{"agent":"Worker","query":"写文件并读取验证"}',
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='write-1',
                        name='Write',
                        arguments='{"path":"result.txt","content":"verified-content"}',
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='bash-1',
                        name='Bash',
                        arguments=(
                            '{"command":"printf shell:; '
                            "tr '[:lower:]' '[:upper:]' < result.txt\"}"
                        ),
                    )
                ]
            ),
            ScriptedTurn(content='worker-finished'),
            ScriptedTurn(content='orchestrator-finished'),
        ]
    )
    catalog = AgentCatalog()
    register_builtins(catalog)
    tools = local_tools(workspace.portal())
    models = {'default': llm}
    host = HostFacts(os='darwin', platform='arm64', shell='/bin/sh')
    factories = catalog.freeze(tools, models, host)
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
        )
    )
    runtime = Runtime(agents=dict(factories), orchestration=False)
    try:
        await runtime.assemble(topology, catalog, tools=tools, models=models, host=host)
        root_id = await runtime.resolve('root', spawn=False)
        assert root_id is not None
        assert await runtime.resolve('worker', spawn=False) is None

        await runtime.send_to_slot('root', '让 Worker 写文件并用 Bash 验证')
        root_context = await asyncio.wait_for(runtime.wait(root_id), timeout=_TIMEOUT)

        assert root_context.stop_reason is StopReason.COMPLETED
        assert (root / 'result.txt').read_text(encoding='utf-8') == 'verified-content'
        workers = [info for info in await runtime.registry.list() if info.name == 'Worker']
        assert len(workers) == 1
        worker_context = await runtime.wait(workers[0].agent_id)
        bash_results = [
            block
            for block in worker_context.current_traj
            if block.role == 'tool' and block.tool_call_id == 'bash-1'
        ]
        assert len(bash_results) == 1
        assert isinstance(bash_results[0].content, str)
        assert 'shell:VERIFIED-CONTENT' in bash_results[0].content
        assert await runtime.resolve('worker', spawn=False) is None
    finally:
        await runtime.shutdown()
        await workspace.close()
