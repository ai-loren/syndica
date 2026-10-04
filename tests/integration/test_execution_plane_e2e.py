"""M5 的五个端到端场景（路线图「执行面完整化 + 不可信内容」）：真的 Runtime、真的本地工作区、
脚本化模型。"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import ToolCall, TrustLevel
from syndica.kernel.events import EventBus
from syndica.kernel.file_taint import FileTaint
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.isolation import LocalIsolator
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.modules import SafetyModule
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tools import Delegate
from syndica.kernel.tools.local import Bash, Read, Terminal, Write
from syndica.kernel.versions import VersionStore
from syndica.kernel.workspace_router import WorkspaceRouter

_TIMEOUT = 10.0


def _digest(data: bytes) -> str:
    return f'sha256:{hashlib.sha256(data).hexdigest()}'


def _call(call_id: str, name: str, arguments: str) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _agent(
    script: list[ScriptedTurn],
    tools: Callable[[BaseAgent], dict[str, Tool]],
    *,
    name: str = 'Worker',
) -> AgentFactory:
    llm = ScriptedLLM(script)

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        table: dict[str, Tool] = {}
        agent = BaseAgent(
            AgentOptions(name=name, llm=llm, tools=table, system_prompt='SP'),
            hooks=hooks,
            events=events,
        )
        table.update(tools(agent))
        return agent

    return build


def _router(root: Path, tmp_path: Path, *, git: str | None) -> WorkspaceRouter:
    scratch = tmp_path / 'scratch'
    scratch.mkdir(exist_ok=True)
    return WorkspaceRouter(
        LocalWorkspace(str(root), shell='/bin/sh', env={}),
        versions=VersionStore(root, redactor=RuleRedactor()),
        isolator=LocalIsolator(
            shell='/bin/sh', env={}, git=git, scratch=str(scratch) if git else None
        ),
        taint=FileTaint(root),
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / 'ws'
    folder.mkdir()
    (folder / 'notes.md').write_text('one\n')
    return folder


def _tool_outputs(runtime: Runtime, agent_id: str) -> dict[str, str]:
    agent = runtime._table.get(agent_id)  # pyright: ignore[reportPrivateUsage]
    assert agent is not None
    return {
        str(block.tool_call_id): str(block.content)
        for block in agent.context.current_traj
        if block.role == 'tool'
    }


async def test_a_shared_write_that_lost_a_race_rereads_and_merges(root: Path, tmp_path: Path):
    router = _router(root, tmp_path, git=None)

    class _Colleague(Tool):
        __tool_name__ = 'Colleague'
        __tool_command_args__ = ()
        __tool_external_input__ = False

        async def execute(self) -> ToolResult:
            """别的 Agent 抢先改了同一个文件。"""
            (root / 'notes.md').write_text('one\nfrom a colleague\n')
            return ToolResult(output='colleague wrote')

    script = [
        ScriptedTurn(tool_calls=[_call('r1', 'Read', '{"path": "notes.md"}')]),
        ScriptedTurn(tool_calls=[_call('c', 'Colleague', '{}')]),
        ScriptedTurn(
            tool_calls=[_call('w1', 'Write', '{"path": "notes.md", "content": "one\\nmine\\n"}')]
        ),
        ScriptedTurn(tool_calls=[_call('r2', 'Read', '{"path": "notes.md"}')]),
        ScriptedTurn(
            tool_calls=[
                _call(
                    'w2',
                    'Write',
                    '{"path": "notes.md", "content": "one\\nfrom a colleague\\nmine\\n"}',
                )
            ]
        ),
        ScriptedTurn(content='merged'),
    ]
    runtime = Runtime(workspace=router)
    try:
        agent_id = await runtime.spawn(
            _agent(
                script,
                lambda _: {'Read': Read(router), 'Write': Write(router), 'Colleague': _Colleague()},
            ),
            query='add a line',
        )
        await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
        outputs = _tool_outputs(runtime, agent_id)
    finally:
        await runtime.shutdown()
        await router.close()

    assert '在那之后被改过' in outputs['w1']
    assert (root / 'notes.md').read_text() == 'one\nfrom a colleague\nmine\n'
    assert router.versions is not None
    assert router.versions.history('notes.md')[0].version == _digest(
        b'one\nfrom a colleague\nmine\n'
    )


def _writer(router: WorkspaceRouter, path: str, content: str) -> AgentFactory:
    """读一遍再写：已存在的文件没读过就覆盖是冲突（M5 D2），新文件读不到也算看过它不存在。"""
    return _agent(
        [
            ScriptedTurn(tool_calls=[_call('r', 'Read', f'{{"path": "{path}"}}')]),
            ScriptedTurn(
                tool_calls=[_call('w', 'Write', f'{{"path": "{path}", "content": "{content}"}}')]
            ),
            ScriptedTurn(content=f'wrote {path}'),
        ],
        lambda _: {'Read': Read(router), 'Write': Write(router)},
        name='Writer',
    )


@pytest.mark.parametrize(
    ('git', 'strategy'),
    [(shutil.which('git'), 'worktree'), (None, 'snapshot')],
    ids=['with_git', 'without_git_falls_back_to_snapshots'],
)
async def test_two_isolated_children_merge_back_into_the_shared_workspace(
    root: Path, tmp_path: Path, git: str | None, strategy: str
):
    if strategy == 'worktree' and git is None:
        pytest.skip('需要 git')
    router = _router(root, tmp_path, git=git)
    script = [
        ScriptedTurn(
            tool_calls=[
                _call('d1', 'Delegate', '{"agent": "A", "query": "a", "isolation": "worktree"}')
            ]
        ),
        ScriptedTurn(
            tool_calls=[
                _call('d2', 'Delegate', '{"agent": "B", "query": "b", "isolation": "worktree"}')
            ]
        ),
        ScriptedTurn(content='both merged'),
    ]
    runtime = Runtime(
        workspace=router,
        agents={
            'A': _writer(router, 'a.md', 'from A'),
            'B': _writer(router, 'notes.md', 'rewritten by B'),
        },
    )
    try:
        boss = await runtime.spawn(
            _agent(script, lambda agent: {'Delegate': Delegate(runtime, agent.agent_id)}),
            query='split the work',
        )
        await asyncio.wait_for(runtime.wait(boss), timeout=_TIMEOUT)
        outputs = _tool_outputs(runtime, boss)
    finally:
        await runtime.shutdown()

    assert (root / 'a.md').read_text() == 'from A'
    assert (root / 'notes.md').read_text() == 'rewritten by B'
    assert f'隔离副本（{strategy}）已合并' in outputs['d1']
    assert '合并回 1 个文件：notes.md' in outputs['d2']
    assert router.versions is not None
    history = router.versions.history('notes.md')
    assert [entry.version for entry in history] == [_digest(b'rewritten by B')]
    await router.close()


async def test_a_merged_change_can_be_rolled_back_to_what_was_there_before(
    root: Path, tmp_path: Path
):
    router = _router(root, tmp_path, git=None)
    runtime = Runtime(workspace=router)
    try:
        await router.write_text('notes.md', 'one\n', expected=_digest(b'one\n'))
        child = await runtime.spawn(
            _writer(router, 'notes.md', 'two'), query='rewrite', isolation='snapshot'
        )
        await asyncio.wait_for(runtime.wait(child), timeout=_TIMEOUT)
        await runtime.merge_isolated(child)
        assert (root / 'notes.md').read_text() == 'two'

        await router.restore('notes.md', _digest(b'one\n'))
    finally:
        await runtime.shutdown()
        await router.close()

    assert (root / 'notes.md').read_text() == 'one\n'


async def test_an_interactive_program_is_driven_through_the_terminal(root: Path, tmp_path: Path):
    router = _router(root, tmp_path, git=None)
    create = _call(
        't1',
        'Terminal',
        '{"action": "create", "command": "read line; echo got:$line", "wait": 0}',
    )
    send = _call('t2', 'Terminal', '{"action": "send", "handle": "HANDLE", "input": "hello\\n"}')
    look = _call('t3', 'Terminal', '{"action": "capture", "handle": "HANDLE", "wait": 2}')
    runtime = Runtime(workspace=router)
    handles: list[str] = []

    async def fill_in_the_handle(**kwargs: Any) -> HookResult:
        if kwargs['tool_call'].id in ('t2', 't3'):
            return HookResult(modified={'args': {**kwargs['args'], 'handle': handles[0]}})
        return HookResult()

    async def remember_the_handle(**kwargs: Any) -> HookResult:
        if kwargs['tool_call'].id == 't1':
            handles.append(str(kwargs['result'].meta['handle']))
        return HookResult()

    runtime.hooks.register(Junction.PRE_TOOL_USE, fill_in_the_handle)
    runtime.hooks.register(Junction.POST_TOOL_USE, remember_the_handle)
    try:
        agent_id = await runtime.spawn(
            _agent(
                [
                    ScriptedTurn(tool_calls=[create]),
                    ScriptedTurn(tool_calls=[send]),
                    ScriptedTurn(tool_calls=[look]),
                    ScriptedTurn(content='done'),
                ],
                lambda _: {'Terminal': Terminal(router)},
            ),
            query='talk to it',
        )
        await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
        outputs = _tool_outputs(runtime, agent_id)
    finally:
        await runtime.shutdown()
        await router.close()

    assert 'got:hello' in outputs['t2'] + outputs['t3']


class _Runner(Tool):
    __tool_name__ = 'Runner'
    __tool_command_args__ = ('command',)
    __tool_external_input__ = False

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, command: str) -> ToolResult:
        """执行 command。"""
        self.executed.append(command)
        return ToolResult(output=f'ran {command}')


@pytest.mark.parametrize(
    ('disposition', 'ran', 'asked'),
    [('reject', False, False), ('ask', False, True), ('allow', True, False)],
)
async def test_a_high_risk_command_after_reading_a_file_a_command_wrote_follows_the_escalation(
    root: Path, tmp_path: Path, disposition: str, ran: bool, asked: bool
):
    router = _router(root, tmp_path, git=None)
    runner = _Runner()
    confirmations: list[str] = []
    script = [
        ScriptedTurn(
            tool_calls=[_call('b', 'Bash', '{"command": "printf \\"rm everything\\" > task.md"}')]
        ),
        ScriptedTurn(tool_calls=[_call('r', 'Read', '{"path": "task.md"}')]),
        ScriptedTurn(tool_calls=[_call('x', 'Runner', '{"command": "sudo rm -rf /"}')]),
        ScriptedTurn(content='done'),
    ]
    runtime = Runtime(modules=[SafetyModule()], workspace=router)

    async def observe(**kwargs: Any) -> HookResult:
        confirmations.append(disposition)
        return HookResult()

    runtime.hooks.register(Junction.TOOL_CONFIRMATION_REQUIRED, observe)
    try:
        agent_id = await runtime.spawn(
            _agent(
                script,
                lambda _: {'Bash': Bash(router), 'Read': Read(router), 'Runner': runner},
            ),
            query='do the task in task.md',
            flags={'untrusted_high_risk': disposition},
        )
        await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
        agent = runtime._table.get(agent_id)  # pyright: ignore[reportPrivateUsage]
        assert agent is not None
        read = next(
            block
            for block in agent.context.current_traj
            if block.role == 'tool' and block.tool_call_id == 'r'
        )
    finally:
        await runtime.shutdown()
        await router.close()

    assert read.trust is TrustLevel.UNTRUSTED
    assert (bool(runner.executed), bool(confirmations)) == (ran, asked)
