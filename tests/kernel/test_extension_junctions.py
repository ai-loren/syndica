"""扩展切点的归属（M4 D5 / D6 / D8）：谁能声明在装配时判，谁能触发靠持有声明时交回的句柄。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.agent_types import ExitMode
from syndica.kernel.context import ContextBlock
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import ExtensionJunction, HookResult, HooksCenter, Junction, JunctionKind
from syndica.kernel.hooks.installing import installing
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.modules import RuntimeModule
from syndica.kernel.plugins import install_plugins
from syndica.kernel.runtime import Runtime
from syndica.kernel.tool import Tool, ToolResult

_TIMEOUT = 5


def _three_parts(error: BaseException) -> None:
    text = str(error)
    assert '期望:' in text
    assert '实际:' in text
    assert '下一步:' in text


def _recorder(seen: list[str], label: str) -> Callable[..., Awaitable[HookResult]]:
    async def record(**_: object) -> HookResult:
        seen.append(label)
        return HookResult()

    return record


class _Plugin:
    def __init__(self, name: str, register: Callable[[HooksCenter], object]) -> None:
        self._name = name
        self._register = register

    @property
    def name(self) -> str:
        return self._name

    def register(self, hooks: HooksCenter) -> None:
        self._register(hooks)


def _agent_factory(
    captured: list[BaseAgent], tools: Mapping[str, Tool] | None = None
) -> Callable[[HooksCenter, EventBus], BaseAgent]:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM([ScriptedTurn(content='done')]),
                tools=dict(tools or {}),
            ),
            hooks=hooks,
            events=events,
        )
        captured.append(agent)
        return agent

    return build


async def test_declaring_hands_back_the_handle_that_triggers_the_junction():
    center = HooksCenter()
    seen: list[str] = []
    handle = center.declare_extension_junction('audit:recorded', JunctionKind.OBSERVABLE)
    center.register('audit:recorded', _recorder(seen, 'by-name'))
    center.register(handle, _recorder(seen, 'by-handle'))
    assert (handle.name, handle.kind) == ('audit:recorded', JunctionKind.OBSERVABLE)
    await center.trigger(handle)
    assert sorted(seen) == ['by-handle', 'by-name']


@pytest.mark.parametrize('junction', list(Junction), ids=lambda junction: junction.name)
async def test_the_runtime_center_refuses_every_kernel_junction(junction: Junction):
    runtime = Runtime()
    seen: list[str] = []
    runtime.hooks.register(junction, _recorder(seen, 'kernel'))
    try:
        with pytest.raises(ContractViolation) as excinfo:
            await runtime.hooks.trigger(junction)  # pyright: ignore[reportArgumentType]
        assert '内核切点只由内核在固定位置触发' in excinfo.value.expected
        assert f'Junction.{junction.name}' in excinfo.value.received
        assert '有一层属于 Runtime' in excinfo.value.received
        assert seen == []
        _three_parts(excinfo.value)
    finally:
        await runtime.shutdown()


async def test_a_spawned_agents_module_layer_refuses_kernel_junctions():
    captured: list[BaseAgent] = []
    runtime = Runtime()
    try:
        await runtime.spawn(_agent_factory(captured))
        module_layer = captured[0]._module_hooks  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(ContractViolation):
            await module_layer.trigger(Junction.TURN_START)
    finally:
        await runtime.shutdown()


async def test_a_host_center_chained_to_a_runtime_refuses_kernel_junctions():
    runtime = Runtime()
    seen: list[str] = []
    runtime.hooks.register(Junction.PRE_TOOL_USE, _recorder(seen, 'policy'))
    inner = HooksCenter(outer=runtime.hooks)
    try:
        with pytest.raises(ContractViolation):
            await inner.trigger(Junction.PRE_TOOL_USE)
        assert seen == []
    finally:
        await runtime.shutdown()


async def test_a_free_standing_center_triggers_kernel_junctions_for_plugin_tests():
    outer = HooksCenter()
    inner = HooksCenter(outer=outer)
    seen: list[str] = []
    inner.register(Junction.PRE_TOOL_USE, _recorder(seen, 'inner'))
    outer.register(Junction.PRE_TOOL_USE, _recorder(seen, 'outer'))
    await inner.trigger(Junction.PRE_TOOL_USE, args={})
    assert seen == ['inner', 'outer']


async def test_a_runtime_level_trigger_does_not_reach_an_agents_own_layers():
    recorded = ExtensionJunction('audit:recorded', JunctionKind.OBSERVABLE)
    seen: list[str] = []
    captured: list[BaseAgent] = []

    def plugin(*_: object) -> _Plugin:
        return _Plugin(
            'audit-hook', lambda hooks: hooks.register(recorded, _recorder(seen, 'module'))
        )

    runtime = Runtime(
        modules=[RuntimeModule('audit', junctions=[recorded], agent_plugins=(plugin,))]
    )
    runtime.hooks.register(recorded, _recorder(seen, 'runtime'))
    try:
        await runtime.spawn(_agent_factory(captured))
        captured[0].register_hook(recorded, _recorder(seen, 'local'))
        await runtime.hooks.trigger(recorded)
        assert seen == ['runtime']
        seen.clear()
        await captured[0].trigger_extension(recorded)
        assert sorted(seen) == ['local', 'module', 'runtime']
    finally:
        await runtime.shutdown()


async def test_triggering_a_declared_junction_by_its_name_is_refused():
    center = HooksCenter()
    seen: list[str] = []
    center.declare_extension_junction('audit:recorded', JunctionKind.OBSERVABLE)
    center.register('audit:recorded', _recorder(seen, 'hook'))
    with pytest.raises(ContractViolation) as excinfo:
        await center.trigger('audit:recorded')  # pyright: ignore[reportArgumentType]
    assert '用声明时交回的那个 ExtensionJunction 触发' in excinfo.value.expected
    assert "'audit:recorded'" in excinfo.value.received
    assert 'declare_extension_junction 的返回值' in excinfo.value.next_action
    assert seen == []


async def test_a_handle_built_by_someone_else_with_the_same_name_is_refused():
    center = HooksCenter()
    seen: list[str] = []
    center.declare_extension_junction('audit:recorded', JunctionKind.OBSERVABLE)
    center.register('audit:recorded', _recorder(seen, 'hook'))
    forged = ExtensionJunction('audit:recorded', JunctionKind.OBSERVABLE)
    with pytest.raises(ContractViolation) as excinfo:
        await center.trigger(forged)
    assert '声明时交回的那个句柄' in excinfo.value.expected
    assert '装配期之外的代码' in excinfo.value.received
    assert '按对象身份比较' in excinfo.value.next_action
    assert seen == []


async def test_a_handle_that_was_never_declared_is_refused():
    with pytest.raises(ContractViolation) as excinfo:
        await HooksCenter().trigger(ExtensionJunction('audit:recorded', JunctionKind.OBSERVABLE))
    assert '已经声明过' in excinfo.value.expected


async def test_an_inner_center_triggers_with_the_handle_its_outer_center_declared():
    outer = HooksCenter()
    inner = HooksCenter(outer=outer)
    seen: list[str] = []
    handle = outer.declare_extension_junction('audit:recorded', JunctionKind.INTERCEPTABLE)
    inner.register('audit:recorded', _recorder(seen, 'inner'))
    outer.register('audit:recorded', _recorder(seen, 'outer'))
    await inner.trigger(handle)
    assert seen == ['inner', 'outer']


def test_a_module_name_is_a_reserved_owner_for_host_code():
    runtime = Runtime(
        modules=[
            RuntimeModule(
                'audit',
                junctions=[ExtensionJunction('audit:recorded', JunctionKind.OBSERVABLE)],
            )
        ]
    )
    for name in ('audit:recorded', 'audit:other'):
        with pytest.raises(ContractViolation) as excinfo:
            runtime.hooks.declare_extension_junction(name, JunctionKind.OBSERVABLE)
        assert "'audit' 是已装 Module 的名字" in excinfo.value.expected
        assert repr(name) in excinfo.value.received
        assert "RuntimeModule('audit', junctions=...)" in excinfo.value.next_action
        _three_parts(excinfo.value)


def test_a_module_name_is_reserved_even_when_its_manifest_declares_nothing():
    runtime = Runtime(modules=[RuntimeModule('audit')])
    with pytest.raises(ContractViolation):
        runtime.hooks.declare_extension_junction('audit:recorded', JunctionKind.OBSERVABLE)


def test_an_agent_layer_center_inherits_the_reserved_owners():
    runtime = Runtime(modules=[RuntimeModule('audit')])
    inner = HooksCenter(outer=runtime.hooks)
    with pytest.raises(ContractViolation):
        inner.declare_extension_junction('audit:recorded', JunctionKind.OBSERVABLE)


async def test_host_code_declares_any_owner_that_no_module_holds():
    runtime = Runtime(modules=[RuntimeModule('audit')])
    try:
        handle = runtime.hooks.declare_extension_junction('host:pinged', JunctionKind.OBSERVABLE)
        assert runtime.hooks.extension_junctions() == {'host:pinged': JunctionKind.OBSERVABLE}
        await runtime.hooks.trigger(handle)
    finally:
        await runtime.shutdown()


async def test_a_runtime_plugin_declares_under_its_own_name_and_keeps_the_handle():
    held: list[ExtensionJunction] = []
    seen: list[str] = []

    def register(hooks: HooksCenter) -> None:
        held.append(hooks.declare_extension_junction('watcher:noticed', JunctionKind.OBSERVABLE))
        hooks.register('watcher:noticed', _recorder(seen, 'watcher'))

    runtime = Runtime(plugins=[_Plugin('watcher', register)])
    try:
        await runtime.hooks.trigger(held[0])
        assert seen == ['watcher']
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize('name', ['other:noticed', 'watcher-x:noticed', 'watche:noticed'])
def test_a_runtime_plugin_cannot_declare_under_another_owner(name: str):
    def register(hooks: HooksCenter) -> None:
        hooks.declare_extension_junction(name, JunctionKind.OBSERVABLE)

    with pytest.raises(ContractViolation) as excinfo:
        Runtime(plugins=[_Plugin('watcher', register)])
    assert "插件 'watcher' 在 register 里声明的切点，owner 段是 'watcher'" in excinfo.value.expected
    assert repr(name) in excinfo.value.received
    assert 'watcher:<event>' in excinfo.value.next_action
    _three_parts(excinfo.value)


@pytest.mark.parametrize('plugin', ['Watcher', 'my_watcher', 'watcher--x'])
def test_a_runtime_plugin_with_a_non_canonical_name_cannot_declare(plugin: str):
    def register(hooks: HooksCenter) -> None:
        hooks.declare_extension_junction(f'{plugin}:noticed', JunctionKind.OBSERVABLE)

    with pytest.raises(ContractViolation) as excinfo:
        Runtime(plugins=[_Plugin(plugin, register)])
    assert 'Module 名的规范拼法' in excinfo.value.expected
    assert repr(plugin) in excinfo.value.received


def test_a_runtime_plugin_named_after_an_installed_module_cannot_declare_its_junctions():
    def register(hooks: HooksCenter) -> None:
        hooks.declare_extension_junction('audit:noticed', JunctionKind.OBSERVABLE)

    with pytest.raises(ContractViolation) as excinfo:
        Runtime(plugins=[_Plugin('audit', register)], modules=[RuntimeModule('audit')])
    assert "'audit' 是已装 Module 的名字" in excinfo.value.expected


def _declare_from(site: str) -> RuntimeModule:
    def declare(*_: object) -> Any:
        hooks = runtime_holder[0].hooks
        hooks.declare_extension_junction('ledger:posted', JunctionKind.OBSERVABLE)
        return {} if site == 'tool_factories' else object()

    runtime_holder: list[Runtime] = []

    def keep(runtime: Runtime, flags: object) -> object:
        del flags
        runtime_holder.append(runtime)
        return object()

    def plugin(*_: object) -> _Plugin:
        return _Plugin(
            'ledger-plugin',
            lambda hooks: hooks.declare_extension_junction(
                'ledger:posted', JunctionKind.OBSERVABLE
            ),
        )

    if site == 'services':
        return RuntimeModule(
            'ledger',
            services={
                'ledger_log': lambda runtime, flags: runtime.hooks.declare_extension_junction(
                    'ledger:posted', JunctionKind.OBSERVABLE
                )
            },
        )
    if site == 'on_start':

        def on_start(runtime: Runtime, flags: object) -> None:
            del flags
            runtime.hooks.declare_extension_junction('ledger:posted', JunctionKind.OBSERVABLE)

        return RuntimeModule('ledger', on_start=on_start)
    if site == 'agent_plugins':
        return RuntimeModule('ledger', agent_plugins=(plugin,))
    if site == 'plugin_factories':
        return RuntimeModule(
            'ledger',
            services={'keep': keep},
            agent_plugins=(lambda *_: (declare(), _Plugin('quiet', lambda hooks: None))[1],),
        )
    return RuntimeModule('ledger', services={'keep': keep}, tool_factories=(declare,))


@pytest.mark.parametrize(
    'site', ['services', 'on_start', 'agent_plugins', 'plugin_factories', 'tool_factories']
)
async def test_module_code_declares_its_junctions_in_the_manifest_not_at_runtime(site: str):
    module = _declare_from(site)
    with pytest.raises(ContractViolation) as excinfo:
        runtime = Runtime(modules=[module])
        try:
            await runtime.spawn(_agent_factory([]))
        finally:
            await runtime.shutdown()
    assert "Module 'ledger' 的扩展切点写在它的清单 junctions 里" in excinfo.value.expected
    assert "'ledger:posted'" in excinfo.value.received
    assert "RuntimeModule('ledger', junctions=...)" in excinfo.value.next_action
    _three_parts(excinfo.value)


async def test_a_module_tool_triggers_its_manifest_junction_on_the_agents_chain():
    recorded = ExtensionJunction('audit:recorded', JunctionKind.INTERCEPTABLE)
    seen: list[str] = []

    class _Record(Tool):
        __tool_command_args__ = ()

        def __init__(self, agent: BaseAgent) -> None:
            self._agent = agent

        async def execute(self) -> ToolResult:
            outcome = await self._agent.trigger_extension(recorded, entry='x')
            return ToolResult(output=str(outcome.modified))

    def tools(runtime: object, agent: object, flags: object) -> Mapping[str, Tool]:
        del runtime, flags
        assert isinstance(agent, BaseAgent)
        agent.register_hook('audit:recorded', _recorder(seen, 'local'))
        return {'Record': _Record(agent)}

    def watcher(hooks: HooksCenter) -> None:
        hooks.register('audit:recorded', _recorder(seen, 'runtime'))

    runtime = Runtime(
        plugins=[_Plugin('watcher', watcher)],
        modules=[RuntimeModule('audit', junctions=[recorded], tool_factories=(tools,))],
    )
    captured: list[BaseAgent] = []
    try:
        await runtime.spawn(_agent_factory(captured))
        tool = captured[0].options.tools['Record']
        await tool.execute()
        assert seen == ['local', 'runtime']
    finally:
        await runtime.shutdown()


async def test_trigger_extension_refuses_kernel_junctions_and_foreign_handles():
    captured: list[BaseAgent] = []
    runtime = Runtime(
        modules=[
            RuntimeModule(
                'audit', junctions=[ExtensionJunction('audit:recorded', JunctionKind.OBSERVABLE)]
            )
        ]
    )
    try:
        await runtime.spawn(_agent_factory(captured))
        agent = captured[0]
        with pytest.raises(ContractViolation):
            await agent.trigger_extension(Junction.TURN_START)  # pyright: ignore[reportArgumentType]
        with pytest.raises(ContractViolation):
            await agent.trigger_extension(
                ExtensionJunction('audit:recorded', JunctionKind.OBSERVABLE)
            )
    finally:
        await runtime.shutdown()


async def test_the_driver_task_does_not_inherit_the_installer_that_spawned_it():
    outcomes: list[str] = []
    runtime = Runtime()

    async def declare(**_: object) -> HookResult:
        runtime.hooks.declare_extension_junction('host:started', JunctionKind.OBSERVABLE)
        outcomes.append('declared')
        return HookResult()

    captured: list[BaseAgent] = []
    factory = _agent_factory(captured)

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = factory(hooks, events)
        agent.register_hook(Junction.AGENT_START, declare)
        return agent

    try:
        with installing('plugin', 'spawner'):
            agent_id = await runtime.spawn(build, query='go')
        await runtime.wait(agent_id)
        assert outcomes == ['declared']
    finally:
        await runtime.shutdown()


async def test_the_shutdown_task_does_not_inherit_the_installer_of_its_caller():
    outcomes: list[str] = []
    runtime = Runtime()

    async def declare(**_: object) -> None:
        runtime.hooks.declare_extension_junction('host:stopped', JunctionKind.OBSERVABLE)
        outcomes.append('declared')

    runtime.hooks.register(Junction.RUNTIME_SHUTDOWN, declare)
    with installing('plugin', 'stopper'):
        await runtime.shutdown()
    assert outcomes == ['declared']


class _BlockingLLM(ScriptedLLM):
    def __init__(self) -> None:
        super().__init__([ScriptedTurn(content='never')])
        self.entered = asyncio.Event()

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError('unreachable')


async def test_the_reaper_does_not_inherit_the_installer_of_the_killer():
    declared = asyncio.Event()
    runtime = Runtime()
    llm = _BlockingLLM()

    async def declare(**_: object) -> None:
        runtime.hooks.declare_extension_junction('host:reaped', JunctionKind.OBSERVABLE)
        declared.set()

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(AgentOptions(name='Worker', llm=llm), hooks=hooks, events=events)

    runtime.hooks.register(Junction.AGENT_DESPAWN, declare)
    try:
        agent_id = await runtime.spawn(build, query='go', exit_mode=ExitMode.EXIT)
        await asyncio.wait_for(llm.entered.wait(), _TIMEOUT)
        with installing('plugin', 'killer'):
            await runtime.kill(agent_id)
        await asyncio.wait_for(declared.wait(), _TIMEOUT)
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize('cb', [None, 'on_event', 1], ids=['None', 'str', 'int'])
def test_register_refuses_a_callback_that_cannot_be_called(cb: object):
    with pytest.raises(ConfigError) as excinfo:
        HooksCenter().register(Junction.TURN_START, cb)  # pyright: ignore[reportArgumentType]
    assert '可调用对象' in excinfo.value.expected
    assert repr(cb) in excinfo.value.received
    assert '不是 on_event()' in excinfo.value.next_action


def test_register_refuses_the_result_of_calling_the_callback():
    async def on_event(**_: object) -> None:
        return None

    pending = on_event()
    try:
        with pytest.raises(ConfigError):
            HooksCenter().register(Junction.TURN_START, pending)  # pyright: ignore[reportArgumentType]
    finally:
        pending.close()


@pytest.mark.parametrize('returned', [{'skip': True}, True, 'ok'], ids=['dict', 'bool', 'str'])
async def test_an_interceptable_hook_must_return_a_hook_result_or_none(returned: object):
    center = HooksCenter()

    async def wrong(**_: object) -> object:
        return returned

    center.register(Junction.PRE_TOOL_USE, wrong)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ContractViolation) as excinfo:
        await center._dispatch(Junction.PRE_TOOL_USE, args={})  # pyright: ignore[reportPrivateUsage]
    assert 'HookResult 或 None' in excinfo.value.expected
    assert type(returned).__name__ in excinfo.value.received
    assert 'HookResult(modified=' in excinfo.value.next_action


async def test_an_interceptable_hook_must_be_awaitable():
    center = HooksCenter()

    def synchronous(**_: object) -> None:
        return None

    center.register(Junction.PRE_TOOL_USE, synchronous)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ContractViolation) as excinfo:
        await center._dispatch(Junction.PRE_TOOL_USE, args={})  # pyright: ignore[reportPrivateUsage]
    assert '可等待对象' in excinfo.value.expected
    assert 'async def' in excinfo.value.next_action


async def test_an_observable_hook_that_raises_before_awaiting_fails_alone(
    caplog: pytest.LogCaptureFixture,
):
    center = HooksCenter()
    seen: list[str] = []

    def explodes(**_: object) -> None:
        raise RuntimeError('raised while building the coroutine')

    center.register(Junction.TURN_START, _recorder(seen, 'before'))
    center.register(Junction.TURN_START, explodes)  # pyright: ignore[reportArgumentType]
    center.register(Junction.TURN_START, _recorder(seen, 'after'))
    with caplog.at_level(logging.ERROR):
        await center._dispatch(Junction.TURN_START, turn=1)  # pyright: ignore[reportPrivateUsage]
    assert sorted(seen) == ['after', 'before']
    (record,) = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert record.args == ('turn_start',)
    assert record.exc_info is not None


async def test_an_observable_hook_that_returns_no_awaitable_is_reported_alone(
    caplog: pytest.LogCaptureFixture,
):
    center = HooksCenter()
    seen: list[str] = []

    def synchronous(**_: object) -> str:
        return 'done'

    center.register(Junction.TURN_START, _recorder(seen, 'before'))
    center.register(Junction.TURN_START, synchronous)  # pyright: ignore[reportArgumentType]
    center.register(Junction.TURN_START, _recorder(seen, 'after'))
    with caplog.at_level(logging.ERROR):
        await center._dispatch(Junction.TURN_START, turn=1)  # pyright: ignore[reportPrivateUsage]
    assert sorted(seen) == ['after', 'before']
    (record,) = [record for record in caplog.records if record.levelno == logging.ERROR]
    args = cast(tuple[object, ...], record.args)
    assert args[0] == 'turn_start'
    assert args[2:] == ('str', 'done')


async def test_an_observable_hook_interrupted_before_awaiting_still_interrupts():
    center = HooksCenter()

    def interrupted(**_: object) -> None:
        raise KeyboardInterrupt

    center.register(Junction.TURN_START, interrupted)  # pyright: ignore[reportArgumentType]
    with pytest.raises(KeyboardInterrupt):
        await center._dispatch(Junction.TURN_START, turn=1)  # pyright: ignore[reportPrivateUsage]


def test_extension_junctions_lists_the_whole_chain_sorted_and_read_only():
    outer = HooksCenter()
    inner = HooksCenter(outer=outer)
    outer.declare_extension_junction('zeta:fired', JunctionKind.OBSERVABLE)
    inner.declare_extension_junction('alpha:fired', JunctionKind.INTERCEPTABLE)
    outer.declare_extension_junction('mid:fired', JunctionKind.INTERCEPTABLE)
    listed = inner.extension_junctions()
    assert list(listed.items()) == [
        ('alpha:fired', JunctionKind.INTERCEPTABLE),
        ('mid:fired', JunctionKind.INTERCEPTABLE),
        ('zeta:fired', JunctionKind.OBSERVABLE),
    ]
    assert list(outer.extension_junctions()) == ['mid:fired', 'zeta:fired']
    with pytest.raises(TypeError):
        listed['beta:fired'] = JunctionKind.OBSERVABLE  # pyright: ignore[reportIndexIssue]


def test_extension_junctions_include_the_module_manifests():
    runtime = Runtime(
        modules=[
            RuntimeModule(
                'audit', junctions=[ExtensionJunction('audit:recorded', JunctionKind.OBSERVABLE)]
            )
        ]
    )
    assert dict(runtime.hooks.extension_junctions()) == {'audit:recorded': JunctionKind.OBSERVABLE}


def test_the_hook_center_surface_is_exactly_this():
    assert sorted(name for name in dir(HooksCenter()) if not name.startswith('_')) == [
        'declare_extension_junction',
        'extension_junctions',
        'register',
        'trigger',
    ]


def test_a_failed_plugin_assembly_takes_back_the_junctions_declared_before_it():
    hooks = HooksCenter()

    def declares(center: HooksCenter) -> None:
        center.declare_extension_junction('first:fired', JunctionKind.OBSERVABLE)

    def explodes(center: HooksCenter) -> None:
        raise RuntimeError('third-party register failed')

    with pytest.raises(RuntimeError):
        install_plugins(hooks, [_Plugin('first', declares), _Plugin('second', explodes)])  # pyright: ignore[reportArgumentType]
    assert dict(hooks.extension_junctions()) == {}
    hooks.declare_extension_junction('first:fired', JunctionKind.OBSERVABLE)


async def test_triggering_something_that_is_no_junction_points_at_the_declared_handle():
    with pytest.raises(ConfigError) as excinfo:
        await HooksCenter().trigger(42)  # pyright: ignore[reportArgumentType]
    _three_parts(excinfo.value)
    assert 'int 类型的 42' in excinfo.value.received
    assert 'declare_extension_junction 交回的句柄' in excinfo.value.next_action


async def test_triggering_an_undeclared_name_is_refused_for_being_a_string():
    with pytest.raises(ContractViolation) as excinfo:
        await HooksCenter().trigger('audit:never')  # pyright: ignore[reportArgumentType]
    assert "收到字符串 'audit:never'" in excinfo.value.received
    assert 'declare_extension_junction 的返回值' in excinfo.value.next_action


def test_register_hook_with_an_unhashable_junction_gets_the_centers_three_part_error():
    agent = BaseAgent(
        AgentOptions(name='Worker', llm=ScriptedLLM([ScriptedTurn(content='done')])),
        hooks=HooksCenter(),
        events=EventBus(),
    )

    async def noop(**_: object) -> None:
        return None

    with pytest.raises(ConfigError) as excinfo:
        agent.register_hook(['x'], noop)  # pyright: ignore[reportArgumentType]
    _three_parts(excinfo.value)
    assert "['x']" in excinfo.value.received
