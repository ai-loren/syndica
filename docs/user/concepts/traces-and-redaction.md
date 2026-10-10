# Traces and redaction

The first debugging question in a multi-agent run is not "which line failed" but "what did each
of the five agents do, who was waiting on whom, where did the time and money go, and from which
step did it go off course". Syndica answers it with one trace model, and makes sure nothing secret
ends up in it.

## The trace model

A trace is one task. Inside it, an **agent span** covers one active period of an agent, a **turn
span** one turn, and **model call** and **tool call** spans sit inside turns. Confirmations and
waits are spans too. Two kinds of edges connect agents:

- **spawn edges**: which span of the parent created the child;
- **message edges**: which span sent a message and which span consumed it.

With those, "who is waiting on whom" is a query, not guesswork from logs.

The CLI writes one trace per `run` and per `chat` session to `.syndica/traces/<UTC time>-<id>/`
(directories `0700`, files `0600`), and prints the path on stderr when the run ends.

## Levels

| Level | Spans and edges | Replay document | Full requests |
| --- | --- | --- | --- |
| `off` | — | — | — |
| `structure` | yes | — | — |
| `replay` (default) | yes | yes | per-unit fingerprints only |
| `full` | yes | yes | yes, redacted leaf by leaf |

Replay needs only the reply side (a few KB per turn), while full requests carry the whole context
every turn and grow with the square of the number of turns, so `replay` is the default and `full`
is opt-in. Levels are not sampling: sampling would drop exactly the turn that mattered. Traces are
deleted only when you run `syndica trace prune`.

## Capture is replay

What a run records at the `replay` level is the same scenario format the test suite uses for
scripted replays. `syndica trace replay <id>` runs it offline: the outside world (model replies,
tool results, human answers, host actions) comes from the record, while the kernel, its danger
judgement and the modules the replay can rebuild run for real and are compared item by item. A
production run that went wrong becomes a regression test.

Some runs depend on things the record cannot reproduce and write no replay document, for example
runs with acceptance, background tools, isolated topology slots, MCP servers, a cost or wall-clock
cap, or `--on-exhausted ask_human`. Their structure is still recorded. A run with a third-party
module replays only if the module declares `replay_safe` or runs no code of its own; see
[Observability](../guides/observability.md#replay).

## Redaction happens before writing

Credentials must be removed **before** anything reaches disk: deleting them afterwards is too
late, because backups already have them. Syndica has one `Redactor`, and everything persisted or
exported goes through it: traces, session records, memory, artifact versions, handoff files,
commit messages, OTLP exports and training exports. A guard in the test suite checks every write
and outbound point in the package. The one exception is the request sent to the model provider.

Redaction replaces each match with `[REDACTED:<rule id>]`. Built-in rules cover:

- values of host environment variables whose names look secret;
- private key blocks, `Authorization` credentials, passwords in URLs;
- well-known token formats;
- credential-like values assigned to secret-looking names (`STRIPE_KEY=...`).

A project adds its own patterns in `.syndica.yaml`; their ids start with `custom.`, and a pattern
that matches the empty string or a placeholder is refused:

```yaml
version: 1
redaction:
  rules:
    - {id: custom.internal_host, pattern: 'corp-[a-z0-9]+\.internal'}
```

Redaction also happens before truncation, because a cut can land in the middle of a secret and the
remaining half may no longer be recognisable. Patterns run inside the host process like the rest
of `.syndica.yaml`, so one with catastrophic backtracking hangs it.

Redaction is lossy and conflicts with replay: a replayed step that needed the real credential gets
the placeholder and fails. That is the intended trade-off.

## Traces are not agent input

The agents' file tools cannot read or write `.syndica/traces` or `.syndica/handoffs`. Traces hold
tool output that was marked untrusted, and reading it back through a file tool would turn it into
trusted text. An agent's shell can still reach them unless the workspace backend sandboxes it.

Commands for reading, replaying, exporting and pruning traces are in
[Observability](../guides/observability.md).
