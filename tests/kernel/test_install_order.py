"""装配次序与 hook 执行次序（M4 D2）。工具定义的次序进请求前缀；hook 的次序决定谁先看见、先改。

每组至少三个、且既非升序也非降序：两个元素时排序与反转里总有一个是恒等变换。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.catalog import AgentCatalog
from syndica.kernel.context import ContextBlock
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import LLM, ScriptedLLM, ScriptedTurn
from syndica.kernel.modules import RuntimeModule
from syndica.kernel.runtime import Runtime
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.topology import AgentSlot, Topology

_ORCHESTRATION = ['Delegate', 'Fork', 'SendMessage', 'QueryAgent', 'KillAgent']


def _tool_class(name: str) -> type[Tool]:
    async def execute(self: Tool) -> ToolResult:
        del self
        return ToolResult(output=name)

    return type(name, (Tool,), {'__tool_command_args__': (), 'execute': execute})


def _tools(*names: str) -> dict[str, Tool]:
    return {name: _tool_class(name)() for name in names}


class _RecordingLLM(ScriptedLLM):
    def __init__(self) -> None:
        super().__init__([ScriptedTurn(content='done')])
        self.tool_names: list[list[str]] = []

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        self.tool_names.append([schema['name'] for schema in kwargs['tools']])
        return await super().create(messages, **kwargs)


class _OwnTools(BaseAgent):
    """自己装三件工具的类式角色。"""

    def install_own_tools(self, runtime: object) -> None:
        del runtime
        self.install_runtime_tools(_tools('Romeo', 'Charlie', 'November'))


def _catalog(*, extends_class: bool = True) -> AgentCatalog:
    catalog = AgentCatalog()
    body: dict[str, object] = {
        'name': 'Role',
        'description': 'Role',
        'tools': ['Kilo', 'Alpha', 'Mike'],
    }
    if extends_class:
        body['impl'] = _OwnTools
    catalog.load_mapping(body)
    return catalog


def _modules() -> list[RuntimeModule]:
    return [
        RuntimeModule('mod-q', tool_factories=(lambda *_: _tools('Quebec'),)),
        RuntimeModule('mod-b', tool_factories=(lambda *_: _tools('Sierra', 'Bravo', 'Lima'),)),
        RuntimeModule('mod-x', tool_factories=(lambda *_: _tools('Xray'),)),
    ]


def _slot(name: str, *extra: str) -> AgentSlot:
    return AgentSlot(
        name=name, agent='Role', cardinality='singleton', boot='lazy', extra_tools=extra
    )


async def _assemble(
    runtime: Runtime,
    topology: Topology,
    catalog: AgentCatalog,
    llm: LLM,
    tools: Mapping[str, Tool],
) -> None:
    await runtime.assemble(topology, catalog, tools=tools, models={'default': llm})


def _agent(runtime: Runtime, agent_id: str) -> BaseAgent:
    agent = runtime._table.get(agent_id)  # pyright: ignore[reportPrivateUsage]
    assert agent is not None
    return agent


async def test_a_slot_agent_installs_role_orchestration_module_slot_then_own_tools():
    llm = _RecordingLLM()
    runtime = Runtime(orchestration=True, modules=_modules())
    try:
        await _assemble(
            runtime,
            Topology(slots=(_slot('one', 'Papa', 'Delta', 'Tango'),)),
            _catalog(),
            llm,
            _tools('Kilo', 'Alpha', 'Mike', 'Papa', 'Delta', 'Tango'),
        )
        agent_id = await runtime.occupy('one')
        await runtime.send(agent_id, 'go')
        await runtime.wait(agent_id)
        expected = [
            'Kilo',
            'Alpha',
            'Mike',
            *_ORCHESTRATION,
            'Quebec',
            'Sierra',
            'Bravo',
            'Lima',
            'Xray',
            'Papa',
            'Delta',
            'Tango',
            'Romeo',
            'Charlie',
            'November',
        ]
        assert list(_agent(runtime, agent_id).options.tools) == expected
        assert llm.tool_names == [expected]
    finally:
        await runtime.shutdown()


async def test_one_role_in_two_slots_shares_every_tool_before_the_slot_tools():
    llm = _RecordingLLM()
    runtime = Runtime(orchestration=True, modules=_modules())
    try:
        await _assemble(
            runtime,
            Topology(slots=(_slot('left', 'Papa'), _slot('right', 'Tango', 'Delta', 'Echo'))),
            _catalog(extends_class=False),
            llm,
            _tools('Kilo', 'Alpha', 'Mike', 'Papa', 'Delta', 'Tango', 'Echo'),
        )
        left = list(_agent(runtime, await runtime.occupy('left')).options.tools)
        right = list(_agent(runtime, await runtime.occupy('right')).options.tools)
        assert left[:-1] == right[:-3]
        assert (left[-1:], right[-3:]) == (['Papa'], ['Tango', 'Delta', 'Echo'])
    finally:
        await runtime.shutdown()


async def test_a_fork_of_a_slot_agent_gets_the_same_tools_in_the_same_order():
    runtime = Runtime(orchestration=True, modules=_modules())
    try:
        await _assemble(
            runtime,
            Topology(slots=(_slot('one', 'Papa', 'Delta', 'Tango'),)),
            _catalog(),
            _RecordingLLM(),
            _tools('Kilo', 'Alpha', 'Mike', 'Papa', 'Delta', 'Tango'),
        )
        source = await runtime.occupy('one')
        factory = runtime._factory_of(source)  # pyright: ignore[reportPrivateUsage]
        assert factory is not None
        fork = await runtime.spawn(factory, fork_from=source)
        assert list(_agent(runtime, fork).options.tools) == list(
            _agent(runtime, source).options.tools
        )
    finally:
        await runtime.shutdown()


async def test_a_slot_tool_named_like_a_module_tool_names_that_module():
    runtime = Runtime(modules=_modules())
    try:
        await _assemble(
            runtime,
            Topology(slots=(_slot('one', 'Papa', 'Bravo'),)),
            _catalog(extends_class=False),
            _RecordingLLM(),
            _tools('Kilo', 'Alpha', 'Mike', 'Papa', 'Bravo'),
        )
        with pytest.raises(ConfigError) as excinfo:
            await runtime.occupy('one')
        assert "槽 'one'" in excinfo.value.expected
        assert "Bravo（Module 'mod-b' 的工具）" in excinfo.value.received
        assert 'extra_tools 里去掉' in excinfo.value.next_action
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


async def test_a_slot_tool_named_like_an_orchestration_tool_says_so():
    runtime = Runtime(orchestration=True)
    try:
        await _assemble(
            runtime,
            Topology(slots=(_slot('one', 'Fork'),)),
            _catalog(extends_class=False),
            _RecordingLLM(),
            _tools('Kilo', 'Alpha', 'Mike', 'Fork'),
        )
        with pytest.raises(ConfigError) as excinfo:
            await runtime.occupy('one')
        assert 'Fork（编排工具）' in excinfo.value.received
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


async def test_a_slot_tool_named_like_a_role_tool_is_refused_as_a_replacement():
    runtime = Runtime(orchestration=True, modules=_modules())
    try:
        await _assemble(
            runtime,
            Topology(slots=(_slot('one', 'Papa', 'Alpha'),)),
            _catalog(extends_class=False),
            _RecordingLLM(),
            _tools('Kilo', 'Alpha', 'Mike', 'Papa'),
        )
        with pytest.raises(ConfigError) as excinfo:
            await runtime.occupy('one')
        assert "角色 'Role' 上：['Alpha']" in excinfo.value.received
        assert '增强不得替换' in excinfo.value.received
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


def _recorder(order: list[str], label: str) -> Callable[..., Awaitable[HookResult]]:
    async def record(**_: object) -> HookResult:
        order.append(label)
        return HookResult()

    return record


class _Plugin:
    def __init__(self, label: str, order: list[str]) -> None:
        self._label = label
        self._order = order

    @property
    def name(self) -> str:
        return self._label

    def register(self, hooks: HooksCenter) -> None:
        hooks.register(Junction.PRE_LLM_REQUEST, _recorder(self._order, self._label))


def _hooked_module(name: str, order: list[str], *plugins: str) -> RuntimeModule:
    def on_start(runtime: object, flags: object) -> None:
        del flags
        assert isinstance(runtime, Runtime)
        runtime.hooks.register(Junction.PRE_LLM_REQUEST, _recorder(order, f'start-{name}'))

    return RuntimeModule(
        name,
        agent_plugins=tuple(_plugin_factory(label, order) for label in plugins),
        on_start=on_start,
    )


def _plugin_factory(label: str, order: list[str]) -> Callable[..., _Plugin]:
    def build(*_: object) -> _Plugin:
        return _Plugin(label, order)

    return build


async def test_hooks_run_local_then_module_layer_then_runtime_plugins_then_module_starts():
    order: list[str] = []
    captured: list[BaseAgent] = []

    def factory(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = BaseAgent(
            AgentOptions(name='Worker', llm=ScriptedLLM([ScriptedTurn(content='done')])),
            hooks=hooks,
            events=events,
        )
        agent.register_hook(Junction.PRE_LLM_REQUEST, _recorder(order, 'local-k'))
        agent.register_hook(Junction.PRE_LLM_REQUEST, _recorder(order, 'local-c'))
        captured.append(agent)
        return agent

    runtime = Runtime(
        plugins=[_Plugin('rt-n', order), _Plugin('rt-e', order), _Plugin('rt-w', order)],
        modules=[
            _hooked_module('mod-q', order, 'q'),
            _hooked_module('mod-b', order, 'b-s', 'b-b', 'b-l'),
            _hooked_module('mod-x', order, 'x'),
        ],
    )
    try:
        agent_id = await runtime.spawn(factory)
        captured[0].register_hook(Junction.PRE_LLM_REQUEST, _recorder(order, 'local-t'))
        await runtime.send(agent_id, 'go')
        await runtime.wait(agent_id)
        assert order == [
            'local-k',
            'local-c',
            'local-t',
            'q',
            'b-s',
            'b-b',
            'b-l',
            'x',
            'rt-n',
            'rt-e',
            'rt-w',
            'start-mod-q',
            'start-mod-b',
            'start-mod-x',
        ]
    finally:
        await runtime.shutdown()
