"""预算与长程的类型（spec §8.4 / §8.6）：取值校验、三档计量、价格表、进度比例、各枚举的成员表。"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from syndica.kernel.budget import (
    Budget,
    BudgetPolicy,
    BudgetView,
    Exhaustion,
    Prices,
    StaticPrices,
    Usage,
)
from syndica.kernel.context import StopReason
from syndica.kernel.errors import ConfigError
from syndica.kernel.long_horizon import BudgetMove, DriftMove, ProgressSnapshot, StallMove


@pytest.mark.parametrize(
    'field',
    [
        {'tokens': True},
        {'tokens': -1},
        {'tokens': 1.5},
        {'turns': '3'},
        {'turns': -2},
        {'cost': 1.5},
        {'cost': Decimal('-1')},
        {'cost': Decimal('NaN')},
        {'wall_clock_s': True},
        {'wall_clock_s': -1.0},
        {'wall_clock_s': math.inf},
    ],
    ids=[
        'bool_tokens',
        'negative_tokens',
        'float_tokens',
        'string_turns',
        'negative_turns',
        'float_cost',
        'negative_cost',
        'nan_cost',
        'bool_seconds',
        'negative_seconds',
        'infinite_seconds',
    ],
)
def test_a_budget_rejects_values_that_would_silently_mean_something_else(field: object) -> None:
    # YAML 1.1 把 yes 读成 True、True 在比较里等于 1；浮点金额会把 0.1 变成 0.1000…055（AGENTS）。
    with pytest.raises(ConfigError):
        Budget(**field)  # type: ignore[arg-type]


def test_a_budget_with_no_dimension_set_is_unlimited() -> None:
    assert Budget().unlimited
    assert not Budget(turns=0).unlimited  # 0 是一个上限（一轮也不给），不是「不限」


def test_tokens_count_input_and_output_and_cached_is_part_of_input() -> None:
    usage = Usage(input_tokens=100, output_tokens=20, cached_input_tokens=60)
    assert usage.tokens == 120
    with pytest.raises(ConfigError):
        Usage(input_tokens=10, cached_input_tokens=11)


def test_an_unknown_cost_stays_unknown_when_added() -> None:
    # 价格未知不是 0 价：加上一笔未知，总数还是未知（§8.4）。
    known = Usage(input_tokens=1, cost=Decimal('0.5'), turns=1)
    unknown = Usage(input_tokens=1, cost=None, estimated=True)
    total = known + unknown
    assert total.cost is None and total.estimated is True
    assert (total.input_tokens, total.turns) == (2, 1)
    assert (known + known).cost == Decimal('1.0')


def test_cost_is_priced_per_tier_and_cached_input_is_not_charged_as_fresh_input() -> None:
    prices = Prices(input=Decimal('3'), output=Decimal('15'), cached_input=Decimal('0.3'))
    cost = prices.cost_of(
        input_tokens=1_000_000, output_tokens=100_000, cached_input_tokens=800_000
    )
    assert cost == Decimal('0.6') + Decimal('0.24') + Decimal('1.5')


def test_an_override_table_falls_back_and_knows_nothing_is_not_zero() -> None:
    base = StaticPrices({'shared': Prices(Decimal('1'), Decimal('2'), Decimal('0.1'))})
    override = StaticPrices({'mine': Prices(Decimal(0), Decimal(0), Decimal(0))}, fallback=base)
    assert override.price_of('mine') == Prices(Decimal(0), Decimal(0), Decimal(0))
    assert override.price_of('shared') == base.price_of('shared')
    assert override.price_of('unknown') is None


@pytest.mark.parametrize('bad', [{'': Prices(Decimal(1), Decimal(1), Decimal(1))}, {'m': 1}])
def test_a_price_table_rejects_a_bad_entry(bad: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        StaticPrices(bad)  # type: ignore[arg-type]


def test_progress_counts_only_mandatory_checkable_rubrics() -> None:
    assert ProgressSnapshot().ratio is None  # 没有可量的东西，不是 0 也不是 1
    snapshot = ProgressSnapshot(satisfied=('R1', 'X9'), mandatory=('R1', 'R2', 'R3'))
    assert snapshot.ratio == pytest.approx(1 / 3)
    assert not snapshot.complete
    assert ProgressSnapshot(satisfied=('R2', 'R1'), mandatory=('R1', 'R2')).complete
    assert not ProgressSnapshot(satisfied=('R1',), mandatory=()).complete


def test_the_new_enum_values_are_exactly_these() -> None:
    # 整张成员→取值表：取值撞车会让成员静默变成别名（AGENTS 变异纪律），这些取值进 trace 与配置。
    assert {m.name: m.value for m in Exhaustion} == {
        'GRACEFUL_WRAPUP': 'graceful_wrapup',
        'HARD_STOP': 'hard_stop',
        'ASK_HUMAN': 'ask_human',
    }
    assert {m.name: m.value for m in StopReason} == {
        'COMPLETED': 'completed',
        'ABORTED': 'aborted',
        'ERROR': 'error',
        'MAX_TURNS': 'max_turns',
        'BUDGET': 'budget',
    }
    assert {m.name: m.value for m in StallMove} == {
        'CONTINUE': 'continue',
        'NUDGE': 'nudge',
    }
    assert {m.name: m.value for m in BudgetMove} == {
        'CONTINUE': 'continue',
        'WRAP_UP': 'wrap_up',
    }
    assert {m.name: m.value for m in DriftMove} == {
        'CONTINUE': 'continue',
        'REMIND': 'remind',
    }


_ONE = Prices(input=Decimal(1), output=Decimal(1), cached_input=Decimal(1))
_TWO = Prices(input=Decimal(2), output=Decimal(2), cached_input=Decimal(2))


def test_a_platform_specific_price_wins_over_the_bare_model_name() -> None:
    table = StaticPrices({'m': _ONE, 'openrouter:m': _TWO})
    assert table.price_of('m', 'openrouter') is _TWO
    assert table.price_of('m', 'gemini') is _ONE
    assert table.price_of('m') is _ONE


def test_the_fallback_is_asked_with_the_same_platform() -> None:
    table = StaticPrices({}, fallback=StaticPrices({'gemini:m': _TWO}))
    assert table.price_of('m', 'gemini') is _TWO
    assert table.price_of('m') is None


async def _ask(agent_id: str, view: BudgetView) -> Budget | None:
    return None


class _AsyncCallable:
    async def __call__(self, agent_id: str, view: BudgetView) -> Budget | None:
        return None


def _sync_ask(agent_id: str, view: BudgetView) -> Budget | None:
    return None


async def _one_arg(agent_id: str) -> Budget | None:
    return None


def test_a_budget_policy_takes_an_async_function_or_an_async_callable_instance() -> None:
    assert BudgetPolicy(ask_human=_ask).ask_human is _ask
    BudgetPolicy(ask_human=_AsyncCallable())


@pytest.mark.parametrize(
    ('changes', 'anchor'),
    [
        ({'root': {'turns': 3}}, 'root'),
        ({'on_exhausted': 'hard_stop'}, 'on_exhausted'),
        ({'prices': {'m': _ONE}}, 'price_of'),
        ({'ask_human': _sync_ask}, 'async def'),
        ({'ask_human': _AsyncCallable}, '不要传类本身'),
        ({'ask_human': _one_arg}, '两个位置参数'),
        ({'ask_timeout_s': True}, 'ask_timeout_s'),
        ({'ask_timeout_s': 0}, 'ask_timeout_s'),
        ({'ask_timeout_s': math.inf}, 'ask_timeout_s'),
        ({'ask_timeout_s': '30'}, 'ask_timeout_s'),
    ],
)
def test_a_malformed_budget_policy_is_refused_naming_what_to_fix(
    changes: dict[str, object], anchor: str
) -> None:
    with pytest.raises(ConfigError) as caught:
        BudgetPolicy(**changes)  # type: ignore[arg-type]
    assert anchor in str(caught.value)
