"""backend 的默认价格只收确定是 0 价的那几种；别的交回 None（价格未知，不是 0）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from syndica.backends.llm_openai import FreeTierPrices
from syndica.kernel.budget import PriceTable


@pytest.mark.parametrize('model', ['openrouter/free', 'google/gemma-3-27b-it:free', 'x:free'])
def test_free_models_cost_nothing(model: str) -> None:
    prices = FreeTierPrices().price_of(model, 'openrouter')
    assert prices is not None
    assert (prices.input, prices.output, prices.cached_input) == (Decimal(0),) * 3


@pytest.mark.parametrize('model', ['gemini-3.5-flash-lite', 'openrouter/auto', 'free', 'x:freebie'])
def test_anything_else_is_unknown_not_free(model: str) -> None:
    assert FreeTierPrices().price_of(model) is None


def test_it_is_a_price_table() -> None:
    assert isinstance(FreeTierPrices(), PriceTable)
