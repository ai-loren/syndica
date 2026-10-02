# 里程碑 3 阶段二审计：trace 模型与捕获

日期：2026-10-02
范围：[M3 计划](../plans/2026-10-01-m3-observability.md) 的 T5–T10
基线：T5 `93c5cb1`、T6 `4b8e4dc`（复评修复 `9406c34`）、T7 `b4add64`、T8 `5a56765`、T9 `edf5d32`；T10 与审计补的
用例和本文同一提交
验证方式：本地验证协议（Actions 额度用完期间，见计划「验证」一节）

## 结论

阶段二的捕获在它该生效的路径上都生效：内核插桩（T7）在每条路径上都有用例，`TraceRecorder`（T9）对插桩交来的
每一种东西都有用例，B-19（T10）在两类并发窗口里都关上了。审计找出两类缺口并补上：

- **一处 B-19 漏口：`TOOL_CONFIRMATION_REQUIRED`。** 它是唯一一个属于某个 Agent、却不带 `ctx` 参数的可观察切点，
  按切点参数冻结的做法因此冻结不到那份轨迹——观察者闭包着 Agent 的轨迹往里写，单调用的段里照样不受约束，而几个
  观察者并行跑。不能给它加 `ctx`（按关键字声明参数的第三方 hook 会因多出一个参数而报错）。审计时先由执行器在
  分发前后冻结；随后 T10 评审把规则改成「轨迹只由内核写」（计划 D9 修订），每一个工具段都是窗口，它随段一起被
  冻结。用例：有没有确认通道两种情况下，观察者经闭包追加都被拒、那次调用仍只有一个结果块。
- **四条路径在 recorder 层没有用例**：Fork、拓扑槽、邮箱确认、kill。内核插桩对它们各有用例（`test_tracing.py`、
  `test_ids.py`），但记录器这一层只在单 Agent、Delegate、AskUser、重试上跑过。补了 `tests/kernel/test_trace_paths.py`
  四条：Fork 的派生边出自 Fork 调用、子 Agent 继承来的历史与父 Agent 逐单元指纹相同（指纹按内容、跨 Agent 一致）；
  拓扑槽要出来的实例边出自要它的调用，惰性启动、从没跑过的实例只有边没有 span；邮箱确认的答复挂在人等的那个
  wait span 上；kill 掉的 run 三个 span 都以 `cancelled` 收尾、被打断的模型调用没有回复。

## 捕获 × 路径

| 记下的东西 | 单个 Agent | Delegate / Fork | 拓扑槽 | 人机（AskUser / 确认） | 重试与备用模型 | kill / 关机 / 中止 | 宿主直接等与发 |
|---|---|---|---|---|---|---|---|
| span 与结局（T7） | `test_tracing.py` | 三个子 Agent 各一条派生边；Fork 见新用例 | 新用例 | 每种确认结局一个 span | 每次重试一个 llm_call span | `test_a_killed_agent_ends_its_open_spans_as_cancelled` 等 | 两次宿主等待各一个 span |
| 稳定键（T6） | `test_ids.py` | 派活与 Fork 按调用计数 | 按槽、键与第几个 | 确认按事件里的键作答 | — | 拆除与关机各自的出处 | 宿主那一档按到达先后 |
| 平台、token、故障（T8） | `test_llm_router.py`、`test_tracing.py` | 同单个 Agent | 同单个 Agent | — | 首次、重试与备用各记各的 | 被取消的调用不记 | — |
| 回复、工具结局、指纹（T9） | `test_trace_recorder.py` | 新用例（Fork 的指纹） | — | 新用例（确认的答复） | 脱敏用例里的重试 | 新用例（kill） | 宿主的答复是宿主的动作，不记 |
| 脱敏（T9） | 十二个通道各一个假凭证 | 同单个 Agent | 同单个 Agent | 人的答复是通道之一 | 提供方错误文本是通道之一 | — | 命令行参数是通道之一 |
| B-19（T10） | 十个带 `ctx` 的切点各一条；工具段里的 hook、工具与它起的任务各一条 | `AGENT_END` 观察者被拒 | Runtime 级切点不冻结 | 确认的观察者被拒（有没有通道） | `LLM_FAILURE` 的分发也冻结 | 分发、段与窗口本身被取消时各有一条用例证明窗口照样关 | 另一个任务里的分发不冻结 Agent 自己 |

## 由构造保证的格子

- **内核替 Agent 跑第三方代码的地方都在窗口里。** 冻结坐在 `HooksCenter.trigger` 里，内核的每个切点都经它分发，
  带 `ctx` 参数的十个因此一律冻结；不带 `ctx` 的是三个 Runtime 级切点（按 D9 不冻结）与工具段里触发的三个
  （`PRE_TOOL_USE`、`POST_TOOL_USE`、`TOOL_CONFIRMATION_REQUIRED`），后三个连同工具本身都在段的窗口里。窗口记在
  contextvar 里，第三方代码起的任务继承它；内核自己的驱动任务开头清掉它。
- **结果块不在窗口里落。** 执行器在 `_run_segment` 返回之后按调用顺序追加结果块；段的窗口只包段本身。src 里的
  每一处 `append` 都是内核自己的，都在 Agent 自己的任务里、窗口之外（评审逐处核过）。
- **recorder 的方法不挂起。** 七个方法都只往内存记、往队列放（`put_nowait`），落盘在 recorder 自己的任务里；
  `_Tracing.call` 把 tracer 交回的 awaitable 当成一次失败并关掉它。
- **记下来的自由文本都过了宿主的整套脱敏器。** span 名、字符串属性、事件、边、payload、命令行参数各有一条用例
  用带环境值与用户规则的脱敏器断言占位符；不经脱敏的只有逐字列出的内核标识、摘要与闭集取值，「其余每个字符串
  属性都脱敏」按模型的闭集参数化断言。

## 公开契约核对

逐条对实现 grep 计划「trace schema」与「内核 API 增量」两节：属性闭集（逐字由 `test_the_attribute_set_is_exactly_the_schema`
钉住）、闭集取值（新增 `danger.provenance`）、三种事件、`tool.outcome` 的十五个取值、payload 引用的五个键与各自的
形状、不完整 trace 的标记、`Runtime(..., tracer=None, ids=None)`。一处分歧已改计划：「内核 API 增量」写的是
`syndica.kernel.trace` 下的 `Tracer`、`NoopTracer`、`TraceRecorder`，实现是 `tracing`、`trace_recorder` 两个模块里的
`Tracer`、`NullTracer`、`TraceRecorder`，都从 `syndica.kernel` 导出；`redaction` 另有两个不进 `kernel.__all__` 的
`redact_argv`、`encode_digests`。`FileTraceStore`、回放文档的 schema 模块与两个规则工厂的 `real=` 属于阶段三。

## 遗留

- 额度恢复后在 Linux 上对 HEAD 补跑 CI：T5–T10 都只在本机验证过。
- T9：`wait.target` 仍按自由文本脱敏（等 `Reply` 时它是调用方的文字），用户规则认得 id 时它会被换掉，Agent 之间
  的关系以边为准；用户规则的 pattern 照记在 trace 头里（回放靠它重建规则集；规则写在仓库的配置里）。
- T9：`Tracer` 协议以后再加通道，从头实现它的 tracer 会被 `Runtime` 拒掉；继承 `NullTracer` 的不受影响。
- T10：宿主的代码在两次运行之间往轨迹里追加不受约束——那时没有窗口，也没有并发的写者。冻结管的是追加：块是可变
  的 dataclass，hook 就地改它不在冻结之内（契约写明别那么做）；冻结认的是切点的 `ctx` 参数，用别的名字交出去的
  轨迹不冻结。压缩要改写轨迹，需要内核另给接口（spec §8.8）。

## 验证

```text
uv run ruff format --check .                                 通过
uv run ruff check .                                          通过
uv run pyright                                               0 errors
uv run python scripts/check_deps.py                          通过
uv run python scripts/check_links.py                         通过
uv run pytest -q                                             6387 passed（从索引导出的树上跑）
```

T5–T10 各自的变异与对抗评审见计划里每个任务的状态；审计补的五条用例各有一条变异杀死。
