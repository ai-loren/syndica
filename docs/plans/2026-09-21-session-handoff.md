# 会话交接：里程碑 1 做到哪、接着做什么

写于 2026-09-21，2026-09-23、2026-09-29 更新。本文给下一任（人或 Agent）接着干，不替代 spec /
计划 / 登记表。

**一句话现状：** 里程碑 1 已完成最终审计，计划 1–6、CLI、人机交互、人工确认与 L2 场景回放
均已落地。2026-09-29 用真实免费模型跑通体验验收并修掉暴露的问题，又补了 M2 之前的基础设施：
多平台模型轮换（spec §5.7 补充、决策 #89）。**下一步是单独规划里程碑 2（自举）**，计划写好先给
用户 review；不要重做 M1，也不要添加 `Runtime.start`。

**先读顺序**

1. 本文
2. [`AGENTS.md`](../../AGENTS.md)
3. [`docs/audits/2026-09-23-m1-final-audit.md`](../audits/2026-09-23-m1-final-audit.md)
4. [`docs/design/2026-09-03-architecture.md`](../design/2026-09-03-architecture.md) spec §15 / §4.6–4.8 / §17
5. [`docs/decisions/2026-09-08-m1-scope.md`](../decisions/2026-09-08-m1-scope.md)
6. [`docs/decisions/2026-09-14-deferred-items.md`](../decisions/2026-09-14-deferred-items.md)

---

## 0. 仓库与 git（动手前先核对）

| 项 | 值 |
|---|---|
| 仓 | `/Users/loren/Documents/loren/syndica`（GitHub 私有，Loren Zhang / zhangzihao） |
| 分支 | `main`（不要另开 worktree，除非用户明确要） |
| 语言 | Python 3.11+，`uv` |
| 写代码的约定 | 中文回复；注释只写**现在的硬约束**（不要写推理过程）；错误三件套；**不要**跑全仓 `make lint` / `make test` |
| 本仓验证 | `uv run pytest <受影响路径>`、`uv run ruff check` / `ruff format --check`、`uv run pyright`、`python scripts/check_deps.py` |
| git | 在 `main` 上开发；门禁与验证通过后 **commit 并 push**（用户 2026-09-29 定） |
| 提交身份 | `ai-loren <1158083128@qq.com>`（全局 git config 已设好，提交前 `git config user.email` 核对） |
| 提交时 | `git -c trace2.eventtarget= -c trace2.eventnesting= commit`（本机 git 会被 trace2 塞 Co-authored-by: Cursor） |
| 本机环境 | `uv sync --extra openai --extra dev` 建 `.venv`（Python 3.11） |
| 模型 key | 放在仓库外的 `~/.config/syndica/gemini.env`、`openrouter.env`（权限 600），用 `set -a; source <文件>; set +a` 导入。**不要**放进仓库或任何 Agent 操作的工作区：`Read` / `Grep` 读得到，key 会随请求发给模型提供方 |
| 模型 | **只用免费模型**：Gemini 免费档项目（没开计费）在前、OpenRouter `:free` 兜底，见 README「Retries and several platforms」与本地验证流程第 1、7 步 |

动手前以 `git log -5 --oneline` 与 `git status -sb` 为准。2026-09-29 起所有提交都已 push 到
`origin/main`，工作区干净、与 `origin/main` 一致即可续写。

下表是里程碑 1 收口时的提交记录，只留作历史：

| sha | 内容 |
|---|---|
| 本文同一提交 | **M1 最终审计**：六个挂单关闭、全量门禁、审计与交接 |
| `26f8a3c` | **M1 审计 D-59**：尾部未配对批次扫描收敛为一个纯函数 |
| `9604831` | **计划 6 Task 8**：L2 E2E、README、D-38 与计划收口 |
| `b9a652f` | **计划 6 Task 7**：Runtime host、chat 与人机事件泵 |
| `33f464e` | **计划 6 Task 6**：CLI 路由与惰性版本命令 |
| `07e2a4d` | **计划 6 Task 5**：L2 ScenarioRunner |
| `ff36133` | **计划 6 Task 4**：人工确认工具独占执行区间 |
| `47a576c` | **计划 6 Task 3**：AskUser / AskUserQuestion |
| `1397fa3` | **计划 6 Task 2**：安全 YAML 配置 |
| `d4d04bc` | **计划 6 Task 1**：有序 EventBus |
| `d60527e` | **计划 6 文件**：CLI、人机交互与 L2 场景回放 |
| `83635d4` | **计划 5 Task 8**：公开面、真实 Runtime E2E、登记与交接收口 |
| `4079d37` | **计划 5 Task 7**：PrefixDriftModule 与 provider 精确前缀单位 |
| `608cf7f` | **计划 5 Task 6**：RetryModule |
| `6533439` | **计划 5 Task 5**：SafetyModule 与两个安全 Flag |
| `f131a28` | **计划 5 Task 4**：per-Agent Module 装配与 AgentInfo.flags |
| `da996c0` | **计划 5 Task 3**：Runtime 级 Module 装配 |
| `8f75e0e` | **计划 5 Task 2**：FlagDeclaration 与 RuntimeModule 声明 |
| `a54da01` | **计划 5 Task 1**：复核并关闭 D-19～D-23 |
| `ade688f` | **计划 4 Task 8**：工作区公开面、依赖门禁、登记表 |
| `10b4dad` | **计划 4 Task 7**：Registry 快照、contracts 再导出、broker 总 timeout |
| `30fed58` | **计划 4 Task 6**：OpenAI 兼容 provider |
| `6b95895` | **计划 4 Task 5**：真实本地工具跑通拓扑 |
| `d0eee37` | **计划 4 Task 4**：Bash / BashOutput / KillBash |
| `6e36b86` | **计划 4 Task 3**：Glob / Grep |
| `4989d9f` | **计划 4 Task 2**：Read / Write / Edit。模型犯的错进 output；`__tool_command_args__ = ()` |
| `c13e103` | 交接改到计划 4 Task 2（已被本文取代） |
| `1fc689c` | **计划 4 Task 1**：Portal / LocalWorkspace。越界用解析后的真实路径，不用字符串前缀 |
| `dc4d690` | 交接改到计划 4 Task 1（已被本文取代） |
| `91b00d7` | **计划 4 文件**：最小本地执行 + OpenAI 兼容客户端 |
| `6c4b2fd` | 上一版交接（当时下一刀还是写计划 4；本文取代它） |
| `15436ed` | **计划 3 Task 8**：目录、拓扑、四个内置进入 `kernel.__all__`；一条拓扑端到端。Delegate 冷启动，不占 lazy 槽 |
| `38ee38f` | 上一版交接（当时下一刀还是 Task 8；本文取代它） |
| `d0e6828` | **计划 3 Task 7**：四个内置角色。Orchestrator 自己装 Delegate；提示词在 freeze 时用目录里当时的 description |
| `dd9eadb` | 上一版交接（当时下一刀还是 Task 7；本文取代它） |
| `b508023` | **计划 3 Task 6**：`Runtime.assemble` / `resolve` / `occupy` / `send_to_slot`。不是 `start()`。别名表与 `_slots` 分开 |
| `4b35c96` | 上一版交接（当时下一刀还是 Task 6；本文取代它） |
| `cb81e02` | **计划 3 Task 5**：`AgentSlot` / `Topology` 校验；还不接 Runtime。没有 `extra_plugins` |
| `505efff` | 上一版交接（当时下一刀还是 Task 5；本文取代它） |
| `73289a2` | **计划 3 Task 4**：`HostFacts` + freeze 时插值 + `AgentFactory`；`{cwd}` 即使宿主知道工作目录也拒绝 |
| `8cd418e` | 上一版交接（当时下一刀还是 Task 4；本文取代它） |
| `7071eda` | **计划 3 Task 3**：`AgentCatalog` 加载 / 覆盖可查询；同目录撞名整批拒绝；`get()` 也查不到未提交的名字 |
| `cb08264` | 上一版交接补一句：Task 3 改的是已有 `catalog.py` |
| `eb73b3b` | 更早一版交接（当时下一刀还是 Task 3；本文取代它） |
| `551b4d2` | **计划 3 Task 2**：`validate_tool_names` 认 Mapping；`catalog.py` 的 `register_tool` / `resolve_tool_names`；关掉 D-49、D-08 |
| `826ef59` | 交接（当时下一刀还是 Task 2） |
| `97c9edc` | **计划 3 Task 1**：`AgentManifest` + `parse_mapping`；未知键当场拒绝；尚未导出 |
| `d066f4c` | 更早一版交接（Task 1 还没写） |
| `dbd7fd9` | 写出计划 3 文件（声明式 Agent、拓扑、四个内置） |
| `77f33fc` | `__init__` 签名守卫 / orchestration 两档 / `attach_gate` 降私有 / `__all__` 扫描 fail-loud / trigger 计数 / 关闭 D-52 |
| `5f808eb` | 交接文档里 1020 那条 pytest 命令写完整 |
| `a15c31b` | 交接文档补全量 1710 与 M-7/M-8 |
| `0269add` | 记下里程碑 1 做到哪、还剩什么（本文初版） |
| `d4961c1` | 入口校验 / CAS 并发契约 / 关机收据 / lost-zombie 分档 / D-56..D-60 |
| `504f488` | `AgentInfo.created_at` |
| `df5db9f` | AGENTS.md 四条横切审计教训 |
| `cba3e3c` | abort 在模型产出后、工具执行前兑现（F-1） |
| `c7d2751` | Task 16 关掉六条移交 + 登记新缺口 |
| `449dccc` | 公开面端到端给 spec §15 判据取证 |
| `312cd59` | `_RUNTIME_ONLY` 触发点扫描守卫 |
| `96c7cdd` | 公开面收口（承诺面 + 扣留面） |

更早的计划 2 Task 1–15 已在 `origin/main` 一侧。

计划 1–6 与 M1 最终审计均已收口。`assemble` 不是 `start()`，后续也不要加
`Runtime.start`。

---

## 1. 这一轮的目标是什么

用户原话可以收成三句（时间越后越具体）：

1. **产品目标（一直有效）**：做 Syndica——框架优先、范围 ≥ maso、Python、单进程 asyncio 内核 + 可插拔存储、完整产品形态的 v1.0。
2. **里程碑目标**：把里程碑 1 做完（子计划 2–6），判据见 spec §15。
3. **本会话收窄后的目标**：「把你认为 multi-agent 没做完的部分做完」+ 全内核多维自审（功能 / 语法 / 稳定性 / 可扩展 / 鲁棒）+ 合理优化。token 见底时改成「先收尾 + 写详细交接」。

裁决过的节奏（不可再解释）：[m1-scope §5–6](../decisions/2026-09-08-m1-scope.md)

- **全程与计划 1 相同的全量严格**：实现 → 自我变异证伪 → 独立评审（评审自创变异）→ 修复 → 收口。
- **一个一个来**：写计划 2 → 执行到收口 → 写计划 3 → 执行到收口 → …。不要穿插写计划 4。

---

## 2. 里程碑 1 对照表（spec §15）

spec §15 里程碑 1 判据原文：*Runtime 能创建多个 Agent、互发消息、各自跑通「LLM + 工具」循环、可终止与恢复。含声明式 Agent 定义（§4.6）、拓扑装配（§4.7）、Tool 系统与 LLM 抽象（§5.6 / §5.7），以及最小本地执行能力（文件读写 + 命令执行）。*

| §15 要求 | 状态 | 落在哪 |
|---|---|---|
| Runtime 创建多个 Agent、互发消息 | **已做** | 计划 2；`tests/integration/test_multi_agent_lifecycle.py` |
| 各自跑通 LLM + 工具循环 | **已做（内核侧）** | 计划 1 turn 循环 + ScriptedLLM；真 LLM 在计划 4 |
| 可终止与恢复 | **已做** | `kill` / `despawn` / `pause` / `resume` / `abort`；ZOMBIE 可寻址 |
| 声明式 Agent 定义（§4.6） | **已做** | 计划 3 Task 1–4、Task 7、Task 8（`15436ed` 起宿主从 `syndica.kernel` 拿到清单与内置） |
| 拓扑装配（§4.7） | **已做（控制面）** | 计划 3 Task 5–6 + `tests/integration/test_topology_lifecycle.py`。真文件工具仍在计划 4 |
| Tool 系统与 LLM 抽象 | **内核抽象已做** | Tool / ToolExecutor / DangerJudge / ScriptedLLM；OpenAI 兼容 provider 在计划 4 |
| 最小本地执行（读写文件 + 跑命令） | **已做** | 计划 4：八个本地工具、真实拓扑集成测试、OpenAI 兼容 provider |

**结论：里程碑 1 已收口。** 计划 1–6、CLI / 人工确认 / L2 场景层与最终审计均已完成。

子计划边界（不要重新解释）：

| 子计划 | 范围 | 状态 |
|---|---|---|
| **1** | 单 Agent turn 循环 | **代码收口**，已在更早的历史上 |
| **2** | 事件循环、生命周期、等待、危险判定、工具并发、插件、编排工具、公开面 | **代码收口**（含审计修复 + Task 16 残留） |
| **3** | 声明式清单、目录、拓扑、四个内置角色 | **代码收口** [`2026-09-21-m1p3-declarative-agents.md`](./2026-09-21-m1p3-declarative-agents.md)；Task 1 = `97c9edc`，Task 2 = `551b4d2`，Task 3 = `7071eda`，Task 4 = `73289a2`，Task 5 = `cb81e02`，Task 6 = `b508023`，Task 7 = `d0e6828`，Task 8 = `15436ed` |
| **4** | Workspace/Portal + 文件与执行工具 + OpenAI 兼容 provider | **代码收口**；Task 1–9 全部完成 |
| **5** | Runtime Module + Flag + 重试 Module + 前缀漂移检测 | **代码收口** [`2026-09-22-m1p5-runtime-modules.md`](./2026-09-22-m1p5-runtime-modules.md)；Task 1–8 全部完成 |
| **6** | CLI + L2 场景回放 + AskUser | **代码收口** [`2026-09-23-m1p6-cli-human-e2e.md`](./2026-09-23-m1p6-cli-human-e2e.md) |
| **M1 收口审计** | spec §15 取证 + 遗留项清空 + 全量门禁 | **已完成**；见 `docs/audits/2026-09-23-m1-final-audit.md` |

---

## 3. 已经做完的

### 3.1 计划 1（单 Agent turn）

已合入。产物：[`docs/plans/2026-09-05-m1p1-single-agent-turn.md`](./2026-09-05-m1p1-single-agent-turn.md)。内核 turn 循环、Context、Hook、ToolExecutor 初版、危险判定骨架。不要回头重做。

### 3.2 计划 2（内核控制面）——代码已收口

计划文件：[`docs/plans/2026-09-08-m1p2-control-plane.md`](./2026-09-08-m1p2-control-plane.md)。Task 1–16 均已落地并 commit。

| 阶段 | 交付 |
|---|---|
| A / Task 1–7 | 事件循环 IDLE↔RUNNING；`TurnGate`；`kill` / `despawn` / `pause` / `resume` / `abort`；级联拆除；`ensure_active` + `on_missing_queue` |
| B / Task 8–9 | `Waiter` 密封类型、`WaitResult` 四态、`wait_for`、`WAITING` |
| C / Task 10–12 | 危险判定 + 阻断/放行；工具并发分段 |
| D / Task 13–15 | `agent.register_hook`、`Runtime(plugins=)`；编排五件套 `Delegate` / `Fork` / `SendMessage` / `QueryAgent` / `KillAgent` + `orchestration_tools` |
| Task 16 | 公开面收口；`tests/integration/test_multi_agent_lifecycle.py` 给 §15 四条「多 Agent 能跑」判据取证 |

公开面判据（记住这一句）：**一个名字进 `kernel.__all__`，当且仅当宿主拿不到它就用不了某个已经公开的入口。** 加名字可逆，去掉名字是破坏性变更。清单与扣留在 `tests/kernel/test_public_api.py`。

今天的 Runtime 公开方法（`_RUNTIME_SURFACE`，Task 6 已加上 `assemble` / `resolve` / `occupy` / `send_to_slot`）：

`abort`, `agent_factory`, `agent_names`, `assemble`, `broker`, `despawn`, `ensure_active`, `events`, `find`, `hooks`, `kill`, `occupy`, `pause`, `plugins`, `registry`, `resolve`, `resume`, `send`, `send_to_slot`, `shutdown`, `spawn`, `wait`, `wait_for`

构造器（`_RUNTIME_INIT`，计划 3 **不许**往这里加 `catalog=` / `topology=`）：

`registry`, `broker`, `plugins`, `agents`, `orchestration` —— 全是 keyword-only、全有默认值。

`send` 第二参实现叫 `content`（spec 叫 `message`）。**不要改名**（M-6）。`find` **绝不激活**（计划 2 承诺，计划 3 必须沿用）。

### 3.3 横切审计（F-1…F-11）——「现在修」的已落地

独立审计（对照当时 `HEAD=c7d2751`）结论：内核稳，可以往上堆计划 3–6。本会话已修完审计要求「现在修」的那些：

| 编号 | 做了什么 | 关键落点 |
|---|---|---|
| **F-1** | spec §5.1 第 9 步：LLM 产出之后、工具执行之前再兑现 abort | `TurnGate.check_abort`（**消费**中止位、不吃邮箱）；`BaseAgent._run_turns`；未跑的调用走 `ToolExecutor.append_unexecuted` |
| **F-2** | `AgentInfo.created_at`（构造时戳，受保护） | `agent_types.py`；`UPDATE_PROTECTED_FIELDS` |
| **F-3** | CAS 并发契约：n=3，恰好一个赢家 | `contracts/registry.py` |
| **F-4 + F-11** | 入口校验铺开 | `EventBus.capacity`（先拒 bool）；`Runtime(registry=/broker=)` 拒类对象；`spawn(metadata=)` 先 deepcopy 再 register |
| **F-5** | `shutdown` 收据：拆除照做，`RUNTIME_SHUTDOWN` 只报一次 | `_shutdown_reported`；第二次不得见 `_closed` 就 return |
| **F-7** | lost-zombie 分档 | `get()` 是 None → DEBUG；CAS 被拒且条目还在 → ERROR |
| **F-6** | `LLM.create` 必须兑现 `config.timeout` | **只补了契约正文**；可执行用例归计划 4（D-56） |
| **F-8** | `update` 写入侧快照 | **只登记** D-57，归计划 4 |
| **F-9** | 没有 `runtime_start` | **只登记** D-58；**禁止加 `async def start()`** |
| **F-10** | 未配对批次扫描重复 | M1 最终审计：`26f8a3c` 抽为 `context._trailing_unpaired_batch`，结构守卫与连续尾块用例覆盖 |

D-47 已拆成两半：墙钟 clamp → 计划 5（与 D-18 同一条 Flag）；被等 Agent 卡在 `LLM.create` → 计划 4 / D-56。

验证过：

- 受影响子集：`uv run pytest tests/kernel/test_{agent_turn,agent_types,control,danger,events,llm,runtime,public_api,tool_executor}.py tests/contracts/test_memory_registry.py -q` → **1020 passed**
- 修复轮在 `d4961c1` 上的全量门禁：`uv run pytest -q` → **1710 passed**；`ruff` / `pyright` 0 errors / `scripts/check_deps.py` 空。审计探针当时在 `/tmp/synaudit/`，多半已经没了。

### 3.4 Task 16 独立评审残留（I-2 / I-3 / I-4 + M-1 / M-2）——已落地，commit `77f33fc`

独立评审当时是 **CHANGES_REQUESTED**（无 Critical，4 Important + 6 Minor）。I-1（`created_at`）由横切审计修掉。其余：

| 项 | 做了什么 | 落点 |
|---|---|---|
| **I-2** | `Runtime.__init__` / `BaseAgent.__init__` 用 `inspect.signature` 钉参数名 + kind + 有无默认值 | `test_public_api.py` `_RUNTIME_INIT` / `_BASE_AGENT_INIT` |
| **I-3** | 公开面按 `orchestration=False/True` 两档参数化，必须相等 | `test_the_runtime_surface_is_exactly_this[False/True]` |
| **I-4** | `attach_gate` → `_attach_gate`；构造器去掉 `gate=`；默认 `NullTurnGate()`；`Runtime.spawn` 调私有方法 | `agent.py`、`runtime.py`；测试 `_agent(..., gate=)` 构造后再 `_attach_gate` |
| **D-52** | 已关 | `deferred-items.md` 已关闭表 |
| **M-1** | `__all__` 扫描遇 Starred / AugAssign / `.append` / 非 List / 非字面量 elt 直接 `pytest.fail` | `_declared_exports` |
| **M-2** | 包里每一次 `....trigger(` 调用必须被匹配器认成 `Junction.X` | `test_the_trigger_matcher_sees_every_trigger_call` |

验证：`uv run pytest tests/kernel/test_{public_api,agent_turn,plugins,runtime}.py tests/integration/test_multi_agent_lifecycle.py -q` → **224 + 237 passed**（两批分别跑）。

### 3.5 计划 3 文件本身 —— 已写、已自审、已 commit（`dbd7fd9`）

文件：[`docs/plans/2026-09-21-m1p3-declarative-agents.md`](./2026-09-21-m1p3-declarative-agents.md)。含 Goal / Architecture / Global Constraints / **9 条设计裁决** / 8 个任务（必写用例名 + 变异点）/ 自我审查 / commit 粒度。

### 3.6 计划 3 Task 1 —— 已落地，commit `97c9edc`

**不要重做。** 门禁：`uv run pytest tests/kernel/test_manifest.py tests/kernel/test_public_api.py -q` → **31 passed**；ruff / pyright 0 errors。

| 文件 | 角色 |
|---|---|
| `src/syndica/kernel/manifest.py` | `AgentManifest`、`ManifestSource`、`parse_mapping` |
| `tests/kernel/test_manifest.py` | 9 条必写用例 + 预算 / 字面提示词 / 路径逃逸补强 |
| `tests/kernel/test_public_api.py` `_WITHHELD` | 扣留 `AgentManifest`、`ManifestSource`、`parse_mapping` |
| `src/syndica/kernel/__init__.py` | 本任务没动；Task 8（`15436ed`）已导出 |

执行时拍板、下一任不要改回去的细节：

1. **字段顺序（公开 API）**：`name`, `description`, `system_prompt`, `tools`, `subagents`, `model`, `extends`, `skills`, `budget`, `artifact_kinds`, `impl`, `source`。`ManifestSource` 是 `kind`, `location`, `origin`。
2. **`origin` 是额外字段。** 计划原文只写了 kind + location；提示词何时当文件读必须有一个目录快照，所以加了 `origin: str | None`。纯 Mapping 不要设 origin，字符串就是提示词。不靠 `.md` 后缀、也不靠换行猜测。
3. **`parse_mapping(data: object, *, source)`**：签名收 `object`，运行时先拒 str/bytes 再拒非 Mapping（与 `_not_a_role_table` 同型）。keyword-only 的 `source` 必填。
4. **未知键一次列出全部**（集合，基数 ≥ 3：`zeta` / `class` / `alpha`）。`class:` 是未知键，判别字段是 `impl`。
5. **`tools` 先拒 str/bytes，再拒 Mapping，再收 Sequence**。tuple 也收，存成 tuple。映射的 next_action 点名 AgentCatalog / 工具目录。
6. **`budget` 只允许键 `turns`。** `{}` 拒绝（含糊：补 turns 还是省略 budget）。`turns: null` → 不限。bool 先拒（YAML `yes`）。存成 `MappingProxyType`，宿主改原 dict 不泄漏。
7. **`impl` 缺省/None = 声明式**；`type` 必须是 `BaseAgent` 子类；`str` 原样保存、**本任务不 import**。
8. **路径安全：** 相对路径含 `..` 或绝对路径不当文件读；`Path.relative_to(origin)` 再挡一层。没有 origin 时，就算字符串恰好是磁盘上一个文件也当字面量。
9. **变异（已杀死）：** 删未知键检查；只报第一个未知键；丢掉 Mapping 守卫让 tools dict 走别的错；`budget` 额外键放行；没有 origin 仍读已存在的文件。`..` 检查相对 `relative_to` 是纵深，单删 `.. in parts` 会被第二层挡住（存活，可接受）。
10. **kernel 零 PyYAML。** `manifest.py` 没有 HostFacts；宿主事实与插值在 `catalog.py`（§3.9）。

`_WITHHELD` 现含 Task 1 三个名字 + Task 2 的 `register_tool` / `resolve_tool_names` + Task 3 的 `AgentCatalog` / `OverrideRecord` + Task 4 的 `HostFacts`，Task 8 再挪进 `kernel.__all__`。不要在 Task 5–7 里提前导出。

### 3.7 计划 3 Task 2 —— 已落地，commit `551b4d2`

**不要重做。** 门禁：`uv run pytest tests/kernel/test_tool.py tests/kernel/test_catalog.py tests/kernel/test_public_api.py tests/kernel/test_tool_executor.py tests/kernel/test_agent_turn.py -q` → **475 passed**；ruff / pyright 0 errors。独立评审先 CHANGES_REQUESTED，补了生插分叉在 resolve 出口的用例后再关 D-08。

| 文件 | 角色 |
|---|---|
| `src/syndica/kernel/tool.py` | `validate_tool_names(tools: object)`：先拒 str/bytes 与非 Mapping |
| `src/syndica/kernel/catalog.py` | `register_tool`、`resolve_tool_names`；字面 `__all__` |
| `tests/kernel/test_tool.py` | 列表三件套 |
| `tests/kernel/test_catalog.py` | 顺序 / 缺名 / 登记拒分叉 / 浅副本 / 生插分叉 |
| `tests/kernel/test_public_api.py` `_WITHHELD` | 扣留 `register_tool`、`resolve_tool_names` |
| `docs/decisions/2026-09-14-deferred-items.md` | D-49、D-08 进「已关闭」 |
| `src/syndica/kernel/__init__.py` | 本任务没动；Task 8（`15436ed`）已导出 |

执行时拍板、下一任不要改回去的细节：

1. **签名收 `object`。** `validate_tool_names(tools: object)`，与 `parse_mapping` 同型，避免 pyright 把 `isinstance(..., Mapping)` 判成多余。
2. **`register_tool` 键永远是 `tool.name`。** 三轴不一致就拒，表保持原样。不要按 `schema['name']` 改键收下。
3. **`table[k]=tool` 物理上仍能写**，不是正式入口。`resolve_tool_names` 对产出立刻 `validate_tool_names`，生插分叉在解析出口被拦。证据是 `test_resolve_rejects_a_catalog_entry_with_disagreeing_identity_axes`（不走 `register_tool`）。
4. **缺名一次列出全部。** 目录 A,C,E / 清单 B,C,D：`received` 与 `next_action` 点名 B 与 D，**不点名 C**（C 在 `expected` 的 wanted 列表里可以出现）。
5. **产出 dict 键序 = 清单顺序**，不是目录迭代顺序。浅副本：改返回值或改目录互不影响；Tool 实例可以共享。
6. **`names` 先拒 str/bytes**（`"A"` 是 Sequence，会拆成字母）。
7. **变异（已杀死，各只红一条）：** drop-mapping-guard；first-missing-only；resolve-catalog-order；register-renames-to-schema-name；return-the-catalog-object（红顺序 + 浅副本两条）；drop-validate-on-resolve（只红生插那条）。
8. **Task 3 会改同一份 `catalog.py` / `test_catalog.py`。** 计划原文写「Create test_catalog.py」已经过时：文件在。加载 / 覆盖加在现有模块上，保留 Task 2 的五个必写用例名。

### 3.8 计划 3 Task 3 —— 已落地，commit `7071eda`

**不要重做。** 门禁：`uv run pytest tests/kernel/test_catalog.py tests/kernel/test_public_api.py tests/kernel/test_manifest.py -q` → **49 passed**；ruff / pyright 0 errors；`scripts/check_deps.py` 空。独立评审先 CHANGES_REQUESTED：撞名失败后只钉 `names()` 挡不住「Unique 写进 `_entries`、不改 `_order`」的漏写，补了 `get('Unique')` / `get('Worker')` 拒绝后再过。

| 文件 | 角色 |
|---|---|
| `src/syndica/kernel/catalog.py` | 同一模块里工具目录 + `AgentCatalog` / `OverrideRecord`；字面 `__all__` 含四名 |
| `tests/kernel/test_catalog.py` | 保留 Task 2 五个用例 + Task 3 五个必写 + 先见顺序 / 文件 origin / register_class docstring |
| `tests/kernel/test_public_api.py` `_WITHHELD` | 扣留 `AgentCatalog`、`OverrideRecord` |
| `src/syndica/kernel/__init__.py` | 本任务没动；Task 8（`15436ed`）已导出 |

执行时拍板、下一任不要改回去的细节：

1. **覆盖记 `OverrideRecord`，`overrides(name)` 从旧到新。** `displaced_at_index` 是该名字第几次被挤掉（从 0 起），**不是** `names()` 下标。从未见过或从未覆盖 → `()`。
2. **`names()` 是第一次见到该名的顺序**，覆盖不把名字挪到末尾，也**不是**排序。证据：`test_names_are_first_seen_not_sorted`（Zeta, Alpha, Mu）。
3. **同一次 `load_json_directory` 内部撞名不是覆盖。** 先解析整批，再 `_reject_duplicate_names_in_one_directory`，再 `_put`。撞了 → ConfigError，**这一批一个都不写**。目录里先在的条目不动。
4. **`names()` 不够钉「整批不写入」。** 必须 `get('Unique')` 与 `get('Worker')` 都 `ConfigError`（received 点名缺失的那个名字），`get('Alpha')` 仍是先在的。只污染 `_entries`、不改 `_order` 的突变体能让 19 个测试全绿。
5. **只收下一层 `*.json`。** `suffix == '.json'`，不递归，不认 `notes.txt` / `worker.json.bak` / 子目录 `hidden.json`。`get('Bak')` / `get('Scout')` / `get('Hidden')` 的 received 必须点名缺失。
6. **JSON 文件 `origin` = 父目录**；目录加载 `origin` = 目录路径，`kind=json_directory`。`system_prompt` 相对路径靠 origin。
7. **`register_class` 不实例化。** description = 类 docstring 去掉首尾空白后的第一行；空白 docstring → ConfigError。
8. **`register_builtin(data, *, location)`** 本任务就提供了（kind=builtin）。`get()` 找不到时 next_action 点名 `load_mapping` / `load_json_file` / `load_json_directory` / `register_class` / `register_builtin`。
9. **`load_mapping` / `register_class` 的 `source`、`register_builtin` 的 `location` 签名收 `object`**，与 `parse_mapping` 同型，避免 pyright 把 `isinstance` 判成多余。None → 默认 `mapping` / `<mapping>`。
10. **`get(name)` 永不返回 None**。没有这个名字就 ConfigError，received 列出 `names()` 现有名单。
11. **本任务（Task 3）只交 `get(name) -> AgentManifest`。** freeze / 插值在 Task 4（§3.9），不要在加载层做。
12. **变异（8/8 已杀死）：** 不记 OverrideRecord；覆盖把名字挪到末尾；只留最后一次覆盖；跳过同目录撞名；`rglob('*.json')`；`names()` 排序；撞名后把 Unique 漏进 `_entries`；`'.json' in name` 当后缀。本机 bash 没有 `declare -A`，跑手是 `/tmp/syndica-t3-mutate.py`（不入库），sha256 还原。

### 3.9 计划 3 Task 4 —— 已落地，commit `73289a2`

**不要重做。** 门禁：`uv run pytest tests/kernel/test_catalog.py tests/kernel/test_prompt_interpolation.py tests/kernel/test_public_api.py -q` → **57 passed**；ruff / pyright 0 errors。变异 12/12 已杀死（跑手 `/tmp/syndica-t4-mutate.py`，不入库）。独立评审先 CHANGES_REQUESTED，补断言后已收口。

| 文件 | 角色 |
|---|---|
| `src/syndica/kernel/catalog.py` | `HostFacts`、`AgentCatalog.freeze`、`_interpolate_prompt`（模块私有） |
| `tests/kernel/test_prompt_interpolation.py` | 插值必写用例 + `{cwd}` 带子类 host + 格式规格 / 未闭合括号 |
| `tests/kernel/test_catalog.py` | freeze / extends / 工厂签名 / 工具副本 / 缺模型与 subagents |
| `tests/kernel/test_public_api.py` `_WITHHELD` | 扣留 `HostFacts` |
| `src/syndica/kernel/__init__.py` | 本任务没动；Task 8（`15436ed`）已导出 |
| `src/syndica/kernel/manifest.py` | **没动**（插值在 catalog 层） |

执行时拍板、下一任不要改回去的细节：

1. **`HostFacts` 只有 `os` / `platform` / `shell` 三个非空白 str。** 内核不读 `os.environ` / `os.getcwd`。`freeze(..., host=)` 必须是 `HostFacts` 实例；带齐三个字段的 `SimpleNamespace` 也拒。
2. **插值白名单只有 `{os}` `{platform}` `{shell}`。** `{cwd}` 即使 `HostFacts` 子类带着 `cwd=` 也拒绝；next_action 必须是「把工作目录写进历史块，不要写进 System Prompt」。`{os:posix}` 当未知占位。`{{` / `}}` 变成字面 `{` / `}`。
3. **插值发生在 freeze，不在 factory 调用时。** `_factory_for` 在闭包之前就 `_interpolate_prompt`。空 prompt 可以不传 host；含占位的 prompt 不传 host → freeze 当场 ConfigError。
4. **`freeze(tools, models, host=None) -> MappingProxyType`。** 键序 = `names()` 第一次见到的顺序，不是排序。结果过 `validate_agent_factories`。
5. **工厂签名必须是 `(hooks, events)`。** 类式：`cls(options, hooks=hooks, events=events)`。`impl` 字符串是 `pkg.mod:Class`，import 失败三件套。
6. **extends 只能指向已登记的类式角色。** 声明式 extends 声明式、环、未知名 → ConfigError。子角色的 prompt 是自己清单的，父 prompt 不继承。工具并集：子清单顺序在前，再补父有、子没有的名字。
7. **工厂捕获 `dict(resolve_tool_names(...))`。** freeze 之后再 `register_tool` 换同名实例，spawn 仍是 freeze 时那一个。
8. **缺省模型名 `default`。** 缺的模型名一次列全（测试钉 3 个独立缺失名）。subagents 同理（测试钉 3 个缺失名，已在的 Gamma 不进「没有」那一半）。
9. **`budget` 缺省 → `max_turns=50`；`turns: null` → `None`。** 缺省那条必须显式断言 `== 50`：`_max_turns` 若在 `budget is None` 时返回 `None`，会覆盖 dataclass 缺省，旧用例全绿。
10. **`_interpolate_prompt` 不进 `catalog.__all__`。** `HostFacts` 进 catalog `__all__` 且进 `_WITHHELD`。

### 3.10 计划 3 Task 5 —— 已落地，commit `cb81e02`

**不要重做。** 门禁：`uv run pytest -W error tests/kernel/test_topology.py tests/kernel/test_public_api.py -q` → **63 passed**；ruff / pyright 0 errors；`scripts/check_deps.py` 空。自我变异 35/35 已杀死（跑手 `/tmp/syndica-t5-mutate.py`，不入库）。独立评审 CHANGES_REQUESTED（`exit_mode='ZOMBIE'`、整浮点 `turns=40.0`、可变 metadata 能活），补断言后再杀 7 条（跑手 `/tmp/syndica-t5-mutate2.py`）。

| 文件 | 角色 |
|---|---|
| `src/syndica/kernel/topology.py` | `AgentSlot`、`Topology.validate` |
| `tests/kernel/test_topology.py` | 字段顺序、禁则、目录缺失名一次列全 |
| `tests/kernel/test_public_api.py` `_WITHHELD` | 扣留 `AgentSlot`、`Topology` |
| `src/syndica/kernel/__init__.py` | 本任务没动；Task 8（`15436ed`）已导出 |
| `src/syndica/kernel/runtime.py` | **没动**（assemble 是 Task 6） |

执行时拍板、下一任不要改回去的细节：

1. **没有 `extra_plugins`，也没有自由 `options`。** 字段顺序：`name`, `agent`, `is_root`, `cardinality`, `key_field`, `boot`, `exit_mode`, `extra_tools`, `budget`, `metadata`。插件仍只在 `Runtime(plugins=)`。
2. **`keyed` 必须有非空白 `key_field`；singleton / on-demand 的 `key_field` 必须是 `None`。** 空字符串也不是 None。`keyed+eager` 与 `on-demand+eager` 各自能单独变红；singleton+eager 合法。
3. **`exit_mode` 必须是 `ExitMode` 成员。** 字符串 `'zombie'` 和成员名 `'ZOMBIE'` 都拒。`IDLE` / `EXIT` 枚举值合法。
4. **槽名唯一，一次列出每个重名和次数（`Alpha×2`）。** root 至多一个；三个 root 的名字都要出现在 received。零个 root、空拓扑合法。两个槽可以引用同一个角色名。
5. **未知角色名不在构造时查。** `Topology.validate(catalog)` 一次列出全部缺失名，同一名字只出现一次；已在目录里的名字不进「缺少」那一半。`catalog` 必须是 `AgentCatalog`，鸭子类型即使有 `names()` / `get()` 也拒。
6. **`metadata` 深拷进 `MappingProxyType`，拒绝键 `artifact_kinds`。** 浅拷贝、返回普通 dict、非 str 键只报第一个，都要能变红。`budget` 与清单同一约束：只有 `turns`；`0` 合法；`40.0` 与 bool 非法；结果也是 `MappingProxyType`。
7. **`extra_tools` / `slots` 先拒 str（否则拆成字符），再收成 tuple。** 重复工具名保留，不去重。
8. **本任务没有 `assemble` / `start`。** `validate` 成功时返回 `None`。

### 3.11 计划 3 Task 6 —— 已落地，commit `b508023`

**不要重做。** 门禁：`uv run pytest -W error tests/kernel/test_assemble.py tests/kernel/test_public_api.py -q` → **33 passed**（assemble 18 + 公开面）；ruff / pyright 0 errors。自我变异：跑手 `/tmp/syndica-t6-mutate.py` 17/17 已杀死，`/tmp/syndica-t6-mutate2.py` 再杀 3 条（eager 半截失败后仍锁、相等预算保留、无限角色可收紧）。独立评审自创 12 条，11 条杀死；删掉 `occupy` 里的早返回仍绿，因为 `_spawn_slot` 还有同一套复用——三处一并去掉会被幂等用例杀死。评审 **APPROVE**。`runtime.py` 还原后 sha256 仍是 `6c16c3cdcdac2bae2470d12e1bd4f8b2cf37495de937d6a0ed84d18e7f8e696f`。

| 文件 | 角色 |
|---|---|
| `src/syndica/kernel/runtime.py` | `assemble` / `resolve` / `occupy` / `send_to_slot`；别名表与 `_slots` 分开 |
| `tests/kernel/test_assemble.py` | 计划里的必写用例 + 预算收紧 / 别名冲突 / 半截 eager 失败仍锁 |
| `tests/kernel/test_public_api.py` `_RUNTIME_SURFACE` | 加上四个名字。`_RUNTIME_INIT` **没改** |
| `src/syndica/kernel/__init__.py` | 本任务没动；Task 8（`15436ed`）已把 `AgentSlot` / `Topology` 挪进承诺面 |

执行时拍板、下一任不要改回去的细节：

1. **`assemble` 不是 `start()`，也不订 `Junction.RUNTIME_START`。** 返回 `None`。构造器参数不变。没 assemble 的 Runtime 与计划 2 一样。
2. **一个 Runtime 只 assemble 一次。** 第二次是 `ContractViolation`（expected「一个 Runtime 只 assemble 一次」），并且发生在 `validate` / `freeze` **之前**，所以第二次不会被当成校验错误。
3. **校验、freeze、非法 tools 失败时不置 `_assembled`。** 调用方改完可以再 assemble。eager spawn 中途失败则已经置位：成功的 eager 留在注册表里，再 assemble 是契约违反，不会把同一个 eager 再 spawn 一次。
4. **别名不是 `_slots`。** `_slots` 是已 spawn Agent 的生命周期。拓扑用 `_topology_bindings` / `_topology_aliases` / `_topology_alias_owners` / `_topology_tools`。别名是「槽名 → 当时的 agent_id」快照。
5. **别名规则：** singleton = 槽名；keyed = `槽名:键`（缺键或空白键即使 `spawn=False` 也 ConfigError，文案含 `keyed`）；on-demand 没有别名，`resolve` 永远 `None`，`occupy` 每次新建。
6. **跨槽算出同一个别名直接拒绝。** `occupy` 不能把别人的 agent_id 当自己的幂等命中。`_owned_alias`：同一槽复用；不同槽 ConfigError，received 含「别名已被占用」。检查在 spawn 之前，不泄漏半个 Agent。
7. **`send_to_slot` 不重载 `send` 的第一个参数。** on-demand 与 `boot='manual'` 在 resolve 之前就 ConfigError，next_action 含「用 occupy」。lazy / keyed / eager singleton 走 `resolve(spawn=True)` 再 `send(agent_id, ...)`。
8. **`find` 只查别名表再 `registry.get`，绝不 spawn。** 没 assemble 时别名表是空的。
9. **catalog 工厂覆盖 `Runtime(agents=)` 同名项，并打 INFO：** 原文是「catalog 覆盖了 Runtime(agents=) 里同名工厂」加名字。没有第二条 OverrideRecord。直接 `spawn(角色名)` / `agent_factory(name)` **不**吃槽的 `extra_tools`；增强只包在 `_enhanced_factory` 里，给 `_spawn_slot` 用。
10. **`extra_tools` 只并上，不替换。** 名字冲突 ConfigError，文案含「增强不得替换」。冲突发生在工厂里、`registry.register` 之前。并集走 `install_runtime_tools`，它换掉 Agent 自己的 options，**不改** freeze 闭包里那份共享 tools dict。
11. **槽 `budget` 只收紧 `max_turns`。** 相等保留；角色无限（清单 `turns: null`）可以被槽收成有限；槽 `turns: null` 或更大的数去放宽有限角色 → ConfigError，文案含「只收紧」，且一个 Agent 都不注册。用 `dataclasses.replace`，不要 `options.tools.update` 或原地改 `max_turns`。角色清单本身不变。
12. **`artifact_kinds` 在 assemble 时从清单快照进 `_TopologyBinding`。** occupy 的 metadata 是槽 metadata 的深拷贝再并上这个 tuple；之后改调用方传入的 dict 不影响已占用实例。两个 on-demand 不共享同一份 metadata dict。
13. **`runtime.py` 顶层不 import `catalog` / `topology`。** 二者在 `assemble` 里懒导入，否则和 `catalog` → `runtime.AgentFactory` 成环。`Tool` 只在 `TYPE_CHECKING`；`cast` 的类型参数必须加引号，否则运行时 NameError，不加引号又会被 pyright 判成未使用导入。
14. **签名挡不住绕过类型检查的调用方。** `assemble` 先把参数收成 `object` 再 `isinstance`。鸭子 Topology / AgentCatalog、str/bytes 形态的 tools 都拒。
15. **没有跨 `await spawn` 的锁。** 同一别名并发 `occupy` 可能双 spawn。测试只覆盖顺序调用。不要加一把跨过 spawn 的锁：`AGENT_SPAWN` 钩子里再 occupy 会死锁。
16. **同一个角色的多个实例共享 freeze 时那一个 ScriptedLLM。** 一个 Agent 发一条消息就够用；第二个实例会把脚本耗尽。eager assemble 本身不消耗脚本。

### 3.12 计划 3 Task 7 —— 已落地，commit `d0e6828`

**不要重做。** 门禁：`uv run pytest -W error tests/kernel/test_builtin_agents.py tests/kernel/test_public_api.py -q` → **23 passed**；同一次还跑过 `test_catalog.py` + `test_runtime.py`（290 passed，其中 builtin 当时 1 红已修）。ruff / pyright 0 errors。`python scripts/check_deps.py` 空。自我变异 8/8 杀死。独立评审自创 13 条，10 条杀死；补上 Orchestrator description、三份声明式 `system_prompt` 原文、以及非 Runtime 调用 `install_own_tools` 必须 ConfigError 之后，那 3 条也杀死。

| 文件 | 角色 |
|---|---|
| `src/syndica/kernel/agents/planner.json` `explorer.json` `worker.json` | 声明式。Planner/Explorer 工具 `Read, Grep, Glob`。Worker 工具 `Read, Write, Edit, Bash` |
| `src/syndica/kernel/agents/orchestrator.py` | 类式。`compose_system_prompt` + `install_own_tools` |
| `src/syndica/kernel/agents/__init__.py` | `register_builtins`：先三份 JSON，再 Orchestrator |
| `src/syndica/kernel/catalog.py` | `_role_prompt`：类上有 `compose_system_prompt` 才在 freeze 时拼 |
| `src/syndica/kernel/agent.py` | `install_own_tools` 缺省空。`_BASE_AGENT_SURFACE` 已加上这个名字 |
| `src/syndica/kernel/runtime.py` | `spawn` 在 `_install_orchestration` 之后调用 `install_own_tools` |
| `tests/kernel/test_builtin_agents.py` | 名字、工具元组、描述、覆盖、重复登记、旗子两档、非 Runtime |
| `tests/kernel/test_public_api.py` | 当时把 `Orchestrator`、`register_builtins` 放进 `_WITHHELD`。`kernel/__init__.py` 当时没动；**Task 8 已把它们挪进承诺面** |

执行时拍板、下一任不要改回去的细节：

1. **编排工具不在工厂里装。** 工厂签名只有 `(hooks, events)`，没有 Runtime、没有 agent_id。`spawn` 先走旗子那一支，再给角色一次 `install_own_tools(runtime)`。
2. **`Runtime(orchestration=False)` 时 Orchestrator 仍有 `Delegate`，Worker 没有。** 旗子打开时 Delegate 已经在工具表里，Orchestrator 必须直接返回，不要再 `install_runtime_tools` 一次（那会换掉旗子刚绑上的实例）。`install_runtime_tools` 自己不报重名。
3. **不是 Runtime 的对象调用 `install_own_tools` 是 ConfigError。** expected 含 `Runtime`。
4. **提示词在 freeze 拼，不在 `register_builtins` 时写死。** `_role_prompt` 看类上有没有可调用的 `compose_system_prompt`，不特判名字 `'Orchestrator'`。拼的是 `catalog.get(subagent).description` 的当前值。用户在 freeze 前 `load_mapping` 覆盖 Worker，编排者提示词里是新描述，内置那句「改文件、跑命令、验证结果时用我」不在。
5. **Orchestrator 用 `register_builtin` / `load_mapping`，不用 `register_class`。** `register_class` 不写 tools / subagents / system_prompt。`impl` 是 `syndica.kernel.agents.orchestrator:Orchestrator`。`subagents` 是 `('Planner', 'Explorer', 'Worker')`。清单 `tools` 是空元组。
6. **`names()` 第一次见到的顺序是 `('Planner', 'Explorer', 'Worker', 'Orchestrator')`。** 第二次 `register_builtins` 是覆盖，留下 `OverrideRecord`，两侧 `kind` 都是 `builtin`，顺序不变。鸭子 catalog（只有 `register_builtin` 方法）ConfigError。
7. **Worker 点名的 Write / Edit / Bash 计划 4 才有真工具。** 测试必须 stub 这些名字，并且把元组钉死。目录里缺任何一个，freeze 失败，这是约束。
8. **声明式角色的 `system_prompt` 原样进 freeze 后的 Agent。** 改 JSON 里的句子必须让 `test_declarative_system_prompts_are_what_freeze_hands_the_agent` 红。
9. **Orchestrator 装上的是五件套，顺序是 `Delegate, Fork, SendMessage, QueryAgent, KillAgent`。** 只断言「有 Delegate」挡不住只装 Delegate、也挡不住把顺序打乱。`orchestration=False` 时 Worker 的工具表是 `Read, Write, Edit, Bash`，一个都不能少。

### 3.13 计划 3 Task 8 —— 已落地，commit `15436ed`

**不要重做。** 门禁：`uv run pytest -W error tests/kernel/test_public_api.py tests/integration/test_topology_lifecycle.py tests/kernel/test_builtin_agents.py -q` → **25 passed**；ruff / pyright 0 errors；`python scripts/check_deps.py` 空。自我变异：承诺面九个名字逐个从 `__all__` 删掉，9/9 杀死。独立评审另创 15 条，10 条当时就被杀死；补上请求里的工具顺序、三条 `Delegate 给 …` 行、Worker 含 `Write`、以及 `find` 不把已结束的 Worker 从 ZOMBIE 激活之后，剩下 5 条也杀死。

| 文件 | 角色 |
|---|---|
| `src/syndica/kernel/__init__.py` | 导出 `AgentCatalog`、`AgentManifest`、`AgentSlot`、`HostFacts`、`ManifestSource`、`Orchestrator`、`OverrideRecord`、`Topology`、`register_builtins`。`__all__` 仍按字母序，`ManifestSource` 在 `Message` 前 |
| `tests/kernel/test_public_api.py` | 同一批名字进 `_KERNEL_EXPORTS`，离开 `_WITHHELD`。`test_promised_names_include_catalog_topology_and_orchestrator` 把期望写死，不从 `__all__` 推导 |
| `tests/integration/test_topology_lifecycle.py` | 只 import `syndica.kernel`。覆盖 Planner 描述后 assemble；eager 根、lazy Worker、on-demand Explorer |
| `tests/kernel/test_builtin_agents.py` | `orchestration=False` 时 Orchestrator 的工具表是完整五件套，Worker 是四件文件工具 |

执行时拍板、下一任不要改回去的细节：

1. **这九个名字进承诺面，`parse_mapping` / `register_tool` / `resolve_tool_names` 仍扣留。** 多导出一个名字，`test_the_public_surface_is_exactly_this` 会红。
2. **`_RUNTIME_INIT` 与 `_RUNTIME_SURFACE` 都不变。** 不给构造器加 `catalog=` / `topology=`，不加 `start`。
3. **Delegate 按角色名冷启动，不占用 lazy 槽。** 跑完之后注册表里恰好一个 `name=='Worker'`，且 `parent_id` 是根；`resolve('worker', spawn=False)` 仍是 `None`。计划正文里「Worker 因 lazy 被创建」说的是现象，因果是冷启动。不要改 Delegate 去 `occupy`。
4. **`find` 不创建，也不激活。** `find('explorer')` 前后注册表里没有 Explorer。`occupy` 两次交出两个不同 id，槽名仍然 `resolve` 不到。对已经 ZOMBIE 的 Worker 再 `find`，状态保持 ZOMBIE。
5. **覆盖必须出现在 assemble 之后真正发出去的那一轮，不只出现在预览工厂。** 提示词含 `Delegate 给 Planner：` 加自定义描述、`Delegate 给 Explorer：` 加内置描述、`Delegate 给 Worker：`。内置 Planner 那句不在。
6. **编排者请求里的工具名顺序是五件套；Worker 那一轮是 `Read, Write, Edit, Bash`，没有 Delegate。** 端到端用 `orchestration=False`。

---

## 4. 还没做完的（按动手顺序）

### 4.0 还没改代码的 Minor（不要在计划 3 里顺手做，除非标注了「极便宜」）

> 2026-09-29：M-4、M-8 已修，M-5 随 D-53 关闭，M-3 与 M-6 / M-7 归 M12。登记表与路线图
> 「遗留项归属（2026-09-29）」是现行记录，下表只留作当时的原文。

| # | 内容 | 建议 |
|---|---|---|
| M-3 | `provenance_of` / `strictest_trust` 不满足「公开签名需要它」 | 可扣留；**已导出再拿掉是破坏性变更**。不要在计划 3 里顺手拿掉 |
| M-4 | 端到端里 `status is WAITING` 调度余量恰好 1 跳 | 改成轮询到 WAITING 或超时；真正见证者是 `await_reply.states == [ANSWERED, ABORTED]` |
| M-5 | D-53 正文夸大了「静默漂移」 | 改登记表措辞即可 |
| M-6 | `send` 第二参 spec 叫 `message`、实现叫 `content`；`spawn.query` 收成 keyword-only | 登记为措辞差异，**不要改名** |
| M-7 | spec `AgentInfo.type` 实现叫 `agent_type`（避开内置名） | 登记即可，不要改回去 |
| M-8 | `WaitState.ABORTED` 自己的 docstring 仍只写「协作式中止落在等待上」 | 补枚举 docstring，把 D-26 契约写到宿主会读的那一行 |

### 4.1 计划 3 已收口。计划 4 Task 1 已落地（`1fc689c`）。下一步：Task 2

**已关：** D-49、D-08。**不要动：** D-58（禁止 `Runtime.start` / `Junction.RUNTIME_START`）。计划 3 的八个任务都已 commit。下面的裁决留给读代码的人，不是待办。

执行中途「这里该怎么写」一律回到计划文件的「设计裁决」节，不要现场再发明。下面是给下一任的压缩版；细节、必写用例名、变异点以计划文件为准。

#### 已经拍板、执行时不许改的裁决

1. **YAML 不进 kernel。** 入口是 `load_mapping` / `load_json_file` / `load_json_directory`。YAML → Mapping 是计划 6 / 宿主的事。测试用 dict 与 `.json`，不要 `yaml.safe_load`。
2. **类式 vs 声明式看 `impl`，不用 `class:`。** 缺省/None = 声明式；`type`（BaseAgent 子类）或 `str`（`pkg.mod:Class`）= 类式。误写 `class:` 走未知键。
3. **十个公开字段顺序永久承诺：** `name`, `description`, `system_prompt`, `tools`, `subagents`, `model`, `extends`, `skills`, `budget`, `artifact_kinds`。另加非十字段的 `impl`、`source`。未知键当场 `ConfigError`，一次列出**全部**非法键。
4. **`tools` 是名字列表，不是 Tool 映射。** D-49：`validate_tool_names` 先认 Mapping。D-08：登记时三条身份轴（dict 键 == `tool.name` == `schema['name']`）必须逐字相同，不要「按 schema 名改键」悄悄修好。
5. **同名覆盖可查询。** `OverrideRecord`；`overrides(name)` 从旧到新；同一次 `load_*` 内部撞名是 ConfigError，不是覆盖。
6. **插值白名单只有 `{os}` `{platform}` `{shell}`。** `{cwd}` 明确禁止。`HostFacts` 由宿主注入，内核不读 env / cwd。插值在 freeze 时一次，不在每轮。
7. **拓扑只增强、不替换。** 没有自由 `options: dict`。`keyed+eager` 与 `on-demand+eager` 禁止。`assemble` 不是 `start()`。第二次 assemble → ContractViolation。`send(agent_id)` 不收别名；别名走 `resolve` / `occupy` / `send_to_slot`。`find` 仍然绝不激活。
8. **四个内置已落地（`d0e6828`）并已进承诺面（`15436ed`）。** 不要重做，也不要再扣留。
9. **公开面已有：** `AgentManifest`, `HostFacts`, `AgentCatalog`, `AgentSlot`, `Topology`, `OverrideRecord`, `ManifestSource`, `Orchestrator`, `register_builtins`；Runtime 已有 `assemble` / `resolve` / `occupy` / `send_to_slot`。**构造器不变。** `parse_mapping` / `register_tool` / `resolve_tool_names` 仍在 `_WITHHELD`。

**执行前默认：`AgentSlot` 不要 `extra_plugins` 字段。** 架构草图第 129 行还留着这个字段；**以自我审查 §7 为准**——Plugin 实例难冻，会膨胀公开面。插件仍只在 `Runtime(plugins=)`。增强面只留 `extra_tools` / `budget` / `metadata`。`send_to_slot` 计划正文（Task 6）**建议加**，加了就必须进 `_RUNTIME_SURFACE`；不要重载 `send` 的第一个参数当别名。

#### 八个任务（建议 8 个 commit，不要 push）

| Task | 标题 | 新建 / 改 | 关掉 | 状态 |
|---|---|---|---|---|
| **1** | `AgentManifest` + `parse_mapping`（不含工具解析） | `manifest.py`、`test_manifest.py`；`__init__.py` 先不要导出；`_WITHHELD` 扣三个名字 | — | **已 commit `97c9edc`** |
| **2** | `validate_tool_names` 认 Mapping + 名字列表→工具表 | 改 `tool.py`、`test_tool.py`；`catalog.py` 的 `resolve_tool_names` / `register_tool` | **D-49、D-08** | **已 commit `551b4d2`** |
| **3** | `AgentCatalog`：加载、覆盖可查询、`get(name)` | **改**已有 `catalog.py`、`test_catalog.py`（不要当空白重写） | — | **已 commit `7071eda`** |
| **4** | 插值 + `freeze` 成 `AgentFactory` | **改**已有 `catalog.py`、`test_catalog.py`；新建 `test_prompt_interpolation.py` | — | **已 commit `73289a2`** |
| **5** | `AgentSlot` / `Topology` 校验（还不接 Runtime） | `topology.py`、`test_topology.py` | — | **已 commit `cb81e02`** |
| **6** | `Runtime.assemble` / `resolve` / `occupy` / `send_to_slot` | `runtime.py`、`test_assemble.py`、`_RUNTIME_SURFACE` 加名字 | — | **已 commit `b508023`** |
| **7** | 四个内置角色 | `src/syndica/kernel/agents/`、`test_builtin_agents.py` | — | **已 commit `d0e6828`** |
| **8** | 公开面收口 + 端到端一条拓扑 | `kernel/__init__.py`、`test_public_api.py`、`tests/integration/test_topology_lifecycle.py` | 把 Task 1 扣留的名字挪进承诺面 | **已 commit `15436ed`** |

每个任务：实现 → 受影响测试绿 → **自我变异**（先清 `__pycache__`；新文件先 `git add`；sha256 基线必须覆盖本轮会改到的每个文件，见 AGENTS.md）→ 独立评审自创变异 → 修复 → commit。受影响测试：

```
uv run pytest tests/kernel/test_<本任务>.py tests/kernel/test_public_api.py -q
```

再按改动加 `test_runtime.py` / `test_tool.py`。不要全仓 `make test`。

遗留项**同时**记进 [`deferred-items.md`](../decisions/2026-09-14-deferred-items.md)，不要只写 gitignored 的 `.superpowers/sdd/progress*.md`。

### 4.2 计划 4 已收口

需求在 [`2026-09-22-m1p4-local-exec.md`](./2026-09-22-m1p4-local-exec.md)。不要在执行时重新划范围：git / worktree / 快照 / 伪终端是里程碑 5。

九个任务均已完成：

| Task | 标题 | 状态 |
|---|---|---|
| 1 | Portal / Workspace / LocalWorkspace 路径监狱 | 已落地 `1fc689c` |
| 2 | Read / Write / Edit | 已落地 `4989d9f` |
| 3 | Glob / Grep | 已落地 `6e36b86` |
| 4 | Bash / BashOutput / KillBash | 已落地 `d0eee37` |
| 5 | 真实工具跑通一条拓扑 | 已落地 `6b95895` |
| 6 | OpenAI 兼容 provider | 已落地 `30fed58` |
| 7 | 注册表快照、契约再导出、等待超时起算点 | 已落地 `10b4dad` |
| 8 | 依赖门禁缺口、注释、公开面、关台账 | 已落地 `ade688f` |
| 9 | publish 与 remove_queue 的回执窗口 | 已落地（本文同一提交） |

计划 4–6 所属遗留项与 M1 最终审计挂单均已转入登记表「已关闭」。

### 4.3 计划 5（已收口）

计划文件：[`2026-09-22-m1p5-runtime-modules.md`](./2026-09-22-m1p5-runtime-modules.md)。

范围已完成：Runtime Module + Flag（§5.5 四类注入）；重试 / 故障转移 Module（§5.7）；
前缀漂移检测（§5.2）。Task 1–7 分别提交为 `a54da01`、`8f75e0e`、`da996c0`、`f131a28`、
`6533439`、`608cf7f`、`4079d37`；Task 8 与本次交接同提交。

挂单 D-18、D-47（墙钟）、D-19–D-23、D-29、D-55 已全部转入登记表“已关闭”。

### 4.4 计划 6（已收口）

计划文件：[`2026-09-23-m1p6-cli-human-e2e.md`](./2026-09-23-m1p6-cli-human-e2e.md)。

范围已完成：CLI（§9.1 名词+动词、默认 `run`、`chat` 共用 `run_request`）；L2 声明式场景回放
（§13.1）；`AskUser` / `AskUserQuestion` 与人工确认；YAML → Mapping。Task 1–7 提交为
`d4d04bc`、`1397fa3`、`47a576c`、`ff36133`、`07e2a4d`、`33f464e`、`b9a652f`。

挂单 D-38 已关闭：获批调用的 `execute + POST_TOOL_USE` 使用 writer-priority 执行门独占。

### 4.5 里程碑 1 收口审计（已完成）

审计记录：[`docs/audits/2026-09-23-m1-final-audit.md`](../audits/2026-09-23-m1-final-audit.md)。
spec §15 已逐条取证；D-51、D-27、D-58、D-59、V-01、V-02 已关闭；全量
`2245 passed`，Ruff、格式、Pyright、依赖守卫与 L2 场景均通过。当时按用户要求未 push；2026-09-29
已全部 push。

### 4.6 明确不在里程碑 1

登记表「BEYOND-M1」整节：B-01 租约/孤儿回收、B-02 `wait_until_parked`、B-15 溯源链、D-40 只读视图嵌套、D-46 树深度、预算树属里程碑 8，等等。不要在计划 3–6 里顺手做。

里程碑 2 起（自举、可观测性、PTY、压缩、产物、RL、HTTP/React、MCP/ACP/A2A）见 spec §15。
开始前另写并评审一份自包含计划，不把 `BEYOND-M1` 整节当成一次性任务。

---

## 5. 登记表归属速查

原文永远以 [`deferred-items.md`](../decisions/2026-09-14-deferred-items.md) 为准。这里只帮下一任定向，不要当权威。

| 归属 | 条目 |
|---|---|
| **计划 3** | 无。D-49、D-08 已关，八个任务都已 commit |
| **计划 4** | 无。Task 1–9 与所属登记项均已关闭 |
| **计划 5** | 无。Task 1–8 与所属登记项均已关闭 |
| **计划 6** | 无。D-38 已关闭 |
| **M1 收口审计** | 无。D-51、D-27、D-58、D-59、V-01、V-02 已关闭 |
| **已关、不要重开** | D-01…D-08、D-24–D-26、D-28、D-30–D-33、D-35、D-39、D-41、D-44、D-45、D-48、D-49、D-52、D-54、D-60 等（见登记表「已关闭」） |

---

## 6. 下一任的第一条命令

在 `/Users/loren/Documents/loren/syndica`：

```bash
git status -sb
git log -3 --oneline
```

然后：

1. 先读 M1 最终审计记录，不重做计划 1–6。
2. 用户要继续时，为 spec §15 的里程碑 2（自举）单独写计划并等待 review。基础 CI 要排在任何让
   Agent 改仓库代码的任务之前（路线图「遗留项归属」）。
3. 每个任务门禁与验证通过后 commit 并 push 到 `main`。

---

## 7. 实现时容易踩的坑（本会话刚踩过或评审钉过）

- **abort 不要复用 `before_turn` 去查第 9 步**：`before_turn` 会消费邮箱。必须 `check_abort` → `take_abort()`。
- **未执行的工具块**用 `append_unexecuted`，与 executor 配对机械一致；不要写成「副作用未知」。
- **bool 是 int 子类**：YAML `yes` → `True` → `capacity=1` 静默丢事件。凡公开整型入口先拒 bool。
- **`isinstance(Class, Protocol)` 为真**（方法挂在类上）。`_checked_store` 必须先 `isinstance(value, type)`。
- **`shutdown` 不能 `if self._closed: return`**：第一次可能还堵在 `RUNTIME_SHUTDOWN` 钩子里，总线没关。收据只挡住「报」，拆照做。
- **`str` 是 Sequence**：清单的 `tools` 若只 `isinstance(x, Sequence)` 会把 `"Read"` 拆成四个字母。先拒 `str` / `bytes`，再拒 Mapping，再收 list/tuple。
- **新 `.py` 必须声明字面量 `__all__`**：`test_the_export_scan_really_covers_the_whole_package` 会因为漏声明而红；名字还必须进承诺面或 `_WITHHELD`。
- **并发 commit 竞态**：本会话曾出现另一条本地提交把工作区拆成两截。`git add` 后立刻 `git status` / `git log -3`。
- 变异验证：基线集合本身要被证明；只对「清单里的文件」做 sha256，清单漏了就会静默缩小范围（AGENTS.md 已记）。新文件必须先 `git add` 再取基线。
- **abort 仍不取消 inflight `LLM.create`**。第 9 步只在 create 返回之后兑现。卡住的 Agent 仍然只有 `kill`（F-6 / D-56，计划 4）。
- **并发 CAS 用例挡不住「两个赢家都写同一个 new」**——三个目标状态不同才分得出。
- **一次没复现的 despawn 级联泄漏**（审计故意不上报）：`p9_growth.py` 第一次跑在「父子级联」段留下 25 条注册表条目 + 25 个 `_slots`，此后九次重跑与 40 轮随机化全零。若将来看到「每次少拆一个」，怀疑 `_destroy_tree` 的 `while fresh :=` 与并发收割交互。
- **本机 bash 没有 `declare -A`**。Task 1 变异跑手因此改成 Python dict。不要写 bash 关联数组。还原用 sha256 基线，不要 `git checkout`（会把未提交的测试也冲掉）。
- **Task 4 会改同一份 `catalog.py`。** 计划原文写 Create `test_prompt_interpolation.py` 且可能改 `manifest.py`；实际插值在 catalog 层，`manifest.py` 没动。Task 1–3 的必写用例名必须继续绿。
- **同目录撞名：`names()` 钉不住「整批不写入」。** 只把 Unique 塞进 `_entries`、不改 `_order` 时 `names()` 仍是 `('Alpha',)`。必须 `get('Unique')` / `get('Worker')` 都拒绝。
- **后缀用 `suffix == '.json'`，不要 `'.json' in name`。** `worker.json.bak` 是诱饵。
- **`displaced_at_index` 不是 `names()` 下标。** 它是该名字第几次被挤掉，从 0 起。
- **装门**：生产路径只有 `Runtime.spawn` → `agent._attach_gate`（测试可 `pyright: ignore[reportPrivateUsage]`）。宿主构造 `BaseAgent` 时不要传 `gate=`。
- **`{cwd}` 测试必须给 host 一个真的 cwd。** 只用 `HostFacts` 三字段时，`getattr(host, 'cwd', None)` 为真才插值的错实现也会绿。用 `HostFacts` 子类带 `cwd=`。
- **子 prompt 钉「省略则空串」，不要钉「子写了 CHILD、父是空」。** `child or parent` 会把 CHILD 当继承结果。
- **无 `budget` 必须断言 `max_turns == 50`。** `_max_turns` 返回 `None` 会覆盖 dataclass 缺省。
- **缺模型 / 缺 subagents 至少 3 个独立缺失名。** 「最多报两个」会绿。
- **host 鸭子类型用 `SimpleNamespace(os, platform, shell, cwd)`，不要只传 `'darwin'`。**
- **`exit_mode='ZOMBIE'` 不是 `ExitMode.ZOMBIE`。** 只拒小写 `'zombie'` 时，`ExitMode[value]` 旁路能活。
- **`budget.turns=40.0` 不是 40。** bool 被拒之后，整浮点仍可能被 `int()` 收成合法轮次。`turns=0` 合法，不要写成 `<= 0` 就拒。
- **`metadata` / `budget` 必须是 `MappingProxyType`。** 只断言深拷贝挡不住「返回普通 dict」。
- **cardinality / boot 大小写敏感。** `casefold()` 之后 `'Keyed'` 会过。
- **`validate` 成功返回 `None`。** 末尾 `return True` 时，只调用、不断言返回值的用例全绿。
- **`assemble` 的 `cast` 类型参数要加引号。** `from __future__ import annotations` 不推迟 `cast()` 的实参；`Tool` 又只在 `TYPE_CHECKING` 里。不加引号是 NameError，为了 NameError 去运行时 import 会被 pyright 判成未使用。
- **别名冲突不能靠「表里已经有就返回」。** 那会把另一个槽的 agent 当成自己的幂等命中，`_spawn_slot` 里的冲突检查根本跑不到。必须看 `_topology_alias_owners`。
- **`_assembled = True` 必须在 eager 循环之前。** 放在循环之后，半截失败可以再 assemble，成功的 eager 会被再 spawn 一次。
- **相等预算用 `>` 不用 `>=`。** `>=` 会把「槽和角色一样」当成放宽。角色无限时 `current is None`，槽给一个整数是收紧，不要拒。
- **槽 `turns: null` 不是「这个实例不限」。** 角色已经有限时，null 是放宽。清单缺省预算是 50，不是无限；无限只来自清单 `budget: {turns: null}`。
- **不要原地改共享 `AgentOptions`。** freeze 的工厂闭包共享一份 options 和一份 tools dict。增强必须 `replace` / `install_runtime_tools`。断言：occupy 之后再 `agent_factory(name)` 出来的新实例仍是角色自己的轮次和工具。
- **编排工具不要塞进工厂。** 工厂没有 Runtime。`spawn` 在旗子之后调 `install_own_tools`。Delegate 已经在表里就返回，再装会盖掉旗子绑的那一套。
- **不要按角色名特判 `'Orchestrator'` 来拼提示词。** 看类上有没有 `compose_system_prompt`。拼的是 freeze 当时的 description，不是登记那一刻抄下来的句子。
- **Worker 工具元组钉死 `('Read', 'Write', 'Edit', 'Bash')`。** 少一个名字，现有测试必须红。真工具仍在计划 4。
- **`__all__` 字母序：`ManifestSource` 在 `Message` 前面。** `test_export_list_is_sorted` 按字符串比，不是按「看起来像一类」分组。
- **Delegate 不占用 lazy 槽。** 端到端同时断言「有一个 Worker 子 Agent」和「`resolve('worker', spawn=False)` 仍是 None」。只断言「有 Worker」会把冷启动误写成占槽。
- **`find` 对 ZOMBIE 保持 ZOMBIE。** 只断言返回值非空，挡不住 `find` 里偷偷 `ensure_active`。
- **编排者请求的工具顺序是五件套。** 只断言集合里有 `Delegate`，挡不住只装 Delegate，也挡不住打乱顺序。
- **文件工具的 `__tool_command_args__` 必须是 `()`。** 保持 `None` 时，危险判定会扫全部参数；正文里的 `curl … | sh` 会在 `execute` 之前变成 HIGH。`echo hi | sh` 打不中默认规则，测例必须带下载器。
- **`local_tools` 不要断言键的个数。** Task 3 起只追加。前三个键的相对顺序是 Read、Write、Edit。
- **超过 1 MiB 的拒绝在 `read_text`，不在工具类里。** 工具不 import `os`。正好 1 MiB 允许（比较是 `>`）。

---

## 8. 关键路径速查

| 要改什么 | 先打开 |
|---|---|
| turn 循环 / 第 9 步 abort | `src/syndica/kernel/agent.py` `_run_turns` |
| 门协议 | `src/syndica/kernel/control.py` `TurnGate` |
| 生命周期 / spawn / shutdown | `src/syndica/kernel/runtime.py` |
| 角色表校验（D-49 的参照实现） | `runtime.py` `validate_agent_factories` / `_not_a_role_table` |
| 声明式清单解析（Task 1 已做） | `src/syndica/kernel/manifest.py` `parse_mapping` |
| 工具目录（Task 2 已做） | `src/syndica/kernel/catalog.py` `register_tool` / `resolve_tool_names`；`tool.py` `validate_tool_names` |
| Agent 目录（Task 3 已做） | `src/syndica/kernel/catalog.py` `AgentCatalog` / `OverrideRecord` |
| freeze / 插值（Task 4 已做） | `src/syndica/kernel/catalog.py` `HostFacts` / `freeze` / `_interpolate_prompt` |
| 拓扑槽（Task 5 已做） | `src/syndica/kernel/topology.py` `AgentSlot` / `Topology.validate` |
| 装配（Task 6 已做） | `src/syndica/kernel/runtime.py` `assemble` / `resolve` / `occupy` / `send_to_slot` |
| 装配测试 | `tests/kernel/test_assemble.py` |
| 四个内置（Task 7 已做） | `src/syndica/kernel/agents/` `register_builtins`；`catalog.py` `_role_prompt` |
| 内置测试 | `tests/kernel/test_builtin_agents.py` |
| 公开面（Task 8 已做） | `src/syndica/kernel/__init__.py`；`tests/kernel/test_public_api.py` |
| 一条拓扑（Task 8 已做） | `tests/integration/test_topology_lifecycle.py` |
| 公开面守卫 / `__init__` 签名 | `tests/kernel/test_public_api.py` |
| 装门（仅内核） | `BaseAgent._attach_gate`；测试走 `_agent(..., gate=)` |
| 多 Agent 取证 | `tests/integration/test_multi_agent_lifecycle.py` |
| 登记表 | `docs/decisions/2026-09-14-deferred-items.md` |
| 计划 2 原文 | `docs/plans/2026-09-08-m1p2-control-plane.md` |
| 计划 3（已收口，不要重做） | `docs/plans/2026-09-21-m1p3-declarative-agents.md` |
| 计划 4（已收口） | `docs/plans/2026-09-22-m1p4-local-exec.md` |
| 本地工具 | `src/syndica/kernel/tools/local.py`；`tests/kernel/test_local_tools_*.py` |
| **下一步** | 为里程碑 2（自举）单独写计划 |
| 范围裁决 | `docs/decisions/2026-09-08-m1-scope.md` |
| 硬约束 / 变异纪律 | `AGENTS.md` |

计划 4 的协议和 Read / Write / Edit 已经在仓库里。不要把下面这些当空白重写：

- `tests/integration/test_topology_lifecycle.py`

已经存在、不要当空白重写：

- `src/syndica/kernel/agents/`（`orchestrator.py` + 三份 JSON + `register_builtins`）
- `tests/kernel/test_builtin_agents.py`
- `src/syndica/kernel/manifest.py`
- `tests/kernel/test_manifest.py`
- `src/syndica/kernel/catalog.py`
- `tests/kernel/test_catalog.py`
- `tests/kernel/test_prompt_interpolation.py`
- `src/syndica/kernel/topology.py`
- `tests/kernel/test_topology.py`
- `tests/kernel/test_assemble.py`
- `src/syndica/kernel/workspace.py`
- `src/syndica/kernel/local_workspace.py`
- `tests/kernel/test_local_workspace.py`
- `src/syndica/kernel/tools/local.py`
- `tests/kernel/test_local_tools_files.py`
