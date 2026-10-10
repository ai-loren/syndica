# HTTP API reference

`syndica up` serves this API on `127.0.0.1`. The authoritative description is the reviewed OpenAPI
snapshot,
[`docs/reference/openapi.json`](https://github.com/ai-loren/syndica/blob/main/docs/reference/openapi.json),
generated from the server's models; every change to it is recorded with its hash and whether it is
breaking in the maintainers'
[API change log](https://github.com/ai-loren/syndica/blob/main/docs/reference/api-changes.md) (in
Chinese). The snapshot's own descriptions are in Chinese in v1.0; this page is the English summary.

<!-- The test suite checks that every path in docs/reference/openapi.json appears on this page. -->

Fields marked `x-syndica-stability: provisional` in the snapshot come from kernel types whose shape
may still change.

## Authentication

Requests carry `Authorization: Bearer <token>`. The browser gets its token by exchanging the
one-time login code printed by `syndica up`; machine clients use the token printed by
`syndica up --api-token`. Tokens live only in the server process and are invalid after a restart.
The server checks `Host` and `Origin` on every request.

## REST

| Method | Path | Auth | Does |
| --- | --- | --- | --- |
| GET | `/api/v1/health` | none | Process nonce and version. |
| POST | `/api/v1/login` | none | Exchange the one-time login code for a session token. |
| GET | `/api/v1/doctor` | bearer | Checks, item by item (missing key, configuration changed). |
| GET | `/api/v1/roles` | bearer | Roles a session can be created with. |
| GET | `/api/v1/sessions` | bearer | Saved and open sessions. |
| POST | `/api/v1/sessions` | bearer | Create a session; returns its snapshot. |
| POST | `/api/v1/sessions/{sid}/resume` | bearer | Open a saved session. |
| POST | `/api/v1/sessions/{sid}/close` | bearer | Close a session (save it and release its lock). |
| DELETE | `/api/v1/sessions/{sid}` | bearer | Close and delete a saved session. |
| GET | `/api/v1/sessions/{sid}/snapshot` | bearer | The session's snapshot, with `as_of`. |
| GET | `/api/v1/sessions/{sid}/events` | bearer | The session's event stream (SSE). |
| GET | `/api/v1/events` | bearer | The global event stream (SSE). |
| GET | `/api/v1/topologies` | bearer | Topologies in the configuration. |
| GET | `/api/v1/traces` | bearer | Traces in the workspace. |
| GET | `/api/v1/traces/{trace_id}` | bearer | The analysis of one trace. |
| GET | `/api/v1/sessions/{sid}/trace` | bearer | This session's trace. |
| GET | `/api/v1/sessions/{sid}/flags` | bearer | The session's resolved flags. |
| GET | `/api/v1/sessions/{sid}/artifacts` | bearer | The artifact index. |
| GET | `/api/v1/sessions/{sid}/artifacts/versions` | bearer | Versions of one artifact (`?id=`). |
| GET | `/api/v1/sessions/{sid}/artifacts/content` | bearer | The raw bytes of one version (`?id=&version=`): always an `application/octet-stream` attachment with a sandbox CSP; the registered MIME type is in `X-Syndica-Mime`. |
| GET | `/api/v1/sessions/{sid}/artifacts/preview` | bearer | A preview of one version (`?id=&version=`). |
| GET | `/api/v1/sessions/{sid}/artifacts/diff` | bearer | The semantic diff of two versions (`?id=&old=&new=`). |
| POST | `/api/v1/sessions/{sid}/artifacts/feedback` | bearer | Feedback on an artifact, stored as a proposed rubric. |
| GET | `/api/v1/sessions/{sid}/rubrics` | bearer | The goal and rubrics. |

Errors carry the three-part message (expected, received, next action) in Chinese.

## Event streams

Both streams are server-sent events. An event id is `<epoch>:<seq>`; reconnect with
`Last-Event-ID` to resume. A `reset` frame (no id, data `{}`) means the client must reload the
snapshot. Every event type and the keys its payload always has are listed under
`x-syndica-events` in the snapshot: agent lifecycle (`agent_spawned`, `agent_start`, `agent_end`,
`agent_error`, `agent_status`, `agent_removed`), turns and tool calls, messages, confirmations and
questions, artifacts, budget, progress, drift, compaction, external activity, and session open and
close.

## WebSockets

| Path | Purpose |
| --- | --- |
| `/api/v1/sessions/{sid}/rpc` | Typed requests: `auth` (first frame), `send`, `answer`, `abort`, `grant`, `budget_answer`, `activate_rubric`. |
| `/api/v1/sessions/{sid}/pty` | The human terminal: first frame `auth` with the terminal size, then `input` and `resize`; output arrives as binary frames. |
| `/api/v1/sessions/{sid}/agent-pty/{agent_id}/{handle}` | A read-only, redacted mirror of an agent's `Terminal` session. |

A refused WebSocket first receives a reply frame with `ok: false` and a three-part `error`, then
closes with 4401 (not authenticated), 4404 (session not open), 4408 (too many connections waiting
to authenticate) or 1013 (too many authenticated streams). Frame formats are under
`x-syndica-rpc` and `x-syndica-pty` in the snapshot.

## Interop endpoints

| Method | Path | Auth | Does |
| --- | --- | --- | --- |
| GET | `/.well-known/agent-card.json` | none | The A2A agent card: role names and descriptions. |
| POST | `/a2a/` | bearer | A2A JSON-RPC (versions 1.0 and 0.3). Needs the `a2a` extra. |
| GET, POST, DELETE | `/mcp` | bearer | MCP Streamable HTTP. Needs the `mcp` extra. |
| GET | `/v1/models` | bearer | OpenAI-compatible: the roles, as models. |
| POST | `/v1/chat/completions` | bearer | OpenAI Chat Completions. |
| POST | `/v1/responses` | bearer | OpenAI Responses. |

Without the extra, `/a2a/` and `/mcp` answer 501 with the `pip install` line to run. See
[Interop](../guides/interop.md).
