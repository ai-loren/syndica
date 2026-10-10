# Demos

Four small demos under
[`examples/`](https://github.com/ai-loren/syndica/tree/main/examples) show the four claims at work:
a team of agents changing a repository under host verification, multi-agent work with a person in
the loop in the web UI, acceptance with a four-value verdict, and turning runs into training data.

Each demo is a directory with a `README.md`, a `.syndica.yaml`, its inputs and a `run.sh`. Run them
from a clone of the repository with `syndica` installed (see [Install](install.md)):

- **With your own model**: export the variables from [Model providers](guides/models.md), for
  example the OpenAI-compatible ones, then run the script.
- **Offline**: `run.sh --offline` starts the scripted fake model that the test suite uses
  (`tests/e2e/fake_openai.py`, with the demo's `offline/script.json`). No key, no cost, and the same
  output every time; the test suite checks every demo against it.

```bash
export OPENAI_BASE_URL=https://api.example.com/v1
export OPENAI_MODEL=your-model-name
export OPENAI_API_KEY=your-key
```

Every run works in a new temporary copy of the demo's `workspace/` and sets `XDG_CONFIG_HOME` inside
it, so neither the demo directory nor your own `~/.config` is changed. Because the scripts run
without a terminal, each one trusts its own `.syndica.yaml` with `syndica config trust --sha256`;
read that file first, as you would any project configuration (see
[Configuration](guides/configuration.md#confirming-a-project-configuration)).

## CLI self-hosting: fix a failing test

[`examples/cli-self-hosting`](https://github.com/ai-loren/syndica/tree/main/examples/cli-self-hosting)
is a tiny Python project whose test fails. A `Worker` runs the tests, reads the code, fixes it and
runs the tests again; then the host runs the project's `workspace.verify` command itself, writes a
redacted handoff and leaves the commit to you.

```bash
sh examples/cli-self-hosting/run.sh --offline
```

The heart of it is one command in [development mode](guides/development-mode.md):

```bash
syndica run --handoff --agent Worker --max-turns 30 "Fix the failing test in this project. Run the tests with python3 -m unittest -q."
syndica trace list
syndica trace show <trace-id>
```

The handoff, printed at the end and saved under `.syndica/handoffs/`, starts like this:

<!-- not-tested: sample output; the trace id and file names differ on every run -->
```text
---
schema: syndica.handoff/2
status: unverified
host_verify: passed
reviewer_verdict: unverified
acceptance_verdict: null
git: not_offered
protected_changed: []
exit_code: 0
---
```

`host_verify: passed` is the host's own verification. `git: not_offered` because the host offers a
commit only when verification passed and a `Reviewer` agent said `pass`; this demo has no
Reviewer, so the fix stays in the working tree for you. `status` stays `unverified` for the same
reason.

## Web UI: an Orchestrator, a Worker and a person

[`examples/web-multi-agent`](https://github.com/ai-loren/syndica/tree/main/examples/web-multi-agent)
starts `syndica up --foreground` on a copy of a workspace with an application log. In the page,
start an `Orchestrator` session and send the message from the demo's `prompt.txt`:

<!-- not-tested: the message to type into the web UI -->
```text
Have the Worker write an incident summary of logs/app.log to incident.md, then copy it to ../outbox/incident.md for the on-call channel.
```

```bash
sh examples/web-multi-agent/run.sh --offline
```

The Orchestrator delegates to the Worker. The Worker reads the log, writes `incident.md`, and asks
to run `cp incident.md ../outbox/incident.md`: a path outside the workspace, so the rule
`shell.path_outside_workspace` puts a card in the page and nothing runs until you approve it, for
that one call (see [Danger judgement](concepts/danger.md)). Afterwards the Agent tab shows both
agents, and the artifacts tab previews `incident.md`. The Worker's answer reaches the Orchestrator
marked untrusted, because the Worker read command output.

The release acceptance runs exactly this flow in a real browser, from a freshly installed wheel.

## Artifact verification: rubrics, a deliverable, a verdict

[`examples/artifact-verification`](https://github.com/ai-loren/syndica/tree/main/examples/artifact-verification)
has a `Worker` write a release note, mark it with `Deliver`, and run into
[acceptance](concepts/artifacts-and-acceptance.md):

```bash
sh examples/artifact-verification/run.sh --offline
```

```bash
syndica run --agent Worker --max-turns 30 --verdict-exit --goal "a release note in notes.md with a Highlights section that lists the breaking change" "Write the release note for version 2.0 to notes.md from changes.txt, then deliver it."
syndica artifacts list
syndica sessions rubrics <session-id>
```

- The goal is projected into rubrics before any work starts, each quoting the words of the goal it
  comes from: `R1 notes.md has a Highlights section`, `R2 notes.md lists the breaking change`.
- `syndica artifacts list` shows `notes.md` as a deliverable (交付物) with its version.
- The verdict is printed on stderr and, with `--verdict-exit`, becomes the exit code (10 `pass`,
  11 `partial`, 12 `block`, 13 `unverified`).

The script then runs the same task with `--flag acceptance_outcome=false`. Nobody checked the
result, so the verdict is `unverified` (验收：unverified；没能验证（outcome_check_off）), never
`pass`:

<!-- not-tested: sample output of run.sh -->
```text
== verdicts: with every layer: pass; with L3 off: unverified
```

## Training export: from a batch to training files

[`examples/training-export`](https://github.com/ai-loren/syndica/tree/main/examples/training-export)
runs a dataset of four tasks and exports the sessions (see [Training export](guides/training-export.md)):

```bash
sh examples/training-export/run.sh --offline
```

```bash
syndica batch tasks.jsonl
syndica export sft --output ../exports/sft
syndica export preference --output ../exports/preference
syndica export rl --output ../exports/rl
syndica export trajectory --output ../exports/trajectory
```

Offline, the tasks are scripted to end in `pass`, `block`, `partial` and `unverified` (a haiku
that "feels calm" can only be judged by a person). The `pass` and the `block` release note share a
goal and rubrics, so they become one preference pair; RL rewards are 1.0, 0.0 and 0.5; and the
`unverified` session is left out of every file and named in each `manifest.json`:

<!-- not-tested: sample output of run.sh -->
```text
sft         3 rows; samples ['block', 'partial', 'pass']; left out as unverified: 1
preference  1 rows; samples ['block', 'partial', 'pass']; left out as unverified: 1
rl          3 rows; samples ['block', 'partial', 'pass']; left out as unverified: 1
trajectory  3 rows; samples ['block', 'partial', 'pass']; left out as unverified: 1
```

With the `trl` extra installed, the script also loads the SFT and preference files into TRL's
`SFTTrainer` and `DPOTrainer` (a dry run: no training, no download).

## With a real model

The demos are written for real models too, but a model is not a script:

- A capable model may pass every training task, and then there is no preference pair.
- The first verdict of the artifact demo can be `partial` or `block`; the defaults send the agent
  back before recording them.
- In the web demo the confirmation card appears only while the Worker has not read any command
  output; afterwards a command touching a path outside the workspace is refused instead of asked
  (see [Trust and provenance](concepts/trust-and-provenance.md)).
