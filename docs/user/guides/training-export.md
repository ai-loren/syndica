# Training export

The run that proves a task was done is also training data. Acceptance verdicts become rewards and
preference pairs, so Syndica needs no separate scoring pipeline. Export runs entirely from what the
workspace already stores: sessions, goals, rubrics, traces and the artifact index.

## Run a batch

`syndica batch <tasks.jsonl>` runs one task per line and prints one JSON result per line. A failed
task does not stop the rest; the exit code is 1 if any task or line failed.

Each line has an `id` and a `prompt`, and optionally an `agent` (like `run --agent`) and a `goal`
(like `run --goal`):

```jsonl
{"id": "report-1", "prompt": "Write a one-page report on the notes in notes/", "agent": "Worker", "goal": "a one-page report with a summary section"}
{"id": "report-2", "prompt": "Turn notes/meeting.md into a list of action items"}
```

```bash
syndica batch tasks.jsonl
```

For data worth training on, enable [acceptance](../concepts/artifacts-and-acceptance.md) in
`.syndica.yaml` so each task ends with a verdict.

## Export

```bash
syndica export sft --output ../exports/sft
syndica export preference --output ../exports/preference
syndica export trajectory --output ../exports/trajectory --include-unverified
```

| Format | File | Contents |
| --- | --- | --- |
| `sft` | `messages.jsonl` | OpenAI-shaped `messages` with tool calls, and the artifacts each sample produced. |
| `sharegpt` | `sharegpt.jsonl` | The same conversations as `human` / `gpt` / `function_call` / `observation`. |
| `preference` | `preference.jsonl` | DPO-shaped `prompt` / `chosen` / `rejected` for a better and a worse verdict of the same goal and the same rubrics. |
| `rl` | `rl.jsonl` | `input_ids`, a `loss_mask` covering the assistant's text and tool calls, and a reward from the verdict: `pass` 1.0, `partial` 0.5, `block` 0.0, `unverified` `null`. |
| `trajectory` | `trajectories.jsonl` | Neutral `syndica.trajectory/1` records: messages with trust markers, tool calls, artifacts, every verdict the session received (across resumed runs too), whether any verdict came from an untrusted judge, causality, timing and redaction metadata. |

Every export also writes a `manifest.json` with the sources, the samples, what was left out and why,
and the redaction rule set.

- **`unverified` samples are left out** unless you pass `--include-unverified`, and the manifest
  records that choice. "We could not verify it" must never become a positive training example.
- A sample is flagged **`untrusted`** when it carries untrusted content, or when any of its verdicts
  was given by a verifier that had read untrusted content: a reward label from a persuaded judge is
  as untrustworthy as what it read.
- Preference pairs are matched on the content of the goal and the rubrics, not on rubric ids a
  model chose.
- RL token ids are UTF-8 bytes by default and the manifest names the tokenizer; re-tokenise for your
  model on the training side.

## Safety of exported data

- Export redacts with the same rules as everything else the host writes (your `redaction.rules`
  plus secret values in the current environment), string by string before encoding. Placeholders
  stay and raw secrets never leave.
- The output directory must be **outside the workspace**, new or empty, and not itself a symlink:
  an export contains other sessions' full text and untrusted content, and inside the workspace the
  next run would treat it as a trusted baseline. Files are created with mode `0600`, and the
  workspace's stores are only read.
- A session or trace that cannot be read is named in the manifest, and the command exits 1 after
  writing the rest.

## TRL dry runs

The `trl` extra adds `syndica.backends.trl.dry_run` and `dry_run_preference`. They load an exported
SFT or preference file into TRL's `SFTTrainer` or `DPOTrainer` with a small local model, without
downloading anything, and stop once the trainer has prepared the dataset (they do not call
`train()`):

<!-- not-tested: needs the trl extra and a dataset exported by `syndica export sft` -->
```python
from pathlib import Path

from syndica.backends.trl.dry_run import dry_run

result = dry_run(Path('../exports/sft/messages.jsonl'))
print(result['rows'], result['prepared_columns'])
```
