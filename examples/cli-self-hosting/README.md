# Demo: the CLI fixes a failing test (development mode)

A copy of a tiny Python project has a failing test. A `Worker` agent runs the tests, reads the code,
fixes it and runs the tests again. Then the **host** (the CLI process, not a model) runs the
project's own verification command itself, writes a redacted handoff, and leaves the commit to you.

What it shows:

- `syndica run --handoff`: host verification from `workspace.verify`, a handoff file in
  `.syndica/handoffs/`, and a Git step that never commits on its own;
- the trace of the run (`syndica trace list`, `syndica trace show`);
- that all of this happens in a copy: the files under `workspace/` are never changed.

## Run it

You need `syndica` with the `openai` extra, `python3` and `git` on `PATH`.

With your own model (any OpenAI-compatible endpoint, or Gemini / OpenRouter; see
[Model providers](../../docs/user/guides/models.md)):

```bash
export OPENAI_BASE_URL=https://api.example.com/v1
export OPENAI_MODEL=your-model-name
export OPENAI_API_KEY=your-key
sh examples/cli-self-hosting/run.sh
```

Offline, against the scripted fake model the tests use (no key, no cost; needs a clone of the
repository):

```bash
sh examples/cli-self-hosting/run.sh --offline
```

The script copies `workspace/` into a new temporary directory, makes it a Git repository with one
commit, trusts its `.syndica.yaml`, and runs:

```bash
syndica run --handoff --agent Worker --max-turns 30 \
  'Fix the failing test in this project. Run the tests with python3 -m unittest -q.'
```

## What to look at

- **The handoff** is printed at the end of the run and saved under `.syndica/handoffs/`. Its header
  says `host_verify: passed` once the host's own `python3 -m unittest -q` passes; what the agent
  claims it ran is listed, never trusted. The section 改动 lists the changed files (`wordcount.py`).
- **Git** is `not_offered`: the host offers to commit only when verification passed **and** a
  `Reviewer` agent said `pass`, and this demo has no Reviewer. The change stays in the working
  tree for you to review. See [development mode](../../docs/user/guides/development-mode.md) for
  the Reviewer and `--git-mode`.
- **The trace** (`syndica trace show <id>`) shows the Worker's model calls, its tool calls
  (`Bash`, `Read`, `Edit`) with timings, and which modules gave it which tools.
- The handoff's "checks the agents ran" section (Agent 跑过的检查) lists only commands of the check
  tools Syndica parses (pytest, ruff, pyright and the repository checks); this project uses
  `unittest`, so the section says 没有 ("none"). The host verification is what counts.

Runtime output is in Chinese in v1.0: 交接 is "handoff", 宿主复核 "host verification",
通过 "passed".

## Files

| Path | What it is |
| --- | --- |
| `workspace/wordcount.py`, `workspace/test_wordcount.py` | The tiny project and its failing test |
| `workspace/.syndica.yaml` | `workspace.verify`: the command the host runs itself |
| `offline/script.json` | What the fake model answers in `--offline` mode |
| `run.sh` | The demo |
