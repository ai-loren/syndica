"""装配的事务边界（M4 D3）：清单切点先于一切第三方代码声明；任何异常（含 `KeyboardInterrupt`）都
按快照回滚；Agent 这一层挂不上只由 Runtime 触发的切点。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.errors import ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import ExtensionJunction, HookResult, HooksCenter, Junction, JunctionKind
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.modules import RuntimeModule
from syndica.kernel.runtime import Runtime
from syndica.kernel.tool import Tool, ToolResult

_INTERRUPTIONS = [RuntimeError('boom'), KeyboardInterrupt(), asyncio.CancelledError()]


class _Injected(Tool):
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        return ToolResult(output='injected')


def _recorder(seen: list[str], label: str) -> Callable[..., Awaitable[HookResult]]:
    async def record(**_: object) -> HookResult:
        seen.append(label)
        return HookResult()

    return record


class _Plugin:
    def __init__(self, name: str, register: Callable[[HooksCenter], None]) -> None:
        self._name = name
        self._register = register

    @property
    def name(self) -> str:
        return self._name

    def register(self, hooks: HooksCenter) -> None:
        self._register(hooks)


def _factory(captured: list[BaseAgent]) -> Callable[[HooksCenter, EventBus], BaseAgent]:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        if not captured:
            captured.append(
                BaseAgent(
                    AgentOptions(name='Worker', llm=ScriptedLLM([ScriptedTurn(content='done')])),
                    hooks=hooks,
                    events=events,
                )
            )
        return captured[0]

    return build


async def test_a_runtime_plugin_can_hook_a_module_manifest_junction_in_its_register():
    seen: list[str] = []
    recorded = ExtensionJunction('audit:recorded', JunctionKind.OBSERVABLE)
    runtime = Runtime(
        plugins=[
            _Plugin(
                'watcher',
                lambda hooks: hooks.register('audit:recorded', _recorder(seen, 'watcher')),
            )
        ],
        modules=[RuntimeModule('audit', junctions=[recorded])],
    )
    try:
        await runtime.hooks.trigger(recorded)
        assert seen == ['watcher']
    finally:
        await runtime.shutdown()


async def test_every_manifest_junction_is_declared_before_the_first_service_factory_runs():
    seen: list[str] = []
    later = ExtensionJunction('ledger:posted', JunctionKind.OBSERVABLE)

    def service(runtime: Runtime, flags: Mapping[str, object]) -> object:
        del flags
        runtime.hooks.register('ledger:posted', _recorder(seen, 'early-service'))
        return object()

    runtime = Runtime(
        modules=[
            RuntimeModule('audit', services={'audit_log': service}),
            RuntimeModule('ledger', junctions=[later]),
        ]
    )
    try:
        await runtime.hooks.trigger(later)
        assert seen == ['early-service']
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize('raised', _INTERRUPTIONS, ids=lambda error: type(error).__name__)
async def test_an_interrupted_start_rolls_back_every_service_and_start_hook(raised: BaseException):
    captured: list[Runtime] = []
    seen: list[str] = []

    def first_start(runtime: Runtime, flags: Mapping[str, object]) -> None:
        del flags
        captured.append(runtime)
        runtime.hooks.register(Junction.AGENT_SPAWN, _recorder(seen, 'first'))

    def second_start(runtime: Runtime, flags: Mapping[str, object]) -> None:
        del runtime, flags
        raise raised

    with pytest.raises(type(raised)):
        Runtime(
            plugins=[
                _Plugin(
                    'kept',
                    lambda hooks: hooks.register(Junction.AGENT_SPAWN, _recorder(seen, 'plugin')),
                )
            ],
            modules=[
                RuntimeModule(
                    'first', services={'first_log': lambda *_: object()}, on_start=first_start
                ),
                RuntimeModule(
                    'second', services={'second_log': lambda *_: object()}, on_start=second_start
                ),
            ],
        )

    (runtime,) = captured
    assert not hasattr(runtime, 'first_log')
    assert not hasattr(runtime, 'second_log')
    await runtime.hooks._dispatch(Junction.AGENT_SPAWN, agent_id='a', parent_id=None)
    assert seen == ['plugin']


@pytest.mark.parametrize('raised', _INTERRUPTIONS, ids=lambda error: type(error).__name__)
def test_an_interrupted_service_factory_unmounts_the_services_before_it(raised: BaseException):
    captured: list[Runtime] = []

    def first(runtime: Runtime, flags: Mapping[str, object]) -> object:
        del flags
        captured.append(runtime)
        return object()

    def second(runtime: Runtime, flags: Mapping[str, object]) -> object:
        del runtime, flags
        raise raised

    with pytest.raises(type(raised)):
        Runtime(
            modules=[RuntimeModule('audit', services={'first_log': first, 'second_log': second})]
        )

    (runtime,) = captured
    assert not hasattr(runtime, 'first_log')


@pytest.mark.parametrize('raised', _INTERRUPTIONS, ids=lambda error: type(error).__name__)
async def test_an_interrupted_module_tool_factory_restores_the_agent(raised: BaseException):
    captured: list[BaseAgent] = []
    seen: list[str] = []

    def tools(runtime: object, agent: object, flags: object) -> Mapping[str, Tool]:
        del runtime, agent, flags
        raise raised

    def plugin(runtime: object, agent: object, flags: object) -> _Plugin:
        del runtime, agent, flags
        return _Plugin(
            'module-hook',
            lambda hooks: hooks.register(Junction.PRE_LLM_REQUEST, _recorder(seen, 'module')),
        )

    runtime = Runtime(
        modules=[
            RuntimeModule('hooked', agent_plugins=(plugin,)),
            RuntimeModule('failing', tool_factories=(tools,)),
        ],
        orchestration=True,
    )
    try:
        with pytest.raises(type(raised)):
            await runtime.spawn(_factory(captured))
        (agent,) = captured
        assert agent.options.tools == {}
        assert agent._gate_attached is False  # pyright: ignore[reportPrivateUsage]
        assert await runtime.registry.list() == []
        await agent._module_hooks._dispatch(  # pyright: ignore[reportPrivateUsage]
            Junction.PRE_LLM_REQUEST, ctx=None, request={'messages': []}
        )
        assert seen == []
    finally:
        await runtime.shutdown()


class _ExplodingOwnTools(BaseAgent):
    def install_own_tools(self, runtime: object) -> None:
        del runtime
        self.install_runtime_tools({'Injected': _Injected()})
        raise KeyboardInterrupt


async def test_an_interrupted_install_own_tools_restores_the_tools_installed_before_it():
    captured: list[BaseAgent] = []

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _ExplodingOwnTools(
            AgentOptions(name='Worker', llm=ScriptedLLM([ScriptedTurn(content='done')])),
            hooks=hooks,
            events=events,
        )
        captured.append(agent)
        return agent

    runtime = Runtime(
        modules=[RuntimeModule('extra', tool_factories=(lambda *_: {'Injected2': _Injected2()},))],
        orchestration=True,
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            await runtime.spawn(build)
        (agent,) = captured
        assert agent.options.tools == {}
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


class _Injected2(Tool):
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        return ToolResult(output='injected')


@pytest.mark.parametrize(
    'junction',
    [Junction.AGENT_SPAWN, Junction.AGENT_DESPAWN, Junction.RUNTIME_SHUTDOWN],
    ids=lambda junction: junction.name,
)
async def test_a_module_plugin_cannot_hook_a_runtime_only_junction(junction: Junction):
    captured: list[BaseAgent] = []
    seen: list[str] = []

    def plugin(runtime: object, agent: object, flags: object) -> _Plugin:
        del runtime, agent, flags
        return _Plugin('misplaced', lambda hooks: hooks.register(junction, _recorder(seen, 'x')))

    runtime = Runtime(modules=[RuntimeModule('misplaced', agent_plugins=(plugin,))])
    try:
        with pytest.raises(ContractViolation) as excinfo:
            await runtime.spawn(_factory(captured))
        assert junction.value in excinfo.value.expected
        assert 'Module 层' in excinfo.value.expected
        assert f'runtime.hooks.register(Junction.{junction.name}' in excinfo.value.next_action
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize(
    'junction',
    [Junction.AGENT_SPAWN, Junction.AGENT_DESPAWN, Junction.RUNTIME_SHUTDOWN],
    ids=lambda junction: junction.name,
)
def test_the_local_layer_refuses_runtime_only_junctions_even_without_register_hook(
    junction: Junction,
):
    agent = BaseAgent(
        AgentOptions(name='Worker', llm=ScriptedLLM([ScriptedTurn(content='done')])),
        hooks=HooksCenter(),
        events=EventBus(),
    )

    async def noop(**_: object) -> None:
        return None

    with pytest.raises(ContractViolation) as excinfo:
        agent._hooks.register(junction, noop)  # pyright: ignore[reportPrivateUsage]
    assert junction.value in excinfo.value.expected


def test_a_root_center_still_takes_runtime_only_junctions():
    async def noop(**_: object) -> None:
        return None

    center = HooksCenter()
    for junction in (Junction.AGENT_SPAWN, Junction.AGENT_DESPAWN, Junction.RUNTIME_SHUTDOWN):
        center.register(junction, noop)
