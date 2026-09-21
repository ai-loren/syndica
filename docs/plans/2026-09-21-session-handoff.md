# 会话交接：里程碑 1 做到哪、接着做什么

写于 2026-09-21。横切审计修复已合入本地 `main`，计划 3 **已写好、未执行**。本文给下一任（人或 Agent）接着干，不替代 spec / 计划 / 登记表。

**先读顺序**：本文 → [`AGENTS.md`](../../AGENTS.md) → [`docs/design/2026-09-03-architecture.md`](../design/2026-09-03-architecture.md)（spec，§15 / §17） → [`docs/decisions/2026-09-08-m1-scope.md`](../decisions/2026-09-08-m1-scope.md) → [`docs/decisions/2026-09-14-deferred-items.md`](../decisions/2026-09-14-deferred-items.md) → 当前子计划。

---

## 0. 仓库与 git 状态（动手前先核对）

| 项 | 值 |
|---|---|
| 仓 | `/Users/bytedance/Documents/syndica`（GitHub 私有，Loren Zhang / zhangzihao） |
| 分支 | `main`（不要另开 worktree，除非你明确要） |
| 语言 | Python 3.11+，`uv` |
| 写代码的约定 | 中文回复；注释只写硬约束；错误三件套；**不要**跑全仓 `make lint` / `make test`；本仓验证用 `uv run pytest <受影响路径>`、`uv run ruff check/format`、`uv run pyright`、`scripts/check_deps.py` |
| git | 本项目此前默认**改完即 commit、不 push**，除非用户再说「推」 |
| 提交时 | `git -c trace2.eventtarget= -c trace2.eventnesting= commit`（本机 git 会被 trace2 塞 Co-authored-by） |

**写本文时的 HEAD（交接当时）**：`d4961c1`。此后又合入了交接文档本身（`0269add` / `a15c31b` / `5f808eb`）、**Task 16 评审残留**（`77f33fc`，见 §2.5）、以及计划 3 文件。动手前以 `git log -5 --oneline` 为准。

```
$ git status -sb
## main...origin/main [ahead N]
```

`origin/main` **还没有**计划 2 收口之后的本地提交。**未 push。** 从新到旧（交接当时 8 个；之后又加了交接文档与公开面残留）：

| sha | 内容 |
|---|---|
| `77f33fc` | `__init__` 签名守卫 / orchestration 两档 / `attach_gate` 降私有 / `__all__` 扫描 fail-loud / trigger 计数 / 关闭 D-52 |
| `5f808eb` | 交接文档里 1020 那条 pytest 命令写完整 |
| `a15c31b` | 交接文档补全量 1710 与 M-7/M-8 |
| `0269add` | 记下里程碑 1 做到哪、还剩什么（本文） |
| `d4961c1` | 入口校验 / CAS 并发契约 / 关机收据 / lost-zombie 分档 / D-56..D-60 |
| `504f488` | `AgentInfo.created_at` |
| `df5db9f` | AGENTS.md 四条横切审计教训 |
| `cba3e3c` | abort 在模型产出后、工具执行前兑现（F-1） |
| `c7d2751` | Task 16 关掉六条移交 + 登记新缺口 |
| `449dccc` | 公开面端到端给 spec §15 判据取证 |
| `312cd59` | `_RUNTIME_ONLY` 触发点扫描守卫 |
| `96c7cdd` | 公开面收口（27 进承诺、27 扣留） |

更早的计划 2 Task 1–15 已在 `origin/main` 一侧。

---

## 1. 这一轮的目标是什么

用户原话可以收成三句（时间越后越具体）：

1. **产品目标（一直有效）**：做 Syndica——框架优先、范围 ≥ maso、Python、单进程 asyncio 内核 + 可插拔存储、完整产品形态的 v1.0。
2. **里程碑目标**：把里程碑 1 做完（子计划 2–6），判据见 spec §15。
3. **本会话收窄后的目标**：「把你认为 multi-agent 没做完的部分做完」+ 全内核多维自审（功能 / 语法 / 稳定性 / 可扩展 / 鲁棒）+ 合理优化。

裁决过的节奏（不可再解释）：[m1-scope §5–6](../decisions/2026-09-08-m1-scope.md)——**全程与计划 1 相同的全量严格**（实现 → 自我变异 → 独立评审自创变异 → 修复 → 收口）；**一个一个来**：写计划 2 → 执行到收口 → 写计划 3 → …。

**对照 §15 里程碑 1 判据**，今天只覆盖了前半：Runtime 能创建多个 Agent、互发消息、各自跑通 LLM+工具循环、可终止与恢复。还缺：

- 声明式 Agent 定义（§4.6）→ **计划 3，未写未做**
- 拓扑装配（§4.7）→ **计划 3，未写未做**
- 最小本地执行能力（文件读写 + 命令执行）→ **计划 4，未写未做**
- Tool 系统与 LLM 抽象的「能跑一轮有意义的 turn」在内核侧已有（ScriptedLLM + 编排工具）；真 provider 与 Portal 工具在计划 4。

所以：**计划 2 + 横切审计 ≈ 内核控制面做完；里程碑 1 本身没完。**

---

## 2. 已经做完的

### 2.1 计划 1（单 Agent turn）

已合入。产物：`docs/plans/2026-09-05-m1p1-single-agent-turn.md`。内核 turn 循环、Context、Hook、ToolExecutor 初版、危险判定骨架等。不要回头重做。

### 2.2 计划 2（内核控制面）——代码已收口

计划文件：[`docs/plans/2026-09-08-m1p2-control-plane.md`](./2026-09-08-m1p2-control-plane.md)。Task 1–16 均已落地并 commit。

| 阶段 | 交付 |
|---|---|
| A / Task 1–7 | 事件循环 IDLE↔RUNNING；`TurnGate`；`kill` / `despawn` / `pause` / `resume` / `abort`；级联拆除；`ensure_active` + `on_missing_queue` |
| B / Task 8–9 | `Waiter` 密封类型、`WaitResult` 四态、`wait_for`、`WAITING` |
| C / Task 10–12 | 危险判定 + 阻断/放行；工具并发分段 |
| D / Task 13–15 | `agent.register_hook`、`Runtime(plugins=)`；编排五件套 `Delegate` / `Fork` / `SendMessage` / `QueryAgent` / `KillAgent` + `orchestration_tools` |
| Task 16 | 公开面收口；`tests/integration/test_multi_agent_lifecycle.py` 给 §15 四条判据取证 |

公开面判据（记住这一句）：**一个名字进 `kernel.__all__`，当且仅当宿主拿不到它就用不了某个已经公开的入口。** 加名字可逆，去掉名字是破坏性变更。清单与扣留在 `tests/kernel/test_public_api.py` 的 `_KERNEL_EXPORTS` / `_WITHHELD` / `_RUNTIME_SURFACE` / `_BASE_AGENT_SURFACE`。

### 2.3 横切审计（F-1…F-11）——代码已落地

独立审计（对照当时 `HEAD=c7d2751`）结论：内核稳，可以往上堆计划 3–6；但有几条要在堆之前修。本会话已修完审计要求「现在修」的那些：

| 编号 | 做了什么 | 关键落点 |
|---|---|---|
| **F-1** | spec §5.1 第 9 步：LLM 产出之后、工具执行之前再兑现 abort | `TurnGate.check_abort`（消费中止位、不吃邮箱）；`BaseAgent._run_turns`；未跑的调用走 `ToolExecutor.append_unexecuted`，**不要**走「副作用未知」那条修复 |
| **F-2** | `AgentInfo.created_at`（构造时戳，受保护） | `agent_types.py`；`UPDATE_PROTECTED_FIELDS`；契约 `test_update_rejects_created_at` |
| **F-3** | CAS 并发契约：n=3，恰好一个赢家 | `contracts/registry.py` |
| **F-4 + F-11** | 入口校验铺开 | `EventBus.capacity`（`validate_positive_int`，bool→类型→值域）；`Runtime(registry=/broker=)` 拒类对象；`spawn(metadata=)` 先 deepcopy 再 register；`DangerJudge` 校验规则；`LLMConfig` 映射拷贝 |
| **F-5** | `shutdown` 收据：拆除照做，`RUNTIME_SHUTDOWN` 只报一次 | `_shutdown_reported`；第二次不得在方法开头见 `_closed` 就 return |
| **F-7** | lost-zombie 分档 | `get()` 是 None → DEBUG（kill 然后 despawn 的正常顺序）；CAS 被拒且条目还在 → ERROR |
| **F-6** | `LLM.create` 必须兑现 `config.timeout` | **只补了契约正文**；可执行用例归计划 4（D-56） |
| **F-8** | `update` 写入侧快照 | **只登记** D-57，归计划 4 |
| **F-9** | 没有 `runtime_start` | **只登记** D-58，不要加 `async def start()` |
| **F-10** | 未配对批次扫描重复 | **只登记** D-59，归 M1 收口审计抽纯函数 |

D-47 已拆成两半：墙钟 clamp → 计划 5（与 D-18 同一条 Flag）；被等 Agent 卡在 `LLM.create` → 计划 4 / D-56。

验证过：

- 受影响子集（本会话控制器跑的）：`uv run pytest tests/kernel/test_{agent_turn,agent_types,control,danger,events,llm,runtime,public_api,tool_executor}.py tests/contracts/test_memory_registry.py -q` → **1020 passed**
- 修复轮在 `d4961c1` 上跑的全量门禁：`uv run pytest -q` / `-W error` → **1710 passed**；`ruff check` / `ruff format --check` / `pyright` 0 errors / `scripts/check_deps.py` 空。审计探针当时在 `/tmp/synaudit/`，多半已经没了，不要依赖它们。

### 2.4 本会话**没有**做、也不该假装做完的（已过时，见 §2.5）

独立评审 Task 16 回来时是 **CHANGES_REQUESTED**（无 Critical，4 Important + 6 Minor）。Important-1（`created_at`）当时已由横切审计修掉。I-2 / I-3 / I-4 与 M-1 / M-2 已在后续一轮落地（§2.5）。M-3…M-8 仍未改代码。

### 2.5 Task 16 评审残留 I-2 / I-3 / I-4 + M-1 / M-2（已落地，未 push）

验证：`uv run pytest tests/kernel/test_{public_api,agent_turn,plugins,runtime}.py tests/integration/test_multi_agent_lifecycle.py -q` → **224 + 237 passed**（两批分别跑）；`ruff check/format` 与 `pyright` 对改动文件 0 errors。

| 项 | 做了什么 | 落点 |
|---|---|---|
| **I-2** | `Runtime.__init__` / `BaseAgent.__init__` 用 `inspect.signature` 钉参数名 + kind + 有无默认值 | `tests/kernel/test_public_api.py` `_RUNTIME_INIT` / `_BASE_AGENT_INIT` / `_init_shape` |
| **I-3** | 公开面按 `orchestration=False/True` 两档参数化，必须相等 | `test_the_runtime_surface_is_exactly_this[False/True]` |
| **I-4** | `attach_gate` → `_attach_gate`；构造器去掉 `gate=`；默认 `NullTurnGate()`；`Runtime.spawn` 调私有方法 | `src/syndica/kernel/agent.py`、`runtime.py`；`test_agent_turn.py` 的 `_agent(..., gate=)` 构造后再 `_attach_gate` |
| **D-52** | 已关，进「已关闭」 | `docs/decisions/2026-09-14-deferred-items.md` |
| **M-1** | `__all__` 扫描遇 Starred / AugAssign / `.append` / 非 List / 非字面量 elt 直接 `pytest.fail` | `_declared_exports` |
| **M-2** | 包里每一次 `....trigger(` 调用必须被匹配器认成 `Junction.X` | `test_the_trigger_matcher_sees_every_trigger_call` |

---

## 3. 还没做完的（按建议动手顺序）

### 3.0 ~~先做：Task 16 评审残留~~ **已完成**（见 §2.5）

I-2 / I-3 / I-4 / M-1 / M-2 已落地；D-52 已关。还没做的 Minor：

| # | 内容 | 建议 |
|---|---|---|
| M-3 | `provenance_of` / `strictest_trust` 不满足「公开签名需要它」 | 可扣留；已导出再拿掉是破坏性变更，要自觉取舍。**不要在计划 3 里顺手拿掉** |
| M-4 | 端到端里 `status is WAITING` 调度余量恰好 1 跳 | 改成轮询到 WAITING 或超时；真正见证者是 `await_reply.states == [ANSWERED, ABORTED]` |
| M-5 | D-53 正文夸大了「静默漂移」 | 改登记表措辞即可 |
| M-6 | `send` 第二参 spec 叫 `message`、实现叫 `content`；`spawn.query` 收成 keyword-only | 登记为措辞差异，**不要改名** |
| M-7 | spec `AgentInfo.type` 实现叫 `agent_type`（避开内置名） | 登记即可，不要改回去 |
| M-8 | `WaitState.ABORTED` 自己的 docstring 仍只写「协作式中止落在等待上」 | 补枚举 docstring，把 D-26 契约写到宿主会读的那一行 |

### 3.1 然后：执行计划 3（计划已写、未执行）

计划文件：[`docs/plans/2026-09-21-m1p3-declarative-agents.md`](./2026-09-21-m1p3-declarative-agents.md)（含 9 条设计裁决、8 个任务、自审）。范围见 [m1-scope](../decisions/2026-09-08-m1-scope.md) 表：

- 声明式 Agent 定义（spec §4.6：清单十字段、加载与同名覆盖可查询、提示词插值白名单）
- Topology 装配（§4.7：三档 cardinality、三档 boot、只增量增强）
- 内核四个 Agent（§4.8：`Orchestrator` 类式，`Planner` / `Explorer` / `Worker` 声明式）

挂在计划 3 上的登记（执行时关掉）：

- **D-49** `validate_tool_names` 非 Mapping 撞裸 `AttributeError`（与已修的 `validate_agent_factories` 同型；清单会在这里转成 tools dict）
- **D-08** 手写 schema 的 `name` 与 `tool.name` 可分叉（收口在注册层）

**不要加 `Runtime.start()`**（D-58）。YAML 不进 kernel，收 Mapping / JSON。文件工具仍是计划 4，清单只点名。

### 3.2 计划 4（未写未做）

- `Workspace` / `Portal` + `LocalWorkspace`（本地目录 + 子进程，零依赖）
- 文件与执行工具：`Read` / `Write` / `Edit` / `Glob` / `Grep` / `Bash` + `BashOutput` / `KillBash`
- 第一个 backend 包：仅 OpenAI 兼容 HTTP（Anthropic 原生不在 M1）

挂在计划 4 上的登记（动手前整表再读一遍原文）：D-09、D-50、D-34、D-36、D-37、D-10、D-11、D-12、D-13、D-14、D-15、D-16、D-17、D-53、**D-56**、**D-57**。其中 D-56 是「第一个 provider 必须在 `config.timeout` 内返回」，**不要在内核里包 `asyncio.timeout`**。

### 3.3 计划 5（未写未做）

Runtime Module + Flag（§5.5 四类注入）；重试 / 故障转移 Module（§5.7）；前缀漂移检测（§5.2）。

挂单：D-18、D-47（墙钟那一半）、D-19、D-20、D-21、D-22、D-23、D-29（`untrusted_high_risk` flag）、D-55（**`flags` 字段不要提前加**，没有读者会被当死代码删掉）。

### 3.4 计划 6（未写未做）

CLI（§9.1 名词+动词、默认 `run`、`chat` = `run --session`）；L2 声明式场景回放（§13.1）；`AskUser` / `AskUserQuestion` 与人工确认。

挂单：D-38（确认通道落地后，「需要授权的调用不与别人并发」必须重新成立）。

### 3.5 里程碑 1 收口审计（计划 6 之后）

spec §15 判据逐条取证 + 遗留项归属清空 + **那时再 push**（或按当时用户指示）。挂单：D-51、D-27、D-58、D-59、V-01、V-02。D-52 已关。

### 3.6 明确不在里程碑 1

登记表「BEYOND-M1」整节（B-01 租约/孤儿回收、B-02 `wait_until_parked`、B-15 溯源链、D-40 只读视图嵌套、D-46 树深度、预算树属里程碑 8，等等）。不要在计划 3–6 里顺手做。

里程碑 2 起（自举、可观测性、PTY、压缩、产物、RL、HTTP/React、MCP/ACP/A2A）见 spec §15，**M1 没完之前不要开。**

---

## 4. 下一任 Agent 建议的第一条命令

在 `/Users/bytedance/Documents/syndica`、确认工作区干净之后：

1. **执行计划 3**（已写好并自审）：[`docs/plans/2026-09-21-m1p3-declarative-agents.md`](./2026-09-21-m1p3-declarative-agents.md)。Task 1 起，不要先写计划 4。关闭 D-49 / D-08；不要加 `Runtime.start`。
2. 每个任务继续走：实现 → 自我变异（先清 `__pycache__`；新文件先 `git add`；sha256 基线必须覆盖本轮会改到的每个文件，见 AGENTS.md）→ 独立评审 → 修复 → 收口。
3. 遗留项**同时**记进 [`docs/decisions/2026-09-14-deferred-items.md`](../decisions/2026-09-14-deferred-items.md)，不要只写 gitignored 的 `.superpowers/sdd/progress*.md`。
4. **仍不要 push**，直到里程碑 1 收口审计通过，或用户明确说推。

---

## 5. 实现时容易踩的坑（本会话刚踩过或评审钉过）

- **abort 不要复用 `before_turn` 去查第 9 步**：`before_turn` 会消费邮箱。必须 `check_abort` → `take_abort()`。
- **未执行的工具块**用 `append_unexecuted`，与 executor 配对机械一致；不要写成「副作用未知」。
- **bool 是 int 子类**：YAML `yes` → `True` → `capacity=1` 静默丢事件。凡公开整型入口先拒 bool。
- **`isinstance(Class, Protocol)` 为真**（方法挂在类上）。`_checked_store` 必须先 `isinstance(value, type)`。
- **`shutdown` 不能 `if self._closed: return`**：第一次可能还堵在 `RUNTIME_SHUTDOWN` 钩子里，总线没关。收据只挡住「报」，拆照做。
- **并发 commit 竞态**：本会话曾出现另一条本地提交把工作区拆成两截。`git add` 后立刻 `git status` / `git log -3`，确认 20 个文件都在历史上。
- 变异验证：基线集合本身要被证明；只对「清单里的文件」做 sha256，清单漏了就会静默缩小范围（AGENTS.md 已记）。
- **abort 仍不取消 inflight `LLM.create`**。第 9 步只在 create 返回之后兑现。卡住的 Agent 仍然只有 `kill`（F-6 / D-56，计划 4）。
- **并发 CAS 用例挡不住「两个赢家都写同一个 new」**——三个目标状态不同才分得出；同目标并发仍可能被坏实现蒙混。
- **一次没复现的 despawn 级联泄漏**（审计故意不上报）：`p9_growth.py` 第一次跑在「父子级联」段留下 25 条注册表条目 + 25 个 `_slots`，此后九次重跑与 40 轮随机化全零泄漏。若将来看到「每次少拆一个」，怀疑 `_destroy_tree` 的 `while fresh :=` 与并发收割交互。

---

## 6. 关键路径速查

| 要改什么 | 先打开 |
|---|---|
| turn 循环 / 第 9 步 abort | `src/syndica/kernel/agent.py` `_run_turns` |
| 门协议 | `src/syndica/kernel/control.py` `TurnGate` |
| 生命周期 / spawn / shutdown | `src/syndica/kernel/runtime.py` |
| 公开面守卫 / `__init__` 签名 | `tests/kernel/test_public_api.py` |
| 装门（仅内核） | `BaseAgent._attach_gate`；测试走 `_agent(..., gate=)` |
| 多 Agent 取证 | `tests/integration/test_multi_agent_lifecycle.py` |
| 登记表 | `docs/decisions/2026-09-14-deferred-items.md` |
| 计划 2 原文 | `docs/plans/2026-09-08-m1p2-control-plane.md` |
| 范围裁决 | `docs/decisions/2026-09-08-m1-scope.md` |
