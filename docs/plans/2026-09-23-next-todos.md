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

未完成：

- M2-M12 共 11 个里程碑尚未实现。
- 真实体验验收留下的三条后续项（见「真实体验验收记录」）。

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

- [ ] `Delegate` 对以 `max_turns` 结束的子 Agent 仍建议「用 SendMessage 让它带着已有上下文继续」，
  但 M1 的轮数跨 `run()` 累计且永不重置（登记 B-21），唤醒只会立刻再次 `max_turns` 并丢弃这条消息。
  验收里 Orchestrator 因此白白用掉两轮。应按 stop reason 给出不同的下一步（归内核 orchestration）。
- [ ] Agent 看不到自己还剩几轮，开放任务在紧预算下常常一直调工具、不收尾。归里程碑 8 的
  `GetBudget` 与预算注入历史。
- [ ] `--max-turns` 是每个 Agent 各自的上限，子 Agent 还能再委派，总请求数没有严格上界。归里程碑 8
  的父子预算树。

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

开工前 TODO：

- [ ] 单独写 M2 实现计划。
- [ ] 明确 M2 的自举拓扑、角色提示词、命令安全策略和验证命令解析器。
- [ ] 先用 fixture 仓库做离线 E2E，不直接让 Agent 改真实仓库。
- [ ] 加 dry-run / no-write 模式，避免自举初期误写。

完成标准：

- Syndica 能在临时工作区完成一次文档自举。
- Syndica 能在 fixture 项目中修复一个小型失败测试。
- 危险命令被拦截。
- handoff 文档能记录改动、验证和下一步。

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
- 列出本里程碑要关闭的 deferred items。
- 明确哪些能力进入公开 API，哪些仍是内部实现。
- 先写测试或 L2 场景，再实现。
- 每个任务单独 commit；完成后独立审查。

---

## P3：文档与用户体验补强

目标：让新用户能在没有上下文的情况下理解 Syndica 能做什么、怎么跑、失败时怎么处理。

TODO：

- [x] README 增加本地验证 runbook 链接。
- [x] README 增加“OpenRouter 免费额度限制”说明。
- [ ] 如果真实体验验收发现免费模型工具调用不稳定，补一节“推荐模型与失败排查”。
- [ ] 增加 `.env.example`，只放变量名和占位符，不放真实 key。
- [ ] 未来发包前补英文版 Quick Start。

完成标准：

- 新用户按 README 能完成 `doctor`。
- 新用户按 runbook 能判断 401 / 429 / 400 的含义。
- 文档中没有真实凭证。

---

## P3：发布与工程卫生

目标：为后续持续开发降低维护成本。

TODO：

- [ ] 决定是否发 PyPI pre-release。
- [ ] 增加 GitHub Actions：lint、typecheck、test、deps check。
- [ ] 明确 release versioning 策略。
- [ ] 增加 issue templates：bug、milestone task、UX validation。
- [ ] 建立 changelog。

完成标准：

- 每次 push 能在 GitHub 上自动跑基础门禁。
- 本地与 CI 的命令一致。
- 版本、变更、文档入口清晰。

---

## 总结

接下来最短路径：

1. 提交并 push 当前 OpenRouter / 错误提示 / 文档改动。
2. 等 OpenRouter 免费额度恢复后，按本地验证 runbook 跑真实体验。
3. 把真实体验结果登记到文档或 issue。
4. 开始 M2 自举计划。
5. 按 v1 路线图依次推进 M3-M12。
