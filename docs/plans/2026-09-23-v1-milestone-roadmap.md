# Syndica v1.0 里程碑路线图

> 本文把 spec §15 的 12 个内部里程碑展开成可执行 backlog。它不是单次开发计划；
> 真正开工某个里程碑时，仍需为该里程碑单独写实现计划、逐任务 TDD、变异验证、独立审查与提交。

## 当前状态

里程碑 1 已完成，审计记录见 [`docs/audits/2026-09-23-m1-final-audit.md`](../audits/2026-09-23-m1-final-audit.md)。
2026-09-29 用真实免费模型跑通体验验收，并补上 M2 之前的一项基础设施：多平台模型轮换（Gemini 在前、
OpenRouter 兜底，每个平台各自指数退避，spec 决策 #89）。

里程碑 2 已完成（2026-10-01），审计记录见 [`docs/audits/2026-10-01-m2-final-audit.md`](../audits/2026-10-01-m2-final-audit.md)：
真实免费模型按本仓的 `syndica-dev` 拓扑在仓库副本里完成了一次 `verified` 的自改动，人选择后由宿主提交、
推送，那个提交原样进了本仓。GitHub Actions 本月额度用完，M2 的提交都只按本地验证协议验证过，M1–M11 都做完
之后在 Linux 上统一补跑（见下面的主线约束）。

里程碑 3 已完成（2026-10-02），审计记录见 [`docs/audits/2026-10-02-m3-final-audit.md`](../audits/2026-10-02-m3-final-audit.md)：
每次 run 记成一个 trace（span、派生边与消息边），默认级别可离线回放，`syndica trace` 读、回放、导出、清理它；
落盘与导出的一切都先过内核的统一 `Redactor`。真实免费模型的 `syndica-dev` 运行留下的 trace 由 `trace show` 回答了
三个问题，并导出到了本地 collector；一次单 Agent 的真实运行在没有任何 API key 的环境里离线回放复现。M3 的提交同样
只按本地验证协议验证过。

里程碑 4 已完成（2026-10-03），审计记录见 [`docs/audits/2026-10-03-m4-final-audit.md`](../audits/2026-10-03-m4-final-audit.md)：
第三方能力经入口点装成 Runtime Module（清单、Flag、服务、工具与插件工厂、启停钩子、自有的命名空间切点），显式
启用、受项目配置的信任确认约束，它们的输出一律不可信；Flag 文档由声明生成；trace 记下每个 Agent 从哪个 Module 拿到
什么、切点触发了几次；拓扑 run 与带第三方 Module 的 run 都能离线回放。示例 Module 在仓库副本里用免费模型跑通了
「装上 → 确认 → 调用 → trace → 离线回放复现」。M4 的提交同样只按本地验证协议验证过。

当前主线约束：

- 不重做 M1，不补 `Runtime.start` / `Junction.RUNTIME_START`。
- 不把 `BEYOND-M1` 条目当成一次性清单；它们分别归入后续对应里程碑。
- 每个里程碑必须保持 `kernel -> surface/backend` 依赖方向不变。
- 每个里程碑完成时必须更新本文和 spec §15 的状态。
- 面向用户或未来贡献者的文档最终需要英文版；本文先作为中文工作底稿。
- CI 推迟到 M1–M11 都做完（2026-10-03 决定）：中途只按本地验证协议验证，不为补跑 CI 停下来；M12 之前统一跑
  一次 CI、修 bug、做回归验证。
- 从 M4 起按阶段验证（2026-10-03 决定）。M3 的耗时里，变异测试、全量测试加导出验证、等对抗评审三项各占约
  17–18%，写代码约 11%、真实模型 E2E 约 1.4%：
  - 每个任务只跑一次全量测试；把暂存区单独检出到临时目录里的导出验证，只在推送前或阶段末做一次。
  - 变异测试只用在安全（脱敏、危险判定、密钥不外泄）与确定性（回放、前缀缓存）这类关键不变式上，其余靠定向测试。
  - 对抗评审每个阶段一次（一个里程碑 3–4 次），在后台跑、不挡提交；发现的问题集中修。
  - 里程碑收口仍要满足下面「全局完成定义」的五类证据。

## 全局完成定义

每个未完成里程碑都必须同时满足五类证据：

1. **架构证据**：新增抽象、Protocol、Module、Surface、Backend 的归属清楚，不能把 surface/backend 依赖带进 kernel。
2. **静态代码验收**：Ruff、format、Pyright、依赖方向检查、公开 API 守卫、契约测试全部通过。
3. **行为验收**：单测 / L1 / L2 场景覆盖主路径、失败路径、取消路径、并发路径和恢复路径。
4. **E2E 验收**：至少一条用户可见的端到端场景，使用真实 Runtime、真实工具、真实 surface；真 LLM 场景可手动或定时。
5. **审计证据**：真实变异验证关键不变式，独立审查无阻塞问题，遗留项登记有归属。

推荐命令基线：

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run python scripts/check_deps.py
uv run pytest tests/kernel tests/contracts tests/runner tests/cli tests/integration -q
uv run pytest -q
```

若某个里程碑新增前端、server、backend 或真实 LLM 依赖，应在该里程碑计划里追加专用命令，
但不得降低上面的免费门禁。

## 里程碑总览

| # | 名称 | 状态 | 下一步 |
|---|---|---|---|
| 1 | 内核骨架 | 已完成 | 只维护回归门禁 |
| 2 | 自举 | 已完成 | 只维护回归门禁；CI 在 M12 之前统一补跑 |
| 3 | 可观测性 + 脱敏收口 | 已完成 | 只维护回归门禁；CI 在 M12 之前统一补跑 |
| 4 | 扩展点完备 | 已完成 | 只维护回归门禁；CI 在 M12 之前统一补跑 |
| 5 | 执行面完整化 + 不可信内容 | 未开始 | 下一个：写实现计划；完成 Workspace/Portal 与真实溯源链 |
| 6 | 状态 | 未开始 | 会话、压缩、记忆持久化 |
| 7 | 产物与验收 | 未开始 | Artifact、Rubric、Verifier |
| 8 | 长程任务 | 未开始 | Budget、Progress、Strategy |
| 9 | 产品面 | 未开始 | HTTP/SSE/WebSocket + React |
| 10 | 互操作 | 未开始 | MCP -> ACP -> A2A -> OpenAI HTTP |
| 11 | 训练出口 | 未开始 | 中立轨迹与 SFT/偏好/RL 导出 |
| 12 | 发布准备 | 未开始 | 英文文档、CI 矩阵、v1.0 发布 |

## 遗留项归属（2026-09-29）

M1 登记表里「明确不在里程碑 1」的 26 条、2026-09-29 真实体验验收与多平台轮换留下的后续项、M1 交接
文档 §4.0 的 Minor 与工程卫生项，在这里逐条给出归属。标为「接受」的是有意保留的限制，不排期，理由写在登记表
对应条目里。归到某个里程碑的条目已同时写进该里程碑的 TODO；开工写计划时把它们列进「要关闭的
deferred items」。

| 条目 | 内容 | 归属 | 依据 |
|---|---|---|---|
| B-01 | 租约字段只能经无条件 `update` 写 | M6 | 跨进程恢复与孤儿回收要原子写租约 |
| B-02 | `ControlBlock.wait_until_parked` 吞取消 | 接受，附触发条件 | 没有生产调用方；出现第一个时先按登记里的正解修 |
| B-03 | `LLMConfig` 不可哈希 | 接受 | 失败是响亮的；有缓存需求时再加稳定身份 |
| B-04 | `WaitResult` 成功路径不可哈希 | 接受 | 根因是 `Message` 可变，已写进 docstring |
| B-05 | 异常链不过 copy / pickle | 接受 | 与标准库行为一致 |
| B-06 | `ConfigError` 被借来表达调用方误用 | M12 | v1.0 冻结公开 API 时统一错误分类 |
| B-07 | 关联 ID 是扁平命名空间 | 接受 | 已裁决不加前缀，理由在 `wait_for` docstring |
| B-08 | 危险规则不认经中间工具执行、落盘后再执行 | 已修 | M5 T9：同一条命令里下载到文件再执行它的是 `shell.download_then_execute`；跨调用执行工作区里不可信的脚本按 `exec.untrusted_script` 判（文件污点 × `executed_scripts`），走强制升级 |
| B-09 | 未声明 `__tool_command_args__` 的非文本参数被静默跳过 | 已修 | M4 D7：Module 的工具与类式角色自己装的工具必须在自己的类上声明，登记表已关闭 |
| B-10 | 未声明工具的文本参数被散文误报 | 已修 | 同 B-09 |
| B-11 | `DangerRequest` 不可哈希 | 接受 | 同 B-04 |
| B-12 | 无 `functools.wraps` 的装饰器让声明落空 | 接受 | 管不了第三方装饰器，已写进 docstring |
| B-13 | wrapper 兜底带来的误报 | 已修 | M5 T9（`a84a889`）：wrapper 选项表本来就在，补的是不执行实参的查询类程序与 `command -v/-V` |
| B-14 | `)` 分隔符零覆盖、续行正则精度 | 已修 | M5 T9（`a84a889`）：找到了能区分 `)` 的自然写法 |
| B-15 | 溯源是「出现过」的上界，不是因果链 | 已修（部分） | M5 T8：工具声明 `__tool_external_input__`，不带外部内容的工具抛出的异常按这次调用的溯源算；裸值替身与中止块仍按不可信（裸值没有标记可读）。一个 Agent 之内仍以窗口为上界（M5 D9），收窄随沙箱后端 |
| B-17 | 并发段里的 `KeyboardInterrupt` / `SystemExit` 逃进事件循环 | 接受 | CPython Task 机制，配对不变式仍成立 |
| B-18 | hook 改写参数且有兄弟时一律按不可信算 | 已修 | M5 T8：只在兄弟里有会带进外部内容的工具时按不可信；按声明判、不看兄弟此刻跑没跑完，结论不随调度变 |
| B-19 | 段内 hook 写轨迹的顺序取决于调度 | 已修 | M3 D9 修订：轨迹只由内核写，并发窗口与可观察切点分发期间冻结 |
| B-20 | 连续两个未配对批次留下孤儿 | 接受 | 不可达，且当前症状是响亮失败 |
| B-21 | `SendMessage` 不限后代，唤醒会烧掉对方不可恢复的轮数 | M8 | 预算树决定这次唤醒记在谁账上 |
| B-22 | `KillAgent` 的后代遍历是 O(子树) | M6 | Store 契约给「后代」一个单一定义 |
| B-23 | `_DRIVING_AGENT` 被第三方任务继承导致保守误拒 | 接受 | asyncio 不暴露 await 链；误拒是安全侧 |
| D-40 | `ReadOnlyKwarg` 只护顶层 | 已修 | M5 T8：`PRE_TOOL_USE` 盯住 `args` 与 `tool_call` 的投影，嵌套就地写算那个 hook 写的并重取溯源，就地改 `tool_call` 直接拒绝并点名 |
| D-62 | 开发期记录器可以被更早装配的 Module 的 hook 截掉 | 已修 | M5 T8：`BaseAgent.observe_tool_outcomes` 不经 hook 链，记录器用它补记被截断的调用 |
| D-63 | 被收紧的 Module 直接往 `runtime.broker` 发消息，文字不收紧 | 已修 | M5 T8：`runtime.broker` 是一层收紧代理，被收紧的 Module 身份下投递的消息收成 UNTRUSTED |
| D-64 | 收紧不跟着数据走过扩展切点 | 已修（工具侧） | M5 T8：结论出自被收紧的 Module 时 `HookResult.untrusted` 为真，触发它的那次工具调用收紧；模型侧见下方 M5 后续 |
| D-42 | 可观察切点丢弃返回值，typing 看不见 | 已关闭 | M4 D9 裁决不拆回调类型，登记表已关闭 |
| D-43 | 扩展切点分类冲突只沿向外方向查 | 接受 | 单向引用结构上补不全，危险方向已挡住 |
| D-46 | 派活 / 分叉树没有上限也没有回收 | M8 | 预算树与回收策略 |
| 验收后续 | Agent 看不到自己还剩几轮，紧预算下一直调工具不收尾 | M8 | `GetBudget` 与预算注入历史 |
| 验收后续 | `--max-turns` 是每个 Agent 各自的上限，总请求数没有严格上界 | M8 | 父子预算树 |
| 轮换后续 | `ModelRouter` 不实现 `CachePrefixProvider`：经它的漂移检测只做逻辑比对，换平台记成一次 `identity` 漂移 | 已修 | M3 T8：LLM span 记下作答平台，`ModelRouter` 转发作答成员的前缀单位 |
| 轮换后续 | 清单显式写的模型名只走 OpenRouter / `OPENAI_*`，一次 run 只能用一个 Gemini 型号 | 已修 | M4 D10（T6）：清单的 `model` 可以写「平台:型号」 |
| D-61 | `Delegate` 对用完轮数的子 Agent 仍建议 SendMessage 继续 | 已修 | 登记表已关闭 |
| M-3 | `provenance_of` / `strictest_trust` 不满足「公开签名需要它」 | M12 | API 冻结时决定是否扣留 |
| M-4 | 端到端 WAITING 断言只有一跳调度余量 | 已修 | 登记表已关闭 |
| M-5 | D-53 正文夸大「静默漂移」 | 已关闭 | D-53 关闭时条目已改写 |
| M-6 | spec 写 `send(..., message)`，实现是 `content` | M12 | API 冻结时按实现改写 spec §4.2 的签名 |
| M-7 | spec 写 `AgentInfo.type`，实现是 `agent_type` | M12 | 同 M-6（spec §4.3） |
| M-8 | `WaitState.ABORTED` 的 docstring 缺 D-26 契约 | 已修 | 登记表已关闭 |
| M5 后续 | `Bash` / `Terminal` 的输出一律不可信，不按命令真实读到的内容收窄溯源 | 沙箱后端 | 用户裁决（2026-10-04）：真正的边界是沙箱化的 Workspace 后端；只有后端知道一条命令读了什么，收窄随它落地 |
| M5 后续 | `Bash` 写文件绕过乐观并发（版本前提只在 `Write` / `Edit` / 删除上） | 接受 | shell 写了什么只有沙箱后端看得到；下一次 `Write` 照样按版本冲突，文件污点把 shell 写的版本记成不可信 |
| M5 后续 | 交互式 shell 的历史展开（`!!`）能把一条经人确认过的命令再跑一次而不再问 | 沙箱后端 | `Terminal` 的文本闸挡失手不挡蓄意；授权仅放行当次调用要靠后端隔离兑现 |
| M5 后续 | 后台跑的只有进程，没有通用的后台工具 | M8 | 长程任务需要它（M5 计划「不做」） |
| M5 后续 | 拓扑槽的隔离 | M8 | 常驻的槽没有自然的合并时刻；隔离只经 `spawn` / `Delegate` / `Fork` |
| M5 后续 | 会话拿控制终端的启动器只能在 Linux 上被证明（macOS 不论有没有 `TIOCSCTTY` 都给控制终端） | M12 | CI 恢复后在 Linux 上跑那条变异 |
| M5 后续 | 隔离副本里的命令经 `..` 写进兄弟 Agent 的副本，危险判据按形状认不出（副本名是 Agent 的 id） | 沙箱后端 | 每个隔离的 Agent 需要自己的根；文本规则只按路径形状判 |
| M5 后续 | 合并之后被叫醒的子 Agent，上下文里仍写着「在隔离副本里」 | 接受 | 已经进 ZOMBIE 的 Agent 收不到不叫醒它的消息；合并结果提示父 Agent 在叫醒它的消息里说明 |
| M5 后续 | 合并只合内容，不合可执行位的变化 | M7 | `Portal` 没有改权限的方法；副本保留可执行位，跑得起来 |
| M5 后续 | 扩展切点在模型侧切点里被触发、结论出自被收紧的 Module 时，那一轮模型请求不收紧（D-64 只做了工具调用那一侧） | M6 | 与上下文压缩同期动模型侧的溯源；`HookResult.untrusted` 已让宿主代码知道这个值不可信 |
| M5 后续 | `shell.download_then_execute` 纯判定认不出 tee 写文件、`wget -P` 前缀、`mv` / `cp` 改名、`uv run` / `deno run` 等解释器 | 接受 | 真实姿态下这些下载落地的文件都不是基线，执行它由执行器的 `exec.untrusted_script` 兜住；纯判定（无路由的消费者）的这些形状留作后续 |
| M5 后续 | `exec.untrusted_script` 对命令里有多个 `cd` / 变量目录的脚本放过（`effective_cwd` 折不出目录） | 接受 | 折不出目录时宁可不升级、不拿错目录误判；有一个 Agent 之内的窗口上界兜着，真正的边界是沙箱后端 |
| 卫生 | 基础 CI：每次 push 跑与本地一致的门禁 | M2 | 自举开始改代码之前先有自动门禁 |
| 卫生 | CHANGELOG、PyPI 预发布、版本策略、issue 模板 | M12 | 发布准备 |
| 卫生 | `.env.example` | 不做 | key 文件不该放在 Agent 操作的工作区里，`Read` / `Grep` 读得到它 |

---

## M1 内核骨架

### 目标

让 Syndica 具备一个可运行的多 Agent 内核：Runtime 能创建多个 Agent、互发消息、各自跑通
`LLM + Tool` 循环，并可终止、恢复、声明式装配与执行最小本地任务。

### 当前状态

已完成并审计通过。M1 的未来工作只允许是回归维护，不再扩大范围。

### 已交付能力

- Runtime 生命周期：`spawn` / `send` / `wait_for` / `pause` / `resume` / `abort` / `kill` / `despawn` / `shutdown`。
- Agent turn loop：消息注入、LLM 调用、工具调用、工具结果回写、失败分类、abort 二次检查。
- 多 Agent 编排：`Delegate` / `Fork` / `SendMessage` / `QueryAgent` / `KillAgent`。
- 声明式 Agent：manifest、catalog、内置四角色、用户覆盖内置。
- Topology：slot、cardinality、lazy/eager/manual boot、Runtime 装配。
- Tool 与危险判定：schema、并发分段、危险规则、一次性人工确认。
- 最小本地工具：`Read` / `Write` / `Edit` / `Glob` / `Grep` / `Bash` / `BashOutput` / `KillBash`。
- CLI 与 L2 runner：`run` / `chat` / `agents` / `topology` / `flags` / `doctor` / `version`，YAML 场景回放。

### 保持不变的设计逻辑

- `assemble` 不是 `start`；内核没有异步 Runtime 启动入口。
- `ZOMBIE` 是可寻址恢复能力，不是泄漏。
- `kernel.__all__` 是公开承诺面；新增名字必须回答为什么宿主需要。
- `AgentOptions.tools` 允许内核换成自己的活表；热插拔应改 `agent.options.tools`。
- 当前溯源是保守上界，误报逃生口由 `SafetyModule.untrusted_high_risk` 提供；真实因果链归 M5。

### 回归 TODO

- 保持 M1 审计文档中的全量门禁可复现。
- 新增公开 API 时同步 `test_public_api.py` 的逐字守卫与扣留理由。
- 新增持久化、trace、记忆、artifact 写入点时不得直接塞进 M1；归入后续里程碑并先设计脱敏收口。
- 新增工具必须显式声明 `__tool_command_args__`，否则散文误报与危险漏报不可控。

### 静态验收标准

- `uv run pytest -q` 不少于 M1 基线覆盖；删除或弱化 M1 测试必须有替代证据。
- `scripts/check_deps.py` 继续证明 kernel 不依赖 surface/backend。
- AST 守卫继续覆盖危险策略唯一调用点、运行时切点、导出面、尾部未配对批次扫描。
- `Runtime`、`BaseAgent`、`AgentOptions` 的公开面逐字比对。

### E2E 验收标准

- 多 Agent 生命周期集成测试继续覆盖 spawn、send、wait、kill/despawn、恢复。
- 本地工具拓扑集成测试继续覆盖真实文件读写与命令执行。
- CLI 人机 L2 场景继续覆盖 AskUser、确认、失败路径和事件顺序。
- 控制台命令 `version`、`doctor`、`agents list`、`topology list`、`flags list` 可运行。

---

## M2 自举

### 目标

让 Syndica 能在自己的仓库里完成一次受控的代码或文档修改：读取约束、规划任务、改文件、运行测试、
读取失败、迭代修复并交出可审计结果。

M2 是效率分水岭。M2 完成前，所有实现都依赖人工手写；M2 完成后，后续里程碑可以用 Syndica 自己加速。

### 当前状态

已完成并审计通过（2026-10-01）。实现计划见 [`2026-09-30-m2-self-hosting.md`](./2026-09-30-m2-self-hosting.md)，
收口审计见 [`docs/audits/2026-10-01-m2-final-audit.md`](../audits/2026-10-01-m2-final-audit.md)。与下面原始 TODO 的
差别：不做命令白名单（D7）；越界路径在可信轨迹里问人而不是直接拒绝；交接文件写进 `.syndica/handoffs/`
而不是 `docs/plans`；提交与推送只由宿主在人选择之后执行。

### 依赖

- M1 的 CLI、本地工具、声明式 Agent、Topology、AskUser、L2 runner。
- `AGENTS.md` 和 M1 审计必须作为自举 Agent 的约束源。

### 技术架构

新增一个面向仓库开发的自举拓扑，不新增内核生命周期：

```text
syndica-dev topology
  Orchestrator  读目标、拆任务、分配角色、汇总状态
  Planner       只读分析 spec/AGENTS/现有计划，输出范围和验收
  Explorer      搜索代码、定位符号、找测试与历史约束
  Worker        应用补丁、运行命令、修复失败
  Reviewer      可先作为声明式 Agent 放在 catalog，M4 后可升级为 Module
```

控制逻辑：

1. `run` 或 `chat` 接收任务，使用 `--topology syndica-dev`。
2. Orchestrator 先要求 Planner 产出任务计划，不直接改文件。
3. Worker 只通过 Workspace/Portal 工具修改仓库，不直接访问裸文件系统。
4. 每次修改后运行最小验证命令，失败结果作为消息回灌给 Worker。
5. 成功后生成一份 handoff 摘要，说明改了什么、跑了什么、还有什么没做。

### TODO

- 定义 `syndica-dev` 内置拓扑与对应 agent manifest。
- 给 Planner / Explorer / Worker / Reviewer 写公开前可读的系统提示词，明确禁止破坏 git 状态。
- 增强 CLI `run` 的开发者配置：工作区 root、允许的命令、默认验证命令、是否允许写文件。
- 为本仓提供 `doctor` 子项：检查 `uv`、Python 版本、git、工作区是否干净、是否能跑基础测试。
- 增加安全命令策略：默认拒绝 `git push`、`git reset --hard`、`git clean`、删除仓库根目录、越界路径。
- 增加测试结果解析器：pytest、ruff、pyright、依赖检查的退出码与关键错误摘要结构化入 `ToolResult.meta`。
- 增加自举 handoff 生成器：记录目标、改动文件、验证命令、失败命令、下一步。
- 增加可选 `--no-write` dry-run 模式，只规划与定位，不落盘。
- 接入基础 CI：每次 push 在 GitHub Actions 上跑与本地一致的门禁（ruff format / check、pyright、
  `scripts/check_deps.py`、pytest）。自举会让 Agent 改仓库代码，进 `main` 之前必须有一道不靠人记得
  去跑的门禁。

### 具体实现逻辑

- 自举能力优先落在 surface/manifest，不改 kernel。
- 命令执行仍走 `Bash`；命令白名单/黑名单通过 Runtime Module 或 CLI host policy 注入。
- 文件修改仍走 `Write` / `Edit`；越界路径由 `LocalWorkspace` 与 `Portal` 拦截。
- 测试解析器只读 stdout/stderr，不依赖 pytest 私有 API。
- 计划与 handoff 是普通 Markdown 产物，先落在 `docs/plans`，后续 M7 再升级为 Artifact。

### 静态代码验收标准

- kernel 仍不读取环境变量、stdin、当前工作目录。
- 自举拓扑、manifest 字段必须经过已有 YAML/manifest 校验。
- 危险命令策略必须有单测覆盖每一个被拒命令族。
- 测试解析器必须用真实样例覆盖通过、失败、收集错误、超时、无输出五类。
- `--no-write` 模式下写工具不可被注册给 Worker，且 L2 场景证明模型调写工具会失败。
- 新增 CLI 参数必须有路由、帮助文本、退出码、三件套错误测试。

### E2E 验收内容

- **文档自举**：在临时复制的仓库中，让 Syndica 修改一个 TODO 文档，运行格式/链接检查，交出 handoff。
- **代码自举**：给一个小型 fixture 注入失败测试，Syndica 修改实现让测试通过。
- **失败迭代**：脚本化 LLM 第一次写错、pytest 失败、第二次读取失败摘要并修复。
- **安全拦截**：脚本化 LLM 尝试 `git push` 或 `rm -rf`，系统拒绝并让 Agent 换路径。
- **人工确认**：一次危险但允许的命令触发确认；批准后只放行该次调用。

### E2E 验收标准

- E2E 全程只使用公开 CLI 与真实 Runtime。
- 所有写入都落在临时工作区，原仓库不被改动。
- 成功场景必须产生可读 handoff，并列出实际验证命令。
- 失败场景不得伪装成功；退出码、stderr、handoff 都要显示未完成原因。
- 安全拒绝后，Agent 能继续执行非危险替代路径。

---

## M3 可观测性 + 脱敏收口

### 目标

建立统一 trace/span 模型、跨 Agent 因果链、可回放捕获格式，并在任何持久化写入之前执行脱敏。

M3 必须排在早期：后续长跑、产物、HTTP、训练都依赖 trace 来调试和验收。

### 当前状态

已完成并审计通过（2026-10-02）。实现计划见 [`2026-10-01-m3-observability.md`](./2026-10-01-m3-observability.md)，
收口审计见 [`docs/audits/2026-10-02-m3-final-audit.md`](../audits/2026-10-02-m3-final-audit.md)。与下面原始 TODO 的
差别：记录是内核同步插桩而不是订阅事件流的 RuntimeModule（D2：钩子与事件拼出来的 trace 恰好在被拦、被取消、
并发的地方缺口最多）；回放文档写进 `TraceStore`，没有单独的 `ReplayStore`；默认级别是「可回放」而不是「失败才留
payload」（D8，用户裁决）；artifact / verification 边等 M7 的产物与验收落地再加；`--topology` 的 run 不写回放文档。

### 依赖

- M1 事件流与 L2 runner。
- M2 自举最好已完成，便于用自身调试可观测性。

### 技术架构

```text
Runtime events
  -> TraceRecorder RuntimeModule
    -> Redactor
      -> TraceStore
      -> ReplayStore
      -> OTelExporter backend
```

核心模型：

- `Trace`：一次任务。
- `Span`：agent / turn / llm_call / tool_call / wait / confirmation / module。
- `CausalityEdge`：spawn edge、message edge、artifact edge、verification edge。
- `PayloadRef`：指向脱敏后的请求、响应、工具输入输出。
- `TracePolicy`：payload 保留级别、失败轮次保留、全量捕获开关。

脱敏是写入前的唯一收口，trace、session、memory、artifact metadata 都必须走同一个 `Redactor`。

### TODO

- 定义 `TraceStore` Protocol 与内存实现。
- 定义 `Redactor` Protocol、内置规则与用户追加规则。
- 在 Runtime、Agent、ToolExecutor、LLM provider、Broker wait 上打 span。
- 记录两类因果边：父子派生、消息发送到消息消费。
- 让 L2 runner 能消费捕获 trace，证明捕获格式与回放格式同源。
- 增加 `syndica trace list/show/replay` CLI。
- 增加 OTel exporter backend，但 kernel 只产中立 span。
- 增加 trace 保留策略：结构永久、失败 payload、全量 payload 显式开启。
- 复核 B-19：捕获与回放同源要求同一批调用留下同一份轨迹。段内 hook 往 `ctx` 写块的顺序若会让回放
  分叉，就由 executor 按调用顺序回放这些写入，或把约束写进 hook 契约。
- LLM span 记下多平台轮换里实际作答的平台（`extra['llm_route']`）与每次平台故障；`ModelRouter`
  转发作答成员的 `cache_prefix_units`，让经过轮换的漂移检测也是精确比对。

### 具体实现逻辑

- TraceRecorder 订阅进程内事件流，不让 HTTP 层成为唯一事件出口。
- 每次 LLM/tool 调用生成稳定 span id；工具结果、确认、异常都落在同一 span 下。
- `Runtime.send` 写入消息时记录 send span；Agent 消费消息时记录 consume span，并以 correlation 关联。
- payload 写入前调用 redactor；被替换的内容只留占位符和规则 ID，不留原文。
- replay 使用脱敏后的 payload；缺凭证导致 replay 失败是正确结果。

### 静态代码验收标准

- 新增持久化写入点若未调用 redactor，依赖/AST 守卫必须失败。
- Trace schema 有版本号；升级必须有兼容测试。
- 所有 span kind、edge kind、redaction rule id 是稳定公开枚举或字符串常量。
- OTel backend 不得被 kernel import。
- 失败路径不允许因为 trace 写入失败拖垮主流程；必须记录降级日志。

### E2E 验收内容

- 跑一次多 Agent 派活场景，trace 能展示父子关系、turn、LLM、tool 和消息因果。
- 往 prompt、工具结果、环境变量样例中注入假密钥，trace/session/replay 文件均只出现占位符。
- 将捕获的失败运行转换为 L2 replay，复现同样失败。
- 开启 OTel exporter，导出 span 可被标准 collector 或本地 mock collector 接收。

### E2E 验收标准

- trace 时间线能回答“谁创建了谁、谁在等谁、哪个工具最耗时”。
- replay 不需要真实 LLM，除非场景显式标记为真实 LLM。
- 脱敏后文件中不得出现原始 secret 字符串。
- trace 写入失败时任务本身继续，并在 stderr/log 中留下唯一证据。

---

## M4 扩展点完备

### 目标

把第三方能力接入从“能手写插件”补成完整机制：三层装配、Runtime Module、命名空间切点、Flag 文档、
能力声明、启停顺序与失败回滚。

### 当前状态

已完成并审计通过（2026-10-03）。实现计划见 [`2026-10-03-m4-extension-points.md`](./2026-10-03-m4-extension-points.md)，
收口审计见 [`docs/audits/2026-10-03-m4-final-audit.md`](../audits/2026-10-03-m4-final-audit.md)。与下面原始 TODO 的
差别：Module 启停失败响亮失败而不是只记日志（D1：启用了的 Module 是宿主要的能力）；§4.7 槽的 `plugins` 字段 v1 不做
（D4）；第三方 Module 的输出缺省不可信，只能在用户级逐个放开，工作区的项目配置要先经人确认（D15）；D-42 裁决不拆回调
类型（D9）。收紧仍挡不住的三条（开发期记录器的次序、直接往 broker 发消息、经扩展切点流动的数据）登记为 D-62、D-63、
D-64，归 M5。

### 依赖

- M3 trace，便于调试第三方能力。
- M1 Runtime Module 雏形。

### 技术架构

扩展能力统一包装为 `RuntimeModule`：

```text
RuntimeModule
  manifest: id/version/capabilities/flags/dependencies
  install(runtime_builder)
    -> hooks
    -> per-agent tool factories
    -> services
    -> extension junction declarations
  uninstall/rollback
```

命名空间切点由声明者触发，格式为 `<owner>:<event>`。内核只校验 owner、分类和冲突，不替第三方触发。

### TODO

- 补齐 Module manifest：id、version、flags、capabilities、dependencies、security notes。
- 增加 Module 装配器：拓扑级、Runtime 级、Agent 级合并顺序。
- 增加装配事务：任一第三方 register 失败时回滚已注册 hook/tool/service。
- 增加 extension junction API：声明、分类、触发、查询。
- 增加 flag 文档生成器，供 CLI、README、未来 Web UI 共用。
- 增加 module discovery 但不自动启用；必须显式配置。
- 增加示例第三方 module：声明 flag、工具、hook、扩展切点。
- 处理 B-09 / B-10：第三方工具随 Module 装配时必须声明 `__tool_command_args__`，未声明就拒绝装配。
- 复核 D-42：切点 API 定型时决定是否拆分可观察与可拦截的回调类型；它是公开 API 的破坏性变更，
  必须在 M12 冻结之前定下。
- 给清单的 `model` 字段一个「平台 + 型号」的写法，让显式模型名也能指到 Gemini 等任一已装配平台；
  清单字段属于公开 API，写法定下后不能再改。

### 具体实现逻辑

- 装配顺序稳定：kernel 内置 -> Runtime modules -> topology slot additions -> Agent local hooks。
- 可拦截切点串行，可观察切点并发；扩展切点继承同样分类。
- Module 失败分三类：manifest 校验失败直接拒绝；安装失败回滚；运行时 hook 失败按切点语义处理。
- Flag 声明是单一事实源，CLI `flags list/show` 与文档站不得手写另一份。

### 静态代码验收标准

- Module manifest 所有用户可见错误必须三件套。
- 装配事务必须用测试证明 `BaseException` 也能回滚内核持有的状态。
- 命名空间冲突、分类冲突、重复 service、重复 tool 都必须有定向测试。
- 新增 extension junction 必须进入公开面/扣留面守卫。
- flag 文档生成结果有快照测试，防止文档与声明漂移。

### E2E 验收内容

- 安装示例 module 后，一个 Agent 获得新工具、新 flag、新 hook。
- 示例 module 触发自有扩展切点，hook 能收到事件。
- 禁用 module 后，工具、hook、service、flag 均不可见。
- 安装过程中故意让第二个 hook 抛错，验证第一个 hook 被回滚。

### E2E 验收标准

- 不修改 kernel 源码即可加载第三方 module。
- `syndica flags list`、agent 可见工具、trace 中 module 事件三者一致。
- 失败装配不能留下半注册状态。
- module 运行时错误不拖垮主流程，除非该切点声明为必须失败。

---

## M5 执行面完整化 + 不可信内容

### 目标

把 M1 的最小本地执行补成完整 Workspace/Portal 协议：git 版本层、worktree/snapshot 隔离、共享写入冲突检测、
伪终端、背景进程、以及真实的不可信内容溯源链。

### 依赖

- M3 trace 因果链。
- M4 module/extension 机制。

### 技术架构

```text
Workspace
  LocalWorkspace
  GitWorktreeIsolation
  SnapshotIsolation
  future backend: docker/ssh/e2b

Portal
  file ops
  process ops
  background process ops
  pty session ops
  artifact/version ops

ProvenanceGraph
  source nodes: user / tool / file / web / skill / artifact / llm
  edges: read / write / summarize / compress / message / fork / delegate
  query: call args -> upstream trust
```

### TODO

- 完整定义 `Workspace` 与 `Portal` Protocol，并发布契约测试。
- 实现 git 初始化可关闭、git worktree 隔离、快照隔离 fallback。
- 实现共享模式乐观并发控制：写前版本、写后比对、冲突返回 ToolResult。
- 实现背景进程三件套：start/query/cancel，保留 stdout/stderr 增量读取。
- 实现 PTY 会话：create/send/capture/resize/kill/status。
- 实现产物版本基础：路径到版本、版本到 diff、版本冲突。
- 把文件、命令、PTY、Skill、外部内容全部接入 `ProvenanceGraph`。
- 收窄 M1 的保守溯源上界：高风险操作根据真实因果链升级，而不是整份 Context `any(UNTRUSTED)`。
- 处理 `BEYOND-M1`：B-15、B-18、D-40 中与真实溯源链相关的误报与嵌套写入问题。
- 处理 B-08 / B-13 / B-14：危险规则识别经中间工具转手执行（`xargs … sh -c`、`find -exec`）与落盘
  后再执行；wrapper 建「哪些选项吃下一个词」的表并与现有兜底并存；补齐分隔符与续行的覆盖。

### 具体实现逻辑

- 所有内置执行工具必须只调用 Portal，不直接使用 `open()` / `subprocess`。
- 写入类工具返回 `ArtifactRef` 或版本 metadata，供后续 M7 artifact 使用。
- 并发写冲突是 ToolResult 的可恢复错误，不是异常。
- PTY 与后台进程是两条能力：后台进程不需要终端，PTY 不等于长耗时。
- 不可信标记随数据流传播：工具输出 -> 摘要 -> 消息 -> 产物 -> 压缩 -> 记忆。
- 溯源失败按最坏情况处理；但 trace 足够时应减少 M1 的永久误报。

### 静态代码验收标准

- AST 守卫禁止内置工具绕过 Portal 直接读写文件或起子进程。
- 每个 Workspace/Portal 实现必须通过内核发布的契约测试。
- 共享写入冲突、worktree 合并冲突、snapshot fallback 均有单测。
- PTY API 对取消、超时、进程退出、窗口 resize 有定向测试。
- 安全矩阵测试覆盖 `risk x provenance x flag` 的全部格子。
- provenance query 的搜索范围必须由独立遍历证明没有收窄。

### E2E 验收内容

- 两个 Agent 共享工作区并发编辑同一文件，后写方收到冲突并重读合并。
- 两个 Agent 使用隔离 worktree 编辑不同文件，最终合并成功。
- 在无 git 可执行文件的临时环境中降级 snapshot，仍可版本化与回滚。
- 运行交互式命令需要 PTY，Agent 通过 `Terminal` 完成一次输入输出。
- Agent 读取不可信外部内容后尝试高风险命令，默认拒绝；`ask` 触发人工确认；`allow` 留审计记录。

### E2E 验收标准

- 并发写不得静默覆盖。
- 所有执行动作在 trace 中有对应 span 和 provenance edge。
- 高风险安全结论在 `max_concurrency=1` 与 `N` 下相同。
- PTY 会话可被取消且不遗留进程。
- 不可信内容经摘要或消息转发后仍能追溯到源。

---

## M6 状态

### 目标

实现会话持久化与恢复、上下文压缩、分层记忆，使长任务能跨进程、跨压缩点、跨会话延续。

### 依赖

- M3 redaction。
- M5 artifact/provenance 基础。
- 目标导向型压缩中的 Rubric 依赖 M7，可在 M7 后补完。

### 技术架构

```text
SessionStore
  session metadata
  runtime topology snapshot
  agent registry snapshot
  context blocks
  waiter/deferred state
  trace links

ContextCompressor
  rule-based
  summary
  pruning
  goal-directed after M7

MemoryStore
  global memory
  workspace memory
  agent-local scratch
```

### TODO

- 定义 `SessionStore`、`ContextStore`、`MemoryStore` Protocol 与内存实现。
- 序列化 Runtime/Agent 可恢复状态，不序列化进程内 task、锁、活协程。
- 实现 session list/show/resume/rm CLI。
- 实现上下文压缩三种基线：规则型、摘要型、剪枝型。
- 预留目标导向型压缩接口，M7 后接入 Rubric。
- 实现压缩前快照、压缩点 trace、压缩后缓存键重建。
- 实现全局/工作区两层记忆，只有根协调者可写全局。
- 增加损坏数据加载策略：尽量修复、跳过坏子项、明确记录。
- 处理 B-01：租约字段改为原子写，才能做跨进程的孤儿回收。
- 处理 B-22：在 Store 契约里给「后代」一个单一定义，`KillAgent` 的授权与 `despawn` 的级联共用它。

### 具体实现逻辑

- session 恢复先重建 registry/context，再恢复可等待队列；不能恢复的 waiter 标记为 preempted。
- ContextBlock 序列化必须保留 trust、tool pairing、artifacts、provenance refs。
- 压缩不碰 System Prompt；动态环境事实被压掉时必须重新注入。
- 摘要型压缩本身受预算约束，必要时先规则型预压缩。
- 记忆注入历史，不进 System Prompt；只有过期、压缩、新会话才重载。

### 静态代码验收标准

- Store Protocol 有契约测试：顺序、exactly-once、CAS、损坏数据、并发写。
- 所有 session/memory/trace 写入先经过 redactor。
- 压缩不变量有单测：工具调用配对、Goal/Rubric 存活、trust 存活、环境事实存活。
- 恢复后公开 Runtime surface 与新建 Runtime 行为一致。
- memory 写入权限有测试证明普通 Worker 不能写全局记忆。

### E2E 验收内容

- 运行一个多 Agent 任务，中途停止进程，重新启动后 `sessions resume` 继续完成。
- 构造超长上下文触发规则型压缩，下一轮 LLM 请求合法且工具配对不破。
- 摘要压缩遇到自身超限，先规则预压缩再摘要。
- 用户偏好写入工作区记忆，新会话可读；另一个工作区不可读。
- 损坏一个子 Agent 的 session 文件，根会话仍可打开并报告跳过项。

### E2E 验收标准

- 恢复后的 Agent 保留上下文、状态、预算、trace link。
- 压缩后任务能继续且不重复注入环境事实。
- 任何被脱敏的 secret 不因 session/memory 落盘重新出现。
- 损坏恢复不能整库崩溃，也不能静默吞掉损坏。

---

## M7 产物与验收

### 目标

让产物成为内核可识别的一等对象，并建立 Goal/Rubric/Anchor/Verdict 的验收闭环。

### 依赖

- M5 version/provenance。
- M6 session/context persistence。

### 技术架构

```text
ArtifactStore
  ArtifactRef
  versions
  deliverable flag
  producer and provenance

ArtifactHandler registry
  preview
  semantic_diff
  validate

VerificationModule
  RubricsBuilder
  OutputVerifier
  OutcomeVerifier
  feedback -> proposed rubric -> human confirm
```

### TODO

- 定义 `ArtifactRef`、`ArtifactStore`、`ArtifactHandler`、`Preview`、`Diff`、`Violation`。
- 给 ContextBlock 增加 artifacts 字段并贯穿工具结果。
- 实现 handler 注册与冲突规则：`(kind, mime)` 精度优先，同精度冲突失败。
- 实现隐式追踪：写入工具在 `post_tool_use` 登记触碰路径。
- 实现 `Deliver(path, kind, note)` 显式交付工具。
- 定义 `Goal`、`Rubric`、`Anchor`、`Verdict`，Verdict 为 `pass/partial/block/unverified`。
- 实现 RubricBuilder：工作前从 Goal 投影标准，标准必须带锚点。
- 实现 L1/L2/L3 验收 Module 与独立预算。
- 实现人的反馈升级为持久标准：自动提议，人确认后生效。

### 具体实现逻辑

- 内核只认身份、版本、kind、mime；格式语义由 backend handler 提供。
- 交付物与中间产物都可登记，但只有 `is_deliverable` 默认进入 L2/L3 验收。
- Rubric 不替代 Goal；Goal 是唯一权威，Rubric 是可检验投影。
- `judgment` 类标准不自动判 pass，输出“需人工确认”。
- 验收器失败 fail-open，但 Verdict 必须是 `unverified`，不得塌缩成 pass。

### 静态代码验收标准

- ArtifactRef 序列化/反序列化保持身份和版本不变。
- handler 冲突、精度优先、fallback 到二进制 diff 有测试。
- 每条 Rubric 没有 Anchor 时拒绝。
- Verdict 四值完整，导出与 UI 不允许把 `unverified` 当 pass。
- ContextBlock artifacts 字段不破坏已有 provider 请求序列化。
- 人类反馈升级必须有确认流程测试，不能自动永久生效。

### E2E 验收内容

- Agent 创建 Markdown 报告并调用 `Deliver`，UI/CLI 能列出交付物版本。
- L1 捕获格式错误，例如空章节、非法 Mermaid、CSV 列数不一致。
- L2 发现用户要求未完成，主 Agent 再做一轮后通过。
- L3 基于工作区证据判定最终回复错误，返回 block。
- 用户反馈“第 3 列口径不对”生成 proposed rubric；确认后后续验收必查。

### E2E 验收标准

- 每个 deliverable 都能追到 producer、version、trace span、rubric verdict。
- 验收失败能驱动主 Agent 修复，而不是只在日志里报错。
- `unverified` 在 CLI/Web/trace/export 中都可见。
- 二进制 handler 失败时降级但不阻断任务。

---

## M8 长程任务

### 目标

让 Syndica 支持数小时到数天的任务：目标持久化、进度可度量、预算可感知、停滞可纠偏、上下文可压缩、
策略可替换。

### 依赖

- M7 Rubric/Verdict。
- M6 state 与 compression。
- M3 observability。

### 技术架构

```text
GoalStore
BudgetTree
ProgressLedger
LongHorizonStrategy
  on_progress_stall
  on_budget_low
  on_goal_drift
  should_stop

LongHorizonModule
  inject budget/progress into history
  expose GetBudget and Compact tools
  drive quality gates
```

### TODO

- 定义 Goal 持久化与变更流程；Goal 仅在人明确改变要求时变更。
- 实现预算树：tokens、cost、wall_clock、turns；子 Agent 从父预算切分。
- 实现成本计量接口：输入、输出、缓存命中三档，价格表归 provider backend。
- 实现 `GetBudget` 工具，预算注入历史但不进 System Prompt。
- 实现停止判据：完成、资源、停滞三条正交。
- 实现漂移检测：锚点检查常态，语义比对仅在可疑时触发。
- 实现 ProgressLedger：以强制 Rubric 满足比例度量进度。
- 实现基线 LongHorizonStrategy，并允许 Module 替换策略。
- 处理 D-46 / B-21：派活/分叉树的规模由预算树与回收策略控制。
- 处理 2026-09-29 真实体验验收的两条后续项：Agent 看不到自己还剩几轮（`GetBudget` 与预算注入
  历史）；`--max-turns` 目前是每个 Agent 各自的上限，由预算树换成父子切分。
- 接入分级质量门：L1 每次产物变化，L2 阶段收尾，L3 里程碑处。

### 具体实现逻辑

- 预算耗尽默认 `graceful_wrapup`，不是硬杀。
- 进度不得来自 Agent 自述，只能来自 Rubric/Verdict。
- 子 Agent 创建前必须申请预算；预算不足时拒绝或缩小任务。
- 停滞不是终止，而是触发策略纠偏。
- Goal/Rubric/预算/进度都是 trace 可观测量。

### 静态代码验收标准

- 预算树不变量：子预算总和不得超过父预算；消耗不能为负；释放/取消有测试。
- `max_turns`、wall clock、token、cost 四种预算分别覆盖耗尽路径。
- 完成/资源/停滞三类停止判据互不替代，有正反测试。
- 进度回退必须记录为警报事件。
- Strategy Protocol 的基线实现与替换实现都通过同一契约测试。
- 预算注入不得改变 System Prompt 前缀。

### E2E 验收内容

- 长任务分派多个子 Agent，父预算被切分，子 Agent 无法超额。
- 构造连续 N 轮无进展，触发策略纠偏而非无限循环。
- 构造目标漂移，锚点检查失败后触发语义比对并回正。
- 预算接近耗尽时 Agent 读取 `GetBudget` 并交付已完成部分。
- 上下文接近窗口上限时 Agent 主动调用 `Compact`。

### E2E 验收标准

- 长跑任务的 UI/CLI/trace 能显示预算、进度、停止原因。
- 预算耗尽不丢已有产物。
- 子 Agent 树不会无界增长；增长都能归因到预算切分。
- 停滞和漂移都有可回放 trace。

---

## M9 产品面

### 目标

交付可部署的产品 surface：HTTP API、SSE 实时流、WebSocket RPC/PTY、React Web UI、移动端适配、
多会话管理、预算与进度可视化。

### 依赖

- M3 trace/event stream。
- M6 session。
- M7 artifact。
- M8 budget/progress。

### 技术架构

```text
syndica-server
  HTTP REST
  SSE event streams
  WebSocket RPC and PTY
  static React bundle
  auth/session adapter

web app
  session list
  chat/event timeline
  agent tree
  artifact viewer
  trace/progress/budget panels
  confirmation and AskUser UI
```

### TODO

- 定义 HTTP API schema：sessions、agents、messages、events、artifacts、trace、flags、topology。
- 实现 SSE 会话流与全局流，支持重连与 last-event-id。
- 实现 WebSocket RPC：发送消息、确认工具、PTY 输入输出。
- 实现 server composition root：加载 config、LLM backend、workspace、store、modules。
- 实现 React UI：会话、chat、agent tree、artifact preview、trace timeline、budget/progress。
- 实现移动端布局，核心操作在窄屏可用。
- 实现本地部署命令：`syndica up/down/status`。
- 实现 API/OpenAPI 文档与前端类型生成。

### 具体实现逻辑

- Server surface 只依赖 kernel，不反向污染 kernel。
- SSE 是事件出口，不是事件源；进程内事件仍是第一事实源。
- WebSocket 只用于双向低延迟：RPC 与 PTY。
- 前端构建产物随 Python 包发布，用户无需 Node。
- UI 只展示后端明确状态，不用前端推断 Agent 是否完成。

### 静态代码验收标准

- Server API schema 快照测试；破坏性变更必须显式记录。
- 前端 TypeScript 类型检查、lint、build 通过。
- API handler 不直接操作 kernel 私有字段。
- SSE 事件序号单调，重连 replay 有单测。
- 前端不能把 `unverified` 显示成通过。
- 依赖检查证明 server 不被 kernel import。

### E2E 验收内容

- 启动 `syndica up`，浏览器创建会话、发送任务、看到 Agent tree 和事件流。
- 工具确认在 UI 中弹出，批准后工具执行，拒绝后 Agent 收到拒绝结果。
- Artifact 产生后 UI 可预览、查看 diff、标记反馈。
- SSE 断线重连后不丢事件。
- PTY 工具通过 WebSocket 交互。
- 移动端视口下 chat、确认、artifact 基本可用。

### E2E 验收标准

- Playwright 桌面与移动视口通过。
- 网络断开/刷新页面后会话可恢复。
- UI 时间线与 trace 查询结果一致。
- 无 API key 的环境下 server doctor 给出三件套错误，而不是白屏。

---

## M10 互操作

### 目标

让 Syndica 与主流 Agent/工具生态互通：MCP、ACP、A2A、OpenAI 兼容 HTTP。顺序为 MCP -> ACP -> A2A -> OpenAI HTTP。

### 依赖

- M4 extension/module。
- M5 tool/workspace。
- M9 server。

### 技术架构

```text
Interop adapters
  MCP client/server backend
  ACP adapter
  A2A adapter
  OpenAI-compatible HTTP adapter

Core mapping
  Syndica Tool <-> protocol tool
  Agent/Topology <-> protocol agent/session
  ContextBlock <-> protocol message
  Artifact/Resource <-> protocol resource
  Event stream <-> protocol stream
```

### TODO

- MCP server：把 Syndica 工具、resources、prompts 暴露给 MCP 客户端。
- MCP client：把外部 MCP server 工具包装成 Syndica Tool，并声明 trust/capabilities。
- ACP adapter：第三方客户端可驱动 Syndica Agent；Syndica UI 可驱动第三方 ACP Agent。
- A2A adapter：Agent card、任务派发、状态查询、消息互通。
- OpenAI HTTP：兼容 chat completions、tool calls、streaming，把请求映射到 Syndica session。
- 增加协议版本协商、认证配置、错误映射。
- 增加互操作 conformance fixtures。

### 具体实现逻辑

- 协议 SDK 进 backend/surface，不进 kernel。
- 每个协议 adapter 只做映射，不复制 Runtime 逻辑。
- 外部工具默认不可信；高风险操作继续走 Syndica 的安全矩阵。
- OpenAI HTTP 的目标是兼容现有客户端，不等于绕过 Syndica 的 session/trace。

### 静态代码验收标准

- 每个协议 adapter 有固定 fixture 与黄金文件。
- 协议错误映射必须保留三件套或协议等价字段。
- MCP/A2A/ACP/OpenAI SDK 不得出现在 kernel 依赖中。
- 外部工具 trust 默认值测试必须覆盖。
- 版本协商失败不能回退到未知行为。

### E2E 验收内容

- MCP 客户端调用 Syndica 暴露的 `Read`/`Grep` 类工具。
- Syndica 连接一个 mock MCP server，把它的工具注册给 Agent 并执行。
- ACP 客户端创建会话、发送消息、接收事件。
- A2A mock Agent 与 Syndica Agent 互发任务。
- OpenAI SDK 调用 Syndica server 的 chat completions，含 streaming 与 tool calls。

### E2E 验收标准

- 协议互通不破坏 trace、provenance、redaction。
- 外部工具高风险调用被同一套 policy 管住。
- 断连/协议错误给出结构化错误，不让 session 卡死。
- 每个协议至少有一条 CI 可跑的 mock E2E；真实客户端场景可手动或 nightly。

---

## M11 训练出口

### 目标

把 Syndica 的工作轨迹转成训练数据：中立轨迹、SFT、偏好数据、RL rollout，并至少对接一个开源训练框架。

### 依赖

- M3 trace/replay。
- M7 artifact/verdict。
- M8 budget/progress。

### 技术架构

```text
NeutralTrajectory
  messages
  tools
  artifacts
  verdicts
  causality
  timing/cost
  redaction metadata

Exporters
  SFT jsonl / ShareGPT
  preference chosen/rejected
  RL tokenized trajectories
  TRL or verl backend
```

### TODO

- 定义中立轨迹 schema 与版本迁移。
- 从 trace/session/artifact/verdict 生成中立轨迹。
- 实现 SFT exporter：messages jsonl、ShareGPT。
- 实现 preference exporter：从多次尝试与 verdict 构造 chosen/rejected。
- 实现 RL exporter backend：token id、loss mask、reward、episode metadata。
- 对接至少一个训练框架：TRL 或 verl。
- 增加 dataset manifest：来源、过滤条件、redaction、unverified 策略。
- 增加 `syndica batch` 与 `syndica export <format>` CLI。

### 具体实现逻辑

- 中立轨迹不含 tokenizer 细节；tokenization 归训练 backend。
- `unverified` 默认排除；包含时必须显式 flag，并写进 dataset manifest。
- secret 已脱敏的占位符保留，但原文永不导出。
- 偏好对必须可追溯到同一 Goal/Rubric 下的不同尝试。
- 训练出口不修改原 trace，只生成派生数据集。

### 静态代码验收标准

- schema 版本、字段顺序、必填字段有黄金测试。
- `unverified` 默认排除有反向测试，防止数据污染。
- redaction metadata 必须随样本导出。
- preference pair 构造必须证明 chosen/rejected 来自同一任务。
- exporter 失败不能破坏源 trace/session。
- 训练 backend 依赖不进入 kernel/runner 核心。

### E2E 验收内容

- 运行一批脚本化任务，生成 pass/partial/block/unverified 四类 verdict。
- 导出 SFT 数据，验证消息顺序、工具调用、产物引用。
- 导出 preference 数据，验证 chosen/rejected 可追溯到 verdict。
- 导出 RL 数据，使用 mock tokenizer 或真实小 tokenizer 生成 loss mask。
- 用 TRL/verl 做最小 smoke：能读取数据集并启动一个 dry-run。

### E2E 验收标准

- 默认导出不含 unverified 样本。
- 导出的任何样本不含原始 secret。
- 数据集 manifest 足够复现过滤条件。
- 一个任务失败不会拖垮整批 export。

---

## M12 发布准备

### 目标

把内部项目整理成可公开发布的 v1.0：英文文档、文档站、CI 矩阵、包发布、贡献流程、安全说明、版本策略和示例。

### 依赖

- M1-M11 全部完成。

### 技术架构

发布形态：

```text
syndica               kernel + CLI
syndica-server        HTTP/Web surface
syndica-runner        batch/export surface
optional backends     llm/store/workspace/artifact/export/protocol
docs site             guides + API reference + protocol contracts
```

### TODO

- 英文化 README、quickstart、concepts、API reference、troubleshooting。
- 建文档站：安装、五分钟上手、核心概念、四个 Protocol 契约、编写 Agent/Tool/Backend。
- 补齐 `LICENSE`、`CONTRIBUTING`、`CODE_OF_CONDUCT`、`SECURITY`、issue/PR 模板。
- 固化 v1.0 公共 API 清单：导出名、切点名、Store Protocol、Flag key、YAML schema、HTTP API。
- 配置 CI 矩阵：Python 3.11/3.12/3.13，Linux/macOS/Windows。
- 配置前端 CI：lint、typecheck、build、Playwright。
- 配置包发布：wheel/sdist、extras、server 静态资源打包、签名/校验。
- 写迁移和兼容策略：semver、弃用周期、breaking change 流程。
- 准备 demo：CLI 自举、Web 多 Agent、artifact verification、training export。
- 处理 B-06（统一错误分类）、M-3（`provenance_of` / `strictest_trust` 是否扣留）、M-6 / M-7（spec
  §4.2 / §4.3 的签名按实现改写）。
- 建立 CHANGELOG，决定 PyPI 预发布与版本策略。

### 具体实现逻辑

- 发布前做一次 API freeze，不再随手改公开面。
- 文档里的命令必须由 CI 或 doc test 验证。
- 安全说明必须明确“不提供多租户隔离”和“第三方同进程代码无法沙箱化”。
- 示例项目应使用最少外部依赖，方便用户复现。
- 真 LLM E2E 不进免费 PR CI，但 nightly/手动必须保留。

### 静态代码验收标准

- `pip install .`、`pip install .[openai]`、`pip install syndica-server` 的 smoke 测试通过。
- 多 Python/OS CI 全绿。
- docs build 无断链；README 代码块可执行或明确标记不可执行。
- package metadata、license、classifiers、py.typed、type stubs 完整。
- public API snapshot 与 v1.0 freeze 清单一致。
- 依赖锁与安全扫描无高危未解释项。

### E2E 验收内容

- 新机器从 README 开始，五分钟内完成一次 CLI run。
- 启动 Web UI，完成多 Agent 任务、人工确认、artifact 查看。
- 用 MCP/OpenAI SDK 连接本地 server，完成一次工具调用。
- 跑一批任务并导出训练数据。
- 从干净 clone 跑完整免费 CI。

### E2E 验收标准

- 新用户不需要读源码即可跑通 quickstart。
- 所有 demo 都有录屏或截图证据，且命令可复制。
- 文档站、PyPI 包、GitHub release 三者版本一致。
- 安全边界、数据保留、脱敏和第三方代码风险在公开文档中明说。

---

## 后续执行方式

每次只开启一个里程碑：

1. 从本文复制对应里程碑，写成 `docs/plans/YYYY-MM-DD-m<N>-<slug>.md`。
2. 对照当时仓库状态重新校准 TODO；本文不是跳过调研的理由。
3. 拆成可独立提交的小任务，每个任务包含 RED/GREEN/变异/独立审查。
4. 完成后更新本文状态、spec §15、审计记录和遗留登记。
5. 在 `main` 上开发，门禁与验证通过后 commit 并 push（用户 2026-09-29 定）。提交身份是
   `ai-loren <1158083128@qq.com>`，一律用 `git -c trace2.eventtarget= -c trace2.eventnesting= commit`。

## 里程碑之间的硬依赖

```text
M1 -> M2
M2 -> M3
M3 -> M4
M3 + M4 -> M5
M3 + M5 -> M6
M5 + M6 -> M7
M6 + M7 -> M8
M3 + M6 + M7 + M8 -> M9
M4 + M5 + M9 -> M10
M3 + M7 + M8 -> M11
M1..M11 -> M12
```

若后续为了交付效率调整顺序，必须先更新这张依赖图，并说明被提前的里程碑如何替代缺失依赖。
