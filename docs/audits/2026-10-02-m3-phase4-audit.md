# 里程碑 3 阶段四审计：surface 与导出

日期：2026-10-02
范围：[M3 计划](../plans/2026-10-01-m3-observability.md) 的 T15–T16
基线：T15 `88321c4`；T16 与审计补的用例和本文同一提交
验证方式：本地验证协议（Actions 额度用完期间，见计划「验证」一节）

## 结论

阶段四的两个面——`syndica trace list / show / replay / export / prune` 与 OTLP 导出——在计划写下的每一条上都有实现
与用例。按计划逐字核对时找出一处没有任何任务交付的条目，并补上：

- **trace 头的 `source.git_commit`。** 计划的 trace schema 写着来源里有 git commit，模型（T5）与回放文档（T14）都
  有这个字段，CLI（T11）却从没填过——每个任务的评审只看自己的 diff，沉默缺席不在任何 diff 里。现在记录开始时经宿主
  git 环境问一次工作区所在仓库的 HEAD（`-c core.fsmonitor=false`：仓库配置里的程序不会被执行；5 秒上限），不在仓库
  里、还没有提交、git 用不了时记 None；`trace replay` 自己的 trace 同样记。用例：在仓库里、不在仓库里、仓库还没有
  提交三种。

另核对了 T16 评审之后新加的两条约束在每个入口上都成立：导出拒绝没有结局的 trace（计划「不完整的 trace」一条，T16
评审之前 `trace export` 照样导出）；run 结束时发出的只是记录器自己写的 span（trace 目录是 Agent 的 Bash 改得到的）。

## 命令 × 关注点

| | `list` | `show` | `replay` | `export` | `prune` | run / chat 结束时导出 |
|---|---|---|---|---|---|---|
| 退出码 | 0；目录用不了 2 | 0；找不到、读不了 2 | 0 复现、1 分叉、2 用不了；Ctrl-C 130 | 0、1 collector 没收下、2 trace 或端点用不了 | 0；没删掉的 1；点名找不到 2 | 不改退出码 |
| 来自 trace 的文字 | `one_line` | `one_line`（含 Agent id） | 分叉文字 `one_line` | 报错经脱敏与转义 | `one_line` | 告警经脱敏与转义 |
| 日志进 CLI 的 stderr 并转义 | 是 | 是 | 是；回放出原始失败时 Runtime 的 ERROR 收起 | 是 | 是 | 是（`close` 里） |
| 不往工作区里写 | 不建 `.syndica/` | 同左 | 只为自己的 trace 写 | 同 `list` | 只删 trace 目录 | — |
| `.syndica` 是符号链接 | 拒（2） | 拒（2） | 拒（2） | 拒（2） | 拒（2），什么都不删 | 记录时就不记 |
| 坏掉的 trace | 那一行报读不进来 | 2 | 2 | 2（发之前先读一遍） | 没删掉的逐个报原因 | 不完整的不导出 |
| 脱敏 | 读的已脱敏 | 同左 | 回放用记录下的规则集 | 自由文本过宿主的脱敏器，结构不过用户规则 | — | 同 `export`，且只发记录器写的 |
| 用例 | `test_trace_commands`、`test_trace_view` | 同左 | 同左、`test_replay` | `test_trace_export`、`test_otel_otlp` | `test_trace_commands` | `test_trace_export` |

## 由构造保证的格子

- **CLI、runner、backend 互不 import。** 回放与导出都是组合根（`syndica.__main__`）按协议注入的（`Replayer`、
  `TraceExporter`）；`check_deps` 管 import，另一条字符串守卫证明 kernel、cli、runner 里没有一个文件提到导出的 backend，
  并用一次独立的遍历证明扫描范围没有缩。
- **外发只有一处。** 导出器的 `client.post(content=encode_redacted(...))`；写入点守卫认得它，不为它开例外。
- **端点只有两个来源。** 用户自己的环境（宿主的环境不进 Agent 的环境：`pass_env` 拒 `OTEL_EXPORTER_OTLP_*`）与命令行；
  项目配置里像导出设置的键一律拒，并说明为什么。
- **结论先于导出。** run 与 chat 结束时的导出排在 `close` 里，`entrypoint` 在打出结论之后才调它；这时的 Ctrl-C 被
  接住，只告诉人怎么补发。

## 公开契约核对

逐条对实现 grep 计划的「CLI 与环境变量」、D14、D15 与「trace schema」：

- 五个 `trace` 子命令、`--allow-divergence`、`--keep`（默认 50）、`--otlp`；`<id>` 收完整 id、唯一前缀或目录名（带着
  stderr 上打出的那段路径也行）。两处偏差已写进计划：`prune` 还能点名删；`--otlp` 可以不给（用环境里的端点与头）。
- 退出码 0 / 1 / 2 / 130 与计划一致。
- 环境变量：计划列了三个，实现另认 OpenTelemetry 约定的六个（按信号的头与协议、两个开关、服务名与资源属性），计划
  已改。
- D14：只从 store 读、十六进制 id、INTERNAL、Unix 纳秒、带类型的属性、状态、事件、links、extra `otel`、组合根注入、
  头的规则、失败的退出码都有用例；导出器的签名与「再过一遍是幂等的」两处偏差写在 T16 的状态里。
- D15：`trace replay` 自己记一个 structure 级、`replay_of` 指向原 trace 的 trace；回放引擎由组合根注入。
- trace schema：`source` 的五项现在都有人填（`git_commit` 见上）。

## 遗留

- 额度恢复后在 Linux 上对 HEAD 补跑 CI：T11–T16 都只在本机验证过。
- `trace show`、`trace replay`、`trace export` 读的是 trace 目录里的文件，Agent 的 Bash 改得到（D17）；沙箱（M5）之前，
  手动导出别人的 trace 前先 `trace show` 看一眼。run 结束时的导出不受此影响。
- `trace list` 为数 span 读完每个 span 文件（10 万个 span 约 1.5 s）。
- 宿主对 `.syndica` 的核对与 store 第一次打开之间换成符号链接，读与删会跟过去（与 run 的记录同一个前提）。
- 导出不重试：429 / 503 只告警，等 collector 好了用 `trace export` 补发；payload 的正文不导出，只发 sha256。

## 验证

```text
uv run ruff format --check .                                 通过
uv run ruff check .                                          通过
uv run pyright                                               0 errors
uv run python scripts/check_deps.py                          通过
uv run python scripts/check_links.py                         通过
uv run pytest -q                                             7215 passed（从索引导出的树上跑）
```

T15、T16 各自的变异与对抗评审见计划里两个任务的状态；审计补的 `git_commit` 用例由「不记 HEAD」那条变异杀死。
