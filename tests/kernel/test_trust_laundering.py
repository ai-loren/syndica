"""阶段三对抗评审找到的洗白路径（M5）：不可信的内容不能换个写法、换条路就读回成可信。"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, TypeVar

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import (
    _CALL_TRUST,  # pyright: ignore[reportPrivateUsage]
    Context,
    ToolCall,
    TrustLevel,
)
from syndica.kernel.danger import (
    AllowAll,
    ConfirmationOutcome,
    ConfirmationRequest,
    DangerJudge,
    DangerRequest,
    Disposition,
)
from syndica.kernel.driving import _DRIVING_AGENT  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.events import EventBus
from syndica.kernel.file_taint import FileTaint
from syndica.kernel.hooks import HookResult, HooksCenter, Junction, JunctionKind
from syndica.kernel.hooks.installing import Installer, acting_as, installing
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tool_executor import ToolExecutor
from syndica.kernel.tools.local import Bash, Glob, Grep, Read
from syndica.kernel.versions import VersionStore
from syndica.kernel.workspace_router import WorkspaceRouter

T = TypeVar('T')
_TIMEOUT = 5.0


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / 'ws'
    (folder / 'src').mkdir(parents=True)
    (folder / 'sub').mkdir()
    (folder / 'README.md').write_text('baseline readme\n')
    (folder / 'src' / 'a.py').write_text('A = 1\n')
    tool = folder / 'sub' / 'tool.sh'
    tool.write_text('echo baseline-tool-ran\n')
    tool.chmod(0o755)
    return folder


@pytest.fixture
async def router(root: Path) -> AsyncIterator[WorkspaceRouter]:
    local = LocalWorkspace(str(root), shell='/bin/sh', env={})
    routed = WorkspaceRouter(local, taint=FileTaint(root))
    yield routed
    await routed.close()


async def test_a_path_through_a_planted_symlink_does_not_borrow_a_baseline_files_trust(
    router: WorkspaceRouter, root: Path
) -> None:
    (root / 'x' / 'y').mkdir(parents=True)
    (root / 'x' / 'README.md').write_text('planted quiz-marker-1\n')
    (root / 'link').symlink_to(root / 'x' / 'y', target_is_directory=True)

    read = await Read(router).execute('link/../README.md')

    assert 'planted' in str(read.output)
    assert read.trust is TrustLevel.UNTRUSTED


async def test_restoring_an_untrusted_version_from_an_earlier_session_stays_untrusted(
    root: Path,
) -> None:
    store = VersionStore(root, redactor=RuleRedactor())
    first = WorkspaceRouter(
        LocalWorkspace(str(root), shell='/bin/sh', env={}), versions=store, taint=FileTaint(root)
    )

    async def untrusted_write() -> str:
        _CALL_TRUST.set(TrustLevel.UNTRUSTED)
        await first.read_text('src/a.py', offset=1, limit=5)
        return await first.write_text('src/a.py', 'BAD = "quiz-marker-5"\n')

    bad = await asyncio.create_task(untrusted_write())
    await first.close()
    (root / 'src' / 'a.py').write_text('A = 1\n')
    second = WorkspaceRouter(
        LocalWorkspace(str(root), shell='/bin/sh', env={}), versions=store, taint=FileTaint(root)
    )
    try:
        await second.restore('src/a.py', bad)
        read = await Read(second).execute('src/a.py')
    finally:
        await second.close()

    assert 'quiz-marker-5' in str(read.output)
    assert read.trust is TrustLevel.UNTRUSTED


async def test_a_file_name_with_a_colon_does_not_borrow_another_files_trust_in_grep(
    router: WorkspaceRouter, root: Path
) -> None:
    (root / 'README.md:evil.txt').write_text('planted quiz-marker-2\n')

    found = await Grep(router).execute('quiz-marker-2')

    assert found.trust is TrustLevel.UNTRUSTED


async def test_listing_a_baseline_tree_with_directories_stays_trusted(
    router: WorkspaceRouter,
) -> None:
    listed = await Glob(router).execute('*')

    assert listed.trust is TrustLevel.TRUSTED


async def test_a_directory_created_after_open_is_an_untrusted_name(
    router: WorkspaceRouter, root: Path
) -> None:
    (root / 'planted-dir').mkdir()

    listed = await Glob(router).execute('*')

    assert listed.trust is TrustLevel.UNTRUSTED


def _bash_agent(router: WorkspaceRouter, arguments: str) -> AgentFactory:
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='b', name='Bash', arguments=arguments)]),
            ScriptedTurn(content='done'),
        ]
    )

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        tools: dict[str, Tool] = {'Bash': Bash(router)}
        return BaseAgent(
            AgentOptions(
                name='Worker', llm=llm, tools=tools, system_prompt='SP', danger_policy=AllowAll()
            ),
            hooks=hooks,
            events=events,
        )

    return build


async def _bash_output(router: WorkspaceRouter, arguments: str) -> str:
    runtime = Runtime(workspace=router)
    try:
        agent_id = await runtime.spawn(_bash_agent(router, arguments), query='go')
        await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
        agent = runtime._table.get(agent_id)  # pyright: ignore[reportPrivateUsage]
        assert agent is not None
        [block] = [b for b in agent.context.current_traj if b.role == 'tool']
    finally:
        await runtime.shutdown()
    return str(block.content)


@pytest.mark.parametrize(
    'command',
    [
        'sh < planted.sh',
        'cat planted.sh | sh',
        'sh ROOT/planted.sh',
        'cd sub && sh planted.sh',
    ],
    ids=['stdin_redirect', 'piped_cat', 'absolute_path_inside_the_workspace', 'after_a_cd'],
)
async def test_a_planted_script_is_not_run_however_its_path_is_spelled(
    router: WorkspaceRouter, root: Path, command: str
) -> None:
    (root / 'planted.sh').write_text('echo planted-ran\n')
    (root / 'sub' / 'planted.sh').write_text('echo planted-ran\n')
    arguments = '{"command": "' + command.replace('ROOT', str(root)) + '"}'

    output = await _bash_output(router, arguments)

    assert 'planted-ran' not in output


@pytest.mark.parametrize(
    'arguments',
    ['{"command": "cd sub && ./tool.sh"}', '{"command": "./tool.sh", "cwd": "sub"}'],
    ids=['cd_into_the_directory', 'cwd_argument'],
)
async def test_a_baseline_script_run_from_its_own_directory_is_not_blocked(
    router: WorkspaceRouter, root: Path, arguments: str
) -> None:
    output = await _bash_output(router, arguments)

    assert 'baseline-tool-ran' in output


def _tightened_center() -> HooksCenter:
    center = HooksCenter()
    center._tighten_modules(frozenset({'thirdparty'}))  # pyright: ignore[reportPrivateUsage]
    return center


async def test_an_in_place_write_by_a_tightened_module_in_an_extension_is_flagged() -> None:
    center = _tightened_center()
    handle = center.declare_extension_junction('cache:lookup', JunctionKind.INTERCEPTABLE)

    async def mutate(**kwargs: Any) -> HookResult:
        kwargs['box']['value'] = 'quiz-marker-6'
        return HookResult()

    with installing('module', 'thirdparty'):
        center.register(handle, mutate)
    box: dict[str, str] = {'value': ''}

    result = await center.trigger(handle, box=box)

    assert box['value'] == 'quiz-marker-6'
    assert result.untrusted is True


@pytest.mark.parametrize(
    ('kind', 'name', 'expected'),
    [('module', 'thirdparty', TrustLevel.UNTRUSTED), ('plugin', 'trusted', TrustLevel.TRUSTED)],
    ids=['tightened_module', 'trusted_plugin'],
)
async def test_a_file_written_from_a_hook_carries_its_authors_trust(
    router: WorkspaceRouter,
    kind: Literal['plugin', 'module'],
    name: str,
    expected: TrustLevel,
) -> None:
    runtime = Runtime(modules=[], workspace=router)
    runtime._untrusted_output = frozenset({'thirdparty'})  # pyright: ignore[reportPrivateUsage]
    try:
        with acting_as(Installer(kind, name)):
            await router.write_text('hooked.md', 'quiz-marker-7\n')
        read = await Read(router).execute('hooked.md')
    finally:
        await runtime.shutdown()

    assert read.trust is expected


class _Policy:
    def decide(self, request: DangerRequest) -> Disposition:
        return Disposition.CONFIRM


class _Runner(Tool):
    __tool_name__ = 'Runner'
    __tool_command_args__ = ('command',)
    __tool_external_input__ = False

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def execute(self, command: str) -> ToolResult:
        """执行 command。"""
        self.ran.append(command)
        return ToolResult(output='ran')


async def test_a_script_rewritten_while_waiting_for_approval_is_not_run(
    router: WorkspaceRouter, root: Path
) -> None:
    (root / 'build.sh').write_text('echo build\n')
    taint_root = FileTaint(root)
    runner = _Runner()
    executor = ToolExecutor(tools={'Runner': runner}, hooks=HooksCenter(), policy=_Policy())
    executor._configure_file_trust(lambda path: taint_root.trust_of(path, None))  # pyright: ignore[reportPrivateUsage]

    async def approve_after_a_rewrite(request: ConfirmationRequest) -> ConfirmationOutcome:
        (root / 'build.sh').write_text('echo rewritten\n')
        return ConfirmationOutcome.APPROVED

    executor._configure_confirmation('agent', approve_after_a_rewrite)  # pyright: ignore[reportPrivateUsage]
    ctx = Context()

    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c', name='Runner', arguments='{"command": "sudo sh build.sh"}')]
    )

    assert runner.ran == []
    assert 'build.sh' in str(ctx.current_traj[-1].content)


@pytest.mark.parametrize(
    'command',
    [
        'sh dl/install.sh',
        'cp dl/install.sh run.sh && sh run.sh',
        'mv dl/install.sh run.sh && sh run.sh',
    ],
    ids=['in_a_subdir', 'copied_before_running', 'moved_before_running'],
)
async def test_running_a_non_baseline_file_is_blocked_however_it_got_there(
    router: WorkspaceRouter, root: Path, command: str
) -> None:
    # download_then_execute 的纯判定认不出 tee / wget -P / mv 等形状（登记为遗留），但真实姿态下这些
    # 下载落地的文件都不是基线，执行它由执行器的 exec.untrusted_script 兜住（评审 F9 的 backstop）。
    (root / 'dl').mkdir()
    (root / 'dl' / 'install.sh').write_text('echo planted-ran quiz-marker-9\n')

    output = await _bash_output(router, '{"command": "' + command + '"}')

    assert 'planted-ran' not in output


def test_a_curl_option_value_is_not_read_as_a_remote_name_flag() -> None:
    verdict = DangerJudge().judge(
        Bash.__new__(Bash), {'command': 'curl -XPOST https://example.com/run.sh && sh run.sh'}
    )

    assert verdict.rule_id != 'shell.download_then_execute'


class _LocalRaiser(Tool):
    __tool_name__ = 'Local'
    __tool_command_args__ = ()
    __tool_external_input__ = False

    async def execute(self) -> ToolResult:
        """算一个数。"""
        raise ZeroDivisionError('quiz-marker-8')


async def test_a_tightened_modules_tool_raising_is_untrusted_even_if_it_declares_local() -> None:
    executor = ToolExecutor(tools={'Local': _LocalRaiser()}, hooks=HooksCenter())
    executor._configure_untrusted_output(frozenset({'Local'}), frozenset({'thirdparty'}))  # pyright: ignore[reportPrivateUsage]
    ctx = Context()

    await executor.execute_tool_calls(ctx, [ToolCall(id='l', name='Local', arguments='{}')])

    assert ctx.current_traj[-1].trust is TrustLevel.UNTRUSTED


async def test_an_untrusted_script_under_a_higher_rule_keeps_that_rules_name(
    router: WorkspaceRouter, root: Path
) -> None:
    (root / 'planted.sh').write_text('echo planted-ran\n')

    output = await _bash_output(router, '{"command": "sudo sh planted.sh"}')

    assert 'sudo' in output or '提权' in output
    assert 'planted-ran' not in output


async def test_an_observer_cannot_change_the_arguments_it_is_shown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Echo(Tool):
        __tool_name__ = 'Echo'
        __tool_command_args__ = ()
        __tool_external_input__ = False

        async def execute(self, text: str) -> ToolResult:
            """回显 text。"""
            return ToolResult(output=text)

    executor = ToolExecutor(tools={'Echo': _Echo()}, hooks=HooksCenter())
    seen: list[str] = []

    def tamper(call: ToolCall, args: Any, result: ToolResult) -> None:
        seen.append(args['text'])
        args['text'] = 'changed'

    executor._observe_outcomes(tamper)  # pyright: ignore[reportPrivateUsage]
    with caplog.at_level(logging.ERROR, logger='syndica.kernel.tool_executor'):
        await executor.execute_tool_calls(
            Context(), [ToolCall(id='e', name='Echo', arguments='{"text": "hi"}')]
        )

    assert seen == ['hi']
    assert [record.levelno for record in caplog.records] == [logging.ERROR]


def test_the_broker_proxy_exposes_only_the_protocol() -> None:
    from syndica.kernel.stores import InMemoryMessageBroker

    class _Extended(InMemoryMessageBroker):
        async def publish_many(self, *_: object) -> None:
            raise AssertionError('收紧不了的投递入口')

    runtime = Runtime(broker=_Extended())

    assert not hasattr(runtime.broker, 'publish_many')
    assert copy.copy(runtime.broker) is not None


async def test_a_trusted_hook_that_copies_a_tightened_extension_value_into_args_is_untrusted() -> (
    None
):
    center = _tightened_center()
    handle = center.declare_extension_junction('cache:lookup', JunctionKind.INTERCEPTABLE)

    async def answer(**_: Any) -> HookResult:
        return HookResult(modified={'value': 'sudo ls'})

    with installing('module', 'thirdparty'):
        center.register(handle, answer)

    async def fill(**kwargs: Any) -> HookResult:
        found = await center.trigger(handle, value=None)
        return HookResult(modified={'args': {'command': found.get('value')}})

    center.register(Junction.PRE_TOOL_USE, fill)
    runner = _Runner()
    executor = ToolExecutor(tools={'Runner': runner}, hooks=center, policy=AllowAll())

    await executor.execute_tool_calls(
        Context(), [ToolCall(id='r', name='Runner', arguments='{"command": "ls"}')]
    )

    assert runner.ran == []


async def _as(agent: str, call: Callable[[], Awaitable[T]]) -> T:
    async def driven() -> T:
        _DRIVING_AGENT.set(agent)
        return await call()

    return await asyncio.create_task(driven())
