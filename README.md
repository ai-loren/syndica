# Syndica

Syndica is a multi-agent harness for **verifiable knowledge work**: a team of agents that runs for
hours, reads and writes files, runs commands, asks a person when it matters, and leaves behind an
auditable trace, versioned artifacts, an acceptance verdict, and data you can train on.

> **Status: 1.0.0rc1, a release candidate.** This is a pre-release of v1.0. The public API is being
> frozen for 1.0 and can still change before the final release.

## Why

Most agent frameworks can call tools. Syndica is built around four claims:

1. **Multi-agent is a first-class runtime concept.** Agents have process semantics (spawned,
   messaged, paused, aborted, reaped, revived), delegation and forking are kernel tools, and the
   organisation of a team is declarative data.
2. **Knowledge work is broader than code.** Reports, tables, diagrams and datasets are work
   products just like source files.
3. **Artifacts and acceptance belong in the system.** Work is done when deliverables satisfy
   criteria traceable to the request, not when a model says so.
4. **Inference and training share one substrate.** The run that proves a task was done becomes
   training data, with acceptance verdicts as rewards.

## Install

Linux or macOS with Python 3.11 to 3.13. On Windows, use WSL2.

```bash
python -m pip install --pre 'syndica[openai]'
```

`openai` is the model client (any OpenAI-compatible endpoint, Gemini, OpenRouter). Other extras:
`server` (web UI and HTTP API), `otel`, `mcp`, `acp`, `a2a`, `trl`. Install Syndica outside the
directories agents will work in. See [Install](docs/user/install.md) for every extra and for
installing from source.

## Quickstart

Point Syndica at any OpenAI-compatible endpoint (keys stay in your environment):

```bash
export OPENAI_BASE_URL=https://api.example.com/v1
export OPENAI_MODEL=your-model-name
export OPENAI_API_KEY=your-key
```

Run a task in an empty directory, then look at what happened:

<!-- executed: quickstart -->
```bash
mkdir syndica-demo
cd syndica-demo
syndica doctor
syndica run --agent Worker "Write a four-line poem about version control to poem.md" --max-turns 10
syndica trace list
syndica trace show <trace-id>
```

The answer goes to stdout; the budget summary and the trace directory go to stderr.
`syndica up` (with the `server` extra) opens the web UI for the same workspace. The
[quickstart](docs/user/quickstart.md) walks through each step, and
[Model providers](docs/user/guides/models.md) covers Gemini, OpenRouter and failover between
platforms.

## Features

- **A multi-agent runtime** with built-in `Orchestrator`, `Planner`, `Explorer` and `Worker` roles,
  declarative role manifests, topologies, delegation and forking.
  [Runtime and agents](docs/user/concepts/runtime-and-agents.md)
- **Tools for files, commands and terminals**, with per-agent workspace isolation and a version
  layer. [Tools](docs/user/concepts/tools.md)
- **Safety by design**: a danger judge with host dispositions, one-shot confirmations, and
  untrusted-content tracking that escalates risky calls.
  [Danger judgement](docs/user/concepts/danger.md),
  [Trust and provenance](docs/user/concepts/trust-and-provenance.md)
- **Artifacts and acceptance**: versions, deliverables, rubrics anchored to the request, and a
  four-value verdict (`pass`, `partial`, `block`, `unverified`).
  [Artifacts and acceptance](docs/user/concepts/artifacts-and-acceptance.md)
- **Long-horizon control**: budgets over turns, tokens, cost and wall clock; progress measured by
  the verifier; stop criteria; drift checks; pluggable strategies.
  [Budgets and long-horizon work](docs/user/concepts/budgets-and-long-horizon.md)
- **State**: persistent sessions, context compaction and two-level memory.
  [Sessions, compaction and memory](docs/user/concepts/sessions-compaction-memory.md)
- **Traces you can replay offline**, OTLP export, and one redactor before every write.
  [Traces and redaction](docs/user/concepts/traces-and-redaction.md),
  [Observability](docs/user/guides/observability.md)
- **A local web UI and HTTP API** (`syndica up`). [Web UI](docs/user/guides/web-ui.md)
- **Interop** over MCP, ACP, A2A and an OpenAI-compatible HTTP API.
  [Interop](docs/user/guides/interop.md)
- **Training export**: batch runs, SFT, ShareGPT, preference, RL and neutral trajectories, with TRL
  dry runs. [Training export](docs/user/guides/training-export.md)
- **Development mode**: agents change a repository while the host verifies the result and a person
  decides what is committed. [Development mode](docs/user/guides/development-mode.md)
- **Extension points**: Runtime Modules with flags, custom tools, class-based agents, and stores
  and workspaces with published contract suites.
  [Writing a module](docs/user/guides/writing-a-module.md),
  [Writing a backend](docs/user/guides/writing-a-backend.md)

## Safety notes

Syndica runs agents that execute commands with **your** permissions on **your** machine.

- **No multi-tenant isolation.** It is designed for a user-owned machine or container, not for
  hostile multi-tenant execution.
- **The real isolation boundary is a sandboxed workspace backend.** With the built-in local
  workspace, code an agent runs can read what your user can read and reach the network. Use it on
  input you trust.
- **Third-party Runtime Modules and backends run in-process** and cannot be sandboxed; their output
  is marked untrusted.
- **A project's `.syndica.yaml` is untrusted** until you confirm it, and can only tighten safety
  settings.
- **No rule matching does not mean a command is safe.**

Read the [security model](docs/user/security.md). To report a vulnerability, follow the
[security policy](https://github.com/ai-loren/syndica/blob/main/SECURITY.md).

## Documentation

The user documentation is in [`docs/user`](docs/user/index.md) and builds into a site with MkDocs.

- [Install](docs/user/install.md) · [Quickstart](docs/user/quickstart.md) ·
  [Troubleshooting](docs/user/troubleshooting.md)
- Guides: [configuration](docs/user/guides/configuration.md),
  [model providers](docs/user/guides/models.md),
  [writing an agent](docs/user/guides/writing-an-agent.md),
  [writing a tool](docs/user/guides/writing-a-tool.md)
- Reference: [CLI](docs/user/reference/cli.md),
  [configuration schema](docs/user/reference/configuration.md),
  [flags](docs/user/reference/flags.md), [HTTP API](docs/user/reference/http-api.md)

**Language.** The documentation is in English. In v1.0, runtime error messages, CLI output, and
the built-in roles' prompts are in Chinese; English is planned for v1.x. Every error has three
parts (期望 / 实际 / 下一步: expected / received / next action), and
[Troubleshooting](docs/user/troubleshooting.md) translates the common ones.

## Development

Contributors and coding agents start with [`AGENTS.md`](AGENTS.md): the layering rules, the
invariants and the testing discipline. The maintainers' design record is in
[`docs/design`](docs/design/2026-09-03-architecture.md) (in Chinese).

```bash
uv sync --all-extras
```

Run the normal local gates:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run python scripts/check_deps.py
uv run python scripts/check_links.py
uv run pytest -q
```

Build the documentation site in strict mode (broken links fail the build):

```bash
sh scripts/check_docs.sh
```

Every code block in this README and under `docs/user` is executed or parsed by
`tests/test_docs.py`, or explicitly marked with `<!-- not-tested: reason -->`.

## License

MIT. See [`LICENSE`](LICENSE).
