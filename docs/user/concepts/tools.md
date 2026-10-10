# Tools

A tool is a class with an async `execute` method. The model sees its name, description and
argument schema; the kernel parses the arguments, judges the call, runs it, and puts the result
into the agent's history.

## Where the schema comes from

| Priority | Source | Use it when |
| --- | --- | --- |
| 1 | `__tool_schema__`, a hand-written JSON schema | You need exact control over what the model sees. |
| 2 | `__tool_model__`, a pydantic model | Richer schemas (constraints, defaults, descriptions per field). The kernel uses the model for the schema only; validate inside `execute` if you need to. |
| 3 | The signature of `execute` and its docstring | Everything else: write a typed method with an `Args:` section. |

With reflection, the description and the argument descriptions come from the docstring of
`execute` itself, not from the class docstring. The tool's name is its class name without leading
underscores, or `__tool_name__`.

## Results and errors

`execute` returns a `ToolResult`:

- `output` is what the model reads.
- `meta` is structured data for code (hooks, the UI, workflows). It never enters the model's
  context.
- `artifacts` lists the artifacts this call touched; write tools report what they wrote here, so
  the kernel does not have to guess from the file system.

An exception that subclasses `ToolFatalError` is passed up to the agent loop: retrying will not
help. Any other exception becomes text in `output`, so the model can try another way. Write
messages for the model the same way as for people: what was expected, what was received, what to
do next.

## Declaring what a tool executes

Every tool class must declare `__tool_command_args__`: the names of the arguments it hands to a
shell, an interpreter or a remote command, or `()` if it runs none of them. The
[danger judge](danger.md) inspects only the declared arguments. Undeclared means "check
everything", which turns prose in a write tool's arguments into false alarms; `()` on a tool that
does run commands switches the check off for it. A declaration inherited from a parent class does
not count.

## Concurrency

The tool calls of one turn run in segments. A call joins a parallel segment only when everything
about it is known to be safe: arguments that fail to parse, unknown tools and calls that need a
person's approval always run alone. A tool runs in parallel only if its `is_concurrency_safe(args)`
returns exactly `True`; the default is `False`. Choosing a concurrency limit never changes a safety
decision.

## Built-in tools

| Group | Tools | Provided by |
| --- | --- | --- |
| Files | `Read`, `Write`, `Edit`, `Glob`, `Grep` | The workspace (`local_tools` when embedding) |
| Commands | `Bash`, `BashOutput`, `KillBash` | The workspace |
| Terminal | `Terminal` (a pseudo-terminal session for programs that need a TTY) | The workspace |
| Orchestration | `Delegate`, `Fork`, `SendMessage`, `QueryAgent`, `KillAgent` | Every agent in CLI and server runs. When embedding, `Runtime(orchestration=True)`; the `Orchestrator` installs them itself either way |
| Human interaction | `AskUser`, `AskUserQuestion` | The `human-interaction` module, which the CLI always installs |
| Artifacts | `Deliver` | The `acceptance` module, for agents that can write files |
| Budget | `GetBudget` | When the run has a budget limit |
| Background | `QueryBackgroundTool`, `CancelBackgroundTool` | With the `background` module |
| Context | `Compact` | With the `compaction` module |
| Memory | `Remember` | With the `memory` module |

A role lists the other tools it may use in its manifest's `tools`. `syndica run --no-write` keeps only
`Read`, `Grep`, `Glob`, `AskUser` and `AskUserQuestion` (orchestration tools stay), and never
verifies or commits.

The CLI adds guards to every run: file tools cannot write inside `.git/`, and cannot read or write
`.syndica/traces` or `.syndica/handoffs` (in any letter case or through a symlink). Traces hold tool
output that was marked untrusted, and reading it back with a file tool would launder it into
trusted text; handoff files are the host's to write. Commands can still reach both: the real
boundary is a sandboxed workspace backend.

## Background calls

With the `background` module and `background_after_seconds: N`, a `Bash` call still running after
`N` seconds moves to the background. The agent gets a placeholder with an id (`bg-1`) and keeps
working; the real result enters its history in a later turn, after the same post-tool hooks and
trust rules as any other result. An agent cannot finish while background calls are running: its
final answer waits for them, and an abort, a kill, or running out of budget cancels them and
records that their side effects are unknown. Only calls in the last segment of a batch move,
calls approved by a person never move, and a call that needs approval is refused while background
calls are running. Runs with the module installed do not write a replay document.

To write your own tool, see [Writing a tool](../guides/writing-a-tool.md).
