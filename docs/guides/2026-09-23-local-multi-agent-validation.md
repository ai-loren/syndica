# Syndica 本地 Multi-Agent 体验与验证流程

本文用于在本地快速体验 Syndica 里程碑 1 的 multi-agent 能力，并判断每一步是否符合预期。

当前状态：Syndica 已支持真实 OpenAI-compatible / OpenRouter 模型调用，也支持不联网的 scripted L2
场景回放。真实模型体验依赖 OpenRouter API key 与免费额度；离线验证不消耗额度。

---

## 0. 进入项目并同步依赖

目的：确认本地 Python 环境、依赖和 CLI 入口可用。

```bash
cd /Users/loren/Documents/loren/syndica
uv sync --extra openai --extra dev
```

符合预期：

- 命令正常结束。
- `.venv` 可用。
- 没有 dependency resolution / install error。

不符合预期：

- `uv: command not found`：先安装 `uv`。
- 依赖解析失败：通常是网络或 Python 版本问题；Syndica 目标运行时是 Python 3.11+。

---

## 1. 配置 OpenRouter 免费模型

目的：给真实模型体验提供 API key，同时避免把 key 写入仓库、shell history 或文档。

```bash
read -rs "OPENROUTER_API_KEY?OpenRouter API key: "
echo
export OPENROUTER_API_KEY
export OPENROUTER_MODEL=cohere/north-mini-code:free

unset OPENAI_BASE_URL OPENAI_MODEL OPENAI_API_KEY
```

符合预期：

- 输入 key 时终端不回显。
- 关闭当前终端后环境变量失效。
- `OPENROUTER_MODEL` 以 `:free` 结尾，符合 Syndica 的免费模型门禁。

不符合预期：

- 把 key 写进 `.syndica.yaml`、README、脚本或 Git tracked 文件：这是凭证泄露风险。
- 同时保留 `OPENAI_BASE_URL` 等旧变量：可能导致排查时混淆模型来源。上面的 `unset` 会清掉它们。

说明：

- key 已经存在某个文件里时，用 `set -a; source <文件>; set +a` 导入当前 shell。这个文件不要放在
  Agent 会操作的工作区里：`Read` / `Grep` 读得到它，key 会随请求发给模型提供方。
- `cohere/north-mini-code:free` 是一个可用于首轮验证的免费模型示例。OpenRouter 免费模型列表会变化；
  如果该模型不可用，改用 OpenRouter 当前模型列表中标记为免费且支持 `tools` 的模型。
- 免费档每天只有 50 次免费模型请求，每个 Agent 的每一轮就是一次请求。下面的命令都用
  `--max-turns` 给**每个** Agent（含委派出去的子 Agent）设了上限，按顺序跑完一遍大约 20 次请求。
- 如果只设置 `OPENROUTER_API_KEY` 而不设置 `OPENROUTER_MODEL`，Syndica 会默认使用
  `openrouter/free` 路由器；路由器更方便，但具体失败原因有时不如显式模型好诊断。

---

## 2. 检查 CLI 组合是否可用

目的：验证 Syndica CLI 能加载默认配置、构造模型对象和定位 workspace。此步骤不应消耗真实模型额度，
因为它不发起 LLM `chat/completions` 请求。

```bash
uv run python -m syndica doctor
```

符合预期：

```text
config: ok .syndica.yaml
models: ok default
workspace: ok /Users/loren/Documents/loren/syndica
```

不符合预期：

- `OPENROUTER_API_KEY 未设置或为空`：重新执行第 1 步。
- `OpenRouter 只使用 openrouter/free 或以 :free 结尾的免费模型`：说明模型名不是免费模型，改回
  `cohere/north-mini-code:free` 或其他 `:free` 模型。
- `OPENAI_BASE_URL=None`：说明没有进入 OpenRouter 模式，通常是没有设置 `OPENROUTER_API_KEY`。

---

## 3. 验证真实模型基础调用

目的：先验证最小 LLM 调用闭环，排除 API key、额度、模型名和网络问题。

```bash
uv run python -m syndica run "用三句话介绍你现在能做什么" --max-turns 3
```

符合预期：

- 终端输出一段自然语言回复。
- 不出现 traceback。
- 不出现 `Context.stop_reason 是 error`。

不符合预期：

- `HTTP 401`：API key 不正确或已失效。
- `HTTP 429 Too Many Requests` 且包含 `free-models-per-day`：OpenRouter 免费日额度耗尽。等待
  `X-RateLimit-Reset` 指定时间、换未耗尽的 key，或给 OpenRouter 充值提升免费额度上限。
- `HTTP 400`：模型名、请求参数或模型能力不兼容。优先换一个当前免费且支持 `tools` 的模型。

---

## 4. 验证单 Agent + 本地工具调用

目的：验证一个 Agent 能拿到本地工具 schema，并通过模型 tool calling 读取仓库文件。这个步骤覆盖
「LLM + Tool loop」但不要求多 Agent 委派。

```bash
uv run python -m syndica run \
  "读取 README.md，并总结 Syndica 当前完成了哪些能力。不要修改文件。" \
  --agent Worker \
  --max-turns 6
```

符合预期：

- 输出会提到 README 中的项目定位、M1 能力或 CLI 使用方式。
- 如果模型调用了 `Read`，Syndica 会执行真实本地读取，但不会修改文件。
- 命令正常结束，最后输出是 assistant 的总结。

不符合预期：

- 模型只泛泛回答、明显没有读取 README：说明模型可能没有正确触发 tool calling；可换支持工具调用的
  `:free` 模型后重试。
- 出现危险确认提示：说明模型尝试执行写文件或命令。首次体验建议拒绝，或重新提示“不要修改文件、
  不要执行 Bash”。
- `HTTP 429`：仍是外部额度问题，不是 Syndica 内核问题。

---

## 5. 验证 Multi-Agent 委派

目的：验证默认 `Orchestrator` 能通过 `Delegate` 创建/调用子 Agent，让 `Explorer` 与 `Planner`
分别工作，最后由根 Agent 汇总。

```bash
uv run python -m syndica run \
  "不要修改文件。请严格按顺序执行三步。第一步：调用 Delegate，agent 填 Explorer，query 原样填写「只调用一次 Glob（pattern 用 *）列出仓库顶层条目，然后立刻用两三句话总结仓库结构，不要调用任何其他工具。」第二步：调用 Delegate，agent 填 Planner，query 原样填写「不要调用任何工具。Syndica 的里程碑 1 已完成，下一个里程碑是自举：让框架修改自己的代码并跑测试。直接写出三条下一阶段计划，每条一句话。」第三步：用中文汇总 Explorer 和 Planner 的结论后结束，不要再调用工具。" \
  --max-turns 4
```

`--max-turns 4` 同时限制 Orchestrator 和它委派出去的每个子 Agent，每个最多 4 轮；这条命令通常用 6 次
请求。分工写死是因为免费模型拿到「调研仓库结构」这类开放任务时，常把轮数全用在调工具上、一直不给
结论（M1 的 Agent 看不到自己还剩几轮）。

符合预期：

- 最终输出包含仓库结构调研结果和下一阶段计划。
- 行为上应体现至少两个角色分工：`Explorer` 负责观察/调研，`Planner` 负责计划整理。
- 不出现 traceback。
- 不出现 `Context.stop_reason 是 error`。
- stderr 没有 `Runtime 错误` 条目。

不符合预期：

- stderr 出现 `Runtime 错误 1/N（agent Explorer <id>）` 这类条目：对应的子 Agent 失败了，下面三行是
  它的失败原因（例如 provider 的 HTTP 400）。根 Agent 可能仍给出了回答，但那份结论缺了这个子任务。
- 以退出码 1 结束并提示「没有留下文字回答」：Agent 用完了轮数还在调用工具。提高 `--max-turns`，
  或把任务拆小。
- 只看到单 Agent 的直接回答：模型没有选择 `Delegate`。可把 prompt 写得更强，例如“必须调用
  Delegate，不要自己完成全部工作”。
- `HTTP 400` 且提示 tool use / tools 不支持：当前模型不支持工具调用，换支持 `tools` 的 `:free`
  模型。
- `HTTP 429`：OpenRouter 免费额度耗尽。此时真实模型 multi-agent 无法继续，只能等重置、换 key 或
  使用离线 scripted 验证。
- 出现写文件或 Bash 确认：首次体验应拒绝；本命令目标是只读调研。

---

## 6. 交互式体验

目的：验证 CLI chat 模式下，同一个 Runtime 进程可以维护会话、列出活动 Agent，并切换目标 Agent。

```bash
uv run python -m syndica chat --max-turns 6
```

chat 里同一个 Agent 的轮数跨消息累计，`--max-turns 6` 是它整个会话的上限；用完后这条消息会提示
「没有留下文字回答」，重开 chat 即可。

进入后输入：

```text
请使用 Delegate：让 Explorer 调研仓库结构，让 Planner 总结下一步计划，最后你汇总。不要修改文件。
```

查看活动 Agent：

```text
:agents
```

切换到某个 Agent：

```text
:use <agent-id>
```

退出：

```text
:quit
```

符合预期：

- 启动后提示符中出现当前 root agent id。
- `:agents` 能列出当前 Runtime 中的 Agent，至少包含 `Orchestrator`；如果模型成功委派，还会出现
  子 Agent。
- `:use <agent-id>` 后提示符切到目标 Agent。

不符合预期：

- `:agents` 只有 `Orchestrator`：说明模型还没成功委派，或之前那轮没跑到 Delegate。
- 关闭终端后 session 丢失：这是 M1 的预期限制。跨进程 SessionStore 属于后续里程碑。
- 429 / 400：处理方式同第 5 步。

---

## 7. 不消耗额度的离线 L2 验证

目的：在 OpenRouter 额度耗尽或网络不可用时，验证 Syndica 的真实 Runtime、多 Agent、工具、人类确认
和 YAML 场景回放。这个流程只把 LLM 替换成 `ScriptedLLM`，不调用真实模型。

```bash
uv run pytest tests/runner/test_scenario.py tests/integration/test_cli_human_scenario.py -q
```

符合预期：

- pytest 全部通过。
- 覆盖真实 Runtime、拓扑装配、本地工具、人工确认、失败路径和 scenario expectation。
- 不消耗 OpenRouter 请求额度。

不符合预期：

- 测试失败：这是本地代码或测试环境问题，需要按失败用例定位。
- 如果只是想体验交互式真实模型，这一步通过不代表 OpenRouter 可用；它只证明本地内核链路可用。

---

## 8. 推荐的完整验证顺序

首次本地体验建议按顺序执行：

```bash
cd /Users/loren/Documents/loren/syndica
uv sync --extra openai --extra dev

read -rs "OPENROUTER_API_KEY?OpenRouter API key: "
echo
export OPENROUTER_API_KEY
export OPENROUTER_MODEL=cohere/north-mini-code:free
unset OPENAI_BASE_URL OPENAI_MODEL OPENAI_API_KEY

uv run python -m syndica doctor
uv run python -m syndica run "用三句话介绍你现在能做什么" --max-turns 3
uv run python -m syndica run "读取 README.md，并总结 Syndica 当前完成了哪些能力。不要修改文件。" --agent Worker --max-turns 6
uv run python -m syndica run "不要修改文件。请严格按顺序执行三步。第一步：调用 Delegate，agent 填 Explorer，query 原样填写「只调用一次 Glob（pattern 用 *）列出仓库顶层条目，然后立刻用两三句话总结仓库结构，不要调用任何其他工具。」第二步：调用 Delegate，agent 填 Planner，query 原样填写「不要调用任何工具。Syndica 的里程碑 1 已完成，下一个里程碑是自举：让框架修改自己的代码并跑测试。直接写出三条下一阶段计划，每条一句话。」第三步：用中文汇总 Explorer 和 Planner 的结论后结束，不要再调用工具。" --max-turns 4
```

如果真实模型额度不可用，执行：

```bash
uv run pytest tests/runner/test_scenario.py tests/integration/test_cli_human_scenario.py -q
```

---

## 9. 当前已知限制

- OpenRouter 免费模型每天有额度限制。`free-models-per-day` 429 是外部额度耗尽，不是 Syndica 逻辑
  错误。
- 免费模型质量和工具调用能力不稳定；同一个 prompt 可能有时不触发 Delegate。
- M1 没有跨进程 session 持久化；`chat` 里的活动 Agent 只存在于当前 CLI 进程。
- M1 没有 Web UI / HTTP server / SSE server；当前体验入口是 CLI 和 L2 scenario runner。
- `--max-cost`、`--max-tokens`、`--max-wall-clock` 是后续预算系统入口，M1 只支持 `--max-turns`。
  它给每个 Agent 各设一个上限，而不是在父子之间切分同一份预算；子 Agent 自己也能再委派，
  所以总请求数没有严格上界，父子预算树归里程碑 8。
