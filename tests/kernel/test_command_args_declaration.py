"""第三方交来的工具在自己的类上声明 `__tool_command_args__`（M4 D7）：继承来的不算，实例上的不算，
参数名写错在装配时就失败。宿主自己交来的工具不在此列。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.catalog import AgentCatalog
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.modules import RuntimeModule
from syndica.kernel.runtime import Runtime
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tools.human import AskUser, AskUserQuestion, HumanInteractionModule


class _Declares(Tool):
    __tool_command_args__ = ()

    async def execute(self, command: str = '') -> ToolResult:
        return ToolResult(output=command)


class _Inherits(_Declares):
    pass


class _Undeclared(Tool):
    async def execute(self, command: str = '') -> ToolResult:
        return ToolResult(output=command)


class _DeclaresNone(Tool):
    __tool_command_args__ = None

    async def execute(self, command: str = '') -> ToolResult:
        return ToolResult(output=command)


class _DeclaresProperty(Tool):
    @property
    def __tool_command_args__(self) -> tuple[str, ...]:  # pyright: ignore[reportIncompatibleVariableOverride]
        return ()

    async def execute(self, command: str = '') -> ToolResult:
        return ToolResult(output=command)


class _DeclaresList(Tool):
    __tool_command_args__ = ['command']  # pyright: ignore[reportAssignmentType]

    async def execute(self, command: str = '') -> ToolResult:
        return ToolResult(output=command)


class _DeclaresString(Tool):
    __tool_command_args__ = 'command'  # pyright: ignore[reportAssignmentType]

    async def execute(self, command: str = '') -> ToolResult:
        return ToolResult(output=command)


class _DeclaresNonStrings(Tool):
    __tool_command_args__ = (1,)  # pyright: ignore[reportAssignmentType]

    async def execute(self, command: str = '') -> ToolResult:
        return ToolResult(output=command)


class _ShadowedOnTheInstance(Tool):
    __tool_command_args__ = ()

    def __init__(self) -> None:
        self.__tool_command_args__ = ('command',)  # pyright: ignore[reportAttributeAccessIssue]

    async def execute(self, command: str = '') -> ToolResult:
        return ToolResult(output=command)


class _Misspelled(Tool):
    __tool_command_args__ = ('cmd',)

    async def execute(self, command: str = '') -> ToolResult:
        return ToolResult(output=command)


class _RunsCommands(Tool):
    __tool_command_args__ = ('command',)

    async def execute(self, command: str = '') -> ToolResult:
        return ToolResult(output=command)


class _InheritsUnderTheSameName(_Declares):
    __tool_name__ = 'Declares'


def _factory(
    captured: list[BaseAgent],
    *,
    cls: type[BaseAgent] = BaseAgent,
    tools: Mapping[str, Tool] | None = None,
) -> Callable[[HooksCenter, EventBus], BaseAgent]:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = cls(
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


def _module(tool: Tool) -> RuntimeModule:
    return RuntimeModule('extra', tool_factories=(lambda *_: {tool.name: tool},))


@pytest.mark.parametrize(
    ('tool', 'shown'),
    [
        (_Inherits(), '类上没有自己的声明'),
        (_Undeclared(), '类上没有自己的声明'),
        (_DeclaresNone(), '类上声明的是 None'),
        (_DeclaresProperty(), '类上声明的是 <property'),
        (_DeclaresList(), "类上声明的是 ['command']"),
        (_DeclaresString(), "类上声明的是 'command'"),
        (_DeclaresNonStrings(), '类上声明的是 (1,)'),
    ],
    ids=['inherited', 'undeclared', 'none', 'property', 'list', 'string', 'non-strings'],
)
async def test_a_module_tool_without_its_own_tuple_declaration_is_refused(tool: Tool, shown: str):
    captured: list[BaseAgent] = []
    runtime = Runtime(modules=[_module(tool)])
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_factory(captured))
        error = excinfo.value
        assert f"Module 'extra' 的工具 {tool.name!r}" in error.expected
        assert type(tool).__qualname__ in error.expected
        assert shown in error.received
        assert '__tool_command_args__ = ()' in error.next_action
        assert '继承来的声明不算' in error.next_action
        assert captured[0].options.tools == {}
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


async def test_a_module_tool_shadowing_its_declaration_on_the_instance_is_refused():
    runtime = Runtime(modules=[_module(_ShadowedOnTheInstance())])
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_factory([]))
        assert '只在类上声明' in excinfo.value.expected
        assert '实例上另有一个同名属性' in excinfo.value.received
        assert '判定器先读实例属性' in excinfo.value.next_action
    finally:
        await runtime.shutdown()


async def test_a_module_tool_naming_a_parameter_execute_does_not_take_fails_at_assembly():
    runtime = Runtime(modules=[_module(_Misspelled())])
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_factory([]))
        assert "['cmd']" in excinfo.value.received
        assert 'command' in excinfo.value.received
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize('tool', [_Declares(), _RunsCommands()], ids=['nothing', 'command'])
async def test_a_module_tool_declaring_on_its_own_class_is_installed(tool: Tool):
    captured: list[BaseAgent] = []
    runtime = Runtime(modules=[_module(tool)])
    try:
        await runtime.spawn(_factory(captured))
        assert captured[0].options.tools[tool.name] is tool
    finally:
        await runtime.shutdown()


class _InstallsInherited(BaseAgent):
    def install_own_tools(self, runtime: object) -> None:
        del runtime
        self.install_runtime_tools({'Inherits': _Inherits()})


class _InstallsDeclared(BaseAgent):
    def install_own_tools(self, runtime: object) -> None:
        del runtime
        self.install_runtime_tools({'Declares': _Declares()})


async def test_a_tool_a_class_role_installs_itself_must_declare_on_its_own_class():
    captured: list[BaseAgent] = []
    runtime = Runtime()
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_factory(captured, cls=_InstallsInherited))
        assert '类式角色 _InstallsInherited 的 install_own_tools' in excinfo.value.expected
        assert "'Inherits'" in excinfo.value.expected
        assert captured[0].options.tools == {}
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


async def test_a_class_role_tool_declaring_on_its_own_class_is_installed():
    captured: list[BaseAgent] = []
    runtime = Runtime()
    try:
        await runtime.spawn(_factory(captured, cls=_InstallsDeclared))
        assert list(captured[0].options.tools) == ['Declares']
    finally:
        await runtime.shutdown()


async def test_tools_the_host_hands_over_are_the_hosts_responsibility():
    captured: list[BaseAgent] = []
    host_tool = _Inherits()
    runtime = Runtime()
    try:
        await runtime.spawn(_factory(captured, tools={'Inherits': host_tool}))
        assert captured[0].options.tools['Inherits'] is host_tool
    finally:
        await runtime.shutdown()


def test_each_human_tool_declares_on_its_own_class():
    for cls in (AskUser, AskUserQuestion):
        assert cls.__dict__['__tool_command_args__'] == ()
    base: Any = AskUser.__mro__[1]
    assert '__tool_command_args__' not in base.__dict__


async def test_the_human_interaction_module_still_installs():
    captured: list[BaseAgent] = []
    runtime = Runtime(modules=[HumanInteractionModule()])
    try:
        await runtime.spawn(_factory(captured))
        assert list(captured[0].options.tools) == ['AskUser', 'AskUserQuestion']
    finally:
        await runtime.shutdown()


class _SneakyPlugin:
    def __init__(self, agent: BaseAgent | None = None) -> None:
        self._agent = agent

    @property
    def name(self) -> str:
        return 'sneaky'

    def register(self, hooks: HooksCenter) -> None:
        del hooks
        if self._agent is not None:
            self._agent.install_runtime_tools({'Inherits': _Inherits()})


def _installs_from_a_tool_factory(
    runtime: object, agent: BaseAgent, flags: object
) -> Mapping[str, Tool]:
    del runtime, flags
    agent.install_runtime_tools({'Inherits': _Inherits()})
    return {}


def _writes_the_tool_table_from_a_tool_factory(
    runtime: object, agent: BaseAgent, flags: object
) -> Mapping[str, Tool]:
    del runtime, flags
    agent.options.tools['Inherits'] = _Inherits()
    return {}


def _installs_from_a_plugin_factory(
    runtime: object, agent: BaseAgent, flags: object
) -> _SneakyPlugin:
    del runtime, flags
    agent.install_runtime_tools({'Inherits': _Inherits()})
    return _SneakyPlugin()


def _installs_from_a_plugin_register(
    runtime: object, agent: BaseAgent, flags: object
) -> _SneakyPlugin:
    del runtime, flags
    return _SneakyPlugin(agent)


@pytest.mark.parametrize(
    ('module', 'where'),
    [
        (
            RuntimeModule('sneaky', tool_factories=(_installs_from_a_tool_factory,)),
            'tool_factories[0]',
        ),
        (
            RuntimeModule('sneaky', tool_factories=(_writes_the_tool_table_from_a_tool_factory,)),
            'tool_factories[0]',
        ),
        (
            RuntimeModule('sneaky', agent_plugins=(_installs_from_a_plugin_factory,)),
            'agent_plugins[0]',
        ),
        (
            RuntimeModule('sneaky', agent_plugins=(_installs_from_a_plugin_register,)),
            "插件 'sneaky' 的 register",
        ),
    ],
    ids=['tool-factory', 'tool-table-write', 'plugin-factory', 'plugin-register'],
)
async def test_a_module_installing_a_tool_on_the_agent_directly_is_refused(
    module: RuntimeModule, where: str
):
    captured: list[BaseAgent] = []
    runtime = Runtime(modules=[module])
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_factory(captured))
        error = excinfo.value
        assert "Module 'sneaky' 的工具只经 tool_factories 交回的映射装上" in error.expected
        assert where in error.received
        assert "['Inherits']" in error.received
        assert 'tool_factories 交回的映射里' in error.next_action
        assert captured[0].options.tools == {}
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


async def test_a_module_installing_even_a_declaring_tool_directly_is_refused():
    def installs(runtime: object, agent: BaseAgent, flags: object) -> Mapping[str, Tool]:
        del runtime, flags
        agent.install_runtime_tools({'RunsCommands': _RunsCommands()})
        return {}

    runtime = Runtime(modules=[RuntimeModule('sneaky', tool_factories=(installs,))])
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_factory([]))
        assert "['RunsCommands']" in excinfo.value.received
    finally:
        await runtime.shutdown()


def _class_role(cls: type[BaseAgent], tools: Mapping[str, Tool] | None = None) -> Any:
    catalog = AgentCatalog()
    catalog.load_mapping(
        {
            'name': 'Planner',
            'description': '规划范围时用我。',
            'impl': cls,
            'tools': list(tools or {}),
        }
    )
    return catalog.freeze(dict(tools or {}), {'default': ScriptedLLM([])})['Planner']


class _InstallsInheritedInItsConstructor(BaseAgent):
    def __init__(self, options: AgentOptions, *, hooks: HooksCenter, events: EventBus) -> None:
        super().__init__(options, hooks=hooks, events=events)
        self.install_runtime_tools({'Inherits': _Inherits()})


class _ReplacesAHostToolInItsConstructor(BaseAgent):
    def __init__(self, options: AgentOptions, *, hooks: HooksCenter, events: EventBus) -> None:
        super().__init__(options, hooks=hooks, events=events)
        self.install_runtime_tools({'Declares': _InheritsUnderTheSameName()})


class _WritesTheSharedTableInItsConstructor(BaseAgent):
    def __init__(self, options: AgentOptions, *, hooks: HooksCenter, events: EventBus) -> None:
        options.tools.setdefault('Inherits', _Inherits())
        super().__init__(options, hooks=hooks, events=events)


class _DeclaresInItsConstructor(BaseAgent):
    def __init__(self, options: AgentOptions, *, hooks: HooksCenter, events: EventBus) -> None:
        super().__init__(options, hooks=hooks, events=events)
        self.install_runtime_tools({'RunsCommands': _RunsCommands()})


@pytest.mark.parametrize(
    ('cls', 'tool_name'),
    [
        (_InstallsInheritedInItsConstructor, 'Inherits'),
        (_ReplacesAHostToolInItsConstructor, 'Declares'),
    ],
    ids=['added', 'replaced'],
)
def test_a_tool_a_class_role_installs_in_its_constructor_must_declare_on_its_own_class(
    cls: type[BaseAgent], tool_name: str
):
    factory = _class_role(cls, {'Declares': _Declares()})
    with pytest.raises(ConfigError) as excinfo:
        factory(HooksCenter(), EventBus())
    assert f'类式角色 {cls.__qualname__} 的构造函数' in excinfo.value.expected
    assert repr(tool_name) in excinfo.value.expected
    assert '继承来的声明不算' in excinfo.value.next_action


def test_a_class_role_writing_the_shared_table_is_refused_on_every_spawn():
    factory = _class_role(_WritesTheSharedTableInItsConstructor)
    for _ in range(2):
        with pytest.raises(ConfigError) as excinfo:
            factory(HooksCenter(), EventBus())
        assert "'Inherits'" in excinfo.value.expected


def test_a_class_role_tool_declared_on_its_own_class_passes_the_constructor_check():
    agent = _class_role(_DeclaresInItsConstructor, {'Declares': _Declares()})(
        HooksCenter(), EventBus()
    )
    assert list(agent.options.tools) == ['Declares', 'RunsCommands']


class _ReplacesAModuleTool(BaseAgent):
    def install_own_tools(self, runtime: object) -> None:
        del runtime
        self.install_runtime_tools({'Declares': _InheritsUnderTheSameName()})


async def test_a_class_role_replacing_a_tool_under_the_same_name_must_declare_on_its_own_class():
    captured: list[BaseAgent] = []
    runtime = Runtime(modules=[_module(_Declares())])
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_factory(captured, cls=_ReplacesAModuleTool))
        assert '_ReplacesAModuleTool 的 install_own_tools' in excinfo.value.expected
        assert '_InheritsUnderTheSameName' in excinfo.value.expected
        assert captured[0].options.tools == {}
    finally:
        await runtime.shutdown()


class _InstallsThreeOwnTools(BaseAgent):
    def install_own_tools(self, runtime: object) -> None:
        del runtime
        self.install_runtime_tools(
            {'RunsCommands': _RunsCommands(), 'Declares': _Declares(), 'Inherits': _Inherits()}
        )


async def test_every_tool_a_class_role_installs_is_checked_not_only_the_first():
    runtime = Runtime()
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_factory([], cls=_InstallsThreeOwnTools))
        assert "'Inherits'" in excinfo.value.expected
    finally:
        await runtime.shutdown()


async def test_every_tool_a_module_returns_is_checked_not_only_the_first():
    def three(*_: object) -> Mapping[str, Tool]:
        return {'RunsCommands': _RunsCommands(), 'Declares': _Declares(), 'Inherits': _Inherits()}

    runtime = Runtime(modules=[RuntimeModule('extra', tool_factories=(three,))])
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_factory([]))
        assert "Module 'extra' 的工具 'Inherits'" in excinfo.value.expected
    finally:
        await runtime.shutdown()


async def test_the_tools_of_every_module_are_checked_not_only_the_first_module():
    runtime = Runtime(
        modules=[
            RuntimeModule('first', tool_factories=(lambda *_: {'Declares': _Declares()},)),
            RuntimeModule('second', tool_factories=(lambda *_: {'Inherits': _Inherits()},)),
        ]
    )
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_factory([]))
        assert "Module 'second' 的工具 'Inherits'" in excinfo.value.expected
    finally:
        await runtime.shutdown()
