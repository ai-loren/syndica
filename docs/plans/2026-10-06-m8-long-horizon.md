# 里程碑 8：长程任务（Long-Horizon）

> **执行者须知：本文件是需求，不是参考。** 「公开契约」一节里的 `Budget` / `Usage` / `Prices` 字段与顺序、`PriceTable` /
> `LongHorizonStrategy` Protocol 方法、策略动作与上下文类型、`StopReason` / `HistoryKind` 新成员、新增的内核切点、长程
> Module 的 Flag key、CLI 参数、trace 的新属性与事件，一旦发布就是兼容承诺；执行中不得为了让测试变绿临时改名或缩小
> 断言范围。**形状按阶段冻结**：各公开类型在其所属阶段结束、契约与用例落地时才算冻结。

**状态：执行中。** 用户 2026-10-06 下达 `/goal 把 M8 都做完`：写计划、评审计划、执行一次做完，沿用 M4–M7 的精简节奏。
CI 仍推迟到 M1–M11 都做完之后，只按本地验证协议验证。

**Goal:** 让 Syndica 能跑数小时到数天的任务，且长跑**可控、可研究**：Goal 独立于对话存储、只在人明确改要求时变；
**预算**是一等资源（tokens / cost / wall clock / turns 四维，父子预算树、子 Agent 从父切分不得超额，成本按输入 /
输出 / 缓存命中三档计量、价格表归 backend），Agent 经注入历史的预算块与 `GetBudget` 工具**看得到**自己还剩多少，
耗尽默认 `graceful_wrapup`（收尾交付已完成的部分，不硬杀）；**进度**只来自验收（已满足的强制 Rubric / 强制总数，
不采信 Agent 自述），进度回退记警报；**三条正交的停止判据**（完成 / 资源 / 停滞）；**漂移检测**两层（零成本锚点检查
常态跑，可疑时才一次语义比对）；**长跑策略可插拔**（`LongHorizonStrategy` Protocol + 内核基线实现 + 契约套件，换
范式写 Module 不动内核）；分级质量门（L1 每次产物变更、L2 收尾、L3 里程碑）。并兑现标给 M8 的遗留：伴生调用进
trace 与预算、目标导向型压缩、压缩前完整前像、`short.json` 回收、后台工具（§5.6 三件套 + §5.4 两个切点）、拓扑槽
隔离、派活树的上限与回收（D-46）、唤醒记账（B-21）、后代的单一定义（B-22）、`--max-turns` 改为父子切分、多 Agent
子树的持久化与恢复。

**Architecture:** 预算树、计量、耗尽处置与预算可感知在**内核**（预算是一等资源，不装任何 Module 也生效：CLI 的
`--max-*` 直接给根预算）。进度账本、停滞 / 漂移检测、策略驱动在一个可选内置 Runtime Module（`long-horizon`），
它经内核私有接缝（仿 `_attach_rubrics`）在每轮模型调用前拿到一次「这一轮要不要注入什么、要不要停」的决定，策略本身
是 Protocol，Module 构造时注入（缺省基线），第三方换范式就是给 Module 一个别的策略。内核只追加署名历史块
（`HistoryKind.BUDGET / PROGRESS / STRATEGY`），hook 与策略不写轨迹。后台工具是**执行器**的能力（一次调用跑过阈值
就转后台、交回占位结果，完成后在后续轮次第 4 步注入），由 §5.4 的两个可观察切点报告。

**Tech Stack:** 不新增运行依赖。成本用 `decimal.Decimal`；价格表 Protocol 在内核、默认价格在 `backends/llm_openai`
（只放确知的免费档 0 价），用户在 `.syndica.yaml` 的 `prices:` 覆盖。漂移的锚点检查是纯函数（词面重叠），语义比对
与里程碑 L3 走 M7 的伴生运行器。

**依据：** spec §4.2 / §4.4（Runtime / Store）、§5.1 第 4 步（注入背景工具结果）、§5.2（前缀缓存）、§5.4（背景工具
切点）、§5.6（`GetBudget` / `Compact` / `QueryBackgroundTool` / `CancelBackgroundTool`）、§7.5–§7.6、§8.1–§8.9、§11
（trace 可观测量）、§12（安全）、§15（M8 完成判据）、§17 #13 / #35–#38 / #40 / #48 / #56 / #63–#66 / #69 / #86；路线图
「M8 长程任务」一节与遗留表里标 M8 的 13 条（B-21、B-22、D-46、真实体验验收两条、M5 后续两条、M6 后续两条、
`short.json` 回收、M7 后续三条）；M5 / M6 / M7 收口审计；`AGENTS.md`。

---

## 硬约束（沿用，违反即打回）

与 M1–M7 相同，`AGENTS.md` 为准。本里程碑尤其承重的：

1. **kernel 不依赖 surface / backend，不读环境变量，零重依赖。** 价格表只是 Protocol；默认价格在 backend，宿主组装。
2. **预算机制的目的不是天花板，是让长跑可控可研究（§8.4）。** 耗尽默认 `graceful_wrapup`；`hard_stop` 与 `ask_human`
   可配；无人值守下 `ask_human` 退回 `graceful_wrapup`（§12：无人值守默认拒绝而非等待——等人会一直占着）。
   **预算耗尽不丢已有产物**（工作区里的文件与会话照存）。
3. **前缀缓存（§5.2）。** 预算 / 进度 / 策略块只进**历史**、只追加、幂等键防重注；不进 SP；`GetBudget` 与后台工具的
   定义只在用得到时装（不让每个不用预算的 run 多付工具定义、改 Worker 的前缀）。
4. **预算树不变量（静态验收）。** 可加维（tokens / cost / turns）：本节点自己的消耗 + 在飞估算 + 已拆除子树的消耗
   + 活着的子节点的预留之和 ≤ 本节点此刻的预留；消耗不为负；子节点周期收尾时缩到实际占用、拆除时整棵结算，余量回父；
   切分失败给三件套。wall clock 不可加：子节点的截止时刻不晚于父节点。四维各有耗尽路径用例（模型见 P1 B1）。
5. **计量三档（§8.4 / #69）。** 输入 / 输出 / 缓存命中分开记；cost = Σ 档位 token × 档位单价；价格不认识时 cost
   **未知**——设了 cost 上限却有模型价格未知，在 spawn 处三件套拒绝（fail closed，不当 0 价算）。backend 没报 usage 时
   按字符估算 token 并在 trace 标「估算」。
6. **进度不来自 Agent 自述（§8.3 / #36）。** 只来自验收裁决的逐条结论；进度回退（此前满足的强制条现在不满足）记
   `progress_regressed` 警报事件与 trace 事件。
7. **停止判据三条正交（§8.5 / #37）。** 完成（强制条全满足且裁决 pass）、资源（预算耗尽 → 耗尽处置）、停滞（连续
   N 轮无进展 → 策略纠偏，**不是终止**）；正反用例互不替代。
8. **策略可插拔（§8.6 / #38）。** `LongHorizonStrategy` 四个方法签名按 spec；基线与至少一个替换实现过**同一份**契约
   套件；策略交回的文字由内核署名注入，信任随策略作者（第三方被收紧的 Module 写的 → 不可信，D15）。
9. **伴生调用进 trace 与预算（M7 后续）。** 投影 / L2 / L3 / 反馈提议 / 语义漂移比对的模型调用记在被服务的那个
   Agent 的预算节点上，并在 trace 里成为它的子 span。
10. **后台工具不改变工具调用与结果的配对（§8.8 不变量 1）。** 转后台的调用立刻交回一条占位结果（配对完整），完成的
    结果在后续轮次第 4 步以署名块注入，信任随原工具结果；取消照常上抛；Runtime 关机时未完成的后台调用取消并记录。
11. **面向调用方的错误三件套**；Agent 不执行 git 写命令；新落盘点经脱敏（AST 守卫）。

---

## 公开契约（发布即承诺）

> 目标形状，按阶段冻结；执行中需调整先改本节 + 决策日志。

### 预算与计量（§8.4，kernel，公开）

- `Budget`（frozen）：`tokens: int | None`、`cost: Decimal | None`、`wall_clock_s: float | None`、`turns: int | None`
  （spec 原样）。拒 bool、负数、非有限数；全 None = 不限。
- `Usage`（frozen）：`input_tokens`、`output_tokens`、`cached_input_tokens`（输入里命中缓存的那部分）、`cost:
  Decimal | None`（None = 价格未知）、`turns`、`estimated: bool`。`tokens` 维 = 输入 + 输出。
- `Prices`（frozen）：`input`、`output`、`cached_input`（每百万 token，`Decimal`）；`PriceTable` Protocol：
  `price_of(model: str) -> Prices | None`。
- `Exhaustion`（enum）：`graceful_wrapup`（缺省）/ `hard_stop` / `ask_human`。
- `BudgetPolicy`（frozen）：`root: Budget`、`on_exhausted: Exhaustion`、`prices: PriceTable | None`、`ask_human`
  （async 处理器）、`ask_timeout_s`。`Runtime(budget=BudgetPolicy | None)`；`Runtime.spawn(..., budget=Budget |
  None)`。读视图与追加走私有 `_budget_of` / `_grant_budget`（P1 H5）。
- `StopReason.BUDGET = 'budget'`（新成员；trace 的 `stop_reason` 取值域同步）。
- `HistoryKind.BUDGET / PROGRESS / STRATEGY / BACKGROUND`（落盘取值，成员表整张断言）。
- 内置工具：`GetBudget`（有预算时由 Runtime 装）、`QueryBackgroundTool` / `CancelBackgroundTool`（开了后台化才装）；
  `Delegate` / `Fork` 加可选参数 `budget`（`{"turns": .., "tokens": ..}`，切不出来就三件套交回模型让它缩小任务）。
- 内核切点：`TOOL_MOVED_TO_BACKGROUND` / `BACKGROUND_TOOL_COMPLETED`（OBSERVABLE，spec §5.4 已列）随阶段五的触发点一起
  加（P1 M15）。

### 长程（§8.2 / §8.3 / §8.5 / §8.6，kernel 协议 + Module）

- `LongHorizonStrategy`（Protocol，spec 原样四个 async 方法）；`LongHorizonContext`（frozen 快照：`agent_id`、`cycle`、
  `turn`、`goal`、`rubrics`、`progress: ProgressSnapshot`、`budget: BudgetView | None`、`stalled_turns`、`drift:
  DriftReport | None`、`verdict`）；动作类型 `StallAction` / `BudgetAction` / `DriftAction` / `StopDecision`（frozen，
  各一个封闭的 move 枚举 + 可选文字）。`BaselineStrategy`（内核）。`LongHorizonStrategyContract`（随 wheel 发布）。
- `ProgressSnapshot`：`satisfied: tuple[str, ...]`、`mandatory: tuple[str, ...]`、`pending_human: tuple[str, ...]`、
  `ratio: float | None`（无强制 checkable 条时 None）、`measured_turn`。
- 长程 Module `long-horizon`（可选内置）Flag：`long_horizon_progress_every`（int ≥ 0，缺省 10；0 = 只在完成时量）、
  `long_horizon_stall_turns`（int ≥ 1，缺省 20）、`long_horizon_budget_low_percent`（1–99，缺省 20）、
  `long_horizon_drift_window`（int ≥ 0，缺省 8；0 = 关）、`long_horizon_semantic_drift`（bool，缺省 true）、
  `long_horizon_max_continuations`（int ≥ 0，缺省 3：完成判据没满足时策略可让 Agent 接着干几次）。

### CLI

- `run --max-turns`：改成**根预算**的 turns（整个子树总轮数，父子切分），不再是每个 Agent 各自的上限（角色清单的
  `budget.turns` 仍是该角色的单 Agent 上限）。`--max-tokens` / `--max-cost` / `--max-wall-clock` 给根预算（不再三件套
  拒绝）。`--on-exhausted {graceful_wrapup,hard_stop,ask_human}`。`.syndica.yaml` 的 `prices:` 覆盖价格。
- run 结束在 stderr 打一行预算（用了 / 上限，三档 token 与 cost）、进度与停止原因；`trace show` 显示预算、进度、
  停止原因、漂移 / 停滞 / 进度回退事件。

---

## 阶段与任务

> 每个任务：实现 + 测试同出；一次全套；阶段末导出验证再推送。每阶段一轮后台对抗评审（不挡提交）。

### T0：类型、协议、切点

`Budget` / `Usage` / `Prices` / `PriceTable` / `Exhaustion` / `BudgetView`；`StopReason.BUDGET`、新 `HistoryKind`
成员（成员→取值表整张断言）；策略 Protocol / 上下文 / 动作类型 / `ProgressSnapshot`；两个背景工具切点（分类守卫、计数、
spec §5.4 同步）；trace schema `/6`（`budget.*`、`progress.*` 属性与 `progress_regressed` / `progress_stalled` /
`goal_drift` / `budget_exhausted` / `background.*` 事件）；公开面守卫。

### 阶段一：预算树、计量、耗尽处置、可感知（内核）

- 预算树（私有 `kernel/budget.py`，Runtime 持有）：节点随 spawn 建、随 despawn / kill / EXIT 结算；切分（显式请求
  ≤ 父节点剩余；不请求 = 父剩余的一半，Flag 化留给宿主参数）；扣账（turn 开始扣 turns；模型调用后按 usage 扣
  tokens / cost，沿祖先链）；wall clock 用截止时刻继承。
- 计量：模型调用后读 `reply.extra['usage']`（三档）；没有就估算并标 `estimated`；cost 用 `PriceTable`；cost 上限 +
  未知价格在 spawn 处拒绝。默认价格（backend）只放 OpenRouter 的 `:free` / `openrouter/free` = 0。
- 耗尽处置：turn 开始与模型调用前检查（四维）；`hard_stop` → `StopReason.BUDGET`；`graceful_wrapup` → 注入署名收尾块、
  给最后一轮且这一轮不发工具定义，之后 `BUDGET`（越界上界：一轮，写进文档与用例）；`ask_human` → 宿主
  `budget_handler(agent_id, view) -> Budget | None`（追加或不追加），没有处理器 / 无人值守 → 当 `graceful_wrapup`。
- 可感知：有预算时，cycle 起点与剩余跨过 50% / 25% / 10% 时追加 `HistoryKind.BUDGET` 块（幂等键按阈值）；`GetBudget`
  工具交回 limit / used / remaining（三档 token、cost、turns、剩余秒数）。
- 伴生：`run_companion` 收被服务 Agent 的预算节点与 tracing，伴生的 turns / tokens / cost 记在它账上，trace 里是它的
  子 span（M7 后续）。
- 唤醒记账（B-21）：发给预算耗尽的 Agent 的消息在 `send` 处三件套拒绝（不唤醒、不静默丢）；唤醒的消耗记在被唤醒者
  自己的节点上。
- CLI：`--max-*` 与 `--on-exhausted` 进根预算；`prices:`；结束时的预算行；trace 显示。

### 阶段二：进度账本、停滞、漂移（Module 内 + 内核纯函数）

- 进度账本：每次裁决（完成时的 L3，加 `long_horizon_progress_every` 轮一次的里程碑 L3）记一条；比例只数强制
  checkable 条，judgment 强制条列为待人判；回退 → 警报事件 + trace 事件 + 进策略上下文。
- 停滞：自上次进度提升以来的轮数 ≥ `long_horizon_stall_turns`（且其间至少量过一次）→ `on_progress_stall`。
- 漂移：最近 `long_horizon_drift_window` 轮的工具调用（名字 + 参数）与产物路径，对 Goal intent / Rubric 陈述 / 锚点
  引文 / 交付物路径做词面重叠（锚点检查，零 LLM）；重叠为空 → 可疑 → `long_horizon_semantic_drift` 开时一次伴生比对
  （JSON `{"on_track": bool, "reason": ...}`，失败 fail-open 为「不确认漂移」）→ 确认漂移 → `on_goal_drift`。每个窗口
  至多一次语义比对。

### 阶段三：策略与长程 Module

- 内核接缝 `_attach_long_horizon(controller)`：每轮在消息 / Goal 注入之后、模型调用之前问一次「追加哪些块、要不要停」；
  完成提议之后（`_propose_completion` 交回「完成」时）再问一次「是否接着干」（受 `long_horizon_max_continuations`）。
- `BaselineStrategy`：停滞 → 一段「换条路或说明卡在哪」的提示；预算低于阈值 → 「先保住核心交付物」；漂移 → 重申
  Goal；`should_stop` → 完成判据满足时停。替换实现（测试里的「简单重启循环」策略）与基线过同一份契约。
- `LongHorizonModule`（`requires=('acceptance',)`：进度要验收）+ CLI 接线（`OPTIONAL_BUILTINS`、flags.md 金标）；分级
  质量门：L1 每次产物变更（已有）、L2 完成时（已有）、L3 完成时 + 里程碑。

### 阶段四：上下文与状态的遗留

- 目标导向型压缩（§8.8 第四种，`compaction_strategy: goal_directed`）：在规则型的基础上，按与**未满足 Rubric**（及
  Goal）的词面相关度挑要保留的旧块；五条不变量照旧由 `apply_compaction` 校验。
- 压缩前完整前像（M6 后续）：会话持久化开着时，压缩前把当前记录 `archive` 进 `SessionStore`（脱敏后），trace 的压缩点
  记下归档序号。
- `short.json` 回收（M6 收口 M5）：整理之后把 `seq ≤ through_seq` 的短期条目裁掉。

### 阶段五：多 Agent 的遗留

- 后台工具（M5 后续，§5.6 / §5.4）：执行器的「过阈值转后台」（工具声明可后台化，缺省 `Delegate` / `Fork` / `Bash`；
  阈值 Flag，0 = 关）、占位结果、第 4 步注入、`QueryBackgroundTool` / `CancelBackgroundTool`、两个切点、关机取消。
- 派活树上限与回收（D-46）：子 Agent 必须切得出预算；每个父节点结束了的子 Agent 超过上限（宿主参数，缺省 8）时回收
  最早结束的那些（despawn，预算结算）。
- 后代的单一定义（B-22）：注册表的 `parent_id` 是唯一定义，预算树与之一致（一致性用例）；遍历照旧带环保护。
- 拓扑槽隔离（M5 后续）：`AgentSlot(isolation=...)`，每个周期结束（槽回到 IDLE）时合并回去、下个周期重新隔离；冲突写
  进下一条输入。
- 多 Agent 子树的持久化与恢复（M6 后续）：改派 M9（P1 H6）。

### 阶段六：收口

- 免费模型真实运行（仓库外临时工作区）：带预算的多 Agent 任务（父预算被切分、子 Agent 超不了）、`GetBudget` 被读、
  预算耗尽 → 收尾交付；构造停滞（脚本化）与漂移（真模型或脚本化）各一次，trace 可查。
- spec §15 / 路线图 / README；收口审计 `docs/audits/<date>-m8-final-audit.md`；收口对抗评审一轮。

### 明确不做（附理由，收尾时复核）

- 跨进程的租约与孤儿接管（#40 的完整形态）：单进程下会话锁已够；多进程宿主随 M9 的服务面定。
- 多 Agent 子树的持久化与恢复：改派 M9（理由见 P1 H6）；预算按「这次调用」计。
- 回放文档 v4（伴生 / aux / 门槽位）：长跑 run 装了验收与长程 Module 时照旧不写回放文档；停滞与漂移的「可回放」以 trace
  事件 + 脚本化用例（零成本可重跑）兑现，不以离线回放文档兑现——这一条与路线图 E2E 的字面不同，写进审计。

---

## 验证节奏

- 每个任务：`ruff` / `format --check` / `pyright` / `check_deps` / `check_links` / `gen_flag_reference --check` 全绿；
  受影响用例先跑，再跑一次全套。
- 定向变异（每条单独施加即变红）：预算树不变量（子上限之和、退回预留、负消耗）、四维各自的耗尽路径、三档计量、未知
  价格 fail closed、`graceful_wrapup` 那一轮不发工具、进度只数强制 checkable、回退报警、停滞判据、锚点检查不发 LLM、
  语义比对每窗口至多一次、策略文字的信任、后台占位配对、关机取消后台、唤醒耗尽者被拒、回收结算预算、前缀不变（预算块只
  追加）。
- 阶段末导出验证（`git archive` + `uv sync --all-extras` + 全门禁 + 全套）再推送。

---

## 决策日志

- **P0（计划，2026-10-06）** 本文件初稿。待计划评审一轮（P1）。
- **P1（计划评审，4B / 8H / 16M / 10L，2026-10-06）** 逐条处置如下；与正文冲突处以本条为准，正文已按它改过的
  地方不再重复。
  - **B1 预算模型 → 预留 + 周期收尾结算 + 唤醒重切。** 节点记 `cap`（切分时定的上限）、`limit`（此刻预留着的
    额度）、`own`（自己的消耗，含伴生与辅助调用）、`pending`（在飞调用的估算）、`closed`（已拆除子树的实际消耗）、
    `running`。有上限的子在父那里占 `limit`；父的可用 = limit − own − pending − closed − Σ 子的 limit。消耗只记在
    自己身上，子树合计是派生视图（不沿链重复记账）。周期收尾时 limit 缩到实际占用、余量回父（做完的 zombie 不再按住
    父的额度）；下个周期开始时 limit 长回 cap，从父的可用里取，父也在收尾态时沿祖先链往上取——「记在谁账上」
    （B-21）＝它这一支血统，直到最近一个正在跑的祖先，唤醒者恰是那个祖先时就是它付；一点都取不到 → `send` 三件套
    拒绝。顶层 Agent 不切分、直接从 Runtime 池取（CLI 的根拿到全部 `--max-turns`）；整条链都不限的那一维不预留。
    子至少切一轮，显式请求 0 拒绝。不变量用例从脚本化 LLM 报的 usage 独立算总账，不复用树自己的算术。
  - **B2 收尾在上限之内。** 轮次剩 1 → 这一轮就是收尾轮，剩 0 → 不调模型直接 `BUDGET`（交回最后一段文字）；
    tokens / cost 剩余不够「这一次 + 下一次」两次调用的估算 → 收尾轮，不够一次 → 直接停（估算 = `traj_tokens` +
    工具定义 + 输出余量，按上一次实报 / 估算之比校准）；wall clock 剩余不够两轮（按上一轮耗时）→ 收尾轮，过了截止
    时刻 → 直接停。收尾轮照发工具定义（前缀不变，provider 不拒），这一轮的工具调用由内核以「未执行：预算用完」交回，
    `send_back` 与「接着干」不生效；验收照跑，伴生调用按预算准入，准入不了 → `unverified`（`budget_exhausted`）。
    越界只剩调用前估算的误差。
  - **B3 策略文字信任** = 最严（作者底线、Goal、每条 Rubric、裁决、漂移报告、主轨迹溯源）；作者底线只有内核
    `BaselineStrategy` 是可信，其余缺省不可信，宿主用 `trusted_strategy=True` 声明（同第三方 Skill）。
  - **B4 后台工具收窄**：工具类声明 `__tool_backgroundable__` 才可后台化（Bash 声明；Delegate / Fork 不，人工批准
    的调用不）；阈值 Flag 归 `background` Module，缺省 0；有在飞后台调用时不能 COMPLETED；`_cycles` 把待注入的
    结果当未处理输入；POST hook / 观察者 / 产物登记 / tool span 在真结果上、在原调用的上下文里跑；abort / kill /
    despawn / 关机取消并记录；恢复时残留占位按「被打断、副作用未知」；开了后台化不写回放文档；时钟可注入。
  - **H1 单一准入与计量点**：都挂在 `llm_attempt`（主轮、重试、轮换、辅助、伴生都经过它），节点由 `llm_call`
    作用域带进来；准入按估算预占 `pending`；定价按作答的模型；价格未知且有成本上限 → 该节点成本按耗尽算，ERROR
    点名模型与 `prices:`。
  - **H2 根预算范围**：`BudgetPolicy.root` 是虚拟根，每个无父 spawn（宿主、拓扑槽、恢复）都在它之下；槽的
    `budget.turns` 保持「实例 max_turns」语义（M8 不扩 `AgentSlot`）；CLI 不给 `--max-turns` 时根预算缺省 200 轮
    （每个子至少切一轮，于是树的规模有界且可归因到切分）；内核缺省不限（宿主的选择，写进 docstring）。
  - **H3 BUDGET 的读者同提交落地**：`_UNFINISHED` / `_NO_ANSWER` / `_CUT_SHORT`；`Delegate` 交回收尾文字并标「部分
    交付」；`_next_step` 不建议 SendMessage；`_cycles` 遇 BUDGET 退出；被丢输入那条 ERROR 提到预算；CLI 对 BUDGET
    与 MAX_TURNS 同等处理。
  - **H4**：追加只经私有 `_grant_budget`，被收紧的 Module 调用时三件套拒绝；每次追加记 trace 事件；追加是新钱，节点
    与每个有上限的祖先同加；`ask_human` 处理器 async、按确认处理器的规则校验、`asyncio.timeout`（缺省 300 秒），
    超时或抛出 → 按 graceful_wrapup。切分不超过父的可用，不铸钱，不必另拒。
  - **H5 公开面**：`Budget` / `Usage` / `Prices` / `PriceTable` / `StaticPrices` / `Exhaustion` / `BudgetView` /
    `BudgetPolicy` 公开（签名用到）；策略协议、上下文、动作、契约随 Goal / Rubric / Verdict 扣到 M12；
    `budget_of` / `grant_budget` 私有；Runtime 只加一个参数 `budget: BudgetPolicy | None`（L10）。spec 同步。
  - **H6 子树持久化改派 M9**：会话记录是公开格式；Fork / 槽 / 类式角色的子 Agent 从角色名重建不出来；邮箱只在
    checkpoint 时存，崩溃会丢已确认的消息——这些与 M9 的多进程租约（#40）同一处定。M8 只定预算按「这次调用」计，
    会话恢复从新预算开始（用例钉住）。
  - **H7 回收**只收：整棵子树都是 zombie、没有保留中的隔离副本、没有在飞等待 / 后台调用指向它；上限 ≥ 1、拒 bool；
    按结束先后；被收的 id 再被寻址时说清它已被回收；≥ 3 个子乱序结束的用例。
  - **H8 策略的停止**：动作枚举去掉 STOP（停滞、漂移是纠偏不是终止）；`BudgetMove` = CONTINUE / WRAP_UP；
    `should_stop` 只在 §8.5 完成判据成立时兑现，兑现成「安排一轮最终回答」；「接着干」只在验收打回用完之后、只对
    partial / block、按周期重置。不加 StopReason。
  - **M1** 幂等键带 agent_id 与追加代数（`budget:{agent}:{gen}:{阈值}`、周期起点 `budget:{agent}:c{cycle}`），
    PROGRESS / STRATEGY 同。**M2** 回退只算「满足 → UNMET」、只比两次都在的 id；集合变了重新起算；不持久化；无强制
    checkable 条时 WARNING 并退回全部 checkable。**M3** 停滞边沿触发 + 冷却；启动时校验 Flag 组合；只作用于有 Goal 的
    Agent。**M4** 分词用 `kernel/lexical.terms`，排除内容参数、保留路径；语义比对缺省关；空窗口不可疑；结论信任 =
    伴生溯源 ∧ 窗口溯源；中文 Goal + 英文参数夹具。**M5** 轮内顺序：门 → 注入消息 → 后台结果 → Goal / Rubric →
    预算判定（硬停在注入之后）→ 预算块 → 长程控制器 → 压缩 → TURN_START；压缩保护最新 BUDGET 块与未看过的
    BACKGROUND 结果。**M6** 截止时刻到达时协作式中止、停止原因记 BUDGET；工具超时与轮内等待按剩余时间收紧；挂起
    时间也算。**M7** 伴生 tokens / cost 记在被服务节点、轮次不占；id 由稳定键派生；span 挂在当前 span 下、带
    `agent.serves`。**M8** `CompactionRequest.needed` + 轨迹兜底（已落）。**M9** 前像接缝已落；归档失败阈值路径跳过
    这次压缩、超限路径照压；保留最近 20 份；`sessions rm` 一并删。**M10** `AgentSlot.isolation` 是结构不是行为；每次
    `run` 返回时合并；冲突以署名块注入。**M11** 注册表契约给「后代」单一定义（向上走、带环保护），KillAgent 与级联
    共用。**M12** `--max-turns` → 根预算，回放宿主画像版本加一。**M13** 缓存写入不报，写进文档；价格按（平台,
    模型）查；YAML / `--max-cost` 走 `Decimal(str(x))` 并拒 bool / 负 / 非有限；`prices:` 在受信配置里，run 开头回显。
    **M14** 策略契约四条；策略 / 价格表 / 处理器抛出或挂住 → `asyncio.timeout` + 失败开放 + ERROR。**M15** T0 撤掉两个
    后台切点（随阶段五触发入），阶段顺序：一 → 二+三 → 四（已完成）→ 五。**M16** GetBudget 同时报上下文占用；硬停 /
    收尾各一条「产物不丢」；漂移断言回正；Goal 变更重新起算。
  - **L1** `Delegate` / `Fork` 加平铺可选整数 `turns` / `tokens`（拒 bool / ≤ 0；cost 不进工具参数，成本只由宿主
    定）。**L2** 成本记字符串；自由文本过 Redactor；新事件名列闭集。**L3** 成员表整张断言，三件套断言锚词。**L4**
    §8.7 的「L2 阶段收尾」＝每次完成提议。**L5** 已落。**L6** 真实运行给免费模型一个非零价格。**L7** 预算块只写事实，
    「该怎么办」只由 `on_budget_low` 写。**L8** 重申 Goal 原文、信任随 Goal。**L9** 不成立：四个 HistoryKind 是本
    里程碑 T0 加的。**L10** 采纳。
- **P2（阶段一执行中，2026-10-06）**
  - **预算块不进回放指纹。** 父的剩余取决于并发的子 Agent 此刻占着多少（预留随周期结算、随唤醒重切），
    同一份记录回放时「剩不到 50%」那块可能出现在另一轮；它只说事实、不改变别的块，所以按 kind 从
    `RequestFingerprints` 里跳过，回放用例的逐块比较同一条规则。预算真正收紧、且有 Agent 并发时，停下与
    收尾的那一轮本身仍可能随先后变化——与墙钟预算同类的限度，写进审计。
  - **唤醒重切一次至多拿父此刻能给的一半**（与切分同一条规则）。同时被叫醒的几个兄弟各自长回整个 cap
    时，先到的两个就把池分光，第三个一个周期都开不了（真实复现：fan-in 回放用例里第三个子 Agent 停在
    budget、那条消息没发出去）。
  - **「你的预算」只在第一个周期注入一次**，此后靠阈值块与 `GetBudget`：chat 的每条消息都是一个周期。
  - **CLI**：`--max-turns` 等是整棵树的根预算，缺省 200 轮；不再逐角色改写上限（角色清单的
    `budget.turns` 照旧约束那个 Agent）。预算那一行只在人自己设了预算、或真停在预算上时打。成本 / 墙钟 /
    `ask_human` 的 run 不写回放文档（价格表、真实时间、人的答复都不在文档里）；轮次与 token 的根预算写进
    回放文档 v4 的 `host.root_budget`，回放原样重建。chat 里预算用完的 Agent 拒收消息（B-21）。
    默认价格由组合根把 backend 的 `FreeTierPrices` 交给宿主（surface 不 import backend）。
  - **收尾轮的估算把这一轮还会追加的两块预算块算进去**：不算的话，计划好的那次收尾调用会在准入处被拒，
    「这是最后一轮」那块后面就没有调用（扫一段预算的性质用例钉住）。
- **P3（阶段一对抗评审，0B / 3H / 7M / 12L，2026-10-06）** 逐条处置；19 条存活变异与处置新增的 21 条一起
  单独施加复验，40 条全部变红。
  - **H1 兄弟在飞的估算不再当成耗尽。** 轮开始的判定只看已落定的占用（自己与子树的消耗、已拆除子树、预留），
    不看别的节点在飞的估算；准入时只因兄弟在飞而放不下，就等树上有调用回来再判（至多 60 秒，且不过截止时刻），
    真放不下才拒。输出余量按实报的输出校准（下限 256、上限 2048），输入校准夹在 [0.5, 4]。
  - **H2 价格未知是 `PriceUnknown`（`BudgetExhausted` 的子类）。** 重试 / 轮换把它当成这个候选用不了、换下一个，
    不中止整条链；每个模型记一次 ERROR，点名模型与 `prices:`；验收原因 `price_unknown`。
  - **H3 收尾轮不做阈值压缩**：摘要压缩是一次辅助调用，花的正是留给收尾那次调用的预算。
  - **M1** 收尾态的中间节点把子树退回的余量继续往上交，交到最近一个正在跑的祖先。**M2** 缺省切分时父是 Agent
    就先留下自己的一轮再对半，父不会被切走最后一轮。**M3** 在收尾态或尚未开跑的父下切分，父先从自己的 cap 长回
    子需要的那一部分。**M4** 价格表抛出 → 按价格未知、ERROR 点名价格表与模型（失败开放，设了成本上限的那一层
    因此按耗尽算）。
  - **M5 改派 M9：截止时刻到达时打断正在跑的这一轮。** M8 的墙钟在轮开始与每次准入处判，越界至多一轮的耗时。
    打断一个正在跑的工具需要通用的工具内取消 / 超时，被打断的写操作副作用未知——与后台调用残留、M9 的租约接管
    是同一种语义，一起定。写进审计。
  - **M6** 内置两个会话 store 只留最近 20 份前像，序号不复用；`SessionStore.archive` 的文档放宽成「可以只留最近
    若干份」，不加进契约套件：已照契约自证合规的第三方 store 不必改。
  - **M7** 伴生记账（经验收式 `COMPLETION_PROPOSED` hook 跑 `run_companion`：记在被服务节点、span 挂在它下面、
    id 稳定）与失败 / 被杀时交回估算，各有端到端用例。
  - **L1** 收尾轮的回复带文字就照常过验收（不打回）；`Delegate` 的部分结果只取收尾这一次回复的文字。**L2** 最新的
    BUDGET 块受压缩保护。**L3** 宿主在 span 之外调 `_grant_budget` 时那条 trace 事件丢掉：追加入口在 M9 的宿主
    服务面定之前保持私有，届时随入口一起挂到 Agent 的根 span。**L4** `_SERVED` 与 `_grant` 私有，追加只有
    `_grant_budget` 一条路。**L5** `StaticPrices` 不知道平台时取同名模型最贵的那个平台价（保守）。**L6** 三件套锚词
    逐条断言。**L7** `Delegate` / `Fork` 的 `turns=0` / `tokens=0` 等于省略（工具说明里写明）：平铺可选整数下模型
    常把「不在乎」填成 0，拒掉会让无害的调用失败；内核 `spawn(budget=Budget(turns=0))` 照旧拒。**L8** 压缩策略
    参数校验；原块从后往前对（相等的两块里受保护的是后一块）；词面单位认假名、谚文、带重音的拉丁字母，纯数字与
    编排调用的 JSON 键不算。**L9** `_drive` 与 `_reap` 清掉 `_SERVED`。**L10** 分叉跟源 Agent 的工具表走（源没有
    GetBudget，分身也没有）。**L11** 会话恢复从新预算开始、硬停 / 收尾各一条「产物不丢」、每个 Agent 只注入一块
    「你的预算」。**L12** 每次问人之后都重判。
- **P4（阶段二 + 三执行中，2026-10-06）**
  - **Flag 缺省照 P1，不照正文**：语义比对缺省关（P1 M4）；`long_horizon_stall_turns` /
    `long_horizon_budget_low_percent` / `long_horizon_drift_window` 都收 0 表示关（正文写的下限 1 作废）。
  - **一轮的动作从轨迹末尾那批工具调用取，按调用 id 去重**，不记下标：压缩会挪动下标，而它总留着最近的轮次；
    上一轮的模型调用没交回东西时末尾那批还是更早那一轮的，不能算两次。第一次被问时还没有上一轮，不记一条空的
    （否则第一个窗口只装了窗口减一轮的动作）。
  - **收尾轮不问控制器；控制器问完之后预算重判一次（不计轮次那一维）**：里程碑验收与语义比对都是伴生调用，花在
    这一轮的判定之后，与阶段一评审 H3 同一类——不重判的话，计划好的那次调用会在准入处被拒、停在 BUDGET 而没有
    回答。重判放不下就停，只放得下这一次就把这一轮改成收尾轮。
  - **策略的 WRAP_UP 在耗尽处置之后才看**：预算还够，所以不问人、hard_stop 也照收；收尾文字与预算耗尽的分开写；
    请求只对提出它的那个周期有效（那个周期没再开下一轮就结束时，被叫醒的下一个周期不能一上来就收尾）。
  - **UNVERIFIED 的裁决不是一次量度**：`derive_verdict` 在验收器失败时给每条补 UNCONFIRMED，照单记下会把进度
    清零、并在验收器坏掉时误报停滞。
  - **内核除了动作类型，还校验 move 是该方法自己那个枚举的成员、stop 是布尔、文字是字符串**（dataclass 不校验）；
    契约四条：类型、封闭枚举、文字不超过 2000 字、快照原样不动。契约的快照里有一千条强制验收条，基线因此只点名
    前十条。
  - **CLI**：`long-horizon` 必须与 `acceptance` 一起启用（三件套拒绝），并拿到同一个验收 Module 实例（另建一个就
    读不到这次 run 的 Goal 与 Rubric）；`trace show` 加「长程：」一节。
  - **两处等价变异**：「漂移重申」与「接着干」那一块的信任下限在端到端上被内核「取主轨迹溯源里最严的」盖住
    （注入的 Goal 块自带它的信任），用直接调控制器的用例钉住，不删：它是控制器自己的承诺，不该靠下游补。
    `_last_batch` 跳过署名块那一句是死代码（署名块的角色都是 user），删掉。
- **P5（阶段五执行中，2026-10-06）**
  - **B-22**：`KillAgent` 的授权改成沿 `parent_id` 往上走（O(深度)，带环保护），不再列出调用者整棵子树；
    注册表契约加一条「`children_of` 与 `parent_id` 描述同一棵树」，于是往上走的授权与沿 `children_of`
    往下走的级联用的是同一个定义。预算树的父指针在 spawn 处取自同一个 `parent_id`，不另建第二张图。
  - **D-46**：`Runtime(keep_finished_children=8)`（拒 bool、拒 < 1）。给一个父挂新子之前回收：只收结果已被
    派活等待取走的（宿主自己建的、等待超时的从不收）、整棵子树都是 zombie、没有留着的隔离副本、此刻没有
    等待指着它的；按进 zombie 的先后、最早的先收；被叫醒的不再算做完，直到再被取走一次。回收在 spawn 里做，
    而不是在子结束时另起任务：树只在这时长大，也就不必再管一个后台任务的生死。被收的 id 再被
    `SendMessage` / `QueryAgent` / `KillAgent` 寻址时说清它已被回收。树的规模上界由预算树给（每个子至少
    一轮）。
  - **拓扑槽隔离**：`AgentSlot.isolation`（追加在最后，`shared` / `worktree` / `snapshot`）是结构——它决定
    在工作区的哪一份上干活，不改角色的提示词、工具与模型。每个周期在共享工作区此刻的拷贝里干活，**周期正常
    结束时**合并（不是「槽回到 IDLE 时」：一直收到消息的 IDLE 槽永远等不到那一刻）；被取消或抛出的周期不
    合并，副本留着。冲突与合并失败在下一个周期开头以 `ENV_FACT` 署名块告诉它，信任取合并那一刻它的溯源，
    不混进别人的消息；隔离的事实按周期键重注。装了隔离槽的 run 不写回放文档（回放不记工作区）。
