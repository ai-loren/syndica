# Syndica

Syndica is a multi-agent harness for **verifiable knowledge work**.

It is designed for jobs where a single chat loop is too small: long-running work, several
specialized agents, local files and commands, human approvals, auditable traces, artifacts,
acceptance criteria, and eventually training-data export.

> Status: **pre-release**.
>
> Milestones 1 to 8 are complete on `main`. The project is still pre-v1.0, so public APIs may
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
  `KillBash`, `Terminal`.
- Human-in-the-loop tools: `AskUser`, `AskUserQuestion`, plus one-shot confirmation for dangerous
  tool calls.
- CLI surface and YAML L2 scenario runner.
- OpenAI-compatible LLM backend.

Milestone 1 audit: [`docs/audits/2026-09-23-m1-final-audit.md`](docs/audits/2026-09-23-m1-final-audit.md).

Milestone 2 makes Syndica able to change its own repository under a host that checks the work (see
[Development Mode](#development-mode)):

- `run --handoff`: the host re-runs the project's verify commands, takes the Reviewer's verdict only
  from a review of the final tree, writes a redacted handoff, and commits or pushes only after a
  human chooses to.
- `run --no-write` for read-only planning, `doctor --verify`, and this repository's own
  `syndica-dev` roles, prompts, and topology.
- Guards for every CLI run: an allowlisted command environment, unconditional blocks on Git writes and
  removing the workspace root, process-group cleanup and output limits for commands and searches.

Milestone 2 audit: [`docs/audits/2026-10-01-m2-final-audit.md`](docs/audits/2026-10-01-m2-final-audit.md).

Milestone 3 records every run as a trace and makes it replayable (see [Observability](#observability)):

- One trace per `run` and per `chat` session under `.syndica/traces/`: spans for agents, turns, model
  calls, tool calls, confirmations and waits, plus spawn and message edges between agents.
- `syndica trace show` answers who created whom, who waits on whom, and which tool calls were slowest.
- At the default `replay` level a captured run replays offline with `syndica trace replay`: the
  outside world comes from the record, the kernel runs for real and is compared item by item.
- OTLP/HTTP JSON export to a collector named in your own environment or on the command line.
- One kernel `Redactor` for everything persisted or exported, with an AST guard over every write and
  outbound point in the package.

Milestone 3 audit: [`docs/audits/2026-10-02-m3-final-audit.md`](docs/audits/2026-10-02-m3-final-audit.md).

Milestone 4 completes the extension surface: three-layer assembly (host plugins, Runtime Modules,
per-agent hooks), Flag declarations, and namespace junctions a capability can declare and trigger.

Milestone 4 audit: [`docs/audits/2026-10-03-m4-final-audit.md`](docs/audits/2026-10-03-m4-final-audit.md).

Milestone 5 completes the execution plane and adds untrusted-content tracking:

- The full `Workspace` / `Portal` protocol with a contract suite any backend must pass, plus the
  pseudo-terminal `Terminal` tool for programs that need a TTY.
- A version layer written in pure Python under `.syndica/vcs` (git object format, but git is never
  run on it): every `Write`/`Edit` records a version and becomes a tracked artifact; blobs are
  redacted before they touch disk, and a redacted version is not restorable.
- Per-agent workspace isolation for `spawn`/`Delegate`/`Fork` (`worktree` and `snapshot`), with a
  per-file merge back into the parent; conflicts are returned, never silently overwritten.
- Optimistic concurrency in the shared workspace: overwriting a file you never read is a conflict.
- File taint and an artifact-provenance graph: content a command changed, an external edit, or a
  read after untrusted content reads back as untrusted, and running an untrusted local script forces
  the danger escalation even under an allow-all policy. The real isolation boundary is a sandboxed
  workspace backend; command output itself stays untrusted.

Milestone 5 audit: [`docs/audits/2026-10-04-m5-final-audit.md`](docs/audits/2026-10-04-m5-final-audit.md).

Milestone 6 adds state: the root conversation is persisted under `.syndica/sessions` and resumed with
`run --session` or `sessions resume`; context compaction (rule-based, pruning, summary) runs at a
threshold, on overflow, or on request; and two-level memory (workspace and global) keeps facts
across sessions, with only the root coordinator writing global memory and untrusted content never
reaching it.

Milestone 6 audit: [`docs/audits/2026-10-05-m6-final-audit.md`](docs/audits/2026-10-05-m6-final-audit.md).

Milestone 7 adds artifacts and acceptance (see the `acceptance` module under
[Configuration](#configuration)):

- Artifacts have identity and versions: every write is registered, `Deliver` marks deliverables,
  and `syndica artifacts list | show` prints them with their versions and producers.
- Pluggable artifact handlers resolved by `(kind, mime)` precision, with a generic text handler.
- A goal and its rubrics: rubrics are projected before work, every rubric must quote the request it
  comes from, and rubrics only a human can judge are never passed automatically.
- Three acceptance layers: a zero-cost format check, a conservative output verifier and an outcome
  verifier that has to re-open its evidence; the kernel derives the four-value verdict
  (`pass` / `partial` / `block` / `unverified`) itself and shows it in the CLI and the trace.
- Human feedback becomes a proposed rubric that a person activates.

Milestone 7 audit: [`docs/audits/2026-10-05-m7-final-audit.md`](docs/audits/2026-10-05-m7-final-audit.md).

Milestone 8 adds long-horizon control (see the `long-horizon` and `background` modules and the
budget options under [Configuration](#configuration)):

- A budget tree over turns, tokens, cost and wall clock: a child's budget is carved from its
  parent's and given back when it finishes, and every model call is admitted against an estimate
  first. When the budget runs out the agent gets one wrap-up turn by default; `GetBudget` and
  reminders at 50, 25 and 10 percent keep it aware.
- Progress measured from the acceptance verdicts (regressions are warnings), three independent stop
  criteria (done, out of budget, stalled), and a zero-cost drift check against the goal and rubrics.
- A pluggable long-horizon strategy with a contract suite; the kernel ships a baseline.
- Goal-directed compaction, with the full pre-compaction history redacted and archived; slow tools
  moved to the background; finished delegated agents reaped; isolated topology slots.

Milestone 8 audit: [`docs/audits/2026-10-06-m8-final-audit.md`](docs/audits/2026-10-06-m8-final-audit.md).

Milestone 9 adds the product surface (see [Web UI](#web-ui)):

- `syndica up / status / down`: a local server with REST, SSE event streams (resumable with
  `Last-Event-ID`) and a WebSocket for requests, answers and terminals. The OpenAPI document is a
  reviewed snapshot in `docs/reference/openapi.json`.
- A React UI shipped inside the package (no Node needed): sessions, chat with untrusted labels, the
  agent tree, the event timeline, confirmations and questions, artifacts with preview, diff and
  feedback, budget, progress and trace panels, and a terminal. It also works on narrow screens.
- A deadline now interrupts the model call in flight; budgets carry across resumed sessions;
  `run --verdict-exit` turns the acceptance verdict into an exit code.

Milestone 9 audit: [`docs/audits/2026-10-08-m9-final-audit.md`](docs/audits/2026-10-08-m9-final-audit.md).

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

### Gemini API

Gemini is reached through Google's OpenAI-compatible endpoint, so it needs no extra dependency:

```bash
export GEMINI_API_KEY=your-gemini-key
export GEMINI_MODEL=gemini-3.5-flash-lite
```

`GEMINI_MODEL` is optional and defaults to `gemini-3.5-flash-lite`. Google bills the project a key
belongs to, and the key itself does not reveal which kind of project that is. A project without
billing uses only the free tier and answers HTTP 429 once a quota runs out; a project with billing
enabled is charged for every request. Use a key from a project without billing if inference must
stay free.

### Retries and several platforms

Models are always called through a rotation. With one platform configured, a failed request is
retried on that platform after a backoff. With several, every role whose manifest leaves `model` as
`default` (all built-in roles do) shares one rotation across them:

- A request goes to the first platform that is not backing off. If that platform fails, the same
  request goes to the next one right away, and the failed platform backs off: about 2 seconds after
  its first consecutive failure, then 4, 8, and so on up to 5 minutes, each with ±20% jitter.
- When the platform says how long to wait (`Retry-After`, OpenRouter's `X-RateLimit-Reset`, or
  Google's `RetryInfo`), the longer wait wins, so a platform whose daily quota ran out is skipped
  until the quota resets.
- A success clears that platform's backoff, and the first platform takes over again as soon as its
  backoff ends.
- When every platform is backing off, the request waits for the earliest one to recover if that
  happens within 2 minutes of the request starting. Failures that waiting cannot fix are not waited
  for: any HTTP 4xx other than 408, 425 and 429, such as a bad key or an unknown model. If nothing
  worth waiting for recovers in time, the request fails right away, listing each platform's last
  error and when it will be tried again.
- Errors that another platform cannot fix (configuration errors, contract violations, context
  overflow) are raised immediately.
- Every platform failure is logged as a warning on stderr. Without it, a primary platform that keeps
  failing would go unnoticed while the fallback quietly spends its own quota.

The default order is Gemini, then OpenRouter, limited to the ones you configured. Set
`SYNDICA_LLM_ORDER` to choose the platforms and their order. `openai` names the generic `OPENAI_*`
endpoint, which is otherwise used only when neither Gemini nor OpenRouter is configured:

```bash
export SYNDICA_LLM_ORDER=openrouter,gemini
```

A run can move between platforms mid-conversation. Gemini 3 rejects a conversation whose earlier
function calls lack their thought signature, so Syndica returns each signature only to the endpoint
(base URL and model) that produced it. Function calls answered by another platform are sent to
Gemini with the placeholder signature Google documents for that case.

A role whose manifest names a model leaves the rotation and calls one platform. Write the model as
`<platform>:<model>`, where the platform is `gemini`, `openrouter` or `openai` (the generic
`OPENAI_*` endpoint): `gemini:gemini-3.8-flash`, `openrouter:cohere/north-mini-code:free`,
`openai:qwen3:8b`. `<platform>:default` is that platform's default model from the environment. The
name must be ASCII and is split at its first colon. A name with a `/` or `.` before its first
colon, such as `cohere/north-mini-code:free`, is a plain model name and goes to OpenRouter when it
is configured, otherwise to `OPENAI_*`. Any other word before the first colon is an error that
names the fix, so a tag like `qwen3:8b` has to be written `openai:qwen3:8b`. OpenRouter still
accepts only free models, whichever way the name reaches it.

The generic `OPENAI_*` endpoint may be a paid one, and a manifest can come from a project's
configuration. A manifest therefore reaches it only when your own environment chose it: `openai`
is listed in `SYNDICA_LLM_ORDER`, or neither Gemini nor OpenRouter is configured.

### Keys and quotas

Keep API keys in environment variables; `.env` files are ignored by Git and are not loaded
automatically. Do not keep a key file inside a workspace that agents operate on: `Read` and
`Grep` can read it and send it to the model provider.

Free tiers limit requests, and every agent turn is one request. OpenRouter allows 50 free-model
requests per day. Gemini limits requests per minute and per day for each model; your limits are
listed in Google AI Studio, and daily quotas reset at midnight Pacific time. Delegated agents run
their own turns, so keep `--max-turns` small; it is the budget of the whole agent tree, and each
delegated agent gets a share of what its parent has left. The step-by-step local
validation, including quota-aware commands and how to read 401 / 429 / 400 failures, is in
[`docs/guides/2026-09-23-local-multi-agent-validation.md`](docs/guides/2026-09-23-local-multi-agent-validation.md).

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
    system_prompt: {file: docs/agents/reviewer.md}
    model: openrouter:cohere/north-mini-code:free
    tools: [Read, Grep, Glob]
    budget:
      turns: 20

danger:
  roles:
    Reviewer: {rules: {shell.path_outside_workspace: block}}

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

`system_prompt` is either the prompt text or `{file: <path>}`, a file next to the configuration
(no absolute paths, nothing outside its directory); a missing file is an error. A plain string that
names an existing file is read as well, so a typo in it would silently become the prompt; the CLI
warns when a single-word string that looks like a path names no file. `model` takes the
`<platform>:<model>` form described under Retries and several platforms.

A `.syndica.yaml` inside the workspace is untrusted input: an agent can edit it, and it decides
which commands run and which code loads. `run`, `chat`, and `doctor` therefore ask you to confirm
it the first time and after every change, showing the prompt files, `impl:` classes, verify
commands, `pass_env`, models, modules, flags, and safety settings it would use; nothing in it runs
before you answer `y` on a terminal. The confirmation covers the bytes of the configuration and of
every prompt file it reads, and the whole package of an `impl:` class kept inside the workspace,
including directories linked into it (code installed elsewhere is named and its location
recorded, but its contents are not covered). A file or package counts as inside the workspace if
its path is inside either as written or after following symlinks, so a `.syndica.yaml` that is a
symlink to a file elsewhere is still confirmed. Code that was confirmed is checked again when it is
imported: a module the confirmed package imports later in the run is refused if it changed since
you confirmed, or did not exist then. Where no terminal is available, review the configuration and
run `syndica config trust --sha256 <digest>` with the digest from the refusal.
Confirmations live in `$XDG_CONFIG_HOME/syndica/trusted.json` (default `~/.config/syndica/`),
never inside the workspace; an agent's own `syndica` writes to a store in its scratch directory
by default, which guards against accidental nesting rather than isolating the agent. A
configuration outside the workspace, passed with `--config`, needs no confirmation.

The confirmation cannot cover code that runs before it. If syndica itself is installed in a
virtual environment inside the workspace (uv's default `.venv`), an agent can write `.pth` files
and installed code there that run at the next start, before any question is asked; the
confirmation lists such `sys.path` entries so you can see them. Install syndica outside the
workspaces agents operate on to avoid this.

`modules:` enables optional modules in the order written; `syndica modules list` shows which ones
exist and where they come from without importing any of them, and marks those whose code is inside
the workspace. `syndica modules show <name>` imports a module to show its manifest; when its code
is inside the workspace, the configuration must enable it and be confirmed first. Besides the built-in `retry`,
`prefix-drift`, `compaction`, `memory`, `acceptance`, `long-horizon` and `background`, it can name a third-party module installed under the `syndica.modules` entry-point
group. Its entry point is a callable `factory(host: ModuleHost) -> RuntimeModule`; the entry-point
name must equal the module's name, and its flag keys must start with the module name, hyphens
turned into underscores (`example` gives `example_`, `git-notes` gives `git_notes_`), and must not
look like secrets. A third-party module runs in-process with
your full environment, so the confirmation shows it and covers its code when that code lives in
the workspace; a distribution installed inside the workspace is also checked against its
`RECORD`. Its tool results, and any result its hooks write for other tools, are marked untrusted;
`--trust-module-output <name>` on `run` or `chat` lifts that for one module, and a project
configuration cannot. [examples/syndica-example-module](examples/syndica-example-module) is a
minimal third-party module to start from: one flag, one tool that triggers the module's own
extension junction, and one plugin that records what it hears there.

`modules: [acceptance]` turns on artifact acceptance (spec §7.5–§7.7). With it installed the host
keeps an artifact index in `.syndica/artifacts` and the agent gets a `Deliver` tool; a delivered file
that fails its format check (an empty file, for the generic handler) is sent back once at no model
cost. The model-backed layers are off until their flags are set: `acceptance_rubrics` projects the
goal into anchored rubrics before work starts, `acceptance_output` asks a conservative verifier
whether every request was done, `acceptance_outcome` checks each rubric against the final reply and
the workspace. The goal is `--goal <text>` on `run` or `chat`, or else the first message. Each run
ends with one verdict on stderr and in the trace: `pass`, `partial`, `block`, or `unverified` with
the reason (a verifier that fails or times out lets the run finish but never reports `pass`; a
rubric only a human can judge stays `unverified`). With `acceptance_feedback`, a later message from
you becomes a proposed rubric; it counts only after `syndica sessions activate <session> <id>`.
Runs with acceptance installed do not write a replay document, because the verifiers' model calls
are not part of the recorded turns.

`modules: [acceptance, long-horizon]` adds the long-horizon loop (spec §8.2–§8.6) for the agent that
has the goal; it needs `acceptance` because progress is only ever measured by the verifier, never
taken from the agent's own account (with `acceptance_outcome` off, only the drift and low-budget
checks run). Milestone checks and the semantic drift check are skipped when the budget cannot fit
them alongside the agent's own next calls. Every `long_horizon_progress_every` turns (default 10) a
milestone check counts the satisfied mandatory rubrics; a rubric that was satisfied and is now
unmet is reported as a regression. When `long_horizon_stall_turns` turns (default 20) pass without
progress, when the budget left drops below `long_horizon_budget_low_percent`, or when the last
`long_horizon_drift_window` turns of tool calls share no words with the goal and rubrics, a
strategy decides what to tell the agent; the built-in one suggests a different approach, says to
secure the core deliverable, or restates the goal verbatim. `long_horizon_semantic_drift` (off by
default) asks the model to confirm suspected drift first. When a milestone passes with every
mandatory rubric satisfied the agent is asked for its final answer, and after a `partial` or
`block` verdict the strategy may send it back up to `long_horizon_max_continuations` times per
cycle. The events appear under `长程：` in `syndica trace show`.

`modules: [background]` with `background_after_seconds: N` moves a `Bash` call that is still running
after N seconds to the background: the agent gets a placeholder with an id (`bg-1`) and keeps
working, and the real result enters its history in a later turn, after the same post-tool hooks and
trust rules as any other result. `QueryBackgroundTool` and `CancelBackgroundTool` look at or stop a
call. An agent cannot finish while background calls are running: its final answer waits for them
(on the last turn, including a budget wrap-up, up to the wall-clock deadline), and an abort, a kill,
or running out of budget or turns before an answer cancels them and records that their side effects
are unknown. Only calls in the last segment of a batch are moved, calls approved by a human are
never moved, and a call that needs approval is refused while background calls are running. Runs
with background installed do not write a replay document.

Writing a module: the entry point returns a `RuntimeModule`, and the kernel checks the following
when it loads one.

- Name: lowercase letters and digits in hyphen-separated segments (`ledger`, `git-notes`). It is
  also the entry-point name, the owner segment of the module's junctions and the `modules:` entry.
  Flag keys start with the name, hyphens turned into underscores (`git-notes` gives `git_notes_`).
  This makes clashes unlikely, not impossible; two enabled modules declaring the same key are
  refused.
- Tools come only from `tool_factories`, callables `(runtime, agent, flags) -> {name: Tool}`.
  Installing tools on the agent from anywhere else is refused. Each tool class declares
  `__tool_command_args__` itself: the names of the arguments it hands to a shell, interpreter or
  remote command, or `()` when it runs none of them. The danger judge inspects only the declared
  arguments, so `()` on a tool that runs commands turns the check off for that tool, and a
  declaration inherited from a parent class does not count.
- Plugins (`agent_plugins`, callables `(runtime, agent, flags) -> Plugin`) register hooks for one
  agent, on the module layer, in module order. Registering with `agent.register_hook` from a
  factory instead puts the hook on the agent's own layer, ahead of every module, where a `skip` on
  `POST_TOOL_USE` also hides the call from the CLI's development log. `AGENT_SPAWN`,
  `AGENT_DESPAWN` and `RUNTIME_SHUTDOWN` fire only on the runtime's own hook center, so register
  those on `runtime.hooks` from `on_start`.
- `on_start(runtime, flags)` is synchronous and returns `None`; `on_shutdown(runtime)` returns an
  awaitable. A service factory or `on_start` that raises fails the whole runtime construction and
  rolls back what the kernel holds; `on_shutdown` failures are raised together after every module
  has shut down.
- Junctions a module owns are listed in its manifest,
  `junctions=[ExtensionJunction('<name>:<event>', JunctionKind.OBSERVABLE)]` (or `INTERCEPTABLE`),
  and triggered with that same object through `agent.trigger_extension(handle, **kwargs)`. The
  handle is positional only, so the payload may use any key, `handle` and `junction` included. It
  returns the chain's merged `HookResult`; for an interceptable junction the module decides what a
  `skip` or a rewrite means.
- Manifest fields: `version`, `description`, `capabilities` (a subset of `environment`,
  `filesystem`, `network` and `subprocess`; `None` means undeclared and is shown as all of them),
  `requires` (modules that must be installed earlier; the CLI always installs `safety` and
  `human-interaction` first), `replay_safe` (its code does no I/O, reads no clock or randomness and
  starts no threads, so a replay may run it again) and `security_notes`.
- Flags: `FlagDeclaration(key, default, description, choices=(), minimum=None, maximum=None)`, with
  a `bool`, `int` or `str` default.
- To hand text to another agent, use `runtime.send` or `runtime.spawn(query=...)`; text a listed
  module writes there is marked untrusted. A `Message` published straight to `runtime.broker`
  carries whatever trust it was given.
- A factory your module passes to `runtime.spawn` runs as your module, so hooks it or the agent's
  `install_own_tools` registers count as yours. Roles the host registered (`Runtime(agents=...)`,
  `assemble`) run as the host even when your tool spawns them.

`flags:` overrides flag defaults for the run; `--flag key=value` overrides `flags:`. Both go through
the same checks against the installed modules' declarations, and a project configuration may only
tighten: `untrusted_high_risk` accepts only `reject` there. The built-in flags are listed in
[docs/reference/flags.md](docs/reference/flags.md); `syndica flags list` and `syndica flags show
<key>` show the flags of the modules a configuration installs, with the value in effect and where
it comes from. When the configuration enables a third-party module they confirm the configuration
first, because they import the module to read its declarations.

`danger.roles` gives a role a stricter disposition than the development table every run uses:
`rules` raises single rules to `confirm` or `block`, and `policy: block_high_risk` blocks every
high-risk rule. A project configuration can only tighten: `allow`, `allow_all`, and anything looser
than the development table are rejected. Replay rebuilds the same table for each role.

Important current limits:

- `--max-turns`, `--max-tokens`, `--max-cost` and `--max-wall-clock` budget the whole agent tree
  (default 200 turns). A delegated or forked agent's budget is carved from what its parent has
  left — half of it unless `Delegate` / `Fork` ask for `turns` / `tokens` explicitly — and a finished
  child gives its unused share back. A role's own `budget.turns` still caps that agent. Agents see
  their budget in their history and through the `GetBudget` tool. When a budget runs out the agent
  gets one wrap-up turn to deliver what it has (`--on-exhausted graceful_wrapup`, the default);
  `hard_stop` uses every turn and stops, `ask_human` asks you for more turns first.
- `--max-cost` needs prices: the backend knows only free models (`*:free`, `openrouter/free`);
  give other models three per-million-token prices under `prices:` in `.syndica.yaml`. A run with
  a cost cap and a model without a price is refused before it starts. Cache writes are not reported
  by OpenAI-compatible endpoints, so cost can be slightly under-counted.
- Runs with `--max-cost`, `--max-wall-clock` or `--on-exhausted ask_human` record a trace but no
  replay document: those decisions depend on the host's prices, real time, or a person.
- Failures of delegated agents are printed to stderr as `Runtime 错误` entries even when the
  root agent still answers. A run whose agent leaves no text answer (for example it ran out of
  turns while still calling tools) fails with exit code 1 instead of printing nothing.
- In `chat`, the budget accumulates across messages; once an agent has nothing left, messages to it
  are refused and you need a new `chat`.
- `chat` sessions are process-local.
- Cross-process `--session` recovery and `syndica sessions ...` require the SessionStore planned
  for Milestone 6.

## Development Mode

Milestone 2 lets a team of agents change a repository, including this one, while the host process
(the CLI itself, not a model) checks the result and a human decides what reaches Git history:

```bash
syndica run --handoff --topology syndica-dev "add the missing test for ..."
syndica run --no-write --handoff --agent Planner "plan how to ..."
syndica doctor --verify
```

This repository's own roles, prompts, and topology live in [`.syndica.yaml`](.syndica.yaml) and
[`docs/agents/`](docs/agents/). A project enables the host checks with a `workspace` section:

```yaml
workspace:
  verify:                      # run by the host after the agents finish, and by doctor --verify
    - uv run ruff format --check .
    - uv run pytest -q
  protected: [pyproject.toml, "scripts/**"]   # changes here are never committed unattended
  pass_env: [NO_COLOR]         # extra variables agents may see, beyond the fixed allowlist
```

In `run --handoff` the host redacts what it writes and prints: both output streams, the handoff
file, and the commit message; `doctor` output is redacted too. Plain `run` and `chat` print what the
agents print. Values of environment variables whose names look secret, private keys,
`Authorization` credentials, passwords in URLs, well-known token formats, and credential-like values
assigned to secret-looking names (`STRIPE_KEY=…`) become `[REDACTED:<rule>]`. A project can add
patterns of its own; their ids start with `custom.`, and a pattern that matches the empty string or
a placeholder is refused:

```yaml
redaction:
  rules:
    - {id: custom.internal_host, pattern: 'corp-[a-z0-9]+\.internal'}
```

Custom patterns only redact. The check that refuses to commit a secret looks for long environment
values, well-known token formats and private keys, never for custom patterns: one misfiring
pattern would block every commit. Patterns run inside the host process like the rest of
`.syndica.yaml`, so one with catastrophic backtracking hangs it.

**What every CLI run enforces.**

- Agents' commands see only `PATH HOME USER LOGNAME LANG LC_ALL LC_CTYPE TMPDIR XDG_CACHE_HOME
  UV_CACHE_DIR`, `workspace.pass_env`, and six Git and `gh` settings the host adds. A name that
  looks like a secret is refused even in `pass_env`: one of its `_`-separated words is `KEY`,
  `TOKEN`, `SECRET`, `PASSWORD`, `AUTH`, `SESSION` or a similar word, or ends in `KEY`, `TOKEN`,
  `SECRET`, `PASSWORD` or `PASSWD` (`SSH_AUTH_SOCK` counts, so agents get no ssh agent; so does
  `MONKEY_MODE`). Git and `gh` get a per-run configuration, so agents cannot use your credential
  helpers, aliases, hooks, or `gh` login.
- Git commands that change state (`commit`, `push`, `checkout`, `-c ...`, anything under `.git/`) and
  `rm` of the workspace root are blocked unconditionally, also behind the wrappers the rules know
  (`env`, `nice`, `nohup`, `setsid`, `timeout`, `sudo`, `command`, `exec`, `xargs` and others) and
  launchers such as `uv run`. Commands handed to a shell's `-c`, to `eval`, or to `find -exec` are
  read as commands too, up to eight levels deep (deeper counts as blocked), and so is a `find` that
  deletes the whole workspace (`find . -delete`). The rules read command text, so a command inside a
  script, a variable, `python -c`, or a program they do not know (`su -c`, `watch`, `ssh`, `make`)
  is not recognised. File tools refuse to write inside `.git/`.
- File tools (Read, Write, Edit, and Glob or Grep pointed at them) cannot reach `.syndica/traces`
  or `.syndica/handoffs`, in any letter case or through a symlink. Traces hold tool output that was
  marked untrusted, and reading it back with a file tool would make it trusted; handoff files are
  the host's to write. Commands still reach both; the real boundary is a sandboxed workspace
  backend (the kernel defines the `Workspace` / `Portal` protocol and its contract suite; the
  sandbox is a backend). The rest of `.syndica/`, such as `.syndica/tmp/`, stays usable.
- A command that touches a path outside the workspace asks you while the agent has not yet read any
  command output. After that, the trajectory counts as untrusted and such commands are rejected;
  `--flag untrusted_high_risk=ask` asks instead. `untrusted_high_risk=allow` is refused.

**What `run --handoff` adds.** After the agents finish, the host runs every `workspace.verify`
command itself; what an agent says it ran is listed but never trusted. It accepts a Reviewer's
`verdict:` line only from a Reviewer run that completed, changed nothing, and saw exactly the tree
that was verified; among those, the one that finished last counts. The handoff (goal, changes, checks the agents ran, host verification,
the agents' conclusion, next steps, Git outcome) is redacted, written to
`.syndica/handoffs/<time>-<id>.md` with mode 0600, and printed to stdout. Only one `--handoff` run
can hold a workspace at a time.

When verification passed and the Reviewer said `pass`, the host shows the branch, upstream, commit
title, and changed paths with the lines each one adds and removes, then asks:

```text
1  commit and push      2  commit only      3  I'll handle it
```

End of input, an empty line, or anything else means `3`, so a run with `</dev/null` never commits.
`--git-mode commit|push|manual` answers in advance for unattended runs; `commit` and `push` exit 3
without touching Git when their preconditions do not hold (a failed run or failed verification
still exits 1). The host commits only the paths this run changed, scans the
added lines for secrets first, runs your commit hooks with the agents' environment rather than yours,
and checks the resulting commit before pushing that commit to the recorded upstream with local
hooks off and never with `--force`. Push is not offered when there is no upstream, when a
`pre-push` or `reference-transaction` hook exists (push it yourself so your hook runs), when the
workspace had changes before the run, or when local commits were already ahead of the upstream.

`run --no-write` keeps only `Read Grep Glob AskUser AskUserQuestion` (orchestration tools such as
`Delegate` stay) and never verifies or commits. Exit codes: `0` success; `1` the run failed, verification failed, the workspace is busy,
or a commit or push that had started failed; `2` usage error; `3` `--git-mode` preconditions not
met; `130` interrupted with Ctrl-C, which takes precedence over `1` and `3`. Ctrl-C during the run
or during verification still writes the handoff, as a failed run with verification skipped, and
any Ctrl-C before the Git step skips that step. Ctrl-C during the Git step stops Git at once and
leaves the file at `git: pending`, because the host cannot tell whether a push reached the remote;
the printed copy says whether a commit was made, and if none was, the new files staged for it are
unstaged. Further presses do not cut that cleanup short. Once the Git step is over or skipped,
Ctrl-C changes nothing. Before that, a third press outside the Git step forces an exit; the
handoff may then be missing or still at `git: pending`.

**Trust and threat model.** `.syndica.yaml` is code you run: `run --handoff` and `doctor --verify`
execute `workspace.verify` on your machine without danger checks, so using them in a repository you
do not trust is the same as running its test scripts. Changes to protected paths (always
`.gitignore` and `.gitattributes`, plus `workspace.protected`) are highlighted and block
`--git-mode` commits, because they decide what "checks passed" means.

These guards stop the common forms of an agent's mistakes (deleting the repository, pushing,
rewriting Git state), naive prompt injection ("read `~/.config/...`", "push this"), and secrets
leaking through the agents' environment or the handoff. They do not stop a command written in a form
the rules cannot read (see above), whether by accident or on purpose, nor an agent that has been
persuaded to evade them: code it writes or runs (`python -c`, a script, `ssh`, and the tests the host
verifies) can read any file your user can
read, reach the network, read the host process's environment and your login keychain, and use
credential files under `HOME` (`~/.netrc`, `~/.npmrc`). Processes that call `setsid` escape process
group cleanup. The real boundary is a sandboxed workspace backend; with the local workspace, use
development mode only on trusted input such as your own repository and prompts.

## Observability

Every `run` and every `chat` session is recorded as a trace in `.syndica/traces/<UTC time>-<id>/`
(directories 0700, files 0600). The directory path is printed on stderr when the run ends, and
`--handoff` files name it too. A trace that lost records says so (`trace（不完整…）`) and is neither
replayed nor exported.

| Level | Spans and edges | Replay document | Full requests |
| --- | --- | --- | --- |
| `off` | — | — | — |
| `structure` | yes | — | — |
| `replay` (default) | yes | yes | per-unit fingerprints only |
| `full` | yes | yes | yes, redacted leaf by leaf |

Set the level per run with `--trace` or for the project with `trace: {level: ...}` in
`.syndica.yaml`; `chat` records at most `structure`. A `--topology` run records the topology as
actually assembled (after `--no-write` rewrote it) and the run's root budget; replay assembles it through
the same slot logic and enters through the root slot, so slot tools, budgets and stable ids match
the original run. A run with a third-party module is replayable only if that module declares
`replay_safe` or runs no code of its own (flags only). Replay rebuilds such a module only when the
configuration enables it (the workspace's `.syndica.yaml`, or the file given with `--config`, which
must be trusted when it lies inside the workspace, as for `run`) and the installed distribution has
the recorded name and version; it checks all of this before importing anything. When it cannot rebuild one,
`trace replay` exits 2 and names the module and the reason: replaying without code the original
run ran would not be the same run.

- `syndica trace list` lists traces newest first (UTC time, id, status, agents, spans, size,
  command; a replay's own trace says which trace it replayed).
  An id may be the full id, a unique prefix, or the directory name printed after a run.
- `syndica trace show <id>` prints who created whom, who waits on whom (a wait cycle is marked),
  the three slowest tool calls (calls that only waited for another agent are left out), model calls
  per agent (time, tokens, failures, platform failovers), a timeline, and which tools and plugins
  each module gave each agent, with how often each extension junction fired. Traces are written
  as `syndica.trace/2`; traces written as `/1` are still read.
- `syndica trace replay <id>` replays the trace offline: model replies, tool results, human answers
  and host actions come from the record, while the kernel, its danger judgement and the modules the
  replay can rebuild (safety, human interaction, retry, prefix drift, and third-party modules as
  described above) run for real and are compared item by item.
  Host modules it cannot rebuild, such as the development guard, are named in the output; their
  effects (the calls they skipped) are already in the record. Every agent sees its messages when the
  record says it did (injected into the same turn, a deferred message put aside in the same turn; a
  cycle stops or carries straight on as it did), a kill lands where it landed, and a call the
  original never got back, or got back without its result landing, stays pending until the same
  interruption cuts it, so agents that ran concurrently replay the same way. Messages the host
  sends (the entry, a person's answers) are not in the record and arrive when they are sent. Exit 0
  means it reproduced, 1 that it diverged (each divergence names the agent, turn and unit), 2 that
  the trace cannot be replayed. The replay records itself as a new trace that points back to the
  original.
- `syndica trace export <id> [--otlp <url>]` sends the trace to an OTLP/HTTP collector. Exports
  also happen at the end of every run when your environment names an endpoint
  (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT`), honouring
  `OTEL_SDK_DISABLED`, `OTEL_TRACES_EXPORTER`, `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES`.
  `OTEL_EXPORTER_OTLP_HEADERS` go only with the environment's endpoint, never over plaintext HTTP to
  another machine. A project's `.syndica.yaml` cannot choose the endpoint: a repository you do not
  trust would otherwise receive your traces and your collector credentials. Install the extra with
  `pip install 'syndica[otel]'`.
- `syndica trace prune [--keep <n>]` keeps the newest `n` finished traces (default 50) together
  with their replays, and never deletes an unfinished one; `syndica trace prune <id>...` deletes
  exactly the named traces. A trace written by a newer syndica is listed as unreadable with what to
  do (upgrade), never as tampered.

Everything written into a trace, a handoff file, a commit message or an export goes through the
kernel `Redactor` first: secret-looking values from the host environment, built-in credential
formats (private keys, `Authorization` headers, URL user info, common token formats, secret-named
assignments), and your own `redaction.rules`. Each match becomes `[REDACTED:<rule id>]`. The
agent's file tools cannot read or write `.syndica/traces` or `.syndica/handoffs`; its shell can,
unless the workspace backend sandboxes it.

## Web UI

The server is an optional extra: `uv sync --extra server` in a checkout, or
`pip install 'syndica[server]'`.

```bash
syndica up          # starts the server for this workspace and prints a one-time login link
syndica status      # where it runs; says so if it holds its lock but does not answer
syndica down        # stops it; sessions are saved first
```

The login link is valid for two minutes and works once. The page keeps its token in the browser
and sends it in a header, and the server never sets a cookie. Restarting the server invalidates
the token, so run `syndica down` and `syndica up` again for a new link.

What the server does and does not protect:

- It listens on `127.0.0.1` only, checks the `Host` and `Origin` headers, and sends a strict CSP.
  Artifact bytes are served as sandboxed attachments, never as pages on the server's origin. To use
  it from another machine, forward the port over SSH. There is no remote mode.
- `up`, `down` and `python -m syndica.serve` refuse to run inside an agent's command environment.
  This stops mistakes, not a determined agent running as the same OS user: the browser stores the
  token in its profile directory, which that user can read. The real boundary is a sandboxed
  workspace backend.
- The human terminal runs in the agent's environment (no host keys, temporary git config). It is
  not a sandbox.
- When no authenticated page has been connected for 30 seconds, pending confirmations are
  rejected and pending questions are aborted, so an unattended agent can move on instead of
  waiting.
- In-flight sub-agents and their mailboxes are not persisted. After `down` or a crash, resuming
  restores the root conversation and says which sub-agents were lost.

## CLI Reference

```text
syndica up [--port <n>] [--foreground] [--no-open] | status | down
syndica run <prompt>
syndica chat [--max-turns <n>] [--on-exhausted <mode>] [--trace <level>] [--trust-module-output <name>] [--goal <text>]
syndica agents list | show <name>
syndica topology list | show <name>
syndica flags list | show <key>
syndica modules list | show <name>
syndica artifacts list | show <id>
syndica config trust [--sha256 <digest>]
syndica sessions list | show <id> | resume <id> | rm <id>
syndica sessions rubrics <id> | activate <id> <rubric-id>
syndica memory list | forget <n> | forget --pending   [--global]
syndica doctor [--verify]
syndica trace list | show <id> | replay <id> [--allow-divergence]
syndica trace export <id> [--otlp <url>] | prune [<id>...] [--keep <n>]
syndica batch <tasks.jsonl>
syndica export sft | sharegpt | preference | rl | trajectory --output <dir> [--include-unverified]
syndica version
```

`run` accepts:

```text
--agent <name>
--topology <name>
--session <id>
--max-turns <n>           turns for the whole agent tree (default 200)
--max-cost <amount>       cost cap for the whole tree; prices come from `prices:` and the backend
--max-tokens <n>          input + output tokens for the whole tree
--max-wall-clock <seconds>
--on-exhausted <mode>     graceful_wrapup (default) | hard_stop | ask_human
--flag <key=value>        repeatable
--goal <text>             what this run must deliver; needs the acceptance module
--trust-module-output <name>  repeatable; this run treats that third-party module's output as trusted
--no-write                read-only tools only
--verdict-exit            exit 10 / 11 / 12 / 13 for pass / partial / block / unverified; needs acceptance
--handoff                 host verification, a redacted handoff file, and the Git question
--git-mode <mode>         commit | push | manual, answered in advance; requires --handoff
--trace <level>           off | structure | replay | full (default: trace.level, then replay)
```

`--agent`, `--topology`, and `--session` are mutually exclusive, and `--no-write` excludes
`--session` and `--git-mode`. `--flag` keys are validated against installed Runtime Module
declarations. `doctor` checks the configuration, models, workspace, the agents' environment, Git,
Python, and every executable the verify commands use; `--verify` then runs each verify command.

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
uv run python scripts/check_links.py
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

## Interop

MCP, ACP, A2A, and OpenAI-compatible HTTP all land in a Syndica session. They do not bypass trace,
provenance, redaction, or danger decisions. Content that arrives through a protocol is untrusted.
There is no switch that marks an external tool or agent as trusted.

`syndica mcp` and `syndica acp` speak those protocols on stdio. `syndica up` also serves `/mcp`,
`/.well-known/agent-card.json`, `POST /a2a/`, `GET /v1/models`, `POST /v1/chat/completions`, and
`POST /v1/responses`. A machine bearer is printed once by `syndica up --api-token` and lives only
in that process. Continue an OpenAI session with the `X-Syndica-Session` response header;
Responses ids look like `resp_<session>_<n>` and are passed back as `previous_response_id`.

A role can call out by setting `model: acp:<name>` or `model: a2a:<name>`. Project `.syndica.yaml`
may only name those external objects. Secret-like environment names belong in the user-level
`interop.yaml` next to the trust store (mode `0600`, variable names only, outside the workspace).
Version negotiation fails closed: MCP accepts only the five versions its SDK lists, ACP accepts
protocol version 1, and A2A accepts only `1.0` and `0.3`. A missing `A2A-Version` header is
rejected rather than treated as `0.3`.

## Training Export

`syndica batch tasks.jsonl` runs one task per line (`{"id": ..., "prompt": ...}`, optionally
`"agent"` and `"goal"`) and prints one JSON result per line; a failed task does not stop the rest.
`syndica export <format> --output <dir>` turns the workspace's sessions, goals, rubrics, traces,
and artifact records into a dataset plus a `manifest.json`:

- `sft` writes OpenAI-shaped `messages` with tool calls and the artifacts each sample produced;
  `sharegpt` writes the same conversations as `human` / `gpt` / `function_call` / `observation`.
- `preference` writes DPO-shaped `prompt` / `chosen` / `rejected` for a better and a worse verdict
  of the same goal and the same rubrics.
- `rl` writes `input_ids`, a `loss_mask` that covers the assistant's text and tool calls, and a
  reward from the verdict (pass 1.0, partial 0.5, block 0.0, unverified `null`).
- `trajectory` writes the neutral `syndica.trajectory/1` records: messages with their trust
  markers, tool calls, artifacts, every verdict the run received, causality, timing, and redaction
  metadata.

Samples whose verdict is `unverified` are left out unless you pass `--include-unverified`, and the
manifest records that choice. Export redacts with the same rules as everything else the host writes
(your `redaction.rules` plus secret values in the current environment), string by string before
encoding; placeholders stay and raw secrets never leave. The output directory must be outside the
workspace and new or empty, files are created `0600`, and the workspace's stores are only read.
`pip install 'syndica[trl]'` adds `syndica.backends.trl.dry_run`, which loads an exported SFT file
into TRL's `SFTTrainer` without downloading a model.

## Roadmap

The v1.0 roadmap has 12 internal milestones:

| # | Milestone | Status |
|---|---|---|
| 1 | Kernel skeleton | Done |
| 2 | Self-bootstrapping | Done |
| 3 | Observability + redaction | Done |
| 4 | Complete extension system | Done |
| 5 | Complete execution layer + provenance | Done |
| 6 | State: sessions, compression, memory | Done |
| 7 | Artifacts and acceptance | Done |
| 8 | Long-horizon task control | Done |
| 9 | HTTP/SSE/Web product surface | Done |
| 10 | MCP / ACP / A2A / OpenAI HTTP interop | In progress |
| 11 | Training-data export | In progress |
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
