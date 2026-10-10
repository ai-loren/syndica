# Versioning and deprecation

Syndica follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html) for its **public
surface**, the parts listed below. Version numbers are written as
[PEP 440](https://peps.python.org/pep-0440/) strings: `1.0.0rc1`, `1.0.0`, `1.1.0`.

- A **major** release (`2.0.0`) may remove or change public surface.
- A **minor** release (`1.1.0`) only adds: new names, new optional parameters, new routes, new
  optional fields, new Flags, new CLI options. Deprecations are announced in minor releases.
- A **patch** release (`1.0.1`) fixes bugs without changing the public surface. A security fix may
  refuse something that used to be accepted when accepting it was the vulnerability; the
  [changelog](https://github.com/ai-loren/syndica/blob/main/CHANGELOG.md) says so under *Security*.

Pre-releases (`1.0.0a1`, `1.0.0b1`, `1.0.0rc1`) carry no compatibility promise between each other.
Names withdrawn between a release candidate and the final release are listed in the changelog under
*Breaking changes before 1.0*.

## What is public

The authoritative list of public Python names is the API reference, `docs/user/reference/api.md`,
which is generated from a reviewed snapshot. The public surface is:

1. **Python names** exported in the `__all__` of the modules the API reference lists: their call
   signatures (parameter names, kinds and defaults), the public members of exported classes, and
   the hierarchy of exported exception classes. A new subclass of an existing exception is an
   addition.
2. **Hook junction names**: the names plugins and Runtime Modules register callbacks under.
3. **Store Protocols** and the contract suites shipped with the package. A backend that passes a
   contract suite in one 1.x release keeps passing it in later 1.x releases.
4. **Runtime Module integration**: the `syndica.modules` entry point group and the module manifest
   fields.
5. **Flag keys**, with their types, allowed values and defaults (see the
   [Flag reference](https://github.com/ai-loren/syndica/blob/main/docs/reference/flags.md)).
6. **The `.syndica.yaml` schema**: root keys, fields of declarative agent manifests and topologies,
   and the user-level `interop.yaml`.
7. **The HTTP API** served by `syndica up`: routes, request and response bodies, SSE event payloads
   and WebSocket frames, as recorded in the
   [OpenAPI snapshot](https://github.com/ai-loren/syndica/blob/main/docs/reference/openapi.json).
   Fields marked `x-syndica-stability: provisional` are excluded until they are promoted.
8. **CLI commands**, their subcommands, options and documented exit codes.
9. **Trace and trajectory schemas**: records identified by a schema id such as `syndica.trace/8`
   and `syndica.trajectory/1`, and the export formats built on them.

Anything the API reference marks as *provisional* may change in a minor release; the change is
recorded in the changelog.

## What is not public

- **Runtime messages.** Error texts, log messages, help texts and the human-readable output of CLI
  commands may change wording in any release, including their language: they are currently in
  Chinese and are planned to be translated in 1.x. Match on exception classes, exit codes and
  structured fields, never on message text.
- Names that start with an underscore, modules the API reference does not list, and anything
  imported into a public module without being in its `__all__`.
- The layout of files under `.syndica/` other than the documented schemas.
- The contents of the web UI and the HTML it serves.

## What counts as a breaking change

Removing or renaming any public item is breaking. So are:

- changing a signature incompatibly: removing a parameter, making an optional parameter required,
  making a keyword parameter positional-only or the reverse, or changing a default in a way that
  changes behaviour;
- adding a method or attribute that implementations of a Store Protocol or `LongHorizonStrategy`
  must provide, even though it looks like an addition;
- adding a required field to a request body, a YAML schema or a module manifest;
- dropping read support for a trace or trajectory schema version that an earlier 1.x release could
  read. Writing a new schema version is not breaking as long as older versions remain readable.

Dropping support for a Python version happens in a minor release and is announced in the changelog.
It is not treated as breaking: `requires-python` keeps older interpreters on the last release that
supports them.

## Deprecation

1. A deprecation is announced in a minor release and listed under *Deprecated* in the changelog,
   with the replacement.
2. The deprecated item keeps working. Python names emit a `DeprecationWarning`; CLI options, Flag
   keys and YAML keys print a warning on stderr; HTTP fields and routes are marked `deprecated` in
   the OpenAPI snapshot.
3. The item stays for at least one further minor release and at least 90 days.
4. It is removed only in the next major release, listed under *Removed*.

If keeping a deprecated behaviour is itself a security problem, it can be removed sooner; the
changelog records that under *Security*.

## Proposing a breaking change

1. Open an issue that says what changes, who it breaks and how they migrate.
2. Prefer an additive path: add the new name, deprecate the old one.
3. In the pull request, update the snapshot and the changelog in the same commit.
4. A maintainer reviews the snapshot diff, not only the code.
5. The removal ships in the next major release.

## Snapshot review

The public surface is pinned by snapshots that live in the repository: the API snapshot behind the
API reference, the OpenAPI snapshot with its
[change log](https://github.com/ai-loren/syndica/blob/main/docs/reference/api-changes.md), and the
Flag reference generated from the module declarations.

**A snapshot change is a public-surface change.** When a snapshot test fails, read the diff first,
then decide whether the code or the snapshot is wrong. Never regenerate a snapshot without reading
the diff. A diff that removes or changes an entry goes through the breaking-change process above;
a diff that only adds gets an *Added* entry in the changelog.
