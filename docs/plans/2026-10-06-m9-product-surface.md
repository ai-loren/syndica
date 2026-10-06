# 里程碑 9：产品面（Product Surface）

> **执行者须知：本文件是需求，不是参考。** 「公开契约」一节里的 HTTP 路由与模型、事件信封与新事件种类、SSE 与
> WebSocket 的消息类型、CLI 参数，一旦发布就是兼容承诺；执行中不得为了让测试变绿临时改名或缩小断言范围。
> **形状按阶段冻结**：各公开类型在其所属阶段结束、契约与用例落地时才算冻结。由 M12 才公开的内核类型序列化出来的
> HTTP 模型标 `x-syndica-stability: provisional`，M12 仍可改（P1 H2）。

**状态：计划已评审（P1），执行中。** 用户 2026-10-06 下达 `/goal 把 M9 都做完`，沿用 M4–M8 的节奏。CI 仍推迟到
M1–M11 都做完之后，只按本地验证协议验证。

**Goal:** `syndica up` 起一个只在本机监听的服务，浏览器里能建会话、发任务、看 Agent 树与事件流、处理工具确认与
AskUser、预览产物与 diff 并提反馈、看 trace / 预算 / 进度，窄屏可用；断线重连与刷新不丢事件；PTY 经 WebSocket 交互；
没有 API key 时给三件套错误而不是白屏。前端构建产物随 Python 包发布，用户不需要 Node。并兑现标给 M9 的遗留。

**Architecture:** 新 surface `syndica.server`（Starlette + uvicorn + websockets，可选依赖 `syndica[server]`），只依赖
kernel。宿主行为（`.syndica.yaml`、信任、Agent 环境白名单、脱敏、开发模式安全模块与危险判定表、会话锁、trace 录制、
一条消息的投递）今天在 `syndica.cli` 里：先把 CLI 组装与驱动一次会话的那段从终端呈现里拆成可复用的异步装配
（`cli/assembly.py`），CLI 自己改用它；再由**组合根**（`syndica/__main__.py` 与同级的 `syndica/serve.py`）把它注入
server——server 只认它自己定义的 Protocol。两个 surface 的安全关键件是同一份代码，分层规则（surface 之间互不依赖）照旧
由 `check_deps` 机器校验，组合根自己也进校验（P1 M1）。事件流以内核为准（spec §9.1）：UI 需要而总线上没有的作为内核
事件加进去，SSE 只是出口。每个会话一个 Runtime（与 CLI 的 chat 相同），server 在其上合出全局流。

**Tech Stack:** Python 侧新增 `starlette`、`uvicorn`、`websockets`，只进 `server` extra；kernel 与 `pip install syndica`
的依赖不变。拆成独立发行的 `syndica-server`（spec §3.3）是 M12「配置包发布」那一条的事，M9 先以 extra 交付（P1 M14）。
OpenAPI 由 pydantic 模型（kernel 已有的依赖）生成，不引 OpenAPI 框架。前端 `web/`：React + TypeScript + Vite、
`@xterm/xterm`、`openapi-typescript`、ESLint、`@playwright/test`（本机缓存里已有 Chromium）——全部只在开发期；构建产物
提交进 `src/syndica/server/static/`，`build-info.json` 记输入哈希与产物哈希，Python 用例不用 Node 就能判出过期（P1 H8）。

**依据：** spec §3（分层、发布形态）、§4.2（Runtime API）、§5.6（工具、背景化）、§8.4（预算）、§9.1–§9.2、§9.4、§11、
§12（安全）、§15 第 9 行；路线图「M9 产品面」一节与遗留表里标 M9 的条目；M7 / M8 收口审计；宿主代码梳理（按 `802cfb8`
写，要点并进本文「现状」）；计划评审报告（P1）；`AGENTS.md`。

---

## 现状（决定设计的事实）

1. **可复用的宿主逻辑全在 `syndica.cli`**（config / trust / modules / environment / redaction / policy / dev_module /
   session_lock / recording / capture / 投递）；YAML 进不了 kernel（`check_deps` 禁 `yaml`）。
2. **人的答复协议**：经 `Runtime.send`，`correlation_id=request_id`（随机 nonce），`sender='human'`、`trust=TRUSTED`、
   `activate=False`；只有文本 `approve` 算批准；AskUserQuestion 要逐字等于某个选项。**没人等的答复照常作为一条可信的
   人类消息进下一轮**（`runtime.py` 的 `wait_for` 语义）。预算的 `ask_human` 不发事件。
3. **事件很薄**：`Event(kind, agent_id, payload, sequence)`，没有时间戳；序号每个 Runtime 从 1 起；总线不留历史、满了
   按订阅者丢最旧并只记 `dropped`；13 种。消息、工具调用、派生 / 回收、状态变化、PTY、请求的了结不在总线上；
   `agent_error` 的 payload 是空的（CLI 靠临时挂进全局 logger 才知道错因）。
4. **CLI 读预算用私有 `runtime._budget_of`**；会话锁用私有 `_valid_sid`；长程进度没有公开读法。spec §4.2 把 Runtime 面
   收紧在 `_RUNTIME_SURFACE`，M8 P1 H5 决定预算读写入口私有、路线图把它们的公开排给 M12。
5. **会话**：`.syndica/sessions/<sid>/`，每个周期收尾与关机时存盘；CLI 持 `flock`（`<sid>.lock`）。只持久化根对话。
6. **配置信任**：工作区里的 `.syndica.yaml` 每条命令重读、重验摘要；不受信任时只在 TTY 上问人。被信任的工作区内
   `impl:` / Module 源码由进程级的 `PinnedImports` 钉住（一个进程一份，换配置就换钉）。
7. **人接不进 Agent 的终端**：句柄只出现在工具结果的 `meta`；第二个写入方会绕过 Terminal 工具「半行不发」的不变式。
8. **进程级状态**：CLI 每条命令临时往根 logger 挂 handler、装 `DaemonExecutor`；`FileTaint` 每个 Runtime 一份、打开时
   取基线。
9. **doctor 不测 key**；缺 key 时建模型客户端就抛 `ConfigError`，没有部分报告。

---

## 硬约束（沿用 + M9 特有，违反即打回）

1. **分层**：`syndica.server` 只 import `syndica.kernel`；宿主装配经组合根注入。`check_deps` 声明组合根集合
   `{__main__, serve}`：任何组件 import 组合根都是违规；顶层模块必须属于已知集合，新的未分类顶层模块响亮失败。
2. **只监听本机**：只绑 `127.0.0.1`，不提供 `--host` / `--allow-remote`。远程用 SSH 端口转发（文档写明）。
3. **凭证不落盘、不用 cookie**：`up` 生成一次性登录码（`secrets.token_urlsafe(32)`，120 秒、只能换一次），只打印在
   `up` 自己的终端（URL 片段 `#login=`，不进服务端日志与 Referer）；浏览器 `POST /api/v1/login` 换到会话令牌（只在
   server 内存里），存进**按源隔离**的 `localStorage`、经 `Authorization: Bearer` 发送；SSE 用 `fetch` 流读（自己带头与
   `Last-Event-ID`），WebSocket 的第一帧认证（5 秒内不认证就关）。server 从不发 `Set-Cookie`（用例钉住）——回环上的
   cookie 不按端口隔离，Agent 起的预览服务会收到它（P1 B2）。比较用 `hmac.compare_digest`。
4. **Agent 拿不到人的身份**：令牌与登录码都不进工作区、不进任何文件；server 的状态文件（pid、端口、进程 nonce，无凭证）
   放在信任存储所在目录的 `servers/` 下、必须在工作区之外；`up` / `down` 在 Agent 环境里（显式标记）三件套拒绝；同一
   工作区已有 server 时 `up` 拒绝而不是再发登录码。`LocalWorkspace` 下这是挡失误与顺手够到的屏障，不挡蓄意的同用户
   进程（与 git 写拦截同一性质，文档写明）；真正的边界是沙箱化的工作区后端。
5. **DNS rebinding / 跨站 / 嵌套**：`Host` 只收 `127.0.0.1:<port>` / `localhost:<port>`；带 `Origin` 的请求与 WS 握手要求
   与服务同源；所有 HTML 响应带严格 CSP（`default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none';
   frame-ancestors 'none'; form-action 'none'`，`style-src` 另放行 `'unsafe-inline'` 供终端组件注入样式）与
   `X-Frame-Options: DENY`。拒绝都给三件套（JSON）。
6. **不可信字节不在 server 源上执行**（P1 B3）：产物原始内容只以 `Content-Disposition: attachment`、
   `Content-Security-Policy: sandbox; default-src 'none'`、`X-Content-Type-Options: nosniff` 交出，且要认证头（导航打不开）；
   前端把消息、工具输出、问题、diff、预览一律当**文本**渲染（不渲染 HTML / Markdown 原文，不用 `dangerouslySetInnerHTML`）；
   图片经带认证的 `fetch` 取成 blob 再显示；终端不装链接与剪贴板扩展。
7. **人的答复只经已认证的通道、只答挂着的请求**：WebSocket RPC 的 `answer` 只接受 server 待答表里挂着的 `request_id`，
   否则 409 三件套、不 `send`；由 server 以 `sender='human'`、`trust=TRUSTED`、`activate=False` 转成 `send`。HTTP 与 WS 都不
   提供以别的 sender / trust 发消息的入口。
8. **外发脱敏走同一个出口**：REST / SSE / WS 的载荷只经 server 的一组序列化函数交出，它们先过这个会话的 `Redactor` 再截断
   （`kernel.clipping`）；`tests/test_write_points.py` 把 Starlette 响应与 WS 发送认作外发点、锚定 `server/` 的文件。两处
   带理由豁免：人的终端（人自己的 shell，只回给本机已认证的人，同终端模拟器）与产物下载（人在自己机器上取自己工作区的
   文件，同在磁盘上打开）；产物预览 / diff / 文本内容、错误体、doctor、标题、轨迹都脱敏。
9. **UI 只展示后端明确状态**：完成、裁决、进度一律来自 API 字段；裁决四值走一张穷尽的映射表，`unverified` 有自己的标签与
   样式、永不显示成通过（类型穷尽检查 + E2E 断言）。
10. **处理器不碰内核私有**：`server/**` 里除 `self` / `cls` 外不得有 `._x` 属性访问、不得 `from syndica.* import _x`、
    不得 `getattr(_, '_x')`；守卫另断言它扫过一个写死的 server 文件，并用独立的 `os.walk` 证明扫描范围没缩。
11. **会话锁共享**：server 打开 / 恢复一个会话时取与 CLI 同一把 `flock`；拿不到就三件套拒绝。
12. **配置在 `up` 时信任一次、整个进程沿用那份快照**：server 从不提示；`up` 在前台（有 TTY 时可问人）做信任检查，
    子进程再验一次摘要（不一致即拒绝启动、把三件套经握手管道交回 `up`）。运行期间不重读 `.syndica.yaml`，导入钉在进程
    启动时装一次、从不替换；配置在那之后变了，doctor 报「配置已改，重启 server 生效」（P1 H1）。
13. **无人值守默认拒绝**：没有已认证客户端连着超过宽限（30 秒，容刷新）即为无人值守：挂着的确认按拒绝答复、AskUser
    中止（同 CLI 的 EOF）、预算询问按收尾；人的等待另有宿主上限。SIGTERM / `down`：挂着的确认按拒绝、会话 checkpoint、
    锁释放、有界等待后退出。
14. **进程级状态只在 server 启动时设一次**：server 不碰 logger 的 handler / `propagate`、不按会话装 executor；事件循环装
    有界的 executor；同一工作区的 `FileTaint` 在 server 进程里所有会话共用一份（P1 M17）。
15. **前端产物随包发布且不过期**：`build-info.json` 记输入哈希（`web/src/**`、`web/index.html`、`web/package.json`、
    `web/package-lock.json`、`web/vite.config.*`、`web/tsconfig*.json`、`docs/reference/openapi.json`）与 `static/` 产物
    哈希，Python 用例两样都重算；wheel 带 `static/` 且有体积上限。缺 `server` extra 时 `syndica up` 三件套指向
    `pip install 'syndica[server]'`。
16. 面向调用方的错误三件套（HTTP 用 `{"expected","received","next_action"}` 的 JSON 体）；注释只写约束；用例名即断言。

---

## 公开契约（发布即承诺）

### 内核新增

- **Runtime 公开面不扩**（P1 H2）：`budget_of` / `grant_budget` / 轨迹与进度读法保持私有，M12 与 Goal / Rubric / Verdict
  一起定；宿主经组合根注入的装配（CLI 侧，已经在读这些私有入口）把它们交给 server 的 Protocol，server 自己一处私有
  访问都没有。`_RUNTIME_SURFACE` 与 spec §4.2 不变。
- **事件新种类**（`Event.kind`，payload 纯数据，成员表整张断言）：
  `message_appended`（`message_id`、`role`、`text`、`sender`、`trust`）、`tool_call_started`（`tool_call_id`、`name`、
  `arguments`）、`tool_call_finished`（`tool_call_id`、`outcome`、`output`、`trust`、`background_id`）、
  `agent_spawned`（`parent_id`、`name`）、`agent_status`（`status`）、`agent_removed`、`transcript_reset`
  （`reason`：`compaction` / `fork` / `resume`）、`artifact_registered`（`ref`）、`budget_changed`（`view`）、
  `human_request_resolved`（`request_id`、`outcome`）、`pty_created`（`handle`、`command`）、`pty_exited`（`handle`、
  `exit_code`）。已有的 `agent_error` 补上 `error_type`、`expected`、`received`、`next_action`（P1 H9）；
  `tool_confirmation_requested` 补 `untrusted`（这次调用的参数是否溯源到不可信内容，P1 M16）。
- `Event` 加 `time_ns`：由 Runtime 已注入的时钟打（与 trace 同源，脚本化用例确定），`compare=False`，缺省 0 = 未知。
- `SessionRecord` 的 `flags` 旁加 `budget`（上限与已用，`Decimal` 存成字符串）：`SessionStoreContract` 加两条——原样往返、
  没有 `budget` 的旧记录读成 `None`——这是一条**追加的**契约要求，写进审计的公开面变化。`valid_sid` 不进 Protocol（P1 H7）。

### server（`syndica.server`）

- 路由前缀 `/api/v1`；请求 / 响应全部是 pydantic 模型；OpenAPI 3.1 由它们生成，`docs/reference/openapi.json` 快照由用例
  守着，SSE 事件信封、每个事件种类一个 schema、WS 帧都进 `components/schemas`（`x-syndica-events` / `x-syndica-rpc`），
  另有用例断言 OpenAPI 的路径表等于 app 的路由表、每个内核事件种类都有 schema（P1 M13）。快照一变，
  `docs/reference/api-changes.md` 必须有一条带新快照 sha256 前缀的记录（用例判，P1 L3）。
- 资源：`login`、`health`（公开：进程 nonce、版本）、`doctor`、`sessions`（list / create / get / close / resume / rm）、
  `sessions/{sid}/snapshot`（树、轨迹、预算、进度、待答请求，带 `as_of`）、`sessions/{sid}/events`（SSE）、`events`
  （全局 SSE）、`sessions/{sid}/agents`、`sessions/{sid}/transcript`、`sessions/{sid}/budget`、`sessions/{sid}/progress`、
  `sessions/{sid}/pending`、`artifacts`（list / versions / content / preview / diff / feedback）、`traces`（list / get，JSON
  分析）、`flags`、`roles`、`topologies`。建会话的请求体：`role`（缺省同 chat 的 `Orchestrator`）、`title`、`budget`
  （tokens / cost / seconds / turns）——别的不收。
- SSE：事件 `id` 是 `<epoch>:<seq>`，epoch 是每个 server 进程与每个会话 Runtime 的随机串；`Last-Event-ID` 同 epoch 且在
  有界环形缓冲里就补发，否则先发一条 `reset`；server 订阅的 `dropped` 一增长也发 `reset`。快照带 `as_of`，客户端用它
  开流；客户端按 `message_id` / `tool_call_id` / `agent_id` / `request_id` 幂等合并（泵的延迟只会造成无害的重复）。全局流
  按到达 server 的顺序合并、自有序号；同时打开的流与 WS 有上限（16），超出 429 三件套（P1 M23）。
- WebSocket `/api/v1/sessions/{sid}/rpc`：第一帧 `{"op":"auth","token"}`；之后 `{"op": "send" | "answer" | "abort" |
  "grant" | "budget_answer" | "activate_rubric", "id", ...}`，逐条回 `{"id", "ok", "error"?}`。**发消息只有这一条路**
  （REST 没有 POST messages，P1 M15）；`send` 可指向会话里任何活着的 Agent（缺省根），UI 始终显示当前对象。
  `/api/v1/sessions/{sid}/pty`：人的终端（二进制帧，只在回环）；`/api/v1/sessions/{sid}/agent-pty/{handle}`：Agent
  终端的**只读镜像**（脱敏后的去转义屏幕文本，按 Agent 路由）。
- CLI：`syndica up [--port N] [--config PATH] [--foreground] [--no-open]`、`syndica down`、`syndica status`。

---

## 阶段与任务

> 每个任务：实现 + 测试同出；一次全套；阶段末导出验证再推送。每阶段一轮后台对抗评审（不挡提交，报告边写边落盘）。
> 前端或 OpenAPI 快照有改动的提交另要过 `scripts/check_web.sh`（npm ci、tsc、eslint、类型生成比对、vite build、产物哈希）。

### 阶段零：组合根进校验、内核事件、拆装配、server 骨架

- `check_deps`：组合根集合与已知顶层模块集合（P1 M1），用例：server import `syndica.serve`、kernel import
  `syndica.__main__`、未知顶层模块都被抓住。
- 内核事件：上面的新种类与 `time_ns`；发在与对应切点相同的位置；压缩、Fork 的播种与恢复发 `transcript_reset`；回收与
  `despawn` 发 `agent_removed`；确认处理器与人类工具在等待返回后发 `human_request_resolved`。用例按种类各一条，另有
  基数 ≥ 3 的多 Agent 串线用例。
- `cli/assembly.py`：先补钉住全局副作用顺序的 CLI 用例（导入钉早于 Module 导入、恢复时取锁早于 `ensure_active`、新
  chat 在 `spawn` 之后取锁），再从 `RuntimeCliHost` 拆出「装好一个会话」与「投递一条消息、等这一轮的回答」
  （`deliver(target, text) -> Turn`），CLI 改用它，CLI 用例不改即绿。
- `syndica.server` 包：Protocol（签名写在下面）、app 工厂、认证、错误体与外发序列化函数；`pyproject` 的 `server` extra；
  私有访问守卫与写入点守卫的扩展。组合根 `syndica/serve.py`。

```python
class SessionAssembly(Protocol):
    async def open(self, *, resume: str | None, options: SessionOptions) -> SessionHandle: ...
    def list_sessions(self) -> list[SessionSummary]: ...
    def remove_session(self, sid: str) -> None: ...

class SessionHandle(Protocol):          # 只暴露内核公开类型与不透明的可调用
    sid: str
    root_id: str
    events: EventBus
    async def deliver(self, target: str, text: str) -> TurnOutcome: ...
    async def answer(self, agent_id: str, request_id: str, text: str) -> DeliveryResult: ...
    async def abort(self, agent_id: str) -> None: ...
    def snapshot(self) -> SessionSnapshot: ...          # 树、轨迹、预算、进度（纯数据）
    def grant(self, agent_id: str, budget: Budget) -> BudgetView: ...
    def redact(self, text: str) -> str: ...
    def artifacts(self) -> ArtifactAccess: ...          # list / versions / content / preview / diff / feedback
    def traces(self) -> TraceAccess: ...
    def open_terminal(self) -> TerminalHandle: ...
    def agent_screen(self, agent_id: str, handle: str) -> str | None: ...
    async def close(self) -> None: ...
```

### 阶段一：服务核心

- 认证（登录码、令牌、Host / Origin、CSP、无 cookie）、sessions REST、快照、SSE（epoch、环形缓冲、`reset`、`dropped`、连接
  上限）、health、doctor（缺 key 三件套、配置漂移）、OpenAPI 生成与快照、api-changes 机制。
- `up`：前台信任检查 → 起 server 子进程（新会话组）→ 读握手行（端口 / 登录码或三件套）→ 打印登录 URL 后退出；子进程
  握手后把 stdout / stderr 换到 server 日志，持 `servers/<key>.lock` 的 `flock`（第二个 `up` 三件套拒绝）。`status`：读
  状态文件、打 `/health` 比对 nonce。`down`：比对 nonce 后 SIGTERM，有界等待，再 SIGKILL；nonce 对不上就报状态文件过期。
- 用例：拒绝面按类型族枚举（无令牌、错令牌、过期 / 用过的登录码、坏 Host、跨源、超连接上限、Agent 环境里的 `up`）；
  从不 `Set-Cookie`；SSE 序号单调、补发、跨 epoch 与溢出发 `reset`、`dropped` 增长发 `reset`；多会话（≥ 3）互不串线；
  组合层用例（经 `syndica.serve` 与真装配、脚本化模型）：锁被持有、`git commit` 被开发模式挡住、宿主密钥不在 Agent 的
  `Bash` 环境里、工具输出里的密钥在 SSE 里被脱敏、改过的 `.syndica.yaml` 不生效（P1 M11）。

### 阶段二：双向通道

- WebSocket RPC 与待答表：`send`（经 `deliver`）、`answer`（只答挂着的）、`abort`、`grant`、`budget_answer`（server 发
  `budget_question` / `budget_question_resolved`）、`activate_rubric`（人的门）。无人值守与 SIGTERM 的处置（硬约束 13）。
- PTY：人的终端（宿主起、跑在 Agent 环境里：没有宿主密钥、git / gh 配置是临时的；**不是沙箱**，文档写明）、会话关闭与
  `down` 后进程不留；宿主建的终端带属主标记，Agent 的 `Terminal` 工具寻址它时拒绝、不占 Agent 的配额。Agent 终端只读
  镜像经 `pty_created` / `pty_exited` 发现、按 Agent 路由、读的是脱敏后的屏幕文本（不给写入面——第二个写入方会绕过半行
  不变式；路线图「PTY 工具通过 WebSocket 交互」由人的终端兑现，Agent 终端收窄为只读，记进明确不做）。
- 用例：确认批准 / 拒绝 / 超时；两个客户端答同一个请求；刷新（缓冲溢出后）待答请求仍在；超时后迟到的答复被拒、什么都
  没进上下文；AskUserQuestion 的选项原文；无人值守宽限后拒绝；WS 的认证与 Origin；人的终端拿不到宿主环境变量、关闭后
  进程不在。

### 阶段三：读模型与产物遗留

- Agent 树、轨迹、预算与进度、裁决；产物 list / versions / content / preview / semantic diff / feedback（反馈带产物定位，
  落成提议的 Rubric，激活走 WS 的人的门）；trace 列表与 JSON 分析（`cli/trace_view.py` 里纯计算的部分下沉为内核纯函数
  `kernel/trace_analysis.py`，CLI 的文字与 server 的 JSON 共用）；flags / roles / topologies。
- 遗留：处理器注册新 kind（索引收 `KINDS` 加上已装处理器声明的类别——处理器是宿主装的代码，不是模型；冲突三件套点名
  双方，P1 M19）、`preview` / `semantic_diff` 的消费方（就是这里的 API）、`Deliver.note` 落进交付记录。

### 阶段四：前端

- 页面：会话列表、聊天与事件时间线、Agent 树、产物（预览、diff、反馈）、trace / 预算 / 进度面板、确认与 AskUser 对话框
  （显示规则 id、级别、原因与是否溯源到不可信内容；不可信的块在时间线上有标签）、终端；窄屏布局（单列、抽屉）。
- API 类型由 OpenAPI 生成（含事件与 RPC 帧）；SSE 流读带 `Last-Event-ID`，收到 `reset` 重取快照。
- 构建：`web/` → `src/syndica/server/static/`（提交）+ `build-info.json`；`scripts/check_web.sh`（Node 主版本钉在
  `package.json#engines` 与脚本里）。

### 阶段五：其余遗留

- **截止时刻打断正在跑的一轮**（P1 M4）：在飞的模型调用到截止时刻取消（计入输入估算），计划中的收尾轮除外（给一段
  有界的宽限）；取消是给路由器的独立信号，不触发换平台；已经带超时的工具拿 `min(自己的, 剩余)`，结果写明「到预算截止
  时刻停下，副作用未知」（与后台取消同一措辞）；人的等待与派活等待按剩余时间收紧；停止原因 BUDGET，走专门的路径而不是
  `abort`。没有超时的工具不做通用的工具内取消。
- **预算跨会话**（P1 M3）：可加的维度（tokens / cost / turns）按「上限 + 已用」存进会话记录，恢复时续上；墙钟不跨会话
  （每次打开从新的截止时刻起算）；恢复时给了新上限就替换存的上限、已用照旧；关机时在飞的估算按 M8 P7 结算。M8 钉住
  「恢复从新预算开始」的那条用例翻转，写进决策。
- **裁决的机器可读出口**（P1 M6）：CLI 加 `--verdict-exit`：`pass` 10、`partial` 11、`block` 12、`unverified` 13；
  没装验收就开它 → 退出码 2 与三件套；优先级 130 > 1 > 交接拒绝的 3 > 裁决码。交接文件加 `acceptance_verdict`（与读
  审查者文字标记的 `reviewer_verdict` 分开），交接 `SCHEMA` 加一。
- **隔离槽每周期的拷贝挪进线程**（P1 M5）：每次隔离一个代号；线程只建到带代号的临时目录，最后的 `os.replace` 回到事件
  循环、代号仍是当前的才做，否则删掉临时目录；拷贝中途消失的文件按不存在处理（不报 ERROR）；Runtime 自有有界
  executor；合并留在事件循环。
- **改派**：多 Agent 子树的持久化与恢复、跨进程租约 → v1 之后（多进程宿主）。理由：M9 的 server 是一个进程、持会话锁，
  根会话的恢复在 M6 已有；Fork / 槽 / 类式角色的子 Agent 从角色名重建不出来，邮箱只在 checkpoint 时存——要一起定的是
  多进程的租约与接管，而 v1 不引入多进程。用户可见的后果：`down` 或崩溃后，在飞的子 Agent 与它们的邮箱丢失，恢复只有
  根对话加「子 Agent 未恢复」的说明。路线图第 177 / 197 行与 spec §15 同步改。

### 阶段六：收口

- E2E（Playwright，桌面 1280×800 与移动 390×844）：测试装置在 `tests/` 里起一个脚本化的 OpenAI 兼容假端点，然后跑**真的**
  `syndica up`（`SYNDICA_LLM_ORDER=openai`、`OPENAI_BASE_URL` 指向假端点）——生产路径一行不改（P1 M10）。覆盖：建会话、
  发任务、Agent 树与事件流；确认弹窗批准 / 拒绝；产物预览 / diff / 反馈；断网后重连不丢事件；刷新后会话与待答请求恢复；
  时间线与 trace 一致（UI 里 `tool_call_id` 与派生 id 的集合与顺序等于 trace 的 TOOL_CALL span 与 SPAWN 边）；人的终端经
  WebSocket；产物里的 `<script>` 不发出任何 API 请求；`unverified` 不显示成通过；无 key 时 doctor 三件套。移动视口断言：
  无横向滚动、聊天输入与确认按钮在视口内可点、产物预览可打开。
- 免费模型真实运行（仓库外临时工作区，经 UI 驱动）。
- spec §15 / 路线图 / README；收口审计 `docs/audits/<date>-m9-final-audit.md`；收口对抗评审一轮。

### 明确不做（附理由，收尾时复核）

- 多进程 / 多 worker 的 server、远程多用户、`--allow-remote`：M9 是本机单用户产品面；远程访问明文过网就是交出一个
  shell 与人的批准通道，SSH 端口转发免费给了加密与认证。
- Agent 终端的写入：第二个写入方绕过「半行不发」，危险判定就看不见整行；人的终端是单独的 PTY。
- 子树持久化与跨进程租约：见阶段五「改派」。
- Web 上的「信任这份配置」按钮：信任是人在终端里看过配置之后的动作；UI 逐字显示 `syndica config trust --sha256 …`。
- 回放文档 v4（伴生 / aux / 门 / 后台槽位）：与 M7 / M8 相同，留到回放子系统那一次深改。

---

## 验证节奏

- 每个任务：`ruff` / `format --check` / `pyright`（按 `" error"` 计数，不按 `errors`：单个错误打印的是单数）/
  `check_deps` / `check_links` 全绿；前端或 OpenAPI 快照有改动另跑 `scripts/check_web.sh`；受影响用例先跑，再跑一次全套。
- 定向变异：认证与拒绝面、Host / Origin / CSP、无 cookie、外发序列化（绕过一个处理器）、答复协议（sender / trust /
  activate、只答挂着的）、SSE epoch / 补发 / `reset`、会话锁、人的终端的环境、私有字段守卫、产物响应头（去掉
  `sandbox`）、产物过期判定、组合根校验。
- 阶段末导出验证（`git archive` + `uv sync --all-extras` + 全门禁 + 全套 + `check_web.sh`）再推送。

---

## 决策日志

- **P0（计划，2026-10-06）** 本文件初稿。依赖按推荐（Starlette + uvicorn + websockets；React + TS + Vite，产物入库并按
  源码哈希判过期）——用户取消了问卷，未表态；kernel 与 `pip install syndica` 不受影响。宿主装配走组合根注入（丙），不建
  共享的 host 层（那是 spec §3.1 的方向改动），也不把 YAML / 开发模式安全模块下沉进内核。
- **P1（计划评审，3B / 10H / 23M / 9L，2026-10-06）** 威胁模型：被注入的 Agent 以同一 OS 用户跑不受沙箱的 `Bash`，
  能 `curl` 本机端口。报告写到 M21 中断，M22 与 L1–L9 只在覆盖矩阵里出现，按矩阵的描述处置（M23 连接上限、L3 破坏性
  变更的记录机制、L4 `unverified` 的展示机制、L7 移动视口断言）。逐条处置：
  - **B1** 令牌写在工作区里，Agent 读到就能以人的身份批准自己、开人的终端：凭证不落盘，一次性登录码只打印在 `up` 的终端，
    状态文件在工作区外且不含凭证，`up` / `down` 在 Agent 环境拒绝、同一工作区不再发码；硬约束 4 改写成它在
    `LocalWorkspace` 下真正保证的东西（挡失误，不挡蓄意）。不做控制 socket：同用户进程连得上它，与落盘的令牌是同一个洞；
    登录后令牌在 `localStorage`（同源的新标签页共用），server 重启后重新 `up` 拿码。
  - **B2** cookie 不按端口隔离：不用 cookie，令牌按源存、经头发送，SSE 用 fetch 流读，WS 第一帧认证。
  - **B3** 产物在 server 源上执行脚本即 XSS 驱动 API：硬约束 6，加 CSP、`frame-ancestors 'none'`、文本渲染、产物下载响应头。
  - **H1** 长驻 server 的配置信任：`up` 时信任一次、进程沿用快照、导入钉装一次不换，运行期间不重读。
  - **H2** 四个新的 Runtime 公开方法违反 M8 P1 H5 与路线图（M12）：取（a）——Runtime 面不扩，经组合根注入的装配交给
    server，私有访问留在 CLI 侧（今天就在那里）；HTTP 里由 M12 才公开的类型标 provisional。
  - **H3** 「span 之外记到最近一个周期的 span」会再丢一次（span 结束即落盘、追加事件被忽略）：追加先挂在预算账上，下一个
    agent span 开始时记 `budget.granted`（带真实时刻）；会话先关了就在关机时记一次；用例钉住两条消息之间追加只记一次。
  - **H4** 流一致性：epoch、快照 `as_of`、`dropped` → `reset`、`transcript_reset`、`agent_removed`、全局流的合并顺序。
  - **H5** 待答请求没有快照与了结事件，迟到 / 重复的答复成了可信消息：待答表、`human_request_resolved`、`pending` 快照、
    只答挂着的（409）；检查与 `send` 之间的残余窗口（恰在其间超时）写进文档并有用例——这条答复作为普通消息进下一轮，
    与 CLI 今天相同。
  - **H6** 外发脱敏没有结构性检查：单一序列化出口 + 写入点守卫扩展；两处豁免写明理由；Agent 终端镜像读脱敏后的整屏文本，
    不存在跨帧的半截密钥。
  - **H7** `valid_sid` 进 Protocol 是破坏性变更：不进；`budget` 追加两条契约用例。
  - **H8** 产物哈希不含 API 类型：输入清单含 OpenAPI 快照，另记产物哈希；快照改动也跑 `check_web.sh`；钉 Node 主版本。
  - **H9** 错因只经全局 logger：`agent_error` 带三件套；server 不碰 logger。
  - **H10** `--allow-remote` 明文传 shell 与批准通道：删掉，文档写 SSH 端口转发。
  - **M1** 组合根不受校验：`check_deps` 加组合根集合与已知顶层集合。**M2** Protocol 签名写进计划。**M3** 预算跨会话的墙钟、
    上限优先级、翻转的 M8 用例：见阶段五。**M4** 截止打断的内部矛盾：按工具类别写规则，收尾轮有宽限，路由器不换平台。
    **M5** 线程拷贝的取消 / 代号 / 消失文件：见阶段五。**M6** 退出码撞 3：改 10–13，字段 `acceptance_verdict`。
  - **M7** PTY 镜像的发现、隔离副本里的 Agent、Portal 协议：`pty_created` / `pty_exited`，按 Agent 路由，读屏幕文本（已有
    能力，不改 Portal），宿主终端带属主标记；只读收窄记进明确不做。**M8** 「人的终端看不到 `.syndica`」在本地工作区下做不到：
    改成如实的表述，加关闭后进程不留的用例。**M9** 无人值守其实在等：定义无人值守与宽限，SIGTERM 处置。**M10** E2E 的测试
    入口进了生产路径：改成假 OpenAI 端点 + 真 `syndica up`。**M11** 假装配测不出漏接的安全件：组合层用例。**M12** 进程级
    状态：硬约束 14、server 锁、`down` 比对 nonce、钉顺序的 CLI 用例。**M13** SSE / WS 类型在 OpenAPI 外：进 components，
    路由表相等用例。**M14** 发行形态偏离 spec §3.3：M9 先以 extra 交付，拆包是 M12「配置包发布」那一条；缺 extra 的三件套与
    wheel 内容用例。**M15** 第三份投递实现：`deliver` 进共享装配，发消息只走 WS，`send` 可选对象，建会话的请求体写明，
    Rubric 激活走人的门。**M16** UI 不显示不可信：确认事件带 `untrusted`，对话框与时间线显示。**M17** 并发会话洗白文件污点：
    同一工作区共用一份 `FileTaint`。**M18** 子树持久化改派到无名去处：写明去处（v1 之后）、用户可见后果与同步改的行。
    **M19** 新 kind 与闭集 `KINDS`：索引收 `KINDS` 加已装处理器声明的类别。**M20** 私有字段守卫写法 AST 做不到：改成三条
    规则 + 范围证明。**M21** `time_ns` 的时钟与「时间线与 trace 一致」的比较：Runtime 的时钟、`compare=False`、比较对象写明。
