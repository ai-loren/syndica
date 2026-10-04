# 里程碑 5：执行面完整化 + 不可信内容

> **执行者须知：本文件是需求，不是参考。** 「公开契约」一节里的 Portal 方法、版本号与冲突的形态、`Terminal` 的
> 参数、`isolation` 的取值、`.syndica.yaml` 的新键、trace 的新边与属性一旦发布就是兼容承诺；执行中不得为了让
> 测试变绿临时改名或缩小断言范围。

**状态：执行中。** 用户 2026-10-04 授权：写计划、评审计划、执行计划一次做完，「同样不要做有过多耗时和等待的
事情」（见「验证节奏」与「决策日志」）。CI 仍推迟到 M1–M11 都做完之后。

**Goal:** 把 M1 的最小本地执行补成完整的执行面，并让不可信标记沿真实的数据流传递：两个 Agent 共享工作区写同一个
文件时后写方收到冲突而不是静默覆盖；要隔离时子 Agent 在 git worktree（没有 git 就是快照）里干活，干完逐文件合并、
冲突显式报出；每次工具写入记一个版本，可以看 diff、可以回滚；交互式程序经 `Terminal` 跑在伪终端里，取消不留进程；
文件、消息、派活、扩展切点的负载都带着来源的信任级别，读到不可信外部内容之后的高风险命令默认拒绝，`ask` 等人，
`allow` 留记录；同一份工具输出出错时不再把整份上下文永久钉成不可信。

**Architecture:** `Portal` 协议补全（字节读写、删除、`stat` 给出内容版本、带版本前提的写、增量读后台输出、伪终端
六件套）。内核加一个 `WorkspaceRouter`：工具仍然共享一份，路由按当前 Agent 把调用分到它的工作区（共享的根或它
自己的隔离副本），并在这一层做乐观并发与文件污点记录。版本层在 `.syndica/vcs`：git 格式的对象由内核用 Python
读写，从不对它调 git，也从不碰用户自己的 `.git`。隔离策略是协议，内核给共享 / worktree / 快照三种。
溯源图记下跨边界的数据流（文件版本、消息、派活、分叉、broker、扩展切点），trace 用已经预留的 `artifact` 边记文件
版本的读写；一个 Agent 内部的溯源仍然是「产出这次调用的那份请求里的全部块」。

**Tech Stack:** 不新增运行依赖。伪终端用标准库 `pty` / `termios` / `fcntl`（只支持 POSIX），git 经子进程调用，
版本层没有 git 时退回纯 Python 的快照。

**依据：** spec §5.6（内置工具、背景化）、§6（Workspace / Portal、git 版本层、隔离默认值、会话式执行）、§7.2–§7.3
（`ArtifactRef`、产物进上下文）、§11.1（trace 因果链）、§12.2（不可信标记与溯源、处置矩阵）、§13（溯源失败按最坏
情况处理）、§15（M5 完成判据）；路线图「M5 执行面完整化 + 不可信内容」与「遗留项归属」；登记表 B-08、B-13、B-14、
B-15、B-18、D-40、D-62、D-63、D-64；`AGENTS.md`。

---

## 现状校准（2026-10-04 调研）

- `kernel/workspace.py`：`Portal` 有 `read_text` / `write_text` / `edit_text` / `glob_paths` / `grep_text` / `run` /
  `start` / `poll` / `kill` / `close`；`Workspace` 只有 `root` 与 `portal()`。没有字节读写、删除、版本、伪终端。
- `kernel/local_workspace.py`（800 行）：路径越界与私有目录（`.git`、宿主给的 `private`）的拒绝、进程组管理与
  超时终止都在这里。CLI 每次 run 开一份，`private=('.syndica/traces', '.syndica/handoffs')`。
- `kernel/tools/local.py`：八个工具全部共享一份 Portal；CLI 用 `local_tools(workspace.portal())` 建表，交给
  `catalog.freeze`，于是所有 Agent 共用同一组工具实例。没有守卫保证工具不绕过 Portal。
- `Bash` 的结果一律 `UNTRUSTED`，`Read` / `Grep` / `Glob` 一律 `TRUSTED`：读一个刚由 `curl` 落盘、或由读过网页的
  Agent 写下的文件，标记就丢了。
- `BashOutput` 每次交回到目前为止的全部输出，不是增量。
- 溯源：`context.provenance_of` 是「轨迹里出现过 `UNTRUSTED` 块」这个上界；执行器在批开头取一次，hook 改写参数后
  重取，多元素段一律按不可信（B-18）。`_tool_raised_result` / `_aborted_result` / 非 `ToolResult` 的 `_skipped_result`
  一律 `UNTRUSTED`（B-15）。
- `untrusted_high_risk`（reject / ask / allow）已经接在闸上（D-29 已关），处置矩阵的四格都有实现。
- trace：`EdgeKind` 有 `spawn` / `message`，`artifact` / `verification` 预留；schema `syndica.trace/2`。
- 回放把本地工具换成桩，结果（含 `trust`）按记录交回。
- 没有 Skill 的实现；没有拓扑槽的隔离字段；`.syndica/` 下只有 traces 与 handoffs。
- 本机 git 2.50.1；用户环境可能更旧或没有 git。

## 路线图条目对照

| 路线图条目 | 落在 |
|---|---|
| 完整定义 `Workspace` 与 `Portal` Protocol，并发布契约测试 | D1、D14、T1 |
| git 初始化可关闭、worktree 隔离、快照 fallback | D4、D6、T5、T6 |
| 共享模式乐观并发控制，冲突是 ToolResult | D2、D3、T2 |
| 背景进程三件套，增量读取 | D8、T3 |
| PTY 会话：create / send / capture / resize / kill / status | D7、T4 |
| 产物版本基础：路径到版本、版本到 diff、版本冲突 | D2、D5、T5 |
| 文件、命令、PTY、Skill、外部内容接入 ProvenanceGraph | D9、T7（Skill 尚不存在，见「不做」） |
| 收窄 M1 的保守上界，B-15 / B-18 / D-40 | D9、D10、T8 |
| B-08 / B-13 / B-14 | D11、T9 |
| AST 守卫、契约测试、冲突与 fallback 单测、PTY 定向测试、安全矩阵、溯源搜索范围的独立遍历 | T1、T2、T4、T6、T7、T9 |
| E2E 五个场景与五条标准 | T11 |
| D-62 / D-63 / D-64（M4 推过来的） | D10、T8 |

## 已确认边界

### 做

- `Portal` 全量接口与内核发布的契约套件；`LocalWorkspace` 与 `WorkspaceRouter` 都跑它。
- 共享模式的乐观并发、每次工具写入记版本、diff、回滚。
- 版本层（私有 git，没有 git 时快照）、三种隔离、逐文件合并。
- 后台输出增量读；伪终端与 `Terminal` 工具。
- 溯源图与文件污点、执行器标记按真实输入算、三条 M4 遗留、三条危险规则遗留、安全矩阵。
- CLI 接线（`workspace.versions`、私有目录、`Terminal` 进工具表）与五个 E2E 场景；一次免费模型真实运行。

### 不做

- docker / ssh / e2b 等远程 Workspace（backend 包，契约套件就是给它们的）。
- 终端屏幕的渲染（VT100 仿真要第三方库）：`capture` 交回去掉控制序列的输出流，见 D7。
- 通用的后台工具（spec §5.6 的 `QueryBackgroundTool` / `CancelBackgroundTool`，任意工具转入后台）：路线图 M5
  只要求后台**进程**；登记新遗留项，归 M8（长程任务需要它）。
- 拓扑槽的隔离：常驻的槽没有自然的合并时刻；隔离只经 `spawn` / `Delegate` / `Fork`。登记新遗留项，归 M8。
- 压缩与摘要的边：图里留着 `summarize` / `compress` 两类，由 M6 的压缩写入。
- Skill：仓里没有 Skill 系统。图里留着 `skill` 来源，实现 Skill 的里程碑按 spec §12.2 把正文缺省记成不可信。
- 一个 Agent 内部按「哪几块影响了这次调用」收窄溯源（理由见 D9）。

## 设计裁决

### D1 Portal 协议补全；新方法都有缺省语义，旧实现照旧能用

新增（全部 `async`，路径越界与私有目录照旧由实现拒绝）：

- `stat(path) -> FileStat | None`：`FileStat(version, size)`，`version` 是内容摘要 `sha256:<hex>`，不存在时 None。
- `read_bytes(path) -> bytes`、`write_bytes(path, data, *, expected=ANY)`、`remove(path, *, expected=ANY)`。
- `write_text` / `edit_text` 加 `expected=ANY`：`ANY` 不检查，`None` 要求文件不存在，字符串要求当前版本等于它；
  不满足时抛 `WriteConflict(path, expected, current)`。检查与写在实现内是一个原子步骤（本地实现按路径加锁）。
- `poll(handle, *, since=(0, 0))`：只交回两条流在偏移之后的部分；`ExecResult` 追加 `stdout_end` / `stderr_end`
  （字段只追加）。
- 伪终端六件套 `pty_create` / `pty_send` / `pty_capture` / `pty_resize` / `pty_kill` / `pty_status`，见 D7。

`WriteConflict` 是新的异常类型（不是 `ConfigError`）：它不是调用方用错了，而是世界变了；工具把它写成可恢复的
`ToolResult`。`Workspace` 协议不变（`root`、`portal()`），隔离与版本是另外两个协议（D4、D6）。

### D2 版本号是内容摘要；乐观并发的基准是「这个 Agent 上一次看到的版本」

- 版本只做相等比较；同样的内容同样的版本，回放与远程后端都算得出。
- `WorkspaceRouter` 按（Agent, 路径）记「看到的版本」：`Read` 读到、`Write` / `Edit` 写成之后都更新。
- `Write` 覆盖一个已存在的文件时，前提是这个 Agent 看到的版本；**它没读过这个文件就是冲突**（「先 Read」），
  这是「并发写不得静默覆盖」唯一做得到的形态：两个都没读过就各写一遍，后写的那个看不见先写的内容。
- `Edit` 在读过时同样以看到的版本为前提；没读过时按当前内容原子替换（`old_string` 必须恰好出现一次本身就是
  对内容的前提）。
- 冲突的 `ToolResult` 三件套：期望的版本、当前的版本（与改它的是工具还是外部）、下一步（重读再改，或换路径）。
- `Bash` / `Terminal` 改文件不经乐观并发，但会改版本，于是此后基于旧版本的写会冲突——正是要的行为。

### D3 一份工具、按当前 Agent 路由

工具实例仍然共享（`catalog.freeze` 绑定的那张表、工具的定义次序与前缀缓存都不变）。`WorkspaceRouter` 实现
`Portal`，用 Runtime 的「当前驱动的 Agent」把调用分到这个 Agent 的工作区；宿主代码（没有当前 Agent）落在根上。
看到的版本、文件污点、隔离的指派都在这一层，后端只管它自己那份目录。`Runtime(workspace=router)` 让 Runtime 能
给隔离的子 Agent 指派工作区并把文件事件记进溯源图；不给时行为与今天相同（没有隔离可用）。

### D4 版本层是 git 格式、但不调用 git：对象由内核用 Python 写

（计划评审 B1：一个不叫 `.git` 的私有 git 目录躲过了现有的三道防线——文件工具对 `.git` 的拒绝、
`shell.git_state_change`、宿主对顶层 git 目录的完整性快照——而 `Bash` 一句 `printf >>` 就能改它的 `config`
（过滤器、fsmonitor、`include.path`）。宿主下一次对它调 git，就在任何危险闸之外执行了 Agent 写下的代码，这是 Agent
本来没有的能力。）

- `.syndica/vcs` 里是 git 格式的对象与引用（zlib 压缩的松散对象、SHA-1 名字；blob、tree、commit），全部由内核用
  Python 读写；历史、diff（difflib 的统一 diff）、回滚、隔离的物化都在 Python 里。**内核从不对 `.syndica/vcs` 调
  git**，于是那里的任何文件都没法让宿主执行代码。它与 git 兼容：用户可以 `git --git-dir=.syndica/vcs log` 去看；文档写明
  它的 `config` Agent 写得到，在那里跑 git 之前先看一眼。
- 版本层是一份会活过文件本身的记录（用户删了文件，历史里还在），所以对象正文与提交消息落盘之前先过
  Redactor（AGENTS.md #8）。脱过敏的版本存的不是原文：历史里标明、按版本取不回来、不能回滚（回滚会把占位符
  写进文件），spec「脱敏与可回放冲突时选安全」；路径本身像密钥的写入不记（路径照原样进 tree 对象）。
- 用户目录有没有 `.git` 都一样对待：不建 `.git`、不提交到用户的历史、不改用户的索引与分支（spec §6.3「不得干扰其历史与
  分支」，AGENTS.md #9）。与 spec「目标目录不是 git 仓库则自动初始化」的字面不同：初始化的是版本层，不是用户的仓库。
- 三方文本合并（D6）：有 git 时调 `git merge-file -p`，但在工作区之外新建的空临时目录里、对三个临时文件、没有仓库
  （`GIT_CEILING_DIRECTORIES` 与工作目录都是那个临时目录）、`GIT_CONFIG_NOSYSTEM=1`、`GIT_CONFIG_GLOBAL=/dev/null`、
  Agent 的环境——它只读那三个文件。没有 git 时退回「两边都改了就是冲突」：这就是 spec 说的快照降级，版本与回滚照常。
- 危险规则把写进任何一段叫 `.syndica` 的路径算作宿主状态变更（与 `.git` 同级）：版本层与宿主记录的纵深防御。
- `.syndica.yaml` 的 `workspace.versions: true | false`（缺省 true）。关掉时没有版本、没有隔离；文件污点的基线不靠版本层
  （D9），所以关掉它不放松任何安全判据，项目配置可以写。

### D5 每次工具写入记一个版本；`Write` / `Edit` 交回 `ArtifactRef`

- 版本层：`record(path, data) -> version`、`history(path)`、`content(path, version)`、`diff(path, a, b)`、
  `restore(path, version)`、`checkpoint() -> 整棵树的版本`（给隔离用）。
- `Write` / `Edit` 写成后由路由记一个版本，结果的 `artifacts` 带 `ArtifactRef(path=…, version=…, producer=<agent>,
  is_deliverable=False)`；`kind` / `mime` 先按扩展名粗分，格式语义归 M7 的处理器。`artifact_id` 由工作区内的
  规范化相对路径派生（同一个文件的各个版本是同一个产物的不同版本，spec §7.1），不随写入者变。
- diff：文本给统一 diff；二进制只说大小变化，格式感知的 diff 归 M7。
- 回滚经路由写回、本身也记一个新版本；它是宿主与工具都能用的公开方法，不另加模型工具（M7 的产物工具再决定）。

### D6 隔离：共享（缺省）/ worktree / 快照；合并逐文件三方比较

- `IsolationStrategy` 协议：`isolate(parent, name) -> IsolatedWorkspace`；`IsolatedWorkspace.merge() -> MergeReport`
  与 `discard()`。三种实现：共享（交回父本身）、worktree（把父此刻的工作文件拷到 `.syndica/worktrees/<name>`，
  基线另存一份给三方合并用，不调 `git worktree`）、快照（拷到 `.syndica/snapshots/<name>`，只记基线的版本号）。
  副本从父的工作文件拷而不从版本层物化：版本层存的是脱过敏的内容，从它物化会把占位符写进子 Agent 的文件、
  再随合并写回父。两种隔离的差别在合并：worktree 有 git 时用 `git merge-file`，快照只做保守合并。没有 git 时
  请求 worktree 退回快照（spec：快照是 worktree 的降级路径），报告里写明。
- 合并：对子相对基线改过的每个文件——父没改过就用子的；两边都改了，worktree 调 D4 那样隔离的 `git merge-file` 三方
  合并，干净就用合并结果，有冲突标记就把这个文件列进冲突、父的那份不动；快照两边都改就是冲突。删除同理。逐文件
  而不是 `merge-tree`：老版本 git 也能跑，快照也能用同一套判据。合并写回父工作区的每个文件带着子 Agent 那份的
  文件污点（D9）。写回是条件写：前提是合并开始时读到的父版本，合并期间父那边又被改过就把这个文件列进冲突，
  不覆盖（共享工作区里别的 Agent 照常在写）。冲突的文件父那份不动，子 Agent 的那份另存成旁边的
  `<path>.<agent 前 8 位>.theirs`（条件写：已存在就不另存、报告里写明），模型读得到、合得了。合并之后副本删除，
  此后被叫醒的子 Agent 直接在共享工作区里干活（报告里写明）；`ExitMode.EXIT` 的隔离 Agent 结束即拆除，副本随之
  丢弃并记一条警告，列出丢掉的文件。
- 入口：`Runtime.spawn(..., isolation=...)`、`Delegate` / `Fork` 的 `isolation` 参数（缺省 `shared`）。`Delegate` /
  `Fork` 在子 Agent 这次工作结束后自动合并，合并报告进结果；宿主经 `Runtime.merge_isolated` / `discard_isolated`
  自己决定。隔离语义以发起分叉的源 Agent 的工作区为基准（spec §6.4）。
- 隔离状态（「你在隔离副本里，路径相对它」）作为环境事实注入子 Agent 的历史，不进 System Prompt（spec §5.2）。

### D7 伪终端：标准库实现，`capture` 交回输出流而不是屏幕

- `pty.openpty` + 子进程用从端做三条流、新会话；主端异步读进缓冲。只支持 POSIX，其它平台三件套报错。
- `pty_capture(handle, *, since=0, wait=0.0)`：交回偏移之后的输出，去掉 CSI / OSC 控制序列；`wait` 秒内没有新输出
  就交回空的。它是输出流，不是渲染后的屏幕（全屏程序看到的会是重绘序列去掉控制符之后的文字）；要屏幕的后端自己
  接 VT 仿真。
- `pty_resize` 设窗口大小并给进程组发 `SIGWINCH`；`pty_kill` 先 `SIGTERM` 整个进程组、宽限后 `SIGKILL`，关闭
  主端并回收；`Portal.close()` 收掉全部会话。每个工作区的会话数有上限。
- `Terminal` 工具：`action ∈ {create, send, capture, resize, kill, status}`；`__tool_command_args__ = ('command',
  'input')`——两者都会被 shell 执行；结果一律 `UNTRUSTED`（终端里的是外部输出）。

### D8 后台进程：增量读取

`BashOutput` 缺省只交回这个 Agent 上次查询之后的新输出（偏移按 Agent 与句柄记在路由里），`full=true` 交回全部；
结果写明是增量还是全部、各截到哪。`Bash` / `KillBash` 不变。

### D9 溯源图：一个 Agent 之内是窗口，跨边界跟着数据走

- **一个 Agent 之内**：模型的下一次调用依赖它被给到的每一块，所以一次调用的上游就是产出它的那份请求里的全部块。
  在一份窗口里挑「哪几块影响了它」只能靠猜，而猜错的方向不可回收（spec §13）。图记下每一轮请求的窗口边界：M6 的
  压缩把块换成摘要（摘要继承被摘要块的标记）之后窗口自然收窄，不必再改闸。
- **跨边界**：文件版本、消息、派活、分叉、broker、扩展切点的负载都带着来源的信任级别，标记跟着数据走，而不是
  跟着「出现过」。
- **文件污点**：工具写入的版本记成那次调用的溯源结论（窗口的结论与作者收紧取严）；`Read` / `Grep` / `Glob` 的
  结果取**出现在结果里**的文件的污点里最严的那个（`Grep` 是有匹配行列出的文件，`Glob` 是列出的文件——文件名也是
  数据）。一个版本是 `TRUSTED` 只有两种来路：
  打开时的基线里就是它，或者由一次溯源可信的工具写入写成；其它版本（`Bash` / `Terminal` / 外部改的）一律
  `UNTRUSTED`。基线是打开工作区时逐文件取的 (inode, 大小, ctime) 指纹（跳过 `.git` 与 `.syndica`），不哈希内容、
  不靠版本层：ctime 由内核在每次改动时设成当前时间，用户态写不回去，`touch -d` 伪造修改时间骗不过它。文件数超过
  上限时停下，没扫到的按不可信算，并记一条警告。
- **精确的执行器标记（B-15）**：工具抛出的异常文字的标记 = 这次调用的溯源结论与这个工具「读不读外部输入」取严。
  `Tool` 加 `__tool_external_input__: ClassVar[bool | None] = None`：`None` 是没声明，按读外部输入算（与
  `__tool_command_args__` 同样取不安全的一侧）；内核的工具逐个声明。非 `ToolResult` 的 hook 替身按作者算（M4 的
  收紧已经认作者）。
- **兄弟段（B-18）**：hook 改写参数后的重取把「本段里在此之前已经交回的兄弟结果」也算进去，而不是有兄弟就一律
  不可信：兄弟还没交回结果时，它的输出不存在，流不进参数。
- **D-40**：`PRE_TOOL_USE` 的 `args` 与 `tool_call` 进 M4 的投影比对：嵌套的就地写算那个回调写的，重取溯源；
  就地改 `tool_call`（名字、id、参数）直接拒绝并点名 hook——配对与宿主策略都按它判。
- 查询的搜索范围由一条独立遍历证明没有收窄（AGENTS.md「搜索范围本身也要被证明」）。

### D10 三条 M4 遗留

- **D-63**：`runtime.broker` 交出一个收紧代理：被收紧的 Module 的身份下 `publish` 的 `Message` 先把 `trust` 收成
  `UNTRUSTED` 再交给真正的 broker。标记是消息自己的字段，远程 broker 序列化之后照样在。
- **D-64**：扩展切点分发的结论里有被收紧的 Module 写的 `modified` 或 `skip` + `result` 时，外层的那次工具调用 /
  模型那一轮按不可信收紧（分发期间设的上下文标志，工具结果与模型侧的收紧都查它）；`HookResult` 追加
  `untrusted: bool = False`，让宿主代码也知道这个值不可信。
- **D-62**：执行器在每次调用收尾（`POST_TOOL_USE` 之后、结果块落进轨迹之前）发一条不经 hook 链的结局事件；开发期
  记录器改订阅它，任何 hook 的 `skip` 都截不断。

### D11 危险规则（B-08 / B-13 / B-14）

- **B-08**：同一条命令里先下载到文件再执行它（`curl -o f …; sh f`、`wget -O f … && bash f`、`… > f; ./f`）加一条纯规则，
  HIGH。跨调用的「先落盘、下一次再执行」走溯源：`danger` 里加纯函数 `executed_scripts(command)` 列出命令会当脚本
  执行的路径；执行器把这些文件的污点并进这次调用的溯源，任一不可信时把裁决提到 HIGH（规则 `exec.untrusted_script`，
  组合函数以污点为输入，仍是纯的）。
- **B-13**：wrapper 建「哪些选项吃下一个词」的表，与现在的兜底并存：表命中就精确，表没命中就退回收集全部。
- **B-14**：为 `)` 找一条能区分的输入；找不到就在登记表里写明它是等价变异及理由；续行正则补反斜杠加空格 / 制表符的用例。

### D12 安全矩阵

参数化覆盖 `RiskLevel × 溯源 × untrusted_high_risk × 宿主策略` 的每一格，断言处置与记录；同一批调用在
`max_concurrency ∈ {1, N}` 下逐块相同。四个 E2E 安全场景（拒绝、等人、放行留记录、经摘要 / 消息转发后仍能追溯到源）
另由 T11 跑真实 Runtime。

### D13 trace：`artifact` 边记文件版本的读写；不升 schema

一次工具调用读到另一次调用写下的文件版本时，记一条 `artifact` 边（写的 span → 读的 span，属性 `artifact.path`、
`artifact.version`、`artifact.trust`）；读到基线或外部改的版本时在读的 span 上记 `provenance.*` 属性。消息与派生
边加 `trust` 属性。`EdgeKind.artifact` 在 `syndica.trace/2` 里已经存在，读者都认得，所以不升版本；M7 的产物沿用它。

### D14 契约套件

`syndica.kernel.contracts.workspace` 发布一套 `Portal` 契约：越界与私有目录、版本与带前提的写（含并发的两次写
只成功一次）、字节与文本、删除、后台进程的增量读与终止、伪终端的六件套（取消、超时、退出、resize、不留进程）、
`close` 之后拒绝。`LocalWorkspace` 与 `WorkspaceRouter` 都跑它；第三方后端照着它自证合规。

## 公开契约

### 内核 API（都向后兼容：新方法、追加字段、关键字参数）

- `Portal`：D1 的新方法与 `expected` / `since` 参数；`FileStat`、`WriteConflict`、`ANY`、伪终端的结果类型。
- `ExecResult` 追加 `stdout_end`、`stderr_end`。
- `VersionStore` / `VersionEntry`（Python 写的 git 格式，内容先脱敏）；`LocalIsolator`、`MergeReport`、
  `MergeConflict`（`IsolatedCopy` 经 `syndica.kernel.isolation` 承诺）；`Portal` 的写交回写下的版本。
- `WorkspaceRouter(root, versions=, isolator=)`、`restore`；`Runtime(workspace=...)`、`spawn(..., isolation=...)`、
  `merge_isolated` / `discard_isolated`。
- `Tool.__tool_external_input__`；`HookResult.untrusted`（追加字段）。
- `Terminal` 工具；`local_tools` 追加 `Terminal` 键；`BashOutput` 加 `full`；`Delegate` / `Fork` 加 `isolation`。

### `.syndica.yaml`

`workspace.versions: true | false`（缺省 true）。

### trace

`artifact` 边与 `provenance.*` 属性（D13）；schema 仍是 `syndica.trace/2`。

### 私有目录

CLI 的私有目录追加 `.syndica/vcs`、`.syndica/worktrees`、`.syndica/snapshots`。

## 任务与进度

每个任务一次提交；提交前跑这个任务的定向用例与一次全量。

### 阶段一：执行面核心

#### T1 — Portal 协议补全与契约套件（完成，`1fe969c`）

D1、D14。`LocalWorkspace` 实现新方法（伪终端在 T4）；契约套件先覆盖文件、版本、后台进程；AST 守卫：
`kernel/tools/` 里不得出现 `open(` / `subprocess` / `os.` 的文件与进程调用。

#### T2 — 路由与乐观并发（完成，与 T3 同一次提交 `cea34f5`）

D2、D3。`WorkspaceRouter` 跑契约套件；`Read` / `Write` / `Edit` 接看到的版本；冲突的 `ToolResult`；
「两个 Agent 先后写同一个文件」「没读过就覆盖」「Bash 改过之后再写」各有用例；变异只打冲突判据。

#### T3 — 后台输出增量读（完成，`cea34f5`）

D8。按 Agent 与句柄的偏移；两个 Agent 各自的增量互不串线。

#### T4 — 伪终端与 `Terminal`（完成，`cf4a91d`）

D7。`LocalWorkspace` 的六件套与契约套件的伪终端部分；取消、超时、进程退出、resize 的定向用例；kill 之后
进程组里没有存活进程；危险判定读 `command` 与 `input`。

### 阶段二：版本层与隔离

#### T5 — 版本层（完成，见决策日志）

D4、D5。Python 写的 git 格式对象库（用户用 git 读得出来，由一条用例在有 git 时核对）、记录 / 历史 / diff / 回滚；
`Write` / `Edit` 交回 `ArtifactRef`。一条用例在 `.syndica/vcs/config` 里写进过滤器、fsmonitor 与 `include.path`、在
工作区放 `.gitattributes`，证明版本层的任何操作都不执行它们；隔离合并调 `git merge-file` 时的目录、环境与配置由一条
用例逐项钉住；没有 git 的环境用 `PATH` 里没有 git 模拟。危险规则对 `.syndica` 路径的新判据每个写法各有一条用例。

#### T6 — 隔离与合并（完成，见决策日志）

D6。三种策略、逐文件合并、冲突报告、`spawn` / `Delegate` / `Fork` 的 `isolation`、隔离状态注入历史。
共享写入冲突、worktree 合并冲突、快照 fallback 各有单测。路由按 Agent 的分配在 Agent 释放（`despawn`、合并、
丢弃）时一并回收，连同它在路由与 `BashOutput` / `Terminal` 里按 Agent 记的偏移与看过的版本；驱动任务里另起的
任务继承 `_DRIVING_AGENT`（B-23），于是它们的调用也路由到那个 Agent 的隔离副本——用例钉住，并写进 `spawn` 的
docstring。合并写回的条件写由一条「合并期间父被改」的用例钉住。

### 阶段三：溯源与危险规则

#### T7 — 溯源图与文件污点（完成，见决策日志）

D9（图、文件污点、基线判据）、D13。查询的独立遍历守卫；读 / 写 / Glob 的污点；`artifact` 边。

#### T8 — 执行器的精确标记与三条 M4 遗留（完成，见决策日志）

D9（B-15、B-18、D-40）、D10。变异打：异常标记的组合、兄弟结果的计入、投影比对、broker 收紧、扩展切点收紧、
结局事件不可跳过。

#### T9 — 危险规则与安全矩阵（B-13 / B-14 完成，`a84a889`：wrapper 选项表本来就在，补的是不执行实参的查询类
程序与 `command -v/-V`；`)` 找到了能区分的自然写法）

D11、D12。规则表每个新成员各有一条用例；安全矩阵全格；`{1, N}` 两档逐块相同。

### 阶段四：接线与收口

#### T10 — CLI 接线

`workspace.versions`、私有目录、`Terminal` 进工具表（`--no-write` 的白名单不变）、路由交给 Runtime、回放
（桩照记录交回结果与 `trust`；版本层在回放的临时工作区里同样打开）。

#### T11 — E2E、真实运行、文档与收口

脚本化模型跑路线图的五个 E2E 场景（共享并发写冲突后重读合并、两个隔离 worktree 合并成功、没有 git 时快照版本与
回滚、经 `Terminal` 完成一次交互、读不可信内容之后的高风险命令 reject / ask / allow）；免费模型跑一次真实运行并离线
回放；README、spec §6 / §12 / §15、路线图、登记表；收口审计。

## 验证节奏（按用户「不要做有过多耗时和等待的事情」收紧）

- 计划评审一轮（一个 Agent，后台跑）。评审期间先做 T1——它只补协议与本地实现，不受方案裁决影响；评审结论按
  严重程度修进方案与已有代码，没有 Blocking 就不开第二轮。
- 每个任务一次全量；导出验证只在阶段末推送前做一次。
- 变异只打安全与确定性不变式（冲突判据、版本层的安全配置、溯源组合、危险规则、处置矩阵、结局事件）。
- 每个阶段一次后台对抗评审，不挡提交；结论在下一个任务开头并入。收口只派一路评审。
- 真实运行一次（免费模型），不重复跑。

## 决策日志

- 2026-10-04：用户「把 M5 做完，包括写计划，review 计划，执行计划，同样不要做有过多耗时和等待的事情」。按 M4 的
  授权方式自主完成，四种情况停下来问（改 spec 方向或四条主张、付费服务或重量级依赖、放宽安全边界、动本仓以外的
  东西）。评审与变异的轮数按上面收紧。
- 2026-10-04：计划评审的 Blocking（B1：私有 git 目录躲过现有三道防线，宿主对它调 git 等于在危险闸之外执行 Agent
  写下的代码）并进 D4——版本层不再调 git，对象由内核用 Python 写；文件污点的基线从版本层挪到 D9 的 stat 指纹；
  `workspace.versions` 于是不再关系安全判据。阶段一（T1–T4 与 B-13 / B-14）已推送（`cea34f5`）。
- 2026-10-04：计划评审的 Agent 中途被停，结论按它留下的笔记补全（其余 High / Medium）：`Terminal` 不在回放的桩
  名单里（已修，回放照记录交回）；Bash 写文件绕过乐观并发（接受：shell 写什么只有沙箱后端看得到，记进路线图
  的遗留项）；D6 合并写回要条件写、`ArtifactRef` 的 id 要有派生规则、隔离下释放的 Agent 与它派生的任务怎么路由
  （三条并进 D5 / D6 / T6）。最重的一条是「同一个 Agent 之内的命令输出能不能按它真实读到的内容收窄溯源」——
  用户裁决推到后端：M5 只交 `Workspace` / `Portal` 协议与契约套件，Bash / Terminal 的输出照旧一律不可信；
  AGENTS.md 与 spec 决策 #94 改成「真正的边界是沙箱化的 Workspace 后端」；收窄随沙箱后端落地，登记进路线图。
- 2026-10-04：阶段一对抗评审的修复并进同一次提交：`Terminal` 的 `input` 不收控制字符（含制表与转义序列）、
  一行必须在一次 send 里交完、`ctrl-c` 附带一个 Ctrl-U（忽略 SIGINT 的程序也丢掉半行）、`ctrl-z` 不清半行；读
  与版本出自同一次读（`Portal.read_versioned`）、没读过的 Edit 不记成看过、读不出来的文件也算看过；伪终端按
  Agent 限 8 个会话、杀会话时连后代的进程组一起收、会话有控制终端、历史不落盘；`BashOutput` 在命令结束后让
  `meta` 带全部输出（开发期记录按它判测试结果）；契约补三方并发、kill 后进程真的不在、`since` 逐字、组长先死、
  作业控制、Ctrl-C。macOS 无论是否 `TIOCSCTTY` 都给会话控制终端，那条启动器的变异只有 Linux 杀得掉。
- 2026-10-04：T5 的版本层落盘前过 Redactor——计划原文没写，而 AGENTS.md #8 点名「提交」、§12 要求新增持久化
  路径一律脱敏；代价是含密钥样内容的版本取不回、不能回滚，按 spec「脱敏与可回放冲突时选安全」。由此 D6 的副本
  改从父的工作文件拷（从版本层物化会把占位符写进子 Agent 再随合并写回）。`LocalIsolator` 的 git 路径与三方合并
  的临时目录由宿主交进来：内核不读 PATH、不读 `TMPDIR`，而 `git merge-file` 会在工作目录里找仓库，临时目录必须
  在 Agent 碰不到的地方。
- 2026-10-04：T6 发现 `Terminal` 的参数说明写成多行，而工具反射「续行不拼接」——模型拿到的 `action` / `key`
  说明是半句话（T4 起就是这样）。改成参数行只留一句、细节挪进摘要段，并加一道守卫：内置工具的参数描述
  有续行就红。阶段二已推送（`d26e516`，导出验证 8488 passed）。
- 2026-10-04：T7 的 D13 原写「不升 schema」，而 `trace.py` 的规矩是闭集有任何增减版本号加一：产物边与读到的
  文件的出处用的是新属性键（`artifact.*`、`provenance.*`），旧版本的读者认不得。升到 `syndica.trace/3`；读者
  照旧读得懂 `/1`、`/2`。文件污点由宿主构造（`FileTaint(root)`）交给路由；执行器在 `tool.execute` 期间把这次
  调用的溯源结论放在 contextvar 里，路由按它给写下的版本记污点；不实现 `TaintAware` 的后端读到的内容照旧可信。
- 2026-10-05：阶段二对抗评审（15 条）修进同一次提交：只改大小写的改名在大小写不敏感的文件系统上会删掉唯一的
  那份（Blocking，改成冲突）；合并期间落进副本的写随副本被删（合并期间路由拒绝它的调用、先关副本里的进程、
  合并之后又变的文件记警告）；`.theirs` 名字被占时子 Agent 那份丢失（改用完整 id 加序号，存不下就保留副本）；
  目录与文件互换让合并停在半路（逐文件转成冲突）；隔离不嵌套（路由记父子关系，没有副本的 Agent 落在祖先的
  副本里，嵌套副本从父的副本拷、合并回父的副本）；副本丢可执行位；暂停 / 等待 / 收尾中的 Agent 可以合并
  （只许 IDLE / ZOMBIE）；危险判据认不出 `../../vcs` 与花括号。另：副本先建在旁边再整个换进来，提交以「这次
  提交动的路径」认历史，崩溃留下的锁按进程号回收，路由按后端交回的版本记。没修的：合并后被叫醒的子 Agent
  上下文里仍写着「在副本里」（合并结果里提示父 Agent 告诉它）；隔离副本里经 `..` 写进兄弟副本按形状认不出
  （登记，边界是沙箱化的后端）；只读地看 `.syndica` 里的记录按宿主状态变更判（与 `.git` 一致）；新的记录目录
  进文件工具的私有目录与 `.gitignore` 随 T10 的 CLI 接线一起做。
- 2026-10-05：T8 与计划的三处出入。B-15：裸值替身（hook 的 `result` 不是 `ToolResult`）仍按不可信——裸值上没有
  标记可读，替身常出自缓存与旁路读取，既有用例逐字钉着这条理由；只有工具抛出的异常按 `__tool_external_input__`
  收窄。B-18：计划写的「把此前已交回的兄弟结果算进去」让溯源随调度变（同一批调用两次运行结论不同、回放分叉），
  改成按兄弟工具的声明判，确定且仍满足 `{1, N}` 同结论。D-62：开发期记录器改成「hook 记 + 结局观察者补记」：
  `check` 标注还得经 hook 写进结果，观察者只补 hook 被截断时漏掉的调用；结局观察者是 `BaseAgent` 上新的公开
  方法（同步，在所有 `POST_TOOL_USE` 之后、结果块落轨迹之前）。D-64 只收紧工具调用那一侧并给 `HookResult` 追加
  `untrusted`；模型那一轮（扩展切点在模型侧切点里被触发）没有收紧，登记进路线图。
