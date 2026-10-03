"""被收紧的第三方 Module 写下的东西经哪条路进上下文都不可信（M4 D15）：模型侧四个切点的改写、跳过与
替身，交给 hook 的对象被就地改动，以及装配期之后才注册的 hook 与 Module 工具里注册的 hook。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from typing import Any

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import ContextBlock, ToolCall, TrustLevel
from syndica.kernel.danger import AllowAll
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction, JunctionKind
from syndica.kernel.hooks.installing import installing
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.modules import RuntimeModule
from syndica.kernel.runtime import Runtime
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tools import Fork
from syndica.kernel.topology import AgentSlot, Topology
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


_HELD = 'held by the role guard'


async def _hold(**_: Any) -> HookResult:
    return HookResult(skip=True, result=ToolResult(output=_HELD))


def _probing_turns() -> list[ScriptedTurn]:
    return [ScriptedTurn(tool_calls=[_shell_call('ls')]), ScriptedTurn(content='done')]


def _guarded(
    *, declares: bool = False, turns: Iterator[list[ScriptedTurn]] | None = None
) -> Callable[[HooksCenter, EventBus], BaseAgent]:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        hooks.register(Junction.PRE_TOOL_USE, _hold)
        if declares:
            hooks.declare_extension_junction('host:started', JunctionKind.OBSERVABLE)
        script = _probing_turns() if turns is None else next(turns)
        return BaseAgent(
            AgentOptions(
                name='Child',
                llm=ScriptedLLM(script),
                tools={'Shell': _Shell()},
                danger_policy=AllowAll(),
            ),
            hooks=hooks,
            events=events,
        )

    return build


class _GuardsItself(BaseAgent):
    def install_own_tools(self, runtime: object) -> None:
        del runtime
        self.register_hook(Junction.PRE_TOOL_USE, _hold)


def _class_role(hooks: HooksCenter, events: EventBus) -> BaseAgent:
    return _GuardsItself(
        AgentOptions(
            name='Child',
            llm=ScriptedLLM(_probing_turns()),
            tools={'Shell': _Shell()},
            danger_policy=AllowAll(),
        ),
        hooks=hooks,
        events=events,
    )


def _acting(action: Callable[[Runtime], Awaitable[None]]) -> RuntimeModule:
    def tools(runtime: Runtime, agent: BaseAgent, flags: object) -> dict[str, Tool]:
        del agent, flags

        class _Acts(Tool):
            __tool_command_args__ = ()

            async def execute(self) -> ToolResult:
                await action(runtime)
                return ToolResult(output='acted')

        return {'Acts': _Acts()}

    return RuntimeModule('extra', tool_factories=(tools,))


async def _act_as_the_module(runtime: Runtime) -> None:
    caller = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='p1', name='Acts', arguments='{}')]),
            ScriptedTurn(content='done'),
        ]
    )
    await runtime.wait(await runtime.spawn(_factory(caller, {}), query='go'))


async def _held_trust(runtime: Runtime, agent_id: str) -> TrustLevel:
    context = await runtime.wait(agent_id)
    [held] = [block for block in context.current_traj if block.role == 'tool']
    assert held.content == _HELD
    return held.trust


def _lazy_slot() -> Topology:
    return Topology(
        slots=(AgentSlot(name='worker', agent='Child', cardinality='singleton', boot='lazy'),)
    )


async def test_a_role_the_host_registered_runs_as_the_host_when_a_listed_module_spawns_it():
    spawned: dict[str, str] = {}

    async def spawn_role(runtime: Runtime) -> None:
        factory = runtime.agent_factory('Child')
        assert factory is not None
        spawned['child'] = await runtime.spawn(factory, query='go')

    runtime = Runtime(
        agents={'Child': _guarded(declares=True)},
        modules=[_acting(spawn_role)],
        untrusted_output_modules=['extra'],
    )
    try:
        await _act_as_the_module(runtime)
        assert await _held_trust(runtime, spawned['child']) is TrustLevel.TRUSTED
    finally:
        await runtime.shutdown()


async def test_a_factory_a_listed_module_hands_to_spawn_itself_runs_as_that_module():
    spawned: dict[str, str] = {}

    async def spawn_own(runtime: Runtime) -> None:
        spawned['child'] = await runtime.spawn(_guarded(), query='go')

    runtime = Runtime(modules=[_acting(spawn_own)], untrusted_output_modules=['extra'])
    try:
        await _act_as_the_module(runtime)
        assert await _held_trust(runtime, spawned['child']) is TrustLevel.UNTRUSTED
    finally:
        await runtime.shutdown()


async def test_a_slot_a_listed_module_wakes_runs_its_role_as_the_host():
    async def wake(runtime: Runtime) -> None:
        await runtime.send_to_slot('worker', 'go')

    runtime = Runtime(modules=[_acting(wake)], untrusted_output_modules=['extra'])
    try:
        await runtime.assemble(_lazy_slot(), {'Child': _guarded(declares=True)}, tools={})
        await _act_as_the_module(runtime)
        worker = await runtime.resolve('worker')
        assert worker is not None
        assert await _held_trust(runtime, worker) is TrustLevel.TRUSTED
    finally:
        await runtime.shutdown()


async def test_a_spawn_from_a_hook_while_a_slot_boots_does_not_take_the_slot_identity():
    spawned: dict[str, str] = {}
    runtime = Runtime(modules=[RuntimeModule('extra')], untrusted_output_modules=['extra'])

    async def spawn_own(**_: Any) -> None:
        if spawned:
            return
        spawned['child'] = ''
        spawned['child'] = await runtime.spawn(_guarded(), query='go')

    try:
        await runtime.assemble(_lazy_slot(), {'Child': _guarded()}, tools={})
        with installing('module', 'extra'):
            runtime.hooks.register(Junction.AGENT_SPAWN, spawn_own)
        await runtime.send_to_slot('worker', 'go')
        assert await _held_trust(runtime, spawned['child']) is TrustLevel.UNTRUSTED
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize('route', ['slot', 'agent_factory'])
async def test_a_role_assembled_by_module_code_runs_as_that_module(route: str):
    runtime = Runtime(modules=[RuntimeModule('extra')], untrusted_output_modules=['extra'])
    try:
        with installing('module', 'extra'):
            await runtime.assemble(_lazy_slot(), {'Child': _guarded()}, tools={})
        if route == 'slot':
            await runtime.send_to_slot('worker', 'go')
            agent_id = await runtime.resolve('worker')
        else:
            factory = runtime.agent_factory('Child')
            assert factory is not None
            agent_id = await runtime.spawn(factory, query='go')
        assert agent_id is not None
        assert await _held_trust(runtime, agent_id) is TrustLevel.UNTRUSTED
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize(
    ('registered', 'trust'), [(True, TrustLevel.TRUSTED), (False, TrustLevel.UNTRUSTED)]
)
async def test_a_class_role_installs_its_own_hooks_under_the_identity_of_its_factory(
    registered: bool, trust: TrustLevel
):
    spawned: dict[str, str] = {}

    async def spawn_role(runtime: Runtime) -> None:
        factory = runtime.agent_factory('Child') if registered else _class_role
        assert factory is not None
        spawned['child'] = await runtime.spawn(factory, query='go')

    runtime = Runtime(
        agents={'Child': _class_role} if registered else {},
        modules=[_acting(spawn_role)],
        untrusted_output_modules=['extra'],
    )
    try:
        await _act_as_the_module(runtime)
        assert await _held_trust(runtime, spawned['child']) is trust
    finally:
        await runtime.shutdown()


async def test_a_fork_reruns_the_factory_under_the_identity_its_source_ran_under():
    spawned: dict[str, str] = {}
    own = _guarded(turns=iter([[ScriptedTurn(content='source done')], _probing_turns()]))

    async def spawn_own(runtime: Runtime) -> None:
        spawned['source'] = await runtime.spawn(own, query='go')

    runtime = Runtime(modules=[_acting(spawn_own)], untrusted_output_modules=['extra'])
    try:
        await _act_as_the_module(runtime)
        await runtime.wait(spawned['source'])
        forked = await Fork(runtime, spawned['source']).execute(query='onward')
        assert await _held_trust(runtime, forked.meta['agent_id']) is TrustLevel.UNTRUSTED
    finally:
        await runtime.shutdown()


async def test_forking_with_another_factory_does_not_borrow_the_identity_of_the_source():
    spawned: dict[str, str] = {}

    async def fork_with_own(runtime: Runtime) -> None:
        spawned['twin'] = await runtime.spawn(
            _guarded(), query='onward', fork_from=spawned['source']
        )

    runtime = Runtime(modules=[_acting(fork_with_own)], untrusted_output_modules=['extra'])
    try:
        source = _factory(ScriptedLLM([ScriptedTurn(content='source done')]), {})
        spawned['source'] = await runtime.spawn(source, query='go')
        await runtime.wait(spawned['source'])
        await _act_as_the_module(runtime)
        assert await _held_trust(runtime, spawned['twin']) is TrustLevel.UNTRUSTED
    finally:
        await runtime.shutdown()
