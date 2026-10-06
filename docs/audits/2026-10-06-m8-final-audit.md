# 里程碑 8 收口审计：长程任务

日期：2026-10-06
范围：[M8 计划](../plans/2026-10-06-m8-long-horizon.md) 的 T0 与六个阶段（预算树与可感知预算、进度账本 / 停滞 / 漂移、
可插拔长程策略与 Module、目标导向型压缩与压缩前完整前像、多 Agent 的遗留、收口），加上路线图遗留表、M5 / M6 / M7 收口
审计里标 M8 的条目
基线：T0 + 阶段四 `1281454`、阶段一 `8d1dce1` / `0933ad6`、阶段一评审处置 `ba36eb1`、阶段二 + 三 `b46ef91`、阶段五
`a0c4e38` / `3a8a1d6`、阶段二 + 三评审处置 `2783295`；阶段五评审处置、收口评审的修复与本文另行提交
验证方式：本地验证协议（Actions 额度用完期间，见路线图「当前主线约束」）

## 结论

spec §15 的完成判据——**目标持久化与漂移检测、预算树与可感知预算、进度度量、三条停止判据、可插拔长跑策略**——由一套
端到端用例（经公开 API 与 `syndica` 命令行、脚本化模型，随全量测试零成本地跑）与四次真实运行共同证明。

- 预算：`Budget`（tokens / cost / wall clock / turns）进 `Runtime(budget=BudgetPolicy(...))` 与 `spawn(budget=)`。预算树按
  预留记账：活着的子 Agent 占着的额度之和永不超过父，周期收尾时缩到实际占用、余量回父，唤醒时至多切回父能给的一半；
  每一次模型调用（主轮、重试、轮换、辅助、伴生）都在 `llm_attempt` 处按估算准入、回来按实报记账，越界只剩估算误差。
  价格按（平台，模型）查，价格未知而设了成本上限时那个模型用不了（重试换下一个候选）。耗尽缺省 `graceful_wrapup`：只放得下
  一次调用的那一轮是收尾轮（工具调用不执行），一次都放不下就停在 `budget`；`hard_stop` / `ask_human` 可选。
- 可感知：`GetBudget`；预算块进历史（第一轮一块、跨 50 / 25 / 10% 各一块，只写事实、不进 System Prompt、不进回放指纹）。
- 进度与停止判据：`ProgressLedger` 只从验收的确定结论量进度（强制 checkable 条），回退报警；完成（这个周期的里程碑 pass
  且强制条全满足 → 一轮最终回答）、资源（预算耗尽处置）、停滞（N 轮没有新的强制条被满足 → 策略纠偏而不是终止）三条正交。
- 漂移：锚点检查（零 LLM，动作的词面对 Goal 与 Rubric 的词面）是常态，语义比对缺省关、开着时只在可疑时跑一次。
- 策略：`LongHorizonStrategy` 四个钩子；内核给 `BaselineStrategy`，`LongHorizonStrategyContract` 随 wheel 发布，基线与
  一个「简单重启循环」替换实现过同一份契约；`long-horizon` Module 把它接进 Runtime。策略写下的文字取下限、Goal、每条
  Rubric、裁决与比对结论里最严的信任；第三方策略缺省不可信。
- 上下文与状态：目标导向型压缩（§8.8 第四种）；压缩前把完整前像脱敏归档（内置 store 留最近 20 份）。
- 多 Agent：后台工具（只转声明了可后台化的工具，获批调用从不转，周期不带着在跑的后台调用结束）；派活树回收（`keep_
  finished_children`）；后代的单一定义（往上走的授权与往下走的级联共用 `parent_id`）；拓扑槽隔离（每个周期在拷贝里干活、
  周期结束合并）。唤醒记在被唤醒者这一支的账上，切不出来就拒收（B-21）。
- CLI：`--max-turns` / `--max-tokens` / `--max-cost` / `--max-wall-clock` 是整棵树的根预算（缺省 200 轮），`--on-exhausted`，
  `.syndica.yaml` 的 `prices:`；结束时一行预算；`trace show` 的「预算：」与「长程：」两节。
- 真实运行（Gemini `gemini-3.5-flash-lite` 免费档，2026-10-06）：仓库外临时工作区，信任存储指到工作区之外的临时文件；
  key 只在子壳里 source，壳外 `env | grep -c ...KEY` 全程为 0。
  - **预算切分**：`--max-turns 8` 的 Orchestrator 先读 `GetBudget`、再 `Delegate` 给 Worker 3 轮；Worker 收到 50% / 10%
    提醒，第 3 轮是收尾轮、`graceful_wrapup` 停下（3 / 3），父用了 7 / 8，回答里报了剩余轮数。
  - **成本路径**（给免费模型一个非零价格）：`--max-cost 0.004` 用到第 13 轮（0.0037497 / 0.004）收尾，`planets.md` 里已
    写的五颗行星原样留着，回答写清没做完的三颗。同一任务把上限压到 0.0012：第一次调用前不知道输出多长、按 2048 token 的
    输出余量估算，第一轮就是收尾轮，而模型在收尾轮里仍发了一个工具调用、没留文字——CLI 照实报「收尾的那一轮也没有留下
    文字」（见遗留）。
  - **长程**：`modules: [acceptance, long-horizon]`、`--goal`，投影出 3 条 Rubric，里程碑与完成验收量出 0/3 → 1/3 → 2/3 →
    2/3 → 3/3，`验收：pass`，`trace show` 的「长程：」列出每次量度。
  - **后台工具**：`background_after_seconds: 2`，`sleep 6 && echo build-ok` 转到后台（`bg-1`），模型用 `QueryBackgroundTool`
    查了三次（两次还在跑、一次已结束），真结果在之后一轮进历史，回答是 `build-ok`；trace 里占位那段是 `backgrounded`、真结果那段是 `ok`。
  - 停滞与漂移的纠偏由脚本化用例证明（计划允许「真模型或脚本化」）。

## 完成判据 × 证据

| 判据（路线图 M8） | L1/L2（脚本化，零成本，随全量测试） | 真实模型（仓库之外的临时工作区） |
|---|---|---|
| 子预算之和不超过父、消耗不为负、释放 / 取消 | `test_budget_tree`（预留、结算、重切、追加、并发兄弟的公平重切、失败与取消交回估算）；`test_budget_review`（准入只因兄弟在飞时等待、价格未知换候选、收尾不被辅助调用吃掉） | 父 7 / 8、子 3 / 3 |
| 四维各自的耗尽路径 | `test_budget_runtime` / `test_budget_review`：轮次、tokens、成本、墙钟（含重试中途过截止时刻停在 BUDGET 而不是 ERROR） | 轮次（子 Agent）、成本 |
| 完成 / 资源 / 停滞三条判据正反 | `test_long_horizon_module`：这个周期的里程碑 pass 才给最终一轮（上个周期的 pass 不算）、策略拒绝时不给；预算耗尽收尾；停滞边沿触发、量过才算、全满足后不报 | 长程任务以 `pass` 完成 |
| 进度回退记为警报 | `test_progress`（满足 → 没能确认 → UNMET 也报）；Module 用例断言 WARNING 的参数、trace 事件与 bus 事件 | — |
| 基线与替换策略过同一份契约 | `tests/contracts/test_long_horizon_strategies.py`；契约的每一条都有只违反它的策略让它变红 | — |
| 预算注入不改 System Prompt 前缀 | 预算块是署名历史块；`test_capture_replay` 的逐块比较按 kind 跳过预算块，前缀守卫照旧 | — |
| 父预算被切分、子无法超额 | `test_budget_tree` / `test_budget_runtime`；`Delegate` / `Fork` 的 `turns` / `tokens` | 子 Agent 3 / 3 |
| 连续 N 轮无进展 → 纠偏而非无限循环 | `test_a_stall_nudges_once_per_window_not_every_turn` 等 | — |
| 漂移：锚点失败 → 语义比对 → 回正 | 锚点检查、语义比对确认 / 否认、比对挂住被截断、比对放不下预算时跳过 | — |
| 接近耗尽时读 `GetBudget` 并交付已完成部分 | 收尾轮用例；`GetBudget` 用例 | 两次运行都读了预算、成本运行收尾交付 |
| 接近窗口上限时主动 `Compact` | M6 的 `Compact` 工具与三触发；目标导向型压缩 | — |
| UI / CLI / trace 显示预算、进度、停止原因 | `test_trace_view`（预算、长程两节）、`tests/cli/test_budget_cli.py` | stderr 一行预算、`trace show` |
| 预算耗尽不丢产物 | 硬停 / 收尾各一条「产物不丢」 | `planets.md` 五颗行星留着 |
| 子 Agent 树不无界增长、增长可归因到切分 | 预算树（每个子至少一轮）；`test_reaping` | — |
| 停滞和漂移有可回放 trace | trace 事件 `progress.*` / `goal.drift` / `strategy.*`；脚本化用例零成本可重跑 | — |

## 计划之外的偏差

- **装了长程、后台与隔离槽的 run 不写回放文档**（P4 / P5）：里程碑与语义比对是伴生调用、后台结果进历史的时刻取决于真实
  耗时、回放不记工作区。「停滞和漂移都有可回放 trace」以 trace 事件 + 脚本化用例（零成本可重跑）兑现，与路线图 E2E 的
  字面不同（计划「明确不做」已写）。
- **墙钟不打断正在跑的一轮**（P3 M5）：截止时刻在轮开始与每次准入处判，越界至多一轮的耗时；打断改派 M9。
- **子树持久化改派 M9**（P1 H6）；预算按「这次调用」计，会话恢复从新预算开始。
- **策略要的收尾以 COMPLETED 结束**（P6 Med3），不是 BUDGET：预算还在，收下的消息照常处理。
- **进度账本对「什么都没能确认」的处置与评审建议不同**（P6 Med2）：里程碑每次都算量度，否则停滞检测在卡住时哑掉。
- **`acceptance_outcome` 关着时长程不再拒绝**（P6 L8）：关掉里程碑与停滞、记一次 WARNING，只剩漂移与预算低的纠偏。
- **Flag 缺省照计划评审**（P4）：语义比对缺省关，停滞 / 预算低 / 漂移窗口收 0 表示关。
- **后台工具照评审收窄**（P1 B4 / P5）：`Delegate` / `Fork` 不能后台化（在轮等待一个 Agent 只有一个槽）。
- **派活树回收在 spawn 里做**（P5）：树只在这时长大，不必另管一个后台任务的生死；只收结果已被取走的子 Agent。

## 公开契约核对

- `kernel.__all__` 新增：`Budget`、`BudgetPolicy`、`BudgetView`、`Exhaustion`、`PriceTable`、`Prices`、`StaticPrices`、`Usage`
  （签名用到）。策略协议、上下文、动作、契约、预算树读写入口、后台调用表、各 Module 扣留到 M12（`_WITHHELD`）。
- `Runtime(..., budget=, keep_finished_children=)`、`spawn(..., budget=)`：都是追加的仅关键字参数。
- `StopReason.BUDGET`；`HistoryKind.BUDGET / PROGRESS / STRATEGY / BACKGROUND`（成员表整张断言）。
- `AgentSlot.isolation`（字段追加在最后）；`Tool.__tool_backgroundable__`（缺省 False）。
- 切点 `TOOL_MOVED_TO_BACKGROUND` / `BACKGROUND_TOOL_COMPLETED`（可观察，随触发点一起加）；`SessionStore.archive` 的文档放宽
  成「可以只留最近若干份、序号不复用」，契约套件不变；注册表契约加一条「`children_of` 与 `parent_id` 是同一棵树」。
- `TRACE_SCHEMA` `/6`：`budget.*`、`progress.*`、`drift.*`、`strategy.*`、`agent.serves`、`compaction.*`、`background.id`，
  `tool.outcome` 加 `backgrounded`；回放场景格式 v4（`host.root_budget`）。
- `docs/reference/flags.md` 重新生成：`long_horizon_*` 六条、`background_after_seconds`、`compaction_strategy` 加 `goal_directed`。
- CLI：`--max-tokens` / `--max-cost` / `--max-wall-clock` / `--on-exhausted`（`run` 与 `chat`），`--max-turns` 改为根预算；
  `prices:`；可选内置 Module 加 `long-horizon`（须与 `acceptance` 一起）与 `background`。

## 遗留

| 条目 | 归属 | 依据 |
|---|---|---|
| 截止时刻到了打断正在跑的这一轮 | M9 | 打断一个正在写的工具要有通用的工具内取消，副作用未知——与后台残留、租约接管同一种语义 |
| 多 Agent 子树的持久化与恢复；预算跨会话 | M9 | 计划 P1 H6 |
| 宿主在 span 之外追加预算时的 trace 事件 | M9 | `_grant_budget` 私有到宿主服务面定 |
| 策略协议 / 上下文 / 动作 / 契约、预算树读写入口进公开面 | M12 | 与 Goal / Rubric / Verdict 一起冻结 |
| `Delegate` / `Fork` 后台化 | 后续 | 要先把在轮等待按子 Agent 分槽 |
| 收尾轮里模型仍可能只发工具调用、不留文字 | 后续 | 收尾轮照发工具定义以保前缀；provider 支持时可带 `tool_choice: none` |
| 第一次调用前按 2048 token 的输出余量估算，紧的成本上限下第一轮就是收尾轮 | 接受 | 保守的一侧；模型配了 `max_tokens` 时按它估 |
| 装了长程 / 后台 / 隔离槽的 run 的回放 | 后续（回放文档 v4） | 与 M7 的验收同属 v4 的槽位 |
| 非免费模型没有内置价格，成本记为未知 | 接受 | 设了成本上限就在 `prices:` 里写价格；价格未知时那个模型在成本上限下用不了，ERROR 点名 |

## 评审与变异

| 评审 | 结论 | 处置 |
|---|---|---|
| 计划评审 | 4 Blocking / 8 High / 16 Medium / 10 Low | 全部折进计划（P1） |
| 阶段一 | 0 Blocking / 3 High / 7 Medium / 12 Low | `ba36eb1`（P3）：兄弟在飞的估算被当成耗尽（H1）、价格未知报成预算耗尽且中止轮换链（H2）、摘要压缩吃掉收尾那次调用（H3）等；19 条存活变异补了用例 |
| 阶段二 + 三 | 0 Blocking / 3 High / 6 Medium / 11 Low | `2783295`（P6）：上个周期的 pass 让下个周期一开头只交付（H1）、里程碑吃掉收尾的预算（H2）、策略文字不随它读的东西降信任（H3）等；51 条存活变异逐条处置 |
| 阶段五 | 见下 | 见下 |
| 收口（整个 M8） | 见下 | 见下 |

**变异的诚实交代。** 每个阶段与每次评审处置都做了「sha256 先校验 → 施加 → 清字节码 → 跑定向用例确认变红 → 还原 → 再按
sha256 校验」的定向变异：阶段一评审处置 40 条、阶段二 + 三 53 条（首轮 9 条存活：7 条补了用例，2 条是死代码或多余的防御、删掉）、阶段二 + 三
评审处置约 75 条、阶段五 a 24 条、阶段五 b 21 条，处置后全部变红。但每一轮的变异集都偏向作者当时在想的路径——阶段二 + 三的评审自己施加了 106 条，51 条存活。几种反复出现的
盲区：两层防御互相掩护（各自单独施加都杀不掉，处置是删掉多余的一层或各补一条单独的用例）；端到端被主轨迹溯源盖住的
信任（只能在控制器层钉）；脚本化模型不让出事件循环，几个 Agent 不交替（路由在每次调用时让出一次）。「零存活」只在给定的
那组变异下成立。

## 验证

```text
uv run ruff check .                                          通过
uv run ruff format --check .                                 通过
uv run pyright                                               0 errors
uv run python scripts/check_deps.py                          通过
uv run python scripts/check_links.py                         通过
uv run pytest -q                                             全部通过（数目见收口提交的说明；flags.md 的金标由用例守着）
```

推送前对提交的树做导出验证（把这次提交单独检出到临时目录、在那里跑上面每一道门禁）。
