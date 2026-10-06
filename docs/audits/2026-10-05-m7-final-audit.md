# 里程碑 7 收口审计：产物与验收

日期：2026-10-06
范围：[M7 计划](../plans/2026-10-05-m7-state.md) 的 T0 与五个阶段（产物身份、Goal / Rubric 与锚点规则、L2 / L3 与四值
裁决、反馈升级、验收 Module 与接线），加上 M5 留给 M7 的两条与 M6 收口审计留给 M7 的两条
基线：T0 `87e9d43`、阶段一 `f39ce30` / `62c08eb` / `fffbaf3` / `106fb1c`、阶段二 `df143c1` / `4f17af5` / `472968a` /
`8bbefc3` / `20b0aa5`、阶段三 `fab3671` / `04b2831`、阶段四 `589d080`、阶段一~三评审处置 `cb6346e`、验收 Module
`a4b933d`、CLI 接线 `48dbbdb`、可执行位合并 `e4f8917`、D-64 模型侧 `4c905d8`、M6 遗留与 `artifacts` / `memory` 命令
`22111c7`；收口评审的修复与本文另行提交
验证方式：本地验证协议（Actions 额度用完期间，见路线图「当前主线约束」）

## 结论

spec §15 的完成判据——**产物模型、处理器、验收标准与锚点、三态裁决、反馈升级为标准**——由一套端到端用例（经公开
API 与 `syndica` 命令行、脚本化模型，随全量测试零成本地跑）与真实运行共同证明。

- 产物：`ArtifactStore` 协议 + 内存 / 文件实现 + 契约（先私有）；工具回报的产物由内核按路径与隔离作用域重新派生 id、
  每批工具之后登记并触发 `ARTIFACT_REGISTERED`；只有 `Deliver` 能标交付物，交付标记是产物级的粘性属性。处理器按
  `(kind, mime)` 精度解析、同精度冲突启动即失败；L1 按扩展名派生的 kind 选处理器。`syndica artifacts list | show` 列出
  交付物与版本。
- Goal / Rubric：Goal 是唯一权威（宿主的 `--goal` 优先，否则根 Agent 的第一条消息），Rubric 是带锚点的投影，锚点要能
  重新打开（有实义的引文、来源对得上、引文确实在那处）；judgment 条永不自动判过。两者经署名历史块注入（进历史不进 System
  Prompt），压缩只保护最新的 Goal 块与 Rubric 块。
- 验收：L1 零 token（交付物违规直接打回）、L2 保守（拿不准就判没做完）、L3 逐条判且证据由内核重新打开；裁决由内核从逐条
  结论推导成四值（`pass` / `partial` / `block` / `unverified`），验收器失败或超时放行但只给 `unverified`；终态裁决进 CLI
  stderr、`agent_end` 事件、trace（`verdict.*`，`TRACE_SCHEMA` `/5`）与 `trace show`。三层与投影、反馈都在 `acceptance`
  Runtime Module 里，Flag 默认全关。
- 反馈：Goal 之后人发来的消息提议成 `proposed` Rubric，`syndica sessions activate` 之后才进验收；Agent 发的、退出了的 Agent
  发的都不算人的反馈。
- 真实运行（Gemini `gemini-3.5-flash-lite` 免费档，2026-10-06；OpenRouter 免费档当日额度已用完）：仓库外临时工作区，
  `modules: [acceptance]`、三层全开，信任存储指到临时文件。`--goal` 投影出锚回 Goal 与任务原文的 Rubric，模型写出
  `report.md` 并 `Deliver`，L3 逐条给出能重新打开的证据，`验收：pass`，trace 的 agent span 带逐条结论；同一任务把时限压到
  1 秒，投影与验收都超时，run 照常完成、`验收：unverified；没能验证（verifier_timeout）`。key 只在子壳里 source，壳外
  `env | grep -c ...KEY` 全程为 0。

## 完成判据 × 证据

| 判据（路线图 M7） | L1/L2（脚本化，零成本，随全量测试） | 真实模型（仓库之外的临时工作区） |
|---|---|---|
| ArtifactRef 身份与版本、序列化保真 | `ArtifactStoreContract` 对内存与文件实现全过（跨 producer 同路径同 id、登记顺序、A→B→A 回退成为当前、粘性交付标记、并发基数 3）；`test_artifact_tracking`：id 由内核派生、带隔离作用域、路径规范化 | `.syndica/artifacts/index.json` 记下交付物；`artifacts show` 列出两次运行的两个版本与 producer |
| 处理器冲突、精度优先、回退 | `test_artifacts`：精度优先、同精度冲突启动失败、被拒的处理器一个键也不留下、未匹配回退 `generic` | — |
| 每条 Rubric 都有能重新打开的锚点 | `test_acceptance_inject` / `test_rubrics_builder`：空 / 过短引文、来源与 locator 不符、编造的引文都被丢弃；重复 id 只留第一条 | 投影出的每条都锚回 Goal 或任务原文 |
| Verdict 四值、`unverified` 不塌缩成 pass | `test_acceptance`：不变量与推导（judgment 恒需人判、空集合 `unverified`、pass 带待人判的条落 `unverified`）；`test_verifiers`：fail-open 只接 `Exception`、超时与模型自己的 `TimeoutError` 分开、矛盾结论降级 | 时限压到 1 秒：`unverified（verifier_timeout）` |
| 验收能驱动修复 | `test_completion_gate` / `test_acceptance_module`：partial / block 由内核追加署名反馈块再做一轮，三个上限各自计数、每周期清零；L1 交付物违规零 token 打回 | — |
| 人的反馈升级要人确认 | `test_feedback` / `test_acceptance_module`：只认人发的、提议恒为 proposed、activate 才生效；`tests/cli/test_acceptance_wiring.py`：提议在回答后告诉人一次，`sessions activate` 让它进验收 | — |
| 每个交付物追得到 producer、version、trace、裁决 | `artifacts show` 的版本与 producer；agent span 的 `verdict.*`；`trace show` 时间线 | 同左，真模型 |
| `unverified` 在 CLI / trace 可见 | `test_tracing` / `test_trace_view` / CLI 用例 | stderr 与 `trace show` |

## 计划之外的偏差

- **注入时机挪到消息注入之后**（P6）：cycle 起点轨迹里还没有这次的任务，派生不出 Goal、第一轮也看不到投影。内核改成每个
  周期第一轮、消息注入之后、模型调用之前调注入器；Goal / Rubric 块于是落在任务之后（仍只追加）。
- **`Deliver` 由验收 Module 给**，不进内置角色（P7）：不验收的 run 不为它多付一个工具定义；角色自己带了就不重复，不能写的
  Agent 不发。
- **宿主此前从没给 Runtime 接产物索引**（P7）：隐式追踪与 L1 在 CLI 里根本不触发，装了 acceptance 才接。
- **只有根协调者有 Goal**（P6）：子 Agent 的交付由派活的一方验收。
- **「没变就跳过 L2 / L3」不做**（P6）：最终回复本身是被判的对象，不看回复的跳过让纯文本任务永远不再重判。
- **装了 acceptance 的 run 不写回放文档**（P7）：伴生调用不在录制轮次里；回放重建器拒绝重建它。
- **M6 收口审计留给 M7 的两条**被计划 P1 漏收（P1 只收了路线图遗留表里标 M7 的两条），收口核对时补上（P9）。

## 公开契约核对

- `Portal` 协议加了 `set_executable`、`FileStat` 加了 `executable`（缺省 False）：协议加方法是对第三方后端的破坏性变更，
  计划 P1 定随 M7 做，`WorkspaceContract` 同步加了两条（往返且不动版本、改写保留、真能执行；不存在 / 越界拒绝）。
- `kernel.__all__` 不变：产物、Rubric、验收 Module、伴生运行器全部扣留（`_WITHHELD`），先私有、有第三方要再放出来。
- `TRACE_SCHEMA` `/3` → `/5`：`/4` 加了验收裁决、`/5` 加了逐条结论；旧 trace 照读，新的自由文本属性经脱敏。
- `docs/reference/flags.md` 重新生成，新增 `acceptance_*` 九条 Flag，金标同步。
- CLI 新增：`run` / `chat` 的 `--goal`，`sessions rubrics` / `sessions activate`，`artifacts list | show`，`memory list |
  forget`；`CliHost` 协议加了 `memory`。

## 遗留

| 条目 | 归属 | 依据 |
|---|---|---|
| 伴生（投影、L2 / L3、反馈提议）的完整 span 与成本 | M8 | 逐条结论已在 agent span；伴生是直接构造的 `BaseAgent`，给它们子 span 并算进预算树随 M8 |
| 装了 acceptance 的 run 的回放 | 后续（回放文档 v4） | 与 M5 C3、M6 aux 调用同属 v4 的槽位 |
| 目标导向型压缩（§8.8 第四种） | M8 | 依赖的 Rubric 已交付；压缩保护最新 Goal / Rubric 块已在 M7 |
| 进度与漂移（§8.2 / §8.3） | M8 | 只留 `Rubric.mandatory` 与只数强制条的接口 |
| 导出器默认排除 `unverified` | M11 | 四值与 fail-open 标记已在内核 |
| 裁决的退出码映射 / 交接文件字段 | M9 | trace 已机器可读 |
| 文件 / 产物锚点钉版本、处理器注册新 kind、`preview` / `semantic_diff` 的消费方、反馈带产物定位、`Deliver.note` 落记录 | 后续 / M9 | 见计划 P10 |
| 二进制格式处理器 | 后端 | 内核只给协议与文本 `generic` |
| `RubricStore` / `ArtifactStore` 契约与验收 Module 进公开面；`encode_assured` 白名单；产物索引存文件名原文 | M12 | 先私有 |
| Module 服务经 `runtime.send` 发的消息会被当成人的反馈提议 | M12 | 提议仍要人激活 |
| 整条回复恰好是要求原文时仍算 L3 证据 | 接受 | 见计划 P10 |
| 只有根协调者有 Goal | 接受 | 宿主可经 store 给子 Agent 一个 Goal |
| 记忆条目的逐条来源（哪个会话、哪条消息） | M12 | `memory forget` 已给撤回入口；段只记得整理的 seq 区间 |

## 评审与变异

| 评审 | 结论 | 处置 |
|---|---|---|
| 计划评审 | 6 Blocking / 12 High / 8 Medium / 4 Low | 全部折进计划（P1） |
| 阶段一~三 | 2 Blocking / 9 High / 11 Medium / 6 Low | `cb6346e`：伴生读到的不可信内容洗成可信（B1）、伴生的读记在主 Agent 名下绕过乐观并发（B2）、链上最后一个 hook 说了算（H1）、裁决跨周期残留（H2）等；H9 拍板 pass 带待人判的条落 `unverified`（P5） |
| 收口（整个 M7） | 1 Blocking / 3 High / 10 Medium / 8 Low | 本次提交，见计划 P10 |

**收口评审（Blocking 与 High 已修）。** 后台对抗评审（私有快照 `git archive 4c905d8`）：

- **B1（已修）** 被收紧的 Module 经 `COMPLETION_PROPOSED` 写的裁决与打回理由以 TRUSTED 进下一轮，`AllowAll` 之下那一轮
  的高风险命令照跑——M4 D15 在 M7 新加的切点上重新打开。改走带作者的分发，出自被收紧 Module 的裁决与反馈块不可信。
- **H1（已修）** L3 的轨迹证据在整份轨迹里找，引 Goal 或 Rubric 原文就判过。语料改成主 Agent 的回复、工具输出与它发起的
  调用；只复述要求的不算，整行就是那段文字的算（真实运行发现：「只回 DONE」的证据就是那一行）。
- **H2（已修）** L1 结果终身不刷新：交付后被命令改坏漏判、旧问题让无关回复也被打回。完成时按此刻版本复核、只报本周期。
- **H3（已修）** `Deliver` 换个 kind 就绕过严格的处理器。L1 按扩展名派生的 kind 选处理器。

**变异的诚实交代。** 每个阶段的修复都做了「sha256 先校验 → 施加 → 清字节码 → 跑定向用例确认变红 → 还原 → 再按 sha256
校验」的定向变异：阶段一~三评审处置 40 条、验收 Module 16 条、CLI 接线 11 条、可执行位 8 条、D-64 2 条、M6 遗留 9 条、
收口修复 20 条，全部变红。但每一轮的变异集都偏向作者当时在想的路径：收口评审自己施加的 24 条里有 16 条存活——它们逐条补了
单独施加即变红的用例。「零存活」只在给定的那组变异下成立。

## 验证

```text
uv run ruff check .                                          通过
uv run ruff format --check .                                 通过
uv run pyright                                               0 errors
uv run python scripts/check_deps.py                          通过
uv run python scripts/check_links.py                         通过
uv run python scripts/gen_flag_reference.py --check          通过
uv run pytest -q                                             全部通过（数目见收口提交的说明）
```

推送前对提交的树做导出验证（把这次提交单独检出到临时目录、在那里跑上面每一道门禁）。
