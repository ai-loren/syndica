"""目标导向型压缩（spec §8.8 第四种）：旧轮次按与「还需要什么」的相关度留，而不是按新旧。"""

from __future__ import annotations

import pytest

from syndica.kernel.compaction import (
    CompactionController,
    CompactionRequest,
    CompactionResult,
    GoalDirectedCompaction,
    RuleBasedCompaction,
    apply_compaction,
    traj_tokens,
)
from syndica.kernel.context import ContextBlock, ToolCall
from syndica.kernel.errors import ConfigError
from syndica.kernel.history_blocks import HistoryKind, signed_block

_FILLER = ' lorem' * 30


def _turn(text: str) -> ContextBlock:
    return ContextBlock(role='assistant', content=text + _FILLER)


def _batch(call_id: str, args: str, output: str) -> list[ContextBlock]:
    return [
        ContextBlock(
            role='assistant', tool_calls=[ToolCall(id=call_id, name='Bash', arguments=args)]
        ),
        ContextBlock(role='tool', content=output + _FILLER, tool_call_id=call_id),
    ]


def _recent() -> list[ContextBlock]:
    return [
        ContextBlock(role='user', content='continue'),
        ContextBlock(role='assistant', content='working on the last step'),
    ]


def _room_for_one_older(head: list[ContextBlock], older: list[ContextBlock]) -> int:
    return traj_tokens([*head, *_recent()]) + traj_tokens([older[0]])


def _kept_text(blocks: list[ContextBlock]) -> str:
    return ' '.join(f'{b.content} {[c.arguments for c in b.tool_calls]}' for b in blocks)


async def test_an_old_relevant_turn_outlives_newer_irrelevant_ones() -> None:
    head = [ContextBlock(role='user', content='go')]
    older = [
        _turn('constraint: invoice numbering stays monotonic'),
        _turn('scratch debug output qwzx'),
        _turn('scratch trace dump vjkp'),
    ]
    blocks = [*head, *older, *_recent()]
    request = CompactionRequest(
        blocks=blocks,
        budget_tokens=_room_for_one_older(head, older),
        protected=frozenset({0}),
        needed=('Invoice numbering must stay monotonic',),
    )
    result = await GoalDirectedCompaction(min_keep_turns=2).compact(request)
    kept = _kept_text(result.kept)
    assert 'monotonic' in kept
    assert 'qwzx' not in kept and 'vjkp' not in kept
    assert apply_compaction(request, result) == result.kept

    recency = await RuleBasedCompaction(min_keep_turns=2).compact(request)
    assert 'vjkp' in _kept_text(recency.kept) and 'monotonic' not in _kept_text(recency.kept)


async def test_tool_arguments_count_toward_relevance_and_batches_stay_paired() -> None:
    head = [ContextBlock(role='user', content='go')]
    older = [
        *_batch('c1', '{"command": "cat schema/ledger.sql"}', 'ok'),
        *_batch('c2', '{"command": "ls /tmp/qwzx"}', 'ok'),
        *_batch('c3', '{"command": "echo vjkp"}', 'ok'),
    ]
    blocks = [*head, *older, *_recent()]
    one_batch = traj_tokens(older[:2])
    request = CompactionRequest(
        blocks=blocks,
        budget_tokens=traj_tokens([*head, *_recent()]) + one_batch,
        protected=frozenset({0}),
        needed=('the ledger schema must not change',),
    )
    result = await GoalDirectedCompaction(min_keep_turns=2).compact(request)
    kept = _kept_text(result.kept)
    assert 'ledger.sql' in kept
    assert 'qwzx' not in kept and 'vjkp' not in kept
    assert apply_compaction(request, result) == result.kept


async def test_without_needed_terms_come_from_the_latest_goal_and_rubric_blocks() -> None:
    head = [
        signed_block(HistoryKind.GOAL, 'Goal: render charts for qwzx', key='goal:1'),
        signed_block(HistoryKind.GOAL, 'Goal: migrate the billing ledger', key='goal:2'),
        signed_block(HistoryKind.RUBRIC, 'Rubric: invoice numbering monotonic', key='rubric:1'),
    ]
    older = [
        _turn('the billing ledger lives in schema b'),
        _turn('invoice numbering uses a sequence'),
        _turn('rendered qwzx charts'),
    ]
    blocks = [*head, *older, *_recent()]
    request = CompactionRequest(
        blocks=blocks,
        budget_tokens=traj_tokens([*head, *_recent()]) + traj_tokens(older[:2]),
        protected=frozenset({1, 2}),
    )
    result = await GoalDirectedCompaction(min_keep_turns=2).compact(request)
    kept = _kept_text(result.kept)
    assert 'billing ledger lives' in kept and 'sequence' in kept
    assert 'rendered qwzx' not in kept


async def test_without_goal_blocks_terms_come_from_the_first_task_message() -> None:
    head = [ContextBlock(role='user', content='Migrate the billing ledger to postgres')]
    older = [
        _turn('postgres ledger connection settled'),
        _turn('scratch qwzx'),
        _turn('scratch vjkp'),
    ]
    blocks = [*head, *older, *_recent()]
    request = CompactionRequest(
        blocks=blocks,
        budget_tokens=_room_for_one_older(head, older),
        protected=frozenset({0}),
    )
    result = await GoalDirectedCompaction(min_keep_turns=2).compact(request)
    kept = _kept_text(result.kept)
    assert 'connection settled' in kept and 'qwzx' not in kept and 'vjkp' not in kept


async def test_equally_relevant_turns_prefer_the_newer_one() -> None:
    head = [ContextBlock(role='user', content='go')]
    older = [_turn('scratch aaaa'), _turn('scratch cccc'), _turn('scratch bbbb')]
    blocks = [*head, *older, *_recent()]
    request = CompactionRequest(
        blocks=blocks,
        budget_tokens=_room_for_one_older(head, older),
        protected=frozenset({0}),
        needed=('unrelated requirement',),
    )
    result = await GoalDirectedCompaction(min_keep_turns=2).compact(request)
    kept = _kept_text(result.kept)
    assert 'bbbb' in kept and 'aaaa' not in kept and 'cccc' not in kept


async def test_more_relevant_wins_over_less_relevant() -> None:
    head = [ContextBlock(role='user', content='go')]
    older = [
        _turn('invoice numbering monotonic gapless'),
        _turn('invoice only'),
        _turn('nothing here'),
    ]
    blocks = [*head, *older, *_recent()]
    request = CompactionRequest(
        blocks=blocks,
        budget_tokens=_room_for_one_older(head, older),
        protected=frozenset({0}),
        needed=('invoice numbering monotonic gapless',),
    )
    result = await GoalDirectedCompaction(min_keep_turns=2).compact(request)
    kept = _kept_text(result.kept)
    assert 'gapless' in kept and 'invoice only' not in kept


async def test_a_turn_too_large_to_fit_does_not_stop_smaller_ones_behind_it() -> None:
    head = [ContextBlock(role='user', content='go')]
    older = [
        _turn('scratch small aaaa'),
        ContextBlock(role='assistant', content='invoice monotonic ' + 'x' * 4000),
        _turn('scratch small bbbb'),
    ]
    blocks = [*head, *older, *_recent()]
    request = CompactionRequest(
        blocks=blocks,
        budget_tokens=traj_tokens([*head, *_recent()]) + traj_tokens([older[0], older[2]]),
        protected=frozenset({0}),
        needed=('invoice monotonic',),
    )
    result = await GoalDirectedCompaction(min_keep_turns=2).compact(request)
    kept = _kept_text(result.kept)
    assert 'aaaa' in kept and 'bbbb' in kept and 'xxxx' not in kept


async def test_keeps_original_block_order_when_everything_fits() -> None:
    head = [ContextBlock(role='user', content='go')]
    older = [_turn('scratch cccc'), _turn('invoice aaaa'), _turn('scratch bbbb')]
    blocks = [*head, *older, *_recent()]
    request = CompactionRequest(blocks=blocks, budget_tokens=10_000, needed=('invoice',))
    result = await GoalDirectedCompaction(min_keep_turns=2).compact(request)
    assert result.kept == blocks


def test_the_controller_passes_its_needed_provider_into_the_request() -> None:
    controller = CompactionController(
        strategy=GoalDirectedCompaction(),
        threshold_tokens=100,
        target_tokens=50,
        needed=lambda: ['rubric one', 'goal two'],
    )
    request = controller.build_request([ContextBlock(role='user', content='go')])
    assert request.needed == ('rubric one', 'goal two')
    bare = CompactionController(
        strategy=GoalDirectedCompaction(), threshold_tokens=100, target_tokens=50
    )
    assert bare.build_request([]).needed == ()


@pytest.mark.parametrize('strategy', [GoalDirectedCompaction, RuleBasedCompaction])
@pytest.mark.parametrize(
    ('arg', 'value'),
    [('min_keep_turns', 0), ('min_keep_turns', True), ('max_output_chars', 0)],
)
def test_strategy_arguments_must_be_positive_integers(
    strategy: type[GoalDirectedCompaction | RuleBasedCompaction], arg: str, value: object
) -> None:
    with pytest.raises(ConfigError) as caught:
        strategy(**{arg: value})  # type: ignore[arg-type]
    assert arg in caught.value.expected and strategy.__name__ in caught.value.expected


def test_dropping_an_earlier_copy_of_the_protected_input_is_not_a_lost_protected_block() -> None:
    blocks = [
        ContextBlock(role='user', content='continue'),
        ContextBlock(role='assistant', content='first'),
        ContextBlock(role='user', content='continue'),
        ContextBlock(role='assistant', content='second'),
    ]
    request = CompactionRequest(blocks=blocks, budget_tokens=10_000, protected=frozenset({2}))
    kept = apply_compaction(request, CompactionResult(kept=[blocks[2], blocks[3]]))
    assert [b.content for b in kept] == ['continue', 'second']
