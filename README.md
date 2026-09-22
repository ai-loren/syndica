# Syndica

A multi-agent harness for verifiable knowledge work.

> **Status: pre-release, under active design and development.**
> Not yet published. The public API is unstable and will change without notice until v1.0.

## What it is

Syndica runs a *team* of agents — not a single agent with tools — on long-horizon knowledge work:
documents, spreadsheets, diagrams, reports, and code.

Four claims that shape the design:

1. **Multi-agent is a first-class primitive.** Agents have process semantics — spawn, terminate,
   suspend, resume, and remain addressable after being reaped. Delegation, forking, and messaging
   are kernel tools. A system's org chart is declarative data, not code.
2. **Built for knowledge work, not only for coding.** Documents, spreadsheets and diagrams are
   treated as seriously as source files.
3. **Artifacts and acceptance are kernel concerns.** Deliverables have identity, versions and
   semantic diffs. Acceptance criteria must be traceable to the original request — which is how
   you tell whether the work is done when there is no compiler and no test suite.
4. **Inference and training share one substrate.** The same agent structures and tools that do the
   work also produce training data. Acceptance verdicts are reward and preference signals for free.

## Design

See [`docs/design/2026-09-03-architecture.md`](docs/design/2026-09-03-architecture.md).

## CLI

Install the package and its OpenAI-compatible backend:

```bash
python -m pip install -e '.[openai]'
export OPENAI_BASE_URL=https://your-endpoint.example/v1
export OPENAI_MODEL=your-model
export OPENAI_API_KEY=your-key
```

Run a prompt directly, or inspect the built-in catalog:

```bash
syndica "write a short report"
syndica run "inspect this repository" --agent Worker --max-turns 20
syndica agents list
syndica flags show untrusted_high_risk
syndica doctor
```

The CLI reads `.syndica.yaml` from the current directory by default. Use `--config path.yaml`
before the command to select another file. Configuration is a single safe YAML mapping:

```yaml
version: 1
agents:
  - name: Reviewer
    description: Reviews a completed artifact.
    model: review-model
    tools: [Read, Grep, Glob]
topologies:
  review:
    slots:
      - name: root
        agent: Reviewer
        is_root: true
        cardinality: singleton
        boot: eager
```

`--flag` is repeatable and uses exact `key=value` syntax. Milestone 1 executes `--max-turns`;
cost, token, and wall-clock budgets are accepted by the parser but fail explicitly until their
budget engine is implemented. `chat` keeps sessions only inside its current process. Cross-process
`--session` recovery and `sessions ...` require the later `SessionStore` milestone.

## Logging

Syndica is a library and does not install handlers or change application logging policy. Hosts
must keep both the `syndica` logger and its handlers at `WARNING` or lower. Raising either
threshold to `ERROR` drops the only record for a low-risk call that was allowed despite
untrusted provenance.

Do not randomly sample or broadly deduplicate `syndica.kernel.runtime` errors or the
untrusted-provenance warning from `syndica.kernel.tool_executor`: each can be the only evidence
of one discarded input, broker failure, or safety-relevant allow decision. The repeat-prone
`syndica.kernel.hooks.center` discarded-return error may be rate-limited by the exact
`(junction, callback, discarded value)` tuple over a bounded window, provided the first record
and a periodic suppressed-count summary are retained. Prefer sink-side rate limits and alerts
over raising logger thresholds; sustained repeats mean the hook is registered on the wrong
junction and should be fixed.

## License

MIT
