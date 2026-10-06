# Flag 参考

<!-- 由 scripts/gen_flag_reference.py 从内置 Module 的声明生成；改声明之后重新生成，不要手改。 -->

内置 Module 声明的全部 Flag，按 Module 的装配次序、再按各自声明的次序。
`.syndica.yaml` 的 `flags:` 与命令行的 `--flag key=value` 都按这里的类型与取值校验，
命令行覆盖配置；项目配置只能收紧。第三方 Module 的 Flag 用 `syndica flags list` 查看
（要先确认配置）。

| key | Module | 类型 | 缺省 | 取值 | CLI 的限制 | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| `max_wait_seconds` | `safety` | int | `0` | ≥ 0 | — | 单个 Agent 等待答复或另一个 Agent 的最长墙钟秒数；0 表示不限制 |
| `untrusted_high_risk` | `safety` | str | `"reject"` | `"reject"` / `"ask"` | 不收 allow；项目配置里只收 reject | 高风险调用可溯源到不可信内容时的处置 |
| `llm_retry_attempts` | `retry` | int | `0` | 0 到 10 | — | 初次调用失败后，对原 LLM 额外重试的次数 |
| `llm_retry_backoff_ms` | `retry` | int | `0` | 0 到 60000 | — | 每次重试或故障转移调用前等待的毫秒数 |
| `llm_fallbacks` | `retry` | str | `""` | — | 只收空值：按名字启用的 retry 没有备用模型 | 按顺序各尝试一次的 fallback LLM 名称，使用逗号分隔 |
| `prefix_drift_detection` | `prefix-drift` | bool | `true` | — | — | 逐轮检测模型请求的缓存前缀是否被改写 |
| `compaction_strategy` | `compaction` | str | `"off"` | `"off"` / `"rule_based"` / `"pruning"` / `"summary"` / `"goal_directed"` | — | 压缩策略：off / rule_based / pruning / summary / goal_directed |
| `compaction_threshold_percent` | `compaction` | int | `80` | 1 到 99 | — | 占用率到上下文窗口的百分之几时提前触发压缩 |
| `compaction_target_percent` | `compaction` | int | `50` | 1 到 99 | — | 压缩压到上下文窗口的百分之几（必须小于触发阈值，迟滞才成立） |
| `compaction_context_tokens` | `compaction` | int | `128000` | ≥ 1 | — | 模型不报上下文窗口时，按多少 token 估算窗口大小 |
| `memory_recall` | `memory` | bool | `true` | — | — | cycle 起点是否把长期记忆注入历史 |
| `memory_consolidate_after` | `memory` | int | `50` | ≥ 1 | — | 短期记忆累积多少条后整理成一段长期记忆 |
| `acceptance_rubrics` | `acceptance` | bool | `false` | — | — | 开工前把 Goal 投影成带锚点的 Rubric（一次伴生调用） |
| `acceptance_output` | `acceptance` | bool | `false` | — | — | L2 输出验收：完成时判断用户的要求是否都已做完（伴生，判定保守） |
| `acceptance_output_retries` | `acceptance` | int | `1` | ≥ 0 | — | L2 判没做完或交付物没过 L1 时最多打回几次；0 = 只记裁决 |
| `acceptance_outcome` | `acceptance` | bool | `false` | — | — | L3 结果验收：完成时按 Rubric 逐条查回复与工作区证据（伴生） |
| `acceptance_outcome_block_retries` | `acceptance` | int | `2` | ≥ 0 | — | L3 判 block（有证据表明做错了）时最多打回几次；0 = 只记裁决 |
| `acceptance_outcome_partial_retries` | `acceptance` | int | `1` | ≥ 0 | — | L3 判 partial（没做完或没能确认）时最多打回几次；0 = 只记裁决 |
| `acceptance_feedback` | `acceptance` | bool | `false` | — | — | 把 Goal 之后人发来的消息提议成 proposed Rubric，人确认后才生效 |
| `acceptance_verifier_timeout` | `acceptance` | int | `300` | ≥ 1 | — | 一次验收（L2 + L3 合计）或投影的时限，秒；超时放行并标 unverified |
| `acceptance_verifier_max_turns` | `acceptance` | int | `8` | ≥ 1 | — | 每个伴生 Agent（投影、L2、L3、反馈提议）各自的轮次上限 |
| `long_horizon_progress_every` | `long-horizon` | int | `10` | ≥ 0 | — | 每隔几轮跑一次里程碑 L3 量进度；0 = 只在完成时量 |
| `long_horizon_stall_turns` | `long-horizon` | int | `20` | ≥ 0 | — | 连续几轮没有新的验收条被满足就算停滞、请策略纠偏；0 = 不检测 |
| `long_horizon_budget_low_percent` | `long-horizon` | int | `20` | 0 到 99 | — | 预算剩不到百分之几时问策略怎么分配；0 = 不问 |
| `long_horizon_drift_window` | `long-horizon` | int | `8` | ≥ 0 | — | 每隔几轮检查一次最近的动作还锚不锚得回 Goal；0 = 不检查 |
| `long_horizon_semantic_drift` | `long-horizon` | bool | `false` | — | — | 锚点检查可疑时再做一次语义比对确认（一次伴生调用） |
| `long_horizon_max_continuations` | `long-horizon` | int | `3` | ≥ 0 | — | 验收没过、打回也用完之后，策略最多让 Agent 接着干几次（每个周期） |
