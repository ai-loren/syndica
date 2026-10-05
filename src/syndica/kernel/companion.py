"""伴生 Agent 运行器（§7.5）：受限、有预算、只读主 Agent 工作区的子计算。

三个伴生（RubricsBuilder / OutputVerifier / OutcomeVerifier）都经它跑。安全靠构造处收口（评审 B6）：

- **不进注册表**：直接构造 `BaseAgent`、不走 `Runtime.spawn`，编排工具（`SendMessage` 等）寻址不到
  它，主 Agent 既发不了「请判 pass」也杀不掉它。
- **工具白名单只有 `Read` / `Grep` / `Glob`**：不经 Module 工具工厂，拿不到 `Write` / `Bash` / 编排
  五件套；只读工具天然不碰 git 写与危险操作，宿主那些守卫对它是空谈、不必复制。
- **Portal 是主 Agent 的那一份**（含隔离副本）：读的是被验收对象的工作区，私有目录由 Portal 自己守。
- **不挂验收插件**：构造时不 `_attach_rubrics` / `_attach_compaction`，伴生跑完不再触发验收（防递归
  验收验收器）。
- **独立预算**：自己的 `max_turns`，不花主 Agent 的轮次。

绑定是绑在主 Agent 的 **Portal 对象**上（调用方传进来），不按 `agent_id` 查——那正是「作用在错主体」
入口。两个主 Agent 各自的伴生只能看到各自的 Portal 与各自那份渲染进 `task` 的历史。
"""

from __future__ import annotations

from dataclasses import dataclass

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import TrustLevel
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import LLM
from syndica.kernel.tool import Tool
from syndica.kernel.tools.local import Glob, Grep, Read
from syndica.kernel.workspace import Portal

__all__ = ['CompanionResult', 'read_only_tools', 'run_companion']


@dataclass(frozen=True)
class CompanionResult:
    """伴生跑完的结果：最终那条 assistant 文本，以及它用了几轮（花的是自己的预算）。"""

    reply: str
    turns: int


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
    ctx = await agent.run(task, trust=trust)
    reply = next(
        (str(block.content) for block in reversed(ctx.current_traj) if _is_reply(block)),
        '',
    )
    return CompanionResult(reply=reply, turns=agent._current_turn)  # pyright: ignore[reportPrivateUsage]


def _is_reply(block: object) -> bool:
    role = getattr(block, 'role', None)
    content = getattr(block, 'content', None)
    return role == 'assistant' and bool(content)
