# Demo: a deliverable, its rubrics and the verdict

A `Worker` writes a release note from `changes.txt` and marks it as a deliverable. Acceptance
checks it against rubrics projected from the goal and ends the run with one of four verdicts. The
same task then runs again with the outcome check turned off, to show what `unverified` looks like.

What it shows:

- **a goal and its rubrics**: `--goal` states what must be delivered; before any work starts, a
  verifier that sees the request but not the result projects it into rubrics, each quoting the
  part of the goal it comes from (`syndica sessions rubrics <session-id>`);
- **a deliverable**: the agent calls `Deliver` on `notes.md`, so only that file is checked
  (`syndica artifacts list` shows it with its version);
- **the verdict**: L1 checks the deliverable's format, L2 whether everything asked was done, L3
  each rubric against the reply and the file, with evidence the kernel re-opens. `--verdict-exit`
  turns the verdict into the exit code: 10 `pass`, 11 `partial`, 12 `block`, 13 `unverified`;
- **`unverified`**: with `--flag acceptance_outcome=false` nobody checked the result, so the run is
  never reported as `pass`, whatever the agent says.

## Run it

You need `syndica` with the `openai` extra and `python3` on `PATH`.

With your own model:

```bash
export OPENAI_BASE_URL=https://api.example.com/v1
export OPENAI_MODEL=your-model-name
export OPENAI_API_KEY=your-key
sh examples/artifact-verification/run.sh
```

Offline, against the scripted fake model (no key, no cost; needs a clone of the repository):

```bash
sh examples/artifact-verification/run.sh --offline
```

Each run works in a fresh copy of `workspace/`:

```bash
syndica run --agent Worker --max-turns 30 --verdict-exit \
  --goal 'a release note in notes.md with a Highlights section that lists the breaking change' \
  'Write the release note for version 2.0 to notes.md from changes.txt, then deliver it.'
syndica artifacts list
syndica sessions rubrics <session-id>
syndica run --agent Worker --max-turns 30 --verdict-exit --flag acceptance_outcome=false \
  --goal 'a release note in notes.md with a Highlights section that lists the breaking change' \
  'Write the release note for version 2.0 to notes.md from changes.txt, then deliver it.'
```

## What to look at

The script ends with a line like:

<!-- not-tested: sample output of run.sh -->
```text
== verdicts: with every layer: pass; with L3 off: unverified
```

- stderr of each run has the verdict (验收：pass, or 验收：unverified；没能验证（outcome_check_off）
  for "could not verify: the outcome check is off") and the trace directory.
- `syndica sessions rubrics` prints the goal and the projected rubrics (`R1`, `R2`), each active and
  checkable.
- With a real model the first verdict depends on the model and the note it writes: `partial` (not
  finished, or could not be confirmed) and `block` (evidence that the result is wrong) are real
  outcomes too, and the defaults send the agent back once or twice before recording them. The
  second verdict is always `unverified`.
- [Training export](../training-export/README.md) shows all four verdicts in one batch, and why
  `unverified` samples are left out of training data by default.

## Files

| Path | What it is |
| --- | --- |
| `workspace/changes.txt` | The input |
| `workspace/.syndica.yaml` | `modules: [acceptance]` and the flags that turn on rubrics, L2 and L3 |
| `offline/script.json` | What the fake model (and the fake verifiers) answer in `--offline` mode |
| `run.sh` | The demo |
