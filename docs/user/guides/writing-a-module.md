# Writing a Runtime Module

**Anything that needs a switch is a Runtime Module.** One flag declaration feeds the code, the
settings UI and this documentation, so the three cannot disagree. A third party adds a capability
by writing a module, without touching the kernel.

A module can give the runtime four kinds of things:

- **tools** for every new agent (`tool_factories`), with no wiring on the host side;
- **per-agent plugins** that register hooks (`agent_plugins`);
- **services**, stateful objects the host can reach as `runtime.<name>` (`services`);
- **start and shutdown hooks** (`on_start`, `on_shutdown`).

## A complete module

This module gives every agent a `Shout` tool whose length limit is a flag, and runs it in-process
with a scripted model:

```python
import asyncio
from collections.abc import Mapping
from pathlib import Path

from syndica.kernel import (
    AgentCatalog,
    BaseAgent,
    FlagDeclaration,
    ManifestSource,
    ModuleHost,
    Runtime,
    RuleRedactor,
    RuntimeModule,
    ScriptedLLM,
    ScriptedTurn,
    Tool,
    ToolCall,
    ToolResult,
)


class Shout(Tool):
    __tool_command_args__ = ()

    def __init__(self, limit: int) -> None:
        self._limit = limit

    async def execute(self, text: str) -> ToolResult:
        """Repeat text in capitals, cut to the configured length.

        Args:
            text: What to shout.
        """
        return ToolResult(output=text.upper()[: self._limit])


def _tools(runtime: Runtime, agent: BaseAgent, flags: Mapping[str, object]) -> dict[str, Tool]:
    limit = flags['shout_limit']
    assert isinstance(limit, int)
    return {'Shout': Shout(limit)}


def build(host: ModuleHost) -> RuntimeModule:
    """The entry point: called with a ModuleHost once the configuration is trusted."""
    return RuntimeModule(
        'shout',
        version='0.1.0',
        description='Gives every agent a Shout tool',
        capabilities=(),
        replay_safe=True,
        flags=(
            FlagDeclaration(
                key='shout_limit',
                default=20,
                description='Longest shout, in characters',
                minimum=1,
            ),
        ),
        tool_factories=(_tools,),
    )


catalog = AgentCatalog()
catalog.load_mapping(
    {'name': 'Crier', 'description': 'Use me to announce things loudly.'},
    source=ManifestSource(kind='mapping', location='<docs>'),
)
model = ScriptedLLM(
    [
        ScriptedTurn(
            tool_calls=[ToolCall(id='c1', name='Shout', arguments='{"text": "hello world"}')]
        ),
        ScriptedTurn(content='Announced.'),
    ]
)
factories = catalog.freeze({}, {'default': model})
host = ModuleHost(workspace_root=Path.cwd(), environment={}, redactor=RuleRedactor())


async def main() -> None:
    runtime = Runtime(agents=factories, modules=[build(host)], flags={'shout_limit': 5})
    agent_id = await runtime.spawn(factories['Crier'], query='Announce hello world')
    context = await runtime.wait(agent_id)
    await runtime.shutdown()
    [result] = [block.content for block in context.current_traj if block.role == 'tool']
    assert result == 'HELLO'


asyncio.run(main())
```

## Packaging it for the CLI

Publish the factory under the `syndica.modules` entry-point group. The entry-point name must equal
the module's name:

```toml
[project]
name = "syndica-shout"
version = "0.1.0"
dependencies = ["syndica"]

[project.entry-points."syndica.modules"]
shout = "syndica_shout:build"
```

Install the distribution into the environment Syndica runs in, then enable the module:

```yaml
version: 1
modules: [retry]
flags:
  llm_retry_attempts: 2
```

Third-party modules are listed the same way (`modules: [shout]`, `flags: {shout_limit: 5}`). Loading
is always explicit: installing a package does not enable it. `syndica modules list` shows every
module it can find without importing any; `syndica modules show shout` imports it and prints its
manifest; `syndica flags list` shows its flags once the configuration enables it.

A complete example distribution lives in
[examples/syndica-example-module](https://github.com/ai-loren/syndica/tree/main/examples/syndica-example-module):
one flag, one tool that triggers the module's own junction, and one plugin that records what it
hears there.

## What the kernel checks when it loads a module

- **Name**: lowercase letters and digits in hyphen-separated segments (`ledger`, `git-notes`). It is
  also the entry-point name, the `modules:` entry and the owner segment of the module's junctions.
- **Flag keys** start with the name, hyphens turned into underscores (`git-notes` gives
  `git_notes_`), and must not look like secrets. Two enabled modules declaring the same key are
  refused.
- **Flags**: `FlagDeclaration(key, default, description, choices=(), minimum=None, maximum=None)`
  with a `bool`, `int` or `str` default. Need structured configuration? Split it into several flags.
- **Tools** come only from `tool_factories`, callables `(runtime, agent, flags) -> {name: Tool}`.
  Installing tools on the agent from anywhere else is refused. Each tool class declares
  `__tool_command_args__` itself (see [Writing a tool](writing-a-tool.md)).
- **Plugins**: `agent_plugins` are callables `(runtime, agent, flags) -> Plugin`; a plugin has a
  `name` and a `register(hooks)` method, and its hooks go on the module layer, in module order.
  Registering with `agent.register_hook` from a factory instead puts the hook on the agent's own
  layer, ahead of every module. `agent_spawn`, `agent_despawn` and `runtime_shutdown` fire only on
  the runtime's own hook center, so register those on `runtime.hooks` from `on_start`.
- **Start and shutdown**: `on_start(runtime, flags)` is synchronous and returns `None`;
  `on_shutdown(runtime)` returns an awaitable. A service factory or `on_start` that raises fails the
  whole runtime construction and rolls back what the kernel holds; `on_shutdown` failures are raised
  together after every module has shut down. An enabled module that fails to start never goes
  missing quietly.
- **Junctions** a module owns are listed in its manifest,
  `junctions=[ExtensionJunction('<name>:<event>', JunctionKind.OBSERVABLE)]` (or `INTERCEPTABLE`),
  and triggered with the same object through `agent.trigger_extension(handle, **kwargs)`. It returns
  the chain's merged `HookResult`; for an interceptable junction the module decides what a `skip` or
  a rewrite means.
- **Manifest fields**: `version`, `description`, `capabilities` (a subset of `environment`,
  `filesystem`, `network` and `subprocess`; `None` means undeclared and is shown as all of them),
  `requires` (modules that must be installed earlier), `replay_safe` (the module's code does no I/O,
  reads no clock or randomness and starts no threads, so a replay may run it again) and
  `security_notes`.

## Trust and safety

A third-party module runs in-process with your full environment. Python cannot sandbox code in the
same process, so capability declarations are for people to review, not enforcement.

- The configuration confirmation shows every enabled module and covers its code when that code
  lives in the workspace; a distribution installed inside the workspace is also checked against its
  `RECORD`.
- The module's tool results, and any result its hooks write for other tools, are marked untrusted.
  `--trust-module-output <name>` on `run` or `chat` lifts that for one run; a project configuration
  cannot.
- To hand text to another agent, use `runtime.send` or `runtime.spawn(query=...)`; text a module
  writes there is marked untrusted. A message published straight to `runtime.broker` carries
  whatever trust it was given.
- A factory your module passes to `runtime.spawn` runs as your module, so hooks it or the agent's
  `install_own_tools` registers count as yours. Roles the host registered run as the host even when
  your tool spawns them.
- A run with your module replays only if it declares `replay_safe` or runs no code of its own.
