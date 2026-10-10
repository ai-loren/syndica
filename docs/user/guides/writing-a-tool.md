# Writing a tool

A tool is a subclass of `Tool` with an async `execute` method that returns a `ToolResult`. Read
[Tools](../concepts/tools.md) first for how the kernel runs them.

## The smallest tool

Type the arguments of `execute` and document them in its docstring; the schema is generated from
both:

```python
from syndica.kernel import Tool, ToolResult


class WordCount(Tool):
    __tool_command_args__ = ()

    async def execute(self, text: str) -> ToolResult:
        """Count the words in a piece of text.

        Args:
            text: The text to count.
        """
        return ToolResult(output=str(len(text.split())))


schema = WordCount().schema
assert schema['name'] == 'WordCount'
assert schema['description'] == 'Count the words in a piece of text.'
assert schema['parameters']['required'] == ['text']
assert schema['parameters']['properties']['text'] == {
    'type': 'string',
    'description': 'The text to count.',
}
```

Rules that keep the generated schema honest:

- The description and the per-argument lines come from the docstring of `execute`, not of the
  class. Only single-line `name: description` entries whose name is a real argument count.
- Each argument is annotated with one of `str`, `int`, `float`, `bool`, `list` or `dict` (plain,
  not parameterised like `list[str]`; `Annotated[...]` is unwrapped). Use `__tool_model__` for
  anything richer. Positional-only arguments are refused, because the model always fills a keyword
  mapping.
- The name is the class name without leading underscores; set `__tool_name__` to choose another.

## Declare what the tool executes

`__tool_command_args__` is required on every tool class. List the arguments that are handed to a
shell, an interpreter or a remote command channel; write `()` if there are none. The danger judge
inspects exactly those arguments:

```python
from syndica.kernel import Tool, ToolResult


class RunScript(Tool):
    __tool_command_args__ = ('command',)

    async def execute(self, command: str, note: str = '') -> ToolResult:
        """Run a shell command.

        Args:
            command: The command line to run.
            note: Why you are running it.
        """
        raise NotImplementedError('run the command through your Portal here')
```

Leaving it undeclared makes the judge check every argument, so a markdown table in a write tool's
text would trip the shell rules. Declaring `()` on a tool that does run commands switches the check
off for it. A declaration inherited from a parent class does not count: each class states its own.

## Richer schemas, errors and concurrency

`__tool_model__` takes the schema from a pydantic model, with field descriptions, defaults and
constraints. The kernel uses the model for the schema only, so validate inside `execute` when the
constraints matter.

Raise `ToolFatalError(expected=..., received=..., next_action=...)` when retrying cannot help (a
resource that will not exist, missing permissions); it ends the agent's cycle with an error. Any
other exception becomes text the model reads, so make it say what was expected, what was received
and what to do next: the model decides from that text whether and how to retry.

`is_concurrency_safe(args)` returning exactly `True` lets calls of this tool run in parallel with
other safe calls in the same turn. Return `True` only when the call writes no shared state. It must
be a pure, fast function that never raises.

This program shows all three with a scripted model, offline:

```python
import asyncio
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from syndica.kernel import (
    AgentCatalog,
    ManifestSource,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    Tool,
    ToolCall,
    ToolResult,
)


class SliceArgs(BaseModel):
    text: str = Field(description='The text to cut.')
    length: int = Field(default=10, ge=1, description='How many characters to keep.')


class Slice(Tool):
    """Keep the first characters of a text."""

    __tool_model__ = SliceArgs
    __tool_command_args__ = ()

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            args = SliceArgs.model_validate(kwargs)
        except ValidationError as error:
            raise ValueError(
                f'Slice expects {{"text": string, "length": integer >= 1}}; got {kwargs}. '
                'Fix the arguments and call again.'
            ) from error
        return ToolResult(
            output=args.text[: args.length], meta={'cut': len(args.text) > args.length}
        )


catalog = AgentCatalog()
catalog.load_mapping(
    {'name': 'Cutter', 'description': 'Use me to shorten text.', 'tools': ['Slice']},
    source=ManifestSource(kind='mapping', location='<docs>'),
)
model = ScriptedLLM(
    [
        ScriptedTurn(
            tool_calls=[
                ToolCall(id='c1', name='Slice', arguments='{"text": "hello world", "length": 0}')
            ]
        ),
        ScriptedTurn(
            tool_calls=[
                ToolCall(id='c2', name='Slice', arguments='{"text": "hello world", "length": 5}')
            ]
        ),
        ScriptedTurn(content='hello'),
    ]
)
factories = catalog.freeze({'Slice': Slice()}, {'default': model})


async def main() -> None:
    runtime = Runtime(agents=factories)
    agent_id = await runtime.spawn(factories['Cutter'], query='Shorten "hello world" to 5 letters')
    context = await runtime.wait(agent_id)
    await runtime.shutdown()
    results = [block.content for block in context.current_traj if block.role == 'tool']
    assert 'Slice expects' in str(results[0])
    assert results[1] == 'hello'


asyncio.run(main())
```

The first call's `ValueError` reaches the model as a tool result (wrapped in the kernel's
three-part message, in Chinese in v1.0), and the scripted model retries with valid arguments.

## Getting a tool to the CLI's agents

A `.syndica.yaml` lists tool **names**; it cannot load Python by itself. To make your tool
available to roles run by the CLI or the server, ship it in a
[Runtime Module](writing-a-module.md): its `tool_factories` give each new agent the tools, and the
module's flags configure them. When you embed the runtime yourself, pass the tool in the table
given to `AgentCatalog.freeze`, as above, or install it from a class-based role (see
[Writing an agent](writing-an-agent.md#a-class-based-role)).

Results from a third-party module's tools are marked untrusted unless the user passes
`--trust-module-output <module>` (see [Trust and provenance](../concepts/trust-and-provenance.md)).
