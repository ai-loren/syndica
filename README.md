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

## License

MIT
