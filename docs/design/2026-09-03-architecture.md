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

**长程任务（long-horizon）是本项目主动选择的探索方向，不只是一项功能。** "7×24" 不是说进程一直开着，而是说任务本身可以跨越数小时到数天。这类任务的失败模式与短任务完全不同（目标漂移、进度不可见、不知何时停、上下文腐烂、成本失控、无法自我纠偏），需要专门的机制而非更强的模型。§8 给出这套机制，并且**刻意把长跑范式做成可替换的策略**——目标是让本项目成为长程任务的研究平台，而不是某一种循环的固定实现。

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
              llm 抽象 / workspace / artifact 与 verify 的类型与协议
              长程任务协议（Budget / Goal / 进度 / 停止判据）
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
                    flags=None, metadata=None, budget=None) -> str: ...
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

`WaitResult` 的状态集合：`answered` / `timeout` / `preempted` / `aborted`。**`timeout` 是必须的** —— 长跑场景下等人工输入可能几小时无人查看，没有超时就是永久挂起，而一个挂住的 Agent 会一直占着预算与租约。每个 `Waiter` 携带自己的等待上限。

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

#### 租约与孤儿回收

这个分离带来一个必须处理的后果：**持有 Agent 实例的进程崩溃后，注册表里会留下一个状态为 `RUNNING` 但没有任何进程在跑它的条目。** 单进程部署下重启即清空；多进程部署下这种孤儿会持续存在，并且会阻止该 Agent 被重新激活（它"看起来"正在运行）。

因此 `AgentInfo` 携带租约：

```python
owner: str          # 持有该 Agent 实例的进程标识
heartbeat_at: float # 最近一次心跳
```

- 持有进程周期性续租；租约过期即视为孤儿
- 孤儿可被回收（转为 `ZOMBIE`，保留可寻址性与已落盘的会话）或被其他进程接管
- **单进程内存实现下租约恒有效，零额外开销** —— 这个机制只在需要它的部署形态下产生成本

### 4.4 五个 Store Protocol

内核只依赖 Protocol，默认提供零依赖的内存 / 文件实现；生产部署可换 Redis / Postgres 实现而内核不改一行。第五个是
里程碑 3 加的 `TraceStore`，见本节末尾。

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

`update()` 是无条件写，因此**只允许用于单一写者拥有的字段**（例如该 Agent 自己的 metadata）。**多方可写的集合类字段（如子 Agent 列表）必须提供专用的原子操作**，不能让调用方"读出来、改一改、写回去"——那在多进程下必然丢更新。

#### TraceStore（里程碑 3）

```python
class TraceStore(Protocol):
    async def begin(self, header: TraceHeader) -> None: ...
    async def append(self, trace_id: str, spans: Sequence[Span] = (), edges: Sequence[CausalityEdge] = ()) -> None: ...
    async def put_payload(self, trace_id: str, payload: RedactedText) -> PayloadRef: ...
    async def put_replay(self, trace_id: str, document: RedactedText) -> None: ...
    async def finish(self, trace_id: str, status: SpanStatus, ended_ns: int) -> None: ...
    async def traces(self) -> list[TraceHeader]: ...
    async def find(self, prefix: str) -> TraceHeader | None: ...
    async def get(self, trace_id: str) -> TraceHeader | None: ...
    async def spans(self, trace_id: str) -> list[Span]: ...
    async def edges(self, trace_id: str) -> list[CausalityEdge]: ...
    async def payload(self, ref: PayloadRef) -> str | None: ...
    async def replay(self, trace_id: str) -> str | None: ...
    async def delete(self, trace_id: str) -> None: ...
```

**写入面只收 `RedactedText`**：脱敏器的输出是唯一能进 store 的东西（§12.3）。payload 按内容的 sha256 引用；没有
结局（`status` 为空）的 trace 是记录到一半就断了的，回放与导出都拒绝它。内核带一份内存实现与随发行版分发的契约套件；
CLI 用文件实现 `FileTraceStore`（`<工作区>/.syndica/traces/<UTC 时间>-<id 前 8 位>/`，目录 0700、文件 0600，每一级按
名字以 `O_NOFOLLOW` 打开）。

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

### 4.6 Agent 定义方式

两种形态，判定规则只有一条：**清单里有没有指向 Python 类的字段。** 有则为类式，无则在运行时生成一个声明式 Agent 类。

**声明式 Agent** —— 一个清单加一段提示词，零 Python 代码：

```yaml
name: Researcher
description: |            # 编排者据此决定把活派给谁
  需要跨多个来源做调研并给出带引用的结论时用我。
  不要用我改代码或写最终交付物。
system_prompt: prompt.md
tools: [Read, Grep, Glob, WebSearch, WebFetch]
subagents: [Explorer]
model: default              # 可选
extends: null               # 可选，继承某个类式 Agent 的工具与行为
skills: []                  # 可选
budget: {turns: 40}         # 可选，角色级默认预算
artifact_kinds: [document]  # 可选，声明该角色产出什么类型的产物
```

清单字段集刻意保持在这十项：覆盖身份、能力、编排、资源、产出五类。**清单字段属于公开 API**，宁少勿多。

**类式 Agent** 用于需要自定义工具注册、钩子，或复杂提示词组装的场景。

#### 为什么声明式是承重设计

若新增一个角色需要写 Python，则 §1 主张 1 的"Multi-Agent 是一等公民"不成立。成熟实践中预置角色有七成以上是声明式的——**声明式是让"加角色的边际成本趋零"的唯一办法**，而这个成本直接决定用户是否愿意把工作拆给多个角色。

#### 两条实操约束

**`description` 必须写"什么时候该用我"，而不是"我是什么"。** 它是编排者派活的唯一依据。"我是一个调研 Agent"没有信息量；"需要跨来源调研并给出带引用结论时用我，不要用我写最终交付物"才可用。文档模板中固化这一要求。

**提示词插值变量只允许"同一会话内逐轮不变"的量。** `{os}` / `{platform}` / `{shell}` 允许（机器不变）；**`{cwd}` 明确禁止** —— 工作目录属于动态环境事实，按 §5.2 纪律第 2 条必须注入历史而非 System Prompt。这是内部一致性约束，不是风格偏好。

#### 加载与覆盖

用户定义的 Agent 同名覆盖内置 Agent。**被覆盖的定义必须记录且可查询** —— 否则"我改了为什么没生效"无法排查。加载源可注册，支持目录扫描。

### 4.7 Topology

拓扑回答三个问题：**系统里有哪些角色、如何寻址到它们、何时启动。**

```python
@dataclass
class AgentSlot:
    # 身份
    name: str
    agent: str                       # Agent 注册名（声明式或类式皆可）
    is_root: bool = False

    # 寻址
    cardinality: Literal['singleton', 'keyed', 'on-demand'] = 'on-demand'
    key_field: str | None = None     # keyed 时用哪个字段作为实例键

    # 生命周期
    boot: Literal['eager', 'lazy', 'manual'] = 'lazy'
    exit_mode: ExitMode = ExitMode.ZOMBIE

    # 构造（只增量增强，见下）
    options: dict = ...
    budget: Budget | None = None     # 角色级预算，实例预算由此切分
    plugins: list = ...
    metadata: dict = ...
```

| cardinality | 含义 | 典型用途 |
| --- | --- | --- |
| `singleton` | 全局唯一 | 根协调者 |
| `keyed` | 按键区分实例 | 每个工作区一个负责人 |
| `on-demand` | 按需创建，无固定身份 | 工人 |

启动策略：`eager`（Runtime 启动即创建）、`lazy`（首次寻址到才创建，依赖 §4.4 的 `on_missing_queue`）、`manual`（仅显式创建）。

#### 硬约束：拓扑只声明结构，不声明行为

slot 可以给 Agent **附加**工具、插件、预算，**但不得替换 Agent 自身的基础配置**。

理由是可 debug 性：若拓扑能覆盖基础配置，同一个 Agent 在不同拓扑下行为不同，排查时必须同时读 Agent 定义与拓扑定义才能知道它实际是什么。这类问题极难定位，而收益（省掉定义一个新角色）远小于代价。需要不同行为就定义不同角色。

#### 为什么值得独立成机制

**它让"换一个产品形态"从改代码变成改数据。** 几十行拓扑声明即可描述一个多 Agent 产品的组织结构——谁是根、谁向谁汇报、谁能派活、谁按需创建。而组织结构的变化频率远高于内核。

这也是 §8.6"长跑范式可插拔"能落地的前提：换范式等于换拓扑加换策略 Module，不动骨架。

### 4.8 内核内置的 Agent

清单受"kernel 零重依赖"（§3.2 规则 5）约束，**这不是清单问题而是架构判断**：任何需要外部服务的角色都不能进 kernel。

| 角色 | 用途 | 形态 |
| --- | --- | --- |
| `Orchestrator` | 编排：拆解任务、派活、汇总结果 | 类式（需要动态工具装配） |
| `Planner` | 只读规划：调研上下文，输出范围、决策、依赖、成功判据 | 声明式 |
| `Explorer` | 只读探索：定位文件、符号、引用 | 声明式 |
| `Worker` | 通用执行：改文件、跑命令、验证结果 | 声明式 |

**明确不进 kernel 的角色**：需要联网检索的研究型角色依赖外部搜索服务，按规则 5 进 backend。

**验收相关的三个 Agent（RubricsBuilder / OutputVerifier / OutcomeVerifier）不在此列** —— 按 §7.5 的归属，它们随验收 Runtime Module 提供，kernel 只持有 `Rubric` / `Anchor` / `Verdict` 类型。列这份清单时才发现这一点，正说明清单本身是有效的一致性检查。

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

- **轮次上限约束整个任务，而非单个执行周期。** Agent 持有自己的上下文并跨周期沿用，轮次计数同样不重置。两者必须一致：若每个周期新建上下文而计数继续，第二个周期就会带着空轨迹立刻撞上上限；而新建上下文本身也与 ZOMBIE 的存在理由冲突——它留在注册表可寻址，正是为了「一条消息就能让它带着上下文继续干」。
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
| Runtime 生命周期 | `runtime_shutdown` `agent_spawn` `agent_despawn` | — |
| Agent 生命周期 | `agent_start` `agent_end` `agent_error` | — |
| Turn | `turn_start` `turn_end` | — |
| 工具 | `pre_tool_use` `post_tool_use` | 是 |
| 工具确认 | `tool_confirmation_required` | — |
| LLM | `pre_llm_request` `post_llm_response` `context_overflow` `llm_failure` | 是 |
| 消息 | `message_injected` | — |
| 产物 | `artifact_registered`（产物登记 / 变更后由内核触发，L1 验收挂它，§7.4 / §7.5） | — |
| 验收 | `completion_proposed`（Agent 判定完成时、`_finish` 快照之前触发，L2 / L3 挂它；已处理 = `modified['verdict']`，§7.5） | 是 |
| 背景工具 | `background_tool_completed` `tool_moved_to_background` | — |

`runtime_start` 不进入内核切点：`Runtime.__init__` 是同步构造，当前没有可等待 hook 的启动阶段。
为它增加 `Runtime.start()` 会让每个宿主多一个必须正确调用的新生命周期入口。需要异步初始化的能力
应在宿主完成后再构造 Runtime，或由后续明确引入异步生命周期的里程碑统一设计。

**扩展方式：固定核心枚举 + 命名空间字符串。** 内核切点是枚举（类型安全、可发现、IDE 可补全）；第三方能力注册带命名空间的切点（如 `artifact:created`），无需修改内核也无需等发版。

**谁来触发扩展切点。** 这一点必须写明，否则扩展切点是个空壳——注册了却没人调用：

- 内核枚举切点由内核在 turn 循环等固定位置触发
- **扩展切点由声明它的那个能力自己触发**，内核提供公开的触发接口
- 命名空间格式固定为 `<owner>:<event>`，`owner` 必须与注册者标识一致。这既避免不同扩展抢同一个名字，也防止一个扩展冒用另一个扩展的切点名发事件

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

### 5.6 Tool 系统

#### schema 三级回退

| 优先级 | 来源 | 适用场景 |
| --- | --- | --- |
| 1 | 手写 schema | 需要精确控制给模型看到的描述 |
| 2 | 从类型化模型生成 | 既做入参校验又出 schema |
| 3 | 反射函数签名 + docstring | 兜底：写个普通方法就能当工具 |

三级都要的理由：**工具描述的质量直接决定模型是否用对它**，所以必须允许手写调优；但多数工具不需要那么精细，反射足够。只留手写会让加工具变贵，只留反射会让关键工具无法调优。

#### 结果形态

```python
class ToolResult(BaseModel):
    output: str | list                 # 给模型看的
    meta: dict                          # 给工作流用的结构化数据，不进模型上下文
    artifacts: list[ArtifactRef] = []   # 本次调用触碰的产物
```

**给模型看的与给代码用的必须分开。** 混在一起只能二选一：要么模型上下文被结构化数据污染（浪费 token 且干扰注意力），要么工作流只能解析给人看的文本（脆弱且易随提示词调整而崩）。

`artifacts` 是 §7.4 隐式追踪的载体 —— 写入类工具在此主动报告触碰了什么，框架不必去监控文件系统变化并猜测因果。

致命错误直接抛出交给 agent loop（§10）；非致命错误进入 `output`，让模型自行换方式重试。

#### 并发执行

一轮内的多个工具调用分段并行，**安全判定从严**：

- 参数解析失败、工具不存在、需要人工授权 —— **一律判为不安全**
- 其余才询问工具自身是否可并发

从严的理由是代价不对称：并发执行两个修改同一份文件的工具，或并发执行一个本应人工确认的危险命令，**后果不可逆**；而串行的代价只是慢一点。

并发上限可配。

#### 权限与确认：判定与处置分离

**危险判定是纯规则**，只输出稳定的规则 ID、风险等级与原因。它**不读配置、不调 LLM、不决定交互方式**。

**处置由宿主决定**：直接阻断 / 让模型自行授权 / 触发人工确认。

分离的理由：同一套规则在不同环境需要完全不同的处置 —— 自动化批处理中危险命令应直接阻断，交互式产品中应询问用户。若规则内写死处置，这套判定就无法跨场景复用。

**授权作用域：仅放行当前这一次调用。** 不提供"此后均可"的选项。长跑场景下 Agent 会执行数百轮，一次宽泛授权可能在完全不同的上下文中被使用 —— 这是长跑特有的风险，短任务中感受不到，因此不能按短任务的直觉来设计。

#### 注入点与可选规则（M2）

宿主在冻结目录与装配拓扑时把判定与处置交给内核：`AgentCatalog.freeze(..., *, danger_judge=None, danger_policy=None)` 与 `Runtime.assemble(..., danger_judge=None, danger_policy=None)`，交给每个建出的 `AgentOptions`。`RuleTablePolicy(table)` 按规则 ID 给处置：表里有的照表，表外的 HIGH 阻断，NONE / LOW 放行。按角色区分的策略归里程碑 4。

`DEFAULT_RULES` 之外有三族可选规则（都是 HIGH，由 `danger.py` 里的工厂建出）：

- `shell.git_state_change`：git 子命令不在只读集合里；`-c`、`--git-dir` 这类改配置或改仓库位置的全局选项；`GIT_` 开头的赋值前缀；任何参数或重定向目标落在 `.git/` 之下；
- `shell.path_outside_workspace`：参数与重定向目标里指向工作区外的路径（绝对路径、`~`、`$HOME`、会逃出根的 `..`）；
- `shell.workspace_root_removal`：`rm` 的目标规范化之后是工作区根或根下全部。

**哪个词是程序，两个方向上的保守相反，这是承重的：** 找危险程序时宁可多判，`setsid`、`timeout`、`stdbuf` 之后的每个可能的命令词与 `uv run` 一类转发启动器展开出的程序都算；豁免程序词时宁可少判，只豁免确定的那一个。找危险程序时唯一的收窄：选项全认得出的 wrapper 启动的是确定不执行实参的程序（grep、echo、cat 这类）时，它的实参不算命令词——runner（`strace`、`caffeinate`）列不全，不执行实参的那一边才列得全，收窄只能落在这一边。规则只做字符串运算，不碰磁盘、不读环境变量与 cwd，工作区根在构造时解析一次。

共享分词随之加固，默认规则的判定按 shell 的实际行为修正：保留字不再当成命令词；命令词不分大小写（APFS 上 `GIT push` 跑的就是 git）；反斜杠、`$'…'`、注释与 heredoc 按 shell 的规则处理；`shell.remote_script_execution` 改认「下载来的内容被当成脚本执行」，不再只认管道末尾那个词。多认出的是被引号、注释、heredoc 藏住的命令；少认的只有 shell 不会执行的写法——`\` 加 CRLF、`$((…))` 算术、只读数据的程序收下的 heredoc 正文（M2 计划「意外发现」记有两次差分）。规则读的是命令文本：转手执行的命令（shell `-c` 的代码、`eval` 的实参、`xargs` 与 `find -exec` 启动的命令）当命令再拆一遍，接在全部段之后（插在中间会改掉管道关系），超过 8 层按命中处理——层数由模型决定，读不到的那层里可能正是要拦的命令；脚本文件、变量与非 shell 解释器的代码看不见。`find` 有一次删除（`-delete` 或 `-exec rm {}`）落在起点下几乎全部文件上才算删根：按 find 的求值规则看它所在的「与」链，链里在它之前有收窄的测试就不算（`-o` 断开链；取反的、对谁都成立的测试不算收窄）——`find . -name '*.pyc' -delete`、`-prune -o -name '*.js' -delete` 这类清理被无条件拦下就没有别的写法可走。

**M5 之前的一个现实：** 跑过命令之后轨迹即不可信，高风险调用按 `untrusted_high_risk` 处置、不再问宿主策略。所以宿主要「永不允许」的事不能只写进处置表——CLI 在 `PRE_TOOL_USE` 上用只含 git 写命令与删工作区根的判定器无条件拦下，交回一段标成可信的说明，并拒收 `untrusted_high_risk=allow`。

#### 背景化

长耗时工具移入后台执行，不阻塞主循环，结果在后续轮次注入。三件套：移入后台、查询状态、取消。对应 §5.4 已有的两个切点。

#### 内核内置的工具

同样受"kernel 零重依赖"约束筛选 —— 下列工具全部经 Portal 执行，不引入额外依赖：

| 组 | 工具 |
| --- | --- |
| 文件 | `Read` `Write` `Edit` `Glob` `Grep` |
| 执行 | `Bash` `BashOutput` `KillBash` |
| 终端 | `Terminal`（PTY 会话，§6.5） |
| 编排 | `Delegate`（冷启动派活）`Fork`（继承上下文）`SendMessage` `QueryAgent` `KillAgent` |
| 任务跟踪 | `TodoWrite` `TaskGraph*` |
| 产物 | `Deliver`（显式提升为交付物，§7.4） |
| 人机交互 | `AskUser` `AskUserQuestion` |
| 背景任务 | `QueryBackgroundTool` `CancelBackgroundTool` |
| 长程 | `GetBudget`（感知剩余预算，§8.4）`Compact`（主动要求压缩，§8.8） |
| 扩展 | `Skill` |

**明确进 backend 的工具**：联网检索与抓取（需要外部服务）、MCP 客户端（需要 MCP SDK）、产物预览与语义 diff（需要格式库）。

`Delegate` 与 `Fork` 的边界：`Delegate` 冷启动、不继承上下文，用于需要独立视角的工作；`Fork` 继承上下文，用于已有结论有价值的延续。已有 Agent 能继续同一工作时用 `SendMessage`，不要新建。**能直接做完的有界工作不该委派。**

### 5.7 LLM 抽象

```python
@dataclass(frozen=True)
class LLMConfig:
    provider: str          # backend 注册的 provider 标识；内核不认识具体厂商
    model: str
    client_args: dict      # 连接参数
    request_args: dict     # 默认请求参数
    timeout: float
    context_window: int | None
```

三条设计决策：

- **请求参数归实例。** config 提供默认值，调用方只传单次覆盖。否则每个调用点都要重复一堆参数，改一次要改十处。
- **frozen，派生走专门方法。** 改动影响身份的参数（provider / model）必须经 `derive()`，否则缓存键与计费归属会静默错位。
- **内核不认识具体 provider。** `provider` 只是字符串标识，实现由 backend 注册。内核只做分发，不 import 任何厂商 SDK —— §3.2 规则 5 的直接要求。

#### 重试与故障转移归 Module

§5.1 提到"重试与故障转移在 `llm_failure` 切点实现"，此处明确归属：**它是一个 Runtime Module，不进内核。**

依据是 §5.5 的规则 —— 它带 flag（重试次数、退避策略、备选模型链），因此必须建模为 Module。实际好处是不同场景对重试的要求差异很大（批处理倾向激进重试，交互式倾向快速失败），做成 Module 意味着可以整套替换而不动内核。

内核只提供切点。

#### 多平台轮换是 LLM 包装器（2026-09-29 补充，决策 #89）

上一条管的是某次调用**失败之后**怎么办。同时持有多个平台的 key（例如 Gemini 与 OpenRouter 的免费档）时，还有两件事切点做不到：

- **调用之前跳过一个正在退避的平台。** `llm_failure` 只在某个平台已经失败之后才触发。
- **让同一个 Runtime 里的 Agent 共享平台健康状态。** 插件按 Agent 各装一份，「这个平台今天的额度用完了」每个 Agent 都得自己先撞一次。

因此轮换做成一个 LLM 包装器（`backends/llm_router` 的 `ModelRouter`），放在 backend，由宿主装配时组装，内核不认识它：

- 按声明顺序逐个试，失败的平台单独指数退避；平台给出的重试时间（`Retry-After`、OpenRouter 的 `X-RateLimit-Reset`、Google 的 `RetryInfo`）更晚时以平台为准，日额度耗尽的平台因此一直跳过到额度重置。只有一个平台时，它就是按退避重试同一个平台。
- 所有平台都在退避时，等最早恢复的那个，一次调用的总等待有上限。等也不会好的失败（认证失败、请求不合法、模型不存在）照样换平台、照样退避，但不为它等——否则 key 写错要干等满上限才报错。
- 换平台也修不好的错误（配置错误、调用方违约、超窗）不轮换，原样上抛。
- 每次平台故障留一条 WARNING：兜底平台答出来时，它是主力平台一直在失败的唯一证据。

它不违反 #62：它没有 Runtime flag，退避参数是构造参数、平台顺序由宿主决定；`RetryModule` 仍可叠加在切点上，处理轮换整体失败之后的事。

同一段历史可以前一轮由 A 平台答、后一轮由 B 平台答。端点私有的状态（Gemini 3 的 `thought_signature`）只回送给产出它的端点（§5.7 `LLM.create` 契约里 `extra` 的唯一例外）；别家平台产出的函数调用，由 backend 挂上宿主配置的占位值。

#### 流式

包装器形态，按 flag 开关。流式的复杂性（分片聚合、中断时的部分结果处理）不应污染非流式路径。中断时部分结果的取舍规则见 §5.1 第 9 步。

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

但"默认共享"带来一个必须处理的后果：**两个 Agent 同时写同一个产物时，若不做任何保护就是后写覆盖先写，且静默丢数据。** git worktree 只在隔离模式下提供保护，共享模式下没有任何机制拦住它。

因此共享模式下对产物写入施加**乐观并发控制**：

- 写入类工具在执行前记下目标产物的当前版本
- 写入完成时（`post_tool_use`）比对版本；若期间已被他人改动，**返回冲突而不是覆盖**
- 冲突结果交回模型，由它决定重读合并还是改写别处

选乐观锁而非悲观锁，是因为 Agent 的持有时间不可预测（一次工具调用可能跑几分钟），悲观锁会带来死锁与长时间阻塞。**丢数据是静默的、灾难性的；冲突是显式的、可恢复的。**

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
class ArtifactReader(Protocol):
    async def read(self, ref: ArtifactRef) -> str: ...   # 按 ref.version 读；版本不在了就抛

class ArtifactHandler(Protocol):
    handles: tuple[tuple[str, str], ...]                 # 声明的 (kind, mime)，mime 可为 '*'
    async def preview(self, ref: ArtifactRef, reader: ArtifactReader) -> Preview: ...
    async def semantic_diff(self, a: ArtifactRef, b: ArtifactRef, reader: ArtifactReader) -> Diff: ...
    async def validate(self, ref: ArtifactRef, reader: ArtifactReader) -> list[Violation]: ...
```

处理器方法都是 `async` 且收一个 `ArtifactReader`：只有 ref 拿不到字节，而每个隔离副本各有一份 Portal（M7 计划评审 H5）。
L1 按路径扩展名派生的 `(kind, mime)` 选处理器，`Deliver` 声明的 kind 只是标签——让 Agent 自选的 kind 决定交给
哪个处理器，就能把交付物绕过严格的那个（M7 收口评审 H3）。

`version` 是**不透明字符串**，含义由生效的隔离策略决定：git 策略用提交号，快照策略用单调递增序号。内核只做相等比较与排序，不解析其结构 —— 否则关闭 git 时（§6.3 允许）产物版本就无处安放。

`kind` 是**内核定义少量基础类别 + 处理器可注册新类别**，与切点的扩展方式保持一致。

**处理器冲突必须有确定的解决规则**，否则装了两个都声明处理表格的后端就会出现行为不可预测：注册按 `(kind, mime)` 建索引，更具体的匹配优先于更宽泛的；同一精度上重复注册**直接报错而不是静默取其一**。装了冲突的后端应该在启动时就失败，而不是在跑到一半时表现出取决于导入顺序的行为。

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

**归属：内核只持有类型与协议，两个伴生 Agent 的实现是 Runtime Module。** 这是 §5.5 那条规则的直接推论——L2/L3 由 Flag 控制，而"带开关的能力一律建模为 Runtime Module"。这样做还有两个实际好处：验收策略是本项目最需要迭代的部分（提示词、裁决口径、重试策略都会反复调整），做成 Module 意味着换一套验收范式不用动内核；同时不装该 Module 的用户不必为验收付任何代码体积与启动开销。

内核侧只保留：`Rubric` / `Anchor` / `Verdict` 类型、`ArtifactStore`、以及验收相关的切点。

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

#### Goal 是权威，Rubric 是它的可检验投影

先厘清两个概念的关系，否则会出现两套互相不一致的"用户到底要什么"：

```
Goal（唯一权威，仅在人明确改变要求时变更）
  intent:   自然语言陈述，允许包含不可检验的要求
  anchors:  指向用户原话 / 任务说明的引用链

Rubric（从 Goal 投影得到，可增补）
  每条带锚点，且锚点必须能追回 Goal 中的某处
  分两类：
    checkable  —— 可自动或由 LLM 检验
    judgment   —— 只能由人判断，验收时输出为「需人工确认」
```

**为什么不把两者合成一个。** 锚点规则的目的是防止验收方发明需求，因此它宁可丢弃无法锚定的要求。若 Goal 就是 Rubric，那么"这份报告要让不懂技术的人看得懂"这类**全局气质要求会被系统性丢弃** —— 它写不成可检验条目，于是 Agent 根本不知道有这个要求，最终产物满是术语，而验收还会判通过（所有 Rubric 都满足了）。这恰是知识工作中最常见的一类要求。

`judgment` 类 Rubric 解决它：Agent 在上下文中能看到并努力遵守；验收时它不被判为通过，而是明确输出"需人工确认"。这与 §10 中"验收器失败要放行但显式标记"是同一套哲学 —— **不确定的东西必须显式，不得塌缩成通过。**

同时，把 Goal 设为唯一权威消除了一致性问题：Rubric 是投影，Goal 变更即重新投影，不存在"谁与谁对齐"。

#### 锚点规则

验收标准由一个伴生 Agent 在**工作开始之前**从 Goal 与任务输入中推导并持久化。它只能看到"被要求了什么"，看不到"产出了什么"——否则会退化为事后为结果编造标准。

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

## 8. 长程任务（Long-Horizon）

长程任务是本项目要主动探索的方向，因此它必须是**一等设计**，而不是"多跑几轮"的副产品。

### 8.1 长程任务的失败模式与短任务不同

短任务的失败基本只有一种：做错了。长程任务（数小时到数天）的失败是另外六种：

| 失败模式 | 表现 |
| --- | --- |
| **目标漂移** | 几百轮之后在做的事已经和最初的要求没关系了 |
| **进度不可见** | 不知道离完成还有多远，也不知道是不是在原地打转 |
| **不知何时停** | 既可能过早收工，也可能无限循环 |
| **上下文腐烂** | 压缩过若干次后，关键约束被丢掉了 |
| **成本失控** | 没有预算感知，一个循环能烧掉巨额费用 |
| **无法自我纠偏** | 陷入局部循环，反复尝试同一条走不通的路 |

这六种都不是"模型不够聪明"，而是**缺少机制**。下面逐个给机制。

### 8.2 目标持久化与漂移检测

Goal **独立于对话存储**，不随上下文压缩丢失。它与 Rubric 的关系见 §7.6：Goal 是唯一权威，Rubric 是它的可检验投影。按 §5.2 纪律第 2 条，Goal 注入历史而非 System Prompt —— 它随任务而非随轮次变化。

**漂移检测分两层，常态零成本：**

| 层 | 做什么 | 成本 |
| --- | --- | --- |
| 锚点检查 | 当前正在做的事（最近若干轮的工具调用与产物变更）能否锚回某一条 Rubric | 零 LLM 调用 |
| 语义比对 | 仅在锚点检查失败时触发：把当前行为与 Goal 的 intent 做语义比较 | 一次 LLM 调用 |

第一层与 §7.6 的锚点规则同构 —— 把"这算不算偏离"这种无法收敛的判断，换成"能不能给出一条引用链"这种二值判断。

两层都需要的原因：锚点检查便宜但**抓不到"锚得上却方式错了"**（Agent 确实在做 R3 这件事，但用了完全错误的路径）；语义比对能抓到，但每轮都跑成本不可忽略。因此常态跑第一层，可疑时才升级到第二层。

漂移确认后触发纠偏，由 §8.6 的策略决定怎么纠。

### 8.3 进度用验收标准度量，不靠 Agent 自述

**关键设计：进度指标复用已有的验收标准。**

`已满足的强制标准数 / 强制标准总数` 就是进度，它有三个性质是 Agent 自述做不到的：

- **可验证** —— 不是模型说"我完成了 60%"（模型会自我欺骗，且这个数字与任何客观事实无关）
- **可比较** —— 跨轮次、跨 Agent、跨任务口径一致
- **非单调本身就是警报** —— 进度回退意味着 Agent 破坏了已经做完的东西，这是最需要立刻干预的情况，而自述指标永远不会回退

这是把 §7 的验收机制复用为进度度量，不额外增加成本。

### 8.4 预算是一等资源，且 Agent 可感知

```python
@dataclass(frozen=True)
class Budget:
    tokens: int | None = None
    cost: Decimal | None = None
    wall_clock_s: float | None = None
    turns: int | None = None
```

- **父子预算树** —— 子 Agent 从父 Agent 的预算中切分，不得超额。这样"多 Agent 并行 + 双伴生验收"三重放大不会失控
  - 宿主给 `Runtime(budget=BudgetPolicy(root=Budget(...), on_exhausted=..., prices=..., ask_human=...))`；`spawn(budget=)` 显式切，不给就切父此刻能给的一半（轮次向上取整），顶层 Agent 直接从 Runtime 的池里取
  - 切分即**预留**：活着的子 Agent 各自占着的额度之和永远不超过父；子 Agent 一个周期结束时缩到实际占用、余量回父，下个周期再从父那里至多切回一半——做完的子 Agent 以 zombie 留着随时可唤醒，不能一直按住父的额度。唤醒记在被唤醒者这一支血统的账上，一点都切不出来就拒收消息
  - 伴生调用的 token 与成本记在被服务的 Agent 上，不占它的轮次；每一次模型调用（主轮、重试、轮换、辅助调用、伴生）都在发出之前按估算准入、回来之后按实报记账，越界只剩估算误差
  - 耗尽：一次调用都放不下就停在 `budget`；这一次放得下、下一次放不下，这一轮就是收尾轮（工具定义照发，这一轮的工具调用不执行，打回不生效）
- **剩余预算注入历史**，Agent 可感知（不进 System Prompt，遵守前缀缓存纪律）：第一轮一块，剩余跨过 50% / 25% / 10% 时各一块，只写事实；`GetBudget` 交回三档明细。预算块不进回放指纹：父的剩余取决于并发的子 Agent 此刻占着多少，回放不复现跨 Agent 的先后
- **耗尽行为可配**：`hard_stop` / `graceful_wrapup` / `ask_human`
- **默认 `graceful_wrapup`** —— 预算耗尽时让 Agent 收尾并交付已完成的部分

最后一条是刻意的：一个跑了十小时的任务被硬杀掉、产物全部丢失，是所有结局里最坏的一个。

**这里的设计立场要明确：预算机制的目的不是给长跑设一个天花板，而是让长跑成为可控可研究的对象。** Agent 知道自己还剩多少资源，就可以自己决定怎么分配——"还剩两成预算，先保住核心交付物，放弃锦上添花的部分"。**预算分配策略本身就是长程任务的研究课题之一**，所以它必须是 Agent 能读到并据此决策的信息，而不是一个它看不见的外部开关。

#### 成本计量：价格表归 backend，且必须分三档

把 token 换算成钱需要价格表，而价格是**外部事实**：各 provider 定价不同、会变、且分档。因此：

- **内核不内置任何价格表。** 内置就会过时，并且把价格更新绑死在发版节奏上。价格由 provider backend 提供默认值，用户可覆盖。这与"内核不认识具体 provider"（§5.7）一致。
- **计量必须区分输入 / 输出 / 缓存命中三档。** 这不是精度问题而是正确性问题：缓存命中的输入 token 通常便宜一个数量级，而 §5.2 整节都在优化缓存命中率——**若计费不分档，那五条纪律的收益无法度量，前缀漂移检测报出的"浪费了多少 token"也换算不成钱。** 这两处机制必须对齐，否则优化前缀缓存这件事就失去了反馈信号。

### 8.5 停止判据：三条正交条件

| 判据 | 触发条件 | 语义 |
| --- | --- | --- |
| **完成** | 所有强制验收标准满足且裁决为 pass | 正常终止 |
| **资源** | 预算耗尽 | 按耗尽策略处置 |
| **停滞** | 连续 N 轮进度无变化 | 卡死，触发纠偏而非终止 |

三者正交，缺一不可：只看"完成"会无限循环；只看"资源"会在还有钱时一直空转；只看"停滞"会把慢但有进展的任务误杀。

### 8.6 长跑策略可插拔

不同的长跑循环范式——简单重启循环、Actor-Critic 双角色、Monitor-Actor-Critic 三角色、分级质量门——**作为策略实现，不写死在内核**。

```python
class LongHorizonStrategy(Protocol):
    async def on_progress_stall(self, ctx: LongHorizonContext) -> StallAction: ...
    async def on_budget_low(self, ctx: LongHorizonContext) -> BudgetAction: ...
    async def on_goal_drift(self, ctx: LongHorizonContext) -> DriftAction: ...
    async def should_stop(self, ctx: LongHorizonContext) -> StopDecision: ...
```

内核提供协议与一个基线实现，其他范式作为 Runtime Module 提供。

**这一条是本节里最重要的。** 既然长程任务是我们要探索的方向，那么"换一种循环范式"必须是写一个 Module 而不是改内核——否则每一个新想法都要动骨架，实验成本高到没人愿意做实验。**目标是让 syndica 成为长程任务的研究平台，而不是某一种长跑循环的实现。**

### 8.7 分级质量门

§7.5 的三层验收接入长跑循环，作为分级质量门：L1 每次产物变更即跑（免费），L2 在阶段收尾时跑，L3 在里程碑处跑。频率与成本成反比，与重要性成正比。

### 8.8 上下文压缩

长跑数百轮必然撑爆上下文窗口。但压缩是**有损且不可逆**的操作，可能丢掉关键约束、丢掉目标、破坏前缀缓存，因此需要明确的策略与不变量。

#### 策略可插拔，四种基线

| 策略 | 做法 | 成本 |
| --- | --- | --- |
| 规则型 | 保留最近 N 轮 + 保证工具调用配对 + 截断超长输出 | 零 LLM |
| 摘要型 | 用模型总结早期对话 | 高 |
| 剪枝型 | 按相关性丢弃（如已完成任务的执行细节） | 低 |
| **目标导向型** | 以 Goal 为锚，优先保留与**未满足 Rubric** 相关的内容 | 中 |

第四种是本项目独有的能力。通常的压缩逻辑是"保留最近的"，但**"最近"与"还需要"是两件不同的事** —— 五十轮前定下的一条约束可能仍然关键，而刚才三轮的调试输出完全可以丢弃。

而"还需要什么"恰好由**未满足的 Rubric** 定义。这正面对付"上下文腐烂"：腐烂的本质就是丢掉了仍然需要的东西。其他框架只能用"最近"近似，因为它们没有 Goal 与 Rubric 这层信息。

#### 四条不得破坏的不变量

1. **工具调用与结果必须配对** —— 配对断裂会使下一轮请求直接非法
2. **System Prompt 不参与压缩** —— 前缀缓存纪律
3. **Goal 与未满足的 Rubric 必须存活** —— 否则压缩之后目标消失，这是最严重的一类腐烂
4. **已注入的环境事实必须存活或重新注入** —— 这类事实是幂等注入的，被压缩掉需要补回
5. **不可信内容标记必须随衍生内容存活**（§12.2 第 2 条）—— 压缩会把不可信内容重写成看起来像结论的文字，标记一丢，此后基于它的高风险操作就绕过了 §12.2 的升级处置。这是压缩与安全的交界处，最容易在实现时漏掉

#### 触发时机：三条路径

- **占用率阈值提前触发**（主路径，阈值为 flag）—— 等到 LLM 报超限时，那一轮已经失败了
- **超限兜底** —— 阈值判断失准时的最后防线
- **Agent 主动要求** —— Agent 预见到接下来要做吃上下文的操作时可以先行整理

#### 三个容易漏掉的点

**压缩前必须留快照。** 压缩不可逆，出问题必须能回看压缩前的状态。这同时构成 §11.3 可回放能力的一部分。

**摘要器自身也需要预算。** 用 LLM 压缩时，摘要器面对的恰好是最长的那份上下文 —— 它自己就可能超限。因此摘要前可能需要先执行一次廉价的规则型预压缩。这个套娃问题不处理，压缩会恰好在最需要它的时候失败。

**压缩必然使前缀缓存失效**（它改动了历史），这不可避免。但要做两件事：压缩后**重建缓存键**，使失效是一次性且明确的，而不是此后每轮持续漂移；**压缩点记入 trace**（§11），否则"这一轮为什么突然变贵"会被误判为漂移缺陷。

### 8.9 分层记忆

压缩会丢弃细节，但有些经验应当跨会话保留：踩过的坑、项目约定、用户偏好。

#### 两阶段、两层次

**两阶段**：短期记录"最近发生了什么" → 累积到阈值后整理为结构化长期记忆。

**两层次**：全局记忆（跨所有工作）与工作区级记忆（特定项目）。不同角色默认可见不同层次 —— 工人可见自己工作区的记忆，不可见其他工作区的。

#### 写入权限必须收紧

**只有根协调者可以更新全局记忆；工人只读，写入限于自己工作区那一层。**

若每个 Agent 都能写全局记忆，数十个工人执行数百轮之后，全局记忆会被局部结论、临时状态与互相矛盾的观察污染。而**记忆一旦污染就会持续影响所有后续任务 —— 那比没有记忆更糟**。

并发写入同一份记忆的问题与 §6.4 的产物写入冲突同构，采用同一套乐观并发控制。

#### 注入方式受前缀缓存约束

记忆**进历史不进 System Prompt**（纪律 3：只 append）。且必须**缓存同一份记忆块，仅在压缩 / 新会话 / 过期时重载**。

这一条容易做错：直觉上"每轮拉取最新记忆"更正确，但那会使前缀逐轮变化，整个会话的缓存全部失效。记忆的新鲜度换不回这个代价。

---

## 9. Surfaces

### 9.1 CLI

统一入口，一张路由表加**全量惰性导入** —— `syndica version` 不导入任何产品包。

#### 命令树

采用 **名词 + 动词** 结构：`syndica agents --help` 即可列出该域的全部操作，且新增能力不污染顶层命名空间。

```
核心（里程碑 1-2）
  syndica run <prompt>                  跑一个任务
          --agent <name>                指定角色
          --topology <name>             指定拓扑
          --session <id>                向已有会话追加消息
          --max-turns / --max-cost / --max-tokens / --max-wall-clock
          --flag <key>=<value>          可重复，feature flag
  syndica chat                          交互式对话（见下）
  syndica agents    list | show <name>
  syndica topology  list | show <name>
  syndica sessions  list | show <id> | resume <id> | rm <id>
  syndica flags     list | show <key>
  syndica doctor                        环境自检
  syndica version

里程碑 3 加   syndica trace  list | show <id> | replay <id>
里程碑 9 加   syndica up | down | status
里程碑 11 加  syndica batch <dataset>
              syndica export <format>
```

**默认子命令是 `run`**：`syndica "写一份季度报告"` 等价于 `syndica run "写一份季度报告"`。属于 server 的参数要求显式写 `syndica up --port 9010` —— **只保留一套隐式规则**，不做"以 `-` 开头就转给某个子命令"这类第二重猜测。

#### 预算与 flag 的传参形态刻意不同

- **预算用独立参数**（`--max-turns` 等）：维度固定且已知（§8.4 四个），因此类型校验与 shell 补全免费获得。
- **feature flag 用可重复的 `--flag key=value`**：它是开放集合（§5.5），只能这么传。

**区别的依据是"集合是否封闭"**，不是形式美观。

`syndica flags list` 从 Flag 声明生成，因此它同时是 §5.5"一份声明供代码 / 界面 / 文档三方消费"的**可执行验证** —— 该命令的输出与文档站不一致即说明机制已坏。

#### 交互模式的两条约束

**一、`chat` 是 `run --session` 的外壳，不是第二套实现。** 两者必须走同一条投递路径，否则行为会分叉——而分叉出来的差异极难排查（"为什么交互里能用、脚本里不行"）。

**二、多 Agent 下必须能选择对话目标。** 单 Agent 系统里"你在跟谁说话"不是问题，多 Agent 系统里是：跟根协调者说话意味着派活，跟某个工人说话意味着干预它当前的工作。`chat` 需要显式的目标切换，且当前目标必须始终可见。

#### 一条由此推出的内核要求

CLI 的交互模式不经过 HTTP，因此**内核事件流必须首先是进程内可订阅的，HTTP SSE（§9.2）只是它的一个出口**。若事件流只存在于 HTTP 层，CLI 就只能自己再实现一套，那就回到了上面第一条禁止的分叉。

### 9.2 实时传输

- **SSE 为主通道**（会话级流 + 全局流）。Agent 事件流本质是单向推送；SSE 自带重连与断点续传、穿代理无障碍、浏览器原生支持。
- **WebSocket 用于两处**：类型化 RPC，以及伪终端（真双向、低延迟）。

### 9.3 互操作协议

四个全部支持：

| 协议 | 作用 | 优先级 |
| --- | --- | --- |
| **MCP** | 工具与资源接入 | 必需，不支持等于自绝于工具生态 |
| **ACP** | 客户端驱动 Agent。**双向红利**：我们的 Agent 能被第三方客户端驱动，我们的界面也能驱动第三方 Agent CLI | 高 |
| **A2A** | 与其他框架的 Agent 互通 | 中 |
| **OpenAI 兼容 HTTP** | 让现有客户端直接请求我们 | 中 |

### 9.4 Web UI

**React + TypeScript，构建产物随 Python 包发布。**

理由：Agent 界面是重实时流、重虚拟列表的场景，需要类型系统与成熟组件生态；更重要的是贡献者门槛——无框架无构建的原生方案在内部项目里有"改一行刷新即见"的优势，但对开源项目会显著流失现代前端贡献者。

**开发者需要 Node，用户不需要。** 构建产物随包发布，`pip install` 即可运行。

### 9.5 Runner 与训练出口

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

**但正因如此，"未完成验收"必须是导出时的一等状态，不能塌缩成"通过"。** §10 规定验收器自身失败时放行（fail-open）并显式标记；如果导出器把这些样本按正样本处理，就会把"我们没能验证"当成"验证通过"写进训练数据——**这是最隐蔽的一类数据污染，且会直接损害本项目最核心的主张**。

因此裁决状态在轨迹里是四值而非三值：`pass` / `partial` / `block` / `unverified`。**导出器默认排除 `unverified` 样本**，需要包含时必须显式开启并在数据集元信息中标注。

---

## 10. 错误处理

总原则：**区分哪些可降级、哪些必须失败，并让降级路径显式可见。**

| 场景 | 处理 | 理由 |
| --- | --- | --- |
| 工具致命错误 | 抛出，交给 Agent 循环 | Agent 需要知道这条路不通 |
| 工具非致命错误 | 进入工具响应给模型看 | 模型可自行换方式重试 |
| 消息投递失败 | 返回结构化结果，**不抛异常** | 调用方需区分可重试的瞬时错误 |
| 会话数据损坏 | **加载期尽量修复，不抛错**，损坏的子项跳过 | 一个子 Agent 的脏数据不应让整个会话打不开 |
| 调试数据写入失败 | 只丢调试数据 | 可观测性不得拖垮主流程 |
| Module 启停钩子失败 | **响亮失败**：服务工厂或 `on_start` 抛出时整个 Runtime 构造失败并回滚内核持有的状态；`on_shutdown` 抛出时其余 Module 照常关闭，最后汇成一个异常组上抛 | 启用了的 Module 是宿主要的能力，一个守卫类 Module 启动失败后悄悄缺席等于放行；「可选」由宿主在启用处声明，不由作者声明 |
| 批处理单任务崩溃 | 转为失败结果并记录堆栈 | **绝不让一个任务拖垮整批** |
| 产物处理器失败 | 降级为二进制 diff / 无预览，**不阻塞** | 产物本身没问题，只是我们看不懂它 |
| 验收器自身失败 | **放行（fail-open）**，但把"未完成验收"显式标记出来 | 验收器异常不应让任务永远无法完成；同时不得伪装成已验收 |

批处理还有一条实现要求：**资源获取全部置于 try 内**，保证任何一步失败都能走到清理逻辑释放沙箱。

---

## 11. 可观测性

多 Agent 长程系统的首要 debug 需求不是"哪一行报错"，而是**"这次任务里五个 Agent 各做了什么、谁在等谁、时间和钱花在哪、从哪一步开始跑偏"**。这要求一套统一模型，而不是散落的日志。

### 11.1 统一 trace / span 模型

```
trace                一次任务
└── span: agent      一个 Agent 的一段活跃期
    ├── span: turn   一轮
    │   ├── span: llm_call
    │   └── span: tool_call
    └── span: turn
```

跨 Agent 的因果关系必须显式记录，否则多 Agent 的时间线无法还原：

- **派生因果** —— 父 Agent 的哪个 span 创建了子 Agent
- **消息因果** —— 发送方 span → 接收方消费该消息的 span

有了这两条，"谁在等谁"就是可查询的事实，而不是靠读日志猜。

### 11.2 导出到既有生态，不自建后端

统一模型用 **OpenTelemetry** 语义导出。LLM 应用的可观测性已有成熟工具链，自建后端是纯负担且没人会用。内核只负责产出规范的 trace，接哪个后端由用户决定。

**里程碑 3 的落地：** backend `syndica.backends.otel_otlp`（httpx，extra `otel`）只从 `TraceStore` 读，按 OTLP/HTTP
JSON 发出；因果边转成 span links。端点只从用户自己的环境（`OTEL_EXPORTER_OTLP_ENDPOINT` 等 OpenTelemetry 约定的变量）
或命令行（`syndica trace export <id> --otlp <url>`）来，**项目配置不能设端点**：仓库可能来自不受信任的人，它选的端点
会收到 trace 的结构与用户的鉴权头。

### 11.3 生产运行可捕获为可回放形式（关键复用）

**运行时记录所有 LLM 请求/响应与工具输入/输出，其格式与 §13.1 的场景回放格式相同。**

这带来一个几乎免费的能力：**线上出问题的运行可以直接转成回归测试用例。** 长程任务的 bug 往往几小时才出现一次、且依赖特定的模型输出序列，靠手写测试复现基本不可能；而如果生产 trace 能直接回放，"修一个长跑 bug"就从"祈祷它再出现一次"变成"跑一遍那条 trace"。

因此捕获格式与回放格式**必须是同一个**。这是设计约束，不是可选优化——两者一旦分叉，这个能力就没了。

#### 保留策略：分级，全量请求显式开启

可回放需要完整的请求与响应正文，而那恰好是体量最大的部分；长跑数百轮乘以数十个 Agent，全量保留会失控。这两个要求直接冲突，解法是分级。里程碑 3 按用户裁决把默认定在「可回放」——回放只需要回复一侧（每轮几 KB），完整请求每轮都带着整段上下文、体量随轮数平方增长：

| 级别（`--trace` / `trace.level`） | span 与边 | 回放文档（回复、工具结果、人的答复、宿主档案、危险判定） | 完整请求 |
| --- | --- | --- | --- |
| `off` | — | — | — |
| `structure` | 记 | — | — |
| `replay`（默认） | 记 | 记 | 只记逐单元指纹 |
| `full` | 记 | 记 | 记（逐叶脱敏后） |

与本文档其他地方一致的原则：**贵的能力按需启用，不默认收费。** 分级不等于采样——采样会恰好丢掉关键那一轮。trace 只在用户执行 `syndica trace prune` 时删除，不自动删（结构永久保留）。

**捕获与回放是同一个格式：** 捕获写下的回放文档就是场景格式 v2（§13.1），`syndica trace replay <id>` 离线跑它——外部世界（模型回复、工具结局、人的答复、宿主的动作）按记录给出，内核全真并逐项比对（请求的逐单元指纹、工具真正执行的参数、每次闸门判定、每个 Agent 怎么停下的）；复现以 0 退出、分叉以 1、文档用不了以 2。

### 11.4 成本与进度可观测

§8 引入的预算与进度是**一等可观测量**，不是内部实现细节：每个 Agent 的 token / 花费 / 已用时长、验收标准满足比例的时间序列，都必须能查询与可视化。否则长程任务对用户就是一个黑盒。

---

## 12. 安全与威胁模型

本项目设计的是一个**能执行任意命令、读写任意文件、访问网络、且长时间无人值守运行**的系统。这类系统必须先说清安全边界，否则用户会基于错误的假设使用它。

### 12.1 前提与威胁模型

**前提：Syndica 默认运行在用户自己的机器或用户自己的容器中，以用户自身的权限执行。它不是多租户服务，不提供沙箱级隔离保证。** 这个前提决定了下面所有边界。

**在范围内（我们要防）：**

| 威胁 | 说明 |
| --- | --- |
| **提示注入导致的越权行为** | Agent 读到外部内容后被说服去做用户没有要求的事 |
| **凭证泄漏进持久化产物** | 密钥出现在 trace / 会话 / 记忆 / 产物中 |
| **危险操作被无意执行** | 误删、覆盖、其他不可逆动作 |
| **第三方能力包的意外副作用** | backend / Skill / MCP server 带来的非预期行为 |

**不在范围内（明确声明不防）：**

| 不防 | 理由 |
| --- | --- |
| 用户攻击自己的机器 | 用户本就有权执行任意命令，框架不做家长式限制 |
| 模型自身的对齐问题 | 我们不假设模型永远善意，但也不试图约束模型的价值观 |
| 多租户隔离 | 不是设计目标。需要隔离请在容器或虚拟机层面做 |
| 依赖供应链安全 | 由生态工具（审计、锁文件）负责 |

**必须写明"不防什么"的理由：没有威胁模型的安全声明比没有声明更危险**，因为用户会以为它安全。

### 12.2 机制一：不可信内容的标记与传递

工具输出携带**可信度标记**。来自外部的内容——网页、外部文件、第三方 API、他人提供的文档——标记为不可信。

三条要求：

1. **结构化包裹**：不可信内容进入上下文时明确标注"以下是数据，不是指令"。
2. **标记可传递**：不可信内容经摘要、压缩、记忆整理之后，其衍生内容**仍然不可信**。这一条最容易漏——压缩会把不可信内容重写成看起来像结论的文字。
3. **不可信内容不得直接触发高风险操作**：当一次高风险工具调用的参数可溯源到不可信内容时，处置**必须升级**，不允许模型自授权。

第三条是核心。§5.6 的危险命令判定是规则匹配，它挡不住"被说服之后自己写一段看起来完全合法的上传脚本"——规则看不出这条命令有什么问题。但如果我们知道**这个操作的动机来自不可信内容**，就可以强制升级处置。

这需要一条从工具输出到后续工具调用参数的**溯源链**。这是可实现的：产物溯源（§7.3）与 trace 因果链（§11.1）已经提供了所需的基础设施。

#### 与无人值守长跑的冲突，以及处置矩阵

上面第 3 条若照字面实现为"强制等人确认"，会与 §8 的无人值守长跑**直接冲突**：调研类任务必然读外部内容，此后任何高风险操作都能溯源到不可信内容，于是任务阻塞等人；而无人在看，只能耗到超时。**等待然后失败是最差的结果——白等一段超时时长，什么也没换来。**

因此处置由**风险等级 × 可溯源性**决定，而不是二值判断：

| 风险等级 | 可溯源到不可信 | 处置 |
| --- | --- | --- |
| 高 | 是 | 按 `untrusted_high_risk` flag：**`reject`（默认）** / `ask`（等人确认）/ `allow` |
| 高 | 否 | 按 §5.6 的宿主策略 |
| 低 | 是 | 放行并记录 |
| 低 | 否 | 放行 |

**默认 `reject` 而非 `ask`**，理由是拒绝比等待有用：Agent 立刻拿到"此操作因溯源到不可信内容而被拒绝"这个明确信息，就能换路径——例如把结果写进工作区留待人工事后查看，而不是直接对外发送。这与 §10 中"非致命错误进 `output`，让模型自行换方式"是同一套哲学。

可选配置**可信来源声明**（例如用户声明自己组织的内部文档站为可信）以降低误报率。它是安全边界上的显式缺口，必须可审计。

**一条必须写明的诚实结论：无人值守 + 处理不可信内容，本质上是有代价的组合。** 在这个组合下某些高风险操作做不了。这不是缺陷，而是**已知且有理由的能力边界** —— 不明说才是缺陷，因为用户会以为两边都能完美满足。

#### Skill 正文默认不可信

Skill 是运行时按需加载的指令文本（§6.6），而第三方 Skill 是**比外部网页更直接的注入向量** —— 网页需要"说服"模型，而 Skill 天生就是"要被当作指令执行的东西"。

因此：**第三方 Skill 的正文默认标记为不可信，用户可逐个声明为可信。**

依据是"用户装了"不等于"用户读过每一行"。Skill 生态的价值在于可以直接用别人写好的东西，而这恰恰意味着用户通常没有逐行审查。默认可信会把整个 Skill 生态变成注入面。

### 12.3 机制二：脱敏发生在写入之前

**凭证脱敏必须在写入 trace / 会话 / 记忆之前完成，不是之后。** 写进去再删除是来不及的——磁盘上已经有了，备份里也已经有了。

- **脱敏在唯一收口执行**：trace 与会话的写入路径必须共用同一个脱敏器。否则每新增一个持久化点就漏一个。
- 已知模式（常见密钥格式、认证头、私钥块）默认脱敏；用户可追加模式。
- **脱敏有损且不可逆，这与可回放（§11.3）直接冲突。** 脱敏后的 trace 回放时缺少真实凭证，需要凭证的步骤会失败。**这是刻意的取舍：回放能力不值得用凭证泄漏风险来换。** 回放使用占位符，相关步骤失败是正确行为，不是缺陷。

**里程碑 3 的落地：统一 `Redactor`。** `syndica.kernel.redaction` 是唯一的收口：规则（内置规则族加用户在
`.syndica.yaml` 追加的 `custom.*`）有稳定 ID，命中处换成 `[REDACTED:<规则 ID>]`；脱敏幂等，结构化的数据逐叶脱敏
（键名像密钥的值另按键判）；写入面只收它的输出 `RedactedText`（只有这个模块造得出来）。一条 AST 守卫扫整个包的
写入点与外发点：落盘与发出的正文要么不带内容、要么是脱敏器的输出，例外逐个文件写明理由（Agent 自己的工具；发给
模型提供方的请求）。trace、交接文件、提交消息、OTLP 导出都经它；规则集（版本、内置规则 ID 的顺序、用户规则）的
摘要记进 trace 头，回放用记录下的规则集对自己的请求取同样的指纹。

**里程碑 3 之前的收口：CLI 的最小脱敏器。** 里程碑 2 带来框架最早的两条落盘路径——`run --handoff` 写的交接文件与宿主执行的提交——它们不能等到 M3 的统一 `Redactor`。`syndica/cli/redaction.py` 先做最小的一份：宿主环境里名字像密钥、值像凭证的变量按值替换（被截断的 key 也认得出），加私钥块、`Authorization` 头、URL 里的 `user:pass@` 与几种公开的令牌格式；每条规则有稳定 ID，替换成 `[REDACTED:<规则 ID>]`。交接文件、`--handoff` 下的 stdout 与 stderr、提交消息都经它；先脱敏再截断，截剩的半截密钥不一定还认得出。提交前另扫一遍将要提交的新增行与文件名，只用逐字值、格式规则与完整的私钥块，命中就不提交——片段匹配允许误报，拦提交不允许。一条 AST 守卫保证 `syndica/cli` 里写文件、子进程参数与 stdin 要么不带内容，要么是脱敏器的输出。M3 建统一 `Redactor` 时把它并进去，规则 ID 保持不变。

### 12.4 机制三：第三方能力包的边界

backend、Skill、MCP server 都是可执行的第三方代码。

- **加载必须显式**：不做自动发现并启用。安装了包不等于启用，需要在配置中点名。
- **能力声明**：第三方包声明它需要什么（网络、文件系统范围、子进程），加载时可供审查。
- **诚实说明限制**：Python 层面**无法真正沙箱化同进程的第三方代码**。能力声明是给人审查用的依据，不是强制隔离。真正需要隔离时，把该能力放进 backend 的独立执行环境（容器或远程）。

### 12.5 给实现的硬约束

**任何新增的持久化路径都必须经过脱敏收口。** 这条进 `AGENTS.md`，并值得配一个 CI 检查——新增写入点若未调用脱敏器则失败。

---

## 13. 测试策略

Agent 框架最难测的是**编排逻辑**——多 Agent 互发消息、状态机转换、工具调用顺序。

| 层 | 测什么 | 何时运行 |
| --- | --- | --- |
| 单元测试 | 纯函数、schema 生成、状态机转换、路径解析 | 每次推送 |
| L1 进程内小流程 | 单 Agent turn 循环的局部行为 | 每次推送 |
| **L2 场景回放** | **多 Agent 编排**、工具顺序、状态快照、事件流单调性 | 每次推送 |
| 契约测试 | 见下 | 每次推送 |
| **真 LLM 端到端 + 评判** | 效果正确性的最终权威 | 手动 / 合并前 / 定时 |

### 13.1 L2 场景回放

声明式场景描述 + **脚本化的假 LLM**，驱动**真实的** Runtime、Agent 与工具执行。断言最终输出、工具调用顺序、状态快照。

关键在于**只有 LLM 是假的，其余都是真的**。这样能覆盖"A 派活给 B、B 汇报、A 继续"这类编排逻辑，且快速、确定、零成本。

它同时是**唯一能测试失败路径的手段**：LLM 异常三级分类、空响应守卫、中断保留部分回复——这些分支无法用真实模型按需触发。

### 13.2 真 LLM 端到端

效果正确性的唯一权威，使用 LLM 评判配合验收标准打分。单独流水线，手动或定时触发。

**两层缺一不可**：仅有真 LLM 测试，则贡献者与 CI（来自 fork 的 PR 拿不到密钥）都无法运行测试，等于没有任何自动化能挡住回归。

### 13.3 Protocol 契约测试（对外承诺）

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

### 13.4 长跑测试

- **假时钟注入** —— 定时任务、超时、重试退避不能靠真等
- **确定性种子** —— 并发调度与随机退避在测试下必须可复现
- **故障注入** —— 可在指定切点注入 LLM 失败、工具超时、网络错误。长跑系统的降级路径必须被主动测过，否则线上首次触发即是事故

---

## 14. 工程规范

| 项 | 决定 |
| --- | --- |
| License | **MIT**，著作权人 Loren Zhang |
| Python | **3.11 起**（`TaskGroup` / `ExceptionGroup` 对多 Agent 的结构化取消直接有用） |
| 工具链 | **uv**（环境 + 依赖 + 发布）、**ruff**（lint + format）、**pyright**（对 Protocol 与泛型支持更好，而本项目大量使用 Protocol）、**pytest + pytest-asyncio** |
| 首个公开版本 | **v1.0**（意味着 API 稳定承诺；v0.x 与"已完成"矛盾） |
| 版本策略 | 语义化版本。**切点名、Store Protocol、Flag key 均计入公开 API** |
| 贡献者协议 | 发布前再定（DCO 或 CLA） |
| 文档 | **Flag 文档从声明自动生成**；必需内容见下 |
| 必需文件 | `LICENSE` `README` `CONTRIBUTING` `CODE_OF_CONDUCT` `SECURITY`、issue/PR 模板、`AGENTS.md` |

**`AGENTS.md` 不是发布前的收尾工作，而是里程碑 2（自举）的前置条件。** 自举意味着 AI 读代码、读文档、改代码——它读 `AGENTS.md` 来理解项目约束。因此**自举的质量直接由这份文档的质量决定，且它必须在自举开始之前就位**，否则第一轮自举就是盲改。

文档站必需内容：五分钟快速上手、核心概念（Agent / Runtime / Workspace / Artifact / 验收）、四个 Protocol 的契约、如何编写自己的 Agent / 工具 / 后端、四个互操作协议的接入方式、自动生成的 API 参考。

CI 检查项：

- lint、格式化、类型检查
- 单元测试 + L1 + L2 回放 + 契约测试（全部免费可跑，无需 API 密钥）
- **依赖方向校验**（§3.2 六条规则必须机器可验）
- 覆盖率门槛（增量覆盖率不低于 50% 作为起点）
- 多 Python 版本 × 多操作系统矩阵
- 真 LLM 端到端（单独流水线）

---

## 15. 里程碑

发布策略是"完整形态之前不公开发布"，因此以下里程碑均为**内部里程碑**。
每个里程碑的目标、TODO、实现逻辑、静态验收和 E2E 验收展开在
[`docs/plans/2026-09-23-v1-milestone-roadmap.md`](../plans/2026-09-23-v1-milestone-roadmap.md)。

| # | 里程碑 | 完成判据 |
| --- | --- | --- |
| 1 | 内核骨架 | Runtime 能创建多个 Agent、互发消息、各自跑通"LLM + 工具"循环、可终止与恢复。含**声明式 Agent 定义**（§4.6，否则加角色要写代码）、**拓扑装配**（§4.7，一有多个角色就需要）、**Tool 系统与 LLM 抽象**（§5.6 / §5.7），以及**最小本地执行能力**（文件读写 + 命令执行）——没有这些连一轮有意义的 turn 都跑不出来 |
| 2 | **自举** | **框架能修改自己的代码**：读写文件、执行命令、跑测试、读取结果并迭代。CLI 可用 |
| 3 | **可观测性 + 脱敏收口** | 统一 trace / span、跨 Agent 因果链、运行可捕获为可回放格式。**刻意排在早期**：它之后的每一个里程碑都依赖它来调试，晚做等于前面全部盲跑。**脱敏（§12.3）必须与它同期完成** —— 可观测性一上线就开始往磁盘写完整请求响应，脱敏晚一天就是一天的泄漏窗口 |
| 4 | 扩展点完备 | 三层装配 + Runtime Module + 命名空间切点及其触发接口 |
| 5 | 执行面完整化 + 不可信内容 | 把里程碑 1 的最小实现补成完整协议：Workspace / Portal 全量接口、git 版本层、worktree 与快照隔离、共享模式下的写入冲突检测、伪终端。同期做**不可信内容标记与溯源链**（§12.2）—— 它约束的正是工具输出，与执行面同时落地最省 |
| 6 | 状态 | 会话持久化与恢复、上下文压缩四种策略（§8.8）、分层记忆（§8.9）。**目标导向型压缩依赖里程碑 7 的 Rubric**，因此该策略可延后到 7 之后补 |
| 7 | 产物与验收 | 产物模型、处理器、验收标准与锚点、三态裁决、反馈升级为标准 |
| 8 | **长程任务** | 目标持久化与漂移检测、预算树与可感知预算、进度度量、三条停止判据、可插拔长跑策略。**依赖里程碑 7**——进度指标复用验收标准 |
| 9 | 产品面 | HTTP + SSE + React 界面 + 移动端适配；预算与进度可视化 |
| 10 | 互操作 | MCP → ACP → A2A → OpenAI 兼容 |
| 11 | 训练出口 | 中立轨迹 + SFT / 偏好 / RL 导出器 + 至少一个训练框架对接 |
| 12 | 发布准备 | 英文文档、文档站、CI 矩阵、许可与贡献流程 |

**状态（2026-10-06）：** 里程碑 1–7 已完成，审计见 [M1 收口审计](../audits/2026-09-23-m1-final-audit.md)、
[M2 收口审计](../audits/2026-10-01-m2-final-audit.md)、[M3 收口审计](../audits/2026-10-02-m3-final-audit.md)、
[M4 收口审计](../audits/2026-10-03-m4-final-audit.md)、[M5 收口审计](../audits/2026-10-04-m5-final-audit.md) 与
[M6 收口审计](../audits/2026-10-05-m6-final-audit.md) 与 [M7 收口审计](../audits/2026-10-05-m7-final-audit.md)。
M7 的完成判据（产物模型、处理器、验收标准与锚点、三态裁决、反馈升级为标准）由一套端到端用例与两次真实运行证明：
免费模型按 `--goal` 投影出一条带锚点的 Rubric，写出报告并 `Deliver`，L2 / L3 判 `pass`；同一任务把验收时限压到 1
秒，投影与验收都超时，run 照常完成而裁决是 `unverified`（`verifier_timeout`）。裁决是四值的（§7.5 的三态加上 §10
的 `unverified`），由内核从逐条结论推导；只能由人判断的 Rubric 让裁决停在 `unverified`（`needs_human`）。验收只给根
协调者：子 Agent 交回的东西由派它的一方验收。装了验收的 run 不写回放文档（伴生调用不在录制轮次里），伴生调用也还
不进 trace（随 M8 的预算树）。
M6 的完成判据（会话持久化与恢复、§8.8 上下文压缩、§8.9 分层记忆）由一套端到端用例与一次真实运行证明：免费模型在
启用了压缩与记忆的工作区里调用 `Remember`，记忆阈值触发一次 LLM 整理写出长期记忆、跨会话落盘，会话与 trace 照常
记下。M6 只持久化根对话（多 Agent 子树的持久化与恢复不在 M6）；压缩的四种策略交付了规则型、剪枝型、摘要型三种，
目标导向型按本表依赖 M7 的 Rubric 后补。
M5 的完成判据（Workspace / Portal 全量接口与契约套件、Python 写的 git 版本层、worktree 与快照隔离、共享模式下的
写入冲突检测、伪终端，以及不可信内容的标记与溯源链）由一套端到端用例与一次真实运行证明：免费模型在一个开了
版本层与文件污点的工作区里读写文件、跑命令，版本层记下每次改动、文件污点把命令改过的文件标成不可信，执行一个
不可信的本地脚本被强制升级拦下。M4 的完成判据（三层装配、Runtime Module、命名空间切点及其触发
接口）由一次真实运行与一套端到端用例证明：一个只用公开 API 写的第三方 Module 经入口点装上、受信任确认约束，免费
模型调用了它的工具，trace 记下它与它的切点，run 离线回放复现。§4.7 槽的 `plugins` 字段 v1 不做：第三方能力由
Runtime Module 提供，槽附加的是工具、预算与 metadata（M4 计划 D4）。
M3 的完成判据（统一 trace / span、跨 Agent 因果链、运行捕获为可回放格式、脱敏收口）由一次真实运行与一套端到端
用例证明，见 M3 收口审计。M2 的完成判据由一次真实运行证明：免费模型按本仓的
`syndica-dev` 拓扑在仓库副本里改了 CLI 的一处交接文字并补了两条用例，宿主复核全过、Reviewer 给出 pass，人选择
之后由宿主提交，这个提交原样进了本仓。

**里程碑 2 是分水岭，优先级最高。** 在它之前每一行代码都得人写；之后维护者的产出效率与框架自身的好用程度形成正反馈。对单人维护 + 追求大范围的项目，这是唯一有效的杠杆。

**本文是架构设计，不是实现计划。** 十二个里程碑各自需要一份独立的实现计划 —— 一份计划覆盖全部范围既写不完也读不动。计划按里程碑逐个产出，前一个完成后再写下一个，这样后面的计划能吸收前面的实际情况。

---

## 16. 风险

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| **范围过大而长期无可用版本** | v1.0 包含四个互操作协议、伪终端、双伴生验收、React 界面、RL 导出、四个 Protocol 及契约测试。单人维护下这是很长的周期，而在发布前没有外部反馈与贡献 | 里程碑 2（自举）尽早达成；每个里程碑内部可用 |
| **产物与验收无先例** | §7 是全新设计，若产物抽象设计失误，返工面很大 | 内核只认身份与版本，格式语义在后端；先用文本格式验证回路，再扩展二进制格式 |
| **前缀缓存纪律靠人守** | 违反不报错，只静默增加费用 | 漂移检测开发模式默认开启 |
| **四个协议的维护负担** | 每个协议都是一套契约、文档与兼容承诺 | 按 MCP → ACP → A2A → OpenAI 兼容 的顺序实现，前一个稳定后再开下一个 |
| **单点维护** | 个人项目，无团队冗余 | 自举 + 发布后引入贡献者；契约测试降低第三方贡献后端的门槛 |
| **验收使 token 成本翻倍** | 双伴生 Agent 方案的固有代价 | L1 零成本前置过滤；验收由 Flag 分别控制；§8.4 的预算树把总开销约束在显式上限内 |
| **长程任务机制无先例** | §8 的进度度量、停止判据、可插拔策略都是自己设计的，可能有根本性缺陷 | 进度指标复用已有的验收标准（可验证而非自述）；长跑范式做成可替换策略，换想法不动内核 |
| **捕获格式与回放格式分叉** | §11.3 那个"生产 trace 直接变回归用例"的能力，完全依赖两个格式是同一个。一旦为了各自方便而分叉，能力静默消失且很难再合回来 | 写成设计约束而非可选优化；CI 中用同一份 trace 同时驱动捕获与回放，格式一旦分叉即失败 |
| **`unverified` 被当成通过** | 验收 fail-open 与训练出口相接处的隐蔽污染，会直接损害本项目最核心的主张 | 裁决状态在轨迹中为四值；导出器默认排除，包含时必须显式开启并在数据集元信息标注 |
| **第三方代码无法同进程沙箱化** | backend / Skill / MCP server 都是可执行代码，Python 层面挡不住恶意实现。这是**接受而非解决**的风险 | 显式加载而非自动发现；能力声明供人审查；文档中如实说明，不做虚假的安全承诺；需要真隔离时引导用户放进独立执行环境 |
| **提示注入绕过溯源** | §12.2 依赖"高风险操作可溯源到不可信内容"，但溯源链可能被多轮改写稀释 | 标记随衍生内容传递（含压缩与记忆整理）；溯源失败时按最坏情况处理（视为不可信） |

---

## 17. 决策速查

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
| 34 | 长程任务 | 一等设计（§8），不是"多跑几轮"的副产品 |
| 35 | 预算 | 一等资源；父子预算树；Agent 可感知；耗尽默认 graceful wrap-up 而非硬杀 |
| 36 | 进度度量 | 复用验收标准的满足比例，**不采信 Agent 自述** |
| 37 | 停止判据 | 完成 / 资源 / 停滞，三条正交，缺一不可 |
| 38 | 长跑范式 | 可插拔策略协议 + Runtime Module，换范式不动内核 |
| 39 | 共享工作区写入 | 乐观并发控制，冲突显式返回而不覆盖 |
| 40 | Agent 租约 | owner + heartbeat；孤儿可回收或接管；单进程下零开销 |
| 41 | 注册表写入 | `update()` 仅限单一写者字段；多方可写集合须提供原子操作 |
| 42 | 扩展切点触发 | 由声明方触发，内核提供触发接口，命名空间校验 owner |
| 43 | 产物处理器冲突 | 按 `(kind, mime)` 精度优先；同精度重复注册直接报错 |
| 44 | 验收归属 | 内核只留类型与协议；两个伴生 Agent 的实现是 Runtime Module |
| 45 | 裁决状态 | 四值：pass / partial / block / **unverified**；导出器默认排除 unverified |
| 46 | 可观测性 | 统一 trace / span + 跨 Agent 因果链，导出 OpenTelemetry，不自建后端 |
| 47 | 捕获与回放 | **格式必须同一个** —— 生产 trace 可直接转为回归用例，这是设计约束 |
| 48 | 预算与进度 | 一等可观测量，必须可查询与可视化 |
| 49 | Agent 定义 | 声明式（清单 + 提示词）与类式并存；判定靠清单里有无指向类的字段 |
| 50 | 声明式清单 | 固定十个字段，属公开 API，宁少勿多 |
| 51 | Agent 描述与插值 | `description` 写"何时该用我"；插值变量**禁止 `{cwd}`**（前缀缓存纪律） |
| 52 | Agent 覆盖 | 用户定义覆盖内置；**被覆盖的定义必须可查询** |
| 53 | Topology | `AgentSlot`；三种基数（singleton / keyed / on-demand）；三种启动策略 |
| 54 | 拓扑边界 | **只声明结构，不声明行为**；严格禁止替换 Agent 基础配置（可 debug 性优先） |
| 55 | Goal 与 Rubric | Goal 为唯一权威，Rubric 是其可检验投影；Rubric 分 checkable / judgment 两类 |
| 56 | 漂移检测 | 两层：锚点检查（零成本，常态）→ 语义比对（仅可疑时） |
| 57 | Tool schema | 三级回退：手写 → 类型化模型 → 反射签名 |
| 58 | ToolResult | 三字段：`output`（给模型）/ `meta`（给代码）/ `artifacts`（产物引用） |
| 59 | 工具并发 | 安全判定从严：解析失败 / 工具不存在 / 需授权一律判不安全 |
| 60 | 危险操作 | **判定与处置分离**（规则只出结论）；**授权仅放行当次调用** |
| 61 | LLMConfig | frozen；请求参数归实例；provider 为字符串标识，内核不认厂商 |
| 62 | LLM 重试与故障转移 | Runtime Module，不进内核 |
| 63 | 上下文压缩 | 四种策略可插拔，含**目标导向型**（以未满足 Rubric 为保留依据） |
| 64 | 压缩不变量 | 工具调用配对 / SP 不参与 / Goal 与未满足 Rubric 存活 / 环境事实存活 |
| 65 | 压缩触发 | 阈值提前触发 + 超限兜底 + Agent 主动要求，三条路径 |
| 66 | 压缩与缓存 | 压缩后重建缓存键（一次性失效）；压缩点记入 trace |
| 67 | 记忆分层 | 全局 + 工作区两层；**只有根协调者可写全局** |
| 68 | 记忆注入 | 进历史不进 SP；缓存同一份块，仅压缩 / 新会话 / 过期时重载 |
| 69 | 成本计量 | 价格表归 provider backend，内核不内置；**必须区分输入 / 输出 / 缓存命中三档** |
| 70 | trace 保留 | 分级：span 结构永久留、失败轮次 payload 留、全量 payload 显式开启（不用采样）。**M3 起由 #111 取代**：默认「可回放」 |
| 71 | 等待结果 | `answered` / `timeout` / `preempted` / `aborted`；**超时必需**，否则长跑中会永久挂起 |
| 72 | Python 版本 | 3.11 起（`TaskGroup` / `ExceptionGroup` 对多 Agent 结构化取消直接有用） |
| 73 | 工具链 | uv + ruff + pyright + pytest / pytest-asyncio |
| 74 | 安全边界 | 明确威胁模型，**写清防什么与不防什么**；不提供多租户隔离保证 |
| 75 | 不可信内容 | 标记且**可传递**（衍生内容仍不可信）；溯源到不可信内容的高风险操作**强制人工确认** |
| 76 | 脱敏 | **写入之前**完成，单一收口；与可回放冲突时**选安全**，回放用占位符 |
| 77 | 第三方能力包 | 显式加载 + 能力声明；诚实说明同进程无法真正沙箱化 |
| 78 | 内置 Agent | 内核 4 个（Orchestrator / Planner / Explorer / Worker）；需外部服务的角色进 backend；验收三个 Agent 随验收 Module |
| 79 | 内置工具 | 全部经 Portal、零额外依赖；联网检索、MCP、产物格式相关进 backend |
| 80 | 错误消息 | 硬规范：**期望什么 / 实际收到什么 / 下一步做什么**，三件套缺一不可 |
| 81 | 不可信高风险操作 | 处置矩阵（风险等级 × 可溯源性）；无人值守下三档可配，**默认 `reject`**——拒绝比等待有用，Agent 能换路径 |
| 82 | 能力边界要明说 | 无人值守 + 不可信内容是有代价的组合，某些高风险操作做不了；**这是已知边界，必须写在文档里** |
| 83 | Skill 可信度 | 第三方 Skill 正文**默认不可信**，可逐个声明可信。"用户装了"不等于"用户读过每一行" |
| 84 | 压缩不变量 | 增加第五条：**不可信标记必须随衍生内容存活**（压缩与安全的交界处，最易漏） |
| 85 | CLI 形态 | 名词 + 动词；**默认子命令 `run`**；只保留一套隐式规则 |
| 86 | 传参形态 | 预算用独立参数（封闭集合，校验与补全免费）；feature flag 用可重复 `--flag k=v`（开放集合） |
| 87 | 交互模式 | `chat` 是 `run --session` 的外壳而非第二套实现；多 Agent 下必须能选对话目标且目标始终可见 |
| 88 | 事件流 | **首先是进程内可订阅的**，HTTP SSE 只是它的一个出口——否则 CLI 必须自己再实现一套 |
| 89 | 多平台模型轮换 | **LLM 包装器，放 backend**（不走切点：切点跳不过退避中的平台，也没法跨 Agent 共享健康状态）；每平台单独指数退避，平台给的重试时间更晚时以平台为准；与 #62 叠加 |

#90 起来自[里程碑 2 计划](../plans/2026-09-30-m2-self-hosting.md)的设计裁决（括号里是计划中的编号）。

| # | 决策 | 结论 |
| --- | --- | --- |
| 90 | 自举的顺序 | **护栏先行**：CI → 内核补口 → CLI 护栏 → 其余能力（D1） |
| 91 | 危险选项的注入 | 冻结目录与装配拓扑时注入判定与处置；`RuleTablePolicy` 按规则 ID 给处置，表外 HIGH 阻断（D2） |
| 92 | 可选危险规则 | git 写命令、越界路径、删工作区根三族进内核 `danger.py`，不进 `DEFAULT_RULES`；**找危险程序宁可多判，豁免程序词宁可少判**（D3） |
| 93 | 开发期处置 | 对所有 CLI run 生效；git 写命令与删根由钩子**无条件**拦下，不依赖处置表；拒收 `untrusted_high_risk=allow`（D4） |
| 94 | M2 的威胁模型 | 挡失手、朴素注入、经环境变量与交接文件的泄露；**挡不住被说服且会绕规则的 Agent**，真边界是沙箱化的 Workspace 后端（D5；M5 交 `Workspace` / `Portal` 协议与契约套件，沙箱后端另行落地，§6.2 / §12.4） |
| 95 | Agent 的命令环境 | 固定白名单加 `pass_env`；形似密钥的名字一律拒收；git 与 gh 换成每次 run 的空配置与占位令牌（D6） |
| 96 | 命令白名单 | 不做：对真实模型太脆，列不全就频繁被拦，列全了又失去意义（D7） |
| 97 | 只读 run | 只读工具**白名单**而不是去掉几个写工具，以后新增的可写工具默认进不来（D8） |
| 98 | 命令执行 | 进程组清理、超时交回部分输出、结构化 `meta`、按流截断、stdin 接 `/dev/null`、拒写 `.git/`（D9） |
| 99 | 交接与复核 | **宿主独立复核，不采信 Agent 的自述**；Reviewer 结论只取看过最终工作树、自己没改文件的那一次；交接文件先脱敏再落盘（D11、D13） |
| 100 | git 收尾 | **人选择，宿主执行**；只提交本次改动的路径，提交在 Agent 的环境里跑；推送只推这次提交、关掉本地钩子、从不 `--force`（D14） |
| 101 | 配置信任 | 项目配置视同用户自己写的代码；受保护路径有改动时不自动提交（D15） |
| 102 | M3 之前的脱敏 | CLI surface 先做最小脱敏器，M3 并进统一 `Redactor`，规则 ID 不变（D12） |
| 103 | 本仓的开发配置 | `syndica-dev` 是纯声明式的项目配置与提示词，不进内核；角色轮数与提示词按真实运行调（D17） |
| 104 | 转手执行的命令 | shell `-c`、`eval`、`xargs`、`find -exec` 交出的命令当命令再拆一遍，四族规则都看得见；超过 8 层按命中；`find` 只在不加收窄地删掉起点时算删根（M3 D16） |
| 105 | wrapper 之后的候选命令词 | 照旧每个词都算候选；只有选项全认得出的 wrapper 启动确定不执行实参的程序时，它的实参不算（M3 T1） |

#106 起来自[里程碑 3 计划](../plans/2026-10-01-m3-observability.md)的设计裁决（括号里是计划中的编号）。

| # | 决策 | 结论 |
| --- | --- | --- |
| 106 | 记录的方式 | **内核插桩，同步，不靠钩子拼**：钩子拼出来的 trace 恰好在被拦的调用、被取消的 Agent、并发的段上缺口最多（D2） |
| 107 | span 与 id | span、两类因果边（派生、消息）；span id 由稳定键确定地导出，回放按稳定键重发同一批 id（D3、D10） |
| 108 | 统一 `Redactor` | 幂等、逐叶、带类型闸；写入面只收 `RedactedText`；规则 ID 沿用 M2（D4） |
| 109 | 守卫的范围 | 写入点与外发点扫整个包，例外逐个文件写明理由（D5） |
| 110 | 第五个 Store | `TraceStore`，内存实现与契约套件随内核发布，文件实现给 CLI（D6、D7） |
| 111 | 默认记录级别 | **可回放**（用户裁决）：回复一侧记全，完整请求只记逐单元指纹；`full` 显式开启（D8） |
| 112 | 轨迹的写者 | **轨迹只由内核写**：并发窗口与可观察切点分发期间冻结，hook 往里追加报错（D9 修订） |
| 113 | 回放 | 外部世界按记录给出，内核全真并逐项比对；第一处分叉即停，`--allow-divergence` 时接着比（D11，用户裁决） |
| 114 | 场景格式 | v2 一份 schema，手写与捕获共用；捕获的文档自成一份（正文内联）（D12） |
| 115 | LLM 侧 | 记作答平台、token、平台故障；`ModelRouter` 转发前缀单位（D13） |
| 116 | 导出 | OTLP/HTTP JSON，httpx backend；端点只从用户环境或命令行来，项目配置不能设；导出的是记录器自己写的（D14） |
| 117 | CLI | `trace list / show / replay / export / prune`；`<id>` 收完整 id、唯一前缀或目录名；`trace replay` 自己记一个指向原 trace 的 trace（D15） |
| 118 | 宿主私有目录 | `.syndica/traces` 与 `.syndica/handoffs` 只归宿主读写，Agent 的文件工具碰不到（Bash 碰得到，真边界在 M5）（D17） |
