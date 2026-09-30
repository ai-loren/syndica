# 里程碑 2：自举（syndica-dev）

> **执行者须知：本文件是需求，不是参考。** CLI 参数、`.syndica.yaml` 键、危险规则 ID、脱敏规则 ID、
> `meta` 键与 handoff 结构一旦发布就是兼容承诺；执行中不得为了让测试变绿临时改名或缩小断言范围。

**状态：待评审。** 五项裁决已由用户确认（2026-09-30，见「决策日志」），其余内容待通读。

**Goal:** 让 Syndica 在一份仓库里完成一次受控修改：读约束、规划、改文件、跑检查、读失败、迭代修复，
最后交出一份可审计的 handoff（交接报告）。检查结果由宿主独立复核，不采信 Agent 的自述；复核通过且
Reviewer 给出 pass 时，由人选择是否让宿主提交、推送。护栏（CI、危险规则、命令环境白名单）先于任何
让 Agent 改仓库代码的能力落地。

**宿主**指运行 Agent 的 syndica 进程本身（这里是 CLI）：它建工作区、装配 Agent、转发人的输入、弹出
确认，代码是写死的，不受模型影响。

**Architecture:** 自举能力落在 CLI surface 与本仓的项目配置（`.syndica.yaml` + 提示词）上。内核只补
两处通用缺口：宿主在冻结目录、装配拓扑时注入危险判定与处置；三族可选的危险规则（git 状态、工作区外
路径、删除工作区根）。开发期的命令结果结构化与输出上限是一个 CLI 侧的 Runtime Module。handoff 由宿主
汇总，经最小脱敏器后写进 `.syndica/handoffs/` 并打印到终端；git 的提交与推送由宿主在人选择之后执行。

**Tech Stack:** 不新增运行依赖。CI 用 GitHub Actions 与 `astral-sh/setup-uv`。

**依据：** spec §15（M2 完成判据）、§5.5、§5.6、§6、§9.1、§12.2、§12.3；路线图「M2 自举」；
`AGENTS.md` 的硬约束与测试纪律。

---

## 现状校准（2026-09-30 调研）

路线图写于 M1 收口时。开工前对照代码核实，下面这些事实改变了 M2 的做法：

1. **Agent 的 Bash 跑不了本仓检查。** CLI 宿主给 `LocalWorkspace` 传 `env={}`，子进程环境被整个替换：
   `uv` 找不到，`HOME` 为空，`python3` 解析到系统的 `/usr/bin/python3`。
2. **Bash 不受工作区约束。** 文件工具有路径监狱，Bash 以用户身份执行任意命令。默认规则只有 4 条
   （提权、下载即执行、递归强删、写裸设备），`cat ~/.config/syndica/gemini.env` 不会被拦。
3. **宿主换不了处置策略。** 危险判定与处置只挂在 `AgentOptions` 上；`AgentCatalog.freeze` 与
   `Runtime.assemble`（内部调 freeze）构造选项时不接收它们，CLI 的所有 Agent 只能用内核默认。
4. **跑过命令之后，高风险调用一律按 `untrusted_high_risk` 处置。** Bash 输出标 `UNTRUSTED`，
   `provenance_of` 是「轨迹里出现过」的上界，且随派活带给子 Agent。`disposition_of` 对「高风险 + 不可信」
   直接用这个 Flag 的处置、不咨询宿主策略：默认 `reject` 一律拦下；`ask` 一律交人确认，**包括宿主策略
   本来要拦的命令**。宿主策略只在可信轨迹里起作用。
5. **命令输出不截断，超时丢输出。** `LocalWorkspace.run` 的 `truncated` 恒为 False；超时时整个进程组被
   杀、已产生的输出丢弃，工具输出变成一条三件套错误。Bash 前台默认超时 30 秒，本仓全量测试约 25 秒。
6. **Reviewer 要靠同名覆盖 Orchestrator 才进得了派活名单。** `Orchestrator.compose_system_prompt` 取
   `catalog.get(cls.__name__)`，`extends: Orchestrator` 的新角色拼出的仍是 Orchestrator 那份名单。
   `Delegate` 能派给目录里任何角色；`assemble` 冻结整个目录。
7. **CLI 配置只认 `version` / `agents` / `topologies`。** `.gitignore` 已忽略 `.syndica/`（本地运行时
   状态）。
8. **没有 CI。** 仓里没有 `.github/`；本机 `gh` 已装、未登录。
9. **无人值守不会卡在确认上。** CLI 在 stdin EOF 时把确认按 reject 回答。
10. **L2 有走公开 CLI 的现成路径。** `RuntimeCliHost(model_factory=...)` 加 `main(argv, host, io)` 只换
    LLM（见 `tests/integration/test_cli_human_scenario.py`）。`runner/scenario.py` 用默认规则、自己的策略
    与空环境，不适合验收开发期策略。调用未注册的工具得到一条「未知工具」结果（列出可用工具），不抛。
11. **框架目前没有任何落盘路径。** M1 的 CLI 不写文件；handoff 文件将是第一条，按 `AGENTS.md` 的硬约束
    必须先过脱敏收口。

## 路线图条目对照

| 路线图 TODO | 本计划 |
|---|---|
| `syndica-dev` 拓扑与 manifest | T14；放在本仓项目配置，不进内核或 CLI 代码（D12） |
| 提示词，明确禁止破坏 git 状态 | T14；Agent 的 git 写命令由规则拦下（T3、T5），提交与推送由宿主执行（T11） |
| run 的开发者配置：工作区 root | 不加参数，工作区仍是 cwd（D16） |
| run 的开发者配置：允许的命令 | 不做命令白名单（D6） |
| run 的开发者配置：默认验证命令 | `workspace.verify`（T4），宿主复核（T10） |
| run 的开发者配置：是否允许写文件 | `run --no-write`（T6） |
| doctor 子项 | T12 |
| 安全命令策略 | T3（规则）+ T5（处置） |
| 测试结果解析器 | T7（解析）+ T8（接进 Bash 结果） |
| handoff 生成器 | T9（脱敏）+ T10（复核与 handoff） |
| `--no-write` dry-run | T6 |
| 基础 CI | T1，先于一切 |

路线图的静态验收与 E2E 验收逐条落在 T3、T6、T7、T14、T16、T17 的用例里。

---

## 已确认边界

### 做

- 基础 CI，排在所有任务之前。
- 内核两处通用补口：冻结与装配时注入危险判定和处置；三族可选危险规则。
- CLI：命令环境白名单；`workspace` 配置段；开发期危险策略；`run --no-write`；`run --handoff`（宿主
  复核、脱敏后写文件并打印）；git 收尾（人选择后由宿主提交、推送）；`doctor` 扩展；根 Agent 失败时报错
  去嵌套。
- 最小脱敏器：handoff 落盘前必经。
- 检查结果解析器，以及把它接进 Bash 结果的开发期 Runtime Module（结构化 `meta`、输出上限、命令记录）。
- 本仓项目配置：`syndica-dev` 拓扑、Reviewer、开发期提示词；`scripts/check_links.py`。
- L2 E2E（公开 CLI + 脚本化 LLM）七个场景；真实模型 E2E（手动，仓库副本里）四个场景。

### 不做

- 沙箱、容器、网络访问控制：M5。
- 命令白名单（D6）。
- 统一 `Redactor` 协议与 trace / 会话 / 记忆的脱敏：M3。M2 的最小脱敏器只服务 handoff 这一条落盘路径
  （D18）。
- `Runtime.start()` 或任何新的生命周期入口。
- Artifact、Verifier、四值裁决的内核实现：M7。handoff 里的「未验收」「验收失败」是文本标记。
- `--workspace` 参数：工作区仍是 cwd（D16）。
- CI 矩阵：M12。
- Agent 直接执行 git 写命令：规则一律拦下，提交与推送只由宿主在人选择之后执行（D4、D19）。

### TDD 接缝

测试只落在这些公开接缝上，不测私有 helper：

1. `main(argv, host, io)` 与 `RuntimeCliHost(model_factory=...)`：CLI 输入、stdout / stderr、退出码。
   L2 E2E 与 CLI 用例都走这里。
2. `DangerJudge(rules).judge(tool, args)` 与 `disposition_of(...)`：规则与处置。
3. `AgentCatalog.freeze(...)` / `Runtime.assemble(...)`：注入的判定与处置出现在建好的 Agent 上，通过
   「一次命中规则的调用被这样处置」这一可观察结果来证明，不读私有字段。
4. `load_config(path)`：配置键校验。
5. 检查解析器与脱敏器的纯函数：样例文本进，结构化结果或脱敏文本出。

---

## 设计裁决

**D1 护栏先行。** 顺序固定为 CI（T1）→ 内核补口（T2、T3）→ CLI 护栏（T4、T5）→ 其余。路线图归属表写明
「自举开始改代码之前先有自动门禁」；而本计划自己的每个提交也要过这道门禁。

**D2 危险选项的注入点是冻结与装配。** `AgentCatalog.freeze(tools, models, host=None, *,
danger_judge=None, danger_policy=None)` 与 `Runtime.assemble(..., danger_judge=None,
danger_policy=None)`，交给每个建出的 `AgentOptions`。keyword-only、默认 `None`，对现有调用方无声。
不放进 `Runtime` 构造器：Runtime 不建 Agent，工厂才建。不做按角色区分：M2 一份策略管全体，按角色的
策略归 M4。

**D3 三族新规则进内核 `danger.py`，不进 `DEFAULT_RULES`。** 规则要复用 `danger.py` 里已经加固过的分词
（分隔符、重定向、wrapper、`nice -n 19` 这类选项取值），而 `DangerRule` 的约定是 `matches` 只收整条
命令文本、解析形态不公开给第三方。在 surface 重写一份分词，等于把那些坑重踩一遍。三族规则由宿主按需
组合；名字进 `danger.__all__` 与 `_WITHHELD`，不进承诺面，M12 冻结公开 API 时再定。

- `git.state_change`：git 的子命令不在只读集合里（`status diff log show blame ls-files ls-tree grep
  rev-parse describe shortlog cat-file merge-base name-rev version help`）。先跳过 git 的全局选项
  （`-C <路径>`、`-c <键=值>`、`--git-dir=…`、`--work-tree=…`、`--no-pager`、`-P`）再找子命令。
  `branch`、`tag`、`remote`、`config`、`stash` 在某些写法下只读、另一些写法下改状态，一律按改状态算。
- `shell.outside_workspace`：参数与重定向目标里指向工作区外的路径——工作区外的绝对路径（`/dev/null`
  `/dev/stdout` `/dev/stderr` `/dev/tty` 除外）、`~` 开头、`$HOME` / `${HOME}` 开头、从工作区根出发会
  逃出去的 `..` 路径；`--选项=值` 取值来判。命令词本身豁免：执行 `/usr/bin/python3` 是常态。路径包含
  按规范化后的路径比较，不按字符串前缀（`/a/b` 与 `/a/bc` 是兄弟目录）。
- `shell.remove_workspace_root`：`rm` 的目标是工作区根本身（`.`、`./`、根的绝对路径）或根下全部
  （`*`、`./*`）。

三族都是 HIGH。工作区根在构造规则时解析一次；`matches` 只做字符串运算，不碰磁盘。已知盲区写进
docstring：变量展开与命令替换算不出路径；引号里一段代码中嵌的路径（`python3 -c "open('/etc/x')"`）
不认；执行工作区外的可执行文件不在覆盖面内。真正的隔离在 M5。

**D4 开发期处置策略对所有 CLI run 生效；Agent 不直接执行 git 写命令。** 规则表是 `DEFAULT_RULES` 加 D3
的三族；策略按规则 ID 处置：默认四条、`git.state_change`、`shell.remove_workspace_root` → BLOCK；
`shell.outside_workspace` → CONFIRM；其余 → ALLOW。CLI 本来就在用户机器上执行，git 与工作区外路径的
保护对任何 run 都成立；M1 的 CLI 还没发布，行为变化可以接受。

git 写命令对模型一律拦下；提交与推送改由宿主在 run 结束、人做出选择之后执行（D19）。这样做而不是
「每条 git 命令弹确认」有三个原因：仓库的硬约束是授权只放行当次调用，给模型一个「这次 run 里都可以
提交」的授权正是它禁止的；宿主执行才能卡住「复核没过就不提交」；提交哪些文件、推到哪里由宿主决定，
被注入内容说服的模型推不上去别的东西。

与现状第 4 条的交互必须写进 README 和 runbook：Agent 跑过任何命令之后，高风险调用都按
`untrusted_high_risk` 处置。默认 `reject` 时全部拦下，这是无人值守要的结果。交互使用时若想让人确认，
加 `--flag untrusted_high_risk=ask`；代价是此后被拦的 git 命令也会变成「问人」，由人把关。这是 spec
§12.2 与决策 #75 的既定语义，M2 不改。

**D5 命令环境按白名单透传。** 宿主（内核不读环境变量）从自己的环境里只取这些名字交给工作区：
`PATH HOME USER LOGNAME LANG LC_ALL LC_CTYPE TMPDIR XDG_CACHE_HOME UV_CACHE_DIR`，再加
`workspace.env` 列出的名字，都只取实际存在的。名字匹配
`KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION`（不区分大小写）时，写在
`workspace.env` 里也拒收，报三件套错误说明原因；三个模型平台的 key 变量都含 `KEY`，由这条规则覆盖，
不另列名单（另列的那份删掉也不会有用例变红）。值在建工作区时拷一份。run、chat、doctor 三处建工作区都用
同一个函数。

**D6 不做命令白名单。** 白名单对真实模型太脆：Agent 会自然地用 `find`、`sed`、`head`、`wc`，列不全就
频繁被拦，列全了又失去意义。M2 的防线是：规则拦危险形态、环境白名单不给密钥、文件工具有路径监狱、git
写命令对模型关闭、提交前有复核与人的选择。真正的隔离在 M5。

**D7 `run --no-write` 在冻结之前拿掉写工具。** 从每个角色的工具名单、以及拓扑槽的 `extra_tools` 里去掉
`Write Edit Bash BashOutput KillBash`，这些工具根本不注册给任何 Agent。Bash 能写盘，只拿掉 Write 与 Edit
不够。与 `--session` 互斥（会话沿用 spawn 时的工具）；chat 不支持。干跑不做宿主复核，也不做 git 收尾，
handoff 标「干跑」。依据路线图静态验收「写工具不可被注册给 Worker」。

**D8 检查结果解析器是纯函数，识别只用于摘要。** 输入是命令文本加 Bash 工具的输出文本（`running /
exit_code / truncated / stdout / stderr` 格式，或超时时那条三件套错误），输出
`CheckResult(tool, status, counts, summary)`。`tool` ∈ `pytest ruff-check ruff-format pyright check-deps
check-links other`；`status` ∈ `passed failed error timeout no-output`。按命令文本的正则识别工具，这一步
不参与任何安全判定，所以不需要分词。

**D9 开发期 Runtime Module（名字 `dev`，不声明 Flag）。** 每个 Agent 一个 `POST_TOOL_USE` 钩子，只处理
Bash 前台结果与已结束的 BashOutput 结果：

- 识别为检查命令时写 `meta['check']`；
- 输出超过 10,000 字符时保留头 2,000、尾 8,000，中间换成一行说明截掉了多少、怎么看全；写
  `meta['truncated_chars']`；
- 每次调用记进服务 `runtime.dev_log`：Agent id、命令、退出码、检查结果；
- `trust` 原样交回。内核只允许收紧，但钩子自己也不得交出更松的结果。

保尾是承重的：pytest、ruff、pyright 的汇总都在输出末尾。输出上限是 M2 的临时措施，正式的上限属于 M5
的 Portal 层（`ExecResult.truncated`）。免费模型的上下文与额度都有限，一次 pytest 全量失败输出能吃掉
一整轮预算。

**D10 `run --handoff` 由宿主复核，脱敏后写文件并打印。** run 结束后，宿主（不是 Agent）依次：

1. 在同一工作区、同一环境里逐条跑 `workspace.verify`，每条上限 900 秒；
2. 用运行前后的 `git status --porcelain`，加上运行前已有改动的文件的 sha256，列出本次改动（排除
   `.syndica/`）；
3. 汇总 `dev_log` 里 Agent 实际跑过的检查；
4. 组装 Markdown，经 D18 的脱敏器之后写到工作区的 `.syndica/handoffs/<UTC 时间戳>-<短 id>.md`，并把同一份
   内容打印到 stdout（取代原本的最终回答），文件路径打到 stderr。第一次创建 `.syndica/` 时在里面写一个
   内容为 `*` 的 `.gitignore`，在没有忽略它的仓库里也不会被误提交。

宿主跑 git 与 verify 直接走 Portal，不经危险判定：宿主不是模型。

**D11 `doctor` 在 Agent 的环境里自检。** 输出 env（只列名字，从不列值）、git 状态、verify 命令用到的每个
可执行文件（`command -v`）。`doctor --verify` 再逐条跑 verify。现状第 1 条那种「Agent 环境里找不到 uv」
要在跑任务之前暴露。

**D12 `syndica-dev` 放在本仓的项目配置里。** 仓库根的 `.syndica.yaml` 加 `docs/agents/*.md` 提示词，纯
声明式。

- 角色：同名覆盖 Planner、Explorer、Worker 与 Orchestrator（Orchestrator 保留 `impl`，`subagents` 加上
  Reviewer，见现状第 6 条）；新增 Reviewer（Read、Grep、Glob、Bash）。
- 拓扑 `syndica-dev`：一个 root singleton 槽放 Orchestrator；其余角色由 `Delegate` 从目录建出。

依据 spec「新角色优先写声明式清单 + 提示词」「改组织结构就改拓扑声明，不改代码」。给别的仓库用的通用
开发预设，等有真实需求时从这份配置里提炼。提示词放 `docs/agents/`，按公开前可读的质量写；`.syndica/`
被 git 忽略，放不了要入库的东西。

**D13 `scripts/check_links.py`。** 检查 Markdown 相对链接指向的文件存在（锚点不查），进本仓的 verify 与
CI。依据路线图的文档自举 E2E「运行格式 / 链接检查」；docs 里交叉引用很多，目前没有任何检查。

**D14 根 Agent 失败时报错去嵌套。** 最近的 Runtime 错误是 SyndicaError 时，stderr 直接给它的三件套，
前面加一句「Agent X 以错误结束」，不再在「实际」里套一层「期望 / 实际 / 下一步」、出现两行「下一步」。

**D15 CI 只跑 Ubuntu + Python 3.11。** 触发条件是 push 到 main 与 pull request；跑与本地逐字相同的门禁，
不需要任何密钥。Linux 与本机 macOS 的差异（`/bin/sh` 是 dash、`/tmp` 不是 `/private/tmp` 的符号链接）
在 T1 里修掉，不跳过测试。矩阵归 M12。push 后用 `gh run watch` 确认，需要用户先执行一次
`gh auth login`。

**D16 工作区仍是 cwd。** 在副本里跑就 `cd` 进去。多一个 `--workspace` 就要回答配置文件相对谁、提示词
路径相对谁，M2 没有这个需求。

**D17 真实模型 E2E 只在仓库副本里跑。** 用 `git clone` 出一份（handoff 要用 git 列改动），删掉原 remote，
另建一个本地 bare 仓库当 remote（验证推送），`uv sync`；key 文件在副本之外。

**D18 最小脱敏器，handoff 落盘前必经。** 放在 CLI surface（`cli/redaction.py`），是框架唯一一条落盘路径
的收口；M3 建统一 `Redactor` 时把它并进去，§12.3「唯一收口」由 M3 兑现。规则（每条一个稳定 ID，替换成
`[REDACTED:<规则 ID>]`，不留原文）：

- `secret.env_value`：宿主环境里名字匹配 D5 密钥正则、且值长度不少于 8 的变量，按值逐字替换，长的先换；
- `secret.private_key_block`：`-----BEGIN … PRIVATE KEY-----` 到对应 END 整块；
- `secret.bearer_token`：`Authorization: Bearer` 之后的令牌；
- `secret.openrouter_key`、`secret.openai_key`、`secret.google_api_key`、`secret.github_token`、
  `secret.aws_access_key`、`secret.slack_token`：各自的公开格式。

按值替换是最可靠的一条：宿主知道自己环境里的密钥是什么，格式规则只兜住它不认识的那些。脱敏发生在组装
完成之后、写文件与打印之前；stdout 与文件是同一份脱敏后的文本。

**D19 git 收尾：人选择，宿主执行。** 只在 `run --handoff` 且不是干跑时进行。

先检查前置条件，任一不满足就只留 handoff，并在「未完成原因」里写明哪一条不满足：工作区是 git 仓库；run
正常结束；配置了 verify 且宿主复核全过；Orchestrator 的最终回答里有一行 `Reviewer 结论: pass`（缺这一行
按 `unverified` 算）；本次有改动；没有「运行前已改动、本次又改」的文件（分不清哪些是人的改动）。

前置条件都满足时：

- 没给 `--git-mode` 时，在终端问人三选一：`1` 提交并推送、`2` 只提交、`3` 我来处理。读到 EOF 或空行
  按 `3`，因此 `</dev/null` 的无人值守 run 永远不会自动提交。
- 给了 `--git-mode commit|push` 时不问，直接按它执行；`--git-mode manual` 等同于选 `3`。

执行时：只 `git add` 本次改动的文件（从不 `git add -A`，从不碰 `.syndica/`）；提交标题取最终回答里
`提交标题: …` 那一行，缺失时用目标的第一行，截到 72 字符，经 D18 脱敏；正文列出复核结果；作者用本机 git
配置，不加任何 trailer。推送只推当前分支到它已配置的上游，从不 `--force`；没有上游或推送被拒时退出 1，
handoff 与 stderr 写明原因与下一步。提交后用 `git log -1 --format=%B` 核对消息与生成的一致：本机 git 被
trace2 守护进程改写过提交消息，宿主提交同样要用 `-c trace2.eventtarget= -c trace2.eventnesting=` 覆盖。

---

## 公开契约

### CLI

| 用法 | 语义 |
|---|---|
| `run --no-write` | 写工具不注册给任何 Agent；与 `--session` 互斥 |
| `run --handoff` | 复核、脱敏后写 `.syndica/handoffs/…md`，stdout 从最终回答换成同一份 handoff |
| `run --handoff --git-mode manual\|commit\|push` | 预先选定 git 收尾，不再询问；只能与 `--handoff` 同用 |
| `doctor --verify` | 自检之后逐条跑 `workspace.verify` |

`run --handoff` 的退出码：0 表示 run 正常结束、verify 全过或没有配置，且选定的 git 收尾（若有）成功；1
表示 run 失败、没有回答、任一 verify 失败，或选定的提交 / 推送失败；2 沿用参数错误。前置条件不满足而没有
执行 git 收尾不算失败，只在 handoff 里写明。`doctor` 在任一可执行文件找不到、或 `--verify` 下任一失败时
退出 1。

### `.syndica.yaml`

```yaml
version: 1
workspace:
  env: [NO_COLOR]                  # 可选：在固定白名单之外再透传的环境变量名
  verify:                          # 可选：宿主复核与 doctor --verify 逐条运行
    - uv run ruff format --check .
    - uv run pytest -q
agents: []
topologies: {}
```

未知键、非字符串、空白字符串、重复项、形似密钥的名字都报 `ConfigError`（三件套）。

### 规则 ID（稳定标识）

- 危险规则（级别均为 HIGH）：`git.state_change`、`shell.outside_workspace`、`shell.remove_workspace_root`。
- 脱敏规则：D18 列出的九个 `secret.*`。

### `meta` 与服务

- Bash 前台结果：`meta['check'] = {'tool', 'status', 'counts', 'summary'}`，键序固定；截断时另有
  `meta['truncated_chars']`。
- `runtime.dev_log` 交出只读快照，改它不影响下一次读。

### handoff 结构

节标题与状态词固定：

```text
# Syndica handoff

状态: 已验收 | 验收失败 | 未验收 | 干跑 | 运行失败

## 目标
## 改动
## Agent 跑过的检查
## 宿主复核
## Agent 的结论
## git 收尾             （写明选了什么、执行结果，或为什么没有执行）
## 未完成原因          （只在验收失败、未验收、运行失败时出现）
```

---

## 任务与进度

每个任务的流程：先写红测试 → 实现 → 定向测试 → 全量门禁 → 自我变异（清 `__pycache__`、新文件先
`git add`、sha256 基线覆盖本轮会改到的每一个文件）→ 独立审查 → commit → push → CI 绿。T1 之后每个
任务都要等 CI 绿再开下一个。

### T1 — 基础 CI

**Files:** `.github/workflows/ci.yml`、`tests/test_ci_workflow.py`，以及 Linux 上暴露的差异修复。

- 步骤：checkout → setup-uv（固定版本）→ 装 Python 3.11 → `uv sync --frozen --extra openai --extra dev`
  → 五道门禁。同一 ref 的旧运行自动取消；超时 15 分钟。
- `tests/test_ci_workflow.py` 读 workflow，断言门禁命令逐字出现且顺序固定，与 README「Development」
  一节的门禁清单一致。否则删掉一道门禁不会有任何东西变红。
- 验收看一次运行日志：五道门禁各自出现且成功。不为了看红灯往 main 推坏提交。
- 前置：用户执行一次 `gh auth login`。
- 变异：删掉 workflow 里任意一步，用例单独变红。

### T2 — 内核：冻结与装配时注入危险判定和处置

**Files:** `kernel/catalog.py`、`kernel/runtime.py`、`tests/kernel/test_catalog.py`、
`tests/kernel/test_assemble.py`。

- 两个参数原样交给每个 `AgentOptions`，入口走 `validate_danger_options`；报错里的参数名是宿主写下的那个。
- 用例：两个以上角色都拿到同一判定与处置，其中包括类式角色（Orchestrator）；不传时行为与现在逐字相同；
  assemble 路径同样生效；传类对象而不是实例时报错指着 `danger_policy`。都通过「一次命中自定义规则的调用
  被这样处置」来观察。
- 变异：只转判定不转处置；assemble 不转交；只给声明式角色、漏掉类式角色。

### T3 — 内核：三族可选危险规则

**Files:** `kernel/danger.py`、`tests/kernel/test_danger.py`、`tests/kernel/test_public_api.py`
（`_WITHHELD`）。

- API：常量 `GIT_STATE_CHANGE`，工厂 `outside_workspace_rule(root)` 与 `workspace_root_removal_rule(root)`。
  `root` 必须是已存在的绝对目录，否则三件套报错。
- 用例按成员逐条写（常量表的每个成员单独施加变异都要变红）：只读集合的每个子命令放行、其余子命令拦下；
  每种全局选项写法都找得到子命令；每个伪设备放行；每种越界形态（绝对路径、`~`、`$HOME`、`${HOME}`、
  逃出的 `..`、`--opt=/x`、`>` / `2>` / `<` 的目标）拦下；兄弟目录（`/a/bc` 对根 `/a/b`）拦下；命令词
  是绝对路径时放行；URL 放行。
- 同一形态换平凡写法仍然命中：管道、`&&`、`;`、子 shell、`\git`、`command git`、`env X=1 git`、
  `X=1 git`、多一次重定向。放宽与收紧两个方向都有用例。
- `DEFAULT_RULES` 的规则 ID 元组逐字不变（一条用例钉住）。
- 变异：只读集合逐个删成员；伪设备逐个删；路径包含改成字符串前缀；命令词不豁免；`=` 不拆；不看重定向
  目标。每条都要单独施加即变红。

### T4 — CLI：`workspace` 配置段与命令环境白名单

**Files:** `cli/config.py`、`cli/host.py`、新模块 `cli/environment.py`、`tests/cli/test_config.py`、
`tests/cli/test_host.py`。

- `CliConfig.workspace`（`env`、`verify`，默认为空），配置根加 `workspace` 键。
- 用例：Agent 的 Bash 执行 `printenv` 看得到 PATH 与 HOME，看不到宿主环境里的 `GEMINI_API_KEY`、
  `OPENROUTER_API_KEY`、`FOO_TOKEN`，也看不到一个不在白名单里的普通变量 `ZZZ_PLAIN`；`workspace.env:
  [FOO]` 生效；`workspace.env: [MY_API_KEY]` 被拒，报错说明为什么；run、chat、doctor 三处各一条用例，
  证明用的是同一份环境。
- 变异：密钥正则逐个删分支（每个模式一条用例）；漏掉 chat 路径；白名单换成「全量减黑名单」
  （`ZZZ_PLAIN` 用例变红）。

### T5 — CLI：开发期处置策略接进 run、chat、doctor

**Files:** 新模块 `cli/policy.py`、`cli/host.py`，以及对应用例。

- 规则表与策略见 D4；表外的 HIGH 按 BLOCK。
- 用例经公开 CLI 与脚本化 LLM：`git push` 被拦，Agent 拿到拦截说明后换路径完成；可信轨迹里
  `cat /etc/hosts` 触发确认，stdin 批准后执行一次，同样的调用第二次再问；跑过一次 Bash 之后，同样的
  调用在默认 Flag 下直接拒绝、在 `--flag untrusted_high_risk=ask` 下交人确认；`2>/dev/null` 不触发；
  拓扑路径（assemble）同样生效。
- 变异：`shell.outside_workspace` 从 CONFIRM 改成 ALLOW；assemble 路径不传策略；表外 HIGH 放行。

### T6 — `run --no-write`

**Files:** `cli/main.py`、`cli/host.py`，以及对应用例。

- 参数、帮助文本、与 `--session` 互斥的三件套错误、退出码。
- 用例：脚本化 Worker 调 Write 与 Bash 都得到「未知工具」结果，工作区逐字节不变；Read、Grep、Glob
  仍然可用；拓扑槽 `extra_tools` 里的 Bash 同样被拿掉。
- 变异：只拿掉 Write 与 Edit；只处理目录、不处理槽；拿掉之后仍把原目录交给 assemble。

### T7 — 检查结果解析器与真实样例

**Files:** `cli/checks.py`、`tests/cli/fixtures/checks/`、`tests/cli/test_checks.py`。

- 样例全部由真实工具在夹具上跑出来，`fixtures/checks/README.md` 记录生成命令与工具版本。
- 覆盖路线图要求的五类：通过、失败、收集错误、超时、无输出。每个工具至少有通过与失败两类，pytest 五类
  齐全。
- 用例：每个样例解析出的结果与期望整表相等，并断言 `list(d)` 的键序；未识别的命令归 `other`，只留退出码。
- 变异：pytest 汇总只认 passed；收集错误当成失败；超时当成失败；退出码取错行。

### T8 — 开发期 Runtime Module

**Files:** 新模块 `cli/dev_module.py`、`cli/host.py`（`_modules()` 装上它），以及对应用例。

- 行为见 D9。
- 用例：`meta['check']` 的键序；截断边界（恰好 10,000 字符不截，10,001 截）；截断后汇总行还在；两个
  Agent 各跑一条命令，`dev_log` 里互不串线；钩子交回的结果仍是 UNTRUSTED；非 Bash 工具不碰；超时那条
  三件套输出记为 `timeout`。
- 变异：保头不保尾；阈值差一；`dev_log` 交出内部列表；工具名匹配改成包含（`BashOutput` 被当成
  `Bash` 处理后台句柄）。

### T9 — 最小脱敏器

**Files:** 新模块 `cli/redaction.py`、`tests/cli/test_redaction.py`。

- 规则与顺序见 D18。样例一律是合成的假值，不放真实凭证。
- 用例：每条规则一个命中样例和一个形似但不该命中的样例；按值替换长的先换（一个值是另一个值的前缀时
  都被完整替换）；值短于 8 的不按值替换；替换后的文本里不残留原值的任何 8 字符以上片段；规则 ID 整表
  相等（成员与取值一次钉住）。
- 变异：逐条删规则（各自单独变红）；按值替换改成短的先换；长度下限差一。

### T10 — `run --handoff`：宿主复核与 handoff

**Files:** 新模块 `cli/handoff.py`、`cli/host.py`、`cli/main.py`，以及对应用例。

- 流程见 D10；结构见公开契约。
- 用例：verify 全过 →「已验收」、列出改动与命令、退出码 0；verify 失败 →「验收失败」、列出失败命令与
  摘要、退出码 1；没有配置 verify →「未验收」、退出码 0；Agent 失败 →「运行失败」、退出码 1，stderr
  同时给出错误；不是 git 仓库 →「改动」一节写明无法列出；运行前已有改动、本次又改了的文件被列出；
  文件内容与 stdout 逐字相同；`.syndica/.gitignore` 只在第一次创建；宿主环境里放一个假密钥，并让 Agent 的
  最终回答里出现它，文件与 stdout 里都只剩占位符。
- 变异：退出码忽略 verify；把 Agent 自称「测试通过」当成复核结果；预先脏的文件不比 sha256；没有配置
  verify 时写「已验收」；先写文件后脱敏（用例在写入函数上断言收到的已是脱敏文本）。

### T11 — git 收尾

**Files:** `cli/handoff.py`（或新模块 `cli/git_finish.py`）、`cli/main.py`、`cli/host.py`，以及对应用例。

- 行为见 D19。
- 用例：前置条件逐条不满足时都不问、不执行，handoff 写明是哪一条；满足时读到 `1` 提交并推送到本地 bare
  remote、`2` 只提交、`3` / 空行 / EOF 什么都不做；`--git-mode push` 不询问直接执行；只有本次改动的文件
  进提交（预先放一个无关的未跟踪文件，提交后它仍未跟踪）；`.syndica/` 从不进提交；提交消息与生成的逐字
  一致、没有任何 trailer；没有上游时退出 1 且下一步写明怎么设上游；`--git-mode` 不和 `--handoff` 同用时
  参数错误。
- 变异：EOF 当成 `1`；`git add -A`；Reviewer 结论缺失时当成 pass；复核失败仍然询问；推送带 `--force`。

### T12 — `doctor` 扩展与 `doctor --verify`

**Files:** `cli/host.py`、`cli/main.py`，以及对应用例。

- 输出行顺序固定：config、models、workspace（已有）、env、git、每个可执行文件（去重保序）、verify
  （仅 `--verify`）。
- 用例：Agent 环境里找不到 uv 时退出 1，下一步指向 PATH 或 `workspace.env`；env 行只有名字；不是 git
  仓库；`--verify` 失败的那一条带解析摘要。
- 变异：env 行打出值；可执行文件检查用宿主环境而不是 Agent 环境（用一个只在宿主 PATH 里的假命令区分）。

### T13 — `scripts/check_links.py`

**Files:** 脚本、`tests/test_check_links.py`；CI、README「Development」与 `AGENTS.md`「常用命令」各加
一步。2026-09-30 扫描：全仓 44 条相对链接，没有断链。

- 检查 `[文本](相对路径)` 与 `[文本](相对路径#锚点)`；跳过外链、纯锚点、围栏代码块与行内代码里的链接；
  报告文件:行号，三件套。
- 行内代码必须跳过：调研时的一次粗扫恰好把本文里反引号中的两个示例报成了断链。
- 变异：围栏代码块不跳过；行内代码不跳过；锚点不剥离；外链当成相对路径。

### T14 — `syndica-dev`：本仓项目配置、提示词、Reviewer

**Files:** `.syndica.yaml`、`docs/agents/{orchestrator,planner,explorer,worker,reviewer}.md`、
`tests/cli/test_repo_config.py`。

- 内容见 D12。`workspace.verify` 是本仓的全部门禁（含 T13）。角色轮数初值：Orchestrator 24、Planner 8、
  Explorer 10、Worker 24、Reviewer 10，按 T17 的真实运行再调。
- 提示词必须写清：先读 `AGENTS.md` 的硬约束、错误消息、测试三节；不执行 git 写命令（提交与推送由宿主在人
  选择之后做）；跑检查时带 `timeout` 与 `-q --tb=short`；Worker 每次改动后跑最小相关测试；Reviewer 看
  `git diff`，按硬约束逐条过，结论写成 `pass` / `partial` / `block` / `unverified` 之一并给理由；
  Orchestrator 按「Planner → Explorer → Worker → Reviewer」推进，Reviewer 给 `block` 时带着理由回到
  Worker，最终回答里必须有 `Reviewer 结论: …` 与 `提交标题: …` 两行（D19 读它们）。
- 用例：配置经 `load_config` 通过；`topology show syndica-dev` 恰有一个 root；`agents list` 含 Reviewer；
  冻结后 Orchestrator 的提示词列出 Reviewer（证明同名覆盖生效）；每份提示词都有 git 禁令的锚词与对
  `AGENTS.md` 的引用；Reviewer 提示词列出四个结论词；Orchestrator 提示词写明那两行的格式；verify 与 CI
  的门禁逐字一致。
- 变异：删掉 Orchestrator 覆盖里的 Reviewer；删掉 Worker 提示词里的 git 禁令；verify 少一条；
  Orchestrator 提示词里的「Reviewer 结论」行格式与 D19 的解析不一致。

### T15 — 根 Agent 失败时报错去嵌套

**Files:** `cli/host.py`、`tests/cli/test_host.py`。

- 行为见 D14。用例断言 stderr 里「下一步」恰好出现一次，且就是底层错误自己的那一条。
- 变异：退回嵌套；丢掉底层错误的「下一步」。

### T16 — L2 E2E：公开 CLI + 脚本化 LLM

**Files:** `tests/integration/test_self_hosting.py`、`tests/fixtures/self_hosting/`（小 Python 包与一份
带断链的文档）。

七个场景，全部在 tmp 副本里；每个场景结束时断言夹具源目录的 sha256 不变（「原仓库不被改动」）：

1. **失败迭代（代码自举）：** Worker 第一次改错，pytest 失败；读失败摘要后第二次改对，pytest 通过。
   `--handoff` 为「已验收」，列出改动与两次 pytest。
2. **安全拦截：** `git push` 与 `rm -rf build` 被拦，Agent 换一条不危险的路完成任务。
3. **人工确认：** 可信轨迹里 `cat /etc/hosts` 触发确认，批准只放行这一次；第二次再问，拒绝后不执行。
4. **干跑：** `--no-write` 下 Write 调用得到「未知工具」，工作区逐字节不变，handoff 为「干跑」。
5. **文档自举：** 修一处断链并跑 `check_links.py`，handoff 列出改动与这次检查。
6. **不伪装成功：** Agent 自称完成而 verify 失败，退出码 1，handoff 为「验收失败」，也不询问 git 收尾。
7. **git 收尾：** 复核通过、`Reviewer 结论: pass`，stdin 回答 `1`，本地 bare remote 收到一个只含本次改动
   的提交；同一流程回答 `3` 时仓库没有新提交。

- 至少一个场景用本仓 `.syndica.yaml` 的角色与拓扑（复制配置与提示词，只换 `workspace.verify`），证明
  随仓发布的配置能端到端跑通。
- 脚本化 LLM 按角色分发（测试 helper），派活是串行的，调用顺序因此确定。
- 命令里的解释器写 `sys.executable` 的绝对路径（命令词豁免），不依赖 CI 的 PATH。

### T17 — 真实模型 E2E、文档与收口

- 在副本里（D17）用默认轮换、`--handoff` 跑四个场景，每次记下请求数与额度：
  1. 文档自举：修一处断链或过期描述，跑链接检查；
  2. 代码自举：夹具包里修一个失败测试；
  3. `--no-write` 规划一个小改动；
  4. 在 Syndica 自己的代码上完成一个真实的小改动（例如补一条缺失的测试），handoff「已验收」，选 `1`
     推送到副本的本地 bare remote。这一条对应 spec §15 的完成判据「框架能修改自己的代码」。
- 文档：README 加「开发模式」一节（含 D4 的 Flag 交互与 D19 的三个选项）；runbook 加一步；路线图与
  spec §15 标 M2 状态；交接文档与 next-todos；`AGENTS.md` 加三条硬约束：命令环境只按白名单透传、形似
  密钥的名字拒收；handoff 是框架唯一的落盘路径，必经脱敏器；Agent 不执行 git 写命令，提交与推送只由宿主
  在人选择之后执行。
- 遗留项登记（见下）。

---

## 验证

| 层 | 内容 | 何时跑 |
|---|---|---|
| 门禁 | ruff format / check、pyright、check_deps、pytest；T13 之后加 check_links | 每个任务、每次 push（CI） |
| L2 | T16 的七个场景；T5、T6、T10、T11 里经公开 CLI 的用例 | 每次 push，零成本 |
| 真实模型 | T17 的四个场景 | 手动，只在副本里 |

---

## 风险、回滚与恢复

- **CI 首跑在 Linux 上暴露平台差异。** T1 内修掉；修不掉的用例要么改成跨平台断言，要么在登记表写明原因
  再处理，不静默跳过。
- **规则误报挡住正常的开发命令。** 每发现一个误报，先补一条「放行」用例再修；T17 里统计误报次数。
- **免费模型完成不了代码自举。** 记录证据；机制已由 L2 证明；换一个支持工具调用的 `:free` 模型重试。
- **自动推送把没看过的改动推上 GitHub。** 推送只在人选 `1` 或显式 `--git-mode push` 时发生，且要求复核
  全过与 Reviewer pass；只推当前分支、不强推；CI 在推送后再跑一遍门禁。
- **额度。** 真实 E2E 每次用 `--max-turns 12`，四个场景预计 150 次请求以内；Gemini 免费档按型号限额，
  OpenRouter 每天 50 次。
- **回滚。** 每个任务独立提交；T2、T3 的内核改动是纯增量，回滚不影响已有调用方。

---

## 遗留项归属

- **本计划关闭：** 路线图归属表中的「卫生：基础 CI」。
- **预计新增：** 最小脱敏器并入统一 `Redactor`，handoff 写入改走它（M3）；沙箱与网络访问控制（M5）；
  Portal 层的正式输出上限（M5）；按角色区分的危险策略（M4）；从 `syndica-dev` 提炼通用开发预设（有需求
  时）；CI 矩阵（M12）。

## 决策日志

- 2026-09-30，用户确认：
  - 开发期危险规则与命令环境白名单对所有 CLI run 生效（D4、D5）。
  - Agent 可以自己 review、自己提交推送，但必须先问人：复核通过且 Reviewer 为 pass 时给人三个选项，选定
    后由宿主执行 git；无人值守时用 `--git-mode` 预先选定（D4、D19）。
  - handoff 写到 `.syndica/handoffs/` 下的文件，M2 先做最小脱敏器（D10、D18）。
  - 提示词放 `docs/agents/`（D12）。
  - CI 只跑 Ubuntu + Python 3.11（D15）。

## 意外发现

（执行中记录。）

## 完成复盘

（收口时填写。）
