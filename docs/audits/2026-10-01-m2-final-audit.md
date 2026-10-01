# 里程碑 2 收口审计：自举

日期：2026-10-01
范围：[M2 计划](../plans/2026-09-30-m2-self-hosting.md) 的 T1–T18；T1–T14 另有
[阶段一审计](./2026-10-01-m2-phase1-audit.md)与[阶段二审计](./2026-10-01-m2-phase2-audit.md)，本文复核它们之后的
T15–T18 并收口整个里程碑
基线：T1 `ce625ce`、T2 `49c867a`、T3 `23cdaa1`、T4 `2f76738`、T5 `e5974a6`、T6 `6232d2b`、T7 `6608fd5`、T8 `643d68b`、
T9 `6a0a647`、T10 `e6362c3`、T11 `cdc5d6d`、T12 `9d95d45` 与 `683ab55`、T13 `4fd4875`、T14 `2a9e5d8`、T15 `b251e03`、
T16 `8094aa4`、T17 `a0d9dc1`；T18 带出的修复 `fe6346a`、`a2d0af6`、`4d1fd58`，提示词与文档和本文同一提交
验证方式：本地验证协议（Actions 额度用完期间，见计划「验证」一节）

## 结论

spec §15 的完成判据——**框架能修改自己的代码：读写文件、执行命令、跑测试、读取结果并迭代，CLI 可用**——由
一次真实运行证明：免费模型（Gemini `gemini-3.5-flash-lite` 为主、OpenRouter `cohere/north-mini-code:free`
兜底）按本仓的 `syndica-dev` 拓扑，在仓库副本里修好了交接文件「下一步」一节的一处错误并补了两条用例；宿主
复核六道门禁全过（4905 条测试）、Reviewer 给出 pass、交接文件 `verified`；人看过 diff 选 1，宿主提交并推到
本地 bare remote。这个提交原样带进本仓，是 `a2d0af6`。

它不是第一次就成的。同一个任务跑了三次，前两次暴露的问题都已处理：第一次 `verified` 了，但 diff 删掉了一条
无关的已有用例，宿主复核与两个 Reviewer 都没看出来（删了用例测试照样是绿的），是人在提交前看 diff 拦下的；
第二次一次 `Grep` 把约 16 万 token 塞进 Worker 的上下文。于是 Glob / Grep 加了输出上限、本地搜索不再进
`.git` 与字节码（`4d1fd58`），提示词按每一次实测失败补齐（D17），第三次只用了一半的请求就成了。这也说明
三道把关里人这一道不是摆设。

## 完成判据 × 证据

| 判据 | L2（脚本化 LLM，零成本，随全量测试） | 真实模型（T18，仓库副本） |
|---|---|---|
| 读约束、规划 | T17 场景 1 用本仓的角色、提示词与拓扑 | `--no-write` 的 Planner 交出计划，并指出需求里说缺的用例其实已有 |
| 改文件、跑检查、读失败、迭代 | T17 场景 1（先改错、pytest 失败、再改对）、场景 5 | 文档断链、夹具包的失败测试、本仓的真实小改动 |
| 宿主独立复核，不采信自述 | T17 场景 6 | Worker 写「全量 4871 项全部通过」而复核有 2 条失败：`verification_failed`、不提供提交 |
| Reviewer 结论只取看过最终工作树、自己没改文件的那次 | T17 的三个 Reviewer 变体 | 第三次的 Reviewer 拿到需求原文，结论被采信 |
| 危险命令被拦、换路径继续 | T17 场景 2；守卫拦下之后的确认照样到人手上 | 第三次 0 次拦截，没有误报 |
| 人工确认只放行那一次 | T17 场景 3（各写一个文件，只有被批准的那个在） | 第三次没有触发（更早的几次没有记录拦截与确认） |
| 只读 | T17 场景 4（Write 与 Bash 都是未知工具，不复核、不收尾） | Planner 场景 `read_only` |
| 人选择、宿主执行 git 收尾 | T17 场景 7：选 1 远端恰好多一个提交，选 3 本地与远端都不动，选 2 不带走人的改动，本地有没推的提交时不提供推送 | 第一次选 3（`declined`），第三次选 1（`pushed`） |
| CLI 可用 | 全部经 `main()` 与 `RuntimeCliHost` | 全部经 `python -m syndica` |

真实模型的逐场景记录（请求数、429、误报）在 [next-todos](../plans/2026-09-23-next-todos.md) 的「真实模型 E2E
记录」。

**D22 的真实 SSH 推送。** 按用户的选择保留本机的企业 DLP `pre-push` 钩子：在 GitHub 上建临时分支并设为上游，
用脚本化模型对着这个真实上游跑一次 `--handoff`。宿主的摘要写出真实的推送地址，因 pre-push 钩子不提供推送并
写明原因，选 2 只提交；之后用普通 `git push`（钩子照常运行）推上去，`ls-remote` 核对远端分支就是宿主的提交，
再删除分支。这一轮测出摘要把 `pre-push` 列成了「提交时会运行的钩子」，已改为只列提交会触发的、可执行的钩子。
宿主自己经 SSH 推送那一段（宿主环境里的 ssh agent）只在本地 bare remote 上跑过，登记为遗留。

## T15–T18 的公开契约核对

- `.syndica.yaml` 的 `workspace`（verify 与 CI 门禁逐字一致、受保护路径）与五个角色、`syndica-dev` 拓扑与 D15、
  D17 一致，由 `tests/cli/test_repo_config.py` 钉住（角色确切的工具表与轮数、每条提示词要求一个锚词）。轮数按
  真实运行调成 Orchestrator 24、Planner 16、Explorer 16、Worker 30、Reviewer 16。
- 内核 API 增量多一条：`Glob(portal, *, output_limit=None)` 与 `Grep(portal, *, output_limit=None)`，`local_tools`
  把同一个上限交给它们；`None` 时行为不变。已写进计划「公开契约」与 D9。
- `LocalWorkspace` 的搜索语义变了：不进以 . 开头的目录与文件（模式或 path 点了名的除外），Grep 跳过二进制
  文件。`Portal` 协议对此没有规定，这是本地实现的选择，写在 D9 与工具描述里。
- 交接文件的 front matter、退出码、规则 ID、标记行语法在 T15–T18 里没有变化。

## 由构造保证的格子

- **真实运行与 L2 走同一条代码路径。** T18 的 CLI 是 `python -m syndica`，T17 是 `main()` 加
  `RuntimeCliHost(model_factory=...)`，差别只在模型对象；宿主复核、交接与 git 收尾一行没换。
- **Agent 写的修复没有经人改动就进了本仓。** `a2d0af6` 的代码与副本里宿主提交的 `076373c` 逐字相同，只换了
  提交说明。

## 遗留

- 额度恢复后在 Linux 上对 `main` 补跑 CI：M2 的每个提交都只在本机验证过，`/bin/dash` 近似 Ubuntu 的 `/bin/sh`。
- 阶段一、阶段二审计各自登记的遗留照旧（链接见文首）。
- T18：问人之前的摘要只列改动的路径，删掉一条无关用例这种事只有人看 diff 才发现；按文件给出增删行数是
  便宜的改进。归 M3 之前的一个小任务。
- T18：Ctrl-C 打断 `--handoff` run 时不留交接文件（它在 Agent 都结束之后才写），stderr 是一段 Python
  traceback。临时目录与子进程照样收拾干净。归同一个小任务。
- T18：Bash 的默认超时是 30 秒，跑测试不传 timeout 就会超时；提示词约束不住每一次（第三次的 Reviewer 仍漏了
  一回）。归 M5 的执行面（或给开发期 run 一个更长的默认值）。
- T18：本地搜索不认 `.gitignore`，`node_modules` 这类不以 . 开头的目录照样进；`rglob` 仍会走一遍隐藏目录，
  只是不读它们。归 M5。
- T18：Agent 看不到自己还剩几轮，一次 run 的请求数没有上界（这个任务 43–85 次，含 429）。归 M8（可感知
  预算、父子预算树）。
- T18：开发期拦截读的是命令文本，认不出 `sh -c "…"`、`eval` 里的命令（D3 记下的盲区），也认不出 `xargs`、
  `find -exec` 跑的命令与 `find <根> -delete`；`rm -rf "$(pwd)"` 只被默认规则 `shell.recursive_force_remove`
  认出，`untrusted_high_risk=ask` 时它是一次询问而不是拦截。挡的是失手不是蓄意，README 已照实写明。把 `xargs`、
  `find` 纳入判定、递归判定 `sh -c` / `eval` 的参数，归 M3 开工前的一个小任务（改的是内核规则，要过差分）。
- T18：Read 一次最多交回 2000 行、不提示后面还有，也没有输出上限：一次读进 `runtime.py` 就是 116 KB，此后每次
  请求都带着。提示词已改成先 Grep 再按行号读；工具本身的上限与提示归 M5。
- T18：`doctor` 的 python 一行报的是 Agent 环境里的 `python3`（本机 3.9.6），低于 `requires-python` 也写 ok；
  本仓的命令都经 `uv run`，不受影响。小任务。
- 本机：全局 `core.hooksPath` 里有企业 DLP 的 `pre-push` 钩子，按 D14 宿主在本机的任何仓库里都不会提供推送；
  T18 的推送那一格在副本里把 `core.hooksPath` 设成空目录才跑到。宿主经 SSH 推送到真实远端（宿主环境里的
  ssh agent）因此没有实测过，归一台没有这类钩子的机器，或带 deploy key 的 CI。

## 验证

收口提交的暂存区单独检出到临时目录里跑（不受工作区里别的改动影响）：

```text
uv run ruff format --check .                                 146 files already formatted
uv run ruff check .                                          All checks passed
uv run pyright                                               0 errors
uv run python scripts/check_deps.py                          passed
uv run python scripts/check_links.py                         passed
uv run pytest -q                                             4941 passed
SYNDICA_TEST_SHELL=/bin/dash：git 收尾、自举场景、本仓配置     296 passed
```

T15–T18 各自的变异与对抗评审见计划里每个任务的状态；T18 带出的代码改动（`4d1fd58` 的输出上限与搜索过滤、
`a2d0af6` 的交接文字、git 收尾的钩子清单与提交环境）各有一轮变异全杀，收口的对抗评审之后又补了一轮（九条全杀）。
