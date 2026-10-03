"""装配的事务边界（M4 D3）：清单切点先于一切第三方代码声明；任何异常（含 `KeyboardInterrupt`）都
按快照回滚；Agent 这一层挂不上只由 Runtime 触发的切点。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, NoReturn, cast

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.agent_types import AgentInfo
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import ExtensionJunction, HookResult, HooksCenter, Junction, JunctionKind
from syndica.kernel.hooks.installing import current_installer, installing
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.modules import RuntimeModule
from syndica.kernel.runtime import Runtime
from syndica.kernel.stores.memory_registry import InMemoryAgentRegistry
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


class _InheritsTheDeclaration(_Injected):
    pass


class _OwnToolInheritsTheDeclaration(BaseAgent):
    def install_own_tools(self, runtime: object) -> None:
        del runtime
        self.install_runtime_tools({'InheritsTheDeclaration': _InheritsTheDeclaration()})


def _cached(cls: type[BaseAgent], captured: list[BaseAgent]) -> Callable[..., BaseAgent]:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        if not captured:
            captured.append(
                cls(
                    AgentOptions(name='Worker', llm=ScriptedLLM([ScriptedTurn(content='done')])),
                    hooks=hooks,
                    events=events,
                )
            )
        return captured[0]

    return build


async def test_a_spawn_refused_after_the_module_plugins_went_in_takes_them_out_again():
    captured: list[BaseAgent] = []
    seen: list[str] = []

    def plugin(*_: object) -> _Plugin:
        return _Plugin(
            'module-hook',
            lambda hooks: hooks.register(Junction.PRE_LLM_REQUEST, _recorder(seen, 'module')),
        )

    runtime = Runtime(modules=[RuntimeModule('hooked', agent_plugins=(plugin,))])
    try:
        with pytest.raises(ConfigError):
            await runtime.spawn(_cached(_OwnToolInheritsTheDeclaration, captured))
        (agent,) = captured
        await agent._module_hooks._dispatch(  # pyright: ignore[reportPrivateUsage]
            Junction.PRE_LLM_REQUEST, ctx=None, request={'messages': []}
        )
        assert seen == []
        assert agent.options.tools == {}
    finally:
        await runtime.shutdown()


async def test_a_failed_spawn_takes_out_the_runtime_hooks_a_module_factory_registered():
    seen: list[str] = []

    def registers(runtime: Runtime, agent: object, flags: object) -> Mapping[str, Tool]:
        del agent, flags
        runtime.hooks.register(Junction.AGENT_DESPAWN, _recorder(seen, 'cleanup'))
        return {}

    def fails(*_: object) -> Mapping[str, Tool]:
        raise RuntimeError('boom')

    runtime = Runtime(
        modules=[
            RuntimeModule('first', tool_factories=(registers,)),
            RuntimeModule('second', tool_factories=(fails,)),
        ]
    )
    try:
        with pytest.raises(RuntimeError):
            await runtime.spawn(_factory([]))
        await runtime.hooks._dispatch(Junction.AGENT_DESPAWN, agent_id='a')  # pyright: ignore[reportPrivateUsage]
        assert seen == []
    finally:
        await runtime.shutdown()


class _RegistersOnTheRuntimeThenInstallsAnInheritedTool(BaseAgent):
    seen: list[str] = []

    def install_own_tools(self, runtime: object) -> None:
        assert isinstance(runtime, Runtime)
        runtime.hooks.register(Junction.AGENT_DESPAWN, _recorder(self.seen, 'own'))
        self.install_runtime_tools({'InheritsTheDeclaration': _InheritsTheDeclaration()})


async def test_a_failed_spawn_takes_out_the_runtime_hooks_install_own_tools_registered():
    seen: list[str] = []
    _RegistersOnTheRuntimeThenInstallsAnInheritedTool.seen = seen
    runtime = Runtime()
    try:
        with pytest.raises(ConfigError):
            await runtime.spawn(_cached(_RegistersOnTheRuntimeThenInstallsAnInheritedTool, []))
        await runtime.hooks._dispatch(Junction.AGENT_DESPAWN, agent_id='a')  # pyright: ignore[reportPrivateUsage]
        assert seen == []
    finally:
        await runtime.shutdown()


async def test_a_runtime_hook_registered_before_the_failed_spawn_stays():
    seen: list[str] = []

    def fails(*_: object) -> Mapping[str, Tool]:
        raise RuntimeError('boom')

    runtime = Runtime(modules=[RuntimeModule('failing', tool_factories=(fails,))])
    runtime.hooks.register(Junction.AGENT_DESPAWN, _recorder(seen, 'host'))
    try:
        with pytest.raises(RuntimeError):
            await runtime.spawn(_factory([]))
        await runtime.hooks._dispatch(Junction.AGENT_DESPAWN, agent_id='a')  # pyright: ignore[reportPrivateUsage]
        assert seen == ['host']
    finally:
        await runtime.shutdown()


def _built_elsewhere() -> BaseAgent:
    return BaseAgent(
        AgentOptions(name='Prebuilt', llm=ScriptedLLM([ScriptedTurn(content='done')])),
        hooks=HooksCenter(),
        events=EventBus(),
    )


async def test_an_agent_built_elsewhere_gets_the_module_layer_refusals_too():
    prebuilt = _built_elsewhere()

    def plugin(*_: object) -> _Plugin:
        return _Plugin(
            'misplaced', lambda hooks: hooks.register(Junction.AGENT_SPAWN, _recorder([], 'x'))
        )

    runtime = Runtime(modules=[RuntimeModule('misplaced', agent_plugins=(plugin,))])
    try:
        with pytest.raises(ContractViolation) as excinfo:
            await runtime.spawn(lambda hooks, events: prebuilt)
        assert 'Module 层' in excinfo.value.expected
    finally:
        await runtime.shutdown()


async def test_kernel_junctions_cannot_be_triggered_publicly_on_an_agent_built_elsewhere():
    prebuilt = _built_elsewhere()
    seen: list[str] = []
    prebuilt.register_hook(Junction.PRE_TOOL_USE, _recorder(seen, 'local'))
    runtime = Runtime()
    try:
        await runtime.spawn(lambda hooks, events: prebuilt)
        with pytest.raises(ContractViolation):
            await prebuilt._hooks.trigger(Junction.PRE_TOOL_USE, args={})  # pyright: ignore[reportPrivateUsage]
        assert seen == []
    finally:
        await runtime.shutdown()


async def test_an_agent_built_on_the_runtime_center_itself_is_refused():
    runtime = Runtime()

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        del hooks
        return BaseAgent(
            AgentOptions(name='Worker', llm=ScriptedLLM([ScriptedTurn(content='done')])),
            hooks=runtime.hooks,
            events=events,
        )

    async def noop(**_: object) -> None:
        return None

    try:
        with pytest.raises(ContractViolation) as excinfo:
            await runtime.spawn(build)
        assert '直接建在 runtime.hooks 上' in excinfo.value.received
        assert '工厂收到的第一个参数' in excinfo.value.next_action
        assert await runtime.registry.list() == []
        runtime.hooks.register(Junction.AGENT_SPAWN, noop)
    finally:
        await runtime.shutdown()


class _RefusesOnce(InMemoryAgentRegistry):
    """第一次注册在一次 await 之后失败；失败之前让 `meanwhile` 跑一下（别的 spawn 同时在装）。"""

    def __init__(self, meanwhile: Callable[[], None]) -> None:
        super().__init__()
        self._meanwhile = meanwhile
        self._refused = False

    async def register(self, info: AgentInfo) -> None:
        if not self._refused:
            self._refused = True
            self._meanwhile()
            await asyncio.sleep(0)
            raise RuntimeError('registry hiccup')
        await super().register(info)


async def test_a_spawn_that_fails_after_its_awaits_takes_out_only_its_own_runtime_hooks():
    seen: list[str] = []
    runtimes: list[Runtime] = []

    def registers(runtime: Runtime, agent: object, flags: object) -> Mapping[str, Tool]:
        del agent, flags
        runtime.hooks.register(Junction.AGENT_DESPAWN, _recorder(seen, 'failed-spawn'))
        return {}

    def meanwhile() -> None:
        runtimes[0].hooks.register(Junction.AGENT_DESPAWN, _recorder(seen, 'concurrent'))

    runtime = Runtime(
        modules=[RuntimeModule('first', tool_factories=(registers,))],
        registry=_RefusesOnce(meanwhile),
    )
    runtimes.append(runtime)
    try:
        with pytest.raises(RuntimeError, match='registry hiccup'):
            await runtime.spawn(_factory([]))
        await runtime.hooks._dispatch(Junction.AGENT_DESPAWN, agent_id='a')  # pyright: ignore[reportPrivateUsage]
        assert seen == ['concurrent']
    finally:
        await runtime.shutdown()


async def test_a_late_spawn_failure_leaves_hooks_that_were_there_before_it_even_the_same_callback():
    seen: list[str] = []
    shared = _recorder(seen, 'shared')

    def registers(runtime: Runtime, agent: object, flags: object) -> Mapping[str, Tool]:
        del agent, flags
        runtime.hooks.register(Junction.AGENT_DESPAWN, shared)
        return {}

    runtime = Runtime(
        modules=[RuntimeModule('first', tool_factories=(registers,))],
        registry=_RefusesOnce(lambda: None),
    )
    runtime.hooks.register(Junction.AGENT_DESPAWN, shared)
    try:
        with pytest.raises(RuntimeError, match='registry hiccup'):
            await runtime.spawn(_factory([]))
        chain = runtime.hooks._collect_authored(Junction.AGENT_DESPAWN)  # pyright: ignore[reportPrivateUsage]
        await runtime.hooks._dispatch(Junction.AGENT_DESPAWN, agent_id='a')  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()

    assert chain == [(shared, None)]
    assert seen == ['shared']


def _boom(*_: object) -> NoReturn:
    raise ValueError('third-party failure')


class _ExplodingPlugin:
    name = 'beta-hooks'

    def register(self, hooks: HooksCenter) -> None:
        raise ValueError('third-party failure')


@pytest.mark.parametrize(
    ('module', 'stage'),
    [
        (RuntimeModule('beta', services={'beta_store': _boom}), "service 'beta_store'"),
        (RuntimeModule('beta', on_start=lambda runtime, flags: _boom()), 'on_start'),
    ],
    ids=['service', 'on-start'],
)
def test_a_module_failing_while_the_runtime_starts_keeps_its_error_and_names_itself(
    module: RuntimeModule, stage: str
):
    with pytest.raises(ValueError, match='third-party failure') as excinfo:
        Runtime(modules=[module])

    assert excinfo.value.__notes__ == [f"Runtime Module 'beta' 的 {stage} 抛出了它"]


@pytest.mark.parametrize(
    ('module', 'stage'),
    [
        (RuntimeModule('beta', tool_factories=(_boom,)), 'tool_factories[0]'),
        (RuntimeModule('beta', agent_plugins=(_boom,)), 'agent_plugins[0]'),
        (
            RuntimeModule('beta', agent_plugins=(lambda *_: _ExplodingPlugin(),)),
            "插件 'beta-hooks' 的 register",
        ),
    ],
    ids=['tool-factory', 'plugin-factory', 'plugin-register'],
)
async def test_a_module_failing_while_an_agent_is_assembled_keeps_its_error_and_names_itself(
    module: RuntimeModule, stage: str
):
    runtime = Runtime(modules=[module])
    try:
        with pytest.raises(ValueError, match='third-party failure') as excinfo:
            await runtime.spawn(_factory([]))
    finally:
        await runtime.shutdown()

    assert excinfo.value.__notes__ == [f"Runtime Module 'beta' 的 {stage} 抛出了它"]


async def test_a_failing_on_shutdown_names_its_module_inside_the_group():
    async def fails(runtime: Runtime) -> None:
        del runtime
        raise ValueError('cannot flush')

    runtime = Runtime(modules=[RuntimeModule('beta', on_shutdown=fails)])

    with pytest.raises(ExceptionGroup) as excinfo:
        await runtime.shutdown()

    [failure] = excinfo.value.exceptions
    assert failure.__notes__ == ["Runtime Module 'beta' 的 on_shutdown 抛出了它"]


async def test_on_shutdown_runs_without_the_identity_of_whoever_called_shutdown():
    seen: list[object] = []

    async def record(runtime: Runtime) -> None:
        del runtime
        seen.append(current_installer())

    runtime = Runtime(modules=[RuntimeModule('beta', on_shutdown=record)])
    with installing('module', 'alpha'):
        await runtime.shutdown()

    assert seen == [None]


class _Echo(Tool):
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        return ToolResult(output='ok')


async def test_two_modules_handing_out_the_same_tool_name_are_refused():
    runtime = Runtime(
        modules=[
            RuntimeModule('first', tool_factories=(lambda *_: {'Echo': _Echo()},)),
            RuntimeModule('second', tool_factories=(lambda *_: {'Echo': _Echo()},)),
        ]
    )
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_factory([]))
    finally:
        await runtime.shutdown()

    assert '只新增工具' in excinfo.value.expected
    assert "'Echo'" in excinfo.value.received and "'second'" in excinfo.value.received


def test_two_modules_mounting_the_same_service_name_are_refused():
    with pytest.raises(ConfigError) as excinfo:
        Runtime(
            modules=[
                RuntimeModule('first', services={'shared_store': lambda *_: object()}),
                RuntimeModule('second', services={'shared_store': lambda *_: object()}),
            ]
        )

    assert "service 'shared_store'" in excinfo.value.received
    assert '前面的另一个 Runtime Module' in excinfo.value.received


async def test_a_tool_factory_replacing_an_existing_tool_in_place_is_refused():
    def replaces(runtime: Runtime, agent: BaseAgent, flags: object) -> Mapping[str, Tool]:
        del runtime, flags
        agent.options.tools['Echo'] = _Echo()
        return {}

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM([ScriptedTurn(content='done')]),
                tools={'Echo': _Echo()},
            ),
            hooks=hooks,
            events=events,
        )

    runtime = Runtime(modules=[RuntimeModule('first', tool_factories=(replaces,))])
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(build)
    finally:
        await runtime.shutdown()

    assert "['Echo']" in excinfo.value.received
    assert 'tool_factories' in excinfo.value.expected


class _PluginClass:
    name = 'beta-hooks'

    def register(self, hooks: HooksCenter) -> None:
        del hooks


async def _async_tools(*_: object) -> Mapping[str, Tool]:
    return {}


@pytest.mark.parametrize(
    ('field', 'anchor'),
    [('agent_plugins', '加上括号'), ('tool_factories', '同步返回工具 Mapping')],
    ids=['plugin-class', 'coroutine-from-a-tool-factory'],
)
async def test_a_factory_returning_the_wrong_kind_of_thing_says_what_to_return(
    field: str, anchor: str
):
    module = (
        RuntimeModule('beta', agent_plugins=(cast(Any, lambda *_: _PluginClass),))
        if field == 'agent_plugins'
        else RuntimeModule('beta', tool_factories=(cast(Any, lambda *_: _async_tools()),))
    )
    runtime = Runtime(modules=[module])
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_factory([]))
    finally:
        await runtime.shutdown()

    assert anchor in excinfo.value.expected + excinfo.value.next_action
