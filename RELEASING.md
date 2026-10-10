# Releasing Syndica

This checklist is for maintainers. **Tagging, pushing a tag, publishing to TestPyPI or PyPI, creating
a GitHub release and making the repository public each need the maintainer's explicit go.** Every
step before that can be run and re-run freely; none of them leaves anything outside the working
tree.

Syndica ships as one distribution, `syndica`, with extras (`openai`, `server`, `otel`, `mcp`, `acp`,
`a2a`, `trl`). The version is written once, in `pyproject.toml`; `syndica --version` reads it from
the installed distribution. In the commands below, replace `X` with that version (`1.0.0rc1`); the
commands are templates and are not run by any test.

## 1. Prepare the release commit

- [ ] Start from an up-to-date `main` with a clean tree: `git status --porcelain` prints nothing.
      Artifacts built from uncommitted changes must never be published.
- [ ] Set `version` in `pyproject.toml` to a canonical PEP 440 version (`1.0.0rc1`, `1.0.0`,
      `1.0.1`; no `v` prefix, no local label) and run `uv lock` so `uv.lock` agrees.
- [ ] In `CHANGELOG.md`, turn `## [Unreleased]` into `## [X] - YYYY-MM-DD`, add a fresh empty
      `## [Unreleased]`, and update the comparison links at the bottom. Every removed or changed
      public item is listed with its replacement (see [the versioning policy](docs/user/versioning.md)).
- [ ] Review every snapshot diff since the last release: the API snapshot behind
      `docs/user/reference/api.md`, `docs/reference/openapi.json` with its entry in
      `docs/reference/api-changes.md`, and `docs/reference/flags.md`. A removal or change that is not
      in the changelog blocks the release.
- [ ] Commit (`release: X`), with the message explaining anything unusual about this release.

## 2. Gates

The hosted CI matrix (`.github/workflows/ci.yml`: Ubuntu and macOS × Python 3.11, 3.12, 3.13, plus
the frontend and packaging jobs) passing on the release commit is the release gate.

While hosted runners are unavailable, run the same matrix locally with `scripts/local_ci.sh` on
macOS and in a Linux container, for Python 3.11, 3.12 and 3.13, and record the result in the release
commit message. A local run is an interim check: do not publish a final release to PyPI until the
hosted matrix has passed on the release commit, unless the maintainer explicitly waives it for a
pre-release.

The gates themselves (the README's Development section lists the Python ones), with every extra
installed so the backend suites run too:

```bash
uv sync --locked --all-extras
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run python scripts/check_deps.py
uv run python scripts/check_links.py
uv run pytest -q
sh scripts/check_web.sh     # needs Node 26; the bundle must rebuild byte for byte
sh scripts/check_e2e.sh     # Playwright, desktop and mobile
```

## 3. Build and inspect the artifacts

```bash
rm -rf dist
uv build
uv run python scripts/check_dist.py
uv tool run twine check --strict dist/*.whl dist/*.tar.gz
```

- Use plain `uv build`: it builds the sdist and then the wheel **from the sdist**, which proves the
  sdist is enough to rebuild the wheel.
- `check_dist.py` compares both artifacts with the files Git tracks under `src/syndica`, byte for
  byte, checks the metadata (name, version, `requires-python`, the SPDX license, classifiers, no
  Windows, the `syndica` entry point, every extra, project URLs), refuses stray files in `dist/`, and
  only then writes `dist/SHA256SUMS`. Add `--list` to print every file in both artifacts.
- PyPI is the authority on classifiers. `check_dist.py` pins the required ones; an unknown
  classifier is rejected by the TestPyPI upload in step 7, before anything reaches PyPI.

## 4. Install smoke

```bash
sh scripts/smoke_install.sh              # base and each extra except trl, ~20 s with a warm uv cache
sh scripts/smoke_install.sh --with-trl   # once per release: trl brings torch (about 0.9 GB)
SMOKE_PYTHON=3.13 sh scripts/smoke_install.sh
```

Each target gets a fresh venv, installs `dist/syndica-X-py3-none-any.whl[extra]`, and runs
`syndica --version`, `syndica --help`, `uv pip check` and an import check for that extra. The base
target also requires every extra's import check to fail, so none of them passes vacuously.

## 5. Acceptance

```bash
sh scripts/acceptance.sh
```

It runs the four end-to-end acceptance scenarios in a clean Linux container, from the built wheel,
following the README: a CLI run within five minutes, a multi-agent task with a confirmation and
artifacts in the web UI, a tool call from MCP and from the OpenAI SDK against a local server, and a
batch followed by a training export.

## 6. Tag (maintainer go)

```bash
git tag -s vX -m "Syndica X"   # -a instead of -s if you do not sign tags
git describe --exact-match     # must print vX: the tag points at the commit the artifacts came from
```

Push the release commit and the tag only after the go: `git push origin main vX`.

## 7. Publish (maintainer go)

Upload exactly the two checked artifacts, never `dist/*` (that would include `SHA256SUMS`):

```bash
uv publish --publish-url https://test.pypi.org/legacy/ \
  dist/syndica-X-py3-none-any.whl dist/syndica-X.tar.gz
```

Confirm TestPyPI holds the same bytes that passed the checks:

```bash
curl -fsS https://test.pypi.org/pypi/syndica/X/json | python3 -c 'import json, sys
for url in json.load(sys.stdin)["urls"]: print(url["digests"]["sha256"] + "  " + url["filename"])' \
  | sort -k2 | diff - dist/SHA256SUMS
```

Then publish to PyPI the same way without `--publish-url`, and repeat the digest check against
`https://pypi.org/pypi/syndica/X/json`.

Credentials, in order of preference:

1. **Trusted publishing** from a GitHub Actions workflow with `id-token: write`
   (`uv publish --trusted-publishing always`). Configure a (pending) trusted publisher for the
   `syndica` project on TestPyPI and PyPI first. This needs hosted runners.
2. **An API token** in `UV_PUBLISH_TOKEN`, never on the command line or in a file in the
   repository. A project-scoped token cannot exist before the first upload, so the very first
   upload needs an account-scoped token: revoke it right after that upload and create a
   project-scoped one for later releases.

Files on PyPI are immutable: a filename can be uploaded only once, even after deletion. If a
published release is broken, yank it on PyPI and release a new version (`rc2`, `X.post1` for a
packaging-only fix, or the next patch).

## 8. GitHub release (maintainer go)

```bash
gh release create vX dist/syndica-X-py3-none-any.whl dist/syndica-X.tar.gz dist/SHA256SUMS \
  --title "Syndica X" --notes-file <the CHANGELOG section for X> [--prerelease]
```

Use `--prerelease` for `a`, `b` and `rc` versions. Users verify a download with
`shasum -a 256 -c SHA256SUMS`.

## 9. After the release

- [ ] Set `version` on `main` to the next development version (`X.Y.(Z+1).dev0`) and run `uv lock`,
      so builds from `main` never report a released version.
- [ ] Check that `pip install 'syndica[server]==X'` works in a fresh venv and that the PyPI page
      renders the README and links.
