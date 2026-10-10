# Quickstart

Five minutes from an installed `syndica` to a finished run, its trace, and the web UI. You need
the `openai` extra for the model client and the `server` extra for the last step (see
[Install](install.md)).

## 1. Point Syndica at a model

Any OpenAI-compatible endpoint works: a hosted API, or a local server such as vLLM or Ollama.
Export its base URL, the model name and, if the endpoint needs one, a key:

```bash
export OPENAI_BASE_URL=https://api.example.com/v1
export OPENAI_MODEL=your-model-name
export OPENAI_API_KEY=your-key
```

Keep keys in your environment. Syndica never reads `.env` files, and a key file inside a workspace
can be read by the agents' `Read` and `Grep` tools and sent to the model provider.

The CLI looks at Gemini and OpenRouter first: if `GEMINI_API_KEY` or `OPENROUTER_API_KEY` is also
set in your shell, those platforms are used and `OPENAI_*` is not. To use only the endpoint above,
name it explicitly:

```bash
export SYNDICA_LLM_ORDER=openai
```

[Model providers](guides/models.md) covers Gemini, OpenRouter's free models, using several
platforms with failover, and naming a model per role.

## 2. Check the setup

Work in an empty directory: agents can read and write everything inside the directory you start
them in, which becomes their workspace.

<!-- executed: quickstart -->
```bash
mkdir syndica-demo
cd syndica-demo
syndica doctor
```

`doctor` prints one line per check. `config: ok .syndica.yaml` appears even without the file: with
no `.syndica.yaml` the built-in roles are used. `models: ok default` means the default model could
be built from your environment. Output is in Chinese in v1.0; for example `git: 非 git 仓库` means
"not a git repository", which is fine here.

## 3. Run a task

<!-- executed: quickstart -->
```bash
syndica run --agent Worker "Write a four-line poem about version control to poem.md" --max-turns 10
```

- `--agent Worker` sends the task to the built-in `Worker` role, which can read, write, edit and
  run commands. Without `--agent`, the `Orchestrator` receives it and delegates to `Planner`,
  `Explorer` and `Worker`.
- `--max-turns` is the budget of the whole agent tree (default 200). Each model call is one turn,
  and a delegated agent's turns come out of its parent's budget.
- `syndica "a prompt"` with no subcommand is the same as `syndica run "a prompt"`.

The final answer goes to stdout. A budget summary and the trace directory go to stderr:

<!-- not-tested: sample output; the numbers and the id differ on every run -->
```text
预算：轮次 2 / 10；token 输入 1830（缓存命中 0）、输出 112；成本 未知；停止原因 completed
trace：.syndica/traces/20261010T032424Z-57219b12
```

That is: turns used 2 of 10, input and output tokens (cache hits in brackets), cost unknown (no
price is configured), stop reason `completed`. `poem.md` is now in the directory.

If the agent wants to run a command the danger judge considers risky, for example one that touches
a path outside the workspace, the CLI shows the call and asks you to allow it once (`y`) or refuse
it. See [Danger judgement](concepts/danger.md).

## 4. Look at the trace

Every run is recorded under `.syndica/traces/`. List the traces, then show one by the id in the
second column (a unique prefix or the directory name printed after the run also works):

<!-- executed: quickstart -->
```bash
syndica trace list
syndica trace show <trace-id>
syndica trace replay <trace-id>
```

`trace show` prints, in this order: who created whom (谁创建了谁), who waits on whom (谁在等谁), the
slowest tool calls (最慢的工具调用), model calls per agent (模型调用), a timeline (时间线), budget
events (预算), long-horizon events (长程) and which modules gave each agent which tools (Module).

`trace replay` runs the recorded trace again offline: model replies and tool results come from the
record while the kernel runs for real, and every step is compared. Exit code 0 means it
reproduced. See [Observability](guides/observability.md).

## 5. Open the web UI

<!-- executed: quickstart -->
```bash
syndica up
```

`up` starts a local server for this workspace in the background, prints a one-time login link
(valid for two minutes, usable once) and opens it in your browser. Start a session, chat with the
agents, watch the agent tree and the event timeline, answer confirmations, and look at artifacts.
The server listens on `127.0.0.1` only. Stop it when you are done; sessions are saved first:

<!-- executed: quickstart -->
```bash
syndica down
```

## Next steps

- Keep talking to an agent with `syndica chat`, or continue a saved conversation with
  `syndica sessions list` and `syndica sessions resume <id>`.
- Give the project its own roles, topologies and checks in a
  [`.syndica.yaml`](guides/configuration.md). The first run after you add or change one asks you to
  confirm it.
- Turn on [acceptance](concepts/artifacts-and-acceptance.md) so each run ends with a verdict.
- Read the [security model](security.md) before pointing agents at a repository you care about.
