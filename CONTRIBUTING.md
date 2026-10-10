# Contributing to Syndica

Thank you for helping. This page covers the development setup, the gates every change passes, how
tests are written here, and how commits are written. The constraints that code in this repository
must respect (layering, prefix caching, redaction, concurrency, the danger judge) are in
[`AGENTS.md`](AGENTS.md); read it before changing the kernel. The design and the reasoning behind
each constraint are in the [architecture spec](docs/design/2026-09-03-architecture.md).

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md). Report security problems
privately as described in [SECURITY.md](SECURITY.md), not in a public issue.

## Development setup

Syndica supports Linux and macOS; on Windows, work inside WSL2. You need Python 3.11 or newer,
[uv](https://docs.astral.sh/uv/), and Git. Node 26 is needed only to change the web UI.

```bash
git clone https://github.com/ai-loren/syndica.git
cd syndica
uv sync --all-extras
uv run syndica --version
```

`uv sync --extra openai --extra dev` is enough for the kernel and CLI; `--all-extras` adds the
server, interop and training backends so their tests run too.

## Gates

Every change passes these before it is merged, in this order:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run python scripts/check_deps.py
uv run python scripts/check_links.py
uv run pytest -q
```

- `check_deps.py` enforces the layering: the kernel imports no surface and no backend, surfaces and
  backends do not import each other. A failure here is an architecture problem, not lint.
- Changes under `web/` or to the OpenAPI snapshot also run `sh scripts/check_web.sh` (type
  generation, type check, lint, build, and a byte-for-byte check of the shipped bundle) and
  `sh scripts/check_e2e.sh` (Playwright, desktop and mobile).
- Changes to packaging (`pyproject.toml`, anything shipped in the wheel) also run
  `uv build`, `uv run python scripts/check_dist.py` and `sh scripts/smoke_install.sh`.

## How tests are written

- **Four layers.** Unit and small in-process flows; L2 scenario replay, where a scripted fake LLM
  drives the real runtime (the only way to test failure paths); contract suites that every
  implementation of a Store Protocol or `Workspace` backend must pass; and real-LLM end-to-end
  runs, which are manual. The first three need no API keys, so they run for every pull request,
  including ones from forks.
- **Judge a test by what it catches.** Ask which line of the implementation you could break without
  any test going red, then break it and see. A suite that looks complete often stops only one of
  several realistic mistakes. Collections need at least three elements in an order that is not
  sorted; order-sensitive structures that reach a model request are compared as lists, not only
  with `==`.
- **The test name is the assertion.** `cpu_substring_is_not_treated_as_non_cpu` needs no comment.
- **Long-running behaviour** uses a fake clock, fixed seeds and fault injection, never real waiting.
- **Snapshots are reviewed by people.** When a snapshot test fails, read the diff before deciding
  whether to fix the code or update the snapshot. See the
  [versioning policy](docs/user/versioning.md) for what a snapshot change means.

## Code conventions

- Errors that a user or a caller can see say three things: **what was expected, what was received,
  and what to do next**. Tests assert a word from the third part, not only that an error happened.
- Comments state constraints the code cannot show and what breaks if they are violated. History,
  alternatives you tried, and arguments for the reviewer belong in the commit message.
- Public APIs are fully typed; the package ships `py.typed`.
- Runtime messages, CLI output and code comments are currently written in Chinese; the user
  documentation is in English. Follow the language of the surrounding code.

## Commits and pull requests

- Open pull requests against `main`. Keep one logical change per pull request and add a line under
  `## [Unreleased]` in [`CHANGELOG.md`](CHANGELOG.md) for anything a user would notice.
- **Explain why** in the commit message, especially for code that looks redundant but must stay.
  Many constraints in this repository exist because something broke; the commit message and the
  comment are the only places that knowledge survives.
- **Never list an AI agent or a model as a co-author** (`Co-authored-by:`). If a tool helped, say so
  in a trailer on the last line:

  ```text
  Assisted-by: <tool name>
  ```
