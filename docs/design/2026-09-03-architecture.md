# Syndica 架构设计

> 状态：设计定稿，待实现
> 日期：2026-09-03
> 语言说明：本文是面向维护者与 AI 协作者的工作文档，用中文书写。**首次公开发布时，面向用户的文档（README、文档站、API 参考）必须提供英文版**，否则会把绝大多数潜在贡献者挡在门外。

---

## 1. 定位

Syndica 是一个**以 Multi-Agent 为一等公民**的 Agent Harness，面向**通用知识工作**。

一句话：**让一支 Agent 团队 7×24 小时完成"可验收的知识工作"，并且把干活的过程变成训练数据。**

四条可检验的主张：

1. **Multi-Agent 是一等公民，不是编排糖衣。** Agent 拥有进程语义（创建 / 终止 / 挂起 / 可被消息唤醒 / 可被回收但仍可寻址）；派活、分叉、通信是内核工具；系统的组织结构是声明式数据而非代码。
2. **面向通用知识工作，不只是编程。** 文档、表格、图表、报告与代码同等对待。当前生态里的并行 Agent 工具几乎全部围绕编程场景，非编程的知识工作是空白。
3. **产物与验收是内核能力。** 产物（Artifact）有身份、版本与语义 diff；验收基于可追溯到原始需求的验收标准（Rubric + Anchor）。这解决"没有编译器和测试时，凭什么说活干对了"。
4. **推理与训练同源。** 同一套 Agent 结构与工具，既能干活，也能产出训练数据。验收结论天然就是奖励信号与偏好数据。

### 1.1 非目标

- **不做又一个通用图/状态机编排框架。** 不与以"持久化状态图"为核心卖点的框架在同一货架竞争。
- **不拼模型 provider 覆盖数量。** 提供中立抽象 + 一个通用兼容实现，具体厂商 SDK 交给可选后端。
- **不追求"最小抽象"。** 本项目刻意提供较厚的运行时语义（隔离、验收、长跑、记忆），因为目标场景需要它们。

---

## 2. 范围

v1.0 的目标是**一个完整可部署的产品**，而非一个纯库：能部署、有 Web 与移动端、能长跑、能批量跑评测、能导出训练数据。

发布策略：**在达到完整形态之前不做公开发布**。这带来两个直接后果，都要利用好：

- 发布前**没有任何兼容承诺**，公开 API 可以自由重构。
- 发布前**没有外部贡献者**，所有产出来自维护者与 AI 协作。因此**自举（框架能改自己的代码）是唯一有效的加速杠杆**，优先级高于任何单个功能。

---

## 3. 架构总览

### 3.1 分层

```
kernel      唯一内核，零重依赖
              runtime / agent / tool / hook / context / topology
              llm 抽象 / workspace / artifact / verify
              内置 agent 与工具 / 四个 Store Protocol 及默认实现

surfaces    彼此平级、互不依赖，都只依赖 kernel
              cli      终端交互与脚本化调用
              server   HTTP + 实时流 + Web UI + 多会话管理
              runner   批处理、评测、轨迹与训练数据导出

backends    全部可选，按需安装
              workspace-docker / workspace-ssh / workspace-e2b …
              llm-openai / llm-anthropic / llm-litellm …
              store-redis / store-postgres
              artifact-office（docx / xlsx / pptx / 图表渲染）
              export-trl / export-verl（训练框架对接）
```

### 3.2 依赖规则（必须由 CI 机器校验）

1. **kernel 不依赖任何 surface 或 backend。**
2. **surface → kernel 单向；surface 之间互不依赖。**
3. **backend → kernel 单向；backend 之间互不依赖。**
4. **kernel 不读环境变量。** 路径、凭证、开关一律由宿主通过构造参数或依赖注入传入。理由：一旦读环境变量，内核就绑定了某种部署方式，而开源项目对用户的部署方式一无所知。
5. **kernel 零重依赖。** 只允许标准库 + `pydantic` + 一个 HTTP 客户端。所有厂商 SDK 进 backend。
6. **公开 API 全量类型标注**，随包发布 `py.typed`。

> 这六条不能靠文档和纪律维持。第一个 PR 就可能把 backend import 进 kernel，因此必须写成 CI 检查。

### 3.3 发布形态

| 安装 | 得到 |
| --- | --- |
| `pip install syndica` | kernel + CLI，零重依赖，单机开箱可跑 |
| `pip install syndica[openai]` | 加一个 LLM 后端 |
| `pip install syndica-server` | Web / HTTP 产品面 |
| `pip install syndica-runner` | 批处理与评测 |
| `pip install syndica[all]` | 全部 |

主包默认自带一个**走 OpenAI 兼容协议的通用 LLM 客户端**（可连 OpenAI / Azure / vLLM / Ollama / 多数国产模型），保证一条命令就能跑通；厂商特有 SDK 才进 backend。

---

## 4. 内核：Runtime 与 Agent

设计隐喻是操作系统进程模型：Runtime 类比 init 系统，Agent 类比进程，Broker 类比 IPC。由此得到三条不变式，所有边界都是它们的推论：

- **Agent 不共享可变状态**，只通过消息通信。
- **Agent 可独立挂起 / 恢复 / 取消。**
- **Agent 不管理自己的生命周期。** 外层事件循环等消息并驱动 Agent 执行；内层只跑"LLM + 工具"循环。一个进程不该调用自己的 `waitpid`。

### 4.1 Agent 状态机

| 状态 | 含义 | 为什么不能与其他态合并 |
| --- | --- | --- |
| `IDLE` | 已注册，事件循环在等消息 | — |
| `RUNNING` | 正在执行 turn 循环 | — |
| `PAUSED` | 被外部挂起 | 恢复依赖**外部**调用 `resume()` |
| `WAITING` | Agent 自身在等待（人工输入 / 工具确认 / 指定回复） | 恢复依赖一条**匹配的答复**，不是外部 resume |
| `ABORTING` | 中止已置位，正在收尾 | 中间态，使 `kill()` 能立即返回而不必等收尾完成 |
| `ZOMBIE` | 事件循环已退出、队列已移除，**但仍在注册表中因此仍可寻址** | 一条消息即可让它继续工作 |

`ZOMBIE` 是刻意保留的能力，不是未清理干净：被终止的 Agent 保留寻址能力，长跑与常驻场景依赖它。

**ExitMode**（`spawn` 时指定，决定一轮工作结束后的归宿）：

- `IDLE` — 事件循环常驻，继续等后续任务（守护 / 人在环场景）
- `ZOMBIE` — 停循环、移队列，保留注册表（默认）
- `EXIT` — 自动销毁

### 4.2 Runtime 公开 API

公开面刻意收紧到 11 个方法。发布之后每个公开方法都是永久兼容承诺，宁少勿多，不够再加。

```python
class Runtime:
    # 生命周期
    async def spawn(self, agent, options, query=None, *, parent_id=None,
                    exit_mode=ExitMode.ZOMBIE, fork_from=None,
                    flags=None, metadata=None) -> str: ...
    async def kill(self, agent_id: str) -> None: ...
    async def despawn(self, agent_id: str) -> None: ...
    async def wait(self, agent_id: str) -> Context: ...

    # 控制
    async def pause(self, agent_id: str, *, recursive: bool = False) -> None: ...
    async def resume(self, agent_id: str, *, recursive: bool = False) -> None: ...
    async def abort(self, agent_id: str, *, recursive: bool = False) -> None: ...

    # 消息
    async def send(self, agent_id: str, message, *,
                   trigger: Trigger = Trigger.PENDING,
                   activate: bool = True) -> DeliveryResult: ...

    # 寻址
    async def ensure_active(self, agent_id: str) -> bool: ...
    async def find(self, agent_id: str) -> AgentInfo | None: ...

    # 等待（供工具使用）
    async def wait_for(self, agent_id: str, waiter: Waiter) -> WaitResult: ...
```

三处刻意的设计：

- **`send` 默认激活目标。** 绝大多数调用方想要的语义是"发消息，它没在跑就叫醒它"。需要严格的"仅入队"语义时显式传 `activate=False`。**投递失败返回结构化 `DeliveryResult` 而不抛异常**，使调用方能识别可重试的瞬时错误。
- **`ensure_active` 是唯一的激活入口。** 复活、重开、从落盘会话恢复、恢复子会话在语义上是同一件事，不应暴露成四个方法让用户猜。
- **`find` 明文承诺绝不激活任何 Agent。** 只读路径必须与激活路径分离，否则查询类工具无法把"不存在"与"存在但未运行"分开回答。

`Waiter` 是密封类型：`UserInput` / `Confirmation(tool_call_id)` / `Reply(correlation_id)`。新增等待类型不需要新增公开方法。

### 4.3 关键分离：注册表 vs 进程内实例表

Agent 的**可序列化状态**与**运行时对象**必须分开存放，这是"存储层可替换"能否成立的地基。

```
AgentRegistry（Protocol，可远程、可多进程共享）
    AgentInfo: agent_id / name / type / status / parent_id / children
               / flags / metadata / created_at / session_id

LocalAgentTable（进程内，不进 Protocol）
    agent_id → Agent 实例、asyncio.Task、asyncio.Event
```

注册表回答"这个 Agent 处于什么状态"，本地表回答"这个 Agent 的对象在哪"。多进程部署时前者共享，后者各进程只持有自己那部分。

状态放在注册表而不是 Agent 自身，理由与操作系统把进程状态放在内核中相同：**Agent 阻塞或崩溃时无法自报状态。**

### 4.4 四个 Store Protocol

内核只依赖 Protocol，默认提供零依赖的内存 / 文件实现；生产部署可换 Redis / Postgres 实现而内核不改一行。

#### AgentRegistry

```python
class AgentRegistry(Protocol):
    async def register(self, info: AgentInfo) -> None: ...
    async def get(self, agent_id: str) -> AgentInfo | None: ...
    async def compare_and_set_status(
        self, agent_id: str, expected: AgentStatus, new: AgentStatus
    ) -> bool: ...
    async def update(self, agent_id: str, **fields) -> None: ...
    async def children_of(self, agent_id: str) -> list[str]: ...
    async def list(self, *, status=None, parent_id=None) -> list[AgentInfo]: ...
    async def unregister(self, agent_id: str) -> None: ...
```

**状态转换必须是 CAS 而不是直接赋值。** 单进程下直接赋值可行；多进程下两个执行体同时改同一个 Agent 的状态会丢失转换（例如挂起与中止相撞）。CAS 保证状态机在分布式实现下仍然是状态机。

#### MessageBroker

```python
class MessageBroker(Protocol):
    async def ensure_queue(self, agent_id: str) -> None: ...
    async def publish(self, agent_id: str, message: Message) -> str: ...
    async def consume(self, agent_id: str, *, max_n: int = 0) -> list[Message]: ...
    async def wait_for_messages(self, agent_id: str, timeout: float | None) -> bool: ...
    async def take_by_correlation(
        self, agent_id: str, correlation_id: str, timeout: float | None
    ) -> Message | None: ...
    async def remove_queue(self, agent_id: str) -> None: ...
    def on_missing_queue(self, cb: Callable[[str], Awaitable[bool]]) -> None: ...
```

- **`take_by_correlation` 按关联 ID 匹配，而不是接受任意谓词函数。** 谓词无法下推到远端队列，分布式实现只能把消息全部取到本地过滤再放回，既慢又有竞态。代价是表达力变窄（只能等"对某条特定请求的回复"），收益是分布式实现从不可行变为自然。
- **`on_missing_queue` 是按需激活的实现基础。** "队列不存在"这一事件直接触发懒启动与 ZOMBIE 复活。
- Redis 实现映射到 Streams：消费组提供 exactly-once，`XREAD BLOCK` 对应 `wait_for_messages`。

#### SessionStore

```python
class SessionStore(Protocol):
    async def exists(self, sid: str) -> bool: ...
    async def load_meta(self, sid: str) -> SessionMeta | None: ...
    async def load(self, sid: str) -> SessionRecord | None: ...
    async def save(self, sid: str, record: SessionRecord) -> None: ...
    async def archive(self, sid: str, record: SessionRecord) -> int: ...
    async def list_metas(self, **filter) -> list[SessionMeta]: ...
    async def replace_content(self, sid: str, content: list[ContextBlock]) -> None: ...
```

三条读写纪律写入 Protocol 文档并由契约测试保证：

- 会话正文可达数十兆。**只判断存在性必须用 `exists()`；只需要一两个元数据字段必须用 `load_meta()`。**
- **归档序号只增不补**：永远取当前最大值 +1，出现空洞不回填。
- 单个会话的查询不要建立在 `list_metas()` 之上（它会按过滤条件裁剪结果）。

#### ArtifactStore

```python
class ArtifactStore(Protocol):
    async def register(self, ref: ArtifactRef) -> None: ...
    async def get(self, artifact_id: str) -> ArtifactRef | None: ...
    async def versions_of(self, artifact_id: str) -> list[ArtifactRef]: ...
    async def list(self, *, kind=None, producer=None,
                   deliverable_only: bool = False) -> list[ArtifactRef]: ...
```

产物内容存放在 Workspace（见 §6），此处只存索引与元数据。

### 4.5 消息语义

两类消息统一经 `send()` 发出，由 `trigger` 区分消费时机：

- **`PENDING`（默认）** — 进入队列，在**下一轮 turn 开始前**被消费。用户中途插话，当前工具执行完即可见。
- **`DEFERRED`** — 挂在延迟队列，等**整个 Agent 工作结束后**依次消费。语义是"先把手上的事做完，然后再看这个"。

两者可互相转换。把"打断"与"排队"分成两种明确语义，而不是让用户猜测行为。

消费方通过消息自带的上下文块取得精确内容，**不依赖在轨迹中的位置推断**。消息来源记录在元数据中，只描述来源事实，**不决定界面展示策略**。

---

## 5. 单轮 turn 与扩展点

### 5.1 turn 循环

```
每一轮 turn：
 1. 中止检查                    置位则标记 ABORTED 并退出
 2. 挂起检查                    等待恢复（这段时间不计入任何 turn）
 3. 消费待处理消息               追加到上下文 → 触发 message_injected
 4. 注入已完成的背景工具结果
 5. turn_start
 6. 组装 messages = [system_block] + 当前轨迹
 7. pre_llm_request              可修改请求；messages 到此才定稿
 8. 调用 LLM                     异常三级分类，见下
 9. 中止再检查                   决定是否保留已攒出的部分回复
10. post_llm_response
11. 空 assistant block 守卫
12. 有工具调用 → 执行工具 → turn_end → 下一轮
    无工具调用 → 标记 COMPLETED → turn_end → 退出
```

三处必须严格实现，都是易被忽略的正确性问题：

**第 8 步：LLM 异常必须分三类，不可合并**

| 异常类型 | 处理 |
| --- | --- |
| 上下文超限 | 触发 `context_overflow`；插件返回"已处理"则结束本轮后重试下一轮 |
| 取消 | **直接透传，绝不当作 LLM 失败** |
| 其他 | 触发 `llm_failure`；插件可返回**替身响应**（重试与故障转移在此实现）；无人处理则给最近一条用户 / 工具消息打失败标记 |

**第 11 步：空 assistant block 绝不能进入上下文。** 模型返回既无内容又无工具调用的响应时，按 LLM 失败处理。原因：脏块会污染此后**所有**请求的前缀，故障表现为后续全部请求异常，排查成本极高。

**第 9 步：流式场景下的中止。** 中止会把已接收的分片汇聚成一个部分响应。若它带有真实内容则应保留（否则用户看到回复凭空消失）；若为空则干净取消。

另外两条：

- **轮次上限约束整个任务，而非单个执行周期。** 每个周期新建上下文，但轮次计数不重置。
- **turn 的可观测数据在 `turn_end` 之后才冻结**，这样切点自身的耗时也被计入。

### 5.2 前缀缓存稳定性（硬约束）

提示词前缀缓存按**请求字节前缀**匹配，System Prompt 位于最前。**System Prompt 变化一个字节，该会话此后所有请求的缓存全部失效。** 而 System Prompt 每轮重建、没有冻结机制，因此这是一条靠纪律维持的不变式：

1. **不变式是"前缀逐轮字节一致"。** 随轮变化的内容一律不进 System Prompt。
2. **动态环境事实注入历史，不进 System Prompt。** 工作目录、隔离状态等在创建时一次性注入历史，并做幂等检查以防恢复或分叉时重复注入。
3. **新增内容只追加，不修改既有前缀。**
4. **缓存亲和随会话语义。** 同类型分叉继承缓存键；跨类型分叉的 System Prompt 不同，只继承历史。
5. **一次性调用关闭缓存写入。** 生成标题、抽取记忆这类请求前缀发一次即弃，写入永远读不回来。

违反这些规则**不会报错，只会静默多付费用**。因此内核内置**前缀漂移检测**：逐轮比对前缀摘要，报出变更点及其之后浪费的 token。**开发模式默认开启，生产默认关闭** —— 默认关闭等于没人会用它。

### 5.3 扩展装配的三层

| 层 | 装配方式 | 用途 |
| --- | --- | --- |
| Agent 局部钩子 | `agent.register_hook(junction, cb)` | 单个 Agent 的行为增强 |
| Runtime 级插件 | `Runtime(plugins=[...])` | 跨 Agent 通用逻辑 |
| Runtime Module | 见 §5.5 | 带开关的自包含能力 |

执行顺序：**Agent 局部先于 Runtime 级**；可拦截切点串行执行，可观察切点可并行。

中间件契约三个字段：

```python
@dataclass
class HookResult:
    skip: bool = False               # 跳过原操作，并终止后续可拦截钩子
    modified: dict[str, Any] = ...   # 修改传给后续钩子与操作的参数
    result: Any = None               # 配合 pre_tool_use + skip=True 提供替代结果
```

### 5.4 切点集合

| 组 | 切点 | 可拦截 |
| --- | --- | --- |
| Runtime 生命周期 | `runtime_start` `runtime_shutdown` `agent_spawn` `agent_despawn` | — |
| Agent 生命周期 | `agent_start` `agent_end` `agent_error` | — |
| Turn | `turn_start` `turn_end` | — |
| 工具 | `pre_tool_use` `post_tool_use` | 是 |
| 工具确认 | `tool_confirmation_required` | — |
| LLM | `pre_llm_request` `post_llm_response` `context_overflow` `llm_failure` | 是 |
| 消息 | `message_injected` | — |
| 背景工具 | `background_tool_completed` `tool_moved_to_background` | — |

**扩展方式：固定核心枚举 + 命名空间字符串。** 内核切点是枚举（类型安全、可发现、IDE 可补全）；第三方能力注册带命名空间的切点（如 `artifact:created`），无需修改内核也无需等发版。

**切点名、Store Protocol、Flag key 全部计入公开 API**，变更即破坏性变更。

### 5.5 Runtime Module

**凡是带开关的能力，一律建模为 Runtime Module。** 一份 Flag 声明同时供代码、设置界面与文档站三方消费，从机制上杜绝三者不一致。

一个 Module 可以向 Runtime 注入四类东西：

- per-Agent 插件
- Runtime 服务（挂成 `runtime.<name>`，供宿主代码调用的有状态能力）
- **per-Agent 工具工厂** —— 每个新创建的 Agent 自动获得该能力的工具，**不需要宿主侧接线**
- 启停钩子

第三点对开源项目尤其重要：第三方贡献一个能力包，应当只写一个 Module，不改内核。

Flag 值域限制为 `bool | int | str`。需要结构化配置时拆成多个 Flag，或在插件内解析字符串。**文档从 Flag 声明自动生成。**

---

## 6. 执行面：Workspace 与隔离

### 6.1 两层抽象

| 层 | 职责 |
| --- | --- |
| **Workspace** | 生命周期与隔离：在哪工作、是否隔离、如何提交与合并 |
| **Portal** | 具体动作：执行命令、读写文件、传输文件、会话式终端 |

分层收益：Portal 可被套壳复用（例如包一层改写工作目录）；更重要的是，**对 Agent 与工具而言，本地子进程与远程容器的接口完全一致**。同一个 Agent 既能在本地跑，也能在集群容器里跑，Agent 代码零改动。

这一层同时是"推理与训练同源"的技术基础：训练时的批量沙箱与交互时的本地目录共用同一套 Agent 结构和工具实现。

### 6.2 归属

| 进 kernel | 进 backend |
| --- | --- |
| `Workspace` 协议、`Portal` 协议 | `workspace-docker` |
| `LocalWorkspace`（本地目录 + 子进程，零依赖） | `workspace-ssh` |
| 隔离策略协议 + 共享 / git worktree / 快照三种实现 | `workspace-e2b` 等托管沙箱 |

快照策略是 git worktree 不可用时的降级路径（见 §6.3），因此必须与它同在内核，否则降级无处可去。

### 6.3 以 git 作为通用产物版本层

**Workspace 初始化时，若目标目录不是 git 仓库则自动初始化（可关闭）。**

编程场景天然有 git；非编程的知识工作通常没有。而把 git 用作通用版本层，可以**免费获得**四样 work 场景必需的能力：

| 能力 | 来源 |
| --- | --- |
| 版本历史 | 每次产物改动一次提交 |
| **语义 diff** | "这一版比上一版改了什么" —— work 场景反馈回路的前提 |
| 并行隔离 | worktree 零克隆，N 个 Agent 各自一份 |
| 合并与冲突检测 | git merge 已解决此问题 |

其中 diff 最关键。work 场景的核心回路是"看产出 → 指出问题 → Agent 修改 → 再看"，而该回路依赖"能说清改了什么"。编程场景免费拥有它，非编程场景没有，因此借用 git。

Markdown、CSV、Mermaid、SVG 是文本，git 直接给出语义 diff。二进制格式（xlsx / docx / pptx）由产物处理器提供格式感知的 diff，git 只负责存版本（见 §7.2）。

**边界情况需在实现时处理**：用户目录已是 git 仓库（不得干扰其历史与分支）、大文件（需要 gitignore 策略或 LFS 提示）、无 git 可执行文件（降级到快照隔离）。

### 6.4 隔离默认值

**子 Agent 默认共享工作区，需要时显式隔离。** 大多数知识工作任务是协作而非竞争写入，默认隔离反而增加不必要的合并负担。隔离语义以**发起分叉的源 Agent** 的工作区为基准，而非调用者的工作区。

### 6.5 会话式执行

Portal 协议包含完整的会话式终端能力（创建 / 销毁会话、在会话中执行、注入按键序列、捕获屏幕、查询命令状态等），支持需要真实伪终端的交互式程序。

注意这与**背景化工具是两件不同的事**：后台执行一个构建只需要子进程加轮询，不需要伪终端。

### 6.6 Skill

Skill 抽象为**独立的 `SkillSource` 协议**，不挂在 Workspace 上。Workspace 只提供"从哪读文件"的能力。这样更换执行环境不影响 Skill 逻辑，反之亦然。

---

## 7. 产物与验收

这是本项目区别于现有框架的核心部分，也是唯一没有成熟先例可循的部分。

### 7.1 问题

编程场景中"活干完了没有"有免费答案：编译通过、测试通过、diff 可读。**非编程的知识工作一样都没有。**

此外，产物在框架眼里是透明的——它只是工作区里的一个文件。框架不知道它是交付物、不知道它有几个版本、不知道用户对它的哪一部分不满意。

因此需要补两样东西：**让产物在框架中拥有身份**，以及**在没有客观真值时如何判定完成**。

### 7.2 产物模型

原则：**内核只认身份与版本，不认格式。** 格式语义全部在后端。

```python
@dataclass(frozen=True)
class ArtifactRef:
    id: str              # 稳定身份，跨版本不变
    path: str            # 工作区内相对路径
    kind: str            # 语义类别：document / spreadsheet / diagram / dataset / code …
    mime: str            # 具体格式
    version: str         # 不透明版本标识，由隔离策略决定含义
    producer: str        # 产出它的 agent_id
    is_deliverable: bool # 交付物，还是中间产物
```

```python
class ArtifactHandler(Protocol):
    kinds: tuple[str, ...]
    def preview(self, ref: ArtifactRef) -> Preview: ...
    def semantic_diff(self, a: ArtifactRef, b: ArtifactRef) -> Diff: ...
    def validate(self, ref: ArtifactRef) -> list[Violation]: ...
```

`version` 是**不透明字符串**，含义由生效的隔离策略决定：git 策略用提交号，快照策略用单调递增序号。内核只做相等比较与排序，不解析其结构 —— 否则关闭 git 时（§6.3 允许）产物版本就无处安放。

`kind` 是**内核定义少量基础类别 + 处理器可注册新类别**，与切点的扩展方式保持一致。

`semantic_diff` 是这套设计的支点。文本格式由 git 提供 diff；二进制格式由处理器读出结构后比较（例如"B 列公式从求和改为平均"这类 diff，git 无法给出）。

### 7.3 产物如何进入上下文

**给上下文块增加一个字段**，不新增块类型、不改动消息模型：

```python
class ContextBlock:
    role: str
    content: str | list | None
    tool_calls: ...
    artifacts: list[ArtifactRef] = []   # 这一轮涉及的产物
```

必须机器可读而非散落在正文里，因为有五个消费方依赖它：

| 消费方 | 依赖什么 |
| --- | --- |
| 验收 | 本次交付物是哪几个 |
| 界面 | 渲染"本轮产出了某产物的第 N 版"并提供预览 |
| **反馈回路** | 用户指出"这张表第 3 列不对"时，该反馈必须**带着产物引用**回到 Agent |
| 多 Agent 协作 | 一方产出、另一方评审，评审方需要知道看什么 |
| 训练出口 | "产出了 X，被接受 / 被打回"是天然的奖励信号 |

### 7.4 交付物如何被认定

两条腿并用：

- **隐式追踪** —— 在 `post_tool_use` 上挂钩，写入与编辑类工具触碰过的路径自动登记为产物。零成本获得全景。
- **显式提升** —— 提供 `Deliver(path, kind, note)` 工具，由 Agent 宣布"这是交付物"。

两者都要的原因是成本：隐式追踪会把临时脚本、中间数据、调试输出全部收入，若拿这些去验收会浪费大量 token 检查无人关心的东西。`is_deliverable` 是成本控制手段。

### 7.5 验收架构

**主路径是双伴生 Agent 验收**，效果优先，成本由用户通过 Flag 控制。此外在其之前放一层零成本过滤。

| 层 | 做什么 | 成本 |
| --- | --- | --- |
| **L1 格式校验** | `ArtifactHandler.validate()` —— 表格是否有空列、文档是否有空章节、图表语法是否合法 | 零 token |
| **L2 输出验收** | 伴生 Agent 判断"用户要求的是否都完成或已明确取消" | LLM |
| **L3 结果验收** | 伴生 Agent 依据验收标准检查最终回复与工作区证据 | LLM |

L1 在前，是因为它免费且能挡掉一类明显问题；L2/L3 是主力。

**触发时机**：L1 在产物登记或变更时立即运行（廉价、可频繁）；L2 与 L3 在 Agent 判定完成时运行，各自由独立 Flag 控制开关与重试上限 —— 二者都会带来额外 LLM 开销，用户需要能分别取舍。

两个伴生 Agent 都拥有独立的轮次预算，通过受限接口读取主 Agent 的历史（只能读取所绑定的那一个）。

**L2 的偏置设定**：判定保守。宁可让主 Agent 多做一轮，也不要让它过早收工。

**L3 的裁决是三态而非布尔**：

```
status = pass | partial | block
```

- `pass` —— 每条验收标准都已验证满足
- `partial` —— 未完成**或无法确认**
- `block` —— 有具体证据表明结果是错的

区分 `partial` 与 `block` 的原因是二者应触发的后续动作不同："未完成"和"已证伪"的重试次数上限分别配置。

### 7.6 验收标准与锚点规则

验收标准由一个伴生 Agent 在**工作开始之前**从任务输入中推导并持久化。它只能看到"被要求了什么"，看不到"产出了什么"——否则会退化为事后为结果编造标准。

防止它凭空发明需求的，只有一条规则：

> **每条验收标准都必须有锚点。**
>
> 锚点是一条验收方可以重新打开的引用链，例如 `spec.md:12 → protocol.md:88`，或 `用户消息#3 "增加一个 --json 参数"`。
>
> 若某项要求无法关联到这样的锚点，那就是在发明需求，不得写入。

这一条测试替代了所有"这算不算合理推断"的判断题：**有锚点，或者没有。**

**本项目的扩展**：验收标准可以锚定到产物上（如 `artifact:report.md#section-4`）。这样标准不只约束"回复里说了什么"，还能约束"产物里有什么"。

### 7.7 人的反馈升级为持久标准

知识工作中人会持续介入："这张表第 3 列口径不对"、"报告结论太弱"。若这些反馈只作为普通消息进入对话，Agent 修改后即失效，**下次仍会犯同样的错**。

而人的反馈**天生自带锚点**（就是这句话本身），且往往自带产物位置。因此它可以升级为一条验收标准：

```
R7: 表格 sales.xlsx 的第 3 列必须使用「已确认收入」口径
    anchor: 用户消息@2026-09-03T14:22 "这张表第3列口径不对，要用已确认收入"
            artifact:sales.xlsx#col-3
```

一旦成为验收标准，它进入此后每一轮验收 —— **同样的错不会犯第二次**。

**机制：自动提议，人确认后生效。** 不自动直接生效，避免把随口一句话固化成永久标准。

---

## 8. Surfaces

### 8.1 CLI

统一入口，一张路由表加**全量惰性导入** —— `syndica --version` 不导入任何产品包。

便利规则：第一个参数以 `-` 开头且不是已知顶层选项时，整体转给默认子命令，因此 `syndica --port 9010` 等价于 `syndica up --port 9010`。

### 8.2 实时传输

- **SSE 为主通道**（会话级流 + 全局流）。Agent 事件流本质是单向推送；SSE 自带重连与断点续传、穿代理无障碍、浏览器原生支持。
- **WebSocket 用于两处**：类型化 RPC，以及伪终端（真双向、低延迟）。

### 8.3 互操作协议

四个全部支持：

| 协议 | 作用 | 优先级 |
| --- | --- | --- |
| **MCP** | 工具与资源接入 | 必需，不支持等于自绝于工具生态 |
| **ACP** | 客户端驱动 Agent。**双向红利**：我们的 Agent 能被第三方客户端驱动，我们的界面也能驱动第三方 Agent CLI | 高 |
| **A2A** | 与其他框架的 Agent 互通 | 中 |
| **OpenAI 兼容 HTTP** | 让现有客户端直接请求我们 | 中 |

### 8.4 Web UI

**React + TypeScript，构建产物随 Python 包发布。**

理由：Agent 界面是重实时流、重虚拟列表的场景，需要类型系统与成熟组件生态；更重要的是贡献者门槛——无框架无构建的原生方案在内部项目里有"改一行刷新即见"的优势，但对开源项目会显著流失现代前端贡献者。

**开发者需要 Node，用户不需要。** 构建产物随包发布，`pip install` 即可运行。

### 8.5 Runner 与训练出口

分两层，实现厂商中立：

```
层 1  中立轨迹（kernel / runner 产出）
      messages + 工具调用 + 产物 + 验收结论 + 时序
      纯 JSON，不含 token id，不依赖分词器

层 2  导出器（backend，按需安装）
      → SFT：      messages jsonl / ShareGPT
      → 偏好数据： chosen / rejected 对
      → RL：       token id + loss mask（需分词器，故进 backend）
      → 对接：     至少一个开源训练框架（TRL 或 verl）
```

**验收结论直接作为训练信号。** 三态裁决加上"哪些标准满足 / 待办"即为奖励；同一任务的多次尝试配上不同裁决即为偏好对。

这是本项目独有的能力，**因为验收是内核能力，训练信号是它的副产品**。其他框架若要产出偏好数据，需另行搭建打分流程。

---

## 9. 错误处理

总原则：**区分哪些可降级、哪些必须失败，并让降级路径显式可见。**

| 场景 | 处理 | 理由 |
| --- | --- | --- |
| 工具致命错误 | 抛出，交给 Agent 循环 | Agent 需要知道这条路不通 |
| 工具非致命错误 | 进入工具响应给模型看 | 模型可自行换方式重试 |
| 消息投递失败 | 返回结构化结果，**不抛异常** | 调用方需区分可重试的瞬时错误 |
| 会话数据损坏 | **加载期尽量修复，不抛错**，损坏的子项跳过 | 一个子 Agent 的脏数据不应让整个会话打不开 |
| 调试数据写入失败 | 只丢调试数据 | 可观测性不得拖垮主流程 |
| Module 启停钩子失败 | 只记录日志 | 一个可选能力不应阻止 Runtime 启动 |
| 批处理单任务崩溃 | 转为失败结果并记录堆栈 | **绝不让一个任务拖垮整批** |
| 产物处理器失败 | 降级为二进制 diff / 无预览，**不阻塞** | 产物本身没问题，只是我们看不懂它 |
| 验收器自身失败 | **放行（fail-open）**，但把"未完成验收"显式标记出来 | 验收器异常不应让任务永远无法完成；同时不得伪装成已验收 |

批处理还有一条实现要求：**资源获取全部置于 try 内**，保证任何一步失败都能走到清理逻辑释放沙箱。

---

## 10. 测试策略

Agent 框架最难测的是**编排逻辑**——多 Agent 互发消息、状态机转换、工具调用顺序。

| 层 | 测什么 | 何时运行 |
| --- | --- | --- |
| 单元测试 | 纯函数、schema 生成、状态机转换、路径解析 | 每次推送 |
| L1 进程内小流程 | 单 Agent turn 循环的局部行为 | 每次推送 |
| **L2 场景回放** | **多 Agent 编排**、工具顺序、状态快照、事件流单调性 | 每次推送 |
| 契约测试 | 见下 | 每次推送 |
| **真 LLM 端到端 + 评判** | 效果正确性的最终权威 | 手动 / 合并前 / 定时 |

### 10.1 L2 场景回放

声明式场景描述 + **脚本化的假 LLM**，驱动**真实的** Runtime、Agent 与工具执行。断言最终输出、工具调用顺序、状态快照。

关键在于**只有 LLM 是假的，其余都是真的**。这样能覆盖"A 派活给 B、B 汇报、A 继续"这类编排逻辑，且快速、确定、零成本。

它同时是**唯一能测试失败路径的手段**：LLM 异常三级分类、空响应守卫、中断保留部分回复——这些分支无法用真实模型按需触发。

### 10.2 真 LLM 端到端

效果正确性的唯一权威，使用 LLM 评判配合验收标准打分。单独流水线，手动或定时触发。

**两层缺一不可**：仅有真 LLM 测试，则贡献者与 CI（来自 fork 的 PR 拿不到密钥）都无法运行测试，等于没有任何自动化能挡住回归。

### 10.3 Protocol 契约测试（对外承诺）

四个 Store Protocol 各配一套**通用契约测试套件，由内核发布**。任何实现——内置的、我们的后端、第三方的后端——都必须通过。

```python
class BrokerContractTests:
    """任何 MessageBroker 实现都必须通过。"""
    async def test_publish_then_consume_preserves_order(self, broker): ...
    async def test_consume_is_exactly_once(self, broker): ...
    async def test_wait_for_messages_wakes_on_publish(self, broker): ...
    async def test_take_by_correlation_leaves_others_queued(self, broker): ...
    async def test_missing_queue_triggers_callback(self, broker): ...
```

理由：**"可替换"若无测试证明即是空话。** 契约测试同时是可执行的契约文档，比散文更能说清我们承诺什么。CAS 语义尤其需要它——它是并发正确性的地基，内存实现下很容易"看起来对"。

### 10.4 长跑测试

- **假时钟注入** —— 定时任务、超时、重试退避不能靠真等
- **确定性种子** —— 并发调度与随机退避在测试下必须可复现
- **故障注入** —— 可在指定切点注入 LLM 失败、工具超时、网络错误。长跑系统的降级路径必须被主动测过，否则线上首次触发即是事故

---

## 11. 工程规范

| 项 | 决定 |
| --- | --- |
| License | **MIT** |
| 首个公开版本 | **v1.0**（意味着 API 稳定承诺；v0.x 与"已完成"矛盾） |
| 版本策略 | 语义化版本。**切点名、Store Protocol、Flag key 均计入公开 API** |
| 贡献者协议 | 发布前再定（DCO 或 CLA） |
| 文档 | **Flag 文档从声明自动生成**；必需内容见下 |
| 必需文件 | `LICENSE` `README` `CONTRIBUTING` `CODE_OF_CONDUCT` `SECURITY`、issue/PR 模板、`AGENTS.md` |

文档站必需内容：五分钟快速上手、核心概念（Agent / Runtime / Workspace / Artifact / 验收）、四个 Protocol 的契约、如何编写自己的 Agent / 工具 / 后端、四个互操作协议的接入方式、自动生成的 API 参考。

CI 检查项：

- lint、格式化、类型检查
- 单元测试 + L1 + L2 回放 + 契约测试（全部免费可跑，无需 API 密钥）
- **依赖方向校验**（§3.2 六条规则必须机器可验）
- 覆盖率门槛（增量覆盖率不低于 50% 作为起点）
- 多 Python 版本 × 多操作系统矩阵
- 真 LLM 端到端（单独流水线）

---

## 12. 里程碑

发布策略是"完整形态之前不公开发布"，因此以下里程碑均为**内部里程碑**。

| # | 里程碑 | 完成判据 |
| --- | --- | --- |
| 1 | 内核骨架 | Runtime 能创建多个 Agent、互发消息、各自跑通"LLM + 工具"循环、可终止与恢复。含**最小本地执行能力**（文件读写 + 命令执行），因为没有它连一轮有意义的 turn 都跑不出来 |
| 2 | **自举** | **框架能修改自己的代码**：读写文件、执行命令、跑测试、读取结果并迭代。CLI 可用 |
| 3 | 扩展点完备 | 三层装配 + Runtime Module + 命名空间切点 |
| 4 | 执行面完整化 | 把里程碑 1 的最小实现补成完整协议：Workspace / Portal 全量接口、git 版本层、worktree 与快照隔离、伪终端 |
| 5 | 状态 | 会话持久化与恢复、上下文压缩、分层记忆 |
| 6 | 产物与验收 | 产物模型、处理器、验收标准与锚点、三态裁决、反馈升级 |
| 7 | 产品面 | HTTP + SSE + React 界面 + 移动端适配 |
| 8 | 互操作 | MCP → ACP → A2A → OpenAI 兼容 |
| 9 | 训练出口 | 中立轨迹 + SFT / 偏好 / RL 导出器 + 至少一个训练框架对接 |
| 10 | 发布准备 | 英文文档、文档站、CI 矩阵、许可与贡献流程 |

**里程碑 2 是分水岭，优先级最高。** 在它之前每一行代码都得人写；之后维护者的产出效率与框架自身的好用程度形成正反馈。对单人维护 + 追求大范围的项目，这是唯一有效的杠杆。

**本文是架构设计，不是实现计划。** 十个里程碑各自需要一份独立的实现计划 —— 一份计划覆盖全部范围既写不完也读不动。计划按里程碑逐个产出，前一个完成后再写下一个，这样后面的计划能吸收前面的实际情况。

---

## 13. 风险

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| **范围过大而长期无可用版本** | v1.0 包含四个互操作协议、伪终端、双伴生验收、React 界面、RL 导出、四个 Protocol 及契约测试。单人维护下这是很长的周期，而在发布前没有外部反馈与贡献 | 里程碑 2（自举）尽早达成；每个里程碑内部可用 |
| **产物与验收无先例** | §7 是全新设计，若产物抽象设计失误，返工面很大 | 内核只认身份与版本，格式语义在后端；先用文本格式验证回路，再扩展二进制格式 |
| **前缀缓存纪律靠人守** | 违反不报错，只静默增加费用 | 漂移检测开发模式默认开启 |
| **四个协议的维护负担** | 每个协议都是一套契约、文档与兼容承诺 | 按 MCP → ACP → A2A → OpenAI 兼容 的顺序实现，前一个稳定后再开下一个 |
| **单点维护** | 个人项目，无团队冗余 | 自举 + 发布后引入贡献者；契约测试降低第三方贡献后端的门槛 |
| **验收使 token 成本翻倍** | 双伴生 Agent 方案的固有代价 | L1 零成本前置过滤；验收整体由 Flag 控制，用户自行权衡 |

---

## 14. 决策速查

| # | 决策 | 结论 |
| --- | --- | --- |
| 1 | 语言 | Python |
| 2 | 并发模型 | 单进程 asyncio 内核；四个存储面 Protocol 化可替换 |
| 3 | 包结构 | 一个 kernel + N 个 surface + M 个可选 backend |
| 4 | Agent 状态机 | 六态，保留 ZOMBIE |
| 5 | Runtime 公开 API | 11 个方法 |
| 6 | 注册表 | 拆分为可序列化状态 + 进程内实例表 |
| 7 | 状态转换 | CAS，非直接赋值 |
| 8 | 等待回复的匹配 | 按关联 ID，不接受任意谓词 |
| 9 | 消息时机 | PENDING + DEFERRED 双时机 |
| 10 | 切点扩展 | 固定核心枚举 + 命名空间字符串 |
| 11 | Runtime Module | 完整机制进 v1 |
| 12 | 前缀漂移检测 | 第一天做，开发模式默认开启 |
| 13 | 背景化工具 | 进 v1 |
| 14 | 执行面 | Workspace / Portal 两层 |
| 15 | 产物版本层 | git，默认开启可关闭 |
| 16 | Skill | 独立 SkillSource 协议 |
| 17 | 伪终端 | 完整进 v1 |
| 18 | 子 Agent 隔离 | 默认共享，显式隔离 |
| 19 | 产物进上下文 | 上下文块增加 artifacts 字段 |
| 20 | 交付物认定 | 隐式追踪 + 显式 Deliver |
| 21 | 验收架构 | 双伴生 Agent 为主 + L1 零成本前置 |
| 22 | 验收标准 | Rubric + 锚点规则；三态裁决 |
| 23 | 人的反馈 | 自动提议标准，人确认后生效 |
| 24 | 产物类别 | 核心少量 + 处理器可注册 |
| 25 | 实时传输 | SSE 为主，WebSocket 用于类型化 RPC 与伪终端 |
| 26 | 互操作协议 | MCP + ACP + A2A + OpenAI 兼容，全部支持 |
| 27 | Web UI | React + TypeScript，构建产物随包发布 |
| 28 | 训练出口 | 中立轨迹 + 导出器，含 RL；验收结论作为训练信号 |
| 29 | 验收器失败 | fail-open，但显式标记未验收 |
| 30 | 测试 | L2 脚本化回放跑 CI + 真 LLM 端到端为权威，两层缺一不可 |
| 31 | 契约测试 | 由内核发布，作为对外承诺 |
| 32 | 发布策略 | 完整形态之前不公开发布，首版即 v1.0 |
| 33 | License | MIT |
