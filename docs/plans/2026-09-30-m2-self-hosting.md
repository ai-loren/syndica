# 里程碑 2：自举（syndica-dev）

> **执行者须知：本文件是需求，不是参考。** 「公开契约」一节里的 CLI 参数、配置键、规则 ID、标记行、
> front matter 字段与内核 API 一旦发布就是兼容承诺；执行中不得为了让测试变绿临时改名或缩小断言范围。

**状态：已评审，执行中（T1–T2 完成）。** 五项裁决由用户确认；计划经作者自审与两轮独立评审修订（2026-09-30、
2026-10-01，见「决策日志」）。GitHub Actions 本月免费额度已用完，恢复之前按「验证」一节的本地验证协议执行。

**Goal:** 让 Syndica 在一份仓库里完成一次受控修改：读约束、规划、改文件、跑检查、读失败、迭代修复，
最后交出一份可审计的 handoff（交接报告）。检查结果由宿主独立复核，不采信 Agent 的自述；复核通过且
Reviewer 给出 pass 时，由人选择是否让宿主提交、推送。护栏（CI、危险规则、命令环境）先于任何让 Agent
改仓库代码的能力落地。

**宿主**指运行 Agent 的 syndica 进程本身（这里是 CLI）：它建工作区、装配 Agent、转发人的输入、弹出确认，
代码是写死的，不受模型影响。

**Architecture:** 自举能力落在 CLI surface 与本仓的项目配置（`.syndica.yaml` + 提示词）上。内核补三处
通用缺口：宿主在冻结目录、装配拓扑时注入危险判定与处置；三族可选危险规则；命令执行的进程组清理、超时、
结构化结果、输出上限、stdin 与 `.git/` 保护。开发期的检查结构化与无条件拦截是一个 CLI 侧 Runtime Module。
复核、handoff 与 git 收尾由宿主执行，handoff 经最小脱敏器写进 `.syndica/handoffs/`。

**Tech Stack:** 不新增运行依赖。CI 用 GitHub Actions 与 `astral-sh/setup-uv`。

**依据：** spec §15（M2 完成判据）、§5.5、§5.6、§6、§9.1、§12；路线图「M2 自举」；`AGENTS.md`。

---

## 现状校准（2026-09-30 调研，10-01 补充）

路线图写于 M1 收口时。开工前对照代码核实，下面这些事实改变了 M2 的做法（两轮独立评审逐条复核，行号以
当时代码为准）：

1. **Agent 的 Bash 跑不了本仓检查。** CLI 宿主建工作区时传 `env={}`，子进程环境被整个替换：`uv` 找不到，
   `HOME` 未设置，`python3` 解析到系统的 `/usr/bin/python3`。
2. **Bash 不受工作区约束。** 文件工具有路径监狱，Bash 以用户身份执行 `/bin/sh -c`。默认规则只有 4 条
   （提权、下载即执行、递归强删、写裸设备），`cat ~/.config/syndica/gemini.env` 不会被拦。
3. **宿主换不了处置策略。** 危险判定与处置只挂在 `AgentOptions` 上；`AgentCatalog.freeze` 与
   `Runtime.assemble`（内部调 freeze）构造选项时不接收它们。
4. **跑过命令之后，高风险调用一律按 `untrusted_high_risk` 处置。** Bash 输出标 `UNTRUSTED`；
   `provenance_of` 是「轨迹里出现过」的上界，且随派活带给子 Agent（`query_trust`）；同一批工具调用共用
   一次溯源快照。`disposition_of` 对「高风险 + 不可信」直接用这个 Flag 的处置、不问宿主策略：`reject`
   一律拦下，`ask` 一律问人，`allow` 一律放行——后两者都比宿主策略更松。`PRE_TOOL_USE` 的 `skip` 若交回
   `ToolResult` 就原样使用，交回别的值则按不可信包装。
5. **命令执行的进程管理有缺陷。** 超时时 `LocalWorkspace.run` 抛出三件套错误，已产生的输出丢弃；命令留下
   占着输出管道的后台子进程时（`sleep 4 &`），调用要等那个子进程结束才返回——实测超时 1 秒的调用 4 秒后
   才返回；`close()` 杀不到这类子进程（实测关闭后仍在运行）。子进程不设 `stdin`，会和 CLI 抢读终端输入。
   输出从不截断（`truncated` 恒为 False）。Bash 前台结果没有 `meta`，退出码只在输出文本里。前台默认超时
   30 秒，本仓全量测试约 25 秒。
6. **现有分词器不能直接拿来判路径。** `_segments` 丢掉重定向目标（`cat foo > /etc/x` 只剩 `cat foo`）；
   `_WRAPPERS` 之后的每个词都算命令词槽（`timeout 60 cat /etc/passwd` 四个词全是槽）——这对「找 git」是
   安全的方向，对「豁免程序词」是危险的方向；`uv run git push` 的命令词只有 `uv`。
7. **Reviewer 要靠同名覆盖 Orchestrator 才进得了派活名单。** `compose_system_prompt` 取
   `catalog.get(cls.__name__)`；`Delegate` 能派给目录里任何角色；`assemble` 冻结整个目录；CLI 以
   `orchestration=True` 建 Runtime，任何角色都能派活。`AGENT_SPAWN` 只带 `agent_id` 与 `parent_id`，且在
   子 Agent 的任务建好之后才触发；`AGENT_END` 在 `COMPLETED` 以外的结局也会触发。
8. **CLI 配置只认 `version` / `agents` / `topologies`；`CliHost` 返回 `str` 就退出 0。** 打印 handoff 又
   以非零码退出，在现有协议里表达不了。`.gitignore` 已忽略 `.syndica/`。
9. **没有 CI。** 仓里没有 `.github/`；`gh` 已登录（keyring），这也意味着拿到真实 HOME 的 Agent 能用 `gh`。
10. **无人值守不会卡在确认上。** CLI 在 stdin EOF 时把确认按 reject 回答。确认提示直接打印参数的 JSON，
    C1 控制字符与双向文字覆盖字符原样到达终端。
11. **L2 有走公开 CLI 的现成路径。** `RuntimeCliHost(model_factory=...)` 加 `main(argv, host, io)` 只换
    LLM。`runner/scenario.py` 用默认规则、自己的策略与空环境，不适合验收开发期策略。调用未注册的工具得到
    一条「未知工具」结果。
12. **框架目前没有任何落盘路径。** handoff 文件与宿主提交会是最早的两条，都要先过脱敏。
13. **本机 git 没有任何 trace2 配置。** 不需要为宿主提交做 trace2 相关的处理。
14. **本机全局 git 配置 `core.hooksPath=~/.git-hooks`，其中 `pre-push` 属主 root、权限 0777。** 宿主推送会
    执行它；Agent 与宿主的 git 环境不同时，两边看到的 hooks 目录也不同。

## 路线图条目对照

| 路线图 TODO | 本计划 |
|---|---|
| `syndica-dev` 拓扑与 manifest | T15；放在本仓项目配置（D17） |
| 提示词，明确禁止破坏 git 状态 | T15；git 写命令由钩子无条件拦下（T6），提交与推送由宿主执行（T12） |
| run 的开发者配置：工作区 root | 不加参数，工作区仍是 cwd（D21） |
| run 的开发者配置：允许的命令 | 不做命令白名单（D7） |
| run 的开发者配置：默认验证命令 | `workspace.verify`（T5），宿主复核（T11） |
| run 的开发者配置：是否允许写文件 | `run --no-write`（T7） |
| doctor 子项 | T13 |
| 安全命令策略 | T3（规则）+ T6（处置与无条件拦截） |
| 测试结果解析器 | T8（解析）+ T9（接进工具结果） |
| handoff 生成器 | T10（脱敏）+ T11（复核与 handoff） |
| `--no-write` dry-run | T7 |
| 基础 CI | T1，先于一切 |

与路线图不一致之处（决策日志里有理由）：越界路径默认问人而不是直接拒绝；handoff 的「下一步」由宿主生成。

---

## 已确认边界

### 做

- 基础 CI，排在所有任务之前。
- 内核补口：冻结与装配时注入危险判定和处置；三族可选危险规则与按表处置的策略；命令执行的进程组清理、
  超时返回部分输出、结构化结果、按流截断、stdin 接 `/dev/null`、文件工具拒写 `.git/`。
- CLI：Agent 命令环境；`workspace` 配置段；开发期处置策略与无条件拦截；`run --no-write`；
  `run --handoff`；git 收尾；`doctor` 扩展；`CliOutcome`；根 Agent 失败时报错去嵌套；所有交互提示转义。
- 检查结果解析器、开发期 Runtime Module、最小脱敏器。
- 本仓项目配置：`syndica-dev` 拓扑、Reviewer、开发期提示词、受保护路径；`scripts/check_links.py`。
- L2 E2E（公开 CLI + 脚本化 LLM）七个场景；真实模型 E2E（手动，仓库副本里）四个场景；M2 收口审计。

### 不做

- 沙箱、容器、网络访问控制：M5。M2 的防护是尽力而为，边界见 D5。
- 命令白名单（D7）。
- 统一 `Redactor` 协议与 trace / 会话 / 记忆的脱敏：M3。
- `Runtime.start()` 或任何新的生命周期入口。
- Artifact、Verifier、四值裁决的内核实现：M7。
- `--workspace` 参数（D21）。
- CI 矩阵：M12。

### TDD 接缝

测试只落在公开接缝上，不测私有 helper：

1. `main(argv, host, io)` 与 `RuntimeCliHost(model_factory=...)`：CLI 输入、stdout / stderr、退出码。
2. `DangerJudge(rules).judge(tool, args)`、`disposition_of(...)`、`RuleTablePolicy`。
3. `AgentCatalog.freeze(...)` / `Runtime.assemble(...)`：通过「一次命中规则的调用被这样处置」观察注入。
4. `local_tools(portal, ...)` 与 `LocalWorkspace` 的公开方法。
5. `load_config(path)`；检查解析器与脱敏器的纯函数。
6. 源码级守卫（AST 扫描）：只用于表达「某类写法不存在」这种无法从行为观察的约束。

---

## 设计裁决

### 护栏与内核

**D1 护栏先行。** 顺序固定为 CI（T1）→ 内核补口（T2–T4）→ CLI 护栏（T5、T6）→ 其余；T6 与 T14 之后各做
一次阶段审计。本计划自己的每个提交也要过同一套门禁：CI 可用时由 CI 复查，额度用完期间按本地验证协议。

**D2 危险选项在冻结与装配时注入；按表处置的策略进内核。** `AgentCatalog.freeze(tools, models,
host=None, *, danger_judge=None, danger_policy=None)` 与 `Runtime.assemble(..., danger_judge=None,
danger_policy=None)` 交给每个建出的 `AgentOptions`，keyword-only、默认 `None`，对现有调用方无声。
`RuleTablePolicy(table)` 按规则 ID 给处置：表里有的照表；表外的 HIGH → BLOCK；NONE / LOW → ALLOW。它是
通用的 `DangerPolicy`，放在 `danger.py`，名字进 `_WITHHELD`。按角色区分的策略归 M4（届时需要一个按角色
解析的入口，而不是这一对参数）。

**D3 三族可选规则进内核 `danger.py`，不进 `DEFAULT_RULES`。** 规则要复用已经加固过的分词，而
`DangerRule.matches` 只收整条命令文本、解析形态不公开。在 surface 重写分词就是把那些坑重踩一遍。三个工厂
`git_state_change_rule()`、`path_outside_workspace_rule(root)`、`workspace_root_removal_rule(root)`，名字进
`_WITHHELD`，M12 冻结公开 API 时再定。

分词先补两样东西，并用一条用例证明 `DEFAULT_RULES` 的全部判定不变：

- 重定向目标单独交出来（现状第 6 条）；
- 转发启动器展开：`uv run`、`uvx`、`poetry run`、`pipx run` 之后被启动的程序，按各自的选项取值表跳过
  选项（`uv run --with X`、`--python X` 等）；取值表认不出的选项让展开失败，失败时按下面各规则的保守方向处理。

三族规则都是 HIGH，但对「哪个词是程序」用两种相反的保守方向，这是承重的：

- **找危险程序时宁可多判。** `shell.git_state_change` 与 `shell.workspace_root_removal` 把
  `_command_word_slots` 的每一个槽、以及启动器展开出来的程序都当成可能的程序词（`setsid git push`、
  `stdbuf -oL git push`、`timeout -k 5 60 git push`、`uv run --with rich git push`、`setsid rm -r ./*` 都
  命中）。只用一个「真实程序词」会让 `_WRAPPERS` 里每个没进表的 wrapper 都成为绕过口。
- **豁免程序词时宁可少判。** `shell.path_outside_workspace` 只豁免每段里**确定的那一个**程序词（没有
  wrapper，或 wrapper 与它的选项都能按表认出时）；认不出就一个都不豁免。

各规则的内容：

- `shell.git_state_change`：git 的子命令不在只读集合里（`status diff log show blame ls-files ls-tree grep
  rev-parse describe shortlog cat-file merge-base name-rev version help`）；先跳过全局选项再找子命令。
  `-c`、`--config-env`、`--exec-path`、`--git-dir`、`--work-tree` 本身就按改状态算（`-c core.fsmonitor=
  <命令>` 会让只读的 `status` 执行任意命令）；`GIT_` 开头的环境赋值前缀同样算；任何参数或重定向目标落在
  `.git/` 之下（不分大小写）也算。
- `shell.path_outside_workspace`：参数与重定向目标里指向工作区外的路径——工作区外的绝对路径
  （`_PSEUDO_DEVICES` 里的伪设备除外）、`~` 开头、`$HOME` / `${HOME}` 开头、从工作区根出发会逃出去的
  `..`；`--选项=值` 取值来判；不带参数的 `cd`、`cd -`、`pushd`、`popd` 算离开工作区。路径包含按规范化
  后的路径比较，不按字符串前缀。
- `shell.workspace_root_removal`：`rm` 的目标规范化之后等于工作区根（`.`、`./`、`.//`、`src/..`、根的
  绝对路径、`../<根目录名>`）或根下全部（`*`、`./*`）。

工作区根在构造规则时解析一次；`matches` 只做字符串运算，不碰磁盘、不读环境变量与 cwd（T3 的内核 AST 守卫）。
已知盲区写进 docstring：`sh -c "…"`、`make`、脚本文件里的命令；变量与命令替换（含 `${HOME%/}`）；引号内
代码里嵌的路径；选项与取值粘在一起的写法（`-o/etc/x`）；`dd of=` 指向普通文件。已知误报同样写明：以 `/`
开头的 sed / awk 脚本（`sed -n '/def x/,/^$/p'`）、`/tmp` 下的临时文件——读过命令输出之后它们会直接被
拒绝，提示词据此引导 Agent 用 Read / Grep、不在工作区外建临时文件。

**D4 开发期处置策略对所有 CLI run 生效；git 写命令与删根由钩子无条件拦下。** 规则表是 `DEFAULT_RULES`
加三族，声明顺序是所有 BLOCK 族在前、`shell.path_outside_workspace` 在后（`judge` 取第一个命中）。
`RuleTablePolicy` 的表：默认四条、`shell.git_state_change`、`shell.workspace_root_removal` → BLOCK；
`shell.path_outside_workspace` → CONFIRM。

现状第 4 条让宿主策略在读过命令输出之后失效，所以两件「永不允许」的事不能只靠处置表：开发期 Module 在
`PRE_TOOL_USE` 上用一个只含 `shell.git_state_change` 与 `shell.workspace_root_removal` 的判定器检查每条
Bash，命中就 `skip`，交回 `ToolResult(output=<拦截规则与替代做法>, trust=TRUSTED)`——这段文字出自宿主，标成
可信，拦截本身才不会把轨迹变成不可信、让后面的确认都变成拒绝。它与 `untrusted_high_risk` 取什么值无关。
CLI 拒收 `--flag untrusted_high_risk=allow`（三件套：它会让读过命令输出之后的所有高风险命令放行，CLI 就在
用户机器上执行）。

CONFIRM 只在可信轨迹里到得了人手上，也就是 Agent 还没读过任何命令输出时；之后按 `untrusted_high_risk`
走：默认 `reject`，`ask` 时问人。README 与 runbook 写清这一点。

**D5 M2 的威胁模型。** M2 挡得住的是：Agent 的失手（误删、误推、误改 git 状态）、朴素的注入（让模型
「读一下 `~/.config/…`」「推一下」）、密钥经 Agent 的环境变量或 handoff 泄露。挡不住的是一个被注入内容
成功说服、会绕规则的 Agent：

- 它能用 `python -c`、`sh -c`、`ssh`（OpenSSH 按系统账户找 `~/.ssh`，不看 `HOME`）读用户可读的任何文件、
  访问网络；Agent 写的代码（包括测试）在 Bash 与宿主复核里执行时同样如此；
- 同一用户的进程读得到宿主进程启动时的环境（Linux 的 `/proc/<pid>/environ`、macOS 的 `ps -E`），在 shell
  里 export 的 API key 因此对它可见；
- 调用 `setsid` 或二次 fork 的进程能逃出进程组清理（D9）。

真正的边界是 M5 的沙箱。因此 M2 的自举只用于可信输入（本仓、本仓提示词），改动由 Reviewer、宿主复核与人
三道把关，CI 在推送后再跑一遍。CLI 改从文件读 key、不经环境变量，归 M3。

**D6 Agent 的命令环境。** 宿主（内核不读环境变量）为 Agent 的工作区准备环境：

- 透传固定白名单 `PATH HOME USER LOGNAME LANG LC_ALL LC_CTYPE TMPDIR XDG_CACHE_HOME UV_CACHE_DIR` 加
  `workspace.pass_env`，都只取实际存在的；
- 名字按 `_` 切词后，任一词是 `KEY KEYS TOKEN TOKENS SECRET SECRETS PASSWORD PASSWD PASS PAT CREDENTIAL
  CREDENTIALS AUTH COOKIE` 之一（不区分大小写）就拒收，写在 `pass_env` 里也拒，报三件套错误说明原因。
  按词匹配而不是子串：`GIT_AUTHOR_NAME` 不是密钥，`SSH_AUTH_SOCK` 是（Agent 不拿 ssh agent）。这条规则只
  决定「不交给 Agent」；哪些值要打码是 D12 的另一条更窄的规则；
- 宿主另设四个值：`GIT_TERMINAL_PROMPT=0`、`GIT_CONFIG_NOSYSTEM=1`、`GIT_CONFIG_GLOBAL` 指向本次 run 的
  一个空文件、`GH_CONFIG_DIR` 指向本次 run 的一个空目录（两者都在工作区外的临时目录里，run 结束删除）。
  Agent 于是用不上用户的 git 凭证助手、别名、hooksPath 与 `gh` 的登录。

值在建工作区时拷一份；run、chat、doctor 三处都用同一个函数。stdin 接 `/dev/null` 在内核做（D9）。

**D7 不做命令白名单。** 白名单对真实模型太脆：Agent 会自然地用 `find`、`sed`、`head`、`wc`，列不全就
频繁被拦，列全了又失去意义。M2 的防线见 D5。

**D8 `run --no-write` 只保留只读工具。** 冻结之前，每个角色的工具名单与拓扑槽的 `extra_tools` 只保留一张
只读白名单（`Read Grep Glob AskUser AskUserQuestion`；编排工具由 Runtime 另装，不受影响）。用白名单而不是
去掉几个写工具：以后新增的可写工具默认进不来。与 `--session`、`--git-mode` 互斥；不做宿主复核与 git 收尾，
handoff 的状态是 `read_only`。依据路线图静态验收「写工具不可被注册给 Worker」。

**D9 命令执行（内核）。** 改动都向后兼容，`ExecResult` 只追加字段：

- **stdin**：子进程 stdin 接 `/dev/null`，命令读 stdin 立刻得到 EOF，不与 CLI 抢终端输入。
- **进程组**：前台与后台命令各自在新的进程组里跑，全部登记。超时、`kill`、`close()` 时无条件杀整个进程组
  ——组长（shell）已经退出也照杀，因为组比组长活得久。前台命令的 shell 退出后，最多再等 1 秒让管道关闭，
  之后杀掉组里剩下的进程并返回已收到的输出。所有等待都有上限：超时 1 秒的调用不会因为 `cmd &` 拖到 4 秒。
- **超时**：`ExecResult` 追加 `timed_out: bool = False`；超时时 `run` 不再抛错，而是返回已收到的部分输出，
  `timed_out=True`、`exit_code=None`。
- **Bash 的结构化结果**：Bash 前台结果与 BashOutput 结果的 `meta = {exit_code, running, truncated,
  timed_out, stdout, stderr}`，后两者是实际展示给模型的（可能已截断的）两条流。开发期的解析器读这里，不从
  渲染后的文本里切分。
- **输出上限**：`local_tools(portal, *, output_limit=None)`；给定时 Bash 与 BashOutput 对 stdout、stderr
  **分别**截断，每条流保留前 20% 与后 80%，中间一行说明截掉多少、怎么看全，`truncated` 如实为 true。CLI
  传每条流 10,000 字符。按流截断是承重的：pytest 的汇总在 stdout 末尾，而渲染文本里 stderr 排在最后。
- **`.git/` 保护**：文件工具拒写路径分量里有 `.git` 的位置，比较不分大小写（APFS 默认不区分大小写，
  `.GIT/hooks/x` 就是真的 hooks 目录）；`.git` 是文件（worktree）时同样拒写。给模型的错误说明为什么。

### 开发工具

**D10 检查结果解析器是纯函数。** 输入是结构化字段 `(command, exit_code, stdout, stderr, timed_out)`，
来自 D9 的 `meta` 或宿主复核的 `ExecResult`；输出 `CheckResult(tool, status, counts, summary)`。
`tool` ∈ `pytest ruff_check ruff_format pyright check_deps check_links other`；`status` ∈
`passed failed error timeout no_output no_tests`（pytest 退出码 5 是 `no_tests`）。`counts` 每个工具的键序
固定。识别工具按命令文本的正则，只用于摘要，不参与任何安全判定。

**D11 开发期 Runtime Module（名字 `dev`，不声明 Flag）。**

- 每个 Agent 的插件工厂（同步执行，早于子 Agent 的任务开始）用一个全局序号记下它的 id、角色与父 id。
  不用 `AGENT_SPAWN`：它不带角色，而且在子 Agent 已经开始跑之后才触发。
- `PRE_TOOL_USE`：D4 的无条件拦截。
- `POST_TOOL_USE`：Bash 前台结果与第一次观察到已结束的 BashOutput 结果（按 handle 去重，handle 到命令的
  对应在 Bash 启动时记下）写 `meta['check']`；每次 Write、Edit、Bash 调用按同一个全局序号记进服务
  `runtime.dev_log`（Agent id、角色、工具、命令或路径、退出码、检查结果）；`trust` 原样交回。
- `AGENT_START` / `AGENT_END`：对 Reviewer 角色各取一次工作树指纹（D14 的宿主 git：改动列表加每个文件的
  blob hash）；`AGENT_END` 另记下每个 Agent 的角色、结局与最后一段文字。钩子里出错时不静默吞掉，而是记成
  「指纹缺失」，D14 据此按 `unverified` 处理。

`dev_log` 交出只读快照。

**D12 最小脱敏器（CLI surface，`cli/redaction.py`）。** M3 建统一 `Redactor` 时并进去。规则各有稳定 ID，
替换成 `[REDACTED:<规则 ID>]`，不留原文：

- `secret.env_value`：宿主环境里名字按 D6 判为密钥、且值像凭证（不是绝对路径、不是不带凭证的 URL、长度
  不少于 8）的变量，按值逐字替换，长的先换。值是长度不少于 20、没有 `/` 与空白的令牌时，另把去掉公开前缀
  （如 `sk-or-v1-`）之后、长度不少于 16 的任意片段也替换，被截断的 key 也认得出；
- `secret.private_key_block`、`secret.authorization_header`（`Authorization:` 之后的 Bearer / token /
  Basic 凭证）、`secret.url_userinfo`（URL 里 `user:pass@` 的部分）；
- `secret.openrouter_key`、`secret.openai_key`、`secret.google_api_key`、`secret.github_token`、
  `secret.aws_access_key`、`secret.slack_token`：各自完整的公开格式（只有前缀不算）。

用在：handoff 文件与 stdout；`--handoff` 下 stdout 与 stderr 都经同一个脱敏写入器；提交消息。暂存区 diff
的提交闸（D14）只用逐字值与格式规则，不用片段匹配：片段匹配允许误报，拦提交不允许。一条 AST 守卫保证
`syndica/cli` 里的写文件调用要么不带内容（空文件、空目录、锁、内容恰为 `*` 的 `.gitignore`），要么写的是
脱敏器的输出；`--handoff` 的两条流必须经那个写入器。

### 复核、交接与 git

**D13 `run --handoff`。** 顺序：

1. 对工作区根目录的目录描述符加 `flock(LOCK_EX | LOCK_NB)`（不用 `.syndica/` 下的文件：Agent 删得掉它），
   同一工作区同时只跑一个 `--handoff` run；已被占用时退出 1，三件套写明另一个 run 在跑；
2. run 开始前用宿主 git 环境（D14）取快照：完整性快照、运行前被忽略的文件、运行前的改动与每个文件的 blob
   hash。`verify`、`protected`、`pass_env` 都从开始时加载的配置里取，Agent 中途改 `.syndica.yaml` 不影响；
3. Agent 结束后关闭 Agent 的工作区（D9：杀掉它留下的全部进程组）；复核用新的工作区（Agent 环境），git 用
   宿主 git 环境；
4. run 失败时不跑复核（`host_verify: skipped`）；否则取快照 A（改动与 blob hash）→ 逐条跑 verify（每条上限
   900 秒）→ 取快照 B。B 与 A 不同说明复核命令改了文件，复核按失败算，handoff 列出被改的文件；
5. 组装 handoff，经 D12 脱敏，以 0600 写到 `.syndica/handoffs/<UTC 时间戳>-<短 id>.md`：这时 git 收尾
   还没发生，front matter 里 `git: pending`、`exit_code: null`。文件路径打到 stderr。写之前 `lstat` 检查
   `.syndica` 与 `handoffs` 不是符号链接、解析后仍在工作区内；第一次创建 `.syndica/` 时写一个内容为 `*` 的
   `.gitignore`；
6. 执行 D14 的 git 收尾（摘要与询问都在 stderr）；
7. 用最终的 `git` 与 `exit_code` 和 git 一节重写同一个文件（写临时文件再原子改名，仍是 0600），然后把这份
   最终 handoff 完整打印到 stdout，只打印这一次。在第 6 步被打断时，文件停在 `git: pending`，如实反映没做完。

**转义。** 所有来自模型、Agent 或文件系统的字符串（文件名、命令、结论、提交标题、确认参数、AskUser 的问题
与选项）进终端与 handoff 之前都经 `terminal_safe()`：除换行与制表符外的 C0 控制字符、DEL、C1 控制字符、
双向文字控制字符（U+061C、U+200E、U+200F、U+202A–U+202E、U+2066–U+2069）与 U+2028 / U+2029 转义成可见的
`\x..` / `\u....`。这对**所有** run 的交互提示都生效，不只 `--handoff`；`--handoff` 下提示另外改走 stderr，
stdout 只有 handoff。

「改动」按 `git status --porcelain=v1 -z --untracked-files=all` 解析，排除 `.syndica/`；不是 git 仓库或
没有 git 时写明原因。「下一步」由宿主按状态生成（修哪条失败、怎么手动提交），不取模型文字。

**D14 git 收尾：人选择，宿主执行。** 只在 `run --handoff` 且不是 `--no-write` 时进行。

**宿主 git 环境。** 快照、状态、hash、提交、推送都在同一个环境里跑：宿主自己的环境（含 ssh agent）加
`GIT_TERMINAL_PROMPT=0`、`GIT_LITERAL_PATHSPECS=1`；每条都加 `-c core.fsmonitor=false`；以参数数组直接
调 git，不经 shell；stdin 接 `/dev/null`（提交消息除外）；本地操作超时 60 秒、推送 120 秒。复核命令则在
Agent 环境里跑：复核验证的是 Agent 能跑出的东西。

**完整性快照（run 开始前取）：** `git rev-parse --show-toplevel`、HEAD、当前分支、上游（remote 与
merge ref）、所有 remote 的 URL、`git rev-parse --git-path config` 指向的文件 hash、
`git rev-parse --git-path hooks` 解析出的目录里每个钩子文件的 hash、有没有进行中的操作。hash 一律用不写
对象库的 `git hash-object`（不带 `-w`），扫描之前不往 `.git/objects` 落任何内容。

**Reviewer 结论。** 取最后一个满足下列全部条件的 Reviewer Agent 的最后一段文字：结局是 `COMPLETED`；自己
没有 Write / Edit；它开始与结束时的工作树指纹（D11）都等于快照 A——这同时挡住了「Reviewer 之后又有人改」
「Reviewer 自己用 Bash 改了」「后台进程在 review 期间改了」三种情况；文字里恰有一行符合标记语法（公开
契约）。找不到这样的 Reviewer（包括从来没有派过 Reviewer）时结论是 `unverified`。handoff 标明这是模型
给出的结论。

**前置条件。** 提交的前置条件，任一不满足就不提供任何自动选项，handoff 写明是哪一条：

- 工作区根就是 git 顶层目录；完整性快照与现在一致；在分支上（不是游离 HEAD）；HEAD 没变；没有进行中的
  merge、rebase、cherry-pick、revert、am、bisect；
- run 正常结束；配置了 verify 且宿主复核全过（含快照 B 与 A 一致）；Reviewer 结论为 pass；
- 本次有改动；没有「运行前已改动、本次又改」的文件；没有运行前被忽略的文件出现在改动里；受保护路径
  （D15）没有改动——这一条只挡 `--git-mode`，交互询问时醒目列出，由人决定。

推送另要求：有上游，且提交前 `git rev-list @{u}..HEAD` 为空（不顺带推走人以前没推的提交）。只满足提交的
前置条件时，只提供「只提交」与「我来处理」，并说明为什么没有推送选项。

**询问。** 没给 `--git-mode` 时，在 stderr 打印宿主生成的摘要作为问题前的最后一段输出：分支 → 上游与
remote URL；改动路径的列表（转义，最多 30 条再给总数），逐条标出它是否由 Agent 的 Write / Edit 产生（不是的
可能来自 Bash，也可能是人在 run 期间改的）；复核结果；Reviewer 结论。然后问可用的选项：`1` 提交并推送、
`2` 只提交、`3` 我来处理。stdin 是终端时，读答复之前先清掉已经缓冲的输入（`tcflush`），误敲的按键不会当成
答复。读到 EOF、空行或其他输入都按 `3`，因此 `</dev/null` 的 run 永远不会自动提交。给了
`--git-mode commit|push` 就不问；`--git-mode manual` 等同于选 `3`。

**执行。**

1. 提交前再算一次改动文件的 blob hash，与快照 A 不同就不提交（复核之后文件又变了）；
2. 用 D12 的逐字值与格式规则扫描将要提交的 diff，命中就不提交，报文件:行号与规则 ID，不打出命中内容；
3. 只提交本次改动的路径：新文件 `git add -- <路径…>`，然后 `git commit --only --cleanup=verbatim -F -
   -- <路径…>`，消息从 stdin 给；`GIT_LITERAL_PATHSPECS=1` 让 `*`、`[ab]`、`:!x` 这样的文件名只匹配它们
   自己，人事先暂存的其他内容也不会被带上；
4. 提交后用 `git diff-tree -r -z --name-only --no-commit-id --root HEAD` 核对这次提交改到的路径**集合**
   恰好等于预期，且每个路径的 blob 与快照 A 一致；不一致就报告且不推送；
5. 推送：`git push <快照里的 remote> HEAD:refs/heads/<快照里的上游分支>`，从不 `--force`；认证失败、钩子
   拒绝、非快进各自给出三件套错误。

提交标题取 Orchestrator 最终回答里的 `commit_title:` 行（缺失时用目标的第一行），转义、截到 72 字符、经
D12 脱敏；正文列出复核结果。作者用本机 git 配置，宿主不加任何 trailer；提交后的实际消息（仓库自己的
commit-msg 钩子可能追加内容）写进 handoff。

**D15 受保护路径与配置信任。** 受保护路径有改动时，`--git-mode commit|push` 拒绝执行（退出 3），交互询问时
醒目列出。受保护路径由两部分组成：

- 内置、总是生效：`**/.gitignore`、`**/.gitattributes`——Agent 改忽略规则就能让本该被忽略的文件（比如
  run 中途新建的 `.env`）变成「新改动」；
- `workspace.protected`：一组相对工作区根的 glob。`**` 跨任意层目录，`*` 与 `?` 不跨 `/`，对 POSIX 形式的
  相对路径做整串匹配（不用 `PurePath.match`：3.11 的它不递归 `**`）。本仓配置保护 `pyproject.toml`、
  `uv.lock`、`**/conftest.py`、`pytest.ini`、`.pytest.ini`、`pyrightconfig.json`、`**/ruff.toml`、
  `**/.ruff.toml`、`setup.cfg`、`tox.ini`、`scripts/**`、`.syndica.yaml`、`docs/agents/**`、
  `.github/**`、`AGENTS.md`——它们决定了「检查通过」是什么意思，Agent 改它们等于改考卷。

项目配置视同用户自己写的代码，如同 Makefile：`run --handoff` 与 `doctor --verify` 会在宿主上执行
`workspace.verify`，不经危险判定。README 写明：在不信任的仓库里这样运行，等于运行它的测试脚本。首次使用
时的信任确认归 M4（与配置发现一起做）。

### 配置、角色与其他

**D16 `doctor` 在 Agent 的环境里自检。** 输出行顺序固定：config、models、workspace（已有）、env（只列
名字）、git（clean / N 处改动 / 非 git 仓库 / 没有 git）、python（Agent 环境里 `python3 --version`）、
verify 命令用到的每个可执行文件（用 `shlex` 拆分、跳过开头的 `NAME=值`，在 Agent 环境里跑
`<exe> --version`）；`doctor --verify` 再逐条跑 verify。

**D17 `syndica-dev` 放在本仓的项目配置里。** 仓库根的 `.syndica.yaml` 加 `docs/agents/*.md` 提示词，纯声明式：

- 角色：同名覆盖 Planner、Explorer、Worker 与 Orchestrator（Orchestrator 保留 `impl`，`subagents` 加上
  Reviewer）；新增 Reviewer（Read、Grep、Glob、Bash）；
- 拓扑 `syndica-dev`：一个 root singleton 槽放 Orchestrator；其余角色由 `Delegate` 从目录建出。

提示词要求：

- Worker 与 Reviewer 先读 `AGENTS.md` 的硬约束、错误消息、测试三节（先 Grep 节标题再按行读，不整篇
  读——整篇约 50 KB，几个 Agent 各读一遍会撞免费档的每分钟 token 上限）；
- 不执行 git 写命令（提交与推送由宿主在人选择之后做）；
- 跑检查时用 Bash 工具的 `timeout` 参数（全量测试给 300 秒），加 `-q --tb=short`；只写 POSIX sh（CI 的
  `/bin/sh` 是 dash；本机没有 `timeout` 命令）；读代码用 Read / Grep，不用以 `/` 开头的 sed / awk 脚本；
  不在工作区外建临时文件（D3 的已知误报）；
- Worker 每次改动后跑最小相关测试；
- Reviewer 看 `git diff`，按硬约束逐条过，不改任何文件（包括不跑格式化这类会写文件的命令），单独一行写
  `verdict: <pass|partial|block|unverified>` 并给理由；
- Orchestrator 按「Planner → Explorer → Worker → Reviewer」推进。Reviewer 给 `block` 时，另派一个新的
  Worker 并把理由与上下文写进派活内容，而不是给原 Worker 发消息（M1 的轮数跨消息累计、不重置）；Worker
  改完一定再派一次 Reviewer；最终回答里单独一行给出 `commit_title: <一句话>`。

**D18 `scripts/check_links.py`。** 检查 Markdown 相对链接指向的文件存在（锚点不查），跳过外链、纯锚点、
围栏代码块与行内代码；进本仓 verify 与 CI。

**D19 `CliOutcome` 与报错去嵌套。** `CliHost.run_request` 与 `doctor` 改为可以返回
`CliOutcome(output, exit_code)`；`_host_call` 照旧接受 `str`（退出 0）。根 Agent 失败且最近的 Runtime 错误
是 SyndicaError 时，stderr 直接给它的三件套，前面加一句「Agent X 以错误结束」，不再在「实际」里套一层。

**D20 CI 只跑 Ubuntu + Python 3.11。** push 到 main 与 pull request 触发；`uv sync --locked`（锁文件过期
即失败）加五道门禁，不需要任何密钥。runner 固定为 `ubuntu-24.04` 而不是 `ubuntu-latest`：后者从 2026-10-19
起换成 Ubuntu 26，平台变化不该在没人改 workflow 的时候悄悄发生（下次改 workflow 时落实）。GitHub Free 的
月度额度用完时按「验证」一节的本地验证协议推进。在只在 main 上开发的流程里，CI 是推送之后的复查，不是闸；挡坏提交
进 main 的是宿主复核（同一套门禁）。Linux 与本机 macOS 的差异在 T1 里修掉，不跳过测试。矩阵归 M12。

**D21 工作区仍是 cwd；同一工作区同时一个开发 run。** 在副本里跑就 `cd` 进去。锁见 D13。

**D22 真实模型 E2E 只在仓库副本里跑。** `git clone` 一份，删掉原 remote，另建本地 bare 仓库作 remote 并设为
上游，`uv sync --locked --extra openai --extra dev`；key 文件在副本之外。另做一次真实 SSH 推送：在 GitHub
上建一个临时分支、设为上游、推送、验证后删除（执行前征得用户同意）。

---

## 公开契约

### CLI

| 用法 | 语义 |
|---|---|
| `run --no-write` | 只注册只读工具；与 `--session`、`--git-mode` 互斥 |
| `run --handoff` | 宿主复核，handoff 脱敏后写 `.syndica/handoffs/…md` 并打印到 stdout |
| `run --handoff --git-mode manual\|commit\|push` | 预先选定 git 收尾，不再询问；只能与 `--handoff` 同用 |
| `doctor --verify` | 自检之后逐条跑 `workspace.verify` |

退出码：`0` 成功；`1` run 失败、没有回答、复核失败、工作区已被另一个 `--handoff` run 占用，或已开始执行的
提交 / 推送失败；`2` 参数错误；`3` 显式 `--git-mode commit|push` 而对应的前置条件不满足、没有执行。同时
满足多种时取 `1`，其次 `3`。`doctor` 在任一检查失败时退出 1。

### `.syndica.yaml`

```yaml
version: 1
workspace:
  pass_env: [NO_COLOR]             # 可选：在固定白名单之外再透传的环境变量名
  verify:                          # 可选：宿主复核与 doctor --verify 逐条运行
    - uv run ruff format --check .
    - uv run pytest -q
  protected: [pyproject.toml, "scripts/**"]   # 可选：改了就不自动提交
agents: []
topologies: {}
```

未知键、类型错误、空白字符串、重复项、形似密钥的名字都报 `ConfigError`（三件套）。`verify` 的每一项
目前是字符串；将来允许写成带 `timeout` 的映射，字符串形式始终有效。

### 规则 ID（稳定标识）

- 危险规则（HIGH）：`shell.git_state_change`、`shell.path_outside_workspace`、`shell.workspace_root_removal`。
- 脱敏规则：D12 列出的十个 `secret.*`。

### 模型标记行

- Reviewer 最后一段文字里：匹配 `^\s*verdict\s*[:：]\s*(pass|partial|block|unverified)\s*$` 的一行（值不分
  大小写）。
- Orchestrator 最终回答里：匹配 `^\s*commit_title\s*[:：]\s*(\S.*?)\s*$` 的一行。

键是小写 ASCII；各自恰好一行才算数，零行或多行都按缺失处理。

### `meta` 与服务

- Bash 前台结果与 BashOutput 结果（内核）：`meta = {exit_code, running, truncated, timed_out, stdout,
  stderr}`，BashOutput 另带 `handle`。
- 开发期 Module 另写 `meta['check'] = {schema: 1, tool, status, counts, summary}`，键序固定。
- `runtime.dev_log` 交出只读快照。

### handoff

机器可读的部分只有开头的 front matter，键与值都是 ASCII，键序固定：

```text
---
schema: syndica.handoff/1
status: run_failed | read_only | verification_failed | verified | unverified
host_verify: passed | failed | not_configured | skipped
reviewer_verdict: pass | partial | block | unverified
git: pending | not_offered | declined | committed | pushed | push_failed | failed
protected_changed: <JSON 数组，ASCII 转义>
exit_code: <int> | null
---
```

`status` 按上面的顺序取第一个成立的：run 失败；只读运行；复核失败；复核通过且 Reviewer 为 pass；其余。
`push_failed` 表示已提交、推送失败；`failed` 表示提交本身失败。`git: pending` 与 `exit_code: null` 只出现在
git 收尾完成之前的文件里（D13 第 5 步）；最终文件与 stdout 里都是确定值。正文按目标、改动、Agent 跑过的
检查、宿主复核、Agent 的结论、下一步、git 收尾分节，节标题与措辞是展示层，不是契约（公开发布前要译成英文）。

### 内核 API 增量（都向后兼容）

- `AgentCatalog.freeze(..., *, danger_judge=None, danger_policy=None)`；
  `Runtime.assemble(..., danger_judge=None, danger_policy=None)`。
- `ExecResult` 追加 `timed_out: bool = False`；`LocalWorkspace.run` 超时时返回部分输出而不是抛错。
- `local_tools(portal, *, output_limit=None)`；Bash 与 BashOutput 结果的 `meta`。
- `danger.py` 里的 `RuleTablePolicy` 与三个规则工厂（进 `_WITHHELD`）。

---

## 任务与进度

每个任务：先写红测试 → 实现 → 定向测试 → 全量门禁 → 自我变异（清 `__pycache__`、新文件先 `git add`、
sha256 基线覆盖本轮会改到的每个文件）→ 对抗验证 → commit → push。CI 可用时等 CI 绿再开下一个任务；额度用完
期间按「验证」一节的本地验证协议走完再开下一个，额度恢复后在 Linux 上补跑。T6 与 T14 之后各做一次阶段审计。

### 阶段一：护栏

#### T1 — 基础 CI

**状态：completed**（`ce625ce`、`cc25006`；CI 运行 36704973780 在 Ubuntu 上跑完 2389 条测试、五道门禁全绿，
没有暴露平台差异；8 条变异全部杀死，含一条 README 与 CI 同时删门禁的联合变异）

**Files:** `.github/workflows/ci.yml`、`tests/test_ci_workflow.py`。

- checkout → setup-uv（固定到完整版本标签）→ Python 3.11 → `uv sync --locked --extra openai --extra dev`
  → 五道门禁；同一 ref 的旧运行自动取消；超时 15 分钟；`permissions: contents: read`。
- `tests/test_ci_workflow.py` 断言门禁命令逐字出现、顺序固定，且与 README「Development」一节一致；README
  的门禁清单本身也被钉住。

#### T2 — 内核：危险选项注入与 `RuleTablePolicy`

**状态：completed**（本地验证协议：五道门禁全绿、2423 条测试；26 条变异全部杀死，其中 11 条来自对抗评审
报出的存活变异与漏洞——空目录不校验判定、表键被归一化、NONE 判定跳过查表、只收 dict、三件套锚词、带首尾
空白的键、`DangerJudge` 不校验 `level` / `rule_id`；Linux 补跑待 CI 额度恢复）

**Files:** `kernel/catalog.py`、`kernel/runtime.py`、`kernel/danger.py`、对应测试与 `_WITHHELD`。

- 用例：两个以上角色都拿到注入的判定与处置，含类式角色（Orchestrator）；不传时行为逐字不变；assemble
  路径同样生效；传类对象报错指着 `danger_policy`；`RuleTablePolicy` 的三种情形（表内、表外 HIGH、NONE /
  LOW）与非法表项。
- 变异：只转判定不转处置；assemble 不转交；漏掉类式角色；表外 HIGH 放行。

#### T3 — 内核：三族可选规则

**Files:** `kernel/danger.py`、`tests/kernel/test_danger.py`、`_WITHHELD`、内核 AST 守卫测试。

- 先补分词（重定向目标、启动器展开），一条用例证明 `DEFAULT_RULES` 在全部既有用例上的判定不变。
- 用例按成员逐条写（常量表每个成员单独施加变异都要变红）：
  - git：只读子命令逐个放行、其余拦下；每种全局选项写法；`-c` / `GIT_` 前缀 / `.git/` 与 `.GIT/` 路径；
    `_WRAPPERS` 的**每个**成员包着 `git push` 都命中；`timeout -k 5 60`、`env -u X`、`nice --adjustment 5`
    这类带取值的选项；`uv run git push` 与 `uv run --with rich git push`；
  - 删根：每种写法（`.`、`./`、`.//`、`src/..`、绝对路径、`../<根目录名>`、`*`、`./*`）与 `setsid rm -r ./*`；
    `rm -r build`、`rm *.pyc` 放行；
  - 越界：每个伪设备放行；每种越界形态（绝对路径、`~`、`$HOME`、`${HOME}`、逃出的 `..`、`--opt=/x`、
    `>` / `2>` / `<` 的目标、不带参数的 `cd`、`cd -`、`pushd`）拦下；兄弟目录；确定的程序词是绝对路径时
    放行；`timeout 60 cat /etc/passwd` 与 `setsid cat /etc/passwd` 拦下；
  - 同时命中 BLOCK 族与越界规则时取 BLOCK 族。
- 同一形态换平凡写法仍然命中：管道、`&&`、`;`、子 shell、`\git`、`command git`、`env X=1 git`、多一次
  重定向。放宽与收紧两个方向都有用例。
- 内核 AST 守卫：`src/syndica/kernel` 里没有 `os.environ`、`os.environb`、`os.getenv`、`from os import
  environ`、`os.getcwd`、`Path.cwd`、`Path.home`、`expanduser`、`expandvars`、`os.path.abspath`、
  `Path.absolute`、`shutil.which`、`getpass.getuser`；守卫另有两条用例：一条证明它确实扫到了 `danger.py`，
  一条用独立的目录遍历（`os.walk`）得到的文件清单与守卫扫描的清单比对，证明扫描范围没被收窄。
- 变异：只读集合逐个删成员；伪设备逐个删；路径包含改成字符串前缀；git 规则改用单一程序词（`setsid git
  push` 漏过）；越界规则把所有槽当程序词（`timeout 60 cat /etc/passwd` 漏过）；`=` 不拆；不看重定向目标；
  不认启动器。

#### T4 — 内核：命令执行

**Files:** `kernel/workspace.py`、`kernel/local_workspace.py`、`kernel/tools/local.py`、对应测试、测试侧的
shell 开关。

- 先做测试侧的 shell 开关：真正执行命令的测试不再写死 `/bin/sh`，改从一个测试 helper 取，环境变量
  `SYNDICA_TEST_SHELL` 可以把它换成 `/bin/dash`（本机自带，近似 Ubuntu 的 `/bin/sh`）。这是本地验证协议
  覆盖 Linux 差异的手段；只在测试里读环境变量，内核不受影响。
- 用例：命令里 `cat`（无参数）立刻结束；超时 1 秒的 `sleep 4 &` 在 1 秒加宽限内返回；`close()` 之后前台与
  后台命令留下的进程组都不在了；超时返回部分输出、`timed_out` 为 true、`exit_code` 为空，不抛错；Bash 前台
  `meta` 六个键与取值（成功、失败、超时）；`output_limit` 下 stdout 与 stderr 各自截断、`truncated` 为
  true、stdout 末尾的汇总行保留；不给 `output_limit` 时输出逐字不变；Write / Edit 到 `.git/config`、
  `.GIT/config`、`.git/hooks/x` 以及 `.git` 是文件时都被拒，`.gitignore` 与 `.github/` 下的文件不受影响。
- 变异：只截合并后的文本；保头不保尾；组长退出后不杀组；等待管道不设上限；`timed_out` 恒为 False；`.git`
  判断区分大小写；`.git` 判断用字符串前缀（`.github/` 被误拒）。

#### T5 — CLI：`workspace` 配置段与 Agent 命令环境

**Files:** `cli/config.py`、`cli/host.py`、新模块 `cli/environment.py`、对应测试。

- 用例：Agent 的 Bash 里 `printenv` 看得到 PATH、HOME，看不到宿主环境里的 `GEMINI_API_KEY`、
  `OPENROUTER_API_KEY`、`SSH_AUTH_SOCK`、`FOO_TOKEN`，也看不到不在白名单里的普通变量 `ZZZ_PLAIN`；
  `GIT_AUTHOR_NAME` 可以经 `pass_env` 透传；`pass_env: [MY_API_KEY]` 被拒；`GIT_CONFIG_GLOBAL` 与
  `GH_CONFIG_DIR` 指向工作区外的空文件和空目录、run 结束后被删；`verify` 与 `protected` 的校验；CLI 确实把
  `output_limit=10_000` 交给 `local_tools`；run、chat、doctor 三处各一条用例证明用的是同一份环境。
- 变异：名字规则逐个删词；按子串匹配（`GIT_AUTHOR_NAME` 被拒）；漏掉 chat 路径；白名单换成全量减黑名单；
  不传 `output_limit`。

#### T6 — CLI：开发期处置策略与无条件拦截

**Files:** 新模块 `cli/policy.py`、`cli/dev_module.py`（本任务只做 `PRE_TOOL_USE`）、`cli/host.py`、对应用例。

- 用例经公开 CLI 与脚本化 LLM：`git push`、`setsid git push`、`uv run git push`、`rm -r .` 被拦，Agent 拿到
  拦截说明后换路径完成；拦截结果是 `TRUSTED`，拦截之后同一 Agent 在可信轨迹里的越界读取仍然到达确认；
  在 `--flag untrusted_high_risk=ask` 下读过命令输出之后，`git push` 仍然被拦、不会问人；`--flag
  untrusted_high_risk=allow` 被拒（三件套）；可信轨迹里同一批两次 `cat /etc/hosts` 各自触发确认，批准第一
  次、拒绝第二次，只执行第一次；读过命令输出之后同样的调用在默认 Flag 下直接拒绝；`2>/dev/null` 不触发；
  拓扑路径（assemble）同样生效。
- 变异：无条件拦截只在默认 Flag 下生效；拦截结果标成 UNTRUSTED；表里越界规则改成 ALLOW；assemble 路径不传
  策略；放行 `allow`。

阶段一收口：阶段审计，确认护栏在 run、chat、doctor、拓扑四条路径上都生效。

### 阶段二：开发工具

#### T7 — `run --no-write`

**Files:** `cli/main.py`、`cli/host.py`、对应用例。

- 用例：参数与帮助文本；与 `--session`、`--git-mode` 同用报参数错误（退出 2）；脚本化 Worker 调 Write 与
  Bash 都得到「未知工具」结果，工作区除 `.syndica/` 外逐字节不变；只读工具仍可用；拓扑槽 `extra_tools` 里
  的 Bash 同样没有；一个新登记的、不在白名单里的假工具也没有。
- 变异：白名单换成去掉五个写工具；只处理目录不处理槽；仍把原目录交给 assemble。

#### T8 — 检查结果解析器与真实样例

**Files:** `cli/checks.py`、`tests/cli/fixtures/checks/`、`tests/cli/test_checks.py`。

- 样例由真实工具跑出，存成数据文件（不用 `.py` 后缀，避开 ruff、pyright 与 pytest 收集），README 记录生成
  命令与工具版本。覆盖路线图要求的五类（通过、失败、收集错误、超时、无输出），加 pytest 的 `no_tests`；
  每个工具至少通过与失败两类。
- 用例：每个样例解析结果整表相等，断言 `list(d)` 键序；未识别命令归 `other`。
- 变异：pytest 汇总只认 passed；收集错误当失败；超时当失败；退出码 5 当失败。

#### T9 — 开发期 Module 的记录

**Files:** `cli/dev_module.py`、对应用例。

- 行为见 D11。用例：`meta['check']` 键序；两个 Agent 各跑一条命令，`dev_log` 互不串线；同一 handle 的
  BashOutput 轮询多次只记一条，且记得是哪条命令；钩子交回的结果仍是 UNTRUSTED；Write / Edit 进 `dev_log`；
  插件工厂记下的角色与序号早于该 Agent 的任何工具调用；Reviewer 的开始与结束指纹；`AGENT_END` 记下结局与
  最后一段文字；超时记为 `timeout`；指纹出错记成缺失。
- 变异：`dev_log` 交出内部列表；BashOutput 不去重；工具名按包含匹配（`BashOutput` 当成 `Bash`）；序号改在
  `AGENT_SPAWN` 里取。

#### T10 — 最小脱敏器

**Files:** `cli/redaction.py`、`tests/cli/test_redaction.py`、写入点 AST 守卫。

- 样例一律是运行时拼出的合成假值，不在源码里逐字出现（否则仓库自己的提交会撞上提交闸）。用例：每条规则
  一个命中与一个形似不命中的样例；按值替换长的先换；令牌型值被截断后的片段被替换；`SSH_AUTH_SOCK` 这种
  路径值不被按片段替换，`EventListener` 这样的普通词不受影响；只有公开前缀（`sk-or-v1-`）不算命中；
  提交闸模式不做片段匹配；规则 ID 整表相等（成员与取值一次钉住）。
- AST 守卫：`src/syndica/cli` 里的写文件调用只有两类——不带内容的（空文件、空目录、锁、内容恰为 `*` 的
  `.gitignore`）与写脱敏器输出的；`--handoff` 的 stdout / stderr 经脱敏写入器。守卫另有一条用例证明它扫过了
  整个 `cli` 包。
- 变异：逐条删规则；短的先换；片段匹配套用到路径值；提交闸做片段匹配；长度下限差一。

#### T11 — `CliOutcome` 与 `run --handoff`

**Files:** `cli/main.py`（`CliOutcome`、`--handoff`）、`cli/host.py`、新模块 `cli/handoff.py`、对应用例。

- 流程见 D13。用例：front matter 的键序与取值；`status` 的优先级（每一级一条用例）；复核全过且 Reviewer
  pass → `verified`、退出 0；复核失败 → `verification_failed`、退出 1；没配 verify → `unverified`、退出 0；
  复核通过但 Reviewer 为 `block` → `unverified`；Agent 失败 → `run_failed`、`host_verify: skipped`、退出 1；
  复核命令改了文件 → 复核失败并列出文件；不是 git 仓库；运行前已改、本次又改的文件被列出；最终文件与
  stdout 逐字相同，且 stdout 只打印一次 handoff；git 收尾之前被打断时文件停在 `git: pending`、`exit_code:
  null`；`--handoff` 下确认提示走 stderr；普通 run 的确认与 AskUser 提示里的 ESC、`\x9b`、U+202E 被转义；
  `.syndica` 是符号链接时拒绝写入；文件权限 0600；`.gitignore` 只在第一次创建；宿主环境里放一个假密钥并让
  它出现在 Agent 的结论里，文件、stdout、stderr 都只剩占位符；同一工作区第二个 `--handoff` run 在锁上退出
  1；Agent 删掉 `.syndica/` 之后锁仍然有效。
- 变异：退出码忽略复核；把 Agent 自称「测试通过」当复核；没配 verify 时写 `verified`；不比对快照 B；
  `terminal_safe` 漏掉 C1；锁放在 `.syndica/` 下的文件上。

#### T12 — git 收尾

**Files:** 新模块 `cli/git_finish.py`、`cli/host.py`、`cli/main.py`、对应用例。

- 行为见 D14。测试里隔离 `HOME`、`GIT_CONFIG_GLOBAL`、`GIT_CONFIG_NOSYSTEM`，设本地身份（CI 上没有全局
  git 身份，也不能跑开发者的全局钩子），remote 是本地 bare 仓库；隔离出来的 HOME 里另放一个全局 hooksPath，
  验证快照哈希的是宿主环境解析出的 hooks 目录。
- 用例：
  - 前置条件逐条不满足时都不提供自动选项，handoff 写明哪一条；只有推送的前置条件不满足时只提供 `2` / `3`
    并说明原因；`1` / `2` / `3` / 空行 / EOF / 乱输入；stdin 是伪终端时，提问前已缓冲的 `1` 不会触发提交；
  - `--git-mode push` 不询问直接执行；前置条件不满足时退出 3；复核也失败时退出 1；已提交而推送失败时
    `git: push_failed`、退出 1；
  - 只有本次改动的路径进提交：人事先暂存的文件、无关的未跟踪文件、运行前被忽略的文件都不进；Agent 改了
    `.gitignore` 时（受保护路径）`--git-mode` 拒绝；暂存 diff 里有假密钥时拒绝提交、不打出密钥；
  - 文件名含 `;`、`$(…)`、反引号、换行、以 `-` 开头，以及 `*`、`[ab].py`、`:(top)x`、`:!x` 时，提交的路径
    集合恰好是这些文件本身，不执行任何东西、不带进别的文件；
  - Agent 改了 `.git/config`（fsmonitor 或 remote URL）或钩子文件时拒绝收尾；进行中的 merge 时拒绝；复核
    之后文件又被改时拒绝提交；上游领先的本地提交存在时不推送；没有上游时不提供推送；
  - Reviewer 结论：结局不是 `COMPLETED`、自己改过文件、开始或结束指纹与快照 A 不同、标记行零行或多行，都按
    `unverified`；
  - 摘要里列出转义后的改动路径，并标出不是由 Write / Edit 产生的那些；提交消息与生成的逐字一致、没有任何
    trailer。
- 变异：EOF 当成 `1`；经 shell 拼命令；去掉 `GIT_LITERAL_PATHSPECS`；`git add -A`；不排除运行前被忽略的
  文件；不查完整性快照；推送带 `--force`；Reviewer 结论缺失当成 pass；不清缓冲输入；推送前置条件不满足时
  仍提供 `1`。

#### T13 — `doctor` 扩展与 `doctor --verify`

**Files:** `cli/host.py`、`cli/main.py`、对应用例。

- 行为见 D16，返回 `CliOutcome`。用例：Agent 环境里找不到 uv 时退出 1，下一步指向 PATH 或
  `workspace.pass_env`；env 行只有名字；不是 git 仓库；`--verify` 失败的那条带解析摘要。
- 变异：env 行打出值；可执行文件检查用宿主环境——用一个只有在某个不透传的环境变量存在时才成功的假可执行
  文件区分。

#### T14 — `scripts/check_links.py`

**Files:** 脚本、`tests/test_check_links.py`；CI、README「Development」与 `AGENTS.md`「常用命令」各加一步。
2026-09-30 扫描：全仓 44 条相对链接，没有断链。

- 检查 `[文本](相对路径)` 与 `[文本](相对路径#锚点)`；跳过外链、纯锚点、围栏代码块、行内代码与测试夹具的
  数据目录；报告文件:行号，三件套。行内代码必须跳过：调研时的一次粗扫恰好把本文里反引号中的两个示例
  报成了断链。
- 变异：围栏代码块不跳过；行内代码不跳过；锚点不剥离；外链当成相对路径。

阶段二收口：阶段审计。

### 阶段三：角色、验收与收口

#### T15 — `syndica-dev`：本仓项目配置、提示词、Reviewer

**Files:** `.syndica.yaml`、`docs/agents/{orchestrator,planner,explorer,worker,reviewer}.md`、
`tests/cli/test_repo_config.py`。

- 内容见 D15、D17。`workspace.verify` 是本仓全部门禁（含 T14）。角色轮数初值：Orchestrator 24、Planner 8、
  Explorer 10、Worker 24、Reviewer 10，按 T18 的真实运行再调。
- 用例：配置经 `load_config` 通过并能冻结；每份提示词的正文就是对应文件的内容（路径写错时清单会把路径
  字符串本身当成提示词）；提示词里的字面大括号已转义；`topology show syndica-dev` 恰有一个 root；Orchestrator
  冻结后的提示词列出 Reviewer；每份提示词有 git 禁令锚词与对 `AGENTS.md` 的引用；Reviewer 与 Orchestrator
  提示词里两条标记行的写法能被公开契约里的语法解析；verify 与 CI 门禁逐字一致；`protected` 覆盖 D15 列出的
  路径。
- 变异：删掉 Orchestrator 覆盖里的 Reviewer；删掉 Worker 的 git 禁令；verify 少一条；标记行写法与语法不一致。

#### T16 — 根 Agent 失败时报错去嵌套

**Files:** `cli/host.py`、`tests/cli/test_host.py`。

- 用例断言 stderr 里「下一步」恰好出现一次，且是底层错误自己的那条。变异：退回嵌套；丢掉底层「下一步」。

#### T17 — L2 E2E：公开 CLI + 脚本化 LLM

**Files:** `tests/integration/test_self_hosting.py`、`tests/fixtures/self_hosting/`（数据文件：小 Python 包
与一份带断链的文档，测试时在 tmp 里还原；不会被 pytest、ruff、pyright 与链接检查当成仓库内容）。

七个场景，全部在 tmp 副本里；每个场景结束时断言夹具源目录的 sha256 不变；涉及 git 的场景按 T12 隔离 git
环境并设本地身份：

1. **失败迭代（代码自举）：** Worker 第一次改错，pytest 失败；读失败摘要后第二次改对；Reviewer 在最后一次
   改动之后给出 pass。handoff `verified`，列出改动与两次 pytest。
2. **安全拦截：** `git push`、`setsid git push` 与 `rm -rf build` 被拦，Agent 换一条不危险的路完成任务。
3. **人工确认：** 可信轨迹里同一批两次 `cat /etc/hosts`，批准一次、拒绝一次，只执行被批准的那次。
4. **只读：** `--no-write` 下 Write 调用得到「未知工具」，工作区除 `.syndica/` 外逐字节不变，状态
   `read_only`。
5. **文档自举：** 修一处断链并跑 `check_links.py`，handoff 列出改动与这次检查。
6. **不伪装成功：** Agent 自称完成而复核失败，退出 1，`verification_failed`，不询问 git 收尾。
7. **git 收尾：** 复核通过、Reviewer pass，stdin 回答 `1`，本地 bare remote 收到一个只含本次改动的提交；
   回答 `3` 时没有新提交。

- 至少一个场景用本仓 `.syndica.yaml` 的角色与拓扑（复制配置与提示词，只换 `workspace.verify`）。
- 脚本化 LLM 按角色分发（测试 helper）；派活串行，调用顺序确定。
- 命令里的解释器写 `sys.executable` 的绝对路径；场景命令只用 POSIX sh。

#### T18 — 真实模型 E2E、文档与收口

- 在副本里（D22）用默认轮换、`--handoff` 跑四个场景，每次记下请求数、额度与 429 次数、误报次数：
  1. 文档自举：修一处断链或过期描述，跑链接检查；
  2. 代码自举：夹具包里修一个失败测试；
  3. `--no-write` 规划一个小改动；
  4. 在 Syndica 自己的代码上完成一个真实的小改动（例如补一条缺失的测试），handoff `verified`，选 `1`
     推送到本地 bare remote。这一条对应 spec §15 的完成判据「框架能修改自己的代码」。
  另做 D22 的一次真实 SSH 推送。
- 文档：README「开发模式」一节（D4 的 Flag 交互、D14 的三个选项、D15 的信任模型、D5 的威胁模型）；runbook
  加一步；路线图与 spec §15 标 M2 状态；spec §5.6 写入注入点与可选规则、§12 写入最小脱敏器是 M3 之前的
  收口、§17 追加本计划的决策；交接文档与 next-todos；`AGENTS.md` 加三条硬约束：Agent 命令环境只按
  白名单透传、形似密钥的名字拒收；handoff 与宿主提交是仅有的落盘路径，都先过脱敏；Agent 不执行 git
  写命令，提交与推送只由宿主在人选择之后执行。
- 写 M2 收口审计（`docs/audits/`），遗留项登记（见下）。

---

## 验证

| 层 | 内容 | 何时跑 |
|---|---|---|
| 门禁 | ruff format / check、pyright、check_deps、pytest；T14 之后加 check_links | 每个任务提交前（本地）；CI 可用时每次 push |
| L2 | T17 的七个场景；T6、T7、T11、T12 里经公开 CLI 的用例 | 随全量测试，零成本 |
| 真实模型 | T18 的四个场景与一次真实 SSH 推送 | 手动，只在副本里 |
| 审计 | 阶段一、阶段二各一次；M2 收口审计 | 阶段结束时 |

### 本地验证协议（CI 额度用完期间）

GitHub Free 每月 2000 分钟的 Actions 额度在 2026-10 已经用完，下个计费周期才恢复。这期间「CI 绿」由下面的
本地协议代替。按开销从低到高依次做，不同时跑两件重负载：

1. **开发中**只跑定向测试（本任务改到的模块对应的测试文件）。
2. **提交前一次**跑全量门禁，内容与 CI 逐字相同，本机约 40 秒。
3. **碰到命令执行的任务**（T4、T5、T6、T11、T12、T17），把受影响的 shell 相关测试模块再用
   `SYNDICA_TEST_SHELL=/bin/dash` 跑一遍，近似 Ubuntu 的 `/bin/sh`（开关在 T4 里加）。
4. **自我变异**只对本任务的定向测试文件跑，跑手按 sha256 还原。
5. **对抗验证**：每个任务起 1 个只读子代理；风险高的任务（T3、T4、T11、T12）起 2 个并行，一个专门找反例，
   一个专门自创作者没想到的变异。约束写进它们的任务说明：
   - 只看本任务的 diff 与计划里对应的裁决和契约；
   - 实验只在 `/tmp` 下做，单个实验不超过 60 秒；不跑全量测试，不起常驻进程；
   - 每条结论给出能复现的命令。
   作者对关键结论先自己复现再采纳，前两轮计划评审都是这样做的。子代理的开销主要是远端模型的 token，本地
   负载只来自那些限时的小实验。
6. commit → push，不等 CI。

阶段审计时另加一次：全量测试在 `SYNDICA_TEST_SHELL=/bin/dash` 下完整跑一遍。

额度恢复之后的第一件事：在 Linux 上对当时的 HEAD 补跑 CI，失败按缺陷处理，修好再继续。push 在这期间照常
进行；每次 push 触发的 CI 会因额度不足而不启动，这不算失败信号。

---

## 风险、回滚与恢复

- **CI 在 Linux 上暴露平台差异。** CI 可用时每个任务都要绿；修不掉的用例改成跨平台断言或写明原因，不静默跳过。
- **CI 额度用完期间没有 Linux 覆盖。** 本地用 `/bin/dash` 近似 Ubuntu 的 shell，只覆盖 shell 语义，不覆盖
  内核与文件系统的差异（进程与信号、大小写敏感的文件系统、`/tmp` 的路径形态）。额度恢复后补跑 CI，把这段
  时间积累的差异一次查出来。
- **规则误报挡住正常的开发命令。** 每发现一个误报，先补一条「放行」用例再修；T18 统计误报次数。
- **规则漏报。** 规则是尽力而为（D5）；补漏时同样先补用例。
- **免费模型完成不了代码自举。** 记录证据；机制已由 L2 证明；换一个支持工具调用的 `:free` 模型重试。
- **免费档的速率上限。** 几个 Agent 同时读大文件会撞每分钟 token 上限；轮换会退避与换平台，T18 记录 429 次数，
  必要时再收紧提示词。
- **自动推送把不该推的东西推上 GitHub。** 推送只在人选 `1` 或显式 `--git-mode push` 时发生，前置条件与执行
  步骤见 D14；CI 在推送后再跑一遍。
- **内核进程管理改动的回归。** T4 改的是所有 Bash 调用的必经之路；除新增用例外，全量测试与 L2 场景都要在
  Linux（CI）与 macOS（本机）上各跑一遍。
- **回滚。** 每个任务独立提交；T2–T4 的内核改动都是增量，回滚不影响已有调用方。

---

## 遗留项归属

- **本计划关闭：** 路线图归属表中的「卫生：基础 CI」。
- **预计新增：**
  - 最小脱敏器并入统一 `Redactor`，handoff 与宿主提交改走它；CLI 改从文件读 API key、不经环境变量（M3）；
  - 项目配置首次使用时的信任确认；清单 `system_prompt` 路径写错时静默当成提示词正文；按角色区分的危险策略（M4）；
  - `untrusted_high_risk` 的 `ask` / `allow` 让不可信轨迹里的高风险调用比宿主策略更松（M5，与溯源链一起重审）；
  - 沙箱与网络访问控制、Portal 层的正式输出上限、`setsid` 与二次 fork 逃出进程组（M5）；
  - CLI 里的复核、handoff、git 收尾与环境过滤在 M9 的 server 需要复用时迁出 `syndica.cli`（surface 之间
    不能互相依赖）；
  - 从 `syndica-dev` 提炼通用开发预设（有需求时）；CI 矩阵（M12）。

---

## 决策日志

- 2026-09-30，用户确认：开发期危险规则与命令环境白名单对所有 CLI run 生效；Agent 可以自己 review、自己
  提交推送，但必须先问人：复核通过且 Reviewer 为 pass 时给人三个选项，选定后由宿主执行 git，无人值守时用
  `--git-mode` 预先选定；handoff 写到 `.syndica/handoffs/`，M2 先做最小脱敏器；提示词放 `docs/agents/`；
  CI 只跑 Ubuntu + Python 3.11。
- 2026-09-30，第一轮独立评审后的修订：宿主执行 git 改为参数数组直调，加完整性快照、提交前后的 blob 核对、
  运行前被忽略文件的排除、暂存 diff 的密钥检测；交给人看的文本先转义；git 写命令与删根改由钩子无条件拦截，
  CLI 拒收 `allow`；危险规则按分词器的实际行为重写，名字统一到 `shell.*`；Reviewer 结论改读 Reviewer 自己的
  文字；handoff 的机器可读部分改成版本化的 ASCII front matter；`workspace.env` 改名 `workspace.pass_env`；
  新增 `workspace.protected`、内核命令执行任务、`CliOutcome`、退出码 3、威胁模型、阶段审计；删掉 trace2
  相关处理（本机没有这项配置）。
- 2026-10-01，第二轮独立评审（一份核对第一轮的修复、一份重新审视）后的修订，关键结论都经作者复现：
  - 规则对「程序词」改用两种相反的保守方向：只用一个真实程序词时 `setsid git push` 能绕过无条件拦截；
  - 内核补进程组清理与等待上限：实测超时 1 秒的 `sleep 4 &` 4 秒后才返回，`close()` 之后后台进程仍在；
    超时改为返回部分输出，`ExecResult` 追加 `timed_out`，Bash 的 `meta` 带两条流；
  - 宿主 git 全部在宿主环境里跑并启用字面路径：文件名 `*` 会把人未提交的工作带进提交；hooks 按宿主环境解析；
  - 提交与推送的前置条件分开，只提供满足条件的选项；新增 `push_failed`，定下退出码与 `status` 的优先级；
  - Reviewer 结论要求结局是 `COMPLETED`，且开始与结束时的工作树指纹都等于快照 A；角色与序号改在插件工厂
    里记（`AGENT_SPAWN` 不带角色、触发得晚）；
  - 脱敏把「不交给 Agent」与「要打码」分开：按名字判出的 `SSH_AUTH_SOCK` 是路径，片段匹配会把普通代码误判
    成密钥、拦下提交；提交闸不用片段匹配；
  - 锁改到工作区根的目录描述符上；忽略规则文件内置为受保护路径；glob 语义写死；提问前清掉缓冲输入；摘要列出
    改动路径；转义扩到所有 run 的交互提示；
  - 规则 ID `shell.remove_workspace_root` 改名 `shell.workspace_root_removal`，与既有 ID 的名词形式一致；
  - 威胁模型补上：同用户进程读得到宿主启动时的环境；`setsid` 与二次 fork 逃出进程组清理。
- 越界路径维持「问人」而不是路线图写的「默认拒绝」：它只在可信轨迹里生效，读过命令输出之后按
  `untrusted_high_risk` 走、默认就是拒绝；保留它给「人工确认」一个真实的触发点。handoff 的「下一步」由
  宿主按状态生成，不取模型文字。

## 意外发现

- T1：`astral-sh/setup-uv` 从 v8 起只发布完整版本标签，`@v10` 解析不到，第一次 CI 在 Set up job 就失败。
  改成 `@v10.2.0`，并加了一条要求完整标签的用例；`actions/checkout` 仍有 `v7` 这样的大版本标签。
- 计划评审：本机全局 git 钩子 `~/.git-hooks/pre-push` 属主 root、权限 0777，任何本地进程都能改写它，而它在
  每次 `git push` 时执行。这是本机环境问题，已告知用户。
- 2026-10-01：GitHub Free 的 Actions 月度额度（2000 分钟）用完，推送触发的 CI 不再启动（GitHub 报的是付款
  或消费上限问题）。改按本地验证协议推进，额度恢复后补跑。

## 完成复盘

（收口时填写。）
