# Errors

Every error Syndica raises for you to handle is a `SyndicaError` and carries three parts, so you can
fix the problem without reading Syndica's source:

- `expected`: what Syndica needed;
- `received`: what it got instead;
- `next_action`: what to do next.

`str(error)` and `error.message` join the three. In v1.0 the texts are in Chinese, like all runtime
messages; the structure is the same in every language. The three parts are keyword-only and must be
non-empty strings: constructing an error without them raises `TypeError` or `ValueError` naming
the class you were constructing. Errors survive `copy` and `pickle` with their notes.

## Hierarchy

<!-- not-tested: a diagram; tests/test_error_taxonomy.py pins the same parents -->
```text
Exception
├── SyndicaError
│   ├── ConfigError
│   ├── ContractViolation
│   │   └── ScenarioMismatch         (syndica.runner)
│   ├── ToolFatalError
│   ├── LLMResponseError
│   │   └── ProviderHTTPError        (syndica.backends.llm_openai)
│   ├── ContextOverflowError
│   ├── PayloadRefused
│   └── OtlpExportError              (syndica.backends.otel_otlp)
└── WriteConflict                    (not a SyndicaError)
```

Classes without a module in parentheses are exported from `syndica.kernel`. Catching
`SyndicaError` catches everything above except `WriteConflict`.

The boundaries below are part of the v1.0 API. New subclasses may be added in v1.x; a subclass
always keeps its parent's meaning, so code that catches the parent keeps working.

## Which one means what

### `ConfigError`: the value you supplied is invalid

Raised when a value the caller handed to Syndica is wrong **as a value**, wherever it came from:

- a configuration file (`.syndica.yaml`, `interop.yaml`) or an agent manifest;
- a declaration (a `FlagDeclaration`, a `RuntimeModule`, a tool's schema, an artifact handler's
  `handles`);
- an argument to a public function or constructor (wrong type, out of range, unknown Flag key, a
  class passed where an instance is required).

The name predates the rule: `ConfigError` covers every caller-fixable input, including API
arguments. The fix is always to change the value and call again; nothing has happened yet.

### `ContractViolation`: the value is fine, the call is not

Raised when a documented contract is broken while the values themselves are valid:

- the call is not allowed in the current state (an unknown or duplicate id, spawning after
  `shutdown`, writing a field another mechanism owns, a hook reading a slot its junction does not
  provide);
- a third-party implementation (a store, a tool, a handler) returned something that breaks the
  protocol it implements.

The fix is to change *when* or *how* you call, or to fix the implementation the message names.

`ScenarioMismatch` (from `syndica.runner`) is a `ContractViolation`: the scenario ran, but the run
did not match its declared expectation.

### `ToolFatalError`: a tool that cannot succeed on retry

Raise it from your own tool when retrying cannot help: the target resource is gone and cannot be
created, permission is denied, a service the tool depends on is down. Whether an error is fatal is
the tool author's decision.

Every `SyndicaError` a tool raises, `ToolFatalError` included, propagates to the agent loop: the
agent's cycle stops with `StopReason.ERROR` and an `agent_error` event carries the three parts,
because they are written for the programmer, not the model. Any other exception from a tool becomes
output the model reads, so it can try another way. So for invalid arguments the model supplied, a
timeout or a busy resource, return a `ToolResult` that explains the problem (or raise an ordinary
exception), not a `SyndicaError`.

### `LLMResponseError`: the model's reply cannot be used

Raised by an `LLM` implementation when a reply is not a valid result: malformed, missing required
parts, or an HTTP error status (`ProviderHTTPError` from `syndica.backends.llm_openai`; pass it to
`recoverable(error)` and `retry_after(error)` from the same module to learn whether waiting may
help). A failed LLM call fires the `llm_failure` junction, where the `retry` Module retries it.

### `ContextOverflowError`: the request is larger than the context window

Raise it from an `LLM` implementation when the provider rejects a request for exceeding the
context window. It fires the `context_overflow` junction, where compaction can shrink the context
and retry.

### `PayloadRefused`: a trace store declines one payload

Raise it from a `TraceStore` that cannot accept one free-text payload (too large for a single item)
while the store itself is healthy. The recorder drops that payload and keeps writing. Any other
exception from a store means the store is broken, and the recorder stops writing to it.

### `OtlpExportError`: the collector did not accept an export

Raised by `OtlpHttpExporter` when the collector is unreachable, rejects the data, accepts only part
of it or times out. `resendable` tells you whether sending the same trace again is safe.

### `WriteConflict`: a versioned write lost a race

Raised by a write that carries a version precondition (`Portal.write_text(..., expected=...)` and
friends) when the file is no longer at that version. It is deliberately not a `SyndicaError`: it is
an expected, recoverable outcome of optimistic concurrency, not a mistake. Read the file again and
decide whether to write.

## Errors from third-party code

Exceptions raised by a Module's factories, hooks, services or lifecycle callbacks propagate
unchanged, with a note naming the Module and the step, so an embedding host can still catch them by
type. Exceptions from your own hooks on an observable junction are logged and the run continues; on
an interceptable junction they stop the agent's run.
