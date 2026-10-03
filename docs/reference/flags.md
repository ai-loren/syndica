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
