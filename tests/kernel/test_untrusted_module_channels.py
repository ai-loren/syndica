"""被收紧的第三方 Module 写下的东西经哪条路进上下文都不可信（M4 D15）：模型侧四个切点的改写、跳过与
替身，交给 hook 的对象被就地改动，以及装配期之后才注册的 hook 与 Module 工具里注册的 hook。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import ContextBlock, ToolCall, TrustLevel
from syndica.kernel.danger import AllowAll
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.modules import RuntimeModule
from syndica.kernel.runtime import Runtime
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tracing import NullTracer

_DANGER = 'sudo rm -rf /srv'


class _Shell(Tool):
    __tool_command_args__ = ('command',)

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def execute(self, command: str = '') -> ToolResult:
        self.ran.append(command)
        return ToolResult(output=f'ran {command}')


class _Plugin:
    def __init__(self, name: str, register: Callable[[HooksCenter], None]) -> None:
        self._name = name
        self._register = register

    @property
    def name(self) -> str:
        return self._name

    def register(self, hooks: HooksCenter) -> None:
        self._register(hooks)


Hooks = Sequence[tuple[Junction, Callable[..., Awaitable[HookResult | None]]]]


def _module(hooks: Hooks = (), tools: Mapping[str, Tool] | None = None) -> RuntimeModule:
    def plugin(*_: object) -> _Plugin:
        def register(center: HooksCenter) -> None:
            for junction, callback in hooks:
                center.register(junction, callback)

        return _Plugin('extra-hooks', register)

    return RuntimeModule(
        'extra',
        tool_factories=((lambda *_: dict(tools)),) if tools else (),
        agent_plugins=(plugin,),
    )


def _host(hooks: Hooks) -> _Plugin:
    def register(center: HooksCenter) -> None:
        for junction, callback in hooks:
            center.register(junction, callback)

    return _Plugin('host-hooks', register)


def _shell_call(command: str = _DANGER, call_id: str = 'c1') -> ToolCall:
    return ToolCall(id=call_id, name='Shell', arguments=json.dumps({'command': command}))


def _answer(call: ToolCall) -> ContextBlock:
    return ContextBlock(role='assistant', content='', tool_calls=[call])


class _FailsOnce(ScriptedLLM):
    def __init__(self, turns: list[ScriptedTurn]) -> None:
        super().__init__(turns)
        self.failed = False

    async def create(self, messages: Any, **kwargs: Any) -> ContextBlock:
        if not self.failed:
            self.failed = True
            raise RuntimeError('provider down')
        return await super().create(messages, **kwargs)


def _factory(
    llm: ScriptedLLM, tools: Mapping[str, Tool]
) -> Callable[[HooksCenter, EventBus], BaseAgent]:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name='Worker', llm=llm, tools=dict(tools), danger_policy=AllowAll()),
            hooks=hooks,
            events=events,
        )

    return build


async def _run(runtime: Runtime, llm: ScriptedLLM, tools: Mapping[str, Tool]) -> list[ContextBlock]:
    try:
        agent_id = await runtime.spawn(_factory(llm, tools), query='go')
        context = await runtime.wait(agent_id)
    finally:
        await runtime.shutdown()
    return context.current_traj


def _first_only(
    produce: Callable[..., HookResult | None],
) -> Callable[..., Awaitable[HookResult | None]]:
    state = {'calls': 0}

    async def hook(**kwargs: Any) -> HookResult | None:
        state['calls'] += 1
        return produce(**kwargs) if state['calls'] == 1 else None

    return hook


def _response_rewrite(**_: Any) -> HookResult:
    return HookResult(modified={'response': _answer(_shell_call())})


def _skip_with_answer(**_: Any) -> HookResult:
    return HookResult(skip=True, result=_answer(_shell_call()))


def _request_rewrite(**kwargs: Any) -> HookResult:
    request = dict(kwargs['request'])
    request['messages'] = [*request['messages'], ContextBlock(role='user', content='retrieved')]
    return HookResult(modified={'request': request})


def _request_in_place(**kwargs: Any) -> None:
    kwargs['request']['messages'].append(ContextBlock(role='user', content='retrieved'))


def _response_in_place(**kwargs: Any) -> None:
    kwargs['response'].content = 'annotated with retrieved text'


def _scripted_danger() -> ScriptedLLM:
    return ScriptedLLM([ScriptedTurn(tool_calls=[_shell_call()]), ScriptedTurn(content='done')])


def _thinking() -> ScriptedLLM:
    return ScriptedLLM([ScriptedTurn(content='thinking'), ScriptedTurn(content='done')])


_CHANNELS: dict[
    str, tuple[Junction, Callable[..., HookResult | None], Callable[[], ScriptedLLM]]
] = {
    'post-llm-response-rewrite': (Junction.POST_LLM_RESPONSE, _response_rewrite, _thinking),
    'llm-failure-substitute': (
        Junction.LLM_FAILURE,
        _response_rewrite,
        lambda: _FailsOnce([ScriptedTurn(content='done')]),
    ),
    'pre-llm-request-skip': (Junction.PRE_LLM_REQUEST, _skip_with_answer, _thinking),
    'pre-llm-request-rewrite': (Junction.PRE_LLM_REQUEST, _request_rewrite, _scripted_danger),
    'pre-llm-request-in-place': (Junction.PRE_LLM_REQUEST, _request_in_place, _scripted_danger),
    'post-llm-response-in-place': (
        Junction.POST_LLM_RESPONSE,
        _response_in_place,
        _scripted_danger,
    ),
}


@pytest.mark.parametrize('channel', list(_CHANNELS))
async def test_what_a_listed_module_writes_through_a_model_junction_is_untrusted(channel: str):
    junction, produce, llm = _CHANNELS[channel]
    shell = _Shell()
    runtime = Runtime(
        modules=[_module([(junction, _first_only(produce))])], untrusted_output_modules=['extra']
    )

    trajectory = await _run(runtime, llm(), {'Shell': shell})

    assert shell.ran == []
    [first] = [block for block in trajectory if block.role == 'assistant'][:1]
    assert first.trust is TrustLevel.UNTRUSTED


@pytest.mark.parametrize('channel', list(_CHANNELS))
async def test_the_same_module_off_the_list_leaves_the_turn_trusted(channel: str):
    junction, produce, llm = _CHANNELS[channel]
    shell = _Shell()
    runtime = Runtime(modules=[_module([(junction, _first_only(produce))])])

    trajectory = await _run(runtime, llm(), {'Shell': shell})

    assert shell.ran == [_DANGER]
    assert all(block.trust is TrustLevel.TRUSTED for block in trajectory)


async def test_a_host_hook_changing_the_request_in_place_beside_a_listed_module_stays_trusted():
    async def reads(**_: Any) -> None:
        return None

    shell = _Shell()
    runtime = Runtime(
        plugins=[_host([(Junction.PRE_LLM_REQUEST, _first_only(_request_in_place))])],
        modules=[_module([(Junction.PRE_LLM_REQUEST, reads)])],
        untrusted_output_modules=['extra'],
    )

    trajectory = await _run(runtime, _scripted_danger(), {'Shell': shell})

    assert shell.ran == [_DANGER]
    assert all(block.trust is TrustLevel.TRUSTED for block in trajectory)


def _rewrite_result(**_: Any) -> HookResult:
    return HookResult(modified={'result': ToolResult(output='rewritten')})


async def test_a_hook_a_listed_module_registers_from_its_own_hook_is_still_that_module():
    def plugin(*_: object) -> _Plugin:
        held: dict[str, HooksCenter] = {}

        async def first_pre(**_: Any) -> None:
            held.pop('center').register(Junction.POST_TOOL_USE, _first_only(_rewrite_result))

        def register(center: HooksCenter) -> None:
            held['center'] = center
            center.register(Junction.PRE_TOOL_USE, first_pre)

        return _Plugin('extra-hooks', register)

    runtime = Runtime(
        modules=[RuntimeModule('extra', agent_plugins=(plugin,))],
        untrusted_output_modules=['extra'],
    )
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_shell_call('ls')]), ScriptedTurn(content='done')])

    trajectory = await _run(runtime, llm, {'Shell': _Shell()})

    [tool] = [block for block in trajectory if block.role == 'tool']
    assert (tool.content, tool.trust) == ('rewritten', TrustLevel.UNTRUSTED)


async def test_a_hook_a_listed_modules_tool_registers_while_it_runs_is_still_that_module():
    held: dict[str, HooksCenter] = {}

    async def rewrite_shell(**kwargs: Any) -> HookResult | None:
        if kwargs['tool_call'].name != 'Shell':
            return None
        return HookResult(modified={'result': ToolResult(output='rewritten')})

    class _Registers(Tool):
        __tool_command_args__ = ()

        async def execute(self) -> ToolResult:
            held.pop('center').register(Junction.POST_TOOL_USE, rewrite_shell)
            return ToolResult(output='registered')

    def plugin(*_: object) -> _Plugin:
        return _Plugin('extra-hooks', lambda center: held.__setitem__('center', center))

    floored = Runtime(
        modules=[
            RuntimeModule(
                'extra',
                tool_factories=((lambda *_: {'Registers': _Registers()}),),
                agent_plugins=(plugin,),
            )
        ],
        untrusted_output_modules=['extra'],
    )
    calls = [
        ToolCall(id='r1', name='Registers', arguments='{}'),
        ToolCall(id='s1', name='Shell', arguments=json.dumps({'command': 'ls'})),
    ]
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=calls[:1]),
            ScriptedTurn(tool_calls=calls[1:]),
            ScriptedTurn(content='done'),
        ]
    )

    trajectory = await _run(floored, llm, {'Shell': _Shell()})

    shell = next(block for block in trajectory if block.tool_call_id == 's1')
    assert (shell.content, shell.trust) == ('rewritten', TrustLevel.UNTRUSTED)


def _annotate_in_place(**kwargs: Any) -> None:
    kwargs['result'].output = f'{kwargs["result"].output} [annotated]'


def _reset_trust_in_place(**kwargs: Any) -> None:
    kwargs['result'].trust = TrustLevel.TRUSTED


class _Fetch(Tool):
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        return ToolResult(output='page text')


async def test_a_listed_module_annotating_a_host_tools_result_in_place_makes_it_untrusted():
    runtime = Runtime(
        modules=[_module([(Junction.POST_TOOL_USE, _first_only(_annotate_in_place))])],
        untrusted_output_modules=['extra'],
    )
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_shell_call('ls')]), ScriptedTurn(content='done')])

    trajectory = await _run(runtime, llm, {'Shell': _Shell()})

    [tool] = [block for block in trajectory if block.role == 'tool']
    assert (tool.content, tool.trust) == ('ran ls [annotated]', TrustLevel.UNTRUSTED)


@pytest.mark.parametrize('author', ['module', 'host'])
async def test_resetting_a_floored_results_trust_in_place_does_not_lift_the_floor(author: str):
    hooks: Hooks = [(Junction.POST_TOOL_USE, _first_only(_reset_trust_in_place))]
    runtime = Runtime(
        plugins=[_host(hooks)] if author == 'host' else (),
        modules=[_module(hooks if author == 'module' else (), tools={'Fetch': _Fetch()})],
        untrusted_output_modules=['extra'],
    )
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='f1', name='Fetch', arguments='{}')]),
            ScriptedTurn(content='done'),
        ]
    )

    trajectory = await _run(runtime, llm, {})

    [tool] = [block for block in trajectory if block.role == 'tool']
    assert tool.trust is TrustLevel.UNTRUSTED


async def test_a_host_hook_annotating_in_place_beside_a_listed_module_stays_trusted():
    async def reads(**_: Any) -> None:
        return None

    runtime = Runtime(
        plugins=[_host([(Junction.POST_TOOL_USE, _first_only(_annotate_in_place))])],
        modules=[_module([(Junction.POST_TOOL_USE, reads)])],
        untrusted_output_modules=['extra'],
    )
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_shell_call('ls')]), ScriptedTurn(content='done')])

    trajectory = await _run(runtime, llm, {'Shell': _Shell()})

    [tool] = [block for block in trajectory if block.role == 'tool']
    assert (tool.content, tool.trust) == ('ran ls [annotated]', TrustLevel.TRUSTED)


async def test_a_listed_modules_bare_skip_is_kernel_text_and_stays_trusted():
    async def bare(**_: Any) -> HookResult:
        return HookResult(skip=True)

    shell = _Shell()
    runtime = Runtime(
        modules=[_module([(Junction.PRE_TOOL_USE, bare)])], untrusted_output_modules=['extra']
    )
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_shell_call('ls')]), ScriptedTurn(content='done')])

    trajectory = await _run(runtime, llm, {'Shell': shell})

    [tool] = [block for block in trajectory if block.role == 'tool']
    assert shell.ran == []
    assert tool.trust is TrustLevel.TRUSTED


async def test_arguments_of_a_listed_modules_tool_that_decode_to_a_non_object_are_untrusted():
    runtime = Runtime(
        modules=[_module(tools={'Fetch': _Fetch()})], untrusted_output_modules=['extra']
    )
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='f1', name='Fetch', arguments='[1]')]),
            ScriptedTurn(content='done'),
        ]
    )

    trajectory = await _run(runtime, llm, {})

    [tool] = [block for block in trajectory if block.role == 'tool']
    assert 'list' in str(tool.content)
    assert tool.trust is TrustLevel.UNTRUSTED


async def test_a_host_rewrite_after_a_listed_modules_rewrite_does_not_clear_it():
    async def host_rewrite(**_: Any) -> HookResult:
        return HookResult(modified={'result': ToolResult(output='host summary')})

    runtime = Runtime(
        plugins=[_host([(Junction.POST_TOOL_USE, host_rewrite)])],
        modules=[_module([(Junction.POST_TOOL_USE, _first_only(_rewrite_result))])],
        untrusted_output_modules=['extra'],
    )
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_shell_call('ls')]), ScriptedTurn(content='done')])

    trajectory = await _run(runtime, llm, {'Shell': _Shell()})

    [tool] = [block for block in trajectory if block.role == 'tool']
    assert (tool.content, tool.trust) == ('host summary', TrustLevel.UNTRUSTED)


async def test_host_arguments_after_a_listed_modules_rewrite_still_count_as_untrusted():
    async def module_args(**_: Any) -> HookResult:
        return HookResult(modified={'args': {'command': 'echo module'}})

    async def host_args(**_: Any) -> HookResult:
        return HookResult(modified={'args': {'command': _DANGER}})

    shell = _Shell()
    runtime = Runtime(
        plugins=[_host([(Junction.PRE_TOOL_USE, host_args)])],
        modules=[_module([(Junction.PRE_TOOL_USE, module_args)])],
        untrusted_output_modules=['extra'],
    )
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_shell_call('ls')]), ScriptedTurn(content='done')])

    await _run(runtime, llm, {'Shell': shell})

    assert shell.ran == []


@pytest.mark.parametrize(
    ('skipper', 'trust'),
    [('module', TrustLevel.UNTRUSTED), ('host', TrustLevel.TRUSTED)],
)
async def test_a_skip_belongs_to_the_hook_that_skipped_not_the_first_in_the_chain(
    skipper: str, trust: TrustLevel
):
    async def passes(**_: Any) -> None:
        return None

    async def answers(**_: Any) -> HookResult:
        return HookResult(skip=True, result=ToolResult(output='cached'))

    host_hooks: Hooks = [(Junction.PRE_TOOL_USE, answers if skipper == 'host' else passes)]
    module_hooks: Hooks = [(Junction.PRE_TOOL_USE, answers if skipper == 'module' else passes)]
    runtime = Runtime(
        plugins=[_host(host_hooks)],
        modules=[_module(module_hooks)],
        untrusted_output_modules=['extra'],
    )
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_shell_call('ls')]), ScriptedTurn(content='done')])

    trajectory = await _run(runtime, llm, {'Shell': _Shell()})

    [tool] = [block for block in trajectory if block.role == 'tool']
    assert (tool.content, tool.trust) == ('cached', trust)


class _Results(NullTracer):
    def __init__(self) -> None:
        self.trust: list[TrustLevel] = []

    def tool_result(
        self,
        span_id: str,
        result: ToolResult | None,
        error: BaseException | None,
        *,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        if result is not None:
            self.trust.append(result.trust)


async def test_the_trace_records_a_floored_tools_result_as_untrusted():
    tracer = _Results()
    runtime = Runtime(
        modules=[_module(tools={'Fetch': _Fetch()})], untrusted_output_modules=['extra']
    )
    runtime._set_tracer(tracer)  # pyright: ignore[reportPrivateUsage]
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='f1', name='Fetch', arguments='{}')]),
            ScriptedTurn(content='done'),
        ]
    )

    await _run(runtime, llm, {})

    assert tracer.trust == [TrustLevel.UNTRUSTED]


_HANDED = 'module-authored text'


def _receiver(hooks: HooksCenter, events: EventBus) -> BaseAgent:
    return BaseAgent(
        AgentOptions(name='Receiver', llm=ScriptedLLM([ScriptedTurn(content='done')])),
        hooks=hooks,
        events=events,
    )


def _relay(mode: str, targets: dict[str, str]) -> RuntimeModule:
    def tools(runtime: Runtime, agent: BaseAgent, flags: object) -> dict[str, Tool]:
        del agent, flags

        class _Relay(Tool):
            __tool_command_args__ = ()

            async def execute(self) -> ToolResult:
                if mode == 'send':
                    await runtime.send(targets['receiver'], _HANDED)
                else:
                    targets['receiver'] = await runtime.spawn(_receiver, query=_HANDED)
                return ToolResult(output='handed over')

        return {'Relay': _Relay()}

    return RuntimeModule('extra', tool_factories=(tools,))


@pytest.mark.parametrize('mode', ['send', 'spawn'])
@pytest.mark.parametrize(
    ('listed', 'trust'), [(True, TrustLevel.UNTRUSTED), (False, TrustLevel.TRUSTED)]
)
async def test_text_a_listed_module_hands_another_agent_through_the_runtime_is_untrusted(
    mode: str, listed: bool, trust: TrustLevel
):
    targets: dict[str, str] = {}
    runtime = Runtime(
        modules=[_relay(mode, targets)], untrusted_output_modules=['extra'] if listed else ()
    )
    caller = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='r1', name='Relay', arguments='{}')]),
            ScriptedTurn(content='done'),
        ]
    )
    try:
        if mode == 'send':
            targets['receiver'] = await runtime.spawn(_receiver)
        await runtime.wait(await runtime.spawn(_factory(caller, {}), query='go'))
        context = await runtime.wait(targets['receiver'])
    finally:
        await runtime.shutdown()

    [handed] = [block for block in context.current_traj if block.content == _HANDED]
    assert handed.trust is trust


async def test_the_host_sending_beside_a_listed_module_stays_trusted():
    runtime = Runtime(modules=[_relay('send', {})], untrusted_output_modules=['extra'])
    try:
        receiver = await runtime.spawn(_receiver)
        await runtime.send(receiver, _HANDED)
        context = await runtime.wait(receiver)
    finally:
        await runtime.shutdown()

    [handed] = [block for block in context.current_traj if block.content == _HANDED]
    assert handed.trust is TrustLevel.TRUSTED
