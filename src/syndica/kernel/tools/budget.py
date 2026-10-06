"""GetBudget（spec §8.4）：Agent 读得到自己还剩多少，才能自己决定怎么分配——预算是它的决策信息，
不是一个它看不见的外部开关。只在这个 Agent 的预算链上有上限时才装：不用预算的 run 不为它多付一份
工具定义。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from syndica.kernel.budget import Budget, BudgetView
from syndica.kernel.compaction import traj_tokens
from syndica.kernel.tool import Tool, ToolResult

if TYPE_CHECKING:
    from syndica.kernel.agent import BaseAgent

__all__ = ['GetBudget']


def _dims(budget: Budget) -> dict[str, object]:
    return {
        'turns': budget.turns,
        'tokens': budget.tokens,
        'cost': None if budget.cost is None else str(budget.cost),
        'wall_clock_s': None if budget.wall_clock_s is None else round(budget.wall_clock_s, 1),
    }


def render_view(view: BudgetView) -> dict[str, object]:
    used = view.used
    return {
        'limit': _dims(view.limit),
        'remaining': _dims(view.remaining),
        'used': {
            'turns': used.turns,
            'input_tokens': used.input_tokens,
            'output_tokens': used.output_tokens,
            'cached_input_tokens': used.cached_input_tokens,
            'cost': None if used.cost is None else str(used.cost),
            'estimated': used.estimated,
        },
        'exhausted': list(view.exhausted),
    }


class GetBudget(Tool):
    __tool_name__ = 'GetBudget'
    __tool_command_args__ = ()
    __tool_external_input__ = False

    def __init__(self, agent: BaseAgent) -> None:
        self._agent = agent

    async def execute(self) -> ToolResult:
        """读你此刻的预算：每一维的上限、剩余与已用，以及上下文窗口占到了多少。
        已用里分输入 / 输出 / 缓存命中 token；剩余里有剩几轮、剩多少 token 与成本、剩几秒。
        剩得不多时据此决定先保住哪些交付物、放弃哪些锦上添花的部分；
        上下文接近压缩阈值时可以先调 Compact。null 表示那一维不限。"""
        agent = self._agent
        account = agent._budget  # pyright: ignore[reportPrivateUsage]
        payload: dict[str, object] = (
            {'limit': None} if account is None else render_view(account.view())
        )
        controller = agent._compaction  # pyright: ignore[reportPrivateUsage]
        if controller is not None:
            payload['context'] = {
                'tokens': traj_tokens(agent.context.current_traj),
                'compaction_threshold_tokens': controller.threshold_tokens,
            }
        return ToolResult(output=json.dumps(payload, ensure_ascii=False))
