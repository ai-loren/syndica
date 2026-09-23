# Syndica

Syndica is a multi-agent harness for **verifiable knowledge work**.

It is designed for jobs where a single chat loop is too small: long-running work, several
specialized agents, local files and commands, human approvals, auditable traces, artifacts,
acceptance criteria, and eventually training-data export.

> Status: **pre-release**.
>
> Milestone 1 is complete on `main`. The project is still pre-v1.0, so public APIs may
> change until the release boundary is frozen.

## Why Syndica Exists

Most agent frameworks can call tools. Syndica treats a team of agents as the primitive:

1. **Multi-agent is a first-class runtime concept.** Agents can be spawned, messaged, waited on,
   aborted, reaped, revived from `ZOMBIE`, delegated to, and forked with context.
2. **Knowledge work is broader than code.** Reports, tables, diagrams, datasets, and source files
   should all be first-class work products.
3. **Artifacts and acceptance belong in the system.** Work is not done because a model says so; it
   is done when deliverables satisfy traceable criteria.
4. **Inference and training should share one substrate.** The same execution history that proves
   a task was completed should become neutral data for SFT, preference learning, or RL.

The long-term goal is a deployable v1.0 product, not only a Python library.

## Current Capability

Milestone 1 provides a working local multi-agent kernel:

- Async `Runtime` for multiple agents, process-like lifecycle, message delivery, waiting, abort,
  shutdown, and recovery.
- `BaseAgent` turn loop with LLM abstraction, tool execution, hooks, events, retries, safety flags,
  prefix drift detection, and structured failures.
- Declarative agent manifests, an agent catalog, built-in `Orchestrator`, `Planner`, `Explorer`,
  and `Worker` roles.
- Declarative topology assembly with singleton, keyed, and on-demand slots.
- Runtime orchestration tools: `Delegate`, `Fork`, `SendMessage`, `QueryAgent`, `KillAgent`.
- Local execution tools: `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`, `BashOutput`,
  `KillBash`.
- Human-in-the-loop tools: `AskUser`, `AskUserQuestion`, plus one-shot confirmation for dangerous
  tool calls.
- CLI surface and YAML L2 scenario runner.
- OpenAI-compatible LLM backend.

Milestone 1 audit: [`docs/audits/2026-09-23-m1-final-audit.md`](docs/audits/2026-09-23-m1-final-audit.md).

## Install From Source

The package is not published yet. Use an editable checkout:

```bash
git clone https://github.com/ai-loren/syndica.git
cd syndica
python -m pip install -e '.[openai]'
```

For development:

```bash
uv sync --extra openai --extra dev
```

Configure an OpenAI-compatible endpoint:

```bash
export OPENAI_BASE_URL=https://your-endpoint.example/v1
export OPENAI_MODEL=your-model
export OPENAI_API_KEY=your-key
```

`OPENAI_API_KEY` is optional for endpoints that do not require it.

### OpenRouter free models

OpenRouter is supported directly through the same OpenAI-compatible backend. Setting either
OpenRouter variable selects this mode:

```bash
export OPENROUTER_API_KEY=your-openrouter-key
export OPENROUTER_MODEL=openrouter/free
```

`OPENROUTER_MODEL` is optional and defaults to `openrouter/free`. To enforce zero-cost inference,
Syndica accepts only `openrouter/free` or a specific model ID ending in `:free` while OpenRouter
mode is active. The free router can select a model that supports the request's required features,
including tool calling.

For the first multi-agent demo, a specific free model that advertises `tools` support is often
easier to diagnose than the router:

```bash
export OPENROUTER_MODEL=cohere/north-mini-code:free
```

OpenRouter mode takes precedence if OpenRouter and generic `OPENAI_*` variables are both present.
Keep API keys in environment variables; `.env` files are ignored by Git and are not loaded
automatically.

## Quick Start

Inspect the built-in catalog:

```bash
syndica version
syndica doctor
syndica agents list
syndica flags list
```

Run a prompt with the default `Orchestrator`:

```bash
syndica "inspect this repository and summarize what it does"
```

Run a specific agent:

```bash
syndica run "read the repository README and suggest improvements" \
  --agent Worker \
  --max-turns 20
```

Use an explicit config file:

```bash
syndica --config .syndica.yaml run "review the draft" --topology review
```

Start the interactive console:

```bash
syndica chat
```

Inside `chat`:

```text
:agents              show active agents
:use <agent-id>      switch the target agent
:quit                exit
```

## Configuration

The CLI reads `.syndica.yaml` from the current directory by default. If the file is missing, it
falls back to the built-in catalog. Use `--config path.yaml` before the command to select another
file.

Configuration is parsed with safe YAML, must be a single mapping, rejects duplicate keys, and
rejects unknown fields rather than silently ignoring them.

```yaml
version: 1

agents:
  - name: Reviewer
    description: Use this agent to review a completed artifact before handoff.
    system_prompt: You review finished work and report concrete issues first.
    model: review-model
    tools: [Read, Grep, Glob]
    budget:
      turns: 20

topologies:
  review:
    slots:
      - name: root
        agent: Reviewer
        is_root: true
        cardinality: singleton
        boot: eager
        exit_mode: zombie
```

Important current limits:

- Milestone 1 executes `--max-turns`.
- `--max-cost`, `--max-tokens`, and `--max-wall-clock` are parsed but fail explicitly until the
  budget engine lands in Milestone 8.
- `chat` sessions are process-local.
- Cross-process `--session` recovery and `syndica sessions ...` require the SessionStore planned
  for Milestone 6.

## CLI Reference

```text
syndica run <prompt>
syndica chat
syndica agents list | show <name>
syndica topology list | show <name>
syndica flags list | show <key>
syndica sessions list | show <id> | resume <id> | rm <id>   # reserved for M6
syndica doctor
syndica version
```

`run` accepts:

```text
--agent <name>
--topology <name>
--session <id>
--max-turns <n>
--max-cost <amount>
--max-tokens <n>
--max-wall-clock <seconds>
--flag <key=value>        repeatable
```

`--agent`, `--topology`, and `--session` are mutually exclusive. `--flag` keys are validated
against installed Runtime Module declarations.

## Architecture

Syndica is intentionally layered:

```text
kernel      runtime, agent, tools, hooks, context, topology, protocols
surfaces    cli, future server, future runner
backends    optional LLM, workspace, store, artifact, protocol and export packages
```

Hard rules:

- `kernel` does not import surfaces or backends.
- surfaces depend on `kernel`, not on each other.
- backends depend on `kernel`, not on each other.
- `kernel` does not read environment variables.
- public APIs are fully typed and shipped with `py.typed`.
- names in `kernel.__all__`, hook junctions, Store Protocols, Flag keys, manifest fields, and
  YAML fields are compatibility surface.

Detailed design: [`docs/design/2026-09-03-architecture.md`](docs/design/2026-09-03-architecture.md).

## Safety Model

Syndica can read files, write files, and run commands. It is designed for a user-owned local
machine or container, not for hostile multi-tenant execution.

Current safety features:

- dangerous command detection and host-controlled disposition;
- one-shot tool confirmation for risky calls;
- trusted/untrusted content markers;
- conservative escalation when high-risk calls can be traced to untrusted content;
- workspace path containment for local tools;
- structured errors with expected / received / next action;
- explicit logging guidance for evidence-bearing warnings and errors.

Current non-goals:

- no Python-level sandbox for same-process third-party code;
- no multi-tenant isolation guarantee;
- no claim that unmatched commands are safe, only that no current rule matched.

See the security section in the architecture spec for the full threat model.

## Development

Run the normal local gates:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run python scripts/check_deps.py
uv run pytest -q
```

Useful focused suites:

```bash
uv run pytest tests/kernel -q
uv run pytest tests/contracts -q
uv run pytest tests/cli -q
uv run pytest tests/runner -q
uv run pytest tests/integration -q
uv run pytest tests/backends -q
```

The dependency guard is part of the architecture, not just lint. It prevents layer leaks such as
`kernel` importing a CLI module or backend SDK.

## Testing Strategy

The project uses four layers:

- **Unit and L1 tests** for pure functions, state machines, schema generation, and local turn-loop
  behavior.
- **L2 scenario replay**: declarative YAML + scripted fake LLM driving real Runtime, tools,
  events, messages, and failure paths.
- **Protocol contract tests** for replaceable stores and future backends.
- **Real LLM E2E** for product-quality validation, kept out of the free CI path.

Every important invariant should be backed by mutation-style checks. A green test suite is not
enough if a realistic broken implementation can survive it.

## Roadmap

The v1.0 roadmap has 12 internal milestones:

| # | Milestone | Status |
|---|---|---|
| 1 | Kernel skeleton | Done |
| 2 | Self-bootstrapping | Planned |
| 3 | Observability + redaction | Planned |
| 4 | Complete extension system | Planned |
| 5 | Complete execution layer + provenance | Planned |
| 6 | State: sessions, compression, memory | Planned |
| 7 | Artifacts and acceptance | Planned |
| 8 | Long-horizon task control | Planned |
| 9 | HTTP/SSE/Web product surface | Planned |
| 10 | MCP / ACP / A2A / OpenAI HTTP interop | Planned |
| 11 | Training-data export | Planned |
| 12 | v1.0 release readiness | Planned |

Expanded roadmap: [`docs/plans/2026-09-23-v1-milestone-roadmap.md`](docs/plans/2026-09-23-v1-milestone-roadmap.md).

## Logging

Syndica is a library and does not install handlers or change application logging policy. Hosts
must keep both the `syndica` logger and its handlers at `WARNING` or lower. Raising either
threshold to `ERROR` drops the only record for a low-risk call that was allowed despite
untrusted provenance.

Do not randomly sample or broadly deduplicate `syndica.kernel.runtime` errors or the
untrusted-provenance warning from `syndica.kernel.tool_executor`: each can be the only evidence
of one discarded input, broker failure, or safety-relevant allow decision. The repeat-prone
`syndica.kernel.hooks.center` discarded-return error may be rate-limited by the exact
`(junction, callback, discarded value)` tuple over a bounded window, provided the first record
and a periodic suppressed-count summary are retained. Prefer sink-side rate limits and alerts
over raising logger thresholds; sustained repeats mean the hook is registered on the wrong
junction and should be fixed.

## License

MIT. See [`LICENSE`](LICENSE).
