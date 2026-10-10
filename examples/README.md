# Examples

Four demos, each a small directory with a `run.sh` that works against your own model (the
`OPENAI_*`, `GEMINI_*` or `OPENROUTER_*` variables in your environment) and, with `--offline`,
against the scripted fake model in `tests/e2e/fake_openai.py`. Every run works in a fresh temporary
copy and never touches your `~/.config`.

| Demo | Shows |
| --- | --- |
| [`cli-self-hosting`](cli-self-hosting/README.md) | Development mode: agents fix a failing test, the host verifies, a redacted handoff, Git left to you |
| [`web-multi-agent`](web-multi-agent/README.md) | `syndica up`: an Orchestrator delegates to a Worker, a confirmation in the page, an artifact preview |
| [`artifact-verification`](artifact-verification/README.md) | A goal, its rubrics, a deliverable, the four-value verdict, and what `unverified` looks like |
| [`training-export`](training-export/README.md) | `syndica batch` with four verdicts, `syndica export` to SFT / preference / RL / trajectory, TRL dry runs |

[`syndica-example-module`](syndica-example-module/src/syndica_example_module/__init__.py) is a
minimal third-party Runtime Module (one flag, one tool, one plugin); see
[Writing a Runtime Module](../docs/user/guides/writing-a-module.md).

The [Demos](../docs/user/demos.md) page of the user documentation walks through all four.
`tests/test_examples.py` runs every demo offline, and the release acceptance
(`scripts/acceptance.sh`) drives the web demo in a real browser from a freshly installed wheel.
