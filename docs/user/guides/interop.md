# Interop: MCP, ACP, A2A and OpenAI HTTP

Syndica speaks four protocols in both directions where it makes sense. All of them land in an
ordinary Syndica session: nothing bypasses the trace, provenance, redaction or danger decisions.

- **Content that arrives through a protocol is untrusted.** There is no switch that marks an
  external tool or agent as trusted.
- **A protocol turn follows the root agent**: a delegated child's answer, end or error never closes
  the turn, and a child's server-side tool call is never handed to an OpenAI client.
- **Version negotiation fails closed**: MCP accepts only the five versions its SDK lists, ACP accepts
  protocol version 1, and A2A accepts only `1.0` and `0.3`. A missing `A2A-Version` header is
  rejected with `-32009` (the SDK's version-not-supported code) rather than treated as `0.3`.
- Protocol errors use the same three-part messages and never echo exception text from inside.

| Protocol | Syndica serves it | Syndica calls out with it | Extra |
| --- | --- | --- | --- |
| MCP | `syndica mcp` (stdio), `/mcp` on `syndica up` | MCP servers listed in `interop.mcp.servers` | `mcp` |
| ACP | `syndica acp` (stdio), for editors | Hosted ACP agents as roles (`model: acp:<name>`) | `acp` |
| A2A | `/.well-known/agent-card.json` and `POST /a2a/` on `syndica up` | Remote A2A agents as roles (`model: a2a:<name>`) | `a2a` |
| OpenAI HTTP | `GET /v1/models`, `POST /v1/chat/completions`, `POST /v1/responses` on `syndica up` | — (model providers are configured separately; see [Model providers](models.md)) | `server` |

## Configuration

A project's `.syndica.yaml` names the external objects; it never contains credentials:

```yaml
version: 1
agents:
  - name: Researcher
    description: Use me when a question needs the remote research agent.
    model: a2a:researcher
  - name: Gemini
    description: Use me for a second opinion from Gemini CLI on a file.
    model: acp:gemini
    tools: [Read]
interop:
  mcp:
    servers:
      - name: files
        command: [npx, -y, '@modelcontextprotocol/server-filesystem', '.']
        roles: [Worker]
        low_risk: [read_file, list_directory]
    serve:
      tools: [Read, Grep, Glob]
  acp:
    agents:
      - name: gemini
        command: [npx, -y, '@google/gemini-cli', --acp]
  a2a:
    agents:
      - name: researcher
        url: 'https://researcher.example/a2a/'
```

`syndica doctor` starts each declared ACP agent for a handshake, fetches each A2A agent card, and
lists every MCP server's tools; an unreachable object gets its own failed line and the rest are
still checked.

## Credentials: the user-level `interop.yaml`

Credentials live in a user-level `interop.yaml` next to the trust store
(`$XDG_CONFIG_HOME/syndica/interop.yaml`, default `~/.config/syndica/interop.yaml`). It must be
outside the workspace and have mode `0600`, and it holds **environment variable names only**, never
values. Each entry pins where its credential may go:

```yaml
credentials:
  a2a:researcher: {url: 'https://researcher.example/a2a/', bearer_env: RESEARCHER_TOKEN}
  acp:gemini: {command: [npx, -y, '@google/gemini-cli', --acp], env: [GEMINI_API_KEY]}
```

- A credential is handed over only when the project declares that object with **exactly** the
  pinned `url` or `command`; otherwise the object does not start. Nothing is normalised (case,
  default ports, trailing slashes), because the client that sends the request might resolve a
  normalised string to another host.
- `url` pairs with `bearer_env` (sent as `Authorization: Bearer`, for A2A agents and HTTP MCP
  servers); `command` pairs with `env` (passed into that process's environment, for ACP agents and
  stdio MCP servers).
- A bearer over `http://` is only sent to a loopback host.
- The project configuration is untrusted, so the address it chooses must not receive your
  credentials by name alone. This is the same reason a project cannot choose the OTLP endpoint.

The project's own `pass_env` lists cannot name secret-looking variables.

## MCP

**As a server**, `syndica mcp` exposes the workspace's tools over stdio, and `syndica up` serves the
same at `/mcp` behind its bearer and host checks. By default only `Read`, `Grep` and `Glob` are
exposed, marked read-only; artifacts are offered as resources and roles as prompts.
`interop.mcp.serve.tools` opens more tools. They still go through the danger judge, and a high-risk
call with nobody to ask is rejected. Logs go to a file outside the workspace so stdout carries only
the protocol, and the command refuses to start inside an agent's command environment.

**As a client**, each server in `interop.mcp.servers` has a `name` and either `command` (stdio, a
list of arguments, never through a shell) or `url` (Streamable HTTP). Its tools appear to agents as
`<server>__<tool>`. A server is started only if some role lists it in `roles`. Results are
untrusted. Calls go through the `mcp.external_tool` danger rule: tools in `high_risk` are high risk,
tools in `low_risk` are low risk, and otherwise a tool is low risk only when it is marked read-only
and not destructive. The CLI's development table confirms these calls.

## ACP

**Editors drive Syndica** with `syndica acp` on stdio. A session's `cwd` must be the workspace the
command was started in; an editor can `session/load` a session after `syndica acp` restarts.
Confirmations are asked in the editor. In a workspace whose configuration is not trusted yet,
`session/new` answers with the `syndica config trust --sha256 ...` command to run, instead of
prompting on stdio.

**Syndica hosts ACP agents** (Gemini CLI, for example) as roles with `model: acp:<name>`. A hosted
agent only gets what its role declares: reading files needs `Read` in the role's `tools`, writing
needs `Write`, a terminal needs `Bash`. `--no-write` and `danger.roles` apply to it like any other
agent, and its answers are untrusted. When the wall clock runs out Syndica sends `session/cancel`
and then stops the whole process group.

## A2A

**As a server**, `syndica up` publishes an agent card at `/.well-known/agent-card.json` (the
workspace name and the roles as skills) and serves JSON-RPC at `POST /a2a/`. An A2A `contextId` is
the Syndica session id: omit it to open a session, send the one Syndica returned to continue it;
any other value gets a failed task saying so. A question an agent asks pauses the task as
`INPUT_REQUIRED`; the next message with the same `taskId` is the answer.

**As a client**, a role with `model: a2a:<name>` sends its turns to the agent declared in
`interop.a2a.agents`. Answers are untrusted; a timeout or cancellation cancels the remote task.

## OpenAI-compatible HTTP

`syndica up` serves `GET /v1/models`, `POST /v1/chat/completions` and `POST /v1/responses`, so
existing OpenAI clients can talk to a Syndica workspace. **The model name is the role name.**

```bash
syndica up --api-token
```

`--api-token` prints a machine bearer token once. It lives only in that server process and is
invalid after a restart.

<!-- not-tested: needs a running `syndica up --api-token`; the port and token come from its output -->
```python
from openai import OpenAI

client = OpenAI(base_url='http://127.0.0.1:PORT/v1', api_key='TOKEN-PRINTED-BY-SYNDICA-UP')
reply = client.chat.completions.with_raw_response.create(
    model='Worker',
    messages=[{'role': 'user', 'content': 'List the files in this workspace.'}],
)
session = reply.headers['X-Syndica-Session']
print(reply.parse().choices[0].message.content)
```

- A request without a session opens one. Send the `X-Syndica-Session` response header back to
  continue it. Responses ids look like `resp_<session>_<n>` and are passed back as
  `previous_response_id`.
- Text, function calls and streaming are supported. Tools the client sends become proxies: when a
  model calls one, it is returned to the client as `tool_calls` (or `function_call`), and the
  client's result in the next request is untrusted. A client tool with the same name as a Syndica
  tool is a 400.
- Sampling parameters (`temperature`, `top_p`, `max_tokens`, ...) are accepted, ignored and listed in
  the `X-Syndica-Ignored` header. Parameters that would change the meaning (`n`, `logprobs`,
  `json_schema`) are a 400.
- A client that disconnects does not cancel a turn that already started.
