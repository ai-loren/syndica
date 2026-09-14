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
| B-16 | **同一段并发调用之间，`PRE_TOOL_USE` 改写参数时的溯源重取看不到兄弟的产出**（原判 BEYOND-M1，理由是「修法就是不并发」） | **判错了，修法在本层且很小**：`is_concurrency_safe` 的语义是「本次调用不写任何共享状态」，不是「我的输出不会经 hook 进到兄弟的参数里」，所以两个**按契约正确声明**的工具就够触发，缺口不被并发的前提框住。多元素段里那次重取**结构上就是一次失败的溯源**（兄弟的证据还没落进轨迹），而 spec §13 已经规定溯源失败时按最坏情况处理。计划 2 Task 12 修复轮：`_execute_one` 收一个必填的 `has_siblings`（由 `_run_segment` 从决定要不要进 `gather` 的同一个 `len(calls)` 判断算出，漏传是 pyright 错误），hook 改写过参数且本段有兄弟时直接取 `TrustLevel.UNTRUSTED`。两种形态各有能单独变红的用例，且都按 `limit` ∈ {1, 4} 参数化——**同一批输入在 `limit=1` 与 `limit=N` 下必须得出同一个安全结论**；另有一条断言同一批调用在两个上限下落下逐块相同的轨迹。作用面只在「hook 改写参数 ∧ 多元素段」这个交集上，**默认路径零成本**：`src/` 里今天一个 `Tool` 子类都没有、零工具声明可并发，线上行为一字未变；它的对象是交给第三方的契约（计划 4 的文件读写 / 命令执行工具落地时就活）。多判的那一侧登记为 B-18 |
| D-04 | 取消（`CancelledError`）会留下 block 数少于 call 数的轨迹 | 设计如此且在该处无法关闭（取消可能落在进入工具之前）。已在 `tool_executor` docstring 记明 resume 需修补，计划 1 Task 13 给「这份 Context 在轮中被打断」找到了落点（`extra['interrupted']`）。计划 2 Task 12 引入并发段后这条**形态不变而量级变了**：取消到达在 `gather` 完成之后、写 block 之前时，一次取消丢掉的是**整段已经跑完的结果**（实测三个工具全部 `exit` 到底而 `n_blocks=0`）；串行时最多丢一个，并发时最多丢 `max_concurrency` 个。保住它的机制也多了一道：`asyncio.gather(return_exceptions=True)` 把子协程自己抛的 `CancelledError` **收进结果列表**而不是上抛（实测 3.11：收到的是一个丢了原消息的新实例），因此 `_without_cancellation` 必须在整段写任何 block 之前先扫一遍并原样上抛——照结果处理就等于把一次取消写成一块工具结果 |
| D-05 | `_resume_one` 的读-改窗口：注册表短暂停在 `WAITING` 而槽里无 waiter | 计划 2 Task 9 明确留着，理由写进 `_resumed_status` docstring：自愈路径是下一个轮边界的 `_oscillate`（`allowed` 含 `WAITING`），而修法自己更糟——「CAS 后复检 waiter、没了就改 RUNNING」有同样窗口，会把刚装上的新等待抹成 RUNNING |
| D-06 | **串联 POST hook 会把中间的 `UNTRUSTED` 标记从 executor 眼前藏掉**（登记时挂在「归计划 2 Task 13」名下，实际已被更早的改动关掉——本条是一次**过期的 OPEN**） | `HookResult.history` 记的是每个键在链上被依次赋过的**全部**值，而 `tool_executor` 读的正是它（`_tightened(_strictest(result, post.rewrites('result')), rewritten)`），模块 docstring 也逐字写明了理由：「用 `history` 而不是 `modified`：后者 last-wins，链中间那次收紧会被后一个 hook 的默认值抹掉」。控制器实测：hook 1 把结果收紧成 `UNTRUSTED`、hook 2 交出一个带默认 trust 的新值，最终块是 `content='summarised'`（内容取最后一个）+ `trust=untrusted`（信任取最严的）——正是要的语义。**顺带的教训**：跟踪的遗留登记本身会过期，条目状态必须在被引用时取证，不能照抄；计划 2 Task 13 派活前若照登记去「修」它，会在一处已经正确的代码上动刀 |
| D-28 | `DangerRequest.provenance` 没有生产者 | 计划 2 Task 11：`tool_executor._provenance(ctx)` 在每批调用进入时算一次，交回 `TrustLevel`（**永不交 `None`**）。用例钉住五面：升级对 `AllowAll` 也生效、不可信块在轨迹任意位置与任意 role 上都算、干净轨迹不触发升级、同一批里中途产出的不可信结果不改变本批的答案、以及 `PRE_TOOL_USE` hook 写过参数时重取并取严者。**范围比 D-28 原文宽一格且是一次自觉的扩大**：原文说的最小形态是「**本轮**上下文里出现过 `UNTRUSTED`」，落地取的是**整份 `current_traj`**（跨轮、跨 `run()`），因为「本轮」这条线在这里既拿不到也守不住——轨迹上没有轮边界标记，而把边界猜出来就是拿一份猜测冒充一次检查。代价是误报面比原文大（见 B-15 的第三条来路），逃生口是 D-29 |
| D-31 | 绕过 `disposition_of` 直接调 `DangerPolicy.decide` 就绕过了强制升级，typing 看不见、运行期不警告 | 计划 2 Task 11：内核唯一调用点是 `ToolExecutor._gated`，走 `disposition_of`；机器守卫 `test_disposition_of_is_the_only_place_in_the_package_that_consults_a_policy`——AST 扫 `src/syndica/**`，调 `.decide(` 的文件清单必须逐字等于 `['kernel/danger.py']`。实测把那一行换成 `self._policy.decide(...)`，守卫与四条行为用例一起变红。**守卫自己也要被守**：`rglob('*.py')` 收窄成 `rglob('danger.py')` 时清单仍逐字相等、全绿，而此后它对包里任何别的文件都瞎了（实测 1277 全过）。因此 `test_the_policy_call_site_scan_really_covers_the_whole_package` 断言扫过的文件含 `kernel/tool_executor.py`、且与一次独立的 `os.walk` 遍历逐个相等 |
| D-32 | `provenance is None`（没溯源过）与 `TRUSTED` 在 `disposition_of` 出口不可区分 | 计划 2 Task 11：内核生产路径上 `None` 不再出现——`_provenance` 看完整份轨迹才作答，报 `None` 就是把做过的检查说成没做过。用例 `test_every_call_the_gate_consults_on_carries_an_answered_provenance` 逐次断言策略收到的是 `TrustLevel` 成员，`test_the_policy_sees_untrusted_provenance_on_the_one_grid_that_reaches_it` 补上另一半——「低风险 + 可溯源到不可信」是策略**唯一**看得见 `UNTRUSTED` 的一格（高风险那格在它之前就被升级接走），只断言干净轨迹上的两次 `TRUSTED` 等于只验了这个出口的一半。`None` 仍是 `DangerRequest` 的合法取值（它对第三方调用方的含义就是「还没走到溯源那一步」），`test_danger.py` 里把两者同结论钉成当前行为的那条用例继续守着漂移 |
| D-33 | spec §12.2 矩阵里「低风险 + 可溯源到不可信 → 放行并记录」的记录没有落点 | 计划 2 Task 11：`tool_executor._report_allowed_low_risk_from_untrusted`，WARNING（root logger 默认停在 WARNING，INFO 的记录根本不会被创建）。断 `record.args` 的五个量：工具名、调用 id、规则 ID、风险等级、溯源结论。三条反向用例钉住不误报：低风险 + 干净轨迹、没有规则命中 + 不可信轨迹、以及被宿主策略拦下的低风险调用都不得留下记录 |

## 待审计核实（认为已被后续改动关闭）

| # | 内容 | 认为关闭它的改动 |
|---|---|---|
| V-01 | `ERROR_CLASSES` 是手工维护列表，跨模块子类（`llm.ContextOverflowError`）覆盖不到 | 计划 1 Task 10 改为从文件树独立推导期望值、动态收集 `SyndicaError` 子类 |
| V-02 | 接管 CAS 在 `parked` 状态下不是有效的所有权守卫 | 计划 2 Task 4 的 `ensure_active`；Task 7 进一步改为**用注册表状态作所有权凭证** |

## 有主待做（里程碑 1 内）

### 归计划 2 Task 13（三层装配 —— hooks 层）

| # | 内容 |
|---|---|
| D-35 | **就地改 `args` 而不声明 `modified['args']` 的 `PRE_TOOL_USE` hook，躲得过溯源重取。** 闸判的仍然是改写后那一份（危险规则照常命中），漏的只是「hook 是这份参数的作者，因此批快照不成立」这个判据——`ToolExecutor._execute_one` 看 `HookResult.history` 与对象身份，两者对未声明的就地改动都无感。实测：`AllowAll` 宿主 + 一个把本批不可信输出就地写进 `args` 的 hook，`sudo rm -rf /` 照常执行（声明了 `modified['args']` 的同一个 hook 则被升级拦下）。与 D-06 同型（hooks 层的合并语义把一次写从 executor 眼前藏掉），**修法也在 hooks 层**：executor 侧唯一的自足修法是拿改写前的快照逐值比对，而那要在每次工具调用的必经之路上对模型与 hook 控制的值调 `__eq__`，一个惰性或恶意的 `__eq__` 就能把整批打成 ERROR——用一条新的故障路换一条窄缺口，方向是反的。默认 `BlockHighRisk` 的宿主不受影响（高风险与溯源无关地一律阻断） |
| D-07 | 可观察切点契约只在文档里失败：typing 看不见、运行期不警告，且两条既有用例把「观察者的返回值被忽略」当成了预期。**计划 2 Task 11 把这个面扩大了一格**：新增的 `TOOL_CONFIRMATION_REQUIRED` 是第一个「hook 作者会真心想回答」的可观察切点——它报告的是「这次调用等人确认」，而挂上去的人最自然的写法就是返回一个批准。返回值照旧被整个丢弃，且没有任何提示。切点自己的 docstring 写明了「观察者答不了这次确认」，但那正是 D-07 说的「只在文档里失败」 |

### 归计划 2（编排工具：`Delegate` / `Fork` / `SendMessage` / `QueryAgent` / `KillAgent`）

| # | 内容 |
|---|---|
| D-39 | **`Runtime._refuse_self` 在并发段里认不出「自己」。** 它按 `self._table.get_task(agent_id) is asyncio.current_task()` 判断这次 `despawn` / `kill` 是不是落在调用者自己的事件循环里（`runtime.py` 里 `ExitMode.EXIT` 那处同款）。计划 2 Task 12 之后，**一个可并发的工具跑在 `asyncio.gather` 建的子任务里**，`current_task()` 不再是那个 Agent 的驱动任务，于是守卫静默放行——而它挡的正是「等自己停下来 = 永久挂死」。实测（进程内复刻同一判据）：同一个工具在单元素段里被拒、在两元素并发段里不被拒。单元素段仍在调用方自己的任务里跑（`_run_segment` 对 `len(calls) == 1` 刻意不进 gather），所以默认工具（`is_concurrency_safe` 返回 False）不受影响；踩到它需要一个把自己声明成可并发、又去 `despawn` 自己的编排工具。**刻意不为当前行为写用例**——那会把一个挂死固化成契约。修法在 runtime 层：沿驱动任务向下传一个标记，而不是比对任务身份。**具体取 contextvar**（在驱动任务上置位、守卫读它）——`asyncio.Task` 建立时会复制一份 contextvars，于是子任务里读到的仍是驱动任务写下的值，一处修好 `_refuse_self` 与 `ExitMode.EXIT` 两个调用点；而任务身份比对在每一个把协程包成 Task 的地方都会重新失效，落在把 Runtime 控制面暴露成工具的那个任务上 |

### 归计划 3（声明式 Agent 定义 —— 工具注册层）

| # | 内容 |
|---|---|
| D-08 | 手写 schema 的 `name` 键可与 `tool.name` 分叉（模型按前者调用、注册表按后者索引）。当前行为已钉住，但守卫属**注册层**——声明式清单会从 manifest 构造 tools dict，那里是唯一收口点 |

### 归计划 4（第一个 backend + provider）

| # | 内容 |
|---|---|
| D-09 | **依赖规则 3 只实现一半**：`backend 不得 import surface` 未覆盖。原始条目自带期限——「第一个 backend 落地前须补」，而计划 4 就是它 |
| D-34 | **每一个内置工具都必须显式声明 `__tool_command_args__`。** 计划 4 加的文件读写与命令执行工具正是这条纪律的第一批用户：执行类工具漏声明会让判定改去扫它的全部参数（保证更弱且会对文本参数误报），而 `Read` / `Write` / `Grep` 这类收文本的工具漏声明会被自己写的文档内容命中——一段含 markdown 表格或围栏代码块的正文里一个裸 `|` 就能拼出假管道链。判定已在结论里点名「这个工具没声明」并给出修法，但那是**事后**诊断；纪律在计划 4 建立，成本是每个工具一行 |
| D-36 | **判定读不完参数会终结一整次 `run()`，而这条路此前打不到任何人。** 未声明 `__tool_command_args__` 的工具，判定按「全查」读它的每个参数；一个收大段文本的普通工具收到超过 `_MAX_TEXT_CHARS`（262144）字符或嵌套超过 8 层的值时，`danger._readable_texts` 抛 `ContractViolation`。计划 2 Task 11 之前判定没有生产调用者，这条只在单测里现身；接上闸之后它坐在**每次工具调用的必经之路**上：实测 300KB 文本的一次调用 → 失败那个调用得一条「判定抛出…工具没有运行」的 block、同批其余得「没有执行」、`ctx.stop_reason=ERROR`，且异常上抛出 `run()`（`SyndicaError` 按 spec §10 交给 agent loop）。**修法是让工具声明**（`__tool_command_args__ = ()` 或只声明真会被执行的那几个参数），与 D-34 是同一条纪律的两面：D-34 说漏声明会让闸变弱或误报，本条说漏声明还能让一次长跑当场失败。B-09 登记的是判定侧「读不出」的行为，**不含**「它现在会终结一次 run」 |
| D-37 | **危险判定现在在每次工具调用上跑两遍**（分段一次看模型发出来的参数，闸一次看 `PRE_TOOL_USE` 改写之后的参数），于是 D-36 那条上限扫描的成本翻倍。**已实测而非推论**：三个调用恰好问 `judge` 六遍、问宿主 `policy` 三遍（分段只问判定不问处置，`test_segmenting_does_not_consult_the_host_policy_a_second_time` 钉住后一半）。**两遍不能合并成一遍**：闸必须判真正会被执行的那份参数，而「参数有没有被 hook 改过」这个判据本身不完备（D-35），复用分段那次结论就等于让闸去判一份不会被执行的参数——用一条静默的安全缺口换一半 CPU，方向是反的。真正的省法是让工具**声明** `__tool_command_args__`（D-34）：声明 `()` 的工具两遍都读不出任何文本，扫描退化成常数。计划 4 加第一批收大段文本的工具时一并量一次实际开销 |
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
| D-29 | **`untrusted_high_risk` flag 未实现**。spec §12.2 的处置矩阵把「高风险 + 可溯源到不可信」交给这条 flag（`reject` 默认 / `ask` / `allow`），而 `danger.disposition_of` 现在把 `reject` 写死。写死的是**默认值**，不是能力边界：`disposition_of` 是内核自己的函数（第三方只调不实现），加一个带默认值的参数对任何调用方都不破坏。做这条时一并给 `ask` 找落点——`Disposition.CONFIRM` 的交互通道在计划 6。**它唯一改变的是 `AllowAll` 这类宿主能不能把这一格调成 `ask` / `allow`**：默认宿主用 `BlockHighRisk`，它对 HIGH 一律 BLOCK 且与 provenance 无关，所以默认宿主的长跑会不会卡在高风险调用上与本条完全无关（决定它的是默认策略本身）。**排在计划 4 之前的理由是另一条**：计划 4 才有第一批真能触发 HIGH 规则的工具（文件读写 + 命令执行，见 D-34），届时 B-15 的误报面——含「任何一次工具异常之后整份轨迹永久算不可信」——才从理论变成活的，而唯一的逃生口就是这条 flag |

### 归计划 6（CLI + 人工确认交互）

| # | 内容 |
|---|---|
| D-38 | **并发分段用「命中任何危险规则」近似 spec §5.6 的「需要人工授权」。** 近似的理由是分段不该问处置：`DangerPolicy` 由宿主实现，可以计数、可以记录，而一次调用问它两遍、两遍看的参数还不同（分段看模型的、闸看 hook 改写后的）就是在骗它；判定则是纯规则，问几遍都一样。这个近似在默认处置下**更严**（LOW 也切段，而 LOW 是放行的），残留缺口是反方向：宿主策略对一条判定为 `NONE` 的调用给出 `BLOCK` / `CONFIRM` 时，批次不会因它切开。**今天这个缺口没有后果**——被闸拦下的调用**自己的工具**根本不执行，所以与它并发的邻居等价于它不在批里。措辞要准：被拦下调用的 **hook** 确实跑了（`PRE_TOOL_USE`，以及 `CONFIRM` 那一格的 `TOOL_CONFIRMATION_REQUIRED` 观察者），而且是与兄弟并发跑的；今天没有安全后果是因为 hook 是宿主自己的代码。**计划 6 落地人工确认通道之后就有后果了**：一次被批准的 `CONFIRM` 会真的执行，那时「需要人工授权的调用不与别人并发」必须重新成立。届时的修法不是在分段里加一次 `decide`，而是让确认通道自己把被批准的调用排成独立一段 |

### 归计划 2 Task 16（公开面收口）

| # | 内容 |
|---|---|
| D-24 | **`waiting` 的六个名字尚未进 `kernel.__all__`**（`Waiter` / `UserInput` / `Confirmation` / `Reply` / `WaitResult` / `WaitState`）。后果不只是导出面不全——只用公开包面的人**调不了** `Runtime.wait_for`（构造不出入参）。计划 Task 16 的 59 项清单已含它们，但 `test_public_api.py` 逐字写死，忘了改**不会**变红 |
| D-25 | `__all__` 内容全仓无测试覆盖（子模块级）。计划 1 的 Task 4 与 Task 9 各挂过一次，是同一条。`kernel.__all__` 有守卫，子模块没有 |
| D-26 | **`ABORTING` 分支扩大了 `wait_for` 的成功面**：`kill` 之后（`_reap` 未跑完那段）调 `wait_for` 现在交回 `ABORTED` 而不再拒绝。计划 2 Task 9 判断这是对的，但它是新行为 |
| D-30 | **`danger` 的公开名字尚未进 `kernel.__all__`**（`RiskLevel` / `Disposition` / `DangerVerdict` / `DangerRule` / `DEFAULT_RULES` / `NO_RULE_MATCHED` / `DangerJudge` / `DangerRequest` / `DangerPolicy` / `BlockHighRisk` / `AllowAll` / `disposition_of` / `validate_danger_options`）。与 D-24 同型，但 Task 16 的 59 项清单成文于 `danger` 之前、**不含**这些名字；只用公开包面的宿主因此写不出自己的 `DangerPolicy`。`test_public_api.py` 逐字写死，漏掉它们**不会**变红。**计划 2 Task 11 之后这条从「不方便」变成「公开面自相矛盾」**：`AgentOptions` 已经有 `danger_judge: DangerJudge \| None` 与 `danger_policy: DangerPolicy \| None` 两个字段，而只用公开包面的宿主导不到这两个类型——一个导出的数据类有两个它填不出来的字段 |

### 归里程碑 1 收口审计

| # | 内容 |
|---|---|
| D-27 | **三条「唯一证据型」ERROR**（`_report_abandoned_input` / `_report_unwaited_timeout` / `_report_discarded_take_failure`）都断 `record.args`，但共同依赖「root logger 默认 WARNING」这个前提；宿主把级别调高则三条一起静默。这是既有模式，发布前确认一次。**计划 2 Task 11 又加了一条同族记录且更靠近那条线**：`tool_executor._report_allowed_low_risk_from_untrusted` 是 WARNING 而非 ERROR（它报的是一次放行，不是故障），于是宿主只要把级别提到 ERROR，spec §12.2 矩阵里那格唯一的痕迹就没了。发布前一并定这四条的级别与宿主配置指引 |

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
| B-13 | **wrapper 兜底带来的一族误报**：C-2 的修法是「遇到 wrapper 之后不停止扫描，把该段剩余的非选项词全部收进候选」，于是「把危险词当普通实参传给 wrapper」会误报——`time which sudo`、`nohup echo sudo apt install`、`nohup cat /etc/mkfs.conf`、`nice -n 19 tar -rf a.tar rm`（`command -v sudo` 基线也命中，非本轮引入）。**刻意不为它们写「这是预期」的用例**：那会把一个误报固化成契约。**正解**：给 wrapper 建一张「哪些选项吃下一个词」的表，并**与现在的兜底并存**（表命中就精确、表没命中就退回收集全部），而不是二选一——只有表就退回 C-2 那条 Critical（漏一个选项 = 漏一道闸）。非 wrapper 段的行为一字未变（`echo sudo apt install` 仍不命中，有用例单独钉住这个边界） |
| B-14 | **`_BOUNDARIES` 里的 `)` 零覆盖**：删掉它没有任何用例变红，且找不到一条既合法又自然的 shell 输入能区分。不构成已知缺陷，只是覆盖空白——记在这里是因为 AGENTS.md 的判据是「每个守卫都要有一条单独施加即变红的变异」，做不到就要说明白。同族：续行正则的精度只由 CRLF 那条用例锁住，`\` + 空格 / 制表符这类 escape 是整体近似处理，没有钉 |
| B-15 | **溯源是「这份轨迹里有没有出现过 `UNTRUSTED` 的 block」这个上界，不是一条因果链**，而它的触发面比「读过一次网页」宽得多。落进这份 Context 的 `UNTRUSTED` 有三条来路，后两条是 **executor 自己写的块**：(1) 工具如实交回外部内容（`ToolResult(trust=UNTRUSTED)`，网页 / 文件 / 子进程）；(2) **任何一个工具抛普通异常**——`_tool_raised_result` 嵌了 `str(exc)`，可以整份来自工具带进来的内容，因此按 UNTRUSTED 落盘，于是一个纯本地的 `ZeroDivisionError` 也够；(3) `_aborted_result`，以及 `_skipped_result` 交出非 `ToolResult` 替代值时。三者都会把这份 Context 余生的高风险调用钉成「可溯源到不可信」，**而宿主关不掉**（D-29 未实现）：明确声明 `AllowAll()` 的宿主在第一次工具异常之后就永久失去那个语义，零提示。实测：一个纯本地 Calc 抛 `ZeroDivisionError` 之后，同一份 Context 上的 `rm -rf ./build` 在 `AllowAll()` 下被强制升级拦下 | spec §15 把**不可信内容标记与溯源链**排在里程碑 5（与执行面完整化同期），它要接产物溯源（§7.3）与 trace 因果链（§11.1），两者在里程碑 1 都还不存在。当前上界是 D-28 声明的最小正确形态：它只会多判、不会少判，而少判才是那道升级要防的方向；那三条来路上的 `UNTRUSTED` 各自都是对的（异常文本确实可能整份来自外部），错的只是把「出现过」当成「这次调用来自它」。收窄它的前提是溯源链本身可信——在此之前把上界换成任何「更准」的启发式，都是拿一份猜测冒充一次检查。**当前行为已被 `test_one_plain_tool_exception_escalates_every_later_high_risk_call_on_that_context` 钉住**（不只钉那些块自己的 trust，而是把它接到闸上）：下一次调整 `_tool_raised_result` 的 trust 会同时改掉闸的行为，这条用例让它成为一次自觉的改动。误报的逃生口是 D-29 |
| B-17 | **并发段里，工具抛出的非 `CancelledError` 的 `BaseException`（`KeyboardInterrupt` / `SystemExit`）逃进事件循环，不经过 `execute_tool_calls`。** 实测 3.11：`asyncio.gather(return_exceptions=True)` 只把 `CancelledError` 收进结果列表，`Task.__step` 对这两类是 `set_exception` 之后**再 raise 一次**，于是它从 `loop._run_once` 里出来——`await gather(...)` 永不返回，写在它后面的那一遍取消扫描根本没有机会跑（在 pytest 里的表现是整轮采集被中止）。串行段不受影响（`except Exception` 让它原样上抛，已有用例钉住） | 这是 CPython 的 Task 机制，本层改不动。**配对不变式仍然成立**：没有任何 block 被写下，留下的与 D-04 同形。两者的差别只在异常交给谁——串行给调用方，并发给事件循环的宿主。真要统一，唯一的办法是不用 `gather` 而自己建 Task 并逐个 `await`，那会把「一个失败其余照跑到完」这条语义也一起换掉 |
| B-18 | **C-1 修法的误报面：「`PRE_TOOL_USE` 改写了参数 ∧ 本段有兄弟」这个交集上，溯源一律按 `UNTRUSTED` 算，即使整份轨迹干净、同段也没有任何不可信产出。** 后果有两个：`AllowAll` 这类宿主的高风险调用在这个交集上被强制升级拦下（默认 `BlockHighRisk` 无变化，它对 HIGH 一律阻断且与溯源无关）；spec §12.2 低风险那一格会多记一条放行记录 | 与 B-15 同族、同方向（**多**判），而少判的后果是那道关不掉的升级与那条记录义务**静默**失效——方向不对称，只能选多判。判据是 spec §13「溯源失败时按最坏情况处理」，而多元素段里的那次重取结构上就是一次失败的溯源。收窄它要靠 spec §7.3 产物溯源 + §11.1 trace 因果链（里程碑 5）把「兄弟的产出有没有进到这份参数里」变成一个能回答的问题；在此之前任何「更准」的启发式都是拿一份猜测冒充一次检查。误报的逃生口与 B-15 同为 D-29。记录那条消息已经改成陈述**溯源结论**而不是「轨迹里有不可信内容」——后者在这个交集上会是假话 |
| B-19 | **段内 `PRE_TOOL_USE` hook 往 `ctx` 写的块，相对顺序是调度的函数。** 实测：三个同段调用各挂一个往 `ctx` append 一块的 hook，让 c1 的 hook 多让出几次，append 顺序就从 `['c1','c2','c3']` 变成 `['c2','c3','c1']`。`role='tool'` 的**结果块**不受影响（始终按调用顺序落，配对不变式没破），破的是「同一批调用跑两次必须留下同一份轨迹」——而轨迹会被持久化、被 resume 与读轨迹的父 Agent 消费 | 判为 **hook 作者的责任面**：`Context.append` 是裸 append，段内并发时它的交错顺序只能由写者自己约束（要确定顺序就不要在段内 hook 里写轨迹，或把内容挂在结果上让 executor 按调用顺序落）。executor 侧的自足修法是把段内 hook 的写入缓存起来按调用顺序回放，那要求 executor 拦截 `Context` 的写入面——它今天不拥有这个面，而拦下来就等于给 hook 换掉了它拿到的 `ctx` 语义。**判断本身写在这里**：Task 12 之前这个顺序是确定的，所以这是一次自觉的放弃，不是没看见 |
| B-07 | 关联 ID 是一个扁平命名空间（`tool_call_id` 由模型厂商生成、`correlation_id` 由 `Runtime.send` 写入，都不带前缀），一条对派活的回复原则上能解掉一次工具确认的等待 | 计划 2 Task 9 裁决**不加前缀**并写进 docstring：加前缀要求发答复的那一侧用同一套编码，两侧不一致的症状是答复躺在邮箱里、等待跑满超时、零日志，比当前无已知来源的碰撞风险更难查；而 `Confirmation` 等的是模型厂商生成的 ID，前缀会变成必须对外公开的编码契约 |
