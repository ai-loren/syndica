# Sessions, compaction and memory

## Sessions

The root agent's conversation of every `run` and `chat` is saved under `.syndica/sessions`. The
session id is the root agent's id.

```bash
syndica sessions list
syndica sessions show <session-id>
syndica run --session <session-id> "now add a summary at the top"
syndica sessions resume <session-id>
syndica sessions rm <session-id>
```

`run --session` sends one more message to a saved session; `sessions resume` opens an interactive
chat on it. Only one process holds a session at a time. Budgets carry across resumed sessions.

Delegated sub-agents and their mailboxes are **not** persisted. Resuming restores the root
conversation and says which sub-agents were lost; the web UI does the same after `syndica down` or
a crash.

## Why the system prompt never changes

Providers cache requests by byte prefix, and the system prompt comes first. One changed byte
invalidates the cache for the rest of the session. That failure is silent: you only notice it on
the bill. So Syndica keeps the prefix byte-identical turn after turn:

- anything that changes during a session (working directory, isolation state, budget left, the
  goal and rubrics, recalled memory) is injected into the history, once and idempotently;
- new content is only appended;
- one-off calls such as titles and memory consolidation do not write the cache.

The `prefix-drift` module reports when a request's prefix changed and how many tokens that wasted.
Prompt variables in role manifests are limited to values that cannot change within a session:
`{os}`, `{platform}` and `{shell}` are allowed; `{cwd}` is refused.

## Compaction

Long runs outgrow the context window. Compaction is lossy and cannot be undone, so it follows
fixed rules. Turn it on with the `compaction` module and choose a strategy:

| `compaction_strategy` | Does | Cost |
| --- | --- | --- |
| `off` (default) | Nothing | — |
| `rule_based` | Keeps recent turns, keeps tool calls paired with their results, truncates long outputs | No model calls |
| `pruning` | Drops content that is no longer relevant, such as the details of finished tasks | Low |
| `summary` | A model summarises early history | High |
| `goal_directed` | Keeps what the **unmet rubrics** still need, wherever it is in the history | Medium |

"Recent" and "still needed" are different things: a constraint set fifty turns ago may still
matter while the last three turns of debugging output do not. `goal_directed` uses the rubrics
from [acceptance](artifacts-and-acceptance.md) to tell them apart.

Compaction runs when the context reaches `compaction_threshold_percent` of the window (default 80),
as a fallback when the model reports an overflow, and when the agent asks for it with the
`Compact` tool. It never breaks five invariants:

1. Tool calls stay paired with their results.
2. The system prompt is not compacted.
3. The goal and the unmet rubrics survive.
4. Injected environment facts survive or are re-injected.
5. Untrusted markers survive on everything derived from untrusted content.

The full pre-compaction history is redacted and archived first, and each compaction is a point in
the trace, so "why did this turn suddenly cost more" is answerable.

## Memory

Some experience should outlive a session: project conventions, pitfalls, a user's preferences.
The `memory` module keeps it in two phases and two levels:

- **Phases.** Short-term records accumulate; after `memory_consolidate_after` records (default 50)
  a model consolidates them into a section of long-term memory.
- **Levels.** Workspace memory belongs to one project; global memory spans all work.

Write access is narrow: **only the root coordinator writes global memory**, workers write only
their workspace level, and **untrusted content never reaches global memory**. Polluted memory
affects every later task, which is worse than no memory. Agents write with the `Remember` tool;
recalled memory is injected into the history once per cycle and cached (`memory_recall`).

The CLI keeps memory in `.syndica/memory`:

```bash
syndica memory list
syndica memory forget 2
syndica memory forget --pending
syndica memory list --global
```

`forget <n>` removes the n-th long-term section shown by `list`; `--pending` drops short-term
records that are not consolidated yet; `--global` works on the global level.
