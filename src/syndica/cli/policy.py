"""开发期的危险判定与处置（spec D4）：CLI 的每一次 run 都用它，不只是 `--handoff`。

规则表是默认四条加三族可选规则。声明顺序是承重的：判定取第一个命中的规则，所以阻断的几族排在前面、
`shell.path_outside_workspace` 排在最后——一条既改 git 状态又碰工作区外路径的命令，结论必须是阻断，
而不是「问人」。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from syndica.kernel import DEFAULT_RULES, DangerJudge, Disposition
from syndica.kernel.danger import (
    RiskLevel,
    RuleTablePolicy,
    git_state_change_rule,
    path_outside_workspace_rule,
    workspace_root_removal_rule,
)

__all__ = [
    'STRICTNESS',
    'RoleTightening',
    'dev_danger_options',
    'dev_guard_judge',
    'dev_table',
    'role_policies',
]

# 处置从松到严。项目配置只能沿这个方向改（M4 D12）。
STRICTNESS: Mapping[Disposition, int] = MappingProxyType(
    {Disposition.ALLOW: 0, Disposition.CONFIRM: 1, Disposition.BLOCK: 2}
)


def _no_rules() -> Mapping[str, Disposition]:
    return MappingProxyType({})


@dataclass(frozen=True)
class RoleTightening:
    """`.syndica.yaml` 里一个角色在开发期处置表上的叠加：`rules` 逐条收紧，`block_high_risk` 把全部
    HIGH 规则改成阻断。只收紧，读配置时已经校验过。"""

    rules: Mapping[str, Disposition] = field(default_factory=_no_rules)
    block_high_risk: bool = False


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
    return judge, RuleTablePolicy(dev_table())


def dev_table() -> dict[str, Disposition]:
    """开发期判定器里每条规则的处置。键就是这张判定器认得的全部规则 ID，与工作区根无关。"""
    table = {rule.rule_id: Disposition.BLOCK for rule in DEFAULT_RULES}
    table.update(
        {
            'shell.git_state_change': Disposition.BLOCK,
            'shell.workspace_root_removal': Disposition.BLOCK,
            # 越界问人：它只在可信轨迹里到得了人手上；读过命令输出之后按 untrusted_high_risk 走。
            'shell.path_outside_workspace': Disposition.CONFIRM,
        }
    )
    return table


def role_policies(
    judge: DangerJudge, tightenings: Mapping[str, RoleTightening]
) -> dict[str, RuleTablePolicy]:
    """每个声明了收紧的角色一张处置表：开发期表叠上它的收紧。落成表而不是 `BlockHighRisk()`，
    回放按表重建；后者会把一条非 HIGH 规则的 CONFIRM 放宽成 ALLOW。"""
    policies: dict[str, RuleTablePolicy] = {}
    for role, tightening in tightenings.items():
        table = dev_table()
        if tightening.block_high_risk:
            for rule in judge.rules:
                if rule.level is RiskLevel.HIGH:
                    table[rule.rule_id] = Disposition.BLOCK
        for rule_id, disposition in tightening.rules.items():
            current = table.get(rule_id, Disposition.ALLOW)
            if STRICTNESS[disposition] > STRICTNESS[current]:
                table[rule_id] = disposition
        policies[role] = RuleTablePolicy(table)
    return policies


def dev_guard_judge(workspace_root: Path) -> DangerJudge:
    """开发期 Module 用的判定器：只含两族「永不允许」的规则。"""
    return DangerJudge(
        (git_state_change_rule(), workspace_root_removal_rule(workspace_root.resolve()))
    )
