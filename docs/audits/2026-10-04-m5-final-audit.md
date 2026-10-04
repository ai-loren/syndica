# 里程碑 5 收口审计：执行面完整化 + 不可信内容

日期：2026-10-04
范围：[M5 计划](../plans/2026-10-04-m5-execution-plane.md) 的 T1–T11（执行面）与 §12.2 的不可信内容
基线：T1 `1fe969c`、T4 `cf4a91d`、B-13/B-14 `a84a889`、T2/T3 `cea34f5`、计划 `a94a7f1`、阶段一评审修复 `cbb09f4`、
T5 `95ca7fd`、T6 `d26e516`、T7 `e8ff335`、阶段二评审修复 `c2a61b2`、T8 `8e8a4fc`、T9 `458a267`、T10 `3828b00`、
阶段三评审修复 `b198d39`；收口评审的修复与 T11 的文档与本文各一次提交
验证方式：本地验证协议（Actions 额度用完期间，见路线图「当前主线约束」）

## 结论

spec §15 的完成判据——**把里程碑 1 的最小本地执行补成完整协议（Workspace / Portal 全量接口、git 版本层、
worktree 与快照隔离、共享模式下的写入冲突检测、伪终端），同期落地不可信内容的标记与溯源链（§12.2）**——
由一套端到端用例（经公开 API 与 `syndica` 命令行、脚本化模型，随全量测试零成本地跑）与一次真实运行共同证明：

- `Workspace` / `Portal` 是公开协议，`WorkspaceContract` 是任何后端都要过的契约套件；`LocalWorkspace` 过它，
  伪终端六件套与 `Terminal` 工具在契约里有定向用例。版本层（`.syndica/vcs`，git 对象格式、从不在它上面跑 git）、
  worktree / 快照隔离（逐文件合并、冲突显式返回）、共享模式的乐观并发（没读过就覆盖是冲突）、文件污点与产物
  溯源图（命令改过的、外部改的、读过不可信之后写的都读回成不可信）各有定向用例与安全矩阵（54 格逐格断言）。
- 真实运行（Gemini `gemini-3.5-flash-lite`，免费档，4 次请求，约 7109 token，trace `b4c489ec`）：在一个开了版本层
  与文件污点的临时工作区里，Worker 用 `Write` 建 `notes.md`（"hello from m5"）、用 `Edit` 改成
  "hello from milestone five"、再 `Read` 读回并原样答出。版本层记下了两个版本（`14f8ac0c…` → `4d9b0421…`），
  `content()` 从 git 对象里逐版本取回；trace 是 `syndica.trace/3`，一条 artifact 边把 `Read` 的 span 连回写它的
  `Write` span（`artifact.path=notes.md`、`artifact.trust=trusted`、`artifact.version=sha256:4d9b…`）。API key 的
  原文与任何 16 字符片段都不在 trace、工作区或输出里；进程环境里全程没有 key（子壳内 source、壳外清零）。

不可信内容这一半的对抗性质（脚本执行升级、跨路径洗白、就地改写、收紧跟着数据走）由测试套件与变异证明——
它们是本仓效果正确性的权威（见测试策略），真实运行只证明版本层、文件污点与产物溯源的接线在真模型下成立。

## 完成判据 × 证据

| 判据（路线图 M5） | L1/L2（脚本化，零成本，随全量测试） | 真实模型（仓库之外的临时工作区） |
|---|---|---|
| Workspace / Portal 全量接口 + 契约套件 | `WorkspaceContract` 对 `LocalWorkspace` 全过；`read_versioned`、CAS、`poll(since)`、PTY 六件套各有用例 | `Write` / `Edit` / `Read` 经路由跑通 |
| git 版本层（Python 写对象，从不跑 git） | `test_versions`：对象格式、脱敏在落盘前、脱敏版本不可恢复、按触碰路径记历史、锁按进程号回收、符号链接守卫 | `.syndica/vcs` 9 个对象，`notes.md` 两个版本，`content()` 逐版本取回 |
| worktree / 快照隔离 + 逐文件合并 | `test_isolation`：原子建副本、按版本合并、冲突存 `.theirs`、大小写改名是冲突、嵌套隔离、合并期写入不丢 | — |
| 共享模式写入冲突检测 | `test_router_versions`：没读过就覆盖是 `WriteConflict`；CLI 用例先读后写 | 单 Agent 顺序写，未触发 |
| 伪终端 | 契约里的 PTY 用例（控制字符、按键、半行、Ctrl-C、作业控制）；`Terminal` 工具 docstring 守卫 | — |
| 不可信内容标记与溯源链（§12.2） | `test_file_taint`、`test_executor_provenance`、`test_download_execute`、`test_security_matrix`（54 格） | artifact 边记成 trusted（可信工具写的内容读回可信） |
| 执行不可信本地脚本强制升级 | `exec.untrusted_script` + 安全矩阵：`AllowAll` 也拦；stdin 喂、绝对路径、`cd` 各有用例 | — |
| trace 记产物与溯源（schema /3） | `test_trace_model` / `test_trace_recorder` 的 /3 断言 | `artifact.path/version/trust` 边 |

## 计划之外的偏差

- **命令输出照旧不可信，不按命令真实读到的内容收窄溯源。** 用户裁决（2026-10-04）：真正的隔离边界是沙箱化的
  Workspace 后端；只有后端知道一条命令读了什么，收窄随它落地。M5 只交协议与契约套件，AGENTS.md 与 spec §17 #94
  照此改写。
- **B-15 的收窄是部分的。** 工具声明 `__tool_external_input__`，不带外部内容的工具抛出的异常按这次调用的溯源算；
  裸值替身与中止块仍按不可信（裸值没有标记可读）。一个 Agent 之内仍以窗口为上界，收窄随沙箱后端。
- **D-64 只做了工具调用那一侧。** 结论出自被收紧的 Module 时 `HookResult.untrusted` 为真、触发它的工具调用收紧，
  PRE/POST hook 分发也铺了污点格子；扩展切点在模型侧切点里被触发的那一轮模型请求不收紧，归里程碑 6。
- **`shell.download_then_execute` 的纯判定留有形状缺口**（tee 写文件、`wget -P`、`mv`/`cp` 改名、`uv run` 等）。
  真实姿态下这些下载落地的文件都不是基线，执行它由执行器的 `exec.untrusted_script` 兜住；纯判定（无路由的消费者）
  的这些形状登记为遗留。`exec.untrusted_script` 对命令里有多个 `cd` / 变量目录的脚本放过（`effective_cwd` 折不出
  目录时宁可不升级、不拿错目录误判），有窗口上界兜着。
- **回放重不出 `exec.untrusted_script` 门（收口 C3，登记为遗留）。** T10 的 CLI 给 Runtime 接了路由，于是真实 run
  会按文件污点升级执行不可信脚本的调用；而回放构造 Runtime 时不接路由（`_untrusted_scripts` 恒空），跑过
  Bash 写脚本再执行的 CLI trace 会在门上报一处分歧。它是**响亮的**（回放报分歧，不是静默错误），只影响这类 trace。
  正确的修法要把执行器的文件信任答案记进回放文档（版本号升到 v4）并在回放侧按记录接回 `_configure_file_trust`——
  这是一次有意的带版本的改动，不在时间紧的收口里赶，登记到后续。

## 公开契约核对

- `syndica.kernel.__all__` 由 129 增到 141：新增 `GrepHit`、`VersionStore`、`VersionEntry`、`LocalIsolator`、
  `MergeReport`、`MergeConflict`、`FileTaint`、`WorkspaceRouter`、`PtyCapture`、`PtyStatus` 等（协议与后端实现、
  第三方写 Workspace 后端要用），没有删掉任何名字。`IsolatedCopy`、`TaintAware`、`ToolOutcomeObserver`、
  `ISOLATIONS` 经子模块承诺、刻意不进 `kernel.__all__`。
- `Portal`：新增 `read_versioned`、写方法交回写下的版本、`grep_text` 交回 `GrepHit`（不是字符串）、`poll(since=)`、
  PTY 六件套。`GrepHit` 是公开类型，第三方后端要构造它。
- `TaintAware` 协议新增 `name_trust`（列表的名字信任，与 `file_trust` 的内容信任分开）；`WorkspaceRouter` 同步新增
  `name_trust`。`Tool` 新增 ClassVar `__tool_session_cwd__`（缺省 `False`；会话式工具如 `Terminal` 置真）。
- `BaseAgent.observe_tool_outcomes`（同步观察者，在所有 `POST_TOOL_USE` 之后、结果块落轨迹之前）；
  `Runtime(workspace=)`、`Runtime.merge_isolated` / `discard_isolated`；`spawn(isolation=)` / `Delegate` / `Fork` 的隔离参数。
- 格式：trace 升到 `syndica.trace/3`（新增 `artifact.*` 边属性与 `provenance.*` span 属性，旧版本照读）。
- `Tool.__tool_external_input__` 是新的 ClassVar（None 按会带外部内容算）。

## 遗留

路线图「遗留与缺陷登记」里本里程碑新增 / 已修的行：B-08、B-13、B-14、B-15、B-18、D-40、D-62、D-63、D-64 标「已修」；
新登记的 M5 后续：命令输出不收窄溯源（沙箱后端）、`Bash` 写文件绕过乐观并发（接受）、交互式 shell 历史展开（沙箱后端）、
隔离副本经 `..` 写兄弟副本（沙箱后端）、合并不合可执行位（M7）、模型侧 D-64（M6）、`download_then_execute` 的形状缺口
（接受，执行器兜住）、`effective_cwd` 折不出多 `cd` 目录（接受）、控制终端启动器只能在 Linux 上证明（M12）。

## 评审与变异

| 评审 | 结论 | 处置 |
|---|---|---|
| 阶段一（Portal / 路由 / PTY） | 修进 `cbb09f4` | 切点、路由 `read_versioned` 记已读版本、BashOutput 完整 meta、契约补用例 |
| 阶段二（版本层 / 隔离） | 15 条 | `c2a61b2`：大小写改名删文件（Blocking）、合并期写入丢失、`.theirs` 占名、目录/文件互换、隔离不嵌套、丢可执行位等 |
| 阶段三（污点 / 执行器 / 危险规则） | 2 Blocking / 4 High / 4 Medium / 2 Low | `b198d39`：trust_of 先路径后版本的洗白（F1/F2/F4 同根）、写入侧两处（F5/F6）、`exec.untrusted_script` 三处绕过（F3）、误报两处（F7）、问人后复查（F8）、`-XPOST` 误报（F9）、broker 代理收紧（F11）、hook 扩展污点（F12）；F10 的 6 条存活变异逐条补用例 |
| 收口 | 0 Blocking / 4 High / 3 Medium / 4 Low | 本次提交。见下 |

**收口评审（0 Blocking）**：C6（High）写入身份下界在 `tool.execute` 内被跳过——被收紧的 Module 的 hook 在一次
可信工具的执行窗口里写的文件记成可信：改成 `call_trust` 与身份下界取更严。C2（High）我在 F1 修复里把叶子符号链接
也 `resolve()` 了，于是打开后新建、指向基线文件的链接，名字经 `Glob` 洗白成可信：把名字信任（列表）与内容信任
（Read / 脚本）分开，名字按叶子是不是基线符号链接判，基线符号链接按 `lstat` 指纹记。C1（High）`effective_cwd` 对
赋值/`if`/花括号后面的持续 cd 认不出（按程序词位置找而非 `tokens[0]`），对子壳 `(cd x)`、管道左侧 `cd x | …`
的不持续 cd 误折到错目录（这两类交回 None）。C4（Medium）`_watch_all` 对没有值相等的普通对象 deepcopy 必然不等，
一个只旁观切点的被收紧 Module 会把结果误标不可信、宿主关不掉：只盯值可比的参数。C5（Medium）`Portal.grep_text`
改成交回 `GrepHit` 却没有契约用例：补一条（含冒号路径）。C7（Medium）`Terminal` 的伪终端跨 send 保持 cwd、而闸按
根解析 `input` 里的相对脚本：工具声明 `__tool_session_cwd__`，会话式工具的相对脚本一律放弃、只判绝对的工作区内
脚本。C8（Low）新加的「含花括号就跳过」让 `sh planted.s{h,}` 绕过：只跳过精确的 `{}` 占位符。C11（Low）
`VersionStore.content` 两次读之间对象被换掉的 TOCTOU：按内容哈希复核。C9（Low，同命令内改名/非 cat 喂 stdin）、
C10（Low，大小写不敏感卷与 `~` 的误报，安全方向）登记为遗留。

本里程碑自己的变异（每条都对改动的文件按 sha256 先校验、施加、清字节码、跑用例、还原、再校验）：阶段一、T5、T6、T7、
阶段二修复、T8、T9、T10、阶段三修复（9 条，含两条 Blocking 的洗白、`restore` 的信任、`position != index` 的兄弟判据、
观察者改不动参数、dedup 摘除）、收口修复（8 条：C1 的子壳与管道左侧、C2 的叶子符号链接、C4 的普通对象、C6 的写入
下界、C7 的会话 cwd、C8 的花括号、C11 的内容复核）各一轮，全部杀死。

## 验证

```text
uv run ruff check .                                          通过
uv run ruff format --check .                                 通过
uv run pyright                                               0 errors
uv run python scripts/check_deps.py                          通过
uv run python scripts/check_links.py                         通过
uv run pytest -q                                             8712 passed
```

推送前对提交的树做了导出验证（把这次提交单独检出到临时目录、在那里跑上面每一道门禁）。
