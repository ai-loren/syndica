"""Goal / Rubric 注入历史（§7.6 / §5.2）+ 锚点可重开校验（§7.6）。

本模块保留 `from __future__ import annotations` 与仓库其余模块一致。
"""

from __future__ import annotations

from typing import cast

from syndica.kernel.acceptance import Anchor, AnchorSource, Goal, Rubric, RubricKind
from syndica.kernel.acceptance_inject import (
    goal_block,
    goal_key,
    meaningful_quote,
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

_ANCHOR = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'ship it now'),)


def _rubric(
    rid: str, *, kind: RubricKind = RubricKind.CHECKABLE, trust: TrustLevel = TrustLevel.TRUSTED
) -> Rubric:
    return Rubric(id=rid, statement=f'{rid} body', kind=kind, anchors=_ANCHOR, trust=trust)


def test_goal_block_is_a_signed_goal_block() -> None:
    goal = Goal(intent='ship a report', anchors=_ANCHOR)
    block = goal_block(goal)
    assert kind_of(block) is HistoryKind.GOAL
    assert block.extra['syndica.key'] == goal_key(goal)
    assert 'ship a report' in str(block.content)


def test_a_changed_goal_gets_a_new_key_so_it_is_reinjected() -> None:
    # 键固定时改了的 Goal 永远注不进去（评审 M6）：键随内容变。
    assert goal_key(Goal(intent='write a poem')) != goal_key(Goal(intent='write a report'))
    assert goal_key(Goal(intent='same')) == goal_key(Goal(intent='same'))


def test_the_goal_block_carries_the_goals_trust_both_ways() -> None:
    # 从不可信消息派生的 Goal 仍不可信（评审 H6）；可信的照旧可信——两侧各一条。
    assert goal_block(Goal(intent='g', trust=TrustLevel.UNTRUSTED)).trust is TrustLevel.UNTRUSTED
    assert goal_block(Goal(intent='g')).trust is TrustLevel.TRUSTED


def test_rubric_block_is_signed_and_names_each_rubric() -> None:
    block = rubric_block([_rubric('R1'), _rubric('R2', kind=RubricKind.JUDGMENT)])
    assert kind_of(block) is HistoryKind.RUBRIC
    assert '[R1]' in str(block.content) and '[R2]' in str(block.content)
    assert '需人工确认' in str(block.content)


def test_a_rubric_block_inherits_the_strictest_trust() -> None:
    assert rubric_block([_rubric('R1'), _rubric('R2')]).trust is TrustLevel.TRUSTED
    mixed = rubric_block([_rubric('R1'), _rubric('R2', trust=TrustLevel.UNTRUSTED)])
    assert mixed.trust is TrustLevel.UNTRUSTED


def test_the_rubric_key_changes_when_the_set_changes() -> None:
    before = rubric_key([_rubric('R1')])
    assert rubric_key([_rubric('R1')]) == before
    assert rubric_key([_rubric('R1'), _rubric('R2')]) != before


def test_the_rubric_key_and_block_ignore_store_order() -> None:
    # 基数 3、既非升序亦非降序：store 交回的顺序变了，键与渲染字节都不变（前缀缓存）。
    a, b, c = _rubric('R2'), _rubric('R3'), _rubric('R1')
    assert rubric_key([a, b, c]) == rubric_key([c, a, b])
    assert rubric_block([a, b, c]).content == rubric_block([c, a, b]).content


def test_the_rubric_key_tracks_trust() -> None:
    # 信任变了就是新集合：否则投影改成不可信后模型还看着那份可信的旧块（评审 M6）。
    assert rubric_key([_rubric('R1')]) != rubric_key([_rubric('R1', trust=TrustLevel.UNTRUSTED)])


def test_verify_anchors_accepts_a_reopenable_anchor() -> None:
    anchors = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'add a --json flag'),)
    assert verify_anchors(anchors, {'msg:1': 'please add a --json flag to it'}) == []


def test_verify_anchors_rejects_a_missing_locator() -> None:
    anchors = (Anchor(AnchorSource.FILE, 'spec.md:99', 'real text here'),)
    problems = verify_anchors(anchors, {'spec.md:12': 'real text'})
    assert len(problems) == 1 and 'spec.md:99' in problems[0]


def test_verify_anchors_rejects_a_fabricated_quote() -> None:
    anchors = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', 'delete production'),)
    problems = verify_anchors(anchors, {'msg:1': 'please tidy the report'})
    assert len(problems) == 1 and '找不到' in problems[0]


def test_verify_anchors_rejects_empty_and_trivial_quotes() -> None:
    # 空引文、一两个字的引文对什么都「命中」，等于没锚点（评审 H4）。
    source = {'msg:1': 'please ship a report with section 4'}
    for quote in ('', '   ', 'e', 'a b'):
        anchors = (Anchor(AnchorSource.USER_MESSAGE, 'msg:1', quote),)
        assert verify_anchors(anchors, source), quote


def test_verify_anchors_rejects_a_source_that_does_not_match_the_locator() -> None:
    source = {'msg:1': 'please ship a report with section 4'}
    anchors = (Anchor(AnchorSource.FILE, 'msg:1', 'ship a report'),)
    assert verify_anchors(anchors, source)


def test_meaningful_quote_needs_four_non_space_characters() -> None:
    assert meaningful_quote('a b c d') is True
    assert meaningful_quote('a b c') is False
    assert meaningful_quote('第四节') is False
    assert meaningful_quote('第四节内容') is True


def _agent(script: list[ScriptedTurn]) -> BaseAgent:
    options = AgentOptions(name='Worker', llm=ScriptedLLM(script), tools={}, system_prompt='SP')
    return BaseAgent(options, hooks=HooksCenter(), events=EventBus())


async def test_the_seam_injects_after_the_task_and_before_the_first_model_call() -> None:
    # Goal 可能就是这条任务本身，所以注入在任务进轨迹之后；第一轮模型调用就看得到投影结果。
    agent = _agent([ScriptedTurn(content='done')])
    goal = Goal(intent='ship a report', anchors=_ANCHOR)
    rubrics = [_rubric('R1')]

    async def inject(ctx: Context) -> None:
        if not has_block(ctx.current_traj, goal_key(goal)):
            ctx.append(goal_block(goal))
        if not has_block(ctx.current_traj, rubric_key(rubrics)):
            ctx.append(rubric_block(rubrics))

    agent._attach_rubrics(inject)  # pyright: ignore[reportPrivateUsage]
    await agent.run('do the work')
    first_request = cast('ScriptedLLM', agent.options.llm).calls[0]
    kinds = [kind_of(b) for b in first_request]
    query_at = next(
        i for i, b in enumerate(first_request) if b.role == 'user' and kind_of(b) is None
    )
    assert query_at < kinds.index(HistoryKind.GOAL) < kinds.index(HistoryKind.RUBRIC)


async def test_the_seam_is_idempotent_across_cycles() -> None:
    agent = _agent([ScriptedTurn(content='a'), ScriptedTurn(content='b')])
    goal = Goal(intent='ship a report', anchors=_ANCHOR)

    async def inject(ctx: Context) -> None:
        if not has_block(ctx.current_traj, goal_key(goal)):
            ctx.append(goal_block(goal))

    agent._attach_rubrics(inject)  # pyright: ignore[reportPrivateUsage]
    await agent.run('cycle one')
    ctx = await agent.run('cycle two')
    assert sum(kind_of(b) is HistoryKind.GOAL for b in ctx.current_traj) == 1
