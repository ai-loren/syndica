# 里程碑 11 计划：训练出口

日期：2026-10-09
来源：[路线图](2026-09-23-v1-milestone-roadmap.md)「M11 训练出口」、[spec](../design/2026-09-03-architecture.md) §9.5
状态：已完成。首版提交为 `0b02601`；第二方收口评审（2 Blocking / 1 High / 7 Medium）的处置见决策日志 P2，在 `400ac7a`；
最终代码提交 `47c2dc2` 过了导出验证。

## 目标

把 Syndica 的工作轨迹转成训练数据：中立轨迹、SFT、偏好数据、RL 行，并对接 TRL。验收结论是训练信号；`unverified`
默认不进数据集。

## 架构

| 部件 | 位置 | 依赖 |
|---|---|---|
| 中立轨迹 schema 与导出器 | `runner/training.py` | 只有 kernel |
| 从四个 store 拼尝试 | `runner/assemble.py` | kernel 的 Store Protocol |
| `batch` 任务清单 | `runner/batch.py` | 只有 kernel |
| `export` 读工作区的文件 store | `runner/commands.py` | kernel 的文件 store |
| TRL dry-run | `backends/trl` | kernel；`trl`、`datasets`、`transformers` 只在函数体里导入 |
| CLI `batch` / `export` | `cli/main.py` 只解析参数，经 `TrainingControl` 交给组合根 | CLI 不 import runner |
| 组合根 `_Training` | `__main__.py` | runner；脱敏器取宿主的 `redactor_for(config)` |

分词器不进中立轨迹，也不进 runner。`export rl` 没有模型分词器时按 UTF-8 字节给 id（manifest 记 `tokenizer: utf8-bytes`），
换模型时由训练框架那一侧重分词。`check_deps` 拒绝 kernel、各 surface 与组合根 import `torch` / `trl` / `datasets` /
`transformers`，只有 backend 可以。

## 公开契约

- 中立轨迹 `syndica.trajectory/1`。顶层键序：`schema`、`id`、`goal_id`、`rubric_id`、`model`、`system_prompt`、`tools`、
  `messages`、`tool_calls`、`artifacts`、`verdict`、`verdicts`、`outcomes`、`source`、`causality`、`timing`、`redaction`。
  消息键序：`role`、`content`、`tool_call_id`、`tool_calls`、`trust`；工具调用键序：`id`、`name`、`arguments`；
  `source` 是 `trace_id` 与 `verdict_source`（`acceptance` / `none` / `missing`）；`redaction` 是 `digest`、
  `rules_version`、`placeholders_kept`、`raw_secrets_exported`。黄金文件 `tests/runner/testdata/trajectory.json`。
  `read_trajectory` 只认这一版，更新的 schema 三件套拒绝。
- `goal_id` 是 Goal 正文的摘要，`rubric_id` 是 active Rubric（id 加陈述）的摘要。偏好对只在同一 Goal、同一份 Rubric
  下配对；模型起的 Rubric id（`R1`）在不同会话里可以指不同的陈述，只按 id 配会把按不同要求验收的两次尝试配成一对。
- 格式与文件：`sft` → `messages.jsonl`，`sharegpt` → `sharegpt.jsonl`，`preference` → `preference.jsonl`，`rl` →
  `rl.jsonl`，`trajectory` → `trajectories.jsonl`；每个目录另有一份 `manifest.json`。
- SFT 行：`id`、`messages`（OpenAI 形状，assistant 带 `tool_calls`，tool 带 `tool_call_id`）、`artifacts`、`untrusted`、
  `redaction`。ShareGPT 行：`id`、`conversations`（`human` / `gpt` / `function_call` / `observation`）、`untrusted`、
  `redaction`。
- 偏好行：`goal_id`、`rubric_id`、`chosen_id`、`chosen_verdict`、`rejected_id`、`rejected_verdict`，对话形状的
  `prompt` / `chosen` / `rejected`（TRL 的 DPO 直接读），`untrusted`、`redaction`。排序 pass > partial > block，
  unverified 不进配对。
- RL 行：`input_ids`、`loss_mask`（assistant 正文与它的工具调用为 1）、`reward`（pass 1.0、partial 0.5、block 0.0、
  unverified 为 null：没能验证不等于失败）、`episode`（含 `verdicts` 与 `untrusted`）、`redaction`。
- manifest：`schema`、`format`、`include_unverified`、`sources`、`samples`（每条的 `id`、`verdict`、`verdicts`、
  `trace_id`、`verdict_source`、`untrusted`）、`dropped_unverified`、`dropped_secret`、`failed`、`skipped_traces`、
  `redaction`；`rl` 另有 `tokenizer`，偏好另有 `preference_pairs`。
- CLI：`syndica batch <dataset>`（每行 `id`、`prompt`，可选 `agent`、`goal`；别的键整行拒收；stdout 一行一个结果，
  有失败退出 1）；`syndica export {sft,sharegpt,preference,rl,trajectory} --output <目录> [--include-unverified]`。
  `--output` 必须在工作区之外、还不存在或是空目录、不是符号链接；有会话或 trace 读不出来退出 1，其余照写。
- trace：内核每给一次裁决，在当前 span 上记一个 `verdict` 事件（属性同 agent span 上的 `verdict.*`）。
- extra：`syndica[trl]`（`trl`、`datasets`）。

## 实现逻辑

- 裁决取根 Agent 的 `verdict` 事件，按时间排；更早的 trace 没有这个事件，退回 agent span 上的终态。验收伴生 Agent 的
  span 与回放写出的 trace（`source.replay_of` 不为空）不算。没有裁决记 `unverified`，`verdict_source` 分清「trace 上
  没有」与「找不到 trace」。
- `SessionMeta.last_trace` 宿主从来没写过，导出按根 Agent 的 id 在 trace store 里找它出现过的最近一个原始 trace。一个
  trace 读不出来只记进 `skipped_traces`。
- 消息照实导出：模型当时看到的 Goal / Rubric / 预算块是 user 消息，也在里面；同一会话里打回之前的回合也在里面，
  `verdicts` 记下先后的裁决，训练侧据此筛。trust 随每条消息导出，样本行带 `untrusted`。
- 脱敏用宿主的脱敏器：`.syndica.yaml` 的用户规则加导出这一刻环境里的密钥值。先逐叶脱敏（正文、system prompt、能解析
  成 JSON 的工具实参的每个叶子、产物字段），再编码，整段再过一遍；占位符保留。调用方点名的原文密钥在解码后的任何
  字符串里（含解析后的工具实参）出现，这个样本不写出，记进 `dropped_secret`。
- 文件以 0600 新建（`O_EXCL`、`O_NOFOLLOW`），一个目录一次导出。
- 一个会话拼不出来、一条尝试脱敏或建轨迹失败，记进 `failed`，其余照写。批处理里一条任务抛出或以非零码结束同样只记这一条。
- 导出只读四个 store，不改源 trace 与会话。

## 明确不做

- verl：路线图要求「TRL 或 verl」，选了 TRL。
- 真跑一步训练：TRL 1.15 的训练步走 fused log-prob，那个核导入时要 `triton`，macOS 上的 PyTorch 不带。dry-run 停在
  `SFTTrainer` 把数据集分词、标好 label 之后（工具调用经模板进了分词）。
- 把 `SessionMeta.last_trace` 写起来：改的是内核的存盘语义，登记到路线图遗留表。
- 按打回边界把一次会话切成几条尝试：先把裁决历史记下来，切不切由训练侧决定。
- `batch` 的并发、重试、结果行里的会话 id、Ctrl-C 时把已跑完的行先打出来。
- 工具的完整 schema：会话只存工具名。

## 验证

- 单测：`tests/runner/test_training_export.py`、`tests/runner/test_training_sources.py`、`tests/cli/test_training_commands.py`、
  `tests/cli/test_training_root.py`、`tests/backends/test_trl_dry_run.py`、`tests/test_deps.py`。
- E2E：`tests/integration/test_training_e2e.py`。一份任务清单经组合根的 `batch`、真的宿主与 Runtime 用脚本化模型跑出
  pass / partial / block / unverified，另有一条打回一次再过的会话与一条没有 Goal 的会话；经 `python -m syndica export`
  导出五种格式。导出时才有的环境密钥与 run 之后才加的用户规则都被换掉；一个读坏的 trace 不拖垮其余；导出到工作区里
  被拒绝；TRL 读进导出的 SFT 文件。`syndica batch` 对本地假模型端点跑一份带坏行的清单。
- 定向变异见[收口审计](../audits/2026-10-09-m11-final-audit.md)。

## 决策日志

- **P1（实现，2026-10-09）** 首版，提交 `0b02601`。
- **P2（收口评审处置，2026-10-09）** 第二方评审 2 Blocking / 1 High / 7 Medium / 若干 Low，全部处置或写进「明确不做」：
  - B1 CLI 导出用了裸 `RuleRedactor()`：改用宿主的 `redactor_for(config)` 与此刻环境的密钥值，manifest 记规则摘要。
  - B2 一个读不出的 trace 让整次导出一行不写：逐个 trace 兜住，记进 `skipped_traces`。
  - H1 点名密钥在 JSON 文本上找，转义之后漏掉：在解码后的字符串与解析后的工具实参上找。
  - 写盘跟随符号链接、0644、覆盖：改为 0600 新建、空目录、拒符号链接；一个目录一份 manifest 不再被覆盖。
  - 回放 trace 顶替原裁决：跳过 `replay_of` 不为空的头；每条样本记 `trace_id` 与 `verdict_source`。
  - 打回之前的回合被当正样本：内核加 `verdict` 事件，样本带 `verdicts`；切分留给训练侧。
  - trust 丢失、导出落在工作区里形成洗白路径：消息带 `trust`、行带 `untrusted`，`--output` 拒绝工作区之内。
  - schema 缺项：加 `trajectory` 格式、`read_trajectory`、`model` / `system_prompt` / `tools` / `outcomes` / `source`，
    每行带 `redaction`。
  - 训练依赖只靠一条 AST 检查：`check_deps` 拒绝 backend 之外 import 四个训练包，另有子进程用例。
  - Low：脱敏失败不再拖垮整批、裁决按时间取、unverified 的 reward 为 null、偏好行改成 DPO 形状、ShareGPT 三个并行
    调用、batch 透传 `--config` / `agent` / `goal` 并拒收拼错的键、导出失败时说明三件套。
