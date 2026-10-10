# Observability

Every `run` and `chat` session is recorded as a trace in `.syndica/traces/<UTC time>-<id>/`; the
model is described in [Traces and redaction](../concepts/traces-and-redaction.md). This page covers
the commands, OTLP export and logging.

## Choosing the level

```bash
syndica run "summarise the notes" --trace full
```

`--trace off|structure|replay|full` sets the level for one run; `trace: {level: ...}` in
`.syndica.yaml` sets the project default (`replay` when unset). `chat` records at most `structure`.
A trace that lost records says so (`trace（不完整…）`, "trace (incomplete...)") and is neither
replayed nor exported.

## Reading traces

```bash
syndica trace list
syndica trace show <trace-id>
```

`trace list` prints traces newest first: UTC time, id, status, agents, spans, size and the command;
a replay's own trace says which trace it replayed. An id may be the full id, a unique prefix, or the
directory name printed after a run.

`trace show` prints who created whom, who waits on whom (a wait cycle is marked), the three slowest
tool calls (calls that only waited for another agent are left out), model calls per agent (time,
tokens, failures, platform failovers), a timeline, budget and long-horizon events, and which tools
and plugins each module gave each agent, with how often each extension junction fired. Traces are
written as `syndica.trace/8`; older trace versions are still read.

## Replay

```bash
syndica trace replay <trace-id>
syndica trace replay <trace-id> --allow-divergence
```

Replay runs a trace offline. Model replies, tool results, human answers and host actions come from
the record, while the kernel, its danger judgement and the modules the replay can rebuild (safety,
human interaction, retry, prefix drift, and third-party modules as described below) run for real
and are compared item by item.

- Host modules it cannot rebuild, such as the development guard, are named in the output; their
  effects (the calls they skipped) are already in the record.
- Every agent sees its messages when the record says it did, a kill lands where it landed, and a
  call the original never got back stays pending until the same interruption cuts it, so agents
  that ran concurrently replay the same way.
- A `--topology` run records the topology as actually assembled and the run's root budget; replay
  assembles it through the same slot logic, so slot tools, budgets and stable ids match.
- Exit code 0 means it reproduced, 1 that it diverged (each divergence names the agent, turn and
  unit), 2 that the trace cannot be replayed. `--allow-divergence` keeps going after the first
  divergence and reports every one (still exit 1). The replay records itself as a new trace that
  points back to the original.

A run with a third-party module is replayable only if that module declares `replay_safe` or runs no
code of its own (flags only). Replay rebuilds such a module only when the configuration enables it
(the workspace's `.syndica.yaml`, or the file given with `--config`, which must be trusted when it
lies inside the workspace) and the installed distribution has the recorded name and version; it
checks all of this before importing anything. When it cannot rebuild one, `trace replay` exits 2 and
names the module and the reason: replaying without code the original run ran would not be the same
run.

## Exporting to OpenTelemetry

Install the `otel` extra. Send one trace to a collector:

```bash
syndica trace export <trace-id> --otlp http://localhost:4318
```

Traces are also exported at the end of every run when **your environment** names an endpoint:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_SERVICE_NAME=syndica
```

Syndica honours `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SDK_DISABLED`, `OTEL_TRACES_EXPORTER`, `OTEL_SERVICE_NAME` and
`OTEL_RESOURCE_ATTRIBUTES`. Export uses OTLP/HTTP JSON; spawn and message edges become span links.

- `OTEL_EXPORTER_OTLP_HEADERS` go only with the environment's endpoint, never with `--otlp`, and
  never over plaintext HTTP to another machine.
- A project's `.syndica.yaml` cannot choose the endpoint: a repository you do not trust would
  otherwise receive your traces and your collector credentials.
- Everything exported goes through the redactor first.

## Pruning

```bash
syndica trace prune
syndica trace prune --keep 10
syndica trace prune <trace-id>
```

`trace prune` keeps the newest finished traces (50 by default, or `--keep <n>`) together with their
replays, and never deletes an unfinished one. Naming traces deletes exactly those; names and
`--keep` cannot be combined. A trace written by a newer Syndica is listed as unreadable with what to
do (upgrade), never as tampered.

## Logging

Syndica is a library and does not install log handlers or change your application's logging
policy. When you embed it:

- Keep both the `syndica` logger and its handlers at `WARNING` or lower. Raising either to `ERROR`
  drops the only record of a low-risk call that was allowed despite untrusted provenance.
- Do not randomly sample or broadly deduplicate errors from `syndica.kernel.runtime` or the
  untrusted-provenance warning from `syndica.kernel.tool_executor`: each can be the only evidence of
  one discarded input, broker failure, or safety-relevant allow decision.
- The repeat-prone "discarded return value" error from `syndica.kernel.hooks.center` may be
  rate-limited by the exact `(junction, callback, discarded value)` tuple over a bounded window, as
  long as the first record and a periodic suppressed-count summary are kept. Sustained repeats mean
  the hook is registered on the wrong junction and should be fixed.
- Prefer rate limits and alerts in your log sink over raising logger thresholds.

The CLI prints warnings to stderr, including every model-platform failure (see
[Model providers](models.md#several-platforms-and-failover)).
