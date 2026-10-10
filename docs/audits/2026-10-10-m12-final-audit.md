# 里程碑 12 收口审计：发布准备

日期：2026-10-10
范围：[M12 计划](../plans/2026-10-10-m12-release.md)，对照路线图「M12 发布准备」的 TODO、静态验收与 E2E 验收，按用户选定的
D1–D4 调整（本月本地验证、托管矩阵下月作发布门禁；只支持 Linux 与 macOS；只英文化用户文档；单包加 extras）
验证方式：本地验证协议。五条线（A 公开 API、B 打包与仓库规范、C CI、D 文档、E demo 与验收）在各自的 worktree 里并行做，
逐条合入主线，合入时的接缝在主线上修。发布动作（推送、发 PyPI、设仓库公开、打 v1.0 标签）都没有做，等维护者确认。

## 结论

Syndica 1.0.0rc1 具备发布条件：公开面冻结并由快照守着，英文用户文档成站且严格构建，包的内容与元数据由脚本检查，
本地矩阵在 3.11 / 3.12 / 3.13 × macOS / Linux 上全绿，从构建出的 wheel 跑的四项 E2E 验收全部通过。准备过程中
在多平台、多版本与真实安装路径上查出并修掉了 9 个产品缺陷（见下）。剩下的是维护者的发布动作、下月托管 runner 上的
完整矩阵，以及两项 GitHub 仓库设置。

## 路线图 TODO × 证据

| TODO | 证据 |
|---|---|
| 英文化 README、quickstart、concepts、API reference、troubleshooting | `README.md`；`docs/user/` 下 32 页：index、install、quickstart、8 篇 concepts、11 篇 guides、reference（CLI、配置、flags、HTTP API、Python API、错误）、troubleshooting、security、versioning、demos |
| 文档站：安装、五分钟上手、核心概念、四个 Protocol 契约、编写 Agent / Tool / Backend | `mkdocs.yml`（MkDocs Material）；`sh scripts/check_docs.sh` 以 `--strict` 构建，导航缺页或断链即失败（把 `errors.md` 挪走时构建与用例都红）；契约与扩展见 `guides/writing-a-backend.md`、`writing-an-agent.md`、`writing-a-tool.md`、`writing-a-module.md` |
| LICENSE、CONTRIBUTING、CODE_OF_CONDUCT、SECURITY、issue / PR 模板 | 根目录与 `.github/`；`tests/test_release_files.py` 钉住文件存在、SECURITY 的两条硬声明逐字出现（不提供多租户隔离；同进程第三方代码无法沙箱化） |
| 固化 v1.0 公共 API 清单：导出名、切点名、Store Protocol、Flag key、YAML schema、HTTP API | `tests/testdata/public_api.json`（各公开模块的 `__all__`、签名、类成员、枚举、切点名、Flag key、`.syndica.yaml` 键与清单字段、CLI 命令与选项、HTTP API 快照摘要），`tests/test_public_api_snapshot.py` 逐字节比对；英文参考页 `docs/user/reference/api.md` 由快照生成，每个公开名字要有描述 |
| CI 矩阵：Python 3.11 / 3.12 / 3.13，Linux / macOS / Windows | 按 D2 去掉 Windows（文档指向 WSL2）。`.github/workflows/ci.yml`：ubuntu / macos × 3.11–3.13；`tests/test_ci_workflow.py` 钉住矩阵、job 与锁定安装，并与 `scripts/local_ci.sh` 一致 |
| 前端 CI：lint、typecheck、build、Playwright | `ci.yml` 的 web job：`check_web.sh`、Playwright（`check_e2e.sh`），再用 `check_web_reproduced.sh` 确认提交里的前端产物与构建逐字节相同 |
| 包发布：wheel / sdist、extras、server 静态资源、签名 / 校验 | `scripts/check_dist.py`（wheel 带 `py.typed` 与 `server/static`，不带 tests / docs / web；元数据、入口点、extras；写 `SHA256SUMS`）；`twine check --strict`；`scripts/smoke_install.sh` 对每个 extra 在干净 venv 里装 wheel 并导入；构建可复现。签名：发布走 PyPI 的 trusted publishing（`RELEASING.md`），GitHub Release 附 `SHA256SUMS`；没有另做 GPG 签名 |
| 迁移与兼容策略：semver、弃用周期、破坏性变更流程 | `docs/user/versioning.md`；`CHANGELOG.md` 的「Breaking changes before 1.0」列出冻结前撤下的名字 |
| demo：CLI 自举、Web 多 Agent、产物验收、训练导出 | `examples/` 下四个目录，各有 README、配置、输入、`run.sh`（真模型或 `--offline`）；`tests/test_examples.py` 离线跑；`docs/user/demos.md` |
| B-06、M-3、M-6 / M-7 | 见路线图遗留表（M12 已处理）：错误分类写进 `errors.md` 并由用例钉住层级；`provenance_of` / `strictest_trust` 撤下；spec §4.2 / §4.3 按实现改写 |
| CHANGELOG、PyPI 预发布与版本策略 | `CHANGELOG.md`（1.0.0rc1）；`RELEASING.md`（TestPyPI 再 PyPI，维护者点头）；版本 `1.0.0rc1` 只写在 `pyproject.toml` |

## 静态验收 × 证据

| 验收 | 结果 |
|---|---|
| `pip install .`、`.[openai]`、`syndica-server` 的 smoke | 按 D4 换成 `syndica[server]`。`smoke_install.sh`：base 与 openai / server / mcp / acp / a2a / otel 七个目标全部通过（`trl` 用 `--with-trl` 跑过一次，905 MB）；验收 (a) 在干净容器里照 README 从 wheel 装 `[openai]` |
| 多 Python / OS CI 全绿 | 本月本地矩阵（D1）：见「验证」；托管 runner 下月作发布门禁 |
| docs build 无断链；README 代码块可执行或明确标记 | `check_docs.sh` 严格构建；`check_links.py`；`tests/test_docs.py`：shell 块里的 `syndica ...` 走真解析器、yaml 走真加载器、python 块离线真跑、标 `executed` 的 quickstart 对着假端点真跑，其余块必须写不测的理由 |
| 包元数据、license、classifiers、`py.typed`、类型信息 | `check_dist.py` 与 `test_release_files.py`：SPDX `MIT`、16 个合法 classifier、只声明 Linux 与 macOS、`Typing :: Typed`、`py.typed` 在 wheel 里 |
| public API snapshot 与 v1.0 freeze 清单一致 | `test_public_api_snapshot.py`；快照的变化即公开面的变化，由人看 diff |
| 依赖锁与安全扫描无高危未解释项 | [安全扫描](2026-10-10-m12-security-scan.md)：pip-audit 扫 128 个锁定版本（全部 extras、各平台），npm audit 213 项，都是 0 个漏洞 |

## E2E 验收 × 证据

[验收记录](2026-10-10-m12-acceptance.md)：`sh scripts/acceptance.sh` 只从构建出的 wheel 跑，模型是测试夹具里的假端点。
主线 `9bc819a` 上四项全部通过，最终代码 `32656dc` 上再跑一次也全部通过（见「验证」）：

| 验收 | 结果 |
|---|---|
| 新机器从 README 开始，五分钟内完成一次 CLI run | 干净的 `python:3.12-slim` 容器照 README 的安装与 quickstart 命令（只把包源换成拷进去的 wheel），从容器启动到 `syndica run` 交回答案并留下 trace 89.5 s |
| Web UI：多 Agent 任务、人工确认、查看产物 | 全新 venv 里 `syndica up`，Playwright 驱动页面：Orchestrator 委派给 Worker、在页面上批准确认、打开产物预览 |
| MCP / OpenAI SDK 连本地 server 完成工具调用 | `openai` SDK 的客户端工具往返；MCP SDK 在 `/mcp` 上列工具并 `Read` 工作区文件；结果在产品写明处标不可信 |
| 跑一批任务并导出训练数据 | `syndica batch` 产出 pass / block / partial / unverified；`syndica export` 的 sft / preference / rl / trajectory 写在工作区之外；unverified 默认排除；环境里的密钥不在任何导出文件里 |

## 准备过程中修掉的产品缺陷

| 缺陷 | 怎么查出来的 | 提交 |
|---|---|---|
| `AcceptanceModule` 自己装的 `Deliver` 不认处理器声明的类别 | 线 A 公开 `ArtifactHandler` 时 | `cd399c2` |
| 不装 `openai` extra 时连 `syndica config trust` 都以裸的 `ModuleNotFoundError` 退出 | 线 E 在只装 `[server,mcp]` 的环境里 | `f35df6a` |
| README 的 `pip install --pre` 装进 httpx 1.0 预发布重写版，所有模型调用失败 | 验收 (a) 在干净容器里照 README 装 | `9bc819a` |
| 关闭 Web 里人的终端会 SIGKILL 掉 server 自己（Linux 每次） | 本地矩阵的 Linux 格 | `ada4936` |
| Linux 粗粒度 ctime 让文件污点漏判同长度改写 | Linux 格 | `0f17969` |
| 3.12+ 上 `Runtime.wait` 在 kill 的收尾做完之前就返回，紧接着的 `send` 丢消息 | 3.12 / 3.13 格 | `d4b2841` |
| kill 收尾期间收到的消息被丢掉（`KillAgent` 却告诉模型一条消息就能复活它） | 同一个窗口在负载下被撑大 | `1ed3771` |
| 3.13 上符号链接环的报错退化成「不存在或不是文件」 | 3.13 格 | `20e1f49` |
| 退出时导出过程中按 Ctrl-C 以 Traceback 收场 | 3.12 格（基线上 30 次复现 1 次） | `02c6a9c` |

另有若干只依赖某个解释器调度或计时的用例改成了跨版本成立的写法（`331c886`、`f6c798a`）。

## 合并接缝

五条线从同一个计划提交出发，合入时在主线上修了这些：线 B 加的 `--version` 让线 A 的 CLI 快照多一行（重新生成，
diff 只有这一行）；线 D 的导航与白名单在线 A、B 的页面到位后恢复严格；线 A 生成的参考页按线 D 的规则给签名块标
不测，生成器把 dataclass 默认工厂写成 `<factory>`；线 E 查出的 httpx 问题在 `pyproject.toml` 修掉，`RELEASING.md`
改成验收的真实做法。

## 明确不做（计划复核）

- Windows 支持（D2）；运行时信息的英文化与本地化（D3）；拆分发行包（D4）。
- 多 Agent 子树持久化（v1 之后）、回放文档 v4 与其余标「后续」的遗留项。
- Module 服务消息与宿主消息的区分：归 v1.x，提议仍要人 `sessions activate`，安全结论不变。

## 还没做

| 条目 | 原因 |
|---|---|
| 托管 runner 上的完整矩阵 | 本月没有 GitHub Actions 额度（D1）；`ci.yml` 已写好，下月跑一次作为发布门禁。本地 Linux 格是 arm64，x86_64 Linux 由托管矩阵覆盖 |
| 推送、发 TestPyPI / PyPI、设仓库公开、打 v1.0 标签 | 对外且不可逆，等维护者确认 |
| GitHub 仓库设置：Private vulnerability reporting、Reported content | SECURITY.md 与行为准则的举报渠道依赖它们，要维护者在设置里打开 |
| 真实模型下的 E2E 验收 | 环境里没有 key；`acceptance.sh` 有 key 时会对真实模型重跑 README 那项与批量导出，key 只经 stdin 进容器 |
| 文档站的公开托管 | 仓库公开之后再接 GitHub Pages；现在 `Documentation` 指向仓库里的 `docs/user` |

## 验证

本地矩阵（`scripts/local_ci.sh`，每格在导出的提交上从干净环境开始；五条线全部合入之后）：

| 格 | 提交 | 结果 |
|---|---|---|
| mac-3.12 | `30879b3` | 11309 passed，7 skipped，12m04s |
| mac-3.13（装 trl） | `30879b3` | 11316 passed，16m25s |
| linux-3.12 | `30879b3` | 11305 passed，11 skipped，8m44s |
| linux-3.13 | `30879b3` | 11305 passed，11 skipped，8m33s |
| web | `30879b3` | Playwright 28 / 28，前端产物逐字节复现 |
| package | `30879b3` | `uv build`、`twine check --strict` 通过 |
| security | `30879b3` | 0 个漏洞 |
| linux-3.11（含 ruff、pyright） | `32656dc` | 11305 passed，11 skipped，9m05s |
| mac-3.11 | `32656dc` | 11309 passed，7 skipped，14m15s |

`30879b3` 上 linux-3.11 与 mac-3.11 两格失败，都是线与线之间的接缝：线 E 的用例静态 `import trl`，而 pyright 那一格不装
`trl`；`test_up_down` 的登录请求只给 5 秒，两格并行满载时刚起来的 server 应答更慢。`32656dc` 只改了这两个测试文件，
重跑两格通过；其余七格的结论对 `32656dc` 同样成立（源码没有变化）。Linux 那几格跳过的 11 条是没装 trl 的 TRL 用例与
容器里没有 `gh`、文件系统区分大小写这类环境用例。

E2E 验收（`sh scripts/acceptance.sh`，从 `32656dc` 构建的 wheel）：四项全部通过，README 那项从容器启动到跑完 8.2 s
（pip 下载命中缓存；`9bc819a` 上不带缓存的那次 89.5 s），Web 6.6 s，MCP / OpenAI SDK 3.5 s，批量与导出 5.0 s。

文档：`sh scripts/check_docs.sh` 严格构建通过；`tests/test_docs.py` 全部通过。之后只有文档提交。
