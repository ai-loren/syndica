"""A minimal third-party Syndica module.

It declares one flag (`example_greeting`), gives every agent one tool (`Greet`) that announces each
greeting on the module's own observable extension junction `example:greeted`, and installs one
plugin that records what it hears there in the `example_greetings` runtime service. It needs no
capabilities and does no I/O, so it declares `replay_safe=True` and runs captured with it replay.

Install it into the environment syndica runs in, then enable it in `.syndica.yaml`:

    uv pip install -e examples/syndica-example-module

    version: 1
    modules: [example]
    flags:
      example_greeting: hi

`syndica modules show example` prints this manifest; `syndica flags list` lists the flag.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from syndica.kernel import (
    BaseAgent,
    ExtensionJunction,
    FlagDeclaration,
    HooksCenter,
    JunctionKind,
    ModuleHost,
    Runtime,
    RuntimeModule,
    Tool,
    ToolResult,
)

__all__ = ['GREETED', 'Greet', 'Greetings', 'build']

GREETED = ExtensionJunction('example:greeted', JunctionKind.OBSERVABLE)


class Greetings:
    """The names the plugin heard on `example:greeted`, in order."""

    def __init__(self) -> None:
        self.names: list[str] = []


class Greet(Tool):
    """Greet someone by name.

    Args:
        name: Who to greet.
    """

    # Greet hands none of its arguments to a shell or interpreter; a tool that does lists them here.
    __tool_command_args__ = ()

    def __init__(self, agent: BaseAgent, greeting: str) -> None:
        self._agent = agent
        self._greeting = greeting

    async def execute(self, name: str) -> ToolResult:
        await self._agent.trigger_extension(GREETED, name=name)
        return ToolResult(output=f'{self._greeting}, {name}')


class _Recorder:
    name = 'example-recorder'

    def __init__(self, greetings: Greetings) -> None:
        self._greetings = greetings

    def register(self, hooks: HooksCenter) -> None:
        async def greeted(*, name: str, **_: Any) -> None:
            self._greetings.names.append(name)

        hooks.register(GREETED, greeted)


def _greetings(runtime: Runtime, flags: Mapping[str, object]) -> Greetings:
    del runtime, flags
    return Greetings()


def _tools(runtime: Runtime, agent: BaseAgent, flags: Mapping[str, object]) -> dict[str, Tool]:
    del runtime
    return {'Greet': Greet(agent, str(flags['example_greeting']))}


def _plugin(runtime: Runtime, agent: BaseAgent, flags: Mapping[str, object]) -> _Recorder:
    del agent, flags
    return _Recorder(cast(Greetings, getattr(runtime, 'example_greetings')))  # noqa: B009


def build(host: ModuleHost) -> RuntimeModule:
    """The entry point: `syndica.modules` calls it with a `ModuleHost` once the configuration is
    trusted. This module reads nothing from the host."""
    del host
    return RuntimeModule(
        'example',
        version='0.1.0',
        description='Greets by name and records every greeting it hears on example:greeted',
        capabilities=(),
        replay_safe=True,
        flags=(
            FlagDeclaration(
                key='example_greeting',
                default='hello',
                description='The word the Greet tool greets with',
            ),
        ),
        services={'example_greetings': _greetings},
        tool_factories=(_tools,),
        agent_plugins=(_plugin,),
        junctions=(GREETED,),
    )
