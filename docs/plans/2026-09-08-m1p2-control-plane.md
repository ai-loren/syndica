# 里程碑 1 · 计划 2：内核控制面与事件循环

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把计划 1 那个「跑一次就完」的 Agent 变成一个**可控的长驻进程**：事件循环等消息、按消息跑周期，能挂起 / 恢复 / 协作式中止 / 硬杀 / 级联销毁，能在轮中等待一个答复，能安全地并发执行一批工具，并且 Agent 自己就能派活给别的 Agent。

**Architecture:** 沿 spec §4 的进程模型继续搭：`AgentRegistry` 是状态的权威（CAS 转换），`LocalAgentTable` 持有运行时对象，本计划补上第三样只在进程内有意义的东西——**控制块**（挂起门、协作式中止位、延迟消息暂存、等待令牌）。`BaseAgent` 仍然只跑「LLM + 工具」循环，但轮与轮之间多了一道**门**（`TurnGate`），控制面通过这道门把「停下来」「先看这条消息」交给它。`Runtime` 的 `_drive` 从「调一次 `run`」变成 IDLE↔RUNNING 的周期循环，`on_missing_queue` 接上 `ensure_active` 之后，「一条消息复活 ZOMBIE」第一次真的成立。

**Tech Stack:** Python 3.11+、pydantic、pytest + pytest-asyncio + pytest-timeout、uv、ruff、pyright

## Global Constraints

以下逐条摘自 [spec](../design/2026-09-03-architecture.md) 与 [`AGENTS.md`](../../AGENTS.md)，**每个任务都隐含包含本节要求**。前八条与计划 1 相同，其余是计划 1 执行过程中新沉淀、其自身的约束节尚未包含的：

- Python **3.11** 起。
- **kernel 零重依赖**：只允许标准库 + `pydantic` + 一个 HTTP 客户端。厂商 SDK 一律进 backend。
- **kernel 不读环境变量**。路径、凭证、开关一律由宿主通过构造参数注入。
- **kernel 不依赖任何 surface 或 backend**；backend 之间互不依赖。
- **公开 API 全量类型标注**，随包发布 `py.typed`。
- **错误消息三件套**：期望什么 / 实际收到什么 / 下一步做什么。缺一不可。**裸 `KeyError` / `TypeError` 逃出公开路径视为缺陷**。
- **注释只写代码表达不了的硬约束**，不写变更历史、不写"我为什么没那么做"。
- 公开 API 的范围包括：函数签名、**切点名、Store Protocol、Flag key、声明式 Agent 清单字段**。
- **dataclass 的容器默认值用参数化泛型别名作工厂**（`field(default_factory=list[ToolCall])`），不用裸 `list` / `dict` —— 裸形式在 `src` 的 pyright strict 下会被判成 `list[Unknown]` 而报错。
- **状态转换只走 CAS**，绝不直接赋值，也绝不"先读一次再写"——读与写之间的窗口正是 CAS 存在的理由。本计划新增的每一处转换都必须能说出它的 `expected` 是什么。
- **等待类代码里电平信号与边沿信号不能混。** "队列非空 / 未挂起"是电平（状态），"有新东西到了 / 有人叫我"是边沿（事件）。用电平驱动"等下一个"的循环会退化成不让出控制权的空转，连 `asyncio.wait_for` 都击穿，表现为整套测试挂死。要等新事件就必须先 `clear()` 再 `wait()`。本计划有三处等待（挂起门、事件循环、`wait_for`），逐处说明用的是哪一种。
- **任何新交出去的引用都要问一句：这是快照还是活的？** 计划 1 里"快照 vs 活引用"被关掉过五次。本计划新增的接缝有五个：`pause` / `abort` 对外暴露什么、`Waiter` 携带什么、并发分段器交给每个工具什么、插件看得见什么、控制块的延迟队列交出什么。逐个给出答案并用"改完再读同一个对象"的断言钉住（**跨两次调用的隔离性断言可能由构造恒成立**）。
- **测试代码是下限，不是验收线。** 交付前必须做变异验证，且报告里要写"在这 N 个变异下零存活"而非"零存活"。做变异前先清 `__pycache__`（`.pyc` 按 (mtime 秒, 源文件字节数) 判失效，等长替换在同一秒内改完会复用旧字节码）；新增文件先 `git add`，用索引做还原基准。
- **本计划的测试代码本身不得出现下列十三种写法**，它们每一条都在计划 1 的执行中真实咬过人：
  1. **stdlib import 块被空行劈开**（`import asyncio` 与 `from typing import ...` 之间空一行）——ruff 的 isort 规则会红。计划 1 的**七个**任务出现过同一处。全部 stdlib 进同一个块。
  2. `pytest.raises(Exception, match='...')`——断言具体类型，消息文本必须保持可改。
  3. **函数内 import**（测试体、hook 体、实现体都算）。
  4. 裸 `field(default_factory=list)`。
  5. `await asyncio.sleep(0)` 当同步手段——改用确定性握手（让被等待方自己发出"我已就绪"的信号），且每一次等待都要有上界，让回归表现为失败而不是挂死。
  6. **基数为 1 的固定装置。** `n=1` 时反转、排序、去重、取首、取尾是同一个答案；`n=2` 仍挡不住排序变异（两元素下 `sorted` 与 `sorted(reverse=True)` 必有一个是恒等）。**基数至少 3，且顺序既非升序、亦非降序、也不等于反转结果。** 本计划有多处这样的集合：子 Agent 列表、并发分段、延迟消息队列、插件列表、hook 链、危险规则表、编排工具表。
  7. **期望值与被测对象同源。** 用补集表达的划分断言由构造恒成立；期望计数与实际计数取自同一处会一起缩水；用模块自己的 re-export 算出来的"确切内容"不独立。期望值必须来自与被测机制无关的第三方事实源。
  8. **枚举只断言取值集合。** 用整张 `{m.name: m.value for m in E}` 映射**并且**另配一条确切内容断言——两者严格互补，谁都替代不了谁。
  9. **定义了工具的测试文件漏 `from __future__ import annotations`。** `Tool` 的反射要求求值字符串标注，漏了这行等于在与生产相反的条件下验证。本计划所有新增测试文件一律写上这一行。
  10. **新接缝交出活引用**（见上一条 Global Constraint）。
  11. **用例名断言得比它的数据能证明的更多。**
  12. 裸 `KeyError` / `TypeError` 逃出该给三件套的地方。
  13. **紧边界的时间断言。** 必须对墙钟耗时下断言时，用宽裕的边界并写明这条断言凭什么有意义。
- **全局 60s pytest 超时已生效**（`pyproject.toml` 的 `timeout = 60`），挂死类回归会带栈变红而不是把 CI 跑到平台上限。本计划大量涉及等待与取消，这是它的主战场。
- **凡是会进 LLM 请求或缓存键的结构，要显式断言 `list(d)` 的顺序**，别只断言相等：`==` 对 dict 键序完全无感，而键序漂移会让前缀缓存整体失效且不报任何错。本计划的编排工具表直接进请求前缀。
- **一次工具调用恰好产出一个 `role='tool'` block**（计划 1 Task 12 的绝对不变式）。本计划的并发分段、危险阻断、`wait_for` 被抢占，每一条路都必须继续满足它。
- **`SyndicaError` 从工具上抛，其余异常转成模型可见的 output**（分界是"这条消息写给谁"）。本计划的编排工具是这条规则的第一批真实用户：控制面方法对**程序员**报 `ContractViolation`，而工具的调用方是**模型**，因此工具必须把这些翻译成 output。
- **hook 只能收紧信任级别，不能放宽**（计划 1 Task 12 裁决）。本计划的 `Delegate` / `Fork` 把子 Agent 轨迹里最严的 trust 传给父，同一条规则的另一面。
- **切点拒绝自己读不到的槽位，且要响亮。** 计划 1 为此修了四个站点，不要引入第五个。
- **扩展切点是 `<owner>:<event>` 形式的命名空间字符串，分类由声明者显式给出，没有默认值。**
- **一致性判据：意图无法表达则抛，后置条件已成立则成功。** `unregister` 未知 id 是 no-op，`LocalAgentTable.pop` 同理。本计划的每一个新公开方法都要按这条判据说明自己抛还是不抛。
- **`find` 明文承诺绝不激活任何 Agent。** 本计划第一次让别的路径真的会激活，这条承诺因此第一次可能被违反——`QueryAgent` 工具必须走 `find`。

---

## 设计裁决

七个问题在动手之前就得有答案，否则它们会在执行中途以「这里该怎么写」的形式重新出现，而那时改动
成本已经不一样了。逐条给出结论与理由；对应的实现落在括号里的任务。

**1. 事件循环到底等在什么上，挂起怎么与这个等待相处（Task 1 / 3 / 5）**
事件循环只等一样东西：`broker.wait_for_messages(agent_id, None)`。它没有超时——这个循环的退出路径
是取消（`kill` / `shutdown`）与队列消失（`despawn`），不是超时，所以答 `False` 只有一个含义：邮箱
已经被摘掉了，该退出。
**挂起门坐在「有消息」与「取走消息」之间，不在两者之外。** 被挂起的 Agent 若在 `wait_for_messages`
醒来后照常 `consume`，那条消息就离开了邮箱而没有人处理它，`resume` 之后它已经不在了。因此门先过
挂起检查再消费，消息一直留在邮箱里。挂起门本身是**电平**信号（可反复进出），唤醒用**边沿**信号
（`clear` 后 `wait`）——拿电平驱动这个循环会退化成不让出控制权的空转。

**2. 协作式 `abort` 检查什么、在哪里检查（Task 1 / 2 / 6）**
它检查控制块上的一个**一次性**中止位，检查点在 turn 循环每一轮的最开头——**在轮次计数之前**，
所以被中止的一轮不消耗预算。这个检查点就是本计划新加的那道门（`TurnGate.before_turn`）：spec §5.1
的前三步依赖的状态一样都不属于 Agent 自己，把它们收进一个方法，`BaseAgent` 才能继续只认识
「LLM + 工具」。中止位一次性，因为 `abort` 的语义是「停下手上这一件事」，不是「从此不再干活」；
不清位则此后每个周期都会立刻结束。中止还必须能**穿透挂起门**：先挂起再中止是真实顺序。

**3. `kill` / `despawn` / `ExitMode.EXIT` 三条拆除路径（Task 3 / 7）**
合并成一条 `_destroy_tree` → `_destroy`。分工按「删不删注册表条目」切开：
- `kill` **不删条目**，把 Agent 送进 ZOMBIE，因此它根本不会制造悬空 `parent_id`，也就不需要级联。
  它取消事件循环后**立刻返回**，收尾放进一个独立的收割任务——收尾要 `await`，而在一个正在被取消的
  任务里 `await` 只会收一半的尾。spec §4.1 给 `ABORTING` 的理由（「使 `kill()` 能立即返回」）就是它。
- `despawn` **一定删条目**，因此**必须**级联到全部子代，且拆除顺序是子先于父。
- `ExitMode.EXIT` 走同一个 `_destroy_tree`。`_destroy` 里那句 `task is not asyncio.current_task()`
  是为它准备的：EXIT 是在驱动任务自己的 `finally` 里走到这条路的，取消并 `await` 自己就是永久挂死。

**4. `wait_for` 阻塞事件循环还是在轮里等（Task 9）**
**在轮里等。** 一次 `Confirmation` 等待是从工具调用内部发起的，此刻 Agent 正在一轮的中间：turn
循环挂在 `wait_for` 上，注册表说 `WAITING`。它的邮箱仍由门在下一轮开始时消费，两件事各等各的。
被等到的那条答复由 `take_by_correlation` 从邮箱里取走，因此不会再作为普通消息注入一遍；反过来，
一条没人在等的带关联 ID 的消息就是一条普通消息，不会凭空消失。

**5. 抢占由谁发起（Task 9）**
**同一个 Agent 上的下一次 `wait_for`。** 一个 Agent 是一个进程、一份注意力；两个并发等待里必然有
一个的答复没人会来。让旧的挂到超时是最坏的选择——发起它的那次工具调用会一直占着位置，而「一次
调用恰好一个结果块」这条不变式要求它必须能返回点什么。`abort` 送达的是 `aborted` 而不是
`preempted`：两者的后续动作不同（被顶掉的可以重来，被中止的不该重来）。**答复优先于中断**——两者
同时就绪时返回 `answered`，因为消息已经离开邮箱，此刻丢掉它就是真的丢了。

**6. 递归的方向，以及子代状态冲突时怎么办（Task 5 / 6 / 7）**
`pause` / `abort` **父先于子**——先掐掉还会生出新子代、派出新活的那一头；`resume` **子先于父**——父
恢复后可能立刻要用子代，先恢复子代它就不会看见一棵只恢复了一半的树；`despawn` **子先于父**——任何
一刻都不存在「父已经没了而子还活着」的中间态。
遍历用 `children_of`（读注册表）做广度优先，带 `visited` 防环：注册表可以是远端的、可以被别的进程
写过，一个环会让这个公共前置永不返回。
**冲突时：被点名的那一个抛异常，遍历到的子代只记日志。** 递归操作天然是尽力而为的——子代可能是
ZOMBIE、可能正在被拆——为第一个冲突抛异常只会留下一棵改了一半的树，而调用方无从得知改到哪了；
被点名的那一个则意图无歧义，照常抛。CAS 失败同样按这条处理。

**7. 编排工具是工具，因此它们跑在轮里、结果变成工具块（Task 14 / 15）**
- `Delegate`：冷启动一个子 Agent、**等它做完**、把它最后一句话作为 output 交回，并带上子的
  `agent_id`（子是 ZOMBIE，仍可寻址）。它**阻塞父的这一轮**——派活出去却不等结果，父就得自己再
  发明一套轮询。父此刻仍是 `RUNNING` 而不是 `WAITING`：`WAITING` 说的是「等一条匹配的答复」，等一个
  子 Agent 干活是工作本身。
- `Fork`：同一个工厂（同类型，因此继承缓存亲和）+ 源 Agent 的轨迹。拷贝在**最后一个未配对的
  assistant block 处截断**——分叉正是从那次调用内部发起的，原样拷过去子 Agent 会把它当成「上次被
  中断的批次」补上一段凭空捏造的历史。
- **目标死了怎么办**：一律进 `output`，不抛。子 Agent 没干成（被 kill、报错、用完轮次、被中止）是
  模型可以应对的情况，而不是程序错误；措辞按三件套，并一律带上子的 `agent_id` 供它接着
  `QueryAgent` / `SendMessage`。**控制面方法对程序员抛 `ContractViolation`，工具对模型写 output**——
  分界仍是「这条消息写给谁」。
- `KillAgent` 拒绝杀自己：取消会在这次工具调用返回之前就把调用者取消掉，这一批调用因此不会有结果、
  轨迹停在半路。

另外两条与上面同源、也在动手前定下：**被闸阻断的调用仍然产出恰好一个 `role='tool'` block**（阻断
不是失败，是一次「这条路走不通」的回答）；**并发分段看模型发出来的参数，闸看 hook 改写之后的参数**
（分段必须在任何 hook 跑之前算完，那时才有一整批可分；而被闸拦下的调用根本不执行，所以「它曾经和
别人并发过」不产生任何副作用）。

---

## 规格中的空白与需要确认的读法

写这份计划时撞到三处 spec 没有给出唯一答案的地方。**这里给出我采用的读法与理由，它们都需要确认**；
若裁决与此不同，受影响的任务在括号里。

1. **`kill` 到底有没有 `recursive`（Task 7）。** spec §4.2 的签名里只有 `pause` / `resume` / `abort`
   带 `recursive`，`kill` 与 `despawn` 都是 `(self, agent_id) -> None`；而
   [`m1-scope-decisions.md`](../decisions/2026-09-08-m1-scope.md) 把悬空 `parent_id` 的归属写成
   「`despawn` / `kill --recursive`」。两者不能同时成立。
   **采用的读法**：以 §4.2 的签名为准（公开面刻意收紧到 11 个方法，签名本身是公开 API），把级联做成
   `despawn` 的**无条件**语义而不是一个开关——留下指向空洞的 `parent_id` 是数据完整性问题，不是策略。
   `kill` 因为根本不删条目，所以它不制造悬空 `parent_id`，也就不需要级联。
2. **`ABORTING` 属于 `kill` 还是 `abort`（Task 6 / 7）。** §4.1 说它是「中止已置位，正在收尾」，给出的
   理由却只提 `kill`（「使 `kill()` 能立即返回而不必等收尾完成」）；而 §5.1 第 1 步的「中止检查」显然
   是 `abort` 的协作式路径，那条路上也存在一段「已置位、尚未收完尾」的时间。
   **采用的读法**：**两者都用它**。`abort` 置位后状态转 ABORTING，直到轮边界兑现；`kill` 也转 ABORTING
   然后立刻返回。这样 §4.1 给的理由成立，`abort` 也有一个可观察的中间状态，而不是「置位了但从外面
   看不出来」。
3. **`WaitResult.preempted` 由谁触发，spec 完全没写（Task 9）。** 这是空白不是矛盾：四态里给了这个
   取值，却没有任何一处说明什么能displace 一次等待。
   **采用的读法**：同一个 Agent 上的下一次 `wait_for` 顶掉上一次（理由见「设计裁决」第 5 条）。若日后
   要让 `pause` 或某种外部信号也构成抢占，这个位置是现成的——`_WaitToken.interrupt` 已经是通用入口。

另有一处**与 §4.2 字面签名的偏离需要记录**（不是歧义，是计划 1 已经开始、本计划继续的一个选择）：
§4.2 写的是 `send(self, agent_id, message, *, trigger, activate)`，计划 1 把 `message` 落成了
`content: str`；本计划为了让编排工具与 `wait_for` 能用，又加了 `sender` 与 `correlation_id` 两个
关键字参数。语义没变（仍是「投递一条消息」），但公开签名与 spec 的字面写法不同，值得在里程碑 1
收口时统一裁决一次。

---

## 文件结构

本计划结束后的目录（**←** 标记本计划新增，**±** 标记本计划修改）：

```
src/syndica/kernel/
    __init__.py                ±  新增导出
    errors.py
    context.py
    agent_types.py
    message.py
    control.py                 ←  ControlBlock / TurnGate / TurnGateDecision / BrokerTurnGate
    waiting.py                 ←  Waiter 密封类型 / WaitState / WaitResult
    danger.py                  ←  DangerRule / DangerJudge / DangerVerdict / Disposition / 处置策略
    plugins.py                 ←  Plugin Protocol
    protocols/
        __init__.py
        registry.py
        broker.py
    stores/
        __init__.py
        memory_registry.py
        memory_broker.py
    contracts/
        __init__.py
        base.py
        registry.py
        broker.py
    local_table.py
    events.py
    hooks/
        __init__.py
        junctions.py           ±  新增四个切点成员及其分类
        result.py
        center.py              ±  分层（outer）装配
    llm/
        __init__.py
        base.py
        scripted.py
    tool.py                    ±  __tool_command_args__
    tool_executor.py           ±  危险处置 + 并发分段
    tools/                     ←
        __init__.py
        orchestration.py       ←  Delegate / Fork / SendMessage / QueryAgent / KillAgent
    agent.py                   ±  turn 门、消息注入、run(query=None)、局部钩子
    runtime.py                 ±  事件循环、11 个公开方法、级联拆除、插件

tests/
    contracts/…                    （不变）
    kernel/test_control.py     ←
    kernel/test_waiting.py     ←
    kernel/test_danger.py      ←
    kernel/test_plugins.py     ←
    kernel/test_orchestration.py ←
    kernel/test_agent_turn.py  ±
    kernel/test_hooks.py       ±
    kernel/test_public_api.py  ±
    kernel/test_runtime.py     ±
    kernel/test_tool.py        ±
    kernel/test_tool_executor.py ±
    integration/test_single_agent_turn.py
    integration/test_multi_agent_orchestration.py ←
```

**责任划分依据**：`control.py` 与 `local_table.py` 同侧——都是**只在持有 Agent 的进程内有意义**的东西，因此都不进 Protocol；注册表回答"它是什么状态"，控制块提供"怎么让它停下来"的机制。`waiting.py` 与 `danger.py` 各自独立成模块而不是塞进 `runtime.py` / `tool_executor.py`，因为两者都是**纯类型 + 纯规则**，独立才能被单独测试并被 surface 复用（危险判定的处置在 CLI，等待的交互在 CLI）。`tools/` 成包是因为计划 4 还要往里放文件与执行工具。

**明确不在本计划**（各有归属，不要在这里发明）：

| 不做 | 归属 |
| --- | --- |
| 文件与执行工具（`Read` / `Write` / `Edit` / `Glob` / `Grep` / `Bash` 三件套）、`Workspace` / `Portal` 协议与 `LocalWorkspace` | 计划 4 |
| 声明式 Agent 定义（清单十字段、加载与同名覆盖）、Topology 装配、内核内置的四个 Agent | 计划 3 |
| Runtime Module 与 Flag 声明（§5.5 四类注入）、重试与故障转移 Module、前缀漂移检测 | 计划 5 |
| CLI、`AskUser` / `AskUserQuestion`、**人工确认的交互**、L2 声明式场景回放层 | 计划 6 |
| 伪终端（PTY） | 里程碑 5 |
| 基于租约的孤儿回收（`owner` / `heartbeat_at` 的原子写与超时接管） | 里程碑 1 之后 |
| 背景化工具三件套、预算与进度、产物与验收 | 里程碑 7 / 8 |

---

## Task 1: 控制块与 turn 之间的门

**Files:**
- Create: `src/syndica/kernel/control.py`
- Test: `tests/kernel/test_control.py`

**Interfaces:**
- Consumes: `syndica.kernel.message`（`Message` / `Trigger`）、`syndica.kernel.protocols`（`MessageBroker`）
- Produces:
  - `ControlBlock`：`pause()` / `resume()` / `request_abort()` / `take_abort()` / `hold_until_runnable() -> bool` / `wait_until_parked(timeout) -> bool` / `defer(message)` / `take_deferred() -> list[Message]` / 只读 `paused` / `parked` / `deferred_count` / `in_run`
  - `TurnGate` Protocol（`before_turn(turn) -> TurnGateDecision`）、`TurnGateDecision(abort, messages)`、`NullTurnGate`、`BrokerTurnGate(agent_id, broker, control)` 及其 `begin_cycle()`

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_control.py
from __future__ import annotations

import asyncio

import pytest

from syndica.kernel.control import (
    BrokerTurnGate,
    ControlBlock,
    NullTurnGate,
    TurnGateDecision,
)
from syndica.kernel.message import Message, Trigger
from syndica.kernel.stores import InMemoryMessageBroker

# 挂死类回归应当变红而不是把 CI 挂住；1s 对纯内存操作是极宽裕的上界。
_TIMEOUT = 1.0
_AGENT = 'a1'


def _message(content: str, *, trigger: Trigger = Trigger.PENDING) -> Message:
    return Message(sender='user', receiver=_AGENT, content=content, trigger=trigger)


async def _publish(broker: InMemoryMessageBroker, *contents: str, trigger: Trigger) -> None:
    for content in contents:
        await broker.publish(_AGENT, _message(content, trigger=trigger))


class _CountingBroker:
    """记下 consume 被调用过几次的 broker 包装。

    「挂起时没有消费消息」只看邮箱剩几条是不够的：一个先取走再放回的实现同样能让邮箱看起来
    没动。计数是能区分两者的那个观测点。
    """

    def __init__(self, inner: InMemoryMessageBroker) -> None:
        self.inner = inner
        self.consume_calls = 0

    async def ensure_queue(self, agent_id: str) -> None:
        await self.inner.ensure_queue(agent_id)

    async def publish(self, agent_id: str, message: Message):
        return await self.inner.publish(agent_id, message)

    async def consume(self, agent_id: str, *, max_n: int = 0) -> list[Message]:
        self.consume_calls += 1
        return await self.inner.consume(agent_id, max_n=max_n)

    async def wait_for_messages(self, agent_id: str, timeout: float | None) -> bool:
        return await self.inner.wait_for_messages(agent_id, timeout)

    async def take_by_correlation(self, agent_id: str, correlation_id: str, timeout: float | None):
        return await self.inner.take_by_correlation(agent_id, correlation_id, timeout)

    async def remove_queue(self, agent_id: str) -> None:
        await self.inner.remove_queue(agent_id)

    def on_missing_queue(self, cb) -> None:
        self.inner.on_missing_queue(cb)


async def test_a_fresh_control_block_lets_the_turn_loop_through():
    control = ControlBlock()
    assert control.paused is False
    assert await asyncio.wait_for(control.hold_until_runnable(), timeout=_TIMEOUT) is False


async def test_pause_parks_the_turn_loop_until_resume():
    control = ControlBlock()
    control.pause()
    held = asyncio.create_task(control.hold_until_runnable())
    # 确定性握手：wait_until_parked 返回 True 就证明它真的停在门上了，
    # 不是「还没被调度到」，因此下面的断言不会由空转恒成立。
    assert await control.wait_until_parked(timeout=_TIMEOUT) is True
    assert control.paused is True
    control.resume()
    assert await asyncio.wait_for(held, timeout=_TIMEOUT) is False
    assert control.parked == 0


async def test_abort_pierces_the_pause_gate():
    """先挂起再中止是真实顺序；中止若要等解除挂起才被看见，这个 Agent 就永远停在那里。"""
    control = ControlBlock()
    control.pause()
    held = asyncio.create_task(control.hold_until_runnable())
    assert await control.wait_until_parked(timeout=_TIMEOUT) is True
    control.request_abort()
    assert await asyncio.wait_for(held, timeout=_TIMEOUT) is True
    assert control.paused is True


async def test_abort_is_consumed_once_so_the_next_cycle_still_runs():
    control = ControlBlock()
    control.request_abort()
    assert await asyncio.wait_for(control.hold_until_runnable(), timeout=_TIMEOUT) is True
    assert await asyncio.wait_for(control.hold_until_runnable(), timeout=_TIMEOUT) is False


async def test_deferred_messages_come_back_in_arrival_order():
    """基数 3 且顺序既非升序亦非降序：n<=2 时排序类变异必有一个是恒等变换。"""
    control = ControlBlock()
    for content in ('beta', 'alpha', 'gamma'):
        control.defer(_message(content, trigger=Trigger.DEFERRED))
    assert control.deferred_count == 3
    assert [message.content for message in control.take_deferred()] == ['beta', 'alpha', 'gamma']
    assert control.deferred_count == 0


async def test_deferring_snapshots_the_message():
    """交进来的消息此后被改动，不得改到已经暂存的那一份。"""
    control = ControlBlock()
    message = _message('original', trigger=Trigger.DEFERRED)
    control.defer(message)
    message.content = 'mutated'
    assert [taken.content for taken in control.take_deferred()] == ['original']


async def test_taking_deferred_hands_over_a_list_the_block_no_longer_owns():
    control = ControlBlock()
    control.defer(_message('one', trigger=Trigger.DEFERRED))
    taken = control.take_deferred()
    taken.append(_message('injected', trigger=Trigger.DEFERRED))
    assert control.deferred_count == 0


async def test_null_gate_never_aborts_and_never_injects():
    decision = await asyncio.wait_for(NullTurnGate().before_turn(1), timeout=_TIMEOUT)
    assert decision == TurnGateDecision()


async def test_gate_injects_pending_and_stashes_deferred_after_the_cycle_started():
    broker = InMemoryMessageBroker()
    await broker.ensure_queue(_AGENT)
    control = ControlBlock()
    gate = BrokerTurnGate(_AGENT, broker, control)
    gate.begin_cycle()
    decision = await asyncio.wait_for(gate.before_turn(1), timeout=_TIMEOUT)
    assert decision.messages == ()

    await _publish(broker, 'p1', trigger=Trigger.PENDING)
    await _publish(broker, 'd1', trigger=Trigger.DEFERRED)
    await _publish(broker, 'p2', trigger=Trigger.PENDING)
    decision = await asyncio.wait_for(gate.before_turn(2), timeout=_TIMEOUT)
    assert [message.content for message in decision.messages] == ['p1', 'p2']
    assert control.deferred_count == 1


async def test_the_first_turn_of_a_cycle_injects_the_stash_before_fresh_arrivals():
    """暂存的那些先到，所以它们排在前面——整条注入序列仍是先来先到。"""
    broker = InMemoryMessageBroker()
    await broker.ensure_queue(_AGENT)
    control = ControlBlock()
    gate = BrokerTurnGate(_AGENT, broker, control)
    gate.begin_cycle()
    await gate.before_turn(1)

    await _publish(broker, 'stashed', trigger=Trigger.DEFERRED)
    await gate.before_turn(2)
    await _publish(broker, 'fresh-pending', trigger=Trigger.PENDING)
    await _publish(broker, 'fresh-deferred', trigger=Trigger.DEFERRED)

    gate.begin_cycle()
    decision = await asyncio.wait_for(gate.before_turn(3), timeout=_TIMEOUT)
    assert [message.content for message in decision.messages] == [
        'stashed',
        'fresh-pending',
        'fresh-deferred',
    ]
    assert control.deferred_count == 0


async def test_a_paused_gate_does_not_consume_the_message_it_was_about_to_take():
    inner = InMemoryMessageBroker()
    await inner.ensure_queue(_AGENT)
    broker = _CountingBroker(inner)
    control = ControlBlock()
    gate = BrokerTurnGate(_AGENT, broker, control)
    gate.begin_cycle()
    await _publish(inner, 'do-not-eat-me', trigger=Trigger.PENDING)

    control.pause()
    pending = asyncio.create_task(gate.before_turn(1))
    assert await control.wait_until_parked(timeout=_TIMEOUT) is True
    assert broker.consume_calls == 0

    control.resume()
    decision = await asyncio.wait_for(pending, timeout=_TIMEOUT)
    assert [message.content for message in decision.messages] == ['do-not-eat-me']


async def test_an_aborting_gate_reports_abort_and_takes_no_message():
    broker = InMemoryMessageBroker()
    await broker.ensure_queue(_AGENT)
    control = ControlBlock()
    gate = BrokerTurnGate(_AGENT, broker, control)
    await _publish(broker, 'still-queued', trigger=Trigger.PENDING)
    control.request_abort()

    decision = await asyncio.wait_for(gate.before_turn(1), timeout=_TIMEOUT)
    assert decision.abort is True
    assert decision.messages == ()
    assert [message.content for message in await broker.consume(_AGENT)] == ['still-queued']


async def test_wait_until_parked_reports_false_when_nobody_parks():
    """守卫自身必须能答"没有"，否则上面那些握手断言可能只是恒真。"""
    control = ControlBlock()
    assert await control.wait_until_parked(timeout=0.05) is False


def test_decision_defaults_are_immutable():
    decision = TurnGateDecision()
    assert decision.abort is False
    assert decision.messages == ()
    with pytest.raises(TypeError):
        decision.abort = True  # type: ignore[misc]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_control.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'syndica.kernel.control'`

- [ ] **Step 3: 写 `control.py`**

```python
# src/syndica/kernel/control.py
"""进程内控制块，以及 turn 与 turn 之间的那道门。

按操作系统进程模型，本模块是 Agent 的 PCB 里**只在持有它的进程内有意义**的那些位，与
`LocalAgentTable` 同侧：`AgentRegistry` 回答「它现在是什么状态」，控制块提供「怎么让它停下来、
怎么把消息交给它」的机制。状态仍以注册表为准——Agent 阻塞或崩溃时无法自报状态（spec §4.3）。

**挂起是电平，中止是一次性边沿。** 挂起要能反复进出，因此用一个「现在可以往下走吗」的电平信号
表达；中止只兑现一次，`take_abort` 取走即清位——否则一次 abort 会让此后每一个周期都立刻结束，
而 abort 的语义是「停下手上这一件事」，不是「从此不再干活」。

**中止必须能穿透挂起门。** 先挂起再中止是现实中会发生的顺序（发现它跑偏 → 先停下来 → 决定不要
了）。若中止只在挂起解除之后才被看见，这个 Agent 会一直停在挂起态，而 abort 的调用方拿不到任何
反馈。因此等待用一个**边沿**唤醒信号，`resume` 与 `request_abort` 都置位它；等待前先 `clear()`
再 `wait()`，把它当边沿用——拿电平驱动这个循环会退化成不让出控制权的空转。

**「已请求挂起」与「真的停下来了」是两件事。** turn 循环可能正卡在一次 LLM 调用里，此时注册表
已经是 PAUSED 而它还在跑。`parked` / `wait_until_parked` 是这个区别唯一的观测点：界面上的暂停
按钮需要它才能显示「已停」，测试需要它才能在不靠 sleep 的前提下做确定性握手。

**DEFERRED 消息暂存在这里而不是 broker 里。** `MessageBroker` 没有「取回来又放回去」的操作
（spec §4.4：谓词无法下推到远端队列，全量取回本地过滤再放回既慢又有竞态），所以按 trigger 分流
只能发生在消费之后。代价是暂存的消息随进程消失——与已经追加进上下文的那些一样。
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from syndica.kernel.message import Message, Trigger
from syndica.kernel.protocols import MessageBroker

__all__ = [
    'BrokerTurnGate',
    'ControlBlock',
    'NullTurnGate',
    'TurnGate',
    'TurnGateDecision',
]


class ControlBlock:
    """一个 Agent 的进程内控制位：挂起门、协作式中止位、延迟消息暂存。"""

    def __init__(self) -> None:
        # 电平：置位 = 允许往下走。挂起要能反复进出，所以它表达状态而不是事件。
        self._runnable = asyncio.Event()
        self._runnable.set()
        # 边沿：resume 与 request_abort 都置位它，等待者在 wait 之前先 clear。
        self._wakeup = asyncio.Event()
        self._abort_requested = False
        self._parked = 0
        self._parked_event = asyncio.Event()
        self._deferred: list[Message] = []
        self._in_run = False

    @property
    def paused(self) -> bool:
        return not self._runnable.is_set()

    @property
    def parked(self) -> int:
        """此刻真的停在挂起门上的执行体数。"""
        return self._parked

    @property
    def abort_requested(self) -> bool:
        """中止位是否置位。读它不消费它——消费只发生在 `take_abort`。"""
        return self._abort_requested

    @property
    def in_run(self) -> bool:
        """这个 Agent 此刻是否在一次 `run()` 之内。`resume` 靠它决定该回到 RUNNING 还是 IDLE。"""
        return self._in_run

    @property
    def deferred_count(self) -> int:
        """暂存的延迟消息条数。只交出条数不交出消息，避免又开一个活引用的口子。"""
        return len(self._deferred)

    def enter_run(self) -> None:
        self._in_run = True

    def leave_run(self) -> None:
        self._in_run = False

    def pause(self) -> None:
        """请求挂起。幂等；它只清电平，不动中止位。"""
        self._runnable.clear()

    def resume(self) -> None:
        """解除挂起。幂等。"""
        self._runnable.set()
        self._wakeup.set()

    def request_abort(self) -> None:
        """置位协作式中止。幂等，且**必须唤醒挂起门上的等待者**。"""
        self._abort_requested = True
        self._wakeup.set()

    def take_abort(self) -> bool:
        """取走中止位并清位。一次 abort 只兑现一次。"""
        taken = self._abort_requested
        self._abort_requested = False
        return taken

    async def hold_until_runnable(self) -> bool:
        """等到可以继续；返回 True 表示应当中止（中止位已被消费）。

        条件检查与 `clear()` 之间没有挂起点，因此这之后的每一次置位都一定能把我们唤醒，不会漏。
        """
        while True:
            if self._abort_requested:
                return self.take_abort()
            if self._runnable.is_set():
                return False
            self._wakeup.clear()
            self._enter_park()
            try:
                await self._wakeup.wait()
            finally:
                self._leave_park()

    async def wait_until_parked(self, timeout: float | None = None) -> bool:
        """等到有执行体真的停在挂起门上；超时返回 False。

        这是一个问句，答不出就答 False，不抛异常——与 `MessageBroker.wait_for_messages` 同一条
        一致性规则。
        """
        if self._parked > 0:
            return True
        try:
            await asyncio.wait_for(self._parked_event.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return self._parked > 0

    def defer(self, message: Message) -> None:
        """把一条 DEFERRED 消息挂进暂存队列。

        存快照而不是调用方的对象：broker 的 `publish` 已经按这条规则做过一次，暂存队列是同一条
        消息的第二个落点，两处行为必须一致。
        """
        self._deferred.append(copy.deepcopy(message))

    def take_deferred(self) -> list[Message]:
        """取走全部暂存消息，按到达顺序。取走之后本块不再持有它们。"""
        taken = self._deferred
        self._deferred = []
        return taken

    def _enter_park(self) -> None:
        self._parked += 1
        self._parked_event.set()

    def _leave_park(self) -> None:
        self._parked -= 1
        if self._parked == 0:
            self._parked_event.clear()


@dataclass(frozen=True)
class TurnGateDecision:
    """一次 turn 前置检查的结论。

    `messages` 是**元组**而不是列表：它跨越了控制面与 turn 循环的边界，交出可变列表等于让接收方
    能改到发送方还在用的东西。
    """

    abort: bool = False
    messages: tuple[Message, ...] = ()


@runtime_checkable
class TurnGate(Protocol):
    """turn 循环与控制面之间的唯一接缝。

    spec §5.1 把每一轮的前三步定为「中止检查 → 挂起检查 → 消费待处理消息」，而这三件事的状态
    全都不属于 Agent 自己（§4.3）。把它们收进一个方法，turn 循环就只需要在轮与轮之间问一句
    「我还能往下走吗、有什么要先看的吗」，不必知道注册表、broker 或控制块的存在。
    """

    async def before_turn(self, turn: int) -> TurnGateDecision:
        """在第 `turn` 轮开始之前调用；返回是否应当中止，以及要注入的消息。"""
        ...


class NullTurnGate:
    """没有控制面时的门：从不中止、从不挂起、没有消息。

    `BaseAgent` 可以脱离 `Runtime` 单独使用（计划 1 的全部测试都这么用），此时它需要一个不改变
    任何行为的门，而不是到处 `if self._gate is not None`。
    """

    async def before_turn(self, turn: int) -> TurnGateDecision:
        return TurnGateDecision()


class BrokerTurnGate:
    """接上 broker 与控制块的门。

    **周期的第一轮注入一切，其后的轮次只注入 PENDING。** spec §4.5 说 DEFERRED 的语义是「先把
    手上的事做完，然后再看这个」——而周期刚开始时手上没有事，此时再把它推迟一个周期只会白跑一次
    模型调用。判据因此是「这个周期已经干过活了吗」，由驱动循环用 `begin_cycle` 划界。

    注入顺序是**先暂存后新到**：暂存的那些消费得更早，整条注入序列因此仍是先来先到。
    """

    def __init__(self, agent_id: str, broker: MessageBroker, control: ControlBlock) -> None:
        self._agent_id = agent_id
        self._broker = broker
        self._control = control
        self._at_cycle_start = True

    def begin_cycle(self) -> None:
        """由驱动循环在每次 `run()` 之前调用，标记「下一轮是这个周期的第一轮」。"""
        self._at_cycle_start = True

    async def before_turn(self, turn: int) -> TurnGateDecision:
        if await self._control.hold_until_runnable():
            return TurnGateDecision(abort=True)
        at_cycle_start = self._at_cycle_start
        self._at_cycle_start = False
        injected: list[Message] = self._control.take_deferred() if at_cycle_start else []
        for message in await self._broker.consume(self._agent_id):
            if message.trigger is Trigger.DEFERRED and not at_cycle_start:
                self._control.defer(message)
            else:
                injected.append(message)
        return TurnGateDecision(messages=tuple(injected))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/kernel/test_control.py -v && uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: 全部 passed；lint 与类型检查通过

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/control.py tests/kernel/test_control.py
git commit -m "feat(kernel): 控制块把「怎么让它停下来」与「它是什么状态」分开

注册表回答状态，是因为 Agent 阻塞或崩溃时无法自报状态；但「停下来」是机制不是
状态，它只在持有该 Agent 实例的进程里有意义，所以和 LocalAgentTable 同侧，不进
Protocol。

三处刻意的设计：
- 中止能穿透挂起门。先挂起再中止是真实顺序（发现跑偏→先停下→决定不要了），
  中止若要等解除挂起才被看见，这个 Agent 就永远停在那里而调用方毫无反馈
- 挂起用电平、唤醒用边沿。拿电平驱动「等下一个」的循环会退化成不让出控制权的
  空转，连 asyncio.wait_for 都击穿，表现为整套测试挂死而不是超时
- parked 是「已请求挂起」与「真的停下来了」之间唯一的观测点。turn 循环可能正卡
  在一次 LLM 调用里，此时状态已是 PAUSED 而它还在跑

DEFERRED 暂存在控制块而不是 broker：Protocol 没有「取回来又放回去」，
按 trigger 分流只能发生在消费之后。"
```

---
## Task 2: turn 循环接上门——中止、挂起、消息注入

**Files:**
- Modify: `src/syndica/kernel/hooks/junctions.py`
- Modify: `src/syndica/kernel/agent.py`
- Test: `tests/kernel/test_agent_turn.py`
- Test: `tests/kernel/test_hooks.py`

**Interfaces:**
- Consumes: Task 1 的 `TurnGate` / `TurnGateDecision` / `NullTurnGate`
- Produces:
  - `Junction.MESSAGE_INJECTED`（可观察）
  - `BaseAgent(options, *, hooks, events, gate=None)`、`BaseAgent.attach_gate(gate)`
  - `BaseAgent.run(query: str | None = None)`：`None` 表示「这一轮的输入从门里来」
  - 每轮开始前依次做：中止检查 → 挂起检查 → 消费并注入消息（spec §5.1 第 1–3 步）

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_agent_turn.py  （在既有文件末尾追加；文件头部已有 from __future__ import annotations）
async def test_gate_abort_between_turns_stops_with_aborted():
    """协作式中止落在轮与轮之间，此时轨迹是一致的——这正是 ABORTED 与硬取消的分界。"""
    control = ControlBlock()
    broker = InMemoryMessageBroker()
    await broker.ensure_queue('a1')
    gate = BrokerTurnGate('a1', broker, control)
    agent = _agent([ScriptedTurn(content='never reached')], gate=gate)
    control.request_abort()

    ctx = await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert ctx.stop_reason is StopReason.ABORTED
    assert [block.role for block in ctx.current_traj] == ['user']


async def test_aborting_between_turns_does_not_spend_a_turn():
    """中止发生在计数之前：一个被中止的周期不该吃掉整个任务的预算。"""
    control = ControlBlock()
    broker = InMemoryMessageBroker()
    await broker.ensure_queue('a1')
    gate = BrokerTurnGate('a1', broker, control)
    agent = _agent(
        [ScriptedTurn(content='first'), ScriptedTurn(content='second')], gate=gate, max_turns=2
    )
    await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)
    control.request_abort()
    await asyncio.wait_for(agent.run('again'), timeout=_TIMEOUT)
    ctx = await asyncio.wait_for(agent.run('third time'), timeout=_TIMEOUT)

    assert ctx.stop_reason is StopReason.COMPLETED


async def test_pending_messages_are_injected_before_the_turn_starts():
    """spec §5.1：消费待处理消息发生在 turn_start 之前，用户中途插话在下一轮就可见。"""
    control = ControlBlock()
    broker = InMemoryMessageBroker()
    await broker.ensure_queue('a1')
    gate = BrokerTurnGate('a1', broker, control)
    agent = _agent(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]),
            ScriptedTurn(content='done'),
        ],
        gate=gate,
    )
    await broker.publish(
        'a1', Message(sender='user', receiver='a1', content='also check the logs')
    )

    ctx = await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert [block.role for block in ctx.current_traj] == [
        'user',
        'user',
        'assistant',
        'tool',
        'assistant',
    ]
    assert ctx.current_traj[1].content == 'also check the logs'
    assert ctx.current_traj[1].extra['sender'] == 'user'


async def test_injected_messages_keep_their_trust_level():
    """不可信内容经消息进来仍然不可信——标记必须随内容走（spec §12.2）。"""
    control = ControlBlock()
    broker = InMemoryMessageBroker()
    await broker.ensure_queue('a1')
    gate = BrokerTurnGate('a1', broker, control)
    agent = _agent([ScriptedTurn(content='ok')], gate=gate)
    await broker.publish(
        'a1',
        Message(
            sender='webhook',
            receiver='a1',
            content='<from the internet>',
            trust=TrustLevel.UNTRUSTED,
        ),
    )

    ctx = await asyncio.wait_for(agent.run(None), timeout=_TIMEOUT)

    injected = [block for block in ctx.current_traj if block.role == 'user']
    assert [block.trust for block in injected] == [TrustLevel.UNTRUSTED]


async def test_three_injected_messages_keep_arrival_order():
    """基数 3 且既非升序亦非降序：两条消息挡不住任何排序类变异。"""
    control = ControlBlock()
    broker = InMemoryMessageBroker()
    await broker.ensure_queue('a1')
    gate = BrokerTurnGate('a1', broker, control)
    agent = _agent([ScriptedTurn(content='ok')], gate=gate)
    for content in ('beta', 'alpha', 'gamma'):
        await broker.publish('a1', Message(sender='user', receiver='a1', content=content))

    ctx = await asyncio.wait_for(agent.run(None), timeout=_TIMEOUT)

    assert [block.content for block in ctx.current_traj if block.role == 'user'] == [
        'beta',
        'alpha',
        'gamma',
    ]


async def test_message_injected_fires_once_per_message():
    control = ControlBlock()
    broker = InMemoryMessageBroker()
    await broker.ensure_queue('a1')
    gate = BrokerTurnGate('a1', broker, control)
    hooks = HooksCenter()
    seen: list[str] = []

    async def record(**kwargs: object) -> None:
        message = kwargs['message']
        assert isinstance(message, Message)
        seen.append(message.content)

    hooks.register(Junction.MESSAGE_INJECTED, record)
    agent = _agent([ScriptedTurn(content='ok')], gate=gate, hooks=hooks)
    for content in ('beta', 'alpha', 'gamma'):
        await broker.publish('a1', Message(sender='user', receiver='a1', content=content))

    await asyncio.wait_for(agent.run(None), timeout=_TIMEOUT)

    assert seen == ['beta', 'alpha', 'gamma']


async def test_run_without_a_query_appends_no_user_block_of_its_own():
    agent = _agent([ScriptedTurn(content='ok')])
    ctx = await asyncio.wait_for(agent.run(None), timeout=_TIMEOUT)
    assert [block.role for block in ctx.current_traj] == ['assistant']


async def test_attaching_a_second_gate_is_rejected():
    """两道门就是两个消费同一个邮箱的人，只有一个会赢，而输的那个静默收不到消息。"""
    agent = _agent([ScriptedTurn(content='ok')])
    control = ControlBlock()
    broker = InMemoryMessageBroker()
    agent.attach_gate(BrokerTurnGate('a1', broker, control))
    with pytest.raises(ContractViolation):
        agent.attach_gate(BrokerTurnGate('a1', broker, control))
```

```python
# tests/kernel/test_hooks.py  （替换既有的 test_eleven_junctions_exist 与两条分类断言）
def test_twelve_junctions_exist():
    """取值撞车会让成员静默塌成别名；切点名计入公开 API。"""
    assert {junction.name: junction.value for junction in Junction} == {
        'AGENT_START': 'agent_start',
        'AGENT_END': 'agent_end',
        'AGENT_ERROR': 'agent_error',
        'TURN_START': 'turn_start',
        'TURN_END': 'turn_end',
        'MESSAGE_INJECTED': 'message_injected',
        'PRE_TOOL_USE': 'pre_tool_use',
        'POST_TOOL_USE': 'post_tool_use',
        'PRE_LLM_REQUEST': 'pre_llm_request',
        'POST_LLM_RESPONSE': 'post_llm_response',
        'CONTEXT_OVERFLOW': 'context_overflow',
        'LLM_FAILURE': 'llm_failure',
    }


def test_each_junction_is_classified_as_declared():
    """分类决定语义，与切点名同属公开 API：挪动一个切点就改了它的执行方式。"""
    assert INTERCEPTABLE == {
        Junction.PRE_TOOL_USE,
        Junction.POST_TOOL_USE,
        Junction.PRE_LLM_REQUEST,
        Junction.POST_LLM_RESPONSE,
        Junction.CONTEXT_OVERFLOW,
        Junction.LLM_FAILURE,
    }
    assert OBSERVABLE == {
        Junction.AGENT_START,
        Junction.AGENT_END,
        Junction.AGENT_ERROR,
        Junction.TURN_START,
        Junction.TURN_END,
        Junction.MESSAGE_INJECTED,
    }
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_agent_turn.py tests/kernel/test_hooks.py -v`
Expected: FAIL —— `Junction` 没有 `MESSAGE_INJECTED`；`BaseAgent` 没有 `attach_gate`；`run()` 不接受 `None`

- [ ] **Step 3: 给 `junctions.py` 加上消息切点**

在 `Junction` 里 `TURN_END` 之后插入一行，并把它加进 `OBSERVABLE`：

```python
class Junction(Enum):
    AGENT_START = 'agent_start'
    AGENT_END = 'agent_end'
    AGENT_ERROR = 'agent_error'
    TURN_START = 'turn_start'
    TURN_END = 'turn_end'
    MESSAGE_INJECTED = 'message_injected'
    PRE_TOOL_USE = 'pre_tool_use'
    POST_TOOL_USE = 'post_tool_use'
    PRE_LLM_REQUEST = 'pre_llm_request'
    POST_LLM_RESPONSE = 'post_llm_response'
    CONTEXT_OVERFLOW = 'context_overflow'
    LLM_FAILURE = 'llm_failure'


OBSERVABLE: frozenset[Junction] = frozenset(
    {
        Junction.AGENT_START,
        Junction.AGENT_END,
        Junction.AGENT_ERROR,
        Junction.TURN_START,
        Junction.TURN_END,
        Junction.MESSAGE_INJECTED,
    }
)
```

并在模块 docstring 的「本计划只落地……」那段后面补一句，说明消息切点是可观察的：

```
`MESSAGE_INJECTED` 是可观察切点：它报告「这条消息已经进上下文了」这个既成事实，改不了任何东西。
要在消息进上下文**之前**改写它，正确的位置是投递侧而不是这里。
```

- [ ] **Step 4: 改 `agent.py`**

import 块补上门的类型（与既有 stdlib 块合并，不另起一块）：

```python
from syndica.kernel.control import NullTurnGate, TurnGate, TurnGateDecision
from syndica.kernel.message import Message
```

构造器与 `attach_gate`：

```python
    def __init__(
        self,
        options: AgentOptions,
        *,
        hooks: HooksCenter,
        events: EventBus,
        gate: TurnGate | None = None,
    ) -> None:
        self.options: AgentOptions = options
        # 全量 uuid4 hex 而非截断：id 撞车不是软失败——AgentRegistry.register 拒绝重复 id，
        # 撞上的那个 Agent 直接注册不进去。
        self.agent_id: str = uuid.uuid4().hex
        self._hooks = hooks
        self._events = events
        self._executor = ToolExecutor(tools=options.tools, hooks=hooks)
        self._context = Context()
        self._current_turn = 0
        self._running = False
        self._gate: TurnGate = gate if gate is not None else NullTurnGate()
        self._gate_attached = gate is not None

    def attach_gate(self, gate: TurnGate) -> None:
        """装上控制面的门。

        `Runtime` 只能在这里装：门要按 `agent_id` 认邮箱，而 `agent_id` 在本类构造时才生成，
        工厂手里没有它。

        已经装过就报错。两道门意味着两个消费同一个邮箱的人，`consume` 恰好一次，因此输的那个会
        **静默**收不到任何消息——一个只在有人插话时才现身的缺陷。
        """
        if self._gate_attached:
            raise ContractViolation(
                expected=f'agent {self.agent_id!r} 还没有装过 turn 门',
                received=f'它已经装了一个 {type(self._gate).__name__}',
                next_action=(
                    '一个 Agent 只归一个控制面驱动：要换驱动方就新建一个 Agent 实例。'
                    '两道门会各自 consume 同一个邮箱，而 consume 恰好一次，输的那个静默收不到消息'
                ),
            )
        self._gate = gate
        self._gate_attached = True
```

`run` 与 `_run` 接受 `None`：

```python
    async def run(self, query: str | None = None) -> Context:
        """跑一个工作周期。

        `query=None` 表示「这一次的输入从门里来」——事件循环被一条消息唤醒时走的就是这条路，
        消息由门在第一轮开始前消费并注入。**只有一条消费路径**：中途插话与唤醒用的是同一段代码，
        两者的行为因此不会分叉。
        """
        if self._running:
            raise _concurrent_run(self.agent_id)
        # 置位与上面的检查之间没有挂起点，单事件循环下这对操作是原子的。finally 必须包住
        # BaseException：取消之后这个实例仍要能被 resume（入口的修复正是为它准备的）。
        self._running = True
        try:
            return await self._run(query)
        finally:
            self._running = False

    async def _run(self, query: str | None) -> Context:
        ctx = self._context
        ctx.stop_reason = None
        _repair_interrupted_batch(ctx)
        if query is not None:
            ctx.append(ContextBlock(role='user', content=query))
        await self._emit('agent_start', {})
        await self._hooks.trigger(Junction.AGENT_START, ctx=ctx)
        # Exception 而非 BaseException：取消既不是正常结束也不是失败，展开的路上不再触发切点、
        # 不再发事件。
        try:
            stop_reason = await self._run_turns(ctx)
        except Exception:
            ctx.stop_reason = StopReason.ERROR
            await self._hooks.trigger(Junction.AGENT_ERROR, ctx=ctx)
            await self._emit('agent_error', {})
            raise
        await self._hooks.trigger(Junction.AGENT_END, ctx=ctx)
        # 事件 payload 必须是纯数据：枚举成员走不出序列化的 SSE 出口。
        await self._emit('agent_end', {'stop_reason': stop_reason.value})
        return ctx
```

轮循环前置三步与注入：

```python
    async def _run_turns(self, ctx: Context) -> StopReason:
        tool_defs = [tool.schema for tool in self.options.tools.values()]
        max_turns = self.options.max_turns
        turns: Iterable[int] = (
            itertools.count(self._current_turn + 1)
            if max_turns is None
            else range(self._current_turn + 1, max_turns + 1)
        )
        for turn in turns:
            # spec §5.1 的第 1–3 步都在计数之前：被中止的一轮不消耗预算，挂起的那段时间也不算
            # 进任何一轮。
            decision = await self._gate.before_turn(turn)
            if decision.abort:
                ctx.stop_reason = StopReason.ABORTED
                return StopReason.ABORTED
            await self._inject(ctx, decision)

            self._current_turn = turn
            await self._emit('turn_start', {'turn': turn})
            await self._hooks.trigger(Junction.TURN_START, ctx=ctx, turn=turn)

            response = await self._one_llm_call(ctx, tool_defs)
            if response is None:
                await self._end_turn(ctx, turn, None)
                continue

            ctx.append(response)
            if not response.tool_calls:
                ctx.stop_reason = StopReason.COMPLETED
                await self._end_turn(ctx, turn, response)
                return StopReason.COMPLETED

            await self._executor.execute_tool_calls(ctx, response.tool_calls)
            await self._end_turn(ctx, turn, response)
        ctx.stop_reason = StopReason.MAX_TURNS
        return StopReason.MAX_TURNS

    async def _inject(self, ctx: Context, decision: TurnGateDecision) -> None:
        """把门交来的消息追加成 user block，逐条触发 MESSAGE_INJECTED。

        `trust` 从消息上原样带过来：外部来源的消息经这条路进上下文时仍必须是不可信的
        （spec §12.2 第 2 条）。`extra` 只记来源事实（谁发的、什么时机），**不带任何展示策略**
        ——那是 surface 的事（spec §4.5）。
        """
        for message in decision.messages:
            ctx.append(
                ContextBlock(
                    role='user',
                    content=message.content,
                    trust=message.trust,
                    extra={
                        'message_id': message.id,
                        'sender': message.sender,
                        'trigger': message.trigger.value,
                    },
                )
            )
            await self._hooks.trigger(Junction.MESSAGE_INJECTED, ctx=ctx, message=message)
```

模块 docstring 补两段（放在「同一个 Agent 不接受并发或重入的 `run()`」那段之后）：

```
**轮与轮之间有一道门。** spec §5.1 的前三步（中止检查 / 挂起检查 / 消费待处理消息）依赖的状态
一样都不属于本类——它们归控制面（§4.3）。因此本类只在每轮开头问门一句「还能往下走吗、有什么要
先看的」，`TurnGate` 之外的一切（注册表、broker、控制块）本类都不认识。没装门时用
`NullTurnGate`，行为与计划 1 逐字一致。

**`ABORTED` 只从这道门出来。** 它是**协作式**中止落在轮与轮之间的结果，此时轨迹是一致的、可以
直接 resume；硬取消落在轮中间，可能留下不配对的调用，因此它**不设** `stop_reason`。合并两者会
毁掉 resume 正需要的那个区分。
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/kernel -v && uv run ruff check . && uv run pyright`
Expected: 全部 passed

- [ ] **Step 6: 提交**

```bash
git add src/syndica/kernel/hooks/junctions.py src/syndica/kernel/agent.py tests/kernel/test_agent_turn.py tests/kernel/test_hooks.py
git commit -m "feat(kernel): turn 循环通过一道门读控制面，而不是自己去认注册表

spec §5.1 的前三步（中止 / 挂起 / 消费消息）依赖的状态一样都不属于 Agent 自己——
Agent 阻塞时无法自报状态，所以状态在它之外。把这三件事收进 TurnGate 之后，
BaseAgent 仍然只认识「LLM + 工具」，控制面换实现不动它一行。

run(query=None) 让「被一条消息唤醒」和「中途插话」走同一段消费代码。两条路径分开
写必然分叉，而分叉出来的差异只在有人插话时才现身，是最难查的一类。

ABORTED 第一次有了产出点：它是协作式中止落在轮与轮之间的结果，此时轨迹一致、可
直接 resume。硬取消落在轮中间因此仍然不设 stop_reason——合并两者会毁掉 resume
正需要的区分。"
```

---

## Task 3: 事件循环——IDLE↔RUNNING 周期与 DEFERRED 的消费时机

**Files:**
- Modify: `src/syndica/kernel/runtime.py`
- Test: `tests/kernel/test_runtime.py`

**Interfaces:**
- Consumes: Task 1 的 `ControlBlock` / `BrokerTurnGate`，Task 2 的 `attach_gate` / `run(None)`
- Produces:
  - `Runtime.spawn(factory, *, query=None, exit_mode, parent_id, metadata)`：`query` 可省，省了就是「起来等消息」
  - 驱动任务从「调一次 `run`」变成 IDLE↔RUNNING 周期循环
  - `ExitMode.ZOMBIE` 现在**真的摘掉队列**（spec §4.1），复活靠计划里下一个任务接线的 `on_missing_queue`
  - `Runtime._switch(agent_id, *, new, allowed)`：只走 CAS 的状态迁移助手

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_runtime.py  （在既有文件末尾追加；下面的 helper 与既有 _factory 并存）
async def _wait_for_status(runtime: Runtime, agent_id: str, status: AgentStatus) -> None:
    """轮询等到注册表里的状态到达；超时抛 TimeoutError，让回归表现为失败而不是挂死。

    状态转换发生在驱动任务内部，测试这边没有可以直接 await 的信号。2s 的上界只用来把「永远
    到不了」和「慢一点」分开——这里全是内存操作，正常路径以微秒计。
    """

    async def _poll() -> None:
        while True:
            info = await runtime.find(agent_id)
            if info is not None and info.status is status:
                return
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout=2.0)


class _Gated(Tool):
    """执行到一半停下来等测试放行：给「轮的中间」这个时刻一个确定的观测点。"""

    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self._entered = entered
        self._release = release

    async def execute(self, text: str) -> ToolResult:
        """回显 text。"""
        self._entered.set()
        await self._release.wait()
        return ToolResult(output=text)


async def test_zombie_removes_the_mailbox_so_revival_has_to_go_through_activation():
    """spec §4.1：ZOMBIE 是「循环已退、队列已移除，但仍在注册表中」。"""
    runtime = Runtime()
    agent_id = await runtime.spawn(_factory([ScriptedTurn(content='done')]), query='go')
    await runtime.wait(agent_id)

    delivered = await runtime.broker.publish(
        agent_id, Message(sender='user', receiver=agent_id, content='hello')
    )

    assert delivered.ok is False
    assert delivered.reason is not None and '下一步' in delivered.reason
    await runtime.shutdown()


async def test_idle_exit_mode_runs_a_second_cycle_for_the_next_message():
    runtime = Runtime()
    script = [ScriptedTurn(content='first'), ScriptedTurn(content='second')]
    agent_id = await runtime.spawn(
        _factory(script), query='go', exit_mode=ExitMode.IDLE
    )
    await _wait_for_status(runtime, agent_id, AgentStatus.IDLE)

    await runtime.send(agent_id, 'and now the second thing')
    await _wait_for_status(runtime, agent_id, AgentStatus.IDLE)

    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert [block.content for block in agent.context.current_traj if block.role == 'user'] == [
        'go',
        'and now the second thing',
    ]
    await runtime.shutdown()


async def test_spawning_without_a_query_parks_the_loop_on_the_mailbox():
    """lazy boot：起来就在等消息，第一条消息才是它的第一个周期。"""
    runtime = Runtime()
    agent_id = await runtime.spawn(
        _factory([ScriptedTurn(content='woken')]), exit_mode=ExitMode.IDLE
    )
    await _wait_for_status(runtime, agent_id, AgentStatus.IDLE)
    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert agent.context.current_traj == []

    await runtime.send(agent_id, 'wake up')
    await _wait_for_status(runtime, agent_id, AgentStatus.IDLE)

    assert [block.role for block in agent.context.current_traj] == ['user', 'assistant']
    await runtime.shutdown()


async def test_deferred_message_waits_for_the_whole_cycle_then_starts_the_next_one():
    """spec §4.5：DEFERRED 的语义是「先把手上的事做完，然后再看这个」。"""
    entered = asyncio.Event()
    release = asyncio.Event()
    runtime = Runtime()
    script = [
        ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Gated', arguments='{"text":"hi"}')]),
        ScriptedTurn(content='first cycle done'),
        ScriptedTurn(content='second cycle done'),
    ]

    def build(hooks, events):
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM(script),
                tools={'Gated': _Gated(entered, release)},
                system_prompt='SP',
            ),
            hooks=hooks,
            events=events,
        )

    agent_id = await runtime.spawn(build, query='go')
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    await runtime.send(agent_id, 'read this afterwards', trigger=Trigger.DEFERRED)
    release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), timeout=2.0)

    assert [block.role for block in ctx.current_traj] == [
        'user',
        'assistant',
        'tool',
        'assistant',
        'user',
        'assistant',
    ]
    assert ctx.current_traj[4].content == 'read this afterwards'
    await runtime.shutdown()


async def test_max_turns_ends_the_event_loop_even_for_a_resident_agent():
    """预算耗尽的 Agent 对任何后续消息都做不了事，继续等只会在同一条消息上空转。"""
    runtime = Runtime()

    def build(hooks, events):
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM([ScriptedTurn(content='only turn')]),
                tools={},
                system_prompt='SP',
                max_turns=1,
            ),
            hooks=hooks,
            events=events,
        )

    agent_id = await runtime.spawn(build, query='go', exit_mode=ExitMode.IDLE)
    await runtime.wait(agent_id)
    await _wait_for_status(runtime, agent_id, AgentStatus.ZOMBIE)
    await runtime.shutdown()


async def test_wait_reports_the_driver_never_took_over_when_the_ownership_cas_was_refused():
    runtime = Runtime()
    agent_id = await runtime.spawn(
        _factory([ScriptedTurn(content='done')]), query='go', exit_mode=ExitMode.IDLE
    )
    await _wait_for_status(runtime, agent_id, AgentStatus.IDLE)
    await runtime.shutdown()

    second = Runtime(registry=runtime.registry, broker=runtime.broker)
    agent = runtime._table.get(agent_id)
    assert agent is not None
    with pytest.raises(ContractViolation):
        await second.wait(agent_id)
    await second.shutdown()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_runtime.py -v`
Expected: FAIL —— `spawn` 的 `query` 仍是必填；ZOMBIE 之后队列还在；没有第二个周期

- [ ] **Step 3: 重写 `runtime.py`**

```python
# src/syndica/kernel/runtime.py
"""Runtime：Agent 生命周期与控制面。

按操作系统进程模型设计：Runtime 类比 init 系统，Agent 类比进程。Agent 不管理自己的生命周期——
外层由本类驱动，内层只跑 LLM + 工具循环。

**驱动任务是一个事件循环，不是一次调用。** spec §4.1 把 IDLE 定义成「已注册，事件循环在等消息」，
所以一个 Agent 的常态是**等**，一条消息触发一个周期。周期与周期之间 Context 不重建、轮次不重置
（spec §5.1）——ZOMBIE 留在注册表可寻址，正是为了「一条消息就能让它带着上下文继续干」。

**只有一条消息消费路径。** 中途插话与唤醒都由同一道门（`BrokerTurnGate`）在轮开始前消费。两条
路径分开写必然分叉，而分叉出来的差异只在有人插话时才现身。

**三处状态转换都只走 CAS，且允许失败。** `_switch` 逐个试 CAS 而不是「读一次再写」：读与写之间
的窗口正是 CAS 存在的理由。失败不是错误——控制面上有别人（pause / abort / wait_for）合法地改过
它，此时保持注册表现有的说法比强行覆盖更诚实。

**`_finish` 先摘队列再改状态。** 这两步之间落进来的 `send` 会拿到 `ok=False`（队列没了，而按需
激活看到它还没进 ZOMBIE 因此不复活），调用方于是收到一次诚实的失败；反过来先改状态，那条消息会
成功投进一个即将消失的队列，静默丢掉。**丢数据是静默的、灾难性的；失败是显式的、可恢复的。**

**预算耗尽（MAX_TURNS）结束事件循环，即便 exit_mode 是 IDLE。** 一个已经用完轮次的 Agent 对任何
后续消息都产不出新的一轮，继续等下去只会被同一条消息反复唤醒又立刻返回——一个不让出控制权的
空转。

**取消不做收尾工作。** 驱动任务被取消时，`finally` 里既不改注册表也不摘资源：取消可能落在轮的
中间，那时轨迹里留着不配对的调用，而 spec 把「收尾」定义为一个**有状态**的动作。收尾由发起取消
的那一方（`kill`）在取消之外完成，因此它不必在一个正在被取消的任务里 await。

**`shutdown` 是终态。** 关闭后的事件总线静默丢弃 emit，此后 spawn 出来的 Agent 会照常干完活却完全
不可观测，因此 spawn 在关闭后报错。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from syndica.kernel.agent import BaseAgent
from syndica.kernel.agent_types import AgentInfo, AgentStatus, ExitMode
from syndica.kernel.context import Context, StopReason
from syndica.kernel.control import BrokerTurnGate, ControlBlock
from syndica.kernel.errors import ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.local_table import LocalAgentTable
from syndica.kernel.message import DeliveryResult, Message, Trigger
from syndica.kernel.protocols import AgentRegistry, MessageBroker
from syndica.kernel.stores import InMemoryAgentRegistry, InMemoryMessageBroker

__all__ = ['AgentFactory', 'Runtime']

logger = logging.getLogger(__name__)

AgentFactory = Callable[[HooksCenter, EventBus], BaseAgent]

# 进了这些状态就不该再被本进程的事件循环带回 RUNNING / IDLE。
_LIVE_STATUSES: tuple[AgentStatus, ...] = (
    AgentStatus.IDLE,
    AgentStatus.RUNNING,
    AgentStatus.PAUSED,
    AgentStatus.WAITING,
    AgentStatus.ABORTING,
)


@dataclass
class _Slot:
    """本进程为一个 Agent 额外持有的东西。

    实例与任务在 `LocalAgentTable`，可序列化状态在 `AgentRegistry`；这里放的是两边都不该管的
    第三样：它是怎么被造出来的（`factory`，`Fork` 要照着再造一个）、这轮工作结束后该去哪
    （`exit_mode`，复活时要按它重开循环）、控制块与门，以及**本进程的驱动任务到底接管过它没有**
    （`owned`——接管失败与从未 spawn 是两回事，`wait` 要分开答）。
    """

    factory: AgentFactory
    exit_mode: ExitMode
    control: ControlBlock
    gate: BrokerTurnGate
    owned: bool = False


def _already_shut_down() -> ContractViolation:
    return ContractViolation(
        expected='Runtime 尚未 shutdown，spawn 出来的 Agent 事件仍有订阅者能收到',
        received=(
            'shutdown() 已经跑过：事件总线已关闭，此后 emit 一律静默丢弃，'
            '这个 Agent 会照常干完活却完全不可观测'
        ),
        next_action='要再跑 Agent 就新建一个 Runtime；shutdown 是终态，同一个 Runtime 不重开',
    )


class Runtime:
    """Agent 的生命周期驱动与控制面。"""

    def __init__(
        self,
        *,
        registry: AgentRegistry | None = None,
        broker: MessageBroker | None = None,
    ) -> None:
        self.registry: AgentRegistry = registry or InMemoryAgentRegistry()
        self.broker: MessageBroker = broker or InMemoryMessageBroker()
        self.events: EventBus = EventBus()
        self.hooks: HooksCenter = HooksCenter()
        self._table: LocalAgentTable[BaseAgent] = LocalAgentTable()
        self._slots: dict[str, _Slot] = {}
        self._closed = False

    async def spawn(
        self,
        factory: AgentFactory,
        *,
        query: str | None = None,
        exit_mode: ExitMode = ExitMode.ZOMBIE,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """建一个 Agent 并启动它的事件循环，返回它的 agent_id。

        `query=None` 就是「起来等消息」（spec §4.7 的 lazy boot）：事件循环直接进 IDLE，第一条
        消息才是它的第一个周期。

        工厂收下本 Runtime 的 hooks 与事件总线：Agent 自己新建一份，宿主注册的切点与订阅就都落空了。
        """
        if self._closed:
            raise _already_shut_down()
        agent = factory(self.hooks, self.events)
        agent_id = agent.agent_id
        control = ControlBlock()
        gate = BrokerTurnGate(agent_id, self.broker, control)
        agent.attach_gate(gate)
        await self.registry.register(
            AgentInfo(
                agent_id=agent_id,
                name=agent.options.name,
                agent_type=type(agent).__name__,
                parent_id=parent_id,
                metadata=metadata or {},
            )
        )
        await self.broker.ensure_queue(agent_id)
        # put 先于 create_task：put 被拒时还没有任务可泄漏。两者之间没有挂起点，任务因此不会在
        # set_task 之前就跑起来。
        self._table.put(agent_id, agent)
        self._slots[agent_id] = _Slot(
            factory=factory, exit_mode=exit_mode, control=control, gate=gate
        )
        task = asyncio.create_task(self._drive(agent_id, agent, query), name=f'agent:{agent_id}')
        self._table.set_task(agent_id, task)
        return agent_id

    async def _drive(self, agent_id: str, agent: BaseAgent, query: str | None) -> None:
        # `get` 而不是下标：任务是 create_task 排上去的，在它真正跑起来之前 despawn 可能已经把
        # slot 摘掉了。那时该静默退出，而不是让一个 KeyError 变成无人认领的任务异常。
        slot = self._slots.get(agent_id)
        if slot is None:
            return
        if not await self.registry.compare_and_set_status(
            agent_id, expected=AgentStatus.IDLE, new=AgentStatus.RUNNING
        ):
            logger.error(
                '期望: agent %s 在注册表里是 IDLE，可由本进程接管。'
                ' 实际: IDLE -> RUNNING 被拒，它当前的状态不是 IDLE，说明已被别人拿走。'
                ' 下一步: 用 find() 读它的 status 与 owner；本进程不跑它，也不清理它。',
                agent_id,
            )
            return
        slot.owned = True
        cancelled = False
        try:
            await self._cycles(agent_id, agent, query, slot)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            logger.exception(
                '期望: agent %s 的事件循环正常结束。'
                ' 实际: 它以异常结束，Context 的 stop_reason 已置为 error，循环不再继续。'
                ' 下一步: 读 wait() 交回的 Context 定位最后一轮；'
                '要让它自愈就挂一个 llm_failure / post_tool_use hook。',
                agent_id,
            )
        finally:
            if not cancelled:
                await self._finish(agent_id, slot)

    async def _cycles(
        self, agent_id: str, agent: BaseAgent, query: str | None, slot: _Slot
    ) -> None:
        """IDLE↔RUNNING：等一条消息，跑一个周期，再等下一条。"""
        while True:
            if query is None and slot.control.deferred_count == 0:
                await self._switch(
                    agent_id,
                    new=AgentStatus.IDLE,
                    allowed=(AgentStatus.RUNNING, AgentStatus.ABORTING),
                )
                # timeout=None 是刻意的：这个循环的退出路径是取消（kill / shutdown）与队列消失
                # （despawn），不是超时。答 False 只有一个原因——邮箱已经被摘掉了。
                if not await self.broker.wait_for_messages(agent_id, None):
                    return
            if await slot.control.hold_until_runnable():
                # 中止落在周期之间：这一轮不开始，位已消费，回去继续等。
                query = None
                continue
            await self._switch(
                agent_id,
                new=AgentStatus.RUNNING,
                allowed=(AgentStatus.IDLE, AgentStatus.ABORTING),
            )
            slot.gate.begin_cycle()
            slot.control.enter_run()
            try:
                await agent.run(query)
            finally:
                slot.control.leave_run()
            query = None
            if agent.context.stop_reason is StopReason.MAX_TURNS:
                return
            if slot.exit_mode is not ExitMode.IDLE and slot.control.deferred_count == 0:
                return

    async def _switch(
        self, agent_id: str, *, new: AgentStatus, allowed: tuple[AgentStatus, ...]
    ) -> bool:
        """把状态 CAS 到 `new`；当前已经是 `new` 也算成功（后置条件已成立）。

        逐个试 CAS 而不是「读一次再决定写什么」：读与写之间的窗口正是 CAS 存在的理由，远端实现
        下另一个执行体可以在这个窗口里改掉状态，而那次转换会被无声吞掉。
        """
        for expected in (new, *allowed):
            if await self.registry.compare_and_set_status(
                agent_id, expected=expected, new=new
            ):
                return True
        return False

    async def _finish(self, agent_id: str, slot: _Slot) -> None:
        """事件循环退出后的归宿（spec §4.1 的 ExitMode）。"""
        if slot.exit_mode is ExitMode.EXIT:
            await self.broker.remove_queue(agent_id)
            await self.registry.unregister(agent_id)
            self._table.pop(agent_id)
            self._slots.pop(agent_id, None)
            return
        await self.broker.remove_queue(agent_id)
        if not await self._switch(agent_id, new=AgentStatus.ZOMBIE, allowed=_LIVE_STATUSES):
            logger.error(
                '期望: agent %s 在注册表里仍是本进程认得的活状态，可由本进程转去 zombie。'
                ' 实际: 转换被拒，它的状态在事件循环退出前被别人改成了别的东西。'
                ' 下一步: 用 find() 读它当前的状态；本进程已经不再驱动它。',
                agent_id,
            )

    async def wait(self, agent_id: str) -> Context:
        """类比 waitpid：等事件循环结束，交回它的轨迹。

        交回的是 Agent 自己那份 Context，不是快照：ZOMBIE 靠一条消息复活时续用的就是它，拿到副本
        的调用方会与 Agent 将来读写的那一份静默分叉。

        **`ExitMode.IDLE` 的 Agent 事件循环常驻**，因此 `wait` 只在它被 kill / despawn / shutdown
        之后才返回——对守护型 Agent 调 `wait` 等于故意把自己挂住。

        Agent 的异常与取消都是**被等待者**的终态，不冒充成本次调用自己的失败。
        """
        task = self._table.get_task(agent_id)
        if task is not None:
            await asyncio.wait({task})
        slot = self._slots.get(agent_id)
        agent = self._table.get(agent_id)
        if slot is None or agent is None:
            raise self._nothing_to_wait_for(agent_id)
        if not slot.owned:
            raise ContractViolation(
                expected=f'agent {agent_id!r} 的事件循环由本进程接管过',
                received=(
                    '本进程 spawn 过它，但驱动任务没有接管成功：'
                    'IDLE -> RUNNING 的转换被拒（它已被别人拿走），或任务在这之前就被取消'
                ),
                next_action='用 find() 读它当前的 status 与 owner；确认它空闲后重新 spawn 一个',
            )
        return agent.context

    def _nothing_to_wait_for(self, agent_id: str) -> ContractViolation:
        return ContractViolation(
            expected=f'agent {agent_id!r} 由本进程 spawn，且结束后仍留在本进程里',
            received=(
                f'本进程没有它的运行记录（已知: {sorted(self._slots)}）：'
                '要么它不是本进程 spawn 的，'
                '要么它以 ExitMode.EXIT 结束或被 despawn，已连同 Context 一起被销毁'
            ),
            next_action=(
                '确认 agent_id 是 spawn 的返回值；'
                '要在结束后读轨迹就用 ExitMode.ZOMBIE 或 ExitMode.IDLE spawn；'
                '若它由其他进程持有，本进程无法 wait 它'
            ),
        )

    async def send(
        self, agent_id: str, content: str, *, trigger: Trigger = Trigger.PENDING
    ) -> DeliveryResult:
        """投递一条消息到目标邮箱。

        可寻址性以注册表为准，不以邮箱在不在为准：残留邮箱（远端 broker 的 remove_queue 失败、
        或 ensure_queue 之后注册失败）会让投递看起来成功，而再没有人会去消费它。
        """
        if await self.registry.get(agent_id) is None:
            return DeliveryResult(
                ok=False,
                reason=(
                    f'期望: agent {agent_id!r} 已注册，因而可寻址。'
                    ' 实际: 注册表里没有这个条目。'
                    ' 下一步: 先 spawn 它；以 ExitMode.EXIT 结束或被 despawn 的 Agent 已被销毁，'
                    '只能重新 spawn。'
                ),
            )
        return await self.broker.publish(
            agent_id,
            Message(sender='runtime', receiver=agent_id, content=content, trigger=trigger),
        )

    async def find(self, agent_id: str) -> AgentInfo | None:
        """只读路径，绝不激活任何 Agent。"""
        return await self.registry.get(agent_id)

    async def shutdown(self) -> None:
        """取消所有事件循环，**等它们收完尾**再关事件总线。幂等，且是终态：此后 spawn 报错。"""
        # 置位在 gather 之前：与本次关机并发的 spawn 既赶不上取消名单、事件又发不出去。
        self._closed = True
        tasks = [
            task
            for agent_id in self._table.agent_ids()
            if (task := self._table.get_task(agent_id)) is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.events.close()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests -v && uv run ruff check . && uv run pyright && uv run python scripts/check_deps.py`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/runtime.py tests/kernel/test_runtime.py
git commit -m "feat(kernel): 驱动任务从「调一次 run」变成事件循环

spec §4.1 把 IDLE 定义成「已注册，事件循环在等消息」，所以一个 Agent 的常态是等，
一条消息触发一个周期。计划 1 里 _drive 调一次 run 就结束，那个定义根本无处落地。

四处刻意的设计：
- ZOMBIE 现在真的摘掉队列。计划 1 保留邮箱是因为按需激活还没接线，此刻不摘就一条
  消息都收不到；接线之后再不摘，就与 §4.1 的定义分叉了
- _finish 先摘队列再改状态。这两步之间的 send 会拿到 ok=False，调用方收到诚实的
  失败；反过来那条消息会成功投进一个即将消失的队列，静默丢掉
- MAX_TURNS 结束事件循环，即便 exit_mode 是 IDLE。预算耗尽的 Agent 对任何后续消息
  都产不出新的一轮，继续等只会被同一条消息反复唤醒又立刻返回，是不让出控制权的空转
- 取消不做收尾。取消可能落在轮中间，而收尾是有状态的动作；它归发起取消的那一方，
  因此不必在一个正在被取消的任务里 await

_switch 逐个试 CAS 而不是读一次再写：读写之间的窗口正是 CAS 存在的理由。"
```

---

## Task 4: `ensure_active` 与真正会激活的 `send`

**Files:**
- Modify: `src/syndica/kernel/runtime.py`
- Test: `tests/kernel/test_runtime.py`

**Interfaces:**
- Consumes: Task 3 的 `_Slot` / `_switch` / `_drive`
- Produces:
  - `Runtime.ensure_active(agent_id) -> bool`：唯一的激活入口（spec §4.2）
  - `Runtime.send(agent_id, content, *, trigger, activate=True, sender='runtime', correlation_id=None)`
  - `on_missing_queue` 接线：一条消息真的能复活一个 ZOMBIE

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_runtime.py  （追加）
async def test_a_message_revives_a_zombie_with_its_context_intact():
    """ZOMBIE 留在注册表可寻址，就是为了「一条消息带着上下文继续干」（spec §4.1）。"""
    runtime = Runtime()
    script = [ScriptedTurn(content='first'), ScriptedTurn(content='second')]
    agent_id = await runtime.spawn(_factory(script), query='go')
    await runtime.wait(agent_id)
    await _wait_for_status(runtime, agent_id, AgentStatus.ZOMBIE)

    delivered = await runtime.send(agent_id, 'keep going')

    assert delivered.ok is True
    ctx = await asyncio.wait_for(runtime.wait(agent_id), timeout=2.0)
    assert [block.content for block in ctx.current_traj if block.role == 'user'] == [
        'go',
        'keep going',
    ]
    await runtime.shutdown()


async def test_send_without_activation_neither_revives_nor_silently_succeeds():
    runtime = Runtime()
    agent_id = await runtime.spawn(_factory([ScriptedTurn(content='done')]), query='go')
    await runtime.wait(agent_id)
    await _wait_for_status(runtime, agent_id, AgentStatus.ZOMBIE)

    delivered = await runtime.send(agent_id, 'queue only', activate=False)

    assert delivered.ok is False
    info = await runtime.find(agent_id)
    assert info is not None and info.status is AgentStatus.ZOMBIE
    await runtime.shutdown()


async def test_suppressing_activation_on_one_send_does_not_leak_to_a_concurrent_one():
    """抑制位是按调用的：两个并发 send 各自的意图不得互相污染。"""
    runtime = Runtime()
    first = await runtime.spawn(_factory([ScriptedTurn(content='a')]), query='go')
    second = await runtime.spawn(_factory([ScriptedTurn(content='b')]), query='go')
    await runtime.wait(first)
    await runtime.wait(second)
    await _wait_for_status(runtime, first, AgentStatus.ZOMBIE)
    await _wait_for_status(runtime, second, AgentStatus.ZOMBIE)

    quiet, loud = await asyncio.gather(
        runtime.send(first, 'queue only', activate=False),
        runtime.send(second, 'wake up'),
    )

    assert quiet.ok is False
    assert loud.ok is True
    await runtime.shutdown()


async def test_ensure_active_is_a_question_and_answers_false_for_an_unknown_agent():
    runtime = Runtime()
    assert await runtime.ensure_active('nope') is False
    await runtime.shutdown()


async def test_ensure_active_on_a_running_agent_reports_the_postcondition_already_holds():
    runtime = Runtime()
    agent_id = await runtime.spawn(
        _factory([ScriptedTurn(content='done')]), exit_mode=ExitMode.IDLE
    )
    await _wait_for_status(runtime, agent_id, AgentStatus.IDLE)
    assert await runtime.ensure_active(agent_id) is True
    await runtime.shutdown()


async def test_find_still_never_activates():
    """只读路径与激活路径必须分离，否则查询类工具无法把「不存在」与「存在但未运行」分开答。"""
    runtime = Runtime()
    agent_id = await runtime.spawn(_factory([ScriptedTurn(content='done')]), query='go')
    await runtime.wait(agent_id)
    await _wait_for_status(runtime, agent_id, AgentStatus.ZOMBIE)

    info = await runtime.find(agent_id)

    assert info is not None and info.status is AgentStatus.ZOMBIE
    delivered = await runtime.broker.publish(
        agent_id, Message(sender='user', receiver=agent_id, content='x')
    )
    assert delivered.ok is False
    await runtime.shutdown()


async def test_ensure_active_refuses_after_shutdown():
    runtime = Runtime()
    agent_id = await runtime.spawn(_factory([ScriptedTurn(content='done')]), query='go')
    await runtime.wait(agent_id)
    await runtime.shutdown()
    assert await runtime.ensure_active(agent_id) is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_runtime.py -v`
Expected: FAIL —— `Runtime` 没有 `ensure_active`；`send` 不接受 `activate`

- [ ] **Step 3: 接线**

在 import 块补 `from contextvars import ContextVar`（与既有 stdlib 块合并），并在 `logger` 之后加：

```python
# 「这一次 send 允不允许把目标叫醒」。用 ContextVar 而不是 Runtime 上的一个字段或集合：
# publish -> on_missing_queue 回调是同一个任务里的直接 await 链，因此 ContextVar 恰好是**按调用**
# 的作用域，两个并发 send 各自的意图互不污染。放在实例上则第二个 send 会读到第一个的开关。
_ACTIVATION_ALLOWED: ContextVar[bool] = ContextVar('syndica_activation_allowed', default=True)
```

`__init__` 末尾接上回调：

```python
        self._closed = False
        # 「队列不存在」是懒启动与 ZOMBIE 复活的唯一触发点（spec §4.4）。接在这里，
        # 而不是让每个调用方自己先判断再激活——那样「复活」就有了 N 份实现。
        self.broker.on_missing_queue(self._on_missing_queue)
```

新增两个方法（放在 `find` 之前）：

```python
    async def ensure_active(self, agent_id: str) -> bool:
        """唯一的激活入口：复活 ZOMBIE、重开事件循环（spec §4.2）。

        复活、重开、从落盘会话恢复、恢复子会话在语义上是同一件事，不该暴露成四个方法让用户猜。

        返回 True 表示「此刻它有一个活着的事件循环」，False 表示本进程做不到——**两者都是回答，
        都不抛异常**（问句只答不抛）。答 False 的三种原因各自记日志：注册表里没有它、本进程手里
        没有它的实例（跨进程持有，或会话恢复能力还不存在）、它正在收尾。
        """
        if self._closed:
            return False
        info = await self.registry.get(agent_id)
        if info is None:
            return False
        slot = self._slots.get(agent_id)
        agent = self._table.get(agent_id)
        if slot is None or agent is None:
            logger.error(
                '期望: agent %s 的实例在本进程的实例表里，可以直接重开它的事件循环。'
                ' 实际: 注册表里有它，本进程手里没有——它由别的进程持有，'
                '或者只剩落盘会话而恢复能力尚未实现。'
                ' 下一步: 在持有它的进程上激活它；'
                '要在本进程续跑就重新 spawn 一个并把会话交给它。',
                agent_id,
            )
            return False
        task = self._table.get_task(agent_id)
        if task is not None and not task.done():
            return True
        if info.status is AgentStatus.ABORTING:
            return False
        if not await self._switch(
            agent_id, new=AgentStatus.IDLE, allowed=(AgentStatus.ZOMBIE,)
        ):
            return False
        await self.broker.ensure_queue(agent_id)
        # 复活的循环没有 query：它要干什么由那条把它叫醒的消息说，门会在第一轮开始前消费它。
        revived = asyncio.create_task(
            self._drive(agent_id, agent, None), name=f'agent:{agent_id}'
        )
        # 已结束的任务可以被替换，未结束的不行——那正是上面 task.done() 那一支挡住的情况。
        self._table.set_task(agent_id, revived)
        return True

    async def _on_missing_queue(self, agent_id: str) -> bool:
        if not _ACTIVATION_ALLOWED.get():
            return False
        return await self.ensure_active(agent_id)
```

`send` 换成带激活开关的版本：

```python
    async def send(
        self,
        agent_id: str,
        content: str,
        *,
        trigger: Trigger = Trigger.PENDING,
        activate: bool = True,
        sender: str = 'runtime',
        correlation_id: str | None = None,
    ) -> DeliveryResult:
        """投递一条消息到目标邮箱。

        **默认激活目标**：绝大多数调用方要的语义是「发消息，它没在跑就叫醒它」（spec §4.2）。
        要严格的「仅入队」语义就传 `activate=False`——此时目标若已经进了 ZOMBIE（队列已摘），
        投递返回 `ok=False`，而不是把消息塞进一个没人消费的地方。

        **投递失败返回结构化结果而不抛异常**，调用方才能识别可重试的瞬时错误。

        可寻址性以注册表为准，不以邮箱在不在为准：残留邮箱（远端 broker 的 remove_queue 失败、
        或 ensure_queue 之后注册失败）会让投递看起来成功，而再没有人会去消费它。
        """
        if await self.registry.get(agent_id) is None:
            return DeliveryResult(
                ok=False,
                reason=(
                    f'期望: agent {agent_id!r} 已注册，因而可寻址。'
                    ' 实际: 注册表里没有这个条目。'
                    ' 下一步: 先 spawn 它；以 ExitMode.EXIT 结束或被 despawn 的 Agent 已被销毁，'
                    '只能重新 spawn。'
                ),
            )
        message = Message(
            sender=sender,
            receiver=agent_id,
            content=content,
            trigger=trigger,
            correlation_id=correlation_id,
        )
        token = _ACTIVATION_ALLOWED.set(activate)
        try:
            return await self.broker.publish(agent_id, message)
        finally:
            _ACTIVATION_ALLOWED.reset(token)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests -v && uv run ruff check . && uv run pyright`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/runtime.py tests/kernel/test_runtime.py
git commit -m "feat(kernel): 一条消息真的能复活一个 ZOMBIE

on_missing_queue 从计划 1 起就存在，但没有任何人接它，于是 ZOMBIE「一条消息即可
继续工作」这条存在理由一直只是文档。接上 ensure_active 之后它才成立，spawn(query=None)
的 lazy boot 也才有意义。

ensure_active 是唯一的激活入口：复活、重开、从落盘会话恢复、恢复子会话在语义上是
同一件事，暴露成四个方法只会让用户猜。它是问句，因此三种做不到的情形都答 False
并各自记日志，而不是抛异常。

activate=False 用 ContextVar 而不是 Runtime 上的字段：publish 到回调是同一个任务里
的直接 await 链，ContextVar 恰好是按调用的作用域；放实例上则两个并发 send 会互相
读到对方的开关。"
```

---
## Task 5: `pause` / `resume`（含 `recursive`）

**Files:**
- Modify: `src/syndica/kernel/runtime.py`
- Test: `tests/kernel/test_runtime.py`

**Interfaces:**
- Consumes: Task 1 的 `ControlBlock`，Task 3 的 `_Slot` / `_switch`
- Produces:
  - `Runtime.pause(agent_id, *, recursive=False)` / `Runtime.resume(agent_id, *, recursive=False)`
  - `Runtime._descendants(agent_id) -> list[str]`：广度优先、带环保护的子代遍历，pause / abort / despawn 三条路共用

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_runtime.py  （追加）
async def _resident(runtime: Runtime, *, parent_id: str | None = None) -> str:
    """起一个常驻 Agent 并等它进 IDLE。脚本给足三轮，够本文件里任何一个用例用。"""
    agent_id = await runtime.spawn(
        _factory([ScriptedTurn(content=f'turn-{index}') for index in range(3)]),
        exit_mode=ExitMode.IDLE,
        parent_id=parent_id,
    )
    await _wait_for_status(runtime, agent_id, AgentStatus.IDLE)
    return agent_id


async def test_a_paused_agent_does_not_consume_its_next_message_and_resume_does_not_lose_it():
    runtime = Runtime()
    agent_id = await _resident(runtime)
    agent = runtime._table.get(agent_id)
    assert agent is not None

    await runtime.pause(agent_id)
    await runtime.send(agent_id, 'arrived while paused')
    control = runtime._slots[agent_id].control
    assert await control.wait_until_parked(timeout=2.0) is True
    assert agent.context.current_traj == []

    await runtime.resume(agent_id)
    await _wait_for_status(runtime, agent_id, AgentStatus.IDLE)

    assert [block.content for block in agent.context.current_traj if block.role == 'user'] == [
        'arrived while paused'
    ]
    await runtime.shutdown()


async def test_pause_is_idempotent_and_resume_puts_an_idle_agent_back_to_idle():
    runtime = Runtime()
    agent_id = await _resident(runtime)
    await runtime.pause(agent_id)
    await runtime.pause(agent_id)
    info = await runtime.find(agent_id)
    assert info is not None and info.status is AgentStatus.PAUSED

    await runtime.resume(agent_id)
    info = await runtime.find(agent_id)
    assert info is not None and info.status is AgentStatus.IDLE
    await runtime.shutdown()


async def test_pausing_a_zombie_is_refused_because_the_bit_would_be_lost_on_revival():
    runtime = Runtime()
    agent_id = await runtime.spawn(_factory([ScriptedTurn(content='done')]), query='go')
    await runtime.wait(agent_id)
    await _wait_for_status(runtime, agent_id, AgentStatus.ZOMBIE)
    with pytest.raises(ContractViolation):
        await runtime.pause(agent_id)
    await runtime.shutdown()


async def test_recursive_pause_reaches_every_descendant():
    """三代四个 Agent：基数 3 以上，且遍历顺序既非升序亦非降序。"""
    runtime = Runtime()
    root = await _resident(runtime)
    first_child = await _resident(runtime, parent_id=root)
    second_child = await _resident(runtime, parent_id=root)
    grandchild = await _resident(runtime, parent_id=first_child)

    await runtime.pause(root, recursive=True)

    for agent_id in (root, first_child, second_child, grandchild):
        info = await runtime.find(agent_id)
        assert info is not None and info.status is AgentStatus.PAUSED
    await runtime.shutdown()


async def test_a_descendant_that_cannot_be_paused_is_skipped_without_stopping_the_walk():
    """递归是尽力而为的：为第一个冲突抛异常只会留下一棵改了一半的树。"""
    runtime = Runtime()
    root = await _resident(runtime)
    finished = await runtime.spawn(
        _factory([ScriptedTurn(content='done')]), query='go', parent_id=root
    )
    await runtime.wait(finished)
    await _wait_for_status(runtime, finished, AgentStatus.ZOMBIE)
    still_running = await _resident(runtime, parent_id=root)

    await runtime.pause(root, recursive=True)

    assert (await runtime.find(finished)).status is AgentStatus.ZOMBIE
    assert (await runtime.find(still_running)).status is AgentStatus.PAUSED
    await runtime.shutdown()


async def test_recursive_resume_reaches_every_descendant():
    runtime = Runtime()
    root = await _resident(runtime)
    child = await _resident(runtime, parent_id=root)
    grandchild = await _resident(runtime, parent_id=child)
    await runtime.pause(root, recursive=True)

    await runtime.resume(root, recursive=True)

    for agent_id in (root, child, grandchild):
        info = await runtime.find(agent_id)
        assert info is not None and info.status is AgentStatus.IDLE
    await runtime.shutdown()


async def test_pausing_an_unknown_agent_says_what_to_do_instead_of_raising_keyerror():
    runtime = Runtime()
    with pytest.raises(ContractViolation) as info:
        await runtime.pause('nope')
    assert '下一步' in str(info.value)
    await runtime.shutdown()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_runtime.py -v`
Expected: FAIL —— `Runtime` 没有 `pause` / `resume`

- [ ] **Step 3: 加上挂起与恢复**

在 `find` 之前插入这一组方法：

```python
    async def pause(self, agent_id: str, *, recursive: bool = False) -> None:
        """挂起：让它在下一个轮的边界上停下来（spec §5.1 第 2 步），注册表转成 PAUSED。

        挂起的那段时间**不计入任何一轮**——门在计数之前，因此挂起既不消耗预算，也不会把一轮切成
        两半。

        允许从 IDLE / RUNNING / WAITING 进入；已经是 PAUSED 是成功（后置条件已成立）。ZOMBIE 与
        ABORTING 报错：前者没有事件循环可停，挂起位会在复活时静默丢掉；后者正在收尾，挂起一个
        正在拆的东西没有可表达的意图。

        `recursive=True` 时**父先于子**：先让父停下来，它才不会在我们走这棵树的时候再生出新的
        子代。子代逐个尽力而为——它们可能是 ZOMBIE、可能正在被拆——**不抛异常，只记日志**：递归
        操作天然是尽力而为的，为第一个冲突抛异常只会留下一棵改了一半的树，而调用方无从得知改到
        哪了。**被点名的那一个照常抛**，它的意图没有歧义。
        """
        await self._pause_one(agent_id, named=True)
        if recursive:
            for descendant in await self._descendants(agent_id):
                await self._pause_one(descendant, named=False)

    async def resume(self, agent_id: str, *, recursive: bool = False) -> None:
        """解除挂起，注册表从 PAUSED 转回它该在的地方。

        **回到哪里由控制块说了算**：它在一次 `run()` 之内就回 RUNNING，否则回 IDLE。只有持有
        事件循环的进程知道这件事，而注册表上没有「挂起之前是什么」——记一份就多出一个必须与真相
        保持一致的东西，且它在挂起期间还会被 `wait_for` 改。

        `recursive=True` 时**子先于父**：父恢复之后可能立刻要用子代，先恢复子代，它就不会看见
        一棵只恢复了一半的树。
        """
        if recursive:
            for descendant in reversed(await self._descendants(agent_id)):
                await self._resume_one(descendant, named=False)
        await self._resume_one(agent_id, named=True)

    async def _pause_one(self, agent_id: str, *, named: bool) -> None:
        slot, info = await self._control_target('pause', agent_id, named=named)
        if slot is None or info is None:
            return
        if info.status in (AgentStatus.ZOMBIE, AgentStatus.ABORTING):
            self._refuse_control(
                'pause',
                agent_id,
                named=named,
                received=f'它当前是 {info.status.value}',
                next_action=(
                    'ZOMBIE 没有事件循环可停，挂起位会在它被一条消息复活时静默丢掉；'
                    'ABORTING 正在收尾。要让它彻底不再被叫醒就用 despawn'
                ),
            )
            return
        # 先落注册表再落门：记不下 PAUSED 就不要真的停——那会停在一个谁都看不见的地方。
        if not await self._switch(
            agent_id,
            new=AgentStatus.PAUSED,
            allowed=(AgentStatus.IDLE, AgentStatus.RUNNING, AgentStatus.WAITING),
        ):
            self._refuse_control(
                'pause',
                agent_id,
                named=named,
                received='转到 PAUSED 的 CAS 被拒，它的状态刚刚被别人改过',
                next_action='用 find() 重读它的状态再决定；不要在读到的旧状态上重试',
            )
            return
        slot.control.pause()

    async def _resume_one(self, agent_id: str, *, named: bool) -> None:
        slot, info = await self._control_target('resume', agent_id, named=named)
        if slot is None or info is None:
            return
        slot.control.resume()
        back_to = AgentStatus.RUNNING if slot.control.in_run else AgentStatus.IDLE
        await self._switch(agent_id, new=back_to, allowed=(AgentStatus.PAUSED,))

    async def _control_target(
        self, operation: str, agent_id: str, *, named: bool
    ) -> tuple[_Slot | None, AgentInfo | None]:
        """取出控制面操作的目标；取不到时按 named 决定抛还是记日志。"""
        slot = self._slots.get(agent_id)
        info = await self.registry.get(agent_id)
        if slot is not None and info is not None:
            return slot, info
        self._refuse_control(
            operation,
            agent_id,
            named=named,
            received=(
                '注册表里没有这个条目'
                if info is None
                else '本进程没有它的实例——它由别的进程持有'
            ),
            next_action=(
                '确认 agent_id 是 spawn 的返回值；'
                '被 despawn 或以 ExitMode.EXIT 结束的 Agent 已被销毁，只能重新 spawn。'
                '若它由其他进程持有，在那个进程上操作它'
            ),
        )
        return None, None

    def _refuse_control(
        self, operation: str, agent_id: str, *, named: bool, received: str, next_action: str
    ) -> None:
        """点名的目标抛异常，遍历到的子代只记日志。"""
        if not named:
            logger.info(
                '%s 跳过子代 %s：%s。这是尽力而为的遍历，不因此中断。', operation, agent_id, received
            )
            return
        raise ContractViolation(
            expected=f'agent {agent_id!r} 处在可以 {operation} 的状态',
            received=received,
            next_action=next_action,
        )

    async def _descendants(self, agent_id: str) -> list[str]:
        """广度优先列出全部子代，浅的在前。

        `visited` 不是防御性编程：`children_of` 读的是注册表，而注册表可以是远端的、可以被别的
        进程写过；一个环会让这个遍历永不返回，而它是 pause / abort / despawn 三条路的公共前置。
        """
        found: list[str] = []
        visited = {agent_id}
        frontier = [agent_id]
        while frontier:
            following: list[str] = []
            for parent in frontier:
                for child in await self.registry.children_of(parent):
                    if child in visited:
                        continue
                    visited.add(child)
                    found.append(child)
                    following.append(child)
            frontier = following
        return found
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests -v && uv run ruff check . && uv run pyright`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/runtime.py tests/kernel/test_runtime.py
git commit -m "feat(kernel): 挂起停在「有消息」与「取走消息」之间

一个被挂起的 Agent 若在 wait_for_messages 醒来后照常 consume，那条消息就离开了邮箱
而没有人处理它——resume 之后它已经不在了。所以门必须先过挂起检查再消费，消息因此
一直留在邮箱里，resume 不会丢。

三处顺序上的裁决：
- 先落注册表再落门。记不下 PAUSED 就不要真的停，否则它停在一个谁都看不见的地方
- pause 父先于子（先掐掉还会生出新子代的那一头），resume 子先于父（父恢复后可能
  立刻要用子代，先恢复子代它就不会看见一棵只恢复了一半的树）
- 递归对子代尽力而为、只记日志，对点名的那一个照常抛。为第一个冲突抛异常会留下
  一棵改了一半的树，而调用方无从得知改到哪了

resume 回到 RUNNING 还是 IDLE 由控制块的 in_run 说了算：注册表上不记「挂起之前是
什么」，记一份就多出一个必须与真相保持一致的东西。"
```

---

## Task 6: `abort`——协作式中止

**Files:**
- Modify: `src/syndica/kernel/runtime.py`
- Test: `tests/kernel/test_runtime.py`

**Interfaces:**
- Consumes: Task 1 的中止位、Task 2 的门、Task 5 的 `_descendants` / `_control_target`
- Produces: `Runtime.abort(agent_id, *, recursive=False)`

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_runtime.py  （追加）
async def test_abort_ends_the_current_cycle_with_a_consistent_trajectory():
    """协作式中止兑现在轮与轮之间，因此轨迹配对完整、可以直接 resume。"""
    entered = asyncio.Event()
    release = asyncio.Event()
    runtime = Runtime()
    script = [
        ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Gated', arguments='{"text":"hi"}')]),
        ScriptedTurn(content='never reached'),
    ]

    def build(hooks, events):
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM(script),
                tools={'Gated': _Gated(entered, release)},
                system_prompt='SP',
            ),
            hooks=hooks,
            events=events,
        )

    agent_id = await runtime.spawn(build, query='go')
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    await runtime.abort(agent_id)
    release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), timeout=2.0)

    assert ctx.stop_reason is StopReason.ABORTED
    assert [block.role for block in ctx.current_traj] == ['user', 'assistant', 'tool']
    await runtime.shutdown()


async def test_abort_pierces_a_pause_so_the_caller_is_not_left_without_feedback():
    runtime = Runtime()
    agent_id = await _resident(runtime)
    await runtime.pause(agent_id)
    await runtime.send(agent_id, 'wake up')
    control = runtime._slots[agent_id].control
    assert await control.wait_until_parked(timeout=2.0) is True

    await runtime.abort(agent_id)

    await _wait_for_status(runtime, agent_id, AgentStatus.IDLE)
    await runtime.shutdown()


async def test_aborting_an_idle_agent_is_refused_and_points_at_the_right_tool():
    """手上没有活就没有「停下手上这一件事」可言；那时想要的多半是 kill / despawn。"""
    runtime = Runtime()
    agent_id = await _resident(runtime)
    with pytest.raises(ContractViolation) as info:
        await runtime.abort(agent_id)
    assert 'kill' in str(info.value) or 'despawn' in str(info.value)
    await runtime.shutdown()


async def test_abort_is_consumed_once_so_the_next_message_still_gets_a_cycle():
    entered = asyncio.Event()
    release = asyncio.Event()
    runtime = Runtime()
    script = [
        ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Gated', arguments='{"text":"hi"}')]),
        ScriptedTurn(content='aborted cycle tail'),
        ScriptedTurn(content='second cycle'),
    ]

    def build(hooks, events):
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM(script),
                tools={'Gated': _Gated(entered, release)},
                system_prompt='SP',
            ),
            hooks=hooks,
            events=events,
        )

    agent_id = await runtime.spawn(build, query='go', exit_mode=ExitMode.IDLE)
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    await runtime.abort(agent_id)
    release.set()
    await _wait_for_status(runtime, agent_id, AgentStatus.IDLE)

    await runtime.send(agent_id, 'try again')
    await _wait_for_status(runtime, agent_id, AgentStatus.IDLE)

    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert agent.context.stop_reason is StopReason.COMPLETED
    await runtime.shutdown()


async def test_recursive_abort_reaches_running_descendants_and_skips_idle_ones():
    runtime = Runtime()
    entered = asyncio.Event()
    release = asyncio.Event()
    root = await _resident(runtime)
    idle_child = await _resident(runtime, parent_id=root)

    def build(hooks, events):
        return BaseAgent(
            AgentOptions(
                name='Busy',
                llm=ScriptedLLM(
                    [
                        ScriptedTurn(
                            tool_calls=[ToolCall(id='c1', name='Gated', arguments='{"text":"x"}')]
                        ),
                        ScriptedTurn(content='tail'),
                    ]
                ),
                tools={'Gated': _Gated(entered, release)},
                system_prompt='SP',
            ),
            hooks=hooks,
            events=events,
        )

    busy_child = await runtime.spawn(build, query='work', parent_id=root)
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    await runtime.abort(root, recursive=True)

    release.set()
    ctx = await asyncio.wait_for(runtime.wait(busy_child), timeout=2.0)
    assert ctx.stop_reason is StopReason.ABORTED
    assert (await runtime.find(idle_child)).status is AgentStatus.IDLE
    await runtime.shutdown()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_runtime.py -v`
Expected: FAIL —— `Runtime` 没有 `abort`

- [ ] **Step 3: 加上协作式中止**

在 `resume` 之后插入：

```python
    async def abort(self, agent_id: str, *, recursive: bool = False) -> None:
        """协作式中止：置位中止位、把状态转成 ABORTING，然后立刻返回。

        中止**在轮与轮之间兑现**（spec §5.1 第 1 步）：这一次工作以 `StopReason.ABORTED` 收场，
        轨迹配对完整、可以直接 resume。要立刻停下、不惜留下不配对的调用，那是 `kill`——两者的
        区别正是 `ABORTED` 存在的理由，不要合并。

        位是**一次性**的：这一个周期结束，事件循环照常回去等下一条消息（`exit_mode` 决定它还在
        不在）。abort 是「停下手上这一件事」，不是「从此不再干活」。

        只允许从 RUNNING / WAITING / PAUSED 发起——**手上没有活就没有「停下手上这一件事」可言**。
        IDLE / ZOMBIE 报错并指向 `kill` / `despawn`：调用方在那两种状态下想要的多半是「别再理它」，
        而那是另一件事。

        对 PAUSED 也允许，且中止会**穿透挂起门**：先挂起再中止是真实顺序，若中止要等解除挂起才被
        看见，这个 Agent 会永远停在那里而调用方毫无反馈。

        `recursive=True` 时父先于子，理由同 `pause`：先掐掉还会派出新活的那一头。
        """
        await self._abort_one(agent_id, named=True)
        if recursive:
            for descendant in await self._descendants(agent_id):
                await self._abort_one(descendant, named=False)

    async def _abort_one(self, agent_id: str, *, named: bool) -> None:
        slot, info = await self._control_target('abort', agent_id, named=named)
        if slot is None or info is None:
            return
        if not await self._switch(
            agent_id,
            new=AgentStatus.ABORTING,
            allowed=(AgentStatus.RUNNING, AgentStatus.WAITING, AgentStatus.PAUSED),
        ):
            self._refuse_control(
                'abort',
                agent_id,
                named=named,
                received=f'它当前是 {info.status.value}，手上没有正在进行的工作',
                next_action=(
                    'abort 停的是「手上这一件事」。要让一个空闲的 Agent 停止事件循环用 kill；'
                    '要连注册表条目一起去掉用 despawn'
                ),
            )
            return
        slot.control.request_abort()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests -v && uv run ruff check . && uv run pyright`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/runtime.py tests/kernel/test_runtime.py
git commit -m "feat(kernel): abort 是协作式的，因此 ABORTED 第一次真的产生

spec 把 ABORTED 留给轮与轮之间的协作式中止——那时轨迹配对完整、可以直接 resume；
硬取消落在轮中间，可能留下不配对的调用，所以它不设 stop_reason。计划 1 保留了这个
区分但没有任何东西走这条路，本任务把它填上。

只允许从 RUNNING / WAITING / PAUSED 发起：手上没有活就没有「停下手上这一件事」
可言。IDLE / ZOMBIE 上报错并指向 kill / despawn，因为那时调用方想要的多半是
「别再理它」，而那是另一件事——两者混成一个方法，调用方就无从表达自己要的是哪个。

中止位一次性：一个周期结束后事件循环照常回去等下一条消息。若不清位，一次 abort
会让此后每一个周期都立刻结束。"
```

---

## Task 7: `kill` / `despawn`——三条拆除路径合并成一条，并收口悬空 `parent_id`

**Files:**
- Modify: `src/syndica/kernel/hooks/junctions.py`
- Modify: `src/syndica/kernel/runtime.py`
- Test: `tests/kernel/test_runtime.py`
- Test: `tests/kernel/test_hooks.py`

**Interfaces:**
- Consumes: Task 3 的 `_finish`、Task 5 的 `_descendants`
- Produces:
  - `Junction.AGENT_SPAWN` / `Junction.AGENT_DESPAWN`（均可观察）
  - `Runtime.kill(agent_id)`：硬取消，立刻返回，收尾在后台；**不注销条目**
  - `Runtime.despawn(agent_id)`：级联销毁自己与全部子代，等它们真的停下来再返回
  - `Runtime._destroy_tree` / `_destroy`：`kill` 的收尾、`despawn`、`ExitMode.EXIT` 共用的唯一拆除顺序

**Task 4 移交的三件既有缺陷**（都不是 Task 4 引入的，都归拆除顺序管，因此归本任务）：

1. **`shutdown` 之后仍可能被激活出一个孤儿驱动。** `ensure_active` 只在方法开头读一次
   `_closed`，之后还隔着 `registry.get` / `compare_and_set_status` / `broker.ensure_queue`
   三个 Protocol await 才 `create_task`，且不复检。任何会挂起的 store（即任何远端实现）下，
   `ensure_active` 与 `shutdown` 并发会在 `gather()` 返回之后建出一个永不被取消、永不被 await
   的驱动，并把注册表从 zombie 翻回 `idle`。**`spawn()` 形状相同、洞相同**（同样先查 `_closed`、
   同样隔着两个 await 才 `create_task`），实测同样产出孤儿——所以要修就两处一起修，补一个
   关机态守卫。目前只是被 `asyncio.run()` 自己的 teardown 兜住，因此 pending-task 扫描是干净的。
2. **`_finish` 里 `_report_abandoned_input` 与 `remove_queue` 之间落进来的 `send` 拿到
   `ok=True` 却被丢弃，且没有任何 ERROR。** 实测：`ok=True` → 事后 `consume` 为空 → ERROR 记录
   0 条。这正是「拿过 ok=True 收据的输入不得静默消失」要防的那件事的盲区，而它就在收尾顺序里。
   注意 `remove_queue` **之后**那个紧邻窗口不同：那里收据是诚实的（`ok=False`），已复核。
3. **`shutdown` 之后 `send` 给一个当时停在邮箱上的 Agent 仍答 `ok=True`**，消息落进一个再没有
   消费者的邮箱（`test_ensure_active_refuses_after_shutdown` 已把这条现状钉住并与「早已 zombie」
   那一种分开）。根因与第 1 条同源——取消不做收尾，因此关机态守卫落地时一并裁决。

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_runtime.py  （追加）
async def test_kill_returns_before_teardown_finishes_and_leaves_a_revivable_zombie():
    entered = asyncio.Event()
    release = asyncio.Event()
    runtime = Runtime()
    script = [
        ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Gated', arguments='{"text":"hi"}')]),
        ScriptedTurn(content='resumed after the kill'),
    ]

    def build(hooks, events):
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM(script),
                tools={'Gated': _Gated(entered, release)},
                system_prompt='SP',
            ),
            hooks=hooks,
            events=events,
        )

    agent_id = await runtime.spawn(build, query='go')
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    await runtime.kill(agent_id)
    await _wait_for_status(runtime, agent_id, AgentStatus.ZOMBIE)

    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert agent.context.stop_reason is None

    delivered = await runtime.send(agent_id, 'carry on')
    assert delivered.ok is True
    await _wait_for_status(runtime, agent_id, AgentStatus.ZOMBIE)
    roles = [block.role for block in agent.context.current_traj]
    assert roles.count('tool') == roles.count('assistant') - 1
    await runtime.shutdown()


async def test_kill_keeps_the_registry_entry_so_children_never_point_at_a_hole():
    runtime = Runtime()
    parent = await _resident(runtime)
    child = await _resident(runtime, parent_id=parent)

    await runtime.kill(parent)
    await _wait_for_status(runtime, parent, AgentStatus.ZOMBIE)

    info = await runtime.find(child)
    assert info is not None and info.parent_id == parent
    assert await runtime.find(parent) is not None
    await runtime.shutdown()


async def test_despawn_takes_the_whole_subtree_with_it():
    """unregister 只把自己从父的 children 上摘掉，不动自己的子代。"""
    runtime = Runtime()
    root = await _resident(runtime)
    first_child = await _resident(runtime, parent_id=root)
    second_child = await _resident(runtime, parent_id=root)
    grandchild = await _resident(runtime, parent_id=first_child)

    await runtime.despawn(root)

    for agent_id in (root, first_child, second_child, grandchild):
        assert await runtime.find(agent_id) is None
    await runtime.shutdown()


async def test_despawning_a_child_leaves_the_parent_and_its_siblings_alone():
    runtime = Runtime()
    root = await _resident(runtime)
    doomed = await _resident(runtime, parent_id=root)
    survivor = await _resident(runtime, parent_id=root)

    await runtime.despawn(doomed)

    assert await runtime.find(doomed) is None
    assert await runtime.registry.children_of(root) == [survivor]
    assert (await runtime.find(survivor)).status is AgentStatus.IDLE
    await runtime.shutdown()


async def test_despawning_an_unknown_agent_succeeds_because_the_postcondition_already_holds():
    runtime = Runtime()
    await runtime.despawn('nope')
    await runtime.shutdown()


async def test_exit_mode_exit_uses_the_same_cascade_as_despawn():
    runtime = Runtime()
    parent = await runtime.spawn(
        _factory([ScriptedTurn(content='done')]), query='go', exit_mode=ExitMode.EXIT
    )
    child = await _resident(runtime, parent_id=parent)
    task = runtime._table.get_task(parent)
    assert task is not None
    await asyncio.wait_for(asyncio.shield(task), timeout=2.0)

    assert await runtime.find(parent) is None
    assert await runtime.find(child) is None
    await runtime.shutdown()


async def test_despawning_yourself_from_inside_your_own_loop_is_refused():
    runtime = Runtime()
    holder: dict[str, str] = {}

    class _SelfDespawn(Tool):
        async def execute(self, note: str) -> ToolResult:
            """记下 note 然后尝试销毁自己。"""
            try:
                await runtime.despawn(holder['agent_id'])
            except ContractViolation as error:
                return ToolResult(output=f'refused: {error.next_action}')
            return ToolResult(output='not refused')

    def build(hooks, events):
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM(
                    [
                        ScriptedTurn(
                            tool_calls=[
                                ToolCall(id='c1', name='SelfDespawn', arguments='{"note":"bye"}')
                            ]
                        ),
                        ScriptedTurn(content='still alive'),
                    ]
                ),
                tools={'SelfDespawn': _SelfDespawn()},
                system_prompt='SP',
            ),
            hooks=hooks,
            events=events,
        )

    agent_id = await runtime.spawn(build, query='go')
    holder['agent_id'] = agent_id
    ctx = await asyncio.wait_for(runtime.wait(agent_id), timeout=2.0)

    tool_blocks = [block for block in ctx.current_traj if block.role == 'tool']
    assert len(tool_blocks) == 1
    assert isinstance(tool_blocks[0].content, str)
    assert tool_blocks[0].content.startswith('refused:')
    await runtime.shutdown()
```

```python
# tests/kernel/test_hooks.py  （替换枚举表与分类断言）
def test_fifteen_junctions_exist():
    """取值撞车会让成员静默塌成别名；切点名计入公开 API。"""
    assert {junction.name: junction.value for junction in Junction} == {
        'RUNTIME_SHUTDOWN': 'runtime_shutdown',
        'AGENT_SPAWN': 'agent_spawn',
        'AGENT_DESPAWN': 'agent_despawn',
        'AGENT_START': 'agent_start',
        'AGENT_END': 'agent_end',
        'AGENT_ERROR': 'agent_error',
        'TURN_START': 'turn_start',
        'TURN_END': 'turn_end',
        'MESSAGE_INJECTED': 'message_injected',
        'PRE_TOOL_USE': 'pre_tool_use',
        'POST_TOOL_USE': 'post_tool_use',
        'PRE_LLM_REQUEST': 'pre_llm_request',
        'POST_LLM_RESPONSE': 'post_llm_response',
        'CONTEXT_OVERFLOW': 'context_overflow',
        'LLM_FAILURE': 'llm_failure',
    }


def test_each_junction_is_classified_as_declared():
    """分类决定语义，与切点名同属公开 API：挪动一个切点就改了它的执行方式。"""
    assert INTERCEPTABLE == {
        Junction.PRE_TOOL_USE,
        Junction.POST_TOOL_USE,
        Junction.PRE_LLM_REQUEST,
        Junction.POST_LLM_RESPONSE,
        Junction.CONTEXT_OVERFLOW,
        Junction.LLM_FAILURE,
    }
    assert OBSERVABLE == {
        Junction.RUNTIME_SHUTDOWN,
        Junction.AGENT_SPAWN,
        Junction.AGENT_DESPAWN,
        Junction.AGENT_START,
        Junction.AGENT_END,
        Junction.AGENT_ERROR,
        Junction.TURN_START,
        Junction.TURN_END,
        Junction.MESSAGE_INJECTED,
    }
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel -v`
Expected: FAIL —— `Runtime` 没有 `kill` / `despawn`；`Junction` 没有那三个成员

- [ ] **Step 3: 加上三个生命周期切点**

`Junction` 顶部插入三项，并全部归入 `OBSERVABLE`（它们报告既成事实，改不了任何东西）：

```python
class Junction(Enum):
    RUNTIME_SHUTDOWN = 'runtime_shutdown'
    AGENT_SPAWN = 'agent_spawn'
    AGENT_DESPAWN = 'agent_despawn'
    AGENT_START = 'agent_start'
    ...
```

> `RUNTIME_START` 不在这里：`Runtime.__init__` 是同步的，没有可以 await hook 的位置。它的自然
> 落点是 Runtime Module 的启停钩子（spec §5.5），归计划 5。

- [ ] **Step 4: 加上 `kill` / `despawn` 与共用的拆除顺序**

`__init__` 里加一行收割任务集合：

```python
        self._reapers: set[asyncio.Task[None]] = set()
```

`spawn` 在 `set_task` 之后、`return` 之前加一行：

```python
        await self.hooks.trigger(Junction.AGENT_SPAWN, agent_id=agent_id, parent_id=parent_id)
```

`_finish` 的 EXIT 分支换成共用拆除：

```python
    async def _finish(self, agent_id: str, slot: _Slot) -> None:
        """事件循环退出后的归宿（spec §4.1 的 ExitMode）。**幂等**——`kill` 的收割任务与驱动任务
        自己的 finally 都可能走到它，而两者谁先谁后取决于取消是否赶上。"""
        if slot.exit_mode is ExitMode.EXIT:
            await self._destroy_tree(agent_id)
            return
        await self.broker.remove_queue(agent_id)
        if not await self._switch(agent_id, new=AgentStatus.ZOMBIE, allowed=_LIVE_STATUSES):
            logger.error(
                '期望: agent %s 在注册表里仍是本进程认得的活状态，可由本进程转去 zombie。'
                ' 实际: 转换被拒，它的状态在事件循环退出前被别人改成了别的东西。'
                ' 下一步: 用 find() 读它当前的状态；本进程已经不再驱动它。',
                agent_id,
            )
```

在 `abort` 之后插入：

```python
    async def kill(self, agent_id: str) -> None:
        """硬终止：取消事件循环并**立刻返回**，收尾在后台完成。

        spec §4.1 给 ABORTING 的理由就是这个——它是中间态，好让 `kill` 不必等收尾。

        取消可能落在轮的中间，因此这条路**不产生 `stop_reason`**，并且会留下一个声明了 N 个调用
        却不足 N 条结果的 assistant block。那不是缺陷：`BaseAgent.run` 在入口按「中断、副作用
        未知」把它修补成配对完整的轨迹，于是被杀掉的 Agent 仍然可以被一条消息复活并继续干。

        **`kill` 不注销注册表条目**，它把 Agent 送进 ZOMBIE：条目还在，因此它的子代不会指向一个
        已被删掉的父。要连条目一起去掉是 `despawn`。

        **刻意不置中止位。** 取消已经足够；而中止位是会留下来的状态，此后一次 `ensure_active` 复活
        会在第一轮就把它消费掉，让复活出来的 Agent 立刻结束——一次 kill 变成了永久的诅咒。
        """
        slot = self._slots.get(agent_id)
        if slot is None:
            raise ContractViolation(
                expected=f'agent {agent_id!r} 的事件循环由本进程持有',
                received=(
                    '本进程没有它的实例：它不是本进程 spawn 的、已被 despawn，'
                    '或以 ExitMode.EXIT 结束'
                ),
                next_action=(
                    '用 find() 确认它还在不在；由其他进程持有的 Agent 只能在那个进程上 kill'
                ),
            )
        await self._switch(agent_id, new=AgentStatus.ABORTING, allowed=_LIVE_STATUSES)
        task = self._table.get_task(agent_id)
        if task is not None and not task.done():
            task.cancel()
        reaper = asyncio.create_task(self._reap(agent_id, slot), name=f'reap:{agent_id}')
        self._reapers.add(reaper)
        reaper.add_done_callback(self._reapers.discard)

    async def despawn(self, agent_id: str) -> None:
        """销毁：连注册表条目一起去掉，**并且级联到全部子代**。

        级联不是可选项，所以这里没有 `recursive` 开关。`AgentRegistry.unregister` 只把自己从父的
        children 上摘掉、不动自己的子代，于是删掉一个有子代的父会留下一串 `parent_id` 指着一个
        不存在的条目——`children_of` 从此永远漏掉它们，而它们自己看起来还有个父。这是数据完整性
        问题不是策略：**要么条目还在，要么它和它的子树一起不在**。（`kill` 因此不需要级联：它
        根本不删条目。）

        拆除顺序是**子先于父**：任何一刻都不存在「父已经没了而子还活着」的中间态。

        `despawn` 等每一棵子树真的停下来再返回——它对调用方的承诺是「回来之后这些条目都不在了」，
        而这个承诺只有等到才作数。要立刻返回就用 `kill`。

        未知 agent 是成功的 no-op：「它不在注册表里」这个后置条件已经成立。
        """
        self._refuse_self('despawn', agent_id)
        if agent_id not in self._slots and await self.registry.get(agent_id) is None:
            return
        await self._destroy_tree(agent_id)

    async def _destroy_tree(self, agent_id: str) -> None:
        """`despawn`、`ExitMode.EXIT` 与 `kill` 的收尾共用的唯一拆除顺序。

        三条路各写一份清理，迟早会在「先摘队列还是先注销」这种地方分叉，而分叉出来的差异只在
        并发投递恰好落进那个窗口时才现身。
        """
        for descendant in reversed(await self._descendants(agent_id)):
            await self._destroy(descendant)
        await self._destroy(agent_id)

    async def _destroy(self, agent_id: str) -> None:
        """拆掉一个 Agent 的全部痕迹。幂等。"""
        await self._switch(agent_id, new=AgentStatus.ABORTING, allowed=_LIVE_STATUSES)
        task = self._table.get_task(agent_id)
        # `is not current_task()`：ExitMode.EXIT 是在驱动任务自己的 finally 里走到这条路的，
        # 取消并 await 自己就是永久挂死。
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.wait({task})
        await self.broker.remove_queue(agent_id)
        await self.registry.unregister(agent_id)
        self._table.pop(agent_id)
        self._slots.pop(agent_id, None)
        await self.hooks.trigger(Junction.AGENT_DESPAWN, agent_id=agent_id)

    async def _reap(self, agent_id: str, slot: _Slot) -> None:
        """在事件循环之外收尾。

        收尾要写注册表、摘队列，而这些 await 落在一个**正在被取消**的任务里会再次被取消——内存
        实现没有挂起点所以看不出来，远端实现下就是收了一半的尾。放进一个独立任务，`kill` 因此也
        能立刻返回。
        """
        task = self._table.get_task(agent_id)
        if task is not None:
            await asyncio.wait({task})
        await self._finish(agent_id, slot)

    def _refuse_self(self, operation: str, agent_id: str) -> None:
        if self._table.get_task(agent_id) is not asyncio.current_task():
            return
        raise ContractViolation(
            expected=f'{operation} 的目标不是发起这次调用的那个 Agent 自己',
            received=f'这次 {operation} 发生在 agent {agent_id!r} 自己的事件循环里',
            next_action=(
                f'{operation} 要等目标的事件循环停下来，等自己就是永久挂死。'
                '要让自己停下来用 abort（它在下一轮边界上兑现，轨迹保持一致）；'
                '要让别人把你销毁，就把这件事交给你的父 Agent'
            ),
        )
```

`shutdown` 末尾把收割任务一并等掉（放在 `events.close()` 之前）：

```python
        await self.hooks.trigger(Junction.RUNTIME_SHUTDOWN)
        if self._reapers:
            await asyncio.gather(*list(self._reapers), return_exceptions=True)
        await self.events.close()
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests -v && uv run ruff check . && uv run pyright`
Expected: 全部 passed

- [ ] **Step 6: 提交**

```bash
git add src/syndica/kernel/hooks/junctions.py src/syndica/kernel/runtime.py tests/kernel/test_runtime.py tests/kernel/test_hooks.py
git commit -m "feat(kernel): despawn 级联，因此不再留下指着空洞的 parent_id

AgentRegistry.unregister 只把自己从父的 children 上摘掉、不动自己的子代，于是删掉
一个有子代的父会留下一串 parent_id 指着不存在的条目：children_of 从此漏掉它们，
而它们自己看起来还有个父。这是数据完整性问题不是策略，所以 despawn 没有 recursive
开关——要么条目还在，要么它和它的子树一起不在。

kill 与 despawn 的分工由此确定：kill 不删条目（送进 ZOMBIE），所以它根本不会制造
悬空 parent_id，也就不需要级联。

三条拆除路径（kill 的收尾 / despawn / ExitMode.EXIT）合并到 _destroy_tree 一处。
各写一份清理迟早会在「先摘队列还是先注销」这种地方分叉，而分叉出来的差异只在并发
投递恰好落进那个窗口时才现身。

kill 刻意不置中止位：位是会留下来的状态，一次 ensure_active 复活会在第一轮把它
消费掉，让复活出来的 Agent 立刻结束——一次 kill 变成永久的诅咒。收尾放进独立的
收割任务，因为收尾要 await，而在一个正在被取消的任务里 await 只会收一半的尾。"
```

---

## Task 8: `Waiter` 密封类型与 `WaitResult` 四态

**Files:**
- Create: `src/syndica/kernel/waiting.py`
- Test: `tests/kernel/test_waiting.py`

**Interfaces:**
- Consumes: `syndica.kernel.message`（`Message`）、`syndica.kernel.errors`
- Produces: `WaitState`（四态）、`WaitResult`、`Waiter` / `UserInput` / `Confirmation` / `Reply`、`correlation_of(waiter)`

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_waiting.py
from __future__ import annotations

from dataclasses import dataclass

import pytest

from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.message import Message
from syndica.kernel.waiting import (
    Confirmation,
    Reply,
    UserInput,
    WaitResult,
    WaitState,
    Waiter,
    correlation_of,
)


def test_four_wait_states_exist():
    """取值撞车会让成员静默塌成别名；这四个状态直接决定工具拿到什么结果。"""
    assert {state.name: state.value for state in WaitState} == {
        'ANSWERED': 'answered',
        'TIMEOUT': 'timeout',
        'PREEMPTED': 'preempted',
        'ABORTED': 'aborted',
    }


def test_each_waiter_kind_maps_to_its_own_correlation_id():
    """三种等待共用一条实现（按关联 ID 匹配），映射写错就是等一个永远不会来的答复。"""
    assert correlation_of(Confirmation(timeout=30.0, tool_call_id='call-7')) == 'call-7'
    assert correlation_of(Reply(timeout=30.0, correlation_id='req-3')) == 'req-3'
    assert correlation_of(UserInput(timeout=30.0, request_id='ask-1')) == 'ask-1'


def test_user_input_generates_its_own_request_id_when_not_given():
    first = UserInput(timeout=30.0)
    second = UserInput(timeout=30.0)
    assert first.request_id and second.request_id
    assert first.request_id != second.request_id


def test_an_unknown_waiter_subclass_is_refused_instead_of_silently_treated_as_user_input():
    """密封的意思就是这个：不在名单上的等待类型必须响亮失败，而不是落进某个默认分支。"""

    @dataclass(frozen=True)
    class Invented(Waiter):
        pass

    with pytest.raises(ContractViolation):
        correlation_of(Invented(timeout=30.0))


@pytest.mark.parametrize('bad', [0.0, -1.0])
def test_a_non_positive_timeout_is_refused(bad: float):
    """spec §4.2：超时是必须的——长跑里等人工输入可能几小时无人查看，没有超时就是永久挂起。"""
    with pytest.raises(ConfigError):
        UserInput(timeout=bad)


@pytest.mark.parametrize(
    'build',
    [
        lambda: Confirmation(timeout=30.0, tool_call_id=''),
        lambda: Reply(timeout=30.0, correlation_id=''),
        lambda: UserInput(timeout=30.0, request_id=''),
    ],
)
def test_an_empty_correlation_id_is_refused(build):
    """空关联 ID 谁都匹配不上，表现为静默等到超时——比当场报错难查得多。"""
    with pytest.raises(ConfigError):
        build()


def test_waiters_are_frozen_so_a_waiter_cannot_be_retargeted_after_it_is_handed_over():
    waiter = Reply(timeout=30.0, correlation_id='req-3')
    with pytest.raises(AttributeError):
        waiter.correlation_id = 'req-4'  # type: ignore[misc]


def test_a_timed_out_result_carries_no_message():
    assert WaitResult(state=WaitState.TIMEOUT).message is None


def test_an_answered_result_carries_the_message_that_answered_it():
    message = Message(sender='user', receiver='a1', content='yes', correlation_id='call-7')
    result = WaitResult(state=WaitState.ANSWERED, message=message)
    assert result.message is message
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_waiting.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'syndica.kernel.waiting'`

- [ ] **Step 3: 写 `waiting.py`**

```python
# src/syndica/kernel/waiting.py
"""等待一个答复：`Waiter` 密封类型与 `WaitResult` 四态。

spec §4.2 把等待收进**一个**公开方法（`Runtime.wait_for`）加一个密封类型，而不是给每种等待各开
一个方法：新增等待类型于是不扩大公开面。密封的实际含义在 `correlation_of` 上——名单之外的
`Waiter` 子类**当场报错**，而不是落进某个默认分支去等一个永远不会来的答复。

**三种等待共用一条实现：按关联 ID 匹配。** `Confirmation` 用 `tool_call_id`，`Reply` 用
`correlation_id`，`UserInput` 用它自己生成的 `request_id`。于是 `wait_for` 就是
`MessageBroker.take_by_correlation`——那个方法刻意不接受任意谓词，正是因为谓词下推不到远端队列
（spec §4.4）。

**超时是必须的，且每个 Waiter 自带自己的上限。** 长跑场景下等人工输入可能几小时无人查看，没有
超时就是永久挂起，而一个挂住的 Agent 会一直占着预算与租约。因此这里不接受 `None`，也不接受 0。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.message import Message

__all__ = [
    'Confirmation',
    'Reply',
    'UserInput',
    'WaitResult',
    'WaitState',
    'Waiter',
    'correlation_of',
]


class WaitState(Enum):
    """一次等待的结局。

    四个都必须存在：`timeout` 是长跑的兜底，`aborted` 是协作式中止落在等待上的样子，`preempted`
    是「有人用一个新的等待顶掉了它」——三者的后续动作各不相同，塌成一个「没等到」就无从分辨。
    """

    ANSWERED = 'answered'
    TIMEOUT = 'timeout'
    PREEMPTED = 'preempted'
    ABORTED = 'aborted'


@dataclass(frozen=True)
class Waiter:
    """等待的基类。**密封**：子类只有下面三个，新增必须同时在 `correlation_of` 里登记。

    frozen 是承重的：Waiter 交给 `wait_for` 之后还被改掉关联 ID，那次等待就永远等不到答复了。
    """

    timeout: float

    def __post_init__(self) -> None:
        if not isinstance(self.timeout, (int, float)) or self.timeout <= 0:
            raise ConfigError(
                expected='timeout 是正数秒数：每个 Waiter 自带自己的等待上限（spec §4.2）',
                received=f'收到 timeout={self.timeout!r}',
                next_action=(
                    '传一个正数。刻意不接受 None——长跑里等人工输入可能几小时无人查看，'
                    '没有上限就是永久挂起，而挂住的 Agent 会一直占着预算与租约'
                ),
            )


def _validated_id(kind: str, field_name: str, value: str) -> str:
    if not value:
        raise ConfigError(
            expected=f'{kind} 的 {field_name} 非空：等待按关联 ID 匹配答复',
            received=f'收到 {field_name}={value!r}',
            next_action=(
                f'填上要等的那条请求的 ID。空 ID 谁都匹配不上，'
                '表现为静默等到超时——比当场报错难查得多'
            ),
        )
    return value


@dataclass(frozen=True)
class UserInput(Waiter):
    """等一个人来回话。`request_id` 就是这次提问的关联 ID，不给则自动生成。"""

    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        super().__post_init__()
        _validated_id('UserInput', 'request_id', self.request_id)


@dataclass(frozen=True)
class Confirmation(Waiter):
    """等一次工具调用的人工确认。关联 ID 就是那次调用的 id。"""

    tool_call_id: str

    def __post_init__(self) -> None:
        super().__post_init__()
        _validated_id('Confirmation', 'tool_call_id', self.tool_call_id)


@dataclass(frozen=True)
class Reply(Waiter):
    """等对某条特定请求的回复（典型场景：派活出去之后等结果）。"""

    correlation_id: str

    def __post_init__(self) -> None:
        super().__post_init__()
        _validated_id('Reply', 'correlation_id', self.correlation_id)


@dataclass(frozen=True)
class WaitResult:
    """一次等待的结果。字段顺序计入公开 API。

    只有 `answered` 带 `message`：其余三种没有答复可交，硬凑一个空消息会让调用方误以为等到了。
    """

    state: WaitState
    message: Message | None = None


def correlation_of(waiter: Waiter) -> str:
    """取出这次等待要匹配的关联 ID。

    **这里就是「密封」落地的地方。** 名单之外的子类报错而不是回落到某个默认分支——回落的后果是
    一次永远等不到答复的等待，只在超时之后才现身，而那可能是几小时以后。
    """
    if isinstance(waiter, Confirmation):
        return waiter.tool_call_id
    if isinstance(waiter, Reply):
        return waiter.correlation_id
    if isinstance(waiter, UserInput):
        return waiter.request_id
    raise ContractViolation(
        expected='waiter 是 UserInput / Confirmation / Reply 之一（Waiter 是密封类型）',
        received=f'收到 {type(waiter).__name__}，它不在这三种之内',
        next_action=(
            '用这三种之一。要加新的等待类型，就在 waiting.py 里加一个 Waiter 子类并在'
            ' correlation_of 里登记它的关联 ID——没登记的类型等不到任何答复'
        ),
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/kernel/test_waiting.py -v && uv run ruff check . && uv run pyright`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/waiting.py tests/kernel/test_waiting.py
git commit -m "feat(kernel): 等待收进一个密封类型，新增等待类型不扩大公开面

spec 把 11 个公开方法当成永久兼容承诺，所以「等人工输入」「等工具确认」「等某条
回复」不能各开一个方法。密封的实际含义落在 correlation_of 上：名单之外的子类当场
报错，而不是回落到某个默认分支去等一个永远不会来的答复——回落的后果只在超时之后
才现身，而那可能是几小时以后。

三种等待共用「按关联 ID 匹配」这一条实现，于是 wait_for 就是 take_by_correlation。
那个方法刻意不接受任意谓词，正因为谓词下推不到远端队列。

timeout 不接受 None 也不接受 0：长跑里等人工输入可能几小时无人查看，没有上限就是
永久挂起，而挂住的 Agent 会一直占着预算与租约。"
```

---

## Task 9: `wait_for` 与 `WAITING` 状态

**Files:**
- Modify: `src/syndica/kernel/runtime.py`
- Test: `tests/kernel/test_runtime.py`

**Interfaces:**
- Consumes: Task 8 的 `Waiter` / `WaitResult` / `correlation_of`，Task 6 的 `abort`
- Produces:
  - `Runtime.wait_for(agent_id, waiter) -> WaitResult`
  - `_Slot.waiter`：一个 Agent 同时只有一个等待者，新的顶掉旧的

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_runtime.py  （追加）
async def _running(runtime: Runtime, entered: asyncio.Event, release: asyncio.Event) -> str:
    """起一个卡在工具里的 Agent：给「轮的中间」一个确定的观测点，wait_for 正是从那里发起的。"""

    def build(hooks, events):
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM(
                    [
                        ScriptedTurn(
                            tool_calls=[ToolCall(id='c1', name='Gated', arguments='{"text":"x"}')]
                        ),
                        ScriptedTurn(content='tail'),
                    ]
                ),
                tools={'Gated': _Gated(entered, release)},
                system_prompt='SP',
            ),
            hooks=hooks,
            events=events,
        )

    agent_id = await runtime.spawn(build, query='go')
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    return agent_id


async def test_a_matching_reply_answers_the_wait_and_never_reaches_the_turn_loop():
    entered = asyncio.Event()
    release = asyncio.Event()
    runtime = Runtime()
    agent_id = await _running(runtime, entered, release)

    waiting = asyncio.create_task(runtime.wait_for(agent_id, Reply(timeout=5.0, correlation_id='r1')))
    await _wait_for_status(runtime, agent_id, AgentStatus.WAITING)
    await runtime.send(agent_id, 'the answer', correlation_id='r1')
    result = await asyncio.wait_for(waiting, timeout=2.0)

    assert result.state is WaitState.ANSWERED
    assert result.message is not None and result.message.content == 'the answer'
    release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), timeout=2.0)
    assert [block.content for block in ctx.current_traj if block.role == 'user'] == ['go']
    await runtime.shutdown()


async def test_the_registry_says_waiting_while_the_turn_is_suspended_and_running_again_after():
    entered = asyncio.Event()
    release = asyncio.Event()
    runtime = Runtime()
    agent_id = await _running(runtime, entered, release)

    waiting = asyncio.create_task(runtime.wait_for(agent_id, UserInput(timeout=0.05)))
    await _wait_for_status(runtime, agent_id, AgentStatus.WAITING)
    result = await asyncio.wait_for(waiting, timeout=2.0)

    assert result.state is WaitState.TIMEOUT
    await _wait_for_status(runtime, agent_id, AgentStatus.RUNNING)
    release.set()
    await asyncio.wait_for(runtime.wait(agent_id), timeout=2.0)
    await runtime.shutdown()


async def test_a_second_wait_preempts_the_first_so_the_older_tool_call_still_gets_a_result():
    """一次调用恰好一个结果块——让被顶掉的那个挂到超时会让那条不变式没法满足。"""
    entered = asyncio.Event()
    release = asyncio.Event()
    runtime = Runtime()
    agent_id = await _running(runtime, entered, release)

    first = asyncio.create_task(runtime.wait_for(agent_id, UserInput(timeout=30.0)))
    await _wait_for_status(runtime, agent_id, AgentStatus.WAITING)
    second = asyncio.create_task(runtime.wait_for(agent_id, UserInput(timeout=30.0)))

    assert (await asyncio.wait_for(first, timeout=2.0)).state is WaitState.PREEMPTED

    second.cancel()
    await asyncio.gather(second, return_exceptions=True)
    release.set()
    await asyncio.wait_for(runtime.wait(agent_id), timeout=2.0)
    await runtime.shutdown()


async def test_abort_while_waiting_reports_aborted_rather_than_hanging_to_the_timeout():
    entered = asyncio.Event()
    release = asyncio.Event()
    runtime = Runtime()
    agent_id = await _running(runtime, entered, release)

    waiting = asyncio.create_task(runtime.wait_for(agent_id, UserInput(timeout=30.0)))
    await _wait_for_status(runtime, agent_id, AgentStatus.WAITING)
    await runtime.abort(agent_id)

    assert (await asyncio.wait_for(waiting, timeout=2.0)).state is WaitState.ABORTED
    release.set()
    ctx = await asyncio.wait_for(runtime.wait(agent_id), timeout=2.0)
    assert ctx.stop_reason is StopReason.ABORTED
    await runtime.shutdown()


async def test_waiting_on_an_agent_that_is_not_running_is_refused():
    """wait_for 是「代表一个正在干活的 Agent 等」；从外面等它结束是 wait()。"""
    runtime = Runtime()
    agent_id = await _resident(runtime)
    with pytest.raises(ContractViolation) as info:
        await runtime.wait_for(agent_id, UserInput(timeout=5.0))
    assert 'wait()' in str(info.value)
    await runtime.shutdown()


async def test_a_reply_that_nobody_is_waiting_for_becomes_an_ordinary_message():
    """关联 ID 只是匹配用的；没人在等它时它就是一条普通消息，不会凭空消失。"""
    runtime = Runtime()
    agent_id = await _resident(runtime)
    await runtime.send(agent_id, 'unclaimed answer', correlation_id='r9')
    await _wait_for_status(runtime, agent_id, AgentStatus.IDLE)
    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert [block.content for block in agent.context.current_traj if block.role == 'user'] == [
        'unclaimed answer'
    ]
    await runtime.shutdown()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_runtime.py -v`
Expected: FAIL —— `Runtime` 没有 `wait_for`

- [ ] **Step 3: 加上 `wait_for`**

import 块补：

```python
from syndica.kernel.waiting import WaitResult, WaitState, Waiter, correlation_of
```

`_Slot` 加一个字段：

```python
    owned: bool = False
    waiter: _WaitToken | None = None
```

在 `_Slot` 之前加令牌类型：

```python
class _WaitToken:
    """一次等待的中断句柄。

    抢占与中止都经它送达，**先到者胜**：两者同时发生时，第一个说法就是最终说法，因为等待只返回
    一个结果，而两种结局的后续动作不同（被顶掉的可以重来，被中止的不该重来）。
    """

    def __init__(self) -> None:
        self._state: WaitState | None = None
        self._raised = asyncio.Event()

    def interrupt(self, state: WaitState) -> None:
        if self._state is None:
            self._state = state
            self._raised.set()

    async def wait(self) -> WaitState:
        await self._raised.wait()
        state = self._state
        if state is None:
            raise ContractViolation(
                expected='等待令牌被唤醒时已经带着一个中断状态',
                received='它被置位了却没有状态：interrupt() 之外还有人动过这个事件',
                next_action='只经 interrupt() 置位这个令牌；它是 runtime 私有的，不要在外面 set 它',
            )
        return state
```

`_abort_one` 末尾补一行，让中止能穿透一次等待：

```python
        slot.control.request_abort()
        waiter = slot.waiter
        if waiter is not None:
            # 中止位只在轮的边界上被读到，而此刻这个 Agent 正卡在一次等待里——不打断它，
            # 中止要等到那次等待自己超时才生效，而超时可能是几小时。
            waiter.interrupt(WaitState.ABORTED)
```

在 `find` 之前加：

```python
    async def wait_for(self, agent_id: str, waiter: Waiter) -> WaitResult:
        """代表一个正在跑的 Agent 等一个答复（spec §4.2，供工具使用）。

        **它在轮里面等，不在事件循环里等。** 一次 `Confirmation` 等待是从工具调用内部发起的，
        所以此刻这个 Agent 正在一轮的中间：turn 循环挂在这里，注册表说 `WAITING`。它的邮箱仍然
        由门在下一轮开始时消费，两件事各等各的、互不干扰——被等到的那条答复由
        `take_by_correlation` 从邮箱里取走，因此不会再作为普通消息注入一遍。

        **一个 Agent 同时只有一个等待者，新的顶掉旧的，旧的收到 `preempted`。** 一个 Agent 是
        一个进程、一份注意力；两个并发的等待里必然有一个的答复没人会来。让旧的挂到超时是最坏的
        选择——发起它的那次工具调用会一直占着位置，而「一次调用恰好一个结果块」这条不变式要求
        它必须能返回点什么。

        **答复优先于中断**：两者同时就绪时返回 `answered`。消息已经离开邮箱了，此刻丢掉它就是
        真的丢了。

        只允许从 RUNNING 发起。它是「代表一个正在干活的 Agent 等」，从外面等它结束是 `wait()`。
        """
        slot = self._slots.get(agent_id)
        if slot is None:
            raise ContractViolation(
                expected=f'agent {agent_id!r} 由本进程持有，可以代表它等一个答复',
                received='本进程没有它的实例',
                next_action='wait_for 由该 Agent 自己的工具在轮中发起；要从外面等它结束用 wait()',
            )
        correlation = correlation_of(waiter)
        if not await self._switch(
            agent_id, new=AgentStatus.WAITING, allowed=(AgentStatus.RUNNING,)
        ):
            info = await self.registry.get(agent_id)
            raise ContractViolation(
                expected=f'agent {agent_id!r} 正在 RUNNING：只有正在干活的 Agent 才谈得上等答复',
                received=f'它当前是 {info.status.value if info is not None else "不在注册表里"}',
                next_action=(
                    'wait_for 由该 Agent 自己的工具在轮中发起；'
                    '要从外面等它结束用 wait()，要让它停下来用 pause / abort'
                ),
            )
        token = _WaitToken()
        previous = slot.waiter
        slot.waiter = token
        if previous is not None:
            previous.interrupt(WaitState.PREEMPTED)
        try:
            return await self._awaited(agent_id, correlation, waiter.timeout, token)
        finally:
            if slot.waiter is token:
                slot.waiter = None
            # 回不去 RUNNING 是正常的：abort 已经把它转成 ABORTING，pause 已经把它转成 PAUSED。
            # 强行覆盖等于把别人的转换抹掉。
            await self._switch(
                agent_id, new=AgentStatus.RUNNING, allowed=(AgentStatus.WAITING,)
            )

    async def _awaited(
        self, agent_id: str, correlation: str, timeout: float, token: _WaitToken
    ) -> WaitResult:
        taking = asyncio.create_task(
            self.broker.take_by_correlation(agent_id, correlation, timeout),
            name=f'wait:{agent_id}:{correlation}',
        )
        interrupted = asyncio.create_task(token.wait(), name=f'interrupt:{agent_id}')
        try:
            await asyncio.wait({taking, interrupted}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (taking, interrupted):
                if not task.done():
                    task.cancel()
            await asyncio.gather(taking, interrupted, return_exceptions=True)
        if taking.done() and not taking.cancelled():
            failure = taking.exception()
            if failure is not None:
                raise failure
            message = taking.result()
            if message is not None:
                return WaitResult(state=WaitState.ANSWERED, message=message)
        if interrupted.done() and not interrupted.cancelled():
            return WaitResult(state=interrupted.result())
        return WaitResult(state=WaitState.TIMEOUT)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests -v && uv run ruff check . && uv run pyright`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/runtime.py tests/kernel/test_runtime.py
git commit -m "feat(kernel): WAITING 是「轮被挂起」，不是「事件循环在等」

一次 Confirmation 等待是从工具调用内部发起的，所以此刻这个 Agent 正在一轮的中间：
turn 循环挂在 wait_for 上，注册表说 WAITING，而它的邮箱仍由门在下一轮开始时消费。
两件事各等各的——被等到的那条答复由 take_by_correlation 从邮箱取走，因此不会再作为
普通消息注入一遍。

三处刻意的裁决：
- 一个 Agent 同时只有一个等待者，新的顶掉旧的。一个 Agent 是一份注意力，两个并发
  等待里必然有一个的答复没人会来；让旧的挂到超时会让「一次调用恰好一个结果块」
  这条不变式没法满足——那次工具调用得能返回点什么
- 答复优先于中断。两者同时就绪时返回 answered：消息已经离开邮箱了，丢掉就是真丢
- abort 主动打断在途的等待。中止位只在轮的边界上被读到，而等待正卡在轮的中间；
  不打断它，中止要等那次等待自己超时才生效，而超时可能是几小时

wait_for 返回时回不去 RUNNING 是正常的（abort 转成了 ABORTING、pause 转成了
PAUSED），强行覆盖等于把别人的转换抹掉。"
```

---
## Task 10: 危险判定——纯规则

**Files:**
- Create: `src/syndica/kernel/danger.py`
- Modify: `src/syndica/kernel/tool.py`
- Test: `tests/kernel/test_danger.py`
- Test: `tests/kernel/test_tool.py`

**Interfaces:**
- Consumes: `syndica.kernel.tool`（`Tool`）、`syndica.kernel.context`（`ToolCall`）
- Produces:
  - `RiskLevel` / `DangerVerdict(rule_id, level, reason)` / `DangerRule` / `DEFAULT_RULES` / `DangerJudge`
  - `Disposition`（`ALLOW` / `BLOCK` / `CONFIRM`）/ `DangerPolicy` Protocol / `BlockHighRisk` / `AllowAll`
  - `Tool.__tool_command_args__`：这个工具的哪些参数会被**执行**

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_danger.py
from __future__ import annotations

from typing import Any

import pytest

from syndica.kernel.context import ToolCall
from syndica.kernel.danger import (
    DEFAULT_RULES,
    AllowAll,
    BlockHighRisk,
    DangerJudge,
    Disposition,
    RiskLevel,
)
from syndica.kernel.tool import Tool, ToolResult

# 期望值逐字写死，**不从 DEFAULT_RULES 推导**：规则 ID 是稳定标识，删一条、改一个拼写都必须先在
# 这里变红。用被测对象自己的数据算出来的期望值测的是恒等式。顺序即优先级（判定按声明顺序取第一个
# 命中的），因此这份清单同时钉住顺序。
_RULE_IDS: tuple[str, ...] = (
    'shell.privilege_escalation',
    'shell.remote_script_execution',
    'shell.recursive_force_remove',
    'shell.raw_device_write',
)


class _Runner(Tool):
    """把 command 交给 shell 执行。"""

    __tool_command_args__ = ('command',)

    async def execute(self, command: str, note: str = '') -> ToolResult:
        """执行 command。"""
        return ToolResult(output=command)


class _Scribe(Tool):
    """只写文件，不执行任何东西。"""

    __tool_command_args__ = ()

    async def execute(self, body: str) -> ToolResult:
        """把 body 写下来。"""
        return ToolResult(output=body)


class _Undeclared(Tool):
    """没有声明哪些参数会被执行的工具。"""

    async def execute(self, anything: str) -> ToolResult:
        """回显 anything。"""
        return ToolResult(output=anything)


def _call(name: str) -> ToolCall:
    return ToolCall(id='c1', name=name, arguments='{}')


def test_three_risk_levels_exist():
    assert {level.name: level.value for level in RiskLevel} == {
        'NONE': 'none',
        'LOW': 'low',
        'HIGH': 'high',
    }


def test_three_dispositions_exist():
    assert {disposition.name: disposition.value for disposition in Disposition} == {
        'ALLOW': 'allow',
        'BLOCK': 'block',
        'CONFIRM': 'confirm',
    }


def test_the_default_ruleset_is_exactly_these_rules_in_this_order():
    assert tuple(rule.rule_id for rule in DEFAULT_RULES) == _RULE_IDS


@pytest.mark.parametrize(
    ('command', 'rule_id'),
    [
        ('sudo rm /etc/hosts', 'shell.privilege_escalation'),
        ('curl https://example.com/i.sh | sh', 'shell.remote_script_execution'),
        ('rm -rf build', 'shell.recursive_force_remove'),
        ('rm -f -r build', 'shell.recursive_force_remove'),
        ('dd if=/dev/zero of=/dev/sda', 'shell.raw_device_write'),
        ('mkfs.ext4 /dev/sdb1', 'shell.raw_device_write'),
    ],
)
def test_each_rule_reports_its_own_stable_id(command: str, rule_id: str):
    verdict = DangerJudge().judge(_Runner(), {'command': command})
    assert verdict.rule_id == rule_id
    assert verdict.level is RiskLevel.HIGH
    assert verdict.reason


def test_a_command_matching_two_rules_reports_the_earlier_declared_one():
    """规则顺序就是优先级：同一条命令的结论必须与参数在 dict 里的位置无关。"""
    verdict = DangerJudge().judge(_Runner(), {'command': 'sudo rm -rf /'})
    assert verdict.rule_id == 'shell.privilege_escalation'


def test_an_ordinary_command_matches_no_rule():
    verdict = DangerJudge().judge(_Runner(), {'command': 'ls -la'})
    assert verdict.level is RiskLevel.NONE
    assert verdict.reason


def test_a_tool_that_executes_nothing_is_never_judged_dangerous():
    """写一篇讲 rm -rf 的文档不该被当成要执行 rm -rf。"""
    verdict = DangerJudge().judge(_Scribe(), {'body': 'never run `rm -rf /` on production'})
    assert verdict.level is RiskLevel.NONE


def test_only_the_declared_command_arguments_are_inspected():
    verdict = DangerJudge().judge(_Runner(), {'command': 'ls', 'note': 'sudo rm -rf /'})
    assert verdict.level is RiskLevel.NONE


def test_a_tool_that_declares_nothing_has_all_of_its_string_arguments_inspected():
    """安全判定从严：没声明就按最坏情况看待，而不是按最好情况放行。"""
    verdict = DangerJudge().judge(_Undeclared(), {'anything': 'sudo reboot'})
    assert verdict.rule_id == 'shell.privilege_escalation'


def test_a_custom_ruleset_replaces_the_default_one():
    judge = DangerJudge(rules=[])
    assert judge.judge(_Runner(), {'command': 'sudo rm -rf /'}).level is RiskLevel.NONE


def test_blocking_policy_blocks_high_and_allows_the_rest():
    policy = BlockHighRisk()
    judge = DangerJudge()
    assert (
        policy.decide(judge.judge(_Runner(), {'command': 'sudo ls'}), _call('Runner'))
        is Disposition.BLOCK
    )
    assert (
        policy.decide(judge.judge(_Runner(), {'command': 'ls'}), _call('Runner'))
        is Disposition.ALLOW
    )


def test_allow_all_policy_lets_a_high_risk_call_through():
    """判定与处置分离：同一套规则在批处理里该阻断，在别的环境里可以由宿主自己接管。"""
    verdict = DangerJudge().judge(_Runner(), {'command': 'sudo ls'})
    assert verdict.level is RiskLevel.HIGH
    assert AllowAll().decide(verdict, _call('Runner')) is Disposition.ALLOW


def test_judging_the_same_arguments_twice_gives_the_same_verdict():
    """纯规则：不读配置、不调 LLM、不带状态，因此同样的输入必然同样的结论。"""
    judge = DangerJudge()
    args: dict[str, Any] = {'command': 'rm -rf build'}
    assert judge.judge(_Runner(), args) == judge.judge(_Runner(), args)
```

```python
# tests/kernel/test_tool.py  （追加）
def test_command_arguments_are_undeclared_by_default():
    """默认 None 而不是 ()：没声明必须按最坏情况处理，声明「我什么都不执行」得是显式动作。"""

    class _Plain(Tool):
        async def execute(self, text: str) -> ToolResult:
            """回显 text。"""
            return ToolResult(output=text)

    assert _Plain.__tool_command_args__ is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_danger.py tests/kernel/test_tool.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'syndica.kernel.danger'`

- [ ] **Step 3: 给 `Tool` 加一条身份声明**

在 `Tool` 的三个 ClassVar 之后加第四个：

```python
    __tool_name__: ClassVar[str | None] = None
    __tool_schema__: ClassVar[dict[str, Any] | None] = None
    __tool_model__: ClassVar[type[BaseModel] | None] = None
    # 这个工具的哪些参数会被**执行**（交给 shell、解释器、远端命令通道）。危险判定只看这些参数
    # （spec §5.6：判定是纯规则）。
    #
    # `None` 是「没声明」，判定按最坏情况看待——扫描全部字符串参数。声明 `()` 是「我什么都不
    # 执行」，判定于是完全跳过它。两者不能合并：一个写文档的工具若被当成执行工具，一篇讲
    # `rm -rf` 的文档就会被阻断；而一个执行工具若因为忘了声明就被跳过，那道闸等于不存在。
    # 默认取不安全的那一侧。
    __tool_command_args__: ClassVar[tuple[str, ...] | None] = None
```

- [ ] **Step 4: 写 `danger.py`**

```python
# src/syndica/kernel/danger.py
"""危险判定与处置——**分开的两件事**（spec §5.6）。

**判定是纯规则**：输入是工具与它这次的参数，输出是一个稳定的规则 ID、一个风险等级和一句原因。
它**不读配置、不调 LLM、不决定交互方式**，因此同样的输入必然给出同样的结论，可以被记录、被比对、
被当成事实引用。

**处置由宿主决定**：自动化批处理里危险命令应当直接阻断，交互式产品里应当询问用户。规则里若写死
处置，这套判定就无法跨场景复用——而规则本身是最值得复用的那部分。

**授权作用域只有「这一次调用」。** 不提供「此后均可」。长跑场景下 Agent 会执行数百轮，一次宽泛
授权可能在完全不同的上下文里被用掉——这是长跑特有的风险，短任务中感受不到，因此不能按短任务的
直觉来设计。本模块因此不持有任何「已授权」的状态。

**本计划只落地 `ALLOW` 与 `BLOCK` 两档处置的实现**（`BlockHighRisk` / `AllowAll`）。`CONFIRM`
这个取值现在就存在，因为并发分段要按「这次调用需不需要人工授权」判不安全（spec §5.6），没有这个
取值那条从严规则就无从表达；真正的人工确认交互随 CLI 落在计划 6。

**规则顺序就是优先级**：判定取第一个命中的规则，因此结论与参数在 dict 里的位置无关。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from syndica.kernel.context import ToolCall
from syndica.kernel.tool import Tool

__all__ = [
    'DEFAULT_RULES',
    'AllowAll',
    'BlockHighRisk',
    'DangerJudge',
    'DangerPolicy',
    'DangerRule',
    'DangerVerdict',
    'Disposition',
    'RiskLevel',
]


class RiskLevel(Enum):
    """风险等级。`NONE` 是「没有规则命中」，不是「已经确认安全」——规则表不完备是常态。"""

    NONE = 'none'
    LOW = 'low'
    HIGH = 'high'


class Disposition(Enum):
    """对一次调用的处置。"""

    ALLOW = 'allow'
    BLOCK = 'block'
    CONFIRM = 'confirm'


@dataclass(frozen=True)
class DangerVerdict:
    """判定结论。字段顺序计入公开 API。

    `rule_id` 是**稳定标识**：它会进日志、进轨迹、被宿主拿去做策略，改一个拼写就是破坏性变更。
    """

    rule_id: str
    level: RiskLevel
    reason: str


NO_RULE_MATCHED = DangerVerdict(
    rule_id='danger.no_rule_matched',
    level=RiskLevel.NONE,
    reason='没有规则命中这次调用的可执行参数；这表示规则表没认出它，不表示已经确认它安全',
)


@dataclass(frozen=True)
class DangerRule:
    """一条规则。`matches` 是纯函数：同样的文本必然同样的结论。"""

    rule_id: str
    level: RiskLevel
    reason: str
    matches: Callable[[str], bool]


# 边界用 `\A` 或分隔符起头，避免 `mysudo` / `format` 这类子串误命中。
_ESCALATION = re.compile(r'(?:\A|[\s;&|(])(?:sudo|doas)\s')
_REMOVE_WITH_FLAGS = re.compile(r'(?:\A|[\s;&|(])rm\s+((?:-\S+\s+)+)')
_DOWNLOAD_INTO_SHELL = re.compile(r'(?:\A|[\s;&|(])(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?\S*sh\b')
_RAW_DEVICE = re.compile(r'(?:\A|[\s;&|(])(?:mkfs\S*\s|dd\s[^\n]*\bof=/dev/)')


def _escalates_privilege(command: str) -> bool:
    return _ESCALATION.search(command) is not None


def _pipes_a_download_into_a_shell(command: str) -> bool:
    return _DOWNLOAD_INTO_SHELL.search(command) is not None


def _removes_recursively_and_forcibly(command: str) -> bool:
    """`rm` 的 flag 里同时出现 r 与 f 就算命中。

    不逐个枚举 `-rf` / `-fr` / `--recursive --force` 的拼法：枚举漏一种就是漏一道闸，而多认几种
    的代价只是一次误报，误报的代价是一次「换个方式」。
    """
    for match in _REMOVE_WITH_FLAGS.finditer(command):
        flags = match.group(1).lower()
        if 'r' in flags and 'f' in flags:
            return True
    return False


def _writes_a_raw_device(command: str) -> bool:
    return _RAW_DEVICE.search(command) is not None


DEFAULT_RULES: tuple[DangerRule, ...] = (
    DangerRule(
        rule_id='shell.privilege_escalation',
        level=RiskLevel.HIGH,
        reason='这次调用以 sudo / doas 提权执行，作用范围超出当前用户能承担的后果',
        matches=_escalates_privilege,
    ),
    DangerRule(
        rule_id='shell.remote_script_execution',
        level=RiskLevel.HIGH,
        reason='这次调用把网络下载的内容直接交给 shell 执行，执行前没有任何人看过它的内容',
        matches=_pipes_a_download_into_a_shell,
    ),
    DangerRule(
        rule_id='shell.recursive_force_remove',
        level=RiskLevel.HIGH,
        reason='这次调用递归且强制删除，删掉的东西没有回收站也没有确认',
        matches=_removes_recursively_and_forcibly,
    ),
    DangerRule(
        rule_id='shell.raw_device_write',
        level=RiskLevel.HIGH,
        reason='这次调用直接写块设备或重建文件系统，一次成功就没有原状可回',
        matches=_writes_a_raw_device,
    ),
)


def _inspected_values(tool: Tool, args: Mapping[str, Any]) -> list[str]:
    declared = tool.__tool_command_args__
    picked: list[str] = []
    if declared is None:
        for value in args.values():
            if isinstance(value, str):
                picked.append(value)
        return picked
    for name in declared:
        value = args.get(name)
        if isinstance(value, str):
            picked.append(value)
    return picked


class DangerJudge:
    """按规则表给一次调用下结论。无状态、无副作用。"""

    def __init__(self, rules: Sequence[DangerRule] | None = None) -> None:
        self._rules: tuple[DangerRule, ...] = tuple(
            DEFAULT_RULES if rules is None else rules
        )

    def judge(self, tool: Tool, args: Mapping[str, Any]) -> DangerVerdict:
        """取第一个命中的规则。

        外层遍历规则、内层遍历参数：结论因此只取决于**规则的声明顺序**，与参数在 dict 里的位置
        无关。反过来写会让同一条命令换个参数名就换个规则 ID，而规则 ID 是要进日志被比对的。
        """
        values = _inspected_values(tool, args)
        for rule in self._rules:
            for value in values:
                if rule.matches(value):
                    return DangerVerdict(
                        rule_id=rule.rule_id, level=rule.level, reason=rule.reason
                    )
        return NO_RULE_MATCHED


@runtime_checkable
class DangerPolicy(Protocol):
    """把结论变成动作。宿主可以整个换掉它。"""

    def decide(self, verdict: DangerVerdict, call: ToolCall) -> Disposition: ...


class BlockHighRisk:
    """内核默认处置：高风险直接阻断，其余放行。

    默认阻断而不是询问：本计划还没有人工确认通道，而**拒绝比等待有用**——Agent 立刻拿到「这条
    路被规则挡住了」这个明确信息，就能换一种做法；等一个不会有人来看的确认只是白耗一段超时
    （spec §12.2 的同一条理由）。
    """

    def decide(self, verdict: DangerVerdict, call: ToolCall) -> Disposition:
        return Disposition.BLOCK if verdict.level is RiskLevel.HIGH else Disposition.ALLOW


class AllowAll:
    """一律放行。给自己已经在别处做了闸的宿主（如整个跑在一次性容器里的批处理）。"""

    def decide(self, verdict: DangerVerdict, call: ToolCall) -> Disposition:
        return Disposition.ALLOW
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/kernel/test_danger.py tests/kernel/test_tool.py -v && uv run ruff check . && uv run pyright`
Expected: 全部 passed

- [ ] **Step 6: 提交**

```bash
git add src/syndica/kernel/danger.py src/syndica/kernel/tool.py tests/kernel/test_danger.py tests/kernel/test_tool.py
git commit -m "feat(kernel): 危险判定是纯规则，处置留给宿主

同一套规则在自动化批处理里该直接阻断，在交互式产品里该询问用户。规则里写死处置，
这套判定就无法跨场景复用——而规则本身正是最值得复用的那部分。所以判定只输出稳定的
规则 ID、风险等级和原因，不读配置、不调 LLM、不决定交互方式。

__tool_command_args__ 默认 None 而不是 ()：没声明按最坏情况处理（扫全部字符串参数），
声明「我什么都不执行」得是显式动作。两者不能合并——写文档的工具若被当成执行工具，
一篇讲 rm -rf 的文档就会被阻断；执行工具若因为忘了声明被跳过，那道闸等于不存在。

规则外层、参数内层：结论只取决于规则的声明顺序，与参数在 dict 里的位置无关。反过来
写会让同一条命令换个参数名就换个规则 ID，而规则 ID 是要进日志被比对的。

CONFIRM 这个取值现在就存在但没有实现：并发分段要按「这次调用需不需要人工授权」判
不安全，没有这个取值那条从严规则就无从表达。交互留给 CLI。"
```

---

## Task 11: 处置接进 ToolExecutor——阻断与放行

**Files:**
- Modify: `src/syndica/kernel/hooks/junctions.py`
- Modify: `src/syndica/kernel/tool_executor.py`
- Modify: `src/syndica/kernel/agent.py`
- Test: `tests/kernel/test_tool_executor.py`
- Test: `tests/kernel/test_hooks.py`

**Interfaces:**
- Consumes: Task 10 的 `DangerJudge` / `DangerPolicy` / `Disposition`
- Produces:
  - `Junction.TOOL_CONFIRMATION_REQUIRED`（可观察）
  - `ToolExecutor(tools, hooks, *, judge=None, policy=None)`
  - `AgentOptions.danger_judge` / `AgentOptions.danger_policy`
  - 被阻断的调用**仍然产出恰好一个 `role='tool'` block**，内容是三件套

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_tool_executor.py  （追加）
class _Runner(Tool):
    """把 command 交给 shell 执行。"""

    __tool_command_args__ = ('command',)

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, command: str) -> ToolResult:
        """执行 command。"""
        self.executed.append(command)
        return ToolResult(output=f'ran {command}')


async def test_a_blocked_call_still_produces_exactly_one_tool_block_and_never_executes():
    tool = _Runner()
    executor = ToolExecutor(tools={'Runner': tool}, hooks=HooksCenter())
    ctx = Context()

    await executor.execute_tool_calls(
        ctx,
        [ToolCall(id='c1', name='Runner', arguments='{"command":"sudo rm -rf /"}')],
    )

    blocks = ctx.current_traj
    assert [block.role for block in blocks] == ['tool']
    assert blocks[0].tool_call_id == 'c1'
    assert tool.executed == []
    assert isinstance(blocks[0].content, str)
    assert 'shell.privilege_escalation' in blocks[0].content
    assert '下一步' in blocks[0].content


async def test_a_harmless_call_is_not_blocked():
    tool = _Runner()
    executor = ToolExecutor(tools={'Runner': tool}, hooks=HooksCenter())
    ctx = Context()

    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Runner', arguments='{"command":"ls -la"}')]
    )

    assert tool.executed == ['ls -la']


async def test_the_gate_judges_the_arguments_a_hook_rewrote_not_the_ones_the_model_sent():
    """闸必须看真正会被执行的那份参数，否则一个改写参数的 hook 就能绕过它。"""
    tool = _Runner()
    hooks = HooksCenter()

    async def escalate(**kwargs: object) -> HookResult:
        return HookResult(modified={'args': {'command': 'sudo rm -rf /'}})

    hooks.register(Junction.PRE_TOOL_USE, escalate)
    executor = ToolExecutor(tools={'Runner': tool}, hooks=hooks)
    ctx = Context()

    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Runner', arguments='{"command":"ls"}')]
    )

    assert tool.executed == []
    assert isinstance(ctx.current_traj[0].content, str)
    assert 'shell.privilege_escalation' in ctx.current_traj[0].content


async def test_a_pre_hook_that_skips_the_call_bypasses_the_gate_because_nothing_runs():
    tool = _Runner()
    hooks = HooksCenter()

    async def substitute(**kwargs: object) -> HookResult:
        return HookResult(skip=True, result=ToolResult(output='cached'))

    hooks.register(Junction.PRE_TOOL_USE, substitute)
    executor = ToolExecutor(tools={'Runner': tool}, hooks=hooks)
    ctx = Context()

    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Runner', arguments='{"command":"sudo rm -rf /"}')]
    )

    assert ctx.current_traj[0].content == 'cached'
    assert tool.executed == []


async def test_a_confirm_disposition_fires_the_confirmation_junction_and_does_not_execute():
    """人工确认的交互在计划 6；此刻只保证这条路不会静默变成放行。"""
    tool = _Runner()
    hooks = HooksCenter()
    seen: list[str] = []

    async def record(**kwargs: object) -> None:
        verdict = kwargs['verdict']
        seen.append(getattr(verdict, 'rule_id'))

    hooks.register(Junction.TOOL_CONFIRMATION_REQUIRED, record)

    class _AlwaysConfirm:
        def decide(self, verdict, call) -> Disposition:
            return Disposition.CONFIRM

    executor = ToolExecutor(tools={'Runner': tool}, hooks=hooks, policy=_AlwaysConfirm())
    ctx = Context()

    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Runner', arguments='{"command":"sudo ls"}')]
    )

    assert seen == ['shell.privilege_escalation']
    assert tool.executed == []
    assert len(ctx.current_traj) == 1


async def test_a_host_policy_can_take_over_the_gate_entirely():
    tool = _Runner()
    executor = ToolExecutor(tools={'Runner': tool}, hooks=HooksCenter(), policy=AllowAll())
    ctx = Context()

    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Runner', arguments='{"command":"sudo ls"}')]
    )

    assert tool.executed == ['sudo ls']


async def test_blocking_one_call_does_not_stop_the_rest_of_the_batch():
    """阻断不是失败：模型换个做法就行，本批其余调用照常执行。"""
    tool = _Runner()
    executor = ToolExecutor(tools={'Runner': tool}, hooks=HooksCenter())
    ctx = Context()

    await executor.execute_tool_calls(
        ctx,
        [
            ToolCall(id='c1', name='Runner', arguments='{"command":"echo beta"}'),
            ToolCall(id='c2', name='Runner', arguments='{"command":"sudo rm -rf /"}'),
            ToolCall(id='c3', name='Runner', arguments='{"command":"echo alpha"}'),
        ],
    )

    assert [block.tool_call_id for block in ctx.current_traj] == ['c1', 'c2', 'c3']
    assert tool.executed == ['echo beta', 'echo alpha']
```

```python
# tests/kernel/test_hooks.py  （枚举表与分类断言再更新一次；这次是最终的 16 项）
def test_sixteen_junctions_exist():
    assert {junction.name: junction.value for junction in Junction} == {
        'RUNTIME_SHUTDOWN': 'runtime_shutdown',
        'AGENT_SPAWN': 'agent_spawn',
        'AGENT_DESPAWN': 'agent_despawn',
        'AGENT_START': 'agent_start',
        'AGENT_END': 'agent_end',
        'AGENT_ERROR': 'agent_error',
        'TURN_START': 'turn_start',
        'TURN_END': 'turn_end',
        'MESSAGE_INJECTED': 'message_injected',
        'PRE_TOOL_USE': 'pre_tool_use',
        'POST_TOOL_USE': 'post_tool_use',
        'TOOL_CONFIRMATION_REQUIRED': 'tool_confirmation_required',
        'PRE_LLM_REQUEST': 'pre_llm_request',
        'POST_LLM_RESPONSE': 'post_llm_response',
        'CONTEXT_OVERFLOW': 'context_overflow',
        'LLM_FAILURE': 'llm_failure',
    }
```

（`OBSERVABLE` 的确切内容断言同步加上 `Junction.TOOL_CONFIRMATION_REQUIRED`。）

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_tool_executor.py tests/kernel/test_hooks.py -v`
Expected: FAIL —— `ToolExecutor` 不接受 `policy`；`Junction` 没有 `TOOL_CONFIRMATION_REQUIRED`

- [ ] **Step 3: 加上确认切点**

`Junction` 里 `POST_TOOL_USE` 之后插入 `TOOL_CONFIRMATION_REQUIRED = 'tool_confirmation_required'`，并加入 `OBSERVABLE`。

> 它是**可观察**的：它报告「这次调用需要人工授权」这个判定结果，改不了任何东西。做成可拦截等于
> 让一个 hook 能把闸放掉，而 hook 只能收紧、不能放宽——这与信任级别那条规则是同一条。

- [ ] **Step 4: 把闸接进 `tool_executor.py`**

import 补：

```python
from syndica.kernel.danger import BlockHighRisk, DangerJudge, DangerPolicy, Disposition, DangerVerdict
```

模块 docstring 补一段：

```
**危险判定的闸坐在 `PRE_TOOL_USE` 之后、执行之前。** 它必须看**真正会被执行的那份参数**——
`PRE_TOOL_USE` 是参数的最后一个写者，把闸放在它之前，一个改写参数的 hook 就能绕过整道闸。放在
它之后不会让 hook 有机会放宽：闸重新判定的正是 hook 自己的产出。`skip=True` 的 hook 顶替了这次
调用，什么都没执行，因此也没有可判的东西。

**被阻断的调用仍然产出恰好一个 `role='tool'` block。** 阻断不是失败，是一次「这条路走不通」的
回答，模型据此换一种做法。它写给模型看，因此按三件套写：哪条规则、为什么、下一步怎么办。
```

新增两个模块级函数：

```python
def _gated_result(
    call: ToolCall, verdict: DangerVerdict, disposition: Disposition
) -> ToolResult:
    """被闸拦下的调用交给模型的那一块。

    两种措辞不可互换：阻断是**确定的否**（换做法），需要确认是**还没有人回答**（这个 Runtime 上
    没有确认通道）。塌成一句，模型就分不清该换做法还是该等。
    """
    if disposition is Disposition.CONFIRM:
        return ToolResult(
            output=(
                f'期望: {call.name}（id={call.id}）这次调用在执行前得到人工确认。'
                f' 实际: 规则 {verdict.rule_id} 判定风险等级 {verdict.level.value}'
                f'（{verdict.reason}），而当前 Runtime 没有装人工确认通道，因此没有执行、'
                '也没有产生任何副作用。'
                ' 下一步: 换一种不触发该规则的做法，或把这一步留给能确认它的人。'
            )
        )
    return ToolResult(
        output=(
            f'期望: {call.name}（id={call.id}）不触发危险规则。'
            f' 实际: 规则 {verdict.rule_id} 判定风险等级 {verdict.level.value}'
            f'（{verdict.reason}），本次调用已被阻断，没有执行、也没有产生任何副作用。'
            ' 下一步: 换一种达成同一目标的做法——缩小作用范围、去掉提权、避开不可逆操作。'
            '这次阻断不会被记住，也不存在「此后均可」的放行，同样的调用重来一次仍会被阻断。'
        )
    )
```

`ToolExecutor.__init__` 与 `_execute_one` 的改动：

```python
    def __init__(
        self,
        tools: dict[str, Tool],
        hooks: HooksCenter,
        *,
        judge: DangerJudge | None = None,
        policy: DangerPolicy | None = None,
    ) -> None:
        validate_tool_names(tools)
        self._tools = tools
        self._hooks = hooks
        self._judge = judge if judge is not None else DangerJudge()
        self._policy = policy if policy is not None else BlockHighRisk()
```

`_execute_one` 里，在 `args = cast(dict[str, Any], rewritten_args)` 之后、`try: returned = ...` 之前插入：

```python
        verdict = self._judge.judge(tool, args)
        disposition = self._policy.decide(verdict, call)
        if disposition is not Disposition.ALLOW:
            if disposition is Disposition.CONFIRM:
                await self._hooks.trigger(
                    Junction.TOOL_CONFIRMATION_REQUIRED,
                    tool_call=call,
                    tool=tool,
                    args=args,
                    verdict=verdict,
                )
            return _gated_result(call, verdict, disposition)
```

- [ ] **Step 5: 让 `AgentOptions` 能换掉判定与处置**

`AgentOptions` 末尾追加两个字段（**追加在末尾**：字段顺序计入公开 API）：

```python
    max_turns: int | None = 50
    danger_judge: DangerJudge | None = None
    danger_policy: DangerPolicy | None = None
```

`BaseAgent.__init__` 把它们传下去：

```python
        self._executor = ToolExecutor(
            tools=options.tools,
            hooks=hooks,
            judge=options.danger_judge,
            policy=options.danger_policy,
        )
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests -v && uv run ruff check . && uv run pyright`
Expected: 全部 passed

- [ ] **Step 7: 提交**

```bash
git add src/syndica/kernel/hooks/junctions.py src/syndica/kernel/tool_executor.py src/syndica/kernel/agent.py tests/kernel/test_tool_executor.py tests/kernel/test_hooks.py
git commit -m "feat(kernel): 闸判定的是真正会被执行的那份参数

PRE_TOOL_USE 是参数的最后一个写者。把闸放在它之前，一个改写参数的 hook 就能绕过
整道闸；放在它之后不会让 hook 有机会放宽，因为闸重新判定的正是 hook 自己的产出。
这与「hook 只能收紧信任级别不能放宽」是同一条规则的另一面。

被阻断的调用仍然产出恰好一个 role='tool' block。阻断不是失败而是一次「这条路走不
通」的回答——模型据此换做法，而 provider 会拒掉一个声明了 N 个调用却少于 N 条结果
的 assistant 消息。

阻断与需要确认两种措辞不可互换：前者是确定的否（换做法），后者是还没有人回答
（这个 Runtime 上没有确认通道）。塌成一句，模型就分不清该换做法还是该等。

tool_confirmation_required 做成可观察切点：它报告判定结果，改不了任何东西。做成
可拦截等于让一个 hook 能把闸放掉。"
```

---

## Task 12: 工具并发分段

**Files:**
- Modify: `src/syndica/kernel/tool_executor.py`
- Modify: `src/syndica/kernel/agent.py`
- Test: `tests/kernel/test_tool_executor.py`

**Interfaces:**
- Consumes: Task 11 的 `judge` / `policy`
- Produces:
  - `ToolExecutor(..., max_concurrency=DEFAULT_MAX_TOOL_CONCURRENCY)`、`AgentOptions.max_tool_concurrency`
  - `_segments(safety, limit)`：纯函数，把一批调用切成可以一起跑的段
  - 结果块**始终按调用顺序**落进轨迹，与完成顺序无关

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_tool_executor.py  （追加）
class _Probe(Tool):
    """并发探针：能在栅栏上会合就说明它们真的一起在跑。"""

    __tool_command_args__ = ()

    def __init__(self, barrier: asyncio.Barrier) -> None:
        self._barrier = barrier

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        return True

    async def execute(self, label: str) -> ToolResult:
        """在栅栏上会合一次然后回显 label。"""
        try:
            await asyncio.wait_for(self._barrier.wait(), timeout=0.5)
        except (TimeoutError, asyncio.BrokenBarrierError):
            return ToolResult(output=f'{label}:alone')
        return ToolResult(output=f'{label}:together')


class _Solo(Tool):
    """默认的不可并发工具。"""

    __tool_command_args__ = ()

    async def execute(self, label: str) -> ToolResult:
        """回显 label。"""
        return ToolResult(output=label)


class _Chained(Tool):
    """按 waits_for 指定的顺序完成，用来把完成顺序与调用顺序拧反。"""

    __tool_command_args__ = ()

    def __init__(self, gates: dict[str, asyncio.Event]) -> None:
        self._gates = gates

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        return True

    async def execute(self, label: str, waits_for: str = '') -> ToolResult:
        """等 waits_for 完成后回显 label。"""
        if waits_for:
            await self._gates[waits_for].wait()
        self._gates[label].set()
        return ToolResult(output=label)


def _probe_call(index: int, label: str) -> ToolCall:
    return ToolCall(id=f'c{index}', name='Probe', arguments=f'{{"label":"{label}"}}')


def test_segments_split_on_every_unsafe_call_and_chunk_by_the_limit():
    """纯函数：基数 6、安全位既非全真亦非全假，且不安全的位置不在两端。"""
    assert _segments([True, True, False, True, True, True], 2) == [
        [0, 1],
        [2],
        [3, 4],
        [5],
    ]


def test_a_single_unsafe_call_still_gets_its_own_segment():
    assert _segments([False], 4) == [[0]]


def test_an_empty_batch_produces_no_segments():
    assert _segments([], 4) == []


async def test_three_safe_calls_within_the_limit_really_run_together():
    barrier = asyncio.Barrier(3)
    executor = ToolExecutor(
        tools={'Probe': _Probe(barrier)}, hooks=HooksCenter(), max_concurrency=4
    )
    ctx = Context()

    await executor.execute_tool_calls(
        ctx,
        [_probe_call(1, 'beta'), _probe_call(2, 'alpha'), _probe_call(3, 'gamma')],
    )

    assert [block.content for block in ctx.current_traj] == [
        'beta:together',
        'alpha:together',
        'gamma:together',
    ]


async def test_the_concurrency_limit_is_respected():
    """上限 2、三个安全调用：前两个会合，第三个只能自己跑。"""
    barrier = asyncio.Barrier(2)
    executor = ToolExecutor(
        tools={'Probe': _Probe(barrier)}, hooks=HooksCenter(), max_concurrency=2
    )
    ctx = Context()

    await executor.execute_tool_calls(
        ctx,
        [_probe_call(1, 'beta'), _probe_call(2, 'alpha'), _probe_call(3, 'gamma')],
    )

    assert [block.content for block in ctx.current_traj] == [
        'beta:together',
        'alpha:together',
        'gamma:alone',
    ]


async def test_an_unsafe_call_in_the_middle_keeps_the_two_sides_from_running_together():
    """不安全的那个可能正是要改它们都会读的东西，因此它把批次切开。"""
    barrier = asyncio.Barrier(2)
    executor = ToolExecutor(
        tools={'Probe': _Probe(barrier), 'Solo': _Solo()},
        hooks=HooksCenter(),
        max_concurrency=4,
    )
    ctx = Context()

    await executor.execute_tool_calls(
        ctx,
        [
            _probe_call(1, 'beta'),
            ToolCall(id='c2', name='Solo', arguments='{"label":"alpha"}'),
            _probe_call(3, 'gamma'),
        ],
    )

    assert [block.content for block in ctx.current_traj] == [
        'beta:alone',
        'alpha',
        'gamma:alone',
    ]


async def test_a_call_needing_authorization_is_never_run_concurrently():
    """spec §5.6 的从严三条之一：需要人工授权一律判不安全。"""
    barrier = asyncio.Barrier(2)

    class _Dangerous(Tool):
        __tool_command_args__ = ('command',)

        def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
            return True

        async def execute(self, command: str) -> ToolResult:
            """执行 command。"""
            return ToolResult(output=command)

    executor = ToolExecutor(
        tools={'Probe': _Probe(barrier), 'Dangerous': _Dangerous()},
        hooks=HooksCenter(),
        max_concurrency=4,
    )
    ctx = Context()

    await executor.execute_tool_calls(
        ctx,
        [
            _probe_call(1, 'beta'),
            ToolCall(id='c2', name='Dangerous', arguments='{"command":"sudo ls"}'),
            _probe_call(3, 'gamma'),
        ],
    )

    assert [block.content for block in ctx.current_traj][0] == 'beta:alone'
    assert [block.content for block in ctx.current_traj][2] == 'gamma:alone'


@pytest.mark.parametrize(
    'call',
    [
        ToolCall(id='c2', name='Nope', arguments='{}'),
        ToolCall(id='c2', name='Probe', arguments='{not json'),
    ],
)
async def test_an_unknown_tool_or_unparsable_arguments_also_split_the_batch(call: ToolCall):
    barrier = asyncio.Barrier(2)
    executor = ToolExecutor(
        tools={'Probe': _Probe(barrier)}, hooks=HooksCenter(), max_concurrency=4
    )
    ctx = Context()

    await executor.execute_tool_calls(
        ctx, [_probe_call(1, 'beta'), call, _probe_call(3, 'gamma')]
    )

    contents = [block.content for block in ctx.current_traj]
    assert contents[0] == 'beta:alone'
    assert contents[2] == 'gamma:alone'


async def test_result_blocks_follow_call_order_not_completion_order():
    """一次调用恰好一个结果块，而且位置由调用顺序决定——配对靠的是相邻性。"""
    gates = {label: asyncio.Event() for label in ('beta', 'alpha', 'gamma')}
    executor = ToolExecutor(
        tools={'Chained': _Chained(gates)}, hooks=HooksCenter(), max_concurrency=4
    )
    ctx = Context()

    await executor.execute_tool_calls(
        ctx,
        [
            ToolCall(
                id='c1', name='Chained', arguments='{"label":"beta","waits_for":"alpha"}'
            ),
            ToolCall(
                id='c2', name='Chained', arguments='{"label":"alpha","waits_for":"gamma"}'
            ),
            ToolCall(id='c3', name='Chained', arguments='{"label":"gamma"}'),
        ],
    )

    assert [block.tool_call_id for block in ctx.current_traj] == ['c1', 'c2', 'c3']
    assert [block.content for block in ctx.current_traj] == ['beta', 'alpha', 'gamma']


async def test_a_failure_inside_a_concurrent_segment_still_pairs_every_call():
    class _Bomb(Tool):
        __tool_command_args__ = ()

        def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
            return True

        async def execute(self, label: str) -> ToolResult:
            """炸。"""
            raise ContractViolation(
                expected='never', received='always', next_action='fix the test tool'
            )

    barrier = asyncio.Barrier(2)
    executor = ToolExecutor(
        tools={'Probe': _Probe(barrier), 'Bomb': _Bomb()},
        hooks=HooksCenter(),
        max_concurrency=4,
    )
    ctx = Context()

    with pytest.raises(ContractViolation):
        await executor.execute_tool_calls(
            ctx,
            [
                _probe_call(1, 'beta'),
                ToolCall(id='c2', name='Bomb', arguments='{"label":"alpha"}'),
                _probe_call(3, 'gamma'),
            ],
        )

    assert [block.tool_call_id for block in ctx.current_traj] == ['c1', 'c2', 'c3']


def test_a_concurrency_limit_below_one_is_refused():
    with pytest.raises(ConfigError):
        ToolExecutor(tools={}, hooks=HooksCenter(), max_concurrency=0)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_tool_executor.py -v`
Expected: FAIL —— 没有 `_segments`；`ToolExecutor` 不接受 `max_concurrency`

- [ ] **Step 3: 加上分段执行**

import 补 `import asyncio`（并入既有 stdlib 块），`__all__` 加 `'DEFAULT_MAX_TOOL_CONCURRENCY'`。模块 docstring 补一段：

```
**分段是调度决策，闸是安全决策，两者看的参数不同。** 分段必须在任何 hook 跑之前算完——那时才有
一整批可分；因此它看的是**模型发出来的**那份参数。闸看的是 `PRE_TOOL_USE` 改写之后、真正会被
执行的那份。后果是：一个改写参数的 hook 不会重新分段。这是有意的取舍，而且是安全的那一侧——
被闸拦下的调用根本不执行，所以「它曾经和别人并发过」不会造成任何副作用。

**只有相邻的安全调用会被并到一段。** 一个不安全的调用把批次切开，它前后的调用因此不会跨过它
并发跑——不安全的那个可能正是要改它们都会读的东西。
```

模块级新增：

```python
DEFAULT_MAX_TOOL_CONCURRENCY = 4


def _parsed_arguments(call: ToolCall) -> dict[str, Any] | None:
    """解析一次调用的参数；解析不出来返回 None。

    分段与执行都要这份结果，但它们各解析一次而不是共享一份：分段的产物只用来决定「谁能和谁一起
    跑」，执行路径必须从原始字符串重新走一遍完整的错误分支（不同的失败要给模型不同的措辞）。
    """
    if not call.arguments.strip():
        return {}
    try:
        decoded = json.loads(call.arguments)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return cast(dict[str, Any], decoded)


def _segments(safety: Sequence[bool], limit: int) -> list[list[int]]:
    """把一批调用切成可以一起跑的段，返回逐段的下标列表。

    纯函数，因此「怎么切」这件事可以脱离工具、hook 与事件循环单独验证。
    """
    segments: list[list[int]] = []
    current: list[int] = []
    for index, is_safe in enumerate(safety):
        if not is_safe:
            if current:
                segments.append(current)
                current = []
            segments.append([index])
            continue
        current.append(index)
        if len(current) == limit:
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return segments
```

`__init__` 加上限并校验：

```python
        judge: DangerJudge | None = None,
        policy: DangerPolicy | None = None,
        max_concurrency: int = DEFAULT_MAX_TOOL_CONCURRENCY,
    ) -> None:
        if max_concurrency < 1:
            raise ConfigError(
                expected='max_concurrency 是正整数：一段里最多同时跑几个工具',
                received=f'收到 max_concurrency={max_concurrency!r}',
                next_action='传一个 >= 1 的值；要完全串行就传 1',
            )
        ...
        self._max_concurrency = max_concurrency
```

`execute_tool_calls` 换成分段版：

```python
    async def execute_tool_calls(self, ctx: Context, tool_calls: list[ToolCall]) -> None:
        safety = [self._concurrency_safe(call) for call in tool_calls]
        handled = 0
        failure: Exception | None = None
        failed_call: ToolCall | None = None
        for segment in _segments(safety, self._max_concurrency):
            outcomes = await self._run_segment([tool_calls[index] for index in segment])
            # 先整段扫一遍取消：取消不该在展开的路上往轨迹里写东西，所以这一段一个 block 都不补。
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
                    raise outcome
            for index, outcome in zip(segment, outcomes, strict=True):
                call = tool_calls[index]
                if isinstance(outcome, Exception):
                    if failure is None:
                        failure, failed_call = outcome, call
                    # 同段里失败的每一个都如实说自己中止了：它们真的跑过，副作用未知。
                    self._append(ctx, call, _aborted_result(call, outcome))
                else:
                    self._append(ctx, call, outcome)
                handled += 1
            if failure is not None:
                break
        if failure is not None and failed_call is not None:
            for pending in tool_calls[handled:]:
                self._append(ctx, pending, _not_executed_result(pending, failed_call, failure))
            raise failure

    async def _run_segment(self, calls: list[ToolCall]) -> list[Any]:
        if len(calls) == 1:
            try:
                return [await self._execute_one(calls[0])]
            # Exception 而非 BaseException：取消照常上抛，不被收进结果列表。
            except Exception as exc:
                return [exc]
        outcomes: list[Any] = await asyncio.gather(
            *[self._execute_one(call) for call in calls], return_exceptions=True
        )
        return outcomes

    def _concurrency_safe(self, call: ToolCall) -> bool:
        """这次调用能不能与同一批里相邻的调用一起跑。**判定从严**（spec §5.6）。

        参数解析失败、工具不存在、需要人工授权——一律判不安全；排除了这三种之后才轮到工具自己
        回答。从严的理由是代价不对称：并发执行两个改同一份文件的工具，或并发执行一个本该人工
        确认的危险命令，后果不可逆；串行的代价只是慢一点。
        """
        tool = self._tools.get(call.name)
        if tool is None:
            return False
        args = _parsed_arguments(call)
        if args is None:
            return False
        if self._policy.decide(self._judge.judge(tool, args), call) is not Disposition.ALLOW:
            return False
        return tool.is_concurrency_safe(args)
```

`_execute_one` 里的参数解析改用共享的 helper（把原来那段 `json.loads` 换成下面这段，错误分支的
措辞一字不改）：

```python
        args: dict[str, Any] = {}
        if call.arguments.strip():
            try:
                decoded = json.loads(call.arguments)
            except json.JSONDecodeError as exc:
                return _bad_arguments_result(call, tool, f'解析失败于 {exc.msg}（位置 {exc.pos}）')
            if not isinstance(decoded, dict):
                return _bad_arguments_result(
                    call, tool, f'解析出的是 {type(decoded).__name__}，不是 JSON 对象'
                )
            args = cast(dict[str, Any], decoded)
```

> 这一段与计划 1 逐字相同——`_parsed_arguments` 只服务于分段，执行路径仍然要走各自的错误措辞。

- [ ] **Step 4: 让 `AgentOptions` 能配上限**

```python
    danger_judge: DangerJudge | None = None
    danger_policy: DangerPolicy | None = None
    max_tool_concurrency: int = DEFAULT_MAX_TOOL_CONCURRENCY
```

```python
        self._executor = ToolExecutor(
            tools=options.tools,
            hooks=hooks,
            judge=options.danger_judge,
            policy=options.danger_policy,
            max_concurrency=options.max_tool_concurrency,
        )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests -v && uv run ruff check . && uv run pyright`
Expected: 全部 passed

- [ ] **Step 6: 提交**

```bash
git add src/syndica/kernel/tool_executor.py src/syndica/kernel/agent.py tests/kernel/test_tool_executor.py
git commit -m "feat(kernel): 并发分段，安全判定从严

代价不对称：并发执行两个改同一份文件的工具，或并发执行一个本该人工确认的危险命令，
后果不可逆；串行的代价只是慢一点。所以参数解析失败、工具不存在、需要人工授权一律
判不安全，排除这三种之后才轮到工具自己回答。

只有相邻的安全调用并到一段：不安全的那个可能正是要改它们都会读的东西，它因此把批次
切开，两侧不会跨过它并发跑。

分段是调度决策、闸是安全决策，两者看的参数不同：分段必须在任何 hook 跑之前算完（那时
才有一整批可分），闸看的是 hook 改写之后真正会被执行的那份。一个改写参数的 hook 因此
不会重新分段——这是安全的那一侧，被闸拦下的调用根本不执行。

结果块按调用顺序落，与完成顺序无关：配对靠的是相邻性，而 provider 会拒掉配对断裂的
消息。同段里失败的每一个都如实说自己中止了（它们真的跑过，副作用未知），只有从未
执行的才否认副作用。"
```

---
## Task 13: 三层装配的前两层——Agent 局部钩子与 Runtime 级插件

**Files:**
- Create: `src/syndica/kernel/plugins.py`
- Modify: `src/syndica/kernel/hooks/center.py`
- Modify: `src/syndica/kernel/agent.py`
- Modify: `src/syndica/kernel/runtime.py`
- Test: `tests/kernel/test_plugins.py`
- Test: `tests/kernel/test_hooks.py`

**Interfaces:**
- Consumes: 既有 `HooksCenter`
- Produces:
  - `HooksCenter(*, outer=None)`：两层合成**一条**链，本层在前
  - `BaseAgent.register_hook(junction, cb)`（spec §5.3 第一层）
  - `Plugin` Protocol、`install_plugins(hooks, plugins)`、`Runtime(plugins=[...])`、只读 `Runtime.plugins`

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_plugins.py
from __future__ import annotations

import asyncio

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import ToolCall
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction, JunctionKind
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.plugins import install_plugins
from syndica.kernel.runtime import Runtime
from syndica.kernel.tool import Tool, ToolResult

_TIMEOUT = 2.0


class _Echo(Tool):
    __tool_command_args__ = ()

    async def execute(self, text: str) -> ToolResult:
        """回显 text。"""
        return ToolResult(output=text)


class _Recorder:
    """把自己的名字记进共享列表的插件。名字与执行顺序都是被测性质。"""

    def __init__(self, name: str, log: list[str]) -> None:
        self._name = name
        self._log = log

    @property
    def name(self) -> str:
        return self._name

    def register(self, hooks: HooksCenter) -> None:
        async def record(**kwargs: object) -> None:
            self._log.append(self._name)

        hooks.register(Junction.PRE_TOOL_USE, record)


def _agent(hooks: HooksCenter) -> BaseAgent:
    return BaseAgent(
        AgentOptions(
            name='Worker',
            llm=ScriptedLLM(
                [
                    ScriptedTurn(
                        tool_calls=[ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
                    ),
                    ScriptedTurn(content='done'),
                ]
            ),
            tools={'Echo': _Echo()},
            system_prompt='SP',
        ),
        hooks=hooks,
        events=EventBus(),
    )


async def test_agent_local_hooks_run_before_runtime_level_ones():
    """spec §5.3：Agent 局部先于 Runtime 级。基数 3，且顺序既非升序亦非降序。"""
    log: list[str] = []
    shared = HooksCenter()
    install_plugins(shared, [_Recorder('beta', log), _Recorder('alpha', log)])
    agent = _agent(shared)

    async def local(**kwargs: object) -> None:
        log.append('local')

    agent.register_hook(Junction.PRE_TOOL_USE, local)
    await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert log == ['local', 'beta', 'alpha']


async def test_a_local_skip_terminates_the_runtime_level_hooks_too():
    """两层是一条链，不是两条：skip 若只终止本层，Runtime 级的 hook 会跑在一个已被顶替的操作上。"""
    log: list[str] = []
    shared = HooksCenter()
    install_plugins(shared, [_Recorder('beta', log)])
    agent = _agent(shared)

    async def veto(**kwargs: object) -> HookResult:
        return HookResult(skip=True, result=ToolResult(output='vetoed'))

    agent.register_hook(Junction.PRE_TOOL_USE, veto)
    ctx = await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert log == []
    assert [block.content for block in ctx.current_traj if block.role == 'tool'] == ['vetoed']


async def test_a_local_rewrite_reaches_the_runtime_level_hook():
    seen: list[object] = []
    shared = HooksCenter()

    async def observe(**kwargs: object) -> None:
        seen.append(kwargs.get('args'))

    shared.register(Junction.PRE_TOOL_USE, observe)
    agent = _agent(shared)

    async def rewrite(**kwargs: object) -> HookResult:
        return HookResult(modified={'args': {'text': 'rewritten'}})

    agent.register_hook(Junction.PRE_TOOL_USE, rewrite)
    ctx = await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert seen == [{'text': 'rewritten'}]
    assert [block.content for block in ctx.current_traj if block.role == 'tool'] == ['rewritten']


async def test_a_hook_registered_on_the_agent_does_not_leak_to_a_sibling():
    log: list[str] = []
    shared = HooksCenter()
    first = _agent(shared)
    second = _agent(shared)

    async def local(**kwargs: object) -> None:
        log.append('local')

    first.register_hook(Junction.PRE_TOOL_USE, local)
    await asyncio.wait_for(second.run('go'), timeout=_TIMEOUT)

    assert log == []


async def test_plugins_run_in_the_order_they_were_given():
    log: list[str] = []
    runtime = Runtime(
        plugins=[_Recorder('beta', log), _Recorder('alpha', log), _Recorder('gamma', log)]
    )
    assert runtime.plugins == ('beta', 'alpha', 'gamma')
    await runtime.shutdown()


def test_installing_two_plugins_with_the_same_name_is_refused():
    """同一个能力装两遍，它的 hook 就跑两遍——计数翻倍、改写叠加，而症状不指向任何地方。"""
    log: list[str] = []
    with pytest.raises(ConfigError):
        install_plugins(HooksCenter(), [_Recorder('same', log), _Recorder('same', log)])


def test_a_plugin_without_a_register_method_is_refused_with_an_actionable_message():
    class _Broken:
        name = 'broken'

    with pytest.raises(ConfigError) as info:
        install_plugins(HooksCenter(), [_Broken()])  # type: ignore[list-item]
    assert '下一步' in str(info.value)


async def test_the_installed_plugin_names_are_a_snapshot():
    log: list[str] = []
    runtime = Runtime(plugins=[_Recorder('beta', log)])
    names = runtime.plugins
    assert names == ('beta',)
    assert runtime.plugins == ('beta',)
    await runtime.shutdown()


async def test_an_agent_can_hook_an_extension_junction_declared_on_the_runtime():
    """分类由声明者给出，声明在外层——局部注册必须能沿装配链找到它，否则 register 就会报错。"""
    shared = HooksCenter()
    shared.declare_extension_junction('artifact:created', JunctionKind.OBSERVABLE)
    agent = _agent(shared)
    seen: list[str] = []

    async def local(**kwargs: object) -> None:
        seen.append('local')

    agent.register_hook('artifact:created', local)
    await shared.trigger('artifact:created')

    # 外层不知道内层的存在：一个插件的行为不该因为某个 Agent 挂了局部钩子而改变。
    assert seen == []


async def test_registering_on_an_undeclared_extension_junction_still_fails_loudly():
    """分层不得把「没声明过」这条守卫稀释掉：往没人声明过的切点挂 hook 只会留一个永不触发的钩子。"""
    agent = _agent(HooksCenter())

    async def local(**kwargs: object) -> None:
        return None

    with pytest.raises(ContractViolation):
        agent.register_hook('artifact:created', local)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_plugins.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'syndica.kernel.plugins'`

- [ ] **Step 3: 让 `HooksCenter` 可分层**

`__init__` 收下外层，并把三处查表改成沿链查：

```python
    def __init__(self, *, outer: HooksCenter | None = None) -> None:
        self._hooks: dict[JunctionRef, list[HookCallback]] = {junction: [] for junction in Junction}
        self._extension_kinds: dict[str, JunctionKind] = {}
        self._outer = outer

    def register(self, junction: JunctionRef, cb: HookCallback) -> None:
        """挂一个 hook。同一切点上按注册先后执行。"""
        self._hooks.setdefault(self._resolved(junction), []).append(cb)

    def _collect(self, junction: JunctionRef) -> list[HookCallback]:
        """本层的 hook 在前，外层的在后——**Agent 局部先于 Runtime 级**（spec §5.3）。

        两层合成**一条**链，不是跑两遍：可拦截链的 `modified` 要沿链累积，`skip` 要能终止其后
        全部 hook（含外层的）。跑两遍就得在外面再实现一遍合并，而那份实现迟早与单层的分叉。
        """
        own = list(self._hooks.get(junction, []))
        if self._outer is None:
            return own
        return own + self._outer._collect(junction)

    def _declared_kind(self, name: str) -> JunctionKind | None:
        own = self._extension_kinds.get(name)
        if own is not None:
            return own
        return self._outer._declared_kind(name) if self._outer is not None else None

    def _declared_names(self) -> list[str]:
        names = set(self._extension_kinds)
        if self._outer is not None:
            names.update(self._outer._declared_names())
        return sorted(names)
```

`declare_extension_junction` 的冲突检查改用 `self._declared_kind(validated)`（沿链查，
外层已声明成另一种分类时同样报错）；`_resolved` 里的 `if validated not in self._extension_kinds`
改成 `if self._declared_kind(validated) is None`，错误消息里的已声明清单改用 `_declared_names()`；
`_kind` 的扩展分支改成：

```python
        declared = self._declared_kind(junction)
        if declared is None:
            raise ContractViolation(
                expected=f'扩展切点 {junction} 在本层或外层已经声明过',
                received='沿装配链找不到它的分类',
                next_action='先由声明它的能力调 declare_extension_junction(name, kind)',
            )
        return declared
```

`trigger` 里 `hooks = list(self._hooks[resolved])` 改成 `hooks = self._collect(resolved)`。

模块 docstring 补一段：

```
**分层装配**：`HooksCenter(outer=...)` 把两层串成一条链，本层在前。spec §5.3 要求 Agent 局部钩子
先于 Runtime 级插件执行，而「先于」在可拦截链上是有实质后果的——局部钩子的 `modified` 会成为
Runtime 级钩子看到的入参，局部钩子的 `skip` 会连 Runtime 级的一起终止。**外层不知道内层的存在**：
从 Runtime 的中心触发只跑 Runtime 级的 hook，这样一个插件不会因为某个 Agent 挂了局部钩子而行为
改变。
```

- [ ] **Step 4: 写 `plugins.py`**

```python
# src/syndica/kernel/plugins.py
"""Runtime 级插件——扩展装配的第二层（spec §5.3）。

**跨 Agent 通用、且永不暴露为开关**的逻辑放这里。带开关的能力一律建模为 Runtime Module（§5.5，
计划 5）——这条判据决定了本模块刻意很薄：一个插件只有名字和一次注册机会，没有配置、没有启停
钩子、没有服务注入。那些都是 Module 的事。

**插件列表的次序是配置的一部分。** 可拦截切点按注册先后串行，因此换个次序就换了行为。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, cast

from syndica.kernel.errors import ConfigError
from syndica.kernel.hooks import HooksCenter

__all__ = ['Plugin', 'install_plugins']


class Plugin(Protocol):
    """一个 Runtime 级插件。

    `name` 用来查重与排障：症状出现在 hook 的效果上，而排查的第一个问题永远是「装了哪些、按什么
    顺序」。
    """

    @property
    def name(self) -> str: ...

    def register(self, hooks: HooksCenter) -> None: ...


def _validated(plugin: object, index: int) -> Plugin:
    """逐项检查，而不是 `isinstance(plugin, Plugin)`：错在哪一项、缺的是哪一样都要说出来。"""
    name = getattr(plugin, 'name', None)
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(
            expected='每个插件有一个非空的 name（用于查重与排障）',
            received=f'plugins[{index}] 是 {type(plugin).__name__}，它的 name 是 {name!r}',
            next_action='给这个插件加一个返回非空字符串的 name 属性',
        )
    if not callable(getattr(plugin, 'register', None)):
        raise ConfigError(
            expected='每个插件有一个 register(hooks) 方法，在里面挂自己的 hook',
            received=f'plugins[{index}]（name={name!r}）没有可调用的 register',
            next_action=(
                '给它加 def register(self, hooks: HooksCenter) -> None，'
                '在里面调 hooks.register(junction, cb)'
            ),
        )
    # 上面两个 getattr 就是这次转换的全部依据：Plugin 是 Protocol，运行时没有更强的检查可做。
    return cast(Plugin, plugin)


def install_plugins(hooks: HooksCenter, plugins: Sequence[Plugin]) -> tuple[str, ...]:
    """按给定顺序装配插件，返回它们的名字（快照）。

    重名报错：同一个能力装两遍，它的 hook 就会跑两遍——计数翻倍、改写叠加，而这类症状没有任何
    东西会指向「装了两次」。
    """
    installed: list[str] = []
    for index, plugin in enumerate(plugins):
        checked = _validated(plugin, index)
        if checked.name in installed:
            raise ConfigError(
                expected='每个插件只装一次：插件名在一个 Runtime 内唯一',
                received=f'plugins[{index}] 的 name={checked.name!r} 与前面某一项重复',
                next_action=(
                    '去掉重复的那一项。同一个能力装两遍，它的 hook 会跑两遍——'
                    '计数翻倍、改写叠加，而症状不会指向「装了两次」'
                ),
            )
        checked.register(hooks)
        installed.append(checked.name)
    return tuple(installed)
```

- [ ] **Step 5: 接进 `BaseAgent` 与 `Runtime`**

`BaseAgent.__init__`：

```python
        # 本 Agent 自己的一层，外层是宿主交来的那个中心。两层合成一条链，本层在前（spec §5.3）。
        self._hooks = HooksCenter(outer=hooks)
        self._executor = ToolExecutor(
            tools=options.tools,
            hooks=self._hooks,
            judge=options.danger_judge,
            policy=options.danger_policy,
            max_concurrency=options.max_tool_concurrency,
        )
```

```python
    def register_hook(self, junction: JunctionRef, cb: HookCallback) -> None:
        """给**这一个** Agent 挂一个 hook（spec §5.3 第一层）。

        它先于 Runtime 级插件执行：局部增强要能先看到、先改，跨 Agent 的通用逻辑在它之后收口。
        两层是一条链，因此局部 hook 的 `skip` 会连 Runtime 级的一起终止。
        """
        self._hooks.register(junction, cb)
```

`Runtime.__init__`：

```python
        plugins: Sequence[Plugin] | None = None,
    ) -> None:
        ...
        self.hooks: HooksCenter = HooksCenter()
        self._plugin_names: tuple[str, ...] = install_plugins(self.hooks, plugins or ())

    @property
    def plugins(self) -> tuple[str, ...]:
        """已装插件的名字，按装配顺序。元组因此是快照——改它改不到 Runtime。"""
        return self._plugin_names
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests -v && uv run ruff check . && uv run pyright`
Expected: 全部 passed

- [ ] **Step 7: 提交**

```bash
git add src/syndica/kernel/plugins.py src/syndica/kernel/hooks/center.py src/syndica/kernel/agent.py src/syndica/kernel/runtime.py tests/kernel/test_plugins.py tests/kernel/test_hooks.py
git commit -m "feat(kernel): 两层装配合成一条 hook 链，局部在前

spec 要求 Agent 局部钩子先于 Runtime 级插件执行，而「先于」在可拦截链上是有实质
后果的：局部钩子的 modified 会成为 Runtime 级钩子看到的入参，局部钩子的 skip 会连
Runtime 级的一起终止。跑两遍链就得在外面再实现一遍合并，而那份实现迟早与单层的分叉。

外层不知道内层的存在：从 Runtime 的中心触发只跑 Runtime 级的 hook。否则一个插件的
行为会因为某个 Agent 挂了局部钩子而改变。

plugins.py 刻意很薄——一个插件只有名字和一次注册机会，没有配置、没有启停钩子、没有
服务注入。带开关的能力一律是 Runtime Module，那些都是它的事。

重名报错：同一个能力装两遍，它的 hook 会跑两遍，计数翻倍、改写叠加，而这类症状没有
任何东西会指向「装了两次」。"
```

---

## Task 14: `Delegate` 与 `Fork`——派活与分叉

**Files:**
- Create: `src/syndica/kernel/tools/__init__.py`
- Create: `src/syndica/kernel/tools/orchestration.py`
- Modify: `src/syndica/kernel/runtime.py`
- Test: `tests/kernel/test_orchestration.py`

**Interfaces:**
- Consumes: Task 3 的 `spawn` / `wait`、Task 7 的 `_destroy_tree`
- Produces:
  - `Runtime(agents={name: factory})`、`Runtime.agent_factory(name)`、`Runtime.factory_of(agent_id)`、只读 `Runtime.agent_names`
  - `Runtime.spawn(..., fork_from=agent_id)`：继承源 Agent 的轨迹
  - `Delegate(runtime, agent_id)` / `Fork(runtime, agent_id)`

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_orchestration.py
from __future__ import annotations

import asyncio

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.agent_types import AgentStatus, ExitMode
from syndica.kernel.context import ContextBlock, ToolCall, TrustLevel
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.message import Message
from syndica.kernel.runtime import Runtime
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tools import (
    Delegate,
    Fork,
    KillAgent,
    QueryAgent,
    SendMessage,
    orchestration_tools,
)

_TIMEOUT = 3.0


class _Tainted(Tool):
    """产出不可信内容的工具：外部抓来的东西就是这个样子。"""

    __tool_command_args__ = ()

    async def execute(self, text: str) -> ToolResult:
        """回显 text，并标记为不可信。"""
        return ToolResult(output=text, trust=TrustLevel.UNTRUSTED)


def _worker(script: list[ScriptedTurn], tools: dict[str, Tool] | None = None):
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM(script),
                tools=tools if tools is not None else {},
                system_prompt='SP',
            ),
            hooks=hooks,
            events=events,
        )

    return build


async def test_delegate_spawns_a_child_waits_for_it_and_brings_back_its_final_answer():
    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='the child answer')])})
    parent = await runtime.spawn(_worker([ScriptedTurn(content='parent idle')]), query='go')
    await runtime.wait(parent)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='do the thing'),
        timeout=_TIMEOUT,
    )

    assert result.output == 'the child answer'
    assert result.meta['stop_reason'] == 'completed'
    child_id = result.meta['agent_id']
    assert (await runtime.find(child_id)).parent_id == parent
    await runtime.shutdown()


async def test_a_delegated_child_stays_addressable_so_the_parent_can_keep_talking_to_it():
    """spec §5.6：已有 Agent 能继续同一工作时用 SendMessage，不要新建。"""
    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='first')])})
    parent = await runtime.spawn(_worker([ScriptedTurn(content='parent')]), query='go')
    await runtime.wait(parent)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='first job'), timeout=_TIMEOUT
    )

    info = await runtime.find(result.meta['agent_id'])
    assert info is not None and info.status is AgentStatus.ZOMBIE
    await runtime.shutdown()


async def test_delegating_to_an_unknown_role_tells_the_model_which_roles_exist():
    """未知角色是模型改得动的错误，因此进 output 而不是上抛。"""
    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='x')])})
    parent = await runtime.spawn(_worker([ScriptedTurn(content='p')]), query='go')
    await runtime.wait(parent)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Nope', query='x'), timeout=_TIMEOUT
    )

    assert isinstance(result.output, str)
    assert 'Worker' in result.output
    assert '下一步' in result.output
    await runtime.shutdown()


async def test_delegate_reports_a_child_that_ran_out_of_budget_instead_of_pretending_it_answered():
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM([ScriptedTurn(content='x')]),
                tools={},
                system_prompt='SP',
                max_turns=0,
            ),
            hooks=hooks,
            events=events,
        )

    runtime = Runtime(agents={'Worker': build})
    parent = await runtime.spawn(_worker([ScriptedTurn(content='p')]), query='go')
    await runtime.wait(parent)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='x'), timeout=_TIMEOUT
    )

    assert result.meta['stop_reason'] == 'max_turns'
    assert isinstance(result.output, str)
    assert '下一步' in result.output
    await runtime.shutdown()


async def test_a_child_that_touched_untrusted_content_hands_an_untrusted_result_to_the_parent():
    """标记必须随衍生内容传递：子 Agent 的结论是它读过的东西的衍生物（spec §12.2）。"""
    child_script = [
        ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Tainted', arguments='{"text":"web"}')]),
        ScriptedTurn(content='summarised from the web'),
    ]
    runtime = Runtime(agents={'Worker': _worker(child_script, {'Tainted': _Tainted()})})
    parent = await runtime.spawn(_worker([ScriptedTurn(content='p')]), query='go')
    await runtime.wait(parent)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='research'), timeout=_TIMEOUT
    )

    assert result.trust is TrustLevel.UNTRUSTED
    await runtime.shutdown()


async def test_fork_inherits_the_parents_trajectory_and_the_same_role():
    runtime = Runtime()
    parent = await runtime.spawn(
        _worker([ScriptedTurn(content='parent conclusion'), ScriptedTurn(content='forked tail')]),
        query='original question',
    )
    await runtime.wait(parent)

    result = await asyncio.wait_for(
        Fork(runtime, parent).execute(query='keep going from here'), timeout=_TIMEOUT
    )

    child = runtime._table.get(result.meta['agent_id'])
    assert child is not None
    assert [block.content for block in child.context.current_traj if block.role == 'user'] == [
        'original question',
        'keep going from here',
    ]
    assert 'parent conclusion' in [block.content for block in child.context.current_traj]
    await runtime.shutdown()


async def test_forking_from_inside_a_tool_call_does_not_copy_the_unpaired_batch():
    """分叉是从工具调用内部发起的，源轨迹此刻末尾是一个尚未配对的 assistant block。"""
    runtime = Runtime()
    parent = await runtime.spawn(
        _worker(
            [
                ScriptedTurn(
                    tool_calls=[ToolCall(id='c1', name='Tainted', arguments='{"text":"x"}')]
                ),
                ScriptedTurn(content='tail'),
            ],
            {'Tainted': _Tainted()},
        ),
        query='go',
    )
    await runtime.wait(parent)
    source = runtime._table.get(parent)
    assert source is not None
    # 手工造出「末尾有一个未配对的批次」这个形状，就是 Fork 真实发起时源轨迹的样子。
    source.context.append(
        ContextBlock(
            role='assistant',
            tool_calls=[ToolCall(id='c9', name='Fork', arguments='{}')],
        )
    )

    child_id = await runtime.spawn(
        _worker([ScriptedTurn(content='child')]), query='continue', fork_from=parent
    )
    child = runtime._table.get(child_id)
    assert child is not None

    assert all(
        not (block.role == 'assistant' and block.tool_calls and block.tool_calls[0].id == 'c9')
        for block in child.context.current_traj
    )
    await runtime.shutdown()


async def test_forking_from_an_agent_this_process_does_not_hold_is_refused():
    runtime = Runtime()
    with pytest.raises(ContractViolation):
        await runtime.spawn(_worker([ScriptedTurn(content='x')]), query='go', fork_from='nope')
    await runtime.shutdown()


async def test_registered_agent_names_are_a_snapshot_in_registration_order():
    """基数 3 且既非升序亦非降序：角色表的顺序会被 Delegate 的错误消息与文档消费。"""
    runtime = Runtime(
        agents={
            'Beta': _worker([ScriptedTurn(content='b')]),
            'Alpha': _worker([ScriptedTurn(content='a')]),
            'Gamma': _worker([ScriptedTurn(content='g')]),
        }
    )
    assert runtime.agent_names == ('Beta', 'Alpha', 'Gamma')
    await runtime.shutdown()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_orchestration.py -v`
Expected: FAIL —— `syndica.kernel.tools` 不存在；`Runtime` 不接受 `agents` / `fork_from`

- [ ] **Step 3: 给 `Runtime` 加角色表与分叉**

import 补 `import copy`、`from collections.abc import Mapping, Sequence`、
`from syndica.kernel.context import ContextBlock`。

模块级新增：

```python
def _forkable_prefix(blocks: list[ContextBlock]) -> list[ContextBlock]:
    """分叉出去的那份轨迹必须配对完整。

    分叉是从一次工具调用**内部**发起的，此刻源 Agent 的轨迹末尾正是一个已声明、尚未配对的
    assistant block——发起分叉的那次调用自己。原样拷过去，子 Agent 一进 `run` 就会把它当成「上一次
    被中断的批次」，补上一堆「副作用未知」的说明：一段凭空捏造的历史。因此在最后一个未配对的
    assistant block 处截断。那次调用是源 Agent 的事，子 Agent 不需要知道。
    """
    index = len(blocks) - 1
    while index >= 0 and blocks[index].role == 'tool':
        index -= 1
    if index < 0 or blocks[index].role != 'assistant' or not blocks[index].tool_calls:
        return list(blocks)
    answered = {block.tool_call_id for block in blocks[index + 1 :]}
    if all(call.id in answered for call in blocks[index].tool_calls):
        return list(blocks)
    return blocks[:index]
```

`__init__` 收下角色表：

```python
        agents: Mapping[str, AgentFactory] | None = None,
    ) -> None:
        ...
        self._agents: dict[str, AgentFactory] = dict(agents or {})
```

```python
    @property
    def agent_names(self) -> tuple[str, ...]:
        """已登记的角色名，按登记顺序。元组因此是快照。"""
        return tuple(self._agents)

    def agent_factory(self, name: str) -> AgentFactory | None:
        """按角色名取工厂；没有就 None。

        这是「派活给谁」的最小落点。**声明式定义、加载源与同名覆盖归计划 3**——那里会在这张表
        之上加载入与覆盖记录，本方法的返回语义不变。
        """
        return self._agents.get(name)

    def factory_of(self, agent_id: str) -> AgentFactory | None:
        """造出这个 Agent 的那个工厂；本进程不持有它则 None。

        `Fork` 要照着源 Agent 再造一个**同类型**的：spec §5.2 纪律 4 说同类型分叉继承缓存亲和，
        而「同类型」在这里的实际含义就是同一个工厂——同一份 System Prompt、同一套工具与次序。
        """
        slot = self._slots.get(agent_id)
        return slot.factory if slot is not None else None
```

`spawn` 加 `fork_from`（放在 `attach_gate` 之后、`register` 之前）：

```python
        fork_from: str | None = None,
        ...
        agent.attach_gate(gate)
        if fork_from is not None:
            source = self._table.get(fork_from)
            if source is None:
                raise ContractViolation(
                    expected=f'分叉源 {fork_from!r} 的实例在本进程手里，它的轨迹才拷得到',
                    received='本进程没有它——它不是本进程 spawn 的，或已被销毁',
                    next_action=(
                        '在持有它的进程上分叉；'
                        '要冷启动一个不继承上下文的 Agent 就不要传 fork_from'
                    ),
                )
            for block in _forkable_prefix(source.context.current_traj):
                agent.context.append(copy.deepcopy(block))
```

- [ ] **Step 4: 写 `tools/orchestration.py` 的前两个工具**

```python
# src/syndica/kernel/tools/__init__.py
from syndica.kernel.tools.orchestration import Delegate, Fork

__all__ = ['Delegate', 'Fork']
```

```python
# src/syndica/kernel/tools/orchestration.py
"""编排工具：让 Agent 自己派活、分叉、通信（spec §5.6）。

它们是 Runtime 控制面方法的薄封装，但**读者不同**，因此错误处理相反：控制面方法对**程序员**报
`ContractViolation`（三件套写给能改代码的人），而这些工具的调用方是**模型**——同一条消息交给它
毫无用处。所以工具把「角色名不存在」「目标已经不在了」这类模型改得动的情况写进 `output`，只让
真正的程序错误上抛。

**边界（写进每个工具的 description，因为那是编排者派活的唯一依据）**：`Delegate` 冷启动、不继承
上下文，用于需要独立视角的工作；`Fork` 继承上下文，用于已有结论有价值的延续；已有 Agent 能继续
同一工作时用 `SendMessage`，不要新建；**能直接做完的有界工作不该委派**。

**`Delegate` 与 `Fork` 会阻塞发起方的这一轮。** 它们要等子 Agent 做完才拿得到结果，而工具是在
轮里跑的。这是有意的：派活出去却不等结果，父 Agent 就得自己再发明一套轮询。要不等就用
`SendMessage`。发起方此刻仍是 RUNNING 而不是 WAITING——`WAITING` 说的是「等一条匹配的答复」，
等一个子 Agent 干活是工作本身。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from syndica.kernel.agent_types import ExitMode
from syndica.kernel.context import Context, StopReason, TrustLevel
from syndica.kernel.tool import Tool, ToolResult

if TYPE_CHECKING:
    from syndica.kernel.runtime import Runtime

__all__ = ['Delegate', 'Fork']


def _final_text(ctx: Context) -> str | None:
    for block in reversed(ctx.current_traj):
        if block.role == 'assistant' and isinstance(block.content, str) and block.content:
            return block.content
    return None


def _inherited_trust(ctx: Context) -> TrustLevel:
    """子 Agent 轨迹里最严的那个信任级别。

    子 Agent 可能读过外部内容，而它交回来的结论是那些内容的**衍生物**——标记必须随衍生内容传递
    （spec §12.2 第 2 条）。这里丢掉标记，父 Agent 就会拿着一段看起来像结论的不可信文字，去做
    本该被升级处置的高风险操作。
    """
    for block in ctx.current_traj:
        if block.trust is TrustLevel.UNTRUSTED:
            return TrustLevel.UNTRUSTED
    return TrustLevel.TRUSTED


_UNFINISHED: dict[StopReason | None, str] = {
    None: '它在给出结论之前被硬终止了（kill / 关机），轨迹可能停在一轮的中间',
    StopReason.ERROR: '它以错误结束',
    StopReason.MAX_TURNS: '它用完了轮次预算',
    StopReason.ABORTED: '它被中止了',
    StopReason.COMPLETED: '它正常结束，但最后一轮没有留下任何文字',
}


def _delivered(child_id: str, ctx: Context) -> ToolResult:
    """把子 Agent 的终局翻译成模型看得懂的一段话。

    **不抛异常**：子 Agent 没干成是模型可以应对的情况（换个角色、换个说法、自己做），而不是
    程序错误。子的 `agent_id` 一律带上——它仍然可寻址，父可以接着 QueryAgent 或 SendMessage。
    """
    reason = ctx.stop_reason
    meta = {'agent_id': child_id, 'stop_reason': reason.value if reason is not None else None}
    trust = _inherited_trust(ctx)
    text = _final_text(ctx)
    if reason is StopReason.COMPLETED and text is not None:
        return ToolResult(output=text, meta=meta, trust=trust)
    return ToolResult(
        output=(
            f'期望: agent {child_id} 完成这件工作并给出结论。'
            f' 实际: {_UNFINISHED[reason]}。'
            f' 下一步: 用 QueryAgent({child_id}) 看它的状态，'
            f'用 SendMessage({child_id}, ...) 让它带着已有上下文继续；'
            '要换条路就自己做，或者派给另一个角色。'
        ),
        meta=meta,
        trust=trust,
    )


class Delegate(Tool):
    """把一件独立的工作派给一个 Agent 角色，等它做完并取回结果。

    冷启动、**不继承你的上下文**：用于需要独立视角的工作。已有 Agent 能继续同一工作时用
    SendMessage，不要新建；能直接做完的有界工作不该委派。
    """

    __tool_command_args__ = ()

    def __init__(self, runtime: Runtime, agent_id: str) -> None:
        self._runtime = runtime
        self._agent_id = agent_id

    async def execute(self, agent: str, query: str) -> ToolResult:
        """派活并等结果。

        agent: 角色名
        query: 交给它的完整任务描述，它看不到你的上下文，所以要自带全部前提
        """
        factory = self._runtime.agent_factory(agent)
        if factory is None:
            return ToolResult(
                output=(
                    f'期望: agent 是已登记的角色之一。'
                    f' 实际: 收到 {agent!r}；已登记的角色: {list(self._runtime.agent_names)}。'
                    ' 下一步: 改用上面列出的角色之一，不要发明角色名；'
                    '没有合适的角色就自己做这件事。'
                )
            )
        child_id = await self._runtime.spawn(
            factory, query=query, parent_id=self._agent_id, exit_mode=ExitMode.ZOMBIE
        )
        return _delivered(child_id, await self._runtime.wait(child_id))


class Fork(Tool):
    """分叉出一个继承了你当前上下文的 Agent，交给它一件延续性的工作，等它做完。

    用于已有结论有价值、需要在此基础上并行推进的场景。要独立视角就用 Delegate。
    """

    __tool_command_args__ = ()

    def __init__(self, runtime: Runtime, agent_id: str) -> None:
        self._runtime = runtime
        self._agent_id = agent_id

    async def execute(self, query: str) -> ToolResult:
        """分叉并等结果。

        query: 交给分身的任务描述；它已经看得到你到此刻为止的轨迹，不必重复前提
        """
        factory = self._runtime.factory_of(self._agent_id)
        if factory is None:
            return ToolResult(
                output=(
                    f'期望: 本进程知道 agent {self._agent_id} 是怎么造出来的，才能分叉出同类型的。'
                    ' 实际: 本进程没有它的构造记录。'
                    ' 下一步: 用 Delegate 派给一个已登记的角色（它冷启动、不继承上下文），'
                    '或者自己把这件事做完。'
                )
            )
        child_id = await self._runtime.spawn(
            factory,
            query=query,
            parent_id=self._agent_id,
            exit_mode=ExitMode.ZOMBIE,
            fork_from=self._agent_id,
        )
        return _delivered(child_id, await self._runtime.wait(child_id))
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests -v && uv run ruff check . && uv run pyright && uv run python scripts/check_deps.py`
Expected: 全部 passed

- [ ] **Step 6: 提交**

```bash
git add src/syndica/kernel/tools src/syndica/kernel/runtime.py tests/kernel/test_orchestration.py
git commit -m "feat(kernel): Agent 自己能派活了，派出去的结果带着信任标记回来

里程碑 1 的判据要求「Runtime 能创建多个 Agent、互发消息」。编排工具不进来，多 Agent
就只是宿主代码的能力而不是 Agent 的能力——Agent 自己派不了活，里程碑 2 自举时也无法
委派。

三处刻意的设计：
- Delegate / Fork 阻塞发起方这一轮。派活出去却不等结果，父 Agent 就得自己再发明一套
  轮询。要不等就用 SendMessage。发起方此刻仍是 RUNNING 不是 WAITING——WAITING 说的是
  「等一条匹配的答复」，等一个子 Agent 干活是工作本身
- 子 Agent 的结果继承它轨迹里最严的 trust。它交回来的结论是它读过的东西的衍生物，
  丢掉标记，父就会拿着一段看起来像结论的不可信文字去做高风险操作
- Fork 在最后一个未配对的 assistant block 处截断。分叉是从工具调用内部发起的，源轨迹
  此刻末尾正是那次调用自己；原样拷过去，子一进 run 就会把它当成「上次被中断的批次」
  补上一堆副作用未知的说明——一段凭空捏造的历史

工具的读者是模型，控制面方法的读者是程序员，所以错误处理相反：角色名不存在这类模型
改得动的情况进 output，只有真正的程序错误上抛。"
```

---

## Task 15: `SendMessage` / `QueryAgent` / `KillAgent` 与自动装配

**Files:**
- Modify: `src/syndica/kernel/tools/orchestration.py`
- Modify: `src/syndica/kernel/tools/__init__.py`
- Modify: `src/syndica/kernel/runtime.py`
- Test: `tests/kernel/test_orchestration.py`

**Interfaces:**
- Consumes: Task 14 的 `Delegate` / `Fork`，Task 4 的 `send`，Task 7 的 `kill`
- Produces:
  - `SendMessage` / `QueryAgent` / `KillAgent`
  - `orchestration_tools(runtime, agent_id) -> dict[str, Tool]`：**次序固定**（它直接进请求前缀）
  - `Runtime(orchestration=True)`：每个新 Agent 自动获得这五个工具

- [ ] **Step 1: 写失败测试**

```python
# tests/kernel/test_orchestration.py  （追加；Task 14 写下的 import 块已含本任务要用的全部名字）
async def _wait_for_registry_status(
    runtime: Runtime, agent_id: str, status: AgentStatus
) -> None:
    """轮询等到注册表里的状态到达；超时抛 TimeoutError，让回归表现为失败而不是挂死。

    与 test_runtime.py 里的同名助手是同一段代码，两个模块各写一份——为它单开一个跨目录可 import
    的 helper 得先解决 tests 目录的包结构，代价大于这十行重复。
    """

    async def _poll() -> None:
        while True:
            info = await runtime.find(agent_id)
            if info is not None and info.status is status:
                return
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout=2.0)


def test_the_five_orchestration_tools_come_in_a_fixed_order():
    """工具表的迭代顺序逐字进请求前缀：重排不报错，只让整条会话的缓存静默失效。"""
    runtime = Runtime()
    tools = orchestration_tools(runtime, 'a1')
    assert list(tools) == ['Delegate', 'Fork', 'SendMessage', 'QueryAgent', 'KillAgent']
    assert [tool.name for tool in tools.values()] == list(tools)


async def test_send_message_reaches_a_zombie_and_wakes_it_up():
    runtime = Runtime()
    target = await runtime.spawn(
        _worker([ScriptedTurn(content='first'), ScriptedTurn(content='second')]), query='go'
    )
    await runtime.wait(target)
    sender = await runtime.spawn(_worker([ScriptedTurn(content='s')]), query='go')
    await runtime.wait(sender)

    result = await asyncio.wait_for(
        SendMessage(runtime, sender).execute(agent_id=target, content='carry on'),
        timeout=_TIMEOUT,
    )

    assert result.meta['ok'] is True
    ctx = await asyncio.wait_for(runtime.wait(target), timeout=_TIMEOUT)
    assert [block.content for block in ctx.current_traj if block.role == 'user'] == [
        'go',
        'carry on',
    ]
    await runtime.shutdown()


async def test_send_message_reports_an_unknown_target_without_raising():
    runtime = Runtime()
    sender = await runtime.spawn(_worker([ScriptedTurn(content='s')]), query='go')
    await runtime.wait(sender)

    result = await asyncio.wait_for(
        SendMessage(runtime, sender).execute(agent_id='nope', content='x'), timeout=_TIMEOUT
    )

    assert result.meta['ok'] is False
    assert isinstance(result.output, str) and '下一步' in result.output
    await runtime.shutdown()


async def test_send_message_rejects_an_unknown_trigger_and_lists_the_valid_ones():
    runtime = Runtime()
    sender = await runtime.spawn(_worker([ScriptedTurn(content='s')]), query='go')
    await runtime.wait(sender)

    result = await asyncio.wait_for(
        SendMessage(runtime, sender).execute(agent_id=sender, content='x', trigger='later'),
        timeout=_TIMEOUT,
    )

    assert isinstance(result.output, str)
    assert 'pending' in result.output and 'deferred' in result.output
    await runtime.shutdown()


async def test_query_agent_never_wakes_the_target_up():
    """find 明文承诺绝不激活；这个工具是它的门面，承诺必须一路守到这里。"""
    runtime = Runtime()
    target = await runtime.spawn(_worker([ScriptedTurn(content='done')]), query='go')
    await runtime.wait(target)
    asker = await runtime.spawn(_worker([ScriptedTurn(content='a')]), query='go')
    await runtime.wait(asker)

    result = await asyncio.wait_for(
        QueryAgent(runtime, asker).execute(agent_id=target), timeout=_TIMEOUT
    )

    assert result.meta['status'] == 'zombie'
    assert (await runtime.find(target)).status is AgentStatus.ZOMBIE
    delivered = await runtime.broker.publish(
        target, Message(sender='t', receiver=target, content='x')
    )
    assert delivered.ok is False
    await runtime.shutdown()


async def test_killing_yourself_is_refused_and_points_at_the_thing_that_actually_works():
    runtime = Runtime()
    agent_id = await runtime.spawn(_worker([ScriptedTurn(content='x')]), query='go')
    await runtime.wait(agent_id)

    result = await asyncio.wait_for(
        KillAgent(runtime, agent_id).execute(agent_id=agent_id), timeout=_TIMEOUT
    )

    assert isinstance(result.output, str)
    assert '下一步' in result.output
    assert (await runtime.find(agent_id)) is not None
    await runtime.shutdown()


async def test_kill_agent_stops_a_resident_target_but_keeps_it_addressable():
    runtime = Runtime()
    target = await runtime.spawn(
        _worker([ScriptedTurn(content='x')]), exit_mode=ExitMode.IDLE
    )
    await _wait_for_registry_status(runtime, target, AgentStatus.IDLE)
    killer = await runtime.spawn(_worker([ScriptedTurn(content='k')]), query='go')
    await runtime.wait(killer)

    result = await asyncio.wait_for(
        KillAgent(runtime, killer).execute(agent_id=target), timeout=_TIMEOUT
    )

    assert result.meta['agent_id'] == target
    await _wait_for_registry_status(runtime, target, AgentStatus.ZOMBIE)
    await runtime.shutdown()


async def test_orchestration_tools_are_installed_on_every_spawned_agent_when_enabled():
    runtime = Runtime(orchestration=True)
    agent_id = await runtime.spawn(_worker([ScriptedTurn(content='x')]), query='go')
    await runtime.wait(agent_id)
    agent = runtime._table.get(agent_id)
    assert agent is not None
    assert list(agent.options.tools)[-5:] == [
        'Delegate',
        'Fork',
        'SendMessage',
        'QueryAgent',
        'KillAgent',
    ]
    await runtime.shutdown()


async def test_installing_orchestration_over_a_tool_of_the_same_name_is_refused():
    """静默覆盖会让 Agent 自己那个同名工具永远调不到，而 run 照常报 COMPLETED。"""
    runtime = Runtime(orchestration=True)

    class _Delegate(Tool):
        __tool_command_args__ = ()

        async def execute(self, x: str) -> ToolResult:
            """假的 Delegate。"""
            return ToolResult(output=x)

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM([ScriptedTurn(content='x')]),
                tools={'Delegate': _Delegate()},
                system_prompt='SP',
            ),
            hooks=hooks,
            events=events,
        )

    with pytest.raises(ConfigError):
        await runtime.spawn(build, query='go')
    await runtime.shutdown()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/kernel/test_orchestration.py -v`
Expected: FAIL —— `orchestration_tools` 不存在；`Runtime` 不接受 `orchestration`

- [ ] **Step 3: 写剩下三个工具与工厂**

`orchestration.py` import 补 `from syndica.kernel.message import Trigger`，`__all__` 补齐五个工具与 `orchestration_tools`。

```python
class SendMessage(Tool):
    """给一个**已有的** Agent 发一条消息，不等它回话。

    已有 Agent 能继续同一工作时用这个，不要新建：新建会丢掉它已经攒下的全部上下文。
    """

    __tool_command_args__ = ()

    def __init__(self, runtime: Runtime, agent_id: str) -> None:
        self._runtime = runtime
        self._agent_id = agent_id

    async def execute(self, agent_id: str, content: str, trigger: str = 'pending') -> ToolResult:
        """投递一条消息。目标没在跑就把它叫醒。

        agent_id: 收件人的 id
        content: 消息正文
        trigger: pending 表示它下一轮开始前就能看到，deferred 表示等它手上的活做完再看
        """
        try:
            when = Trigger(trigger)
        except ValueError:
            return ToolResult(
                output=(
                    f'期望: trigger 是 pending 或 deferred。'
                    f' 实际: 收到 {trigger!r}。'
                    ' 下一步: 要打断它当前的工作用 pending，'
                    '要等它把手上的事做完再看用 deferred。'
                )
            )
        delivered = await self._runtime.send(
            agent_id, content, trigger=when, sender=self._agent_id
        )
        if not delivered.ok:
            return ToolResult(
                output=delivered.reason or f'投递给 {agent_id} 失败，且没有给出原因',
                meta={'ok': False, 'agent_id': agent_id},
            )
        return ToolResult(
            output=f'已投递给 agent {agent_id}（trigger={when.value}）。它会在自己的下一轮读到它。',
            meta={'ok': True, 'agent_id': agent_id, 'message_id': delivered.message_id},
        )


class QueryAgent(Tool):
    """查一个 Agent 的状态。**只读**，绝不会把它叫醒。"""

    __tool_command_args__ = ()

    def __init__(self, runtime: Runtime, agent_id: str) -> None:
        self._runtime = runtime
        self._agent_id = agent_id

    async def execute(self, agent_id: str) -> ToolResult:
        """读一个 Agent 的名字、状态、父与子代数量。

        agent_id: 要查的 Agent 的 id
        """
        info = await self._runtime.find(agent_id)
        if info is None:
            return ToolResult(
                output=(
                    f'期望: agent {agent_id!r} 在注册表里。'
                    ' 实际: 没有这个条目——它从未存在，或已被销毁。'
                    ' 下一步: 核对 id；确实需要这件工作就用 Delegate 派一个新的。'
                ),
                meta={'agent_id': agent_id, 'status': None},
            )
        return ToolResult(
            output=(
                f'agent {agent_id}: name={info.name} status={info.status.value} '
                f'parent={info.parent_id} children={len(info.children)}'
            ),
            meta={
                'agent_id': agent_id,
                'status': info.status.value,
                'children': list(info.children),
            },
        )


class KillAgent(Tool):
    """终止一个 Agent 的事件循环。它仍留在注册表里，因此此后仍可寻址、可被一条消息复活。"""

    __tool_command_args__ = ()

    def __init__(self, runtime: Runtime, agent_id: str) -> None:
        self._runtime = runtime
        self._agent_id = agent_id

    async def execute(self, agent_id: str) -> ToolResult:
        """终止目标的事件循环。

        agent_id: 要终止的 Agent 的 id
        """
        if agent_id == self._agent_id:
            return ToolResult(
                output=(
                    '期望: KillAgent 的目标是别人。'
                    ' 实际: 收到的是你自己的 id。'
                    ' 下一步: 杀自己会在这次工具调用返回之前就把你取消掉，'
                    '这一批调用因此不会有结果、轨迹会停在半路。'
                    '想结束当前工作就直接给出最终回复；由别人来停你则请你的父 Agent 动手。'
                )
            )
        if await self._runtime.find(agent_id) is None:
            return ToolResult(
                output=(
                    f'期望: agent {agent_id!r} 在注册表里。'
                    ' 实际: 没有这个条目，它已经不在了。'
                    ' 下一步: 核对 id；已经不在的 Agent 不需要再终止一次。'
                )
            )
        await self._runtime.kill(agent_id)
        return ToolResult(
            output=(
                f'已终止 agent {agent_id} 的事件循环。它仍留在注册表里，'
                '一条 SendMessage 就能让它带着原有上下文继续。'
            ),
            meta={'agent_id': agent_id},
        )


def orchestration_tools(runtime: Runtime, agent_id: str) -> dict[str, Tool]:
    """给一个 Agent 造一套编排工具。

    **次序固定且不是字母序**：工具表的迭代顺序逐字进请求前缀（spec §5.2），重排不报错，只让整条
    会话的缓存静默失效。这个次序照 spec §5.6 的清单：先派活、再分叉、再通信、再查询、最后终止。
    """
    return {
        'Delegate': Delegate(runtime, agent_id),
        'Fork': Fork(runtime, agent_id),
        'SendMessage': SendMessage(runtime, agent_id),
        'QueryAgent': QueryAgent(runtime, agent_id),
        'KillAgent': KillAgent(runtime, agent_id),
    }
```

`tools/__init__.py` 同步导出这五个类与工厂。

- [ ] **Step 4: 让 `Runtime` 能自动装上它们**

import 补 `from syndica.kernel.tool import validate_tool_names`、
`from syndica.kernel.tools import orchestration_tools`。

`__init__` 加 `orchestration: bool = False` 与 `self._orchestration = orchestration`。

`spawn` 里 `attach_gate` 之后加：

```python
        if self._orchestration:
            installed = orchestration_tools(self, agent_id)
            clashes = sorted(set(installed) & set(agent.options.tools))
            if clashes:
                raise ConfigError(
                    expected='这个 Agent 自己的工具名与编排工具不冲突',
                    received=f'它已经有同名工具 {clashes}',
                    next_action=(
                        '给自己的工具换个名字，或者关掉 Runtime(orchestration=True) 手工装配。'
                        '静默覆盖会让它自己那个同名工具永远调不到，而 run 照常报 COMPLETED'
                    ),
                )
            agent.options.tools.update(installed)
            validate_tool_names(agent.options.tools)
```

> `agent.options.tools` 是**活的** dict，executor 与它共享同一个对象（计划 1 的裁决），因此这里
> 更新它会立刻对本 Agent 生效，不需要重建 executor。这也正是那条「不替换成副本」的用途。

`ConfigError` 需要 import。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests -v && uv run ruff check . && uv run pyright && uv run python scripts/check_deps.py`
Expected: 全部 passed

- [ ] **Step 6: 提交**

```bash
git add src/syndica/kernel/tools src/syndica/kernel/runtime.py tests/kernel/test_orchestration.py
git commit -m "feat(kernel): 编排五件套齐了，且自动装配不会静默盖掉同名工具

QueryAgent 走 find，因此 find「绝不激活任何 Agent」这条明文承诺一路守到了模型手里：
查询类工具必须能把「不存在」和「存在但没在跑」分开回答，而这两者的区别只有不激活
才看得见。

KillAgent 拒绝杀自己：取消会在这次工具调用返回之前就把调用者取消掉，这一批调用因此
不会有结果、轨迹停在半路。想结束当前工作直接给最终回复就行。

工具表次序固定且不是字母序：迭代顺序逐字进请求前缀，重排不报错，只让整条会话的缓存
静默失效。

自动装配遇到同名工具报错而不是覆盖。静默覆盖会让 Agent 自己那个同名工具永远调不到，
而 run 照常报 COMPLETED——失败被当成成功上报。"
```

---

## Task 16: 端到端验证与公开面收口

**Files:**
- Modify: `src/syndica/kernel/__init__.py`
- Test: `tests/kernel/test_public_api.py`
- Test: `tests/integration/test_multi_agent_orchestration.py`

**Interfaces:**
- Consumes: 全部前序任务
- Produces: `syndica.kernel` 顶层导出（新增 22 项）；本计划的交付判据

- [ ] **Step 1: 写端到端测试**

```python
# tests/integration/test_multi_agent_orchestration.py
"""端到端：本计划的交付判据。

一个编排者派活给一个工人、工人做完并保持可寻址、编排者再给它发一条消息让它继续，全程只有 LLM 是
假的——Runtime、事件循环、消息、工具、控制面都是真的（spec §13.1）。
"""

from __future__ import annotations

import asyncio

from syndica.kernel import (
    AgentOptions,
    AgentStatus,
    BaseAgent,
    ExitMode,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    StopReason,
    Tool,
    ToolCall,
    ToolResult,
)


class Notebook(Tool):
    """把 entry 记进笔记本。"""

    __tool_command_args__ = ()

    def __init__(self) -> None:
        self.entries: list[str] = []

    def is_concurrency_safe(self, args: dict[str, object]) -> bool:
        return False

    async def execute(self, entry: str) -> ToolResult:
        """记下 entry。"""
        self.entries.append(entry)
        return ToolResult(output=f'recorded: {entry}', meta={'count': len(self.entries)})


def _worker_factory(notebook: Notebook):
    def build(hooks, events):
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM(
                    [
                        ScriptedTurn(
                            tool_calls=[
                                ToolCall(
                                    id='w1', name='Notebook', arguments='{"entry":"survey"}'
                                )
                            ]
                        ),
                        ScriptedTurn(content='survey done'),
                        ScriptedTurn(
                            tool_calls=[
                                ToolCall(
                                    id='w2', name='Notebook', arguments='{"entry":"follow-up"}'
                                )
                            ]
                        ),
                        ScriptedTurn(content='follow-up done'),
                    ]
                ),
                tools={'Notebook': notebook},
                system_prompt='You are a worker.',
            ),
            hooks=hooks,
            events=events,
        )

    return build


async def test_an_orchestrator_delegates_then_keeps_talking_to_the_same_worker():
    notebook = Notebook()
    runtime = Runtime(orchestration=True, agents={'Worker': _worker_factory(notebook)})

    def build_orchestrator(hooks, events):
        return BaseAgent(
            AgentOptions(
                name='Orchestrator',
                llm=ScriptedLLM(
                    [
                        ScriptedTurn(
                            tool_calls=[
                                ToolCall(
                                    id='o1',
                                    name='Delegate',
                                    arguments='{"agent":"Worker","query":"survey the field"}',
                                )
                            ]
                        ),
                        ScriptedTurn(content='the worker finished the survey'),
                    ]
                ),
                tools={},
                system_prompt='You orchestrate.',
            ),
            hooks=hooks,
            events=events,
        )

    orchestrator = await runtime.spawn(
        build_orchestrator, query='find out what we know', exit_mode=ExitMode.ZOMBIE
    )
    ctx = await asyncio.wait_for(runtime.wait(orchestrator), timeout=5.0)

    assert ctx.stop_reason is StopReason.COMPLETED
    assert [block.role for block in ctx.current_traj] == [
        'user',
        'assistant',
        'tool',
        'assistant',
    ]
    delegated = ctx.current_traj[2]
    assert delegated.content == 'survey done'
    worker_id = delegated.extra['tool_meta']['agent_id']
    assert notebook.entries == ['survey']

    info = await runtime.find(worker_id)
    assert info is not None
    assert info.status is AgentStatus.ZOMBIE
    assert info.parent_id == orchestrator

    delivered = await runtime.send(worker_id, 'now do the follow-up')
    assert delivered.ok is True
    worker_ctx = await asyncio.wait_for(runtime.wait(worker_id), timeout=5.0)

    assert notebook.entries == ['survey', 'follow-up']
    assert [block.content for block in worker_ctx.current_traj if block.role == 'user'] == [
        'survey the field',
        'now do the follow-up',
    ]

    await runtime.despawn(orchestrator)
    assert await runtime.find(orchestrator) is None
    assert await runtime.find(worker_id) is None
    await runtime.shutdown()
```

> `tool_meta` 只在 `ToolResult.meta` 非空时才写进 `extra`（计划 1 的实现），`_delivered` 一律带
> `agent_id`，因此这条断言成立。

- [ ] **Step 2: 收口公开面**

```python
# src/syndica/kernel/__init__.py  （在既有 import 之外补这些）
from syndica.kernel.control import TurnGate
from syndica.kernel.danger import (
    AllowAll,
    BlockHighRisk,
    DangerJudge,
    DangerPolicy,
    DangerRule,
    DangerVerdict,
    Disposition,
    RiskLevel,
)
from syndica.kernel.plugins import Plugin
from syndica.kernel.tools import (
    Delegate,
    Fork,
    KillAgent,
    QueryAgent,
    SendMessage,
    orchestration_tools,
)
from syndica.kernel.waiting import (
    Confirmation,
    Reply,
    UserInput,
    WaitResult,
    WaitState,
    Waiter,
)
```

`__all__` 的最终内容（59 项，已排序）：

```python
__all__ = [
    'AgentFactory',
    'AgentInfo',
    'AgentOptions',
    'AgentRegistry',
    'AgentStatus',
    'AllowAll',
    'ArtifactRef',
    'BaseAgent',
    'BlockHighRisk',
    'ConfigError',
    'Confirmation',
    'Context',
    'ContextBlock',
    'ContextOverflowError',
    'ContractViolation',
    'DangerJudge',
    'DangerPolicy',
    'DangerRule',
    'DangerVerdict',
    'Delegate',
    'DeliveryResult',
    'Disposition',
    'Event',
    'EventBus',
    'ExitMode',
    'Fork',
    'HookResult',
    'HooksCenter',
    'InMemoryAgentRegistry',
    'InMemoryMessageBroker',
    'Junction',
    'KillAgent',
    'LLM',
    'LLMConfig',
    'LLMResponseError',
    'Message',
    'MessageBroker',
    'Plugin',
    'QueryAgent',
    'Reply',
    'RiskLevel',
    'Runtime',
    'ScriptedLLM',
    'ScriptedTurn',
    'SendMessage',
    'StopReason',
    'SyndicaError',
    'Tool',
    'ToolCall',
    'ToolFatalError',
    'ToolResult',
    'Trigger',
    'TrustLevel',
    'TurnGate',
    'UserInput',
    'WaitResult',
    'WaitState',
    'Waiter',
    'orchestration_tools',
]
```

`tests/kernel/test_public_api.py` 里的 `_KERNEL_EXPORTS` 逐字同步成上面这份（**仍然写死，不从
`kernel.__all__` 推导**：删一个导出、加一个导出都必须先在那里变红）。

- [ ] **Step 3: 全量门禁**

Run: `uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run python scripts/check_deps.py`
Expected: 全部 passed；lint、格式化、类型检查与依赖方向校验均通过

- [ ] **Step 4: 单独跑一遍端到端**

Run: `uv run pytest tests/integration/test_multi_agent_orchestration.py -v`
Expected: 1 passed

- [ ] **Step 5: 提交**

```bash
git add src/syndica/kernel/__init__.py tests/kernel/test_public_api.py tests/integration/test_multi_agent_orchestration.py
git commit -m "feat(kernel): 计划 2 的交付判据达成——一个编排者派活、工人保持可寻址、再收一条消息继续

至此 spec §4.2 的 11 个公开方法全部就位，六态状态机的每一个状态都有产生它的路径与
观察它的方式，DEFERRED 有了真正的消费时机，一条消息真的能复活一个 ZOMBIE。

端到端只有 LLM 是假的：Runtime、事件循环、消息、工具、控制面都是真的。这一条覆盖的
正是最难测的那部分——多 Agent 编排里「A 派活给 B、B 汇报、A 继续」的时序，用真模型
既不确定也无法按需触发失败路径。

公开面加了 22 项。每一项都是永久兼容承诺，因此逐字写死在 test_public_api 里，
不从 __all__ 推导——用被测对象自己的导出面算出来的期望值测的是恒等式。"
```

---

## 自检

**1. 规格覆盖** —— 本计划对应 spec 里程碑 1 的以下部分：

| spec 章节 | 覆盖情况 |
| --- | --- |
| §4.1 六态状态机 | Task 3（IDLE↔RUNNING）、Task 5（PAUSED）、Task 6 / 7（ABORTING）、Task 9（WAITING）、Task 3 / 7（ZOMBIE 及其队列语义）。至此六态**全部有产生路径** |
| §4.1 ExitMode 三档 | Task 3（IDLE 常驻 / ZOMBIE 摘队列留条目）、Task 7（EXIT 走级联拆除） |
| §4.2 Runtime 11 个公开方法 | 计划 1 已有 `spawn` / `wait` / `send` / `find`；本计划补 `kill`（Task 7）/ `despawn`（Task 7）/ `pause`（Task 5）/ `resume`（Task 5）/ `abort`（Task 6）/ `ensure_active`（Task 4）/ `wait_for`（Task 9）。**11 个到齐** |
| §4.2 `Waiter` 密封类型 / `WaitResult` 四态 / 强制超时 | Task 8（类型）、Task 9（四态各自的产生路径） |
| §4.2 `send` 默认激活、`find` 绝不激活 | Task 4（`activate` 与 `on_missing_queue` 接线）、Task 15（`QueryAgent` 守住 `find` 的承诺） |
| §4.3 注册表 vs 进程内实例表 | Task 1（控制块与实例表同侧）、Task 3（`_Slot`） |
| §4.3 租约与孤儿回收 | **不做** → 里程碑 1 之后（字段已在计划 1 就位） |
| §4.4 `on_missing_queue` 是按需激活的基础 | Task 4 |
| §4.5 `PENDING` / `DEFERRED` 双时机 | Task 1（门的分流）、Task 2（注入）、Task 3（周期边界） |
| §5.1 turn 循环第 1–3 步（中止 / 挂起 / 消费消息） | Task 2 |
| §5.1 第 4 步（注入背景工具结果） | **不做** → 里程碑 7（背景化工具不在里程碑 1） |
| §5.3 三层装配第一、二层 | Task 13。第三层 Runtime Module → 计划 5 |
| §5.4 切点集合 | Task 2（`message_injected`）、Task 7（`runtime_shutdown` / `agent_spawn` / `agent_despawn`）、Task 11（`tool_confirmation_required`）。`runtime_start` 与背景工具两个切点 → 计划 5 / 里程碑 7 |
| §5.6 并发执行、安全判定从严、上限可配 | Task 12 |
| §5.6 权限与确认：判定与处置分离、授权仅当次 | Task 10（纯规则）、Task 11（阻断 / 放行两档）。**人工确认交互 → 计划 6** |
| §5.6 编排工具五个 | Task 14（`Delegate` / `Fork`）、Task 15（`SendMessage` / `QueryAgent` / `KillAgent`） |
| §5.6 文件 / 执行 / 终端 / 任务跟踪 / 产物 / 人机交互 / 背景 / 长程 / Skill 各组工具 | **不做** → 计划 4（文件与执行）、计划 6（`AskUser*`）、里程碑 5（PTY）、里程碑 7 / 8（其余） |
| §12.2 不可信标记可传递 | Task 2（消息带 trust 进上下文）、Task 14（子 Agent 结论继承最严 trust）。**溯源链与升级处置 → 里程碑 5** |
| §13.1 L2 场景回放 | 本计划的集成测试用 `ScriptedLLM` 驱动真 Runtime，是它的雏形；**声明式场景层 → 计划 6** |
| Global Constraints 错误三件套 | 各任务的错误消息遵循；模型可见的失败（阻断、派活失败、未知角色）同样按三件套写 |

**明确不在本计划**（本文「文件结构」一节已列表，此处复述归属）：文件与执行工具 + `Workspace` / `Portal` → 计划 4；声明式 Agent 与拓扑 → 计划 3；Runtime Module 与 Flag → 计划 5；CLI、`AskUser` / `AskUserQuestion`、人工确认交互、L2 声明式场景层 → 计划 6；PTY → 里程碑 5；基于租约的孤儿回收 → 里程碑 1 之后。

**2. 占位符扫描** —— 无 TBD / TODO；每个代码步骤都给出完整可运行代码；无"参照 Task N"式引用。三处引用既有代码的地方（Task 12 的参数解析、Task 11 的 `_execute_one` 插入点、Task 13 的 `HooksCenter` 局部改动）都逐字给出了要写的代码与它在文件中的位置，不是"照旧"。

**3. 类型一致性核对**

- `TurnGateDecision(abort, messages)` 在 Task 1 定义（`messages` 是 `tuple[Message, ...]`），Task 2 的 `_inject` 按元组消费，Task 1 的两个门实现都返回元组。
- `ControlBlock` 的方法名在 Task 1 定下：`hold_until_runnable` / `take_abort` / `wait_until_parked` / `defer` / `take_deferred` / `enter_run` / `leave_run`，Task 3（事件循环）、Task 5（pause / resume）、Task 6（abort）四处调用一致。
- `Runtime._switch(agent_id, *, new, allowed)` 在 Task 3 定义，Task 4 / 5 / 6 / 7 / 9 五处按同一签名调用，`allowed` 一律是 `tuple[AgentStatus, ...]`。
- `_Slot` 字段在 Task 3 定义（`factory` / `exit_mode` / `control` / `gate` / `owned`），Task 9 追加 `waiter`，Task 14 的 `factory_of` 读 `factory`，Task 4 的 `ensure_active` 读 `exit_mode`。
- `Waiter.timeout` 与 `correlation_of(waiter)` 在 Task 8 定义，Task 9 的 `_awaited` 按 `(agent_id, correlation, timeout, token)` 调用一致。
- `WaitState` 四个成员在 Task 8 定义，Task 9 的三条返回路径（`ANSWERED` / `TIMEOUT` / 中断令牌带出的 `PREEMPTED` / `ABORTED`）覆盖全部四个。
- `DangerVerdict(rule_id, level, reason)` 与 `Disposition` 在 Task 10 定义，Task 11 的 `_gated_result`、Task 12 的 `_concurrency_safe` 按同一形状消费。
- `DangerPolicy.decide(verdict, call)` 的参数顺序在 Task 10 定义，Task 11、Task 12 与测试里的自定义策略三处一致。
- `ToolExecutor(tools, hooks, *, judge, policy, max_concurrency)` 在 Task 11 / 12 逐步成型，`BaseAgent.__init__` 在 Task 11 / 12 / 13 三次更新后与它最终签名一致。
- `AgentOptions` 新增字段一律**追加在末尾**（`danger_judge` → `danger_policy` → `max_tool_concurrency`），字段顺序计入公开 API。
- `orchestration_tools(runtime, agent_id) -> dict[str, Tool]` 在 Task 15 定义，Task 15 的 `Runtime.spawn` 与测试按同一返回类型消费；五个键的次序在实现、测试与 `__init__` 导出三处一致。
- `Plugin.name` / `Plugin.register(hooks)` 在 Task 13 定义，`install_plugins` 与测试里的 `_Recorder` 一致。
- `Junction` 成员在 Task 2 / 7 / 11 三次扩充，每次同步更新 `OBSERVABLE`、枚举整表断言与确切内容断言；最终 16 项，`INTERCEPTABLE` 保持 6 项不变。

