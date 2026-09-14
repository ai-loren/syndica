# 遗留项登记（里程碑 1）

每一条要么**有归属**（写明落在哪个计划 / 任务），要么**已关闭**（写明证据）。

存在的理由：执行台账（`.superpowers/sdd/progress*.md`）是 gitignored 的工作文件，挂在里面的遗留项
对别人不可见。计划 1 有八个任务的「交最终整枝 triage」条目就是这么丢的——计划 1 结束在 Task 14，
整枝小节从未出现，于是那些条目至今无人处置。**新增遗留项必须同时记进本文件**，台账只记推理过程。

状态取值：`OPEN`（有主待做）· `CLOSED`（已关闭，附证据）· `BEYOND-M1`（明确不在里程碑 1，附理由）
· `VERIFY`（认为已被后续改动关闭，里程碑 1 收口审计时取证确认）

---

## 已关闭

| # | 内容 | 证据 |
|---|---|---|
| D-01 | `unregister` 有子代的父留下悬空 `parent_id`（自计划 1 起**无主**） | 计划 2 Task 7：级联无条件，`_dangling_parents` 助手断言注册表里不存在悬空条目。三条拆除路径（`kill` / `despawn` / `ExitMode.EXIT`）合并成 `_destroy_tree` 一条顺序 |
| D-02 | `AgentInfo` 加一个未声明字段不会让任何测试变红 | 判为非隐患：加字段是增量变更，不构成静默错误。`AgentInfo` 的必填字段已由用例钉住 |
| D-03 | 函数局部定义的 `SyndicaError` 子类不被 copy/pickle 覆盖机制发现 | 按构造即不可 pickle，无现实风险 |
| D-04 | 取消（`CancelledError`）会留下 block 数少于 call 数的轨迹 | 设计如此且在该处无法关闭（取消可能落在进入工具之前）。已在 `tool_executor` docstring 记明 resume 需修补，计划 1 Task 13 给「这份 Context 在轮中被打断」找到了落点（`extra['interrupted']`） |
| D-05 | `_resume_one` 的读-改窗口：注册表短暂停在 `WAITING` 而槽里无 waiter | 计划 2 Task 9 明确留着，理由写进 `_resumed_status` docstring：自愈路径是下一个轮边界的 `_oscillate`（`allowed` 含 `WAITING`），而修法自己更糟——「CAS 后复检 waiter、没了就改 RUNNING」有同样窗口，会把刚装上的新等待抹成 RUNNING |

## 待审计核实（认为已被后续改动关闭）

| # | 内容 | 认为关闭它的改动 |
|---|---|---|
| V-01 | `ERROR_CLASSES` 是手工维护列表，跨模块子类（`llm.ContextOverflowError`）覆盖不到 | 计划 1 Task 10 改为从文件树独立推导期望值、动态收集 `SyndicaError` 子类 |
| V-02 | 接管 CAS 在 `parked` 状态下不是有效的所有权守卫 | 计划 2 Task 4 的 `ensure_active`；Task 7 进一步改为**用注册表状态作所有权凭证** |

## 有主待做（里程碑 1 内）

### 归计划 2 Task 13（三层装配 —— hooks 层）

| # | 内容 |
|---|---|
| D-06 | **串联 POST hook 会把中间的 `UNTRUSTED` 标记从 executor 眼前藏掉。** `HooksCenter` 的合并语义只交出最后一个 `HookResult`，前一个 hook 的收紧因此丢失。这是信任单调性（只收紧不放松）这条硬约束的一个真实缺口，**正确修法在 hooks 层**，不在 executor |
| D-07 | 可观察切点契约只在文档里失败：typing 看不见、运行期不警告，且两条既有用例把「观察者的返回值被忽略」当成了预期 |

### 归计划 3（声明式 Agent 定义 —— 工具注册层）

| # | 内容 |
|---|---|
| D-08 | 手写 schema 的 `name` 键可与 `tool.name` 分叉（模型按前者调用、注册表按后者索引）。当前行为已钉住，但守卫属**注册层**——声明式清单会从 manifest 构造 tools dict，那里是唯一收口点 |

### 归计划 4（第一个 backend + provider）

| # | 内容 |
|---|---|
| D-09 | **依赖规则 3 只实现一半**：`backend 不得 import surface` 未覆盖。原始条目自带期限——「第一个 backend 落地前须补」，而计划 4 就是它 |
| D-10 | `__tool_model__` 层输出带 `title` / `$defs` / `$ref`，部分 provider API 会拒。要知道目标 API 约束才能定，属 provider backend 的事 |
| D-11 | **`remove_queue` 的回执窗口**（计划 2 Task 4 的 Defect B）。不是「无法关闭」，而是「完整修法要改 `MessageBroker` Protocol，Task 4 选择不改」。评审已用一个在 `remove_queue` 内挂起的 broker 证明窗口真实存在（`ok=True`、消息消失、零 ERROR），且该 broker 就是 Protocol 修法的测试手段。计划 4 要动 broker 契约，一并做 |
| D-12 | 契约套件缺一条「`timeout` 从调用那一刻起算」的用例——计划 2 Task 9 的 `_report_unwaited_timeout` 判据建立在它上面，而它现在只是契约里的一句文字 |
| D-13 | `scripts/check_deps.py` 的 `encoding='utf-8'` 无自动化保护（删掉它套件仍全绿） |
| D-14 | `check_deps` 退出码 2（「没查成」）与 1（「查出违规」）的区分无测试钉住，而它的诊断依赖这个区分 |
| D-15 | `check_deps` 错误三件套的第三部分（`next_action`）未被任何断言覆盖。**计划 2 Task 8 已把这条立成仓库规则**（AGENTS.md「错误消息」节：三件套里最容易整条零断言的就是第三件），所以这是一条已知规则的存量违反 |
| D-16 | `tests/test_deps.py` 里 `args.py` 的 `write_text` 多余（校验器纯静态扫描，不看磁盘） |
| D-17 | `pyproject.toml` 的 pyright 注释后半句带「为什么没那样写」的反事实色彩，违反 AGENTS.md 注释纪律 |

### 归计划 5（Runtime Module + Flag）

| # | 内容 |
|---|---|
| D-18 | **宿主级等待上限（Flag）**。spec §4.2 只说「每个 `Waiter` 带自己的上限」，全文无宿主级 clamp 的取值依据，于是 `1e308` 秒仍是合法输入、实质是永久挂起。落点是收下 Waiter 的那一层读一条 Flag 做 clamp 或拒绝 |
| D-19 | `errors._validated` 写死 `'SyndicaError'` 类名 → `ConfigError(expected=None)` 会报「SyndicaError 需要 str…」，三件套的 `expected` 一栏指向错的类。**计划 2 Task 8 已把这条立成仓库规则**（AGENTS.md：类名由构造中的那个类给出，不写死基类名），存量违反，一行可修 |
| D-20 | `AgentStatus` / `ExitMode` 加 `str` mixin 不会让任何测试变红。计划 1 判为「推测性」，但**计划 2 Task 8 已把 `WaitState(str, Enum)` 钉成必须变红**（`str` 混入会让 `WaitState.TIMEOUT == 'timeout'` 为真、掩盖错误），于是这两个枚举现在与更新的标准不一致 |
| D-21 | 「子类新增必填 kwarg 须自行覆写 `__reduce__`」这条约束只在报告里，未落进代码注释——按本仓「注释只写代码表达不了的硬约束」的规矩，它正该是一行注释 |
| D-22 | `errors` 的类型错误抛 `ValueError` 而非 `TypeError`，调用方无法区分「类型不对」与「内容为空」 |
| D-23 | 多字段同时非法时先点名谁，无用例钉住 |

### 归计划 2 Task 16（公开面收口）

| # | 内容 |
|---|---|
| D-24 | **`waiting` 的六个名字尚未进 `kernel.__all__`**（`Waiter` / `UserInput` / `Confirmation` / `Reply` / `WaitResult` / `WaitState`）。后果不只是导出面不全——只用公开包面的人**调不了** `Runtime.wait_for`（构造不出入参）。计划 Task 16 的 59 项清单已含它们，但 `test_public_api.py` 逐字写死，忘了改**不会**变红 |
| D-25 | `__all__` 内容全仓无测试覆盖（子模块级）。计划 1 的 Task 4 与 Task 9 各挂过一次，是同一条。`kernel.__all__` 有守卫，子模块没有 |
| D-26 | **`ABORTING` 分支扩大了 `wait_for` 的成功面**：`kill` 之后（`_reap` 未跑完那段）调 `wait_for` 现在交回 `ABORTED` 而不再拒绝。计划 2 Task 9 判断这是对的，但它是新行为 |

### 归里程碑 1 收口审计

| # | 内容 |
|---|---|
| D-27 | **三条「唯一证据型」ERROR**（`_report_abandoned_input` / `_report_unwaited_timeout` / `_report_discarded_take_failure`）都断 `record.args`，但共同依赖「root logger 默认 WARNING」这个前提；宿主把级别调高则三条一起静默。这是既有模式，发布前确认一次 |

## 明确不在里程碑 1（BEYOND-M1）

| # | 内容 | 理由 |
|---|---|---|
| B-01 | 租约字段只能经无条件 `update` 写，真做孤儿回收需要原子操作 | 孤儿回收不在 spec §15 的里程碑 1 判据内 |
| B-02 | **`ControlBlock.wait_until_parked` 有与计划 2 Task 9 的 C1 同形的洞**（`asyncio.wait_for` 会吞掉到达的取消） | 三条依据：(a) 它**没有生产调用方**，只在测试里用；(b) `ControlBlock` 不在 `kernel.__all__`、也不在 Task 16 的 59 项导出清单里，不是公开面；(c) 直接换 `asyncio.timeout` 会把 3.11 的窗口压成 0 槽，让计划 1 Task 1 那条复检守卫**再没有任何变异能证明它承重**——正是 AGENTS.md 新增的「只能被联合变异证明的守卫」那类退化。**正解**：先把那条用例改成自己确定性地构造窗口（不再依赖 `wait_for` 的实现细节），再换实现。与 Task 1 那条裁决一起重裁 |
| B-03 | `LLMConfig` 含 dict 故 `hash()` 会抛（frozen 却不可哈希） | 失败是响亮的。等真有缓存需求时再加稳定身份访问器 |
| B-04 | `WaitResult` 的成功路径不可哈希（`hash(WaitResult(ANSWERED, msg))` 抛） | 根因是 `Message` 可变，归 `message.py` 的裁决。已写进 `WaitResult` docstring（不可哈希恰好落在成功路径上，别当 dict 键） |
| B-05 | `__cause__` / `__context__` / `__traceback__` 不能活过 copy/pickle 往返 | C 级槽位不在 `__dict__`，与标准库一致、非本仓回归 |
| B-06 | `ConfigError` 被借用于「调用方误用」（`register` / `wait_for` 同型借用） | 是否给 `errors.py` 加专门成员是跨点改动，等第三个站点出现再定，不在单点发明 |
| B-07 | 关联 ID 是一个扁平命名空间（`tool_call_id` 由模型厂商生成、`correlation_id` 由 `Runtime.send` 写入，都不带前缀），一条对派活的回复原则上能解掉一次工具确认的等待 | 计划 2 Task 9 裁决**不加前缀**并写进 docstring：加前缀要求发答复的那一侧用同一套编码，两侧不一致的症状是答复躺在邮箱里、等待跑满超时、零日志，比当前无已知来源的碰撞风险更难查；而 `Confirmation` 等的是模型厂商生成的 ID，前缀会变成必须对外公开的编码契约 |
