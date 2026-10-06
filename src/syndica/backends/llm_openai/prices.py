"""这个 backend 认得的默认价格（spec §8.4：价格是外部事实，内核不内置；provider backend 给默认值，
用户在 `.syndica.yaml` 的 `prices:` 里覆盖）。

只收确定是 0 价的：OpenRouter 的 `:free` 模型与 `openrouter/free` 路由。别的模型价格会变、分档，
写死在这里就会过时——不认识就交回 None（价格未知），设了成本上限的 run 于是在 spawn 处被拒，提示去
`prices:` 里写。
"""

from __future__ import annotations

from decimal import Decimal

from syndica.kernel.budget import Prices

__all__ = ['FreeTierPrices']

_FREE = Prices(input=Decimal(0), output=Decimal(0), cached_input=Decimal(0))


class FreeTierPrices:
    """OpenRouter 免费模型的价格表：`*:free` 与 `openrouter/free` 是 0，其余不认识。"""

    def price_of(self, model: str, platform: str | None = None) -> Prices | None:
        if model == 'openrouter/free' or model.endswith(':free'):
            return _FREE
        return None
