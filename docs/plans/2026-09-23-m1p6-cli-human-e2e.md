# 里程碑 1 · 计划 6：CLI、人机交互与 L2 场景回放

> **执行者须知：本文件是需求，不是参考。** 公开命令名、事件 kind、工具名、Flag key 与场景格式一旦
> 发布就是兼容承诺；执行中不得为了让测试变绿临时改名或缩小断言范围。

**状态：执行中。**

**Goal:** 提供一个可安装、可脚本测试的 `syndica` CLI；让 Agent 能通过 `AskUser` /
`AskUserQuestion` 等待人类答复，让交互式宿主能批准或拒绝单次危险调用；并提供 YAML 声明式 L2
场景回放器，以脚本化假 LLM 驱动真实 Runtime、Agent、工具、消息与事件流。

**Architecture:** `syndica.cli` 是只依赖 kernel 的 surface；`syndica.__main__` 是进程级 composition
root，按命令惰性装配可选 backend。YAML 只在 surface / testing 层解析成 Mapping，kernel 继续不认识
YAML。人机请求通过进程内 `EventBus` 发出，通过带 correlation id 的 `Runtime.send` 回答，并由现有
`Runtime.wait_for` 完成 WAITING 状态机。工具确认先由危险策略决定一次，再在批准后独占工具执行区间，
不与同批兄弟工具重叠。L2 runner 只替换 LLM，其他路径全部使用生产实现。

**Tech Stack:** Python 3.11、argparse、PyYAML、pytest、pytest-asyncio；不引入 CLI 框架，不把
PyYAML import 进 kernel。

**依据：** spec §5.6、§9.1、§13.1；M1 scope 的计划 6；登记表 D-38。

---

## 已确认边界

### 做

- `syndica` 命令树采用名词 + 动词；默认子命令只对“首个位置参数是 prompt”这一种情况补 `run`。
- `run`、`chat`、`agents list/show`、`topology list/show`、`flags list/show`、`doctor`、`version`。
- `run` 接受 `--agent` / `--topology` / `--session`、四个独立预算参数、可重复 `--flag key=value`。
- `chat` 的每一条普通输入都构造同一种 `RunRequest(session=<当前目标>)` 并调用与 `run` 相同的
  `run_request`；支持查看 Agent 与切换目标，提示符始终显示当前目标。
- `.yaml` / `.yml` 安全解析成 Mapping，再交给 `AgentCatalog.load_mapping` 或本计划的 topology /
  scenario parser；CLI 与 runner 各自持有薄适配器，surface 之间不互相 import；错误必须带
  期望 / 实际 / 下一步。
- `AskUser` / `AskUserQuestion`、人机请求事件、按 correlation id 回答、等待四态。
- 人工确认的批准 / 拒绝 / 超时 / 抢占 / 中止；批准只作用于当前 tool call。
- L2 YAML 场景：脚本化 LLM、真实 Runtime、真实内置或本地工具、最终输出、工具调用顺序、状态快照、
  事件顺序与失败路径断言。

### 不做

- 不实现 SessionStore 或跨进程会话恢复；那属于里程碑 6。`sessions ...` 与控制台进程中新鲜启动的
  `run --session` 必须明确说明底层尚不可用，不得假装持久化成功。
- 不实现 cost / token / wall-clock 预算执行器；那属于里程碑 8。参数形态现在固定，非空值在执行前
  明确报“不支持”，只有 `--max-turns` 落到现有 `AgentOptions.max_turns`。
- 不实现 server、HTTP、SSE、Web UI、PTY、trace 捕获、批处理或真 LLM 评判。
- 不给 Runtime 加 `start()`，不加 `Junction.RUNTIME_START`。
- 不让 `syndica.cli` import `syndica.backends.*`；backend 只由顶层 composition root 注入。
- 不在 kernel 读取环境变量、当前目录、stdin 或终端能力。

### TDD 接缝

本计划的测试只落在四个已由 spec / scope 确认的公开接缝：

1. `main(argv, host, io) -> int` 的命令行输入、stdout/stderr 与退出码。
2. `AskUser*` / confirmation request 事件 + `Runtime.send(correlation_id=...)` + 工具结果。
3. `ToolExecutor.execute_tool_calls` 对并发工具与获批确认工具的可观察执行区间。
4. `load_scenario(...)` / `ScenarioRunner.run(...)` 的 YAML 输入与 `ScenarioResult`。

不测 argparse 私有对象、内部锁状态、私有 parser helper；变异通过公开结果证明。

---

## 公开契约

### 1. YAML → Mapping

- CLI 与 runner 各自提供薄的安全 YAML loader；输入是显式路径，不搜索环境变量，也不跨 surface
  共享实现。
- 只允许单文档 Mapping；空文件、列表根、重复键、未知字段、不可读文件都响亮失败。
- 使用 `yaml.safe_load`，禁止任意对象构造。
- source location 保留解析前路径，manifest 的 `origin` 使用 YAML 文件父目录，从而让
  `system_prompt: prompt.md` 继续按计划 3 的规则解析。
- PyYAML 是 CLI / scenario 的轻量运行依赖；kernel 源码树不得出现 `import yaml`。

### 2. 人机工具与事件

新增两个内置工具：

```text
AskUser(question, timeout=300)
AskUserQuestion(question, options, timeout=300)
```

- 每个实例绑定一个 Runtime 与 agent id，经 Module 的 per-Agent tool factory 创建，不跨 Agent 共享。
- 先 emit 一条请求事件，再 `Runtime.wait_for(agent_id, UserInput(...))`。
- 事件 kind 固定为 `user_input_requested` / `user_question_requested`；payload 至少含 `request_id`、
  `question`，后者另含按输入顺序保存的 `options`。
- CLI 用 `Runtime.send(..., correlation_id=request_id, sender='human')` 回答。投递失败必须显示原因。
- 匹配 correlation id 之后仍要校验 `sender == 'human'` 且 `trust is TRUSTED`；不可信或伪造来源
  不能成为回答，也不能批准工具调用。
- `answered` 返回消息正文并保留 `Message.trust`；`timeout` / `preempted` / `aborted` 返回不同的
  ToolResult 文案与机器可读 meta，不伪造成回答。
- question 非空；options 是 2–10 个互异非空字符串；timeout 沿用 Waiter 的有限正数约束。
- 工具名和 schema 字段顺序纳入公开面测试。

`HumanInteractionModule` 只注入这两个工具，不声明 Flag。CLI 默认安装它；其他宿主按需安装。

### 3. 单次工具确认

新增 kernel 侧 `ConfirmationHandler` 协议与不可变 `ConfirmationRequest` / `ConfirmationOutcome`：

- request 至少含本次确认专属的不可预测 `request_id`、agent id、tool call 快照、危险 verdict 与
  执行参数快照；`request_id` 与模型给出的 `tool_call_id` 是两种身份，不得复用。
- outcome 是 `approved` / `rejected` / `timeout` / `preempted` / `aborted`，不得用 bool 抹掉原因。
- Runtime 构造参数可选注入 handler；未注入保持当前“没有答复通道，不执行”的行为。
- 默认提供 mailbox handler：发 `tool_confirmation_requested` 事件，然后用
  `Runtime.wait_for(..., Confirmation(tool_call_id=request_id))` 等答复；这里的字段承载确认请求的
  correlation id，真实 tool call id 仍保存在 `ConfirmationRequest.call.id`。只接受明确的批准 /
  拒绝词，其他内容按拒绝并留下原因。
- 匹配到的答复还必须来自 `sender='human'` 且为 `TRUSTED`；只猜中 tool call id 的 Agent 消息不能
  批准调用。
- handler 只被调用一次；policy 也只决定一次。批准不写入任何长期授权表。
- 每次 handler 调用都生成新 `request_id`；超时后的迟到答复不能命中下一次确认，即使模型复用了同一个
  tool call id。
- 拒绝、超时、抢占与中止不执行工具，结果必须区分结局；中止仍让本轮按既有 abort 语义收口。

关闭 D-38 的执行约束：

- 现有规则命中导致的调用仍按原分段保守串行。
- 即使规则返回 `NONE` 而宿主 policy 返回 `CONFIRM`，批准后的工具及其 POST hook 也必须占用独占
  执行区间；它开始时没有兄弟工具在执行，结束前兄弟工具不能进入执行区间。
- `PRE_TOOL_USE` 与确认观察者可以与兄弟重叠；它们是宿主代码，D-38 明确允许。真正的 Tool.execute
  和 POST hook 不得重叠。
- 取消等待或执行时释放独占权；不得把同一批准复用于重试、下一轮或另一 Agent。

### 4. L2 场景格式

场景 `version` 固定为 `1`，最小结构：

```yaml
version: 1
name: delegated-write
agents:
  Coordinator:
    manifest: {name: Coordinator, description: root}
    instances:
      - turns:
          - tool_calls:
              - {id: d1, name: Delegate, arguments: {agent: Worker, query: write result}}
          - {content: done}
  Worker:
    manifest: {name: Worker, description: worker, tools: [Write]}
    instances:
      - turns:
          - tool_calls:
              - {id: w1, name: Write, arguments: {path: result.txt, content: ok}}
          - {content: written}
topology:
  slots:
    - {name: root, agent: Coordinator, is_root: true, cardinality: singleton, boot: eager}
entry: {slot: root, prompt: start}
responses:
  - {kind: user_input, agent: "Worker[0]", answer: proceed}
  - {kind: confirmation, agent: "Worker[0]", answer: approve}
expect:
  final_output: {root: done}
  tool_order: {root: [Delegate], Worker: [Write]}
  statuses: {root: zombie, Worker: zombie}
  events: [agent_start, turn_start, turn_end, agent_end]
```

- tool call `arguments` 在 YAML 中是 Mapping，loader 用稳定 JSON 编码成 `ToolCall.arguments`。
- `raises` 只允许白名单异常名，不接受 import path 或任意代码。
- `responses` 按请求出现顺序匹配 `kind + 稳定 Agent selector`，从请求事件读取动态 request id 后经
  `Runtime.send` 回答；未消费项、顺序错误、未知 kind 都失败。
- runner 只依赖 kernel，在临时 `LocalWorkspace` 上装真实 local tools、真实编排工具、真实 hooks /
  broker / registry；`LocalWorkspace` 是 kernel 的默认实现，不 import 任何 backend。
- `agents.<role>.instances` 是脚本列表，按该角色的实例创建顺序逐份消费；每个实例各有独立
  ScriptedLLM。同角色创建数超过脚本数时，该次工厂调用在注册第三个实例之前响亮失败，不能从头复用
  或共享游标；此前合法实例已经产生的效果由 runner 的 `finally` 清理，不承诺整场景零副作用。
- `ScenarioResult` 保存最终输出、每 Agent 的 tool call 名顺序、最终 registry 状态、收到顺序中的事件
  与 workspace 路径快照。
- EventBus 给投递事件分配从 1 开始、严格递增的 `sequence`；所有订阅者看到同一序号。
- 期望不匹配抛场景断言错误，列出期望、实际与修复场景或实现的下一步；不得只返回 bool。

### 5. CLI

- 默认读取当前目录的 `.syndica.yaml`，也接受顶层 `--config <path>`。配置顶层只允许
  `version / agents / topologies`：agents 是清单 Mapping 列表，topologies 是名字到 `{slots: [...]}`
  的映射。没有配置时加载四个内置角色，`run` 默认直接 spawn `Orchestrator`。
- `syndica version` 只 import 顶层版本模块与 CLI router；不得 import kernel、backend 或 PyYAML。
- 顶层已知命令不走默认补全；未知首词作为 prompt 补成 `run`。首词以 `-` 开头时不猜，交给顶层
  parser 报错。
- noun 域必须带 verb；`agents --help` 等可列出该域全部 verb。
- `--flag` 先按 `key=value` 拆分，再依据 `Runtime.flag_declarations` 的精确类型解析；未知 key、
  bool 拼法、int 非法值、重复 key 均在 spawn 前报错。
- `flags list/show` 直接读同一份 `FlagDeclaration`，不得维护第二张说明表。
- `run` 与 `chat` 共用 `RunRequest` 和 `CliHost.run_request`。chat 的 `:use <agent-id>` 只改变后续
  request 的 session；每个提示符打印完整当前目标，不用颜色作为唯一提示。
- 输出最后一块有内容的 assistant block；运行失败返回非零退出码并把三件套写 stderr。
- `__main__` 仅在实际执行 run/chat/doctor 时惰性创建 OpenAI-compatible provider 与
  `LocalWorkspace`；读取配置和环境只发生在 composition root。
- console smoke test 用 ScriptedLLM 注入，不访问网络；OpenAI adapter 只验证装配字段，不发真实请求。

---

## 任务与进度

### Task 1 — 基线与事件序号

**状态：completed**（50 条事件测试；4 条真实变异；独立审查 APPROVE）

**Files:** `events.py`、`test_events.py`、`test_public_api.py`

- Event 追加 `sequence`，EventBus 在 emit 时分配单调序号；输入 Event 不得伪造已分配序号。
- 多订阅者看到同一序号；溢出后剩余序号仍单调且 dropped 可见。
- 变异：固定 sequence、每订阅者分别计数、从 0 开始、接受调用方非零值。

### Task 2 — YAML loader 与声明式 topology

**状态：completed**（135 条定向测试；8 条真实变异；独立审查修复 3 项后 APPROVE）

**Files:** `pyproject.toml`、`src/syndica/cli/config.py`、`kernel/manifest.py`、
`kernel/topology.py`、`scripts/check_deps.py`、`tests/cli/test_config.py`、`tests/test_deps.py`

- 落地 CLI 安全单 Mapping loader、manifest 加载、命名 Topology / AgentSlot Mapping 解析。
- 拒绝重复键、未知键、bool 冒充 int、非法 enum 字符串；不 normalize 名字或键。
- 依赖守卫证明 kernel 不 import yaml。
- 变异：safe_load → unsafe、重复键 last-wins、丢未知键、origin 取 cwd。

### Task 3 — AskUser / AskUserQuestion

**状态：completed**（505 条定向测试；7 条真实变异；独立审查修复 2 项后 APPROVE）

**Files:** `kernel/tools/human.py`、`kernel/modules.py`、公开导出与对应单测

- 先事件、后等待、按 correlation 回答，四态都有独立结果。
- 双 Agent 同时提问互不串答；options 复制且保序。
- 变异：共用 request id、先 wait 后 emit、丢 correlation、把 timeout 当回答、trust 洗白。

### Task 4 — 人工确认与 D-38

**状态：completed**（1040 条定向测试；9 条真实变异；独立审查修复 4 项后 APPROVE）

**Files:** `danger.py`、`tool_executor.py`、`agent.py`、`runtime.py`、对应单测

- 加 handler 契约与 mailbox 实现；Runtime 在 spawn 时为每个 executor 绑定 agent id。
- policy / handler 每调用各一次；批准、拒绝、超时、抢占、中止分别验证。
- 构造一个 `RiskLevel.NONE + policy CONFIRM + 三个 concurrency-safe 工具` 的确定性并发用例，
  证明批准工具的 execute + POST 区间与两个兄弟都不重叠。
- 变异：批准后直接执行、只等前序不挡后序、policy 调两次、授权缓存到下一次调用。

### Task 5 — L2 场景 schema 与 runner

**状态：completed**（23 条 Runner 测试；10 条真实变异；两轮审查修复 5 项后最终 APPROVE）

**Files:** `src/syndica/runner/scenario.py`、`src/syndica/runner/yaml.py`、
`tests/runner/`、YAML fixtures

- 垂直切片：单 Agent 最终输出 → 工具顺序 → 多 Agent 派活 → 状态快照 → 事件单调 → 指定失败。
- 同角色两实例使用两份不同脚本；第三次工厂调用在注册该实例之前失败，runner 随后清理既有实例。
- responses 事件泵覆盖 AskUser 与 confirmation，动态 correlation id 不写入黄金文件。
- 场景运行总在 `finally` 关闭 Runtime / Workspace；失败同样不遗留 agent/subprocess task。
- 期望值来自 fixture 字面量，不从运行结果派生。
- 变异：假 Runtime、跳过工具、按完成顺序改写工具序、排序事件、异常分支不清理。

### Task 6 — CLI 路由、参数与惰性导入

**状态：completed**（61 条 CLI / 依赖测试；6 条真实变异；独立审查修复 1 项后 APPROVE）

**Files:** `src/syndica/cli/`、`src/syndica/__main__.py`、`pyproject.toml`、CLI 单测

- 一张命令路由表，名词 + 动词，默认 run 规则唯一。
- 固定 RunRequest 字段与四类预算 / flag 形态；错误走 stderr + 非零码。
- subprocess 证明 `version` 不加载 kernel/backend/yaml；`python -m syndica` 与 console entry 同路。
- 变异：所有未知首词都报错、以 `-` 开头也补 run、version 顶层 import backend、chat 自建投递路径。

### Task 7 — CLI Runtime host 与 chat

**状态：pending**

**Files:** CLI host / terminal 模块、composition root、CLI 集成测试

- 内置目录 + 本地工具 + Safety/Human Module + 可注入模型组成一轮真实 run。
- `--agent` 与 `--topology` 路径、`--max-turns`、flags 生效；未实现预算与持久化 session 明确拒绝。
- 事件消费者处理 AskUser、AskUserQuestion、tool confirmation；EOF 按拒绝 / 中止，不永久等待。
- chat 目标切换后下一条消息投给所见目标；普通输入只走 `run_request`。
- 变异：chat 直接 `runtime.send`、隐藏当前目标、回答不带 correlation、关闭时漏 Runtime/Workspace。

### Task 8 — L2 E2E、文档与计划收口

**状态：pending**

**Files:** `tests/scenarios/*.yaml`、`tests/integration/test_cli_human_scenario.py`、README、
`deferred-items.md`、handoff、本计划

- 一条场景覆盖：CLI/YAML 装配 → 根 Agent → Delegate → Worker 写文件 → 人类问答 → 危险命令确认
  → 最终回复。
- 另一条场景稳定触发 LLM 异常与确认拒绝，证明失败路径可回放。
- 关闭 D-38，更新 README 最小命令示例与配置约束。
- 全量门禁通过后把本计划标为完成。

---

## 验证

每个 Task：

1. 写一条失败测试并记录失败原因。
2. 最小实现到绿；继续一条行为一条测试。
3. 清 `__pycache__` 后做至少四条真实变异，逐条确认目标测试变红。
4. 还原后按 sha256 校验本轮所有变异文件，再重跑目标测试。
5. 独立 subagent 审查；修复 Important 以上问题并补回归。
6. `ruff check`、`ruff format --check`、`pyright`、依赖检查按影响范围运行。
7. 单独 commit；不 push。

计划 6 最终：

```bash
.venv/bin/python -m pytest -q tests/kernel tests/runner tests/cli tests/integration
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pyright
.venv/bin/python scripts/check_deps.py
.venv/bin/python -m syndica version
```

本计划是 CLI / Python workflow，不存在浏览器 URL 或页面断言。`harness-e2e-test` 的浏览器 contract
不适用；E2E 证据是子进程 CLI smoke + YAML 驱动的真 Runtime L2 场景，不能把单元测试冒充 E2E。

---

## 风险、回滚与恢复

- **确认死锁：** CLI 必须先订阅事件再 spawn；EOF 必须发送拒绝或中止，不能让 wait_for 跑满。
- **批准与兄弟重叠：** 用可观测执行区间测试，不依赖 `asyncio.sleep` 的概率时序。
- **surface 越层：** `scripts/check_deps.py` 必须继续抓 CLI import backend；composition root 是唯一
  同时看见两侧的地方。
- **YAML 隐式类型：** bool/int 轴由 Mapping parser 再校验，不能把 PyYAML 的结果直接塞进构造器。
- **场景 DSL 过早膨胀：** 只实现本计划列出的字段；trace 捕获格式等里程碑 3 到来时再向后兼容追加。
- **会话误承诺：** 不落盘、不生成“看似可恢复”的 id；显式错误说明依赖 SessionStore。

恢复点：每个 Task 一个 commit；若中断，从本节第一个 `pending` Task 继续。不得 reset 或覆盖用户改动。

---

## 决策日志

| 日期 | 决策 | 理由 |
| --- | --- | --- |
| 2026-09-23 | CLI 暂不伪造跨进程 session | SessionStore 属里程碑 6；内存 id 在下一进程失效却看似成功更糟 |
| 2026-09-23 | PyYAML 只进非 kernel 层 | 兑现 YAML → Mapping，同时保持 kernel 依赖边界 |
| 2026-09-23 | CLI 与 runner 各自持有薄 YAML adapter | surface 互不依赖；几行解析胶水不值得发明第三个共享层 |
| 2026-09-23 | 确认使用 handler + mailbox，而非让观察 hook 回值 | `TOOL_CONFIRMATION_REQUIRED` 明确是可观察切点，返回值必须被丢弃 |
| 2026-09-23 | 获批调用使用独占执行区间 | 关闭 D-38，且不需要让有副作用的 policy 决定两次 |
| 2026-09-23 | L2 为 package 能力，不把 fixture helper 当实现 | 生产 trace 最终要与它共享格式，临时测试函数无法承接该契约 |
| 2026-09-23 | 确认 request id 与 tool call id 分离 | tool call id 可能复用或被猜到，迟到答复不得批准下一次调用 |
| 2026-09-23 | 人工答复同时校验 correlation、sender 与 trust | 只猜中关联 ID 的 Agent 消息不能成为人类回答 |

## 意外发现

- Task 2：`scripts/check_deps.py` 原先只校验 Syndica 组件间依赖，不能发现 kernel 新增 PyYAML；
  已增加外部包守卫，并用一正一反两个 fixture 证明只禁 kernel、不禁 CLI。
- Task 3：反射 schema 只接受基础 `list`，不能表达 `Sequence[str]` 的元素类型；`AskUserQuestion`
  因此使用手写 schema，运行时仍保留更精确的 Sequence 校验。
- Task 4：可观察 hook 虽然丢弃返回值，仍能保留收到的可变对象并延迟修改；确认切点因此必须同时
  复制最终参数与 ToolCall，批准后再从不可变 JSON 快照重建执行参数。
- Task 4：writer-priority 执行门在 writer 等待期间被取消时必须 `notify_all`；否则已经在等的
  reader 没有后续状态变化可唤醒，会永久停住。
- Task 5：`expect` 字段缺省表示不检查，但显式 `{}` / `[]` 必须断言结果为空；只看 truthiness
  会让一份写明“什么都不应发生”的黄金文件假绿。
- Task 5：场景目录包含被用户角色覆盖后的全部内置角色；冻结目录时模型占位必须覆盖整个 catalog，
  不能只按实际运行角色取模型名，否则未参与回放的内置角色会阻断非默认模型场景。
- Task 5：EventBus 的有界订阅会在单轮突发超过 1024 条时丢最旧事件；L2 runner 必须同时检查
  `subscription.dropped` 与从 1 连续的 sequence，不能把截断后的有序后缀当成完整轨迹。
- Task 5：真实 Bash 可以生成二进制文件，而 L2 v1 的 workspace 契约是文本 Mapping；快照遇到
  非 UTF-8 文件时必须给出带相对路径的 `ScenarioMismatch`，不能泄漏裸解码异常。
- Task 6：argparse 默认把 help / usage 写到进程全局流，注入 `CliIO` 本身不够；解析必须在重定向
  上下文中运行，否则嵌入式宿主和测试都拿不到完整输出。
- Task 6：Runtime host 可能抛不带三件套的普通异常；CLI 进程边界必须保留异常类型与消息并补齐
  expected / received / next_action，不能把 traceback 直接交给终端用户。

## 文档更新清单

- `README.md`：安装、最小 run、配置路径、当前 session / budget 边界。
- `docs/decisions/2026-09-14-deferred-items.md`：关闭 D-38。
- `docs/plans/2026-09-21-session-handoff.md`：计划 6 收口证据与 M1 最终审计入口。
- 本文件：逐 Task 状态、提交、变异与审查结论。

## 完成复盘

- 待 Task 8 补。
