# Syndica 本地 Multi-Agent 体验与验证流程

本文用于在本地快速体验 Syndica 里程碑 1 的 multi-agent 能力与里程碑 2 的自举（第 10 步），并判断每一步
是否符合预期。

当前状态：Syndica 已支持真实 OpenAI-compatible / Gemini / OpenRouter 模型调用，配了多个平台时自动
组成轮换；也支持不联网的 scripted L2 场景回放。真实模型体验依赖 Gemini 或 OpenRouter 的 API key 与
免费额度；离线验证不消耗额度。

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

## 1. 配置模型平台（Gemini 和 / 或 OpenRouter）

目的：给真实模型体验提供 API key，同时避免把 key 写入仓库、shell history 或文档。两个平台配一个
就能跑；两个都配就组成轮换，Gemini 在前、OpenRouter 兜底。

```bash
read -rs "GEMINI_API_KEY?Gemini API key: "
echo
export GEMINI_API_KEY
export GEMINI_MODEL=gemini-3.5-flash-lite

read -rs "OPENROUTER_API_KEY?OpenRouter API key: "
echo
export OPENROUTER_API_KEY
export OPENROUTER_MODEL=cohere/north-mini-code:free

unset OPENAI_BASE_URL OPENAI_MODEL OPENAI_API_KEY SYNDICA_LLM_ORDER
```

符合预期：

- 输入 key 时终端不回显。
- 关闭当前终端后环境变量失效。
- `OPENROUTER_MODEL` 以 `:free` 结尾，符合 Syndica 的免费模型门禁。
- Gemini key 来自**没开计费**的 Google 项目。开了计费的项目每次请求都收费，而 Syndica 从 key 上
  看不出区别。

不符合预期：

- 把 key 写进 `.syndica.yaml`、README、脚本或 Git tracked 文件：这是凭证泄露风险。
- 同时保留 `OPENAI_BASE_URL` 等旧变量：可能导致排查时混淆模型来源。上面的 `unset` 会清掉它们。

说明：

- key 已经存在某个文件里时，用 `set -a; source <文件>; set +a` 导入当前 shell。这个文件不要放在
  Agent 会操作的工作区里：`Read` / `Grep` 读得到它，key 会随请求发给模型提供方。
- `cohere/north-mini-code:free` 是一个可用于首轮验证的免费模型示例。OpenRouter 免费模型列表会变化；
  如果该模型不可用，改用 OpenRouter 当前模型列表中标记为免费且支持 `tools` 的模型。
- OpenRouter 免费档每天只有 50 次免费模型请求；Gemini 免费档按型号限制每分钟与每天的请求数（在
  Google AI Studio 里查看），日额度在太平洋时间午夜重置（北京时间夏令时 15:00、冬令时 16:00）。
  每个 Agent 的每一轮就是一次请求。下面的命令都用 `--max-turns` 给**每个** Agent（含委派出去的
  子 Agent）设了上限，按顺序跑完一遍大约 20 次请求。
- 如果只设置 `OPENROUTER_API_KEY` 而不设置 `OPENROUTER_MODEL`，Syndica 会默认使用
  `openrouter/free` 路由器；路由器更方便，但具体失败原因有时不如显式模型好诊断。
- 要换轮换顺序或只用其中一个平台，设 `SYNDICA_LLM_ORDER`，例如 `openrouter,gemini` 或 `gemini`。
  通用的 `OPENAI_*` 端点叫 `openai`，只有被点名或两个免费平台都没配时才会用到。

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

- `OPENROUTER_API_KEY 未设置或为空` / `GEMINI_API_KEY 未设置或为空`：设了该平台的模型却没设 key，
  重新执行第 1 步。
- `OpenRouter 只使用 openrouter/free 或以 :free 结尾的免费模型`：说明模型名不是免费模型，改回
  `cohere/north-mini-code:free` 或其他 `:free` 模型。
- 报错提到 `SYNDICA_LLM_ORDER`：里面有未知平台名、重复项、空项，或点名了一个没配 key 的平台。
  按报错的下一步改，或 `unset SYNDICA_LLM_ORDER` 用默认顺序。
- `OPENAI_BASE_URL=None`：说明 Gemini 和 OpenRouter 都没配置，第 1 步的 key 没有导出到当前 shell。

---

## 3. 验证真实模型基础调用

目的：先验证最小 LLM 调用闭环，排除 API key、额度、模型名和网络问题。

```bash
uv run python -m syndica run "用三句话介绍你现在能做什么" --max-turns 3
```

符合预期：

- 终端输出一段自然语言回复。
- 不出现 traceback。
- 退出码是 0；stderr 里没有「以错误结束」，也没有 `Context.stop_reason 是 error`。

不符合预期：

- `HTTP 401`，或 Gemini 的 `HTTP 400` 且提到 `API key`：API key 不正确或已失效。
- `HTTP 429 Too Many Requests` 且包含 `free-models-per-day`：OpenRouter 免费日额度耗尽。等待
  `X-RateLimit-Reset` 指定时间、换未耗尽的 key，或给 OpenRouter 充值提升免费额度上限。
- `HTTP 429` 且包含 `RESOURCE_EXHAUSTED`：Gemini 免费档额度用完。每分钟额度一分钟内恢复，轮换会按
  Google 给的 `retryDelay` 等它；日额度要等太平洋时间午夜重置。
- `HTTP 503` 且提到 high demand：Gemini 一时过载，通常几秒后就好，轮换会自动换平台或稍后重试。
- `HTTP 400`：模型名、请求参数或模型能力不兼容。优先换一个当前免费且支持 `tools` 的模型。
- stderr 有 `轮换中的平台 gemini 失败（连续第 1 次），接下来 2.1 秒跳过它：…`，stdout 仍有回答：这个
  平台失败了，同一请求已交给下一个平台，回答有效。同一个平台反复出现就按冒号后面的原因查它的 key
  或额度，否则兜底平台会一直替它答、替它耗额度。
- `轮换中的 2 个平台里，至少一个在 120 秒内答出这次请求`（只配一个平台时是 `平台 gemini 在 120 秒内
  答出这次请求`）：所有平台都失败了。限流和服务端故障会先按退避重试，最多等到请求开始后 120 秒；
  401、403、400、404 这类等也不会好的失败不等，立刻报错（两个 key 都错时两秒内就会看到它）。报错逐个列出每个平台最后一次的失败原因和
  多少秒后才会再试，按原因处理即可。

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
- 退出码是 0；stderr 里没有「以错误结束」，也没有 `Context.stop_reason 是 error`。
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

## 7. 验证多平台轮换与故障切换（两个平台都配了时）

目的：确认排在前面的平台失败时，同一请求会交给下一个平台，并在 stderr 留下记录。这里故意把
OpenRouter 排在前面、给它一个错误的 key：401 不计入 OpenRouter 免费额度，这一步只用 Gemini 的一两次
请求。

```bash
SYNDICA_LLM_ORDER=openrouter,gemini OPENROUTER_API_KEY=invalid-key \
  uv run python -m syndica run "用一句话回答：1 加 1 等于几？" --max-turns 2
```

符合预期：

- stdout 有回答，由 Gemini 答出。
- stderr 有一行 `轮换中的平台 openrouter 失败（连续第 1 次），接下来 1.9 秒跳过它：HTTP 请求失败：
  HTTP 401 …`（秒数带 ±20% 抖动）。
- 401 之后请求立刻交给 Gemini，不为 OpenRouter 等待；总耗时基本就是 Gemini 作答的时间，Gemini
  过载时可能要几十秒。

不符合预期：

- 以 `轮换中的 2 个平台里，至少一个在 120 秒内答出这次请求` 结束、stdout 没有回答：Gemini 也失败了，
  按报错里 gemini 那一段的原因处理，多半是 key 或额度。
- stderr 没有 openrouter 那一行：`SYNDICA_LLM_ORDER` 没生效，或者这个 key 意外是有效的。

---

## 8. 不消耗额度的离线 L2 验证

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

## 9. 推荐的完整验证顺序

首次本地体验建议按顺序执行：

```bash
cd /Users/loren/Documents/loren/syndica
uv sync --extra openai --extra dev

read -rs "GEMINI_API_KEY?Gemini API key: "
echo
export GEMINI_API_KEY
export GEMINI_MODEL=gemini-3.5-flash-lite
read -rs "OPENROUTER_API_KEY?OpenRouter API key: "
echo
export OPENROUTER_API_KEY
export OPENROUTER_MODEL=cohere/north-mini-code:free
unset OPENAI_BASE_URL OPENAI_MODEL OPENAI_API_KEY SYNDICA_LLM_ORDER

uv run python -m syndica doctor
uv run python -m syndica run "用三句话介绍你现在能做什么" --max-turns 3
uv run python -m syndica run "读取 README.md，并总结 Syndica 当前完成了哪些能力。不要修改文件。" --agent Worker --max-turns 6
uv run python -m syndica run "不要修改文件。请严格按顺序执行三步。第一步：调用 Delegate，agent 填 Explorer，query 原样填写「只调用一次 Glob（pattern 用 *）列出仓库顶层条目，然后立刻用两三句话总结仓库结构，不要调用任何其他工具。」第二步：调用 Delegate，agent 填 Planner，query 原样填写「不要调用任何工具。Syndica 的里程碑 1 已完成，下一个里程碑是自举：让框架修改自己的代码并跑测试。直接写出三条下一阶段计划，每条一句话。」第三步：用中文汇总 Explorer 和 Planner 的结论后结束，不要再调用工具。" --max-turns 4
SYNDICA_LLM_ORDER=openrouter,gemini OPENROUTER_API_KEY=invalid-key uv run python -m syndica run "用一句话回答：1 加 1 等于几？" --max-turns 2
```

如果真实模型额度不可用，执行：

```bash
uv run pytest tests/runner/test_scenario.py tests/integration/test_cli_human_scenario.py -q
```

---

## 10. 自举：让 Agent 团队改仓库（里程碑 2）

目的：验证 Agent 团队按本仓的 `syndica-dev` 拓扑改代码、宿主独立复核、写交接文件、由你决定提交与
推送。第一次在仓库副本里跑，不在你正在用的仓库里跑：

```bash
git clone /Users/loren/Documents/loren/syndica /tmp/syndica-copy
cd /tmp/syndica-copy
git remote remove origin
git init -q --bare /tmp/syndica-copy.git
git remote add origin /tmp/syndica-copy.git
git push -q -u origin main
uv sync --locked --extra openai --extra dev

uv run python -m syndica doctor --verify
uv run python -m syndica run --no-write --handoff --agent Planner "规划：……（点名要看的文件）"
uv run python -m syndica run --handoff --topology syndica-dev "……（点名要改的文件与函数）"
```

符合预期：

- `doctor --verify` 对 `.syndica.yaml` 里的每条 verify 命令各一行，全部 passed。
- `--no-write` 的 run：交接文件 `status: read_only`，没有复核与 git 收尾。
- 拓扑 run：stderr 先打出交接文件的路径；复核全过且 Reviewer 给了 pass 时，接着打出 git 收尾的摘要
  （分支、上游、提交标题、改动列表）与「1 提交并推送 / 2 只提交 / 3 我来处理」；回答之后 stdout 是
  完整的交接文件，`.syndica/handoffs/` 下有同一份（权限 0600）。
- 回答之前自己看一遍 `git diff`：宿主复核与 Reviewer 都看不出「删掉了一条无关的用例」——删了用例，
  测试照样是绿的。

不符合预期 / 常见情况：

- 只有 2 和 3：推送没有提供，摘要写了原因——没有上游；钩子目录里有 `pre-push`（全局 `core.hooksPath`
  指向的目录也算，推送要用你的 ssh agent，宿主不替你的钩子做决定）；运行前工作区里就有改动；本地已有
  没推的提交。
- `status: verification_failed`：宿主复核没过，看「宿主复核」一节；「Agent 的结论」里说全部通过不算数。
- `status: unverified`：没有可用的 Reviewer pass（没派 Reviewer、它之后又有改动、它自己改了文件）。
- `status: run_failed` 且写着用完了轮次预算：免费模型常一轮只调一个工具。把需求拆小，在需求里点名文件
  与函数。
- 一次 run 用几十到上百次请求：一个 Worker 最多 30 轮，用完还会另派一个。Gemini 免费档的每分钟请求数与
  token 数先撞上限，轮换把请求交给 OpenRouter，而 OpenRouter 一天只有 50 次。
- Ctrl-C 中断的 `--handoff` run 照样写交接文件：记成一次失败的 run、复核跳过，不提供 git 收尾。

---

## 11. 看 trace、回放与导出（里程碑 3）

目的：验证每次 run 都留下一个可读、可回放、可导出的 trace，且里面没有密钥。在仓库副本里跑：

```bash
uv sync --locked --extra openai --extra dev --extra otel
uv run python -m syndica run "读取 README.md 的 Observability 一节，用两句话总结。不要修改文件。" --agent Worker --max-turns 4
uv run python -m syndica trace list
uv run python -m syndica trace show <上面那一行的 id>
env -u GEMINI_API_KEY -u OPENROUTER_API_KEY uv run python -m syndica trace replay <id>
uv run python -m syndica trace export <id> --otlp http://127.0.0.1:4318
uv run python -m syndica trace prune --keep 50
```

符合预期：

- run 结束时 stderr 打出 `trace：.syndica/traces/<UTC 时间>-<id>`。
- `trace show`：谁创建了谁、谁在等谁、最慢的三个工具调用与时间线。
- `trace replay` 在没有任何 API key 的环境里以 0 退出、打出「复现」：回放不发模型请求。它自己记一个新的 trace，
  `trace list` 里那一行写着「回放自 <原 id>」。
- `trace export`：本机的 collector（任何 OTLP/HTTP 接收端）收到 `/v1/traces`；以 1 退出说明 collector 没收下，
  三件套说为什么。环境里设了 `OTEL_EXPORTER_OTLP_ENDPOINT` 时，每次 run 结束都会自动导出。
- 在 trace 目录里搜你环境里的密钥值：一处也没有，只有 `[REDACTED:…]`。

不符合预期 / 常见情况：

- `trace replay` 以 2 退出、说没有回放文档：这个 trace 是 `--trace structure` 或 `--topology` 的 run 记的。
- 回放以 1 退出：分叉那一行说是哪个 Agent、第几轮、哪个单元；你刚改过内核或宿主配置时这正是它该报的。
- `trace list` 里一行写着「span 读不进来」：那个 trace 被改过（多半是 Agent 的 Bash），`trace show` 说是哪一行。

---

## 12. 当前已知限制

- OpenRouter 免费模型每天有额度限制。`free-models-per-day` 429 是外部额度耗尽，不是 Syndica 逻辑
  错误。Gemini 免费档同样按型号限额。
- 轮换只覆盖清单里 `model` 为 `default` 的角色（内置角色都是）。显式写了模型名的角色只走一家：
  `gemini:<型号>`、`openrouter:<型号>`、`openai:<型号>` 点名平台；不带平台前缀的模型名仍走 OpenRouter（配置了
  的话）或 `OPENAI_*`。
- 免费模型质量和工具调用能力不稳定；同一个 prompt 可能有时不触发 Delegate。
- M1 没有跨进程 session 持久化；`chat` 里的活动 Agent 只存在于当前 CLI 进程。
- M1 没有 Web UI / HTTP server / SSE server；当前体验入口是 CLI 和 L2 scenario runner。
- `--max-cost`、`--max-tokens`、`--max-wall-clock` 是后续预算系统入口，M1 只支持 `--max-turns`。
  它给每个 Agent 各设一个上限，而不是在父子之间切分同一份预算；子 Agent 自己也能再委派，
  所以总请求数没有严格上界，父子预算树归里程碑 8。
