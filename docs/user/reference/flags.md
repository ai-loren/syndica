# Flag reference

Every flag declared by the built-in modules, in installation order and then declaration order.
`flags:` in `.syndica.yaml` and `--flag key=value` on the command line are both checked against
these types and values, and the command line overrides the configuration. **A project
configuration can only tighten**: it cannot loosen a safety flag.

`safety` and `human-interaction` are always installed by the CLI; the other modules are enabled
with `modules:` (see [Configuration](../guides/configuration.md#modules-and-flags)). Flags of a
third-party module are shown by `syndica flags list` once the configuration enables it.

<!-- The test suite compares this table with the flag declarations in the code and with the
maintainers' generated table in docs/reference/flags.md: a flag added there and missing here
fails the build. -->

| Key | Module | Type | Default | Allowed values | CLI restriction | Meaning |
| --- | --- | --- | --- | --- | --- | --- |
| `max_wait_seconds` | `safety` | int | `0` | ≥ 0 | — | Longest wall-clock wait, in seconds, for one agent waiting on an answer or on another agent; 0 means no limit. |
| `untrusted_high_risk` | `safety` | str | `"reject"` | `"reject"` / `"ask"` | `allow` is refused; a project configuration accepts only `reject` | What happens to a high-risk call that traces back to untrusted content. |
| `llm_retry_attempts` | `retry` | int | `0` | 0 to 10 | — | Extra attempts on the same model after a failed call. |
| `llm_retry_backoff_ms` | `retry` | int | `0` | 0 to 60000 | — | Milliseconds to wait before each retry or failover call. |
| `llm_fallbacks` | `retry` | str | `""` | — | Only the empty value: `retry` enabled by name has no fallback models | Comma-separated fallback model names, each tried once in order. |
| `prefix_drift_detection` | `prefix-drift` | bool | `true` | — | — | Check every turn whether the cached prefix of the model request was rewritten. |
| `compaction_strategy` | `compaction` | str | `"off"` | `"off"` / `"rule_based"` / `"pruning"` / `"summary"` / `"goal_directed"` | — | Compaction strategy. |
| `compaction_threshold_percent` | `compaction` | int | `80` | 1 to 99 | — | Compact early when the context reaches this percentage of the window. |
| `compaction_target_percent` | `compaction` | int | `50` | 1 to 99 | — | Compact down to this percentage of the window; must be below the threshold so there is hysteresis. |
| `compaction_context_tokens` | `compaction` | int | `128000` | ≥ 1 | — | Window size, in tokens, to assume when the model does not report one. |
| `memory_recall` | `memory` | bool | `true` | — | — | Inject long-term memory into the history at the start of each cycle. |
| `memory_consolidate_after` | `memory` | int | `50` | ≥ 1 | — | Consolidate short-term records into a long-term section after this many. |
| `acceptance_rubrics` | `acceptance` | bool | `false` | — | — | Project the goal into anchored rubrics before work starts (one verifier call). |
| `acceptance_output` | `acceptance` | bool | `false` | — | — | L2 output acceptance: on completion, judge conservatively whether everything asked was done. |
| `acceptance_output_retries` | `acceptance` | int | `1` | ≥ 0 | — | How many times L2, or a deliverable failing L1, sends the agent back; 0 only records the verdict. |
| `acceptance_outcome` | `acceptance` | bool | `false` | — | — | L3 outcome acceptance: on completion, check each rubric against the reply and evidence in the workspace. |
| `acceptance_outcome_block_retries` | `acceptance` | int | `2` | ≥ 0 | — | How many times an L3 `block` (evidence it is wrong) sends the agent back; 0 only records the verdict. |
| `acceptance_outcome_partial_retries` | `acceptance` | int | `1` | ≥ 0 | — | How many times an L3 `partial` (not done, or could not be confirmed) sends the agent back; 0 only records the verdict. |
| `acceptance_feedback` | `acceptance` | bool | `false` | — | — | Turn a person's messages after the goal into proposed rubrics, active only once a person activates them. |
| `acceptance_verifier_timeout` | `acceptance` | int | `300` | ≥ 1 | — | Time limit in seconds for one acceptance (L2 and L3 together) or projection; on timeout the run continues and is marked `unverified`. |
| `acceptance_verifier_max_turns` | `acceptance` | int | `8` | ≥ 1 | — | Turn limit for each verifier agent (projection, L2, L3, feedback proposals). |
| `long_horizon_progress_every` | `long-horizon` | int | `10` | ≥ 0 | — | Run a milestone L3 check to measure progress every this many turns; 0 measures only on completion. |
| `long_horizon_stall_turns` | `long-horizon` | int | `20` | ≥ 0 | — | Turns without a newly satisfied rubric before the run counts as stalled and the strategy is asked to correct; 0 disables the check. |
| `long_horizon_budget_low_percent` | `long-horizon` | int | `20` | 0 to 99 | — | Ask the strategy how to spend the rest when less than this percentage of the budget is left; 0 never asks. |
| `long_horizon_drift_window` | `long-horizon` | int | `8` | ≥ 0 | — | Every this many turns, check that recent actions still anchor to the goal; 0 disables the check. |
| `long_horizon_semantic_drift` | `long-horizon` | bool | `false` | — | — | When the anchor check is suspicious, confirm with a semantic comparison (one verifier call). |
| `long_horizon_max_continuations` | `long-horizon` | int | `3` | ≥ 0 | — | After acceptance failed and its retries are used up, how many more times per cycle the strategy may send the agent back. |
| `background_after_seconds` | `background` | int | `0` | ≥ 0 | — | Move a call that declared itself backgroundable to the background after this many seconds; 0 turns it off. |
