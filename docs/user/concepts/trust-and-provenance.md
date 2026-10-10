# Trust and provenance

Prompt injection is the main way an agent ends up doing something nobody asked for: it reads a web
page, a file or a command's output, and the text talks it into an action. Rule-based danger checks
cannot catch "the agent was persuaded and then wrote a perfectly normal-looking upload script". So
Syndica tracks **where content came from**, and uses that to escalate risky calls.

## Trust levels

Every block in an agent's history and every tool result is `trusted` or `untrusted`. Untrusted
content enters the context wrapped and labelled as data, not instructions.

Untrusted by default:

- the output of commands (`Bash`, `Terminal`);
- files a command changed, files changed outside Syndica, and files written after the agent read
  untrusted content (file taint, tracked per file);
- everything that arrives through a protocol: MCP tool results, hosted ACP agents, remote A2A
  agents, tool results sent back by OpenAI-compatible HTTP clients;
- tool results of third-party Runtime Modules, and results their hooks write for other tools.

There is no switch that marks an external tool or agent as trusted. For a third-party module,
`--trust-module-output <name>` on `run` or `chat` lifts the marking for that one run; a project
configuration cannot.

## The marking is transitive

Untrusted content stays untrusted after it is summarised, compacted, consolidated into memory or
handed to another agent. This is the rule that is easiest to break: compaction rewrites untrusted
text into something that reads like a conclusion. Concretely:

- compaction keeps the marking on everything derived from untrusted blocks;
- untrusted content never reaches global memory;
- an acceptance verdict given by a verifier that read untrusted content is itself marked
  untrusted, and exported training samples carry the flag.

## Escalation

When a high-risk tool call can be traced back to untrusted content, its disposition is escalated,
and the model can never approve it on its own:

| Risk | Traceable to untrusted content | Disposition |
| --- | --- | --- |
| High | Yes | The `untrusted_high_risk` flag: `reject` (default) or `ask` |
| High | No | The host's disposition table (see [Danger judgement](danger.md)) |
| Low | Yes | Allowed and recorded |
| Low | No | Allowed |

`reject` is the default because refusing is more useful than waiting: the agent gets a clear
"refused because it traces back to untrusted content" and can take another path, such as writing
the result into the workspace for a person to review. Waiting for a person who is not watching
only burns a timeout. The CLI refuses `untrusted_high_risk=allow`, and a project configuration may
only set `reject`.

In practice, with the CLI's development disposition table: a command that touches a path outside
the workspace asks you while the agent has not read any command output yet. After that the
trajectory counts as untrusted and such commands are rejected; `--flag untrusted_high_risk=ask`
asks you instead.

Running a local script whose content is untrusted (because a command or an external edit changed
it, or it was written after untrusted content was read) forces the escalation even under an
allow-all policy.

## Honest limits

- **Unattended runs that read untrusted content cannot do some high-risk operations.** That is a
  known, deliberate boundary, not a bug.
- Command output itself stays untrusted, but a command can still read anything your user can read.
  Provenance tracking narrows what a persuaded agent can do through Syndica's own tools; the real
  isolation boundary is a sandboxed workspace backend. See the [security model](../security.md).
