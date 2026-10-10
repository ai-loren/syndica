# Changelog

All notable changes to Syndica are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Version numbers follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) for the public surface described in the
[versioning policy](docs/user/versioning.md), written as [PEP 440](https://peps.python.org/pep-0440/)
strings (`1.0.0rc1`, `1.0.0`).

## [Unreleased]

## [1.0.0rc1] - unreleased

The first release candidate of Syndica 1.0: milestones 1 to 11 of the v1.0 roadmap, shipped as one
distribution, `syndica`, with optional extras `openai`, `server`, `otel`, `mcp`, `acp`, `a2a` and
`trl`. It runs on Linux and macOS with Python 3.11, 3.12 and 3.13; on Windows, use WSL2.

### Added

- **Multi-agent kernel.** An async `Runtime` for many agents with a process-like lifecycle: spawn,
  message, wait, abort, reap, revive from `ZOMBIE`, shut down. A `BaseAgent` turn loop over an LLM
  abstraction with tools, hooks, events, retries and structured failures. Declarative agent
  manifests, built-in `Orchestrator`, `Planner`, `Explorer` and `Worker` roles, and declarative
  topologies with singleton, keyed and on-demand slots.
- **Built-in tools.** Orchestration (`Delegate`, `Fork`, `SendMessage`, `QueryAgent`, `KillAgent`),
  local execution (`Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`, `BashOutput`, `KillBash`,
  `Terminal`), and human-in-the-loop (`AskUser`, `AskUserQuestion`, one-shot confirmation of risky
  calls).
- **Models.** An OpenAI-compatible backend (`syndica[openai]`) with retries, backoff, and rotation
  across Gemini, OpenRouter free models and a generic OpenAI-compatible endpoint.
- **CLI.** `syndica run` and `chat`, resource commands for agents, topologies, flags, modules,
  artifacts, sessions and memory, `config trust`, `doctor [--verify]`, `version` and `--version`.
  `run --handoff` lets the host re-run the project's verify commands, write a redacted handoff and
  commit or push only after a human chooses; `run --no-write` keeps read-only tools. Every CLI run
  gives agents an allowlisted command environment and blocks Git writes and removal of the workspace
  root.
- **Observability and redaction.** One trace per `run` and per `chat` session, with spans and
  spawn / message edges between agents; `syndica trace list | show | replay | export | prune`,
  offline replay at the default `replay` level, and OTLP/HTTP JSON export (`syndica[otel]`). One
  kernel `Redactor` runs before anything is persisted or exported.
- **Extension system.** Third-party capabilities install as Runtime Modules through the
  `syndica.modules` entry point group: a manifest, Flags, tools, plugins and start / stop hooks,
  enabled explicitly, subject to config trust, with untrusted output by default. Three-layer assembly
  (host plugins, Runtime Modules, per-agent hooks), declared Flags with a generated reference, and
  namespace junctions.
- **Execution plane.** The full `Workspace` / `Portal` protocol with a contract suite for backends, a
  pseudo-terminal, a pure-Python version layer under `.syndica/vcs`, per-agent isolation
  (`worktree`, `snapshot`) with a per-file merge back, optimistic concurrency in the shared
  workspace, and file taint with artifact provenance: content that a command changed, an external
  edit, or a write after reading untrusted content reads back as untrusted.
- **Sessions, compaction and memory.** The root conversation is persisted and resumed
  (`run --session`, `sessions resume`). Compaction is rule-based, pruning, summary or goal-directed.
  Two-level memory (workspace and global): only the root coordinator writes global memory, and
  untrusted content never reaches it.
- **Artifacts and acceptance.** Artifacts with identity and versions, `Deliver` for deliverables,
  handlers resolved by `(kind, mime)`. A goal with rubrics that quote the request they come from;
  format, output and outcome verifiers as a Runtime Module; a four-value verdict (`pass` /
  `partial` / `block` / `unverified`) derived by the kernel; human feedback becomes a proposed rubric
  that a person activates with `sessions activate`.
- **Long-horizon control.** A budget tree over turns, tokens, cost and wall clock, `GetBudget` and
  reminders at 50, 25 and 10 percent, progress measured from acceptance verdicts, three stop criteria
  (done, out of budget, stalled), a pluggable `LongHorizonStrategy` with a contract suite, background
  tools, reaping of finished delegated agents, deadlines that interrupt the model call in flight, and
  `run --verdict-exit`.
- **Product surface.** `syndica up | status | down` (`syndica[server]`): a server on `127.0.0.1`
  with REST, resumable SSE and a WebSocket, described by a reviewed OpenAPI snapshot, and a web UI
  shipped inside the package (no Node needed): sessions, chat with untrusted labels, the agent tree,
  the event timeline, confirmations and questions, artifacts with preview, diff and feedback,
  budget, progress and trace panels, and a terminal.
- **Interop.** MCP (`syndica[mcp]`): connect to external tool servers, and serve the workspace with
  `syndica mcp` or at `/mcp` (read-only tools by default). ACP (`syndica[acp]`): editors drive
  Syndica through `syndica acp`, and roles can host third-party ACP agents (`model: acp:<name>`).
  A2A (`syndica[a2a]`): an agent card and `POST /a2a/`, and roles backed by remote agents
  (`model: a2a:<name>`). OpenAI-compatible HTTP on `syndica up`: `/v1/models`,
  `/v1/chat/completions` and `/v1/responses`, with a machine token from `syndica up --api-token`.
  Content arriving through any protocol is untrusted; credentials come only from the user-level
  `interop.yaml`.
- **Training export.** `syndica batch` runs a JSONL task list; `syndica export sft | sharegpt |
  preference | rl | trajectory` writes a dataset and a manifest from sessions, goals, rubrics, traces
  and artifact records, with the neutral `syndica.trajectory/1` format underneath. `unverified`
  samples are left out unless `--include-unverified` is given. `syndica[trl]` adds dry runs that load
  an exported SFT or preference file into TRL's `SFTTrainer` / `DPOTrainer`.
- **Packaging.** Inline type information (`py.typed`), Linux and macOS classifiers, the web UI
  build inside the wheel, and a `SHA256SUMS` file for each release's artifacts.

### Breaking changes before 1.0

Public names withdrawn before the 1.0 freeze are listed here, with what to use instead. Nothing has
been listed yet.

### Known limitations

- Runtime messages, CLI output and code comments are in Chinese; the user documentation is in
  English. Translating runtime messages is planned for 1.x.
- Windows is supported only through WSL2.
- Sub-agents and their mailboxes are not persisted: resuming a session restores the root
  conversation and names the sub-agents that were lost.
- There is no multi-tenant isolation, and third-party code that runs in-process cannot be
  sandboxed. See the [security policy](SECURITY.md).

[Unreleased]: https://github.com/ai-loren/syndica/compare/v1.0.0rc1...HEAD
[1.0.0rc1]: https://github.com/ai-loren/syndica/releases/tag/v1.0.0rc1
