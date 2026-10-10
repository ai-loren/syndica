# M12 发布验收记录（线 E：demo 与 E2E 验收）

日期：2026-10-10
分支：`m12-line-e-acceptance`（基于 `6ca5a64`）
计划：[`docs/plans/2026-10-10-m12-release.md`](../plans/2026-10-10-m12-release.md) 线 E；路线图
[「M12 发布准备 / E2E 验收内容」](../plans/2026-09-23-v1-milestone-roadmap.md)

## 结论

合入主线、修掉 B1 与 B3 之后（`9bc819a`），在主线上从构建出的 wheel 重跑 `sh scripts/acceptance.sh`，四项全部通过：

| 项 | 主线 `9bc819a` |
| --- | --- |
| (a) 新机器照 README 五分钟内完成 CLI run | PASS，95.4 s（容器启动到跑完 89.5 s，上限 300 s） |
| (b) Web UI：多 Agent 任务、人工确认、查看产物 | PASS，4.6 s（委派、在页面上批准、预览产物） |
| (c) MCP / OpenAI SDK 连本地 server 完成工具调用 | PASS，2.2 s（客户端工具往返、MCP 列工具与 Read、该不可信处标不可信） |
| (d) 跑一批任务并导出训练数据 | PASS，3.0 s（裁决 pass / block / partial / unverified；导出在工作区之外；密钥不在导出里） |

下面是线 E 在自己分支上的记录，保留原样。

| 项 | 本分支（`530a0d7` 构建的 wheel） | 只加 `httpx<1` 上限的同一棵树 |
| --- | --- | --- |
| (a) 新机器照 README 五分钟内完成 CLI run | **FAIL**：照 README 的 `pip install --pre` 装到 `httpx 1.0.dev6`，`syndica run` 每次调模型都失败（问题 B1） | PASS，容器启动到跑完 105.7 s（另一次 7.4 s） |
| (b) Web UI：多 Agent 任务、人工确认、查看产物 | PASS，4.2 s | PASS，5.2 s |
| (c) MCP / OpenAI SDK 连本地 server 完成工具调用 | PASS，2.3 s | PASS，2.2 s |
| (d) 跑一批任务并导出训练数据 | PASS，2.8 s | PASS，2.9 s |

(a) 的失败是产品问题，修法在 `pyproject.toml` 或 README，二者不在线 E 的范围里，按要求只报告不改（见 B1）。
右列是在本分支的一个一次性克隆里只改 `pyproject.toml` 两行（`openai` 与 `otel` 两个 extra 的
`httpx>=0.27` → `httpx>=0.27,<1`）后，用同一个 `scripts/acceptance.sh` 跑的完整结果，证明这一处是 (a) 唯一的阻塞。

验收用的模型是 `tests/e2e/fake_openai.py` 的拷贝（测试夹具，不进 wheel）。真模型模式没有跑：环境里没有
`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`，也没有 `GEMINI_API_KEY`。

## 环境与版本

- 宿主：macOS 26.6（arm64），uv 0.12.19，宿主 venv 的 Python 3.12.14（uv 管理）。
- 容器：Colima 0.10.3 上的 Docker 29.5.2（linux/arm64），镜像 `python:3.12-slim`（Python 3.12.15、pip 25.0.1）。
- 前端：Node v26.10.0，Playwright 1.63.0（Chromium 1246）。
- 被测 wheel：`syndica-1.0.0rc1-py3-none-any.whl`，本分支 sha256
  `a391e61fb09c9457f17a08703a9703e53df5482295c9f74d9ea3b58a017b4f22`（`uv build` 到私有目录，
  `scripts/check_dist.py` 通过）；加上限的克隆 sha256 `433fc8de…16cbe0`（只差 METADATA 里的那条约束）。
- (b) 的 server venv 解析到 starlette 1.7.0、uvicorn 0.54.0、httpx 0.28.1、mcp 2.3.0、pydantic 2.14.0；
  (c) 的客户端 venv 是 openai 3.28.0、mcp 2.3.0、httpx 0.28.1，没装 syndica。

## 怎么跑

```bash
sh scripts/acceptance.sh                 # 四项都跑
sh scripts/acceptance.sh --only a,d      # 只跑其中几项
sh scripts/acceptance.sh --dist dist     # 用已经构建好的产物（照样过 check_dist）
```

`acceptance.sh` 检查 uv、docker、npx 在不在，然后用 uv 的 Python 3.12 跑 `scripts/acceptance_run.py`（只用标准库）。
产物、日志、截图留在 `--out`（缺省一个新的临时目录）里，`summary.json` 记每项结论、用时与说明。

## 各项做了什么

### (a) 新机器照 README 五分钟

1. `docker pull python:3.12-slim`（不计时，3.7–3.9 s；README 的前提是「有 Python 3.11–3.13 的 Linux 或 macOS」，
   镜像就是这台新机器）。
2. `docker create` 一个容器，`docker cp` 进去三样：wheel、`fake_openai.py`、假端点的剧本
   `scripts/acceptance_quickstart.json`（让 Worker 真的写出上手页说的 `poem.md`），以及从 README 生成的
   `run.sh`。不用挂载：Colima 只挂载 HOME。
3. `docker start --attach` 起计时，容器里依次：起假端点 → README「Install」的命令 → README「Quickstart」export 的
   变量 → README 里标了 `<!-- executed: quickstart -->` 的块，逐字。
4. 判据：每条命令退出 0；`syndica run` 的 stdout 有回答、stderr 有 `trace：.syndica/traces/…`、工作区里恰好一个
   trace；`poem.md` 存在；从容器启动到结束 < 300 s。

**对 README 做的替换，只有这一处**（`scripts/acceptance_readme.py` 断言恰好一处）：

| README 原文 | 容器里执行的 |
| --- | --- |
| `python -m pip install --pre 'syndica[openai]'` | `python -m pip install --pre '/accept/syndica-1.0.0rc1-py3-none-any.whl[openai]'` |

另外两件事不是对命令的替换，记在这里以免误读：

- README 的 export 块写的是占位（`https://api.example.com/v1`、`your-model-name`、`your-key`），README 自己说
  「指向任何 OpenAI 兼容端点」。容器里取它 export 的变量名（`OPENAI_BASE_URL`、`OPENAI_MODEL`、
  `OPENAI_API_KEY`），值指向容器里的假端点。与 `tests/test_docs.py` 的做法相同；真模型模式下这些值经 stdin 读入。
- executed 块里的 `syndica trace show <trace-id>`，占位按 README 说的办法填：`syndica trace list` 第一行的 id。

逐字执行的命令（与 `tests/test_docs.py` 执行的是同一批，`tests/test_acceptance_helpers.py` 钉住两者相等）：

```text
mkdir syndica-demo
cd syndica-demo
syndica doctor
syndica run --agent Worker "Write a four-line poem about version control to poem.md" --max-turns 10
syndica trace list
syndica trace show <trace-id>
```

**五分钟的测量**（`docker start` 到容器退出；每步时长由容器里的脚本记下）：

| 运行 | pip install | doctor | run | trace list / show | 容器合计 | 判定 |
| --- | --- | --- | --- | --- | --- | --- |
| 本分支 | 7.4 s | 0.2 s | 66.1 s，退出 1 | 没跑到 | 77.8 s（含建容器） | FAIL（B1） |
| 加上限，第一次（还没有逐步计时） | | | | | 7.4 s | PASS |
| 加上限，最终一次 | 103.5 s | 0.5 s | 0.5 s | 0.3 s / 0.5 s | 105.7 s | PASS |

pip 那一步全花在从 PyPI 下载上：最终一次 `pydantic_core`（2.0 MB）只有 45.6 kB/s，没有回溯解析。即使这样也在
五分钟的三分之一左右；README 命令本身（doctor 到 trace show）合计约 2 s。

### (b) Web UI

1. `uv venv` + `uv pip install '<wheel>[openai,server,mcp]'`，一个全新的 venv。openai 是模型客户端（上手页写明
   Web UI 要 openai 与 server 两个 extra），mcp 给 (c) 用。
2. 工作区是 `examples/web-multi-agent/workspace` 的拷贝（配置：`modules: [acceptance]` 带出产物索引；一个英文
   提示词的 Worker 替掉内置 Worker），假端点用这个 demo 的 `offline/script.json`。syndica 子进程只拿到干净的环境：
   独立的 HOME 与 `XDG_CONFIG_HOME`，不带调用者的 `SYNDICA_` / `OPENAI_` / `GEMINI_` 变量。
3. `syndica config trust --sha256 …`，`syndica up --no-open --api-token`，取登录链接与机器令牌。
4. Playwright 跑 `web/e2e/acceptance/web.accept.ts`（自己的配置，不走开发树的 globalSetup）：用一次性登录链接
   登录 → 选 Orchestrator 建会话 → 发 `prompt.txt` 那句话 → 出现「需要你答复」的卡片，写着
   `shell.path_outside_workspace` 与 `cp incident.md ../outbox/incident.md` → 点批准 → Orchestrator 的最终回答
   提到 incident.md → Agent 树里恰好两个（Orchestrator、Worker）→ 产物页点 incident.md，预览里有内容。
   三张截图：`web-1-confirmation.png`、`web-2-agents.png`、`web-3-artifact.png`（在运行目录的 `b/screenshots/`）。
5. 页面之外再核对：被批准的那次 `cp` 真的执行了（`outbox/incident.md` 存在）。
6. (c) 跑完之后 `syndica down`。

### (c) MCP / OpenAI SDK

对 (b) 那个 server，另建一个只装 `openai>=3.26,<4`、`mcp>=2.3,<3`、`httpx` 的客户端 venv，跑
`scripts/acceptance_clients.py`：

- OpenAI SDK：`/v1/models` 列出 Worker；带一个客户端工具 `lookup_order` 发 chat，第一轮 `finish_reason=tool_calls`、
  交回一次 `lookup_order` 调用；客户端带着同一个 `X-Syndica-Session` 把结果送回，第二轮 `finish_reason=stop`，
  回答里用到了结果（`captured`）。会话快照里这条客户端送回的工具结果恰好一块、标 `untrusted`
  （产品文档：OpenAI 兼容客户端送回的工具结果不可信）。
- MCP SDK：Streamable HTTP 连 `/mcp`（Bearer 是机器令牌），`tools/list` 缺省只有 `Read`、`Grep`、`Glob`；
  `Read logs/app.log` 读到文件内容，`_meta.trust` 是 `untrusted`。交给宿主之外调用方的结果一律收紧为不可信
  （`kernel/external_calls.py` 的 `call_external`），所以工作区打开时就在的文件经 MCP 读出来也是 `untrusted`；
  第一版用例按文件污点期望 `trusted`，是用例写错了，不是产品问题。

### (d) 批量与导出

1. 全新 venv 装 `'<wheel>[openai]'`；工作区是 `examples/training-export/workspace` 的拷贝（验收三层全开、打回次数为 0）。
2. 剧本是这个 demo 的 `offline/script.json`，再由 `scripts/acceptance_export.py prepare` 在 release-1.2 那个任务的
   最终回答里加上一个随机密钥 `sk-acceptance-quiz-…`；宿主环境里 `ACCEPTANCE_DEPLOY_TOKEN` 是同一个值。
3. `syndica batch examples/training-export/tasks.jsonl`：四个任务都 `ok`，裁决依次 `pass`、`block`、`partial`、
   `unverified`（最后一个的 Rubric 是只能人判的 judgment）。
4. `syndica export sft|preference|rl|trajectory --output <工作区之外>`，`acceptance_export.py check` 核对：
   每种格式 `include_unverified=false`、`dropped_unverified` 恰好 1 个（等于 batch 报出的 unverified 个数）、导出的行与
   manifest 的样本里都没有它；SFT / RL / trajectory 各 3 行，偏好 1 对（同一 Goal 与 Rubric 下 pass 对 block），
   RL 奖励 0.0 / 0.5 / 1.0；任何导出文件里都没有那个密钥，而 SFT 里有 `[REDACTED:secret.env_value]`
   （证明密钥真的流过、是被换掉的，而不是根本没出现）。

## 发现的问题

### B1（未修，超出线 E 范围）：README 的安装命令装上 httpx 的开发版，`syndica run` 无法调用任何模型

- 现象：干净的 `python:3.12-slim` 里照 README 运行 `python -m pip install --pre 'syndica[openai]'`，`--pre` 对**所有**
  依赖生效，pip 把 `httpx>=0.27` 解析成 `httpx 1.0.dev6`。那是一次重写：没有 `AsyncClient`、`HTTPError`、
  `TimeoutException`。`syndica doctor` 还是 ok，`syndica run` 每次调模型都以
  `AttributeError: module 'httpx' has no attribute 'TimeoutException'` 失败，退避重试约 66 s 后退出 1。
- 范围：`uv pip install --dry-run --prerelease allow` 显示 `openai`、`otel`、`a2a` 三个 extra 都会解析到
  `httpx==1.0.dev6`（`trl` 会解析到 pandas 3.1.0rc0 与 safetensors 0.9.0rc1）。`scripts/smoke_install.sh` 用不带
  `--pre` 的 `uv pip install`，所以测不到。
- 建议修法（两处都在线 E 之外）：`pyproject.toml` 里 `openai` 与 `otel` 的 `httpx>=0.27` 改成 `httpx>=0.27,<1`
  （`<1` 按 PEP 440 也排除 1.0 的预发布；a2a 走 a2a-sdk 自己的 httpx 约束，要另看）；README 与
  `docs/user/install.md` 的安装命令可改成 `python -m pip install 'syndica[openai]==1.0.0rc1'`：显式点名的预发布版本
  只对 syndica 放行预发布，不再波及依赖。只改第一处，(a) 就通过（上面的右列）。

### B2（已修，`ea038ca`）：没装 openai extra 时，连 `config trust` 都是一条裸 ModuleNotFoundError

- 现象：只装 `syndica[server,mcp]`（或基础包）时，`config trust`、`doctor`、`run` 全部报
  「期望: CLI host 完成当前命令 / 实际: ModuleNotFoundError: No module named 'httpx' / 下一步: 检查 Runtime host
  配置和错误日志后重试」。宿主每次都向 OpenAI 兼容 backend 要默认价格表，那次 import 带出 httpx。
- 修法（`src/syndica/__main__.py`）：backend 装不上时默认价格表是空表（建不出模型，就没有要查价的）；建模型时把
  失败的 import 换成三件套，下一步写明 `pip install 'syndica[openai]'`，与 a2a、acp 两条路径的做法一致。
- 用例：`tests/cli/test_main.py` 在 import 不到 httpx 的新解释器里跑 CLI（等于没装这个 extra）：`config trust` 照常
  报摘要并能信任；`doctor` 与 `run` 报出安装命令。修复前三条都红；两处守卫各自单独删掉都有用例变红。

### B3（未修，文档）：RELEASING.md 说四项都在干净的 Linux 容器里跑

实际只有 (a) 在容器里；(b)、(c)、(d) 在宿主上的全新 venv 里从同一个 wheel 跑（(b) 要浏览器，与本次任务的要求一致）。
RELEASING.md 不在线 E 的范围里，建议把第 5 节那句改成「(a) 在干净的 Linux 容器里，其余三项在全新的 venv 里，都从构建出的 wheel」。

### 顺带修的

- `examples/syndica-example-module`：`Greet` 的说明写在类的 docstring 上，而反射出的工具描述取 `execute` 的
  docstring，模型看到的描述是空的、参数也没有说明（线 D 发现）。挪到 `execute` 上，
  `tests/integration/test_example_module.py` 读模型实际收到的工具定义。

### 与本分支无关的既有失败

全量 `uv run pytest -q`（venv 是 Python 3.13.15）：11269 通过、11 失败。失败的 11 条（broker 取消、`kill_agent`、
background、tracing、符号链接环的配置错误、`runner/test_document` 的 JSON 报错文本）在未改动的 `6ca5a64` 上同样失败，
其中 JSON 那条是 3.13 改了 `json` 的报错措辞；属于 3.13 兼容，归线 C 的矩阵。

## demo 与离线检查

四个 demo 在 `examples/`，每个有英文 README、`.syndica.yaml`、输入、`run.sh`（缺省用环境里的模型，`--offline` 用假端点）
与 `offline/script.json`；文档页 `docs/user/demos.md` 已进导航。`tests/test_examples.py`：三个 CLI demo 原样跑
`run.sh --offline`，web demo 照它 README 的步骤走 HTTP API 与 RPC WebSocket（真浏览器那一遍就是上面的 (b)）；另查每个
demo 的配置能被真加载器读、没有文件被 `.gitignore` 挡在克隆之外（`*.log` 曾挡掉 web demo 的输入）。
真模型下的 demo 没有跑（没有 key）。

为了让 demo 与验收能编排多步流程，`tests/e2e/fake_openai.py` 加了可选的 JSON 剧本（`FAKE_OPENAI_SCRIPT`）；
不给剧本时行为与原来逐字相同。

## 门禁

`ruff format --check .`、`ruff check .`、`pyright`（0 错误）、`scripts/check_deps.py`、`scripts/check_links.py`、
`sh scripts/check_docs.sh`（strict 构建通过）、`tests/test_docs.py`（394 通过）、web 的 typecheck 与 lint、既有的
Playwright 套件（桌面与移动 28/28，用改过的假端点）都通过。全量 pytest 见上一节。线 C 的本地 CI 同时在这台机器上跑，
负载下 `tests/server/test_up_down.py` 的一条（`down` 之后端口不再应答）失败过一次，单独连跑三次与和 demo 用例一起跑都通过。

## 遗留

- B1 已在主线修掉（`9bc819a`）：直接 import httpx 的 backend 所在的 extra（`openai`、`otel`、`a2a`，以及 `dev`）
  封顶 `httpx>=0.27,<1`，MCP backend 用的是 MCP SDK 的 `httpx2`，不需要；`smoke_install.sh` 改为
  `--prerelease allow` 解析，照用户装 rc 的方式撞预发布依赖；一条用例把每个 import httpx 的 backend 映射到它的
  extra 并要求封顶。修完之后 (a) 通过（见结论）。
- B3 已改：RELEASING.md 写明 (a) 在容器里，其余三项在全新的 venv 里，都从构建出的 wheel。
- 真模型模式：有 key 时 (a) 与 (d) 会自动多跑一遍并报告；本次没有 key，没跑。
- 路线图的「所有 demo 都有录屏或截图证据」：验收每次在运行目录里写出 (b) 的三张截图，没有提交进仓库。
