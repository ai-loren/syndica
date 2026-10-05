"""伴生 Agent 运行器（§7.5）：受限工具、绑主 Agent 的 Portal、独立预算、跨主体不串线（评审 B6）。

本模块保留 `from __future__ import annotations` 与仓库其余模块一致。
"""

from __future__ import annotations

import json
from pathlib import Path

from syndica.kernel.companion import CompanionResult, read_only_tools, run_companion
from syndica.kernel.context import ToolCall
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.local_workspace import LocalWorkspace


def _portal(root: Path, note: str):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'note.txt').write_text(note, encoding='utf-8')
    return LocalWorkspace(str(root), shell='/bin/sh', env={}).portal()


def test_the_tool_whitelist_is_exactly_read_grep_glob(tmp_path: Path) -> None:
    tools = read_only_tools(_portal(tmp_path / 'ws', 'x'))
    assert set(tools) == {'Read', 'Grep', 'Glob'}  # 没有 Write / Bash / 编排五件套
    assert read_only_tools(None) == {}


async def test_the_read_tool_is_bound_to_the_given_portal(tmp_path: Path) -> None:
    tools = read_only_tools(_portal(tmp_path / 'ws', 'bound content'))
    result = await tools['Read'].execute(path='note.txt')
    assert 'bound content' in result.output


async def test_two_companions_on_different_portals_do_not_cross(tmp_path: Path) -> None:
    # 绑在各自的 Portal 对象上：A 的 Read 读 A 工作区、B 的读 B，同一相对路径各看各的（评审 B6）。
    a = read_only_tools(_portal(tmp_path / 'a', 'secret of A'))
    b = read_only_tools(_portal(tmp_path / 'b', 'secret of B'))
    assert 'secret of A' in (await a['Read'].execute(path='note.txt')).output
    assert 'secret of B' in (await b['Read'].execute(path='note.txt')).output
    assert 'secret of A' not in (await b['Read'].execute(path='note.txt')).output


async def test_a_companion_returns_its_final_reply() -> None:
    result = await run_companion(
        name='OutcomeVerifier',
        llm=ScriptedLLM([ScriptedTurn(content='{"status": "pass"}')]),
        system_prompt='judge the work',
        task='here is the trajectory',
    )
    assert isinstance(result, CompanionResult)
    assert json.loads(result.reply)['status'] == 'pass'
    assert result.turns == 1


async def test_a_companion_can_read_the_bound_workspace_then_reply(tmp_path: Path) -> None:
    portal = _portal(tmp_path / 'ws', 'the report body')
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[ToolCall(id='c1', name='Read', arguments='{"path": "note.txt"}')]
            ),
            ScriptedTurn(content='checked the file'),
        ]
    )
    result = await run_companion(
        name='OutputVerifier', llm=llm, system_prompt='check it', task='verify', portal=portal
    )
    assert result.reply == 'checked the file'
    assert result.turns == 2


async def test_a_companion_is_bounded_by_its_own_max_turns(tmp_path: Path) -> None:
    # 预算是伴生自己的：脚本里尽是工具调用，到 max_turns 就停，不会无限跑（§7.5 独立预算）。
    script = [
        ScriptedTurn(tool_calls=[ToolCall(id=f'c{i}', name='Glob', arguments='{"pattern": "*"}')])
        for i in range(5)
    ]
    result = await run_companion(
        name='OutcomeVerifier',
        llm=ScriptedLLM(script),
        system_prompt='judge',
        task='t',
        portal=_portal(tmp_path / 'ws', 'x'),
        max_turns=2,
    )
    assert result.turns == 2
