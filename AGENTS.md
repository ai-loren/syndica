# Syndica — Agent Guide

> This is the **list of constraints** for the AIs and humans who change code in this repository,
> not a tutorial. It is injected into agents' context, so it **only states positioning, boundaries
> and invariants**: implementation details stay in the code, design reasoning stays in
> [`docs/design/2026-09-03-architecture.md`](docs/design/2026-09-03-architecture.md) (the spec).
> Writing it as a changelog would crowd out the context budget of the other documents.
>
> Three section titles keep their Chinese names in front (硬约束, 错误消息, 测试): the development
> roles' prompts in `docs/agents/` look those sections up by name.

## What this is

**An agent harness with multi-agent as a first-class citizen, for general knowledge work.**
In one sentence: let a team of agents complete "verifiable knowledge work" 7×24 hours, and turn
the process of doing the work into training data.

Four claims (a change that conflicts with them is going in the wrong direction):

1. Multi-agent is a first-class citizen: agents have process semantics; delegating / forking /
   messaging are kernel tools; the organisational structure is declarative data.
2. General knowledge work, not just programming: documents, tables and diagrams are treated the
   same as code.
3. Artifacts and acceptance are kernel capabilities: artifacts have identity and versions;
   acceptance criteria must be traceable to the original request.
4. Inference and training share one source: acceptance verdicts are reward and preference signals.

Long-horizon tasks are a deliberately chosen direction of exploration; see spec §8.

---

## 硬约束 · Hard constraints (a violation is sent back)

| # | Constraint | What breaks if it is violated |
|---|---|---|
| 1 | **The kernel depends on no surface and no backend** | The layering collapses; the kernel cannot be released on its own |
| 2 | **surface → kernel only; surfaces do not depend on each other** | The CLI drags in web dependencies; install size gets out of control |
| 3 | **backend → kernel only; backends do not depend on each other** | Installing one backend forces installing another |
| 4 | **The kernel reads no environment variables** | The kernel gets tied to one way of deploying, while an open-source project knows nothing about how users deploy |
| 5 | **The kernel has zero heavy dependencies** (standard library + pydantic + one HTTP client only); vendor SDKs always go into backends | `pip install` drags down half the ecosystem |
| 6 | **The public API is fully type-annotated** and ships `py.typed` | Downstream gets no types, and the Protocols are hollow |
| 7 | **The agents' command environment is passed through by allowlist only, and names that look like secrets are always refused**, even when written in `pass_env` | Once a secret is in the agents' environment, a command prints it into the context and it is sent to the model provider with the request |
| 8 | **Data written into traces, handoff files and commits, and data exported to third parties, all go through the kernel's `Redactor` first**; requests sent to the model provider are the only exception; new persistence or outbound paths go through the same redactor | Secrets are written to disk, to git history and to someone else's collector; deleting them afterwards is too late |
| 9 | **Agents do not run git write commands; commits and pushes are made only by the host, after a person chooses** | The model rewrites history by mistake, or pushes commits nobody reviewed |

The first three are checked by machine in CI, not by people; numbers 7–9 are guarded by tests (cases
for the command environment, an AST guard over every write point and outbound point in the
repository, cases for the unconditional development-time block). The block for number 9 reads the
command text: commands handed on through `sh -c`, `eval`, `xargs` and `find -exec` are recognised;
those inside scripts, variables, `python -c` and programs outside the list (`su -c`, `ssh`, `make`)
are not. It stops mistakes, not intent; the real boundary is a sandboxed Workspace backend (the
kernel only defines the `Workspace` / `Portal` protocols and their contract suite; the sandbox is
implemented by a backend). So this constraint is first of all a requirement on the code: no change
may give agents a new way to perform git write operations.

**The public API is wider than intuition suggests**: besides function signatures, **junction names,
Store Protocols, Flag keys and declarative agent manifest fields** all count, and changing them is
a breaking change.

---

## Key invariants

### Prefix cache (spec §5.2)

Caches match on the **byte prefix of the request**, and the system prompt comes first. **Change one
byte of the SP and every later request in that session misses the cache**, and the SP is rebuilt
every turn with no freezing mechanism.

1. The prefix is **byte-identical** from turn to turn; nothing that changes from turn to turn goes
   into the SP.
2. **Dynamic environment facts are injected into the history, not into the SP** (working
   directory, isolation state and so on), once at spawn and idempotently.
3. New content is **only appended**; the existing prefix is never changed.
4. Cache affinity follows session semantics: a fork of the same type inherits the cache key, a fork
   of another type does not.
5. One-off calls (generating a title, extracting memories) turn cache writes off.

Violations **raise no error; they only cost money silently**. Development mode turns drift
detection on by default.

Corollary: prompt interpolation variables of declarative agents **must not include `{cwd}`**;
memory must cache one and the same block and must not be re-fetched every turn.

### Context compaction (spec §8.8)

Compaction must not break five things: tool calls stay **paired** with their results, the SP **takes
no part** in compaction, **the goal and unmet rubrics survive**, injected environment facts survive
or are re-injected, and **untrusted markers survive on derived content**.

The last one sits where compaction meets security and is the easiest to miss: compaction rewrites
untrusted content into text that looks like a conclusion; once the marker is lost, high-risk
operations based on it bypass the security escalation.

After compaction, **rebuild the cache key** (so the invalidation happens once), and record the
compaction point in the trace.

### Concurrency (spec §4.4 / §6.4)

- Agent state transitions use **CAS**, not direct assignment. `update()` is only for fields with a
  single writer; collections that several parties write must provide atomic operations.
- Artifact writes in a shared workspace use **optimistic concurrency control**; a conflict is
  returned explicitly instead of silently overwriting. Losing data is silent and catastrophic; a
  conflict is explicit and recoverable.
- The same for memory writes; **only the root coordinator may write global memory**.

### Uncertainty must be explicit (spec §7.5 / §10 / §9.5)

- Verifier fails → let the run through, but **explicitly mark it as not accepted**.
- The verdict has four values: `pass` / `partial` / `block` / **`unverified`**.
- The training-data exporter **excludes `unverified` by default**. Treating "we could not verify
  it" as "verified" is the most insidious data pollution, and directly damages claim 4.

### Authorisation and dangerous operations (spec §5.6)

- Danger judgement is **pure rules**: it outputs only a rule ID, a risk level and a reason; it reads
  no configuration, calls no LLM and does not decide how to interact.
- The disposition is decided by the host (block / let the model authorise itself / ask a person).
- **An authorisation allows only the current call**; there is no "allowed from now on". In a long
  run of hundreds of turns, a broad authorisation ends up used in a completely different context.

### Topology (spec §4.7)

**Declare structure only, never behaviour.** A slot may attach tools / plugins / budget to an agent,
but **must not replace its base configuration**. Otherwise the same agent behaves differently in
different topologies, and debugging requires reading two definitions at once. If you need
different behaviour, define a different role.

### Security (spec §12)

- **Every new persistence path must go through the redaction choke point.** Redaction happens
  **before writing**: writing to disk and deleting afterwards is too late, the backups already have
  it. A new write point that bypasses the choke point is a defect. Redaction also happens **before
  truncation**: a cut may land in the middle of a secret, and the remaining half may no longer be
  recognisable.
- **An action the host performs on an agent's behalf must not give the agent a capability it does
  not have.** The host's `git commit` runs hooks and programs from the configuration
  (`gpg.program`, filters), and they often execute code in the workspace that agents can change
  (husky, lint-staged, `npm test`); running them in the host's environment hands the ssh agent and
  every secret to the agent. Any subprocess that executes workspace code runs only in the agents'
  environment; where the host environment is required (pushing needs the ssh agent), turn off the
  parts that execute code, and if they cannot be turned off, do not offer the feature. When a
  question to a person sits between a check and an action, **do the check again** right before the
  action: while the question is pending, a process that escaped process-group cleanup can still
  change the repository.
- **The untrusted marker must be transitive.** After external content is summarised, compacted or
  consolidated into memory, the derived content is **still untrusted**. This is the easiest one to
  miss: compaction rewrites untrusted content into text that looks like a conclusion.
- **If a high-risk operation can be traced to untrusted content, its disposition must be
  escalated**, and the model may not authorise it itself. Rule matching cannot stop "persuaded,
  then wrote a script that looks legitimate". Unattended, **refuse by default instead of waiting**:
  refusing lets the agent switch paths immediately, waiting only burns a timeout.
- **The body of a third-party Skill is untrusted by default** (it can be declared trusted one by
  one). A Skill is by nature "something to be executed as instructions", a more direct injection
  vector than a web page; "the user installed it" does not mean "the user read every line".
- When redaction conflicts with replayability, **choose safety**: replay uses placeholders, and a
  step that needs a real credential failing is correct behaviour.

### 错误消息 · Error messages (hard rule)

**An error a user sees must have three parts: what was expected / what was actually received /
what to do next.** None may be missing.

The quality of an open-source project's error messages directly determines the number of issues:
if the error is unclear, users can only open an issue.

```
✗  duplicate handler for spreadsheet
✓  产物处理器冲突：kind=spreadsheet mime=xlsx 已由 syndica-office 注册，
   syndica-sheets 重复注册同一精度。卸载其中之一，或用更具体的 mime 区分。
```

(The good example says: artifact handler conflict: kind=spreadsheet mime=xlsx is already
registered by syndica-office, and syndica-sheets registers the same precision again; uninstall one
of them, or tell them apart with a more specific mime. Runtime messages stay in Chinese in v1.0.)

Scope: Protocol contract violations, dependency rule violations, Flag configuration errors, handler
conflicts, topology validation failures: anything a user can see. Purely internal diagnostic logs
may be brief.

**Of the three parts, the third is the easiest to end up with zero assertions.** Measured: in a new
module, all nine `raise` sites had their `next_action` replaced with "please check your request",
and all 959 tests stayed green, although five test files in the same repository already asserted
its anchor words. At each site, assert one **irreplaceable** anchor word (the name of the field to
change, the name of the switch, the word unique to this verdict), and leave the wording free to
change.

**The class name comes from the class being constructed; do not hard-code the base class name.**
The reader of that message is writing a subclass, and reporting the base class name points at a
place that is fine. `type(self).__name__`, not a literal.

---

## Which layer a new feature belongs in

| What you want to do | Where to change |
|---|---|
| A capability **needs a switch** (configurable, documented) | Write a **Runtime Module** + Flag declarations. **This is the default answer** |
| Common to all agents and **never exposed as a switch** | A kernel built-in plugin |
| Enhances **only one agent** | Register hooks in that agent |
| Support a **new execution environment** | Implement `Workspace` + `Portal`, in a backend; zero changes to the tool layer |
| Support a **new model vendor** | Implement a provider, in a backend; the kernel knows no vendors |
| Support a **new artifact format** | Implement an `ArtifactHandler`, in a backend |
| Change the **storage backend** | Implement the corresponding Store Protocol; it **must pass the contract tests the kernel publishes** |
| Need a **new agent role** | Prefer a **declarative manifest + prompt**; write a class-based one only for custom tool registration / hooks / complex prompt assembly |
| Change the **system's organisational structure** | Change the topology declaration, not code |
| Change the **long-run paradigm** | Implement `LongHorizonStrategy` as a Runtime Module |

Order of judgement: **Can it be a Module? If not, is it a Protocol implementation of some layer?
Only if neither, consider touching the kernel.**

---

## 测试 · Testing

| Layer | When it runs | Notes |
|---|---|---|
| Unit / L1 in-process small flows | Every push | Pure functions, schema generation, state machine transitions |
| **L2 scenario replay** | Every push | Declarative scenarios + a scripted fake LLM driving the **real** runtime. **The only way to test failure paths** |
| **Protocol contract tests** | Every push | One suite for each of the four Store Protocols; every implementation must pass |
| Real-LLM end to end | Manual / scheduled | The only authority on effective correctness |

**The first three layers must run at zero cost** (no API keys). Reason: PRs from forks get no
secrets; if the tests all needed a real LLM, no automation at all could stop regressions.

Long-run tests use **a fake clock + deterministic seeds + fault injection**, not real waiting.

**Whether a test suite is strong enough is judged by mutation**: ask "which part of the
implementation could I break without turning any test red", then actually break it. Several times
a suite that looked complete stopped only one of the mutations. Two blind spots that were hit
repeatedly are worth listing on their own:

- **Enum members with colliding values are not an error; they are aliases.** `UNTRUSTED = 'trusted'`
  silently makes `UNTRUSTED` an alias of `TRUSTED`; the whole untrusted-marker system collapses
  into one value and the tests stay green. The defence is to assert the whole member → value table
  for equality (`{m.name: m.value for m in E} == {...}`): enum iteration skips aliases, so on a
  collapse the dict loses an entry and fails; one statement pins distinctness, completeness of
  members and the persisted values at once.
- **A mutation that runs green may be a stale bytecode cache.** `.pyc` is invalidated by (mtime in
  seconds, source size in bytes), so an equal-length replacement (`'max_turns'` → `'completed'`)
  made within the same second reuses the old bytecode, and the mutation is invisible to the
  interpreter. Clear `__pycache__` before mutation testing.
- **While new files are not `git add`ed, `git diff` is blind.** The closing step of mutation testing
  is "after restoring, confirm `git diff` is empty", but untracked files are not in the diff, so a
  missed mutation gets snapshotted as the baseline; this has really happened once. For tasks that
  add files, `git add` first, use the index as the restore baseline, and after restoring run again
  to confirm green. **But the index itself can be polluted**: `git checkout <commit> -- <file>`
  writes both the working tree and the index, and afterwards "restoring from the index" gives back
  that commit's version, not your baseline. So after restoring, always compare against the baseline
  by sha256; do not rely only on whether the diff is empty.
- **Checking against a baseline by sha256 only holds if the baseline set covers every file this
  round will change; the baseline set itself has to be proven.** It has really happened: a mutation
  changed a file that was not in the snapshot list, so on restore it was not restored and on
  verification it was never checked; the script reported "restore OK", the next two mutations and
  the attribution of the whole round ran on a tree with leftovers (54 existing tests red), and the
  verification said nothing the whole time; `git status`, not the verification, found it. **This
  kind of hole does not make the verification fail; it silently narrows its scope**, the same shape
  as "a guard with a hard-coded search scope can be narrowed quietly". Fix: also run the
  verification **before** applying each mutation (a baseline self-consistency check), or derive the
  list automatically from "the files this round will change" instead of writing it by hand.
- **Test helpers also fix the "cardinality", hiding every ordering and multiplicity property with
  it.** The shallow version came first: the helper always passed `max_turns` / `system_prompt`
  explicitly, so those defaults were never executed. The deeper layer was Task 13: the helper fixed
  `tools` at exactly one, so **reversing the order of tools passed all 498 tests**, and the order
  of tool definitions belongs precisely to the prefix-cache hard constraint. **Criterion**: any
  property involving a set, list or mapping needs at least one case with cardinality above 2; at
  `n=1`, reverse, sort, deduplicate, take-first and take-last all give the same answer.

  **And cardinality ≥ 2 is still not enough: the order must not be any natural ordering.** At n=2,
  one of `sorted` and `sorted(reverse=True)` is always the identity, so two elements **cannot stop a
  sorting mutation however they are arranged**: "neither ascending nor descending" is impossible at
  n=2. For the order to be no natural ordering, **cardinality must be at least 3**, and the fixture's
  order must be neither ascending, nor descending, nor equal to its reverse.
- **`==` on dicts is completely blind to key order, and key order is load-bearing in this
  project.** In the schema sent to the model or the message bodies assembled into a request, any
  drift in key order **invalidates the whole prefix cache without any error** (the cost only shows
  up on the bill and in the hit rate; the same class of reason `LLMConfig.derive` exists for). In
  Task 11, all three "reorder keys" mutations survived, because nearly every assertion in that file
  was `==`. **For any structure that goes into an LLM request or a cache key, assert the order of
  `list(d)` explicitly**, not just equality.
- **Tests must run under the same module conditions as production.** This repository requires
  `from __future__ import annotations` (PEP 563) at the top of every module, so all annotations in
  that module are **strings** at run time, and `inspect.signature` does not evaluate them by
  default. Task 11's test file lacked that line, so its tool classes kept real type objects: the
  suite was green, while every real tool written to the repository's convention raised an error.
  **For code that reflects on annotations, `__module__`, `__qualname__`, import order or
  compile-time switches, the test file must reproduce the production module's header conditions**,
  otherwise it verifies the opposite of the code that ships.
- **`asyncio.wait_for` inserts scheduling hops of its own, so any argument of the form "there is no
  suspension point between these two statements" must account for it.** It has bitten three times:
  once a case went green under mutation because `wait_for` inserted an extra scheduling step; once
  "immediately after the event's `set()` comes the re-check, no other task can get in between" was
  disproved by measurement: 3.11's `wait_for` wraps the inner coroutine in a Task and adds a
  done-callback, and the real window is **six ready-queue slots**; two ordinary tasks are enough to
  build a distinguishing case. **Criterion**: whenever you want to assert "no interleaving window",
  do not reason about it; instrument and count the ready-queue slots.

  **The third time had a different shape: it does not just insert scheduling hops, it swallows an
  arriving cancellation whole.** 3.11's `wait_for` contains
  `except CancelledError: if fut.done(): return fut.result()`: a cancellation arriving when the
  inner future is already done is consumed as "the result has arrived", so `task.cancel()`
  silently has no effect and the call is **not cancellable** from then on. Measured: in the loop
  where the broker takes messages by correlation ID, delivering one **non-matching** message makes
  the inner future ready (`arrival` set), the
  cancellation at that moment is swallowed, `while True` carries on, and `abort` / `shutdown` /
  `despawn` all have to wait out the whole timeout given to it (measured: 19.95 s each on a 20 s
  timeout, and in production the timeout can be hours). The in-memory store's `publish` never
  suspends, so this interleaving is hit **deterministically**, not by chance. **Criterion**: for
  any blocking call that callers stop on the spot by cancelling it, the inner wait always uses
  `asyncio.timeout`, never `asyncio.wait_for`; `asyncio.timeout` turns `CancelledError` into
  `TimeoutError` only when its own deadline expires, and external cancellation propagates as usual.
  This class of defect is **invisible to the whole test suite** (the same implementation gave 985
  passed both before and after the fix); it needs cases written specifically for it.

  **Conversely, wrapping a call in `asyncio.wait_for` in a test to bound it inserts a scheduling hop
  and closes the very window under test.** Measured in the same round of fixes: after the `abort`
  in a C1 regression case was wrapped in `_TIMEOUT`, `wait_for` turned it into a Task, the broker's
  take cleared the signal and settled again before the abort arrived, and the case went green with
  the bug still there. **Add the bound only to calls that really can hang** (waiting on someone
  else's task: `shutdown` / `despawn` / `kill`); control-plane calls that only touch the registry
  and synchronous flags (never suspending under the in-memory implementation) get none: it brings
  no benefit and changes the interleaving.

  **And the number of slots is a function of the interpreter version, so an instrumented conclusion
  holds only for the version it was instrumented on.** The same instrumentation measured "every
  callback queued from the moment of the set runs before the re-check" on 3.11.9, and **zero slots**
  on 3.12.11: 3.12's `wait_for` became `async with timeouts.timeout(...): return await fut`, no
  longer wraps a Task, and `set()` schedules the waiter on the spot. This repository has
  `requires-python = ">=3.11"`, so the version where the window exists decides how the
  implementation is written (the re-check must stay), while **a test must not write one version's
  slot count into an assertion**: that is a mine that only goes off when the interpreter is
  upgraded. Write assertions in a contract form that holds on every version ("the answer is
  consistent with the state at this moment"), at the cost that it only kills the mutation on the
  versions that have the window.
- **In waiting code, level signals and edge signals must not be mixed.** It has bitten twice.
  "The queue is not empty" is a **level** (state); "something new arrived" is an **edge** (event);
  the two coincide only when the queue is empty. A loop that "waits for the next one" driven by a
  level signal, once the buffer holds an element that does not satisfy the condition, sees the
  signal stay high, wakes immediately to check again, and degenerates into a spin that never yields
  control. It even defeats `asyncio.wait_for`, because it never awaits a point where it can be
  cancelled, and shows up as the whole suite hanging rather than timing out. To "wait for a new
  event", always `clear()` first and then `wait()`, using it as an edge.
- **A global test timeout is required infrastructure, not a luxury.** Without a timeout, hang-type
  regressions (permanent park, non-yielding spin) show up as CI running silently to the platform
  limit, with no information to locate them. `pytest-timeout` turns that into a red assertion with
  a stack.
- **A derived value automatically satisfies any invariant expressed through its own derivation.**
  It has appeared twice in different guises: when `OBSERVABLE = all - INTERCEPTABLE`, the partition
  assertion "the two sets are disjoint and their union covers everything" **holds by construction**,
  so it cannot catch "a new member was silently classified", the very defect it was meant to stop;
  likewise, when the expected value and the object under test come from the same source (both
  derived from one `--collect-only`), missing a contract suite makes the expected value shrink with
  it. **Criterion**: after writing an assertion, ask "if the implementation were changed to derive
  this from the object under test itself, would this assertion still turn red?" If not, it tests an
  identity, not behaviour. The fix is to add a test that asserts the **exact contents**.

  **When asserting "the exact contents of some set", the thing asserted must be something other
  cases cannot change.** If it is module-level mutable state, the assertion depends on what ran
  before it, and "green" becomes a function of run order instead of behaviour; this repository
  avoids it by putting extension classifications in **instances** and kernel classifications in a
  frozenset (measured: the two kernel classification assertions hold when run alone, too).

  But "exact contents" can also be fed: if enumerating exception subclasses walks only the
  top-level module, re-exports in sub-packages' `__init__` fill the list, and a missing subclass
  still goes unnoticed. **The expected value must come from a third-party source of truth unrelated
  to the mechanism under test** (such as the file tree), not from the export surface of the object
  under test.

  **A fourth guise: a hard-coded expected value is not enough; the search scope itself must be
  proven too.** An AST guard scans `src/syndica/**` for "who calls `DangerPolicy.decide`", with the
  expected value hard-coded as `['kernel/danger.py']`. Pointing the scan root at a directory with no
  callers turns it red (the list becomes empty), but narrowing `rglob('*.py')` to
  `rglob('danger.py')` stays **all green**: it still finds the one expected answer, and from then on
  it is blind to every other file in the package; the guard's only value (catching files added in
  the future that no behavioural case covers yet) is deleted by that one word. It is not that the
  expected value derives from the object under test; **the search scope shrank to contain only the
  expected answer**, with the same effect. **Criterion**: every guard of the form "scan some scope,
  assert the result" needs another assertion that **it really scanned the place most worth
  catching** (hard-code that file name), and an independent traversal (`os.walk` against `rglob`)
  proving the scope did not shrink: the two lists only agree if both are changed together.
- **Isolation assertions must span "two reads of the same object", not two calls.** A third guise
  of the same family: "modifying the returned list does not affect the next call" **holds by
  construction** for an implementation that builds a fresh record on every call, so the mutation
  "hand out the internal list instead of a copy" passes untouched; it only turns red as "modify it,
  then read the same object again". Same criterion as above: ask "what in the implementation
  actually makes this assertion hold?" If the answer is "the two calls had separate copies anyway",
  it does not test isolation.
- **In async tests, "the sibling task was not abandoned" must be snapshotted at the moment the call
  under test returns.** A task abandoned by `gather` is not dead: it keeps running as the event loop
  turns, so asserting "it ran" after an `await` tests scheduling order, not waiting semantics.
  Measured: the same case kills the "bare gather" mutation when awaited directly, and goes green
  when wrapped in `asyncio.wait_for` (one extra Task hop). The way to do it: make the siblings block
  on an `Event` only the test can release, and record which ones finished at the instant the call
  under test returns.
- **This repository is a *multi*-agent framework, so "the operation is right but applied to the
  wrong subject" is a first-class mutation axis.** Measured: making every agent in a Runtime share
  one `ControlBlock` kept all 712 cases green, while it means one agent's deferred message silently
  enters another's context and one `pause` holds the whole Runtime. Anything indexed by `agent_id`
  (control blocks, mailboxes, contexts, the instance table, registry entries) needs a case proving
  that **two** agents do not cross wires; single-agent cases are completely blind to this whole
  class of defects.
- **The kernel must not modify objects the host holds in place.** The host may reuse its dict /
  list between calls (a factory handing out the same tool table every time is legitimate), so the
  first write makes it "belong to the first caller". The symptoms come in two grades: within one
  process, the second call **fails loudly, but the error points at something the host never did**
  ("your own table already has these five": the kernel just put them there); across processes /
  Runtimes, it **silently** binds capabilities to another subject (measured: an agent in a Runtime
  with `orchestration=False` inherited the five orchestration tools, bound to another agent of
  another Runtime, so it stopped an agent that was neither its descendant nor in its Runtime, with
  zero logs and zero errors). **Criterion**: any mutable container received from the host is either
  read-only or copied first; and "how many times the same table was used" is an **independent
  cardinality axis**: three agents does not mean the table was used twice.

  **And "copy it first" does not mean swapping in any fresh copy.** Where two places express
  "hot-plugging" by **sharing the same object** (here `AgentOptions.tools` and `ToolExecutor`),
  replacing the table must switch both sides to **the same** new object: if only one side is
  switched, the assertion "the installed ones are callable" stays green, while every later
  hot-plug is visible to only one side, the model's calls made from the description miss, and the
  run still ends `COMPLETED`. The only witness for this property is **two reads of the same object**
  (after installing, add one more to the table and see whether the executing side accepts it), not
  "install, then call once".
- **A property pinned in one layer is not pinned at the seam that actually depends on it.**
  Measured: the top ruling "pause is a level, wake is an edge" is guarded by `test_control.py`, but
  changing the `runtime` side's wake signal to a level kept all 83 cases of `test_runtime.py` green;
  the layer that would really spin because of it had zero coverage. **Criterion**: a cross-layer
  property needs a case **in every layer that consumes it**, and triggering it often requires
  **moving the cardinality from sets to sequences of operations** (applying the same operation to the
  same subject repeatedly, such as three pause–wake round trips in a row); a single operation cannot
  tell a level from an edge.
- **This repository's core promise is "four replaceable sides", so the mutation most worth trying is
  "a legitimate replacement on the other side of a seam"**: the collaborator neither fails nor lies;
  it is just **another implementation that satisfies every written clause**. Criterion: replace the
  in-memory implementation with one that also passes the contract suite but makes different choices
  where the contract says nothing; is the behaviour still right? Measured, it hit both times: if
  `on_missing_queue` does not run in a descendant context of `publish`, `activate=False` silently
  stops working, and that constraint **passes the whole 66-case contract suite**; and a broker that
  freezes its callback list at the first publish is equally green, but no Runtime created afterwards
  can revive its own zombies. **Any property the kernel depends on that the contract does not state
  is a property a third-party backend is certain to violate.**

  **The third hit was "the contract text is right, and the cases test only half of it"**, harder to
  find than "the contract does not say": the text is already there and reads as perfectly correct.
  The text says "after a take is cancelled, the message is still in the mailbox", while the
  accompanying case only asks "can another take still get it"; so a broker that moves the cancelled
  message into a pending entry (exactly how Redis consumer groups work) passes the **whole** contract
  suite, while what the kernel really depends on is "the gate's `consume` can see it": wired into a
  Runtime, an aborted wait returns ABORTED as promised, and that reply is neither in `consume` nor in
  the trajectory, with **zero ERROR**, while `send` had long since answered `ok=True`.
  **Criterion**: every word in the text that can be satisfied in two ways ("in the mailbox",
  "visible", "re-takeable") must be **pinned to one executable path** in the cases; the difference
  between text and cases is exactly the gap third parties walk into. Also: **alternative
  implementation paths** offered to third parties must be verified as satisfiable one by one; the
  main path "no suspension point between removing and returning" cannot be arranged at all for
  Redis Streams (`XREADGROUP` and `XACK` are naturally a round trip apart), so the alternative path is
  required, and it must spell out the obligations that come with it (a pending entry requires that
  implementation's `consume` to read its own pending first).
- **When you run out of new directions for mutations, attack the collaborator on the other side of a
  seam: not what it says, but that it breaks.** This repository has many seams (the gate, the
  broker, the LLM, tools, hooks, four Stores), and mutations usually only try "it returned the wrong
  thing", rarely "it raised". Measured twice: swallowing the gate's exception and substituting a
  default decision silently becomes "ignore the abort, keep running"; folding it into a normal
  `COMPLETED` makes the UI look as if the work was done. The baseline behaviour was right in both
  cases; no case had ever taken that path.
- **Some losses are design rulings, and then the log line is the only evidence, so the evidence
  itself becomes load-bearing.** An agent whose budget is exhausted cannot produce a new turn, and
  input it has already accepted can only be dropped: **dropping data is acceptable, silence is not**,
  so that ERROR is the only trace the loss leaves. Mutations on such paths need not touch behaviour:
  making the report **lie** (under-report one dropped quantity) or **invisible** (`logger.error` →
  `logger.debug`; the root logger defaults to WARNING, so the record is never created) puts "silent
  data loss" right back, and both pass loose assertions such as "there is an ERROR" or
  "`agent_id in caplog.text`". **Criterion**: assert `record.args`: the level, the subject, and
  **every quantity** that was dropped; leave the wording free to change.

  **The third way to lie is to report a loss that did not happen.** The subject of such a record is
  "what was lost because of this", so its trigger must be **that something was really lost**, not
  some approximation by shape. Measured: when "an observable junction discards a hook's return
  value" was implemented as "report whenever it returns non-`None`", two existing hooks that only
  record, written the type-correct way `return HookResult()`, each produced an ERROR immediately,
  and an empty result does nothing on an interceptable junction either, so nothing was lost in those
  two cases. A false report sends readers to change code that is fine, and it shares its assertion
  with a missed report ("there is an ERROR here"), so **every new report needs cases on both sides,
  "reported when it should" and "silent when it should"**, the latter enumerated by the shapes that
  "look like it but lose nothing".
- **A surviving mutation means a test is missing, not that the mutated code is useless.** This
  really produced the repository's first Critical: a mutation on a reference count could not be
  killed, the author concluded it was "unverifiable over-engineering" and deleted it, and it was
  exactly the mechanism preventing a permanently dangling `parent_id`; after deleting it, the defect
  could be produced with the default store through public paths only. **Deleting code because a
  mutation cannot be killed goes the wrong way**: first assume you have not yet found the
  distinguishing scenario (often it requires raising the concurrency of the same operation to 2, or
  making some path fail halfway), and only call it useless if you cannot find one. Before deleting a
  piece of defensive code, say what it defends against and who defends against that now.
- **Someone else's completion signal is not evidence about the world, and a wait edge that lands on
  "another caller of the same operation" can form a cycle.** The repository's second and third
  Criticals were introduced by the same fix, both from "trusting the other side when
  deduplicating": a `torn_down` event is set in a `finally`, so it means **the holder left**, not
  **the postcondition it promised holds**; when the holder is cancelled the two diverge, and the side
  that returns as soon as it wakes carries on with a false postcondition, producing the same kind of
  defect being fixed, with **zero logs**. The same fix also added the edge "wait for another caller"
  to an operation that had never blocked on a caller before, so two executors each stopped on a
  resource the other held, both calls never returned, and the stuck resources had no remaining way to
  be rescued. **Two criteria**: (1) what a deduplication guard waits for must be **the postcondition
  itself**; the signal only means "worth another look", and on waking it must re-check and **take
  over itself**; (2) before adding a blocking edge, ask whether it can form a cycle: waiting along
  your own **descendants** is safe (descendants form a tree), waiting on a **peer caller** is not.
  When neither deduplication nor waiting is necessary, the cheapest form is **each acts on its own +
  a receipt taken synchronously**: the action is idempotent so repetition costs nothing, and the
  "exactly once" part (report / log / callback) is decided by the receipt.

  It has happened twice: the author reported "every mutation red, zero survivors", and an
  independent reviewer refuted it immediately with a batch of mutations of their own. The reason was
  not laziness: **the author's mutation set leaned towards the paths they were thinking about at the
  time**. In the most typical case, the guard itself was written correctly, but no test ever took
  its most destructive branch (calling `put` with a task on an id already taken, overwriting the
  instance while dropping a task that may still be alive), and that was exactly the path the task's
  top ruling depended on. So "zero survivors" holds only for the given set of mutations, and the
  report must say so honestly.
- **In a sealed type (tagged union), "no two members inherit from each other" does not guarantee an
  instance belongs to only one.** Being siblings does not exclude a **common subclass**:
  `class Both(Confirmation, Reply)` can be constructed and hits both branches, so the premise "the
  order of branches does not affect the result" falls, while the assertion pinning it (no
  inheritance between any two) stays green; the assertion was not wrong, the proposition was one
  notch too narrow. **A structural fix beats an extra assertion**: instead of taking the first hit in
  order, count the hits (0 is an unknown type, ≥2 is ambiguous, exactly 1 gives the value); the
  answer then is a function of the **set** of hits, order is truly irrelevant semantically, and a
  reordering goes from "surviving mutation" to "truly equivalent mutation". Also: the ambiguity must
  be decided **before reading any field**; if a multiply-inherited subclass is not wrapped in
  `@dataclass` again, the second parent's fields do not exist at all, and reading a value produces a
  bare `AttributeError` instead of the clear ambiguity error.
- **A guard that can only be proven by a "joint mutation" can be deleted silently by the next
  refactoring.** Measured: the ambiguity error reports the matched type names `sorted`, so that
  reordering the list cannot change even the error text; but the list happened to be declared in
  lexicographic order already, so deleting `sorted` **alone** made no case speak, and only "reorder
  the list + delete sorted" applied together turned red. **Criterion**: every guard needs a mutation
  that turns red when applied alone; failing that usually means the fixture degenerated (here,
  declaring the list in non-lexicographic order is enough: one line, free, because the order does
  not affect the result anyway).
- **Dataclass parameters on a base class are shadowed by the subclass's own `@dataclass`.** Each
  subclass's own `@dataclass(frozen=True)` generates its own `__eq__`, so the base class's `eq=False`
  is completely invisible to cases that only test subclasses. This is another face of "a property
  pinned in one layer is not pinned at the seam"; the trigger is to extend the parametrised set of
  values **from the subclasses to include the base class**.
- **When every value in a fixture is a fixed point of normalisation, identity and `strip` / `lower` /
  `casefold` are the same function.** Measured: adding `.strip()` or `.lower()` to the correlation
  ID's output passed all 959 tests, because every ID in the tests was lowercase without whitespace,
  while downstream compares byte by byte, so with normalisation the reply never matches again. For
  any "passed through unchanged" property, the fixture needs at least one value that is not a fixed
  point (`'  Call-7  '`), plus a case that really goes through the downstream seam.
- **`bool` is a subclass of `int`, and YAML 1.1 parses `yes` / `on` / `true` as `True`.** So any
  numeric setting checked with `isinstance(v, int | float)` accepts `True` as 1: `timeout: yes`
  silently becomes a 1-second timeout that always times out without an error. Numeric settings must
  reject `bool` explicitly, and the rejection goes **before** the numeric check (after it, it is
  dead code). Pin the other end too: when only "reject invalid" is tested and not "accept valid",
  narrowing acceptance to `float` only (rejecting `timeout: 30` too) is equally green. **The widening
  direction must be pinned as well, and its symptom is more hidden**: with the parametrisation
  listing only `0 / -1 / True / '4'`, widening `isinstance(v, int)` to `isinstance(v, int | float)`
  stays all green, while `len(x) == limit` is never equal to any float, so `max_concurrency: 2.5` or
  `inf` removes the limit entirely (measured peak 8/8, i.e. no limit at all). **Parametrise the
  rejection side by "type family"** (boolean / string / numeric but not integer / out-of-range
  integer), not by "a few bad values that come to mind".
- **A performance / scheduling setting that can silently change a security conclusion is a
  first-class defect where concurrency meets security.** Measured: after tool calls ran in
  concurrent segments, raising `max_concurrency` from 1 to 4 disabled the forced escalation of spec
  §12.2 that "cannot be turned off" (`sudo rm -rf /` went from blocked to executed), and made the
  recording obligation of the low-risk cell disappear silently, because the result blocks of calls
  in the same segment land in the trajectory only after the whole segment finishes, so the
  provenance re-fetch inside the segment sees nothing. **Criterion: the same batch of input must
  reach the same security conclusion under `limit=1` and `limit=N`**, so every case related to a
  security conclusion is parametrised over the limit (at least the two values `{1, N}`), and there
  is **a separate** assertion "the same batch of calls leaves block-for-block identical trajectories
  under both limits": `limit=1` being block-for-block equivalent to the serial implementation is the
  only anchor for this class of regression; without it, any "casual widening of concurrency" can
  change a security conclusion while the whole suite notices nothing. The general form of this
  family: **any mechanism that claims "it only changes when things run, not what they produce" needs
  a case that actually asserts that sentence.**
- **Degrading to the safe side is also a loss, so it needs evidence too.** The known face is
  "an exhausted budget can only drop input"; the new face is a fallback:
  `except Exception: return False` degrades a declaration violation (a tool's `is_concurrency_safe`
  raising, or its signature not accepting `args` at all) into "this tool runs alone in every batch
  from now on". The direction
  is safe and the behaviour is pinned by a case, but **running serially neither raises nor changes
  the result**, so no level can detect it. The same repository's handling of "misspelled
  `__tool_command_args__`" is to fail loudly, with the stated reason "a gate that disappears silently
  is much harder to find than an error"; the same reason applies to every degrading fallback, and "a
  wrong signature" deserves less tolerance than "a misspelled parameter name".
- **Diagnostic machinery introduced for one failure mode, installed on only some of the cases that
  need it, leaves the others as they were under that mode.** Measured: the file has a
  `_SEGMENT_DEADLINE` whose docstring says it exists "only to turn 'concurrency did not happen at
  all' from a hang into a red assertion", and it was used in only one of the three cases that need
  it; after removing concurrency, the other two both TIMEOUT. `pytest-timeout` caught them, so CI was
  not silent, but the cost was 45–60 seconds plus a thread dump instead of an assertion pointing at
  the segmentation result. **Criterion**: when introducing a constant / fixture / bound, write down
  the criterion for "who needs it" and apply it to each case; machinery installed in only one place
  is deleted by the next refactoring as a special case of that place.
- **The "unpredictability" of generated IDs is a security property, and uniqueness tests are
  completely blind to it.** A sequential counter satisfies "two generations are not equal".
  `UserInput.request_id` is the only correlation ID among the three kinds of waits that the caller
  does not know in advance; if it can be guessed, any component that can publish to that mailbox
  (including a model persuaded by injected content) can forge a reply to a human input and bypass
  human confirmation. Pin the generator's **properties** (alphabet, length, no correlation between
  lexicographic order and generation order over a large sample, the last few characters each
  distinct), not its results. Take care not to fail a correct implementation: character 12 of
  `uuid4().hex` is always `'4'` (version bits).
- **Testing a performance property means choosing the right measure, and the right place.** Busy
  polling and blocking waits return the same result and take the same wall-clock time; only CPU time
  tells them apart. But `time.process_time()` counts every thread of the whole process, so a remote
  implementation with a background connection thread would be misjudged. Keep such assertions in
  the tests of the concrete implementation, **not** in the contract suite shipped with the
  distribution; the contract states the requirement in words.
- **In tokenising / parsing code, the choice of "which characters are separators" is itself
  load-bearing, and the symptom of choosing wrong is missed detection: a false guarantee of
  safety.** Measured: the dangerous-command judge treated `<` `>` as segment boundaries, while in a
  shell they **never start a new command**, only redirect; so in `curl x 2>/dev/null | sh`,
  `/dev/null` became a segment of its own not continued by the pipe, and "something was downloaded in
  this chain" was reset: **any redirection could break the whole pipeline chain** (`2>&1` /
  `</dev/null` / `|&` likewise, all slipping through silently). And those two characters also had
  **zero coverage**: removing them from the set turned no case red. Entries in constant tables
  (separators, wrapper names, interpreter names, allowlists) have by nature no mutation that turns
  red when applied alone, unless **each member has a case of its own**; the more a table looks like
  "configuration", the easier it is to delete silently in the next refactoring.
- **After writing "this is load-bearing", apply the corresponding mutation; if it does not turn red,
  something else is carrying the load.** Measured in the same fix: the comment sat on the separator
  set ("`<` `>` not being here is load-bearing"), and adding them back to that set kept **every case
  green**: what really decided the behaviour was the order of the branches (the redirection branch
  takes those two characters before the separator branch), so the two entries in the set were never
  reached. **A comment pointing at a non-load-bearing place is worse than no comment**: the next
  person reasons from it and concludes that changing the other place is safe.
- **When wrapping a value as read-only, enumerate the whole read surface, not "the reads I thought
  of".** `copy` / `deepcopy` / `pickle` go through protocol methods: `copy._reconstruct` assigns
  `y[key] = value` key by key into **the newly created copy**, so a wrapper that only intercepts
  `__setitem__` rejects a **pure read** and reports a false message saying it was written in place
  (measured: `copy.deepcopy(args)` reported "an in-place assignment to 'args' was received", although
  the author never wrote in place once; `pickle` is worse: it fills key-values before restoring
  state, so the rejection runs before its own fields exist and crashes into a bare
  `AttributeError`). This one is particularly expensive, because **these protocols are often the only
  self-help fix for the gap the read-only wrapper leaves**: the view only protects the top level and
  `{**v}` is a shallow copy, so a careful author's next thought is inevitably `deepcopy`.
  **Criterion**: read-surface cases cover the protocol layer (`copy` / `deepcopy` / `pickle` /
  `reversed` / `|` / `fromkeys`), not just indexing and `.get`; what each "give me another copy"
  protocol returns is pinned as a contract (which are shallow and which deep must be pinned
  separately, otherwise the two ids are the same assertion). Also: `__slots__` makes `vars(view)`
  raise instead of handing out a writable `__dict__`, so who the diagnostic points at cannot be
  forged either.
- **On a chain of one-directional references, any "unique across the chain" invariant holds only
  in the direction of the references.** Measured: the classification-conflict check of
  `HooksCenter(outer=...)` walks "this layer → outer layer", and the docstring accordingly said "a
  name has at most one classification on a chain"; but the outer layer has no reference to the inner
  layer, so **an outer layer declared later** can fork a name the inner layer already declared into
  two classifications, and the same callback runs under two sets of semantics under the two layers'
  `trigger` (one swallowing exceptions, the other taking `skip` seriously). The one-directionality is
  intentional (no reverse write surface = the assembly chain cannot form a cycle), so this is not a
  check that can be completed. **Criterion**: before writing "unique across the chain / globally",
  ask which end the check starts from, and whether the other end can be added later; when the answer
  is "yes", the docstring may only state the **directional** constraint, and must say which direction
  is the dangerous one and whether it is blocked.
- **"Validate everything before acting" only covers the errors the kernel itself can validate.**
  Measured: `install_plugins` makes two passes (first validate each item, then register each one), and
  the second pass calls third-party `register`; when it raises, the earlier plugins are already
  attached, and the center has no way to detach them, so the docstring "a rejected assembly has no
  effect" was false. **Criterion**: in any two-pass "validate, then execute" structure, as soon as the
  second pass contains a single **third-party call**, either roll back from a private snapshot, or
  narrow the promise to what can be kept (and write down what the rollback cannot catch: what the
  third party did outside your objects). Rollback catches `BaseException`, not `Exception`:
  `KeyboardInterrupt` / `CancelledError` coming out of third-party code leaves the same half-assembled
  state. Put the snapshot / restore in the class of **the object being restored**, do not let callers
  reach into its private fields: otherwise the next person who adds state to it cannot find the
  place where restoring happens.
- **A truncation made to keep an invariant is at the same time a path of information loss.** To keep
  the invariant "tool calls paired with results", `Fork` must cut the trailing unpaired batch, and
  **the cut blocks may carry the only security marker in that trajectory**. The criterion: **at every
  place that drops data, ask "what does it carry that exists nowhere else"**, and the compensation
  must stay **at the place where the dropping happens**: after returning, the fact that something was
  dropped no longer exists, and downstream has no way to make up for it (measured: `spawn` already
  compensated on the path with a `query`, and its docstring states exactly this reason, while the
  `query=None` path still missed it; the downstream entry "let the caller pass `UNTRUSTED` itself"
  cannot fix it, because the caller cannot know that a block was just dropped). **The same truncation
  has a second pitfall**: it runs on **the upstream data**, so any in-place write
  (`head.tool_calls = []`) writes into the upstream; a rewrite that keeps an invariant must be made on
  a copy (`replace` / deep copy), otherwise the batch of declared calls disappears from the source
  agent's trajectory. Both are pinned by one case each: "two entries into the same code" and "whether
  the upstream was changed".
- **A conservative marker that is "safe in direction" becomes a permanent source of false positives
  the host cannot turn off once its scope is the whole trajectory and the escape hatch is not
  implemented.** Measured: the block for an interrupted delegation wait was unconditionally marked
  `UNTRUSTED`, although it only embeds text written by the kernel and an id generated by the kernel,
  without a single external byte; after one legitimate `abort`, that block landed in the trajectory,
  and from then on **every** high-risk call on a host that had explicitly declared `AllowAll` was
  blocked, because provenance is the upper bound "it appeared, so it counts". Another path in the same
  module (reading real provenance) gave the opposite answer for the same input. **Criterion**: before
  putting down a conservative marker, ask two questions: "does a single byte of this content come
  from outside", and "how long is this marker's scope, and who can turn it off". If both have bad
  answers, being conservative is not the safe side.
- **When every generation runs in its own `asyncio.Task`, the stack does not accumulate, so any
  argument of the form "it will fail loudly once it gets deep enough" does not hold.** Measured: a
  leaf at delegation depth 12 and an independent agent measured the same stack depth
  (`len(inspect.stack())` was 16 for both), because `spawn` starts child agents with `create_task`. A
  leftover item once used "it will hit Python's recursion / frame limit first" as the fallback for
  "so unbounded growth does not matter", while in reality **no depth ever fails**; what grows is the
  number of tasks, registry entries and a chain of waits each blocked on the next, all silently.
  **Criterion**: fallbacks of the form "it will hit some limit first" must always be measured, never
  reasoned.
- **Public surface: adding a public name is not a breaking change, removing one is, so "private
  first, release it when a third party needs it" is strictly better than "public first, take it back
  in the next task".** And a verbatim guard on `__all__` is completely blind to **methods added to a
  class** (it only exports the class name, one string), so one "casually add a public method"
  enlarged a surface the spec deliberately limited to 11 to 16, and nothing turned red.
  **Criterion**: for any type whose "public surface is deliberately limited to N", the guard
  enumerates the non-underscore names of **a real instance** and compares them verbatim; enumerating
  the class misses instance attributes assigned in `__init__`, and those are often exactly the ones
  the host reads and writes.
- **A probe used to prove "this did not happen" can itself make it happen.** Measured: to prove
  `QueryAgent` did not wake a ZOMBIE, the most natural way is to `broker.publish` a message to its
  mailbox and assert `ok=False`; but the `on_missing_queue` callback of `publish` is the repository's
  **only** revival entry, so the assertion is bound to fail, and the failure is caused by the probe.
  Same family: any read with a default side effect (activate by default, create resources by
  default, retry by default) cannot be used as an observation. The criterion: **the probe takes the
  path that explicitly turns the side effect off** (here `send(activate=False)`), and **asserts both
  sides**: asserting only the registry state lets the mutation "wake it up after querying" survive on
  the mailbox side.
- **"Skip an option" and "skip its value" are two different things, and confusing them swallows the
  real command word.** Measured: command-word parsing skipped tokens starting with `-` but not their
  values, so in `nice -n 19 rm -rf /`, `19` became the command word and `rm` was never seen again:
  **not a single gate** under the default rule table. And "which option takes the next word" cannot
  be decided from syntax (the `30` in `timeout 30 rm` is not an option value at all, it is a
  positional argument), so after such prefixes the assumption "the first non-option word is the
  command" must be dropped. For any criterion where **both widening and narrowing go wrong**, each
  direction needs cases: widening causes false positives, narrowing causes missed detections, and
  testing one side locks only half.
- **Items that are in the spec but that no task is responsible for delivering are structurally
  invisible to single-task reviews.** Each round looks only at its own diff, and a silent absence is
  in no diff. A cross-cutting audit found three at once (`created_at`, `runtime_start`, step 9 of
  §5.1 "check for abort again"), and step 9 had live security consequences: the abort documentation
  treated "honoured between turns" as the whole story, and tools declared within a turn still ran.
  Countermeasure: when closing each plan, grep the implementation for the spec's signatures and
  field tables **verbatim**, instead of ticking a self-check list.
- **Entry validation discipline must reach every public constructor entry, not only the two written
  most carefully.** In the repository, `validate_max_concurrency` and `_validate_timeout` first
  reject bool, then check the type, then the range; four other public entries
  (`EventBus.capacity`, `Runtime(registry=/broker=)`, `spawn(metadata=)`, `DangerJudge`) had none of
  it. The symptoms come in two grades: YAML `capacity: yes` silently degrades to capacity 1 and drops
  events; `DangerJudge('abc')` raises `AttributeError` from inside the error formatter, and the
  traceback points at the formatter instead of the broken rule table. Two traps the same repository
  had already blocked (`plugins._is_a_class`'s "forgot the parentheses", `_validated_sequence`'s
  str-is-a-Sequence) slipped through unchanged at adjacent entries.
- **Without concurrent cases, a CAS contract "looks right" under the in-memory implementation.** Spec
  §13.3 names CAS as the foundation of concurrent correctness; all three CAS cases in the suite were
  sequential. A store that does `await asyncio.sleep(0)` between read and write (the round trip a
  remote implementation is bound to have) passes the whole contract, while what spec §4.4 wants to
  prevent is exactly the kind of loss where "a pause collides with an abort". Cardinality 3: n=2 has a
  degenerate solution. Once the contract ships in the wheel and third-party stores prove compliance
  against it, adding requirements is a breaking change.
- **If the receipt of an idempotent operation only pins how many times it fired, it cannot stop an
  early return.** Three concurrent `shutdown` calls still report the hook only once, but
  `if self._closed: return` at the top of the method satisfies "reported once" just as well: the
  second call returned while the first was still stuck in the RUNTIME_SHUTDOWN hook, and the bus was
  not closed. Countermeasure: make the second call while the first is stuck in the hook, and after it
  returns, an emit must not reach a still-open subscription (the bus is closed); do not just count
  hooks.
- **When the control group comes from the same source as the code under test, the comparison is
  always equal.** In the "hot versus cold" case of the fingerprint cache, the cold side ran the same
  implementation: removing the role from the cache key made both sides collide on the same key and be
  wrong together, the comparison was still equal, and the mutation survived. The truth must come from
  a path unrelated to the mechanism under test (computed block by block, separately). Same family: a
  hand-written replay document may have a shape the kernel cannot produce (a message arrived but was
  never handled, an empty waiting list), and right or wrong measured in an impossible world counts for
  nothing: shapes come from real captures; hand-writing is only for making bad documents captures
  cannot produce.
- **Load stretches a "less than a millisecond" window until it can be reproduced.** Signals arrive
  late under load: the window between handing back the conclusion and printing it was estimated at
  under a millisecond when recorded, and with several cores saturated it broke 2 times in 25. Design
  windows involving key presses, signals and shutdown as "may arrive at any moment"; to judge whether
  a fix works, loop it under load (`yes > /dev/null` saturating several cores), do not look at one
  green run. The first fix closed only one of the windows; the second round of looping found the
  other.
- **Wait for an event, wake up and then read, and you may read the next one.** Measured: `Delegate`
  waited for the child agent's driver task to end and only on waking read its live Context; a child
  that went into zombie can be woken, and when the revived cycle is scheduled before the waiter wakes
  (in replay the outside world takes no time, so it always is; in real runs it can be too), what is
  handed back is the next cycle's conclusion. If you wait for an event, read the value at the moment
  of that event: at the very start of `_finish`, before going into zombie, the kernel saves a snapshot
  per driver task. Criterion: if the awaited object can still be changed by someone else between
  "done" and "the waiter wakes", do not read it after waking.
- **Before reinterpreting a kernel call as "wait", count how many callers it has.** Measured: the
  replay broker stretched the `wait_for_messages(timeout=0)` at the end of a cycle into "wait for the
  next turn's message in the record", and `_finish` asks the same question when recording dropped
  input; the reaping task of a killed agent then waited for a message that is only sent after the
  second kill lands, and the second kill was waiting for it to stop: deadlock; after it stopped, the
  shutdown was still waiting for that reaping task. As many callers as a signature has, that many
  cases must reach them; a stretched wait must be released on stop.
- **A case that starts a subprocess tests different code under a relative `PYTHONPATH`.** The
  subprocess has a different working directory, the relative entry does not resolve, and Python falls
  back to the copy installed in the environment (an editable install pointing at the live working
  tree). Measured: a reviewer ran mutations on an exported copy, and the Ctrl-C case imported the live
  working tree's code; several mutations were falsely killed by it and real survivors were hidden.
  `tests/conftest.py` replaces the entries with absolute paths at the start of the session; mutation
  and export verification still pass absolute paths.
- **The substring of a negative assertion (`x not in out`) must not be something a random id can
  spell.** Measured: the assertion "the header value `caf%C3%A9` was not echoed" was written as
  `'caf' not in err`, while stderr contains the 8 random hex digits of the trace directory name, and
  `c` `a` `f` are all hex digits: about one in a hundred full runs over nine parameters hits it, and it
  only shows up in re-checks on busy hosts that run a lot. A negative assertion's probe value must
  contain letters outside hex (`quiz`), or replace the ids in the output before asserting.

---

## Common commands

```bash
uv sync                  # install dependencies
uv run pytest            # run the tests
uv run ruff check .      # lint
uv run ruff format .     # format
uv run pyright           # type check
uv run python scripts/check_deps.py   # dependency direction
uv run python scripts/check_links.py  # relative Markdown links
sh scripts/check_docs.sh              # the docs site, strict build
```

## Where the docs are

- **[spec](docs/design/2026-09-03-architecture.md)**: the complete architecture design. Read the
  relevant section before changing something; **§17 is a quick reference of 89 decisions, check it
  first**.
- The **reasons** for every constraint are in the spec. This guide only gives conclusions and does
  not repeat the reasoning.
- [`docs/user/`](docs/user/index.md): the English user documentation, built with MkDocs
  (`mkdocs.yml`). Every code block in it and in `README.md` is executed or parsed by
  `tests/test_docs.py`, or marked `<!-- not-tested: reason -->`.

## Code conventions

- **Comments only state hard constraints the code cannot express**; no change history, no "why I
  did not do it that way", no defence addressed to a reviewer. Those belong in the commit message.
- A test case's name is its assertion; if the name says it, do not add a comment.
- New code matches the comment density of its surroundings.
- Code comments, runtime error messages and CLI output stay in Chinese in v1.0; the user
  documentation and this guide are in English.
