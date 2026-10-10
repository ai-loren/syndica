# Syndica

Syndica is a multi-agent harness for **verifiable knowledge work**: a team of agents that can run
for hours, read and write files, run commands, ask a person when it matters, and leave behind an
auditable trace, versioned artifacts, an acceptance verdict, and data you can train on.

It is built for jobs where a single chat loop is too small: several specialised agents, local
files and commands, human approvals, deliverables that have to be checked against what was asked,
and long runs that need budgets and stop criteria.

!!! info "Status: 1.0.0rc1 (release candidate)"
    This is a pre-release of v1.0. The public surface is being frozen for 1.0; until the final
    release it can still change. The *Versioning* page explains what counts as the public API and
    how changes are announced.

## Four claims

Syndica is designed around four claims. A change that contradicts one of them is going the wrong
way.

1. **Multi-agent is a first-class runtime concept.** Agents have process semantics: they are
   spawned, messaged, paused, aborted, reaped, and revived from `ZOMBIE`. Delegating, forking and
   messaging are kernel tools, and the organisation of a team is declarative data (a
   [topology](concepts/runtime-and-agents.md#topologies)), not code.
2. **Knowledge work is broader than code.** Reports, tables, diagrams and datasets are work
   products just like source files.
3. **Artifacts and acceptance belong in the system.** Work is not done because a model says so.
   Artifacts have identity and versions, and acceptance criteria must be traceable to the original
   request. See [Artifacts and acceptance](concepts/artifacts-and-acceptance.md).
4. **Inference and training share one substrate.** The run that proves a task was done also
   becomes data: acceptance verdicts are reward and preference signals. See
   [Training export](guides/training-export.md).

Long-horizon work (hours to days) is a deliberate research direction: budgets, progress measured
by the verifier rather than by the agent's own account, drift checks and pluggable strategies. See
[Budgets and long-horizon work](concepts/budgets-and-long-horizon.md).

## What you get

- A local multi-agent runtime with built-in `Orchestrator`, `Planner`, `Explorer` and `Worker`
  roles, declarative role manifests and topologies.
- File, shell and terminal tools, with a danger judge, one-shot human confirmation and
  untrusted-content tracking that escalates risky calls.
- One trace per run, replayable offline; OTLP export; a single redactor for everything written to
  disk or sent elsewhere.
- Persistent sessions, context compaction and two-level memory.
- Artifact versions, deliverables and a four-value acceptance verdict (`pass`, `partial`, `block`,
  `unverified`).
- Budgets over turns, tokens, cost and wall clock, with long-horizon control.
- A local web UI and HTTP API (`syndica up`), and interop over MCP, ACP, A2A and an
  OpenAI-compatible HTTP API.
- Batch runs and training-data export (SFT, ShareGPT, preference, RL, neutral trajectories).
- Extension points: Runtime Modules with flags, custom tools, class-based agents, and replaceable
  stores and workspaces with published contract suites.

## Language of messages

The documentation is in English. In v1.0, **runtime error messages, CLI output, and the prompts and
descriptions of the built-in roles are in Chinese**; translating them is planned for v1.x. Every
error has the same three parts (期望 / 实际 / 下一步 = expected / received / next action), so you
can act on it with a translation tool. [Troubleshooting](troubleshooting.md) explains the format and
lists the most common messages with their English meaning.

## Platforms

Linux and macOS, Python 3.11 to 3.13. Windows users run Syndica inside WSL2. See
[Install](install.md).

## Where to go next

- [Install](install.md) and the [five-minute quickstart](quickstart.md).
- [Concepts](concepts/runtime-and-agents.md): how a run is put together.
- [Configuration](guides/configuration.md): the `.syndica.yaml` file.
- [Security model](security.md): what Syndica protects against, and what it does not.
