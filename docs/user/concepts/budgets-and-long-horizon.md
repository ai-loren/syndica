# Budgets and long-horizon work

Long tasks fail in ways short ones do not: the goal drifts, progress is invisible, nobody knows
when to stop, the context rots after several compactions, cost runs away, and the agent loops on a
path that does not work. None of these is "the model is not smart enough"; each needs a mechanism.

## Budgets

A run has a budget over four dimensions:

| Option | Limits |
| --- | --- |
| `--max-turns <n>` | Turns for the whole agent tree (default 200) |
| `--max-tokens <n>` | Input plus output tokens |
| `--max-cost <amount>` | Cost, priced from `prices:` and the backend's defaults |
| `--max-wall-clock <seconds>` | Real time; a deadline also interrupts the model call in flight |

- **Budgets form a tree.** A delegated or forked agent's budget is carved from what its parent has
  left: half of it, unless `Delegate` or `Fork` ask for `turns` / `tokens` explicitly. A finished
  child gives its unused share back. A role's own `budget.turns` still caps that agent.
- **Every model call is admitted against an estimate first** (main turns, retries, failovers,
  verifiers) and booked with the real usage afterwards, so overruns are limited to estimation
  error. Verifier calls are booked to the agent they serve.
- **Agents can see their budget.** The first turn gets a budget block in the history, and new
  blocks appear when the remaining budget crosses 50, 25 and 10 percent. `GetBudget` returns the
  details.

When the budget runs out, `--on-exhausted` decides:

| Mode | Behaviour |
| --- | --- |
| `graceful_wrapup` (default) | One last wrap-up turn to deliver what is done. |
| `hard_stop` | Use every turn and stop. |
| `ask_human` | Ask you for more turns first; with no answer, wrap up. |

The default is deliberate: a ten-hour task killed with all its work lost is the worst possible
ending.

### Cost

`--max-cost` needs prices. The backend knows only free models (`*:free`, `openrouter/free`); give
other models three per-million-token prices in `.syndica.yaml`:

```yaml
version: 1
prices:
  gpt-4.1-mini: {input: 0.4, output: 1.6, cached_input: 0.1}
```

`cached_input` defaults to `input`. A run with a cost cap and a model without a price is refused
before it starts. OpenAI-compatible endpoints do not report cache writes, so cost can be slightly
under-counted.

Runs with `--max-cost`, `--max-wall-clock` or `--on-exhausted ask_human` record a trace but no
replay document: those decisions depend on prices, real time or a person.

In `chat`, the budget accumulates across messages. Once an agent has nothing left, messages to it
are refused and you need a new `chat`.

## Progress, stop criteria and drift

`modules: [acceptance, long-horizon]` adds the long-horizon loop for the agent that has the goal.
It needs `acceptance` because progress is only ever measured by the verifier, never taken from
the agent's own account.

- **Progress** is the number of satisfied mandatory rubrics. Every `long_horizon_progress_every`
  turns (default 10) a milestone check measures it. A rubric that was satisfied and is now unmet is
  reported as a **regression**: the agent broke something it had finished.
- **Three independent stop criteria**: done (every mandatory rubric satisfied and the verdict is
  `pass`), out of budget, and stalled (`long_horizon_stall_turns` turns, default 20, without
  progress). A stall triggers correction, not termination.
- **Drift** is checked without model calls: if the last `long_horizon_drift_window` turns of tool
  calls share no words with the goal and rubrics, that is suspected drift.
  `long_horizon_semantic_drift` (off by default) asks a model to confirm it first.
- When the remaining budget drops below `long_horizon_budget_low_percent`, the strategy is asked
  how to spend the rest.

Milestone checks and the semantic drift check are skipped when the budget cannot fit them next to
the agent's own next calls.

What to tell the agent is decided by a **strategy**. The built-in one suggests a different approach
when progress stalls, says to secure the core deliverable when the budget is low, and restates the
goal verbatim on drift. When a milestone passes with every mandatory rubric satisfied, the agent is
asked for its final answer; after a `partial` or `block` verdict the strategy may send it back up
to `long_horizon_max_continuations` times per cycle. The events appear under `长程：` in
`syndica trace show`.

```yaml
version: 1
modules: [acceptance, long-horizon]
flags:
  acceptance_rubrics: true
  acceptance_outcome: true
  long_horizon_progress_every: 5
  long_horizon_stall_turns: 15
```

Strategies are pluggable: a different long-run paradigm (restart loops, actor-critic,
monitor-actor-critic) is a `LongHorizonStrategy` implementation shipped as a Runtime Module, with a
contract suite to prove it behaves. See [Writing a backend or store](../guides/writing-a-backend.md).

## Reaping finished children

Finished delegated agents stay addressable as zombies so a message can bring them back. To keep
long runs from accumulating them, a parent keeps at most eight finished children whose results
were already collected; older ones are reaped (their budget settles back into the parent), and
addressing a reaped id says so.
