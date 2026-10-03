"""第三方交来的工具在自己的类上声明 `__tool_command_args__`（M4 D7）：继承来的不算，实例上的不算，
参数名写错在装配时就失败。宿主自己交来的工具不在此列。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
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
