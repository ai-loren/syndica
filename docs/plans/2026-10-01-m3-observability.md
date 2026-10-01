# 里程碑 3：可观测性 + 脱敏收口

> **执行者须知：本文件是需求，不是参考。** 「公开契约」一节里的 trace schema、span / 边的种类、属性键与类型、
> 规则 ID、回放文档的键、CLI 参数、配置键与环境变量一旦发布就是兼容承诺；执行中不得为了让测试变绿临时改名或
> 缩小断言范围。

**状态：草案，已经过一轮两路独立评审修订（2026-10-01），待用户确认。** 五项裁决由用户确认（见「决策日志」）。
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
  `RedactedText` 类型闸；写入点与外发点守卫扩到全仓；宿主私有目录（`.syndica`）对文件工具关闭。
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
- 内核经一个内部保护层调 tracer：只接 `Exception`（`CancelledError`、`KeyboardInterrupt` 照常上抛），第一次失败记
  一条 ERROR（三件套），此后同一个 tracer 的失败只计数，Runtime 关闭时报一次总数；run 照常进行。

**D3 span、边与确定的 id。**

- span 种类：`agent turn llm_call tool_call confirmation wait`；`module` 保留。消息的发送是发送方当前 span 上的一个
  事件。边：`spawn`、`message`（发送时的 span → 接收方消费这条消息的那一轮的 turn span，按 message id 连）；
  `artifact`、`verification` 保留给 M7。等待 span 带被等的对象（`wait.target`）。
- id：trace id 32 个十六进制字符（随机）；span id 16 个十六进制字符，取 `sha256` 于**长度前缀编码**的结构键：
  agent =（agent id、周期序号），turn =（agent id、turn），llm_call =（agent id、turn、第几次调用：首次与每次重试
  分开），tool_call 与 confirmation =（agent id、周期、turn、批内位置），wait =（agent id、该 Agent 的第几次等待）。
  模型给的 `tool_call.id` 只作属性记录（脱敏后），不进键：它可以重复、可以是空串。
- 时间：`Tracer` 收可注入的时钟（纳秒，默认 `time.time_ns`）。状态：`ok`、`error`、`cancelled`。

**D4 统一 `Redactor`：幂等、逐叶、带类型闸。**

- `syndica.kernel.redaction`：`Redactor` 协议（`redact(text) -> str`、`scan(text) -> tuple[Finding, ...]`）与实现
  `RuleRedactor(*, secrets=(), rules=())`。十条内置规则的 ID、顺序与占位符 `[REDACTED:<规则 ID>]` 与 M2 一致；
  新增 `secret.named_assignment`：形如 `NAME=value`、`"name": "value"` 的赋值里，名字按 M2 D6 的词表像密钥、值像
  凭证时替换值（`.env` 里的 `sk_live_…`、AWS secret key 这类没有固定格式的值此前一条都不命中）。名字的判定
  （`looks_secret` 与它的词表）移进内核：它是对名字的纯函数，不读环境。
- **占位符对所有后续规则不透明**：`redact` 先按已有的占位符切段，只在段内匹配，所以 `redact(redact(x)) == redact(x)`
  对所有规则成立；修掉 M2 `url_userinfo` 吃占位符的问题。用户规则 `RedactionRule(rule_id, pattern)` 的 ID 必须以
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

- 按根目录扫 `src/syndica/cli`、`src/syndica/runner`、`src/syndica/kernel`、`src/syndica/backends`，每个根一个
  「必须扫到」的锚文件与一次独立的 `os.walk` 交叉核对。
- 安全的内容只认：字面量、模块级绑定字面量的名字、直接调用从 `syndica.kernel.redaction` import 的 `redact` /
  `encode_redacted` 的结果、类型标注为 `RedactedText` 的参数。禁止在 `kernel/redaction.py` 之外 `def redact`、
  构造 `RedactedText(...)`、`cast(RedactedText, ...)`。
- 外发点：`.post` / `.put` / `.request` / `.send` 带 `json=` / `content=` / `data=` 的调用按写入点同样检查。显式白名单
  两类并写明理由：Agent 自己的工具（`LocalWorkspace` 的写文件与子进程管道、`tools/local.py`）；LLM 客户端发给模型
  提供方的请求（M2 D5 的威胁模型：对话本来就要发给提供方，这是 run 的前提，不是观测数据）。`FileTraceStore` 与
  OTLP 导出器不在白名单里。

**D6 `TraceStore` 是第五个 Store Protocol。**

- 写：`begin(header)`（同一 trace id 第二次 `begin` 报错）、`append(trace_id, spans, edges)`、
  `put_payload(trace_id, RedactedText) -> PayloadRef`（引用是内容的 sha256，由 store 算，不由调用方给；重复的只存一份）、
  `put_replay(trace_id, RedactedText)`、`finish(trace_id, status, ended_ns)`；未 `begin` 或 `finish` 之后的写报
  `ContractViolation`。
- 读：`traces()`（按开始时间、再按 id 排序）、`find(prefix)`（唯一前缀才返回，歧义报错）、`get(trace_id)`、
  `spans(trace_id)`、`edges(trace_id)`、`payload(ref)`、`replay(trace_id)`；`delete(trace_id)`。读到的是快照：改
  返回值嵌套的属性，**再读同一个 trace** 不受影响。
- 同一个 trace 的追加按调用顺序保序；并发追加不丢不乱序。内存实现进 `kernel/stores/`；文件实现（T11）同样在内核
  （只用标准库）；两者过同一套契约。spec §4.4 补上第五个 Store。

**D7 文件布局与落盘安全（用户裁决：文件）。**

- `.syndica/traces/<UTC 时间>-<trace id 前 8 位>/`：`trace.json`（头）、`spans.jsonl`、`edges.jsonl`、
  `payloads/<sha256>.txt`、`replay.json`（场景格式 v2 的同一份 schema，捕获写 JSON：内核不能依赖 PyYAML，PyYAML
  读 JSON 也会把非 ASCII 字符弄坏）。目录 0700、文件 0600；`.syndica/.gitignore` 照旧。
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
Agent 自己的轮次里写）。冻结用计数而不是布尔（可以嵌套）。写进 hook 契约（`hooks/junctions.py` 的 docstring）；
`tests/kernel/test_orchestration.py` 里那条从观察者追加的用例改写。

**D10 可复现的 id 与稳定键。**

- 执行器在执行每个工具调用时设一个内核 contextvar「当前工具调用」（agent id、周期、turn、批内位置、模型给的 id）；
  `spawn` 与 `send` 从它取键；人机请求的事件补上同样的键（现在只带 `request_id`）。
- `Runtime(..., ids=None)` 收一个 id 源（默认 uuid4）。agent id 在工厂返回之后、任何东西绑定它之前由 id 源覆盖；
  message id 同样从 id 源取。稳定键：Agent =（创建它的工具调用键）或（宿主，第几次 spawn）或（拓扑槽、键、第几次）；
  message =（发送时的工具调用键或宿主，第几条）；人的答复 =（它答复的那个请求的键）。按键而不是按全局顺序：几个
  Agent 并发时创建顺序取决于调度。
- 后台命令的 handle 与 `request_id` 不需要复现：它们不进模型的上下文（`extra` 不发给提供方），工具按记录回放。

**D11 回放：外部世界按记录给出，内核全真并逐项比对（用户裁决）。**

- **模型：** `ReplayLLM` 按（Agent、turn、第几次调用）给出记录的回复——含首次调用与 `RetryModule` 的每次重试、`extra`、
  原样的参数字节、带类型的错误；原始 run 在某次调用上被打断（kill、shutdown、Ctrl-C）的，回放在同一处阻塞，直到被
  同样地取消。`RetryModule` 的等待在回放里注入成不等。
- **指纹：** 原始 run 对每次请求按单元（模型参数、系统提示词、工具定义、每条消息）取指纹：每个单元先逐叶脱敏（D4）
  再规范编码再取 sha256；`extra` 与 `trust` 不进指纹（它们不发给提供方），提供方身份不进。回放用**记录下的规则集**
  （内置规则与用户规则，不含宿主环境里的值）对自己的请求做同样的计算，第一个不一致的单元即失败并报出 Agent、轮次与
  单元；`--allow-divergence` 时只记录。规则集的摘要也记进 trace，两边不一致时报出来。
- **工具：** 触达外部世界的工具（`Read Write Edit Glob Grep Bash BashOutput KillBash`）与输出取决于时机的
  `QueryAgent` 按工具调用键返回记录的结局（`ToolResult` 的输出、`meta`、`trust`，或「工具抛了某个异常」），不碰磁盘、
  不起进程；`Delegate Fork SendMessage KillAgent` 与人机工具真跑——它们是内核。
- **跳过：** 原始 run 里被 `PRE_TOOL_USE` 跳过的调用（CLI 的 git 守卫、`--no-write` 的只读守卫、第三方 hook）记成
  `skipped` 并带最终结果；回放装一个内核侧的 `PRE_TOOL_USE` hook，按调用键原样再跳过。否则这些调用在回放里会走到
  危险闸，被 `untrusted_high_risk` 拦下、给出不同的文字，下一次请求的指纹必然不同。
- **危险判定与处置：** 回放从宿主档案按顺序重建规则（规则 ID 加参数，工作区根记字面值与解析后的值）、`RuleTablePolicy`
  的表与 Flag，**真跑** `disposition_of` 与策略；原始 run 记下每次闸门判定（命中的规则、风险等级、溯源、由 Flag 还是
  策略决定、处置），回放逐项比对，不一致、缺少或没被消费的都算分叉。只有人的答复按记录给出。这样丢掉 `UNTRUSTED`
  标记、改坏判定器的改动会在回放里现形（`trust` 不进请求字节，只比轨迹文本看不出来）。
- **人：** 确认与 AskUser / AskUserQuestion 的答复按请求的键给出；超时、中止、被抢占直接给出结局，不真的等。
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
  只随环境里给的端点发出；明文 HTTP 的非回环地址不带头；头里的值进脱敏器的 `secrets`。导出失败只告警，不影响退出码。

**D15 CLI。**

- `run` 与 `chat` 收 `--trace`；`trace.level` 与 `redaction.rules` 在 run 开始时快照（Agent 中途改 `.syndica.yaml`
  不影响）。结束时 stderr 打出 trace 目录（经脱敏与转义）；`--handoff` 的交接文件正文写上 trace 目录（front matter
  不变）。`chat` 的一个会话是一个 trace，每条消息是根 Agent 的一个工作周期；回放文档的 `entry` 记下消息序列。
- `trace list`、`trace show <id>`、`trace replay <id> [--allow-divergence]`、`trace export <id> --otlp <url>`、
  `trace prune [--keep N]`（默认 50）。`<id>` 接受完整 id、唯一前缀或目录名。
- `trace replay` 本身也记一个 trace（`structure` 级，新的 trace id，`source.replay_of` 指向原 trace）。退出码：复现
  原始结局且没有分叉 0；有分叉或结局不同 1；回放文档读不进来、版本不认识 2。回放引擎在 runner 里，由 `__main__.py`
  注入 CLI。
- **Ctrl-C：** 退出 130，优先于 1 与 3（中断本身就是没完成的原因）；stderr 没有 traceback。交接文件写出之前被中断：
  照样写交接文件，`status: run_failed`，原因「被中断」，复核标 `skipped`；交接文件写出之后（git 收尾时）被中断：
  照 M2 D13，文件停在 `git: pending`。trace 以 `cancelled` 收尾。

**D16 M2 遗留（用户裁决：先做）。**

- **危险规则（内核，T1）：** 共享分词把 shell 的 `-c` 参数（`sh bash dash zsh ksh`，包括 `-lc`、`-ec` 这类组合选项）
  与 `eval` 的参数当成一段命令递归判定；`xargs` 进 wrapper，带一张选项取值表（`-n -I -P -L -s -d -E -a` 等）；`find`
  的 `-exec -execdir -ok -okdir` 到 `;` 或 `+` 之间是一条命令；`find <根或根下全部> -delete` 进
  `shell.workspace_root_removal`。递归深度有上限，超过上限**按命中处理**（宁可多判）。与 HEAD 在 M2 那批 8253 条
  命令上差分：新增命中逐条过目，少认的逐条给出理由。
- **git 收尾的摘要（CLI，T2）：** 每个改动路径后面给出 `+N −M`（`git diff --numstat`，新文件数行数，二进制写「二进制」）。
- **Ctrl-C（CLI，T2）：** 见 D15 末条。

**D17 宿主私有目录。** `LocalWorkspace(..., private=())` 收一组相对根的目录（CLI 传 `('.syndica',)`）；文件工具
（Read、Write、Edit，以及 Glob / Grep 的显式路径）拒绝这些目录下的路径，比较不分大小写、按目录身份（与 M2 的 `.git`
保护同样做法），给模型的错误说明为什么。理由：trace 里存着此前标成不可信的工具输出，`Read` 读回来是可信的，等于
绕过 spec §12.2 的溯源升级；Agent 改得到以前的交接文件（M2 遗留）也一并关掉。Bash 照样读得到（它的输出本来就是
不可信的），这条挡的是失手与洗白，不是蓄意（真正的边界是 M5 的沙箱）。

---

## 公开契约

### CLI 与环境变量

| 用法 | 语义 |
|---|---|
| `run|chat --trace off\|structure\|replay\|full` | 记录级别，默认 `trace.level` 或 `replay` |
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
  `redaction`（规则集的 sha256 与用户规则的 ID）、`source`（命令行参数、配置文件的 sha256、syndica 版本、git commit、
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
  `danger.risk` str、`danger.provenance` str、`danger.decided_by` str（`flag policy`）、`danger.disposition` str、
  `confirmation.outcome` str（`approved rejected timed_out aborted preempted`）、`wait.target` str、`message.id` str。
- `tool.outcome` 取值：`ok error aborted judge_failed not_executed skipped blocked rejected preempted no_channel
  unknown_tool bad_arguments`（与执行器追加结果块的位置一一对应）。
- 版本升级要有读旧版本的兼容测试；枚举的成员→取值表由一条整体断言钉住。

### 场景格式 v2（手写 YAML 与捕获 JSON 共用一份 schema）

在 v1 之上：`version: 2`；`host`（`flags`、`danger.rules`（有序，`{id, params}`）、`danger.table`、`output_limit`、
`host_facts`、`max_turns`、`no_write`、`modules`）；`agents.<Role>` 可以用 `frozen`（`system_prompt` 正文、`tools`、
`max_turns`、`impl`）代替 `manifest`；`instances` 带稳定键（`key: {tool_call: …}`、`{host: n}`、`{slot, key, n}`）；
回合的 `content` 可以是列表、带 `extra`、`tool_calls[].arguments_raw`、带类型的 `raises`、`request_digests`；实例的
`tool_results`（按调用键：`output`、`meta`、`trust`，或 `raised: {type, message}`）、`skipped`（按调用键的最终结果）、
`gate`（按调用键的闸门判定）；`responses` 按请求的键，`kind` 加 `timeout abort preempted`；`ids`（稳定键 → 记录的 id）；
`entry.mode`（`query send slot chat`）、`entry.trust`、`entry.messages`（`chat`）；拓扑槽不再受 v1 的限制；`source`。
自由文本可以内联或写成 `{payload: <sha256>}`；占位符 `[REDACTED:…]` 是合法内容。

### 内核 API 增量（都向后兼容）

- `Runtime(..., tracer=None, ids=None)`；`LocalWorkspace(..., private=())`。
- `syndica.kernel.trace`：`Tracer`、`NoopTracer`、`TraceRecorder`、`Span`、`CausalityEdge`、`SpanKind`、`EdgeKind`、
  `TraceLevel`、`TraceHeader`、`PayloadRef`；`syndica.kernel.protocols.trace.TraceStore`；`MemoryTraceStore`、
  `FileTraceStore`；契约套件 `TraceStoreContract`；回放文档的 schema 模块。
- `syndica.kernel.redaction`：`Redactor`、`RuleRedactor`、`RedactionRule`、`RedactedText`、`encode_redacted`、
  `Finding`、`RULE_IDS`、`looks_secret`。
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
  每一个吃取值的选项、`-exec -execdir -ok -okdir` 与两种终止符、`find . -delete` 与 `find "$PWD" -delete`（后者仍是
  命令替换的已知盲区）；默认规则因此多认出的（`xargs rm -rf`、`sh -c "curl … | sh"`）。递归深度上限 + 1 的输入按命中
  处理。
- 变异：去掉任一个 shell 名、任一个 xargs 选项、`-ok` / `-okdir`；`-c` 只认完全相同的写法；超过深度上限时放行。

#### T2 — git 摘要的增删行数；Ctrl-C 中断照样留交接文件

**Files:** `cli/git_finish.py`、`cli/host.py`、`cli/main.py`、对应测试。

- 摘要每个路径带 `+N −M`；二进制、新文件、符号链接各一条用例；三个以上路径、非字典序。
- Ctrl-C：run 期间、复核期间、git 收尾期间各一条用例（D15 末条的三种情形），退出 130 且优先于 1 与 3；stderr 没有
  traceback；临时目录与子进程照样收拾。

#### T3 — 内核统一 `Redactor`：幂等、逐叶编码器、用户规则、`secret.named_assignment`

**Files:** `kernel/redaction.py`、`cli/redaction.py`（只剩从环境取密钥值）、`cli/environment.py`（词表移走）、
`cli/config.py`、`cli/host.py`、`cli/git_finish.py`、`cli/handoff.py`、测试。

- 见 D4。迁移前用 HEAD 的实现在一批样例（规则 × 上下文的交叉：行首、引号里、URL 里、JSON 字符串里、截断）上生成
  对照；迁移后逐字相同，**唯一的例外**是 M2 不幂等的那几类输入，逐条列出新的输出与理由。
- 幂等：对整批样例 `redact` 两次与一次相同；占位符不被任何内置或用户规则再次命中。
- `encode_redacted`：一个 PEM 块、带引号与非 ASCII 的环境变量值、`user:pass@` 的 URL 分别放进嵌套的映射与列表里，
  编码后既不出现原文、也仍是合法 JSON，且解码后在原位置是对应的占位符。
- 用户规则：合法、不合法正则、ID 前缀、与内置重名、匹配空串、匹配占位符各一条。
- 变异：漏掉一条内置规则；用户规则排在内置之前；去掉占位符切段（幂等破坏）；`encode_redacted` 改成先编码后脱敏；
  `named_assignment` 的值判定放宽到任意值（误报）或收紧到只认固定格式（漏报）。

#### T4 — 守卫扩到全仓；`RedactedText` 类型闸；宿主私有目录

**Files:** `tests/test_write_points.py`（由 `tests/cli/test_write_points.py` 迁出并扩展）、`kernel/redaction.py`、
`kernel/local_workspace.py`、`kernel/tools/local.py`、`cli/host.py`、测试。

- 见 D5、D17。每个扫描根各有锚文件与 `os.walk` 交叉核对；在每个根里种一个违规样例证明被抓到：白名单之外写文件、
  `client.post(json=...)`、另起一个 `def redact(self, x): return x`、`cast(RedactedText, ...)`、白名单目录里的新文件。
- 私有目录：Read / Write / Edit 拒 `.syndica/…`（含 `.SYNDICA/…` 与经符号链接的路径）；Glob / Grep 显式指向它时同样
  拒；Bash 不受影响；错误说明写明为什么。

### 阶段二：trace 模型与捕获

#### T5 — trace 模型、`TraceStore` 协议、内存实现与契约套件

**Files:** `kernel/trace.py`、`kernel/protocols/trace.py`、`kernel/stores/memory_trace.py`、`kernel/contracts/trace.py`、
`tests/contracts/test_memory_trace.py`、`tests/kernel/test_public_api.py`。

- 见 D3、D6。契约逐条：保序（三条以上、跨两次 `append`）、快照（改一次读回的 span 的嵌套属性，再读同一个 trace 不变）、
  第二次 `begin`、未 `begin` 与 `finish` 之后的写、收到 `str` 而不是 `RedactedText`、payload 去重且引用由 store 算、
  `find` 的唯一与歧义、`traces()` 的顺序（三个 trace，开始时间非单调地写入）、`delete`、并发追加（三个任务，每次追加
  之间 `await asyncio.sleep(0)`）。
- 故意违约的子类（丢掉最后一条、不深拷贝、`finish` 后仍收、接受 `str`）让契约变红；枚举成员→取值表整体断言。

#### T6 — 可复现的 id 与「当前工具调用」

**Files:** `kernel/runtime.py`、`kernel/tool_executor.py`、`kernel/tools/human.py`、测试。

- 见 D10。默认 id 源与现状一致（uuid4，形式不变）；注入的 id 源对 agent 与 message 都生效且在任何东西绑定之前；
  「当前工具调用」在工具执行期间可见、执行完复位、并发段里各调用互不串线；人机请求的事件带上键。
- 稳定键：三个子 Agent 以与记录相反、且非单调的顺序被创建时，各拿到自己记录的 id。

#### T7 — 内核插桩：同步的 `Tracer` 与因果边

**Files:** `kernel/trace.py`、`kernel/agent.py`、`kernel/tool_executor.py`、`kernel/runtime.py`、`kernel/retry.py`、测试。

- 见 D2、D3。插桩前先列出 D2 列的每个「中间没有挂起点」的窗口，各写一条用例证明插桩没有在其中引入挂起点
  （tracer 在回调里断言自己处在同步调用中、计数事件循环的调度槽位不变）。
- 每个插桩点一条用例；`tool.outcome` 的每个取值各一条（含钩子看不见的那些：未知工具、坏参数、跳过、阻断、拒绝、
  抢占、没有确认通道、判定器出错、未执行、中止）；被取消的 Agent 的 agent span 以 `cancelled` 收尾；`RetryModule`
  的每次重试是一个独立的 llm_call span（`llm.attempt`）。
- 因果：Delegate 派出的三个子 Agent 各有一条从对应 Delegate 调用 span 出发的派生边；被 `SendMessage` 唤醒的 zombie
  只有消息边、没有派生边；三条消息各自连到接收方消费它的那一轮，互不串线。
- span id：断言完整的「结构键 → id」映射（三轮以上、三个调用以上、含一个重复的 `tool_call.id` 与一个空串 id），且两两
  不同；结构键用长度前缀编码。
- 失败隔离：每次都抛 `Exception` 的 tracer 不影响 run 的结局，日志恰好一条 ERROR（断言 `record.args` 里的 tracer、
  步骤与级别），关闭时报出失败次数；抛 `CancelledError` 的照常上抛。

#### T8 — LLM 侧：token、作答平台、平台故障、前缀单位

**Files:** `backends/llm_openai/client.py`、`backends/llm_router/router.py`、测试。

- 见 D13。`usage` 有、没有、缺一部分各一条；`extra` 不进请求（逐字比对发出的 JSON）；换平台时 `llm_failures` 按顺序
  列出被换掉的三个平台；`cache_prefix_units` 不换平台时与作答成员逐字节相同。

#### T9 — `TraceRecorder`：缓冲、写入任务、逐叶脱敏、级别、指纹

**Files:** `kernel/trace.py`（或 `kernel/trace_recorder.py`）、测试。

- `TraceRecorder(store, redactor, *, level, clock)` 实现 `Tracer`：同步缓冲、一个写入任务按序落盘、`finish` 排空；
  按 D8 的级别决定记什么；所有字符串经 `encode_redacted`；请求按 D11 取逐单元指纹；宿主档案与人的答复由宿主报给它。
- 脱敏用例：每个通道一个**不同的**假密钥（提示词、命令行参数、工具参数、工具输出、`meta`、模型回复、`extra`、
  提供方错误文本、未知工具名、模型给的调用 id、环境变量样例），每个规则族至少一个；断言 store 里解码后的每个字符串
  与每个 16 字符片段都不含任何原文，**且**每个通道的位置上确实是预期的占位符（防止「什么都没记」也通过）。
- `structure` 没有 payload；`full` 有完整请求且已脱敏；store 抛错时 run 照常、恰好一条 ERROR。

#### T10 — B-19：并发窗口里不许写轨迹

**Files:** `kernel/context.py`、`kernel/tool_executor.py`、`kernel/hooks/center.py`、`kernel/hooks/junctions.py`
（契约 docstring）、`tests/kernel/test_orchestration.py`、测试。

- 见 D9。三个声明可并发的工具、它们的 `POST_TOOL_USE` hook 写块 → `ContractViolation`；只有一个观察者的 `TURN_END`
  写块 → 同上；嵌套分发里冻结计数正确复位；串行的单调用段写块照旧可以。
- 变异：计数不复位（之后所有写入都被拒）；只冻结工具段不冻结观察者分发；只在两个以上观察者时冻结。

阶段二收口：阶段审计。

### 阶段三：文件存储、CLI 记录与回放

#### T11 — `FileTraceStore` 与 CLI 记录

**Files:** `kernel/stores/file_trace.py`、`cli/host.py`、`cli/main.py`、`cli/config.py`、`src/syndica/__main__.py`、测试。

- 见 D6、D7、D8、D15。`FileTraceStore` 过契约套件；`O_NOFOLLOW` / `dir_fd`（在 run 中途把 `payloads` 换成符号链接，
  写入被拒、不写到链接指向的地方）、`O_EXCL` 与 sha256 核对各一条；写失败（目录只读）时 run 照常、stderr 一条说明。
- CLI：`run` / `chat` 默认记录、四个级别、`trace.level` 快照、宿主档案（规则带参数、处置表、Flag、`--max-turns`、
  `--no-write`、模块、拓扑与入口）与人的答复报给 recorder、交接文件写 trace 目录、Ctrl-C 时以 `cancelled` 收尾。

#### T12 — 场景格式 v2：内核的 schema 与 runner 的两种读法

**Files:** 内核的 schema 模块、`runner/scenario.py`、`runner/yaml.py`、测试。

- 见 D12。v1 的全部现有场景与用例原样通过；v2 的每个新键各有合法与不合法的用例（三件套）；`{payload: sha}` 的引用；
  `impl` 只认内核内置类与白名单。
- 有序字段（`danger.rules`、`frozen.tools`）对照手写的固定装置逐项比顺序；同一份 v2 文档写成 YAML 与 JSON，读出的
  结果相同。

#### T13 — 回放执行

**Files:** `runner/replay.py`、`runner/scenario.py`、测试。

- 见 D11。`ReplayLLM`：首次调用与重试各按键给出；被打断的调用处阻塞；逐单元指纹第一处不同即失败并报出单元。工具桩：
  测试期间禁止任何子进程与写模式的 `open`（打补丁断言零次），编排与人机工具真跑，`QueryAgent` 走桩。跳过 hook 原样
  再跳过。危险判定真跑并与记录逐项比对（改坏判定器、丢掉一个 `UNTRUSTED` 标记各一条用例证明报出分叉）。
- 宿主档案重建：每个字段一条用例证明回放里生效。
- 并发：两个子 Agent 交错的 run，在一组固定种子的调度扰动下（每次插入不同位置的 `sleep(0)`）回放结局与分叉判定一致。
- 未消费的记录算分叉；回放文档读不进来退出 2。

#### T14 — 捕获即回放

**Files:** `kernel/trace.py`（recorder 写回放文档）、`tests/integration/test_capture_replay.py`。

- recorder 在 `finish` 时写 `replay.json`，`expect` 填原始结局。
- 往返：同一个脚本化 CLI run（含派活、消息、确认、AskUser、守卫跳过的 `git commit`——发生在一次不可信的 Bash 输出
  之后——、被阻断的调用、失败的工具、`--no-write`、一个被 kill 的子 Agent）捕获后回放：两份轨迹逐块相同（比 `role`、
  `content`、`tool_calls`、`trust`，不比 `extra`），闸门判定逐项相同，`expect` 全部成立；一个以错误结束的 run 回放出
  同样的错误；一个 Ctrl-C 中断的 run 回放到同一处。
- 变异：recorder 漏记一种工具结局；工具桩按调用顺序而不是按调用键取；指纹在脱敏前算（固定装置里让一个可脱敏的值
  进入之后的请求，这条才杀得死）；跳过 hook 不装。

### 阶段四：surface 与导出

#### T15 — `syndica trace list/show/replay/export/prune`

**Files:** `cli/main.py`、`cli/trace_view.py`、`src/syndica/__main__.py`、测试。

- `show` 的三个回答各一条用例（三个以上 Agent、非单调的创建顺序；等待链；最慢的三个工具调用按耗时排序）；所有来自
  模型或文件系统的字符串经 `terminal_safe()`。`replay` 三种退出码各一条，且它自己记的 trace 指向原 trace；`prune` 只删
  trace 目录、按开始时间保留最近的。

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

## 风险与遗留（开工前登记）

- **并发下的回放分叉。** 几个 Agent 并发时，即使 id 按键取回，同一个 Agent 收到消息的时机仍可能随调度变化，请求指纹
  因此不同。T13 用固定种子的调度扰动量化；不稳定时回放按记录的消息注入时机（哪一轮之前）投递，而不是按真实的发送时机。
- **脱敏改变了组合文本的边界情形。** 回放的请求由分别脱敏过的片段拼成，个别情况下（密钥跨片段、`repr` 里的转义）与
  「整段脱敏」不同，表现为一次误报的分叉；`--allow-divergence` 与报告里的单元定位让它可以被认出来。
- **体量。** 「可回放」级别逐字存工具结果（`Read` 一次最多 2000 行）；按内容寻址去重之后，T18 记下真实 run 的大小。
- **第三方并发工具的写法被冻结拦下**（T10）：有意的契约收紧，写进 hook 契约与发布说明。
- **宿主私有目录只挡文件工具**（D17）：Bash 照样读写得到，真正的边界是 M5 的沙箱。
