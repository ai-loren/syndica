# 里程碑 6 收口审计：状态（会话、压缩、记忆）

日期：2026-10-05
范围：[M6 计划](../plans/2026-10-05-m6-state.md) 的五个阶段（会话持久化、恢复、§8.8 上下文压缩、§8.9 分层记忆、接线）
基线：存储与脱敏收口 `1b68ff5`、文件 store `1286c91`、存盘 `60163b1`、署名历史块 `b4e7870`、阶段一评审修复 `8f80b78` /
`cbcc366` / `c2afbe1`、恢复 `7f5b305`、CLI 会话 `52fb35f` / `60d96b2`、阶段二评审修复 `7dda947`、压缩契约 `cb661d8`、
三策略 `ab30f55`、内核压缩步骤 `4783015`、压缩 Module `92a7f03`、阶段三评审加固 `d094e6f`、记忆存储 `b02390d`、
记忆 Module `e219c0e`、`FileMemoryStore` `382828c`、接线 `dd01e43`、LLM 整理与一次性调用 `6733d70`、真实运行与提示收紧
`a17f547`；收口评审的修复与本文另行提交
验证方式：本地验证协议（Actions 额度用完期间，见路线图「当前主线约束」）

## 结论

spec §15 的完成判据——**会话持久化与恢复、上下文压缩（§8.8）、分层记忆（§8.9）**——由一套端到端用例（经公开 API 与
`syndica` 命令行、脚本化模型，随全量测试零成本地跑）与一次真实运行共同证明。范围按计划 P14 收窄为**只持久化根对话**：
多 Agent 子树的持久化与恢复不在 M6。压缩的四种策略交付三种（规则型、剪枝型、摘要型），目标导向型按 spec §15 依赖 M7。

- 会话：`SessionStore` 是公开协议、`SessionStoreContract` 随 wheel 发布，内存与文件实现都过。Runtime 在周期收尾与关机时存盘；
  `ensure_active` 从记录重建根 Agent。CLI `sessions list / show / resume / rm` 与 `run --session` 可用，`flock` 会话锁挡住跨进程
  并发改同一会话。
- 压缩：内核压缩步骤在 turn 开始（阈值 / 主动）与 `CONTEXT_OVERFLOW` 兜底时跑；五条不变量与信任由 `apply_compaction` 对内核
  私有的深拷快照校验，策略就地改入参洗不白。`CompactionModule` 默认关，配置里启用。
- 记忆：`MemoryStore` 协议 + 内存 / 文件实现 + 契约；「不可信不进全局」在脱敏铸造处强制；`Remember` 工具只让根协调者写全局；
  阈值触发 LLM 整理；cycle 起点按幂等键缓存召回（进历史不进 SP）。`MemoryModule` 默认关，配置里启用。
- 真实运行（OpenRouter `:free` 模型，2026-10-05）：仓库外临时工作区，`modules: [compaction, memory]`、`memory_consolidate_after: 1`，
  信任存储指到临时文件。模型答 `DONE`、**真的调了 `Remember`**；短期条目落盘，阈值触发一次 LLM 整理（真模型的一次性调用）写出
  长期记忆；会话与 trace 记下。key 只在子壳里 source，壳外 `env | grep -c ...KEY` 全程为 0。

## 完成判据 × 证据

| 判据（路线图 M6） | L1/L2（脚本化，零成本，随全量测试） | 真实模型（仓库之外的临时工作区） |
|---|---|---|
| Store 协议 + 契约（顺序、CAS、损坏数据、并发写） | `SessionStoreContract`、`MemoryStoreContract` 各对内存与文件实现全过；并发基数 3；fail-closed 反序列化用例 | 会话、记忆都经文件 store 落盘 |
| 所有 session / memory / trace 写入先过 redactor | `RedactedSessionRecord` / `RedactedMemoryEntry` 等只由脱敏函数铸造；嵌套多模态文本逐叶脱敏用例 | 落盘记录与 trace 里没有 key |
| 恢复后 Runtime 行为一致 | `test_session_resume`：原样种回、未配对批次标「副作用未知」、sid 不符则拆掉、store 异常答 False | 会话落 `.syndica/sessions/<id>` |
| session list / show / resume / rm CLI | `tests/cli/test_sessions.py`：落盘、列出、续跑、`run --session`、删除、遇锁拒删 | — |
| 压缩三基线 + 五条不变量 | `test_compaction`：配对、protected、递增子序列、衍生 system 被拒、信任置于权威快照 | `compaction` 模块装上、run 正常收尾 |
| 压缩前快照、压缩点 trace、缓存键重建 | 压缩点 trace + 事件；`on_compacted` 让 prefix-drift 忘基线（不报漂移）的用例 | — |
| 压缩三触发 | `test_compaction_step`：阈值（turn 开始）、超限兜底（严格缩小、终止不死循环）、主动 `Compact` | — |
| 两层记忆、只有根协调者写全局 | `test_memory_module`：spawn-parent 的工人与 is_root 拓扑的工人写全局都被拒；工作区隔离；不可信进全局在铸造处被拒；坏文件不崩整次 run | `Remember` 写进工作区层 |
| 记忆注入历史、只在新会话 / 压缩后重载 | 召回幂等键用例（同一 Context 召回两次只注一块） | — |
| 接进 CLI | `tests/cli/test_memory_wiring.py`：`modules: [memory]` 一次 `Remember` 落盘；`modules: [compaction]` 正常收尾 | 同左，真模型 |

## 计划之外的偏差

- **只持久化根对话（P14）。** 路线图 E2E「多 Agent 任务中途停止再恢复」的子树恢复语义被取代：持久化整棵子树要连 broker 队列、
  租约与子 Agent 状态一起恢复，M6 不做。B-01 因此不需要基于租约的孤儿回收（改由 `flock` 会话锁挡并发，见遗留表）。
- **「轮」按 assistant 回复切**，不只按 user 消息：否则单条 query 的多轮工具调用整段只算一轮，规则型压缩压不动（P29）。
- **阈值触发挪到 turn 开始**（阶段三评审 H3）：放在 turn 末尾时，以文字回复结束的轮（消息驱动的 Agent 每轮都是）永远到不了。
- **记忆整理改 LLM 结构化**（用户决策，P32 / P33）：原计划是规则型折叠。真实运行发现 LLM 整理会**添油加醋**（源条目只有
  「用 tab 缩进」，整理出 `tabWidth: 1` 与编辑器建议，且按源条目信任记为 trusted）；整理与摘要的系统提示改为只保留源里明确
  写出的事实，同一任务重跑只剩源事实（`a17f547`）。
- **一次性辅助调用的回放降级。** 摘要压缩与 LLM 记忆整理的一次性模型调用走 `_aux_llm_call`（负 turn 号的 `llm_call`
  作用域），ReplayCapture 把不在任何一轮里的调用当 gap：这类 run 结构照常记录、不写回放文档并记一条 warning。改动前摘要
  压缩在回放里是直接报错，现在是优雅降级（P33）。

## 公开契约核对

- `syndica.kernel.__all__` 新增会话的 10 个名字：`SessionStore`、`InMemorySessionStore`、`FileSessionStore`、`SessionRecord`、
  `SessionMeta`、`SessionStatus`、`RedactedSessionRecord`、`RedactedContent`、`redacted_record`、`redacted_content`；
  `syndica.kernel.contracts.__all__` 新增 `SessionStoreContract`。
- 压缩、记忆、署名历史块与 `encode_assured` 等**全部扣留**（`_WITHHELD`），只经子模块可达、不是兼容承诺：先私有、有第三方要再
  放出来。CLI 经子模块用 `CompactionModule` / `MemoryModule` / `FileMemoryStore`。
- `docs/reference/flags.md` 重新生成，新增 `compaction_*` 四条与 `memory_*` 两条 Flag，金标同步。

## 遗留

| 条目 | 归属 | 依据 |
|---|---|---|
| 压缩前**完整前像**落 `SessionStore.archive`（spec §8.8「压缩前必须留快照」的完整形态） | M8 | 压缩点已进 trace、被丢的不可信内容有 UNTRUSTED 标记兜溯源；完整前像要把会话 store 递进 Agent 的压缩步骤，随 M8 长程任务的回看需求一起做 |
| 一次性辅助调用的完整回放（回放文档 v4 的 aux_calls 槽） | 后续（回放文档 v4） | 与 M5 收口 C3 同属回放文档 v4；要动 ReplayCapture、文档 schema、回放读者与版本门；两类调用都 opt-in，降级有日志 |
| 目标导向型压缩 | M7 | 依赖 Rubric（spec §15） |
| `MemoryStore` / `MemoryStoreContract` 进公开面 | M12 | 先私有；`SessionStoreContract` 已发布而记忆契约未发布，第三方记忆后端暂无公开契约可照着自证 |
| B-22 后代的单一定义 | M8 | 只持久化根对话之后 M6 不再依赖它，随多 Agent 长程任务（子树管理）做 |
| D-64 模型请求一侧的收紧（M5 后续） | M7 | 压缩信任由内核在 `apply_compaction` 里置，不依赖它 |
| 阶段二评审推迟项 M3 / M7 / L2–L4 / L6 | 见计划 P28 | 角色键与 `options.name`、恢复时安全 flags 取更严、`_close_sessions` 首个失败即中断等，各附理由 |
| 阶段一评审推迟项 F13（`RedactedSessionRecord` 可变 + AST 守卫未覆盖）/ F14（收尾存盘同步做）/ F17-复活态 | 见计划 P27 | 收口评审复核仍未做，原理由不变（扩守卫风险、`to_thread` 优化、自纠低危） |
| 恢复时安全 flags 取「当前或更严」（阶段二 P28-M7） | M7 | `record.flags` 恢复时未合并；与 M7 的溯源 / 授权一起做，收口评审重新点名了它 |
| CLI 里「全局」记忆存在各项目的 `.syndica/memory` 内（收口评审 M1） | M12 | 配置信任确认不覆盖记忆文件：clone 进来的仓库里的记忆也会当可信加载；跨项目的真全局记忆位置随 v1 发布定 |
| 一条不可信工作区记忆永久影响此后每个会话、且无法删（收口评审 M4） | M7 | 记忆没有「忘记 / 撤回」入口；与 M7 的验收 / 溯源一起给记忆加来源与撤回 |
| `short.json` 永不裁剪，到 64 MiB 后 store 停摆（收口评审 M5） | M8 | 计划里写了裁剪、实现未做；长跑的垃圾回收随 M8 长程任务做 |
| 记忆注入让回放发散（收口评审 M2）：回放用空 store 重建 Module，早先注入过长期记忆的 run 回放不出 | 后续（回放文档 v4） | 与上面 aux_calls 同属 v4；回放从不写真实记忆目录，是安全的发散 |

## 评审与变异

| 评审 | 结论 | 处置 |
|---|---|---|
| 计划评审 | 4 Blocking / 12 High / 8 Medium / 1 Low | 全部折进计划（P1–P25） |
| 阶段一（会话 store / 脱敏收口） | 6 High + 若干 Medium（17 条） | `8f80b78` / `cbcc366` / `c2afbe1`：保真序列化、fail-closed、必给 redactor、关机存盘读终态、嵌套多模态逐叶脱敏等；F13 / F14 / F17-复活态推迟 |
| 阶段二（恢复 / CLI） | 5 High / 8 Medium / 6 Low | `7dda947`：未校验 sid 拼锁路径可删工作区外文件（H1）、锁只挡恢复对恢复（H2）、pid 锁竞态改 `flock`（H3）、恢复截掉未配对批次（H4）、`--no-write` 恢复后升权（H5）等 |
| 阶段三（压缩） | 4 High / 6 Medium | `d094e6f`：策略就地改入参洗白信任（H1）、超限兜底死循环 / 空转（H2 / H4）、阈值对消息驱动 Agent 不触发（H3）、退避每轮失效缓存（M1）、当前周期输入被插话顶走（M2）、kept 乱序 / 重复 / 衍生 system（M3） |
| 收口（整个 M6） | 3 High / 6 Medium / 8 Low | 本次提交，见下 |

**收口评审（3 High 已修）。** 后台对抗评审（私有快照，报告 `/tmp/syndica-review-m6/`）：

- **H1（已修）** 文件工具的禁写名单 `cli/host._PRIVATE` 漏了 `.syndica/sessions` 与 `.syndica/memory`（shell 侧的
  `danger._HOST_RECORDS` 两个都有）。于是 Agent 读了恶意网页后能用 `Write` 造一份
  `.syndica/memory/global/long.json`（`"trust": "trusted"`），store 信磁盘上的内容，此后每个会话都把它当可信记忆注入；
  同一个洞也能改写落盘的会话洗白 provenance——正是阶段一 F11 本该堵上的。补齐两个目录，补回归用例（两目录各一条，变异验证）。
- **H2（已修）** 记忆整理的一次性调用没有重试与失败兜底，超窗 / 坏回复属于 tool executor 会上抛的错族：整理一失败，
  条目已存、整次 run 随即失败；整理输入无上限、积压只增，超窗之后该工作区此后每条 `Remember` 都崩。改为整理失败只记一条
  ERROR、条目留在短期、下次再整理，输入按 `_CONSOLIDATE_BATCH` 分批、每条截断；`ContractViolation`（真违反不变量）仍响亮上抛。
- **H3（已修）** 「根」按 `parent_id is None` 判，而拓扑的槽 Agent 互为对等、都没有 parent，于是 `--topology` run 里每个槽都算
  根、工人都能写全局。加 `Runtime._is_root_coordinator`：装了拓扑按 is_root 槽的占用者判，否则按 `parent_id`。补拓扑用例。
- **Medium / Low（记入遗留，未在收口改）**：见下表。M3（召回 / 记录遇坏文件不崩）已随 H2 一并加了 try/catch。

**变异的诚实交代。** 本里程碑没有像 M5 那样对每个阶段都跑系统变异轮。变异覆盖来自四轮后台对抗评审
（含收口评审）各自的「哪一处改动不会让任何用例变红」分析，每条被点名的存活变异都补了单独施加即变红的用例。
收口评审的三个 High 修复各做了一轮「sha256 先校验、施加反向变异、清字节码、跑用例确认变红、还原、再按 sha256
校验」：H1（禁写名单去掉新目录）、H2（把宽 catch 收窄到 `ValueError` 让超窗上抛）、H3（把根判定退回只看
`parent_id`）三条反向变异都让对应用例变红、三份源文件都按 sha256 还原无误。阶段一 / 二 / 三的收口前没有再独立跑系统
变异轮，这是本里程碑相对 M5 的已知短板，记在这里而不是假装做过。

## 验证

```text
uv run ruff check .                                          通过
uv run ruff format --check .                                 通过
uv run pyright                                               0 errors
uv run python scripts/check_deps.py                          通过
uv run python scripts/check_links.py                         通过
uv run python scripts/gen_flag_reference.py --check          通过
uv run pytest -q                                             8900 passed
```

推送前对提交的树做导出验证（把这次提交单独检出到临时目录、在那里跑上面每一道门禁）。
