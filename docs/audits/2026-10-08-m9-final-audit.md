# 里程碑 9 收口审计：产品面

日期：2026-10-08
范围：[M9 计划](../plans/2026-10-06-m9-product-surface.md) 的阶段零至六（组合根与内核事件、服务核心、双向通道、读模型与
产物遗留、前端、其余遗留、收口），加上路线图遗留表与 M7 / M8 收口审计里标 M9 的条目
基线：阶段零 `59e8c56`、阶段零至一 `8a5ce8e`、阶段二至四 `756054d`、阶段五与阶段零至一评审处置 `03ec561`；收口评审的处置
与本文同一个提交（计划 P5）
验证方式：本地验证协议（Actions 额度用完期间，见路线图「当前主线约束」）

## 结论

路线图 M9 的完成判据——**`syndica up` 起服务，浏览器建会话、发任务、看 Agent 树与事件流，处理工具确认，预览产物与 diff 并
提反馈，断线重连不丢事件、刷新后恢复，人的终端经 WebSocket，窄屏可用，UI 时间线与 trace 一致，无 key 时 doctor 给三件套**——
由 Playwright 的桌面与移动两套视口（28 / 28，真的 `syndica up` 对脚本化的 OpenAI 兼容端点，不花钱）、server 的单测与组合层
用例（真的会话装配、脚本化模型），以及一次经 UI 驱动的免费模型真实运行共同证明。

- 服务面：`syndica.server` 是新 surface（Starlette + uvicorn + websockets，`syndica[server]` extra），组合根
  `syndica/serve.py` 与 CLI 共用 `assemble_session`，安全件（信任、环境白名单、开发模式的拦截、脱敏、会话锁）只有一处。
  只在 `127.0.0.1` 监听，`Host` / `Origin` 精确比对，严格 CSP，不发 cookie；一次性登录码只打印在人的终端，令牌只在
  server 的内存里。
- 出口：REST、SSE、WebSocket 的每个字符串叶子先脱敏再截断（`server/outbound`），写入点守卫认得 Starlette 的响应与
  WebSocket 发送。产物原文一律按沙箱化的附件交出，不在 server 的源上执行。
- 流：事件 id 是 `<epoch>:<seq>`，`Last-Event-ID` 续读，读者掉出环形缓冲或内核总线丢过事件时发一条没有 id 的 `reset`；
  待答表随 `human_request_resolved` 了结，等待被取消时同样发了结事件；丢过事件就按内核此刻的等待状态对一遍待答表。
- 人的通道：只答挂着的请求（409），答复以人的身份、可信、不激活投递；没有已认证的客户端连着超过 30 秒即无人值守，挂着
  的确认按拒绝、提问中止；server 收到退出信号先结束事件流，会话并发收尾、存盘。
- 前端：React + TypeScript，产物入库、随 wheel 发布；类型由 OpenAPI 快照生成，`build-info.json` 记输入与产物的哈希，
  Python 用例不用 Node 就能判出过期。不可信内容一律按文本渲染，`unverified` 有自己的标签与色调。
- 阶段五：截止时刻打断在飞的模型调用（路由器到点取消、不换平台、按估算的输入计费，收尾轮 30 秒宽限，带超时的工具与人的
  等待、派活等待按剩余时间收紧）；预算跨会话（上限 + 已用存进会话记录，没给新上限就沿用存的）；`--verdict-exit` 与
  交接文件的 `acceptance_verdict`；隔离拷贝挪进 Runtime 自有的线程池；宿主追加预算的 trace 事件挂到下一个 agent span；
  `Deliver.note` 进 trace 与事件、处理器注册表一个会话一份；子树持久化改派到 v1 之后。

## 真实运行

Gemini `gemini-3.5-flash-lite` 免费档，2026-10-08，仓库外的临时工作区 `/tmp/m9real/ws`（`modules: [acceptance]`），信任
存储与 server 状态都在工作区之外；key 只在子壳里 source，壳外 `env | grep -c GEMINI_API_KEY` 全程为 0。经 UI 驱动（浏览器
自动化，点按钮、填输入框），截图留在 `/tmp/m9real/`。

- **建会话、发任务、交付**：Worker 会话里让它写 `planets.md`（四颗内行星）、`Deliver` 并写 note、再跑一条写文件的 Bash。
  时间线依次出现 `Write`、`Deliver`、`Bash`，Bash 的结果带「不可信」标签；产物面板列出 `planets.md`（交付物、1 个版本）与
  「交付说明（模型写的）：内行星清单」；结局 zombie、验收 `unverified`（`no_active_rubrics`），界面标「未验收」而不是通过。
- **越界读被拦**：同一会话接着让它 `ls -l /etc/hosts`：此前的 Bash 输出让轨迹溯源不可信，`shell.path_outside_workspace` 按
  `untrusted_high_risk` 直接拦下、不问人，三件套写明「这次阻断不会被记住」；模型如实回答读不到。
- **确认弹窗与批准**：新开一个会话（轨迹可信）让它 `ls -l /etc/hosts`：弹窗显示规则、风险 high、原因与「参数没有溯源到不可信
  内容」，点批准之后命令执行，回答「213 字节」（宿主上 `stat` 一致）。
- **trace 与时间线一致**：trace 面板对第二个会话显示「时间线里的工具调用与 trace 一致」，3 次模型调用、6903 token；
  `syndica trace show` 列出两个会话的 trace（第一个会话 7 次模型调用、2 个工作周期）。
- **不泄露、能停**：`/tmp/m9real` 下没有任何文件含 key，server 日志为空；页面开着时 `syndica down` 2 秒内优雅退出，三个会话
  都存了盘，不留进程。

## 完成判据 × 证据

| 判据（路线图 M9） | 脚本化 / 单测（零成本，随全量测试） | 浏览器 E2E（`scripts/check_e2e.sh`，桌面 + 移动） | 真实运行 |
|---|---|---|---|
| API schema 快照，破坏性变更显式记录 | `test_openapi`：快照与模型一致、当前快照摘要在 `api-changes.md` 里、文档里的路径等于 app 的路由、每种事件有 schema、M12 才冻结的字段标 provisional | — | — |
| 前端类型检查、lint、build | `scripts/check_web.sh`（类型生成比对、tsc、eslint、vite build、产物哈希）；`test_static_bundle`：产物来自当前源码与契约 | 跑的就是随包发布的那份产物 | — |
| handler 不碰内核私有字段 | `test_private_guard`（三条规则 + 范围证明） | — | — |
| SSE 序号单调、重连补发 | `test_streams`、`test_app`（epoch、补发、跨 epoch 与溢出发 reset、丢事件发一次 reset） | 断网期间的事件重连后到达 | — |
| 前端不把 `unverified` 显示成通过 | `test_unverified_has_its_own_label_and_never_the_passing_tone` | `an unverified verdict is never shown as a pass` | 界面标「未验收」 |
| server 不被 kernel import | `scripts/check_deps.py` 与 `tests/test_deps.py`（组合根集合、已知顶层模块集合） | — | — |
| `syndica up`、建会话、发任务、Agent 树与事件流 | `test_composition`（真的装配）、`test_up_down`（up / status / down 往返、不留凭证） | `a session delegates and shows the agent tree and the event timeline` | 两个会话 |
| 工具确认：批准执行、拒绝回到 Agent | `test_app`（只答挂着的、两个客户端答同一个、无人值守拒绝）、`test_composition` | 批准与拒绝各一条 | 批准一次 |
| 产物预览、diff、反馈 | `test_reads`、`test_composition` | `artifacts can be previewed, diffed and given feedback that a human activates` | 产物面板与交付说明 |
| SSE 断线重连不丢 | 同上 | `events that happen while offline arrive after reconnecting`、`after a reset the stream restarts from the refetched snapshot` | — |
| 刷新后会话与待答请求恢复 | 快照里的 `pending` | `a refresh restores the session and its pending confirmation` | 换 URL 重进会话 |
| PTY 经 WebSocket | `test_composition`（人的终端拿不到宿主环境变量、关闭后进程不在；隔离副本里开的终端镜像得到）、`test_reads` | `the human terminal works over a websocket`、`an agent's terminal is mirrored read-only and a background command is not listed as one` | — |
| 移动端视口 | — | 全部 14 条在 390×844 上再跑一遍，加无横向滚动、输入与按钮可点 | — |
| UI 时间线与 trace 一致 | `kernel/trace_analysis` 的用例 | `the timeline and the trace list the same tool calls` | 「时间线里的工具调用与 trace 一致」 |
| 无 key 时 doctor 三件套 | `test_composition`（模型建不出来、配置改过 / 改坏） | `without an API key the page explains what to do instead of going blank` | — |
| 阶段五：截止打断、预算跨会话、裁决退出码、线程拷贝、追加事件、交付说明 | `tests/kernel/test_m9_phase5.py`、`test_isolation`、`test_slot_isolation`、`test_budget_review`、`test_session_serde`、`test_acceptance_wiring`、`test_handoff_run` | — | 交付说明 |

## 计划之外的偏差

- **`pty_created` / `pty_exited` 没加**（P2）：Agent 的终端只由 Terminal 工具建，句柄在结果块的 `tool_meta` 里，
  `message_appended` 已经带着。
- **播种的轨迹只发一条 `transcript_reset`**（阶段零至一评审 M-7）：逐块重发会在 Fork 或恢复长轨迹时一口气挤爆每个订阅者的
  缓冲；客户端照它从快照重取。
- **`Event.time_ns` 由总线按墙钟打**（P2）：Runtime 没有可注入的时钟；E2E 的「时间线与 trace 一致」按 id 比，不按时刻。
- **trace 的 JSON 分析是内核纯函数，CLI 的文字视图没有改写成复用它**（P3）。
- **`budget.granted` 在会话关闭前没跑过周期时只记 WARNING**（P4）：trace 只在开着的 span 上收事件，造一个周期 span 会让
  `trace show` 多数一个周期；追加后的上限随会话存盘。
- **`Deliver.note` 不进产物索引**（P4）：索引的写盘以「没有自由文本」为脱敏证明；note 进 trace 与事件，服务面按产物缓存
  最近一次，server 重启之后只剩 trace 里那一份。
- **人的等待没有另加宿主上限**（P4，评审 L-2 的后一半）：没人连着时 30 秒后一律拒绝，有人连着时等多久由人决定。
- **截止上限只对经路由器的 LLM 生效**（P4）：内核不在 `llm.create` 外包超时（`LLM.create` 的文档）；CLI 与服务面的模型
  都经路由器。
- **Agent 终端镜像的路径改了**（P5，破坏）：`agent-pty/{handle}` → `agent-pty/{agent_id}/{handle}`。只凭句柄，宿主找不到
  隔离副本里的终端；旧路径从没发布过。
- **恢复会话的墙钟要每次重给**（P5）：墙钟不存进会话（P4），服务面的 `resume` 于是收一个可选的 `budget`；会话已经开着时
  带预算返回 409。
- **人的等待与派活等待被截止时刻收紧时，结果仍是普通的「超时」**（P5，评审 L-3 的后一半）：计划只要求带超时的工具用预算的
  措辞；截止之后不会再有新的一轮，模型据此改不了什么。

## 公开契约核对

- HTTP：`docs/reference/openapi.json` 四次快照，每次在 `api-changes.md` 记一条（`99c4b2720a3e` 首版、`bc7abfde2e3a` 产物与
  终端、`2bffe79d1f69` WebSocket 的拒绝帧、交付说明、预算请求体严格类型、描述不带版本号、`a623629bccf3` Agent 终端镜像按
  Agent 寻址与恢复会话可带预算）；只有最后一次的镜像路径是破坏性的，旧路径从没发布过。由 M12 才公开的内核类型序列化来的
  字段标 `x-syndica-stability: provisional`。
- `WorkspaceRouter.pty_capture_of(agent_id, handle, *, since, wait)`：追加的公开方法，宿主读某个 Agent 开的终端。
- 内核事件：新种类 `message_appended`、`transcript_reset`、`tool_call_started` / `tool_call_finished`、`agent_spawned` /
  `agent_removed`、`agent_status`、`human_request_resolved`、`budget_changed`、`artifact_registered`（必有键 `ref`、`note`）、
  `progress_measured`；`agent_error` 带三件套，`tool_confirmation_requested` 带 `untrusted`。场景格式的 `expect.events` 只比对
  v1–v4 定义时的 13 种（`SCENARIO_EVENT_KINDS`）。
- `kernel.__all__` 新增 `SessionBudget`；`SessionRecord` 追加字段 `budget`（缺省 None，旧记录读成 None）。
- `local_tools(..., artifact_kinds=)`、`Deliver(..., kinds=)`：追加的仅关键字参数；`ArtifactHandlers.kinds()`（私有类型）。
- `LocalIsolator.prepare` / `commit` / `begin` / `supersede` / `drop`，`WorkspaceRouter.stage_isolation` / `finish_isolation` /
  `abandon_staged`：`isolate` 照旧可用（同步，CLI 之外的宿主仍可直接调）。
- trace 属性：`budget.granted_at_ms`（int）、`artifact.note`（自由文本，照常过脱敏）；事件 `artifact.delivered`。
- CLI：`up` / `status` / `down`，`run --verdict-exit`；交接文件 schema `syndica.handoff/2`（加 `acceptance_verdict`）。
- 发行：`syndica[server]` extra，前端产物在 wheel 里（大小有用例守着）。

## 遗留

| 条目 | 归属 | 依据 |
|---|---|---|
| 多 Agent 子树的持久化与恢复、跨进程租约 | v1 之后（多进程宿主） | M9 计划阶段五；用户可见的后果写进 README 与路线图 |
| 拆成独立发行的 `syndica-server` | M12 | 配置包发布那一条（计划 P1 M14）；M9 以 extra 交付 |
| 远程访问、多用户、多进程 / 多 worker 的 server | 不做（v1） | 远程明文就是交出 shell 与人的批准通道；SSH 端口转发给了加密与认证 |
| Agent 终端的写入面 | 不做 | 第二个写入方绕过「半行不发」，危险判定看不到整行 |
| Web 上的「信任这份配置」按钮 | 不做 | 信任是人在终端里看过配置之后的动作 |
| 装第三方产物处理器的 CLI 入口 | M12 | 注册表与 `Deliver` 已经认它们声明的类别；入口随 Module 的扩展点定 |
| 服务面里的交付说明跨 server 重启 | 后续 | 只在 trace 里；要持久化就得给产物索引一个经脱敏的写入面 |
| 页面的令牌在 `localStorage` 里（浏览器配置目录，同用户可读） | 接受 | 与硬约束 4 同一档：挡失误不挡蓄意；被 Service Worker 接管的页面拒绝登录 |
| 回放文档 v4（伴生 / aux / 门 / 后台槽位） | 后续 | 与 M7 / M8 相同 |

## 评审与变异

| 评审 | 结论 | 处置 |
|---|---|---|
| 计划评审 | 3 Blocking / 10 High / 23 Medium / 9 Low | 全部折进计划（P1） |
| 阶段零至一 | 0 Blocking / 2 High / 8 Medium / 17 Low | 一部分随 `756054d` 的工作区改动，其余 `03ec561`（P4）：服务面会话各取一份文件污点基线（H-1）、页面开着时 `down` 挂满被强杀（H-2）、未认证的 WebSocket 占满名额（M-1）、无人值守答复已了结的请求（M-2）、`/health` 慢就删掉在跑的 server 的状态文件（M-3）、取消的等待不发了结事件（M-4）、播种轨迹挤爆订阅者（M-7）、四处测不出缺陷的用例（M-5 / M-6 / M-8 / L-10）等 |
| 评审之外 | 1 条 | `03ec561`：会话记录里 `tool_meta` 不脱敏，`Bash` 的整段输出原样落盘（M6 起，硬约束 8） |
| 收口（阶段二至五） | 1 Blocking / 1 High / 5 Medium / 4 Low | 全部处置（P5）：server 子进程会导入 Agent 写进工作区根的模块（B-1，加 `-P`）；截止上限不罩换平台与退避（H-1）；会话记录存池的用量（M-1）；`--verdict-exit` 与 `--session` 一起时被静默丢掉（M-2）；事件循环先没了时取消的拷贝留下临时目录（M-3）；页内 reset 之后快照覆盖掉它之后到的事件（M-4）；13 条测不出缺陷的用例（M-5）；二进制帧拆连接（L-1）；隔离副本里的终端镜像不到（L-2，路径加 `agent_id`）；没起过的命令说「可能做了一部分」（L-3）；恢复会话给不了墙钟（L-4） |

**变异的诚实交代。** 每轮都按「整个 `src` 树 sha256 先校验 → 施加 → 清字节码 → 跑定向用例 → 还原 → 再校验」：阶段零至一
24 条（评审施加，12 条存活，处置后补了用例）、阶段二至四 21 条（19 条直接变红，2 条补了用例）、阶段五 13 条、阶段零至一评审处置
15 条，处置后全部变红。一条起初存活：`tool_meta` 里的标识原样留的那一半，测试用的脱敏器根本认不出 `'a' * 32`，原样留与脱敏
是同一个结果；给用例加了认得十六进制的用户规则之后变红。变异还逼出过一个测试设计缺陷：两条用例在被测的判定失效时会起一个
真的 server 进程（继承真实的 HOME、写进用户的配置目录），失败了也不收；现在一条把 `_spawn` 换成响亮失败，另一条无论断言
如何都杀掉子进程。

收口评审自己在导出副本上施加了 42 条变异，16 条存活（3 条等价：M10、M12、S08），这正是作者的变异集偏向自己当时在想的路径
的又一例：阶段五的截止收紧只在手绑墙钟的 `Bash` 上测过，周期里根本不绑墙钟、人的等待与派活等待不收紧，整套 1711 条照绿。
处置之后本轮 27 条（每条发现的修复至少一条，含前端 2 条要重新构建产物才看得见的）全部变红，包括评审那 13 条真缺口。一条
前端用例起初是为错的理由变红：它等「两个 assistant 块」，而第一条消息之后就已经有两个，标签数的断言跑在终端开出来之前；
改成等两条「完成」之后，变异按「标签多了一个」变红。没有单独施加即变红的变异的两处：`sse.ts` 停下之后不再交出同一块里
剩下的帧，`serve` 调 `_blocking_executor()` 的那一行。「零存活」只在给定的那组变异下成立。

## 验证

```text
uv run ruff check .                                          通过
uv run ruff format --check .                                 通过
uv run pyright                                               0 errors
uv run python scripts/check_deps.py                          通过
uv run python scripts/check_links.py                         通过
sh scripts/check_web.sh                                      通过
sh scripts/check_e2e.sh                                      28 / 28
uv run pytest -q                                             全部通过（数目见收口提交的说明）
```

推送前对提交的树做导出验证（把这次提交单独检出到临时目录、在那里跑上面每一道门禁）。
