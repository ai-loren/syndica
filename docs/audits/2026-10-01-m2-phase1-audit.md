# 里程碑 2 阶段一审计：护栏

日期：2026-10-01
范围：[M2 计划](../plans/2026-09-30-m2-self-hosting.md) 的 T1–T6
基线：T1 `ce625ce` `cc25006`、T2 `49c867a`、T3 `23cdaa1`、T4 `2f76738`、T5 `e5974a6`；T6 与本文同一提交
验证方式：本地验证协议（Actions 额度用完期间，见计划「验证」一节）

## 结论

阶段一的护栏在 run、chat、doctor、拓扑四条路径上都生效。每一格要么有经公开 CLI 与脚本化 LLM 的用例，
要么由构造保证、理由写在下面。审计与 T6 的对抗评审一共找出三处缺口，都已修掉并有用例守着：

1. **Bash 的 `cwd` 指进 `.git` 时，git 状态可以被改而没有任何一道闸出声**（T6 评审，Critical）。危险规则只看
   命令文本，从 `.git` 里出发的相对路径不带 `.git` 这一段。改为内核 `LocalWorkspace` 按目录身份拒绝这样的
   `cwd`，与拒写 `.git/` 同一套判断（计划 D9）。
2. **`SYNDICA_TEST_SHELL=/bin/dash` 没作用到经 CLI 的用例。** CLI 宿主把 Agent 的 shell 写死成 `/bin/sh`，
   本机那是 bash 3.2，T5 的 dash 复跑对 CLI 用例其实没换 shell。改为宿主一个常量、测试侧自动装置换掉它，
   另有一条子进程用例证明换得过去。
3. **拓扑路径上的命令环境与输出截断没有用例。** 补了一条，并用一条变异证明它守得住（拓扑装配改用不带
   `output_limit` 的工具表时变红）。

## 护栏 × 路径

用例文件：`tests/cli/test_environment.py`（环境）、`tests/cli/test_dev_policy.py`（策略与拦截）、
`tests/kernel/test_local_workspace.py`（执行）。

| 护栏 | run | chat | doctor | 拓扑 |
|---|---|---|---|---|
| 命令环境：白名单、密钥名拒收、宿主另设的六个值（T5） | `test_the_agent_sees_the_whitelist_and_pass_env_but_no_secret_or_unlisted_variable` | `test_run_chat_and_doctor_hand_the_agent_the_same_environment` | 同左，比对 `env:` 行 | `test_a_topology_run_hands_its_slot_the_same_environment_and_output_limit` |
| 临时目录收尾（T5） | `test_run_and_doctor_leave_no_scratch_directory_behind` | `test_chat_removes_its_scratch_directory_when_the_host_closes` | 同 run | 每条 CLI 用例的自动装置 `no_agent_scratch_left_behind` |
| 每条流 10 000 字符截断（T4、T5） | `test_the_cli_clips_each_bash_stream_at_ten_thousand_characters` | `test_chat_clips_each_bash_stream_at_ten_thousand_characters_too` | 不执行工具 | 同拓扑环境用例 |
| 开发期判定与处置（T2、T3、T6） | `test_each_outside_read_in_one_trusted_batch_is_confirmed_on_its_own` 等 | `test_a_chat_session_uses_the_same_policy_and_guard` | 冻结拿到同一对选项（见下） | `test_a_topology_run_uses_the_same_policy_and_guard` |
| git 写命令与删根无条件拦截（T6） | `test_a_git_write_or_root_removal_is_blocked_and_the_agent_carries_on` | 同上 chat 用例 | 不建 Runtime | 同上拓扑用例 |
| 拒收 `untrusted_high_risk=allow`（T6） | `test_letting_untrusted_high_risk_calls_through_is_refused` | chat 不收 `--flag` | 不收 `--flag` | 拓扑是 run 的分支，Flag 在分支之前解析 |
| 拒写 `.git/`、命令不在 `.git` 里运行（T4、T6） | `test_a_command_run_from_inside_dot_git_is_refused_before_it_runs`；内核 `test_a_command_cannot_run_inside_dot_git` 等 | 同一个 `LocalWorkspace` | 不执行工具 | 同一个 `LocalWorkspace` |
| stdin 接 `/dev/null`、进程组清理、超时交回部分输出（T4） | 内核 `LocalWorkspace` 的用例 | 同左 | 同左 | 同左 |

## 由构造保证的格子

- **doctor 的冻结不传开发期选项是等价变异。** doctor 不建 Runtime、不执行工具；两者是常量对象，冻结只校验
  类型。doctor 的职责是「run 会建出的东西在这里也建得出」，冻结时传同一对选项保住了这一点。
- **子 Agent 都装上拦截插件。** 派活、Fork、拓扑槽都经 `Runtime.spawn` → `install_agent`，开发期 Module
  的插件工厂对每个 Agent 都执行一次。
- **守卫看到的是最终参数。** 可拦截切点按注册顺序串行、前面改过的参数传给后面；开发期 Module 排在 run 与
  chat 的 Module 最后（`_agent_modules`），CLI 不装 Runtime 级插件。目前没有任何插件改工具参数，所以把它挪到
  第一位是等价变异；约束写在 `_agent_modules` 上。
- **`allow` 没有旁路。** 只有 `run` 收 `--flag`；配置文件、环境变量、`--session`、拓扑声明、目录清单、派活
  工具都设不了 Flag，子 Agent 继承父 Agent 的 Flag。`flags show` 也不再把 `allow` 列成可选值。
- **T4 的执行护栏在四条路径上是同一份代码。** 四条路径都经 `_open_workspace` 建 `LocalWorkspace`，工具表都
  来自同一次 `local_tools(...)`；生产入口 `python -m syndica` 构造的也是 `RuntimeCliHost`。

## 遗留

- 额度恢复后在 Linux 上对 HEAD 补跑 CI（T1–T6 都只在本机验证过，`/bin/dash` 近似 Ubuntu 的 `/bin/sh`）。
- 默认规则 `shell.remote_script_execution` 漏掉 `curl … | sh -s -- foo` 这类写法（T6 评审顺带发现），紧接着
  单独提交修复。
- M5：`setsid` / `setpgid` 逃出进程组清理、组号复用的窄窗口、`HOME` 下的凭证文件与登录钥匙串（计划 D5）；
  `.git` 是符号链接或 `gitdir:` 指针时，真正的 git 目录以它自己的名字出现，拒写与拒绝 `cwd` 都认不出。
- T18：README 与 runbook 写清 CONFIRM 只在可信轨迹里到得了人手上，之后按 `untrusted_high_risk` 走（计划 D4）。

## 验证

```text
uv run ruff format --check .                                 124 files already formatted
uv run ruff check .                                          All checks passed
uv run pyright                                               0 errors
uv run python scripts/check_deps.py                          passed
uv run pytest -q                                             3305 passed
SYNDICA_TEST_SHELL=/bin/dash uv run pytest -q                3305 passed
```

T6 的变异两轮共 27 条杀死、3 条等价（见计划 T6 的状态）；审计补的拓扑用例另有 1 条变异杀死。
