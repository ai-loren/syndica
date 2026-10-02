# 里程碑 3 收口审计：可观测性 + 脱敏收口

日期：2026-10-02
范围：[M3 计划](../plans/2026-10-01-m3-observability.md) 的 T1–T18；T5–T10 与 T15–T16 另有
[阶段二审计](./2026-10-02-m3-phase2-audit.md)与[阶段四审计](./2026-10-02-m3-phase4-audit.md)，本文复核它们之外的
任务并收口整个里程碑
基线：T1 `aff29a4`、T2 `17651f3`、T3 `06cf340`、T4 `6937784`、T5 `93c5cb1`、T6 `4b8e4dc`（复评修复 `9406c34`）、
T7 `b4add64`、T8 `5a56765`、T9 `edf5d32`、T10 `ef9ddf5`、T11 `dea6cfd`、T12 `788d958`、T13 `dcc8155`、T14 `67f498c`、
T15 `88321c4`、T16 `a5606ce`、T17 `8bde5f9`；T18 的文档与本文同一提交
验证方式：本地验证协议（Actions 额度用完期间，见计划「验证」一节）

## 结论

spec §15 的完成判据——**统一 trace / span、跨 Agent 因果链、运行可捕获为可回放格式，脱敏与它同期完成**——由 T17
的端到端用例（经公开 CLI、脚本化模型，随全量测试零成本地跑）与 T18 的两次真实运行共同证明：

- 免费模型（Gemini `gemini-3.5-flash-lite` 为主、OpenRouter `cohere/north-mini-code:free` 兜底）按本仓的 `syndica-dev`
  拓扑在仓库副本里完成了一处真实小改动（`trace list` 的大小一栏加上 GB 一档并补一条用例）。它留下的 trace 由
  `trace show` 回答了三个问题：Orchestrator 先后创建 Planner、Explorer、Worker、Reviewer；Orchestrator 依次等这四个
  子 Agent（Worker 703 s）；最慢的工具调用是 Orchestrator 等 Worker 的那次 `Delegate` 与 Worker 自己跑全量测试的那次
  `Bash`（550 s）。环境里设了 `OTEL_EXPORTER_OTLP_ENDPOINT`，run 结束时 trace 导出到了本机的 mock collector。
- 一次单 Agent 的真实运行（`--agent Worker --no-write`，3 次 Gemini 请求，Grep 与 Read 各一次）在默认级别写下回放
  文档。在去掉两个 API key、把 HTTP(S) 代理指到一个不通的端口的环境里，`trace replay` 以 0 退出、报「复现」，并自己
  记了一个 `回放自 68ca095a` 的 trace。这次运行的宿主环境里另有一个名字像密钥的变量，它的值也写进了提示词：trace
  目录的 11 个文件里没有它的原文与任何 16 字符片段（5 处 `[REDACTED:secret.env_value]`），导出到 collector 的命令行
  里也是占位符。

拓扑 run 的宿主复核有一条失败，因此没有提供提交——Worker 自己跑的全量是 7233 条全过。失败的是 M3 自己的一条用例
（`test_an_environment_the_export_cannot_use_warns_and_sends_nothing`）：它用 `'caf' not in err` 证明头的值没被回显，
而 stderr 里有 trace 目录名的 8 位随机十六进制，约百分之一的全量跑会撞上一次。探针值换成含十六进制之外字母的
`quiz`，教训写进 `AGENTS.md`。Agent 的改动本身是对的，原样带进本仓（另补一条四个单位各自起点的用例：此前 MB 一档
从没被测过，边界的 `<` 改成 `<=` 也没有用例会红）。交接文件对复核失败只给计数（刻意不抄 Agent 控制的输出），这条
用例的名字是从副本里 pytest 的 `lastfailed` 缓存找到的。

## 完成判据 × 证据

| 判据 | L2（脚本化模型，零成本，随全量测试） | 真实模型（T18，仓库副本） |
|---|---|---|
| 统一的 span：Agent、轮、模型调用、工具调用、确认、等待 | 阶段二审计的「捕获 × 路径」表；T17 的派活 run | 拓扑 run：130 个 span（5 个 agent、40 轮、40 次模型调用、41 次工具调用、4 次等待） |
| 跨 Agent 因果链：派生边与消息边 | T17：边按两端核对（派生边出自 Delegate 调用，消息边从发送到消费） | 4 条派生边；这个拓扑经 `Delegate` 交回结果，没有消息边 |
| 谁创建了谁、谁在等谁、哪个工具最耗时 | T17 的 `trace show` 逐行对 | 见「结论」 |
| 运行捕获为可回放格式，回放不发模型请求 | T14 捕获即回放的往返；T17：失败的 run 回放出同样的失败与同样的文字，给内核打补丁后报出分叉并指对单元 | 单 Agent run：没有 API key、代理不通的环境里 `trace replay` 复现（0） |
| 脱敏在写入之前：原文不出现在任何文件里 | T17：十二个通道各一个假凭证、各命中一族规则，每个文件解码后的每个字符串都不含原文与原文的任何 16 字符片段 | 单 Agent run：宿主环境里的假凭证写进提示词，trace 与导出里都只有占位符 |
| 导出到既有生态 | T16、T17：发出的 span 与 `spans.jsonl` 逐个对上，collector 记下路径与内容类型 | 两次 run 结束时各导出一次：collector 收到 `/v1/traces`、`application/json`，130 与 9 个 span |
| trace 写入失败时任务照常、留下唯一证据 | T11、T17：stderr 恰好一条 ERROR，trace 标成不完整且不导出 | 没有触发 |
| 新增持久化写入点不经脱敏即失败 | T4 的全仓写入点与外发点守卫（含扫描范围没缩的证明） | — |
| CLI 可用 | 全部经 `main()` 与 `RuntimeCliHost` | 全部经 `python -m syndica` |

**体量与 `prune` 的默认值。** 拓扑 run 用时 769 s、40 次模型请求（Gemini 31 次、OpenRouter 9 次），Gemini 回了 2 次
429（各记成那次模型调用上的一条 `llm.platform_failed`，轮换换到 OpenRouter 接着答）；输入 37 万 token、输出 5 千。
默认的「可回放」级别下 trace 目录 256.6 KB（`trace list` 那一栏）、120 个文件（payload 按内容寻址去重，大头是工具结果：
`Read` 交回的文件内容与 `Bash` 的测试输出）。M2 记下
的同类任务是 43–85 次请求，按比例一个 trace 在 0.3–0.6 MB；`prune` 默认留 50 个，即十几到三十 MB，保持 50。

## 计划之外的偏差

- `--topology` 的 run 不写回放文档（T14）：回放重建不出拓扑槽给角色附加的东西。T18 的回放因此用第二次、单 Agent 的
  真实运行证明，而不是计划写的「同一个 trace」。拓扑 run 的 trace 照样有 span、边与 payload，`trace show` 与导出不受
  影响。
- 其余偏差都写在计划里各任务的状态中（阶段二、阶段四审计各自核对过它们那一段）。

## 公开契约核对

逐条对实现 grep 计划的「内核 API 增量」与「CLI 与环境变量」（阶段四审计已核对 D14、D15 与 trace schema）：

- `Runtime(..., tracer=None, ids=None)`、`LocalWorkspace(..., private=())`、`path_outside_workspace_rule(root, *,
  real=None)`、`workspace_root_removal_rule(root, *, real=None)` 与计划逐字一致。
- `syndica.kernel.tracing`、`trace_recorder`、`trace`、`protocols.trace`、`redaction` 列出的每个名字都在；
  `ATTRIBUTE_KINDS` / `ATTRIBUTE_DOMAINS`、`redact_argv`、`encode_digests` 不在 `kernel.__all__`，与计划一致。
  `TraceStoreContract` 在 `syndica.kernel.contracts` 的承诺面上（契约随协议一起进承诺面）。
- span 种类 `agent turn llm_call tool_call confirmation wait`（`module` 保留）、边 `spawn message`（`artifact
  verification` 保留）、结局 `ok error cancelled`、级别 `off structure replay full`，与 trace schema 一节一致，枚举的
  成员→取值表各由一条整体断言钉住。

## 遗留

- 额度恢复后在 Linux 上对 HEAD 补跑 CI：M3 的每个提交都只在本机验证过。
- 阶段二、阶段四审计各自登记的遗留照旧（链接见文首）。
- 计划「风险与遗留（开工前登记）」里的条目：并发下的回放分叉由 T13 的固定种子扰动用例钉住（结局与分叉判定一致），
  没有出现要按记录时机投递的情形；脱敏改变组合文本边界的误报分叉没有在真实运行里出现；体量见上；其余各条（冻结拦下
  第三方并发工具的写法、病态用户正则、粘连的格式 key、Bash 能在 `.syndica` 的位置建文件、私有目录只挡文件工具、chat
  过期提问吞掉下一行、结论交回与打印之间的按键）照计划登记的归属不变。
- T18：交接文件对复核失败只给计数，找出是哪条用例要自己重跑（或读工作区里 pytest 的缓存，那也是 Agent 写得到的）。
  给出失败用例的名字而又不抄 Agent 控制的文字，需要宿主自己跑一遍只收集结果的命令；小任务。
- T18：拓扑 run 不能回放（见「计划之外的偏差」）。要回放它，回放文档得记下槽附加的工具、插件与预算，并在回放时按记录
  重建；归 M4（扩展点完备时 Module 与槽的装配定型）。

## 验证

收口提交的暂存区单独检出到临时目录里跑（不受工作区里别的改动影响）：

```text
uv run ruff format --check .                                 189 files already formatted
uv run ruff check .                                          All checks passed
uv run pyright                                               0 errors
uv run python scripts/check_deps.py                          通过
uv run python scripts/check_links.py                         通过
uv run pytest -q                                             7237 passed
```

T1–T17 各自的变异与对抗评审见计划里每个任务的状态；T18 带进来的两处用例改动各有变异：偶发失败那条的探针换掉之后，
把头的值回显进报错的变异被杀死（回显整项的那条由宿主的脱敏器兜住，属等价变异）；四个单位起点的用例杀死三处边界
`<` → `<=`、去掉 GB 一档与改成两位小数，共五条，无存活。
