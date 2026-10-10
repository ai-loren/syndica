# Troubleshooting

## How errors read

In v1.0, error messages and CLI output are in Chinese; English messages are planned for v1.x. Every
error that Syndica raises for you to act on has the same three parts, so it can be acted on without
reading the source:

| Label | Means |
| --- | --- |
| `期望:` | **Expected**: what Syndica needed. |
| `实际:` | **Received**: what it got instead. |
| `下一步:` | **Next action**: what to do about it. |

For example, running with no model configured:

<!-- not-tested: real CLI output, shown for reference -->
```text
期望: OPENAI_BASE_URL 是非空的 OpenAI-compatible API 根 URL
实际: 收到 OPENAI_BASE_URL=None
下一步: 设置 OPENAI_BASE_URL，例如 https://host/v1，再重试 run/chat/doctor；用 Gemini 或 OpenRouter 就改设 GEMINI_API_KEY 或 OPENROUTER_API_KEY
```

Expected: `OPENAI_BASE_URL` is a non-empty OpenAI-compatible API root URL. Received:
`OPENAI_BASE_URL=None`. Next: set `OPENAI_BASE_URL` (for example `https://host/v1`) and retry; to use
Gemini or OpenRouter, set `GEMINI_API_KEY` or `OPENROUTER_API_KEY` instead.

A headline line may come first, such as `Agent Orchestrator（<id>）以错误结束：` ("agent
Orchestrator ended with an error:"). Errors that come from third-party code are redacted before they
are shown. The same three-part structure is used in tool results the model reads, in HTTP error
bodies, and in protocol errors.

## Common messages

| Message (Chinese fragment) | Meaning | What to do |
| --- | --- | --- |
| `期望: 参数符合当前命令的 usage` | The command line does not match the command's usage. | Run `syndica <command> --help`. Options of `run` go after `run`; only `--config` goes before the command. |
| `收到顶层选项 … 不会猜测它属于哪个子命令` | An option came before any command. | Write the command first: `syndica run --agent Worker "..."`. |
| `工作区之内的配置 … 经人确认之后才用` | The workspace's `.syndica.yaml` is new or changed and has not been confirmed. | See [Trust prompts](#trust-prompts). |
| `信任这份配置并继续？[y/N]` | "Trust this configuration and continue?" | Read the summary above it, then answer `y` or anything else to refuse. |
| `配置根只含 …` / `未知键 …` | An unknown key in `.syndica.yaml`. | Fix the spelling; unknown keys are never ignored. See the [schema](reference/configuration.md). |
| `flags: 的 key 是已装 Module 声明的 Flag 之一` | A flag that no installed module declares. | Check `syndica flags list`; enable the module that declares it in `modules:`. |
| `项目配置只能收紧` | "A project configuration can only tighten." | Loosen a safety setting only on your own command line (`--flag ...`), or not at all. |
| `轮换中的平台 … 失败（连续第 N 次），接下来 … 秒跳过它` | A model platform failed for the Nth time in a row and is skipped for a while (a warning, not an error). | Read the reason after the colon; see [Model provider errors](#model-provider-errors). |
| `期望: 平台 … 在 120 秒内答出这次请求` | No platform answered within the request's wait limit. | Fix the cause named for each platform (key, model name, quota). |
| `期望: CLI 请求对应的 Agent 用一段文字回答这次请求` | The agent ended without a text answer; the received part says why (out of turns, out of budget, aborted, error). | Raise `--max-turns` or the budget, split the task, or fix the error listed above it. |
| `注意: Agent 用完了轮次预算（max_turns）才停下` | The answer printed is the agent's last text before it ran out of turns and may be incomplete. | Raise `--max-turns` and run again. |
| `Runtime 错误` | A delegated agent failed, even though the root agent may still answer. | Read the entry; the root agent's answer may be partial. |
| `预算：轮次 a / b；…；停止原因 completed` | The budget summary: turns used of the limit, tokens, cost, stop reason. | Nothing; it is printed after every run. |
| `trace（不完整…）` | The trace lost records. | It cannot be replayed or exported; the rest of the run is unaffected. |
| `被中断（Ctrl-C）：命令可能没有做完` | Interrupted with Ctrl-C; the command may not have finished. | For `run --handoff`, trust the handoff file named on stderr; otherwise rerun. |
| `这个工作区没有在跑的 Web 服务` | `syndica status` / `down`: no server is running for this workspace. | Start one with `syndica up`. |

A tool call refused by the danger judge reaches the model as a tool result naming the rule ID (for
example `shell.recursive_force_remove`) and the reason, so the agent can take another path. A call
refused because its arguments trace back to untrusted content (`可溯源到不可信内容`) is explained
in [Trust and provenance](concepts/trust-and-provenance.md#escalation).

## Trust prompts

The first `run`, `chat` or `doctor` after a `.syndica.yaml` inside the workspace is added or changed
shows what the file would do (prompt files, `impl:` classes, verify commands, `pass_env`, models,
modules, flags, safety settings, interop targets) and asks `[y/N]` on the terminal. Only an
interactive terminal counts: piping `yes` does not confirm.

Without a terminal (CI, a script, `syndica acp`), the command stops and prints the configuration's
digest. Review the file, then:

```bash
syndica config trust --sha256 <digest>
```

If the file changed after you copied the digest, the command says so and prints the new one. The
digest also covers prompt files and `impl:` code inside the workspace, so editing those asks again.
See [Configuration](guides/configuration.md#confirming-a-project-configuration).

## Missing extras

A feature whose extra is not installed stops with the `pip install` line to run, for example
`pip install 'syndica[server]' 之后再运行 syndica up` ("run syndica up after
`pip install 'syndica[server]'`"):

| Feature | Extra |
| --- | --- |
| Any model call | `openai` |
| `syndica up`, the web UI, the OpenAI-compatible HTTP endpoints | `server` |
| `syndica trace export`, OTLP export at the end of runs | `otel` |
| `syndica mcp`, `/mcp`, roles with MCP servers | `mcp` |
| `syndica acp`, `model: acp:<name>` | `acp` |
| `/a2a/`, `model: a2a:<name>` | `a2a` |
| `syndica.backends.trl` dry runs | `trl` |

The model client is the one exception without a dedicated message: without the `openai` extra a run
stops with `实际: ModuleNotFoundError: No module named 'httpx'`. Install `syndica[openai]`.

## Model provider errors

Every failed platform request is logged on stderr as a warning, with the platform, how many times
in a row it failed, how long it is skipped for, and the provider's reason.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| HTTP 401 or 403, or the error mentions an API key | Wrong, expired or missing key | Fix `OPENAI_API_KEY`, `GEMINI_API_KEY` or `OPENROUTER_API_KEY`. Syndica does not wait for these errors to recover. |
| HTTP 400 or 404 | Unknown model name, or a request feature the model does not support (for example tool calling) | Fix `OPENAI_MODEL` / `GEMINI_MODEL` / `OPENROUTER_MODEL`, or pick a model that supports tools. |
| HTTP 429 | Rate limit or exhausted quota | Wait; a platform that says when to retry is skipped until then. Add another platform to the rotation, or keep `--max-turns` small on free tiers. |
| `ConnectError` | Wrong `OPENAI_BASE_URL`, or the local server is not running | Check the URL (it usually ends in `/v1`) and that the server is up. |
| `OpenRouter 只使用 openrouter/free 或以 :free 结尾的免费模型` | A paid OpenRouter model was named | Use `openrouter/free` or a model id ending in `:free`. |
| `清单 model 的平台前缀是 … 之一` | A role's `model` has an unknown prefix, such as a tagged model `qwen3:8b` | Write `openai:qwen3:8b`, or another `<platform>:<model>`. |
| `清单的 model 走通用的 OPENAI_* 端点之前，你自己的环境选了它` | A role names a model that would go to the generic `OPENAI_*` endpoint, but your environment chose Gemini or OpenRouter | Add `openai` to `SYNDICA_LLM_ORDER`, or name the model with its platform. |

`syndica doctor` checks the model configuration without running a task. See
[Model providers](guides/models.md).

## Still stuck

- `syndica doctor` checks the configuration, models, workspace, the agents' environment, Git,
  Python and the declared interop objects, one line each.
- `syndica trace show <id>` shows where a run spent its time and which calls failed.
- Report bugs on the [issue tracker](https://github.com/ai-loren/syndica/issues). Do not paste
  secrets: traces and handoff files are redacted, but your terminal output may not be.
