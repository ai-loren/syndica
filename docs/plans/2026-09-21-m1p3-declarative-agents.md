# 里程碑 1 · 计划 3：声明式 Agent、Topology、四个内置角色

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「加一个角色」不再写 Python：宿主交一份十字段清单 + 一段提示词，内核生成工厂、装进 `Runtime.agents`、能被 `Delegate` 按名字派活。再让「换一个产品形态」变成改一份拓扑：系统里有哪些槽位、怎么寻址、何时起来。最后把 spec §4.8 的四个内置角色交出来（`Orchestrator` 类式，`Planner` / `Explorer` / `Worker` 声明式）。

**Architecture:** 计划 2 已经有一张「角色名 → 工厂」表（`Runtime(agents=...)` / `agent_names` / `agent_factory`）和一条懒启动钩子（`broker.on_missing_queue → ensure_active`）。本计划在这张表**之上**加三层，不替换它：

1. **清单**（`AgentManifest`）——十个公开字段 + 一条判别（有没有指向 Python 类的字段）。
2. **目录**（`AgentCatalog`）——加载源、同名覆盖可查询、名字列表转工具表、提示词插值、产出 `AgentFactory`。
3. **拓扑**（`Topology` / `AgentSlot`）—— cardinality × boot；`Runtime.assemble` 只 spawn eager 槽，lazy 走既有 `on_missing_queue`，manual 只显式占用。槽位**只增量增强**基础配置，不得替换。

`send` / `spawn` 仍然认 `agent_id`。拓扑的稳定名字是一层别名（`resolve`），不要改投递签名。

**Tech Stack:** 与计划 2 相同。kernel **不加 PyYAML**（见裁决 1）。

**关闭：** D-49、D-08。**不要动：** D-58（禁止加 `async def start()` / `runtime_start`）。

**先读：** [`docs/design/2026-09-03-architecture.md`](../design/2026-09-03-architecture.md) §4.6–4.8、[`docs/decisions/2026-09-08-m1-scope.md`](../decisions/2026-09-08-m1-scope.md) 计划 3 行、[`docs/decisions/2026-09-14-deferred-items.md`](../decisions/2026-09-14-deferred-items.md) D-49 / D-08 / D-58、[`docs/plans/2026-09-21-session-handoff.md`](./2026-09-21-session-handoff.md)、`src/syndica/kernel/runtime.py` 的 `validate_agent_factories` / `agent_factory` / `spawn`、`src/syndica/kernel/tool.py` 的 `validate_tool_names`、`tests/kernel/test_public_api.py` 的 `_RUNTIME_SURFACE` / `_RUNTIME_INIT`。

---

## Global Constraints

计划 2 的 Global Constraints **全部沿用**（含测试十三禁、CAS、快照 vs 活引用、错误三件套、`from __future__ import annotations`、基数 ≥ 3）。本计划额外：

- **清单字段属于公开 API**（spec §4.6），宁少勿多。十个声明式字段的名字、顺序、缺省值一经导出就是永久承诺；新字段只能追加。未知键当场 `ConfigError`，不要静默丢掉——丢掉等于「我改了为什么没生效」。
- **kernel 仍然零重依赖、不读环境变量。** 插值要用的 `{os}` / `{platform}` / `{shell}` 由宿主注入 `HostFacts`，内核不得 `os.environ` / `os.getcwd()`。
- **`AgentOptions` 新字段只能追加在末尾**；本计划能映射的用现有字段（`max_turns` ← `budget.turns`），不要为拓扑提前加 flags 字段。
- **`validate_tool_names` 必须在遍历 `.items()` 之前认 Mapping**（D-49）。清单里的 `tools: [Read, Grep]` 是名字列表，**不得**直接丢给 `AgentOptions.tools`。
- **从名字列表构造 tools dict 时三条身份轴必须逐字相同**（D-08）：dict 键 == `tool.name` == `schema['name']`。不一致就 `ConfigError`，不要「按 schema 名改键」悄悄修好——修好等于让一份写错的工具表在生产里换了一套寻址。
- **拓扑只增强、不替换。** slot 试图改 `system_prompt` / `name` / `model` / `llm` / 整表替换 `tools` → `ConfigError`，next_action 写「需要不同行为就定义不同角色」。
- **`assemble` 不是 `start()`。** 禁止新增 `Runtime.start`、禁止 `Junction.RUNTIME_START`、禁止在 `__init__` 里 spawn（构造是同步的，eager boot 必须可 await）。D-58 继续有效。
- **公开面每加一个名字，同步改** `kernel.__all__`、`_RUNTIME_SURFACE` / `_RUNTIME_INIT`（若动了 Runtime）、以及 `_WITHHELD`（若子模块导出但不承诺）。漏改即 Task 16 那套守卫变红——那是本计划的验收线之一，不是事后补。
- **文件工具（Read / Grep / Glob / Bash）是计划 4。** 本计划的清单可以**点名**它们；解析时工具表里没有这个名字 → `ConfigError`，不要内置空壳工具装样子。测试注入同名 stub。
- **SkillSource 不在本计划。** `skills` 字段存成名字元组，不加载、不执行。
- 变异验证：先清 `__pycache__`；新文件先 `git add`；sha256 基线覆盖本任务会改到的每个文件。

---

## 设计裁决

动手之前必须有答案。执行中途「这里该怎么写」一律回到本节，不要现场再发明一条。

### 1. YAML 是文档形态，内核收的是已经解析的 Mapping（Task 1）

spec 示例是 YAML，kernel 今天的依赖只有 pydantic。**不加 PyYAML。** 公开入口：

- `AgentCatalog.load_mapping(data, *, source=...)` —— 已解析的 Mapping。
- `AgentCatalog.load_json_file(path)` / `load_json_directory(path)` —— stdlib `json`。

YAML → Mapping 是计划 6 CLI / 宿主的事。测试用 dict 与 `.json` 文件，不要在 kernel 测试里 `yaml.safe_load`。

`system_prompt: prompt.md`：有 `origin` 目录且该相对路径存在则读文件内容；没有 origin（纯 Mapping）则字符串**就是**提示词。不要靠「有没有换行 / 是否以 `.md` 结尾」猜。

### 2. 类式 vs 声明式：有没有 `impl`（Task 1）

spec：「清单里有没有指向 Python 类的字段。有则为类式，无则在运行时生成一个声明式 Agent 类。」

十个声明式字段**不含**这个判别字段。公开键名：`impl`。

| `impl` 取值 | 含义 |
|---|---|
| 缺省 / `None` | 声明式 |
| `type`（`BaseAgent` 子类） | 类式，进程内 |
| `str`（`pkg.mod:Class`） | 类式，按 import 路径加载 |

`class` 不做键：Python 数据类里它是关键字，YAML 里能写、代码里写不出标注。未知的第十一键（含误写的 `class:`）走「未知键」那条 `ConfigError`。

### 3. 十个字段的内核形状（Task 1）

| 字段 | 内核类型 | 缺省 | 映射 |
|---|---|---|---|
| `name` | 非空白 `str` | 必填 | `AgentOptions.name`；覆盖键 |
| `description` | 非空白 `str` | 必填 | 只存清单；编排者派活依据。内置角色的 description 必须写「什么时候用我」，测试断言不含「我是一个」这种开场 |
| `system_prompt` | `str` | `''` | 插值后 → `AgentOptions.system_prompt` |
| `tools` | `tuple[str, ...]` | `()` | 名字列表；**解析期**经工具目录变成 `dict[str, Tool]` |
| `subagents` | `tuple[str, ...]` | `()` | 存清单；工厂表必须能 `agent_factory(名字)`（缺名 `ConfigError`，在 catalog.freeze / assemble 时查，不要拖到第一次 Delegate） |
| `model` | `str` | `'default'` | 经 `models: Mapping[str, LLM]` 解析；缺名 `ConfigError`。测试把 `ScriptedLLM` 登记为 `default` |
| `extends` | `str \| None` | `None` | 必须指向**已经登记的类式**条目；声明式 extend 声明式 → `ConfigError`。环 → `ConfigError`。继承的是工具名并集 + 被继承者的行为（类），提示词仍用本清单自己的 |
| `skills` | `tuple[str, ...]` | `()` | 不透明名字，本计划不加载 |
| `budget` | `Mapping` 且**只允许**键 `turns` | `None`（则 `AgentOptions.max_turns` 保持 50） | `{turns: 40}` → `max_turns=40`；`turns: null` → `max_turns=None`（不限）。其它键 `ConfigError` |
| `artifact_kinds` | `tuple[str, ...]` | `()` | 写入 `AgentInfo.metadata['artifact_kinds']` 的**副本**；不实现产物系统 |

字段顺序计入公开 API，与上表一致。容器缺省用 `tuple` 而不是 `list`：清单是快照。

### 4. 工具目录与 D-49 / D-08（Task 2）

`tools: [Read, Grep, Glob]` 是 spec 明文。两条收口：

1. **D-49：** `validate_tool_names` 入口 `isinstance(..., Mapping)`，失败用与 `_not_a_role_table` 同型的三件套：期望「名字 → Tool 的映射」，收到 list 时 next_action 点名「声明式清单的 `tools` 是名字列表，先经 `AgentCatalog` / 工具目录解析，不要直接塞进 `AgentOptions.tools`」。
2. **D-08：** catalog 把名字列表变成 dict 时，键必须等于 `tool.name`，并立刻 `validate_tool_names`。工具目录按**注册名**取值；注册名必须已经与 `tool.name` / `schema['name']` 一致，否则登记进目录的那一步就报，不要等到装 Agent。

工具目录是 `Mapping[str, Tool]`（或极窄的 Protocol：`get(name) -> Tool | None`）。内核**不**在本计划里放 Read 实现。内置 Worker/Explorer 的清单点名 `Read` / `Grep` / `Glob`（Explorer）与执行类名字（Worker，计划 4 才会真有）。**freeze 时缺工具 → ConfigError**，所以内置角色的测试必须注入 stub；生产路径在计划 4 把真工具登记进同一张目录之前，freeze 内置 Worker 本来就该响亮失败——这是约束，不是缺陷。

编排工具（`orchestration_tools`）**不是**清单里的名字：它们仍由 `Runtime(orchestration=True)` 在 spawn 后 `install_runtime_tools` 并上。Orchestrator 类自己动态装，不靠清单点名 Delegate。

### 5. 覆盖必须可查询（Task 3）

后载入的同名条目覆盖先载入的。被覆盖的那一份留下 `OverrideRecord(name, displaced_source, displaced_at_index, by_source)`。`catalog.overrides(name) -> tuple[OverrideRecord, ...]`（从旧到新）；从未被覆盖则 `()`。`catalog.source_of(name)` 返回**当前**生效条目的 source。

Source 是快照数据类：`kind: Literal['mapping','json_file','json_directory','builtin','class']` + `location: str`（mapping 用调用方传入的标签，缺省 `'<mapping>'`；文件用绝对路径）。不要交活的 Path。

加载源可注册：`catalog.add_source(loader)` 太宽。本计划只提供 mapping / json file / json directory / `register_class` / `register_builtin`。第三个加载器（YAML、zip）归计划 6。

覆盖**不**删历史：`overrides` 永远能把链讲完。同一次 `load_*` 内部撞名（一个目录两个 `name: Worker`）→ `ConfigError`，这不是覆盖，是一份源自己打架。

### 6. 插值白名单（Task 4）

允许且仅允许：`{os}` `{platform}` `{shell}`。`{cwd}` **明确禁止**，即便 HostFacts 以后加了 cwd 也不许进 System Prompt（spec §5.2：工作目录是动态环境事实，进历史）。未知 `{name}`、以及 `{os:foo}` 这类带格式的，一律 `ConfigError`。字面量 `{` 用 `{{` / `}}`（与 str.format 相同），测试钉住。

`HostFacts(os: str, platform: str, shell: str)` 三个字段都必填、都是 str。内核构造 catalog 时收下它；缺 HostFacts 则不允许提示词里出现任何 `{...}`（空提示词除外）。

插值发生在 **freeze → 工厂闭合** 时一次，不在每轮。同一会话内这三个量不变，是这条纪律能成立的前提；内核信任宿主注入的值，不复核。

### 7. Topology 与 assemble（Task 5–6）

```python
@dataclass(frozen=True)
class AgentSlot:
    name: str
    agent: str                          # catalog 角色名
    is_root: bool = False
    cardinality: Literal['singleton','keyed','on-demand'] = 'on-demand'
    key_field: str | None = None        # keyed 必填；其它 cardinality 必须是 None
    boot: Literal['eager','lazy','manual'] = 'lazy'
    exit_mode: ExitMode = ExitMode.ZOMBIE
    extra_tools: tuple[str, ...] = ()   # 只并上，不替换
    extra_plugins: tuple[Plugin, ...] = ()  # 本计划若 Plugin 实例难冻，改为名字元组 + catalog 侧解析；不要让 slot 替换 Runtime(plugins=)
    budget: Mapping[str, Any] | None = None  # 只允许 turns，且只收紧/设定实例 max_turns，不改角色清单
    metadata: dict[str, Any] = ...      # 深拷后写入 AgentInfo.metadata；不得含保留键冲突时静默覆盖——冲突 ConfigError
```

**不要**给 slot 一个自由 `options: dict`。自由 dict 就是「替换基础配置」的入口。增强面收成显式字段：`extra_tools` / `budget` / `metadata`。想换 system prompt → 新角色。

`Topology(slots: tuple[AgentSlot, ...])`：`is_root=True` 至多一个；0 个合法（纯工人拓扑）。slot 名唯一。`agent` 必须已在 catalog。

寻址（不改 `send(agent_id)`）：

| cardinality | 稳定键 | `resolve` |
|---|---|---|
| singleton | `slot.name` | 0 或 1 个 `agent_id` |
| keyed | `f"{slot.name}:{key}"`，`key` 来自 `key_field` | 每个 key 0 或 1 个 |
| on-demand | 无 | `resolve` 答 None；每次 `occupy` / Delegate 新实例 |

`Runtime.assemble(topology, catalog) -> Assembled`（名字可再收）：校验拓扑 → 把 catalog.factories() **合并进**已有 `self._agents`（同名：catalog 覆盖 Runtime 构造时那张表，并记 override？——**不**。Runtime 构造表没有 source。冲突策略：assemble 时 catalog 有的名字覆盖 `self._agents`，并打一条 INFO，record.args 含名字与「catalog 覆盖了 Runtime(agents=) 里同名工厂」。不要第二套 OverrideRecord）。然后 spawn 每个 `boot='eager'` 的 singleton（keyed eager 需要 key 集合——**本计划禁止 keyed+eager**：没有 key 集合可枚举。`keyed`+`eager` → ConfigError，next_action 改 lazy 或改 singleton）。on-demand+eager 同样禁止。

lazy singleton：第一次 `resolve(slot.name, spawn=True)` 或向该别名 `send` 时 spawn。实现上：别名表未命中且 slot.boot==lazy → spawn 并登记。`find` **仍然绝不激活**（计划 2 承诺）；`find` 可以按别名查已存在的 id，没有就 None。激活只走 `send` / `ensure_active` / `occupy`。

manual：`resolve(..., spawn=True)` 也拒绝；只有 `occupy(slot_name, key=None)` 显式 spawn。

`assemble` 可 await（eager spawn）。它**不是**生命周期 start：没 assemble 的 Runtime 与今天完全一样。第二次 assemble → `ContractViolation`（拓扑装过一次；要换形态就新 Runtime）。

### 8. 四个内置（Task 7）

| 角色 | 形态 | 工具名（清单） | 备注 |
|---|---|---|---|
| `Orchestrator` | 类式，`impl` 指向 `syndica.kernel.agents.Orchestrator` | 不靠清单；构造后 `install_runtime_tools(orchestration_tools(...))` | 动态装配是它成为类式的理由 |
| `Planner` | 声明式 | 只读探测名：本计划 stub `Read`/`Grep`/`Glob` 即可；联网检索**不进 kernel** | description 写「规划范围 / 决策 / 依赖 / 成功判据时用我」 |
| `Explorer` | 声明式 | `Read`,`Grep`,`Glob` | description 写「定位文件、符号、引用时用我，不要用我改文件」 |
| `Worker` | 声明式 | 点名计划 4 才会有的执行工具（`Read` 加上未来的写/命令）。freeze 无 stub 则失败 | description 写「改文件、跑命令、验证结果时用我」 |

内置 JSON 放 `src/syndica/kernel/agents/*.json` + `orchestrator.py`。`register_builtins(catalog)` 一次装四个；测试用的 catalog 可以只装子集。

Orchestrator 的 `subagents` 默认 `(Planner, Explorer, Worker)`，缺一 freeze 失败——所以 register_builtins 的顺序是先三个声明式再 Orchestrator，或 Orchestrator 的 subagents 在第一次 freeze 时解析。

### 9. 公开面加什么、扣留什么（Task 8）

**进 `kernel.__all__`（宿主写不出标注就装不了）：**

`AgentManifest`, `HostFacts`, `AgentCatalog`, `AgentSlot`, `Topology`, `OverrideRecord`, `ManifestSource`, `Orchestrator`

**进 Runtime 公开面：** `assemble`, `resolve`, `occupy`（三个都是拓扑的入口；少一个宿主就得摸私有别名表）。

**不进构造器：** 不要 `Runtime(catalog=)` / `Runtime(topology=)`——会涨 `_RUNTIME_INIT`，而今天没 catalog 的宿主仍然是主路径。catalog 经 `assemble` 传入。

**扣留：** `interpolate_prompt`、`validate_manifest`、json loader 细节、别名表类型、`register_builtins` 若只被内置包自己用——若测试与宿主都要装四个内置，则 `register_builtins` **进公开面**（「装内置角色」是已经公开的能力）。倾向：**导出 `register_builtins`**。

工厂闭包捕获的 Tool dict 必须是 catalog freeze 时的**副本**，此后改宿主工具目录不影响已 freeze 的角色（与 `Runtime._agents` 快照同型）。热插拔仍走 `install_runtime_tools`。

---

## 任务拆解

每个任务：实现 → 受影响测试绿 → 自我变异（清 pyc、新文件 git add）→ 独立评审 → 修复 → commit。**不要 push。** 受影响包测试用 `uv run pytest tests/kernel/test_<本任务>.py tests/kernel/test_public_api.py -q`，再按改动加 `test_runtime.py` / `test_tool.py`。不要全仓 `make test`。

---

### Task 1 — `AgentManifest` 与 Mapping 加载（不含工具解析）

**Files:**
- Create: `src/syndica/kernel/manifest.py`
- Create: `tests/kernel/test_manifest.py`
- Modify: `src/syndica/kernel/__init__.py`（先**不要**导出，等 Task 8 收口；本任务模块自己 `__all__`）

**内容：**
- frozen dataclass `AgentManifest`，十字段顺序与裁决 3 一致；另：`impl: type[BaseAgent] | str | None = None`、`source: ManifestSource`。
- `parse_mapping(data, *, source) -> AgentManifest`：未知键、缺 name/description、空白字符串、`tools` 不是「字符串序列」、`budget` 多键、`impl` 类型不对 → 全部 `ConfigError` 三件套。
- `tools` / `subagents` / `skills` / `artifact_kinds` 接受 list 或 tuple，存成 tuple；元素非 str 或空白 → 报。
- 基数 ≥ 3 的未知键测试：一次塞三个非法键，消息列出**全部**非法键（漏报一个即变红），顺序与输入无关（用集合）。

**必写用例名（就是断言）：**
- `test_the_manifest_field_order_is_exactly_this`
- `test_a_mapping_with_unknown_keys_is_rejected_naming_every_unknown_key`
- `test_blank_name_or_description_is_rejected`
- `test_tools_must_be_a_sequence_of_names_not_a_tool_mapping`
- `test_budget_rejects_keys_other_than_turns`
- `test_impl_absent_means_declarative`
- `test_impl_type_must_be_a_base_agent_subclass`
- `test_system_prompt_file_is_read_only_when_source_has_a_directory`
- `test_system_prompt_string_is_literal_without_an_origin_directory`

**变异至少：** 删未知键检查；只报第一个未知键；`tools` 为 dict 时当 Mapping 放行；`budget: {turns: 40, tokens: 1}` 放行。

---

### Task 2 — `validate_tool_names` 认 Mapping + 名字列表→工具表（关 D-49、D-08）

**Files:**
- Modify: `src/syndica/kernel/tool.py`
- Modify: `tests/kernel/test_tool.py`（或现有 validate 用例所在文件）
- Create: `src/syndica/kernel/catalog.py` 的工具解析函数可先放 catalog，本任务至少把 `resolve_tool_names(names, tools: Mapping[str, Tool]) -> dict[str, Tool]` 落地

**内容：**
- `validate_tool_names`：非 Mapping → ConfigError（对齐 `_not_a_role_table`）。然后现有循环。
- `resolve_tool_names`：缺名 → ConfigError，列出缺的每一个名字（基数 3：目录有 A、C、E，清单要 B、C、D，消息含 B 与 D 不含 C）。产出 dict 的键序与**清单顺序**一致（进请求前缀）。立刻 `validate_tool_names`。
- 目录里登记的 Tool 若三条轴不一致，**登记函数**就报（D-08 收口在注册层）。提供 `register_tool(table, tool)` 或 catalog 方法，禁止 `table[wrong]=tool` 成为正式入口——公开入口只有这一条。

**关闭：** 把 D-49、D-08 移到 deferred-items「已关闭」，证据写本任务用例名。

**必写用例：**
- `test_validate_tool_names_rejects_a_list_with_the_three_part_error`
- `test_resolve_tool_names_preserves_manifest_order`
- `test_resolve_tool_names_names_every_missing_tool`
- `test_register_tool_rejects_disagreement_among_the_three_identity_axes`
- `test_resolved_dict_shares_no_mutation_with_the_catalog`（改返回 dict 不影响目录；改目录不影响已 resolve 的 dict——工厂闭合用的是副本）

---

### Task 3 — `AgentCatalog`：加载、覆盖可查询、产出工厂雏形

**Files:**
- Modify: `src/syndica/kernel/catalog.py`
- Create: `tests/kernel/test_catalog.py`

**内容：**
- `load_mapping` / `load_json_file` / `load_json_directory`（只 `*.json`，忽略其它后缀；子目录**不**递归——递归是隐藏加载面）。
- `register_class(name, cls, *, source=...)`.
- 同名覆盖记 `OverrideRecord`；`overrides(name)` / `source_of(name)` / `names() -> tuple[str, ...]`（登记顺序：第一次见到该名的顺序，覆盖不把名字挪到末尾—— Delegate 错误消息里的名单不能因为覆盖而洗牌）。
- 同一目录两个文件 `name` 相同 → ConfigError。
- 本任务工厂可以先交「未解析 tools 的占位」，但 `freeze` 的形状要在 Task 4 完成。至少 `get(name) -> AgentManifest`。

**必写用例：**
- `test_later_source_overrides_the_same_name_and_the_displaced_record_is_queryable`
- `test_override_chain_keeps_every_displaced_source_oldest_first`（三次覆盖，长度 2 的 displaced 链）
- `test_names_keep_first_seen_order_when_overridden`
- `test_two_json_files_in_one_directory_with_the_same_name_are_rejected`
- `test_load_json_directory_ignores_non_json_and_does_not_recurse`

---

### Task 4 — 插值 + freeze 成 `AgentFactory`

**Files:**
- Modify: `catalog.py` / `manifest.py`
- Create: `tests/kernel/test_prompt_interpolation.py`
- Modify: `test_catalog.py`

**内容：**
- `HostFacts(os, platform, shell)`。
- 插值白名单；`{cwd}` 专用错误（next_action：把工作目录写进历史块，不要写进 System Prompt）。
- `catalog.freeze(tools, models, host) -> Mapping[str, AgentFactory]`：解析 tools/model/extends/subagents；闭包捕获副本；声明式工厂签名必须是 `(hooks, events) -> BaseAgent`，交出的 `AgentOptions.system_prompt` 已插值、`tools` 已是 dict、`max_turns` 已从 budget 映射。
- extends 环、extends 指向声明式、extends 未知名 → ConfigError。
- freeze 后的工厂表交给 `validate_agent_factories`。
- 类式：`impl` 是 type 则 `lambda hooks, events: cls(options, hooks=hooks, events=events)`，options 由清单能映射的字段填（类式清单仍可以带 tools 名字并集）。`impl` 是 str 则 importlib 加载，失败三件套。

**必写用例：**
- `test_os_platform_shell_are_interpolated_once_at_freeze`
- `test_cwd_in_the_prompt_is_rejected_even_if_the_host_knows_a_cwd`
- `test_unknown_placeholder_is_rejected`
- `test_double_braces_survive_as_a_literal_brace`
- `test_freeze_without_host_rejects_a_prompt_that_uses_placeholders`
- `test_missing_model_name_is_rejected`
- `test_extends_a_declarative_agent_is_rejected`
- `test_extends_cycle_is_rejected`
- `test_frozen_factory_does_not_see_later_catalog_tool_mutations`
- `test_declarative_factory_signature_binds_hooks_and_events`

---

### Task 5 — `AgentSlot` / `Topology` 校验（还不接 Runtime）

**Files:**
- Create: `src/syndica/kernel/topology.py`
- Create: `tests/kernel/test_topology.py`

**内容：** 裁决 7 的字段与禁则（keyed+eager、on-demand+eager、keyed 缺 key_field、非 keyed 却有 key_field、两个 root、重名 slot、未知 agent 名——agent 名检查需要 catalog，放 `Topology.validate(catalog)`）。

slot 试图带 `system_prompt` 之类——类型上就没有这个字段。若有人用 `make_dataclass` 加字段，公开字段顺序守卫会红。

**必写用例：** 每个禁则一条能单独变红的；`test_the_slot_field_order_is_exactly_this`；`test_at_most_one_root`；`test_keyed_eager_is_rejected`。

---

### Task 6 — `Runtime.assemble` / `resolve` / `occupy`

**Files:**
- Modify: `src/syndica/kernel/runtime.py`
- Modify: `tests/kernel/test_public_api.py`（`_RUNTIME_SURFACE` 加三个名字）
- Create: `tests/kernel/test_assemble.py`
- Modify: `tests/kernel/test_runtime.py` 仅当 `_on_missing_queue` 需要认别名

**内容：**
- 内部别名表：稳定键 → `agent_id`。快照语义：`resolve` 交 `str | None`，不是活引用。
- `assemble`：合并工厂、校验拓扑、spawn eager singleton、登记别名。第二次 assemble 拒绝。
- `resolve(name, *, key=None, spawn=False)`：find 语义当 `spawn=False`（不激活、不创建）。`spawn=True` 时按 boot 规则 occupy。
- `occupy`：manual/lazy 创建；singleton 第二次 occupy 交回**同一个** agent_id（幂等），不要再 spawn。keyed 同 key 幂等；on-demand **每次新实例**，不进别名表。
- lazy singleton + `send`：若 `agent_id` 参数其实是别名——**不要重载 send 的第一个参数搞成 magically both**。提供 `resolve` 之后宿主 `send(id, ...)`。内核自己的编排工具继续用真 id。可加 `Runtime.send_to_slot(slot, content, *, key=None)` 若否则宿主必踩坑；若加，必须进公开面与 `_RUNTIME_SURFACE`。**建议加 `send_to_slot`**：少它则 lazy boot 没有除 occupy 以外的「发消息即创建」入口，而 spec 把 lazy 定义成「首次寻址到才创建」。`send_to_slot` 内部 resolve(spawn=按 boot) 再 send。`find` 不走这条。
- extra_tools：occupy 时 resolve 并集，经 `install_runtime_tools` 并上（并上 = 原 tools + extra，键冲突：extra 与基础同名 → ConfigError「增强不得替换」）。
- metadata：深拷进 spawn(metadata=)；与 `artifact_kinds` 合并时键冲突 ConfigError。

**必写用例：**
- `test_assemble_spawns_eager_singleton_and_not_lazy_or_manual`
- `test_second_assemble_is_a_contract_violation`
- `test_lazy_singleton_appears_only_after_send_to_slot`
- `test_find_does_not_spawn_a_lazy_slot`
- `test_manual_slot_refuses_resolve_with_spawn`
- `test_occupy_singleton_is_idempotent`
- `test_keyed_same_key_is_idempotent_and_different_keys_are_not`
- `test_on_demand_occupy_always_creates`
- `test_extra_tools_union_rejects_replacing_an_existing_name`
- `test_assemble_does_not_add_runtime_start_or_a_start_method`（AST 或 `_RUNTIME_SURFACE` 不含 `start`）

---

### Task 7 — 四个内置角色

**Files:**
- Create: `src/syndica/kernel/agents/orchestrator.py`
- Create: `src/syndica/kernel/agents/planner.json`、`explorer.json`、`worker.json`
- Create: `src/syndica/kernel/agents/__init__.py`（`register_builtins`）
- Create: `tests/kernel/test_builtin_agents.py`
- 内置 JSON 的 description / tools 按裁决 8

**内容：**
- Orchestrator 子类 `BaseAgent`，构造后装编排工具；system prompt 写清何时 Delegate 给 Planner/Explorer/Worker（描述从 catalog.get(name).description 拼，这样覆盖内置 Worker 时编排者看到的是新 description）。
- 声明式三个 JSON 合法且 `parse_mapping` 过。Worker/Explorer 点名的工具在 **测试** 里用 stub 登记；本任务不实现文件工具。
- `register_builtins(catalog)` 可重复调用：第二次是覆盖并留下 OverrideRecord（用户后载入的才该覆盖内置；内置自己装两次视为同名覆盖，source=builtin，要可查询）。

**必写用例：**
- `test_builtin_names_are_orchestrator_planner_explorer_worker`
- `test_orchestrator_is_class_based_and_the_others_are_declarative`
- `test_planner_explorer_worker_descriptions_say_when_to_use_them`
- `test_user_mapping_overrides_worker_and_overrides_is_queryable`
- `test_orchestrator_factory_installs_orchestration_tools_when_runtime_asks`（或 Orchestrator 自己装；不要两套。**裁决补：Orchestrator 自己在工厂里装编排工具，不依赖 `Runtime(orchestration=True)`**——否则没开 orchestration 的拓扑根派不了活。`Runtime(orchestration=True)` 仍给**其它**角色装。测试钉住：`orchestration=False` 时 Orchestrator 仍有 Delegate，Worker 没有。）

---

### Task 8 — 公开面收口 + 端到端一条拓扑

**Files:**
- Modify: `kernel/__init__.py`、`test_public_api.py`
- Create: `tests/integration/test_topology_lifecycle.py`

**内容：**
- 按裁决 9 导出。`_RUNTIME_INIT` **不变**（不给构造器加参）。
- 端到端（只 import `syndica.kernel`）：HostFacts + stub 工具 + ScriptedLLM as default → register_builtins → 用户覆盖 Planner description → freeze → Runtime(agents=factories) → Topology(root Orchestrator eager singleton + Worker lazy singleton + Explorer on-demand) → assemble → send_to_slot Orchestrator → ScriptedLLM 调 Delegate(Worker) → Worker 因 lazy 被创建 → 断言 overrides(Planner) 非空、find 不创建 Explorer、occupy Explorer 才有 id。
- `test_the_runtime_surface_is_exactly_this` 两档 orchestration 仍相等。

**必写用例：** 上一段即场景；另 `test_promised_names_include_catalog_topology_and_orchestrator`（差集守卫，期望值写在测试里，不要从 `__all__` 推导）。

---

## 自我审查（写计划时，执行前）

1. **会不会加 `start()`？** 不会。eager 走 `assemble`。D-58 原句保留。
2. **会不会把 YAML 塞进 kernel？** 不会。json + Mapping。
3. **D-49 会不会只在 catalog 修、漏掉 `AgentOptions(tools=[...])` 直达？** 不会。`validate_tool_names` 本身收紧，所有入口都过它。
4. **D-08 会不会「自动改键」变成静默换寻址？** 不会。登记时拒绝。
5. **`send(agent_id)` 会不会变成又收 id 又收别名？** 不会。别名走 `send_to_slot` / `resolve`。避免布尔参数爆炸。
6. **Worker 在计划 4 之前 freeze 会失败**——接受；测试注入 stub。
7. **`extra_plugins` 冻结 Plugin 实例** 可能与 frozen dataclass 打架。执行时若 Plugin 不可 hash，把 `extra_plugins` 从 Task 5 去掉，元数据增强只留 extra_tools / budget / metadata。**执行前默认：Task 5 不包含 extra_plugins 字段**，插件仍只在 `Runtime(plugins=)`。写进这里以免执行时膨胀公开面。
8. **公开名字宁可少：** `send_to_slot` 是 lazy 的最小入口，保留。`interpolate_prompt` 扣留。

---

## 执行顺序与 commit 粒度

建议 8 个 commit，message 写**为什么**（D-49 是裸 AttributeError；assemble 不是 start；覆盖不可查询就无法排障）。每任务结束把 deferred-items 该关的关上。计划 3 全部收口后再写计划 4，不要穿插。
