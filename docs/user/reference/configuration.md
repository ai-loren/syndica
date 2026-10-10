# Configuration schema

`.syndica.yaml` is one YAML mapping parsed with safe YAML. Duplicate keys and unknown keys are
errors at every level, never ignored. For how the pieces fit together, see
[Configuration](../guides/configuration.md).

<!-- The test suite checks that every key the configuration loader accepts appears on this page. -->

YAML 1.1 reads unquoted `yes`, `no`, `on`, `off`, `true` and `false` as booleans. Quote such words
when you mean a string; the loader rejects booleans where it expects strings or numbers and says
so.

## Root keys

| Key | Type | Meaning |
| --- | --- | --- |
| `version` | integer | Required; must be `1`. |
| `agents` | list | Role manifests. |
| `topologies` | mapping | Topology name to `{slots: [...]}`. |
| `workspace` | mapping | Host checks and workspace settings. |
| `redaction` | mapping | Extra redaction rules. |
| `trace` | mapping | Default trace level. |
| `danger` | mapping | Per-role tightening of dispositions. |
| `modules` | list | Optional modules to install, in order. |
| `flags` | mapping | Flag values for the run. |
| `prices` | mapping | Model prices for cost budgets. |
| `interop` | mapping | MCP, ACP and A2A objects. |

## `agents`

Each item is a role manifest:

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | required | Role name. Reusing a built-in name replaces that role. |
| `description` | string | required | When to use this role. |
| `system_prompt` | string or `{file: <path>}` | `''` | Prompt text, or a file relative to the configuration's directory. |
| `tools` | list of names | `[]` | Tools the role may use. |
| `subagents` | list of role names | `[]` | Roles it delegates to. |
| `model` | string | `default` | `default`, `<platform>:<model>`, `acp:<name>` or `a2a:<name>`. |
| `extends` | role name | none | A class-based role to build on. |
| `skills` | list of names | `[]` | Recorded only; no skill loader in v1.0. |
| `budget` | `{turns: <int or null>}` | 50 turns | Turn cap for this agent; `null` removes it. |
| `artifact_kinds` | list of names | `[]` | Kinds of artifact the role produces. |
| `impl` | `package.module:Class` | none | Makes the role class-based. |

Two items with the same `name` in one file are an error.

## `topologies`

`topologies: {<name>: {slots: [...]}}`: each topology has one key, `slots`, a list of slots. Each
slot:

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | required | Slot name. |
| `agent` | role name | required | The role that fills the slot. |
| `is_root` | boolean | `false` | The slot a `run --topology` task enters through. |
| `cardinality` | `singleton`, `keyed`, `on-demand` | `on-demand` | How many instances and how they are addressed. |
| `key_field` | string | none | Required for `keyed`, forbidden otherwise. |
| `boot` | `eager`, `lazy`, `manual` | `lazy` | When instances start. `keyed` and `on-demand` cannot be `eager`. |
| `exit_mode` | `idle`, `zombie`, `exit` | `zombie` | What happens after a cycle of work. |
| `extra_tools` | list of names | `[]` | Tools added on top of the role's. |
| `budget` | `{turns: <int or null>}` | none | Budget for the slot's instances. |
| `metadata` | mapping | `{}` | Extra metadata on the instances. |
| `isolation` | `shared`, `worktree`, `snapshot` | `shared` | Which copy of the workspace the instance works on. |

A slot cannot set `system_prompt`, `model`, `impl` or other fields that would replace the role's
behaviour.

## `workspace`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `verify` | list of commands | `[]` | Run by the host after `run --handoff` and by `doctor --verify`. |
| `protected` | list of relative globs | `[]` | Changes here are highlighted and never committed unattended. No absolute paths, no `..`. |
| `pass_env` | list of variable names | `[]` | Extra variables agents may see. Secret-looking names are refused. |
| `versions` | boolean | `true` | The version layer and per-agent isolation. |

## `redaction`

| Key | Type | Meaning |
| --- | --- | --- |
| `rules` | list of `{id, pattern}` | Extra patterns, after the built-in rules. Each item has exactly `id` (starting with `custom.`) and `pattern` (a regular expression that must not match the empty string or a placeholder). |

## `trace`

| Key | Type | Meaning |
| --- | --- | --- |
| `level` | `off`, `structure`, `replay`, `full` | Default trace level; `--trace` overrides it. |

Export endpoints cannot be set here; keys that look like one are refused.

## `danger`

`danger` has one key, `roles`: `danger: {roles: {<role>: {...}}}`. Each role entry has at least one
of:

| Key | Type | Meaning |
| --- | --- | --- |
| `rules` | mapping of rule ID to `confirm` or `block` | Raise single rules. Only rule IDs of the development table, and only stricter values. |
| `policy` | `block_high_risk` | Block every high-risk rule for this role. |

## `modules`

A list of module names, each once, installed in the order written: `retry`, `prefix-drift`,
`compaction`, `memory`, `acceptance`, `long-horizon`, `background`, or a third-party module's
entry-point name.

## `flags`

A mapping of flag key to a boolean, integer or string. Keys must be declared by an installed
module, values must match the declaration, and a project configuration may only tighten. See the
[flag reference](flags.md).

## `prices`

`prices: {<model>: {input, output, cached_input}}`: prices per million tokens for a model name (or
`<platform>:<model>`). `input` and `output` are required; `cached_input` defaults to `input`.
Amounts are non-negative numbers.

## `interop`

| Key | Meaning |
| --- | --- |
| `mcp` | `servers` (MCP servers to call) and `serve` (what `syndica mcp` exposes). |
| `acp` | `agents`: hosted ACP agents. |
| `a2a` | `agents`: remote A2A agents. |

Names of external objects match `[a-z][a-z0-9_-]{0,31}` and are unique within their list.

`interop.mcp.servers` items:

| Key | Meaning |
| --- | --- |
| `name` | Server name; its tools appear as `<name>__<tool>`. |
| `command` | Argument list for a stdio server (never run through a shell). Exactly one of `command` and `url`. |
| `url` | URL of a Streamable HTTP server. |
| `pass_env` | Extra environment variable names for the server process; secret-looking names are refused. |
| `roles` | Roles that get the server's tools. A server no role lists is never started. |
| `high_risk` | Tool names treated as high risk. |
| `low_risk` | Tool names treated as low risk. Must not overlap `high_risk`. |

`interop.mcp.serve`:

| Key | Meaning |
| --- | --- |
| `tools` | Tools `syndica mcp` exposes (default `Read`, `Grep`, `Glob`). |

`interop.acp.agents` items:

| Key | Meaning |
| --- | --- |
| `name` | Referenced by roles as `model: acp:<name>`. |
| `command` | Argument list that starts the agent. |
| `pass_env` | Extra environment variable names for the agent process. |

`interop.a2a.agents` items:

| Key | Meaning |
| --- | --- |
| `name` | Referenced by roles as `model: a2a:<name>`. |
| `url` | The agent's JSON-RPC URL. |

## User-level `interop.yaml`

Next to the trust store (`$XDG_CONFIG_HOME/syndica/interop.yaml`), mode `0600`, outside the
workspace. One root key:

| Key | Meaning |
| --- | --- |
| `credentials` | Mapping of `mcp:<name>`, `acp:<name>` or `a2a:<name>` to an entry. |

Each entry pins a destination and names variables, never values:

| Key | Meaning |
| --- | --- |
| `url` | Pinned URL (A2A agents, HTTP MCP servers). Pairs with `bearer_env`. |
| `bearer_env` | Variable whose value is sent as `Authorization: Bearer`. Over `http://`, only to a loopback host. |
| `command` | Pinned argument list (ACP agents, stdio MCP servers). Pairs with `env`. |
| `env` | Variables passed into that process's environment. |

## Environment variables

Settings that belong to you, not to the project, come from the environment:

| Variable | Meaning |
| --- | --- |
| `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY` | Generic OpenAI-compatible endpoint. |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Gemini. |
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | OpenRouter. |
| `SYNDICA_LLM_ORDER` | Which platforms rotate, in order (`gemini`, `openrouter`, `openai`). |
| `SYNDICA_TRUST_STORE` | Path of the trust store file (default `$XDG_CONFIG_HOME/syndica/trusted.json`). |
| `XDG_CONFIG_HOME` | Base for the trust store, `interop.yaml` and server state (default `~/.config`). |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SDK_DISABLED`, `OTEL_TRACES_EXPORTER`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | OTLP export. |

See [Model providers](../guides/models.md) and [Observability](../guides/observability.md).
