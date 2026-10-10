# Writing an agent

Prefer a **declarative role**: a manifest and a prompt, no Python. If adding a role required code,
splitting work across roles would be expensive and "multi-agent is first-class" would not hold.
Write a **class-based role** only when you need custom tool wiring, hooks or prompt assembly.

## A declarative role

Declare it under `agents:` in `.syndica.yaml`:

```yaml
version: 1
agents:
  - name: Researcher
    description: |
      Use me when a question needs several sources and an answer with citations.
      Do not use me to change code or write the final deliverable.
    system_prompt: {file: prompts/researcher.md}
    tools: [Read, Grep, Glob]
    budget: {turns: 40}
```

Then run it directly, or let the `Orchestrator` delegate to it:

```bash
syndica run --agent Researcher "What does this repository say about retries?"
syndica agents show Researcher
```

### Manifest fields

| Field | Meaning |
| --- | --- |
| `name` | Required. How the role is addressed (`--agent`, `Delegate`, topology slots). |
| `description` | Required. **When to use this role**, not what it is: an orchestrator decides who gets work from this text alone. |
| `system_prompt` | Prompt text, or `{file: <path>}` relative to the configuration file. |
| `tools` | Names of the tools the role may use (see [Tools](../concepts/tools.md)). |
| `subagents` | Roles this one is expected to delegate to. The `Orchestrator` lists their descriptions in its prompt. |
| `model` | `default` (the rotation), or `<platform>:<model>`, `acp:<name>`, `a2a:<name>`. See [Model providers](models.md). |
| `extends` | A registered class-based role whose class and tools this declarative role builds on. |
| `skills` | Accepted and shown by `agents show`; v1.0 ships no skill loader, so it has no effect yet. |
| `budget` | `{turns: <n>}` caps this agent's turns (default 50); `{turns: null}` removes the cap. The run's tree budget still applies. |
| `artifact_kinds` | Kinds of artifact the role produces. |
| `impl` | Makes the role class-based: `package.module:Class`, a subclass of `BaseAgent`. |

These ten fields plus `impl` are the whole manifest; unknown keys are errors.

### Prompts

- Write the description as a routing rule: "Use me when ...; do not use me for ...".
- Prompts may use `{os}`, `{platform}` and `{shell}`, which never change within a session. `{cwd}`
  is refused: the working directory is injected into the history instead, so the system prompt
  stays byte-identical and cacheable. Write literal braces as `{{` and `}}`.

### Replacing a built-in role

Declaring a role named `Planner`, `Explorer`, `Worker` or `Orchestrator` replaces the built-in one,
and the replacement is recorded. The built-in prompts are in Chinese in v1.0; replace a role if you
want its prompt in another language.

## A class-based role

Point `impl` at a `BaseAgent` subclass importable from the environment Syndica runs in:

```yaml
version: 1
agents:
  - name: Audited
    description: Use me when every file you read must leave an audit line.
    system_prompt: Read the files you are asked about and summarise them.
    impl: my_project.agents:AuditedAgent
    tools: [Read, Grep]
```

If that package lives inside the workspace, the trust confirmation covers its code (see
[Configuration](configuration.md#confirming-a-project-configuration)).

A subclass can:

- register per-agent hooks in `__init__` with `self.register_hook(junction, callback)`;
- install tools of its own by overriding `install_own_tools(runtime)` and calling
  `self.install_runtime_tools({...})` (the `Orchestrator` installs `Delegate` and friends this way);
- assemble its system prompt when the catalog is frozen, with a classmethod
  `compose_system_prompt(catalog)` that returns the prompt.

This complete program defines a tool and a class-based role that records every tool call before it
runs, and drives it with a scripted model, so it runs offline:

```python
import asyncio
from typing import Any

from syndica.kernel import (
    AgentCatalog,
    AgentOptions,
    BaseAgent,
    EventBus,
    HooksCenter,
    Junction,
    ManifestSource,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    Tool,
    ToolCall,
    ToolResult,
)

AUDIT: list[str] = []


class Echo(Tool):
    __tool_command_args__ = ()

    async def execute(self, text: str) -> ToolResult:
        """Repeat the text back.

        Args:
            text: What to repeat.
        """
        return ToolResult(output=text)


class AuditedAgent(BaseAgent):
    """Use me when every tool call must leave an audit line."""

    def __init__(self, options: AgentOptions, *, hooks: HooksCenter, events: EventBus) -> None:
        super().__init__(options, hooks=hooks, events=events)

        async def audit(*, tool_call: ToolCall, **_: Any) -> None:
            AUDIT.append(tool_call.name)

        self.register_hook(Junction.PRE_TOOL_USE, audit)


catalog = AgentCatalog()
catalog.load_mapping(
    {
        'name': 'Audited',
        'description': 'Use me when every tool call must leave an audit line.',
        'system_prompt': 'Repeat what you are told with Echo.',
        'tools': ['Echo'],
        'impl': AuditedAgent,
    },
    source=ManifestSource(kind='mapping', location='<docs>'),
)
model = ScriptedLLM(
    [
        ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Echo', arguments='{"text": "hi"}')]),
        ScriptedTurn(content='Done.'),
    ]
)
factories = catalog.freeze({'Echo': Echo()}, {'default': model})


async def main() -> None:
    runtime = Runtime(agents=factories)
    agent_id = await runtime.spawn(factories['Audited'], query='Say hi')
    context = await runtime.wait(agent_id)
    await runtime.shutdown()
    print(context.current_traj[-1].content)


asyncio.run(main())
assert AUDIT == ['Echo']
```

## Embedding: catalogs, factories and the runtime

The example shows the embedding path the CLI uses under the hood:

1. An `AgentCatalog` collects manifests: `load_mapping`, `load_json_file`, `load_json_directory`,
   `register_class`, and `register_builtins(catalog)` for the four built-in roles.
2. `catalog.freeze(tools, models)` resolves tool names against a `{name: Tool}` table and model
   names against a `{name: LLM}` table, and returns one factory per role. Danger options can be
   passed here too (`danger_judge=`, `danger_policy=`).
3. `Runtime(agents=factories)` hosts the agents; `spawn` starts one with a query, `wait` returns its
   context when the cycle ends, and `shutdown` stops everything.

`ScriptedLLM` replays a fixed list of `ScriptedTurn`s, which is how Syndica's own tests drive real
runtimes without a model. For real models, use the CLI's providers (see
[Model providers](models.md)) or implement the `LLM` protocol in a backend.
