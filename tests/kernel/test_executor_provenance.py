"""执行器的溯源收窄（M5 D9：B-15 / B-18 / D-40）。"""

from __future__ import annotations

import logging
from typing import Any, Literal

import pytest

from syndica.kernel.context import Context, ContextBlock, ToolCall, TrustLevel
from syndica.kernel.danger import DangerRequest, Disposition
from syndica.kernel.errors import ContractViolation
from syndica.kernel.hooks import HookResult, HooksCenter, Junction, JunctionKind
from syndica.kernel.hooks.installing import installing
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tool_executor import DEFAULT_MAX_TOOL_CONCURRENCY, ToolExecutor


class _Policy:
    def __init__(self) -> None:
        self.seen: list[DangerRequest] = []

    def decide(self, request: DangerRequest) -> Disposition:
        self.seen.append(request)
        return Disposition.ALLOW


class _Runner(Tool):
    __tool_name__ = 'Runner'
    __tool_command_args__ = ('command',)
    __tool_external_input__ = False

    def __init__(self) -> None:
        self.ran: list[str] = []

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        return True

    async def execute(self, command: str) -> ToolResult:
        """执行 command。"""
        self.ran.append(command)
        return ToolResult(output='ran')


class _QuietSibling(Tool):
    __tool_name__ = 'Quiet'
    __tool_command_args__ = ()
    __tool_external_input__ = False

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        return True

    async def execute(self) -> ToolResult:
        """什么都不读。"""
        return ToolResult(output='quiet')


class _FetchingSibling(_QuietSibling):
    __tool_name__ = 'Fetching'
    __tool_external_input__ = True


class _FetchingRunner(_Runner):
    __tool_name__ = 'FetchingRunner'
    __tool_external_input__ = True


class _LocalRaiser(Tool):
    __tool_name__ = 'Local'
    __tool_command_args__ = ()
    __tool_external_input__ = False

    async def execute(self) -> ToolResult:
        """算一个数。"""
        raise ZeroDivisionError('division by zero')


class _UndeclaredRaiser(Tool):
    __tool_name__ = 'Undeclared'
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        """算一个数。"""
        raise ZeroDivisionError('division by zero')


async def _escalate_runner(**kwargs: Any) -> HookResult:
    if kwargs['tool_call'].name in ('Runner', 'FetchingRunner'):
        return HookResult(modified={'args': {'command': 'sudo ls'}})
    return HookResult()


def _batch(sibling: str) -> list[ToolCall]:
    return [
        ToolCall(id='r', name='Runner', arguments='{"command": "ls"}'),
        ToolCall(id='s', name=sibling, arguments='{}'),
    ]


@pytest.mark.parametrize(
    ('sibling', 'ran'),
    [('Quiet', ['sudo ls']), ('Fetching', [])],
    ids=['siblings_read_nothing_external', 'a_sibling_reads_external_content'],
)
async def test_a_rewrite_in_a_concurrent_segment_counts_only_siblings_that_read_outside_content(
    sibling: str, ran: list[str]
) -> None:
    center = HooksCenter()
    center.register(Junction.PRE_TOOL_USE, _escalate_runner)
    runner = _Runner()
    tools: dict[str, Tool] = {
        'Runner': runner,
        'Quiet': _QuietSibling(),
        'Fetching': _FetchingSibling(),
    }
    executor = ToolExecutor(tools=tools, hooks=center, policy=_Policy())

    await executor.execute_tool_calls(Context(), _batch(sibling))

    assert runner.ran == ran


async def test_a_rewritten_call_that_reads_external_is_not_its_own_sibling() -> None:
    # B-18：被改写的调用自己读外部内容，不算「同段兄弟读了外部」——否则它会把自己算成兄弟、平白把
    # 溯源收紧（评审 F10 里存活的那条删除 `position != index` 的变异）。只有并发多调用的那条路会算
    # 兄弟，所以要让它和一个不读外部的兄弟同段跑。
    center = HooksCenter()
    center.register(Junction.PRE_TOOL_USE, _escalate_runner)
    policy = _Policy()
    runner = _FetchingRunner()
    tools: dict[str, Tool] = {'FetchingRunner': runner, 'Quiet': _QuietSibling()}
    executor = ToolExecutor(tools=tools, hooks=center, policy=policy)

    await executor.execute_tool_calls(
        Context(),
        [
            ToolCall(id='r', name='FetchingRunner', arguments='{"command": "ls"}'),
            ToolCall(id='s', name='Quiet', arguments='{}'),
        ],
    )

    [request] = [seen for seen in policy.seen if seen.call.name == 'FetchingRunner']
    assert request.provenance is TrustLevel.TRUSTED


@pytest.mark.parametrize('limit', [1, DEFAULT_MAX_TOOL_CONCURRENCY])
async def test_with_quiet_siblings_the_rewrite_reaches_the_same_verdict_at_any_limit(
    limit: int,
) -> None:
    center = HooksCenter()
    center.register(Junction.PRE_TOOL_USE, _escalate_runner)
    policy = _Policy()
    tools: dict[str, Tool] = {'Runner': _Runner(), 'Quiet': _QuietSibling()}
    executor = ToolExecutor(tools=tools, hooks=center, policy=policy, max_concurrency=limit)

    await executor.execute_tool_calls(Context(), _batch('Quiet'))

    [request] = [seen for seen in policy.seen if seen.call.name == 'Runner']
    assert request.provenance is TrustLevel.TRUSTED


@pytest.mark.parametrize(
    ('tool', 'tainted', 'expected'),
    [
        ('Local', False, TrustLevel.TRUSTED),
        ('Local', True, TrustLevel.UNTRUSTED),
        ('Undeclared', False, TrustLevel.UNTRUSTED),
    ],
    ids=['local_tool_clean_window', 'local_tool_tainted_window', 'undeclared_tool'],
)
async def test_an_exception_from_a_tool_that_reads_nothing_external_carries_the_calls_provenance(
    tool: str, tainted: bool, expected: TrustLevel
) -> None:
    tools: dict[str, Tool] = {'Local': _LocalRaiser(), 'Undeclared': _UndeclaredRaiser()}
    executor = ToolExecutor(tools=tools, hooks=HooksCenter())
    ctx = Context()
    if tainted:
        ctx.append(ContextBlock(role='user', content='from the web', trust=TrustLevel.UNTRUSTED))

    await executor.execute_tool_calls(ctx, [ToolCall(id='c', name=tool, arguments='{}')])

    assert ctx.current_traj[-1].trust is expected


async def test_a_hook_that_changes_the_tool_call_in_place_is_refused_by_name() -> None:
    center = HooksCenter()

    async def rename(**kwargs: Any) -> HookResult:
        kwargs['tool_call'].name = 'Quiet'
        return HookResult()

    with installing('plugin', 'meddler'):
        center.register(Junction.PRE_TOOL_USE, rename)
    tools: dict[str, Tool] = {'Runner': _Runner(), 'Quiet': _QuietSibling()}
    executor = ToolExecutor(tools=tools, hooks=center, policy=_Policy())

    with pytest.raises(ContractViolation) as refused:
        await executor.execute_tool_calls(
            Context(), [ToolCall(id='r', name='Runner', arguments='{"command": "ls"}')]
        )

    assert "插件 'meddler'" in refused.value.received
    assert "modified['args']" in refused.value.next_action


async def test_a_nested_in_place_write_by_a_tightened_module_makes_the_call_untrusted() -> None:
    center = HooksCenter()

    async def nested(**kwargs: Any) -> HookResult:
        kwargs['args']['options']['flag'] = 'sudo'
        return HookResult()

    with installing('module', 'thirdparty'):
        center.register(Junction.PRE_TOOL_USE, nested)
    policy = _Policy()

    class _Nested(Tool):
        __tool_name__ = 'Nested'
        __tool_command_args__ = ('command',)
        __tool_external_input__ = False

        async def execute(self, command: str, options: dict) -> ToolResult:  # type: ignore[type-arg]
            """执行 command。"""
            ran.append(command)
            return ToolResult(output='ran')

    ran: list[str] = []
    executor = ToolExecutor(tools={'Nested': _Nested()}, hooks=center, policy=policy)
    executor._configure_untrusted_output(frozenset(), frozenset({'thirdparty'}))  # pyright: ignore[reportPrivateUsage]

    await executor.execute_tool_calls(
        Context(),
        [
            ToolCall(
                id='n',
                name='Nested',
                arguments='{"command": "sudo ls", "options": {"flag": "none"}}',
            )
        ],
    )

    assert (ran, policy.seen) == ([], [])


class _Echo(Tool):
    __tool_name__ = 'Echo'
    __tool_command_args__ = ()
    __tool_external_input__ = False

    async def execute(self, text: str) -> ToolResult:
        """回显 text。"""
        return ToolResult(output=text)


async def test_the_outcome_observer_sees_executed_calls_even_when_a_post_hook_stops_the_chain():
    center = HooksCenter()

    async def stop_the_chain(**_: Any) -> HookResult:
        return HookResult(skip=True)

    async def never_reached(**_: Any) -> HookResult:
        raise AssertionError('链在前一个 hook 那里断了')

    center.register(Junction.POST_TOOL_USE, stop_the_chain)
    center.register(Junction.POST_TOOL_USE, never_reached)
    executor = ToolExecutor(tools={'Echo': _Echo()}, hooks=center)
    seen: list[tuple[str, dict[str, Any], str]] = []
    executor._observe_outcomes(  # pyright: ignore[reportPrivateUsage]
        lambda call, args, result: seen.append((call.id, dict(args), str(result.output)))
    )

    await executor.execute_tool_calls(
        Context(), [ToolCall(id='e', name='Echo', arguments='{"text": "hi"}')]
    )

    assert seen == [('e', {'text': 'hi'}, 'hi')]


async def test_a_call_skipped_before_it_ran_is_not_an_outcome():
    center = HooksCenter()

    async def skip(**_: Any) -> HookResult:
        return HookResult(skip=True, result=ToolResult(output='cached'))

    center.register(Junction.PRE_TOOL_USE, skip)
    executor = ToolExecutor(tools={'Echo': _Echo()}, hooks=center)
    seen: list[str] = []
    executor._observe_outcomes(lambda call, args, result: seen.append(call.id))  # pyright: ignore[reportPrivateUsage]

    await executor.execute_tool_calls(
        Context(), [ToolCall(id='e', name='Echo', arguments='{"text": "hi"}')]
    )

    assert seen == []


async def test_a_broken_observer_is_logged_and_the_call_still_lands(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = ToolExecutor(tools={'Echo': _Echo()}, hooks=HooksCenter())

    def broken(*_: Any) -> None:
        raise RuntimeError('observer bug')

    executor._observe_outcomes(broken)  # pyright: ignore[reportPrivateUsage]
    ctx = Context()
    with caplog.at_level(logging.ERROR, logger='syndica.kernel.tool_executor'):
        await executor.execute_tool_calls(
            ctx, [ToolCall(id='e', name='Echo', arguments='{"text": "hi"}')]
        )

    assert ctx.current_traj[-1].content == 'hi'
    assert [record.levelno for record in caplog.records] == [logging.ERROR]


def _extension_center(
    author: tuple[Literal['plugin', 'module'], str],
) -> tuple[HooksCenter, Any]:
    center = HooksCenter()
    center._tighten_modules(frozenset({'thirdparty'}))  # pyright: ignore[reportPrivateUsage]
    handle = center.declare_extension_junction('cache:lookup', JunctionKind.INTERCEPTABLE)

    async def answer(**_: Any) -> HookResult:
        return HookResult(modified={'value': 'from a cache'})

    with installing(*author):
        center.register(handle, answer)
    return center, handle


@pytest.mark.parametrize(
    ('author', 'untrusted'),
    [(('module', 'thirdparty'), True), (('plugin', 'trusted-plugin'), False)],
    ids=['tightened_module', 'trusted_plugin'],
)
async def test_an_extension_conclusion_written_by_a_tightened_module_says_so(
    author: tuple[Literal['plugin', 'module'], str], untrusted: bool
) -> None:
    center, handle = _extension_center(author)

    result = await center.trigger(handle, value=None)

    assert (result.get('value'), result.untrusted) == ('from a cache', untrusted)


async def test_a_tool_that_used_a_tightened_extension_conclusion_returns_an_untrusted_result() -> (
    None
):
    center, handle = _extension_center(('module', 'thirdparty'))

    class _Looker(Tool):
        __tool_name__ = 'Looker'
        __tool_command_args__ = ()
        __tool_external_input__ = False

        async def execute(self) -> ToolResult:
            """查缓存。"""
            found = await center.trigger(handle, value=None)
            return ToolResult(output=str(found.get('value')))

    executor = ToolExecutor(tools={'Looker': _Looker()}, hooks=HooksCenter())
    ctx = Context()

    await executor.execute_tool_calls(ctx, [ToolCall(id='l', name='Looker', arguments='{}')])

    assert ctx.current_traj[-1].trust is TrustLevel.UNTRUSTED


async def test_a_tightened_module_publishing_straight_to_the_broker_lands_untrusted() -> None:
    from syndica.kernel.hooks.installing import Installer, acting_as
    from syndica.kernel.message import Message
    from syndica.kernel.modules import RuntimeModule
    from syndica.kernel.runtime import Runtime

    runtime = Runtime(
        modules=[RuntimeModule('thirdparty')], untrusted_output_modules=['thirdparty']
    )
    try:
        await runtime.broker.ensure_queue('target')
        with acting_as(Installer('module', 'thirdparty')):
            await runtime.broker.publish(
                'target',
                Message(
                    sender='x',
                    receiver='target',
                    content='hello',
                    trust=TrustLevel.TRUSTED,
                    id='m1',
                ),
            )
        await runtime.broker.publish(
            'target',
            Message(
                sender='host', receiver='target', content='hi', trust=TrustLevel.TRUSTED, id='m2'
            ),
        )
        delivered = await runtime.broker.consume('target')
    finally:
        await runtime.shutdown()

    assert [(message.id, message.trust) for message in delivered] == [
        ('m1', TrustLevel.UNTRUSTED),
        ('m2', TrustLevel.TRUSTED),
    ]
