# Demo: a batch of tasks becomes training data

`syndica batch` runs the four tasks in `tasks.jsonl`, acceptance gives each one a verdict, and
`syndica export` turns the sessions into training files. The run that proves a task was done is the
training sample, and its verdict is the reward.

What it shows:

- **a batch dataset**: one JSON task per line with `id`, `prompt`, `agent` and `goal`;
- **four verdicts**: offline, the tasks are scripted to end in `pass`, `block`, `partial` and
  `unverified` (a haiku that "feels calm" can only be judged by a person, so it stays
  `unverified`);
- **four export formats**, written outside the workspace: `sft` (OpenAI-shaped messages with tool
  calls), `preference` (the `pass` and the `block` note share a goal and rubrics, so they form a
  chosen / rejected pair), `rl` (token ids, a loss mask and the reward: `pass` 1.0, `partial` 0.5,
  `block` 0.0) and `trajectory` (neutral records with trust markers and timing);
- **`unverified` is left out** of every export by default, and each `manifest.json` says so;
- **TRL dry runs**: with the `trl` extra installed, the SFT and preference files are loaded into
  TRL's `SFTTrainer` and `DPOTrainer` with a small local model, without training or downloading.

## Run it

You need `syndica` with the `openai` extra and `python3` on `PATH`; add the `trl` extra for the dry
runs.

With your own model:

```bash
export OPENAI_BASE_URL=https://api.example.com/v1
export OPENAI_MODEL=your-model-name
export OPENAI_API_KEY=your-key
sh examples/training-export/run.sh
```

Offline, against the scripted fake model (no key, no cost; needs a clone of the repository):

```bash
sh examples/training-export/run.sh --offline
```

The script copies `workspace/` into a new temporary directory and runs:

```bash
syndica batch tasks.jsonl
syndica export sft --output ../exports/sft
syndica export preference --output ../exports/preference
syndica export rl --output ../exports/rl
syndica export trajectory --output ../exports/trajectory
```

## What to look at

Offline, the summary at the end reads:

<!-- not-tested: sample output of run.sh -->
```text
sft         3 rows; samples ['block', 'partial', 'pass']; left out as unverified: 1
preference  1 rows; samples ['block', 'partial', 'pass']; left out as unverified: 1
rl          3 rows; samples ['block', 'partial', 'pass']; left out as unverified: 1
trajectory  3 rows; samples ['block', 'partial', 'pass']; left out as unverified: 1
```

- stderr of the batch has one verdict line per task (验收：pass, 验收：block；..., and so on) and
  stdout one JSON result per task.
- Each `manifest.json` lists the samples with their verdicts, the session left out as
  `dropped_unverified`, and the redaction rule set. Pass `--include-unverified` to keep
  `unverified` samples; the manifest records that choice.
- With a real model the verdicts depend on the model: a capable one may pass every task, and a
  preference pair then needs two different verdicts for the same goal. Re-running a task with
  `syndica run --session <id>` adds a second verdict to the same session.

See [Training export](../../docs/user/guides/training-export.md) for every format and field.

## Files

| Path | What it is |
| --- | --- |
| `tasks.jsonl` | The dataset |
| `workspace/changes/` | The inputs the tasks read |
| `workspace/.syndica.yaml` | Acceptance with rubrics, L2 and L3; retries off so every task keeps its first verdict |
| `offline/script.json` | What the fake model and verifiers answer in `--offline` mode |
| `run.sh` | The demo |
