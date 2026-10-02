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
- 计划「风险与遗留（开工前登记）」里的条目：并发下的回放分叉，T13 的固定种子扰动只覆盖了先后不影响结局的形状；
  收口之后的评审找到了先后由真实耗时决定的形状，回放因此改成按记录的决定点投递（见「收口之后的评审」）；
  脱敏改变组合文本边界的误报分叉没有在真实运行里出现；体量见上；其余各条（冻结拦下
  第三方并发工具的写法、病态用户正则、粘连的格式 key、Bash 能在 `.syndica` 的位置建文件、私有目录只挡文件工具、chat
  过期提问吞掉下一行、结论交回与打印之间的按键）照计划登记的归属不变。
- T18：交接文件对复核失败只给计数，找出是哪条用例要自己重跑（或读工作区里 pytest 的缓存，那也是 Agent 写得到的）。
  给出失败用例的名字而又不抄 Agent 控制的文字，需要宿主自己跑一遍只收集结果的命令；小任务。
- T18：拓扑 run 不能回放（见「计划之外的偏差」）。要回放它，回放文档得记下槽附加的工具、插件与预算，并在回放时按记录
  重建；归 M4（扩展点完备时 Module 与槽的装配定型）。

## 收口之后的评审

收口之后按功能全面性、鲁棒性与稳定性、可扩展与可维护三路对抗评审（各 10–11 条发现），回放时机的修复又单独评审了
两轮。每处修复都带回归用例与变异（存活的补了用例，或写明为什么等价），提交前在暂存区单独检出的目录里跑完门禁与
全量测试。修复各自成提交：

- 回放时机（7c37ec6）。原始 run 里，消息在收件人的哪一轮被注入、一个周期结束时接着开下一个还是停下来、kill 落在
  目标做到哪一步，都取决于真实耗时；回放里外部世界不花时间，这三件事都会变，而内核什么都没改，于是请求对不上记录、
  派活的一方拿到另一个结果、或者 kill 落在别处。评审给出的十二个形状里，两个落在原来的「复现」判定上却轨迹不同（kill
  落在目标等派活的时候），其余的分叉或停住。现在捕获记下每一轮注入的消息、哪些周期紧接着上一个开、哪些调用开了头却
  没有交回；回放的 broker 在门取消息、周期刚结束、停下之后三个决定点上答原始 run 那一刻的答案，邮箱摘掉时把记录里
  属于以后的消息留着；会落下的 kill 等目标走到原始 run 里 kill 落下的地方（周期之间、停在记录里没有的一步上、或停在
  一次原始 run 里没交回的调用里；周期里等着的消息不算）；原始 run 里没交回的派活回放里也不交回。宿主发的消息不在
  记录里，到了就投递。
- trace 命令（1cd8e5e）。更新版本的 syndica 写的 trace 说「升级」，而不是「被改过、删掉它」；没有回放文档时按原因给
  下一步；`prune` 不把回放算进保留数、回放随原 trace 一起删；`show` 按自身耗时排工具、列出模型调用与 token；配置坏了
  不挡 `replay` 与 `export`；导出把失败的工具结局标成错误。
- CLI 的鲁棒性（6e42433）。解析不了的导出端点不再拖住退出；收尾 trace 时按的 Ctrl-C 不丢一次做完的 run 的结论；trace
  先落盘再写结局，有结局而尾部写坏的按损坏处理；分批导出部分成功之后不再叫人整份重发。
- recorder（3690cfc）。store 抛出的 `CancelledError` 与写入任务被宿主取消都算 store 失败：ERROR 一条，不往没被取消的
  调用方抛；store 收不下的单项（`PayloadRefused`）只丢那一项；store 坏了之后不再算指纹、编码与记账，`dropped` 照样
  精确；完整性由 recorder 自己判定（`TraceRecorder.complete`），不读 Agent 写得到的头。
- 体量（c69a246）。逐单元指纹只在回放文档里存一份，按这个实例上一次请求的前缀增量写；历史块按进指纹的四项缓存。
  40 轮对 80 轮的回放文档与 payload 增长都小于 2.5 倍，此前是平方。
- 非 CLI 宿主（ac4f54a）。宿主档案记下编排、确认通道的超时与处置策略的种类，回放照它重建；`describe_run` 当场报出
  回放不出来的规则、策略与字段，不再拖到 run 结束时报成 recorder 的缺陷。
- 第三方接缝（79a6074）。`Tracer` 与 `TraceStore` 的方法签名冻结并由形状表钉住；契约加上几个 Agent 交错、同一对 span
  之间的几条边；`RedactedText`、`encode_redacted`、`Redactor`、`verbatim_attribute` 进公开面，经子模块承诺的名字单列；
  trace 命令只依赖协议；模型失败里回放文档收不下的状态码与等待不记。
- Ctrl-C（6cfd1fb、b68e44f）。计划登记的「结论交回与打印之间的按键」在负载下宽到复现得出来（一条用例 25 次里 2 次
  交接文件没打印出来）：结论定下之后宿主的按键处理一直留到它关闭；关闭时的按键不再把退出码变成 1；git 收尾结束时先定
  结论、再放下按键处理。同样的负载下 50 次全过。
- 回放时机的第二轮复评（0918684、85ae73c、8c0e708）。四处，每处有复现：
  - 派活的一方等子 Agent 的驱动任务结束、醒来才读它活的 Context；子 Agent 此时已是 zombie，一条消息就叫得醒它，复活的
    周期排在派活的一方醒来之前时，交回的是下一个周期的结论。这是内核的竞态，真实运行里同样会发生（一个在子 Agent 进
    zombie 的那一刻给它发消息的注册表，不经回放就复现）；`_finish` 一开头、早于进 zombie，按驱动任务存一份快照，在轮
    等待读那一份（0918684）。
  - kill 落在记录说紧接着开了下一个周期的周期边界上：那次等待与停在邮箱上登记的是同一种，kill 落点分不出来。broker
    现在标出它，它不算 kill 落点。
  - 门在 kill 之前暂存的延迟消息，回放里没有暂存，kill 之后它把 zombie 提前叫醒；原始 run 里对 zombie 不起作用的第二次
    kill，回放里等一个永远来不了的落点。门报出它暂存了什么，内核在那一轮记 `message.deferred`，捕获记成 `deferred`，
    回放的门在同一轮交给内核。`continued` 只看从邮箱取的消息：算上暂存的，被 kill 的 Agent 收尾时那次轮询要等一条下一次
    kill 之后才发的消息，而那次 kill 在等它停下。
  - 外部工具交回之后、结果落进轨迹之前被 kill 或关机打断，回放判成「复现」而轨迹不同：只有宿主中断的等待名单会把它
    停住。捕获记成 `unlanded`，回放拿到结局之后停住，等同样的打断。

  另外：回放停下时，周期边界上的等待给出答案（收割任务也做那次轮询，而关机只等收割任务、不取消它们）；复评报出的两个
  存活变异（回放不看 `continued`、kill 只认直接子代）补了用例；测试会话开始时把相对的 `PYTHONPATH` 换成绝对路径——
  评审对导出的副本跑变异时，Ctrl-C 那条用例导入的是活工作区的代码（8c0e708）。27 条变异里 26 条被杀死，存活的一条
  见下。

修复之后的遗留：

- 处置策略的种类 `block_high_risk` 今天与内核默认相同（对应的变异是等价的）；明写出来，是为了内核默认将来变了，按它
  记下的文档照样按原来的策略回放。
- 进了 zombie 的 Agent 要在 50 轮事件循环之内走完收尾，回放才替早到的消息叫醒它；走不完时它没用上的记录报成分叉，
  不是静默。
- 内核层捕获的拓扑 run 里，根结束之后平级的 Agent 再给它发消息：原始 run 的关机可能拒绝激活，回放的收尾却先让剩下的
  Agent 跑完。CLI 不给拓扑 run 写回放文档，碰不到。
- 快照只取第一次（存活的那条变异）。同一个驱动任务的第二次 `_finish`（它被取消之后收割任务的那一次）要读到不同的东西，
  得注册表进 zombie 的转换写下去之后才答复、kill 恰好落在这一拍里、又有消息在收割任务收尾之前把它叫醒——内存实现在
  那一拍里不挂起。同一个交错里，过期的收割任务还会摘掉复活之后的邮箱、把在跑的 Agent 转去 zombie：这是远端 store 才
  有的竞态，归接入远端 store 的那一步。
- 旧版本写下的回放文档没有 `deferred`：门暂存过、又赶上 kill 的那种形状照旧报分叉（报得出来，不会挂住）。

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

收口之后的评审修复，每个提交同样在单独检出的目录里跑门禁与测试（第二轮复评的三个提交：0918684 跑相关的 736 条，
85ae73c 跑全量 7494 条，8c0e708 在相对的 `PYTHONPATH` 下跑 conftest 与捕获即回放的 50 条）。工作区在最后一个代码
提交之上：

```text
uv run ruff format --check .                                 193 files already formatted
uv run ruff check .                                          All checks passed
uv run pyright                                               0 errors
uv run python scripts/check_deps.py                          通过
uv run python scripts/check_links.py                         通过
uv run pytest -q                                             7497 passed
```

修复之后又用免费模型在仓库副本里跑了真实运行，回放都在去掉两个 API key、HTTP(S) 代理指到不通端口的环境里做：

- 第二轮复评之前：一次多 Agent 运行（trace `cae913be`）回放复现，导出 14 个 span；一次 `--handoff` 运行（trace
  `f2fef230`）回放复现，导出 9 个 span，`prune` 留下原 trace 与它的回放。trace 与导出里都没有 token 的原文。
- 第二轮复评的修复之后：Orchestrator 经 `Delegate` 派 Explorer 读 README（只读，5 次模型调用，11.7 s，trace
  `77b40ab2`）；`trace show` 画出派生与等待，`trace replay` 以 0 退出、报「复现」。trace 与回放的两个目录里没有任何
  一个 API key 的原文。
