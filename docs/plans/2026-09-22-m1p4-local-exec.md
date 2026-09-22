# 里程碑 1 · 计划 4：最小本地执行 + OpenAI 兼容 provider

> **执行者须知：本文件是需求，不是参考。** 逐字落地。设计裁决已经写死的，不要再选一遍。

**Goal:** 宿主给一个本地目录，Agent 就能在这个目录里读、写、精确替换、按名字找、按内容搜、跑命令，并且能把同一套对话发给一个 OpenAI 兼容的 HTTP 端点。内核仍然只有 pydantic 一个依赖。

**Architecture:** 工具不碰 `os` / `subprocess`。它们只调 `Portal`。`LocalWorkspace` 是内核里唯一允许创建子进程和读写磁盘的实现，根目录由宿主传入。厂商客户端放在 `syndica.backends`，由宿主注入，内核不 import 它。

**Tech Stack:** Python 3.11、pytest、pytest-asyncio、pydantic。backend 的可选依赖是 `httpx`，进 `optional-dependencies`，不进 `dependencies`。

**依据：** [`docs/decisions/2026-09-08-m1-scope.md`](../decisions/2026-09-08-m1-scope.md)（2026-09-08 拍板，不得再解释）、spec §5.6 工具表的文件与执行行、§5.7、§6.1–§6.3 的协议形状、§15 里程碑 1「最小本地执行能力」。完整的 git 版本层、worktree、快照隔离、写入冲突检测、伪终端是里程碑 5，不在本计划。

---

## 关闭什么、不要动什么

本计划收口时，台账里挂着「计划 4」的项必须按下面的处置落到证据上，不许整段挪给计划 5。

| 编号 | 处置 |
| --- | --- |
| D-09 | 扫描器和 `test_backend_importing_a_surface_is_caught` 已经在仓库里。Task 8 只在台账标关闭，证据指向该测试。禁止重写扫描器。 |
| D-10 | Task 6。发出去的工具 schema 不得含 `title` / `$defs` / `$ref`。 |
| D-11 | Task 9。改 broker 契约，关掉 `ok=True` 之后消息消失且零 ERROR 的窗口。 |
| D-12 | Task 7。契约测试钉住「timeout 从调用那一刻起算」。 |
| D-13 | `check_deps.py` 的 `read_text` 已经带 `encoding='utf-8'`。Task 8 只关台账。禁止为了这项再改读取方式。 |
| D-14 | Task 8。布局错误必须是退出码 2，违规必须是退出码 1。现有测试只断言非零，盖不住把 2 改成 1。 |
| D-15 | Task 8。违规输出必须含下一步动作那一句，测试要断言这句话，不能只断言退出码。 |
| D-16 | `check_deps.py` 里没有 `write_text`。Task 8 只关台账。 |
| D-17 | Task 8。`pyproject.toml` 里 pyright 那段注释只留约束，删掉「改写成另一种形式会怎样」的第二句。 |
| D-34 / D-36 / D-37 | Task 2–4。本批每个带文本参数的工具都声明 `__tool_command_args__`。不执行文本的工具声明 `()`。 |
| D-50 | Task 6。请求体只有 `role` / `content` / `tool_calls` / `tool_call_id`，键序就是这个顺序。 |
| D-53 | Task 7。三个 `UPDATE_*` 从 `syndica.kernel.contracts` 再导出。 |
| D-56 | Task 6。provider 自己在 `config.timeout` 内返回或抛错。内核不得套 `asyncio.timeout`。 |
| D-57 | Task 7。`AgentRegistry.update` 写入的是快照。 |

**明确不做（写进里程碑 5 或后续计划，本计划的任务里不许出现它们的实现）：**

- git 自动 init、worktree、快照隔离、乐观锁、共享模式的并发写入冲突检测
- 伪终端 / `Terminal` 工具
- `TodoWrite`、`TaskGraph`、`Deliver`、`GetBudget`、`Compact`、Skill 加载、背景化工具三件套
- Anthropic 原生客户端、重试 / 故障转移（计划 5）、`AskUser`（计划 6）
- `Runtime.start`、`Junction.RUNTIME_START`（D-58 仍关着）
- 把 `httpx` 放进内核 `dependencies`

**不要动：** 计划 3 的 `assemble` 签名、`_RUNTIME_SURFACE`、`_RUNTIME_INIT`、四个内置角色的工具名列表、`Delegate` 冷启动不占用 lazy 槽。给拓扑接上真实工具是「宿主传入 `tools=`」，不是改 `freeze` 去自己 new 工具。

---

## 先读

1. [`docs/decisions/2026-09-08-m1-scope.md`](../decisions/2026-09-08-m1-scope.md) 里「计划 4」那一行。
2. spec §5.6 文件与执行工具行、§5.7、§6.1–§6.3。
3. `src/syndica/kernel/llm/base.py` 里 `LLM.create` 与 `LLMConfig` 的 docstring（D-50 / D-56 的文字已经在，本计划补可执行证据）。
4. `src/syndica/kernel/tool.py` 的 `__tool_command_args__`。
5. `src/syndica/kernel/protocols/broker.py` 的 `remove_queue` 与 `wait_for_message` docstring，再读计划 2 移交的窗口描述（`docs/plans/2026-09-08-m1p2-control-plane.md` 里「`_report_abandoned_input` 与 `remove_queue` 之间」那条）。

---

## Global Constraints

1. 内核 `dependencies` 保持只有 `pydantic`。`httpx` 只出现在 `[project.optional-dependencies]` 的 `openai` 里，并加入 `dev`，让本仓测试不用额外安装。
2. `src/syndica/kernel/**` 不得 import `syndica.backends`、`syndica.cli`、`httpx`。`src/syndica/backends/**` 不得 import `syndica.cli` / `syndica.server` / `syndica.runner`，也不得 import 另一个 backend 包。
3. 内核不得读 `os.environ`、不得调 `os.getcwd`。根目录、shell、子进程环境都是构造参数。相对根目录直接 `ConfigError`，下一步写「传入绝对路径」。
4. 模型犯的错（路径越界、旧串不唯一、正则非法、命令超时）写进 `ToolResult.output`，三段式中文（期望 / 实际 / 下一步），不抛。编程错误（根目录不是绝对路径、portal 句柄不存在于本次构造）在构造或调用契约层抛 `ConfigError`。
5. 路径越界包含 `..`、根外绝对路径、以及解析后落到根外的符号链接。判定用解析后的真实路径是否仍在根下，不用字符串前缀硬拼。
6. 注释只写现在的约束。测试名就是断言，不在测试里写「曾经怎样」。

---

## 设计裁决

### 1. 谁碰磁盘

`Workspace` 只暴露 `root: str` 和 `portal() -> Portal`。`Portal` 的方法就是下面八个工具要用的那些，不多加：

| 方法 | 给谁 |
| --- | --- |
| `read_text(path, *, offset, limit) -> str` | Read |
| `write_text(path, content) -> None` | Write |
| `edit_text(path, old, new) -> None` | Edit |
| `glob_paths(pattern) -> tuple[str, ...]` | Glob |
| `grep_text(pattern, path) -> tuple[str, ...]` | Grep |
| `run(command, *, timeout, cwd) -> ExecResult` | Bash 前台 |
| `start(command, *, cwd) -> str` | Bash 后台，返回句柄 |
| `poll(handle) -> ExecResult` | BashOutput |
| `kill(handle) -> None` | KillBash |
| `close() -> None` | 停掉还活着的后台进程 |

`ExecResult` 是冻结数据类，字段顺序固定：`exit_code: int | None`、`stdout: str`、`stderr: str`、`truncated: bool`、`running: bool`。后台还在跑时 `exit_code is None` 且 `running is True`。

`LocalWorkspace(root, *, shell, env)` 三个参数都必填。`root` 是已存在的绝对目录。`shell` 是非空字符串，用作可执行文件（`[shell, '-c', command]`），不用 `shell=True`。`env` 是 `Mapping[str, str]`，每次启动子进程时复制一份，不读、不改进程自己的环境。

### 2. 工具怎么接到 Agent 上

`local_tools(portal) -> dict[str, Tool]`，键顺序固定为 `Read, Write, Edit, Glob, Grep, Bash, BashOutput, KillBash`。宿主把它传进 `catalog.freeze(..., tools=)` 或 `assemble(..., tools=)`。`freeze` 不调用这个函数，内核不知道当前有没有工作区。

八个类都在 `syndica.kernel.tools.local`。每个类：

- `__tool_name__` 与上表键一致
- 带字符串参数的，`__tool_command_args__` 不得保持默认 `None`（那会在执行前提前抛，杀掉这一轮）
- Read / Write / Edit / Glob / Grep / BashOutput / KillBash 声明 `()`。它们的文本不是要拿去判定的命令
- Bash 只声明 `('command',)`。`run_in_background` 不是命令文本

### 3. 各工具的行为

- **Read**：`path` 必填。`offset` 从 1 开始的行号，默认 1。`limit` 默认 2000 行。超出文件的 offset 把实际行数写进 output。单文件超过 1 MiB 拒绝读全文，output 里写实际字节数。
- **Write**：覆盖写。父目录不存在就建，但父目录必须仍在根下。
- **Edit**：`old_string` 在文件里出现次数必须正好是 1。0 次或多于 1 次都拒绝，output 里写实际次数。`old_string` 为空串拒绝。
- **Glob**：模式相对根。结果是相对根的路径，按字典序。不返回根外路径。
- **Grep**：Python `re`，不是外部 `rg`。非法正则写进 output。`path` 缺省时搜整棵根。每条结果是 `相对路径:行号:行内容`。
- **Bash**：`command` 必填，`timeout` 默认 30 秒且必须为正数。`cwd` 缺省为根，传入时也必须落在根下。超时杀进程组，output 写实际超时秒数。`run_in_background=true` 时立刻返回句柄，不等待。stdout 与 stderr 都放进 output，并标成模型可见的不可信文本（工具返回的 `ToolResult` 使用现有的不可信标记；没有该字段时只把文本放进 output，不新造协议字段）。
- **BashOutput / KillBash**：句柄必须是本 portal `start` 返回的。未知句柄写进 output。`close()` 之后再 poll，output 写「工作区已关闭」。

本地工作区读到的文件内容视为宿主打开的目录，Read 的结果按可信文本返回。Bash 的输出视为不可信。

### 4. OpenAI 兼容客户端放哪

包路径：`src/syndica/backends/llm_openai/`。类名 `OpenAICompatible`，实现 `LLM`。构造参数：`config: LLMConfig`，以及一个由测试注入的 `httpx.AsyncClient`（生产路径可以在未注入时用 `client_args` 里的 `base_url` / `api_key` 自己建，但单元测试必须能不联网）。

请求：

- URL 路径是 `{base_url}/chat/completions`，`base_url` 来自 `config.client_args['base_url']`，缺了抛 `ConfigError`，写明缺的键。
- body 的 `model` 来自 `config.model`。
- 每条消息只保留有值的 `role`、`content`、`tool_calls`、`tool_call_id`，键的出现顺序就是这个顺序。`extra`、`trust`、`artifacts` 不得出现在 JSON 里。
- 工具 schema 递归删掉 `title`、`$defs`，并拒绝仍含 `$ref` 的 schema（`ConfigError`，下一步写「展开 $ref 后再交给这个客户端」）。不在内核的 schema 生成里删，在这个 backend 发出去之前删。
- `httpx` 的超时等于 `config.timeout`。本类的源码不得出现 `asyncio.timeout`。超时映射成 `LLMResponseError`，`received` 里带上超时秒数。

`provider` 字符串本任务固定认 `openai-compatible`。其它字符串抛 `ConfigError`，列出收到的值。

### 5. broker 窗口（D-11）怎么关

现状：`publish` 在看见邮箱存在之后、把消息放进邮箱之前让出；另一边 `remove_queue` 把邮箱摘掉。`publish` 仍返回 `ok=True`，消息不在任何邮箱里，也没有 ERROR。

契约补丁（写进 `protocols/broker.py` 的 `publish` docstring，并加进 `MessageBrokerContract`）：

**返回 `ok=True` 的消息，在返回那一刻必须已经在目标邮箱里，并且 `remove_queue` 尚未把它丢弃。** 实现上：`publish` 把「检查邮箱存在」和「放入邮箱」放在同一次不让出的临界区里；`remove_queue` 不得在消息已经决定返回 `ok=True` 之后再丢掉它。内存实现用同一把锁（或等价的不让出区）包住这两步。远端实现的契约测试用一个会在 `remove_queue` 里挂起的假 broker 复现旧窗口，修完之后那条假 broker 若仍在放入之前让出，契约测试必须失败。

不改 `publish` 的返回类型。不把失败收成 `ok=True`。

### 6. 公开面

Task 8 才改 `syndica.kernel.__all__`。新增且只新增：`Workspace`、`Portal`、`LocalWorkspace`、`ExecResult`、`local_tools`。按字母序插入。

不导出：`OpenAICompatible`（从 `syndica.backends.llm_openai` 引入）、八个工具类本身、`parse_mapping`、`register_tool`、`resolve_tool_names`。

`syndica.kernel.contracts.__all__` 增加 `UPDATE_PROTECTED_FIELDS`、`UPDATE_PROTECTED_FIELD_OWNERS`、`UPDATE_WRITABLE_FIELDS`，字母序。

---

## 任务拆解

每个任务：先写失败测试，再写实现，再跑该测试文件、`ruff check`、`ruff format --check`、`pyright`（只针对改过的路径）。变异用改文件后跑指定测试再还原，还原后哈希必须与改前一致。不要用 `git checkout` 还原单个文件。

### Task 1: Portal、Workspace、LocalWorkspace

**Files:**
- Create: `src/syndica/kernel/workspace.py`
- Create: `src/syndica/kernel/local_workspace.py`
- Test: `tests/kernel/test_local_workspace.py`

**内容：** 协议、`ExecResult`、`LocalWorkspace` 的路径监狱。本任务实现 `read_text` / `write_text` / `edit_text` / `glob_paths` / `grep_text` / `close`。`close` 在还没有后台进程时是幂等空操作。`run` / `start` / `poll` / `kill` 的方法签名本任务就放在协议上，内存实现留到 Task 4，写在同一个 `LocalWorkspace` 里，不另起一个会碰磁盘的类。调用未实现的方法必须抛 `ContractViolation`，`next_action` 写明「Task 4 之前不要调用」。Task 4 删掉这句抛错。

**必写用例名：**

- `relative_root_is_rejected_without_calling_getcwd`
- `symlink_escaping_the_root_is_rejected`
- `dotdot_is_rejected_after_resolve`
- `write_then_read_roundtrips_inside_the_root`
- `close_is_idempotent`

**变异至少：** 把符号链接检查改成只看字符串前缀，`symlink_escaping_the_root_is_rejected` 必须变红。

### Task 2: Read、Write、Edit

**Files:**
- Create: `src/syndica/kernel/tools/local.py`
- Test: `tests/kernel/test_local_tools_files.py`

**内容：** 三个工具 + `local_tools` 先只放这三个键（Task 3、4 按固定顺序把其余键补上，最终顺序以设计裁决第 2 条为准）。`__tool_command_args__ = ()`。

**必写用例名：**

- `edit_rejects_when_old_string_matches_twice_and_leaves_the_file_unchanged`
- `edit_rejects_when_old_string_is_missing`
- `read_of_a_file_outside_the_root_returns_output_and_does_not_raise`
- `command_args_are_empty_so_prose_pipes_are_not_scanned`

最后一条把一段含 `| sh` 的文件内容交给危险判定扫描这些工具的参数，断言不会因为正文里的管道被当成命令。

**变异至少：** 删掉 `__tool_command_args__ = ()`，执行该工具必须在调用前失败（D-36），对应用例变红。

### Task 3: Glob、Grep

**Files:**
- Modify: `src/syndica/kernel/tools/local.py`
- Test: `tests/kernel/test_local_tools_search.py`

**必写用例名：**

- `glob_does_not_return_paths_outside_the_root`
- `grep_reports_an_invalid_regex_in_output`
- `grep_results_are_relative_and_include_line_numbers`

**变异至少：** Glob 改成跟随根外符号链接，第一条必须变红。

### Task 4: Bash、BashOutput、KillBash

**Files:**
- Modify: `src/syndica/kernel/tools/local.py`
- Modify: `src/syndica/kernel/local_workspace.py`
- Test: `tests/kernel/test_local_tools_bash.py`

**必写用例名：**

- `bash_timeout_kills_the_process_and_names_the_timeout`
- `background_bash_can_be_polled_then_killed`
- `unknown_handle_is_reported_in_output`
- `bash_command_is_the_only_scanned_argument`
- `close_kills_background_processes`
- `local_tools_keys_are_in_canonical_order`

`local_tools_keys_are_in_canonical_order` 断言键的元组等于设计裁决第 2 条的八个名字，顺序一致。Task 2、Task 3 不得写死「只有三个键 / 五个键」这种会被后续任务打脸的断言。

`bash_command_is_the_only_scanned_argument`：`command='echo ok'` 且另一个字符串参数如果存在也不得被扫描；危险判定看到的文本就是 `command`。用一条会命中现有 HIGH 规则的命令（例如 `rm -rf /`）断言扫描结果不是空。

**变异至少：** 把 `__tool_command_args__` 改成 `()`，HIGH 命令那条必须变红（命令不再被扫描）。

### Task 5: 真实工具跑通一条拓扑

**Files:**
- Test: `tests/integration/test_local_tools_topology.py`

**内容：** 只 import `syndica.kernel` 里已经公开的符号，加上 `local_tools` 所在模块（Task 8 之前允许测试从 `syndica.kernel.tools.local` 引入 `local_tools`）。`ScriptedLLM` 让 Worker 调用 `Write` 再调用 `Bash`，断言临时目录里出现文件，并且 Bash 的 stdout 回到工具结果。沿用计划 3 的拓扑夹具（Orchestrator + Worker），把桩工具换成 `local_tools(portal)` 里的真实工具。`Delegate` 仍然冷启动，不改成 `occupy`。

**必写用例名：**

- `worker_write_and_bash_touch_the_workspace`

**变异至少：** Write 写成根目录以外的固定路径，本用例必须变红。

### Task 6: OpenAI 兼容 provider

**Files:**
- Create: `src/syndica/backends/__init__.py`
- Create: `src/syndica/backends/llm_openai/__init__.py`
- Create: `src/syndica/backends/llm_openai/client.py`
- Modify: `pyproject.toml`（只加 optional 依赖）
- Test: `tests/backends/test_llm_openai.py`

**内容：** 设计裁决第 4 条。测试用 `httpx.MockTransport`，不访问网络。

**必写用例名：**

- `request_drops_extra_trust_and_artifacts`
- `message_keys_follow_role_content_tool_calls_tool_call_id`
- `schema_title_and_defs_are_stripped`
- `schema_with_ref_is_rejected`
- `timeout_sent_to_the_client_equals_config_timeout`
- `source_does_not_call_asyncio_timeout`
- `kernel_does_not_import_httpx_or_backends`

**变异至少：** 把 `extra` 放进 JSON，第一条必须变红。把 `asyncio.timeout` 包住请求，`source_does_not_call_asyncio_timeout` 必须变红。

### Task 7: 注册表快照、契约再导出、等待超时起算点

**Files:**
- Modify: `src/syndica/kernel/contracts/__init__.py`
- Modify: `src/syndica/kernel/stores/memory_registry.py`（只在 `update` 尚未拷贝嵌套容器时）
- Modify: `src/syndica/kernel/contracts/broker.py`
- Test: `tests/kernel/test_registry_update_snapshot.py`（若已有同名文件则追加到已有 registry 测试文件，禁止再开一个并行测试文件）
- Test: 追加到已有的 broker 契约测试文件

**内容：**

- `from syndica.kernel.contracts import UPDATE_WRITABLE_FIELDS, UPDATE_PROTECTED_FIELDS, UPDATE_PROTECTED_FIELD_OWNERS` 必须成功，且与 `protocols.registry` 是同一对象。
- `update` 写入 `metadata` 之后，调用方再改自己传入的 dict，`get` 看到的不得变。
- 契约：邮箱里已有一条不匹配的消息，等待期间再来一条不匹配的，`timeout` 不得被顺延。用假时钟或很短的 timeout 钉住总时长。

**必写用例名：**

- `update_stores_a_snapshot_of_metadata`
- `contracts_reexports_the_three_update_sets`
- `wait_timeout_is_not_extended_by_nonmatching_messages`

**变异至少：** `update` 直接保存调用方传入的 dict，第一条必须变红。

### Task 8: 依赖门禁缺口、注释、公开面、关台账

**Files:**
- Modify: `tests/test_deps.py`
- Modify: `pyproject.toml` 注释（D-17）
- Modify: `src/syndica/kernel/__init__.py`
- Modify: `tests/kernel/test_public_api.py`
- Modify: `docs/decisions/2026-09-14-deferred-items.md`

**内容：**

- `test_missing_root_exits_2` 与 `test_violations_exit_1`：分别断言退出码等于 2 和等于 1。现有「非零」用例保留。
- 抽一条已有违规用例，断言 stdout 含「改为」或该条消息里已经写好的下一步句子。断言的是现有文案，不改文案去凑测试。
- D-09 / D-13 / D-16 在台账标关闭，各写一行证据（测试名或文件行），不改对应实现。
- D-14 / D-15 / D-17 随本任务关闭。
- 公开面按设计裁决第 6 条。`test_public_api.py` 里用字面量元组列出新增名字，禁止从 `__all__` 推导期望值。

**必写用例名：**

- `test_missing_root_exits_2`
- `test_violations_exit_1`
- `kernel_exports_workspace_types_and_local_tools`

**变异至少：** 布局错误改成 `return 1`，`test_missing_root_exits_2` 必须变红。

### Task 9: publish 与 remove_queue 的回执窗口

**Files:**
- Modify: `src/syndica/kernel/protocols/broker.py`（docstring）
- Modify: `src/syndica/kernel/stores/memory_broker.py`
- Modify: `src/syndica/kernel/contracts/broker.py`
- Test: 追加到已有 broker 契约测试

**内容：** 设计裁决第 5 条。先写会失败的契约测试：假 broker 在确认邮箱存在之后让出，再 `remove_queue`，然后 `publish` 返回。修复前这条必须能观察到 `ok=True` 且随后 `consume` 为空。修复后内存实现这条为绿；假 broker 若仍在放入前让出，契约测试判定该实现对契约违约（测试失败信息写明期望的临界区）。

**必写用例名：**

- `publish_ok_means_the_message_is_still_in_the_mailbox`

**变异至少：** 把「放入邮箱」移出临界区、在 `await` 之后再放，本用例必须变红。

---

## 收口

九个任务都落地之后：

1. 台账中上表列出的编号全部标成已关闭或写明证据，不得再标「计划 4」。
2. 重写 `docs/plans/2026-09-21-session-handoff.md`：计划 4 收口，下一步是写计划 5。
3. 提交，不推送。推送留到里程碑 1 审计。
