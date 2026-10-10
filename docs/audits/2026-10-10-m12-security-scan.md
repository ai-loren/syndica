# 里程碑 12 依赖安全扫描

日期：2026-10-10
范围：[M12 计划](../plans/2026-10-10-m12-release.md) C 线「依赖安全扫描（Python 与前端）」；`uv.lock`（全部 extras）与
`web/package-lock.json`。两份锁自 `9096d49` 起没有改动，本文的结论对 C 线分支上的每个提交都成立。
方法：[`scripts/audit_deps.sh`](../../scripts/audit_deps.sh)。CI 的 security job 与 `scripts/local_ci.sh` 的 security 格
跑的是同一个脚本；本次在本机（macOS arm64）跑。

## 结论

没有已知漏洞，也就没有要升级或要解释的条目，锁没有改。

| 来源 | 扫了什么 | 工具 | 结果 |
|---|---|---|---|
| `uv.lock` | 128 个锁定版本（126 个包；`numpy`、`networkx` 按 Python 版本各锁两版），含全部 extras | pip-audit 2.10.1，`--strict`，PyPI 漏洞源 | No known vulnerabilities found |
| `web/package-lock.json` | 213 项：209 项只在开发期用（构建、类型、lint、Playwright），4 项进运行时产物（`react`、`react-dom`、`scheduler`、`@xterm/xterm`） | npm 11.19.1 `npm audit --audit-level=high` | found 0 vulnerabilities（各级别都是 0） |

## 为什么扫描前去掉环境标记

`uv export` 导出的是跨平台的锁：只在 Linux 上装的 CUDA 轮子（torch 带进来的 `nvidia-*`、`cuda-*`、`triton`）、只在
Windows 上装的 `colorama`、只在 Python 3.13 以下装的 `aiologic` / `culsans`，以及 `numpy` / `networkx` 按 Python 版本分开
的两版，都带着环境标记。pip-audit 按它自己那个解释器求值标记，不满足的**静默跳过**：直接扫导出的文件，128 个只扫了 103
个，而输出照样是「No known vulnerabilities found」。所以脚本先去掉标记，再把同名的第 n 个版本放进第 n 个文件（pip-audit
拒收同一个文件里同名的两行），每个文件用 `--no-deps --disable-pip` 扫；`--strict` 让任何一个扫不了的包直接失败，而不是
被跳过。

## 处置

无。CI 的 security job 在每次推送与 PR 上重跑：新公布的漏洞会让一个与依赖无关的 PR 变红，这是有意的（它是发布门禁）。
届时能升就升（`uv lock --upgrade-package <包>`、在 `web` 里 `npm update <包>`）并让门禁保持全绿；不适用的在这里另起一份
当期记录写明理由。

## 没有覆盖的

- 构建后端 `hatchling` 不在 `uv.lock` 里（`pyproject.toml` 的 `[build-system] requires = ["hatchling"]` 没钉版本），每次
  `uv build` 解析当时最新的一版，本次没扫。钉不钉归 B 线（`pyproject.toml`）。
- `uv tool run` 跑的工具（`twine==7.0.0`、`pip-audit==2.10.1`）只钉了顶层版本，它们自己的依赖每次重新解析；它们不进发行包。
- workflow 里的 action 按完整发布标签钉（`@v7.0.1`），不是按提交 SHA：标签被改指之后，CI 会以只读令牌跑另一份代码。这是仓库
  现行的约定（`tests/test_ci_workflow.py` 钉的是标签格式），改成 SHA 是另一项决定。
- Node、Python 解释器、Playwright 下载的浏览器不在这两个扫描器的范围里。
- 漏洞库随时间变化，本文的结论只对 2026-10-10 成立。
