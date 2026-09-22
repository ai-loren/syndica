# 会话交接：里程碑 1 做到哪、接着做什么

写于 2026-09-21。本文给下一任（人或 Agent）接着干，不替代 spec / 计划 / 登记表。

**一句话现状：** 计划 1、计划 2（含横切审计与 Task 16 评审残留）的**代码已收口并 commit**；计划 3 **计划已写，Task 1–5 已落地并 commit（`97c9edc` / `551b4d2` / `7071eda` / `73289a2` / `cb81e02`），Task 6–8 未做**；计划 4–6 未写未做。本地 `main` 比 `origin/main` 超前 24 个 commit，**未 push**。下一刀是计划 3 Task 6（`Runtime.assemble` / `resolve` / `occupy` / `send_to_slot`）。

**先读顺序**

1. 本文
2. [`AGENTS.md`](../../AGENTS.md)
3. [`docs/design/2026-09-03-architecture.md`](../design/2026-09-03-architecture.md) spec §15 / §4.6–4.8 / §17
4. [`docs/decisions/2026-09-08-m1-scope.md`](../decisions/2026-09-08-m1-scope.md)
5. [`docs/decisions/2026-09-14-deferred-items.md`](../decisions/2026-09-14-deferred-items.md)
6. 当前子计划：[`docs/plans/2026-09-21-m1p3-declarative-agents.md`](./2026-09-21-m1p3-declarative-agents.md)

---

## 0. 仓库与 git（动手前先核对）

| 项 | 值 |
|---|---|
| 仓 | `/Users/bytedance/Documents/syndica`（GitHub 私有，Loren Zhang / zhangzihao） |
| 分支 | `main`（不要另开 worktree，除非用户明确要） |
| 语言 | Python 3.11+，`uv` |
| 写代码的约定 | 中文回复；注释只写**现在的硬约束**（不要写推理过程）；错误三件套；**不要**跑全仓 `make lint` / `make test` |
| 本仓验证 | `uv run pytest <受影响路径>`、`uv run ruff check` / `ruff format --check`、`uv run pyright`、`python scripts/check_deps.py` |
| git | 本项目此前默认**改完即 commit、不 push**，除非用户再说「推」 |
| 提交时 | `git -c trace2.eventtarget= -c trace2.eventnesting= commit`（本机 git 会被 trace2 塞 Co-authored-by: Cursor） |

**本文对照的基线 HEAD：`cb81e02`。** 提交本文之后 `HEAD` 会再前进一步；动手前以 `git log -3 --oneline` 与 `git status -sb` 为准。工作区干净、比 `origin/main` 超前即可续写。

```
$ git status -sb
## main...origin/main [ahead 24]
（工作区干净，无未提交改动——本文 commit 之后会变成 ahead 25）
```

`origin/main` **还没有**计划 2 收口之后的本地提交。**未 push。** 从新到旧（相对 origin/main 的 24 个）：

| sha | 内容 |
|---|---|
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

**本轮停笔时工作区干净。** 不要重做 Task 1 / Task 2 / Task 3 / Task 4 / Task 5。下一刀是 Task 6：`Runtime.assemble` / `resolve` / `occupy` / `send_to_slot`。`assemble` 不是 `start()`。

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
| 声明式 Agent 定义（§4.6） | **清单解析 + 工具目录 + 加载/覆盖 + freeze 已做；四个内置未做** | 计划 3 Task 1–4 已 commit；还差 Task 7–8 |
| 拓扑装配（§4.7） | **槽校验已做；assemble / resolve / occupy 未做** | 计划 3 Task 5 = `cb81e02`；还差 Task 6、8 |
| Tool 系统与 LLM 抽象 | **内核抽象已做** | Tool / ToolExecutor / DangerJudge / ScriptedLLM；OpenAI 兼容 provider 在计划 4 |
| 最小本地执行（读写文件 + 跑命令） | **未做** | 计划 4（`Read`/`Write`/`Edit`/`Glob`/`Grep`/`Bash`…） |

**结论：计划 2 + 横切审计 ≈ 内核控制面做完；计划 3 Task 1–5 把清单解析、工具目录、AgentCatalog 加载/覆盖、freeze/插值、拓扑槽校验钉住了。里程碑 1 本身没完。** 还缺 assemble、四个内置、本地执行。

子计划边界（不要重新解释）：

| 子计划 | 范围 | 状态 |
|---|---|---|
| **1** | 单 Agent turn 循环 | **代码收口**，已在更早的历史上 |
| **2** | 事件循环、生命周期、等待、危险判定、工具并发、插件、编排工具、公开面 | **代码收口**（含审计修复 + Task 16 残留） |
| **3** | 声明式清单、目录、拓扑、四个内置角色 | **计划已写** [`2026-09-21-m1p3-declarative-agents.md`](./2026-09-21-m1p3-declarative-agents.md)；**5/8 已执行**（Task 1 = `97c9edc`，Task 2 = `551b4d2`，Task 3 = `7071eda`，Task 4 = `73289a2`，Task 5 = `cb81e02`） |
| **4** | Workspace/Portal + 文件与执行工具 + OpenAI 兼容 provider | 未写未做 |
| **5** | Runtime Module + Flag + 重试 Module + 前缀漂移检测 | 未写未做 |
| **6** | CLI + L2 场景回放 + AskUser | 未写未做 |
| **M1 收口审计** | spec §15 取证 + 遗留项清空 + **那时再 push** | 未做 |

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

今天的 Runtime 公开方法（`_RUNTIME_SURFACE`，计划 3 会再加 `assemble` / `resolve` / `occupy` / 建议的 `send_to_slot`）：

`abort`, `agent_factory`, `agent_names`, `broker`, `despawn`, `ensure_active`, `events`, `find`, `hooks`, `kill`, `pause`, `plugins`, `registry`, `resume`, `send`, `shutdown`, `spawn`, `wait`, `wait_for`

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
| **F-10** | 未配对批次扫描重复 | **只登记** D-59，归 M1 收口审计抽纯函数 |

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
| `src/syndica/kernel/__init__.py` | **没动**（等 Task 8） |

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
| `src/syndica/kernel/__init__.py` | **没动**（等 Task 8） |

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
| `src/syndica/kernel/__init__.py` | **没动**（等 Task 8） |

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
| `src/syndica/kernel/__init__.py` | **没动**（等 Task 8） |
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
| `src/syndica/kernel/__init__.py` | **没动**（等 Task 8） |
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

---

## 4. 还没做完的（按动手顺序）

### 4.0 还没改代码的 Minor（不要在计划 3 里顺手做，除非标注了「极便宜」）

| # | 内容 | 建议 |
|---|---|---|
| M-3 | `provenance_of` / `strictest_trust` 不满足「公开签名需要它」 | 可扣留；**已导出再拿掉是破坏性变更**。不要在计划 3 里顺手拿掉 |
| M-4 | 端到端里 `status is WAITING` 调度余量恰好 1 跳 | 改成轮询到 WAITING 或超时；真正见证者是 `await_reply.states == [ANSWERED, ABORTED]` |
| M-5 | D-53 正文夸大了「静默漂移」 | 改登记表措辞即可 |
| M-6 | `send` 第二参 spec 叫 `message`、实现叫 `content`；`spawn.query` 收成 keyword-only | 登记为措辞差异，**不要改名** |
| M-7 | spec `AgentInfo.type` 实现叫 `agent_type`（避开内置名） | 登记即可，不要改回去 |
| M-8 | `WaitState.ABORTED` 自己的 docstring 仍只写「协作式中止落在等待上」 | 补枚举 docstring，把 D-26 契约写到宿主会读的那一行 |

### 4.1 下一步：执行计划 3 Task 6–8（8 个任务，5 完成）

**已关：** D-49、D-08。**不要动：** D-58（禁止 `Runtime.start` / `Junction.RUNTIME_START`）。

执行中途「这里该怎么写」一律回到计划文件的「设计裁决」节，不要现场再发明。下面是给下一任的压缩版；细节、必写用例名、变异点以计划文件为准。

#### 已经拍板、执行时不许改的裁决

1. **YAML 不进 kernel。** 入口是 `load_mapping` / `load_json_file` / `load_json_directory`。YAML → Mapping 是计划 6 / 宿主的事。测试用 dict 与 `.json`，不要 `yaml.safe_load`。
2. **类式 vs 声明式看 `impl`，不用 `class:`。** 缺省/None = 声明式；`type`（BaseAgent 子类）或 `str`（`pkg.mod:Class`）= 类式。误写 `class:` 走未知键。
3. **十个公开字段顺序永久承诺：** `name`, `description`, `system_prompt`, `tools`, `subagents`, `model`, `extends`, `skills`, `budget`, `artifact_kinds`。另加非十字段的 `impl`、`source`。未知键当场 `ConfigError`，一次列出**全部**非法键。
4. **`tools` 是名字列表，不是 Tool 映射。** D-49：`validate_tool_names` 先认 Mapping。D-08：登记时三条身份轴（dict 键 == `tool.name` == `schema['name']`）必须逐字相同，不要「按 schema 名改键」悄悄修好。
5. **同名覆盖可查询。** `OverrideRecord`；`overrides(name)` 从旧到新；同一次 `load_*` 内部撞名是 ConfigError，不是覆盖。
6. **插值白名单只有 `{os}` `{platform}` `{shell}`。** `{cwd}` 明确禁止。`HostFacts` 由宿主注入，内核不读 env / cwd。插值在 freeze 时一次，不在每轮。
7. **拓扑只增强、不替换。** 没有自由 `options: dict`。`keyed+eager` 与 `on-demand+eager` 禁止。`assemble` 不是 `start()`。第二次 assemble → ContractViolation。`send(agent_id)` 不收别名；别名走 `resolve` / `occupy` / `send_to_slot`。`find` 仍然绝不激活。
8. **四个内置：** Orchestrator 类式（自己装编排工具，即使 `Runtime(orchestration=False)`）；Planner / Explorer / Worker 声明式 JSON。文件工具点名即可，freeze 时目录里没有 → ConfigError；测试注入 stub。Worker 在计划 4 之前 freeze 失败是约束，不是缺陷。
9. **公开面加：** `AgentManifest`, `HostFacts`, `AgentCatalog`, `AgentSlot`, `Topology`, `OverrideRecord`, `ManifestSource`, `Orchestrator`, `register_builtins`；Runtime 加 `assemble` / `resolve` / `occupy` / `send_to_slot`。**构造器不变。** Task 1 先不要往 `kernel.__all__` 导，等 Task 8；新模块自己的 `__all__` 必须进 `_WITHHELD`，否则 `test_public_api` 变红。

**执行前默认：`AgentSlot` 不要 `extra_plugins` 字段。** 架构草图第 129 行还留着这个字段；**以自我审查 §7 为准**——Plugin 实例难冻，会膨胀公开面。插件仍只在 `Runtime(plugins=)`。增强面只留 `extra_tools` / `budget` / `metadata`。`send_to_slot` 计划正文（Task 6）**建议加**，加了就必须进 `_RUNTIME_SURFACE`；不要重载 `send` 的第一个参数当别名。

#### 八个任务（建议 8 个 commit，不要 push）

| Task | 标题 | 新建 / 改 | 关掉 | 状态 |
|---|---|---|---|---|
| **1** | `AgentManifest` + `parse_mapping`（不含工具解析） | `manifest.py`、`test_manifest.py`；`__init__.py` 先不要导出；`_WITHHELD` 扣三个名字 | — | **已 commit `97c9edc`** |
| **2** | `validate_tool_names` 认 Mapping + 名字列表→工具表 | 改 `tool.py`、`test_tool.py`；`catalog.py` 的 `resolve_tool_names` / `register_tool` | **D-49、D-08** | **已 commit `551b4d2`** |
| **3** | `AgentCatalog`：加载、覆盖可查询、`get(name)` | **改**已有 `catalog.py`、`test_catalog.py`（不要当空白重写） | — | **已 commit `7071eda`** |
| **4** | 插值 + `freeze` 成 `AgentFactory` | **改**已有 `catalog.py`、`test_catalog.py`；新建 `test_prompt_interpolation.py` | — | **已 commit `73289a2`** |
| **5** | `AgentSlot` / `Topology` 校验（还不接 Runtime） | `topology.py`、`test_topology.py` | — | **已 commit `cb81e02`** |
| **6** | `Runtime.assemble` / `resolve` / `occupy` / `send_to_slot` | `runtime.py`、`test_assemble.py`、`_RUNTIME_SURFACE` 加名字 | — | **下一刀** |
| **7** | 四个内置角色 | `src/syndica/kernel/agents/{orchestrator.py,planner.json,explorer.json,worker.json,__init__.py}`、`test_builtin_agents.py` | — | 未做 |
| **8** | 公开面收口 + 端到端一条拓扑 | `kernel/__init__.py`、`test_public_api.py`、`tests/integration/test_topology_lifecycle.py` | 把 Task 1 扣留的名字挪进承诺面 | 未做 |

每个任务：实现 → 受影响测试绿 → **自我变异**（先清 `__pycache__`；新文件先 `git add`；sha256 基线必须覆盖本轮会改到的每个文件，见 AGENTS.md）→ 独立评审自创变异 → 修复 → commit。受影响测试：

```
uv run pytest tests/kernel/test_<本任务>.py tests/kernel/test_public_api.py -q
```

再按改动加 `test_runtime.py` / `test_tool.py`。不要全仓 `make test`。

遗留项**同时**记进 [`deferred-items.md`](../decisions/2026-09-14-deferred-items.md)，不要只写 gitignored 的 `.superpowers/sdd/progress*.md`。

### 4.2 计划 4（未写未做）——计划 3 全部收口后再写

- `Workspace` / `Portal` + `LocalWorkspace`（本地目录 + 子进程，零依赖）
- 文件与执行工具：`Read` / `Write` / `Edit` / `Glob` / `Grep` / `Bash` + `BashOutput` / `KillBash`
- 第一个 backend 包：仅 OpenAI 兼容 HTTP（Anthropic 原生不在 M1）

挂单（动手前整表再读原文）：D-09、D-50、D-34、D-36、D-37、D-10、D-11、D-12、D-13、D-14、D-15、D-16、D-17、D-53、**D-56**、**D-57**。

D-56：**第一个 provider 必须在 `config.timeout` 内返回。不要在内核里包 `asyncio.timeout`。**

### 4.3 计划 5（未写未做）

Runtime Module + Flag（§5.5 四类注入）；重试 / 故障转移 Module（§5.7）；前缀漂移检测（§5.2）。

挂单：D-18、D-47（墙钟那一半）、D-19、D-20、D-21、D-22、D-23、D-29（`untrusted_high_risk` flag）、D-55（**`flags` 字段不要提前加**，没有读者会被当死代码删掉）。

### 4.4 计划 6（未写未做）

CLI（§9.1 名词+动词、默认 `run`、`chat` = `run --session`）；L2 声明式场景回放（§13.1）；`AskUser` / `AskUserQuestion` 与人工确认。YAML → Mapping 也在这里。

挂单：D-38（确认通道落地后，「需要授权的调用不与别人并发」必须重新成立）。

### 4.5 里程碑 1 收口审计（计划 6 之后）

spec §15 判据逐条取证 + 遗留项归属清空 + **那时再 push**（或按当时用户指示）。

挂单：D-51、D-27、D-58、D-59、V-01、V-02。D-52、D-49、D-08 已关。

### 4.6 明确不在里程碑 1

登记表「BEYOND-M1」整节：B-01 租约/孤儿回收、B-02 `wait_until_parked`、B-15 溯源链、D-40 只读视图嵌套、D-46 树深度、预算树属里程碑 8，等等。不要在计划 3–6 里顺手做。

里程碑 2 起（自举、可观测性、PTY、压缩、产物、RL、HTTP/React、MCP/ACP/A2A）见 spec §15。**M1 没完之前不要开。**

---

## 5. 登记表归属速查（里程碑 1 内仍 OPEN 的）

原文永远以 [`deferred-items.md`](../decisions/2026-09-14-deferred-items.md) 为准。这里只帮下一任定向，不要当权威。

| 归属 | 条目 |
|---|---|
| **计划 3（还没关的）** | 无（D-49、D-08 已关）。剩下的是 Task 6–8 的功能，不是登记表 OPEN 项 |
| **计划 4** | D-09、D-50、D-34、D-36、D-37、D-10–D-17、D-53、D-56、D-57 |
| **计划 5** | D-18、D-47（墙钟）、D-19–D-23、D-29、D-55 |
| **计划 6** | D-38 |
| **M1 收口审计** | D-51、D-27、D-58、D-59、V-01、V-02 |
| **已关、不要重开** | D-01…D-08、D-24–D-26、D-28、D-30–D-33、D-35、D-39、D-41、D-44、D-45、D-48、D-49、D-52、D-54、D-60 等（见登记表「已关闭」） |

---

## 6. 下一任的第一条命令

在 `/Users/bytedance/Documents/syndica`：

```bash
git status -sb          # 应是 main...origin/main [ahead 25]（本文 commit 之后），工作区干净
git log -3 --oneline    # 第一条应是本文；第二条 `cb81e02` Task 5
```

然后：

1. 打开 [`docs/plans/2026-09-21-m1p3-declarative-agents.md`](./2026-09-21-m1p3-declarative-agents.md)，从 **Task 6** 开始。不要重做 Task 1–5，不要先写计划 4，不要加 `Runtime.start`。
2. Task 6 必写用例名已经列在计划里。改 `src/syndica/kernel/runtime.py`，新建 `tests/kernel/test_assemble.py`。`assemble` / `resolve` / `occupy` / `send_to_slot` 进 `_RUNTIME_SURFACE`。`AgentSlot` / `Topology` 仍先留在 `_WITHHELD`，等 Task 8。
3. 每个任务走完变异 + 独立评审再 commit。**仍不要 push**，直到 M1 收口审计通过，或用户明确说推。

Task 6 压缩版（细节以计划 Task 6 节为准）：

- `assemble` 不是生命周期 `start`。没 assemble 的 Runtime 与今天一样。第二次 assemble → `ContractViolation`。
- 只 spawn `boot='eager'` 的 singleton。keyed / on-demand 的 eager 已在 Task 5 拒绝。
- `extra_tools` 只并上，不替换清单里已有的名字。`metadata` 深拷进 `spawn(metadata=)`；与 `artifact_kinds` 合并时键冲突 ConfigError（槽上已禁止这个键）。
- `find` 仍然绝不激活。
- 本机 bash **没有** `declare -A`，变异跑手用 Python。先清 `__pycache__`，sha256 还原（不要 `git checkout`）。新文件先 `git add` 再取基线。

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
| 公开面守卫 / `__init__` 签名 | `tests/kernel/test_public_api.py` |
| 装门（仅内核） | `BaseAgent._attach_gate`；测试走 `_agent(..., gate=)` |
| 多 Agent 取证 | `tests/integration/test_multi_agent_lifecycle.py` |
| 登记表 | `docs/decisions/2026-09-14-deferred-items.md` |
| 计划 2 原文 | `docs/plans/2026-09-08-m1p2-control-plane.md` |
| **计划 3（下一份要执行的）** | `docs/plans/2026-09-21-m1p3-declarative-agents.md` |
| 范围裁决 | `docs/decisions/2026-09-08-m1-scope.md` |
| 硬约束 / 变异纪律 | `AGENTS.md` |

计划 3 执行时会新建、今天**还不存在**的文件：

- `src/syndica/kernel/agents/`（`orchestrator.py` + 三份 JSON）
- `tests/kernel/test_assemble.py` / `test_builtin_agents.py`
- `tests/integration/test_topology_lifecycle.py`

已经存在、不要当空白重写：

- `src/syndica/kernel/manifest.py`
- `tests/kernel/test_manifest.py`
- `src/syndica/kernel/catalog.py`
- `tests/kernel/test_catalog.py`
- `tests/kernel/test_prompt_interpolation.py`
- `src/syndica/kernel/topology.py`
- `tests/kernel/test_topology.py`
