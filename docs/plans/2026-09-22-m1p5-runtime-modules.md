# 里程碑 1 · 计划 5：Runtime Module、Flag、重试与前缀漂移检测

> **执行者须知：本文件是需求，不是参考。** 逐字落地。公开 Flag key 与装配顺序一旦发布就是兼容
> 承诺，不得在实现中临时换名或换序。

**状态：已完成。** Task 1–7 分别提交为 `a54da01`、`8f75e0e`、`da996c0`、`f131a28`、
`6533439`、`608cf7f`、`4079d37`；Task 8 与本次文档收口同提交。全量测试、类型、lint、依赖检查及
真实 Runtime E2E 均通过，逐 Task 独立评审无未处理 Important 以上问题。

**Goal:** 宿主能用一份 Runtime Module 声明 Flag，并一次性注入 per-Agent 插件、Runtime 服务、
per-Agent 工具与启停钩子；每个 Agent 得到可追溯的 Flag 快照。内置 Module 进一步提供等待墙钟上限、
不可信高风险处置、LLM 重试 / 故障转移与前缀漂移检测。

**Architecture:** `RuntimeModule` 是声明，`Runtime` 是唯一装配者。Runtime 构造时先完整校验 Module、
Flag 与服务名，再改动任何 Runtime 自有状态；spawn 时先解析该 Agent 的 Flag 快照，再创建插件与工具，
被拒的 spawn 不写 Registry、不建邮箱、不启动任务。Module 不增加 `Runtime.start` 或
`Junction.RUNTIME_START`。LLM 重试只经 `LLM_FAILURE` 切点实现；前缀漂移检测只观察最终请求，不改请求。

**Tech Stack:** Python 3.11、pytest、pytest-asyncio、pydantic；内核不新增依赖。

**依据：** spec §5.2、§5.5、§5.7、§12.2；M1 scope 的计划 5；登记表 D-18、D-47（墙钟一半）、
D-19–D-23、D-29、D-55。

---

## 关闭什么、不要动什么

| 编号 | 本计划处置 |
| --- | --- |
| D-18 | `max_wait_seconds` Flag 同时 clamp `wait_for` 与在轮等待。`0` 表示宿主未设上限；不凭空发明有限默认值。 |
| D-47 | `_wait_in_turn` 在上限到达时交回 `WaitState.TIMEOUT`，不取消被等 Agent。provider 自己的 timeout 已由计划 4 关闭。 |
| D-19–D-23 | 当前代码与测试已实现；Task 1 用定向用例和变异复核后关登记，不重复改实现。 |
| D-29 | `untrusted_high_risk` 支持 `reject` / `ask` / `allow`，默认 `reject`。`ask` 仍走现有 `CONFIRM`；交互通道归计划 6。 |
| D-55 | `spawn(flags=...)` 与 `AgentInfo.flags` 同批落地；字段保存**解析后的完整快照**。 |

**明确不做：**

- `Runtime.start`、`Junction.RUNTIME_START`
- 计划 6 的 CLI、YAML、AskUser、人工确认答复通道与 L2 场景 DSL
- 精确 tokenizer、计费、trace、缓存 backend；漂移检测只给稳定的估算值
- 流式响应、预算树、压缩、记忆、Agent 树回收
- 在 kernel 读取环境变量来猜“开发 / 生产”

---

## 公开契约

### 1. Flag 声明与取值

新增冻结数据类 `FlagDeclaration`，字段顺序固定：

1. `key: str`
2. `default: bool | int | str`
3. `description: str`
4. `choices: tuple[bool | int | str, ...] = ()`
5. `minimum: int | None = None`
6. `maximum: int | None = None`

约束：

- `key` 是非空 snake_case；Flag key 计入公开 API，不 trim、不改大小写、不做别名。
- `description` 是非空字符串，供 CLI / UI / 文档共同消费。
- 值只允许 `bool | int | str`，并按**精确类型**匹配默认值；`bool` 不得冒充 `int`。
- `choices` 若非空，默认值与覆盖值都必须在其中；成员类型必须与默认值精确相同。
- `minimum` / `maximum` 只用于 `int` Flag，边界包含端点；`minimum <= maximum`。
- Module 间 Flag key 全局唯一，即使两份声明逐字相同也拒绝，避免所有权不明。

Runtime 的 Flag 解析顺序固定：

1. 已装 Module 的声明默认值
2. `Runtime(flags=...)` 的宿主级覆盖
3. 父 Agent 已解析快照；`fork_from` 存在时以源 Agent 快照取代父快照
4. `spawn(flags=...)` 的本次覆盖

未知 key、类型错误、越界、非法 choice 都在副作用之前抛 `ConfigError`。Runtime 与 Agent 对外只交
只读快照；`AgentInfo.flags` 存普通 `dict` 快照以供 Store 序列化。

本计划固定六个公开 key：

| key | 类型 / 默认值 | 所属 Module | 语义 |
| --- | --- | --- | --- |
| `max_wait_seconds` | `int / 0, minimum=0` | `SafetyModule` | `0` 不 clamp；正数同时限制外部答复等待与在轮等待 |
| `untrusted_high_risk` | `str / reject` | `SafetyModule` | choices = `reject, ask, allow` |
| `llm_retry_attempts` | `int / 0, minimum=0` | `RetryModule` | 初次调用之外，对原 LLM 再试几次 |
| `llm_retry_backoff_ms` | `int / 0, minimum=0` | `RetryModule` | 每次后续尝试前等待的毫秒数 |
| `llm_fallbacks` | `str / ''` | `RetryModule` | 逗号分隔的 fallback 名，按出现顺序各试一次 |
| `prefix_drift_detection` | `bool / True` | `PrefixDriftModule` | 开启逐轮前缀比较；生产宿主显式覆写为 false |

### 2. Runtime Module 形状

新增冻结数据类 `RuntimeModule`，字段顺序固定：

1. `name`
2. `flags`
3. `agent_plugins`
4. `services`
5. `tool_factories`
6. `on_start`
7. `on_shutdown`

构造签名固定为：

```python
RuntimeModule(
    name: str,
    *,
    flags: Sequence[FlagDeclaration] = (),
    agent_plugins: Sequence[
        Callable[[Runtime, BaseAgent, Mapping[str, FlagValue]], Plugin]
    ] = (),
    services: Mapping[
        str, Callable[[Runtime, Mapping[str, FlagValue]], object]
    ] | None = None,
    tool_factories: Sequence[
        Callable[[Runtime, BaseAgent, Mapping[str, FlagValue]], Mapping[str, Tool]]
    ] = (),
    on_start: Callable[[Runtime, Mapping[str, FlagValue]], None] | None = None,
    on_shutdown: Callable[[Runtime], Awaitable[None]] | None = None,
)
```

所有 `Sequence` 在构造时转成 tuple，`services` 复制后交只读 mapping；调用方之后修改原容器不得改变
Module。`SafetyModule()`、`RetryModule(...)`、`PrefixDriftModule(...)` 都是 `RuntimeModule` 的具体
子类，不是全局单例，也不是返回 Module 的函数。

四类注入逐字对应 spec §5.5：

- `agent_plugins`：工厂收到 `(runtime, agent, flags)`，返回一个 `Plugin` 实例；按 Module 顺序、
  再按工厂顺序装到该 Agent。它们在角色自己注册的局部 hook 之后、Runtime 级插件之前执行。
- `services`：`service_name -> factory(runtime, flags)`；结果挂成 `runtime.<service_name>`。
  服务名必须是公开 Python 标识符、不得以下划线开头、不得覆盖 Runtime 已有属性或另一项服务。
- `tool_factories`：工厂收到 `(runtime, agent, flags)`，返回 `Mapping[str, Tool]`；每次 spawn 都重建，
  不共享绑定了 `agent_id` 的实例。
- `on_start` / `on_shutdown`：`on_start(runtime, flags)` 在 Runtime 构造末尾同步执行；
  `on_shutdown(runtime)` 在所有 Agent 与 reaper 停下后按 Module **逆序**各 await 一次。

Module 名非空且在一个 Runtime 内唯一。Module、服务、工厂与回调在运行前完整校验。同步装配失败时，
Runtime **自己拥有的** hooks 与已挂服务回到构造前状态；第三方 factory / `on_start` 在 Runtime
之外启动的任务、写入的文件或外部注册不在回滚承诺内，Module 作者必须让自己的失败路径可清理。
本地快照捕获 `BaseException` 后还原再原样抛出。

构造期校验 callback 的参数签名，并拒绝会返回 coroutine 的异步 service / tool / plugin factory 与
`on_start`。`on_shutdown` 的公开类型是“callable 返回 Awaitable”，因此同步包装器合法；Runtime
调用后校验返回值确实可 await，不合法时产生带 Module 名与修复动作的 `ConfigError`，但仍继续其余
清理。

shutdown 使用一个独立 `_shutdown_task` 独占完整清理流程；所有 `shutdown()` 调用都用
`asyncio.shield` 等同一个任务。取消某个等待者不取消清理，后续调用仍能等到同一结果。顺序固定为：
置终态并摘 broker 回调 → 取消并等待 Agent → 等 reaper → 触发一次 `RUNTIME_SHUTDOWN` → Module
逆序 shutdown → 关闭事件总线。Module callback 失败不跳过其余 callback 或总线关闭；普通异常最终
组成 `ExceptionGroup`，含 `CancelledError` 等 `BaseException` 时组成 `BaseExceptionGroup`。

`Runtime` 新增构造参数 `modules=` 与 `flags=`；新增只读面 `modules`、`flags`、
`flag_declarations`。没有 Module 时，Runtime 行为与计划 4 完全一致。

参数只能追加：`Runtime.__init__` 固定为现有 `..., orchestration=False, modules=None, flags=None`；
`Runtime.spawn` 固定为现有 `..., metadata=None, flags=None`。`AgentInfo.flags` 以独立
`default_factory=dict` **追加在 `created_at` 之后**，不得插进已有字段中间。

### 3. spawn 与继承

`Runtime.spawn(..., flags=None)` 在造 Agent 之后、注册之前完成以下装配：

1. 保存 Agent 工厂刚返回、尚未经过任何 Runtime 装配的 tools / hooks 基线；
2. 解析该 Agent 的 Flag 快照；
3. 装编排工具；
4. 装 Module 的 per-Agent 插件；
5. 装 Module 工具；
6. 调角色的 `install_own_tools`；
7. 所有可失败装配完成后才 `_attach_gate`；
8. 写 `AgentInfo.flags`，再注册、建邮箱、启动驱动任务。

Module 插件或工具工厂抛错、返回错误类型、产生重名工具时，这次 spawn 不留下 AgentInfo、邮箱、任务或
半套工具。若 Agent 工厂把实例另外暴露给宿主，失败后该实例的 tools 与 hooks 也恢复到 Module 装配
前，也就是**工厂刚返回、任何 Runtime 装配尚未发生**的基线；gate 尚未安装，同一实例可被修正后重新
spawn。宿主传入的 Mapping 不被修改。错误点至少覆盖第二个插件工厂、`Plugin.register`、第二个工具
工厂、错误 Mapping、工具身份校验、重名和 `install_own_tools`。工具顺序按上述装配顺序稳定，禁止
排序；同名响亮拒绝，不覆盖。

Runtime 为每次 spawn 新建 `module_hooks = HooksCenter(outer=self.hooks)`，并把它而不是
`self.hooks` 交给 Agent 工厂；Agent 自己的中心因此形成
`local -> per-Agent Module -> Runtime plugin` 三层链。Runtime 通过
`BaseAgent._install_module_plugins(module_hooks, ...)` 安装 Module 插件，该私有入口复用
`install_plugins` 的快照回滚，不直接修改 `agent._hooks`。spawn **之后**再调用公开
`agent.register_hook()`，新局部 hook 仍必须排在 Module 前。per-Agent Module 插件名在同一 Agent
的 Module 层全局唯一；它与 Runtime 级插件分属两层，允许同名，但错误消息必须带 Module 名和层级，
不能只报裸插件名。

普通子 Agent 默认继承父 Agent 的已解析快照；fork 默认继承源 Agent。显式 `spawn(flags=...)` 最后
覆盖。快照之后修改调用方原 dict、父条目或 Runtime 默认值，均不得改变已创建 Agent。
`parent_id` 与 `fork_from` 同时存在且值不同时，fork 快照**整体取代**父快照后再叠显式覆盖，不把
两份 Agent 快照逐键拼接。

### 4. 两个安全 Flag 的消费

`SafetyModule` 只声明 `max_wait_seconds` 与 `untrusted_high_risk`，行为由现有 Runtime /
ToolExecutor 接缝消费，不另造第二套等待或危险判定。

Runtime 在注册 Agent 前调用 `BaseAgent._configure_runtime_flags(flags)`；该私有入口把安全处置的
不可变枚举值交给已构造的 ToolExecutor。禁止 Runtime 直接改 `agent._executor`。两个 Agent 用不同
Flag 时必须各自生效，不能共享上一个 Agent 的配置。

- `wait_for` 的有效 timeout 为 `min(waiter.timeout, max_wait_seconds)`；上限为 0 时原样使用
  `waiter.timeout`。
- `_wait_in_turn` 在上限为正时给 `asyncio.wait` 同一上限；到期只让发起方得到 `TIMEOUT`，不取消
  目标 Agent。
- 两类等待同时就绪时都按 `answer / target 完成 > abort 或 preempt > timeout` 裁决；abort 与
  preempt 之间仍由 `_WaitToken` 保留第一个信号。`wait_for` 必须把有效上限传给 broker，不在外层
  再套 `asyncio.timeout`。
- `disposition_of` 对「HIGH + UNTRUSTED」先按 Flag 映射为 `BLOCK / CONFIRM / ALLOW`，仍在宿主
  `DangerPolicy` 之前，且不咨询 policy。默认 `reject` 保持现行为。
- `ask` 触发现有 `TOOL_CONFIRMATION_REQUIRED`，本计划仍返回“等待确认”的工具结果；计划 6 才接答复。

### 5. RetryModule

`RetryModule(fallbacks: Mapping[str, LLM], *, sleep=asyncio.sleep)` 是一个 `RuntimeModule`：

- `fallbacks` 是名字到 `LLM` 的快照；`llm_fallbacks` 按逗号顺序引用它。
- `sleep` 是可注入的异步函数，生产默认 `asyncio.sleep`，测试用假时钟。
- 它为每个 Agent 创建独立插件，在 `LLM_FAILURE` 上读取该 Agent 的 Flag 快照与原 Agent 的 LLM。
- 前序局部 hook 已经在 kwargs 中给出 `response` 时立即 no-op，不再次调用模型、不覆盖前序结果。
- 先对原 LLM做 `llm_retry_attempts` 次额外尝试，再按 `llm_fallbacks` 各尝试一次；第一次成功即用
  `modified['response']` 交回替身。
- 每次调用使用切点收到的**最终** `request`，消息、工具和键序不重建。
- `CancelledError` 立即向外传播；候选的普通失败记入原异常 notes。全部失败后返回空 `HookResult`，
  由内核按既有语义重新抛初始错误。
- 重试耗尽不得直接 raise：后续 `LLM_FAILURE` hook 仍有机会恢复。最终无人恢复时抛出的必须是初始
  异常对象，既有 notes 保留，候选失败按调用顺序追加。
- 重试预算按**每次 `LLM_FAILURE` 触发**计算。若替身后来被 `POST_LLM_RESPONSE` 清空并触发第二次
  `LLM_FAILURE`，它得到一份新的预算；用例必须把这两次触发和总调用数钉死。
- `llm_fallbacks=''` 表示空链；非空时按逗号切分并 trim 每项。首尾逗号、连续逗号、trim 后空项、
  重复名、未知名都在 spawn 副作用之前报 `ConfigError`。

Module 不包装 `LLM`、不改 `LLM.config`，不把重试塞进 kernel agent loop。

### 6. PrefixDriftModule

`PrefixDriftModule(*, token_counter: Callable[[bytes], int] | None = None)` 注入
`runtime.prefix_drift` 服务，并给每个 Agent 装独立观察插件。

新增可选的 `CachePrefixProvider` Protocol：

```python
def cache_prefix_units(
    self, messages: list[ContextBlock], **kwargs: Any
) -> tuple[bytes, ...]: ...
```

它由 LLM backend 给出 provider 实际用于缓存匹配的有序请求单位。`OpenAICompatible` 与
`ScriptedLLM` 本计划实现该协议；第三方 LLM 未实现时，检测器使用下面的通用逻辑编码，并在记录中
标 `exact=False`，不得把逻辑编码冒充 provider 的精确字节。

通用逻辑编码的检测单位顺序固定为：

1. `llm.config.provider` 与 `model`
2. `llm.config.request_args` 与 `PRE_LLM_REQUEST` 产生的其余请求参数
3. system block
4. tools（保留 schema 的列表顺序与每层映射键序）
5. 历史 block（保留列表顺序；只取 provider 可见的 `role / content / tool_calls / tool_call_id`）

每个单位用稳定 JSON（UTF-8、紧凑分隔符、不排序键）编码并取 SHA-256。相邻请求满足“上次单位序列是
本次单位序列的前缀”即稳定；只 append 新历史不报警。第一处不相等就是变更点。

检测发生在 `POST_LLM_RESPONSE`、`LLM_FAILURE`、`CONTEXT_OVERFLOW` 收到最终 request 时；同一个
request 最多记一次。记录至少含 agent id、变更单位、前后摘要、变更点之后失去复用的 UTF-8 字节数、
估算 token 数。默认估算为 `ceil(bytes / 4)`，字段与日志必须明确写“估算”，不得冒充 provider
计费；服务允许宿主注入精确 token counter 替换它。

开启时每次漂移写 WARNING，并追加到服务的只读记录；关闭时不计算摘要、不写记录。新 Agent 与 fork
的第一次请求各自建立基线，不把不同 Agent 的状态混在一起。`OpenAICompatible` 的单位必须由它实际
构造请求体的同一组 helper 产生，测试覆盖 `request_args` 与 PRE hook 新增参数的漂移。
只改 `trust / extra / artifacts` 不报警；UTF-8 字节数按编码后的真实长度计算。`records` 每次返回
不可变快照，调用方改不了服务内部历史。

---

## 任务拆解

每个 Task 都执行：失败测试 → 最小实现 → 定向测试 → `ruff check` → `ruff format --check` →
`pyright` → 至少一条真实变异 → 哈希还原 → 独立评审 → 修复 → commit。新增文件先加入索引，再做变异。

### Task 1：复核并关闭 D-19～D-23

**Files:** `tests/kernel/test_errors.py`、`tests/kernel/test_agent_types.py`、
`docs/decisions/2026-09-14-deferred-items.md`

- 证明错误类名来自 `type(self)`、类型错是 `TypeError`、空串是 `ValueError`、校验顺序固定为
  `expected -> received -> next_action`、新增必填字段的子类必须覆写 `__reduce__`。
- 给 `AgentStatus` / `ExitMode` 增加“不等于同值字符串”的断言。
- 变异：把 `AgentStatus` 或 `ExitMode` 混入 `str`，对应测试必须变红。

### Task 2：FlagDeclaration 与 RuntimeModule 声明

**Files:** 新建 `src/syndica/kernel/modules.py`、`tests/kernel/test_modules.py`

- 落地两份冻结声明、值域校验、精确类型、choice / min / max、Module 名与四类注入形状。
- 校验错误全部三件套，并对每个公开字段顺序写死断言。
- 变异：允许 bool 通过 int Flag，测试必须变红。

### Task 3：Runtime 级 Module 装配

**Files:** `src/syndica/kernel/modules.py`、`src/syndica/kernel/runtime.py`、
`tests/kernel/test_modules.py`

- 新增 `Runtime(modules=, flags=)` 与 `modules / flags / flag_declarations`。
- 服务挂载、同步 start、逆序 async shutdown、失败回滚与继续清理。
- 三个并发 shutdown 只跑一次 Module 清理；首个等待者取消不取消共享清理；callback 抛普通异常或
  `CancelledError` 时其余 callback 与事件总线仍完成。
- 无 Module 时继续断言既有精确公开面；装 Module 时，公开名字的差集只能是
  `modules / flags / flag_declarations` 与声明的服务名，不得把守卫放宽成“包含基线”。
- 验证 Runtime 级 Flag override 不持有调用方 dict。
- 三个整数 Flag 在 Runtime 与 spawn 覆盖中收到负数都于副作用前拒绝。
- 变异：删掉服务名碰撞检查，测试必须变红。

### Task 4：per-Agent 装配与 D-55

**Files:** `src/syndica/kernel/agent_types.py`、`src/syndica/kernel/agent.py`、
`src/syndica/kernel/runtime.py`、`src/syndica/kernel/contracts/registry.py`、
`tests/kernel/test_modules.py`、`tests/contracts/test_memory_registry.py`

- `AgentInfo.flags`、`spawn(flags=...)`、父 / fork 继承、快照隔离。
- 每次 spawn 创建 per-Agent 插件与工具；固定顺序、重名拒绝、失败零部分写。
- `flags` 加入 Registry 的受保护字段，不允许普通 `update` 改写。发布的 Registry contract 覆盖：
  register 后改原 dict 不影响存储；改同一次 get/list 返回值不影响再次读取；与可写字段混合 update
  时拒绝且零部分写；`UPDATE_PROTECTED_FIELD_OWNERS['flags']` 精确指向 spawn。
- spawn 后再注册 local hook，数据与短路顺序仍是 `local -> module A -> module B -> runtime`；
  同时断言 `modified` 逐层传递与 `skip` 截断。
- 参数化第二插件工厂、register、第二工具工厂、坏 Mapping、工具身份、重名、角色自装工具失败，
  同时检查 Registry / broker / task / slot 为空，暴露出去的 Agent 与宿主 Mapping 未被污染。
- 失败后复用同一个暴露实例重新 spawn，证明 gate、tools 与 hooks 都回到工厂返回时的基线。
- 变异：让两个 Agent 共用同一份 flags dict 或同一个插件实例，测试必须变红。

### Task 5：SafetyModule 与两个安全 Flag

**Files:** `src/syndica/kernel/modules.py`、`src/syndica/kernel/runtime.py`、
`src/syndica/kernel/agent.py`、
`src/syndica/kernel/danger.py`、`src/syndica/kernel/tool_executor.py`、
`tests/kernel/test_runtime.py`、`tests/kernel/test_danger.py`、
`tests/kernel/test_tool_executor.py`

- clamp `wait_for` 与 `_wait_in_turn`；覆盖未超限、超限、0 禁用、abort / target 完成优先级。
- 替换现有“waiter.timeout 原样下传”与“在轮等待永不 timeout”的旧断言；两类等待都覆盖
  `answer / target > abort 或 preempt > timeout` 的同 tick 竞态，timeout 时目标仍在运行。
- `reject / ask / allow` 三态接到真实工具闸；非法值在 spawn 前拒绝。
- 两个 Agent 分别用 `reject` / `allow`，证明 ToolExecutor 配置不串线。
- 变异：只 clamp `wait_for`、不 clamp `_wait_in_turn`，集成用例必须变红。

### Task 6：RetryModule

**Files:** 新建 `src/syndica/kernel/retry.py`、`tests/kernel/test_retry_module.py`

- 原模型重试、顺序 fallback、假 sleep、取消透传、全失败保留初始异常。
- 前序 hook 已给替身时零调用；同一 request 因替身被清空而第二次失败时按每次触发重新计预算。
- 耗尽后允许后续失败 hook 恢复；取消分别落在 sleep、原模型重试与 fallback 时都立即传播；两个
  Agent 交错失败时计数不串；每个候选收到同一个最终 request 对象及原键序。
- 至少三项非自然排序的 fallback，显式断言调用顺序，挡住排序 / 反转变异。
- 变异：把 fallback 顺序排序或把“额外尝试”误写成“总尝试”，测试必须变红。

### Task 7：PrefixDriftModule

**Files:** 新建 `src/syndica/kernel/prefix_drift.py`、
`src/syndica/kernel/llm/base.py`、`src/syndica/kernel/llm/scripted.py`、
`src/syndica/backends/llm_openai/client.py`、`tests/kernel/test_prefix_drift.py`、
`tests/backends/test_llm_openai.py`

- 稳定 append 不报警；system、tools、历史中段分别漂移时准确定位。
- 工具至少三项且顺序非升非降；schema 映射显式断言键序敏感。
- success / failure / overflow 三个出口都取到最终请求，同一 request 不重复记录。
- `request_args` 与 PRE hook 新增的额外参数发生变化时也报警；第三方 LLM 的通用记录标
  `exact=False`。
- 只改旁路字段不报警；中文内容钉住 UTF-8 字节数与 `ceil(bytes / 4)`；关闭时 hash 与 token
  counter 都不调用；同一 request 多出口只处理一次、两个等值但不同对象的 request 都推进基线。
- Retry / Prefix 两种 Module 安装顺序下都只记录一次。
- 变异：排序 schema 键、忽略 tools、用当前摘要覆盖上次摘要后再比较，分别必须变红。

### Task 8：公开面、真实 Runtime E2E 与计划收口

**Files:** `src/syndica/kernel/__init__.py`、`tests/kernel/test_public_api.py`、
新建 `tests/integration/test_runtime_modules.py`、
`docs/decisions/2026-09-14-deferred-items.md`、
`docs/plans/2026-09-21-session-handoff.md`

- 导出 `FlagDeclaration`、`RuntimeModule`、`SafetyModule`、`RetryModule`、
  `PrefixDriftModule` 及漂移记录 / 服务所需公开类型；其余辅助函数明确扣留。
- 更新 Runtime 构造签名、spawn 签名、`AgentInfo` 字段顺序与 Runtime 实例公开面守卫。
- E2E：真实 Runtime 装三个 Module；首个 LLM 失败后重试成功并调用工具；子 Agent 继承 flags；
  动态 prompt 在第二轮产生一条漂移记录；shutdown 逆序清理且没有悬空任务。
- 登记表关闭 D-18、D-47（墙钟）、D-19–D-23、D-29、D-55；交接指向计划 6。

---

## 计划 5 收口门禁

```bash
uv run pytest \
  tests/kernel/test_errors.py \
  tests/kernel/test_agent_types.py \
  tests/kernel/test_modules.py \
  tests/kernel/test_runtime.py \
  tests/kernel/test_danger.py \
  tests/kernel/test_tool_executor.py \
  tests/kernel/test_retry_module.py \
  tests/kernel/test_prefix_drift.py \
  tests/kernel/test_public_api.py \
  tests/contracts/test_memory_registry.py \
  tests/integration/test_runtime_modules.py -q
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyright
python scripts/check_deps.py
```

收口判据：

1. Module 四类注入均有行为测试，构造 / spawn 失败不留部分状态。
2. 六个 Flag key 的声明、默认、类型、范围、文档文本均来自同一对象。
3. 每个 Agent 的 Flag 快照可存储、可继承、不可被外部 dict 事后改写。
4. 两种等待都能被同一宿主上限截断，且不误杀被等 Agent。
5. `untrusted_high_risk` 三态都经过真实 ToolExecutor 闸。
6. 重试与 fallback 只由 Module 的 `LLM_FAILURE` hook 实现。
7. 漂移检测能区分合法 append 与已有前缀改写，并给出可查询记录。
8. 全量测试、ruff、pyright、依赖方向全部通过；独立评审无未处理 Important 以上问题。
