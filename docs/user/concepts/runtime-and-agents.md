# Runtime and agents

Syndica borrows the operating-system process model: the `Runtime` is the init system, an agent is a
process, and the message broker is IPC. Three invariants follow, and every boundary in the kernel
is a consequence of them:

- **Agents share no mutable state.** They talk only through messages.
- **Each agent can be paused, resumed and cancelled on its own.**
- **An agent does not manage its own lifecycle.** An outer loop waits for messages and drives the
  agent; the inner loop only runs "model call, then tools".

## Agent states

| State | Meaning |
| --- | --- |
| `idle` | Registered; its loop is waiting for a message. |
| `running` | Executing turns. |
| `paused` | Suspended from outside; resumes only when someone calls `resume`. |
| `waiting` | Waiting by itself: for a person's answer, a tool confirmation, or a specific reply. |
| `aborting` | Abort has been requested and the agent is winding down. |
| `zombie` | Its loop has exited and its mailbox is gone, but it is still in the registry and addressable. One message brings it back, with its context. |

How an agent ends a cycle of work is its **exit mode**, chosen when it is spawned: `zombie` (the
default: stop the loop, keep the registry entry), `idle` (keep the loop alive for more work) or
`exit` (remove it). A cycle stops with one of `completed`, `aborted`, `error`, `max_turns` or
`budget`.

Messages are sent with a trigger: `pending` (the default) is consumed before the agent's next turn,
so an interjection is seen as soon as the current tools finish; `deferred` waits until the agent has
finished its current work.

## One turn

Each turn the agent checks for abort and pause, takes pending messages into its context, assembles
the request (system prompt first, then the history), calls the model, and runs the tool calls in
the reply. A reply without tool calls completes the cycle. Model failures are classified: context
overflow can trigger compaction, cancellation is passed through and never counted as a model
failure, and other failures go to the retry and failover machinery. An empty reply (no text and no
tool calls) is treated as a failure, never stored in the context.

The system prompt must stay byte-identical from turn to turn, because providers cache requests by
prefix. Anything that changes during a session (the working directory, the budget left, memory,
the goal) is injected into the history instead. See
[Sessions, compaction and memory](sessions-compaction-memory.md).

## Roles

A role is what you spawn an agent from. There are two kinds, and the only difference is whether
the manifest points at a Python class:

- **Declarative roles** are a manifest and a prompt, no Python. A manifest has ten public fields:
  `name`, `description`, `system_prompt`, `tools`, `subagents`, `model`, `extends`, `skills`,
  `budget` and `artifact_kinds`.
- **Class-based roles** add `impl: package.module:Class`, a subclass of `BaseAgent`, for custom tool
  wiring, hooks or prompt assembly.

`description` is what an orchestrator reads to decide who gets a piece of work, so it should say
*when to use me*, not *what I am*. A role you declare with the name of a built-in role replaces
it, and the replacement is recorded. See [Writing an agent](../guides/writing-an-agent.md).

The kernel ships four roles:

| Role | Kind | Does |
| --- | --- | --- |
| `Orchestrator` | class-based | Splits the work and delegates to `Planner`, `Explorer` and `Worker`; does not change files itself. |
| `Planner` | declarative | Read-only: scope, decisions, dependencies, success criteria. |
| `Explorer` | declarative | Read-only: finds files, symbols and references. |
| `Worker` | declarative | Changes files, runs commands, verifies the result. |

Their descriptions and prompts are in Chinese in v1.0. `syndica agents list` and
`syndica agents show <name>` print them.

## Delegation

Five kernel tools let agents work as a team:

| Tool | Does |
| --- | --- |
| `Delegate` | Starts another role cold, without the caller's context, for work that needs a fresh view. |
| `Fork` | Starts a copy that inherits the caller's context, for continuing work whose conclusions matter. |
| `SendMessage` | Sends a message to an existing agent (waking it from `zombie` if needed). |
| `QueryAgent` | Reads another agent's state without waking it. |
| `KillAgent` | Stops an agent. |

A delegated or forked agent's budget is carved from its parent's; see
[Budgets](budgets-and-long-horizon.md).

## Topologies

A topology declares which roles a system has, how to address them and when to start them. It is
data in `.syndica.yaml`, selected with `syndica run --topology <name>`:

```yaml
version: 1
topologies:
  review:
    slots:
      - {name: root, agent: Orchestrator, is_root: true, cardinality: singleton, boot: eager}
      - {name: worker, agent: Worker, cardinality: on-demand, isolation: worktree}
```

| Slot field | Values |
| --- | --- |
| `cardinality` | `singleton` (one instance), `keyed` (one per key, named by `key_field`), `on-demand` (created as needed; the default) |
| `boot` | `eager` (at start), `lazy` (when first addressed; the default), `manual` |
| `exit_mode` | `zombie` (default), `idle`, `exit` |
| `isolation` | `shared` (default), `worktree`, `snapshot` |
| `extra_tools`, `budget`, `metadata` | Additions on top of the role |

**A topology declares structure, never behaviour.** A slot can add tools, a budget or metadata to
a role, but cannot replace its prompt, tools or model. Otherwise the same role would behave
differently in different topologies and you would need to read two definitions to know what it
does; if you need different behaviour, define a different role.

## Workspaces and isolation

Agents act on the world through two protocols: a **Workspace** decides where work happens and
whether it is isolated; a **Portal** performs the actions (run a command, read and write files,
drive a pseudo-terminal). To agents and tools, a local directory and a remote container look the
same. The kernel ships `LocalWorkspace` (a local directory and subprocesses); other execution
environments are backends that pass the published contract suite (see
[Writing a backend](../guides/writing-a-backend.md)).

- **Versions.** Every `Write` and `Edit` records a version in a version layer under `.syndica/vcs`,
  written in pure Python in git's object format (git is never run on it). Contents are redacted
  before they reach disk, so a redacted version cannot be restored. `workspace.versions: false`
  turns the layer and isolation off.
- **Isolation.** Agents share the workspace by default. A slot or a `Delegate` / `Fork` can ask for
  `worktree` or `snapshot` isolation: each cycle works on a copy of the workspace and is merged
  back file by file when the cycle ends. Conflicts are reported to the agent, never silently
  overwritten.
- **Optimistic concurrency.** In a shared workspace, overwriting a file you have not read, or that
  changed since you read it, is a conflict returned to the agent.

## Extension layers

Behaviour is added in three layers, applied in this order:

| Layer | Installed with | For |
| --- | --- | --- |
| Agent hooks | `agent.register_hook(junction, callback)` | One agent |
| Runtime plugins | `Runtime(plugins=[...])` | Logic shared by every agent |
| Runtime Modules | `modules:` in `.syndica.yaml`, or `Runtime(modules=[...])` | Self-contained capabilities with flags |

Hooks run at **junctions**: lifecycle points (`agent_spawn`, `agent_start`, `turn_start`, ...),
tool calls (`pre_tool_use`, `post_tool_use`), model calls (`pre_llm_request`, `llm_failure`, ...),
artifacts, acceptance and background tools. Interceptable junctions can skip or rewrite the
operation. Modules can declare their own junctions named `<module>:<event>`. Anything with a switch
is a [Runtime Module](../guides/writing-a-module.md).

## Layering

The package has three layers, and the dependency rules are enforced by a check in CI:

- **kernel**: runtime, agents, tools, hooks, context, topology, protocols. It imports no surface or
  backend, reads no environment variables, and depends only on the standard library, pydantic and
  one HTTP client.
- **surfaces**: the CLI, the server and the runner. Each depends only on the kernel.
- **backends**: model clients, protocol clients, the OTLP exporter, TRL. Each depends only on the
  kernel.

The full design is in the
[architecture spec](https://github.com/ai-loren/syndica/blob/main/docs/design/2026-09-03-architecture.md)
(maintainers' document, in Chinese).
