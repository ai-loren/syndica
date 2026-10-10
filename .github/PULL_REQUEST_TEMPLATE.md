## What and why

<!-- What changes and why. Put the why in the commit message too: it is what survives. -->

## Gates

- [ ] `uv run ruff format --check .`
- [ ] `uv run ruff check .`
- [ ] `uv run pyright`
- [ ] `uv run python scripts/check_deps.py`
- [ ] `uv run python scripts/check_links.py`
- [ ] `uv run pytest -q`
- [ ] Web UI or OpenAPI snapshot changed: `sh scripts/check_web.sh` and `sh scripts/check_e2e.sh`
- [ ] Packaging changed: `uv build`, `uv run python scripts/check_dist.py`,
      `sh scripts/smoke_install.sh`

## Review

- [ ] Every snapshot diff (API reference, `docs/reference/openapi.json` with
      `docs/reference/api-changes.md`, `docs/reference/flags.md`) is read and explained above;
      removals and changes follow the
      [versioning policy](https://github.com/ai-loren/syndica/blob/main/docs/user/versioning.md).
- [ ] A user-visible change has an entry under `## [Unreleased]` in `CHANGELOG.md`.
- [ ] Errors a user can see say what was expected, what was received, and what to do next.
- [ ] I broke the implementation at least once and saw a test go red.
- [ ] No AI tool is listed as a co-author; `Assisted-by: <tool>` is used if one helped.
