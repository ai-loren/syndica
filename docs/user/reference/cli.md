# CLI reference

Usage: `syndica [--config <path>] <command> [options]`

<!-- The test suite checks this page against the CLI's argument parser: every command has a
section, every option of a command appears in its section, and no section names an option the
command does not have. -->

- `--config <path>` selects the configuration file (default `.syndica.yaml` in the current
  directory). It is the only top-level option and must come **before** the command.
- `syndica "<prompt>"` with no command is `syndica run "<prompt>"`. Only that one implicit rule
  exists: a leading option is never guessed to belong to some command.
- Help and output are in Chinese in v1.0; `syndica <command> --help` lists the same options as this
  page. Errors use the three-part format described in [Troubleshooting](../troubleshooting.md).
- Exit code 2 means the command line itself was wrong; 130 means interrupted with Ctrl-C.

## syndica run

Usage: `syndica run [options] <prompt>...`

Runs one task. The words of the prompt are joined with spaces. The final answer goes to stdout;
the budget summary, the verdict and the trace path go to stderr.

| Option | Meaning |
| --- | --- |
| `--agent <name>` | Role that receives the task (default `Orchestrator`). |
| `--topology <name>` | Topology from `.syndica.yaml` to assemble; the task enters through its root slot. |
| `--session <id>` | Continue a saved session with this message. |
| `--max-turns <n>` | Turns for the whole agent tree (default 200). |
| `--max-cost <amount>` | Cost cap for the whole tree; prices come from `prices:` and the backend. |
| `--max-tokens <n>` | Input plus output tokens for the whole tree. |
| `--max-wall-clock <seconds>` | Real-time limit for the run. |
| `--on-exhausted <mode>` | `graceful_wrapup` (default), `hard_stop` or `ask_human`. |
| `--flag <key=value>` | Override a flag for this run; repeatable, one value per key. |
| `--goal <text>` | What this run must deliver; needs the `acceptance` module. Default: the first message. |
| `--trust-module-output <module>` | Treat this third-party module's tool results as trusted for this run; repeatable. |
| `--no-write` | Read-only run: `Read`, `Grep`, `Glob`, `AskUser`, `AskUserQuestion` and the orchestration tools. |
| `--verdict-exit` | Exit 10 / 11 / 12 / 13 for `pass` / `partial` / `block` / `unverified`; exit 2 without `acceptance`. 130, 1 and the handoff's 3 take precedence. |
| `--handoff` | Host verification, a redacted handoff file in `.syndica/handoffs/`, and the Git question. See [development mode](../guides/development-mode.md). |
| `--git-mode <mode>` | `commit`, `push` or `manual`, answered in advance; requires `--handoff`. |
| `--trace <level>` | `off`, `structure`, `replay` or `full` (default: `trace.level`, then `replay`). |

`--agent`, `--topology` and `--session` are mutually exclusive. `--no-write` and `--handoff` exclude
`--session`; `--git-mode` needs `--handoff` and excludes `--no-write`. Exit codes: 0 success, 1 the
run failed (including an agent that left no text answer), 2 usage error, 3 `--git-mode`
preconditions not met, 130 interrupted.

## syndica chat

Usage: `syndica chat [options]`

An interactive console on the root agent. Each line is a message; the budget accumulates across
messages. Control commands: `:agents` lists active agents, `:use <agent-id>` switches the target,
`:quit` exits.

| Option | Meaning |
| --- | --- |
| `--max-turns <n>` | Turns for the whole chat (default 200). |
| `--on-exhausted <mode>` | As for `run`. |
| `--goal <text>` | As for `run`. |
| `--trust-module-output <module>` | As for `run`. |
| `--trace <level>` | As for `run`; `chat` records at most `structure`. |

## syndica agents

Roles from the built-in catalog and `.syndica.yaml`.

### syndica agents list

Prints the role names.

### syndica agents show

Usage: `syndica agents show <name>`

Prints the role's manifest as JSON, with where it came from.

## syndica topology

### syndica topology list

Prints the topologies defined in the configuration.

### syndica topology show

Usage: `syndica topology show <name>`

Prints the topology's slots as JSON.

## syndica flags

### syndica flags list

Prints every flag of the modules the configuration installs: key, module, type, default.

### syndica flags show

Usage: `syndica flags show <key>`

Prints the declaration with the value in effect and where it comes from. Both commands confirm the
configuration first when it enables a third-party module, because they import it to read its
declarations.

## syndica modules

### syndica modules list

Lists built-in and installed modules and where they come from, without importing any, and marks
those whose code is inside the workspace.

### syndica modules show

Usage: `syndica modules show <name>`

Imports the module and prints its manifest. A module whose code is inside the workspace must be
enabled by a confirmed configuration first.

## syndica artifacts

### syndica artifacts list

Prints the workspace's artifact index (`.syndica/artifacts`): id (such as `file:report.md`),
deliverable or intermediate, kind, current version, number of versions, producer.

### syndica artifacts show

Usage: `syndica artifacts show <id>`

Prints one artifact and its versions.

## syndica sessions

Saved root conversations under `.syndica/sessions`. A session id is the root agent's id.

### syndica sessions list

Lists saved sessions.

### syndica sessions show

Usage: `syndica sessions show <id>`

Prints one session's metadata: title, role, status, turns, model, tools, when it was created and
updated, how many archives and history blocks it has.

### syndica sessions resume

Usage: `syndica sessions resume <id>`

Opens an interactive chat on a saved session (the same console as `chat`).

### syndica sessions rm

Usage: `syndica sessions rm <id>`

Deletes a saved session.

### syndica sessions rubrics

Usage: `syndica sessions rubrics <id>`

Prints the session's goal and rubrics, including proposed ones.

### syndica sessions activate

Usage: `syndica sessions activate <id> <rubric-id>`

Activates a proposed rubric; it counts in acceptance only after this.

## syndica memory

Memory kept by the `memory` module.

### syndica memory list

Usage: `syndica memory list [--global]`

| Option | Meaning |
| --- | --- |
| `--global` | The global level instead of this workspace's. |

Lists long-term memory sections and short-term records that are not consolidated yet.

### syndica memory forget

Usage: `syndica memory forget <n> [--global]`, or `syndica memory forget --pending [--global]`

| Option | Meaning |
| --- | --- |
| `--pending` | Drop the short-term records that are not consolidated yet. |
| `--global` | The global level instead of this workspace's. |

Forgets long-term section `<n>` as numbered by `memory list`, or, with `--pending`, the pending
records. Give exactly one of the two.

## syndica config

### syndica config trust

Usage: `syndica config trust [--sha256 <digest>]`

| Option | Meaning |
| --- | --- |
| `--sha256 <digest>` | Trust the configuration without a terminal prompt, if its current digest is this one. The digest is printed in the refusal. |

Records the configuration's digest in the trust store. Without `--sha256` it shows the confirmation
on the terminal. A configuration outside the workspace needs no trust.

## syndica doctor

Usage: `syndica doctor [--verify]`

| Option | Meaning |
| --- | --- |
| `--verify` | After the checks, run each `workspace.verify` command in the agents' environment; any failure exits 1. |

Checks the configuration, models, workspace, the agents' environment, Git, Python, every executable
the verify commands use, and the declared interop objects.

## syndica up

Usage: `syndica up [--port <n>] [--foreground] [--no-open] [--api-token]`

| Option | Meaning |
| --- | --- |
| `--port <n>` | Port to listen on (default: a free one). |
| `--foreground` | Run in the foreground; Ctrl-C stops it. |
| `--no-open` | Do not open the browser. |
| `--api-token` | Also print a bearer token for machine clients, once. It lives only in that server process. |

Starts the web server for this workspace on `127.0.0.1` and prints a one-time login link. Needs the
`server` extra. See [Web UI and HTTP API](../guides/web-ui.md).

## syndica status

Where this workspace's server runs. Exit code 3 when none is running, 1 when it holds its lock but
does not answer.

## syndica down

Saves sessions and stops this workspace's server.

## syndica mcp

Serves the workspace's tools as an MCP server on stdio. Needs the `mcp` extra. See
[Interop](../guides/interop.md#mcp).

## syndica acp

Accepts editor sessions as an ACP agent on stdio. Needs the `acp` extra. See
[Interop](../guides/interop.md#acp).

## syndica batch

Usage: `syndica batch <tasks.jsonl>`

Runs one task per line (`{"id": ..., "prompt": ...}`, optionally `"agent"` and `"goal"`) and prints
one JSON result per line. A failed task does not stop the rest; exit 1 if any failed. See
[Training export](../guides/training-export.md).

## syndica export

Usage: `syndica export <format> --output <dir> [--include-unverified]`

| Option | Meaning |
| --- | --- |
| `--output <dir>` | Required. Directory for the dataset and `manifest.json`; must be outside the workspace, new or empty. |
| `--include-unverified` | Include `unverified` samples and record that in the manifest. |

`<format>` is `sft`, `sharegpt`, `preference`, `rl` or `trajectory`.

## syndica version

Prints the installed version.

## syndica trace

Traces under `.syndica/traces`. A trace id may be the full id, a unique prefix, or the directory
name printed after a run. See [Observability](../guides/observability.md).

### syndica trace list

Lists traces newest first: start time (UTC), id, status, agents, spans, size, command.

### syndica trace show

Usage: `syndica trace show <trace-id>`

Who created whom, who waits on whom, the slowest tool calls, model calls per agent, a timeline,
budget and long-horizon events, and modules per agent.

### syndica trace replay

Usage: `syndica trace replay [--allow-divergence] <trace-id>`

| Option | Meaning |
| --- | --- |
| `--allow-divergence` | Keep replaying after a divergence and report each one (still exit 1). |

Exit 0 reproduced, 1 diverged or ended differently, 2 the trace cannot be replayed.

### syndica trace export

Usage: `syndica trace export [--otlp <url>] <trace-id>`

| Option | Meaning |
| --- | --- |
| `--otlp <url>` | Collector endpoint, for example `http://localhost:4318`. Without it, the endpoint and headers from your environment are used. Headers are never sent with `--otlp`. |

Sends the trace as OTLP/HTTP JSON; exit 1 if the export fails. Needs the `otel` extra.

### syndica trace prune

Usage: `syndica trace prune [--keep <n>]`, or `syndica trace prune <trace-id>...`

| Option | Meaning |
| --- | --- |
| `--keep <n>` | How many finished traces to keep (default 50). Cannot be combined with named traces. |

Deletes old traces, keeping the newest finished ones with their replays and never deleting an
unfinished one; named traces are deleted exactly.
