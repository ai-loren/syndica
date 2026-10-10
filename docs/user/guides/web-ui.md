# Web UI and HTTP API

`syndica up` starts a local server for the current workspace: a REST API, server-sent event
streams, a WebSocket for requests, answers and terminals, and a React UI shipped inside the
package (no Node needed). It needs the `server` extra.

```bash
syndica up
syndica status
syndica down
```

| Command | Does |
| --- | --- |
| `syndica up` | Starts the server in the background, prints a one-time login link and opens it in your browser. `--port <n>` picks the port (default: any free one), `--foreground` runs it in the terminal until Ctrl-C, `--no-open` skips the browser, `--api-token` also prints a machine token for API clients. |
| `syndica status` | Says where it runs. Exit code 3 when nothing is running, 1 when the server holds its lock but does not answer. |
| `syndica down` | Saves sessions and stops the server. |

The login link is valid for two minutes and works once. The page keeps its token in the browser and
sends it in a header; the server never sets a cookie. Restarting the server invalidates the token,
so run `syndica down` and `syndica up` again for a new link. A workspace whose `.syndica.yaml` is
not trusted yet is confirmed in the terminal before the server starts.

## What the UI shows

- Sessions: create, resume, close and delete; chat with the root agent or another agent, with
  untrusted content labelled.
- The agent tree and the event timeline.
- Confirmations and questions, answered in place.
- Artifacts with preview, version diff and feedback that becomes a proposed rubric.
- Budget, progress and trace panels.
- A terminal, and read-only mirrors of the agents' terminals.

It also works on narrow screens.

## What the server protects, and what it does not

- It listens on `127.0.0.1` only, checks the `Host` and `Origin` headers, and sends a strict content
  security policy. Artifact bytes are served as sandboxed attachments, never as pages on the
  server's origin. To use it from another machine, forward the port over SSH; there is no remote
  mode.
- `up`, `down` and the server process refuse to run inside an agent's command environment. This
  stops mistakes, not a determined agent running as the same OS user: the browser stores the token
  in its profile directory, which that user can read. The real boundary is a sandboxed workspace
  backend.
- The human terminal runs in the agents' environment (no host keys, a temporary git configuration).
  It is not a sandbox.
- When no authenticated page has been connected for 30 seconds, pending confirmations are rejected
  and pending questions are aborted, so an unattended agent can move on instead of waiting.
- In-flight sub-agents and their mailboxes are not persisted. After `down` or a crash, resuming
  restores the root conversation and says which sub-agents were lost.
- Server state (port, process id) lives next to the trust store, outside the workspace. Credentials
  are never written to disk.

## The HTTP API

Everything the UI does goes through the documented API, so you can script it:

- REST under `/api/v1/`: health, login, doctor, roles, sessions, snapshots, topologies, traces,
  flags, artifacts, rubrics.
- Server-sent events per session and globally, resumable with `Last-Event-ID`.
- A WebSocket for typed requests (`send`, `answer`, `abort`, `grant`, ...) and for terminals.
- The interop endpoints: `/mcp`, `/.well-known/agent-card.json`, `/a2a/`, and the OpenAI-compatible
  `/v1/...` routes (see [Interop](interop.md)).

Except for `/api/v1/health`, `/api/v1/login` (which exchanges the one-time code for a token) and
the A2A agent card, every request carries `Authorization: Bearer <token>`: the browser's session
token, or the machine token printed by `syndica up --api-token`. A WebSocket authenticates with its
first frame.

The routes, payloads, event schemas and WebSocket frames are listed in the
[HTTP API reference](../reference/http-api.md), generated from a reviewed OpenAPI snapshot.
