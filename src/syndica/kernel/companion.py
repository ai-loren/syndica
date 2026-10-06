"""伴生 Agent 运行器（§7.5）：受限、有预算、只读主 Agent 工作区的子计算。

三个伴生（RubricsBuilder / OutputVerifier / OutcomeVerifier）都经它跑。安全靠构造处收口（评审 B6）：

- **不进注册表**：直接构造 `BaseAgent`、不走 `Runtime.spawn`，编排工具（`SendMessage` 等）寻址不到
  它，主 Agent 既发不了「请判 pass」也杀不掉它。
- **工具白名单只有 `Read` / `Grep` / `Glob`**：不经 Module 工具工厂，拿不到 `Write` / `Bash` / 编排
  五件套；只读工具天然不碰 git 写与危险操作，宿主那些守卫对它是空谈、不必复制。
- **Portal 是主 Agent 的那一份**（含隔离副本）：读的是被验收对象的工作区，私有目录由 Portal 自己守。
- **不挂验收插件**：构造时不 `_attach_rubrics` / `_attach_compaction`，伴生跑完不再触发验收（防递归
  验收验收器）。
- **预算记在被服务的 Agent 上**：自己的 `max_turns`，不花主 Agent 的轮次；token 与成本记在主 Agent
  的预算节点上、准入也按它（§8.4：多 Agent 并行 + 双伴生验收的三重放大必须在同一棵树里收住）。主
  Agent 的预算耗尽时伴生一次调用都发不出去，`stop_reason` 是 BUDGET，验收据此标 `budget_exhausted`。
- **trace 里是主 Agent 的子 span**：agent span 挂在发起验收的那一处、带 `agent.serves`；id 由（主
  Agent、伴生名、第几个）派生，两次运行的 span id 一致。

绑定靠调用方交进来的 Portal：有 `WorkspaceRouter` 时**必须**交 `router.reader_for(主 Agent 的 id)`，
不能交路由本身——路由按此刻在驱动的 Agent 记读基线，伴生在主 Agent 的驱动任务里跑，经路由读就记在主
Agent 名下、刷新它的写前提，主 Agent 下一次整篇覆盖于是静默盖掉别人的改动（评审 B2）。只读视图钉住
主 Agent 那份工作区、不记读基线、信任照算。两个主 Agent 的伴生各看各的工作区与历史。
"""

from __future__ import annotations

from dataclasses import dataclass

from syndica.kernel.agent import SERVED, AgentOptions, BaseAgent
from syndica.kernel.context import StopReason, TrustLevel, provenance_of
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.ids import assigning_agent_id, releasing_agent_id
from syndica.kernel.llm import LLM
from syndica.kernel.tool import Tool
from syndica.kernel.tools.local import Glob, Grep, Read
from syndica.kernel.workspace import Portal

__all__ = ['CompanionResult', 'read_only_tools', 'run_companion']


@dataclass(frozen=True)
class CompanionResult:
    """伴生跑完的结果：最终那条 assistant 文本、它用了几轮（花的是自己的预算），以及它这份轨迹的
    溯源结论 `provenance`——它读过的不可信内容（视图本身、Read 到的被污染文件）都在里面。验收器据此给
    裁决定信任：只看主 Agent 的轨迹会把伴生读到的不可信内容洗成可信（评审 B1）。"""

    reply: str
    turns: int
    provenance: TrustLevel = TrustLevel.TRUSTED
    stop_reason: StopReason | None = None


def read_only_tools(portal: Portal | None) -> dict[str, Tool]:
    """伴生的工具白名单：只 `Read` / `Grep` / `Glob`，绑在主 Agent 的 Portal 上。`portal=None` 时无
    工具（RubricsBuilder 只看「被要求了什么」、不读产物时走这条）。"""
    if portal is None:
        return {}
    tools: tuple[Tool, ...] = (Read(portal), Grep(portal), Glob(portal))
    return {tool.name: tool for tool in tools}


async def run_companion(
    *,
    name: str,
    llm: LLM,
    system_prompt: str,
    task: str,
    portal: Portal | None = None,
    max_turns: int = 8,
    trust: TrustLevel = TrustLevel.TRUSTED,
) -> CompanionResult:
    """跑一个伴生 Agent，交回它最终那条 assistant 文本。`task` 是渲染好的受限视图（只含该伴生该看的
    那部分主 Agent 历史）；`trust` 应反映视图内容（主 Agent 读过不可信内容时视图不可信，§12.2）。"""
    served = SERVED.get()
    token = None
    if served is not None:
        served.agent._companions += 1  # pyright: ignore[reportPrivateUsage]
        companions = served.agent._companions  # pyright: ignore[reportPrivateUsage]
        token = assigning_agent_id(f'{served.agent.agent_id}.{name}.{companions}')
    try:
        agent = BaseAgent(
            AgentOptions(
                name=name,
                llm=llm,
                tools=read_only_tools(portal),
                system_prompt=system_prompt,
                max_turns=max_turns,
            ),
            hooks=HooksCenter(),
            events=EventBus(),
        )
    finally:
        if token is not None:
            releasing_agent_id(token)
    if served is not None:
        agent._serves = served.agent.agent_id  # pyright: ignore[reportPrivateUsage]
        agent._attach_tracing(served.tracing)  # pyright: ignore[reportPrivateUsage]
        if served.account is not None:
            agent._attach_budget(served.account.for_companion())  # pyright: ignore[reportPrivateUsage]
    ctx = await agent.run(task, trust=trust)
    reply = next(
        (str(block.content) for block in reversed(ctx.current_traj) if _is_reply(block)),
        '',
    )
    return CompanionResult(
        reply=reply,
        turns=agent._current_turn,  # pyright: ignore[reportPrivateUsage]
        provenance=provenance_of(ctx),
        stop_reason=ctx.stop_reason,
    )


def _is_reply(block: object) -> bool:
    role = getattr(block, 'role', None)
    content = getattr(block, 'content', None)
    return role == 'assistant' and bool(content)
