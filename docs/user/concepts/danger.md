# Danger judgement and dispositions

Syndica separates **deciding that a call is risky** from **deciding what to do about it**.

- The **danger judge** is pure rules. For a tool call it returns a stable rule ID, a risk level
  (`none`, `low` or `high`) and a reason. It reads no configuration, calls no model and knows
  nothing about how the host interacts with people.
- The **disposition** belongs to the host: `allow`, `confirm` (ask a person) or `block`. A batch
  job should block what an interactive session would ask about; the same rules serve both.

An approval covers **one call only**. There is no "allow from now on": in a run of hundreds of
turns, a broad approval would end up used in a completely different context.

## Rules

The default rules, all high risk:

| Rule ID | Matches |
| --- | --- |
| `shell.privilege_escalation` | Running something with `sudo` or `doas`. |
| `shell.remote_script_execution` | Content downloaded from the network handed straight to an interpreter. |
| `shell.download_then_execute` | Downloaded content written to a file and executed in the same command. |
| `shell.recursive_force_remove` | Recursive, forced deletion. |
| `shell.raw_device_write` | Writing a block device or recreating a file system. |

Optional rule families the CLI adds:

| Rule ID | Matches |
| --- | --- |
| `shell.git_state_change` | Git subcommands outside the read-only set, `-c` / `--git-dir` style global options, `GIT_` assignments, any path under `.git/`. |
| `shell.path_outside_workspace` | Arguments or redirect targets outside the workspace (absolute paths, `~`, `$HOME`, `..` that escapes). |
| `shell.workspace_root_removal` | `rm` whose target is the workspace root or everything under it. |
| `mcp.external_tool` | Calls to MCP tools; high or low risk per the server's `high_risk` / `low_risk` lists or the tool's read-only hints. |

The rules read the **command text**. Commands handed to a shell's `-c`, to `eval`, to `xargs` or to
`find -exec` are read as commands too, up to eight levels deep (deeper counts as matched), and
wrappers such as `env`, `nice`, `nohup`, `setsid`, `timeout`, `sudo`, `command`, `exec` and launchers
such as `uv run` are seen through. A command inside a script, a variable, `python -c`, or a program
the rules do not know (`su -c`, `watch`, `ssh`, `make`) is not recognised. **No rule matching does
not mean a command is safe**; it means no current rule matched.

## The CLI's disposition table

Every CLI run uses a development table:

| Rule | Disposition |
| --- | --- |
| `shell.privilege_escalation` | `block` |
| `shell.remote_script_execution` | `block` |
| `shell.download_then_execute` | `block` |
| `shell.recursive_force_remove` | `block` |
| `shell.raw_device_write` | `block` |
| `shell.git_state_change` | `block` |
| `shell.workspace_root_removal` | `block` |
| `shell.path_outside_workspace` | `confirm` |
| `mcp.external_tool` | `confirm` |

On top of the table, the CLI blocks Git commands that change state and `rm` of the workspace root
**unconditionally**, before any policy or flag is consulted, so no setting can allow them. File
tools refuse to write inside `.git/`. Commits and pushes happen only through the host, after a
person chooses to (see [development mode](../guides/development-mode.md)).

A high-risk call that can be traced to untrusted content never reaches this table: the
`untrusted_high_risk` flag decides it. See [Trust and provenance](trust-and-provenance.md).

## Tightening per role

A project's `.syndica.yaml` can make a role stricter than the development table, never looser:

```yaml
version: 1
danger:
  roles:
    Worker:
      rules: {shell.path_outside_workspace: block}
    Explorer:
      policy: block_high_risk
```

`rules` raises single rules to `confirm` or `block`; `policy: block_high_risk` blocks every
high-risk rule. `allow`, `allow_all`, and anything looser than the development table are rejected.
Replay rebuilds the same table for each role.

## Confirmations

In the CLI a confirmation is a `[y/N]` question on the terminal; anything but `y` refuses. In the
web UI it is a card in the session. When no authenticated page has been connected to the server
for 30 seconds, pending confirmations are rejected so an unattended agent can move on. Over ACP the
editor is asked; other protocol clients cannot answer a confirmation, so it is rejected unless a
person is watching the session in the web UI.

## The agents' environment

Commands run with an allowlisted environment: `PATH HOME USER LOGNAME LANG LC_ALL LC_CTYPE TMPDIR
XDG_CACHE_HOME UV_CACHE_DIR`, the names listed in `workspace.pass_env`, and six Git and `gh`
settings the host adds so that agents cannot use your credential helpers, aliases, hooks or `gh`
login. A name that looks like a secret is refused even in `pass_env`: one of its `_`-separated
words is `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `AUTH`, `SESSION` or a similar word, or it ends in
`KEY`, `TOKEN`, `SECRET`, `PASSWORD` or `PASSWD`. `SSH_AUTH_SOCK` counts, so agents get no ssh
agent; so does `MONKEY_MODE`.
