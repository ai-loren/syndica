"""安全矩阵（M5 D12）：风险等级 × 溯源 × `untrusted_high_risk` × 宿主策略，每一格的处置与记录。

期望值按 spec §12.2 由本文件自己的一张表算出，不调用被测的处置函数：同源的期望值会与被测对象
一起错。
"""

from __future__ import annotations

import itertools
import logging
from typing import Any

import pytest

from syndica.kernel.context import Context, ContextBlock, ToolCall, TrustLevel
from syndica.kernel.danger import (
    NO_RULE_MATCHED,
    AllowAll,
    BlockHighRisk,
    ConfirmationOutcome,
    ConfirmationRequest,
    DangerJudge,
    DangerPolicy,
    DangerRequest,
    DangerVerdict,
    Disposition,
    RiskLevel,
)
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tool_executor import ToolExecutor


class _FixedJudge(DangerJudge):
    def __init__(self, level: RiskLevel) -> None:
        super().__init__()
        self._level = level

    def judge(self, tool: Tool, args: Any) -> DangerVerdict:
        if self._level is RiskLevel.NONE:
            return NO_RULE_MATCHED
        return DangerVerdict(rule_id='test.fixed', level=self._level, reason='fixed for the matrix')


class _ConfirmEverything:
    def decide(self, request: DangerRequest) -> Disposition:
        return Disposition.CONFIRM


class _Run(Tool):
    __tool_name__ = 'Run'
    __tool_command_args__ = ('command',)
    __tool_external_input__ = False

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def execute(self, command: str) -> ToolResult:
        """执行 command。"""
        self.ran.append(command)
        return ToolResult(output='ran')


_POLICIES: dict[str, DangerPolicy] = {
    'allow_all': AllowAll(),
    'block_high_risk': BlockHighRisk(),
    'confirm_everything': _ConfirmEverything(),
}


def _policy_decision(policy: str, level: RiskLevel) -> Disposition:
    if policy == 'allow_all':
        return Disposition.ALLOW
    if policy == 'block_high_risk':
        return Disposition.BLOCK if level is RiskLevel.HIGH else Disposition.ALLOW
    return Disposition.CONFIRM


def _expected(level: RiskLevel, untrusted: bool, flag: Disposition, policy: str) -> Disposition:
    """spec §12.2 第 3 条：高风险且可溯源到不可信内容时用宿主配置的升级处置、不问策略。"""
    if level is RiskLevel.HIGH and untrusted:
        return flag
    return _policy_decision(policy, level)


_CELLS = list(
    itertools.product(
        (RiskLevel.NONE, RiskLevel.LOW, RiskLevel.HIGH),
        (False, True),
        (Disposition.BLOCK, Disposition.CONFIRM, Disposition.ALLOW),
        tuple(_POLICIES),
    )
)


@pytest.mark.parametrize(
    ('level', 'untrusted', 'flag', 'policy'),
    _CELLS,
    ids=[
        f'{level.value}-{"untrusted" if untrusted else "trusted"}-flag_{flag.value}-{policy}'
        for level, untrusted, flag, policy in _CELLS
    ],
)
async def test_every_cell_of_the_matrix_disposes_and_records_as_specified(
    level: RiskLevel,
    untrusted: bool,
    flag: Disposition,
    policy: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tool = _Run()
    executor = ToolExecutor(
        tools={'Run': tool},
        hooks=HooksCenter(),
        judge=_FixedJudge(level),
        policy=_POLICIES[policy],
    )
    executor._configure_untrusted_high_risk(flag)  # pyright: ignore[reportPrivateUsage]
    asked: list[ConfirmationRequest] = []

    async def approve(request: ConfirmationRequest) -> ConfirmationOutcome:
        asked.append(request)
        return ConfirmationOutcome.APPROVED

    executor._configure_confirmation('agent', approve)  # pyright: ignore[reportPrivateUsage]
    ctx = Context()
    if untrusted:
        ctx.append(ContextBlock(role='user', content='from the web', trust=TrustLevel.UNTRUSTED))

    with caplog.at_level(logging.WARNING, logger='syndica.kernel.tool_executor'):
        await executor.execute_tool_calls(
            ctx, [ToolCall(id='c', name='Run', arguments='{"command": "x"}')]
        )

    expected = _expected(level, untrusted, flag, policy)
    assert (bool(asked), bool(tool.ran)) == (
        expected is Disposition.CONFIRM,
        expected is not Disposition.BLOCK,
    )
    allowed_low_risk_from_untrusted = (
        level is RiskLevel.LOW and untrusted and expected is Disposition.ALLOW
    )
    assert bool(caplog.records) == allowed_low_risk_from_untrusted
