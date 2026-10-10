# Security Policy

## Supported versions

| Version | Security fixes |
| --- | --- |
| 1.0.x (once released) | Yes |
| 1.0.0 release candidates | Until 1.0.0 is released; fixes land in the next candidate |
| Earlier builds | No; they were never published |

After 1.0, security fixes go to the latest minor release of the current major version.

## Reporting a vulnerability

**Do not open a public issue.** Report privately through GitHub's private vulnerability reporting:
open the repository's **Security** tab and choose **Report a vulnerability**
(<https://github.com/ai-loren/syndica/security/advisories/new>). Only the maintainers can see the
report.

Please include the Syndica version (`syndica --version`), the operating system, the configuration
involved (with secrets removed), and the steps or a script that reproduce the problem. Secrets you
send by mistake are treated as compromised: rotate them.

We acknowledge the report in the advisory, keep you updated there, and agree on a disclosure date
with you. Fixes are released first, and the advisory is published with credit to you unless you
prefer otherwise.

## Security model

Syndica lets language-model agents read and write files and run commands. It is built for one
user on a machine or container that user owns, with the user's own projects and prompts. Read the
limits below before pointing it at input you do not trust.

### What Syndica does not isolate

Syndica provides no multi-tenant isolation. Every session, agent and module in one Syndica process
or workspace shares the same operating-system user, file system and network; do not serve several
mutually untrusting people or tenants from one installation.

Third-party code that runs in-process, such as Runtime Modules and backends, cannot be sandboxed.
It runs with every permission of the host process: it can read the host's environment, files and
credentials. Syndica treats the *output* of third-party modules as untrusted, but it cannot stop
the module's code. Install a Runtime Module or backend only if you would run the same code
directly. A project's `.syndica.yaml` is code you run as well: Syndica asks you to trust it
(`syndica config trust`) before using it, and `workspace.verify` commands run on your machine
without danger checks.

### The danger judge is a guard against mistakes

The danger judge is a guard against mistakes, not an adversarial sandbox. It is a set of pure rules
that classify a tool call's risk from its text; the host then blocks it, asks a person, or lets it
run. A confirmation allows only the one call it was asked for. The rules read command text, so a
command inside a script, a variable, `python -c`, or a program the rules do not know is not
recognised, and "no rule matched" never means "safe". An agent that has been persuaded by injected
content can write a script that looks legitimate.

The unconditional block on Git writes and on removing the workspace root works the same way: it
stops an agent's mistakes, not an agent trying to evade it. Commits and pushes are made by the host,
only after a person chooses to.

Two mechanisms make the judge stricter when content is not trustworthy. Content that arrives from
commands, external tools and protocols (MCP, ACP, A2A, OpenAI-compatible HTTP) and third-party
modules is marked untrusted, and the marker survives summaries, compaction and memory. A high-risk call that can be traced to untrusted content is escalated: an agent cannot
authorise it by itself, and with nobody watching it is rejected rather than left waiting.

**The real isolation boundary is a sandboxed workspace backend.** The kernel defines the `Workspace`
/ `Portal` protocol and its contract suite; a sandbox is a backend that implements it. The local
workspace that ships with Syndica is not a sandbox: code an agent writes or runs there can read any
file your user can read, reach the network, and use credential files under your home directory.

### Secrets and redaction

- **Agent environment.** Agents' commands see only an allowlist of variables (`PATH`, `HOME`,
  locale settings and a few others), plus the names listed in `workspace.pass_env`. A name that looks
  like a secret is refused even when it is listed there, so agents get no tokens and no ssh agent.
  Git and `gh` run with a per-run configuration, without your credential helpers, hooks or login.
- **Redaction before writing.** Everything written to a trace, a handoff file or a commit message,
  and everything exported (training data, OTLP), passes through the kernel `Redactor` first. It
  replaces the values of secret-looking variables from the host environment, built-in credential
  formats (private keys, `Authorization` headers, URL passwords, common token formats) and your own
  `redaction.rules` with `[REDACTED:<rule>]`.
- **The exception is the model provider.** Requests sent to the model provider are not redacted:
  the model has to see the content it works on. Do not put secrets in files or prompts an agent
  will read.
- **Redaction is pattern based.** It catches the secrets it can recognise; a secret in a format it
  does not know and that is not in the host environment is not caught. The agent's file tools cannot
  reach `.syndica/traces` or `.syndica/handoffs`, but its shell can unless the workspace backend
  sandboxes it.
- **Interop credentials** come only from the user-level `interop.yaml` (mode `0600`, outside the
  workspace, variable names only), and each credential is pinned to the URL or command it may be
  sent to. A project's `.syndica.yaml` can name external agents and servers but never a secret.

### The local server

`syndica up` listens on `127.0.0.1` only, checks the `Host` and `Origin` headers, sends a strict
Content Security Policy, and logs in through a one-time link whose token lives only in that server
process. It refuses to start inside an agent's command environment, which stops mistakes but not a
determined agent running as the same operating-system user. The terminal in the web UI runs in the
agents' environment and is not a sandbox. There is no remote mode; forward the port over SSH to use
it from another machine.

## What we treat as a vulnerability

Examples that are in scope:

- a secret from the host environment, or in a built-in credential format, reaching a trace, a
  handoff file, a commit or an export unredacted;
- an agent gaining a capability through the host that it does not have itself, for example the host
  running workspace code with the host's environment or credentials;
- a lost untrusted marker that lets a high-risk call skip the escalation described above;
- an interop credential sent to a destination other than the one it is pinned to;
- authentication or origin checks of the local server being bypassed from another origin or
  another machine.

Bypasses of the danger judge's rules or of the Git-write block, by writing a command in a form the
rules do not read, are welcome as ordinary bug reports: those guards are documented as protection
against mistakes, not as a security boundary.
