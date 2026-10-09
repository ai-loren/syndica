# 里程碑 11 收口审计：训练出口

日期：2026-10-09
范围：[M11 计划](../plans/2026-10-09-m11-training-export.md)，对照路线图「M11 训练出口」的 TODO 与验收标准
验证方式：本地验证协议。改动在工作区里，没有提交，也没有推送。

## 结论

中立轨迹、SFT（messages 与 ShareGPT）、偏好、RL 四种导出与 `syndica batch` / `syndica export` 都已接上，数据从真的会话、
Goal / Rubric、trace 与产物登记里拼出来。TRL 读得进导出的 SFT 文件，`SFTTrainer` 把它分好词、标好 label；训练步本身没跑
（见「没做」）。

里程碑 10 还没有收口（Gemini CLI、导出验证、对抗评审），按目标的顺序，路线图总览先不把里程碑 11 改成「已完成」。

## 路线图验收 × 证据

| 要求 | 证据 |
|---|---|
| schema 版本、字段顺序、必填字段有黄金测试 | `test_a_trajectory_matches_the_golden_key_order`，黄金文件 `tests/runner/testdata/trajectory.json` |
| `unverified` 默认排除有反向测试；包含时显式且写进 manifest | `test_unverified_is_left_out_unless_asked_and_a_secret_is_never_written`、`test_include_unverified_rejects_a_yaml_bool_lookalike`；E2E `test_include_unverified_is_explicit_and_recorded` |
| redaction metadata 随样本导出；原文密钥永不导出、占位符保留 | manifest 的 `redaction`；E2E `test_no_exported_file_contains_the_raw_secret_and_placeholders_survive`（宿主环境里的密钥进了模型回答，四种导出都没有它，SFT 里是 `[REDACTED:secret.env_value]`）；逐叶脱敏 `test_write_export_redacts_each_leaf_before_encoding` |
| 偏好对来自同一任务 | `test_preference_pairs_come_from_the_same_goal_and_rubric`、`test_attempts_judged_against_different_statements_are_not_paired`；E2E 里 chosen 是 pass、rejected 是 block，partial 不进这一对 |
| 导出失败不破坏源 trace / 会话 | E2E 导出前后 `.syndica` 下除导出目录外每个文件的 sha256 相同；`test_four_verdicts_export_without_touching_the_stores` |
| 训练 backend 依赖不进 kernel / runner 核心 | `backends/trl` 只在函数体里导入；`test_the_trl_module_does_not_import_torch_at_import_time`；`check_deps` 通过 |
| 一批脚本化任务产出四类 verdict | E2E 夹具：真的宿主、acceptance 模块、脚本化模型，四次 run 的 stderr 分别报 `验收：pass` / `partial` / `block` / `unverified` |
| SFT 的消息顺序、工具调用、产物引用 | E2E：`user` × 4（人的话与内核注入的 Goal / Rubric / 预算块）→ `assistant`（`Write` 调用）→ `tool`（同一个 `tool_call_id`）→ `assistant`；`artifacts` 是 `report.md` |
| RL：分词、loss mask、reward | `test_rl_row_masks_only_assistant_tokens_including_its_tool_calls`；E2E 三行 mask 与 id 等长、reward 分别是 1.0 / 0.5 / 0.0 |
| TRL / verl 最小 smoke | E2E `test_trl_reads_the_exported_sft_file`：3 行，分词后的列里有 `input_ids` |
| 一个任务失败不拖垮整批 | 导出：没有 Goal 的会话记进 `failed`、退出 1，其余三条照写；批处理：`test_one_batch_failure_does_not_drop_the_rest`、`test_a_run_that_exits_nonzero_is_a_failed_row_and_the_next_task_still_runs`，真进程 `test_syndica_batch_runs_every_good_line_and_reports_the_bad_one` |
| manifest 足够复现过滤条件 | `format`、`include_unverified`、`sources`、`dropped_unverified`、`dropped_secret`、`failed` |

## 实现中发现的问题

- `SessionMeta.last_trace` 从 M6 起就没有人写，新会话一律是 null。导出改为按根 Agent 的 id 在 trace store 里找；内核那一侧
  登记进路线图遗留表。
- 验收伴生 Agent 在同一个 trace 里，只取最后一个 `verdict.status` 会被它们的 span 干扰；裁决只读根 Agent 的 span。
- 整段 JSON 编码之后再脱敏，转义过的引号让规则只换掉密钥的后半截。改为先逐叶脱敏再编码；整段那遍仍保留，写入点守卫靠它
  认出这次写过了脱敏。
- 模型起的 Rubric id 在不同会话里可以指不同的陈述，偏好对按 Goal 与 Rubric 内容的摘要配对。
- `batch` 起初把一次以非零码结束、但没有抛异常的 run 记成成功。

## 变异

每条都按「`src` 的 sha256 校验 → 施加 → 清字节码 → 跑定向用例 → 还原 → 再校验」。第一轮 15 条里 3 条存活：

- 去掉逐叶脱敏：用例只查了密钥的后半截，整段那遍脱敏也能把它换掉。改成断言解码后的正文是完整的占位符。
- Rubric 摘要不看陈述：缺一条「同一个 id、不同陈述」的用例。补了摘要的单测与「不配对」的用例。
- 组合根忽略 run 的退出码：真进程用例里的任务都成功。补了直接驱动 `_Training` 的用例。

补完之后 15 条全部变红。「零存活」只在这组变异下成立；这一轮没有第二个人施加变异。

## 没做

| 条目 | 原因 |
|---|---|
| verl | 路线图要求 TRL 或 verl，选了 TRL |
| 真跑一步 TRL 训练 | TRL 1.15 的训练步导入时要 `triton`，macOS 上的 PyTorch 不带；dry-run 停在分词与标 label 之后 |
| 写起 `SessionMeta.last_trace` | 改内核的存盘语义，登记进路线图遗留表 |
| 对抗评审 | 2026-10-09 起不再起子代理 |
| 导出验证（`git archive`） | 改动还没提交 |

## 公开契约

见计划的「公开契约」一节：`syndica.trajectory/1` 的键序、SFT / ShareGPT / 偏好 / RL 行、manifest、`syndica batch`、
`syndica export`、`syndica[trl]`。`runner.training` 与 `runner.assemble` 没有进 `syndica.runner` 的包级导出。

## 验证

```text
uv run ruff check .                                          通过
uv run ruff format --check .                                 通过
uv run pyright                                               0 errors
uv run python scripts/check_deps.py                          通过
uv run python scripts/check_links.py                         通过
sh scripts/check_web.sh                                      通过
uv run pytest -q                                             10300 passed（813 秒，含 M10）
```

环境装了 `syndica[trl]`（`uv sync --all-extras`），所以 TRL 的两条用例真的跑了；没装时它们跳过。
