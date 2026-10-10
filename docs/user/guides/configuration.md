# Configuration

A project configures Syndica with a `.syndica.yaml` file in the directory you run `syndica` from.
Without one, the built-in roles are used. `--config <path>` before the subcommand selects another
file:

```bash
syndica --config review.yaml run "review the draft" --topology review
```

The file is parsed with safe YAML, must be one mapping with `version: 1`, and rejects duplicate
keys and unknown fields rather than silently ignoring them. Every key is listed in the
[configuration schema](../reference/configuration.md).

## An example

```yaml
version: 1

agents:
  - name: Reviewer
    description: Use this agent to review a completed artifact before handoff.
    system_prompt: {file: prompts/reviewer.md}
    tools: [Read, Grep, Glob]
    budget:
      turns: 20

modules: [acceptance]
flags:
  acceptance_outcome: true

danger:
  roles:
    Reviewer: {rules: {shell.path_outside_workspace: block}}

topologies:
  review:
    slots:
      - name: root
        agent: Reviewer
        is_root: true
        cardinality: singleton
        boot: eager
        exit_mode: zombie
```

## Roles

`agents:` declares roles, or replaces built-in ones by reusing their name. See
[Writing an agent](writing-an-agent.md).

`system_prompt` is either the prompt text or `{file: <path>}`: a file next to the configuration
(no absolute paths, nothing outside its directory); a missing file is an error. A plain string that
names an existing file is read as well, so a typo in it would silently become the prompt; the CLI
warns when a single-word string that looks like a path names no file. Prefer `{file: ...}`.

`model` defaults to `default`, the rotation built from your environment. Name a platform and model
as `<platform>:<model>` to pin a role to one platform; see [Model providers](models.md).

## Confirming a project configuration

A `.syndica.yaml` inside the workspace is **untrusted input**: an agent can edit it, and it decides
which commands run and which code loads. `run`, `chat` and `doctor` therefore ask you to confirm it
the first time and after every change. The question shows the prompt files, `impl:` classes,
verify commands, `pass_env`, models, modules, flags, safety settings and interop targets it would
use; nothing in it runs before you answer `y` on a terminal.

- The confirmation covers the bytes of the configuration and of every prompt file it reads, and
  the whole package of an `impl:` class kept inside the workspace, including directories linked
  into it. Code installed elsewhere is named and its location recorded, but its contents are not
  covered.
- A file or package counts as inside the workspace if its path is inside either as written or
  after following symlinks, so a `.syndica.yaml` that is a symlink to a file elsewhere is still
  confirmed.
- Confirmed code is checked again when it is imported: a module the confirmed package imports
  later in the run is refused if it changed since you confirmed, or did not exist then.
- Where no terminal is available, review the file and run the command printed in the refusal:

```bash
syndica config trust --sha256 <digest>
```

Confirmations are stored in `$XDG_CONFIG_HOME/syndica/trusted.json` (default
`~/.config/syndica/trusted.json`, or the file named by `SYNDICA_TRUST_STORE`), never inside the
workspace. A configuration outside the workspace, passed with `--config`, needs no confirmation.

The confirmation cannot cover code that runs before it. If Syndica itself is installed in a
virtual environment inside the workspace, an agent can write `.pth` files and installed code there
that run at the next start; the confirmation lists such `sys.path` entries so you can see them.
Install Syndica outside the workspaces agents operate on.

## Modules and flags

`modules:` enables optional modules, installed in the order written. The CLI always installs
`safety` and `human-interaction` first. The built-in optional modules are `retry`, `prefix-drift`,
`compaction`, `memory`, `acceptance`, `long-horizon` and `background`; a third-party module is named
by its entry-point name (see [Writing a Runtime Module](writing-a-module.md)).

```bash
syndica modules list
syndica modules show acceptance
```

`modules list` shows which modules exist and where they come from without importing any of them,
and marks those whose code is inside the workspace. `modules show` imports one to print its
manifest; when its code is inside the workspace, the configuration must enable it and be confirmed
first.

`flags:` overrides flag defaults for the run, and `--flag key=value` on the command line overrides
`flags:`. Both are checked against the declarations of the installed modules. **A project
configuration can only tighten**: for example `untrusted_high_risk` accepts only `reject` there.

```bash
syndica flags list
syndica flags show compaction_strategy
syndica run "summarise the notes" --flag compaction_strategy=rule_based
```

`flags list` and `flags show` print the flags of the modules a configuration installs, the value in
effect and where it comes from. Every built-in flag is in the [flag reference](../reference/flags.md).

## Workspace checks

The `workspace:` section is used by [development mode](development-mode.md) and `doctor --verify`:

```yaml
version: 1
workspace:
  verify:
    - uv run ruff format --check .
    - uv run pytest -q
  protected: [pyproject.toml, "scripts/**"]
  pass_env: [NO_COLOR]
  versions: true
```

| Key | Meaning |
| --- | --- |
| `verify` | Commands the host runs after the agents finish in `run --handoff`, and that `doctor --verify` runs. |
| `protected` | Relative globs whose changes are highlighted and never committed unattended. |
| `pass_env` | Extra environment variable names agents may see, beyond the fixed allowlist. Secret-looking names are refused. |
| `versions` | The version layer and per-agent isolation (default `true`). |

`.syndica.yaml` is code you run: `run --handoff` and `doctor --verify` execute `workspace.verify`
on your machine without danger checks, so using them in a repository you do not trust is the same
as running its test scripts.

## Other sections

| Section | See |
| --- | --- |
| `topologies:` | [Topologies](../concepts/runtime-and-agents.md#topologies) |
| `danger:` | [Tightening per role](../concepts/danger.md#tightening-per-role) |
| `redaction:` | [Redaction](../concepts/traces-and-redaction.md#redaction-happens-before-writing) |
| `trace:` | `trace: {level: structure}` sets the default trace level; `--trace` overrides it. See [Traces](../concepts/traces-and-redaction.md#levels). |
| `prices:` | [Cost](../concepts/budgets-and-long-horizon.md#cost) |
| `interop:` | [Interop](interop.md) |

A project configuration cannot choose where traces are exported: an OTLP endpoint set there would
send your traces and collector credentials to whoever wrote the repository. Keys that look like an
export setting are refused with an explanation. See [Observability](observability.md).
