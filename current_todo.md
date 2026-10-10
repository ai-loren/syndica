# Current TODO

M12 is done (see [the M12 audit](docs/audits/2026-10-10-m12-final-audit.md)); `main` is at 1.0.0rc1. The items
below were deliberately deferred on 2026-10-10: the maintainer chose to publish only after the hosted CI
matrix passes, and this month has no GitHub Actions minutes left.

## 1. Hosted CI matrix (next month, when Actions minutes reset)

- [ ] Run `.github/workflows/ci.yml` on `main` on GitHub-hosted runners: gates on ubuntu / macos x Python
      3.11 / 3.12 / 3.13, plus the web, package and security jobs.
- [ ] All jobs green. This is the release gate. Locally the Linux cells ran on arm64 (Colima), so
      ubuntu-latest (x86_64) is verified for the first time here; fix what it finds before going on.

## 2. Release 1.0.0rc1 (only after step 1 is green; follow [RELEASING.md](RELEASING.md))

- [ ] `uv build`, `uv run python scripts/check_dist.py`, `sh scripts/smoke_install.sh --with-trl`,
      `sh scripts/acceptance.sh` on the commit to be tagged.
- [ ] Tag `v1.0.0rc1` and push the tag.
- [ ] Publish to TestPyPI, install from it in a clean environment, then publish to PyPI (trusted publishing
      or a token; the maintainer sets up the credentials).
- [ ] Create the GitHub release with `SHA256SUMS` attached and the `CHANGELOG.md` entry as notes.

## 3. Make the repository public (after the release)

- [ ] Switch the repository to public.
- [ ] Never run pull requests from forks on self-hosted runners; keep `ci.yml` on GitHub-hosted runners only.
- [ ] Publish the docs site (`mkdocs build` from `docs/user`, e.g. GitHub Pages) and point
      `[project.urls] Documentation` in `pyproject.toml` and the README links at it.

## 4. GitHub repository settings (any time, maintainer only)

- [ ] Enable *Private vulnerability reporting*: [SECURITY.md](SECURITY.md) sends reports there.
- [ ] Enable *Reported content*: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) relies on it for enforcement reports.

## 5. Optional

- [ ] Real-model acceptance: export a model key in the environment and run `sh scripts/acceptance.sh`; it
      repeats the README run and the batch / export against the real model (the key goes to the container on
      stdin and is never written to disk).
- [ ] For 1.0.0 final: change the classifier to `Development Status :: 5 - Production/Stable` and the
      expectation in `tests/test_release_files.py` with it, bump the version, add the CHANGELOG entry.
