"""Goal / Rubric 注入历史（§7.6 / §5.2）+ 锚点可重开校验（§7.6）。

本模块保留 `from __future__ import annotations` 与仓库其余模块一致。
"""

from __future__ import annotations

from syndica.kernel.acceptance import Anchor, AnchorSource, Goal, Rubric, RubricKind
from syndica.kernel.acceptance_inject import (
    GOAL_KEY,
    goal_block,
    rubric_block,
    rubric_key,
    verify_anchors,
)
from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import Context, TrustLevel
from syndica.kernel.events import EventBus
from syndica.kernel.history_blocks import HistoryKind, has_block, kind_of
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn

_ANCHOR = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'ship it'),)


def _rubric(
    rid: str, *, kind: RubricKind = RubricKind.CHECKABLE, trust: TrustLevel = TrustLevel.TRUSTED
) -> Rubric:
    return Rubric(id=rid, statement=f'{rid} body', kind=kind, anchors=_ANCHOR, trust=trust)


def test_goal_block_is_a_signed_goal_block() -> None:
    block = goal_block(Goal(intent='ship a report', anchors=_ANCHOR))
    assert kind_of(block) is HistoryKind.GOAL
    assert block.extra['syndica.key'] == GOAL_KEY
    assert 'ship a report' in str(block.content)


def test_rubric_block_is_signed_and_names_each_rubric() -> None:
    block = rubric_block([_rubric('R1'), _rubric('R2', kind=RubricKind.JUDGMENT)])
    assert kind_of(block) is HistoryKind.RUBRIC
    assert '[R1]' in str(block.content) and '[R2]' in str(block.content)
    assert '需人工确认' in str(block.content)  # judgment 条标出来


def test_a_rubric_block_inherits_the_strictest_trust() -> None:
    # 从不可信任务输入投出的 Rubric 仍不可信（§12.2 / 评审 H9）：任一条不可信 → 整块不可信。
    assert rubric_block([_rubric('R1'), _rubric('R2')]).trust is TrustLevel.TRUSTED
    mixed = rubric_block([_rubric('R1'), _rubric('R2', trust=TrustLevel.UNTRUSTED)])
    assert mixed.trust is TrustLevel.UNTRUSTED


def test_the_rubric_key_changes_when_the_set_changes() -> None:
    before = rubric_key([_rubric('R1')])
    assert rubric_key([_rubric('R1')]) == before  # 内容不变、键不变（不重注）
    assert rubric_key([_rubric('R1'), _rubric('R2')]) != before  # 集合变了、键变（追加替代块）


def test_verify_anchors_accepts_a_reopenable_anchor() -> None:
    anchors = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'add a --json flag'),)
    assert verify_anchors(anchors, {'msg:1': 'please add a --json flag to it'}) == []


def test_verify_anchors_rejects_a_missing_locator() -> None:
    anchors = (Anchor(AnchorSource.FILE, 'spec.md:99', 'x'),)
    problems = verify_anchors(anchors, {'spec.md:12': 'real text'})
    assert len(problems) == 1 and 'spec.md:99' in problems[0]


def test_verify_anchors_rejects_a_fabricated_quote() -> None:
    # 锚点规则的牙齿：引文对不上就是编造（评审 H10），不只是「有没有锚点」。
    anchors = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'delete production'),)
    problems = verify_anchors(anchors, {'msg:1': 'please tidy the report'})
    assert len(problems) == 1 and '找不到' in problems[0]


def _agent(script: list[ScriptedTurn]) -> BaseAgent:
    options = AgentOptions(name='Worker', llm=ScriptedLLM(script), tools={}, system_prompt='SP')
    return BaseAgent(options, hooks=HooksCenter(), events=EventBus())


async def test_the_seam_injects_goal_and_rubrics_before_the_query() -> None:
    agent = _agent([ScriptedTurn(content='done')])
    goal = Goal(intent='ship a report', anchors=_ANCHOR)
    rubrics = [_rubric('R1')]

    async def inject(ctx: Context) -> None:
        if not has_block(ctx.current_traj, GOAL_KEY):
            ctx.append(goal_block(goal))
        if not has_block(ctx.current_traj, rubric_key(rubrics)):
            ctx.append(rubric_block(rubrics))

    agent._attach_rubrics(inject)  # pyright: ignore[reportPrivateUsage]
    ctx = await agent.run('do the work')
    kinds = [kind_of(b) for b in ctx.current_traj]
    goal_at = kinds.index(HistoryKind.GOAL)
    rubric_at = kinds.index(HistoryKind.RUBRIC)
    query_at = next(
        i for i, b in enumerate(ctx.current_traj) if b.role == 'user' and kind_of(b) is None
    )
    assert goal_at < query_at and rubric_at < query_at  # Goal / Rubric 在查询之前


async def test_the_seam_is_idempotent_across_cycles() -> None:
    agent = _agent([ScriptedTurn(content='a'), ScriptedTurn(content='b')])
    goal = Goal(intent='ship a report', anchors=_ANCHOR)

    async def inject(ctx: Context) -> None:
        if not has_block(ctx.current_traj, GOAL_KEY):
            ctx.append(goal_block(goal))

    agent._attach_rubrics(inject)  # pyright: ignore[reportPrivateUsage]
    await agent.run('cycle one')
    ctx = await agent.run('cycle two')
    assert sum(kind_of(b) is HistoryKind.GOAL for b in ctx.current_traj) == 1  # 不重注
