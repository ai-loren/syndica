# 里程碑 2 阶段二审计：开发工具

日期：2026-10-01
范围：[M2 计划](../plans/2026-09-30-m2-self-hosting.md) 的 T7–T14
基线：T7 `6608fd5`、T8 `643d68b`（`check_deps` 修复 `5f729ad`）、T9 `6a0a647`、T10 `e6362c3`、T11 `cdc5d6d`、
T12 `9d95d45`、T13 `4fd4875`、T14 `2a9e5d8`；审计补的用例与本文同一提交
验证方式：本地验证协议（Actions 额度用完期间，见计划「验证」一节）

## 结论

阶段二的开发工具在它们该生效的路径上都生效，计划「公开契约」一节逐条对过实现，没有分歧。审计找出一处
缺口：**`run --handoff --topology` 没有任何用例**，而 T15 的 `syndica-dev` 正是一个拓扑、T18 的真实运行走的
就是这条路。补了两条经公开 CLI 的用例——拓扑 run 的交接与单 Agent run 一样（Reviewer 结论、改动列表、文件与
stdout 逐字相同），拓扑根槽用完轮数时按 run 失败——并用两条变异证明它们守得住：拓扑路径上不收 `dev_log`
（Reviewer 结论随之变成 `unverified`），以及不收根槽的结局（一个用完轮数、但留下了半句话的根 Agent 会被当成
正常完成）。后一条在补用例之前是存活的。

## 开发工具 × 路径

| 工具 | run | 拓扑 run | chat | doctor |
|---|---|---|---|---|
| `--no-write`（T7） | `test_a_worker_asking_to_write_or_run_a_command_gets_an_unknown_tool_and_nothing_changes` 等 | `test_a_topology_slot_cannot_add_a_write_tool_back`、`test_a_worker_delegated_from_a_topology_root_is_read_only_too` | `test_a_session_cannot_be_turned_read_only`（退出 2） | 不收这个参数 |
| 开发期 Module：记录、指纹、拦截（T9） | 交接用例读它的 `dev_log` | `test_a_topology_run_hands_off_like_a_single_agent_run` | `test_a_chat_session_uses_the_same_policy_and_guard` | 不建 Runtime |
| 检查结果解析（T8） | `meta['check']`；交接复核 | 同 run | 同 run | `doctor --verify` 的摘要 |
| 交接（T11） | `tests/cli/test_handoff_run.py` | 上面两条新用例 | `--handoff` 与 `--session` 互斥（退出 2） | — |
| git 收尾（T12） | `tests/cli/test_git_finish.py`（经交接） | 与 run 共用收尾 | 不做 | — |
| 脱敏（T10） | 交接文件、stdout、stderr、提交消息、git 收尾的问题 | 同 run | 不落盘 | 输出经脱敏 |
| 链接检查（T14） | CI 第六道门禁；T15 进本仓 `workspace.verify` | — | — | — |

## 由构造保证的格子

- **三条路径装的是同一个开发期 Module。** run、拓扑 run、chat 都经 `_agent_modules` 拿到 Module 列表，开发期
  Module 的两个插件对每个 Agent（含派活、Fork 与拓扑槽建出的）都装一次。
- **拓扑与单 Agent 的交接走同一段收尾。** `_run_request` 在分支之前收 `dev_log`、在等待之后收根 Agent 的结局；
  现在两条路各有用例，两处各有一条变异钉住。
- **CLI 包里的每个写入点都经脱敏或是常量。** 由 T10 的 AST 守卫保证；T12 起它也检查 `communicate(input=...)`，
  宿主自己起的提交子进程因此也在守卫之内。
- **doctor 不执行项目代码。** `doctor` 只用 `command -v` 找程序，解析到工作区里的不跑（T13）；只有
  `run --handoff` 与 `doctor --verify` 执行 `workspace.verify`（D15）。

## 公开契约核对

逐条对实现 grep：CLI 的三个新用法与它们的互斥（`--no-write` 与 `--session` / `--git-mode`、`--git-mode` 只与
`--handoff` 同用）；退出码 0 / 1 / 2 / 3 与「1 先于 3」；三个危险规则 ID 与十个脱敏规则 ID；两条标记行的语法
（`splitlines`、整行匹配、`\s`、值只认 ASCII）；Bash 与 BashOutput 的 `meta` 键、`meta['check']` 的键序；
`runtime.dev_log`；交接文件 front matter 的键、取值与键序；内核 API 增量（`freeze` / `assemble` 的两个关键字参数、
`ExecResult.timed_out`、`local_tools(output_limit=)`、`RuleTablePolicy` 与三个规则工厂不进 `kernel.__all__`）。
没有分歧。

## 遗留

- 额度恢复后在 Linux 上对 HEAD 补跑 CI：T7–T14 都只在本机验证过，`/bin/dash` 近似 Ubuntu 的 `/bin/sh`。
- T9：被拦下的调用不记进 `dev_log`；后台命令按 handle 字符串去重；被取消的 git 线程最多还会跑 60 秒。
- T11：Agent 改得到 `.syndica/` 里以前的交接文件（这次 run 的那一份由锁与 0600 写入护着）；`_HASH_BATCH` 的
  批大小与复核的 900 秒上限没有用例。
- T12：逃过进程组清理的进程（`setsid`）在最后一次复查与提交 / 推送之间仍有一瞬间的窗口；钩子在 Agent 的环境里
  照样读得到 Agent 读得到的文件（D5）；推这次提交的 sha 而不是 HEAD 是一条等价变异，只防那一瞬间的竞争。
- T13：带 shell 语法的 verify 命令里用到的程序不单独检查（写成 unchecked），由 `doctor --verify` 真的跑一遍
  兜底。
- T14：链接检查认不出缩进代码块与列表项里没关上的围栏，写在脚本开头。
- 本仓之外：agent-adk 仓库 AGENTS.md 说命令行 `-c trace2.eventtarget=` 能关掉改写提交消息的进程，git 2.50
  实测它盖不过全局配置里的事件目标（见计划「意外发现」），已告知用户。

## 验证

```text
uv run ruff format --check .                                 145 files already formatted
uv run ruff check .                                          All checks passed
uv run pyright                                               0 errors
uv run python scripts/check_deps.py                          passed
uv run python scripts/check_links.py                         passed
uv run pytest -q                                             4753 passed
SYNDICA_TEST_SHELL=/bin/dash uv run pytest -q                4751 passed（补用例之前；两条新用例另在 dash 下跑过）
```

T7–T14 各自的变异与对抗评审见计划里每个任务的状态；审计补的两条用例各有一条变异杀死。
