# 里程碑 10 收口审计：互操作

日期：2026-10-09
范围：[M10 计划](../plans/2026-10-09-m10-interop.md) 的阶段零至七
验证方式：本地验证协议（Actions 额度用完期间，见路线图「当前主线约束」）。改动还在工作区里，没有提交，也没有推送。

## 结论

四个协议都映射到已有的 Syndica 会话，不另写一套 Runtime。被调用的适配在 `syndica.server`，调用外部的三个 backend 互不依赖，由组合根注入。外部内容一律不可信。协议错误是三件套，不回显异常原文。密钥只从工作区之外的用户级 `interop.yaml` 来。

回归证据是官方 SDK 对真进程或真 HTTP 服务的脚本化往返，不花钱。MCP Inspector 的 CLI 模式对 `syndica mcp` 与 `syndica up` 的 `/mcp` 都已手工跑通。Gemini CLI 没有跑：这个环境里没有 Gemini key。路线图把真实客户端写成可手动或 nightly；计划阶段七仍把它列进收口。这一条没做，所以路线图总览写「进行中」，不写「已完成」。

## 完成判据 × 证据

| 判据 | 证据 |
|---|---|
| MCP server 缺省只读，`tools/call` 经 `call_external`，产物与提示脱敏，异常不回显 | `tests/server/test_mcp_serve.py`，黄金文件 `tests/server/testdata/mcp_serve_tools.json` |
| `/mcp` Bearer、Host、未知版本在交给 SDK 之前以 -32022 拒绝 | `tests/server/test_mcp_http.py` |
| 真的 `syndica mcp` 子进程：只有 Read / Grep / Glob，读到的环境密钥被换掉，日志在工作区外 | `tests/server/test_mcp_stdio.py` |
| MCP Inspector CLI 对 `syndica mcp` 与 `/mcp` | 手工：stdio 的 `tools/list` 只有那三个只读工具；`Read` 读回文件，结果带 `trust: untrusted`。HTTP 同样：`http://127.0.0.1:<port>/mcp` 加 Bearer，`tools/list` 没有 Write，`Read` 读回标记且不含探针密钥，日志里也没有令牌。命令必须写在 Inspector 选项前面，且不能用 `python -m` |
| MCP client：版本只收 SDK 的五版，结果不可信，`$ref` 内联，断连不自动重连 | `tests/backends/test_mcp_client.py` |
| ACP agent：版本 1、非文本拒绝、确认、提问、取消、`session/load` | `tests/server/test_acp_agent.py`、`tests/server/test_acp_turn.py` |
| 真的 `syndica acp` 子进程，模型是本地假端点 | `tests/server/test_acp_stdio.py`：版本 0 拒绝后再用 1，图片块拒绝，提问必须答选项，确认先拒绝再批准，批准后 `exit_code: 0`。假 key 不进回答和日志 |
| ACP client：读写与终端经 `call_external`，外部写入读回不可信，高风险命令不执行，崩溃不回显、不重连，挂起的进程组被杀掉 | `tests/backends/test_acp_model.py`，活动黄金文件 `tests/backends/testdata/acp_activity.json` |
| A2A server：卡片、1.0 与 0.3、缺版本头 -32022、异常不回显、提问、无人值守时确认按拒绝 | `tests/server/test_a2a_http.py`、`tests/server/test_a2a_session.py` |
| `a2a` SDK 客户端对 Syndica：流式发送、取任务、取消 | `tests/server/test_a2a_http.py`，黄金文件 `tests/server/testdata/a2a_sdk_task.json` |
| Syndica 客户端对 SDK 写的远端 Agent：INPUT_REQUIRED 后同一任务答复 | `tests/backends/test_a2a_sdk_peer.py` |
| Orchestrator 派活给 `a2a:` 角色，交回不可信 | `tests/backends/test_a2a_model.py` |
| OpenAI HTTP：模型列表、chat、Responses、流、客户端工具、续接、400 / 401 / 404、有人看着时批准确认、断开不中止这一轮 | `tests/server/test_openai_http.py`，黄金文件 `tests/server/testdata/openai_chat.jsonl` |
| 真的 `syndica up --api-token`：`/v1/models` 列出角色，机器令牌不进状态文件；`openai` SDK 打到本地假端点 | `tests/server/test_up_down.py` |
| kernel 不 import 协议 SDK；surface 不 import backend | `scripts/check_deps.py`、`tests/test_deps.py` |
| 协议响应与外发 JSON 先脱敏 | `tests/test_write_points.py`；OpenAI / A2A / MCP 的 HTTP 出口走 `json_response` 或 `redacted` |
| server 不碰内核私有属性 | `tests/server/test_private_guard.py`。客户端工具经 `SessionHandle.install_tools` 装，不读 `runtime._table` |

## 定向变异

按计划「验证节奏」那一组，在 `call_external`、四个协议各自的信任标记、版本协商、出口脱敏、令牌与 Host、密钥来源、进程组上
施加 17 条，每条都做「`src` 的 sha256 校验 → 施加 → 清字节码 → 跑定向用例 → 还原 → 再校验」。第一轮 3 条存活：

- ACP 客户端收下对端答的版本 2：用例只测了版本 0。改成 0 与 2 两档参数化。
- OpenAI 客户端工具的结果被当成可信：往返用例只看了模型的最终回答。补上「模型下一轮看到的那块工具结果不可信」。
- 客户端工具与 Syndica 工具撞名不报错：测试替身的 `install_tools` 不查重名，真装配那条路径没有用例。补了经真装配、用
  `Read` 当客户端工具名的 400。

补完之后 17 条全部变红。「零存活」只在这组变异下成立；这一轮没有第二个人施加变异，收口对抗评审仍算没做。

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
| Gemini CLI 作为托管 ACP Agent，用免费 key 做一次读写 | 环境里没有 Gemini key。key 只能在子壳里 source，不能写进仓库 |
| MCP Inspector 对 `/mcp`（HTTP） | 已手工跑通，见上表。没有收成自动测试：Inspector 经 npx 拉取 |
| `openai` SDK 对 `syndica up` 且背后是免费 Gemini | 同一条 key 缺口。现在的证明用本地假端点 |
| 收口对抗评审 | 2026-10-09 起不再起子代理。作者自己做了 17 条定向变异（见上），但没有第二个人施加变异 |
| 导出验证（`git archive` 到临时目录再跑全门禁） | 改动还没提交。提交前再做 |
| 回放文档里的 `mcp.external_tool` | 规则参数表只收内核规则。带 MCP 的 run 不写回放文档，结构 trace 照常记 |

## 公开契约

- 内核：`ExternalCallPolicy`、`call_external`；事件 `external_activity`；`TRACE_SCHEMA` 为 `syndica.trace/7`，新增属性 `tool.caller`。
- HTTP：`docs/reference/api-changes.md` 的 `aefdf03abce0`（`external_activity`）与 `ab8ab7901630`（协议路由）。OpenAI 兼容路由不另装包；没装 `syndica[mcp]` / `syndica[a2a]` 时对应路由仍是 501。
- 会话：`SessionHandle.install_tools`。撞上已有工具名时抛 ConfigError，不覆盖。
- CLI：`syndica mcp`、`syndica acp`、`syndica up --api-token`。
- 配置：项目 `.syndica.yaml` 的 `interop` 只按名字引用；用户级 `interop.yaml` 在信任存储旁边，权限 0600，只存变量名。
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
