# Writing a backend or store

The kernel depends only on protocols. Its default implementations are in-memory or file based and
have no dependencies; a deployment can replace any of them without changing the kernel. "Replaceable"
means nothing without proof, so the kernel **publishes contract suites**: test classes that every
implementation, built-in or third-party, must pass.

Where things belong:

| You want to support | Implement | Ship it as |
| --- | --- | --- |
| A new execution environment (container, SSH, hosted sandbox) | `Workspace` and `Portal` | A backend package |
| A new model provider | the `LLM` protocol | A backend package; the kernel knows no vendor |
| A different storage backend | a store protocol | A backend package, passing its contract suite |
| A new artifact format | `ArtifactHandler` | A backend package |
| A different long-run paradigm | `LongHorizonStrategy` | A Runtime Module, passing its contract suite |

Backends depend on the kernel only, never on each other, and vendor SDKs live in backends, never in
the kernel.

## Store protocols

| Protocol | Holds | Built-in implementations |
| --- | --- | --- |
| `AgentRegistry` | Serialisable agent state: id, role, status, parent, children, flags, metadata | `InMemoryAgentRegistry` |
| `MessageBroker` | Mailboxes, waiting, correlation, on-demand activation | `InMemoryMessageBroker` |
| `SessionStore` | Persisted root conversations | `InMemorySessionStore`, `FileSessionStore` |
| `TraceStore` | Traces, payloads and replay documents (accepts only redacted text) | `InMemoryTraceStore`, `FileTraceStore` |

Contracts that matter most for a distributed implementation:

- **Agent status changes are compare-and-set**, never plain assignment. Two processes changing the
  same agent's status at once must not lose a transition. `update()` is for fields with a single
  writer; collections several parties write need their own atomic operations.
- **`take_by_correlation` matches a correlation id**, not an arbitrary predicate, so it can be pushed
  down to a remote queue (for example Redis Streams consumer groups).
- **`on_missing_queue`** is the hook for lazy start and reviving zombies; it must run in the
  context of the `publish` that found the queue missing.
- **Sessions can be tens of megabytes**: existence checks use `exists()`, metadata reads use
  `load_meta()`, and archive numbers only grow (always the current maximum plus one).
- **The trace store's write side accepts only `RedactedText`**, which only the kernel's redactor
  produces, so nothing unredacted can be persisted through it.

The memory, rubric and artifact stores have protocols and contract suites in the package too, but
they are **provisional** in 1.0: not part of the public API, and they may change.

## Running a contract suite

Subclass the suite and provide its fixture. The suites are async, so they need pytest-asyncio with
`asyncio_mode = "auto"` (or another async plugin marking each test); without it, async tests would
be skipped silently, so every suite fails loudly instead.

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

```python
import pytest

from syndica.kernel import InMemorySessionStore, SessionStore
from syndica.kernel.contracts import SessionStoreContract


class TestMySessionStore(SessionStoreContract):
    @pytest.fixture
    def store(self) -> SessionStore:
        return InMemorySessionStore()  # return your implementation here
```

| Suite (`syndica.kernel.contracts`) | Fixture to provide |
| --- | --- |
| `AgentRegistryContract` | `registry` |
| `MessageBrokerContract` | `broker` |
| `SessionStoreContract` | `store` |
| `TraceStoreContract` | `store` |
| `WorkspaceContract` | `workspace` (async; yield the workspace and close it afterwards) |

The long-horizon strategy suite is `LongHorizonStrategyContract` in
`syndica.kernel.contracts.long_horizon`, with a `strategy` fixture.

A suite pinned to the published contract is the proof a backend gives its users. Run it in your own
CI on every change; it needs no model and no API key.

## Workspaces

A workspace backend implements both protocols: `Workspace` (lifecycle and isolation: where the work
happens, how copies are made and merged) and `Portal` (actions: run a command, stream its output,
read and write files, stat, glob, grep, and pseudo-terminal sessions). The tools need no change:
`Read`, `Bash` or `Terminal` behave the same on a local directory or a remote container.

A sandboxed workspace backend is **the** isolation boundary in Syndica's security model. The
built-in `LocalWorkspace` runs commands as your user on your machine; see the
[security model](../security.md).

## Model backends

The `LLM` protocol is small: a `config` (an `LLMConfig` with a provider id, the model, connection
arguments and default request arguments) and `async create(messages, **kwargs)` that returns the
assistant's `ContextBlock`. The kernel never imports a vendor SDK. Changes to identity fields
(provider, model) go through `LLMConfig.derive()` so cache keys and billing attribution never drift
silently. The bundled OpenAI-compatible client is in `syndica.backends.llm_openai`.

## Artifact handlers

An `ArtifactHandler` declares the `(kind, mime)` pairs it handles (`*` allowed for the MIME type)
and implements `validate`, `preview` and `semantic_diff`, each receiving an `ArtifactReader` to read
the version it is asked about. The more specific registration wins, and two handlers at the same
precision are an error at start-up. The built-in handler is a generic text handler.
