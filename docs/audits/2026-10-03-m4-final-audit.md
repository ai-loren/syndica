# 里程碑 4 收口审计：扩展点完备

日期：2026-10-03
范围：[M4 计划](../plans/2026-10-03-m4-extension-points.md) 的 T1–T16
基线：T1–T5 `7b1ac03`（阶段一评审修复 `03281a6`）、T6 `73ab156`、T7/T8 `ce16a75`（`abbf0c1`）、T9a `6d8a3de`、
T11 `99bd6ee`（修补 `a84ac44`）、阶段二评审修复 `cf1fcaf`、T9b `c351ab1` 与 `1c8168a`、T10 `aa4bcf4`、T12 `ac8c7f1`、
T13 `fcbe437`、T14 `1e277c9`、阶段三评审修复 `1beaa42` 与 `08ffcbd`、T15 `0aeb2dc`、阶段四评审修复 `e77a645`；
收口评审的修复与 T16 的文档和本文各一次提交
验证方式：本地验证协议（Actions 额度用完期间，见路线图「当前主线约束」）

## 结论

spec §15 的完成判据——**三层装配 + Runtime Module + 命名空间切点及其触发接口**——由 T15 的端到端用例（经公开 API
与 `syndica` 命令行、脚本化模型，随全量测试零成本地跑）与 T16 的一次真实运行共同证明：

- 示例第三方 Module（`examples/syndica-example-module`：一个 Flag、一个触发自有扩展切点的工具、一个收这个切点的
  插件、一个服务、零能力、`replay_safe`）只用 `syndica.kernel` 公开的名字写成，经入口点装进仓库副本的环境，不改
  内核源码。
- 真实运行（Gemini `gemini-3.5-flash-lite`，免费档，2 次请求，3.7 s，输入 3124 / 输出 17 token）：信任存储放在
  临时目录，第一次 run 因配置未受信任被拒并给出摘要，`config trust --sha256` 之后，`flags show example_greeting`
  报 `effective: hi`（来自 `flags:`），Agent 调了 `Greet` 并答出 `hi, Ada`。trace `3ed4c2af` 的 `trace show`
  在 Module 一节列出 `example 0.1.0：工具 Greet；插件 example-recorder` 与 `扩展切点：example:greeted ×1`；
  `Greet` 的结果在 trace 里记成 `untrusted`（第三方 Module 的输出一律不可信）。回放文档是 version 3，
  `module_records` 记下 `syndica-example-module 0.1.0`。在去掉两个 API key、HTTP(S) 代理指到不通端口的环境里，
  `trace replay` 以 0 退出、报「复现」，示例 Module 被重建，没有重建的只有宿主自己的 `dev`。trace 目录里没有
  API key 的原文，也没有它任何 16 字符片段。

## 完成判据 × 证据

| 判据（路线图 M4） | L2（脚本化模型，零成本，随全量测试） | 真实模型（T16，仓库副本） |
|---|---|---|
| 不修改 kernel 源码即可加载第三方 module | T15 经 `importlib.metadata.EntryPoint` 装上示例；收口评审 B 在仓库之外另写了一个 Module（可拦截切点、带界的 Flag、`requires`、启停钩子）走完 list / show / trust / run / trace / replay | 示例装进副本环境，`modules list` 认出 `syndica-example-module 0.1.0` |
| 装上后 Agent 获得新工具、新 Flag、新 hook | `test_an_installed_example_gives_the_agent_its_tool_flag_and_hook` | `Greet` 被调用，答案用了 `flags:` 的值 |
| 自有扩展切点触发、hook 收得到 | 同上（服务记下名字）；切点触发记进 trace（T12） | `example:greeted ×1` |
| 不启用时工具、hook、服务、Flag 都不可见 | `test_without_the_example_its_tool_hook_service_and_flag_are_not_there` | — |
| 失败装配不留半注册状态 | 第二个 Module 的插件抛错时示例的 hook 被撤回；装配在 await 之后才失败时只撤回这一次装上的 Runtime 级 hook（收口评审 A 的 F2） | — |
| `flags list`、Agent 的工具、trace 的 module 事件三者一致 | `test_flags_list_the_agents_tools_and_the_trace_name_the_same_declaration_and_it_replays` | 三处同指 `example 0.1.0` |
| 运行期错误不拖垮主流程（除非切点必须失败） | 可观察切点上抛错只记一条点名的三件套 ERROR、run 完成；可拦截切点上抛错让 run 以 error 结束；`on_shutdown` 失败照样交出答案 | — |
| Module manifest 的用户可见错误三件套；`BaseException` 也回滚 | T1–T3 的定向用例与变异；收口评审 B 的 manifest 校验 13 条变异全杀 | — |
| 新增扩展切点进公开面 / 扣留面守卫 | `test_public_api` 的名单与形状守卫（句柄的实例面、`FlagRow` 字段次序、新参数只能按关键字） | — |
| Flag 文档有快照、不漂移 | `scripts/gen_flag_reference.py --check` 是门禁之一 | `flags list` 与参考文档同源 |

## 计划之外的偏差

- **不可信输出的收紧比 D15 宽。** D15 只收紧工具结果与工具切点上的跳过和改写。评审证明被收紧的 Module 还能经模型侧
  四个切点、就地改动、装配期之后注册的 hook、以及 `Runtime.send` / `spawn(query=)` / `send_to_slot` 交出带着缺省
  `TRUSTED` 的文字，于是全部按作者收紧：hook 回调与 Module 工具按注册它的代码的身份跑，作者是被收紧的 Module 时
  这一轮的 assistant block、就地改过的对象、它发出的消息都标 `UNTRUSTED`。仍挡不住的写进计划 D15：在没有身份的
  地方（事件订阅者、线程）注册的 hook，与就地改轨迹里已有的 block。
- **信任确认在 import 时再核一次。** 计划只摘要配置引用的代码；评审证明符号链接、大小写不敏感的卷、预先种下的
  字节码与 run 中途的惰性 import 都能让没被确认的代码跑起来。现在「在工作区之内」按字面或解析之后有一种在里面
  就算、按文件身份比；摘进去的代码在 import 时按确认时的内容核对（`PinnedImports`）；摘要格式升到
  `syndica-trust/2`。
- **回放文档 v3 冻结在阶段四的键集**，扩展它要升 v4；拓扑 run 与 `max_turns: 0` 的角色写 v3。回放重建不了文档记着的
  第三方 Module 时退出 2 并说原因，不再继续跑出一个假的分叉。只声明 Flag 的 Module 不要求 `replay_safe` 就重建。
- **`retry` 的两个 Flag 设了上限**（10 次、60000 毫秒），项目配置写不出一个无休止重试的值。

## 公开契约核对

- `syndica.kernel.__all__` 新增 `ExtensionJunction`、`FlagRow`、`FlagValue`、`ModuleHost`、`flag_reference`，没有
  删掉任何名字（124 → 129）。
- `RuntimeModule` 只在末尾追加字段；`ModuleHost` 新字段必须带默认值（守卫钉住）；两者按值比较、明确不可哈希。
- `HooksCenter.trigger(junction, /, **kw)` 与 `BaseAgent.trigger_extension(handle, /, **kw)` 的句柄只能按位置传，
  负载里可以有 `junction` / `handle` 这两个键。
- 只能按关键字传的新参数：`AgentCatalog.freeze(role_policies=)`、`Runtime.assemble(role_policies=)`、
  `TraceRecorder.describe_run(topology=)`；`Runtime(untrusted_output_modules=)`。
- 格式：trace `syndica.trace/2`（读者收不高于自己的每个版本）；回放文档 version 3（冻结）；信任摘要
  `syndica-trust/2`，信任存储格式 version 1（更新版本写下的存储不信任也不覆盖）。

## 遗留

- 登记表关闭 B-09、B-10（D7：第三方工具必须在自己的类上声明命令参数）与 D-42（D9：不拆回调类型）。
- 新登记 D-62（归里程碑 5）：CLI 的开发期记录器挂在 Agent 的局部层，更早装配的 Module 从工厂里往局部层挂的
  `POST_TOOL_USE` hook 能用 `skip` 让一次调用不进开发期日志。
- 负载下偶发失败、单独重跑都过的用例（留到统一补跑 CI 时处理）：`test_messages_from_two_senders_to_a_woken_child_replay_into_the_same_turns_under_noise`、
  `test_a_hook_that_ignores_sigterm_is_killed`、`test_a_deferred_message_and_a_kill_replay_into_the_cycle_after_the_kill`、
  `test_ctrl_c_mashed_at_the_git_question_still_prints_the_handoff`。`test_trace_export_that_cannot_start_exits_2`
  的前缀 `cd` 约每 256 次撞上一次随机 trace id，已改成按这次的 id 挑一个撞不上的前缀。

## 评审与变异

| 评审 | 结论 | 处置 |
|---|---|---|
| 计划四轮 | 第四轮 0 Blocking、0 High | 三条 Medium 修进计划（`3964a42`） |
| 阶段一 | 0 / 1 High / 3 Medium / 6 Low | `03281a6` |
| 阶段二 | 0 / 1 / 3 / 6 | `cf1fcaf` |
| 阶段三（信任闸与输出收紧） | 0 / 3 / 6 / 5（评审写报告时被内容过滤截断，结论按它的探针与笔记重建） | `1beaa42`、`08ffcbd` |
| 阶段四（trace 与回放） | 0 / 1 / 3 / 14；它的 58 条变异 20 条存活，全部补了用例 | `e77a645` |
| 收口 A（功能与鲁棒性） | High：`send` / `spawn(query=)` 没收紧；Medium：await 之后失败的 spawn 留下 Runtime 级 hook、第三方失败的报错既不点名也不脱敏且关机失败丢掉答案；Low：开发期记录器的次序、`--config` 下的回放 | 收口修复提交（记录器次序登记为 D-62） |
| 收口 B（扩展与可维护） | 0 / 0 / 4 / 13；它的 41 条变异 12 条存活，全部补了用例 | 收口修复提交 |

本里程碑自己的变异（每条都对改动的文件按 sha256 先校验、施加、清字节码、跑用例、还原、再校验）：阶段一修复 17、
阶段二修复 19、T11 修补 9、T9b 34、T10 7、T12 6、T13 8、T14 11、阶段三内核修复 22、阶段三 CLI 修复 27、阶段四
修复 27、收口修复 33，全部杀死；收口时第一次跑出的存活变异（「撤回快照之后的全部」「撤回不比作者」）各补了一条
用例。

## 验证

```text
uv run ruff check .                                          通过
uv run ruff format --check .                                 通过
uv run pyright                                               0 errors
uv run python scripts/check_deps.py                          通过
uv run python scripts/check_links.py                         通过
uv run python scripts/gen_flag_reference.py --check          通过
uv run pytest -q                                             8219 passed
```

推送前对提交的树做了导出验证（把这次提交单独检出到临时目录、在那里跑上面每一道门禁）。
