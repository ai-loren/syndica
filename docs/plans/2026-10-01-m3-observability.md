# 里程碑 3：可观测性 + 脱敏收口

> **执行者须知：本文件是需求，不是参考。** 「公开契约」一节里的 trace schema、span / 边的种类、属性键与类型、
> 规则 ID、回放文档的键、CLI 参数、配置键与环境变量一旦发布就是兼容承诺；执行中不得为了让测试变绿临时改名或
> 缩小断言范围。

**状态：执行中。** 已经过两轮独立评审修订（2026-10-01；第二轮 5 High / 9 Medium / 7 Low 全部并入，见「决策日志」
末条）。五项裁决由用户确认，用户确认按计划开工（见「决策日志」）。
GitHub Actions 本月免费额度已用完，恢复之前按 M2 计划「验证」一节的本地验证协议执行。

**Goal:** 一次多 Agent 的 run 结束后，人能回答「谁创建了谁、谁在等谁、时间花在哪、哪个工具最慢、从哪一步开始
跑偏」；这次 run 不用真实模型就能原样回放，失败的 run 回放出同样的失败，内核行为一变回放就报出第一处分叉；
任何写进 trace 或导出给第三方的内容都先过同一个脱敏器；span 能导出到标准的 OpenTelemetry collector。

**Architecture:** 内核在固定位置同步地产出中立的 span 与因果边（`Tracer` 协议，默认什么都不做）；`TraceRecorder`
把它们逐叶脱敏后经一个写入任务按序写进 `TraceStore`（第五个 Store Protocol，内存与文件两种实现）。记录下来的
回放文档就是 L2 场景格式 v2（schema 归内核一个模块所有）：回放时触达外部世界的工具、模型、人的答复按记录给出，
内核本身——包括危险判定与处置——全真并与记录逐项比对。CLI 默认把每次 run 记录到 `.syndica/traces/`，并提供
`syndica trace list/show/replay/export/prune`；OTLP/HTTP JSON 导出在一个只依赖 httpx 的 backend 里。

**Tech Stack:** 不新增运行依赖。OTel 导出用已有的 httpx（新 extra `otel`）。

**依据：** spec §11（可观测性）、§12（安全，尤其 §12.2、§12.3）、§13.1（L2 回放）、§4.4（Store Protocol）、
§15（M3 完成判据）；路线图「M3 可观测性 + 脱敏收口」与「遗留项归属」；
[M2 收口审计](../audits/2026-10-01-m2-final-audit.md)的遗留；`AGENTS.md`。

---

## 现状校准（2026-10-01 调研，两路评审复核）

1. **事件总线不能当捕获源。** `EventBus` 每个订阅者一个有界缓冲，满了丢最旧的一条（`kernel/events.py`）；只发
   `agent_start / agent_end / agent_error / turn_start / turn_end`、确认请求与人机工具的事件，没有 LLM 与工具调用，
   事件不带时间戳。路线图「TraceRecorder 订阅进程内事件流」一句因此改为内核插桩（D2）。
2. **钩子看得全每一轮，但缺主体、缺位置。** Agent 级切点只带 `ctx`（`Context` 里没有 agent id），工具切点连 `ctx`
   都不带；`PRE_TOOL_USE` 对未知工具与坏参数不触发，`POST_TOOL_USE` 对被跳过、被拦、被拒绝、抛 `SyndicaError`、
   返回值不是 `ToolResult` 的调用不触发；取消时 `AGENT_END` 与 `AGENT_ERROR` 都不触发；`AGENT_SPAWN` 在子 Agent 的
   任务建好之后才触发、不带角色；拓扑槽建出的 Agent 不带 `parent_id`；`ensure_active` 唤醒不触发 `AGENT_SPAWN`，
   而且新的驱动任务建在**发送方**的上下文里。没有切点的位置：`Runtime.send`、等待（`wait_for` 与 Delegate / Fork
   的 `_wait_in_turn`）、确认的结局、平台故障。
3. **LLM 调用。** 每轮一次 `llm.create`，轮换在它里面；`RetryModule` 是一个 `LLM_FAILURE` hook，在 hook 里**再调**
   `agent.options.llm.create`（这些重试的回复与首次调用同样是回复，回放要能给出）。作答平台只在响应块的
   `extra['llm_route']` 上；`usage` 全仓没有解析；`ModelRouter` 不实现 `CachePrefixProvider`，它的 `config` 报的是
   最近一次作答的成员且被所有 Agent 共用。
4. **工具执行器的结局不止八种。** 结果块从多处追加：正常结果、工具抛普通异常（转成错误结果）、`SyndicaError` /
   返回值不是 `ToolResult` / `POST_TOOL_USE` 出错（上抛并给余下调用补「未执行」）、判定器出错、`PRE_TOOL_USE` 跳过、
   处置为阻断（含 `untrusted_high_risk` 的升级——它在问宿主策略**之前**生效）、确认被拒、确认被抢占、需要确认却
   没有确认通道、未知工具、坏参数、第 9 步中止补的「未执行」、恢复时修补被打断的批次。Bash 超时是一个正常结果
   （`meta.timed_out`），执行器本身没有工具超时。
5. **B-19 的实际可达性。** 内核与本仓的工具没有一个声明可并发，生产路径上每段只有一个调用；B-19 只在第三方声明
   可并发的工具上可达。同型的另一处：可观察切点的观察者总是经 `gather` 并行跑（只有一个观察者也一样）。全仓
   `src/` 里没有 hook 或插件往 `ctx` 写块；测试里有一处从观察者追加（`tests/kernel/test_orchestration.py`）。
6. **没有 run / trace id，没有时钟抽象。** agent id 在 `BaseAgent.__init__` 里生成（uuid4），`spawn` 之后才登记；
   `spawn` 不知道是哪个工具调用创建了它；AskUser 的事件只带 `request_id` 与问题；模型给的 `tool_call.id` 可以重复、
   可以是空串（客户端照收）。
7. **场景格式 v1 是手写脚本，不是录像**：只存回复的一侧；工具在空的临时目录里真跑；危险处置用测试专用的策略；
   `ScriptedLLM` 记下了请求但没人比对；实例按角色计数分配；人的答复按全局顺序匹配，表达不了超时与中止；v1 限制
   拓扑槽并拒收 `impl`。CLI run 与回放在模块、Flag、危险规则与处置表、`PRE_TOOL_USE` 守卫、输出上限、子进程环境、
   HostFacts、LLM、入口、拓扑、`--max-turns`、`--no-write`、类式角色上都不同。
8. **脱敏。** M2 的 `cli/redaction.py` 里只有 `secret.env_value` 依赖宿主（环境变量与查磁盘的 `_is_path`），其余九条
   是纯文本规则。**它不幂等**：`https://ghp_<36 位>@github.com` 第一遍变成 `github_token` 的占位符，第二遍因为占位符
   里有冒号又被 `url_userinfo` 吃掉。对 JSON 编码之后的文本做脱敏会漏（转义后的私钥块、带引号的环境变量值）并且
   会弄坏 JSON（`url_userinfo` 跨越字符串边界）。写入点守卫只扫 `src/syndica/cli`，按名字认任何 `.redact(...)`，
   不认网络外发。
9. **Store 的范式**：Protocol（`@runtime_checkable`，不变式写在 docstring）+ 内存实现（深拷贝进出、先校验后写、
   `ContractViolation` 三件套）+ 随 wheel 发布、不 import pytest 的契约套件 + `tests/contracts/` 的测试类与故意违约的
   子类 + `tests/test_contract_guard.py`。spec §4.4 列了四个 Store，TraceStore 是第五个。
10. **装配与依赖。** `pyproject.toml` 只声明了 `syndica` 这一个命令入口，没有插件入口；`src/syndica/__main__.py` 是
    装配层（惰性 import backend、注入 `model_factory`）；CLI 不能 import backend，surface 之间互不 import；内核不能
    依赖 PyYAML（`scripts/check_deps.py`）；httpx 只在 `openai` 与 `dev` 两个 extra 里。
11. **`Read` 把文件内容标成可信**，`LocalWorkspace` 只护 `.git`；Agent 用 `Read` 读 `.syndica/` 下的文件，等于把
    此前标成不可信的内容洗成可信（spec §12.2）。M2 已登记「Agent 改得到以前的交接文件」。

## 路线图条目对照

| 路线图 TODO | 本计划 |
|---|---|
| `TraceStore` Protocol 与内存实现 | T5；文件实现 T11 |
| `Redactor` Protocol、内置规则与用户追加规则 | T3（统一 M2 的脱敏器，规则 ID 不变，修掉不幂等） |
| Runtime、Agent、ToolExecutor、LLM provider、Broker wait 上打 span | T7（内核插桩）、T8（LLM 侧） |
| 两类因果边：派生、消息 | T7 |
| L2 runner 消费捕获 trace，捕获与回放同源 | T12–T14 |
| `syndica trace list/show/replay` | T15（另加 `export`、`prune`） |
| OTel exporter backend，kernel 只产中立 span | T16 |
| trace 保留策略 | D8（用户裁决：默认「可回放」级） |
| B-19 复核 | T10 |
| LLM span 记作答平台；`ModelRouter` 转发 `cache_prefix_units` | T8 |
| M2 遗留 | T1、T2（用户裁决：先做） |

与路线图不一致之处：捕获靠内核插桩而不是订阅事件流（D2）；默认记录级别是「可回放」（D8，用户裁决）；OTLP 导出
的端点只从用户环境或命令行来，不从项目配置来（D14）。

---

## 已确认边界

### 做

- M2 遗留：危险规则认出 `sh -c` / `eval` / `xargs` / `find -exec` 里的命令与 `find <根> -delete`；git 收尾的摘要
  按文件给出增删行数；Ctrl-C 中断时照样留交接文件。
- 内核统一 `Redactor`（规则 ID 不变，修掉不幂等，加用户规则与 `secret.named_assignment`）；逐叶脱敏的编码器与
  `RedactedText` 类型闸；写入点与外发点守卫扩到全仓；宿主私有目录（`.syndica/traces`、`.syndica/handoffs`）对文件
  工具关闭。
- 内核 trace 模型、同步的 `Tracer` 插桩、`TraceRecorder`、`TraceStore`（内存、文件）与契约套件。
- LLM 侧：token 计数、作答平台与平台故障、`ModelRouter` 转发前缀单位。
- B-19 收口；可复现的 id 与稳定键。
- 场景格式 v2（schema 归内核，手写与捕获共用）与回放执行；捕获即回放。
- CLI：默认记录、`--trace` 级别、`syndica trace list/show/replay/export/prune`。
- OTLP/HTTP JSON 导出 backend。
- L2 E2E 与真实模型 E2E；M3 收口审计。

### 不做

- 成本（价格表）与预算：价格表归 provider backend（spec 决策 #69），可感知预算归 M8。M3 只记 token 计数。
- 会话、记忆、产物的持久化：M6、M7。M3 只把它们将来要过的脱敏闸与守卫准备好。
- 回放时真跑触达外部世界的工具（用户裁决：按记录回放）。手写的 v2 场景不给 `tool_results` 时工具照 v1 真跑。
- `chat` 会话的回放：随 M6 的会话持久化做，M3 的 chat 只记 `structure`（D15）。
- 采样；OTel 的 metrics 与 logs；Web 可视化（M9）。
- `module` 种类的 span：保留种类名不产出，归 M4。

### TDD 接缝

1. `main(argv, host, io)` 与 `RuntimeCliHost`：CLI 的输入、输出、退出码与写下的文件。
2. `Tracer` / `TraceRecorder` / `TraceStore` 的公开方法与契约套件。
3. `Redactor` 与 `encode_redacted` 的纯函数。
4. `ScenarioRunner` 与 `load_scenario`：场景（v1、v2）跑出的结果。
5. 捕获 → 回放的往返：同一个 run 的两份轨迹逐块相同（含 `trust`，不含 `extra`），危险判定逐项相同。
6. 源码级守卫（AST）：只用于「某类写法不存在」（未脱敏的写入点与外发点、内核 import backend 或 PyYAML）。

---

## 设计裁决

**D1 顺序。** M2 遗留（T1–T2）→ 脱敏收口（T3–T4）→ trace 模型与捕获（T5–T10）→ 文件存储、CLI 记录与回放
（T11–T14）→ surface 与导出（T15–T16）→ E2E 与收口（T17–T18）。T10 之后做阶段二审计，T16 之后做阶段四审计。
任何 trace 落盘之前统一的脱敏器与守卫必须先在。

**D2 内核插桩，同步，不靠钩子记录。** 现状第 2、4 条：拿钩子拼出来的 trace 恰好在被拦的调用、被取消的 Agent、
重试这些最需要的地方有洞。内核在固定位置调 `Tracer`：

- `Tracer` 的方法**全是同步的，从不 await**：内核里有几处正确性依赖「这两句之间没有挂起点」（spawn 的登记与建任务、
  停在邮箱上、装等待令牌后复检中止、`run` 的检查与置位）；异步的 tracer 会在这些窗口里插进调度，而且只在文件存储
  下才插、内存存储下不插，测试照绿而 CLI 的调度变了。`TraceRecorder` 只往内存缓冲里追加，由它自己的一个写入任务
  按序落进 `TraceStore`；`finish` 等写入任务排空。
- `Runtime(..., tracer=None)`，默认实现什么都不做；Runtime 把它交给管的每个 Agent 与执行器（内部接线，不进
  `AgentOptions`）。
- 插桩点：Agent 的每个工作周期（含被取消）、每一轮、每一次 LLM 调用（含 `RetryModule` 在 hook 里发起的每一次重试）、
  执行器**每一处追加结果块的地方**（现状第 4 条的全部结局）、确认（在执行器的 `_gated` 里，从请求到结局）、等待、
  `send`、spawn、despawn。
- **派生边在 `spawn` 里发出**：起点是调用 `spawn` 那一刻的当前 span（contextvar；Delegate / Fork 时就是那次工具
  调用的 span），终点是子 Agent 第一个工作周期的 agent span（它的 id 可以事先算出，D3）。`ensure_active` 唤醒的
  周期只有消息边，没有派生边；agent span 不挂父 span，Agent 之间的关系只由边表达——驱动任务建在发送方的上下文里，
  靠 contextvar 继承会把「唤醒它的人」误记成「创建它的人」。
- 内核经一个内部保护层调 tracer：同步调用里收不到 asyncio 的取消，tracer 抛出的任何东西都是它自己的缺陷，只有
  `KeyboardInterrupt` 与 `SystemExit` 照常上抛（与 asyncio 运行同步回调的规矩一致）；`CancelledError`、`GeneratorExit`
  与交回协程 / 生成器 / Future 的方法都按失败计。第一次失败记一条 ERROR（三件套），此后只计数，Runtime 关闭时报一次
  总数；run 照常进行。插桩点坐在内核原本不会抛出的几段之间，漏出来的 `CancelledError` 会让 Agent 停在 running 且
  不留日志（T7 评审）。

**D3 span、边与确定的 id。**

- span 种类：`agent turn llm_call tool_call confirmation wait`；`module` 保留。消息的发送是发送方当前 span 上的一个
  事件。边：`spawn`、`message`（发送时的 span → 接收方消费这条消息的那一轮的 turn span，按 message id 连）；
  `artifact`、`verification` 保留给 M7。
- `trace show` 要的东西都在 span 与边上，不靠事后推断：派生边带子 Agent 的 id（`child.agent_id`）——惰性启动的子
  Agent 可能从没有自己的 agent span，而 span id 是单向的哈希；发送事件带接收方（`message.to`）；等待 span 带种类
  （`wait.kind`：`agent human`）、被等的对象（`wait.target`：Agent 等的是它的 id，人等的是请求的稳定键）与结局
  （`wait.outcome`，取 `WaitState` 的值）。
- id：trace id 32 个十六进制字符（随机）；span id 16 个十六进制字符，取 `sha256` 于**长度前缀编码**的结构键，第一个
  分量是 span 种类（agent、turn、wait 的其余分量形状相同，不带种类就撞）：agent =（agent id、周期序号），turn =
  （agent id、turn），llm_call =（agent id、turn、第几次调用：首次与每次重试分开），tool_call 与 confirmation =
  （agent id、周期、turn、批内位置），wait = 它的出处键（D10：`wait_for` 用请求键，Delegate / Fork 的在轮等待用出处
  里的第几次等待，都没有时是（宿主、agent id、第几次））——「该 Agent 的第几次等待」在同一个 Agent 的几次调用并发
  等待时随调度换号（T7）。恢复时补上的那批调用按**恢复它的那个周期**取键，不与被取消的原调用撞号。模型给的
  `tool_call.id` 只作属性记录（脱敏后），不进键：它可以重复、可以是空串。
- 消息边的终点是消费它的那个 span：注入它的那一轮，或等到它的那次等待（T7）。宿主发出的消息与宿主发起的 spawn
  没有起点 span，因此没有边。`despawn` 是发起它的那个 span 上的一个事件（`agent.despawned`，带 `agent.id`）。
  已经结束的 span 不再收事件（工具里 `create_task` 出来的任务活得比调用长），但仍可以当边的起点。
- 时间：`Tracer` 收可注入的时钟（纳秒，默认 `time.time_ns`）。墙钟可以回拨（`CLOCK_REALTIME`，NTP 校时），而模型拒收
  结束早于开始的 span：开始时间取墙钟，结束时间 = 开始时间 + 单调时钟（`time.monotonic_ns`）量出的时长，回拨既造不出
  倒着走的 span，也不会让 `finish` 失败（T5 评审）。状态：`ok`、`error`、`cancelled`。
- span 只有收尾之后才进 store（模型里没有开着的 span）：`finish` 时还开着的 span 一律以 `cancelled` 收尾再写；SIGKILL
  丢掉还在内存里的 span 与缓冲，这是已知的边界（T5 评审）。

**D4 统一 `Redactor`：幂等、逐叶、带类型闸。**

- `syndica.kernel.redaction`：`Redactor` 协议（`redact(text) -> str`、`scan(text) -> tuple[Finding, ...]`）与实现
  `RuleRedactor(*, secrets=(), rules=())`。十条内置规则的 ID、顺序与占位符 `[REDACTED:<规则 ID>]` 与 M2 一致；
  新增 `secret.named_assignment`：形如 `NAME=value`、`"name": "value"` 的赋值里，名字按 M2 D6 的词表像密钥、值像
  凭证时替换值（`.env` 里的 `sk_live_…`、AWS secret key 这类没有固定格式的值此前一条都不命中）。名字的判定
  （`looks_secret` 与它的词表）移进内核：它是对名字的纯函数，不读环境。「值像凭证」同样是纯谓词（M2 查磁盘的
  `_is_path` 留在 CLI）：按 M2 的词表加「8 个字符以上」在 `src/` 里就命中 69 处（`key='llm_retry_attempts'`），所以值要
  按长度、字符集与熵收紧，并以「在 `src/` 与测试固定装置里零命中」为误报门槛——过不了就推迟这条规则并登记，不放宽
  门槛。它**不进 `scan`**：`scan` 是 git 收尾的提交闸，一处误报就让 Agent 的每次提交都被拦。映射里的键值对
  （`{"STRIPE_KEY": "sk_live_…"}`）由 `encode_redacted` 按映射判，逐叶脱敏看不见键与值的关系。
- **占位符对所有后续规则不透明**：每条规则在同一份视图上找出要换的区间，相互重叠的并成一段、换成其中最长那条规则
  的占位符（一样长时上下文规则盖住嵌在里面的逐字值，逐字值盖住格式规则）；已经找到的区间与输入里原有的占位符在
  视图里涂成不透明的空白再找一轮，直到找不出新的东西。所以 `redact(redact(x)) == redact(x)`，占位符不会被任何规则
  再次命中，上下文规则（`Authorization`、URL、赋值）也总看到完整的凭证。（T3 实现时从「按占位符切段、规则依次在
  段内匹配」改成这样：依次段内匹配会让嵌着一个已知密钥值的凭证只换掉一半，见决策日志。）用户规则 `RedactionRule(rule_id, pattern)` 的 ID 必须以
  `custom.` 开头且不与内置重名，加载时拒绝能匹配空串或能匹配任一占位符的模式。
- 内核不读环境变量：CLI 保留「从宿主环境里取密钥值」那一段（遍历环境、像不像凭证、是不是存在的路径），交给
  `RuleRedactor(secrets=...)`。CLI 里与协议重名的类删除。
- **逐叶脱敏的编码器：** `encode_redacted(obj, redactor) -> RedactedText` 先对每个字符串叶子（含映射的键）分别脱敏，
  再做规范 JSON 编码（`ensure_ascii=False`、键排序、无多余空白）。trace 的每一个文件（头、span、边、payload 引用、
  回放文档）只经它编码；**从不对编码之后的文本做脱敏**（现状第 8 条）。工具参数（JSON 字符串）先解析、逐叶脱敏、
  再规范编码；解析失败的整串作为一个叶子。
- **类型闸：** `RedactedText` 是一个 final 类，只有 `kernel/redaction.py` 能构造（AST 守卫）；`TraceStore` 的写方法
  只收它，运行时也检查（收到 `str` 报 `ContractViolation`）。脱敏器抛错时这一项什么都不写，计入 D2 的失败计数。
- **span 属性是闭集：** 每个属性键有固定类型（见公开契约）；字符串属性同样走逐叶脱敏；未知工具的名字记成
  `<unknown>`，原名只进 payload。

**D5 守卫扩到全仓：写入点与外发点。**

- 扫整个 `src/syndica`（`__main__.py` 在四个子包之外，它也装配写入与导出），`cli`、`runner`、`kernel`、`backends`
  与包根各一个「必须扫到」的锚文件，再用一次独立的 `os.walk` 交叉核对。
- 安全的内容只认：字面量、模块级绑定字面量且只绑定过一次的名字、脱敏器调用（`redactor.redact(...)`、从
  `syndica.kernel.redaction` import 的 `encode_redacted`）的结果，以及它们的 `.encode(...)` /
  `gzip.compress(...)`。`RedactedText` 写进 fd 只经 `write_redacted`：它在运行时检查类型并把短写接着写完（T4 评审
  证明「标注为 `RedactedText` 的参数」挡不住 `Any`，而 `os.write` 只调一次可能只写进一部分、不报错）。
  `kernel/redaction.py` 之外不许定义或赋值 `redact` / `encode_redacted`，`RedactedText` 只许出现在类型标注与
  `isinstance` 里（不许构造、继承、cast、取别名、当参数传）；它的构造器另要一个模块私有的令牌，`type(x)(...)`
  在运行时同样被拒。
- 外发点：`.post` / `.put` / `.patch` / `.request` / `.stream` / `.send` / `.build_request` / `Request(...)` /
  `urlopen` / 套接字发送的正文按写入点同样检查，关键字与位置参数都看（位置参数只在认得出是 HTTP 的调用上看），
  方法先交给 `partial` / `to_thread` 这类包装再调的也看；套接字、网络日志 handler、`logging.config` 不许用。显式
  白名单两类并写明理由：Agent 自己的工具（`LocalWorkspace` 的写文件与子进程管道、`tools/local.py`）；LLM 客户端
  发给模型提供方的请求（M2 D5 的威胁模型：对话本来就要发给提供方，这是 run 的前提，不是观测数据）。外加
  `write_all` 里那一处把短写写完的 `os.write`。`FileTraceStore` 与 OTLP 导出器不在白名单里：store 经
  `write_redacted` 写，导出器收一个 `Redactor`，发出的正文是 `content=encode_redacted(...)`（从 store 读出的已经
  脱敏，再过一遍是幂等的，守卫因此不必为它开例外）。

**D6 `TraceStore` 是第五个 Store Protocol。**

- 写：`begin(header)`（同一 trace id 第二次 `begin` 报错）、`append(trace_id, spans, edges)`、
  `put_payload(trace_id, RedactedText) -> PayloadRef`（引用是内容的 sha256，由 store 算，不由调用方给；重复的只存一份）、
  `put_replay(trace_id, RedactedText)`、`finish(trace_id, status, ended_ns)`；未 `begin` 或 `finish` 之后的写报
  `ContractViolation`。
- 读：`traces()`（按开始时间、再按 id 排序）、`find(prefix)`（唯一前缀才返回，歧义报错）、`get(trace_id)`、
  `spans(trace_id)`、`edges(trace_id)`、`payload(ref)`、`replay(trace_id)`；`delete(trace_id)`。读到的是快照：改
  返回值嵌套的属性，**再读同一个 trace** 不受影响。
- 同一个 trace 的追加按调用顺序保序；一次 `append` 是原子的（整批出现、不被别的 `append` 插进来，混着坏项的整批一项都
  不写）；同一个 trace id 并发 `begin`、并发 `put_replay` 恰好一个成功；`delete` 之后的写报错（攥着目录 fd 的文件实现
  要自己确认 trace 还在）；payload 的引用属于它的 trace；读回的自由文本是普通 `str`，与引用的 sha256 对不上时报错。
  内存实现进 `kernel/stores/`（`InMemoryTraceStore`，与另两个内存 store 同名法）；文件实现（T11）同样在内核（只用
  标准库），它收一个 `Redactor`：span 与头经 `encode_redacted` 编码、经 `write_redacted` 写；两者过同一套契约。
  spec §4.4 补上第五个 Store。
- 头里的用户规则存成 `RedactionRule` 记录：存成 (id, pattern) 两项时 `encode_redacted` 把它当成一对键值，id 像密钥名
  （`custom.api_token`）的 pattern 会被换掉，回放就重建不出规则集。模型的字段名与字符串属性的键都不许像密钥名（一条
  用例钉住）：否则落盘时值会被换掉。

**D7 文件布局与落盘安全（用户裁决：文件）。**

- `.syndica/traces/<UTC 时间>-<trace id 前 8 位>/`：`trace.json`（头）、`spans.jsonl`、`edges.jsonl`、
  `payloads/<sha256>.txt`、`replay.json`（场景格式 v2 的同一份 schema，捕获写 JSON：内核不能依赖 PyYAML，PyYAML
  读 JSON 也会把非 ASCII 字符弄坏）。目录 0700、文件 0600。记录 trace 时同样确保 `.syndica/.gitignore` 存在：M2 只在
  写交接文件时建它，而记录默认开着，不建的话 `.syndica/` 在用户仓库里显示成未跟踪。
- Agent 运行期间就在写，所以比交接文件更严：`begin` 时以 `O_DIRECTORY | O_NOFOLLOW` 打开 trace 目录，此后所有文件
  经 `dir_fd` 以 `O_NOFOLLOW` 创建；payload 以 `O_EXCL` 创建，`EEXIST` 与读取时都核对 sha256。
- **宿主私有目录（D17）。**

**D8 记录级别（用户裁决：默认「可回放」）。** `--trace off|structure|replay|full`，默认取配置的 `trace.level`，再
默认 `replay`：

| 级别 | span 与边 | 回放文档（回复、工具结果、人的答复、宿主档案、危险判定） | 完整请求 |
|---|---|---|---|
| `off` | — | — | — |
| `structure` | 记 | — | — |
| `replay`（默认） | 记 | 记 | 只记逐单元指纹 |
| `full` | 记 | 记 | 记（逐叶脱敏后） |

回放只需要回复一侧（每轮几 KB），完整请求每轮都带着整段上下文、体量随轮数平方增长，正是 spec §11.3「贵的能力按需
启用」。与 spec 原表（默认只留失败轮次的 payload）不同，spec §11.3 在 T18 同步。trace 只在用户执行 `trace prune`
时删除，不自动删（spec §11.3「结构永久保留」）。

**D9 B-19 收口：并发窗口里不许写轨迹。** 多调用的段在跑、以及**任何**可观察切点在分发（只有一个观察者也一样）时，
`Context` 处于冻结状态，追加块报 `ContractViolation`（三件套：哪个切点 / 哪一段、为什么、改到串行的可拦截切点或
Agent 自己的轮次里写）。冻结用计数而不是布尔（可以嵌套）。**冻结的只是触发切点的那个 Agent 的 `Context`**；Runtime
级的切点不冻结任何 `Context`——`AGENT_SPAWN` 分发时子 Agent 的任务已经建好，冻住它，子 Agent 的第一次追加就会失败。
写进 hook 契约（`hooks/junctions.py` 的 docstring）；`tests/kernel/test_orchestration.py` 里那条从观察者追加的用例改写。

**D9 修订（T10 评审）：轨迹只由内核写。** 上面「串行的可拦截切点里照旧可以写」是错的：写在 `PRE_TOOL_USE` /
`POST_TOOL_USE` 里的块落在一次调用与它的结果之间，提供方会拒掉下一次请求，hook 也无从知道自己是不是一段里唯一的
调用；写在 `PRE_LLM_REQUEST` / `POST_LLM_RESPONSE` 里的块不在这一次请求里、却排在回复之前，轨迹会说模型看过它
其实没看过的东西。改为：带 `ctx` 参数的切点分发（可拦截与可观察都算）与每一个工具段（单个调用也算）都是窗口；
窗口记在 contextvar 里、跟着代码走——窗口里跑的代码与它起的任务不许追加；别的任务开的窗口不冻结 Agent 自己的
循环（扩展切点由声明它的能力在任意任务里触发）；轨迹的拷贝不带窗口（观察者在轮末做的快照不会永远冻住）；Agent
的驱动任务从没有窗口开始（否则一个在自己的观察者里被叫醒的 Agent 写不进自己的轨迹）。正确的写法：改一次调用的
产出用 `POST_TOOL_USE` 的 `modified['result']`，告诉模型一件事用 `Runtime.send`，改这一次请求用
`PRE_LLM_REQUEST` 的 `modified['request']`。

**D10 可复现的 id 与稳定键。**

- 内核 contextvar「当前工具调用」（agent id、周期、turn、批内位置、模型给的 id）。执行器自己不知道 agent、周期与
  turn（turn 在 `agent.py`，周期只在 `runtime.py`），所以由 Agent 把（agent id、周期、turn）交给执行器，`_execute_one`
  在 `PRE_TOOL_USE` 之前设、执行完复位；`_drive` 一开始就清掉它——驱动任务经 `create_task` 建在发送方的上下文里，
  不清就继承了别人的「当前工具调用」。`spawn` 与 `send` 从它取键；人机请求的事件补上同样的键（`request_ref`；字段
  名不能像密钥名——`key`、`*_key` 下的值落盘前被逐叶脱敏换掉，agent id 就没了）。工具调用里 `create_task` 出来的
  任务继承这个上下文，因此它们创建的东西也算这次调用的、共用它的计数（T7 的 span 归属同理）：几个子任务都在一次
  取决于时延的 await 之后才派活，序号随到达的先后——要并发地派，先按确定的顺序派完再等。
- `Runtime(..., ids=None)` 收一个 id 源（默认 uuid4）。**`spawn` 在调用工厂之前**从 id 源取得 agent id，经内核私有的
  contextvar 交给 `BaseAgent.__init__`（没设时照旧 uuid4）：工厂里就已经有东西绑定 `agent.agent_id`（Delegate / Fork
  工具、scenario 的选择器表），工厂返回之后再改，`spawn(parent_id=<旧 id>)` 会被注册表拒掉。message id 同样从 id 源
  取。稳定键 =（出处，第几个）：出处依次是当前工具调用；否则是当前**属主**——一个 Agent 自己的事件循环
  （`('agent', id)`，它的 hook 派的活、发的消息；计数放在 slot 上，zombie 被唤醒后接着数）、一次拆除
  （`('despawn', id)`：`AGENT_DESPAWN` 的 hook 在这里跑，因为几条并发拆除里谁取得收据取决于调度）、一次 `kill` 的
  收尾（`('reap', id)`，只为清掉继承来的那次工具调用）、关机（`('shutdown',)`），各自串行，序号与别的 Agent 的
  快慢无关；都没有时是宿主（第几次）。经拓扑槽创建的 Agent：on-demand 槽每要一次都新建，实例属于要它的那次调用，
  键是（槽名、键、调用方的出处键）；singleton 与 keyed 的实例由槽与键确定，谁先来寻址都是同一个，键是（槽名、键，
  第几个）且不占调用方的计数——带上先到者，回放时换一个先到者它就换了 id。人的答复 =（它答复的那个请求的键，
  `reply`）：请求按（等待方、关联 id）登记，登记在 `wait_for` 入口、任何 await 之前（会挂起的注册表下，转 WAITING
  的 CAS 期间答复就可能到达——回放按事件里的键当场作答）；发出请求事件与调用 `wait_for` 之间不得有挂起点。同一个
  （等待方、关联 id）下的登记按先后入栈：答复取最新的一个，被拒或被抢占的等待只摘自己那一个。按键而不是按全局
  顺序：几个 Agent 并发时创建顺序取决于调度。键在结果可能取决于调度的检查**之前**占用：spawn 在关机与拆除这两道
  拒绝之前、投给已经不在的收件人的那一条、被拒的等待都占掉序号。边界：宿主自己的代码并发地 spawn / send 时，宿主
  那一档的序号随到达的先后；键只在一个 Runtime 之内唯一，一个 id 源给几个 Runtime 用时由它自己按 Runtime 分开
  （T6 评审、复评）。
- 后台命令的 handle 与 `request_id` 不需要复现：handle 原样在记录的工具输出里，工具按记录回放；`request_id` 不进
  模型的上下文。

**D11 回放：外部世界按记录给出，内核全真并逐项比对（用户裁决）。**

- **模型：** `ReplayLLM` 按（Agent、turn、第几次调用）给出记录的回复——含首次调用与 `RetryModule` 的每次重试、`extra`、
  带类型的错误。工具调用的参数存规范编码（逐叶脱敏后的规范 JSON，解析不了的原串作为一个字符串叶子）：原样存字节
  会把截断的私钥块漏进 trace，而规范编码改了键序，所以指纹与 T14 的比对都按解析后的参数。键从内核 contextvar
  「当前 LLM 调用」（agent id、turn、第几次）取：`create(messages, **kwargs)` 不带这些，CLI 按模型名共用模型对象，
  `RetryModule` 的备用模型跨 Agent 共用。Agent 在每次 `llm.create` 前设它，`RetryModule` 每次重试递增。原始 run 在某次
  调用上被打断（kill、shutdown、Ctrl-C）的，回放在同一处阻塞，直到被同样地取消（见下面「宿主的动作」）。
  `RetryModule` 的等待在回放里注入成不等。
- **指纹：** 原始 run 对每次请求按单元（模型参数、系统提示词、工具定义、每条消息）取指纹：每个单元先逐叶脱敏（D4）
  再规范编码再取 sha256，消息里工具调用的参数先解析再规范编码。`trust` 不进指纹（不发给提供方）；`extra` 与提供方
  身份也不进：`extra` 里有随提供方而变的部分（`llm_route`、`usage`，以及 OpenAI 兼容客户端回传给提供方的
  `extra_content`），回放不发请求，只比中立的单元。指纹按单元原文的 sha256 缓存：每轮把整段上下文重新脱敏是轮数的
  平方（实测一个 5 KB 的单元 0.69 ms，50 轮约 3.5 s，跑在事件循环上）。回放用**记录下的规则集**对自己的请求做同样的
  计算，第一个不一致的单元即失败并报出 Agent、轮次与单元；`--allow-divergence` 时只记录。规则集 =（`RULES_VERSION`、
  内置规则 ID 的顺序、用户规则的 (id, pattern)），不含宿主环境里的值；`trace.json` 记下用户规则的 (id, pattern) 与
  摘要；`RULES_VERSION` 由一份黄金语料钉住（内置规则的行为一变就必须升版本）；摘要不一致单独报一种分叉。
- **工具：** 触达外部世界的工具（`Read Write Edit Glob Grep Bash BashOutput KillBash`）与输出取决于时机的
  `QueryAgent` 按工具调用键返回记录的结局（`ToolResult` 的输出、`meta`、`trust`，或「工具抛了某个异常」），不碰磁盘、
  不起进程；`Delegate Fork SendMessage KillAgent` 与人机工具真跑——它们是内核。
- **跳过：** 原始 run 里被 `PRE_TOOL_USE` 跳过的调用（CLI 的 git 守卫、`--no-write` 的只读守卫、第三方 hook）记成
  `skipped` 并带最终结果；回放装一个内核侧的 `PRE_TOOL_USE` hook，按调用键原样再跳过。否则这些调用在回放里会走到
  危险闸，被 `untrusted_high_risk` 拦下、给出不同的文字，下一次请求的指纹必然不同。
- **危险判定与处置：** 回放从宿主档案按顺序重建规则（规则 ID 加参数，工作区根记字面值与解析后的值；
  `path_outside_workspace_rule` 与 `workspace_root_removal_rule` 加仅限关键字的 `real=`，回放直接用记录的解析结果，
  不在回放的机器上重新解析符号链接）、`RuleTablePolicy`
  的表与 Flag，**真跑** `disposition_of` 与策略；原始 run 记下每次闸门判定（命中的规则、风险等级、溯源、由 Flag 还是
  策略决定、处置），回放逐项比对，不一致、缺少或没被消费的都算分叉。只有人的答复按记录给出。这样丢掉 `UNTRUSTED`
  标记、改坏判定器的改动会在回放里现形（`trust` 不进请求字节，只比轨迹文本看不出来）。
- **人与宿主的动作：** 确认与 AskUser / AskUserQuestion 的答复按请求的键给出。宿主自己的动作单独记进 `host_actions`
  并在原处重放：提示时读到 EOF 触发的 `runtime.abort` 按它所在的请求记——中止标记会活过那次等待、把下一轮也结束成
  ABORTED，只给结局不给标记，Agent 就会去要一条从没记录过的回复；Ctrl-C 与关闭记在 run 安静下来的那一处。超时经
  回放注入的时钟 / broker 钩子触发，不真的等，也不靠桩 broker（那会触发 `runtime.py` 里一条假的 ERROR）。`preempted`
  由内核自己产生，回放比对而不注入。
- **宿主档案：** Flag、危险规则与处置表、输出上限、HostFacts、`--max-turns`、`--no-write`、模块名单、拓扑与入口方式
  （含 `entry.trust`）、冻结后的角色（提示词正文、工具顺序、轮数、类式角色的 `impl` 只认内核内置类与白名单）。
- 每一项记录都必须被消费：回放结束时还有没用上的回复、工具结局、人的答复、闸门判定，算分叉。

**D12 场景格式 v2：一份 schema，手写与捕获共用。** schema 由内核的一个模块拥有（纯映射 + 校验，不依赖 PyYAML）；
runner 读手写的 `.yaml`（PyYAML）与捕获的 `.json`（标准库 `json`），都交给同一个校验。v1 照旧可读；v2 是它的超集
（键见公开契约）；自由文本的值可以内联，也可以写成 `{payload: <sha256>}` 引用 payload 文件（捕获一律引用，免得
YAML 的标量把 `yes`、`0777` 变成别的类型）。捕获的 `expect` 填上原始 run 的结局（各 Agent 的状态、停止原因、错误、
最终回答），回放「复现」的判据是 `expect` 全部成立且没有分叉。手写 v2 场景不给 `tool_results` 时工具照 v1 真跑。

**D13 LLM 侧。**

- OpenAI 兼容客户端解析 `usage`：`extra['usage'] = {'input_tokens', 'output_tokens', 'cached_input_tokens'}`（端点
  不给的记 `None`），以及 `extra['finish_reason']`；这些不发给模型（逐字比对发出的 JSON）。
- `ModelRouter` 保留 `extra['llm_route']`，把同一次调用里换掉的平台记进 `extra['llm_failures']`（平台名、错误类型、
  HTTP 状态）；实现 `CachePrefixProvider`，转发给最近一次作答的成员（路线图原文），不发生换平台时漂移检测是精确比对。

**D14 OTLP/HTTP JSON 导出（用户裁决：httpx，无新依赖）。**

- backend `syndica.backends.otel_otlp`：`OtlpHttpExporter(endpoint, *, headers=None, client=None)` 只从 `TraceStore`
  读（因此只发出脱敏后的内容），转成 OTLP/HTTP JSON 发往 `<endpoint>/v1/traces`：十六进制 id、`kind` INTERNAL、
  Unix 纳秒、带类型的属性、状态、span 事件；因果边转成 span links。新 extra `otel = ["httpx>=0.27"]`；由
  `__main__.py` 注入 CLI。
- **端点只从用户自己的环境或命令行来**：`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `OTEL_EXPORTER_OTLP_ENDPOINT` 设了就
  在每次 run 结束时导出，或用 `trace export <id> --otlp <url>` 手动导出。**项目配置不能设端点**：M2 D15 承认会在不
  信任的仓库里运行，仓库选的端点会收到 trace 的结构（命令、路径、工具名）与用户的鉴权头。`OTEL_EXPORTER_OTLP_HEADERS`
  只随环境里给的端点发出；明文 HTTP 的非回环地址不带头；头里的值进脱敏器的 `secrets`。run 结束时的自动导出失败只
  告警、不影响退出码；`trace export` 是用户点名要做的事，失败退出 1。

**D15 CLI。**

- `run` 与 `chat` 收 `--trace`；`trace.level` 与 `redaction.rules` 在 run 开始时快照（Agent 中途改 `.syndica.yaml`
  不影响）。结束时 stderr 打出 trace 目录（经脱敏与转义）；`--handoff` 的交接文件正文写上 trace 目录（front matter
  不变）。`chat` 的一个会话是一个 trace，M3 里最高只记 `structure`：`:use <id>` 能把消息发给任何 Agent，回放要按
  消息记目标，这与会话恢复是同一件事，随 M6 的会话持久化做；要的级别更高时降到 `structure` 并在 stderr 说一句。
- `trace list`、`trace show <id>`、`trace replay <id> [--allow-divergence]`、`trace export <id> --otlp <url>`、
  `trace prune [--keep N]`（默认 50）。`<id>` 接受完整 id、唯一前缀或目录名。
- `trace replay` 本身也记一个 trace（`structure` 级，新的 trace id，`source.replay_of` 指向原 trace）。退出码：复现
  原始结局且没有分叉 0；有分叉或结局不同 1；回放文档读不进来、版本不认识 2（与 M2 的「参数错误」同一类：交给它的
  输入不能用）。回放引擎在 runner 里，由 `__main__.py` 注入 CLI。
- **Ctrl-C：** 退出 130，优先于 1 与 3（中断本身就是没完成的原因）；stderr 没有 traceback。run 或复核期间被中断：
  照样写交接文件，`status: run_failed`，原因「被中断」，复核标 `skipped`；git 收尾开始之前任何时候被中断都不做 git
  收尾（写交接文件期间按的只记下，不改 status）。git 收尾时被中断：第一次按键当场停下 git，照 M2 D13 磁盘上的文件停在
  `git: pending`；之后的按键不打断收拾（停钩子、撤回为提交暂存的新文件，都有时限），打印的那份与 stderr 写明宿主看到
  的仓库状态（提交了没有、暂存区收拾了没有）。结论定了之后（git 收尾结束或不做）按键什么都不改，也不再强制退出；结论
  定下之前、git 收尾之外，第三次按键强制退出。`run --handoff` 期间 SIGINT 由宿主自己按阶段处理，不用 `asyncio.Runner`
  那一套（取消主任务在同步代码里只留下一个 Python 3.11 撤不掉的挂着的取消）。宿主读人的答复只经一个读者：问题过期后
  还卡着的那次 readline 读到的行，早于下一次提问就丢掉，晚于就交给它。trace 以 `cancelled` 收尾。

**D16 M2 遗留（用户裁决：先做）。**

- **危险规则（内核，T1）：** 共享分词把 shell 的 `-c` 参数（`sh bash dash zsh ksh`，包括 `-lc`、`-ec` 这类组合选项）
  与 `eval` 的参数当成一段命令递归判定；`xargs` 进 wrapper，带一张选项取值表（`-n -I -P -L -s -d -E -a` 等）；`find`
  的 `-exec -execdir -ok -okdir` 到 `;` 或 `+` 之间是一条命令；`find` **不加收窄地**删掉根或根下全部进
  `shell.workspace_root_removal`：按 find 的求值规则，看每一次删除（`-delete` 或 `-exec rm {}`）所在的「与」链在它之前
  有没有收窄的测试（`-o` 与 `,` 断开链，括号每一支都收窄才算收窄，取反的与对谁都成立的测试不算）——
  `find . -name '*.pyc' -delete`、`-prune -o -name '*.js' -delete` 很常见，在开发期被无条件拦下就没有别的写法可走。
  递归深度有上限，超过上限**按命中处理**（宁可多判）。与 HEAD 差分：新增命中逐条过目，少认的逐条给出理由。
- **git 收尾的摘要（CLI，T2）：** 每个改动路径前面一栏给出 `+N −M`（`git diff --numstat`，新文件数行数，二进制写
  「二进制」，数不出写 `?`）。放在路径前面、数不出也占位：文件名可以写成像一个计数的样子，固定的一栏它占不了。
- **Ctrl-C（CLI，T2）：** 见 D15 末条。

**D17 宿主私有目录。** `LocalWorkspace(..., private=())` 收一组相对根的目录（CLI 传
`('.syndica/traces', '.syndica/handoffs')`）；文件工具（Read、Write、Edit，以及 Glob / Grep 的显式路径）拒绝这些目录下
的路径，隐式的 Glob / Grep 结果里也不出现；比较不分大小写，照写的路径、按字面折叠 `..` 之后的路径（含经根的别名给出
的绝对路径）与解析之后的路径都看（与 M2 的 `.git` 保护同样做法），给模型的错误说明为什么。Write 也不许在这些
目录的上级位置建文件（`.syndica` 成了文件，宿主就建不出 trace 目录）。理由：trace 里存着此前
标成不可信的工具输出，`Read` 读回来是可信的，等于绕过 spec §12.2 的溯源升级；Agent 改得到以前的交接文件（M2 遗留）
也一并关掉。不整个关 `.syndica`：本仓的 Worker 提示词让它把长输出存到 `.syndica/tmp/` 再读（`docs/agents/worker.md`，
由 `tests/cli/test_repo_config.py` 钉住，T18 就跑这份提示词）。Bash 照样读写得到（它的输出本来就是不可信的）；
Bash 写下的文件再经 `Read` 读回就成了可信内容，这条洗白的路 D17 不关。所以这条挡的是失手与读回 trace，不是蓄意
（真正的边界是 M5 的沙箱）。

---

## 公开契约

### CLI 与环境变量

| 用法 | 语义 |
|---|---|
| `run|chat --trace off\|structure\|replay\|full` | 记录级别，默认 `trace.level` 或 `replay`（`chat` 最高 `structure`） |
| `trace list` | 列出 trace（UTC 时间、id、命令、状态、Agent 数、span 数、大小） |
| `trace show <id>` | 时间线与三个回答：谁创建了谁、谁在等谁、最慢的工具调用 |
| `trace replay <id> [--allow-divergence]` | 回放；0 复现、1 分叉或结局不同、2 回放文档不可用 |
| `trace export <id> --otlp <url>` | 导出为 OTLP/HTTP JSON |
| `trace prune [--keep N]` | 只留最近 N 个，默认 50 |

退出码在 M2 的 0 / 1 / 2 / 3 之外加 `130`（被 Ctrl-C 中断，优先于 1 与 3）。CLI 读的环境变量：
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`、`OTEL_EXPORTER_OTLP_ENDPOINT`、`OTEL_EXPORTER_OTLP_HEADERS`（内核不读）。

### `.syndica.yaml`

```yaml
trace:
  level: replay                 # 可选：off | structure | replay | full
redaction:
  rules:                        # 可选：用户追加的脱敏规则
    - {id: custom.internal_host, pattern: 'corp-[a-z0-9]+\.internal'}
```

未知键、类型错误、不合法的正则、ID 不以 `custom.` 开头、与内置重名、能匹配空串或占位符，都报 `ConfigError`（三件套）。

### trace schema `syndica.trace/1`

- `trace.json`：`schema`、`trace_id`、`started_ns`、`ended_ns`、`status`（`ok error cancelled`）、`level`、
  `redaction`（规则集的 sha256、`RULES_VERSION` 与用户规则的 (id, pattern)）、`source`（命令行参数、配置文件的 sha256、
  syndica 版本、git commit、
  `replay_of`）。所有字符串经逐叶脱敏。
- `spans.jsonl` 每行一个 span：`span_id`、`parent_span_id`、`kind`、`name`、`agent_id`、`start_ns`、`end_ns`、
  `status`、`attributes`、`events`（`[{name, time_ns, attributes}]`）。`edges.jsonl` 每行一条边：`kind`、
  `source_span_id`、`target_span_id`、`attributes`。
- span 种类 `agent turn llm_call tool_call confirmation wait`（`module` 保留）；边 `spawn message`（`artifact
  verification` 保留）。
- 属性（闭集，键 → 类型）：`agent.role` str、`agent.cycle` int、`turn` int、`stop_reason` str、`llm.platform` str、
  `llm.model` str、`llm.attempt` int、`llm.input_tokens` / `llm.output_tokens` / `llm.cached_input_tokens` int 或 null、
  `llm.request_digest` str、`tool.name` str（未知工具为 `<unknown>`）、`tool.call_id` str、`tool.index` int、
  `tool.outcome` str、`tool.exit_code` int 或 null、`tool.timed_out` bool、`danger.rule_ids` list[str]、
  `danger.risk` str、`danger.provenance` str（`trusted untrusted`）、`danger.decided_by` str（`flag policy`）、
  `danger.disposition` str、
  `confirmation.outcome` str（取内核 `ConfirmationOutcome` 的值：`approved rejected timeout preempted aborted`）、
  `wait.kind` str（`agent human`）、`wait.target` str、`wait.outcome` str（取 `WaitState` 的值）、`message.id` str、
  `message.to` str（发送事件上）、`child.agent_id` str（派生边上）、`agent.id` str（拆除事件上）、`llm.error` str 与
  `llm.status` int 或 null（平台故障事件上）。`wait.target`：人等
  的是请求键（JSON；宿主直接发起、没有请求键的是空串），Delegate / Fork 等的是子 Agent 的 id，等 `Reply` 的是关联
  id。`danger.rule_ids` 没有规则命中时是空表。
- payload 引用（种类 `payload`：同一个 trace 里一份 payload 的 sha256，64 个小写十六进制；`replay` 级起才有）：
  `llm.request_units`（逐单元指纹的列表）、`llm.request`（`full` 才有：`{messages, params}`）、`llm.response`
  （`{kind: reply, role, content, tool_calls: [{id, name, arguments | arguments_raw}], tool_call_id, extra, trust}`，
  `arguments` 是解析后的值，解析不了的原串记成 `arguments_raw`（原串 `ls` 与解析出来的字符串 `"ls"` 由此分得开）；或 `{kind: error, type, message, status_code?, retry_after?}`，`type` 是 `模块.限定名`）、`tool.result`
  （工具自己交回的、`POST_TOOL_USE` 改写之前的 `{kind: result, output, meta, trust, artifacts}`，或它抛出的
  `{kind: error, …}`；被 `PRE_TOOL_USE` 跳过的调用是跳过它的那个结果；内核自己写的结局——未知工具、坏参数、
  阻断、确认的结局——没有，回放时内核照样写得出来）、`wait.answer`（只在人等的 span 上：`{kind: answer, sender,
  content, trigger, trust}`，不记关联 id；宿主直接发来的消息与等待过期后才到的答复是宿主的动作，进 `host_actions`）。
  被取消的调用没有回复也没有错误。值里 JSON 表达不了的写成文字：环写成 `<cycle>`，一份 payload 超过 10 万个节点的
  部分写成 `<truncated>`，超过 8192 位的整数写成十六进制，默认 repr 写成类型名（带内存地址的文字每次运行都不同）。
- 脱敏：span 名、字符串属性（含列表的每一项）、事件、边的属性、payload、头里的命令行参数逐叶脱敏；内核生成的标识与
  摘要（`agent.id`、`child.agent_id`、`message.id`、`message.to`、`llm.request_digest`、逐单元指纹的列表）、`payload`
  引用与有固定取值的属性原样记——用户规则可能认得一段十六进制或一个词，换掉之后引用就断了、span 就校验不过。
  `wait.target` 按自由文本脱敏（等 `Reply` 时它是调用方给的关联 id）：用户规则认得 id 时它会被换掉，Agent 之间的
  关系以边为准。
- 不完整的 trace：store 失败、或一条记录编码不出来时，recorder 只丢那一条（计入 `dropped`，记一条 ERROR）并且不写
  结局——头里 `status` 为空的 trace 是记录到一半就断了的，回放与导出据此拒绝它。
- 事件：`message.sent`（发送方的 span 上，投递成功才记，带 `message.id`、`message.to`）、`agent.despawned`（发起拆除
  的 span 上，带 `agent.id`；宿主、EXIT 收尾、kill 的收尾、关机发起的拆除不在任何 span 里，不记）、
  `llm.platform_failed`（llm_call span 上，同一次调用里每个被换掉的平台一个，带 `llm.platform`、`llm.error`、
  `llm.status`）。
- `tool.outcome` 取值：`ok error aborted judge_failed not_executed skipped blocked rejected timeout preempted
  confirmation_aborted no_channel unknown_tool bad_arguments interrupted`，与追加结果块的位置一一对应：`rejected
  timeout preempted confirmation_aborted` 是确认的四种结局（`_confirmation_result`）；`aborted` 是一次调用抛出、整批
  停下的那一块，同批停下之后的与第 9 步中止补的都是 `not_executed`（工具都没开始跑）；`interrupted` 是恢复时修补
  被打断的批次（`agent.py` 的 `_repair_interrupted_batch`，在执行器之外，插桩点随之扩到那里）。结局说的是这次
  执行：同段的兄弟被取消时整段的结果块都不落，已经跑完的那一次仍记它自己的结局，恢复时补上的另有一个
  `interrupted` 的 span（T7 评审）。
- 版本升级要有读旧版本的兼容测试；枚举的成员→取值表由一条整体断言钉住。

### 场景格式 v2（手写 YAML 与捕获 JSON 共用一份 schema）

在 v1 之上：`version: 2`；`host`（`flags`、`danger.rules`（有序，`{id, params}`）、`danger.table`、`output_limit`、
`host_facts`、`max_turns`、`no_write`、`modules`）；`agents.<Role>` 可以用 `frozen`（`system_prompt` 正文、`tools`、
`max_turns`、`impl`）代替 `manifest`；`instances` 带稳定键（`key: {tool_call: …}`、`{host: n}`、`{slot, key, n}`）；
回合的 `content` 可以是列表、带 `extra`、`tool_calls[].arguments`（解析后的任何 JSON 值，按 D11 编码；解析不了的原串写
`arguments_raw`）、带类型的 `raises`、
`request_digests`；实例的 `tool_results`（按调用键：`output`、`meta`、`trust`，或 `raised: {type, message}`）、
`skipped`（按调用键的最终结果）、`gate`（按调用键的闸门判定）；`responses` 按请求的键，`kind` 加 `timeout`；
`host_actions`（`abort` 按请求的键，`interrupt` 与 `shutdown` 按它发生的位置）；`ids`（稳定键 → 记录的 id）；
`entry.mode`（`query send slot`）、`entry.trust`；拓扑槽不再受 v1 的限制；`source`。
自由文本可以内联或写成 `{payload: <sha256>}`；占位符 `[REDACTED:…]` 是合法内容。

### 内核 API 增量（都向后兼容）

- `Runtime(..., tracer=None, ids=None)`；`LocalWorkspace(..., private=())`；`path_outside_workspace_rule(root, *,
  real=None)` 与 `workspace_root_removal_rule(root, *, real=None)`。
- `syndica.kernel.tracing`：`Tracer`、`NullTracer`；`syndica.kernel.trace_recorder`：`TraceRecorder`；
  `syndica.kernel.trace`：`Span`、`SpanEvent`、`CausalityEdge`、`SpanKind`、`EdgeKind`、`SpanStatus`、`TraceLevel`、
  `TraceHeader`、`TraceRedaction`、`TraceSource`、`PayloadRef`、`AttributeValue`、`ATTRIBUTE_KINDS`、
  `ATTRIBUTE_DOMAINS`；`syndica.kernel.protocols.trace.TraceStore`；`InMemoryTraceStore`、`FileTraceStore`；契约套件
  `TraceStoreContract`；回放文档的 schema 模块。除 `ATTRIBUTE_KINDS` / `ATTRIBUTE_DOMAINS` 外都从 `syndica.kernel`
  导出。
- `syndica.kernel.redaction`：`Redactor`、`RuleRedactor`、`RedactionRule`、`RedactedText`、`encode_redacted`、
  `write_redacted`、`write_all`、`Finding`、`RULE_IDS`、`RULES_VERSION`、`looks_secret`；不进 `kernel.__all__` 的
  `redact_argv`（命令行参数，`--api-key <值>` 两项一对）与 `encode_digests`（只收 sha256 的不脱敏编码）。
- 哪些进 `kernel.__all__`、哪些进 `_WITHHELD`，在各自任务里按「宿主不用它就做不成某件事」判定并写进
  `tests/kernel/test_public_api.py`。

---

## 任务与进度

每个任务：先写红测试 → 实现 → 定向测试 → 全量门禁 → 自我变异（清 `__pycache__`、新文件先 `git add`、sha256 基线
覆盖本轮会改到的每个文件）→ 对抗验证（每个任务一路；T3、T7、T13、T14 两路）→ commit → push。集合与顺序类的性质
一律用基数 ≥ 3、非单调顺序的固定装置。

### 阶段一：M2 遗留与脱敏收口

#### T1 — 危险规则认出 `sh -c` / `eval` / `xargs` / `find` 里的命令

**Files:** `kernel/danger.py`、危险规则的测试；差分脚本不入库。

- 见 D16。**每个成员各一条用例**：`sh bash dash zsh ksh` 各自的 `-c`、组合选项 `bash -lc`、`eval`、`xargs` 选项表里
  每一个吃取值的选项、`-exec -execdir -ok -okdir` 与两种终止符、`find . -delete` 与 `find "$PWD" -delete`；默认规则
  因此多认出的（`xargs rm -rf`、`sh -c "curl … | sh"`）。递归深度上限 + 1 的输入按命中处理。
- 变异：去掉任一个 shell 名、任一个 xargs 选项、`-ok` / `-okdir`；`-c` 只认完全相同的写法；超过深度上限时放行。

**状态：完成（aff29a4）。** 共享分词在最外层把转手执行的命令（shell `-c` 的代码、`eval` 的实参、`find` 每个动作的命令）拆成段接在
全部段之后，一层层往下，第 8 层还在转手就接一段 `_TOO_DEEP`，七条规则都按命中处理；`shell.remote_script_execution` 对
转手的文本从替换开始整个再扫一遍。shell 的选项认不清时把余下每个操作数都当代码。`xargs` 进 wrapper；有选项表的
wrapper 启动的是不执行实参的程序（`_INERT_PROGRAMS`）时，它的实参不再算候选（`xargs grep -n git` 不再撞 git 闸），
启动 runner（`strace`、`caffeinate`）时照旧。`find` 按求值规则判一次删除是不是落在几乎全部文件上（`_FindExpression`）。
删根只看每段第一个 `rm` 与第一个 `find`，判定保持线性。

验证：与 HEAD 差分（三份危险规则测试里的字符串 × 五种包装，8110 条），新增命中都来自转手的命令；少认的只有
`timeout <时长> grep -n git` 一类（原来的误报，7 条）与 `xargs` 选项之后的绝对路径程序（2 条，与 `timeout 60 /usr/bin/true`
同样豁免）。变异 246 条全杀。对抗评审两轮：第一轮 1 Critical（删根判定平方时间，`rm` 那一支 HEAD 就有）、2 High（`-o`
一律当放宽、拦下多模式清理；认不出的 shell 选项让 `-c` 的代码对七条规则隐身）、2 Medium；复审 1 Critical（直接跳到
wrapper 启动的程序，`nice strace rm -rf /` 这类 runner 后面的命令对默认规则隐身）、1 High（多起点 × `-path` 是平方）与
一批 Low，全部修掉并补了用例。

意外发现：从命令里取来的正则交给正则引擎，`-regex '(a+)+'` 配一个长起点就让判定回溯成指数时间，`-regex` 改成只认
几种「全部」的写法。复审的那条 Critical 出自第一轮的修复本身：为一类误报收窄候选命令词，换来的是所有规则的漏报——
「宁可多判」要在每一层都成立，收窄只能落在确定不执行实参的程序上。

#### T2 — git 摘要的增删行数；Ctrl-C 中断照样留交接文件

**Files:** `cli/git_finish.py`、`cli/host.py`、`cli/main.py`、对应测试。

- 摘要每个路径带 `+N −M`；二进制、新文件、符号链接各一条用例；三个以上路径、非字典序。
- Ctrl-C：run 期间、复核期间、git 收尾期间各一条用例（D15 末条的三种情形），退出 130 且优先于 1 与 3；stderr 没有
  traceback；临时目录与子进程照样收拾。

**状态：完成（17651f3）。** 摘要的行数在路径前面固定的一栏（`line_counts`）：跟踪的文件按 `git diff --numstat -M --relative HEAD`，
新文件由宿主数（只读到打开时的大小、开头 8000 字节里有 NUL 就停、超过 512 MiB 不数、不跟符号链接、打开之后再认一次
类型），git 没认出的改名新旧两半合成一条。Ctrl-C：`run --handoff` 期间由宿主的 `_CtrlC` 按阶段处理（干活时取消干活的
子任务；收尾时只记下；git 收尾时第一次按键当场停下 git，之后的按键不打断收拾；结论定了之后什么都不改；结论定下之前
三次强制退出）。宿主读人的答复只经一个读者 `_Lines`：readline 在守护线程里跑、同一时刻至多一个，读到的行只交给开始
等之后的那次提问——被取消的等待不再拖住退出，过期的提问也不再吞掉 git 收尾的答复、让 git 提问上的 Ctrl-C 失灵。
`_stop` 认 macOS 对僵尸进程组的 EPERM、SIGKILL 放进 finally；git 收尾被打断时抛 `GitInterrupted`：HEAD 没动才撤回为
提交暂存的新文件（动了就是提交已经做了，撤回会把它暂存成删除），打印的那份与 stderr 写明宿主看到的仓库状态。顺手修了
同一处两个原有的问题：问题超时、Agent 先结束时 run 结束后不再等人敲回车；读人的答复那一侧的错误不再顶替 run 自己的
错误。

验证：真的 SIGINT 打在子进程 CLI 上（run、复核、git 提问、写交接文件时、连按两次、git 提问上连按三次、普通 run、
AskUser、chat、问题已超时、问题超时之后的 git 提问与它上面的 Ctrl-C）各一条用例，修复前全部失败；进程内直接调宿主
装上的处理，逐个阶段按键。对抗评审两轮。第一轮：1 High（git 收尾前后各有一个几毫秒的窗口，按键在前被忽略、照样提问
并提交，在后丢掉 stdout 上的交接文件且退出码与文件不符）、4 Medium（macOS 上 EPERM 把 Ctrl-C 变成「提交起不来」；
第二次按键留下不理 SIGTERM 的钩子；连按两次写不成交接文件；提交被打断时新文件留在暂存区）与一批 Low。第二轮复审改过
的设计：0 Critical / 0 High；2 Medium（问题过期之后那次还卡着的 readline 吞掉 git 收尾的第一个答复，git 提问上的
Ctrl-C 也停不下来；强制退出按累计次数算，已经起过作用的按键也算进去：git 提问上连按三次丢掉 stdout 上的交接文件，
钩子跑着时连按打断撤回暂存）；3 Low（git 收尾开始前一刻的按键报成「git 收尾被中断」；泵的错误顶替 run 自己的错误；
结论交回之后、打印之前的按键见风险一节）与 7 条它自己发明的存活变异。除结论交回与打印之间的那个窗口外全部修掉并补了
用例；解释器退出途中的按键（退出码成了 -2，或打出 traceback）也一并关掉：宿主关掉之后入口忽略 SIGINT。子进程用例
改走真的 `entrypoint`。变异：第一版我自己的 69 条（git_finish 32、host 37）全杀；复审之后为新代码与评审的存活变异
写的 39 条（git_finish 17，host、main 与入口 22）全杀，评审的 7 条存活变异各有一条单独施加即变红的用例（大文件消息
那条原有用例就杀得掉，是评审选的用例没有覆盖到它）。零存活只对这些变异集成立。

意外发现：`asyncio.Runner` 的 SIGINT 处理只取消主任务，而 Python 3.11 的 `Task.uncancel` 撤不掉一次落在同步代码里的
取消（3.13 才改），协程一返回写好的结论就被换成 KeyboardInterrupt——第一版拿 `uncancel` 打补丁，评审证明窗口照样在；
根本的修法是 Ctrl-C 从不取消主任务。

#### T3 — 内核统一 `Redactor`：幂等、逐叶编码器、用户规则、`secret.named_assignment`

**Files:** `kernel/redaction.py`、`cli/redaction.py`（只剩从环境取密钥值）、`cli/environment.py`（词表移走）、
`cli/config.py`、`cli/host.py`、`cli/git_finish.py`、`cli/handoff.py`、测试。

- 见 D4。迁移前用 HEAD 的实现在一批样例（规则 × 上下文的交叉：行首、引号里、URL 里、JSON 字符串里、截断）上生成
  对照；迁移后逐字相同，**唯一的例外**是 M2 不幂等的那几类输入，逐条列出新的输出与理由。
- 幂等：对整批样例 `redact` 两次与一次相同；占位符不被任何内置或用户规则再次命中。
- `encode_redacted`：一个 PEM 块、带引号与非 ASCII 的环境变量值、`user:pass@` 的 URL 分别放进嵌套的映射与列表里，
  编码后既不出现原文、也仍是合法 JSON，且解码后在原位置是对应的占位符。
- 用户规则：合法、不合法正则、ID 前缀、与内置重名、匹配空串、匹配占位符各一条。
- `RULES_VERSION` 与一份黄金语料：语料的输出变了而版本没升，测试失败（D11 的规则集摘要靠它）。
- `named_assignment`：在 `src/` 与测试固定装置里零命中（误报门槛，过不了就推迟这条规则并登记）；不进 `scan`；映射
  `{"STRIPE_KEY": "sk_live_…"}` 经 `encode_redacted` 后值被替换。
- 变异：漏掉一条内置规则；用户规则排在内置之前；去掉占位符切段（幂等破坏）；`encode_redacted` 改成先编码后脱敏；
  `named_assignment` 的值判定放宽到任意值（误报）或收紧到只认固定格式（漏报）。

**状态：完成（06cf340）。** 按修订后的 D4 落地：每条规则在同一份视图上找区间，相互重叠的区间并成一段、换成其中最长那一条的
占位符（一样长时内置在前，用户规则按声明顺序）；已找到的区间与输入里原有的占位符在视图里涂成不透明的空白再找，
至多 4 轮到不动点——第二次 `redact` 看到的就是第一次最后看到的视图，所以幂等、占位符不被再次命中。私钥块在把空白
看成空格、把 JSON 的 `\n` 看成换行的视图上找；紧贴在一个格式命中后面的格式在同一轮里认完。`encode_redacted` 逐叶
脱敏后编码：名字像密钥的键下面（任意层）仍像凭证的字符串叶子换成 `secret.named_assignment`，`Authorization` 键按
头脱敏，`name`/`value` 环境变量表按名字判，两项的字符串列表按一对键值读，撞键按原键排序加 `#n`；输出是一行、总能
编码成 UTF-8（U+2028、U+2029、U+0085、DEL 与 C1 控制字符、落单的代理项都转义）。`secret.named_assignment` 认
`=`、`:`、`:=`、`=>`、`--flag=`、`os.environ[...]`、Kubernetes 环境变量表，值判定排除路径、文件名、版本号、属性
路径、短词串、资源路径、摘要与示例词，熵门槛 3.5（够长的十六进制 3.0）；在 `src/`、测试固定装置、`tests/scenarios`、
`tests/kernel/data` 与一份近似样例文件上零命中；不进 `scan`。用户规则经 `.syndica.yaml` 的 `redaction.rules` 进来，
只脱敏、不拦提交。公开面：`Finding`、`RedactionRule`、`RuleRedactor` 进承诺面；`Redactor` 协议、`encode_redacted`、
`RedactedText`、`RULE_IDS`、`RULES_VERSION`、`looks_secret` 扣留到第一个收它们的公开入口（trace 的写入面）。
`RULES_VERSION=1` 由一份自成一体的黄金语料钉住（含编码器的输出；样例取自 sha256 而不是 `random`，后者的序列不保证
跨 Python 版本不变），`RuleRedactor.digest` 的算法另有一条逐字用例。

对照 M2（212 条样例）：11 条不同。3 条是 M2 的泄露，现在关掉了（JSON 字符串里截断的私钥；第一行恰好是密钥值的截断
PEM 与 PuTTY 块）；8 条是 URL 里已知密钥值的占位符名字不同（M2 的 URL 规则吞掉占位符里的冒号；现在那个值留着
`secret.env_value` 的名字），密钥照样全部藏住。M2 不幂等的 6 条，新输出等于 M2 第一遍的输出。

验证：对抗评审两路。泄露猎手看的是第一版（逐条规则、按占位符切段）：2 High（段内匹配让上下文规则只看到半截凭证——
宿主环境里有一个 JWT 时，`Authorization: Bearer` 后面另一个同头部的 JWT 漏出 payload 与签名；`encode_redacted` 漏掉
请求头映射里的 Bearer）、5 Medium（粘连的格式令牌与带锚点的用户规则不幂等；零误报门槛几乎没考到值判定；
`named_assignment` 漏报；私钥匹配平方时间，M2 就有）与一批 Low。根子在切段的设计本身，于是整体重写成上面的结构。
契约与设计评审看的也是重写之前的版本：2 High（同一条泄露；`secrets=` 收下字符串、映射与 None 不报错）、7 Medium
与一批 Low；重写之后仍成立的逐条修掉：`secrets=` 的整体校验；`Redactor` 扣留；黄金语料自成一份、按 sha256 取样、
含编码器输出；密钥键下嵌套的映射与列表；编码输出单行且总能编码成 UTF-8；`redact` / `scan` / `encode_redacted` 的
入口类型；用户规则报错里的占位符按规则顺序取；README 不再说「全部输出都脱敏」，并写明用户规则只脱敏不拦提交、病态
正则会卡住宿主；误报门槛扩到 `tests/scenarios` 与 `tests/kernel/data`；CLI 的相对路径用例。变异：重写版 79 条
（74 条直接杀、3 条补用例后杀、2 条指向多余代码、已删）；评审修复之后为新代码写的 21 条全杀。粘连模糊测试：幂等语料
两两粘连（无分隔、空格、换行）共 286,443 条，不幂等 0；有分隔时格式令牌泄露 0，泄露都在 key 紧贴字母数字的位置（见
风险一节）。零存活只对这些变异集成立。

意外发现：D4 原文「按占位符切段、各段内匹配」本身就会泄露——占位符对后面的规则不透明，而上下文规则要看到完整的
凭证；两者只能靠「先找全、合并、到不动点为止」同时满足（决策日志 2026-10-02）。

#### T4 — 守卫扩到全仓；`RedactedText` 类型闸；宿主私有目录

**Files:** `tests/test_write_points.py`（由 `tests/cli/test_write_points.py` 迁出并扩展）、`kernel/redaction.py`、
`kernel/local_workspace.py`、`kernel/tools/local.py`、`cli/host.py`、`tests/cli/test_handoff_run.py`（它用 Write 在
`.syndica/handoffs` 建挡路的文件，改用 Bash）、测试。

- 见 D5、D17。每个扫描根各有锚文件与 `os.walk` 交叉核对；在每个根里种一个违规样例证明被抓到：白名单之外写文件、
  `client.post(json=...)`、另起一个 `def redact(self, x): return x`、`cast(RedactedText, ...)`、白名单目录里的新文件。
- 私有目录：Read / Write / Edit 拒 `.syndica/traces/…` 与 `.syndica/handoffs/…`（含大小写变体与经符号链接的路径）；
  Glob / Grep 显式指向它们时同样拒；`.syndica/tmp/…` 照常可读写（Worker 的提示词用它）；Bash 不受影响；错误说明写明
  为什么。

**状态：完成（6937784）。** 守卫（`tests/test_write_points.py`）扫整个 `src/syndica`：五个扫描根各有锚文件，另用一次
`os.walk` 核对范围；在每个根里种的违规样例（写文件、`client.post(json=...)`、`def redact`、`cast(RedactedText,
...)`、白名单目录里的新文件、白名单文件里多出来的一处）都被报出。白名单按文件、按写法与次数逐条写死：模型提供方的
请求、Agent 的文件工具与命令管道、`write_all` 里把短写写完的那一处 `os.write`。落盘原语收进 `kernel/redaction.py`：
`write_all`（写满为止；交接文件的三处写入改用它，此前单次 `os.write` 在磁盘将满时可能静默截断）与
`write_redacted`（运行时检查 `RedactedText`，T11 的 trace 文件只经它写）。`RedactedText` 的构造器要模块私有的
令牌，`copy` 交回它自己、`pickle` 读回普通 `str`。私有目录（D17）在 `LocalWorkspace` 里，CLI 传 traces 与
handoffs 两项。

验证：对抗评审一路（D17 用文件工具绕不过去：尾随点与空格、`//`、反斜杠、`~`、根的别名、长 s `ſ`、`[t]races`、
硬链接与竞争都要 Bash）。评审报的守卫缺口逐条修掉：经别名或间接调用造出 `RedactedText`（改成标注与 `isinstance`
之外一律不许出现，加构造令牌）；标注为 `RedactedText` 的参数挡不住 `Any`（去掉这条信任，改用运行时检查的
`write_redacted`）；按位置给的正文、经 `partial` / `to_thread` 包装的外发、套接字发送、网络日志 handler 与
`logging.config`；子进程 stdin 的 `.write`；`-am` 这类以 `m` 结尾的短选项串；模块常量被再次绑定；`os.write`
短写让正确的 trace 写入器无法合规（`write_all` / `write_redacted`）。私有目录的修正：照写与折叠之后的名字都看、
根的别名、Glob 少一次 `resolve()`（15000 个文件 1.0 秒，与只解析一次的基线 0.85 秒持平）、Write 不许在私有目录的
上级位置建文件。变异：第一轮 34 条全杀；评审之后 43 条全杀（评审列出的 10 条存活变异各补了一条单独施加即变红的
用例，其中参数信任的那几条随这条信任一起删掉）。零存活只对这些变异集成立。

意外发现：`os.write` 在文件大小上限与磁盘将满时返回比请求少的字节数而不报错（评审在 `RLIMIT_FSIZE` 下实测），
M2 的交接文件写入一直是单次调用。

### 阶段二：trace 模型与捕获

#### T5 — trace 模型、`TraceStore` 协议、内存实现与契约套件

**Files:** `kernel/trace.py`、`kernel/protocols/trace.py`、`kernel/stores/memory_trace.py`、`kernel/contracts/trace.py`、
`tests/contracts/test_memory_trace.py`、`tests/kernel/test_public_api.py`。

- 见 D3、D6。契约逐条：保序（三条以上、跨两次 `append`）、快照（改一次读回的 span 的嵌套属性，再读同一个 trace 不变）、
  第二次 `begin`、未 `begin` 与 `finish` 之后的写、收到 `str` 而不是 `RedactedText`、payload 去重且引用由 store 算、
  `find` 的唯一与歧义、`traces()` 的顺序（三个 trace，开始时间非单调地写入）、`delete`、并发追加（三个任务，每次追加
  之间 `await asyncio.sleep(0)`）。
- 故意违约的子类（丢掉最后一条、不深拷贝、`finish` 后仍收、接受 `str`）让契约变红；枚举成员→取值表整体断言。

**状态：完成（93c5cb1）。** 模型（`kernel/trace.py`）：span、事件、边、头、规则集记录、来源、payload 引用，全部冻结、构造时
校验；属性是闭集（键 → 种类），整数限在 int64 之内、布尔不算整数，取值有限的属性（`tool.outcome`、`stop_reason`、
`confirmation.outcome`、`wait.kind` / `wait.outcome`、`danger.risk` / `decided_by` / `disposition`）只收表里的值，
与内核枚举一致由用例钉住。`TraceStore` 协议、`InMemoryTraceStore`、`TraceStoreContract`（25 条）。公开面：全部
扣留到 `TraceRecorder`（第一个收 TraceStore 的公开入口），契约随协议一起进承诺面。

验证：对抗评审一路，结论是契约太弱：18 个故意违约的 store 过了全部 17 条契约，它施加的 21 条变异全部存活。2 High
（头读回不全没人查——级别、规则集、来源可以写死；固定装置的父 span、结局、事件、列表、布尔与 None 都只有一种取值，
写死、排序、丢掉的实现照样过）、4 Medium（被拒的写留下一半；`delete` 之后的写照收；逐叶编码器把 id 像密钥名的
用户规则的 pattern 换掉；只有 `append` 有并发用例）与一批 Low（`find` 的索引不随 `finish` 更新、只比 8 个字符；
`finish(None, None)` 静默通过；整数不设上限；取值有限的属性收拼错的值；`None` 当序列给时报裸 `TypeError`）。全部
修掉：契约补到 25 条（每个字段都不止一种取值的固定装置与头、混着坏边的整批、`delete` 之后的写、payload 引用属于
它的 trace、读回是普通 `str`、交回的列表是快照、`find` 的边界、`finish` 的参数、并发的 `begin` 与 `put_replay`），
22 个故意违约的 store 各自让对应的那条契约变红；规则集改成 `RedactionRule` 记录；字段名与字符串属性键不许像密钥名
（用例当场查出 `llm.input_tokens` 这三个计数像密钥名——整数不受影响，用例改成只管字符串）。变异：第一轮 34 条，
33 条杀、1 条等价（内容相同的 payload 重写一遍）；评审之后为它的 21 条存活变异与新代码写的 27 条全杀。零存活只对
这些变异集成立。

#### T6 — 可复现的 id 与「当前工具调用」

**Files:** `kernel/runtime.py`、`kernel/agent.py`、`kernel/tool_executor.py`、`kernel/tools/human.py`、测试。

- 见 D10。默认 id 源与现状一致（uuid4，形式不变）；注入的 id 源对 agent 与 message 都生效且在任何东西绑定之前——
  一个在工厂里把 `agent.agent_id` 绑进 Delegate 工具的用例，注入 id 源之后派活照样被注册表接受；「当前工具调用」在
  `PRE_TOOL_USE` 里就可见、执行完复位、并发段里各调用互不串线、不经 `_drive` 漏进被唤醒的 Agent；人机请求的事件
  带上键。
- 稳定键：三个子 Agent 以与记录相反、且非单调的顺序被创建时，各拿到自己记录的 id。

**状态：完成（4b8e4dc）。** 「当前工具调用」与稳定键在 `kernel/ids.py`：Agent 把（agent id、周期、turn）交给执行器，
`_execute_one` 按批内位置（跨段计数）在 `PRE_TOOL_USE` 之前设、执行完复位；工具调用之外退到当前属主（Agent 自己的
事件循环、kill 的收尾、关机），都没有时是宿主。`Runtime(ids=...)` 收 `IdSource`（默认 uuid4，形式不变）；spawn 在
调工厂之前按键取 id，交给工厂里构造的第一个 `BaseAgent`；消息 id 同样按键取；AskUser 与邮箱确认的事件带上请求的
键，答复按（等待方、关联 id）登记的键取 id。公开面加 `IdSource`、`StableKey`（`Runtime(ids=)` 的标注要它们），其余
扣留。

验证：对抗评审一路，1 High / 3 Medium / 2 Low，另有 9 条存活变异。High：Agent 自己的 hook 派的活、发的消息落在宿主
那一档全局计数上，两个并发的 Agent 换着拿 id。Medium：拓扑槽的键优先于工具调用的键，并发的 `occupy` 换着拿；kill
的收尾继承了发起它的那次调用，与那次调用抢序号；请求只按关联 id 登记，一个 Agent 的答复能取走另一个的键。Low：把类
当 id 源要等第一次 spawn 才以裸 `TypeError` 炸开、回放源缺键抛裸 `KeyError`；工厂先构造别的 Agent 时静默拿到随机
id。全部修掉：属主作用域（`('agent', id)` 的计数放在 slot 上，唤醒后接着数）、槽键带上出处、请求按（等待方、关联
id）登记、键在注册表检查之前占用、拒收类、id 源的异常包成带键的 `ContractViolation`、注入的 id 源下工厂没用上交给它
的 id 时报 WARNING（默认源下不报，记录时的检查留给 T14）。修的时候又查出一条：请求在 `_wait_for` 之前登记时，一次
被拒或当场返回的等待会盖掉、再摘掉正在等的那一个的登记；改成在成为等待者之后才登记。周期分量在 turn 跨周期累计时
对唯一性是冗余的（`_cycle = 1` 因此存活），留着是因为 D3 的 agent span 键要它，用一条用例钉住取值。变异：33 条全杀
（评审的 9 条存活与为修复写的 24 条）；用例在三路并行负载下重复 12 次、倒序跑一次全绿，排除假杀。零存活只对这组
变异成立。全套 6047 条。

复评（对 4b8e4dc）：1 High / 2 Medium / 一批 Low，另有 8 条存活变异，全部修掉，单独提交（9406c34）。High 是上一轮修复引入的：
「槽键带上出处」对 singleton 与 keyed 槽是错的——它们的实例由槽与键确定，谁先寻址都是同一个，键里带上先到者，回放
时换一个先到者它就换了 id，先到者那次调用里此后的 spawn 也跟着错一位；改成只有 on-demand 跟着调用方取键，另两种按
（槽名、键，第几个）且不占调用方的计数。Medium：请求在转 WAITING 的 CAS 之后才登记，会挂起的注册表下，当场作答
的答复被记成宿主的消息——改成在 `wait_for` 入口登记，同一个（等待方、关联 id）下按先后入栈（答复取最新的，被拒的
等待只摘自己那一个，于是不必等成为等待者再登记）；事件里的 `key` 字段名本身像密钥名，落盘前的逐叶脱敏把其中的
agent id 换掉了——改名 `request_ref`，用例把事件经 `encode_redacted` 走一遍。Low：交给工厂的 id 是 contextvar 里的
值，工厂在构造 Agent 之前起的任务拿到的是拷贝、能再取一次（两个 Agent 同名）——改成共享的一格，取走与收回都清空它；
spawn 的键在关机 / 拆除两道拒绝之后才占用；`AGENT_DESPAWN` 的 hook 按取得收据的那条拆除取键——改在
`('despawn', id)` 里跑（作用域可复位，不改调用方的上下文）；`isinstance(RandomIds)` 让继承它写的记录源听不到警告；
不可哈希的关联 id 变成裸 `TypeError`；id 源的方法不可调用、写成 async、缺了哪个，错误都说不清。`_Slot.reap_issued`
是死状态（收尾只在拆除时取键，一个 Agent 至多拆一次），删掉。工具调用里并发的子任务按到达先后取号、键只在一个
Runtime 之内唯一，写进 D10 与 `IdSource` 的文档。宿主自己写的 `ConfirmationHandler` 拿不到请求键（`peek_key` 扣留），
留给 T13 决定。变异：为复评写的 25 条与上一轮里仍然适用的 20 条全杀，上一轮 13 条因代码重写失效，重写成 10 条
等价的也全杀；用例 59 条。

#### T7 — 内核插桩：同步的 `Tracer` 与因果边

**Files:** `kernel/trace.py`、`kernel/agent.py`、`kernel/tool_executor.py`、`kernel/runtime.py`、`kernel/retry.py`、测试。

- 见 D2、D3。插桩前先列出 D2 列的每个「中间没有挂起点」的窗口，各写一条用例证明插桩没有在其中引入挂起点
  （tracer 在回调里断言自己处在同步调用中、计数事件循环的调度槽位不变）。
- 每个插桩点一条用例；`tool.outcome` 的每个取值各一条（含钩子看不见的那些：未知工具、坏参数、跳过、阻断、确认的
  四种结局、没有确认通道、判定器出错、未执行、第 9 步中止、恢复时修补的批次）；被取消的 Agent 的 agent span 以
  `cancelled` 收尾；`RetryModule` 的每次重试是一个独立的 llm_call span（`llm.attempt`），「当前 LLM 调用」在首次与
  每次重试里各自正确（D11 的回放键靠它）。
- `trace show` 要的属性：派生边的 `child.agent_id`（含一个惰性启动、从没跑过的子 Agent）、发送事件的 `message.to`、
  等待的 `wait.kind` / `wait.target` / `wait.outcome`（Agent 等与人等各一条，四种结局各一条）。
- 因果：Delegate 派出的三个子 Agent 各有一条从对应 Delegate 调用 span 出发的派生边；被 `SendMessage` 唤醒的 zombie
  只有消息边、没有派生边；三条消息各自连到接收方消费它的那一轮，互不串线。
- span id：断言完整的「结构键 → id」映射（三轮以上、三个调用以上、含一个重复的 `tool_call.id` 与一个空串 id），且两两
  不同；结构键用长度前缀编码。
- 失败隔离：每次都抛 `Exception` 的 tracer 不影响 run 的结局，日志恰好一条 ERROR（断言 `record.args` 里的 tracer、
  步骤与级别），关闭时报出失败次数；抛 `CancelledError` 的照常上抛。

**状态：完成（b4add64）。** `kernel/tracing.py`：同步的 `Tracer` 协议（`start_span` / `end_span` / `add_event` / `add_edge`，时间
由 tracer 自己取）、`NullTracer`、长度前缀且带类型标记的 `span_id_of`、当前 span 与「当前 LLM 调用」两个 contextvar、
`_Tracing`（每个 Runtime 一份，交给它的 Agent 与执行器）。插桩点：agent（每个周期，脱离父）、turn、llm_call（首次、
重试与备用模型各一次，`llm.model` 取 `config.model`）、tool_call（每处追加结果块，含未执行与恢复时补的）、
confirmation、wait（`wait_for` 与 Delegate / Fork 的在轮等待）；派生边在任务建好之后发出，终点是子 Agent 在这里的
下一个周期；消息的来源在投递之前记，投递成功才在发送方 span 上记 `message.sent`，消费时（注入那一轮或等到它的
等待）连边。内核自己起的任务（驱动、收尾、关机）与拆除的 hook 都在任何 span 之外跑。tracer 的公开面（`Runtime(
tracer=)`）留给 T9：它要为回放级别加回复、工具结果与请求指纹的通道，此刻公开就是给第三方一个之后改不动的协议；
测试经私有的 `Runtime._set_tracer`。

验证：两路对抗评审。正确性一路 5 Medium / 9 Low：死在注入里的一轮下一个周期会重开同号的 turn span（计数挪到注入
之前）；`llm.model` 读的是协议里没有的 `llm.model`；未知工具名原样记下（改 `<unknown>`）；子 Agent 继承父的「当前
LLM 调用」（清掉，并且只在 `create` 在飞时有值——hook 与工具自己的模型调用不在计数里，给它一个别人的键回放只会交回
别人的回复）；模型在更早的轮里用过同一个调用 id 时，补「未执行」的那块按 id 认成答过而漏掉（改按批内位置认；这条
早于 T7）；以及派生边的终点写死第一个周期、`tool.exit_code` / `tool.timed_out` 从不产出、嵌在工具里的 Runtime 把
事件记进外层 trace、换 tracer 拆开开始与结束、宿主 `wait_for(None)` 报错走样、`_sent_from` 在邮箱随收尾摘掉时只增
不减。健壮性一路 1 High / 2 Medium / 5 Low：tracer 抛 `CancelledError` / `GeneratorExit` 会让 Agent 停在 running、
半建子 Agent、销毁人的答复、吞掉 `AGENT_DESPAWN`（改为只放过 `KeyboardInterrupt` 与 `SystemExit`）；嵌套 Runtime
（打开的 span 记下开它的 `_Tracing`，别人的当不在 span 里）；换 tracer（还有 span 开着时拒绝，旧的失败先报掉再清零）；
生成器写的 tracer 方法静默丢记录；在轮等待的键在取决于调度的检查之后才取；GC 关掉协程时 reset 抛错吞掉 span 的
收尾。外加 15 条存活变异（各状态的收尾、来源在投递前、备用模型的计数与名字、关机时的总数、轮边界被中止的那一轮、
拆除事件只报一次、Future、单个收件人、等待的主体与结局、spawn 与在轮等待两个不许挂起的窗口）。全部修掉；两个窗口
各有一条探针用例（`put` 时排一个回调，断言它跑的时候任务已经建好；在轮等待的 span 开始时排一个回调，断言令牌已经
装上）。变异：最初 74 条里仍然适用的 56 条、失效 18 条的等价重写 15 条、评审 40 条，全杀；用例 104 条。

#### T8 — LLM 侧：token、作答平台、平台故障、前缀单位

**Files:** `backends/llm_openai/client.py`、`backends/llm_router/router.py`、测试。

- 见 D13。`usage` 有、没有、缺一部分各一条；`extra` 不进请求（逐字比对发出的 JSON）；换平台时 `llm_failures` 按顺序
  列出被换掉的三个平台；`cache_prefix_units` 不换平台时与作答成员逐字节相同。

**状态：完成（5a56765）。** OpenAI 兼容客户端给每个回复带上 `extra['usage']`（三个计数；端点没报、类型不对或超出 int64 的记
None，猜一个数比没有更糟）与 `extra['finish_reason']`，它们不随历史发回（第二轮请求体逐字节比对；`_message` 只回传
`_EXTRA_KEY` 下的内容）。`ModelRouter` 交回作答成员回复的一份副本（不就地改成员的对象：会缓存回复的成员下次交回
同一个对象），带上 `llm_route` 与同一次调用里按先后被换掉的平台 `llm_failures`（平台名、错误类型、HTTP 状态——状态
按属性认，只收整数：backend 之间不互相依赖）；实现 `CachePrefixProvider`，`config` 与前缀单位按**调用方**最近一次
作答的成员取（contextvar：路由器被几个 Agent 共用，全局的「最近一次」会把别的 Agent 的换平台算到这个 Agent 头上），
那个成员给不出单位时交回 None——`CachePrefixProvider` 因此放宽成可以交回 None，漂移检测退回逻辑单位并标成不精确。
内核的 llm_call span 记下作答平台、token 计数（越界与负数不记）与这次调用之后的模型名（轮换在调用前报的是上一次
作答的平台），每个被换掉的平台各一个 `llm.platform_failed` 事件（`llm.platform`、`llm.error`、`llm.status`，路线图
「LLM span 记下每次平台故障」）；首次、重试与备用模型各记自己那一次的回复；读回复出错只记一条 ERROR，不把一次付过
钱的回复变成模型失败。

验证：对抗评审一路，1 High（`retry.py` 的 `with llm_attempt(...) as attempt` 遮住了重试序号，评审期间已在全套里抓到
并改掉——只跑变异用的三个文件看不见它，变异集从此带上 `test_retry_module.py`）、3 Medium（超出 int64 的计数会让记录器
丢掉整个 span 并把错算到 tracer 头上；换平台时一个 span 里是两个平台；共用路由器的漂移记录记到没换平台的 Agent 头上）、
4 Low（`exact` 被冒充、缓存回复带着旧的轮换记录、`observe` 不隔离失败、注释与计划说多了），另有 12 条存活变异。全部
修掉。变异：44 条全杀（评审的 12 条存活与修复的反向变异都在里面）。

#### T9 — `TraceRecorder`：缓冲、写入任务、逐叶脱敏、级别、指纹

**Files:** `kernel/trace.py`（或 `kernel/trace_recorder.py`）、测试。

- `TraceRecorder(store, redactor, *, level, clock)` 实现 `Tracer`：同步缓冲、一个写入任务按序落盘、`finish` 排空
  （还开着的 span 以 `cancelled` 收尾再写，见 D3）；
  按 D8 的级别决定记什么；所有字符串经 `encode_redacted`；请求按 D11 取逐单元指纹（按单元原文的 sha256 缓存）；人的
  答复由内核在等待等到它时交给 tracer（见下面的状态）。宿主档案的 schema 到 T12 才有，T9 与 T11 不写它，从 T12 起写、
  T14 补进往返。
- 脱敏用例：每个通道一个**不同的**假密钥（提示词、命令行参数、工具参数、工具输出、`meta`、模型回复、`extra`、
  提供方错误文本、未知工具名、模型给的调用 id、环境变量样例），每个规则族至少一个；断言 store 里解码后的每个字符串
  与每个 16 字符片段都不含任何原文，**且**每个通道的位置上确实是预期的占位符（防止「什么都没记」也通过）。
- `structure` 没有 payload；`full` 有完整请求且已脱敏；store 抛错时 run 照常、恰好一条 ERROR。
- T7 留下的：`Tracer` 协议要为回放级别加通道（模型的请求与回复、工具结果、人的答复），`llm_attempt` 的两处调用点
  （`agent.py`、`retry.py`）随之改；结构性的 id（span id、agent id、消息 id）不是自由文本，不经逐叶脱敏——32 位十六
  进制挂在像密钥名的键下会被换掉（T6 复评那条 `key` 字段就是这么丢的）。`Runtime(tracer=)` 在这里公开，`Tracer`
  与它用到的模型类型一起进承诺面。

**状态：完成（edf5d32）。** `kernel/trace_recorder.py`：`TraceRecorder` 实现 `Tracer`——同步方法只往内存记、往队列放，一个
写入任务按收到的先后成批落盘（一批里 payload 先于引用它的 span），`finish` 把还开着的 span 以 `cancelled` 收尾、
等写入任务排空（被取消也排空）、写下结局。时间只在 `begin` 读一次墙钟，此后按单调时钟往后推、不回退，span 不会
落在 trace 之外。级别按 D8；指纹按 D11（模型参数是内核交给 `create` 的关键字参数，后端的配置不在其中，模型名记在
`llm.model`）；逐单元指纹的列表经新的 `encode_digests` 编码——只收 64 位十六进制、不经脱敏。新增属性种类 `payload`
（模型校验它是摘要）；`danger.provenance` 进闭集。

与计划不同之处：(1) 人的答复由内核交：`Tracer` 加 `wait_answer`，`wait_for` 等到消息时在 span 收尾之前交出，
recorder 只记人等的那一种——由宿主报，答复要赶在等待消费它之前报，第三方宿主漏报就静默丢了；(2) 命令行参数由
recorder 脱敏（`redact_argv`，`--api-key <值>` 分成两项时也认），`TraceSource.argv` 原样交进来；(3) 内核生成的
标识、摘要与闭集取值不经脱敏；(4) 被 `PRE_TOOL_USE` 跳过的调用经 `tool_result` 交出跳过它的结果（D11 要「原样再
跳过」）；(5) 回复多记 `trust`（不进指纹）。T11 的 `FileTraceStore` 只用内置规则再编码：宿主的用户规则在 recorder
那里已经用过，在 store 再用一遍只会换掉 id 与摘要。

记录不改变 run：store 失败时第一次一条 ERROR，此后只计数（`dropped`，按 payload、span、边逐条、不多不少；
`finish` 之后才到的 span 与边也算）；store 交回的引用与内容对不上（摘要或 trace id）算 store 故障，下一步写成
「修 store」；`begin` 失败原样上抛给宿主，此后只计数；一条记录编码不出来只丢那一条（第一次一条 ERROR），这两种
情况都不写结局。`Runtime(tracer=)`、`Tracer`、`NullTracer`、`TraceRecorder`、`TraceStore`、`InMemoryTraceStore`、
`TraceStoreContract` 与 trace 模型类型进承诺面；`TraceRecorder` 的实例公开面逐字钉住。`Tracer` 协议以后再加通道，
从头实现它的 tracer 会被 `Runtime` 拒掉：第三方继承 `NullTracer`（`Runtime` 的报错里写着）。

验证：自己的变异 79 条，存活 1 条（`_fail` 里「只记一次」的守卫两个调用方都已挡住，是死代码，删掉，另两条变异证明
调用方钉住了这件事）。对抗评审一路：没有脱敏泄漏；1 High（请求里一个落单的代理字符让此后每一次模型交换都记不下、
`dropped` 仍是 0——缓存键按 UTF-8 编码；同族的还有 `str(error)` 自己抛出、超过 4300 位的整数）、3 Medium（用户
规则认得十六进制时逐单元指纹被换成占位符；`_jsonable` 对共享与成环的结构按路径展开，三个节点的双向链表能把事件
循环按住几个小时；默认级别缺跳过调用的结果与回复的 `trust`）、若干 Low（时钟的类型、`finish` 的收尾、两个时间
基准、`wait_for` 给一个不认识的 agent id 开 span、`span id` 重开、`begin` 在飞时 `finish`、引用的 trace id 不核对、
`_fail` 的下一步、`tracing.py` 两处到不了的守卫、实例公开面没钉、默认 repr 带内存地址），外加 23 条存活变异（每个
通道用的是不是宿主的整套脱敏器、元组列表、覆盖次序、答复与结果的取值、`retry.py` 交不交请求）。全部修掉；没采纳
的两条写进契约：`wait.target` 仍按自由文本脱敏（等 `Reply` 时它是调用方的文字，拆属性收益小于改模型的代价），
用户规则的 pattern 照记在头里（回放靠它重建规则集，规则写在仓库的配置里）。变异：修复后 49 条、旧集里仍适用的 56 条
与改写的 23 条，全杀。

#### T10 — B-19：并发窗口里不许写轨迹

**Files:** `kernel/context.py`、`kernel/tool_executor.py`、`kernel/hooks/center.py`、`kernel/hooks/junctions.py`
（契约 docstring）、`tests/kernel/test_orchestration.py`、测试。

- 见 D9。三个声明可并发的工具、它们的 `POST_TOOL_USE` hook 写块 → `ContractViolation`；只有一个观察者的 `TURN_END`
  写块 → 同上；嵌套分发里冻结计数正确复位；串行的单调用段写块照旧可以；`AGENT_SPAWN` 的观察者分发期间子 Agent 的
  第一次追加照常（Runtime 级切点不冻结任何 `Context`）。
- 变异：计数不复位（之后所有写入都被拒）；只冻结工具段不冻结观察者分发；只在两个以上观察者时冻结。

**状态：完成（ef9ddf5）。** 按 D9 修订实现。`Context.append` 在这份轨迹的窗口里报三件套 `ContractViolation`、点名最近的
那个窗口；窗口由 `context._frozen(ctx, window)` 开、记在 contextvar 里（可以嵌套、可以同名，异常与取消时照样关），
`Runtime._drive` 开头调 `_no_windows()`。两类窗口：`HooksCenter.trigger` 的每一次带 `ctx` 的分发（可拦截与可观察
都算），与 `ToolExecutor._run_segment` 的每一段（单个调用也算——`PRE_TOOL_USE`、`POST_TOOL_USE`、
`TOOL_CONFIRMATION_REQUIRED` 与工具本身都在段里；结果块在窗口外按调用顺序落）。契约写进 `junctions` 模块文档；
`test_orchestration.py` 那条从观察者追加的用例改写成断言追加被拒、结论照旧；`_final_text` 从后往前找的理由改成
「恢复时补的结果块、宿主在两次运行之间追加的」。B-19 移到遗留项登记的已关闭一节。

验证：最初的实现（只冻结多调用的段与可观察分发、窗口记在对象上）8 条变异全杀。对抗评审一路：1 High（契约推荐的
写法本身是错的——单调用段里的 `PRE_TOOL_USE` / `POST_TOOL_USE` 破坏调用与结果的配对，`PRE_LLM_REQUEST` /
`POST_LLM_RESPONSE` 让轨迹说谎，hook 也无从知道自己是不是一段里唯一的调用）、3 Medium（别的任务开的窗口冻住
Agent 自己的追加，模型的回复与结果块都会被拒；窗口里取的拷贝永远冻着；审计写的「取消时照样关上」没有用例）、
4 Low（就地改块、观察者起的任务一轮之后又能写、点错窗口、文字），外加 14 条存活变异。改成 D9 修订的设计；评审的
复现各成一条用例（另一个任务里的分发、轮末快照、在自己的观察者里被叫醒），14 条存活变异的同型改写全杀；就地改块
写进契约，不在冻结之内。

阶段二收口：阶段审计，见 [M3 阶段二审计](../audits/2026-10-02-m3-phase2-audit.md)。

### 阶段三：文件存储、CLI 记录与回放

#### T11 — `FileTraceStore` 与 CLI 记录

**Files:** `kernel/stores/file_trace.py`、`cli/host.py`、`cli/main.py`、`cli/config.py`、`src/syndica/__main__.py`、测试。

- 见 D6、D7、D8、D15。`FileTraceStore(root, redactor)` 过契约套件；`O_NOFOLLOW` / `dir_fd`（在 run 中途把 `payloads`
  换成符号链接，写入被拒、不写到链接指向的地方）、`O_EXCL` 与 sha256 核对各一条；写失败（目录只读、`.syndica` 被 Bash
  建成了文件）时 run 照常、stderr 一条说明；攥着目录 fd 时，`delete` 之后的写要报错而不是写进一个已经删掉的目录。
- CLI：`run` / `chat` 默认记录（`chat` 最高 `structure`，D15）、四个级别、`trace.level` 快照、命令行参数原样交给
  `TraceSource`（recorder 写头之前脱敏）、交接文件写 trace 目录、`.syndica/.gitignore` 照样建、Ctrl-C 时以 `cancelled`
  收尾、`begin` 失败（store 写不进去）时 stderr 一条说明且照常跑。人的答复不用宿主报：内核在等待等到它时交给 recorder。宿主档案（规则带参数、处置表、
  Flag、`--max-turns`、`--no-write`、模块、拓扑与入口）从 T12 起报。

**状态：完成（dea6cfd）。** `kernel/stores/file_trace.py`：`FileTraceStore(root)` 过契约套件。每一级按名字以 `O_NOFOLLOW`（目录另加
`O_DIRECTORY`）经 `dir_fd` 重开；文件另加 `O_NONBLOCK`、打开之后核对是普通文件，要写的只许有一个硬链接，读之前按大小设上限
（头 4 MiB、别的 256 MiB，撞上已有的 payload 时按它应有的大小）；`root` 记下第一次打开时的身份，换成别的目录就拒绝；`begin`
记下自己建的目录名，此后按名字直接打开、核对头里的 id，没记下的（别的 store 实例读）按目录名里的 id 前缀找，两个目录都自称是
同一个 trace 时报错；payload 以 `O_EXCL` 创建并核对 sha256，写到一半失败删掉半截文件；头与记录读不成（包括模型类型自己的
校验、嵌套过深）都报「这个 trace 已经不可信：删掉它所在的目录」。CLI：`run` / `chat` 默认记录到 `<工作区>/.syndica/traces`
（`--trace`，再 `trace.level`，再 `replay`；chat 最高 `structure`，人点名要了更高的级别才在 stderr 说一句）；结束时 stderr 打出
trace 目录（经宿主的脱敏器与转义，不完整时标出来），`--handoff` 的正文多一节 `## trace`（front matter 不变，不完整时多一句）；
Ctrl-C 以 `cancelled` 收尾（`run --handoff` 期间第二次按键不再打断收尾），失败的 run、以失败的一轮结束的 chat 以 `error` 收尾；
trace 开不了头（`.syndica` 是普通文件、`traces` 是符号链接、记录器自己出错）时 stderr 一条说明、照常跑。命令行参数逐项脱敏
之后整行再过一遍：`API_TOKEN: <值>` 跨两项，连起来认出了就交回脱敏之后的一整行。recorder 的 store 失败报告用错误自己的下一步
（三件套时），不再一律说「store 有缺陷」。

与计划不同之处：(1) `FileTraceStore` 不收 `Redactor`：内容在 recorder 里已经按宿主的整套规则逐叶脱敏，store 再用一遍宿主的
用户规则只会换掉内核生成的 id 与摘要（读回时校验不过）；它写盘时用只含内置规则的脱敏器再编码，对已脱敏的文本是幂等的。
(2) 不攥长期的目录 fd：每次调用按名字重开、核对根的身份，「`delete` 之后的写报错」由此成立。(3) chat 的默认级别不提示。

验证：自己的变异 53 条，存活 5 条（同一秒里同前缀的两个 trace、按完整 id 认目录、stderr 上两处的脱敏、交接文件的不完整标记），
各补一条用例之后全杀。对抗评审一路：3 Medium（Agent 种下的 FIFO 或稀疏大文件让宿主挂住或撑爆内存；`NAME: <值>` 分成两个
命令行参数时原样进了 `trace.json`；被改过的 trace 的报错让人去跑还不存在的 `trace prune`，外层又说 store 有缺陷）、7 Low（硬链接
与上级目录的符号链接把写入带出 trace 目录；一个读不成的头让列表与查找都失败；`run --handoff` 的第二次 Ctrl-C 打断收尾、trace
没有结局；每一次写都列一遍历史上所有的 trace，复制出的同名前缀目录能接走写入；chat 以失败的一轮结束时 trace 记成 ok；两个 run
同时建 `.syndica` 时一个报错；未知级别是裸 `KeyError`），外加 14 条存活变异（其中 4 条已被评审导出之后补的用例杀掉）。全部修掉并
各补用例；修复与评审存活变异的同型改写 35 条，全杀（两条起初靠超时「杀掉」的另补了断言）。

#### T12 — 场景格式 v2：内核的 schema 与 runner 的两种读法

**Files:** 内核的 schema 模块、`runner/scenario.py`、`runner/yaml.py`、测试。

- 见 D12。v1 的全部现有场景与用例原样通过；v2 的每个新键各有合法与不合法的用例（三件套）；`{payload: sha}` 的引用；
  `impl` 只认内核内置类与白名单。
- 有序字段（`danger.rules`、`frozen.tools`）对照手写的固定装置逐项比顺序；同一份 v2 文档写成 YAML 与 JSON，读出的
  结果相同。

**状态：完成（788d958）。** `kernel/scenario_schema.py`：`parse_scenario_document(raw, *, source)` 把一份已经读出来的映射校验成不可变的
`ScenarioDocument`（v1 与 v2，纯映射、不依赖 PyYAML）；`ScenarioDocument`、`PayloadText` 与入口进承诺面，各部分的类型等捕获
写出的文档定稿时再判。v2 在 v1 之上多出公开契约里的那些键，另有几条结构约束：同一份文档里实例的稳定键都给或都不给；回复的
`(turn, attempt)` 严格递增、每一轮的 attempt 从 1 起逐个加 1、拿到回复之后不再有下一次调用、`raises` 不与回复的内容并存；
`tool_results` / `skipped` / `gate` 的调用位置必须落在那一轮最后一次调用的回复里真有的工具调用上（于是这样的实例的回复必须
按键给）。JSON 值有深度（64）与整份文档节点数（一百万）的上限，清单整份冻结、与调用方的输入脱钩。runner：`load_document`
读 `.yaml` / `.yml`（safe YAML，重复键报错，不收别名，`0777`、`1:30` 读成字符串而不是八进制与六十进制）或 `.json`（重复键报
出所在行列，NaN、无穷大与溢出的字面量报错，嵌套过深报错而不是 `RecursionError`），交给同一个校验；`load_scenario` 读 v1，由
文档建出原来的 `Scenario`，遇到 v2 指向 `load_document`。

与计划不同之处：(1) `instances[].key` 是内核稳定键的原样数组（`['host', 'spawn', 0]`），不另造带标签的写法：一种写法，捕获时
原样写出。(2) `host_actions` 的 `interrupt` / `shutdown` 不带位置，发生在 run 安静下来的那一处（D11 的原意）。(3) 工具调用的
参数：解析后的任何 JSON 值写 `arguments`，解析不了的原串写 `arguments_raw`；recorder 的 `llm.response` 同样拆开（原来两者都记成
一个字符串，原串 `ls` 与 JSON 字符串 `"ls"` 分不开，回放交回的字节就不同）。(4) v1 的工具参数也按 JSON 值校验：嵌套对象里的
数字键原来会被编码成与同名字符串键重复的两个键、后一个静默顶掉前一个，现在报错。

#### T13 — 回放执行

**Files:** `runner/replay.py`、`runner/scenario.py`、`kernel/danger.py`（`real=`）、测试。

- 见 D11。`ReplayLLM`：首次调用与重试各按「当前 LLM 调用」的键给出（两个 Agent 共用同一个模型对象时互不串线）；被
  打断的调用处阻塞；逐单元指纹第一处不同即失败并报出单元。`host_actions`：EOF 触发的中止在原请求处重放、下一轮照样
  结束成 ABORTED；超时经注入的时钟触发，零真实等待，日志里没有假的 ERROR；`preempted` 只比对。`real=`：工作区根是
  符号链接、回放机器上解析结果不同，判定照样与记录一致。工具桩：
  测试期间禁止任何子进程与写模式的 `open`（打补丁断言零次），编排与人机工具真跑，`QueryAgent` 走桩。跳过 hook 原样
  再跳过。危险判定真跑并与记录逐项比对（改坏判定器、丢掉一个 `UNTRUSTED` 标记各一条用例证明报出分叉）。
- 宿主档案重建：每个字段一条用例证明回放里生效。
- 并发：两个子 Agent 交错的 run，在一组固定种子的调度扰动下（每次插入不同位置的 `sleep(0)`）回放结局与分叉判定一致。
- 未消费的记录算分叉；回放文档读不进来退出 2。
- 人的答复按请求键给出，而宿主自己写的 `ConfirmationHandler`（CLI 的终端确认就是一个）拿不到请求键：`peek_key` 不在
  公开面。先定取法再写回放：`ConfirmationRequest` 带上它（尾部带默认值的字段，加名字不破坏兼容），或者在 handler 的
  调用里交给它（T6 复评）。
- `current_llm_call()` 只在 `create` 在飞时有值：模型调用前后的 hook、工具与模块自己发起的模型调用拿到的是 None。
  `ReplayLLM` 在 None 下要拒绝（三件套，指名这次调用不在内核的计数里）而不是猜一个键（T7 评审）。

**状态：完成（dcc8155）。** `runner/replay.py`：`ReplayRunner(payloads=, redactor=, allow_divergence=, perturb=, real_tools=)
.run(document, workspace_root=)` 交回 `ReplayResult`（分叉列表、结局、没能重建的模块名与它们的 Flag、每个 Agent 回放出的轨迹；
`reproduced` 是「没有分叉」）。模型按「当前 LLM 调用」的键给出记录的回复（`extra`、`trust`、带类型的错误照原样：抛出的是与记录
同名、同模块、同文字的类，记录的是内核错误时保留它的身份），记了逐单元指纹的回复先比请求、第一处不同报出单元。外部工具
（`Read Write Edit Glob Grep Bash BashOutput KillBash`，以及 Runtime 自己装的 `QueryAgent`）换成同名的替身，按「当前工具调用」的
位置交回记录的结局，记录带执行参数的指纹（`arguments_digest`，hook 与确认之后真正交给工具的那一份）时先比参数；内核工具与人机
工具真跑。跳过的调用由一个 `PRE_TOOL_USE` hook 原样再跳过，`no_write` 另装只读守卫。危险规则按宿主档案重建（`real=` 用记录的
解析结果）、判定真跑，每次闸门判定按 span id 逐项比对，记录带 `outcome` 时连这次调用的结局一起比（确认被拒、超时、被抢占分得
开）；捕获的文档一条闸门记录都没有也照样比。人的答复按请求事件里的键给出。

记录里没有的那一次（原始 run 在模型调用、工具调用或人的请求上被 Ctrl-C、关闭、kill 或根结束后的关机打断）先停在那里，run
安静下来时还停着、又没有什么会去取消它，才算分叉。宿主的 interrupt / shutdown 在 run 安静下来、记录都用完之后发出；记录了它
发生时在等哪些调用（`waiting`）的，只在回放恰好停在那些调用上时发出，停在别处报 `stall`。还有记录没用上时先让定时器走完。
根结束之后别的 Agent 接着跑到安静下来，再像原始 run 那样关掉。每一项记录都要被消费，剩下的算分叉；分叉之后停下
（`allow_divergence` 时只记下、尽量往下跑：缺结局的工具调用拿一个说明接着跑），停下之后的结局比对与未消费检查不再报。payload
在回放开始之前全部读出并按 sha256 校验，读不到、不是文本、对不上，以及回放自己在替身里出错，都由 `run` 抛出（文档不可用），
不变成一个分叉或一个看起来正常的 Agent 失败。

与计划不同之处：(1) 回放跑在一个虚拟时钟的事件循环上：`time()` 只在循环空闲时直接跳到最早的定时器（浮点精度用尽的量级上跳过
它一个最小单位，否则定时器永远不就绪），`RetryModule` 的退避与人的超时零真实等待，超时由内核自己的 `wait_for` 触发；没有定时器、
没有停在记录之外的调用也没有能发出的动作时空转 40 次判为卡住，停下之后关机仍不结束则放弃并报出。(2) 真跑工具只给调用方
明确允许的手写文档（`real_tools=True`）：文档决定不了这件事——来路不明的文档按它自己的危险规则真跑，就是在执行它写的命令；
真时钟下判不出安静，所以那种回放不收 `allow_divergence` 与 interrupt / shutdown，没有记录的调用当场算分叉。(3) 宿主的
`ConfirmationHandler` 拿请求键的取法：`ConfirmationRequest` 带上尾部带默认值的 `request_ref`（`peek_key('request')`，与等待、
事件用的同一个）。(4) 逐单元指纹从 `TraceRecorder` 拆成 `RequestFingerprints`，执行参数的指纹是 `arguments_digest`（都不进
`kernel.__all__`），回放用记录的规则集算同一个。(5) 结果带上每个 Agent 回放出的轨迹（被打断的停在打断处）：人的答复交给了哪个
请求、超时有没有先到，只有轨迹能独立证明（拿被测代码自己算的指纹当期望值是恒等式）。(6) 模块按名字重建（安全、人机交互、重试），
其余名字与只有它们声明的 Flag 报进 `unrebuilt`、不交给 Runtime。(7) schema 补了四处（都可选）：`tool_results[].arguments_digest`、
`gate[].outcome`、interrupt / shutdown 的 `waiting`、v2 的 `expect.events` 可按 Agent 选择器分开写（几个 Agent 的事件交错取决于
调度）。v1 的 `context_overflow` 原来指向一个不存在的路径，改成内核真正抛出的 `syndica.kernel.llm.base.ContextOverflowError`。
(8) 变异 `position-ignores-cycle`（工具结局的位置不看周期）是等价变异：一个 Agent 的 turn 跨周期全局递增，（turn，批内位置）
已经唯一。

两路对抗评审（保真度；鲁棒性与内核接缝）报了 5 High（`QueryAgent` 没走桩、只有模型调用会等宿主的动作、分叉发生在进入阶段时
永远挂住、真跑工具的回放没有安静判定而挂住、手写文档能让回放按它自己的规则真跑命令）、8 Medium、4 Low 与 55 条存活变异，全部
按上面的设计修掉；修后一轮 71 条变异（含内核的 `request_ref`、`real=` 与 schema 的新键）没有存活。

#### T14 — 捕获即回放

**Files:** `kernel/trace.py`（recorder 写回放文档）、`tests/integration/test_capture_replay.py`。

- recorder 在 `finish` 时写 `replay.json`，`expect` 填原始结局。
- 往返：脚本化的 CLI run 捕获后回放。`--no-write` 会去掉 Bash，所以分两次 run：一次含派活、消息、确认、AskUser、
  守卫跳过的 `git commit`（发生在一次不可信的 Bash 输出之后）、被阻断的调用、失败的工具、一个被 kill 的子 Agent；
  另一次是 `--no-write`。两份轨迹逐块相同（比 `role`、`content`、`trust`，`tool_calls` 按解析后的参数比，不比
  `extra`），闸门判定逐项相同，`expect` 全部成立；一个以错误结束的 run 回放出同样的错误；一个 Ctrl-C 中断的 run
  回放到同一处。
- 变异：recorder 漏记一种工具结局；工具桩按调用顺序而不是按调用键取；指纹在脱敏前算（固定装置里让一个可脱敏的值
  进入之后的请求，这条才杀得死）；跳过 hook 不装。
- 捕获时，id 不是 id 源按它的键交出的那个的 Agent（工厂交回了别处构造的 Agent，或先构造了别的 Agent）回放不出
  记录的 id：recorder 在捕获时就指出它（三件套，指名工厂），而不是等回放发散。内核的 WARNING 只在注入的 id 源下
  发出，记录时默认的 uuid4 源看不出这件事（T6 评审）。

**状态：完成。** `kernel/replay_capture.py`：recorder 在插桩到达时把回放要的东西记进一个账本——按（轮、第几次）的模型回复与
逐单元指纹或带类型的错误、外部工具（`REPLAYED_TOOLS`，与回放共用一份名单）按调用位置的结局与执行参数的指纹、跳过、每次
闸门判定连同这次调用的结局、按请求键的人的答复（确认、AskUser、AskUserQuestion、超时）、宿主在请求处的中止、各 Agent 最后
一个工作周期的停下原因；`finish` 时与宿主交给 `TraceRecorder.describe_run` 的部分（宿主档案、冻结后的角色、入口）合成一份
v2 文档，逐叶脱敏、按场景格式读一遍确认读得回来，再经 `put_replay` 写下。宿主把 `TraceRecorder.ids`（交出 uuid4 并记下每个
稳定键交出了什么）交给 `Runtime(ids=...)`；宿主没调 `describe_run` 时账本什么都不攒。账本任何一处出错只少这份文档（一条
ERROR）；回放造不出来的 run（Agent 的 id 不是 id 源交出的——工厂交回了别处构造的或先构造了别的、宿主漏交了角色、一个键交出
两个 id、模型调用不在任何一轮里、人的等待没有请求键、宿主在 run 中途自己发了消息或在请求之外中止了某个 Agent、脱敏规则认得
某个工具名、有内核之外的类式角色、两个角色名脱敏之后撞成一个）记一条三件套的 WARNING；store 拒收这份文档时 trace 不写结局。
被 Ctrl-C 打断的 run 记一条 `interrupt`：宿主在拆 Runtime 之前调 `TraceRecorder.interrupted()`，此刻还在等外部世界的调用记成
`waiting`——在飞的模型调用、开着的外部工具调用（在执行，或已经交回、结果还没落进轨迹：宿主的 hook 还在跑）、还挂着的人的
请求；底下开着一个确认的工具调用不算（在飞的是那个请求）。回放里结果已经记下、却在 `waiting` 里的那次调用停在它的替身上，
等打断同样地取消它。宿主没调 `interrupted()` 的 cancelled trace 记一条不带位置的 `interrupt`。CLI：`_run_traced` 交出 `ids`、
调 `describe_run`（`cli/capture.py` 从这次 run 真正用的判定器、策略与冻结出的 Agent 上读，不另写一份），被打断时先调
`interrupted()` 再关 Runtime；`--topology` 的 run 与带自定义类式角色的目录不交（建角色出错同样只少这份文档），只在人点名要了
replay / full 时说一句。

内核接缝：`Tracer.tool_result` 多一个仅限关键字的 `arguments`（真正交给工具执行的参数；`Tracer` 是本计划新加的公开面，
发布之前一并定下）；`encode_redacted` 多一个 `identifiers`（只收 32 / 64 位小写十六进制：内核生成的 id 与摘要原样保留，
名字像密钥的键 `key` 下面也一样，否则 agent id 会被当成凭证换掉、引用就断了；被拒的那一项不回显）；`encode_prepared`
（回放文档的每一项先在自己的上下文里经 `encode_redacted` 脱敏，拼起来之后只确认每个字符串都已经是脱敏器的不动点、
不再按外层的键给下面定性：整份一起脱敏时，一个像密钥的角色名 `second-pass` 下面的每个提供方工具调用 id 都被换掉，而指纹
是按各项自己的上下文算的）；`DangerRule.params`（工作区根的两条规则记下字面与解析之后的根），`DangerJudge.rules`、
`RuleTablePolicy.table` 只读；场景格式 v2 的角色可以没有实例（捕获列出宿主目录里的每个角色：交给模型的工具定义会点它们的名）。

与计划不同之处：(1) 回放文档里的自由文本内联、不引用 payload：D12 要求引用是为了躲开 YAML 标量的类型转换，而捕获写的是
JSON；内联让文档自成一份、`trace replay` 不必再读一批 payload，体积与引用相同（payload 的 JSON 里本来就有同一段文字）。(2) 捕获
的 `expect` 只有 `stop_reasons`，取自 agent span（回放按同一个判据取，所以被 despawn 的子 Agent 也比得到）：最终回答与错误只是
记录的回声，状态取决于注册表的生命周期（结束即 despawn 的子 Agent 在回放里查不到）。(3) `--topology` 的 run 不写回放文档、CLI
说明原因：拓扑槽给角色附加的工具与预算回放重建不出来。(4) 回放像 CLI 一样开着编排工具重建 Runtime，宿主档案没有记 Module 名单
时装安全与人机交互两个；别的宿主照样记下它们，否则第一次请求的「工具定义」就对不上。(5) 变异「工具桩按调用顺序取」在回放与
原始 run 一致时是等价的（本地工具都不声明可并发，一个 Agent 的调用按位置的先后执行）；它只在某次记录过的调用回放里没有执行时
才错位，那条由允许分叉的用例钉住。(6) `expect.stop_reasons` 取每个 Agent 最后一个工作周期的原因，被取消的周期没有原因（回放
同一个判据）：沿用前一个周期的原因会把一个被切断的 Agent 报成「做完了」。(7) 执行参数的指纹在脱敏之后算，一段脱敏认不出的
低熵秘密（hook 塞进参数的短口令）仍可能从指纹上被猜出：与请求指纹同一个前提，靠脱敏认得出真正的凭证。

两路对抗评审（捕获的保真度；鲁棒性、安全与内核接缝）都在写报告之前撞上了模型用量上限，发现取自它们的工作记录：中断位置
累计了整个 run 里被取消的调用（先被 kill 切断的子 Agent 的调用也在里面，真 SIGINT 复现回放卡住）、整份一起脱敏让像密钥的
角色名下的 id 被换掉、宿主在请求之外的动作被静默丢掉、规则认得工具名、没写文档的 run 也攒账本、默认级别下每次 `--topology`
都说一句、自定义类式角色让每次 run 记一条误导的 ERROR、建角色不隔离、读回失败的报错有两个「下一步」、文档的顺序随调度、
被切断的周期沿用旧的停下原因、标识校验回显被拒的值，以及 2 条存活变异。全部按上面的设计修掉；修后一轮 32 条变异没有存活。

### 阶段四：surface 与导出

#### T15 — `syndica trace list/show/replay/export/prune`

**Files:** `cli/main.py`、`cli/trace_view.py`、`src/syndica/__main__.py`、测试。

- `show` 的三个回答各一条用例（三个以上 Agent、非单调的创建顺序；等待链；最慢的三个工具调用按耗时排序）；所有来自
  模型或文件系统的字符串经 `terminal_safe()`。`replay` 三种退出码各一条，且它自己记的 trace 指向原 trace；`prune` 只删
  trace 目录、按开始时间保留最近的，且不删还没有结局的 trace（一次长跑的 trace 开始得最早、却还在写；崩掉的 trace
  同样没有结局，要删它得显式点名）。`list` 的大小由文件实现自己报（协议里没有这一项，需要时在这里加）。

#### T16 — OTLP/HTTP JSON 导出

**Files:** `backends/otel_otlp/`、`pyproject.toml`（extra `otel`）、`src/syndica/__main__.py`、测试。

- 见 D14。`MockTransport` 逐字段断言发出的 JSON（id 格式、纳秒时间是字符串、属性按类型、状态、事件、links，顺序用
  三个以上非单调的 span）；本地 mock collector（标准库 HTTP 服务）收到并解析成功；collector 不可达、返回 4xx / 5xx 时
  三件套告警、退出码不变。
- 端点与头：项目配置里写端点被拒（`ConfigError`，说明为什么）；环境的端点带环境的头；`--otlp` 给的端点不带环境的头；
  明文 HTTP 的非回环地址不带头。
- 依赖方向：内核不 import 这个 backend（`check_deps` 与一条字符串守卫）。

阶段四收口：阶段审计。

### 收口

#### T17 — L2 E2E

**Files:** `tests/integration/test_trace_e2e.py`、`tests/fixtures/trace/`（数据文件带 `.fixture` 后缀）。

- 多 Agent 派活的 run：`trace show` 的三个回答正确；`spans.jsonl` 的派生边与消息边齐全。
- 每个通道、每个规则族一个不同的假密钥：trace 目录里每个文件解码后的字符串与 16 字符片段都不含原文，预期的占位符都在。
- 失败的 run 捕获后 `trace replay` 复现同样的失败；在测试里给内核打一个改变行为的补丁后，回放报出分叉并指对单元。
- 导出到本地 mock collector 成功；trace 写入失败时 run 照常结束、stderr 恰好一条说明。
- Agent 用 Read 读 `.syndica/traces/…` 被拒。

#### T18 — 真实模型 E2E、文档与收口

- 在仓库副本里（M2 的 D22 同样做法）用默认轮换跑一次 `syndica-dev` 的真实小改动：`trace show` 回答三个问题；同一个
  trace 离线 `trace replay` 复现结局（不发任何模型请求）；设 `OTEL_EXPORTER_OTLP_ENDPOINT` 导出到本地 mock collector。
  记下请求数、429、trace 目录大小，据此定 `prune` 的默认值。
- 文档：README（可观测性一节、`trace` 命令、`--trace` 级别、脱敏、私有目录）；runbook 加一步；spec §4.4（第五个
  Store）、§11.3（默认级别与 D8 的表）、§12.3（统一 Redactor 落地）、§15 标 M3 状态、§17 追加本计划的决策；路线图；
  交接文档与 next-todos；`AGENTS.md` 硬约束第 8 条改成「写进 trace 与导出给第三方的数据都先过内核的 `Redactor`；
  发给模型提供方的请求是唯一的例外」。
- 写 M3 收口审计（`docs/audits/`），遗留项登记。

---

## 验证

| 层 | 内容 | 何时跑 |
|---|---|---|
| 门禁 | ruff format / check、pyright、check_deps、check_links、pytest | 每个任务提交前（本地）；CI 可用时每次 push |
| 契约 | `TraceStoreContract` 对内存与文件两种实现 | 随全量测试 |
| L2 | 回放场景（v1、v2）、捕获即回放的往返、T17 的 E2E | 随全量测试，零成本 |
| 真实模型 | T18 | 手动，只在副本里 |
| 审计 | 阶段二、阶段四各一次；M3 收口审计 | 阶段结束时 |

本地验证协议沿用 M2 计划「验证」一节（定向用例、六道门禁、碰 shell 的模块在 `SYNDICA_TEST_SHELL=/bin/dash` 下再跑、
自我变异、对抗评审、暂存区单独检出到临时目录验证后再提交）。CI 额度恢复后对 HEAD 补跑。

## 决策日志

- 2026-10-01 用户裁决：默认记录级别「可回放」（回复、工具结果、人的答复；请求只记指纹，完整请求 `--trace full`）；
  回放时内核之外按记录给出；trace 存成工作区里的文件；OTel 导出用 httpx 自写的最小 OTLP/HTTP JSON 导出器；M2 的三件
  遗留作为 M3 的头两个任务先做。
- 2026-10-01 两路独立评审（可行性；脱敏、信任、回放保真与测试强度）共 2 Critical、12 High、10 Medium，逐条修进本文：
  被 `PRE_TOOL_USE` 跳过的调用在回放里必然分叉（改为回放装再跳过的 hook）；只有 payload 有类型闸而命令行参数、
  提供方错误、`meta`、属性照样落盘，且「编码后再脱敏」本身会漏（改为逐叶脱敏的编码器与全量 `RedactedText`）；M2 的
  脱敏器不幂等（占位符改为对后续规则不透明）；内核不能写 YAML（捕获写 JSON，schema 归内核）；`tool.outcome` 漏了执行器
  的半数结局（按追加结果块的位置重列）；tracer 是同步还是异步没写（改为同步、缓冲加写入任务）；回放的键现有签名取不到
  （加「当前工具调用」上下文与人机事件的键、id 在工厂之后覆盖）；按记录返回处置会让丢掉 `UNTRUSTED` 的回归在回放里
  照绿（改为真跑判定并逐项比对）；项目配置选的 OTLP 端点会收到用户的鉴权头（端点只从环境或命令行来）；trace 经 `Read`
  读回成可信内容（宿主私有目录对文件工具关闭）；唤醒被误记成派生（派生边在 `spawn` 里发）；任务顺序里的隐藏依赖（id 与
  CLI 记录提前到用到它们的任务之前）；Ctrl-C 与 M2 退出码、`git: pending` 的关系；以及每个任务里最弱的那条断言。
- 2026-10-01 用户确认按计划开工，先做 T1、T2，第二轮评审的结果回来后先修计划再进入 T3。
- 2026-10-01 第二轮评审（5 High、9 Medium、7 Low）逐条修进本文。High：agent id 在工厂返回之后才覆盖太晚，工厂里已经
  绑定了它（改为 `spawn` 先取 id、经内核私有的 contextvar 交给 `BaseAgent`）；整个关掉 `.syndica` 会弄坏本仓 Worker 的
  `.syndica/tmp` 工作流（私有目录收窄到 `traces` 与 `handoffs`）；「原样的参数字节」与逐叶脱敏矛盾（参数存规范编码，
  指纹与往返比对按解析后的参数）；宿主自己的动作（EOF 触发的中止、Ctrl-C、超时）没法回放（加 `host_actions` 与注入的
  时钟）；`tool.outcome` 仍不是一一对应、拼写与内核枚举不一致（补三种结局、沿用内核拼写）。Medium：「当前工具调用」
  要由 Agent 交出 (id、周期、turn) 并在 `_drive` 里清掉；回放模型的键改从「当前 LLM 调用」取；危险规则工厂加 `real=`；
  规则集摘要的定义（`RULES_VERSION` 加黄金语料）；`named_assignment` 的误报门槛与不进提交闸；导出器经 `encode_redacted`
  发出、守卫扫整个 `src/syndica`；冻结只作用于触发切点的 Agent；`trace show` 需要的属性；chat 的消息可以发给任何
  Agent（chat 在 M3 只记 `structure`，回放随 M6 做）。Low：记录时建 `.syndica/.gitignore`；`trace export` 失败退出 1；
  T14 分两次 run；指纹按单元缓存；「`extra` 不发给提供方」这条理由是错的（OpenAI 兼容客户端会回传 `extra_content`）；
  宿主档案从 T12 起写。
- 2026-10-02 T3 对抗评审推翻了 D4 的字面做法：「按占位符切段、规则依次在段内匹配」让上下文规则看不到被前一条规则换掉
  的那一段——宿主环境里有一个 JWT 时，`Authorization: Bearer` 后面另一个同头部的 JWT 只换掉共用的片段，payload 与
  签名原样留下，M2 反而不漏。改为「每条规则在同一份视图上找全、重叠的区间合并、到不动点为止」，D4 的承诺（幂等、占位符
  不被再次命中）由不动点保证，D4 已按此改写。
- 2026-10-02 T3 的契约评审之后：`Redactor` 协议不进承诺面（还没有公开入口收它，trace 的写入面还要规则集摘要，协议的
  形状到那时连同这个成员一起定）；`RULES_VERSION` 也覆盖 `encode_redacted` 的行为（指纹按脱敏之后的编码算）；
  `RedactedText` 保证是一行、总能编码成 UTF-8，T11 的 JSONL 因此可以按行切而不必另做转义；用户规则的病态正则按
  「`.syndica.yaml` 是你运行的代码」处理，不在加载时猜回溯（嵌套量词的启发式会拒掉 `(?:[a-z]+\.)+corp` 这类常见写法）。
- 2026-10-02 T4 评审之后：D5 不再信任「标注为 `RedactedText` 的参数」（pyright 挡不住 `Any`，从 JSON 读出的值就是
  `Any`），改为落盘只经 `kernel/redaction.py` 的 `write_redacted`（运行时检查类型）与 `write_all`（内容在调用处按
  写入点规则看）；两者都把短写接着写完。D17 的名字比较加上按字面折叠 `..` 与根的别名。
- 2026-10-02 T5 评审之后：D6 的契约收紧到「一次写是原子的」（`append` 整批、并发 `begin` / `put_replay` 恰好一个
  成功）、`delete` 之后的写报错、读回是普通 `str`；头里的用户规则存成 `RedactionRule` 记录；span 的结束时间由单调时钟
  量出的时长算（D3），`finish` 时还开着的 span 以 `cancelled` 收尾；`prune` 不删没有结局的 trace（T15）。

## 风险与遗留（开工前登记）

- **并发下的回放分叉。** 几个 Agent 并发时，即使 id 按键取回，同一个 Agent 收到消息的时机仍可能随调度变化，请求指纹
  因此不同。T13 用固定种子的调度扰动量化；不稳定时回放按记录的消息注入时机（哪一轮之前）投递，而不是按真实的发送时机。
- **脱敏改变了组合文本的边界情形。** 回放的请求由分别脱敏过的片段拼成，个别情况下（密钥跨片段、`repr` 里的转义）与
  「整段脱敏」不同，表现为一次误报的分叉；`--allow-divergence` 与报告里的单元定位让它可以被认出来。
- **体量。** 「可回放」级别逐字存工具结果（`Read` 一次最多 2000 行）；按内容寻址去重之后，T18 记下真实 run 的大小。
- **第三方并发工具的写法被冻结拦下**（T10）：有意的契约收紧，写进 hook 契约与发布说明。
- **用户脱敏规则里的病态正则会卡住宿主**（T3）：`re` 的匹配不响应 Ctrl-C，一条灾难性回溯的 `redaction.rules` 能让
  `run --handoff` 停在脱敏上，只能 `kill`。与 `workspace.verify` 一样按「`.syndica.yaml` 是你运行的代码」处理，README
  写明；要挡得换带超时的正则引擎（第三方依赖，内核不收）或把用户规则放进子进程。
- **紧贴在字母数字后面的格式 key 认不出**（T3）：格式规则刻意不从一个词的中间起头（否则 `task-…`、`risk-…` 这类长词
  会被当成 `sk-` key），所以 `xsk-proj-…`、`fooAKIA…` 这种粘连只靠逐字值与片段兜住（宿主环境里有这个 key 时）。M2 的
  `\b` 同样如此。
- **Bash 能在 `.syndica` 的位置上建文件**（T4 评审）：Write 已经不许，Bash 不受 D17 约束。`.syndica` 成了文件时
  宿主建不出 trace 目录；T11 的记录要把它当成一次可报告的失败（记录不了、run 照常），不能让 run 因此失败。
- **宿主私有目录只挡文件工具**（D17）：Bash 照样读写得到，Bash 写下的文件经 `Read` 读回也照样成了可信内容；真正的
  边界是 M5 的沙箱。
- **chat 里过期的提问会吞掉下一行**（T2 评审发现，M2 就有）：chat 的提示符在 `main` 里直接读 stdin，不经宿主的
  `_Lines`；AskUser 超时之后还卡着的那次 readline 读走人敲的下一行（它早于下一次提问，被丢掉）。`run --handoff` 的
  git 提问已经经 `_Lines`。要修得让 chat 的提示符也经同一个读者（`CliHost` 协议要加一个读行的入口）；随会话持久化
  （M6）一起做。
- **结论交回与打印之间的按键**（T2 第二轮评审）：`run --handoff` 的结论交回主程序之后、打印完之前（不到一毫秒；stdout
  堵住时更长）按下的 Ctrl-C 由主程序按普通中断报，退出 130，打印可能不全，而磁盘上的交接文件已经是最终那份。主程序
  的消息因此只说「可能没有做完」并指向交接文件。要关掉它得让宿主的处理一直装到打印完，与 CLI 的入口一起改。
