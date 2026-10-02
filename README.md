# Syndica

Syndica is a multi-agent harness for **verifiable knowledge work**.

It is designed for jobs where a single chat loop is too small: long-running work, several
specialized agents, local files and commands, human approvals, auditable traces, artifacts,
acceptance criteria, and eventually training-data export.

> Status: **pre-release**.
>
> Milestones 1 and 2 are complete on `main`. The project is still pre-v1.0, so public APIs may
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

A model name written explicitly in a role manifest belongs to a single platform and is not rotated:
it goes to OpenRouter when OpenRouter is configured, and to the generic `OPENAI_*` endpoint
otherwise.

### Keys and quotas

Keep API keys in environment variables; `.env` files are ignored by Git and are not loaded
automatically. Do not keep a key file inside a workspace that agents operate on: `Read` and
`Grep` can read it and send it to the model provider.

Free tiers limit requests, and every agent turn is one request. OpenRouter allows 50 free-model
requests per day. Gemini limits requests per minute and per day for each model; your limits are
listed in Google AI Studio, and daily quotas reset at midnight Pacific time. Delegated agents run
their own turns, so keep `--max-turns` small; it caps every agent in the run. The step-by-step local
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

- Milestone 1 executes `--max-turns`. `--max-turns N` caps every agent in the run — the root,
  delegated and forked agents, and every topology slot — at N turns each, never loosening a
  smaller role budget. Parent-to-child budget splitting arrives with the budget tree in
  Milestone 8.
- `--max-cost`, `--max-tokens`, and `--max-wall-clock` are parsed but fail explicitly until the
  budget engine lands in Milestone 8.
- Failures of delegated agents are printed to stderr as `Runtime 错误` entries even when the
  root agent still answers. A run whose agent leaves no text answer (for example it ran out of
  turns while still calling tools) fails with exit code 1 instead of printing nothing.
- In `chat`, an agent's turn count accumulates across messages; once it is used up, start a new
  `chat`.
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
  the host's to write. Commands still reach both; the real boundary is the sandbox planned for
  Milestone 5. The rest of `.syndica/`, such as `.syndica/tmp/`, stays usable.
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
group cleanup. The real boundary is the sandbox planned for Milestone 5; until then, use development
mode only on trusted input such as your own repository and prompts.

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
`.syndica.yaml`; `chat` records at most `structure`. `--topology` runs write no replay document:
the replay cannot rebuild what topology slots attach to their roles.

- `syndica trace list` lists traces newest first (UTC time, id, status, agents, spans, size,
  command; a replay's own trace says which trace it replayed).
  An id may be the full id, a unique prefix, or the directory name printed after a run.
- `syndica trace show <id>` prints who created whom, who waits on whom (a wait cycle is marked),
  the three slowest tool calls, and a timeline.
- `syndica trace replay <id>` replays the trace offline: model replies, tool results, human answers
  and host actions come from the record, while the kernel, its danger judgement and every hook run
  for real and are compared item by item. Exit 0 means it reproduced, 1 that it diverged (each
  divergence names the agent, turn and unit), 2 that the trace cannot be replayed. The replay
  records itself as a new trace that points back to the original.
- `syndica trace export <id> [--otlp <url>]` sends the trace to an OTLP/HTTP collector. Exports
  also happen at the end of every run when your environment names an endpoint
  (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT`), honouring
  `OTEL_SDK_DISABLED`, `OTEL_TRACES_EXPORTER`, `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES`.
  `OTEL_EXPORTER_OTLP_HEADERS` go only with the environment's endpoint, never over plaintext HTTP to
  another machine. A project's `.syndica.yaml` cannot choose the endpoint: a repository you do not
  trust would otherwise receive your traces and your collector credentials. Install the extra with
  `pip install 'syndica[otel]'`.
- `syndica trace prune [--keep <n>]` keeps the newest `n` finished traces (default 50) and never
  deletes an unfinished one; `syndica trace prune <id>...` deletes exactly the named traces.

Everything written into a trace, a handoff file, a commit message or an export goes through the
kernel `Redactor` first: secret-looking values from the host environment, built-in credential
formats (private keys, `Authorization` headers, URL user info, common token formats, secret-named
assignments), and your own `redaction.rules`. Each match becomes `[REDACTED:<rule id>]`. The
agent's file tools cannot read or write `.syndica/traces` or `.syndica/handoffs`; its shell can,
until the sandbox of milestone 5.

## CLI Reference

```text
syndica run <prompt>
syndica chat [--max-turns <n>]
syndica agents list | show <name>
syndica topology list | show <name>
syndica flags list | show <key>
syndica sessions list | show <id> | resume <id> | rm <id>   # reserved for M6
syndica doctor [--verify]
syndica trace list | show <id> | replay <id> [--allow-divergence]
syndica trace export <id> [--otlp <url>] | prune [<id>...] [--keep <n>]
syndica version
```

`run` accepts:

```text
--agent <name>
--topology <name>
--session <id>
--max-turns <n>           caps every agent in the run
--max-cost <amount>
--max-tokens <n>
--max-wall-clock <seconds>
--flag <key=value>        repeatable
--no-write                read-only tools only
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

## Roadmap

The v1.0 roadmap has 12 internal milestones:

| # | Milestone | Status |
|---|---|---|
| 1 | Kernel skeleton | Done |
| 2 | Self-bootstrapping | Done |
| 3 | Observability + redaction | Done |
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
