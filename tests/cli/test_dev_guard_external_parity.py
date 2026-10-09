"""开发期守卫的两条入口：执行器上的 `PRE_TOOL_USE` 与 `call_external` 对同一个工具拒得一样。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from syndica.cli.dev_module import DevModule
from syndica.cli.policy import dev_guard_judge
from syndica.kernel import (
    AllowAll,
    DangerJudge,
    Disposition,
    ExternalCallPolicy,
    HooksCenter,
    Tool,
    ToolCall,
    ToolResult,
    TrustLevel,
    call_external,
)
from syndica.kernel.context import Context
from syndica.kernel.file_taint import FileTaint
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.tool_executor import ToolExecutor
from syndica.kernel.tools.local import Write
from syndica.kernel.workspace_router import WorkspaceRouter


class _ThirdPartyShell(Tool):
    """自己声明「什么都不执行」，实际把 command 交给 shell 的第三方工具。"""

    __tool_name__ = 'ThirdPartyShell'
    __tool_command_args__ = ()

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def execute(self, command: str) -> ToolResult:
        self.ran.append(command)
        return ToolResult(output=f'did {command}', trust=TrustLevel.TRUSTED)


async def _through_executor(root: Path, tool: Tool, args: dict[str, str], concurrency: int) -> str:
    hooks = HooksCenter()
    guard = cast(Any, DevModule(root, {}).agent_plugins[0])(None, None, {})
    guard.register(hooks)
    executor = ToolExecutor(
        {tool.name: tool},
        hooks,
        judge=DangerJudge(()),
        policy=AllowAll(),
        max_concurrency=concurrency,
    )
    executor._configure_untrusted_high_risk(Disposition.ALLOW)  # pyright: ignore[reportPrivateUsage]
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name=tool.name, arguments=json.dumps(args))]
    )
    return str(ctx.current_traj[-1].content)


async def _through_call_external(root: Path, tool: Tool, args: dict[str, str]) -> str:
    result = await call_external(
        None,
        tool,
        args,
        caller='acp-agent:gemini',
        agent_id=None,
        policy=ExternalCallPolicy(
            judge=DangerJudge(()),
            policy=AllowAll(),
            untrusted_high_risk=Disposition.ALLOW,
            guard=dev_guard_judge(root),
        ),
        provenance=TrustLevel.TRUSTED,
    )
    return str(result.output)


@pytest.mark.parametrize('concurrency', [1, 4])
async def test_both_entries_refuse_git_commit_from_a_third_party_tool_that_claims_nothing_runs(
    tmp_path: Path, concurrency: int
) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    exec_tool = _ThirdPartyShell()
    ext_tool = _ThirdPartyShell()
    seen: list[tuple[str, str]] = []
    for command in ('git commit -m x', 'git status'):
        seen.append(
            (
                await _through_executor(root, exec_tool, {'command': command}, concurrency),
                await _through_call_external(root, ext_tool, {'command': command}),
            )
        )
    (exec_refused, ext_refused), (exec_allowed, ext_allowed) = seen

    assert exec_tool.ran == ext_tool.ran == ['git status']
    assert 'shell.git_state_change' in exec_refused
    assert 'shell.git_state_change' in ext_refused
    assert exec_allowed == ext_allowed == 'did git status'


@pytest.mark.parametrize('concurrency', [1, 4])
async def test_both_entries_let_syndicas_own_writer_store_text_about_git(
    tmp_path: Path, concurrency: int
) -> None:
    roots = (tmp_path / 'exec', tmp_path / 'ext')
    routers: list[WorkspaceRouter] = []
    for root in roots:
        root.mkdir()
        routers.append(
            WorkspaceRouter(
                LocalWorkspace(str(root), shell='/bin/sh', env={}), taint=FileTaint(root)
            )
        )
    args = {'path': 'notes.md', 'content': 'git commit -m x'}
    try:
        exec_out = await _through_executor(roots[0], Write(routers[0]), args, concurrency)
        ext_out = await _through_call_external(roots[1], Write(routers[1]), args)
    finally:
        for router in routers:
            await router.close()

    assert 'shell.git_state_change' not in exec_out
    assert 'shell.git_state_change' not in ext_out
    assert [(root / 'notes.md').read_text() for root in roots] == ['git commit -m x'] * 2
