# 里程碑 11 计划：训练出口

日期：2026-10-09
来源：[路线图](2026-09-23-v1-milestone-roadmap.md)「M11 训练出口」、[spec](../design/2026-09-03-architecture.md) §9.5
状态：实现与本地验证已完成，未提交。本计划没有经过单独的对抗评审（2026-10-09 起不再起子代理），收口时由定向变异代替。

## 目标

把 Syndica 的工作轨迹转成训练数据：中立轨迹、SFT、偏好数据、RL 行，并对接 TRL。验收结论是训练信号；`unverified`
默认不进数据集。

## 架构

| 部件 | 位置 | 依赖 |
|---|---|---|
| 中立轨迹 schema 与三种导出器 | `runner/training.py` | 只有 kernel |
| 从四个 store 拼尝试 | `runner/assemble.py` | kernel 的 Store Protocol |
| `batch` 任务清单 | `runner/batch.py` | 只有 kernel |
| `export` 读工作区的文件 store | `runner/commands.py` | kernel 的文件 store |
| TRL dry-run | `backends/trl` | kernel；`trl`、`datasets`、`transformers` 只在函数体里导入 |
| CLI `batch` / `export` | `cli/main.py` 只解析参数，经 `TrainingControl` 交给组合根 | CLI 不 import runner |
| 组合根 `_Training` | `__main__.py` | runner |

分词器不进中立轨迹，也不进 runner。`export rl` 没有模型分词器时按 UTF-8 字节给 id，换模型时由训练框架那一侧重分词。

## 公开契约

- 中立轨迹 `syndica.trajectory/1`。顶层键序：`schema`、`id`、`goal_id`、`rubric_id`、`messages`、`tool_calls`、
  `artifacts`、`verdict`、`causality`、`timing`、`redaction`。消息键序：`role`、`content`、`tool_call_id`、`tool_calls`；
  工具调用键序：`id`、`name`、`arguments`。黄金文件 `tests/runner/testdata/trajectory.json`。
- `goal_id` 是 Goal 正文的摘要，`rubric_id` 是 active Rubric（id 加陈述）的摘要。偏好对只在同一 Goal、同一份 Rubric
  下配对；模型起的 Rubric id（`R1`）在不同会话里可以指不同的陈述，只按 id 配会把按不同要求验收的两次尝试配成一对。
- SFT 行：`{"id", "messages", "artifacts"}`，消息是 OpenAI 形状（assistant 带 `tool_calls`，tool 带 `tool_call_id`）。
  ShareGPT 行：`{"id", "conversations"}`，`from` 取 `human` / `gpt` / `function_call` / `observation`。
- 偏好行：`goal_id`、`rubric_id`、`chosen`、`rejected`（各带 `id` 与 `verdict`），排序 pass > partial > block。
- RL 行：`input_ids`、`loss_mask`（assistant 正文与它的工具调用为 1）、`reward`（pass 1.0、partial 0.5、block 与
  unverified 0.0）、`episode`。
- manifest：`schema`、`include_unverified`、`sources`、`dropped_unverified`、`dropped_secret`、`failed`、`redaction`、
  `format`（偏好另有 `preference_pairs`）。
- CLI：`syndica batch <dataset>`（每行 `{"id","prompt"}`，stdout 一行一个结果，有失败退出 1）；
  `syndica export {sft,sharegpt,preference,rl} --output <目录> [--include-unverified]`（有会话拼不出来退出 1，其余照写）。
- extra：`syndica[trl]`（`trl`、`datasets`）。

## 实现逻辑

- 裁决取 trace 里根 Agent 的 span 上最后一次 `verdict.status`；验收伴生 Agent 的 span 不算。没有裁决记 `unverified`。
- `SessionMeta.last_trace` 宿主从来没写过，导出按根 Agent 的 id 在 trace store 里找它出现过的最近一个 trace。
- 消息照实导出：模型当时看到的 Goal / Rubric / 预算块是 user 消息，也在里面。
- 落盘之前逐叶脱敏，再编码；占位符保留。调用方交来的原文密钥若仍在样本里，这个样本不写出，记进 `dropped_secret`。
- 一个会话拼不出来（没有 Goal、正文读不回）记进 `failed`，其余照写。批处理里一条任务抛出或以非零码结束同样只记这一条。
- 导出只读四个 store，不改源 trace 与会话。

## 明确不做

- verl：路线图要求「TRL 或 verl」，选了 TRL。
- 真跑一步训练：TRL 1.15 的训练步走 fused log-prob，那个核导入时要 `triton`，macOS 上的 PyTorch 不带。dry-run 停在
  `SFTTrainer` 把数据集分词、标好 label 之后。
- 把 `SessionMeta.last_trace` 写起来：改的是内核的存盘语义，登记到路线图遗留表。
- `batch` 的并发与重试：按清单顺序一条一条跑。

## 验证

- 单测：`tests/runner/test_training_export.py`、`tests/runner/test_training_sources.py`、`tests/cli/test_training_commands.py`、
  `tests/cli/test_training_root.py`、`tests/backends/test_trl_dry_run.py`。
- E2E：`tests/integration/test_training_e2e.py`。真的宿主与 Runtime 用脚本化模型跑出 pass / partial / block /
  unverified，经 `python -m syndica export` 导出四种格式，TRL 读进导出的 SFT 文件；`syndica batch` 对本地假模型端点
  跑一份带坏行的清单。
- 定向变异 15 条，见[收口审计](../audits/2026-10-09-m11-final-audit.md)。
