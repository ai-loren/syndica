# Changing a repository (development mode)

Development mode lets a team of agents change a repository, including Syndica's own, while the
host process (the CLI itself, not a model) checks the result and a person decides what reaches Git
history.

```bash
syndica run --handoff --topology syndica-dev "add the missing test for the parser"
syndica run --no-write --handoff --agent Planner "plan how to split the config loader"
syndica doctor --verify
```

Syndica's own roles, prompts and topology are in its
[`.syndica.yaml`](https://github.com/ai-loren/syndica/blob/main/.syndica.yaml) and
[`docs/agents/`](https://github.com/ai-loren/syndica/tree/main/docs/agents) (prompts in Chinese). A
project turns on the host checks with a `workspace` section:

```yaml
version: 1
workspace:
  verify:
    - uv run ruff format --check .
    - uv run pytest -q
  protected: [pyproject.toml, "scripts/**"]
  pass_env: [NO_COLOR]
```

`verify` is run by the host after the agents finish and by `doctor --verify`; changes to
`protected` paths are never committed unattended; `pass_env` lists extra variables agents may see.

## What every CLI run enforces

These hold for every `run` and `chat`, with or without `--handoff`:

- Agents' commands see only an allowlisted environment, and Git and `gh` get a per-run
  configuration, so agents cannot use your credential helpers, aliases, hooks or `gh` login (see
  [the agents' environment](../concepts/danger.md#the-agents-environment)).
- Git commands that change state (`commit`, `push`, `checkout`, `-c ...`, anything under `.git/`) and
  `rm` of the workspace root are blocked unconditionally, also behind the wrappers and launchers the
  rules know, inside `sh -c`, `eval` and `find -exec`, up to eight levels deep. A `find` that deletes
  the whole workspace (`find . -delete`) counts too. File tools refuse to write inside `.git/`.
- File tools cannot reach `.syndica/traces` or `.syndica/handoffs`.
- A command that touches a path outside the workspace asks you while the agent has not read any
  command output yet; after that it is rejected unless you pass `--flag untrusted_high_risk=ask`.
- Process groups are cleaned up and command and search output is limited.

In `run --handoff` the host redacts what it writes and prints: both output streams, the handoff
file, and the commit message; `doctor` output is redacted too. Plain `run` and `chat` print what the
agents print.

## What `run --handoff` adds

After the agents finish, the host runs every `workspace.verify` command itself; what an agent says
it ran is listed but never trusted. It accepts a Reviewer's `verdict:` line only from a Reviewer run
that completed, changed nothing, and saw exactly the tree that was verified; among those, the one
that finished last counts. The handoff (goal, changes, checks the agents ran, host verification,
the agents' conclusion, next steps, Git outcome) is redacted, written to
`.syndica/handoffs/<time>-<id>.md` with mode 0600, and printed to stdout. Only one `--handoff` run
can hold a workspace at a time.

When verification passed and the Reviewer said `pass`, the host shows the branch, upstream, commit
title, and changed paths with the lines each one adds and removes, then asks:

<!-- not-tested: the host's interactive question, shown for reference -->
```text
1  commit and push      2  commit only      3  I'll handle it
```

End of input, an empty line, or anything else means `3`, so a run with `</dev/null` never commits.
`--git-mode commit|push|manual` answers in advance for unattended runs; `commit` and `push` exit 3
without touching Git when their preconditions do not hold (a failed run or failed verification
still exits 1).

The host commits only the paths this run changed, scans the added lines for secrets first, runs
your commit hooks with the agents' environment rather than yours, and checks the resulting commit
before pushing it to the recorded upstream with local hooks off and never with `--force`. Push is
not offered when there is no upstream, when a `pre-push` or `reference-transaction` hook exists
(push it yourself so your hook runs), when the workspace had changes before the run, or when local
commits were already ahead of the upstream.

`run --no-write` keeps only `Read`, `Grep`, `Glob`, `AskUser` and `AskUserQuestion` (orchestration
tools such as `Delegate` stay) and never verifies or commits.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | The run failed, verification failed, the workspace is busy, or a commit or push that had started failed |
| `2` | Usage error |
| `3` | `--git-mode` preconditions not met |
| `130` | Interrupted with Ctrl-C; takes precedence over `1` and `3` |

Ctrl-C during the run or during verification still writes the handoff, as a failed run with
verification skipped, and any Ctrl-C before the Git step skips that step. Ctrl-C during the Git step
stops Git at once and leaves the file at `git: pending`, because the host cannot tell whether a
push reached the remote; the printed copy says whether a commit was made, and if none was, the new
files staged for it are unstaged. Further presses do not cut that cleanup short. Once the Git step
is over or skipped, Ctrl-C changes nothing. Before that, a third press outside the Git step forces
an exit; the handoff may then be missing or still at `git: pending`.

## Custom redaction and the commit check

A project can add redaction patterns (see
[Redaction](../concepts/traces-and-redaction.md#redaction-happens-before-writing)). Custom patterns
only redact. The check that refuses to commit a secret looks for long environment values,
well-known token formats and private keys, never for custom patterns: one misfiring pattern would
block every commit.

## Trust and threat model

`.syndica.yaml` is code you run: `run --handoff` and `doctor --verify` execute `workspace.verify` on
your machine without danger checks, so using them in a repository you do not trust is the same as
running its test scripts. Changes to protected paths (always `.gitignore` and `.gitattributes`,
plus `workspace.protected`) are highlighted and block `--git-mode` commits, because they decide
what "checks passed" means.

These guards stop the common forms of an agent's mistakes (deleting the repository, pushing,
rewriting Git state), naive prompt injection ("read `~/.config/...`", "push this"), and secrets
leaking through the agents' environment or the handoff. They do not stop a command written in a
form the rules cannot read, whether by accident or on purpose, nor an agent that has been persuaded
to evade them: code it writes or runs (`python -c`, a script, `ssh`, and the tests the host
verifies) can read any file your user can read, reach the network, read the host process's
environment and your login keychain, and use credential files under `HOME` (`~/.netrc`,
`~/.npmrc`). Processes that call `setsid` escape process-group cleanup. The real boundary is a
sandboxed workspace backend; with the local workspace, use development mode only on trusted input
such as your own repository and prompts.
