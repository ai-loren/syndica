# 里程碑 10 收口审计：互操作

日期：2026-10-09
范围：[M10 计划](../plans/2026-10-09-m10-interop.md) 的阶段零至七
验证方式：本地验证协议（Actions 额度用完期间，见路线图「当前主线约束」）。实现在提交 `b1ec080` 里，评审处置在其后的
几个提交里；`86e235b` 推送之后又做了一轮逐条复核，最终代码提交 `1de9756` 过了导出验证（见文末）。

## 结论

四个协议都映射到已有的 Syndica 会话，不另写一套 Runtime。被调用的适配在 `syndica.server`，调用外部的三个 backend 互不依赖，由组合根注入。外部内容一律不可信。协议错误是三件套，不回显异常原文。密钥只从工作区之外的用户级 `interop.yaml` 来。

回归证据是官方 SDK 对真进程或真 HTTP 服务的脚本化往返，不花钱。真实客户端另外手工跑过：MCP Inspector 的 CLI 模式对 `syndica mcp` 与 `syndica up` 的 `/mcp`；Gemini CLI 0.63 作为托管的 ACP Agent，用免费 Gemini key 做了一次读写（批准时写入、拒绝时不写）；`openai` SDK 对背后是免费 Gemini 的 `syndica up --api-token`。三处都查过 key 零泄露、外部进程不留。

## 完成判据 × 证据

| 判据 | 证据 |
|---|---|
| MCP server 缺省只读，`tools/call` 经 `call_external`，产物与提示脱敏，每个处理器的异常都不回显 | `tests/server/test_mcp_serve.py`，黄金文件 `tests/server/testdata/mcp_serve_wire.jsonl`（一次脚本化 stdio 会话的线上 JSON 行） |
| `/mcp` Bearer、Host、未知版本（initialize 正文与每个 POST 的 `MCP-Protocol-Version` 头）在交给 SDK 之前以 -32022 拒绝 | `tests/server/test_mcp_http.py` |
| 真的 `syndica mcp` 子进程：只有 Read / Grep / Glob，读到的环境密钥被换掉，日志在工作区外 | `tests/server/test_mcp_stdio.py` |
| MCP Inspector CLI 对 `syndica mcp` 与 `/mcp` | 手工：stdio 的 `tools/list` 只有那三个只读工具；`Read` 读回文件，结果带 `trust: untrusted`。HTTP 同样：`http://127.0.0.1:<port>/mcp` 加 Bearer，`tools/list` 没有 Write，`Read` 读回标记且不含探针密钥，日志里也没有令牌。命令必须写在 Inspector 选项前面，且不能用 `python -m` |
| MCP client：版本只收 SDK 的五版，结果不可信，`$ref` 内联，分页取全（真 SDK 服务），只连角色用到的服务，断连不自动重连 | `tests/backends/test_mcp_client.py`、`tests/cli/test_mcp_session.py`，黄金文件 `tests/backends/testdata/mcp_client_wire.jsonl` |
| 外部进程不留：会话关闭、Runtime 关机、chat、run、Ctrl-C、`down`，含 setsid 出去的孙进程 | `tests/cli/test_mcp_lifecycle.py`、`tests/server/test_up_down.py`、`tests/backends/test_mcp_stdio_descendants.py`、`tests/backends/test_acp_descendants.py` |
| ACP agent：版本 1、非文本拒绝、确认、提问、取消、工具输出（脱敏后截断）、停止原因映射、`session/load`（含跨进程）、stdin 断开时挂着的确认按拒绝、配置没被信任时在 `session/new` 拒绝 | `tests/server/test_acp_agent.py`、`tests/server/test_acp_turn.py`、`tests/server/test_acp_bridge.py`、`tests/server/test_acp_stdio_lifecycle.py`，黄金文件 `tests/server/testdata/acp_agent.jsonl`（真子进程，两个方向的原始字节） |
| 真的 `syndica acp` 子进程，模型是本地假端点 | `tests/server/test_acp_stdio.py`：版本 0 拒绝后再用 1，图片块拒绝，提问必须答选项，确认先拒绝再批准，批准后 `exit_code: 0`。假 key 不进回答和日志 |
| ACP client：读写与终端经 `call_external`，外部写入读回不可信，高风险命令不执行，被拒与没做成都回三件套 RequestError，`outputByteLimit` 留结尾，kill / release / wait_for_exit，崩溃不回显、不重连，后代（含 setsid）被收掉 | `tests/backends/test_acp_model.py`、`tests/backends/test_acp_callbacks.py`、`tests/backends/test_acp_environment.py`、`tests/backends/test_acp_descendants.py`，活动黄金文件 `tests/backends/testdata/acp_activity.json` |
| A2A server：卡片、1.0 与 0.3（SDK 自己的 0.3 传输层）、缺版本头 -32009、异常不回显、提问、确认有人等人批准 / 没人按拒绝、`contextId` 就是会话 id、流式按根文本逐段发 WORKING | `tests/server/test_a2a_http.py`、`tests/server/test_a2a_session.py`、`tests/server/test_a2a_live.py`，黄金文件 `tests/server/testdata/a2a_session_stream.jsonl`（真 desk 加脚本化模型） |
| `a2a` SDK 客户端对 Syndica：流式发送、取任务、取消 | `tests/server/test_a2a_http.py`，黄金文件 `tests/server/testdata/a2a_sdk_task.json` |
| Syndica 客户端对 SDK 写的远端 Agent：INPUT_REQUIRED 后同一任务答复；Syndica 对 Syndica 全程一个会话；被取消时发 CancelTask | `tests/backends/test_a2a_sdk_peer.py`、`tests/backends/test_a2a_model.py`，黄金文件 `tests/backends/testdata/a2a_client_wire.jsonl` |
| Orchestrator 派活给 `a2a:` 角色，交回不可信 | `tests/backends/test_a2a_model.py` |
| OpenAI HTTP：模型列表、chat、Responses、流（每块一个 delta、每个 function_call 都发）、客户端工具、续接、400 / 401 / 404 / 500 的 OpenAI `type` / `param` / `code`、有人看着时批准确认、断开不中止这一轮 | `tests/server/test_openai_http.py`，黄金文件 `openai_chat.jsonl`、`openai_errors.jsonl`、`openai_streams.jsonl` |
| `syndica doctor` 报告外部对象：ACP 握手、A2A 卡片、MCP 版本与工具名、被排除的工具 | `tests/cli/test_doctor_interop.py` |
| 真的 `syndica up --api-token`：`/v1/models` 列出角色，机器令牌不进状态文件；`openai` SDK 打到本地假端点 | `tests/server/test_up_down.py` |
| Gemini CLI 作为托管的 ACP Agent，免费 Gemini key 下完成一件读写 | 手工，2026-10-09，仓库外的临时工作区。项目配置写 `model: acp:gemini` 与命令 `npx @google/gemini-cli@0.63.0 --acp -m gemini-3.5-flash-lite`；用户级 `interop.yaml`（0600）只写 `acp:gemini: {env: [GEMINI_API_KEY]}`；key 只在子壳里从 0600 的临时文件读进环境，跑完删掉。任务是读 `notes.md`、把暗号写进 `answer.md`。Gemini 请求 `edit` 权限时 Syndica 在终端问人：回 `approve` 后 `answer.md` 是 `BLUEFIN`，回 `reject` 后文件不存在。trace 里两次 Read、一次 Write 都记成 `tool.caller=acp-agent:gemini`。工作区（含 `.syndica`）、配置目录、stdout / stderr 与 npm 日志里都找不到 key，跑完没有残留的 Gemini 进程 |
| `openai` SDK 对背后是免费 Gemini 的 `syndica up` | 手工，同一个临时工作区：`/v1/models` 含 Worker；`chat.completions` 答 `PELICAN`，带 `X-Syndica-Session` 续接后答 `OSPREY`，`X-Syndica-Ignored` 列出 `temperature`；key 与机器令牌都不在工作区和 server 状态目录里 |
| kernel 不 import 协议 SDK；surface 不 import backend | `scripts/check_deps.py`、`tests/test_deps.py` |
| 协议响应与外发 JSON 先脱敏 | `tests/test_write_points.py`。OpenAI 的出口、各协议在 SDK 之前就回的响应（版本错误、鉴权失败）走 `json_response` / `redacted`；MCP Streamable HTTP、A2A JSON-RPC 与 ACP NDJSON 的正常响应由 SDK 序列化，处理器在交出之前逐字段脱敏（计划决策日志 P3） |
| server 不碰内核私有属性 | `tests/server/test_private_guard.py`。客户端工具经 `SessionHandle.install_tools` 装，不读 `runtime._table` |

## 定向变异

按计划「验证节奏」那一组，在 `call_external`、四个协议各自的信任标记、版本协商、出口脱敏、令牌与 Host、密钥来源、进程组上
施加 17 条，每条都做「`src` 的 sha256 校验 → 施加 → 清字节码 → 跑定向用例 → 还原 → 再校验」。第一轮 3 条存活：

- ACP 客户端收下对端答的版本 2：用例只测了版本 0。改成 0 与 2 两档参数化。
- OpenAI 客户端工具的结果被当成可信：往返用例只看了模型的最终回答。补上「模型下一轮看到的那块工具结果不可信」。
- 客户端工具与 Syndica 工具撞名不报错：测试替身的 `install_tools` 不查重名，真装配那条路径没有用例。补了经真装配、用
  `Read` 当客户端工具名的 400。

补完之后 17 条全部变红。「零存活」只在这组变异下成立；第二方的收口对抗评审另记在下一节。

## 收口对抗评审

用户 2026-10-09 允许起子代理。第二方评审（独立子代理，只在私有导出副本里实验）对 `b1ec080` 给出 2 Blocking /
5 High / 若干 Medium，自己施加 68 条变异、35 条存活（去掉等价与假存活之后大部分是真缺口）。它也指出审计里有几条
「已证明」测的是测试替身或死代码。处置分五条互不重叠的线并行做（各自一个 worktree），再由主线补跟进，计划决策日志
P2：

| 级别 | 发现 | 处置 | 提交 |
|---|---|---|---|
| Blocking | 三个会话适配不按根 Agent 过滤：子 Agent 的回答被当成这一轮的结果，子 Agent 先结束就收掉这一轮；OpenAI 把子 Agent 在服务端真跑的 `Bash` 当客户端工具调用交出去；ACP 的失败标记一旦置上永不清除 | 回答、结束与失败只认根；子 Agent 的提问与确认照样交给人、答复交回提问者；OpenAI 根出错不再挂住请求；每个适配一条派活用例 | `f88bb50` |
| Blocking | 托管的 ACP Agent 拿到完整的本地工具与全局策略，`--no-write`、角色 `tools:`、`danger.roles` 都管不到它 | 按角色绑定工具表与处置，读 / 写 / 终端分别要 Read / Write / Bash，`--no-write` 下写与终端拒绝；真子进程的假 Agent 证明 | `0cee938` |
| High | 凭证按名字绑定，目的地由项目配置决定；`http://` 也发 Bearer | 用户级凭证钉住 `url` / `command`，逐字相同才交出，不同就不启动；明文 Bearer 只给回环；抓包服务器证明攻击者地址零请求 | `990b815` |
| High | A2A 真会话路径把异常原文回给对端 | 发送与取消路径都兜底，只留类型名；根出错只报类型 | `f88bb50`、`f16246b` |
| High | stderr 先按字节截断再脱敏，跨在上限上的密钥漏半截 | 整行先脱敏再截断，单行上限 64 KiB，超长退到空白 | `271004b` |
| High | MCP stdio 关闭后，自建会话组的孙进程留下 | 按 ppid 与进程组记下后代（核对启动时刻），关闭、取消、崩溃后收掉；托管 ACP Agent 的监管各自复制一份，阶梯被取消时也收 | `271004b`、`7ec858a` |
| High | A2A 客户端没有预算时永不超时 | 按配置超时兜底 | `0cee938` |
| Medium | OpenAI 的 400 / 500 分支、ACP 与 A2A 的脱敏、无人值守提问、A2A 客户端版本协商与远端错误脱敏、ACP allow-kinds 与隔离路由、MCP serve 的溯源 / schema / 精确版本门 / 外层兜底、`call_external` 的确认后复检与守卫回退、`interop.yaml` 权限族与 `syndica acp` 的 cwd 校验 | 逐项补用例；发现的行为缺陷一并修（A2A 真会话路径重复脱敏、远端三件套不脱敏、守卫认第三方的 `()`） | `a9cef5f`、`f88bb50`、`0cee938`、`990b815` |

跟进（主线，`f16246b`）：ACP 紧挨着的两条提示与取消后的下一条提示不再丢失、不再拿到上一轮的尾巴；A2A 与 ACP 根出错
只报类型；A2A 取消兜底；ACP 确认参数限长。

跟进（主线，`bf903d2`）：信任确认提示读用户级 `interop.yaml`，逐项写明哪个变量交给哪里、哪项因钉子对不上而不交；
只在项目声明了会用到它的外部对象时才读这份文件，坏掉的用户级文件不再挡住无关的 run（doctor 照样报）；A2A 客户端
取公开的 agent card 不带 Bearer。

变异：五条线各自对评审的存活项与自己补的点施加变异（MCP serve 与 `call_external` 13 条、凭证 15 条、会话适配 33 条、
stderr 与进程 13 条、托管角色 38 条、ACP 进程树 7 条），主线跟进 13 条（1 条存活，是一处多余的等待，已删掉），处置之后
全部变红。「零存活」只在这些变异下成立。

评审指出、这次没有改的：`call_external` 与开发期守卫各有一份「不认第三方 `()`」的规则，靠对照用例钉住（去重要把一个
kernel helper 公开，留到 v1.0 冻结公开 API 时）；MCP stdio 客户端从 SDK 挂起的生成器里读子进程 pid（读不到就拒绝连接
并写明 mcp 版本，真 SDK 用例钉住）；两次 fork、在两次轮询之间同时离开进程组与父子链的守护进程认不出；
`syndica.backends.mcp` 导出的 `ExternalProcess` 仍只按进程组收（`src/` 里没有活路径用它，MCP stdio 走 SDK 加进程树）。

## 逐条复核（2026-10-10）

`86e235b` 推送之后，独立子代理只读地把计划的硬约束、公开契约与阶段零至七逐条对到代码与用例，标出 PROVEN / PARTIAL /
MISSING / CONTRADICTED。它推翻了这份审计的几条说法：三处与公开契约冲突，计划要求的黄金文件只有 OpenAI 一个达标，
ACP 有几条计划条目没做也没登记，进程不留只在 backend 层证明过。处置分六条互不重叠的线并行做，主线另补几处；有意不照
计划做的几条写进计划决策日志 P3。

| 发现 | 处置 | 提交 |
|---|---|---|
| A2A 缺版本头答 -32022，计划与 SDK 都是 -32009 | 改成 -32009，SDK 客户端认出 `VersionNotSupportedError` | `a141096` |
| A2A 的 `contextId` 不是会话 id | `contextId` 就是会话 id；不带就新开，未知的交回三件套的 FAILED 任务 | `a141096` |
| A2A 流式整轮结束才发一条状态 | 先发 Task，再按根 Agent 的每段文本发 WORKING，最后产物与终态 | `a141096` |
| A2A 0.3 只用裸 httpx 对替身测过；有人值守的确认没测；客户端被取消不发 CancelTask；两个方向都没有线上往返黄金文件 | SDK 自己的 0.3 传输层对真 desk；有人批准的用例；取消时有界地发 CancelTask；`a2a_session_stream.jsonl`（真 desk）与 `a2a_client_wire.jsonl` | `a141096` |
| `external_activity` 的 `source` 发 `acp:<名字>`、`name` 装的是 tool_call_id，黄金文件把偏差钉住了 | `source='acp'`、`name` 是托管 Agent 名；SSE schema 给 `source` 加了枚举（快照 `ea6fc49a256e`） | `c49d078` |
| ACP client：`outputByteLimit` 留开头且拒收 0；`kill` / `release` 没用例；被拒的写入答成功、读被拒时把说明当内容；回调里的 SyndicaError 漏给 SDK | 留结尾、截在字符边界、接受 0；被信号杀掉的终端报信号名；所有回调经同一个出口回三件套 RequestError；真子进程证明外部进程的环境只有 Agent 环境加点名的变量 | `c49d078` |
| ACP client：读不存在的文件把说明当内容交回；终端输出交的是 Bash 工具的报告 | Read 失败在 meta 里标 `unread`，读回调回错误；终端输出取命令自己的 stdout 与 stderr | `f288fb9`、`86179b8` |
| ACP agent：没有黄金文件；停止原因映射零用例；stdin EOF 零用例；`session/load` 只能在同一进程内 | `acp_agent.jsonl`（真子进程，原样记两个方向的字节）；`max_turn_requests` / `max_tokens` 在真装配上；关闭时挂着的确认先按拒绝答掉；第一条提示时把 sessionId 与会话 id 的对照写进工作区之外，load 经恢复会话重放 | `aa2afa3` |
| stdout 只放协议没有抓原始字节的用例；`syndica mcp` / `syndica acp` 缺 SDK 时抛裸 ImportError | 两个 stdio 命令各一条逐行解析 JSON-RPC 的用例；先查 `find_spec`，缺了报三件套指向 `pip install 'syndica[mcp]'` / `'syndica[acp]'` | `aa2afa3` |
| OpenAI 错误体的 `type` / `code` 恒为 `invalid_request_error` / `syndica`；错误不带会话头；`n=true` 当成 1；多块合成一个 delta；Responses 流只发第一个 function_call | 按 OpenAI 的取值给 `type` / `param` / `code`；能续接时带会话头；先拒 bool；每块一个 delta；每个 function_call 都发；新增 `openai_errors.jsonl` 与 `openai_streams.jsonl` | `58b1f22` |
| OpenAI：Responses 对象的 `tools` / `tool_choice` 恒为空；`/v1/*` 的 401 是 Syndica 的形状 | 回显客户端工具，有工具时 `tool_choice` 写 auto，请求里的 `tool_choice` 列进 `X-Syndica-Ignored`；Bearer 不对时 `code: invalid_api_key` | `d6a74bb`、`4422f9b` |
| `syndica doctor` 只报 MCP，不列工具清单 | ACP 握手（协议版本、Agent 名）、A2A 卡片（版本、技能，取卡片不带 Bearer）、MCP 每个服务的工具名；一个对象失败只占一行 | `61f8c9f` |
| 进程不留只在 backend 层证明过；Runtime 关机用的是不起进程的替身 | 真 SDK stdio 服务留下两个孙进程（一个 setsid）：`close_session`、`runtime.shutdown`、chat 关闭（含中断）、run、MCP 调用中途的 Ctrl-C（退出码 130）、`up` → 建会话 → `down`，三个进程都不在；没发现泄漏，宿主代码没改 | `eb69973` |
| MCP：没有角色用到的服务也被拉起；分页只对替身测过；非 initialize 请求不查 `MCP-Protocol-Version`；三个 list / get 处理器没兜底；两份黄金文件只钉 schema 或名字 | 只连角色用到的服务（doctor 给它们一行 `mcp: unused`，组合根不为它们导入 backend）；真 SDK 服务分页；每个 POST 先按接受表查版本头（不带头按规范视为 2025-03-26）；兜底只报类型；`mcp_serve_wire.jsonl` 与 `mcp_client_wire.jsonl` 逐字节比对线上 JSON 行 | `eb69973`、`1de9756` |
| 全套里唯一的 RuntimeWarning；导出的 MCP `ExternalProcess` 只按进程组收 | 可观察切点的分发被取消时关掉还没开始的 hook 协程；`ExternalProcess` 接上进程树 | `72c7533` |
| ACP agent 的 `tool_call_update` 从不带输出（计划要「输出经脱敏截断」） | 工具结果块作为那次调用的 `raw_output` 交给编辑器，先脱敏再截到 4000 字符；子 Agent 的工具结果同样推过去 | `061f7b2` |
| 配置没被信任时 `syndica acp` 启动就退出，编辑器收不到指向 `config trust` 的三件套 | 照常握手，`session/new` / `session/load` 交出带 `syndica config trust --sha256 …` 的三件套；不读配置的任何内容；load 先查信任再查 sessionId | `31cda5b` |
| `POST /a2a/` 缺 extra 时的 501 没有用例 | 补上 | `10e5a83` |

## 明确不做（计划收尾复核）

这些都没有做，和计划「明确不做」一致：

- MCP 上不提供「派一个 Syndica Agent 干活」的工具。
- MCP server 不做反向请求、订阅、可续传事件存储、OAuth。
- 没有「把外部工具或外部 Agent 声明为可信」的开关。
- A2A 不做推送、REST、gRPC。ACP 不做 HTTP / WebSocket，也不做 fork / resume / close、elicitation。
- LLM 层没有真流式。OpenAI 的流是整轮结束后按块合成的。
- 外部进程崩溃后不自动重连。

## 还没做

| 条目 | 原因 |
|---|---|
| 真实客户端收成自动测试 | Inspector 与 Gemini CLI 经 npx 拉取、Gemini 要 key，按路线图归手动 / nightly |
| 回放文档里的 `mcp.external_tool` | 规则参数表只收内核规则。带 MCP 的 run 不写回放文档，结构 trace 照常记 |
| A2A 客户端在一个任务的第一条 `SendMessage` 被取消时没法通知远端 | 非流式 `SendMessage` 要到响应里才给出任务 id；要做得让客户端改走流式请求 |
| A2A 版本头与方法名的写法对不上（头是 0.3、方法用 1.0 的名字）时 | 由 SDK 以 -32009 拒绝，消息不是三件套 |
| doctor：凭证钉子对不上、钉住的 Bearer 变量没设、`interop.yaml` 权限不对时整条命令失败；MCP 一个服务连不上时整个 MCP 只有一行失败 | 与 run 的绑定共用同一步，失败方式与 run 一致；MCP 的连接是全有或全无 |
| 前端生成的事件类型里 `external_activity.source` 仍是 `unknown` | `gen-events.mjs` 不读属性 schema 的枚举 |
| `syndica sessions rm` 不删 ACP 的 sessionId 对照文件 | 之后 load 那个 id 交回三件套「会话不在了」，只留下一个很小的文件 |
| `/mcp` 的版本头只在 POST 上查 | GET 与 DELETE 不经我们的处理器，交给 SDK；initialize 正文里的版本被拒时 HTTP 是 200，版本头被拒时按 MCP 规范是 400 |
| `syndica mcp`（stdio）没有自己的版本门；真子进程用例只跑了旧时代 | stdio 由 SDK 按 MCP 规范协商（计划决策日志 P3）；两个时代在进程内各有用例 |

## 公开契约

- 内核：`ExternalCallPolicy`、`call_external`；事件 `external_activity`；`TRACE_SCHEMA` 由 M10 升到 `syndica.trace/7`，新增属性 `tool.caller`（M11 加 `verdict.trust` 后是 `/8`）。
- HTTP：`docs/reference/api-changes.md` 的 `aefdf03abce0`（`external_activity`）与 `ab8ab7901630`（协议路由）。OpenAI 兼容路由不另装包；没装 `syndica[mcp]` / `syndica[a2a]` 时对应路由仍是 501。
- 会话：`SessionHandle.install_tools`。撞上已有工具名时抛 ConfigError，不覆盖。
- CLI：`syndica mcp`、`syndica acp`、`syndica up --api-token`。
- 配置：项目 `.syndica.yaml` 的 `interop` 只按名字引用；用户级 `interop.yaml` 在信任存储旁边，权限 0600，只存变量名，
  每一项钉住 `url`（配 `bearer_env`）或 `command`（配 `env`）。
- 角色：`model: acp:<名字>` 的角色，读 / 写 / 终端分别要在 `tools` 里有 Read / Write / Bash。
- 依赖：`syndica[mcp]`、`syndica[acp]`、`syndica[a2a]`；`openai` 只在 dev，当测试客户端。

## 验证

```text
uv run ruff check .                                          通过
uv run ruff format --check .                                 通过
uv run pyright                                               0 errors
uv run python scripts/check_deps.py                          通过
uv run python scripts/check_links.py                         通过
sh scripts/check_web.sh                                      通过（产物已按当前 OpenAPI 快照重建）
```

全套 `uv run pytest -q`：第一次（守卫修复前）10265 passed、4 failed（自由文本属性 `tool.caller`、server 私有名、写入点、前端产物哈希）。第二次 10263 passed、6 failed，都在 `tests/server/test_acp_turn.py`（公开方法没有转调测试覆盖的私有钩子）。第三次 10275 passed、1 failed：`runner/training.py` 的两处 `write_text` 没在同一个表达式里脱敏。ACP 那 6 条在第三次已经通过。第四次（含 M11）10300 passed，0 failed，813 秒；唯一的警告是 DevModule 在 ACP 取消时留下没 await 的 `_Recorder._ended`（已知，未修）。

收口评审处置之后的导出验证（`git archive` 到临时目录，`uv sync --all-extras` 后跑全部门禁，空闲机器、没有并行任务）：

- `bec0684`：除 pytest 外全部通过，Playwright 28 / 28；全套 10560 passed、1 failed。失败的是凭证钉子的一条用例仍断言取
  agent card 也带 Bearer，与 `bf903d2` 的新契约冲突；`47c2dc2` 改成逐个请求断言（取 card 不带，JSON-RPC 带）。
- `47c2dc2`（最终代码提交）：ruff、format、pyright（0 errors）、`check_deps`、`check_links`、`check_web`、Playwright 桌面 +
  移动 28 / 28 都过；全套 10561 passed、0 failed，957 秒。警告仍只有上面那一条。

`86e235b` 推送之后的逐条复核处置完，最终代码提交 `1de9756` 在空闲机器上做了导出验证：ruff、format、pyright
（0 errors）、`check_deps`、`check_links`、`check_web`、Playwright 桌面 + 移动 28 / 28 都过；全套 10714 passed、
0 failed，1036 秒，没有任何警告（那条 `_Recorder._ended` 的 RuntimeWarning 由 `72c7533` 修掉）。合并的过程中查出两处
分支之间的接缝，都在合并后修了：两个同名的测试模块让 pytest 收集中断（`16ff9d7` 改名），doctor 的逐字断言没算上
另一条线新加的一行（`1de9756`）。

M10 与 M11 都推送之后，完成审计时把逐条复核的报告再对一遍，又补了三处（`061f7b2`、`31cda5b`、`10e5a83`，见上表）。
最终代码提交 `10e5a83` 在空闲机器上做了导出验证：所有门禁通过，Playwright 28 / 28，全套 10757 passed、0 failed，
1085 秒，没有警告。

之后只有文档提交。
