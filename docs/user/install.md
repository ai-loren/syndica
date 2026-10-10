# Install

## Requirements

- **Linux or macOS.** Syndica uses POSIX process groups, `setsid` and pseudo-terminals and has no
  Windows code paths. **On Windows, install and run it inside WSL2.**
- **Python 3.11, 3.12 or 3.13.**
- A model endpoint: any OpenAI-compatible API, Google Gemini, or OpenRouter. See
  [Model providers](guides/models.md).
- Optional: `git` for [development mode](guides/development-mode.md). The web UI ships prebuilt
  inside the package, so you do not need Node.

## Install with pip

Syndica is one distribution, `syndica`, with optional extras. The base package is the kernel and
the CLI with two dependencies (pydantic and PyYAML); everything that talks to the network or needs
a third-party SDK is an extra.

While 1.0 is a release candidate, pip only picks it up with `--pre`:

```bash
python -m pip install --pre 'syndica[openai]'
```

Add the extras you need, separated by commas:

```bash
python -m pip install --pre 'syndica[openai,server]'
```

| Extra | Adds | Needed for |
| --- | --- | --- |
| `openai` | `httpx` | Calling models: the OpenAI-compatible client used for every provider (OpenAI-compatible endpoints, Gemini, OpenRouter). Install it unless you only replay traces. |
| `server` | `starlette`, `uvicorn`, `websockets` | `syndica up`: the web UI, the HTTP API, and the OpenAI-compatible HTTP endpoints. |
| `otel` | `httpx` | Exporting traces to an OTLP/HTTP collector. |
| `mcp` | `mcp`, `jsonschema` | `syndica mcp`, `/mcp` on `syndica up`, and roles that use MCP servers. |
| `acp` | `agent-client-protocol` | `syndica acp` and roles backed by a hosted ACP agent (`model: acp:<name>`). |
| `a2a` | `a2a-sdk` | The A2A endpoint on `syndica up` and roles backed by a remote A2A agent (`model: a2a:<name>`). |
| `trl` | `trl`, `datasets` | Loading exported datasets into TRL's trainers as a dry run. |
| `docs` | `mkdocs`, `mkdocs-material` | Building this documentation site. |
| `dev` | test and lint tools | Working on Syndica itself. |

A command that needs a missing extra stops with a message that names the `pip install` line to run.
See [Troubleshooting](troubleshooting.md#missing-extras).

!!! warning "Install Syndica outside the workspaces agents work in"
    Agents can write files inside their workspace. If Syndica is installed in a virtual environment
    inside that workspace (for example uv's default `.venv` in the project), an agent can plant
    `.pth` files or change installed code that runs at the next start, before any trust question is
    asked. Use a virtual environment or a tool such as `pipx` somewhere else.

A tool installer keeps Syndica in its own environment:

```bash
pipx install --pip-args=--pre 'syndica[openai,server]'
```

## Install from source

You need [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/ai-loren/syndica.git
cd syndica
uv sync --extra openai --extra server
uv run syndica version
```

`uv sync --all-extras` installs every extra, which is what the test suite expects. To use a checkout
from another environment, install it editable:

```bash
python -m pip install -e '.[openai]'
```

## Check the installation

```bash
syndica version
syndica doctor
```

`syndica doctor` checks the configuration, the model settings, the workspace, the environment
agents will see, Git and Python. It works in any directory; without a `.syndica.yaml` it uses the
built-in roles. Continue with the [quickstart](quickstart.md).
