# 里程碑 1 最终审计

日期：2026-09-23
基线：计划 6 收口提交 `9604831`
代码修复：`26f8a3c`
审计记录：本文所在提交

## 结论

里程碑 1 完成。spec §15 的全部判据已有生产实现和可执行证据；D-51、D-27、D-58、D-59、
V-01、V-02 已关闭。登记表中不再有归里程碑 1 的 OPEN / VERIFY 项。

本次不引入 `Runtime.start` / `Junction.RUNTIME_START`，不扩大公开 API，不处理
`BEYOND-M1` 条目，也没有 push。

## Spec §15

| 判据 | 实现与证据 |
|---|---|
| Runtime 创建多个 Agent、互发消息 | `tests/integration/test_multi_agent_lifecycle.py` |
| 各自跑通 LLM + 工具循环 | `tests/integration/test_single_agent_turn.py`、`tests/integration/test_local_tools_topology.py` |
| 可终止与恢复 | `Runtime.kill` / `despawn` / `abort` / `ensure_active`；`tests/kernel/test_runtime.py` |
| 声明式 Agent 定义 | `AgentManifest` / `AgentCatalog`；`tests/kernel/test_manifest.py`、`test_catalog.py`、`test_builtin_agents.py` |
| 拓扑装配 | `Runtime.assemble` / `resolve` / `occupy` / `send_to_slot`；`tests/integration/test_topology_lifecycle.py` |
| Tool 系统与 LLM 抽象 | `Tool` / `ToolExecutor` / `LLM` / `OpenAICompatible` 的单测与契约测试 |
| 最小本地执行 | Read / Write / Edit / Glob / Grep / Bash / BashOutput / KillBash；`tests/integration/test_local_tools_topology.py` |
| CLI、人机交互与 L2 回放 | `tests/integration/test_cli_human_scenario.py` 的三项验收 |

## 遗留项

| 条目 | 审计结论 |
|---|---|
| D-51 | 保留按需换表语义；`AgentOptions.tools` 已写明宿主必须通过 `agent.options.tools` 热插拔。三条定向用例通过 |
| D-27 | 保留证据日志级别；README 新增宿主阈值、去重、采样与限流规则。五条定向用例通过 |
| D-58 | spec §5.4 与实现对齐，明确不提供异步启动入口。公开面与 assemble 守卫 3 passed |
| D-59 | 尾部未配对批次扫描收敛为一个纯函数。六个真实变异全部被杀死，独立复审 APPROVE |
| V-01 | 文件树导入、动态异常发现、确切清单和三种往返证据 20 passed |
| V-02 | 本进程活驱动与 ZOMBIE CAS 分工明确；多状态、所有权、关机边界证据 7 passed |

## 验证

```text
uv run ruff format --check .                         110 files already formatted
uv run ruff check .                                  All checks passed
uv run pyright                                       0 errors
uv run python scripts/check_deps.py                  passed
uv run pytest tests/kernel -q                        2023 passed
uv run pytest tests/runner -q                        23 passed
uv run pytest tests/cli -q                           73 passed
uv run pytest tests/integration -q                   8 passed
uv run pytest tests/integration/test_cli_human_scenario.py -q
                                                       3 passed
uv run pytest -q                                     2245 passed
```

控制台取证：

```text
python -m syndica version                            syndica 0.0.0
python -m syndica agents list                        4 个内置角色
python -m syndica topology list                      成功，默认配置为空
python -m syndica flags list                         2 个 Flag
python -m syndica doctor                             缺模型环境变量时三件套并退出 1
python -m syndica doctor（注入占位模型配置）          config/models/workspace 均 ok
```

## 评审

D-59 首轮独立审查发现两个 P2：结构守卫可被伪调用绕过、连续尾部 tool block 未覆盖。补上独立
文件树范围校验、消费者字段访问约束，并把恢复固定装置扩为两个连续已配对结果；对应真实变异均被
杀死，复审 APPROVE。

最终分支级独立审查覆盖 `origin/main..main` 与本审计文档，未发现阻塞 M1 结论的 P0 / P1 / P2，
结论为 APPROVE；残余风险均已保留在登记表 `BEYOND-M1`。
