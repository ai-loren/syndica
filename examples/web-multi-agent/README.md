# Demo: a multi-agent task in the web UI

`syndica up` serves the web UI for a copy of `workspace/`. In an `Orchestrator` session you ask for
an incident summary of `logs/app.log`; the Orchestrator delegates it to the `Worker`, the Worker
reads the log and writes `incident.md`, and its copy of the report out of the workspace waits for
your approval in the page. The report can then be previewed as an artifact.

What it shows:

- **delegation**: the agent tree shows the Orchestrator and the Worker it created with `Delegate`;
- **a human confirmation**: `cp incident.md ../outbox/incident.md` touches a path outside the
  workspace, so the danger rule `shell.path_outside_workspace` asks a person, and the approval
  covers that one call only;
- **an artifact**: `incident.md` is in the artifacts tab with its version and a preview;
- **trust markers**: the Worker's answer reaches the Orchestrator marked untrusted (不可信), because
  the Worker read command output.

## Run it

You need `syndica` with the `server` and `openai` extras and `python3` on `PATH`.

With your own model:

```bash
export OPENAI_BASE_URL=https://api.example.com/v1
export OPENAI_MODEL=your-model-name
export OPENAI_API_KEY=your-key
sh examples/web-multi-agent/run.sh
```

Offline, against the scripted fake model (no key, no cost; needs a clone of the repository):

```bash
sh examples/web-multi-agent/run.sh --offline
```

The script copies `workspace/` into a new temporary directory, trusts its `.syndica.yaml` and runs
`syndica up --foreground`, which prints a one-time login link and opens it. Then, in the page:

1. pick the role `Orchestrator` and click 新会话 (new session);
2. send the message in [`prompt.txt`](prompt.txt):
   "Have the Worker write an incident summary of logs/app.log to incident.md, then copy it to
   ../outbox/incident.md for the on-call channel.";
3. approve (批准) the card that asks about `cp incident.md ../outbox/incident.md`;
4. open the 产物 (artifacts) tab and click `incident.md`.

Ctrl-C stops the server; sessions are saved first.

## What to look at

- The confirmation card names the rule (`shell.path_outside_workspace`), the risk (`high`), the
  reason and the exact command. Refusing (拒绝) reaches the Worker as a refusal and it carries on
  without the copy.
- The approved copy lands in the `outbox/` directory next to the workspace copy.
- The Agent tab shows two agents; the timeline tab shows each spawn, turn and tool call.
- With a real model the confirmation appears only if the Worker has not run a command before the
  copy: once an agent has read command output, a command touching a path outside the workspace is
  refused instead of asked (see
  [Trust and provenance](../../docs/user/concepts/trust-and-provenance.md)). Ask it to read the log
  with the Read tool if that happens.

The release acceptance drives exactly this flow in a real browser from a freshly installed wheel
(`scripts/acceptance.sh`, check (b)).

## Files

| Path | What it is |
| --- | --- |
| `workspace/logs/app.log` | The input |
| `workspace/.syndica.yaml` | The acceptance module (for the artifact index) and a Worker with an English prompt |
| `prompt.txt` | The message to send |
| `offline/script.json` | What the fake model answers in `--offline` mode |
| `run.sh` | The demo |
