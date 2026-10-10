# Model providers

The CLI and the server build their models from environment variables. Three platforms are
supported, all through the OpenAI-compatible client in the `openai` extra; none needs another SDK.

| Platform | Selected when set | Variables |
| --- | --- | --- |
| `openai` (any OpenAI-compatible endpoint) | `OPENAI_BASE_URL` | `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY` (optional) |
| `gemini` (Google, through its OpenAI-compatible endpoint) | `GEMINI_API_KEY` or `GEMINI_MODEL` | `GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-3.5-flash-lite`) |
| `openrouter` | `OPENROUTER_API_KEY` or `OPENROUTER_MODEL` | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` (default `openrouter/free`) |

Keep API keys in environment variables. `.env` files are ignored by Git and are not loaded
automatically. Do not keep a key file inside a workspace that agents operate on: `Read` and `Grep`
can read it and send it to the model provider.

## Any OpenAI-compatible endpoint

```bash
export OPENAI_BASE_URL=https://your-endpoint.example/v1
export OPENAI_MODEL=your-model
export OPENAI_API_KEY=your-key
```

`OPENAI_API_KEY` is optional for endpoints that do not require one (a local vLLM or Ollama, for
example). Because this endpoint may be a paid one, it is used for the default rotation only when
neither Gemini nor OpenRouter is configured, or when `SYNDICA_LLM_ORDER` names it.

## OpenRouter free models

```bash
export OPENROUTER_API_KEY=your-openrouter-key
export OPENROUTER_MODEL=openrouter/free
```

To keep inference free, Syndica accepts only `openrouter/free` or a model id ending in `:free` while
OpenRouter is used, whichever way the name reaches it. The free router picks a model that supports
the request's features, including tool calling. For a first multi-agent run, a specific free model
that advertises tool support is often easier to diagnose than the router:

```bash
export OPENROUTER_MODEL=cohere/north-mini-code:free
```

## Gemini

```bash
export GEMINI_API_KEY=your-gemini-key
export GEMINI_MODEL=gemini-3.5-flash-lite
```

Google bills the project a key belongs to, and the key does not reveal which kind of project that
is. A project without billing uses only the free tier and answers HTTP 429 once a quota runs out;
a project with billing enabled is charged for every request. Use a key from a project without
billing if inference must stay free.

## Several platforms and failover

Models are always called through a rotation. With one platform configured, a failed request is
retried on that platform after a backoff. With several, every role whose manifest leaves `model` as
`default` (all built-in roles do) shares one rotation across them:

- A request goes to the first platform that is not backing off. If it fails, the same request goes
  to the next one right away, and the failed platform backs off: about 2 seconds after its first
  consecutive failure, then 4, 8, and so on up to 5 minutes, each with ±20% jitter.
- When the platform says how long to wait (`Retry-After`, OpenRouter's `X-RateLimit-Reset`, or
  Google's `RetryInfo`), the longer wait wins, so a platform whose daily quota ran out is skipped
  until the quota resets.
- A success clears that platform's backoff, and the first platform takes over again as soon as its
  backoff ends.
- When every platform is backing off, the request waits for the earliest one to recover if that
  happens within 2 minutes of the request starting. Failures that waiting cannot fix are not waited
  for: any HTTP 4xx other than 408, 425 and 429, such as a bad key or an unknown model. If nothing
  worth waiting for recovers in time, the request fails, listing each platform's last error and
  when it will be tried again.
- Errors another platform cannot fix (configuration errors, contract violations, context overflow)
  are raised immediately.
- Every platform failure is logged as a warning on stderr. Without it, a primary platform that
  keeps failing would go unnoticed while the fallback quietly spends its own quota.

The default order is Gemini, then OpenRouter, limited to the ones you configured. Set
`SYNDICA_LLM_ORDER` to choose the platforms and their order; `openai` names the generic `OPENAI_*`
endpoint:

```bash
export SYNDICA_LLM_ORDER=openrouter,gemini,openai
```

Every platform named there must be configured, each may appear once, and unknown names are errors.

A run can move between platforms mid-conversation. Gemini 3 rejects a conversation whose earlier
function calls lack their thought signature, so Syndica returns each signature only to the
endpoint (base URL and model) that produced it, and sends function calls answered by another
platform with the placeholder signature Google documents for that case.

## A model per role

A role whose manifest names a model leaves the rotation and calls one platform. Write the model as
`<platform>:<model>`, where the platform is `gemini`, `openrouter` or `openai`:

```yaml
version: 1
agents:
  - name: Researcher
    description: Use me to gather sources and summarise them with citations.
    system_prompt: Gather sources and cite them.
    tools: [Read, Grep, Glob]
    model: gemini:gemini-3.8-flash
  - name: Coder
    description: Use me to change code and run the tests.
    system_prompt: Change code and run the tests.
    tools: [Read, Write, Edit, Bash]
    model: openai:qwen3:8b
```

- `<platform>:default` is that platform's default model from the environment.
- The name must be ASCII and is split at its **first** colon. A name with a `/` or `.` before its
  first colon, such as `cohere/north-mini-code:free`, is a plain model name: it goes to OpenRouter
  when OpenRouter is configured, otherwise to `OPENAI_*`.
- Any other word before the first colon is an error that names the fix, so a tagged model name like
  `qwen3:8b` has to be written `openai:qwen3:8b`.
- A manifest comes from the project's configuration, and the generic `OPENAI_*` endpoint may be
  paid. A manifest therefore reaches it only when your own environment chose it: `openai` is listed
  in `SYNDICA_LLM_ORDER`, or neither Gemini nor OpenRouter is configured.
- `acp:<name>` and `a2a:<name>` make a role a hosted ACP agent or a remote A2A agent; see
  [Interop](interop.md).

## Quotas

Free tiers limit requests, and every agent turn is one request. OpenRouter allows a limited number
of free-model requests per day; Gemini limits requests per minute and per day for each model (your
limits are listed in Google AI Studio, and daily quotas reset at midnight Pacific time). Delegated
agents run their own turns, so keep `--max-turns` small while you experiment: it is the budget of
the whole agent tree, and each delegated agent gets a share of what its parent has left.

How to read 401, 429 and 400 failures is in
[Troubleshooting](../troubleshooting.md#model-provider-errors).
