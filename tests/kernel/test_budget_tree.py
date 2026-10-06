"""预算树（spec §8.4）：切分即预留、周期收尾结算、唤醒重切、准入按估算预占、截止时刻继承。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from syndica.kernel.budget import Budget, BudgetPolicy, Prices, StaticPrices, Usage
from syndica.kernel.budget_tree import BudgetAccount, BudgetExhausted, BudgetRefused, BudgetTree
from syndica.kernel.context import ContextBlock
from syndica.kernel.llm import LLMConfig


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Model:
    def __init__(self, name: str, **request_args: Any) -> None:
        self.config = LLMConfig(provider='scripted', model=name, request_args=request_args)


def _reply(*, route: str | None = None, **usage: int | None) -> ContextBlock:
    extra: dict[str, object] = {'usage': dict(usage)}
    if route is not None:
        extra['llm_route'] = route
    return ContextBlock(role='assistant', content='ok', extra=extra)


def _tree(root: Budget | None = None, **policy: Any) -> BudgetTree:
    return BudgetTree(BudgetPolicy(root=root or Budget(), **policy))


def _acct(tree: BudgetTree, agent_id: str) -> BudgetAccount:
    found = tree.account(agent_id)
    assert found is not None
    return found


def _turns(account: BudgetAccount, n: int) -> None:
    for _ in range(n):
        account.take_turn()


# ---- 切分 ----


def test_an_unrequested_dimension_gets_half_of_what_the_parent_can_give() -> None:
    tree = _tree()
    parent = tree.carve('p', None, Budget(turns=9, tokens=1001, cost=Decimal('1.00')))
    _turns(parent, 3)
    child = tree.carve('c', 'p', None)
    assert child.view().limit == Budget(turns=3, tokens=501, cost=Decimal('0.50'))


def test_an_explicit_request_within_what_the_parent_can_give_is_the_childs_cap() -> None:
    tree = _tree()
    tree.carve('p', None, Budget(turns=10, tokens=1000))
    child = tree.carve('c', 'p', Budget(turns=7))
    assert child.view().limit == Budget(turns=7, tokens=500)


def test_a_request_beyond_what_the_parent_can_give_is_refused_naming_the_dimension() -> None:
    tree = _tree()
    parent = tree.carve('p', None, Budget(turns=10, tokens=1000))
    parent.charge(Usage(input_tokens=600))
    with pytest.raises(BudgetRefused) as caught:
        tree.carve('c', 'p', Budget(tokens=401))
    assert 'tokens' in caught.value.received and '400' in caught.value.expected
    assert tree.account('c') is None


def test_a_zero_request_is_refused() -> None:
    tree = _tree()
    tree.carve('p', None, Budget(turns=10))
    with pytest.raises(BudgetRefused) as caught:
        tree.carve('c', 'p', Budget(turns=0))
    assert 'turns=0' in caught.value.received


def test_a_parent_with_nothing_left_cannot_split_off_a_child() -> None:
    tree = _tree()
    parent = tree.carve('p', None, Budget(turns=2))
    _turns(parent, 2)
    with pytest.raises(BudgetRefused) as caught:
        tree.carve('c', 'p', None)
    assert 'turns' in caught.value.received and '追加' in caught.value.next_action


def test_an_unlimited_parent_gives_an_unlimited_child_unless_it_asks_for_a_cap() -> None:
    tree = _tree()
    tree.carve('p', None, None)
    assert tree.carve('free', 'p', None).view().limit == Budget()
    assert tree.carve('capped', 'p', Budget(turns=4)).view().limit == Budget(turns=4)


def test_top_level_agents_draw_from_the_pool_without_splitting_it() -> None:
    tree = _tree(Budget(turns=10))
    first = tree.carve('a', None, None)
    second = tree.carve('b', None, None)
    assert first.view().limit == Budget(turns=10)
    _turns(first, 6)
    assert second.view().remaining.turns == 4
    _turns(second, 4)
    assert first.exhausted() == ('turns',) and second.exhausted() == ('turns',)


def test_live_children_reserve_and_their_reservations_never_exceed_the_parent() -> None:
    tree = _tree()
    parent = tree.carve('p', None, Budget(turns=10))
    caps = [tree.carve(name, 'p', None).view().limit.turns for name in ('a', 'b', 'c')]
    assert caps == [5, 3, 1]
    assert parent.view().remaining.turns == 1
    parent.take_turn()
    assert parent.exhausted() == ('turns',)


def test_a_child_spends_its_own_reservation_not_the_parents_room() -> None:
    tree = _tree()
    parent = tree.carve('p', None, Budget(turns=10))
    child = tree.carve('c', 'p', Budget(turns=4))
    _turns(child, 4)
    assert child.exhausted() == ('turns',)
    assert parent.view().remaining.turns == 6
    assert parent.view().used.turns == 4


def test_siblings_do_not_share_a_node() -> None:
    tree = _tree()
    tree.carve('p', None, Budget(turns=30))
    left = tree.carve('l', 'p', Budget(turns=5))
    right = tree.carve('r', 'p', Budget(turns=5))
    _turns(left, 2)
    assert (left.view().used.turns, right.view().used.turns) == (2, 0)
    assert right.view().remaining.turns == 5


# ---- 周期结算与唤醒 ----


def test_ending_a_cycle_returns_the_unused_reservation_to_the_parent() -> None:
    tree = _tree()
    parent = tree.carve('p', None, Budget(turns=10))
    child = tree.carve('c', 'p', Budget(turns=5))
    _turns(child, 2)
    assert parent.view().remaining.turns == 5
    child.end_cycle()
    assert parent.view().remaining.turns == 8
    assert child.view().remaining.turns == 0


def test_the_next_cycle_carves_the_cap_back_from_the_parent() -> None:
    tree = _tree()
    parent = tree.carve('p', None, Budget(turns=10))
    child = tree.carve('c', 'p', Budget(turns=5))
    _turns(child, 2)
    child.end_cycle()
    child.begin_cycle()
    assert child.view().remaining.turns == 3
    assert parent.view().remaining.turns == 5


def test_a_wake_gets_only_what_the_parent_still_has() -> None:
    tree = _tree()
    parent = tree.carve('p', None, Budget(turns=10))
    child = tree.carve('c', 'p', Budget(turns=5))
    _turns(child, 2)
    child.end_cycle()
    _turns(parent, 7)
    assert child.unwakeable() == ()
    child.begin_cycle()
    assert child.view().remaining.turns == 1
    child.take_turn()
    child.end_cycle()
    _turns(parent, 1)
    assert child.unwakeable() == ('turns',)


def test_a_wake_under_a_settled_parent_is_paid_by_the_nearest_running_ancestor() -> None:
    tree = _tree()
    top = tree.carve('top', None, Budget(turns=20))
    middle = tree.carve('mid', 'top', Budget(turns=8))
    leaf = tree.carve('leaf', 'mid', Budget(turns=4))
    _turns(leaf, 1)
    leaf.end_cycle()
    _turns(middle, 1)
    middle.end_cycle()
    assert top.view().remaining.turns == 18
    leaf.begin_cycle()
    assert leaf.view().remaining.turns == 3
    assert top.view().remaining.turns == 15
    assert middle.view().remaining.turns == 0


def test_siblings_woken_together_each_take_half_of_what_is_left_so_none_starves() -> None:
    tree = _tree()
    parent = tree.carve('p', None, Budget(turns=100))
    kids = []
    for name in ('a', 'b', 'c'):
        kid = tree.carve(name, 'p', None)
        kid.take_turn()
        kid.end_cycle()
        kids.append(kid)
    for kid in kids:
        kid.begin_cycle()
    left = [kid.view().remaining.turns for kid in kids]
    assert left == [49, 24, 12]
    assert parent.view().remaining.turns == 100 - (50 + 25 + 13)


def test_a_lazily_started_child_reserves_nothing_until_its_first_cycle() -> None:
    tree = _tree()
    parent = tree.carve('p', None, Budget(turns=10))
    lazy = tree.carve('lazy', 'p', Budget(turns=4), running=False)
    assert parent.view().remaining.turns == 10
    lazy.begin_cycle()
    assert parent.view().remaining.turns == 6


def test_closing_a_node_keeps_what_its_subtree_spent_and_frees_its_reservation() -> None:
    tree = _tree()
    parent = tree.carve('p', None, Budget(turns=10))
    child = tree.carve('c', 'p', Budget(turns=4))
    grandchild = tree.carve('g', 'c', Budget(turns=2))
    child.take_turn()
    grandchild.take_turn()
    tree.close('c')
    tree.close('c')
    assert tree.account('c') is None and tree.account('g') is None
    assert parent.view().used.turns == 2
    assert parent.view().remaining.turns == 8


# ---- wall clock ----


def test_a_childs_deadline_is_never_later_than_its_parents() -> None:
    clock = _Clock()
    tree = BudgetTree(BudgetPolicy(), clock=clock)
    tree.carve('p', None, Budget(wall_clock_s=60))
    clock.now += 20
    late = tree.carve('late', 'p', Budget(wall_clock_s=600))
    early = tree.carve('early', 'p', Budget(wall_clock_s=10))
    inherited = tree.carve('inherited', 'p', None)
    assert late.view().remaining.wall_clock_s == 40
    assert early.view().remaining.wall_clock_s == 10
    assert inherited.view().remaining.wall_clock_s == 40
    clock.now += 10
    assert early.exhausted() == ('wall_clock_s',)
    assert late.exhausted() == () and inherited.exhausted() == ()
    clock.now += 30
    assert late.exhausted() == ('wall_clock_s',)


def test_the_pool_deadline_starts_at_the_first_spawn() -> None:
    clock = _Clock()
    tree = BudgetTree(BudgetPolicy(root=Budget(wall_clock_s=30)), clock=clock)
    clock.now += 1000
    agent = tree.carve('a', None, None)
    assert agent.view().remaining.wall_clock_s == 30
    clock.now += 30
    assert agent.exhausted() == ('wall_clock_s',)


# ---- 成本未知、伴生 ----


def test_unknown_cost_exhausts_a_cost_cap_instead_of_counting_as_free() -> None:
    tree = _tree()
    capped = tree.carve('capped', None, Budget(cost=Decimal('5')))
    free = tree.carve('free', None, None)
    capped.charge(Usage(input_tokens=1, cost=None))
    free.charge(Usage(input_tokens=1, cost=None))
    assert capped.exhausted() == ('cost',)
    assert capped.view().used.cost is None
    assert free.exhausted() == ()


def test_a_companion_spends_its_agents_money_but_not_its_turns() -> None:
    tree = _tree()
    agent = tree.carve('a', None, Budget(turns=1, tokens=100))
    agent.take_turn()
    companion = agent.for_companion()
    companion.take_turn()
    companion.end_cycle()
    assert agent.view().used.turns == 1
    assert agent.exhausted() == ('turns',)
    assert companion.exhausted() == ()
    companion.charge(Usage(input_tokens=100))
    assert agent.view().used.tokens == 100
    assert companion.exhausted() == ('tokens',)


# ---- 准入 ----


def test_admission_reserves_the_estimate_so_a_second_call_cannot_overlap_it() -> None:
    tree = _tree()
    agent = tree.carve('a', None, Budget(tokens=5000))
    request = {'messages': [ContextBlock(role='user', content='x' * 4000)]}
    model = _Model('m', max_tokens=1000)
    ticket = agent.admit(model, request)
    assert ticket.tokens > 1000
    assert agent.view().remaining.tokens == 5000 - ticket.tokens
    with pytest.raises(BudgetExhausted) as caught:
        for _ in range(5):
            agent.admit(model, request)
    assert 'tokens' in caught.value.expected


def test_a_released_admission_returns_its_estimate() -> None:
    tree = _tree()
    agent = tree.carve('a', None, Budget(tokens=5000))
    ticket = agent.admit(_Model('m', max_tokens=100), {'messages': []})
    agent.release(ticket)
    assert agent.view().remaining.tokens == 5000


def test_settling_replaces_the_estimate_with_what_was_reported() -> None:
    tree = _tree()
    agent = tree.carve('a', None, Budget(tokens=5000))
    model = _Model('m', max_tokens=100)
    ticket = agent.admit(model, {'messages': []})
    agent.settle(ticket, model, {'messages': []}, _reply(input_tokens=30, output_tokens=12))
    assert agent.view().remaining.tokens == 5000 - 42


def test_admission_under_a_cost_cap_refuses_a_model_without_a_price() -> None:
    tree = _tree(prices=StaticPrices({}))
    agent = tree.carve('a', None, Budget(cost=Decimal('1')))
    with pytest.raises(BudgetExhausted) as caught:
        agent.admit(_Model('mystery'), {'messages': []})
    assert '价格未知' in caught.value.received


def test_admission_after_the_deadline_is_refused() -> None:
    clock = _Clock()
    tree = BudgetTree(BudgetPolicy(), clock=clock)
    agent = tree.carve('a', None, Budget(wall_clock_s=5))
    clock.now += 5
    with pytest.raises(BudgetExhausted) as caught:
        agent.admit(_Model('m'), {'messages': []})
    assert 'wall_clock_s' in caught.value.expected


def test_the_input_estimate_is_calibrated_by_the_last_report() -> None:
    tree = _tree()
    agent = tree.carve('a', None, None)
    model = _Model('m', max_tokens=10)
    request = {'messages': [ContextBlock(role='user', content='汉' * 400)]}
    first = agent.admit(model, request)
    agent.settle(first, model, request, _reply(input_tokens=first.raw_input * 3, output_tokens=1))
    second = agent.admit(model, request)
    assert second.tokens - 10 == first.raw_input * 3


# ---- 提醒与追加 ----


def test_each_threshold_is_announced_once_and_a_jump_announces_only_the_lowest() -> None:
    tree = _tree()
    agent = tree.carve('a', None, Budget(turns=100))
    agent.charge(Usage(turns=40))
    assert agent.crossed_threshold() is None
    agent.charge(Usage(turns=10))
    assert agent.crossed_threshold() == 50
    assert agent.crossed_threshold() is None
    agent.charge(Usage(turns=45))
    assert agent.crossed_threshold() == 10
    assert agent.crossed_threshold() is None


def test_another_account_on_the_same_node_sees_the_same_reminders() -> None:
    tree = _tree()
    tree.carve('a', None, Budget(turns=10))
    _acct(tree, 'a').charge(Usage(turns=6))
    assert _acct(tree, 'a').crossed_threshold() == 50
    assert _acct(tree, 'a').crossed_threshold() is None


def test_a_grant_raises_the_node_and_every_capped_level_and_rearms_the_reminders() -> None:
    tree = _tree(Budget(turns=30))
    parent = tree.carve('p', None, Budget(turns=10))
    child = tree.carve('c', 'p', Budget(turns=4))
    _turns(child, 4)
    assert child.crossed_threshold() == 10
    child.grant(Budget(turns=6, tokens=500))
    assert child.view().limit == Budget(turns=10)
    assert parent.view().limit.turns == 16
    assert tree.pool().view().limit.turns == 36
    assert parent.view().remaining.turns == 6
    assert child.exhausted() == ()
    assert child.crossed_threshold() is None
    child.take_turn()
    assert child.crossed_threshold() == 50


# ---- 计量 ----


def test_metering_records_three_tiers_and_prices_them() -> None:
    prices = StaticPrices(
        {'m': Prices(input=Decimal('2'), output=Decimal('8'), cached_input=Decimal('0.5'))}
    )
    tree = _tree(prices=prices)
    agent = tree.carve('a', None, None)
    agent.settle(
        None,
        _Model('m'),
        {'messages': []},
        _reply(input_tokens=1_000_000, output_tokens=500_000, cached_input_tokens=400_000),
    )
    used = agent.view().used
    assert (used.input_tokens, used.output_tokens, used.cached_input_tokens) == (
        1_000_000,
        500_000,
        400_000,
    )
    assert used.cost == Decimal('1.2') + Decimal('0.2') + Decimal('4')
    assert used.estimated is False


def test_metering_prices_the_platform_that_answered() -> None:
    cheap = Prices(input=Decimal('1'), output=Decimal('1'), cached_input=Decimal('1'))
    dear = Prices(input=Decimal('9'), output=Decimal('9'), cached_input=Decimal('9'))
    tree = _tree(prices=StaticPrices({'m': cheap, 'other:m': dear}))
    agent = tree.carve('a', None, None)
    agent.settle(
        None, _Model('m'), {}, _reply(route='other', input_tokens=1_000_000, output_tokens=0)
    )
    assert agent.view().used.cost == Decimal(9)


def test_metering_estimates_missing_counts_and_marks_them() -> None:
    tree = _tree()
    agent = tree.carve('a', None, None)
    request = {'messages': [ContextBlock(role='user', content='x' * 400)]}
    agent.settle(None, _Model('m'), request, _reply(input_tokens=None, output_tokens=7))
    used = agent.view().used
    assert used.input_tokens > 0 and used.output_tokens == 7 and used.estimated is True


def test_metering_an_unknown_model_records_unknown_cost() -> None:
    tree = _tree(prices=StaticPrices({}))
    agent = tree.carve('a', None, None)
    agent.settle(None, _Model('mystery'), {}, _reply(input_tokens=5, output_tokens=5))
    assert agent.view().used.cost is None


def test_cached_tokens_over_the_input_count_are_clamped() -> None:
    tree = _tree()
    agent = tree.carve('a', None, None)
    agent.settle(
        None, _Model('m'), {}, _reply(input_tokens=10, output_tokens=1, cached_input_tokens=50)
    )
    assert agent.view().used.cached_input_tokens == 10
