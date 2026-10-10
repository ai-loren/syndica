# Security model

Syndica runs a system that can execute arbitrary commands, read and write files, reach the network,
and run unattended for a long time. A security claim without a threat model is more dangerous than
none, because people assume it protects them. This page is the summary; how to report a
vulnerability is in the project's
[security policy](https://github.com/ai-loren/syndica/blob/main/SECURITY.md).

## The premise

**Syndica runs on your own machine or in your own container, with your own permissions.** It is not
a multi-tenant service and gives no sandbox-level isolation guarantee.

## What it defends against

| Threat | Main mechanisms |
| --- | --- |
| Prompt injection leading an agent to do something nobody asked for | [Trust and provenance](concepts/trust-and-provenance.md): untrusted content is labelled, the label survives summaries, compaction and memory, and high-risk calls traceable to it are escalated (rejected by default). |
| Dangerous operations executed by mistake | The [danger judge](concepts/danger.md) with host dispositions; one-shot confirmations; unconditional blocks on Git writes and deleting the workspace root; Git commits and pushes only through the host after a person chooses. |
| Credentials leaking into anything persisted | One [redactor](concepts/traces-and-redaction.md#redaction-happens-before-writing) before every write and export (traces, sessions, memory, artifact versions, handoffs, commits, OTLP, training data); an allowlisted environment for agents' commands; credentials for external agents only from a user-level file with pinned destinations. |
| Unintended side effects of third-party packages | Explicit loading (installing is not enabling), capability declarations shown for review, a confirmation that covers workspace code, third-party output marked untrusted. |
| Untrusted project configuration | A `.syndica.yaml` inside the workspace runs nothing before you confirm it; a project can only tighten safety settings and cannot choose where traces or credentials go. |

## What it does not defend against

- **Multi-tenant isolation.** Not a goal. Isolate users with containers or virtual machines.
- **Same-process third-party code.** Python cannot sandbox code running in the same process.
  Runtime Modules and backends run with your full environment; capability declarations are for
  review, not enforcement. Put capabilities that need isolation into a backend's separate
  execution environment.
- **Commands the rules cannot read.** The danger rules read command text; a command inside a
  script, a variable, `python -c`, or an unknown program is not recognised. No rule matching does
  not mean a command is safe.
- **A persuaded agent with the local workspace.** Code an agent writes or runs can read any file
  your user can read, reach the network, read the host process's environment and your login
  keychain, and use credential files under `HOME`. Processes that call `setsid` escape
  process-group cleanup. **The real isolation boundary is a sandboxed workspace backend.** With the
  built-in local workspace, run agents only on input you trust.
- **You attacking your own machine**, the model's own alignment, and dependency supply-chain
  security (use lock files and auditing tools).

## Deliberate trade-offs

- **Unattended runs that read untrusted content cannot do some high-risk operations.** Refusing is
  more useful than waiting for a person who is not there.
- **Redaction beats replay.** A replayed step that needed a real credential gets a placeholder and
  fails.
- **Approvals cover one call.** There is no "allow from now on".
- **The web server listens only on `127.0.0.1`** and has no remote mode; forward the port over SSH.
  Its token stops mistakes, not a determined process running as the same OS user.

## Practical advice

- Install Syndica outside the workspaces agents operate on, so agents cannot change the code that
  runs before the trust confirmation.
- Keep API keys in your environment, never in files inside a workspace.
- Review a project's `.syndica.yaml` before confirming it: `workspace.verify` commands run on your
  machine without danger checks.
- Use [development mode](guides/development-mode.md) only on repositories and prompts you trust.
