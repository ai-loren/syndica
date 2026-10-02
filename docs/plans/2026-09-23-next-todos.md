# Syndica 后续 TODO 总表

本文记录 M1 完成后，Syndica 仓库接下来要做的事。它不是某个里程碑的详细实现计划；真正开工时仍需
为对应里程碑单独写计划、逐任务 TDD、变异验证、独立审查和提交。

相关入口：

- M1 最终审计：[`docs/audits/2026-09-23-m1-final-audit.md`](../audits/2026-09-23-m1-final-audit.md)
- 本地体验验证流程：[`docs/guides/2026-09-23-local-multi-agent-validation.md`](../guides/2026-09-23-local-multi-agent-validation.md)
- v1.0 里程碑路线图：[`docs/plans/2026-09-23-v1-milestone-roadmap.md`](./2026-09-23-v1-milestone-roadmap.md)

---

## 当前状态

已完成：

- M1 multi-agent 内核骨架已完成并审计通过。
- CLI、声明式 Agent、Topology、本地工具、人工确认、AskUser、L2 scenario runner 已落地。
- OpenAI-compatible backend 已支持真实模型调用。
- OpenRouter 免费模型接入已提交（`6f8471c`），免费模型门禁和错误提示已补强。
- 2026-09-29 用 `cohere/north-mini-code:free` 跑通真实体验验收，并修掉验收暴露的四个问题（见下方
  「真实体验验收记录」）。
- 2026-09-29 接入 Gemini（免费档）并加上多平台模型轮换：Gemini 在前、OpenRouter 兜底，每个平台
  各自指数退避；只配一个平台时按退避重试它（见下方「多平台模型轮换」）。
- 2026-10-01 M2 自举完成：护栏（CI、危险规则、Agent 命令环境、开发期无条件拦截）、`run --no-write`、
  `run --handoff` 的宿主复核与交接文件、人选择宿主执行的 git 收尾、`doctor --verify`、本仓的
  `syndica-dev` 配置；真实免费模型在仓库副本里完成了一次 `verified` 的自改动并推到本地 bare remote，
  那个提交原样进了本仓。收口审计见 [`docs/audits/2026-10-01-m2-final-audit.md`](../audits/2026-10-01-m2-final-audit.md)。
- 2026-10-02 M3 可观测性 + 脱敏收口完成：内核统一 `Redactor` 与全仓写入点 / 外发点守卫、trace 模型与第五个
  Store Protocol `TraceStore`、内核同步插桩（span 与派生 / 消息因果边）、默认「可回放」级别的捕获与离线
  `trace replay`、`syndica trace list/show/replay/export/prune`、OTLP/HTTP JSON 导出。收口审计见
  [`docs/audits/2026-10-02-m3-final-audit.md`](../audits/2026-10-02-m3-final-audit.md)。

未完成：

- M4-M12 共 9 个里程碑尚未实现。
- GitHub Actions 本月额度用完：M2、M3 的提交都只按本地验证协议验证过，额度恢复后在 Linux 上补跑 CI。
- 其余遗留项已逐条归入 M2-M12 或标为接受的限制，见路线图「遗留项归属（2026-09-29）」。

---

## 真实体验验收记录（2026-09-29）

模型 `cohere/north-mini-code:free`，在去掉 `.git` 与密钥的仓库副本里运行。基础调用、Worker 读
README、Orchestrator 委派 Explorer / Planner 并汇总、`chat --max-turns` 均通过；免费额度日用 44/50，
花费 0。

验收暴露并已修复的问题：

1. `Glob` / `Grep` 交回的列表原样进 tool 消息 `content`，真实端点一律 HTTP 400（`content[0] must be
   an object`），带这两个工具的 Agent 在真实模型下全部失败。OpenAI 兼容客户端现在把非内容块的列表
   编码成 JSON 文本，空输出也保留 `content` 键。
2. 子 Agent 失败只在根 Agent 也失败时才显示。CLI 现在把运行期间捕获的每条 Runtime 错误按
   「角色名 + id + 三件套」写进 stderr。
3. Agent 没留下文字回答时 CLI 空输出、退出码 0。`run` 现在以三件套错误和退出码 1 结束，chat 在
   stderr 提示后继续；用完轮数截断的回答另有提醒；chat 只认这条消息之后的回答。
4. `--max-turns` 只限制根 Agent，子 Agent 按角色预算（缺省 50 轮）跑满，一次委派就能耗尽免费日额度。
   现在它收紧目录里每个角色的预算并同步收紧每个拓扑槽，`chat` 也支持 `--max-turns`。

后续项：

- [x] `Delegate` 对以 `max_turns` 结束的子 Agent 仍建议「用 SendMessage 让它带着已有上下文继续」，
  但 M1 的轮数跨 `run()` 累计且永不重置（登记 B-21），唤醒只会立刻再次 `max_turns` 并丢弃这条消息。
  验收里 Orchestrator 因此白白用掉两轮。已修：这一格改为建议自己做完或另派新 Agent（登记 D-61）。
- Agent 看不到自己还剩几轮，开放任务在紧预算下常常一直调工具、不收尾。已写进里程碑 8 的 TODO
  （`GetBudget` 与预算注入历史）。
- `--max-turns` 是每个 Agent 各自的上限，子 Agent 还能再委派，总请求数没有严格上界。已写进里程碑 8
  的 TODO（父子预算树）。

---

## 多平台模型轮换（2026-09-29）

用户同时有 Gemini AI Studio（免费档）与 OpenRouter 的 key，要求：按各平台自己的规范构造请求，一个
平台不通就换下一个，每个失败的平台单独指数退避。M2 之前完成，设计见 spec §5.7 补充与决策 #89。

- `backends/llm_router` 的 `ModelRouter`：按声明顺序轮换，失败的平台单独退避（2 秒起、翻倍、上限
  5 分钟、±20% 抖动），平台给的重试时间更晚时以平台为准；所有平台都在退避时最多等到请求开始后
  120 秒，等也不会好的失败（认证、请求不合法）不等。每次平台故障留一条 WARNING。
- OpenAI 兼容后端：HTTP 错误带 `status_code` / `retry_after`，解析 `Retry-After`、OpenRouter 的
  `X-RateLimit-Reset`、Google 的 `RetryInfo`；Gemini 3 的 `thought_signature` 只回送给产出它的端点，
  别家平台产出的函数调用挂 Google 给的占位签名。
- 装配层：`GEMINI_API_KEY` / `GEMINI_MODEL` / `SYNDICA_LLM_ORDER`，默认 Gemini 在前；模型总是经
  `ModelRouter` 调用，只配一个平台时就是按退避重试它。

真实验收（2026-09-30，`gemini-3.5-flash-lite` + `cohere/north-mini-code:free`，在去掉 `.git` 与 key
的仓库副本里跑）：

- 跨平台接续两个方向都是 200：OpenRouter 答出的工具调用交给 Gemini（挂 1 个占位签名）；Gemini 答出的
  调用交给 OpenRouter（不外泄签名），再回到 Gemini（带自己的真实签名）。
- runbook 第 2–5 步全部通过。委派那一步 Gemini 连续两次 503 high demand，路由器把请求交给 OpenRouter
  并留下两条 WARNING；Explorer 的下一次请求直接跳过还在退避的 Gemini；退避到期后 Gemini 重新接手，
  带着占位签名接住 OpenRouter 答出的 `Delegate` 调用。
- 第 7 步故障切换通过：OpenRouter 的 401 之后立刻换到 Gemini。两个 key 都错时 2 秒内报错，不空等。
  Gemini 对无效 key 返回的是 400（`Please pass a valid API key`），报错的下一步已按原因描述。
- OpenRouter 免费额度用 5/50，401 不计数；花费 0。

后续项（已归入路线图「遗留项归属」）：经路由器的前缀漂移检测只做逻辑比对（M3）；清单显式写的模型名
只走 OpenRouter / `OPENAI_*`（M4）。

---

## P0：当前改动收口

目标：把本轮已经完成的 OpenRouter 接入、错误提示优化和体验文档收口成可推送状态。

状态：已完成。`6f8471c` 已在 `origin/main`；2026-09-29 本机复跑全量门禁全绿，密钥扫描无真实 key。

TODO：

- [x] 复跑目标测试：

  ```bash
  uv run pytest tests/cli/test_host.py tests/backends/test_llm_openai.py tests/cli/test_main.py -q
  ```

- [x] 复跑静态检查：

  ```bash
  uv run ruff check src/syndica/cli/host.py src/syndica/backends/llm_openai/client.py tests/cli/test_host.py tests/backends/test_llm_openai.py tests/cli/test_main.py
  uv run pyright src/syndica/cli/host.py src/syndica/backends/llm_openai/client.py tests/cli/test_host.py tests/backends/test_llm_openai.py tests/cli/test_main.py
  uv run python scripts/check_deps.py
  git diff --check
  ```

- [x] 如时间允许，复跑全量：

  ```bash
  uv run pytest -q
  uv run ruff check .
  uv run ruff format --check .
  uv run pyright
  ```

- [x] 检查仓库没有真实 API key：

  ```bash
  rg "sk-or-v1|OPENROUTER_API_KEY|OPENAI_API_KEY" .
  ```

  预期：只允许文档中的占位符和环境变量名，不允许真实 key。

- [x] 提交本地改动。
- [x] push 到 GitHub `main`。

完成标准：

- 工作区干净。
- `main` 与 `origin/main` 对齐。
- README、体验 runbook、错误提示与测试均已进入远端。

---

## P1：真实用户体验验收

目标：用真实 OpenRouter 免费模型跑通一次用户可感知的 multi-agent demo。

状态：已完成，结果见「真实体验验收记录（2026-09-29）」。下面的命令是当时的计划原文；可复现的最新
命令以本地验证流程文档为准（`--max-turns` 已收紧，委派用分工写死的 prompt）。

前置条件：

- OpenRouter 免费额度恢复，或换一个未耗尽额度的 key。
- 当前终端已设置：

  ```bash
  export OPENROUTER_API_KEY=<new-key>
  export OPENROUTER_MODEL=cohere/north-mini-code:free
  unset OPENAI_BASE_URL OPENAI_MODEL OPENAI_API_KEY
  ```

TODO：

- [x] 跑 `doctor`：

  ```bash
  uv run python -m syndica doctor
  ```

- [x] 跑基础真实模型调用：

  ```bash
  uv run python -m syndica run "用三句话介绍你现在能做什么" --max-turns 3
  ```

- [x] 跑单 Agent + 本地工具：

  ```bash
  uv run python -m syndica run \
    "读取 README.md，并总结 Syndica 当前完成了哪些能力。不要修改文件。" \
    --agent Worker \
    --max-turns 8
  ```

- [x] 跑 multi-agent 委派：

  ```bash
  uv run python -m syndica run \
    "请使用 Delegate：让 Explorer 调研当前仓库结构，让 Planner 制订下一阶段计划，最后由你汇总结论。不要修改文件。" \
    --max-turns 12
  ```

- [x] 跑 `chat` 交互：

  ```bash
  uv run python -m syndica chat
  ```

  进入后验证：

  ```text
  :agents
  :use <agent-id>
  :quit
  ```

需要观察并记录：

- 免费模型是否稳定触发 `Read` / `Delegate`。
- 失败时是模型能力问题、额度问题、还是 Syndica 请求格式问题。
- CLI 输出是否对普通用户足够可理解。
- 是否需要给 README 增加“免费模型不稳定”的更醒目说明。

完成标准：

- 至少一次真实模型基础调用成功。
- 至少一次 Worker 读取 README 成功。
- 至少一次 Orchestrator 委派 Explorer / Planner 成功，或记录免费模型无法稳定工具调用的证据。
- `chat` 至少能启动、列出 Agent，并可退出。

---

## P1：离线回归验收

目标：在真实模型不可用时，继续验证 Syndica 的核心 multi-agent 机制。

TODO：

```bash
uv run pytest tests/runner/test_scenario.py tests/integration/test_cli_human_scenario.py -q
```

完成标准：

- L2 scenario 全部通过。
- 验证真实 Runtime、工具、人工确认、失败路径和 YAML 场景回放。
- 不消耗 OpenRouter 额度。

说明：这一步证明内核和 runner 可用，但不证明真实模型体验可用。

---

## P2：M2 自举里程碑

目标：让 Syndica 能在自己的仓库里完成受控代码/文档修改、运行验证、读取失败并迭代修复。

入口文档：

- [`docs/plans/2026-09-23-v1-milestone-roadmap.md#M2 自举`](./2026-09-23-v1-milestone-roadmap.md#m2-自举)
- 实现计划：[`2026-09-30-m2-self-hosting.md`](./2026-09-30-m2-self-hosting.md)

开工前 TODO：

- [x] 单独写 M2 实现计划。
- [x] 明确 M2 的自举拓扑、角色提示词、命令安全策略和验证命令解析器。
- [x] 先用 fixture 仓库做离线 E2E，不直接让 Agent 改真实仓库（T17 的七个场景）。
- [x] 加 dry-run / no-write 模式，避免自举初期误写（`run --no-write`）。

完成标准：

- [x] Syndica 能在临时工作区完成一次文档自举。
- [x] Syndica 能在 fixture 项目中修复一个小型失败测试。
- [x] 危险命令被拦截。
- [x] handoff 文档能记录改动、验证和下一步。

### 真实模型 E2E 记录（2026-10-01，M2 T18）

在仓库副本里跑（D22：clone、删掉原 remote、本地 bare 仓库作上游、`uv sync --locked`、key 文件在副本
之外），默认轮换（Gemini `gemini-3.5-flash-lite` 在前、OpenRouter `cohere/north-mini-code:free` 兜底），
`--handoff`。请求数按 httpx 的请求日志计：表里分列成功的请求与 429，正文里的总数是两者之和；误报指开发期
策略拦下了正当的命令。

| 场景 | 结果 | 成功请求 Gemini / OpenRouter | 429 | 误报 |
|---|---|---|---|---|
| 3：`--no-write` 规划（Planner） | 第一次 Planner 每轮只调一个工具，8 轮用完没写出计划，`run_failed`；调轮数、要求一轮里并行调用之后 `read_only`、退出 0，还指出需求里说缺的那条用例其实已经有了 | 24 / 0（含两次失败与一次诊断重跑） | 0 | 0 |
| 2：夹具包里修一个失败测试（内置 Worker） | 改对；交接列出改动与两次 pytest（failed → passed），复核通过；没派 Reviewer，所以 `unverified`、不提供提交 | 8 / 0 | 0 | 0 |
| 1：文档自举（本仓 Worker） | 断链修对；Worker 把门禁用 `&&` 串成一条，最终回答写「全量 4871 项全部通过」，宿主复核有 2 条失败（副本漏同步了一个测试文件），`verification_failed`、退出 1、不提供提交 | 14 / 0 | 0 | 0 |
| 4：本仓真实小改动（`syndica-dev` 拓扑），第一次 | 两个 Worker 各用完 30 轮；宿主复核全过、Reviewer pass、`verified`，但 diff 删掉了一条无关的已有用例，两个 Reviewer 都没看出来；人选 3 | 75 / 8 | 2 | 未记录 |
| 4：第二次 | 一次 `Grep` 把约 16 万 token 的匹配行塞进 Worker 的上下文，此后每次请求都带着它，Gemini 按每分钟 token 数拒绝；中止 | 22 / 15 | 4 | 未记录 |
| 4：第三次（补了提示词、Glob / Grep 加上限之后） | 23 分钟；Worker 一轮发出六道门禁、24 轮内交回，Reviewer 拿到需求原文；宿主复核全过（4905 条测试）、Reviewer pass、`verified`；人看过 diff 选 1，宿主提交并推到本地 bare remote。这个提交原样带进本仓（`a2d0af6`） | 39 / 3 | 1 | 0 |

几条跨场景的观察：

- 免费档的 429 都是每分钟的请求数或 token 数（Google 给的重试时间 12–48 秒），一天里没有撞上日额度；
  轮换把这些请求交给 OpenRouter，当天它用了 26 次。
- 免费模型倾向每轮只调一个工具、40～60 行一段地翻大文件；提示词要求同一轮并行调用、先 Grep 定位
  之后，同样的任务请求数从 85 降到 43（都含 429）。
- 宿主复核挡住了一次「自称全量通过」；挡不住「删掉一条无关用例」——删了用例测试照样是绿的，两个
  Reviewer 也都给了 pass，是人在提交前看 diff 拦下的。
- 本机全局 `core.hooksPath` 里有企业 DLP 的 `pre-push` 钩子，按 D14 宿主不会提供推送；副本里把
  `core.hooksPath` 设成空目录才跑到推送那一格（推向本地 bare remote）。
- D22 的真实 SSH 推送保留了这个钩子：GitHub 上的临时分支作上游，脚本化的 `--handoff` 里宿主不提供推送、写明
  原因，选 2 只提交；普通 `git push` 推上去、`ls-remote` 核对、删除分支。宿主自己经 SSH 推送那一段没有在
  真实远端跑过。

---

## P2：M3-M12 里程碑执行队列

详细目标、架构、TODO、静态验收和 E2E 验收均以 v1 路线图为准。这里仅记录执行顺序和最关键的开工条件。

| 优先级 | 里程碑 | 关键目标 | 开工条件 |
|---|---|---|---|
| P2 | M3 可观测性 + 脱敏 | Trace/span、因果链、脱敏、回放 | M2 至少能辅助开发或已有手工计划 |
| P2 | M4 扩展点完备 | 三层装配、第三方工具/Module/Backend 插件化 | M3 trace 能观测插件调用 |
| P2 | M5 执行面完整化 + 不可信内容 | Workspace/Portal 完整能力、git 层、PTY、真实溯源链 | M3/M4 后做，避免执行面不可观测 |
| P3 | M6 状态 | SessionStore、压缩、记忆持久化 | M3 trace 与 M5 执行面稳定 |
| P3 | M7 产物与验收 | Artifact、Verifier、Rubric、三态验收 | M6 状态可保存产物 |
| P3 | M8 长程任务 | Budget、Progress、停止判据、策略 | M6/M7 后做 |
| P3 | M9 产品面 | HTTP/SSE/WebSocket、React UI | M6 状态和 M8 长程任务稳定 |
| P4 | M10 互操作 | MCP、ACP、A2A、OpenAI-compatible HTTP | 核心产品面稳定 |
| P4 | M11 训练出口 | 中立轨迹、SFT/偏好/RL 导出 | M3 trace 与 M7 artifact 完整 |
| P4 | M12 发布准备 | 英文文档、CI 矩阵、许可、贡献流程 | M2-M11 主体完成 |

每个里程碑开工时必须：

- 写独立计划文档。
- 列出本里程碑要关闭的 deferred items（归属见路线图「遗留项归属（2026-09-29）」）。
- 明确哪些能力进入公开 API，哪些仍是内部实现。
- 先写测试或 L2 场景，再实现。
- 每个任务单独 commit；完成后独立审查。

---

## P3：文档与用户体验补强

目标：让新用户能在没有上下文的情况下理解 Syndica 能做什么、怎么跑、失败时怎么处理。

TODO：

- [x] README 增加本地验证 runbook 链接。
- [x] README 增加“OpenRouter 免费额度限制”说明。
- [x] 如果真实体验验收发现免费模型工具调用不稳定，补一节“推荐模型与失败排查”。已并进本地验证
  流程文档：推荐模型、分工写死的委派 prompt、401 / 429 / 400 与 stderr `Runtime 错误` 的读法。
- 不做 `.env.example`：key 文件不该放在 Agent 操作的工作区里，`Read` / `Grep` 读得到它；README 与
  验证流程文档已写明放在仓库外。
- [ ] 未来发包前补英文版 Quick Start（归 M12）。

完成标准：

- 新用户按 README 能完成 `doctor`。
- 新用户按 runbook 能判断 401 / 429 / 400 的含义。
- 文档中没有真实凭证。

---

## P3：发布与工程卫生

目标：为后续持续开发降低维护成本。

TODO：

- [ ] 决定是否发 PyPI pre-release（归 M12）。
- [x] 增加 GitHub Actions：lint、typecheck、test、deps check（M2 T1，加上链接检查共六道门禁）。
- [ ] 明确 release versioning 策略（归 M12）。
- [ ] 增加 issue templates：bug、milestone task、UX validation（归 M12）。
- [ ] 建立 changelog（归 M12）。

完成标准：

- 每次 push 能在 GitHub 上自动跑基础门禁。
- 本地与 CI 的命令一致。
- 版本、变更、文档入口清晰。

---

## 总结

接下来最短路径：

1. ~~提交并 push 当前 OpenRouter / 错误提示 / 文档改动。~~ 已完成。
2. ~~按本地验证 runbook 跑真实体验，并登记结果。~~ 已完成，见「真实体验验收记录」。
3. ~~多平台模型轮换。~~ 已完成，见「多平台模型轮换」。
4. ~~按 M2 自举计划（[`2026-09-30-m2-self-hosting.md`](./2026-09-30-m2-self-hosting.md)）逐个任务推进。~~
   已完成，见 [M2 收口审计](../audits/2026-10-01-m2-final-audit.md)。
5. 额度恢复后在 Linux 上补跑 CI；然后写 M3（可观测性 + 脱敏收口）的实现计划，按 v1 路线图依次推进
   M3-M12。M3 起可以用 `syndica run --handoff --topology syndica-dev` 让 Syndica 帮着改自己。
