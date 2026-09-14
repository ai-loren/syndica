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

### 归计划 2 Task 11（工具执行接处置）

| # | 内容 |
|---|---|
| D-28 | **没有任何东西计算 `DangerRequest.provenance`。** spec §12.2 第 3 条的强制升级已在 `danger.disposition_of` 里结构性落地（高风险 + `UNTRUSTED` → 阻断，且不咨询 policy），但「这次调用的参数能不能溯源到不可信内容」还没有生产者，字段现在只有调用方显式传 `None`（= 没溯源过）这一种实际取值。Task 10 只钉了签名与升级本身：`DangerPolicy` 由第三方实现，方法签名发布后就改不动，而升级**逻辑**不在 spec §15 的里程碑 1 判据内。**Task 11 把处置接进 `ToolExecutor` 时必须一并算出 provenance 并传进去**；最小正确形态是「本轮上下文里出现过 `trust=UNTRUSTED` 的 block」，完整形态要接产物溯源（§7.3）与 trace 因果链（§11.1）。漏做的症状是那道升级恒不命中而调用方以为有它。**接进去时必须确认 `provenance` 不会以 `None` 长期存活**——见 D-32 |
| D-32 | **`provenance is None`（没溯源过）与 `TRUSTED`（溯源过、来源可信）在 `disposition_of` 出口上不可区分。** 模块内部两处「我不知道」的默认方向相反：`__tool_command_args__ = None` 判成「按最坏情况处理」，`provenance = None` 判成「按最好情况处理」。当前语义有理由（冒充一份没做过的检查等于谎报），且 AGENTS.md「不确定性必须显式」那节的处方是「放行，但**显式标记**未验收」——现在只有放行、没有标记。已加用例把「两者产出同一个 `Disposition`」钉成当前行为，于是改动它是一次自觉的改动而非漂移。**Task 11 接上 provenance 生产者之后，`None` 就只应出现在「还没走到溯源那一步」的调用上**；若它在生产路径上长期存活，就要么给它一条显式的未验收标记、要么把默认方向改成从严 |
| D-33 | **spec §12.2 处置矩阵里「低风险 + 可溯源到不可信 → 放行并记录」的「记录」没有落点。** `danger.py` 全文无 logging，而判定是纯函数、不该持有日志。落点在 Task 11 的 executor：它是唯一同时拿得到 verdict、provenance 与 trace 的地方。漏做的症状是矩阵里这一格与「低 + 不可溯源 → 放行」逐字节相同，那条记录义务无声消失 |

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
| D-34 | **每一个内置工具都必须显式声明 `__tool_command_args__`。** 计划 4 加的文件读写与命令执行工具正是这条纪律的第一批用户：执行类工具漏声明会让判定改去扫它的全部参数（保证更弱且会对文本参数误报），而 `Read` / `Write` / `Grep` 这类收文本的工具漏声明会被自己写的文档内容命中——一段含 markdown 表格或围栏代码块的正文里一个裸 `|` 就能拼出假管道链。判定已在结论里点名「这个工具没声明」并给出修法，但那是**事后**诊断；纪律在计划 4 建立，成本是每个工具一行 |
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
| D-29 | **`untrusted_high_risk` flag 未实现**。spec §12.2 的处置矩阵把「高风险 + 可溯源到不可信」交给这条 flag（`reject` 默认 / `ask` / `allow`），而 `danger.disposition_of` 现在把 `reject` 写死。写死的是**默认值**，不是能力边界：`disposition_of` 是内核自己的函数（第三方只调不实现），加一个带默认值的参数对任何调用方都不破坏。做这条时一并给 `ask` 找落点——`Disposition.CONFIRM` 的交互通道在计划 6 |

### 归计划 2 Task 16（公开面收口）

| # | 内容 |
|---|---|
| D-24 | **`waiting` 的六个名字尚未进 `kernel.__all__`**（`Waiter` / `UserInput` / `Confirmation` / `Reply` / `WaitResult` / `WaitState`）。后果不只是导出面不全——只用公开包面的人**调不了** `Runtime.wait_for`（构造不出入参）。计划 Task 16 的 59 项清单已含它们，但 `test_public_api.py` 逐字写死，忘了改**不会**变红 |
| D-25 | `__all__` 内容全仓无测试覆盖（子模块级）。计划 1 的 Task 4 与 Task 9 各挂过一次，是同一条。`kernel.__all__` 有守卫，子模块没有 |
| D-26 | **`ABORTING` 分支扩大了 `wait_for` 的成功面**：`kill` 之后（`_reap` 未跑完那段）调 `wait_for` 现在交回 `ABORTED` 而不再拒绝。计划 2 Task 9 判断这是对的，但它是新行为 |
| D-30 | **`danger` 的公开名字尚未进 `kernel.__all__`**（`RiskLevel` / `Disposition` / `DangerVerdict` / `DangerRule` / `DEFAULT_RULES` / `NO_RULE_MATCHED` / `DangerJudge` / `DangerRequest` / `DangerPolicy` / `BlockHighRisk` / `AllowAll` / `disposition_of`）。与 D-24 同型，但 Task 16 的 59 项清单成文于 `danger` 之前、**不含**这些名字；只用公开包面的宿主因此写不出自己的 `DangerPolicy`。`test_public_api.py` 逐字写死，漏掉它们**不会**变红 |

### 归里程碑 1 收口审计

| # | 内容 |
|---|---|
| D-31 | **`disposition_of` 是强制升级的唯一收口，而绕过它直接调 `DangerPolicy.decide` 就绕过了这一条** —— typing 看不见、运行期不警告，只有 docstring 写着。与 D-07 同型（契约只在文档里失败）。内核自己的唯一调用点在 Task 11 的 executor；发布前确认没有第二条路径，并考虑这条约束要不要有机器守卫 |
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
| B-08 | 危险规则表不认「经中间工具转手执行」（`xargs ... sh -c`、`find -exec`）与「下载落盘后再单独执行」（`curl -o f; sh f`） | 规则表不完备是**声明过的**（`RiskLevel.NONE` 是「没有规则命中」而非「已确认安全」），这两类是**另外的形态**而不是同一形态的平凡改写——后者（多一段管道 / 标志换位置 / 加一对引号 / 带路径前缀）必须命中且已由用例钉住。补这两类要么按语义解析 `sh -c` 的实参、要么跨命令跟踪文件，代价远高于它挡住的那部分；当前边界写在 `danger` 模块 docstring 里并各有一条用例钉住，不会悄悄漂移 |
| B-09 | **未声明 `__tool_command_args__` 的工具，读不出文本的参数值（自定义对象）仍然静默跳过** | 声明过的那条路上已经响亮失败（`ContractViolation`）。未声明这条路按定义就是「不知道哪些参数会被执行」，在它每一个 `int` 参数上抛异常会让整批工具用不了；强化它的正解是让工具**声明**，而不是把猜测路径也变成报错路径。两条路的保证强弱差异写在 `Tool.__tool_command_args__` 的声明处。**读不「完」不在此列**：深度 / 总量超限在两条路上都响亮失败，那是规模问题而非类型问题 |
| B-10 | **未声明 `__tool_command_args__` 的工具，文本类参数会被散文误报。** 除反引号外至少还有三条来路：围栏代码块、markdown 表格（`\| sh \| 执行脚本 \|` 拼得出假管道链）、中文散文里一个裸 `\|`。本仓的 spec 与 AGENTS.md 本身全是 markdown 表格 | 不采纳「未声明路只跑保守子集规则」：那与「未声明 = 按最坏情况处理」这条已声明的立场方向相反，而立场本身是对的——一个真会执行东西却忘了声明的工具，正是最需要被检查的那个。采纳的缓解是**让诊断说清怎么修**：未声明路产出的结论里点名「这个工具没有声明 `__tool_command_args__`；若它什么都不执行，声明 `()`」，于是误报的代价从「下游把整套闸关掉」降到「给这个工具加一行声明」。纪律见 D-34 |
| B-11 | **`hash(DangerRequest)` 抛 `TypeError`**（frozen 却不可哈希，根因是 `ToolCall` 可变） | 与 B-03 / B-04 完全同型，按 B-04 的先例处置：失败是响亮的，已写进 `DangerRequest` docstring（别当 dict 键，要稳定身份就按 `call.id` 取） |
| B-12 | **不带 `functools.wraps` 的装饰器让 `execute` 的签名塌成 `(*a, **k)`，于是 `VAR_KEYWORD` 豁免落在一个本来有名单的工具上**，拼错的参数名照样被接受、那条声明静默落空 | 与 `**kwargs` 本身那个洞同源（换了个入口），已写进 `_declared_command_args` 的 docstring。堵它要么禁止无 `wraps` 的装饰器（管不了第三方），要么放弃 `**kwargs` 豁免（会让合法的 kwargs 工具全部报错）；两条都比它挡住的那部分贵 |
| B-07 | 关联 ID 是一个扁平命名空间（`tool_call_id` 由模型厂商生成、`correlation_id` 由 `Runtime.send` 写入，都不带前缀），一条对派活的回复原则上能解掉一次工具确认的等待 | 计划 2 Task 9 裁决**不加前缀**并写进 docstring：加前缀要求发答复的那一侧用同一套编码，两侧不一致的症状是答复躺在邮箱里、等待跑满超时、零日志，比当前无已知来源的碰撞风险更难查；而 `Confirmation` 等的是模型厂商生成的 ID，前缀会变成必须对外公开的编码契约 |
