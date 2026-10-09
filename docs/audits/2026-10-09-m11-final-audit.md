# 里程碑 11 收口审计：训练出口

日期：2026-10-09
范围：[M11 计划](../plans/2026-10-09-m11-training-export.md)，对照路线图「M11 训练出口」的 TODO 与验收标准
验证方式：本地验证协议。首版提交为 `0b02601`；第二方评审的处置在其后的提交里，首轮推送到 `86e235b`；之后的逐条复核
处置在 `19c0e6d`、`52ad5a8`、`d867450`，最终代码提交 `d867450` 过了导出验证。

## 结论

中立轨迹（`syndica.trajectory/1`）、SFT（messages 与 ShareGPT）、偏好（DPO 形状）、RL 与 `trajectory` 五种导出，以及
`syndica batch` / `syndica export` 都已接上。数据从真的会话、Goal / Rubric、trace 与产物登记里拼出来，用宿主的脱敏器
（用户规则加导出这一刻环境里的密钥值）逐叶脱敏。恢复过的会话保留每次 run 的裁决；不可信的验收给出的裁决随样本标成
不可信。TRL 读得进导出的 SFT 与偏好文件：`SFTTrainer` 把 SFT 分好词、建出 `labels`，工具调用经模板进了分词；
`DPOTrainer` 建出 chosen / rejected 的 token。训练步本身没跑（见「没做」）。

## 路线图验收 × 证据

| 要求 | 证据 |
|---|---|
| schema 版本、字段顺序、必填字段有黄金测试；迁移在只有一版时不做（计划「明确不做」） | `test_a_trajectory_matches_the_golden_key_order`（黄金文件 `tests/runner/testdata/trajectory.json`）、`test_read_trajectory_accepts_its_own_schema_and_refuses_another`、`test_an_inconsistent_attempt_is_refused`（空 id、user 带工具调用、trust、裁决历史、裁决来源、裁决信任） |
| 从 trace / session / artifact / verdict 生成中立轨迹 | `assemble_attempts`；E2E `test_every_exported_trajectory_reads_back_with_its_prompt_tools_and_timing`：真实 run 导出的每一行都过 `read_trajectory`，`model` / `tools` / `system_prompt` 非空，`timing` 有开始时刻 |
| 恢复过的会话保留每次 run 的裁决 | `test_a_resumed_session_keeps_the_verdicts_of_every_run`、`test_an_older_trace_without_verdict_events_still_counts_in_a_resumed_session`；E2E `test_a_session_blocked_then_resumed_and_passed_exports_both_verdicts`：经 `run --session` 恢复，manifest、轨迹、RL episode 都是 `['block', 'pass']` |
| 不可信标记随导出存活（消息与裁决） | `test_the_trace_records_the_trust_of_each_verdict`（被收紧的 Module、不可信验收、可信验收）、`test_an_untrusted_verdict_anywhere_in_the_session_marks_the_sample`、`test_every_row_format_carries_the_untrusted_flag`（SFT / ShareGPT / 偏好 / RL / 轨迹，正反两个方向）、`test_the_trajectory_keeps_the_trust_of_each_message` |
| `unverified` 默认排除有反向测试；包含时显式且写进 manifest | `test_unverified_is_left_out_unless_asked_and_a_secret_is_never_written`、`test_include_unverified_rejects_a_yaml_bool_lookalike`；E2E `test_include_unverified_is_explicit_and_recorded` |
| redaction metadata 随样本导出；原文密钥永不导出、占位符保留 | 每行与 manifest 带 `redaction`（规则摘要、规则版本）；E2E `test_export_uses_the_hosts_rules_and_secrets_of_this_moment`：会话里还存着 run 时没认出的环境密钥与内网主机名（`test_the_session_still_holds_what_only_export_time_redaction_can_catch` 先证明这一点），五种导出里一个都没有，manifest 的摘要等于带那条用户规则的脱敏器；逐叶脱敏与转义：`test_write_export_redacts_each_leaf_before_encoding`、`test_a_quoted_secret_inside_tool_arguments_is_redacted_whole`、`test_a_quoted_secret_inside_an_artifact_field_is_redacted_whole`、`test_a_named_secret_that_json_escapes_is_still_dropped`（引号、反斜杠、换行 × 正文、实参） |
| 偏好对来自同一任务 | `test_preference_rows_are_dpo_shaped_and_come_from_the_same_goal_and_rubric`、`test_attempts_judged_against_different_statements_are_not_paired`、`test_two_attempts_with_the_same_verdict_are_not_a_pair`；E2E 里 chosen 是 pass、rejected 是 block |
| 导出失败不破坏源 trace / 会话 | E2E 导出前后 `.syndica` 下每个文件的 sha256 相同；导出拒绝写进工作区（`test_an_export_inside_the_workspace_is_refused`，以及真子进程 `tests/cli/test_export_safety.py` 里的大小写变体路径、指回工作区的上级链接）；`--output` 本身是符号链接时 CLI 拒绝（同一文件，链接目标仍为空）；写盘 0600、拒非空目录、不覆盖不跟随（`test_write_export_files_are_private_and_new`、`test_write_export_refuses_a_directory_that_already_holds_an_export`、`test_the_writer_never_replaces_or_follows_an_existing_name`） |
| 训练 backend 依赖不进 kernel / runner 核心 | `check_deps` 拒绝 backend 之外 import `torch` / `trl` / `datasets` / `transformers`（`test_a_training_package_is_caught_outside_backends`，4 个包 × 5 个位置）；子进程 `test_importing_the_trl_backend_does_not_load_torch` |
| 一批脚本化任务产出四类 verdict | E2E 夹具：一份任务清单经组合根 `_Training.batch` 交给真的宿主，脚本化模型下四次 run 依次报 `验收：pass` / `partial` / `block` / `unverified` |
| SFT 的消息顺序、工具调用、产物引用 | E2E：`user` × 4（人的话与内核注入的 Goal / Rubric / 预算块）→ `assistant`（`Write` 调用）→ `tool`（同一个 `tool_call_id`）→ `assistant`；`artifacts` 是带版本的 `report.md`；三个并行调用的顺序 `test_three_parallel_calls_keep_their_order_in_sft_and_sharegpt`；产物两个来源各一条用例 |
| RL：分词、loss mask、reward | `test_rl_row_masks_only_assistant_tokens_including_its_tool_calls`、`test_the_reward_follows_the_verdict_and_unverified_is_not_a_failure`；E2E 四行 mask 与 id 等长、reward 1.0 / 1.0 / 0.5 / 0.0，manifest 记 `tokenizer: utf8-bytes` |
| TRL / verl 最小 smoke | `tests/backends/test_trl_dry_run.py`：`SFTTrainer` 读 `write_export` 导出的 SFT 文件，`labels` 在准备后的列里，只差一个工具调用的两行里带调用的 token 更多；`DPOTrainer` 读导出的偏好文件，建出 `prompt_ids` / `chosen_ids` / `rejected_ids`；E2E `test_trl_reads_the_exported_sft_file_with_its_tool_calls`：真实 run 的导出，`labels` 在列里，带 `Write` 调用的那行 142 个 token、不带的 61 个 |
| 一个任务失败不拖垮整批 | 导出：没有 Goal 的会话记进 `failed`、一个读坏的 trace 记进 `skipped_traces`（E2E `test_one_unreadable_trace_does_not_drop_the_rest_of_the_export`），读坏的 trace 头与会话 meta 也点名（`tests/cli/test_export_safety.py`，坏 JSON 与更新版本 schema 各一条），退出 1 并写三件套，其余照写；脱敏或编码失败的单条尝试同样只记这一条；批处理：`test_one_batch_failure_does_not_drop_the_rest`、`test_a_run_that_exits_nonzero_is_a_failed_row_and_the_next_task_still_runs`，真进程 `test_syndica_batch_runs_every_good_line_and_reports_the_bad_one` |
| manifest 足够复现过滤条件 | `format`、`include_unverified`、`sources`、`samples`（每条的 trace id、裁决历史、裁决来源、是否含不可信内容）、`dropped_unverified`、`dropped_secret`、`failed`、`skipped_traces`、`redaction`、`tokenizer`；一个目录一次导出，不会被下一次导出覆盖 |

## 实现中发现的问题

- `SessionMeta.last_trace` 从 M6 起就没有人写，新会话一律是 null。导出改为按根 Agent 的 id 在 trace store 里找；内核那一侧
  登记进路线图遗留表。
- 验收伴生 Agent 与回放写出的 trace 都带着根 Agent 的 id 或 `verdict.status`；裁决只读根 Agent，回放 trace 跳过。
- 打回之前的裁决原来不进 trace（agent span 只记终态）。内核在每次给出裁决时记一个 `verdict` 事件。
- 整段 JSON 编码之后再脱敏，转义过的引号让规则只换掉密钥的后半截；工具实参本身是 JSON 文本，同样如此。改为先按原文
  解析、逐叶脱敏再编码。

## 第二方收口评审

第二方评审（独立的子代理，只在私有导出副本里施加变异）对 `0b02601` 给出 2 Blocking / 1 High / 7 Medium / 若干 Low，自己施加 27 条变异、19 条存活。
处置见计划决策日志 P2，要点：

| 级别 | 发现 | 处置 |
|---|---|---|
| Blocking | CLI 导出用裸 `RuleRedactor()`，不读用户规则、不带宿主密钥 | 用宿主的 `redactor_for(config)` 与此刻环境的密钥值；E2E 用 run 之后才出现的密钥与规则证明 |
| Blocking | 一个读不出的 trace 让整次导出一行不写 | 逐个 trace 兜住，记进 `skipped_traces` |
| High | 点名密钥在 JSON 文本上找，转义后漏掉 | 在解码后的字符串与解析后的工具实参上找 |
| Medium | 写盘跟随符号链接、0644、静默覆盖 | 0600 新建（`O_EXCL`、`O_NOFOLLOW`），空目录，拒符号链接 |
| Medium | 同一目录多次导出覆盖 manifest | 一个目录一次导出 |
| Medium | 回放 trace 顶替原裁决；没有 trace 与没有裁决混为一谈 | 跳过回放 trace；`verdict_source` |
| Medium | 打回之前的回合被当正样本 | `verdict` 事件与样本的 `verdicts`；切分留给训练侧（「没做」） |
| Medium | trust 丢失；导出落在工作区里成为洗白路径 | 消息带 `trust`、行带 `untrusted`；`--output` 拒绝工作区之内 |
| Medium | 中立轨迹不落盘、缺字段、redaction 元数据是常量 | `trajectory` 格式、`read_trajectory`、`model` / `system_prompt` / `tools` / `outcomes` / `source`，`redaction` 取实际用的脱敏器 |
| Medium | 训练依赖只有一条 AST 检查 | `check_deps` 规则与子进程用例 |
| Low | 单条脱敏失败拖垮整批；裁决取列表顺序；unverified 的 reward 当 0；偏好行不能直接喂 DPO；ShareGPT 并行调用；batch 不透传配置；失败时 stderr 什么也不说；多处测试盲区 | 全部修掉或补了用例 |

## 变异

每条都按「`src` 与 `scripts` 的 sha256 校验 → 施加 → 清字节码 → 跑定向用例与 `check_deps` → 还原 → 再校验」。

- 首版：15 条，3 条存活（逐叶脱敏、Rubric 摘要不看陈述、组合根忽略退出码），补用例后全部变红。
- 评审在私有副本里施加 27 条，19 条存活，见上表。
- 处置之后：评审存活的那批（M1–M25）加上这次新加的点（N1–N8：回放 trace、读坏的 trace、工作区之内、非空目录、
  覆盖写、batch 透传、`verdict` 事件、trust）共 27 条。第一轮 5 条存活：同一会话的多个 trace 取了第一个（缺恢复过的
  会话的用例）、产物字段不逐叶脱敏（用例里的密钥没有引号）、manifest 丢掉建轨迹时的失败（坏尝试在脱敏那一步就被拦下）、
  去掉对 `ContractViolation` 的专门处理（通用兜底照样接住，只差记下的正文）、写盘不用 `O_EXCL`（空目录检查挡住了常规
  路径，防的是检查与写之间的竞争）。各补一条用例后 27 条全部变红。

「零存活」只在这几组变异下成立。

## 逐条复核（2026-10-10）

`86e235b` 推送之后，独立子代理只读地把计划、路线图 M11 一节与 spec §9.5 逐条对到代码与用例。它推翻了这份审计的几条
说法：「拒符号链接」只在库函数层成立、「打回之前的回合」只在一个 trace 之内成立、「trust 随导出」只测到 SFT 而裁决的
trust 根本没导出、「TRL 直接读偏好行」从没被 TRL 读过、「schema 迁移有黄金测试」其实只有拒绝。处置分三条互不重叠的
线（计划决策日志 P3）：

| 发现 | 处置 | 提交 |
|---|---|---|
| `syndica export --output <符号链接>` 不拒绝（CLI 先 `resolve()`）；大小写变体的路径进得去工作区；读坏的 trace 头与会话 meta 静默消失、退出 0 | 解析之前查输出路径本身；按设备号与 inode 认工作区；`FileTraceStore.unreadable()` 与新的 `FileSessionStore.unreadable()` 并进 `skipped_traces` / `failed`，退出 1 | `19c0e6d` |
| TRL 只读过 SFT；SFT 用例没断言 `labels` 与工具调用；`trl>=0.21` 而 dry-run 靠的是 1.15 的行为；导出文件为空时只有一个没有信息的 `StopIteration` | `dry_run_preference`（`DPOTrainer` 读偏好文件，需显式的本地参考模型）；`labels` 与 token 数断言；`trl>=1.15`（`uv.lock` 版本不变）；空文件先拦下报三件套 | `52ad5a8` |
| 恢复过的会话只读最近一个 trace，丢掉早先的裁决；裁决的不可信标记不进 trace、不进导出；trust 在 ShareGPT / 偏好 / RL / 轨迹上没断言；裁决事件按时间排没有用例；真实 run 的轨迹没验内容 | 合并全部原始 trace 的裁决、因果边与时序；`verdict.trust` 进 trace（`TRACE_SCHEMA` 升到 `/8`），裁决历史里任何一次不可信样本就带 `untrusted`，轨迹 `source.verdict_trust`；每种格式的正反两向用例；乱序事件按 `time_ns`；E2E 逐行 `read_trajectory` 与恢复会话 | `d867450` |

变异：三条线各自施加，导出安全 5 条、TRL 6 条、裁决与信任 19 条，全部变红。「`untrusted` 不看裁决信任」只用 E2E
跑时存活（真实 run 里造不出不可信的验收），由单测、store 层与内核用例守着。

## 没做

| 条目 | 原因 |
|---|---|
| verl | 路线图要求 TRL 或 verl，选了 TRL |
| 真跑一步 TRL 训练 | TRL 1.15 的训练步导入时要 `triton`，macOS 上的 PyTorch 不带；dry-run 停在分词、建出 `labels` / chosen / rejected 之后。本地模板没有 generation 标记，SFT 的 `labels` 是整段序列 |
| 轨迹的版本迁移 | 只有一版，没有可迁移的旧版本 |
| RL 用模型分词器、单独的 RL 导出 backend | 分词器绑定模型；`export rl` 按 UTF-8 字节给 id 并在 manifest 记下，按模型分词由训练框架那一侧重做 |
| E2E 里的不可信裁决与验收伴生 Agent 的因果边 | 真实 CLI 里造出读过不可信内容的验收代价太大，由单测与内核用例守着；伴生 Agent 挂成根 Agent 的子 span，真实 run 不产生 spawn 边 |
| 导出时报出没有 `meta.json` 的半截会话目录 | store 的语义里那是第一次存盘半途失败，会话本来就不存在；报它会让每个残留都让导出退出 1 |
| 写起 `SessionMeta.last_trace` | 改内核的存盘语义，登记进路线图遗留表 |
| 按打回边界把会话切成几条尝试 | 先记下裁决历史，切不切由训练侧决定 |
| 工具的完整 schema | 会话只存工具名 |
| `batch` 的并发、重试、结果行带会话 id、Ctrl-C 时先打出已跑完的行 | 按清单顺序一条一条跑 |

## 公开契约

见计划的「公开契约」一节。`runner.training` 与 `runner.assemble` 没有进 `syndica.runner` 的包级导出。内核新增 trace
上的 `verdict` 事件与 `verdict.trust` 属性，`TRACE_SCHEMA` 升到 `syndica.trace/8`；`FileSessionStore.unreadable()`
（具体类上的方法，SessionStore Protocol 不变）；`syndica.backends.trl.dry_run_preference`。

## 验证

首版 `0b02601` 的导出验证（`git archive` 到临时目录，`uv sync --all-extras` 后跑全部门禁）：ruff、format、pyright、
`check_deps`、`check_links`、`check_web`、Playwright 28 / 28 都过；全套 10301 passed、1 failed，失败的是 `syndica down`
的计时断言（4.48 秒对上限 4 秒），当时两个评审的变异 worker 正把机器压满。

处置与 M10 收口评审之后，最终代码提交 `47c2dc2` 在空闲机器上做了导出验证（没有并行任务）：`uv sync --all-extras`、
ruff、format、pyright（0 errors）、`check_deps`、`check_links`、`check_web`、Playwright 桌面 + 移动 28 / 28 都过；全套
10561 passed、0 failed，957 秒。上面那条 `down` 的计时断言这次通过。

逐条复核处置之后（M10 的处置已在 `2a3e66d` 推送），最终代码提交 `d867450` 在空闲机器上做了导出验证：
`uv sync --all-extras`、ruff、format、pyright（0 errors）、`check_deps`、`check_links`、`check_web`、Playwright 桌面 +
移动 28 / 28 都过；全套 10753 passed、0 failed，1090 秒，没有警告。之后只有文档提交。
