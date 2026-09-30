"""开发期的危险判定与处置（spec D4）：CLI 的每一次 run 都用它，不只是 `--handoff`。

规则表是默认四条加三族可选规则。声明顺序是承重的：判定取第一个命中的规则，所以阻断的几族排在前面、
`shell.path_outside_workspace` 排在最后——一条既改 git 状态又碰工作区外路径的命令，结论必须是阻断，
而不是「问人」。
"""

from __future__ import annotations

from pathlib import Path

from syndica.kernel import DEFAULT_RULES, DangerJudge, Disposition
from syndica.kernel.danger import (
    RuleTablePolicy,
    git_state_change_rule,
    path_outside_workspace_rule,
    workspace_root_removal_rule,
)

__all__ = ['dev_danger_options', 'dev_guard_judge']


def dev_danger_options(workspace_root: Path) -> tuple[DangerJudge, RuleTablePolicy]:
    """(判定, 处置)：交给目录冻结与拓扑装配，每个建出的 Agent 都用它们。"""
    root = workspace_root.resolve()
    judge = DangerJudge(
        (
            *DEFAULT_RULES,
            git_state_change_rule(),
            workspace_root_removal_rule(root),
            path_outside_workspace_rule(root),
        )
    )
    table = {rule.rule_id: Disposition.BLOCK for rule in DEFAULT_RULES}
    table.update(
        {
            'shell.git_state_change': Disposition.BLOCK,
            'shell.workspace_root_removal': Disposition.BLOCK,
            # 越界问人：它只在可信轨迹里到得了人手上；读过命令输出之后按 untrusted_high_risk 走。
            'shell.path_outside_workspace': Disposition.CONFIRM,
        }
    )
    return judge, RuleTablePolicy(table)


def dev_guard_judge(workspace_root: Path) -> DangerJudge:
    """开发期 Module 用的判定器：只含两族「永不允许」的规则。"""
    return DangerJudge(
        (git_state_change_rule(), workspace_root_removal_rule(workspace_root.resolve()))
    )
