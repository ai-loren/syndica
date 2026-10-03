# 里程碑 4：扩展点完备

> **执行者须知：本文件是需求，不是参考。** 「公开契约」一节里的 Module 清单字段、能力词表、扩展切点的归属规则、
> `.syndica.yaml` 的新键、CLI 命令与输出、入口点组名、trace schema 的新属性、清单 `model` 的写法一旦发布就是兼容承诺；
> 执行中不得为了让测试变绿临时改名或缩小断言范围。

**状态：执行中。** 用户 2026-10-03 授权自主完成：方案由 Agent 评审、修到无阻塞问题后开工，不等用户审方案
（见「决策日志」）。验证按路线图 2026-10-03 定下的阶段节奏；CI 推迟到 M1–M11 都做完之后。

**Goal:** 第三方贡献一个能力包只写一个 Module、不改内核：它声明自己是谁（名字、版本、要什么能力、依赖谁、拥有
哪些扩展切点），宿主显式启用后它的工具、插件、服务、Flag 与扩展切点按稳定的次序装上，装到一半失败什么都不留下；
它触发自己的扩展切点，别人的 hook 收得到，别的扩展冒用不了；Flag 的说明只有一份声明，CLI 与参考文档都从它生成
（README 链过去）；
trace 记下每个 Agent 从哪个 Module 拿到了什么，`flags list`、Agent 看得到的工具、trace 三者一致。

**Architecture:** `RuntimeModule` 加上清单字段（追加的关键字参数）；`Runtime` 仍是唯一装配者，Runtime 级与
Agent 级各是一个事务。扩展切点用 `ExtensionJunction` 句柄：Module 在清单里声明自己拥有的切点（Module 名是保留的
owner），触发必须交回声明时的那个句柄，挂在 Runtime 上的中心不再能从公开入口触发内核切点。CLI 从 `.syndica.yaml` 的 `modules:` 显式启用
Module（内置的按名字，第三方的经 `syndica.modules` 入口点发现，发现只读元数据、不 import），项目配置只能收紧安全
设置，首次使用要经人确认信任（摘要盖住实际加载的东西，确认之前不 import 任何第三方代码）。Flag 参考文档与
`flags list/show` 由同一个生成器从已装 Module 的声明产出。trace 产出保留了一个里程碑的 `module` span。

**Tech Stack:** 不新增运行依赖（入口点用标准库 `importlib.metadata`）。

**依据：** spec §4.7（拓扑）、§5.3–§5.5（三层装配、切点、Runtime Module）、§5.6（按角色的处置）、§12.4（第三方能力包）、
§15（M4 完成判据）；路线图「M4 扩展点完备」与「遗留项归属」；M2 计划的三条 M4 遗留；M3 收口审计的「拓扑 run 不能
回放」与保留的 `module` span；`AGENTS.md`。

---

## 现状校准（2026-10-03 调研）

三路调研对照代码核实，下面这些事实决定了 M4 的做法：

1. **Module 只有名字与 Flag。** `RuntimeModule`（`kernel/modules.py`）七个字段：`name`、`flags`、`agent_plugins`、
   `services`、`tool_factories`、`on_start`、`on_shutdown`；没有版本、能力、依赖、安全说明，Module 也声明不了
   扩展切点。名字只要求非空，`'a'` 与 `'a '` 都能装。
2. **装配已有回滚，但覆盖面不全。** Runtime 级：服务与 `on_start` 失败时按快照还原 hook 中心、摘掉服务再上抛
   （接 `BaseException`）；已经 start 过的 Module 不补偿（`on_shutdown` 是异步的，同步构造里调不了）。Agent 级：
   spawn 的失败整个还原 Agent（含 Module 层的 hook 中心与工具表）。`BaseException` 只在 `install_plugins` 上有用例。
   Module 插件往 Agent 层挂 `RUNTIME_SHUTDOWN` / `AGENT_SPAWN` / `AGENT_DESPAWN` 会被收下、永不触发（只有
   `register_hook` 拒绝它们）。
3. **扩展切点只有声明、分类、触发。** 没有查询；owner 段不绑注册者身份（`center.py` 写着「日后绑」）；谁拿到中心
   谁就能触发任何切点；Agent 局部 hook 只在 Agent 私有的中心上触发才收得到，没有公开入口。生产代码里没有任何扩展
   切点（只有测试用）。`test_the_trigger_matcher_sees_every_trigger_call` 要求 `src/syndica` 里每个 `.trigger(` 的
   第一个参数都是 `Junction.X`。
4. **工具的实际次序与路线图相反。** 角色清单的工具 → 槽的 `extra_tools`（在工厂里就装上）→ 编排工具 → Module
   工具 → 类式角色自己的工具。槽没有插件（M1 的决定：插件实例难冻结、会撑大公开面）。
5. **CLI 的 Flag 是手写的另一份。** `flags list/show` 读写死的 `_modules()`（安全与人机交互两个），自己再校验一遍；
   README 里的 Flag 是手写的。`retry` 与 `prefix-drift` 两个内置 Module 的 Flag 在 CLI 里够不着。`.syndica.yaml`
   没有 `modules` 与 `flags`；`run --flag` 是唯一入口。
6. **没有发现机制。** CLI（`host.py` 的 `_agent_modules`）与回放（`replay.py`）各写死一张 Module 表；`pyproject.toml`
   没有入口点。
7. **第三方工具的声明没人检查。** Module 的工具只过 `validate_tool_names`；没声明 `__tool_command_args__` 的工具在
   调用时按「每个参数都可能被执行」判（B-09 / B-10）。`AskUser` / `AskUserQuestion` 继承 `_HumanTool` 的 `()`，自己
   没声明。
8. **清单 `model` 的显式名字只走 OpenRouter 或 `OPENAI_*`。** 组合根（`__main__._configured_model`）里 `default`
   进轮换（Gemini 在前），显式名字走 OpenRouter（配了的话）否则 `OPENAI_*`；Gemini 只服务 `default`。
9. **危险处置只有一份。** `AgentCatalog.freeze` / `Runtime.assemble` 收一对 `danger_judge` / `danger_policy`，交给
   每个角色；spec §5.6 写明「按角色区分的策略归里程碑 4」。
10. **项目配置首次使用不确认。** `--config` 缺省是工作区根的 `.syndica.yaml`，不存在就用内置默认；M2 把「首次使用
    时的信任确认」归给 M4（与配置发现一起做）。
11. **`system_prompt` 写错的路径静默变成提示词正文**（M2 遗留）：清单刻意不靠后缀猜路径，相对路径真是一个文件才读；
    本仓自己的 `.syndica.yaml` 五个角色都是这种写法。
12. **trace 里没有 Module 的痕迹。** `SpanKind.MODULE` 保留未产出（M3）；属性是闭集，加属性要把 `TRACE_SCHEMA`
    加一。回放文档的宿主档案只记 Module 名单；回放按名字重建三个（安全、人机交互、重试）。
13. **拓扑 run 不写回放文档**（M3 遗留）：宿主在 `_describe_run` 里直接拒绝；场景格式本来就有 `topology` 段与
    `entry.mode: slot`（M1），但回放的槽入口绕过了 `assemble`：直接 `spawn` 角色的冻结工厂，不加槽的工具与预算、不管
    lazy / manual，稳定键是 `('host', 'spawn', n)` 而原始 run 是 `('slot', 槽名, 键, n)`。`assemble` 只收 `AgentCatalog`
    并重新冻结（冻结后的提示词已经插值过，再插一遍会出错）；冻结角色的 `max_turns` 要求 ≥ 1，清单与槽允许 0。
14. **按角色的处置要在 Agent 构造之前定下**：执行器在 `BaseAgent.__init__` 里取走 `danger_judge` / `danger_policy`，
    事后换 `agent.options` 到不了执行器。HIGH 且不可信的调用不问处置表（按 `untrusted_high_risk`），开发期守卫对
    每个角色都装，因此按角色的策略只改变可信溯源下的判定。
15. **`run --flag` 在读配置之前校验**，对的是写死的 `_modules()`；`chat` 没有 `--flag`。

## 路线图条目对照

| 条目 | 任务 |
|---|---|
| Module 清单：id、version、flags、capabilities、dependencies、security notes | T1 |
| Module 装配器：拓扑级、Runtime 级、Agent 级的合并顺序 | T2 |
| 装配事务：任一第三方 register 失败回滚 hook / tool / service；`BaseException` 也回滚 | T3 |
| extension junction API：声明、分类、触发、查询；owner 绑注册者 | T4 |
| B-09 / B-10：Module 的工具必须声明 `__tool_command_args__` | T5 |
| D-42：是否拆分可观察与可拦截的回调类型（M12 冻结前定） | T4（裁决 D9） |
| 清单 `model` 的「平台 + 型号」写法 | T6 |
| `system_prompt` 路径写错静默变成正文（M2 遗留） | T7 |
| 按角色区分的危险处置（spec §5.6，M2 遗留） | T8 |
| module discovery 但不自动启用；`.syndica.yaml` 显式启用 | T9 |
| Flag 文档生成器，供 CLI、README 共用；快照测试 | T10 |
| 项目配置首次使用的信任确认（M2 遗留） | T11 |
| trace 中的 module 事件；`module` span（M3 保留） | T12 |
| 拓扑 run 回放（M3 遗留） | T13 |
| Module 的 run 能回放（宿主档案记清单，回放按入口点重建） | T14 |
| 示例第三方 module 与四个 E2E 场景；「三者一致」 | T15 |
| 真实模型 E2E、文档、收口审计 | T16 |

## 已确认边界

### 做

- `RuntimeModule` 加清单字段与校验；依赖按给定次序检查；Runtime 级、Agent 级各一个事务。
- 扩展切点：`ExtensionJunction` 句柄；Module 在清单里声明自己拥有的切点（Module 名是保留 owner）；触发要句柄；内核
  切点不再能从公开入口触发；查询接口；Agent 范围的触发入口。
- Module 工具的命令参数声明；清单 `model` 的平台写法；`system_prompt` 的显式文件写法；按角色的危险处置。
- `.syndica.yaml` 的 `modules:` / `flags:` / `danger.roles`；入口点发现；`syndica modules list/show`；`flags list/show`
  改为从已装 Module 生成；README 的 Flag 一节由生成器写；项目配置的信任确认。
- trace 的 `module` span 与扩展切点事件；拓扑 run 与（纯）Module run 的回放。
- 仓库里一个示例第三方 Module 包（`examples/`），只当文档与 E2E 夹具，不发布。

### 不做

- 不加 `Runtime.start` / `RUNTIME_START`（D-58，路线图主线约束）；运行中卸载 Module（「禁用」= 配置里不启用）；
  `optional` Module（D1）。
- 项目配置放宽安全边界（`allow_all`、`untrusted_high_risk=allow`、第三方工具结果标成可信）：放宽要从用户级配置来，
  M4 不做用户级配置。
- 不给拓扑槽加插件或任意 `options`（M1 的决定维持，见 D4）；Module 只在 Runtime 级启用，不按槽启用。
- 不做沙箱：第三方 Module 与内核同进程，能力声明只供人审查（spec §12.4 #77 的诚实说明）。
- 不做运行时热插拔 Module 的 Flag；Flag 在 Runtime 构造时定下。
- 没有声明 `replay_safe` 的第三方 Module 给 Agent 装了东西时，这次 run 不写回放文档（D13）。

### TDD 接缝

- 装配事务：在第 N 个 Module 的服务工厂 / `on_start` / 插件 `register` / 工具工厂里抛 `RuntimeError` 与
  `KeyboardInterrupt`，断言 hook 中心、服务、工具表与进来时逐项相同。
- 扩展切点归属：用两个 Module 互相冒用切点名，断言声明被拒；拿不到句柄（字符串、同名的另一个对象）触发被拒。
- 次序：每一层至少三个元素、次序既非升序也非降序（`AGENTS.md` 的基数纪律），断言 `list(...)` 的确切次序。
- CLI：`RuntimeCliHost` 收一个可注入的入口点来源与信任存储，用例不碰真实的 `importlib.metadata` 与 HOME。

## 设计裁决

### D1 Module 清单：追加的关键字参数，名字就是 id

`RuntimeModule(name, *, flags=(), agent_plugins=(), services=None, tool_factories=(), on_start=None, on_shutdown=None,
version='0.0.0', description='', capabilities=None, requires=(), junctions=(), replay_safe=False, security_notes='')`。
新字段只追加在末尾（M1 计划 5：构造参数只能追加），数据类字段次序随之追加。

- `name` 收紧为 `[a-z][a-z0-9]*(?:-[a-z0-9]+)*`（现有六个内置名都符合）：它同时是扩展切点的 owner 段、入口点名与
  配置里启用它的写法，三处必须是同一个规范拼法；不 strip、不折叠大小写。
- `version`：非空、不含空白与控制字符的字符串（不强求 semver：发行版本号按 PEP 440，`1.0.0rc1` 合法），只比较相等。
  内置 Module 取 `syndica.__version__`。
- `description`、`security_notes`：不含控制字符（C0、C1、双向格式字符、落单代理）的字符串，`description` 还必须是
  一行，`security_notes` 可以换行与缩进（`\n`、`\t`）；它们进 `modules show`，控制字符能在终端上伪造显示（评审 H1）。
  内核与 CLI 各有一份逐字相同的字符表（CLI 的 `version` 命令不加载内核），用例钉住两份相等。
- `capabilities`：`None` 表示**未声明**，按「全部能力」对待并如实显示「未声明」（与 `__tool_command_args__` 的
  `None` = 按最坏情况处理同一个立场）；声明时是闭集 `environment`（读宿主环境变量）、`filesystem`（读写工作区之外，
  或绕过工具层读写文件）、`network`、`subprocess` 的子集，每项一次，`()` 表示什么都不要。只供审查，不做强制，
  也**不是**回放判据（D13）。
- `requires`：它依赖的 Module 名；必须出现在 `modules` 序列里它的前面（装配时查，缺失与次序不对分开报）。
- `junctions`：`ExtensionJunction` 对象的序列，它拥有的扩展切点；每个名字的 owner 段必须等于 `name`（D5）。
- `replay_safe`：作者声明「我的工具、hook、服务与启停钩子不做 I/O、不读时钟与随机数、不起线程」，回放据此决定能否
  真跑它（D13）。缺省 `False`。
- 不加 `optional`：启停钩子失败一律响亮（服务工厂或 `on_start` 抛出即整个 Runtime 构造失败并回滚）。由作者声明的
  「可以静默缺席」是错的权威（评审 M7）——一个守卫类 Module 启动失败后悄悄不在，等于放行。spec 错误处理表那行
  「Module 启停钩子失败只记录日志」随之改写，写明理由；宿主将来需要「可选」时由宿主在启用处声明。

所有用户可见的错误都是三件套，锚词逐字断言。

### D2 两种次序分开写，并且都钉住

- **装配次序**（谁先装上）：校验全部 Module → 保留全部 Module 名为 owner、声明**全部** Module 的清单切点 →
  Runtime 插件 `register` → 建**全部** Module 的服务 → 依次 `on_start` → 每个 Agent spawn 时：角色清单的工具 → 编排
  工具 → Module 的插件与工具（Module 次序）→ 槽的 `extra_tools` → 类式角色自己的工具（`install_own_tools`）。
- **执行次序**（hook 谁先跑，spec §5.3）：Agent 局部 → 本 Agent 的 Module 层（Module 次序、插件次序）→ Runtime 级
  （Runtime 插件在前，Module 的 `on_start` 挂上去的在后）。

槽的工具挪到 Module 工具之后：工具定义的次序是请求前缀的一部分（spec §5.2），同一个角色在不同槽里附加不同工具时，
共同前缀（清单工具、编排工具、Module 工具）因此逐字节相同，前缀缓存在槽之间共享。拓扑 run 此前不写回放文档，
没有记录依赖旧次序。

机制：槽增强仍是工厂的一部分（`Fork` 经 `_factory_of` 用同一个增强过的工厂重新 spawn，分身必须拿到同样的工具与
次序），但工厂只解析槽的工具、收紧预算，把解析好的工具**挂在 Agent 上待装**；`spawn` 在 Module 工具之后、
`install_own_tools` 之前装它们。槽工具与编排工具、Module 工具撞名时报新的三件套（点名对方是谁），不再借用「已经在
角色上」那句。用例：槽 Agent 的分身 `list(options.tools)` 与源逐项相同。Runtime 级的装配次序不变：先建全部服务、
再依次 `on_start`（现有用例钉住这一点，A 的 `on_start` 看得见 B 的服务）。

### D3 事务的边界

- Runtime 级：`modules` 校验全部做完才动 Runtime；先保留 Module 名、声明**全部** Module 的清单切点（在任何第三方
  代码跑之前占住名字——Runtime 插件的 `register` 是第一段第三方代码，所以声明排在它前面），再建全部服务，再依次
  `on_start`（两遍的次序不变）；插件与 Module 两段各按快照回滚。已经 `on_start` 过的 Module 不补偿：`on_shutdown` 是
  异步的，同步构造里调不了，这一点写进 `RuntimeModule` 的 docstring（第三方在 `on_start` 里起的任务、写的文件不在
  回滚承诺内，延续 M1）。
- Agent 级：spawn 里的 Module 插件与工具与其它装配在同一个事务里（现状已是），补 `BaseException` 用例。
- Agent 层的两个中心（Module 层与局部层）拒绝挂 Runtime 级切点：挂上永不触发，静默空转是最难查的一种。

### D4 槽不加插件

M1 的理由仍成立（插件实例难冻结、撑大公开面），而 M4 的诉求（第三方能力）由 Runtime 级的 Module 满足。M3 审计里
「槽附加的插件」是泛称：槽今天附加的是工具、预算与 metadata，T13 回放它们。spec §4.7 的 `plugins` 字段在 §15 状态
里注明「v1 不做」。

### D5 扩展切点的归属：声明靠 owner 段与保留名，触发靠句柄

两半分开解决：**谁能声明**在装配时判（那时知道是谁在装），**谁能触发**靠持有句柄（运行时不靠环境身份——评审 H3
证实环境身份会经子 Agent 的驱动任务、服务调用、并行分发泄漏或洗白）。

- **句柄**：`ExtensionJunction(name, kind)` 是公开类型，按对象身份比较（两个同名对象不相等）。声明之后，触发这个切点
  必须交回**同一个**对象：`HooksCenter.trigger(handle, **kwargs)`；拿字符串触发扩展切点报 `ContractViolation`
  （「字符串谁都拼得出来，用声明时的那个 ExtensionJunction 触发」）。挂 hook 仍可以用名字（监听不是冒用）。
- **Module 的切点**：写在清单 `junctions` 里（作者在定义 Module 时构造这些对象，工具与插件引用同一批对象）。Runtime
  在任何第三方代码跑之前按对象声明它们（D3）。owner 段等于 Module 名在构造 `RuntimeModule` 时就查。**Module 名是保留
  的 owner**：清单之外，任何人不能再声明 `<module>:*`（清单是权威）。
- **其余声明**（宿主代码、Runtime 插件在 `register` 里）：`HooksCenter.declare_extension_junction(name, kind)` 交回
  一个新句柄。在插件的 `register` 里声明时（内核在那一处设好装配期的身份），owner 段必须等于插件名，插件名此时还必须
  符合 Module 名的规范拼法、且不等于任何已装 Module 名（评审 M3）。同一个名字第二次声明一律报错（不论分类），不再有
  「同名同分类即成功」：句柄只能有一个持有者。
- **装配期身份**只在内核枚举的几处设：插件 `register`（Runtime 插件 = 插件名，Module 插件 = Module 名）、Module 的
  服务工厂、`on_start`、工具工厂与插件工厂（= Module 名）。只用于声明时的 owner 校验；`_drive`、收割任务与关机任务
  开头清掉它（同现有的四个 contextvar）。Module 的代码不在运行期声明切点（切点写进清单），在那几处调
  `declare_extension_junction` 报三件套。
- **内核切点不可伪造**（评审 M2）：装配链上只要有一层属于 Runtime（Runtime 自己的中心、spawn 给 Agent 建的 Module
  层，以及挂在它们里面的任何中心），公开的 `HooksCenter.trigger` 就拒绝内核 `Junction`（报 `ContractViolation`，下一步
  「内核切点只由内核在固定位置触发」）；整条链都不属于任何 Runtime 的独立中心照收——第三方靠它给自己的插件写单测
  （第二轮评审 M2：否则唯一的公开测试入口没了）。内核自己的触发点改走私有的分发入口，仍是同一条分发路径。AST 守卫
  随之改认私有入口，并断言包里没有 `.trigger(Junction.X` 的调用点。
- 从 Runtime 的中心触发扩展切点只跑 Runtime 级的 hook（外层看不见内层）；要让一个 Agent 的局部层与 Module 层也跑，
  用 `BaseAgent.trigger_extension`（D6）。docstring 写明，用例钉住。
- 这是同进程里的约束，挡的是误用与抢名，不是恶意代码（spec §12.4）：恶意代码能 import 别人的句柄、改任何东西，这条
  在 docstring 里写明。

### D6 查询与 Agent 范围的触发

- `HooksCenter.extension_junctions() -> Mapping[str, JunctionKind]`：整条装配链上已声明的扩展切点，按名字排序的只读
  映射，供宿主查询。`modules show` 读清单里的 `junctions`，trace 经私有的观察入口记每次触发，都不经它。
- `BaseAgent.trigger_extension(handle, **kwargs) -> HookResult`：在这个 Agent 的整条链（局部 → Module 层 → Runtime）
  上触发一个扩展切点，句柄规则同上。名字带 `_extension`：`trigger` 太容易与类式角色自己的属性撞名（评审 L5）。
  Module 的工具工厂与插件工厂都收到 `agent`，因此拿得到它。`_BASE_AGENT_SURFACE` 加这一个名字，理由写进守卫。
- 触发仍只有一条分发路径；`trigger_extension` 只是选了起点。

### D7 Module 工具必须在自己的类上声明命令参数（B-09 / B-10）

`install_agent` 检查 Module 交来的每个工具：`type(tool).__dict__` 里有 `__tool_command_args__` 且是字符串的 tuple
（`None`、property 都不算），实例上不得另有同名属性（判定器先读实例属性，评审 M5），并当场跑一遍判定器读声明的
那段解析（参数名写错在装配时就失败）；否则拒绝装配（三件套：哪个 Module、哪个工具、什么类，下一步「它不执行任何东西
就声明 `()`，否则列出会被执行的参数名」）。「在自己的类上」而不是「沿继承链找得到」：继承一个 `()` 会让日后真会执行
东西的子类静默失去那道闸（D-34）。`AskUser` /
`AskUserQuestion` 补上自己的声明。类式角色在 `install_own_tools` 里装的工具同样检查（`impl:` 让第三方类式角色从
`.syndica.yaml` 就能进来）：spawn 比对它装之前与之后的工具表，按对象身份比（同名换了一个对象也查），新增与换掉的
逐个查；类式角色的构造函数装的工具同样查（目录冻结出的工厂比对构造前后，阶段一评审 H1）。Module 的工具只经
`tool_factories` 交回的映射装上：工厂与插件 `register` 期间直接往 Agent 上装（`install_runtime_tools` 或直接写
`options.tools`）一律拒绝，否则那个工具既不过这道检查、也不知道属于谁（阶段一评审 H1、第四轮评审 N2）。宿主经
`assemble(tools=)` 交来的工具不在此列（宿主的责任）。

### D8 回调的校验

`register` 拒绝不可调用的回调；可拦截切点上返回既非 `None` 也非 `HookResult` 的值报 `ContractViolation`（此前是一条
指着内核内部的 `AttributeError`）；回调返回的不是可等待对象时同样报出来（此前是 gather 里的 `TypeError`）。可观察
切点上，回调在**同步阶段**就抛出（建协程时）也按「这个 hook 失败了」记 ERROR，不再从 `gather` 的参数展开里逃出去
（评审 L1：在 `AGENT_SPAWN` 上它会在子 Agent 的任务已经起来之后从 `spawn` 抛出）。

### D9 D-42 的裁决：不拆分回调类型

`HookCallback` 维持一个类型。拆成两个是公开面的破坏性变更，且管不住动态注册（切点名可以是运行期算出来的字符串）；
真正管用的「在可观察切点上拒绝标注了 `-> HookResult` 的回调」要在注册路径上求值第三方模块的标注。运行期那条 ERROR
已经把「静默」换成「响亮」，D8 又补上了可拦截一侧的响亮失败。登记表 D-42 关闭为「已裁决：不拆」。

### D10 清单 `model` 的平台写法：`<platform>:<model>`，前缀命名空间封闭

清单（内核）只存字符串；解释在组合根。按**第一个** `:` 切，且**前缀命名空间是封闭的**（评审 H6）：第一个 `:` 之前
没有 `/` 与 `.`、形如一个名字（`^\s*[A-Za-z][A-Za-z0-9_-]*\s*:`）时，它必须恰好是组合根认得的平台之一（`gemini`、
`openrouter`、`openai`，小写、无空白），否则报三件套（`Gemini:x`、` gemini:x`、`gemni:x` 都在这里失败；`qwen3:8b`
这类 Ollama 标签报出「改写成 `openai:qwen3:8b`」）。第一个 `:` 之前有 `/` 或 `.` 的（`cohere/north-mini-code:free`、
`openrouter/free`）是模型名，照旧走 OpenRouter 或 `OPENAI_*`。这样以后加平台不会改变任何已有字符串的含义（未知前缀
此刻就是错误，不会被悄悄当成模型名）。只收 ASCII（全角冒号与长得像拉丁字母的别的字母会让前缀检查认不出平台名，
阶段二评审 M1）；冒号后紧跟型号，前后带空白即拒；`<平台>:default` 是那一家在环境里配置的缺省型号。带平台的名字只走
那一家，不进轮换；OpenRouter（含 `OPENAI_BASE_URL` 指向 OpenRouter 时）仍只收免费模型；平台没配置时报三件套（点名要设
的环境变量）。清单来自项目配置，而通用的 `OPENAI_*` 端点可能付费：`openai:<型号>`、以及没配 OpenRouter 时落到
`OPENAI_*` 的不带前缀的名字，只在用户自己的环境选了它时才走（`SYNDICA_LLM_ORDER` 里列了 `openai`，或两家免费平台
都没配置），否则报三件套（阶段二评审 M1）。每个角色的平台与模型进信任确认的展示
（项目配置选的模型可能在付费端点上，人要看得见）。顺手修掉 OpenRouter 免费门禁的错误来源：清单写的名字被拒时，
错误说的是「清单的 model」而不是 `OPENROUTER_MODEL`。

### D11 `system_prompt` 的显式文件写法

清单的 `system_prompt` 另收 `{file: <相对路径>}`：路径相对清单的 origin，必须在 origin 之内且是一个可读文件，否则报
三件套。字符串的语义不变（M1：不靠后缀猜）。CLI 加载配置时，一个单行、不含空白、带路径分隔符或以 `.md` / `.txt`
结尾、却没有对应文件的字符串记一条 WARNING（它会被当成提示词正文）。本仓 `.syndica.yaml` 改用显式写法。

### D12 按角色的危险处置

`AgentCatalog.freeze(..., role_policies=None)` 与 `Runtime.assemble(..., role_policies=None)`：`Mapping[str, DangerPolicy]`，
列出的角色用自己的，其余用 `danger_policy`；未知角色名报三件套（列出已有角色）。策略在 `freeze` 建每个角色的
`AgentOptions` 时就定下（执行器在 Agent 构造时取走它，现状 14）。

**项目配置只能收紧**（评审 B1：项目配置是不受信的来源，放宽安全边界要问用户）。`flags:` 里的
`untrusted_high_risk` 只收 `reject`（缺省值），`ask` 只能来自命令行的 `--flag`（第二轮评审 M1）。CLI 的 `.syndica.yaml` 里
`danger.roles.<角色>` 是开发期处置表上的**叠加**：`rules: {规则 ID: confirm|block}` 只能把某条规则改得更严
（allow → confirm → block），`policy: block_high_risk` 在表上叠加「全部 HIGH 规则 → block」（落成一张表，回放按表
重建，不映射成 `BlockHighRisk()`——后者会把将来某条非 HIGH 规则的 CONFIRM 放宽）；`allow_all`、把任何规则改成 `allow`、
把 `block` 改回 `confirm` 都报三件套（「项目配置只能收紧处置」）。规则 ID 必须是判定器里存在的（拼错报错，不静默
回落）。内核的 `role_policies` 本身不设方向限制（嵌入内核的宿主自己负责），限制在 CLI 读项目配置的那一层。
放不开的写进文档：开发期守卫（git 写、删工作区根）在 `PRE_TOOL_USE` 上；HIGH 且可溯源到不可信内容的调用按
`untrusted_high_risk` 处置、不问处置表。宿主档案逐个角色记下实际的处置表（从冻结出的 Agent 上读，`--no-write` 重新
冻结时保留按角色的策略），回放照它重建。

### D13 Module 的 run 能不能回放

宿主档案的 `modules` 保持名字列表（承诺类型只加字段），另加 `module_records`：每个已装 Module 一条
`{name, module_version, distribution?, distribution_version?}`（第二轮评审 M5：`version` 分不清是 Module 的还是发行版的）。
回放从不让**文档**决定要不要跑第三方代码（评审 H4）：

- 内置 Module 照旧按名字重建；开发期 Module（`dev`、`read-only`）照旧不重建，效果在记录里（被跳过的调用）。
- 第三方 Module：只有**已安装的那一份**清单写着 `replay_safe=True`、已安装发行版的名字与版本等于记录里的、**且当前
  工作区受信任的配置启用了它**，回放才经入口点重建它。**检查次序是承重的**（第三轮评审 M3）：配置受信任 → 配置
  启用了它 → 已安装发行版的名字与版本与记录一致，三项都过了才 import 入口点、读 `replay_safe`；trace 目录 Agent
  写得进去，先 import 就让一份伪造的 trace 能叫 `trace replay` import 任何装着的入口点。回放交给工厂的 `ModuleHost`：
  回放的工作区根、空环境（回放不跑真命令）、回放用的脱敏器。重建的 Module 依赖的 Module 没被重建时，这次 run 判
  `Unreplayable`（第三轮评审 L2）。捕获时断言脱敏没有改动 Module 名、发行版名与版本、收紧名单（同已有的工具名检查，
  第三轮评审 L1），改动了就不写回放文档。「受信任」由回放**被动地**查信任存储，从不发问（第二轮评审
  H4：`trace` 命令不经信任确认，所以回放不能自己去问人）；没被信任时不重建，next_action 指向 `syndica config trust`。
  判据读已安装的 Module 与本地配置，不读文档里的声明。
- 组合根注入回放的只有第三方 Module 的重建判据（`runner` 不得依赖 `cli`，依赖规则 2）。回放按名字重建内置
  Module 的那张表与 CLI 的 `MANDATORY` / `OPTIONAL_BUILTINS` 各有一份，由一条跨层用例钉住相等：漏了一个，那个内置
  Module 就会被当成「作用已经在记录里」的宿主 Module 放过去（收口评审 B 的 F13）。没有记录的名字是宿主自己的
  Module（开发期守卫、嵌入的宿主自装的），照旧列出、不重建。
- 其余第三方 Module 只要有一点会跑的代码（服务、`on_start`、插件工厂、工具工厂任一）就让这次 run 在 `describe_run`
  时判 `Unreplayable`（说清是哪个 Module、为什么、要怎样才能回放）；只声明 Flag 的 Module 不挡回放。`on_start` 能往
  Runtime 级挂作用于每个 Agent 的 hook（`PrefixDriftModule` 就是），服务能被别的 Module 的工具调用，跳过它们会静默
  分叉（第二轮评审 M7）。
- `replay_safe` 的含义写进 docstring：不做 I/O、不读时钟与随机数、不起线程或执行器（回放跑在虚拟时钟上，等线程会让
  事件循环看起来空闲、定时器提前触发）；读 `ModuleHost.environment` 的工厂在回放里会分叉（回放交空环境，第四轮评审
  L3）。作者声明错了，回放报分叉，不会静默。
- 回放文档的格式升到 3（`SCENARIO_VERSIONS = (1, 2, 3)`）：`module_records`、`danger.roles`、冻结角色的
  `artifact_kinds` 只在 3 里出现；读者接受 1 到 3（第二轮评审 M6：键集是闭集，不升版本旧读者会让人「删掉未知的键」）。

### D14 trace 里的 Module

- 每个 Agent spawn 时，Runtime 里每个已装的 Module 一个 `module` span（根 span，`agent_id` 是这个 Agent；只声明 Flag
  或服务的 Module 也有，这样 trace 自己就说得清这个 Agent 在哪些 Module 下跑），属性 `module.name`、
  `module.version`、`module.tools`（`str[]`，装配次序）、`module.plugins`（`str[]`）、`module.flags`（`str[]`，它声明
  的 Flag 键）。
- 扩展切点被触发时，在当前 span 上记事件 `junction.triggered`，属性 `junction.name`。
- 新属性进 `ATTRIBUTE_KINDS`，**都过脱敏**（第二轮评审 B2：它们是第三方字符串，用户规则在工具 span 上脱敏掉的工具
  名不能在 `module.tools` 里原样落盘或外发，硬约束 #8），`TRACE_SCHEMA` 改为 `syndica.trace/2`。
  **读者接受 `/1` 到当前版本**：今天的 `readable_schema` 只认相等，直接加一会让每一个已有 trace 报「不再支持的旧版本、
  删掉它」——改为按版本号比，头里保留读到的那个；把用例里当「更新版本」用的 `/2` 夹具改成 `/3`；`/1` 的 trace 照样能
  list、show、replay、export（各一条用例）。不改 `Tracer` / `TraceStore` 已有方法的签名（M3 冻结）：用已有的开 span、
  记事件。
- `module` span 在 spawn **提交之后**（注册表登记成功）才记，开即关（零时长）：回滚掉的 spawn 不留痕迹。宿主代码在
  任何 span 之外触发的扩展切点不记事件（没有可挂的 span），`trace show` 的 Module 一节写明这一点。`trace show` 不把 `module` span
  当成 Agent（今天任何非 agent 的根 span 都会被画成一个「没结束」的 Agent）。
- `junction.triggered` 记在唯一的分发入口里：Runtime 把插桩接在自己的中心上，链上任何一层分发时沿 `outer`
  找到它（`trigger_extension` 是从 Agent 的局部中心分发的，第三轮评审 L4）。`module.plugins` 要知道每个插件来自
  哪个 Module：`install_agent` 一并交回插件的归属（第三轮评审 L9）。
- `trace show` 加一节「Module」：每个 Agent 从哪个 Module 拿到哪些工具与插件。

### D15 CLI：显式启用、发现、Flag 文档、信任

- `.syndica.yaml`：`modules:`（名字列表，按次序装，排在宿主必装的 `safety`、`human-interaction` 之后、开发期 Module
  之前）与 `flags:`（Runtime 级覆盖，按已装 Module 的声明校验）；优先级：声明的缺省 < `flags:` < `--flag`。`--flag`
  改为读完配置、定下要装的 Module 之后，对它们的声明校验（此前在读配置之前对写死的两个 Module 校验）；`chat` 用配置
  里的 `flags:`。
- **可启用的 Module**：内置可选的 `retry`、`prefix-drift`，与入口点组 `syndica.modules` 里发现的。发现只读包元数据
  （入口点名、目标、发行版名与版本），**不 import**（评审 M1：import 就是在跑代码，没启用的 Module 必须不可见）；
  只 import `modules:` 里点名的。入口点名与内置 Module 名重名的不收、两个发行版提供同名入口点的报「有歧义」；导入失败
  的原因过宿主的脱敏器再显示。
- **入口点的契约**：值是一个收一个参数的可调用对象，`factory(host: ModuleHost) -> RuntimeModule`；`ModuleHost` 是内核
  的冻结数据类（M4 里有 `workspace_root`、`environment`（交给 Agent 的那份白名单环境）、`redactor`），以后加字段不破坏
  已发布的 Module（评审 H5：零参数的工厂以后再也加不了东西）。入口点名必须等于它交回的 Module 的名字。
- **第三方 Module 的边界写清**：它们与 syndica 同进程、拿得到用户的完整环境、能绕过工具层与开发期守卫（`AGENTS.md`
  硬约束 #7、#9 管不到它们），信任确认与文档如实说。第三方 Module 的工具交回的结果一律标 `UNTRUSTED`（同 §12.2 对
  第三方 Skill 的处理）。做法：**内核执行器收紧，收紧点按结果是谁写的定**——不包工具（第二轮评审 High 1：包一层会让
  D7 看见包装类），也不靠 hook（第三轮评审 High 1：Runtime 级 hook 排在最后，更早的 hook 一个裸 `skip` 就截断它）。
  宿主把要收紧的 Module 名交给 Runtime（内核 API 增量 `Runtime(untrusted_output_modules=...)`，按 `_module_sequence`
  的规矩校验：字符串不当成字符序列，名单里的名字必须是已装的 Module——认不出的名字什么都不收紧，等于静默放开）。
  - **执行过的调用**：工具的 `execute` 一返回、在记 trace 与 `POST_TOOL_USE` 之前，出自被收紧 Module 的工具的结果
    收紧成 `UNTRUSTED`，之后的改写经 `_strictest` 只会更严（第四轮评审 N1：在 hook 结论合并之后才收紧太晚，
    `POST_TOOL_USE` 上的插件与 trace 看到的是工具自报的 `TRUSTED`）。参数校验失败的结果同样收紧。
  - **没执行的调用与改写按作者判**：`PRE_TOOL_USE` 的 skip 与 `POST_TOOL_USE` 的改写写下的文本出自某个 hook，
    `register` 时记下回调的装配期身份，私有的 `_dispatch` 交回是谁 skip / 改写的；作者是被收紧的 Module 时，不论是
    哪个工具的调用都收紧。宿主自己的守卫（`dev`、`--no-write`）写下的跳过文本不收紧（第四轮评审 N1：按工具名两头都错
    ——第三方插件能替宿主的 `Bash` 答一段缓存的输出、带着缺省的 `TRUSTED`，宿主守卫跳过第三方调用时又会把一次没执行
    的调用标成不可信、污染整份轨迹，而唯一的开关会连真实输出一起放开）。收紧后的信任记进 trace，回放的
    `skip_recorded` 照它重建。
  - **归属只来自 `tool_factories` 交回的映射**：工具工厂、插件工厂与插件 `register` 期间直接往 Agent 上装工具
    （`install_runtime_tools` 或直接写 `options.tools`）一律拒绝（阶段一评审 H1 已实现），所以不靠继承来的装配期身份
    归属（第四轮评审 N2：从装配窗口里起的任务继承身份，按身份归属会把编排、槽与角色自己的工具算到 Module 头上）。
    `install_own_tools` 不得替换 Module 的工具（同名换了对象即拒）。执行器的收紧状态进装配快照，spawn 失败时一并还原。
    spawn 之后再装上的工具算宿主的（同进程的代码能留住 Agent 的引用，挡不住，写进风险）。
  - **模型侧四个切点同样按作者收紧**（阶段三评审 High：只收紧工具切点时，被收紧的 Module 能经
    `PRE_LLM_REQUEST` 的改写与跳过、`POST_LLM_RESPONSE` 的改写、`LLM_FAILURE` 的替身交出一段带着缺省
    `TRUSTED` 的响应，它带出的高风险调用于是绕过 §12.2 的升级）：其中任何一处出自被收紧的 Module，这一轮的
    assistant block 整块标 `UNTRUSTED`。`CONTEXT_OVERFLOW` 的 skip 不带内容，压缩后的请求经下一次
    `PRE_LLM_REQUEST` 进来，按那里的规矩判。
  - **就地改动按作者收紧**：交给 hook 的请求、响应与工具结果是可变对象，就地改不经 `modified`。有被收紧的
    Module 时，分发对这几个对象逐个回调前后各取一次投影，变了就算那个回调的作者写的；工具结果的收紧底线
    按分发之前的 trust 算，就地把它改回 `TRUSTED` 放不开。
  - **身份跟着代码走**：每个 hook 回调按注册它的那段代码的身份跑，Module 工具的 `execute` 按它的 Module 的
    身份跑，回调与工具里再注册的 hook 于是记在它们头上（阶段三评审 Medium：此前记成宿主）。剩下挡不住的
    写进风险：在没有身份的地方注册的 hook（事件订阅者、线程）算宿主的；hook 就地改轨迹里已有的 block
    （block 是公开的可变数据类）不归属——轨迹在分发期间冻结的只是追加。
  - **发出去的文字同样收紧**（收口评审 A 的 F1）：被收紧的 Module 的代码经 `send` / `send_to_slot` /
    `spawn(query=)` 写下的文字标 `UNTRUSTED`。仍挡不住、归里程碑 5 溯源链的：直接往 `runtime.broker` 发的
    `Message`（D-63），与经扩展切点的负载流到另一个 Module 的数据（D-64）。
  - **工厂按交出它的那一方的身份跑**（收口评审 A 的 F7）：`Runtime(agents=)` 登记的角色算宿主的，`assemble` 登记的
    按调用 `assemble` 的那一方，经拓扑槽起的按槽上那个角色的，`Fork` 照源 Agent 当初的身份再跑同一个工厂；调用方自己
    交给 `spawn` 的工厂按调用方。类式角色的 `install_own_tools` 与它的工厂同一个身份。内核分辨不出交进来的函数是谁写
    的，所以 Module 把宿主的函数直接交给 `spawn` 时仍按 Module 算（偏保守）；宿主要让角色按自己跑，就登记它。
  - 放开它的开关只在用户级：`run` / `chat` 的 `--trust-module-output <name>`（可重复），宿主档案记下，回放照它重建。
    回放里不在替身名单上的工具会真的执行（`_KernelTool`，含第三方 Module 的工具，第四轮评审 L5），第三方 Module 又只在
    声明了 `replay_safe` 时才重建（D13）；被放宽的结果在回放里能驱动的只有替身工具、内核工具与这些声明了不做 I/O 的
    工具。项目配置放不开（第二轮评审 High 2：没有开关的保守标记会让一次第三方调用之后的每个高风险调用都被拒）。
  - 开发期守卫对这些工具不认它们自己声明的 `()`，按「每个字符串参数都可能被执行」判（第二轮评审 M12；实现按类定义在
    不在 syndica 包里认，`impl:` 角色自己的工具一并收紧）。开发期记录器挪到 Agent 的局部层：Module 层与 Runtime 层的
    `skip` 截不断它，看到的也是工具自己交回的结果（第四轮评审 Low 给的两个选项之一；另一项「执行器一侧的
    私有回调」要给内核加一个只供 CLI 用的私有入口，而记录器还要往结果里写 `check`，回调做不到）。它**不是**整条链
    最前（收口评审 A 的 F3）：`dev` 最后装，在它之前的 Module 的工厂拿得到 Agent，往局部层挂的 POST_TOOL_USE hook 排在
    它前面，那个 hook 的 skip 让这次调用记不下。挡掉就得拒收 Module 身份下的局部层注册，而 `dev` 自己正是这样挂的；
    登记为 D-62，归里程碑 5（执行面在执行器里记下每个调用的结局）。
- **第三方 Flag**：键必须以 `<Module 名的下划线写法>_` 开头（`example` → `example_greeting`），像密钥的键名一律拒收
  （「凭证从环境来，不进 Flag」）——Flag 的值会进宿主档案与回放文档。
- **`modules:` / `flags:`**：`modules:` 是名字列表；`flags:` 与 `--flag` 走**同一个**校验函数，CLI 拒收的取值（如
  `untrusted_high_risk=allow`）在两处都拒收，生成器也不把它们列成可选值（评审 B1）；`flags:` 另外只收收紧的取值
  （D12）。`FlagDeclaration.description` 在构造时拒收控制字符（内核），显示第三方字符串的每一处（`modules list/show`、
  `flags list/show`、工具与插件名）都过 `one_line` 转义（第二轮评审 M10）。
- `syndica modules list`：名字、来源（内置 / 发行版名与版本）、状态（必装 / 已启用 / 可启用 / 坏的）；不 import。
  `modules show <name>`：显式请求才 import，列出清单全部字段（能力未声明时写「未声明」）与它的 Flag。
- `syndica flags list` / `flags show <key>` 与参考文档 `docs/reference/flags.md` 由同一个生成器从声明产出（键、所属
  Module、类型、缺省、取值范围、说明；`show` 另给生效值与来源）。生成器是内核里的纯函数（只产结构化的行，不读环境），
  渲染在 CLI；以后的 Web UI 用同一份。参考文档只由内置 Module 生成（不随 venv 里装了什么变），用例断言它与生成器的
  输出逐字相同，另有一份独立写死的金标（至少三个 Flag、次序非自然排序）防生成器本身写错。放在中文的参考文档而不是
  英文 README：Flag 的说明今天是中文（英文版归 M12）。README 只链过去，不生成。
- CLI 接线：`modules`、`config` 进命令表（今天 `syndica modules list` 会被当成 prompt 跑一个 Agent）；`CliHost` 协议、
  `InspectRequest.resource`、各处 `config` 的字面缺省、chat 的「配置没变」检查随之改；`chat` 装配置里的 `modules:`
  与 `flags:`。启用顺序：`safety`、`human-interaction`、配置里的 Module、`read-only`（`--no-write` 时）、`dev`（最后）。
- `retry` 按名字启用时没有备用模型（CLI 不给 `fallbacks`），`llm_fallbacks` 非空在读配置时就报错；回放的 Module 表补上
  `prefix-drift`。
- 第三方 Module 的工具碰不到宿主的 Portal（它是宿主私有的），要读写文件只能绕过工具层：M4 不给 `ModuleHost` 加
  Portal，归 M5（执行面完整化），写进风险。
- **信任**（评审 B2、H1、H2、M8）：
  - 何时要：自动发现的 `.syndica.yaml`（没给 `--config`），以及 `--config` 指向工作区之内的文件，第一次使用或摘要变了
    时；`run`、`chat`、`doctor`，以及启用了第三方 Module 时的 `flags` 与 `modules show`。只解析不执行的命令（`agents`、
    `topology`）不要；`load_config` 本身不 import 任何东西（不碰入口点与 `impl:`）。
  - 摘要盖住实际会被加载的东西：配置文件的字节；每个被引用的提示词文件（隐式路径与 `{file:}`）的路径与字节；每个
    `impl:` 目标的字符串，以及它在**工作区之内**时整棵顶层包里的**每一个文件**（Agent 改得到它们，第二轮评审
    M11、第三轮评审 M2：只摘一个文件盖不住父包的 `__init__.py` 与它 import 的兄弟模块；第四轮评审 N3：Python 先于
    `.py` 加载同名的扩展模块，也加载没有源文件的 `.pyc`，只摘源文件盖不住它们）；每个启用的第三方 Module 的入口点
    目标、发行版名与版本。找 `impl:` 的位置按 import 系统自己的次序逐段问 `sys.meta_path` 上的 finder（子段用父包
    spec 的 `submodule_search_locations`），不 import（`find_spec('a.b')` 会先 import `a`，那是在确认之前跑代码）；
    `sys.modules` 里已有的、finder 认不出的、origin 不是普通文件的（命名空间包、zip、自定义 loader）一律拒绝，不当成
    「工作区之外」放过去（第四轮评审 N3：只问 `PathFinder` 时，可编辑安装的 finder 认得而它认不出的代码会被当成工作区
    之外、不进摘要）。import 工作区里的代码之前把 `sys.pycache_prefix` 指到本进程新建的 0700 临时目录（Agent 种下的
    `.pyc` 会被当成源文件的缓存；固定的目录 Agent 的 Bash 写得进去，而 unchecked-hash 的 `.pyc` 从不与源文件比对）。
    正在运行的 syndica 自己的包不摘（宿主进程本来就在跑那份代码）。工作区之外的 `impl:` 代码与发行版的内容（含可编辑
    安装）不进摘要，展示里如实写「信任它就是信任那份代码」。`sys.path` 里落在工作区之内的条目（工作区里的 `.venv`、
    脚本所在目录）在展示里逐条写出：Agent 写得到它们，那里的 `.pth` 在任何闸之前、带着完整环境运行，工作区内 `impl:`
    包 import 的其它顶层模块也不在摘要里——这些摘要盖不住，只能如实说（写进风险）。启用的第三方 Module 的发行版装在
    工作区之内时，按它的 `RECORD` 核对每个文件的哈希并把 `RECORD` 进摘要，核对不过就拒绝（T9b）。
  - 只读一次：提示词文件由 CLI 自己解析与读取（隐式路径与 `{file:}` 都在 CLI 这一侧认），读出的文本内联交给内核的清单
    加载，并告诉内核不要再按路径读（内核 API 增量，第二轮评审 High 3：否则内核在摘要之后再读一次文件）；读出的字节
    同时用于算摘要、解析与展示；解码与 `read_text` 一致（通用换行：`\r\n`、`\r` 都成 `\n`），否则 CRLF 的提示词
    文件交给模型的字节与此前不同，前缀缓存整体失效（第四轮评审 Low）；人答完之后重读一遍再比，变了就中止并要求重跑
    （AGENTS.md：检查与动作之间隔着一次问人，要再查一次）。
  - **确认之前不 import 任何第三方代码**（第二轮评审 B1）：展示只用元数据——启用的 Module 的入口点目标、发行版名与
    版本；能力要 import 才知道，展示里写明「能力用 `syndica modules show <name>` 查看」（代码在工作区之内的，先确认
    这份配置才看得到）。另外展示
    `workspace.verify`、`pass_env`、每个角色的平台与模型、`impl:` 目标、提示词文件；单行字段过 `one_line`、多行字段逐行
    过它并加固定的缩进前缀（`terminal_safe` 保留换行，多行配置值能伪造出一行顶格的假确认，第二轮评审 H1 残留、第三轮
    评审 M10）。
  - 确认的提示与回答走 stderr（不混进 run 的答案，stdout 被重定向时也看得见，第二轮评审 M14）。
  - 只认人：答案必须来自交互终端（stdin 与 stderr 都是 TTY，管道里的 `yes` 不算）；非交互时拒绝并给出下一步
    `syndica config trust --sha256 <摘要>`（摘要从拒绝消息里抄，等于人读过）。开发期守卫**不另加规则**：Agent
    环境里的 `SYNDICA_TRUST_STORE` 让嵌套的 syndica 写临时存储，直接写用户信任存储的 shell 命令落在工作区之外，
    已有的 `shell.path_outside_workspace` 要人确认（实现时的裁决：一条只靠文本匹配的新规则挡不住
    `python -m syndica` 与 `python -c`，加了只多一张要维护的表）。TTY 判定同样是尽力而为（`script` 之类的 pty 包装能
    同时伪造 stdin 与 stderr，第三轮评审 M5），真正的隔离在 M5：宿主在 Agent 的环境里设 `SYNDICA_TRUST_STORE`
    指向这次 run 的临时目录，`pass_env` 拒收这个名字——Agent 里嵌套起来的 syndica 缺省写的是那份。这只防无意的
    嵌套，不是隔离：`HOME` 照常透传，`env -u SYNDICA_TRUST_STORE` 就回到用户的存储（第四轮评审 M5）。
  - 存储：`$XDG_CONFIG_HOME/syndica/trusted.json`（缺省 `~/.config/syndica/`），按配置文件的绝对路径记摘要；存储在
    工作区之内时拒绝使用；原子写入（临时文件 + rename），读不出按「未信任」处理；写入点登记进 CLI 的写入点守卫。
  - 宿主收一个**必填**的信任闸参数（第二轮评审 M8：可选参数等于缺省放行，而 `RuntimeCliHost` 是导出的类）：组合根
    （`__main__._LazyHost`）接上真实存储；不要这道闸必须显式传一个写明「不设闸」的值，用例经共用的夹具传它。
  - 展示另外列出 `flags:`、`danger.roles`、`redaction.rules`（它们只能收紧，但人要看得见）。`trace` 系列命令不经
    信任确认：它们读 `redaction.rules`（病态正则按 M3 的裁决当成「你运行的代码」），回放另外被动地查信任存储（D13），
    从不发问。

## 公开契约

### `RuntimeModule` 与 `ModuleHost`

- 追加的关键字参数与字段：`version: str`、`description: str`、`capabilities: tuple[str, ...] | None`、
  `requires: tuple[str, ...]`、`junctions: tuple[ExtensionJunction, ...]`、`replay_safe: bool`、`security_notes: str`
  （D1）。能力词表：`environment`、`filesystem`、`network`、`subprocess`；`None` = 未声明。名字的规范拼法：
  `[a-z][a-z0-9]*(?:-[a-z0-9]+)*`。
- `ModuleHost`（内核，冻结数据类）：`workspace_root: Path`、`environment: Mapping[str, str]`、`redactor: Redactor`；
  只追加字段，新字段带默认值（第三方的测试会自己构造它）。

### 扩展切点

- `ExtensionJunction(name, kind)`：公开类型，按对象身份比较；名字 `<owner>:<event>`。
- 声明：Module 写在清单 `junctions` 里（owner 段 = Module 名，Module 名是保留 owner）；其余经
  `HooksCenter.declare_extension_junction(name, kind) -> ExtensionJunction`（插件里声明时 owner 段 = 插件名）。同名只能
  声明一次。
- 触发：`HooksCenter.trigger(handle, **kwargs)` 与 `BaseAgent.trigger_extension(handle, **kwargs)` 只收声明时交回的
  那个句柄；字符串触发扩展切点报 `ContractViolation`；装配链上有一层属于 Runtime 时公开入口触发内核 `Junction` 也报
  （独立的中心照收，供插件单测）。挂 hook 可以用名字或句柄（`HooksCenter.register`、`BaseAgent.register_hook`）。
- `HooksCenter.extension_junctions() -> Mapping[str, JunctionKind]`。
- `HookCallback` 维持一个类型（D9）。

### 内核 API 增量（都向后兼容，除扩展切点的触发规则外）

- `AgentCatalog.freeze(..., role_policies=None)`、`Runtime.assemble(..., role_policies=None)`（D12）。
- `Runtime.assemble(topology, catalog, ...)` 的 `catalog` 另收已冻结的 `Mapping[str, AgentFactory]`（T13；收它时不再
  冻结，`role_policies` 与 `danger_*` 不适用、传了就报错）。
- 清单 `system_prompt` 另收 `{file: path}`（D11）；`manifest.prompt_file(value, origin)` 交回清单会读的那个文件
  （或 None）。CLI 自己读好文本后以字面提示词、不给 origin 交给清单——没有 origin 时内核一律不按路径读，这条入口
  M1 起就有，不必另开（D15 的「只读一次」）。
- Module 工具与类式角色自己装的工具必须在自己的类上声明 `__tool_command_args__`（D7）；`AskUser` /
  `AskUserQuestion` 补声明。
- 扩展切点的触发规则收紧（D5）：此前谁拿到中心谁就能按名字触发任何切点；没有生产代码依赖旧规则。
- `TraceRecorder.describe_run(..., topology=None)`（T13）。
- `Runtime(..., untrusted_output_modules=())`：这些 Module 的工具（含它们装配期另装的）交回的结果由执行器收紧成
  `UNTRUSTED`（T9b，D15）。
- `HostProfile` 加字段，不改 `modules` 名字列表（T8、T14）。

### 清单 `model`

`<platform>:<model>`，平台为 `gemini` / `openrouter` / `openai`；形如名字的前缀必须是这三个之一；第一个 `:` 之前有
`/` 或 `.` 的是模型名（D10）。这不是严格向后兼容：以这三个前缀开头的名字含义变了，`qwen3:8b` 这类名字要改写成
`openai:qwen3:8b`（错误消息直接给出改写）。

### `.syndica.yaml`

```yaml
modules: [retry, example]          # 显式启用，按次序装
flags:
  llm_retry_attempts: 2
  example_greeting: hi
danger:
  roles:                           # 只能收紧（D12）
    Explorer: {policy: block_high_risk}
    Worker: {rules: {shell.path_outside_workspace: block}}
agents:
  - name: Planner
    description: 拆解需求
    system_prompt: {file: docs/agents/planner.md}
```

### CLI

- `syndica modules list|show <name>`；`syndica flags list|show <key>`（输出改为生成器产出）；
  `syndica config trust [--sha256 <摘要>]`（非交互时必须给摘要）；`run` / `chat` 的 `--trust-module-output <name>`。
- `--config` 不再有字面缺省：省略时自动发现工作区根的 `.syndica.yaml`；工作区之内的配置（自动发现或显式给出）要信任
  确认，工作区之外显式给出的视为已确认。

### 入口点

组名 `syndica.modules`；值是 `factory(host: ModuleHost) -> RuntimeModule`；入口点名等于 Module 名，不得与内置 Module
重名；第三方 Flag 键以 `<Module 名的下划线写法>_` 开头。

### trace schema `syndica.trace/2`

新属性：`module.name: str`、`module.version: str`、`module.tools: str[]`、`module.plugins: str[]`、`module.flags: str[]`、
`junction.name: str`。`module` span 开始产出；新事件 `junction.triggered`。`/1` 照旧读得进。

### 回放文档

格式升到 3。宿主档案 `modules` 仍是名字列表，新增 `module_records`（`{name, module_version, distribution?,
distribution_version?}`）、`danger.roles`（逐个角色的实际处置表）与第三方工具结果被放开信任的 Module 名单；拓扑 run 写
`topology` 段与 `entry.mode: slot`；冻结角色多一个可选的 `artifact_kinds`，`max_turns` 允许 0。

## 任务与进度

每个任务：先写失败的用例，再实现；定向用例与六道门禁过了、跑一次全量就提交。导出验证（暂存区单独检出到临时目录跑
门禁与全量）在推送前或阶段末做一次。变异只打安全与确定性的不变式（标在任务里）。每个阶段末派一次对抗评审，后台跑、
不挡下一阶段，发现的问题集中修。

### 阶段一：内核的扩展机制

#### T1 — Module 清单（完成）
- 字段、校验与三件套错误（D1）；内置 Module 填上版本、说明、能力（显式声明，多数是 `()`）、`replay_safe` 与安全说明；
  `ModuleHost`。`ExtensionJunction` 这个数据类型随 T1 落地（清单字段要它；第二轮评审 M13 指出的倒置依赖），切点的
  声明与触发规则仍在 T4。
- 用例：每个字段的拒绝面按类型族枚举（bool 冒充、空串、换行与控制字符、重复、非法名、owner 段不符、能力词表外的词）；
  `capabilities=None` 与 `()` 是两件事；字段次序与构造签名的形状守卫追加；`requires` 缺失与次序不对分开报。

#### T2 — 次序（完成）
- spawn 里槽的 `extra_tools` 挪到 Module 工具之后（D2）；钉住工具次序与 hook 执行次序（每组至少三个、非自然排序）。
- 变异：反转任一组的次序必须变红（前缀缓存的确定性）。

#### T3 — 事务与 Agent 层的拒绝（完成）
- 清单切点先于一切第三方代码声明（内核私有的按对象声明随 T3 落地，T4 在它上面加规则）；`BaseException` 在 Runtime 级与 Agent 级都回滚（`KeyboardInterrupt` 用例）；Agent
  层中心拒绝 Runtime 级切点（D3）。
- 变异：去掉任一处还原必须变红。

#### T4 — 扩展切点：句柄、保留名、内核切点私有分发、查询、Agent 范围触发、回调校验（完成）
- D5、D6、D8、D9；清单切点由 Runtime 声明（D3 的「先于一切第三方代码」）；内核的触发点改走私有分发入口；更新 AST
  守卫；给 `HooksCenter` 加按真实实例枚举的公开面守卫（今天没有）；句柄与声明者进 `_assembly_snapshot`（回滚用例各一条）；
  现有按字符串触发扩展切点的用例改用句柄，按内核切点触发来测分发语义的用例（82 处）改走私有分发入口。
- 变异：字符串触发放行、句柄比相等而不是比身份、保留名不查、公开入口放行内核切点、插件声明不查 owner，都必须变红。

#### T5 — Module 工具的命令参数声明（D7，完成）
- `_HumanTool` 的基类声明去掉、两个子类自己声明（D-34）；类式角色自己装的工具按对象身份比对。
- 变异（安全）：沿继承链取声明、放过实例属性、不跑解析、不查 Module 工具、不查类式角色的工具，都必须变红。

阶段末：对抗评审一（扩展机制：事务、归属、次序）。

### 阶段二：清单与策略

#### T6 — 清单 `model` 的平台写法（D10，完成）
#### T7 — `system_prompt` 的显式文件写法与 CLI 警告（D11，完成）；本仓配置改用显式写法
#### T8 — 按角色的危险处置（D12，完成），宿主档案与回放随之扩展
- 回放文档格式 3 随 T8 落地（`danger.roles` 是第一个 v3 的键）；捕获写「写得下这份内容的最低版本」，没用到 v3 的
  键就仍写 2，旧版本的读者照样读得进。
- `HostProfile` 是承诺类型：`modules` 名字列表保留，Module 记录与按角色的处置表都加**新字段**，不改旧字段的形状。
- 变异：按角色的策略查错角色、回落错策略必须变红（安全）。

### 阶段三：CLI surface

次序是 T9a → T11 → T9b → T10：信任的摘要要盖住第三方 Module 的发行版版本，所以发现先于信任；启用第三方 Module
的配置不能早于信任确认落地，所以启用在信任之后（第二轮评审 M13）。

#### T9a — 发现、`modules:` 的解析与 `modules list`（D15，完成）
- 发现只读元数据（入口点名、目标、发行版名与版本），不 import；内置名保留、重名有歧义；`modules` 进命令表。
- `.syndica.yaml` 的 `modules:` 在这里解析，只启用内置的（`retry`、`prefix-drift`）；第三方名字在 T9b 落地之前报
  「启用第三方 Module 要先有信任确认」（第三轮评审 M4：`modules list` 的状态与 T11 的摘要都要读它）。
- 变异（安全）：`modules list` 触发 import 必须变红。
#### T11 — 项目配置的信任确认（D15，完成；第四轮评审 N3 的修补另起一次提交）
- 摘要的每一部分、只读一次与答后复查、终端转义、只认 TTY、`config trust --sha256`、存储不在工作区内、原子写入。
  开发期守卫不另加拦 `config trust` 的规则（裁决见 D15「只认人」）。
- 修补（第四轮评审 N3 与 Low）：按 `sys.meta_path` 找 `impl:`、认不出就拒；整棵包的每个文件进摘要；每个进程新建
  0700 的 `pycache_prefix`；`sys.path` 里的工作区条目进展示；提示词按通用换行解码（CRLF 用例）。变异：只问
  `PathFinder`、只摘 `.py`、固定的 `pycache_prefix`、不展示工作区里的 `sys.path` 条目、按字节原样解码，都必须变红。
- `config` 进命令表；`--config` 的字面缺省改为自动发现；提示与回答走 stderr；信任闸是宿主的必填参数。
- Agent 的环境里设 `SYNDICA_TRUST_STORE`，`pass_env` 拒收它。
- 阶段三评审的修补：「在工作区之内」按字面或解析之后有一种在里面就算、按文件身份比（工作区里的符号链接 Agent
  改得到指向；大小写不敏感的卷上同一个目录有多种拼法）；摘包时走进链接进来的目录；工作区之外的代码进摘要的是
  它解析之后的位置（摘要格式升到 `syndica-trust/2`）；Module 的代码在工作区之内时同样换私有的 `pycache_prefix`；
  摘进去的代码在 import 时按确认时的内容核对（`PinnedImports`，惰性 import 在 run 中途才读盘），回放重建时也装；
  `modules show` 对工作区之内的代码要求先启用、经确认；`modules list` 标出它；确认提示列出 `flags:`；`retry` 的
  两个 Flag 设上限（10 次、60000 毫秒）；一个发行版的 `entry_points.txt` 坏了只坏它那一行。仍挡不住的：工作区里
  `sys.path` 条目上没摘进去的代码（只展示，见上），以及在任何确认之前就跑的 `.pth`。
- 变异：摘要漏掉任一部分（提示词文件、`impl:` 与工作区内的整棵包、Module 的发行版版本）、答后不复查、展示不转义、
  管道输入被当成确认、非交互时放行、确认之前 import 了第三方代码（含找 `impl:` 源文件时）、Agent 的环境里没有
  `SYNDICA_TRUST_STORE`，都必须变红（安全）。
#### T9b — `modules:` / `flags:` 启用、第三方工具结果的信任（D15，完成）
- 入口点 `factory(ModuleHost)`；第三方 Flag 键前缀与像密钥的键名；`flags:` 与 `--flag` 共用一个校验函数、`flags:` 只收
  收紧的取值；`--flag` 改为读完配置之后校验；`FlagDeclaration.description` 拒收控制字符；组装 Module 的 CLI 函数（次序、
  收紧名单、Flag 规则）只有一份；内核执行器收紧第三方工具的结果（`Runtime(untrusted_output_modules=...)`：执行过的
  调用在 `execute` 一返回时收紧，skip 与 `POST_TOOL_USE` 的改写按写下它的 hook 的作者收紧），`--trust-module-output`
  放开；`install_own_tools` 不得替换 Module 的工具；收紧状态进装配快照；开发期守卫不认第三方工具自己声明的 `()`；
  开发期记录器挪到 Agent 的局部层；启用的第三方 Module 的发行版装在工作区之内时按 `RECORD` 核对，它在工作区里的
  代码与 `impl:` 一样整棵进摘要。启用了第三方 Module 的 run 暂不写回放文档（T14 按 D13 补上重建）；`modules show`
  看第三方清单与 `flags list/show` 认配置里的 Module 归 T10。
- 变异（安全）：`flags:` 收下 `untrusted_high_risk=allow` 或 `ask`、第三方工具结果没被标 `UNTRUSTED`（含 `POST_TOOL_USE`
  的插件看到自报的 `TRUSTED`、第三方插件 skip 宿主工具、宿主守卫 skip 第三方工具被误标三种）、名单里的字符串被当成
  字符序列、`install_own_tools` 替换 Module 工具被放过、开发期守卫认了第三方的 `()`、`RECORD` 不核对，都必须变红。
#### T10 — Flag 文档生成器、`flags list/show`、参考文档（D15，完成）
- 内核 `flag_reference(modules) -> tuple[FlagRow, ...]`（按 Module 次序、再按声明次序，不重排）；`FlagDeclaration`
  的说明拒收会改写终端显示的字符与换行。CLI 渲染 `flags list`（key、Module、类型、缺省）、`flags show`（另给生效值与
  来源）与 `docs/reference/flags.md`（`scripts/gen_flag_reference.py` 生成，用例比对生成器输出与一份独立金标），
  三处都不把 CLI 拒收的取值列成可选值；`llm_fallbacks` 非空一律拒收（CLI 按名字启用 `retry` 时不给备用模型）。
- `flags list/show` 在配置启用了第三方 Module 时先过信任确认（要 import 才知道它声明了什么）；`modules show <名字>`
  对第三方 Module 照点名 import、不过信任确认——那是用户在命令行上点的名，不是配置替它选的代码（可推翻）。

阶段末：对抗评审二（清单、策略与 CLI：安全边界、配置与发现的失败路径）。

### 阶段四：trace 与回放

#### T12 — `module` span、扩展切点事件、`trace show` 的 Module 一节（D14，完成）
- `TRACE_SCHEMA` 升到 `syndica.trace/2`，读者接受 `/1` 到当前版本（`/1` 的 trace 照样 list、show、replay、export）。
- spawn 提交之后（入表、任务建好、派生边记下）每个已装 Module 一个开即关的根 span，带 `module.name` / `version` /
  `tools`（装配次序）/ `plugins` / `flags`；`install_agent` 一并交回插件归属。这些属性都过脱敏。
- 扩展切点的分发经 Runtime 接在自己中心上的观察者记 `junction.triggered`（链上任何一层沿 `outer` 找到它，有没有
  hook 都记）；本 Runtime 的 span 之外触发的不记。`trace show` 加「Module」一节，不把只有 Module span 的 Agent
  画成没结束的 Agent。
#### T13 — 拓扑 run 回放（M3 遗留，完成）
- 已落地：`assemble` 另收冻结工厂（`artifact_kinds` 由调用方给，`models` 可省、冻结用的参数一律拒收），
  `Topology.validate` 按工厂表的名字校验；CLI 在建 Runtime 之前算出实际装配的拓扑交给 `describe_run`
  （`topology=`，入口 `mode: slot`），冻结角色记 `artifact_kinds`（有就写 v3），`max_turns` 允许 0；回放与
  `runner/scenario.py` 的槽入口都改走 `assemble` + `send_to_slot`。根结束之后平级 Agent 再给它发消息那一条登记为
  已知限制（见风险），没写用例：它取决于两个 Agent 的先后，CLI 的捕获里排不出稳定的一次。
- 宿主把实际装配的拓扑（CLI 改写过预算与只读工具之后的那一份）交给 `describe_run`，回放文档写 `topology` 段与
  `entry: {mode: slot, slot: <根槽>}`。
- 内核：`Runtime.assemble` 的第二个参数另收 `Mapping[str, AgentFactory]`（已冻结的工厂，不再冻结一次，也不覆盖
  `Runtime(agents=)` 里同名的工厂之外的东西——两种入参共用同一段装配逻辑：绑定、别名、`_SLOT_SPAWN`、实例
  metadata）；回放用它装配，入口走 `send_to_slot`，槽的工具、预算、`artifact_kinds`、lazy / manual 与稳定键都由内核
  那一份逻辑给出，与原始 run 同一条路。`TraceRecorder.describe_run(..., topology=None)` 收实际装配的拓扑。冻结角色
  记下 `artifact_kinds`；`max_turns` 允许 0。
- M3 登记的那条（根结束之后平级的 Agent 再给它发消息：原始 run 的关机可能拒绝激活，回放的收尾却先让剩下的 Agent
  跑完）随拓扑回放变得可达：写一条用例钉住现状，修不了就登记成已知限制。
- `assemble` 收冻结工厂时：每个角色的 `artifact_kinds` 由调用方给出、按名字校验拓扑（不要求 `AgentCatalog`）、
  `models` 可省（第二轮评审 M4）。`runner/scenario.py` 的槽入口同样绕过了 `assemble`，一并改走它，改不了就登记。
- 变异：回放丢掉槽的工具或预算、走回 `spawn` 而不是 `send_to_slot`，都必须报分叉（确定性）。
#### T14 — Module 的 run 回放（D13，完成）
- 宿主档案只在有第三方 Module 时写 `module_records`（`{name, module_version, distribution?, distribution_version?}`）
  与非空的 `untrusted_output_modules`（都是 v3 的键；没有第三方 Module 的 run 照旧写 v2）。捕获时第三方 Module 有会
  跑的代码又没声明 `replay_safe` 就不写回放文档（只声明 Flag 的不挡）；脱敏会改掉 Module 名或发行版的同样不写。
- 回放的判据在 `cli/replay_modules.py`，由组合根注入 `ReplayRunner(modules=)`；重建的 Module 依赖的 Module 没重建
  时回放拒绝开始；被收紧的名单照档案重建（只对重建了的 Module）。
- 阶段四评审的修补：回放文档 v3 的键与取值到此冻结（没有哪个发布带过 v3；此前阶段二、三的 `main` 写下的 v3 读不进
  此后的键），用例逐项钉住 v3 比 v2 多出的键，再扩就得升 v4。拓扑 run 与 `max_turns: 0` 的角色写 v3（v2 的读者读得进
  却回放不出、或会叫人改成 1）；v2 里 `max_turns: 0` 报「改成 version 3」。槽 `extra_tools` 被脱敏改掉、装配拓扑或寻址
  入口槽时就失败的 run 不写回放文档；入口槽是 manual 或 on-demand 的场景在解析时就拒。回放重建遇到工作区之内的信任
  存储一律不重建；依赖没重建的报错保留自己的下一步（`ModulesNotRebuilt`），不再被换成「重新记录」。
- `replay_safe`、已安装发行版名字版本一致、当前配置受信任（被动查）且启用了它，才重建；否则有可跑代码的第三方
  Module 让 run 判 `Unreplayable`。宿主档案加 `module_records`；回放文档格式升到 3。
- 变异：读文档里的声明而不是已安装的那一份、版本不比、`replay_safe=False` 也重建、不查信任、只有 `on_start` 的
  Module 被当成不挡回放、检查之前先 import 了入口点，都必须变红（确定性与安全）。

阶段末：对抗评审三（trace 与回放的保真）。

### 收口

#### T15 — 示例第三方 Module 与 E2E（完成）
- 示例只用 `syndica.kernel` 公开的名字；用例经 `importlib.metadata.EntryPoint` 装上（不装进环境），`pyright` 的
  include 加上 `examples`。
- `examples/syndica-example-module/`：一个 Flag、一个工具（触发自己的扩展切点）、一个插件（收这个切点并记下）、
  零能力。用例里经可注入的入口点来源装上，不改内核。
- 四个 E2E 场景（路线图）：装上之后 Agent 有新工具、新 Flag、新 hook；触发自有切点 hook 收得到；不启用时工具、hook、
  服务、Flag 都不可见；第二个 hook 抛错时第一个被回滚。外加「三者一致」：`flags list`、Agent 的工具、trace 的
  `module` span 指向同一份声明；这次 run 离线回放复现。再加一条运行期错误：可观察切点上的 hook 抛错只记 ERROR、run
  照常完成；可拦截切点上的抛错上抛（路线图：除非切点声明为必须失败）。

#### T16 — 真实模型 E2E、文档与收口（完成）
- 结论与证据见 [M4 收口审计](../audits/2026-10-03-m4-final-audit.md)。
- 仓库副本里把示例包装进副本的环境，`XDG_CONFIG_HOME` 指向临时目录（不写真实的信任存储），用免费模型跑一次启用它
  的 run：Agent 用了 Module 的工具，trace 有 `module` span
  与切点事件，`flags list` 列出它的 Flag，回放复现。
- README、本地验证指南、spec §15 与路线图的状态；spec 错误处理表里「Module 启停钩子失败只记录日志」那行按 D1
  改写（第三轮评审 L7）；登记表关闭 B-09、B-10、D-42；收口审计。
- 收口评审两路（功能与鲁棒性；扩展与可维护），修完再收口。

## 验证

| 层 | 内容 | 何时跑 |
|---|---|---|
| 门禁 | ruff format / check、pyright、check_deps、check_links、pytest | 每个任务一次；阶段末导出验证 |
| 契约 | 现有 Store 契约（不变） | 随全量 |
| L2 | 示例 Module 的 E2E、拓扑与 Module run 的捕获即回放 | 随全量，零成本 |
| 变异 | 只打安全与确定性的不变式（任务里标出） | 任务完成时 |
| 评审 | 每阶段一次对抗评审；收口两路 | 后台，不挡提交 |
| 真实模型 | T16 | 手动，只在副本里，只用免费模型 |

## 决策日志

- 2026-10-03 用户授权自主完成 M4：方案由 Agent 评审、修到无阻塞问题后开工，不等用户审方案；只在要改 spec 的方向或
  四条主张、引入付费服务或重量级依赖、放宽安全边界、动本仓以外的东西时停下来问。收口报告单列「可推翻的决定」。
  这取代了 2026-09-21 交接文档里「计划写好先给用户 review」那一条。
- 2026-10-03 验证按阶段：每任务一次全量、导出验证在推送前或阶段末、变异只打安全与确定性、对抗评审每阶段一次不挡
  提交；CI 推迟到 M1–M11 之后（路线图主线约束）。
- 2026-10-03 方案的安全与契约评审（2 Blocking、6 High、8 Medium、5 Low）逐条修进本文：项目配置能放开 CLI 刻意拒收
  的设置（`flags:` 收 `untrusted_high_risk=allow`、按角色 `allow_all`）——改为项目配置只能收紧、`flags:` 与 `--flag`
  共用校验；信任摘要只盖 YAML 且可被竞争——改为盖住提示词文件、`impl:` 目标与 Module 的发行版版本，只读一次、答后
  复查；信任确认可被终端转义伪造、Agent 能替自己确认——转义显示、只认 TTY、`config trust --sha256`、守卫拦下；环境
  身份在运行期会泄漏与洗白——触发改用句柄，环境身份只留在装配期声明；回放让文档决定跑第三方代码、能力缺省即「纯」——
  改为 `replay_safe` 显式声明且读已安装的那一份、能力缺省为「未声明」；第三方代码的边界与零参数入口点——入口点收
  `ModuleHost`、第三方工具结果标 `UNTRUSTED`、风险如实登记；`<platform>:<model>` 未知前缀当模型名——前缀命名空间封闭；
  发现会 import 代码——只读元数据；内核切点可从公开入口伪造——改走私有分发；以及插件名与 Module 名的身份冲突、第三方
  Flag 键、D7 的实例属性绕过、`optional` 的权威、需要信任的命令清单、可观察切点同步抛出、按角色策略的回放、信任存储
  的写入点、README 生成器的输入、`trigger` 撞名。
- 2026-10-03 方案的可行性评审（对着修订之前的版本：2 Blocking、5 High、14 Medium、10 Low）：两条 Blocking 与安全评审
  重合，已修。其余修进本文：槽工具挪位要有机制且不能丢掉分身的工具（工厂把解析好的工具挂在 Agent 上待装）；拓扑
  回放要内核共用一段装配逻辑、`describe_run` 收拓扑、M3 那条收尾差异要钉住；`TRACE_SCHEMA` 加一会让每个旧 trace 读不
  进来（读者按版本号比）；信任的展示内容、范围与缺省（只有组合根接真实存储）；先做信任再做启用；回滚状态、身份命名
  空间、Runtime 级两遍次序不变；`runner` 不得依赖 `cli`（发现由组合根注入）；`HostProfile` 是承诺类型（加字段）；运行期
  错误的 E2E；`HooksCenter` 的公开面守卫；CLI 接线；`module` span 在 spawn 提交之后记；切点事件记在唯一分发入口；Flag
  参考文档放中文的 `docs/reference/flags.md` 并有独立金标；`retry` 无备用模型、回放表补 `prefix-drift`；Portal 归 M5；
  两处偏离 spec 的裁决列为可推翻。

- 2026-10-03 第二轮评审（2 Blocking、4 High、14 Medium、10 Low；第一轮 15 条里 5 条已解、10 条部分解、0 条未解），
  逐条修进本文：信任确认要显示能力就得先 import 未受信配置启用的代码——改为确认前只用元数据；Module 与切点的 trace
  属性不过脱敏（硬约束 #8）——改为都过脱敏；包工具标 `UNTRUSTED` 会让 D7 与参数名校验失效——改为只包工厂、经
  `POST_TOOL_USE` 收紧；无条件的 `UNTRUSTED` 没有开关——加用户级的 `--trust-module-output`；提示词文件「只读一次」做
  不到——CLI 自己读、内联交给内核且内核不再按路径读；D13 与 D15 在回放的信任上矛盾——回放被动查信任存储；以及
  `flags:` 的 `ask`、公开 `trigger` 让插件单测无门（改为只在挂着 Runtime 的链上拒内核切点）、装配次序的写法、拓扑回放
  的 API 缺口、`HostProfile.modules` 的形状、回放格式要升版本、只有 `on_start` 的 Module、信任闸缺省放行、run 与回放的
  组装要同一份、显示转义的缺口、工作区内 `impl:` 源文件进摘要、开发期守卫认第三方的 `()`、阶段内的倒置依赖、确认走
  stderr，与十条 Low（`ModuleHost` 新字段带默认值；`redactor` 维持 `Redactor` 协议——同进程的 Module 本来就读得到环境，
  换成函数藏不住什么，而协议只有 `redact` / `scan`，是内核已承诺的类型）。
- 2026-10-03 第二轮评审把「#7 / #9 管不到第三方 Module」与 D1 / D4 两处偏离 spec 归为「该停下来问」。判断不停：
  第三方代码经 `.syndica.yaml` 的 `impl:` 进程内运行从 M1 起就有；M4 加了一条要经用户确认才打开的路（入口点
  Module），并补上开发期守卫对第三方 `()` 的缺口——启用的 Module 绕得过 #7 / #9，信任确认与文档如实说。D1 把
  「失败只记日志」收紧成「一律响亮」，D4 把槽插件推迟到 v1 之后，都不改 spec 的方向与四条主张。三条都列进收口报告的
  「可推翻的决定」。
- 2026-10-03 阶段一（T1–T5）在第二轮评审进行期间实现，评审没有挡住阶段一的裁决（涉及阶段一的三条 Medium 与四条 Low
  已修进代码与本文）；阶段一作为一次提交落地，不再拆成五个，因为五个任务改的是同一批文件里交错的行。

- 2026-10-03 第三轮评审（0 Blocking、1 High、5 Medium、9 Low；第二轮的 Blocking 与 High 全部解决，Medium 9 条已解、
  5 条部分解）逐条修进本文：第三方工具结果的收紧挪进内核执行器、按工具名（High 1）；内联提示词探测路径时崩溃（M1，
  已修进 T7 的代码）；工作区内 `impl:` 的摘要只盖一个文件（M2）；回放的检查次序（M3）；`modules:` 的解析挪进 T9a
  （M4）；Agent 能替自己确认信任（M5）；以及九条 Low。
- 2026-10-03 阶段一、二的对抗评审（阶段一：0 Blocking、1 High、3 Medium、6 Low；阶段二：0 Blocking、1 High、
  3 Medium、6 Low）修进代码：D7 只盖住四条装工具的路里的两条——工厂与插件 `register` 期间直接装工具改为拒绝、类式角色
  的构造函数装的工具同样查；同名替换、多个工具、多个 Module 的检查补上用例；spawn 失败时一并还原 Runtime 级的 hook；
  建在别处的 Agent 的 Module 层同样标成「服务单个 Agent、属于 Runtime」，建在 Runtime 中心本身上的直接拒绝（评审
  原建议一律拒绝建在别处的 Agent，那会推翻 spawn 已承诺的「留着自己的 id」行为，改为标记）；按角色的策略记录让任何一个
  构造失败的角色挡掉整个 run——改为在 try 里、每个角色只建一次；`openai:` 让项目配置把角色送到可能付费的端点——
  改为要用户自己的环境选了它；以及提示词的符号链接、按角色收紧在拓扑与 chat 里的落地、回放的角色名检查。
- 2026-10-03 第四轮评审（只核对修订部分：0 Blocking、0 High、3 Medium、6 Low）修进本文：收紧点挪到 `execute` 一返回
  之时，skip 与改写按 hook 的作者判（N1）；归属只来自交回的映射、`install_own_tools` 不得替换 Module 工具（N2，直接
  装工具的拒绝已随阶段一评审实现）；信任摘要按 import 系统自己的次序找代码、摘整棵包的每个文件、每个进程新的
  `pycache_prefix`、工作区里的 `sys.path` 条目如实展示、工作区内发行版按 `RECORD` 核对（N3）；以及名单校验、CRLF、
  开发期记录器的位置、多行展示的缩进、信任存储那句措辞、`replay_safe` 与环境、回放会真的执行 Module 工具。

## 风险与遗留（开工前登记）

- **同进程的扩展没有沙箱。** 归属规则挡误用不挡恶意；能力声明不强制。第三方 Module 的代码拿得到用户的完整环境、
  能绕过工具层与开发期守卫，`AGENTS.md` 硬约束 #7（环境白名单）与 #9（Agent 不执行 git 写）管不到它们（评审 H5）；
  信任确认是唯一的人工闸，它与文档都如实说。真正的边界是 M5 的沙箱。
- **第三方 Module 碰不到 Portal。** 要读写文件的第三方工具只能绕过工具层（路径监狱与开发期守卫都管不到）；Portal
  交给 Module 归 M5。
- **两处偏离 spec 的裁决可推翻**：D1 改写 spec 错误处理表那行「Module 启停钩子失败只记录日志」（改为一律响亮）；D4 把
  spec §4.7 槽的 `plugins` 定为 v1 不做。都写进收口报告的「可推翻的决定」。
- **信任摘要不盖工作区之外的代码。** 工作区之外的 `impl:` 模块与已安装发行版（含可编辑安装）的内容不进摘要（只进
  目标字符串与版本），信任一份配置等于信任它加载的代码；展示里写明。工作区之内的 `impl:` 源文件进摘要。
- **信任确认看不到能力。** 能力要 import 才知道，而确认之前不跑第三方代码；展示指向 `modules show`。
- **`on_start` 的副作用不补偿。** 后面的 Module 失败时，前面已 start 的不会被叫停（D3）；第三方应把副作用放进服务
  的惰性方法里。
- **工具次序变了。** 拓扑 run 的请求字节变了（D2）；此前没有拓扑 run 的回放文档，不影响已有记录。
- **trace schema 加一。** 旧版本 syndica 读不进 `/2` 的 trace，会报「升级」而不是「损坏」（M3 已有）。
- **信任确认改变了首次使用的体验。** 自动发现的配置第一次要确认；脚本与 CI 先跑 `syndica config trust --sha256
  <摘要>`，或用工作区之外的 `--config`（工作区之内的显式 `--config` 同样要确认）。
- **spawn 之后装上的工具不收紧。** 第三方代码能留住 Agent 的引用、在 spawn 之后再装工具，那些工具按宿主的算，结果
  不收紧、不过 D7。同进程的代码挡不住这一条，真正的边界是 M5 的沙箱。
- **根结束之后平级 Agent 再给它发消息，回放可能与原始 run 分叉。** 原始 run 在根结束后立刻关机，那条消息的
  激活可能被拒；回放的收尾先让剩下的 Agent 跑完，于是根被叫醒。拓扑 run 能被捕获之后这条路可达（M3 登记）。
- **工作区里的 `sys.path` 条目在任何闸之前运行。** syndica 装在工作区里的 `.venv`（uv 的缺省）时，Agent 写得到那里的
  `.pth` 与已装的代码，下一次启动就在信任确认之前、带着完整环境运行；工作区内 `impl:` 包 import 的其它顶层模块也不在
  摘要里。摘要盖不住这些，信任展示逐条列出这样的条目；把 syndica 装在工作区之外才真正避开。
- **参数校验失败的结果里带着工具自己的参数说明。** 第三方工具的说明是一条注入通道，溯源看不见它；它们的结果按上面的
  规则收紧，其余的说明文本（工具清单里的描述）照常进请求。
