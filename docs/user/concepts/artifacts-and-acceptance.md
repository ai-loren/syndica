# Artifacts and acceptance

Programming has free answers to "is the work done?": it compiles, the tests pass, the diff is
readable. Most knowledge work has none of these. Syndica adds two things: **artifacts with
identity**, and **acceptance criteria traceable to the request**.

## Artifacts

An artifact is a file the agents produced, with a stable id across versions, a path, a `kind`
(`document`, `spreadsheet`, `diagram`, `dataset`, `code`, ...), a MIME type, an opaque version, the
agent that produced it, and whether it is a **deliverable**.

- Every `Write` and `Edit` registers the file it touched, with a new version.
- `Deliver(path, kind, note)` marks a file as a deliverable. Only deliverables are checked, so
  scratch scripts and debug output do not cost verifier tokens.
- **Artifact handlers** know formats. They are chosen by `(kind, MIME)`, the more specific match
  wins, and two handlers registered at the same precision is an error at start-up rather than a
  behaviour that depends on import order. A handler can validate, preview and diff its format; the
  kernel ships a generic text handler. The handler is picked from the file's extension, not from
  the kind the agent chose in `Deliver`, so an agent cannot route its deliverable past a stricter
  handler.

`syndica artifacts list` and `syndica artifacts show <id>` print the index with versions and
producers; the web UI previews artifacts and diffs their versions.

## Goal and rubrics

- The **goal** is the authority on what was asked: `--goal <text>` on `run` or `chat`, otherwise the
  first message. It changes only when a person changes the request.
- **Rubrics** are the goal's checkable projection, derived before any work starts by a verifier
  that sees what was asked but not what was produced. **Every rubric must quote the part of the
  request it comes from.** A requirement that cannot be anchored is an invented requirement and is
  dropped.
- A rubric is `checkable` (a verifier can judge it) or `judgment` (only a person can). A judgment
  rubric is shown to the agent but never passed automatically; it stays `unverified`.

## Three layers

| Layer | Checks | Cost |
| --- | --- | --- |
| L1 format | The artifact handler validates each deliverable (for the generic handler: not empty). | Free |
| L2 output | A conservative verifier: was everything that was asked done, or explicitly cancelled? | Model calls |
| L3 outcome | A verifier checks each rubric against the final reply and the workspace, and must re-open its evidence. | Model calls |

A deliverable that fails L1 is sent back once at no model cost. L2 and L3 run when the agent says it
is done, and each has its own flag and retry limit.

## Four verdicts

The kernel derives one verdict per run from the layers' findings:

| Verdict | Meaning |
| --- | --- |
| `pass` | Every rubric was verified as satisfied. |
| `partial` | Not finished, **or could not be confirmed**. |
| `block` | There is concrete evidence the result is wrong. |
| `unverified` | Acceptance could not run to a conclusion: a verifier failed or timed out, or a rubric only a person can judge. |

A verifier that fails lets the run finish, **but the run is never reported as `pass`**: "we could
not verify it" must not collapse into "verified". Training export leaves `unverified` samples out
by default for the same reason.

The verdict is printed on stderr at the end of a run and recorded in the trace.
`syndica run --verdict-exit` turns it into the exit code: 10 `pass`, 11 `partial`, 12 `block`, 13
`unverified` (130, 1 and the handoff's 3 take precedence).

## Turning it on

Acceptance is a Runtime Module. With `modules: [acceptance]` the host keeps an artifact index in
`.syndica/artifacts` and agents that can write files get `Deliver`. The model-backed layers are off
until their flags are set:

```yaml
version: 1
modules: [acceptance]
flags:
  acceptance_rubrics: true
  acceptance_output: true
  acceptance_outcome: true
```

- `acceptance_rubrics` projects the goal into anchored rubrics before work starts.
- `acceptance_output` turns on L2, `acceptance_outcome` turns on L3; their `*_retries` flags say
  how many times a `partial` or `block` sends the agent back.
- `acceptance_verifier_timeout` and `acceptance_verifier_max_turns` bound the verifiers.

All acceptance flags are in the [flag reference](../reference/flags.md). Runs with acceptance
installed do not write a replay document, because the verifiers' model calls are not part of the
recorded turns.

## Feedback becomes a rubric

With `acceptance_feedback: true`, a message you send after the goal ("column 3 of this table must
use confirmed revenue") becomes a **proposed** rubric anchored to your words. It counts only after
a person activates it:

```bash
syndica sessions rubrics <session-id>
syndica sessions activate <session-id> <rubric-id>
```

Once active, it is checked in every later acceptance, so the same mistake is not made twice. The
web UI's artifact panel can propose rubrics from feedback too.
