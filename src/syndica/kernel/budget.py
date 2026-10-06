"""预算与计量的类型（spec §8.4）：四维预算、三档 token 计量、价格表协议、耗尽处置。

预算是一等资源：它的目的不是给长跑设天花板，而是让长跑可控可研究——Agent 读得到自己还剩多少、据此决定
怎么分配。计量必须分输入 / 输出 / 缓存命中三档（§17 #69）：缓存命中的输入通常便宜一个数量级，不分档
就度量不了 §5.2 前缀缓存纪律的收益。价格是外部事实，内核不内置任何价格表：`PriceTable` 由 provider
backend 给默认值、宿主覆盖。
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Protocol, cast, runtime_checkable

from syndica.kernel.errors import ConfigError

__all__ = [
    'AskHuman',
    'Budget',
    'BudgetPolicy',
    'BudgetView',
    'Exhaustion',
    'PriceTable',
    'Prices',
    'StaticPrices',
    'Usage',
]

_PER = Decimal(1_000_000)


def _count(field_name: str, value: object, *, allow_none: bool) -> None:
    if value is None and allow_none:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            expected=f'{field_name} 是 >= 0 的整数' + ('，或 None（不限）' if allow_none else ''),
            received=f'{field_name}={value!r}，类型是 {type(value).__name__}',
            next_action=f'传一个整数 {field_name}；YAML 里的 yes / no 会被读成布尔值，不要用',
        )
    if value < 0:
        raise ConfigError(
            expected=f'{field_name} >= 0',
            received=f'{field_name}={value}',
            next_action=f'传一个非负的 {field_name}',
        )


def _amount(field_name: str, value: object, *, allow_none: bool) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, Decimal):
        raise ConfigError(
            expected=f'{field_name} 是 decimal.Decimal'
            + ('，或 None（不限）' if allow_none else ''),
            received=f'{field_name}={value!r}，类型是 {type(value).__name__}',
            next_action=(
                "金额用 Decimal 传，例如 Decimal('1.50')；浮点数会把 0.1 变成 0.1000000000000000055"
            ),
        )
    if not value.is_finite() or value < 0:
        raise ConfigError(
            expected=f'{field_name} 是有限的非负金额',
            received=f'{field_name}={value}',
            next_action=f'传一个 >= 0 的有限 {field_name}',
        )


def _seconds(field_name: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(
            expected=f'{field_name} 是 >= 0 的秒数，或 None（不限）',
            received=f'{field_name}={value!r}，类型是 {type(value).__name__}',
            next_action=f'传一个数字 {field_name}',
        )
    if not math.isfinite(value) or value < 0:
        raise ConfigError(
            expected=f'{field_name} 是有限的非负秒数',
            received=f'{field_name}={value}',
            next_action=f'传一个 >= 0 的有限 {field_name}；不限就传 None',
        )


@dataclass(frozen=True)
class Budget:
    """四维预算；None 是那一维不限。`tokens` 数输入 + 输出（缓存命中是输入的一部分，照样占
    窗口）。"""

    tokens: int | None = None
    cost: Decimal | None = None
    wall_clock_s: float | None = None
    turns: int | None = None

    def __post_init__(self) -> None:
        _count('tokens', self.tokens, allow_none=True)
        _amount('cost', self.cost, allow_none=True)
        _seconds('wall_clock_s', self.wall_clock_s)
        _count('turns', self.turns, allow_none=True)

    @property
    def unlimited(self) -> bool:
        return (
            self.tokens is None
            and self.cost is None
            and self.wall_clock_s is None
            and self.turns is None
        )


@dataclass(frozen=True)
class Usage:
    """一段消耗。三档 token 分开记；`cost` 为 None 表示其中有价格未知的调用（未知会传染：加上一个未
    知还是未知，不当成 0）。`estimated` 表示其中有 backend 没报 usage、按字符估出来的 token。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost: Decimal | None = field(default_factory=lambda: Decimal(0))
    turns: int = 0
    estimated: bool = False

    def __post_init__(self) -> None:
        for name in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'turns'):
            _count(name, getattr(self, name), allow_none=False)
        _amount('cost', self.cost, allow_none=True)
        if self.cached_input_tokens > self.input_tokens:
            raise ConfigError(
                expected='cached_input_tokens 是 input_tokens 里命中缓存的那一部分，不超过它',
                received=(
                    f'cached_input_tokens={self.cached_input_tokens}，'
                    f'input_tokens={self.input_tokens}'
                ),
                next_action=(
                    '按 backend 报的 prompt 总数填 input_tokens，'
                    '命中缓存的那部分填 cached_input_tokens'
                ),
            )

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        cost = None if self.cost is None or other.cost is None else self.cost + other.cost
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cost=cost,
            turns=self.turns + other.turns,
            estimated=self.estimated or other.estimated,
        )


@dataclass(frozen=True)
class Prices:
    """一个模型的三档单价，每百万 token。"""

    input: Decimal
    output: Decimal
    cached_input: Decimal

    def __post_init__(self) -> None:
        for name in ('input', 'output', 'cached_input'):
            _amount(name, getattr(self, name), allow_none=False)

    def cost_of(self, input_tokens: int, output_tokens: int, cached_input_tokens: int) -> Decimal:
        fresh = input_tokens - cached_input_tokens
        return (
            fresh * self.input
            + cached_input_tokens * self.cached_input
            + output_tokens * self.output
        ) / _PER


@runtime_checkable
class PriceTable(Protocol):
    def price_of(self, model: str, platform: str | None = None) -> Prices | None:
        """`model` 的单价；`platform` 是作答的平台（模型轮换时同名模型可能在两个平台上价格不同），
        不知道时为 None。不认识交回 None（价格未知，不是 0 价）。"""
        ...


class StaticPrices:
    """按模型名查的价格表，可以接在另一张后面当覆盖：先查自己，查不到再问 `fallback`。键可以是
    `平台:模型`（只对那个平台生效）或单独的模型名；两者都有时平台那一条优先。作答的平台不知道、或
    这张表里没列这个平台时，按这个模型在已列平台里最贵的那一份算：记成「价格未知」会让设了成本上限
    的那一层（顶层 Agent 共用的池）从此按耗尽算，一次来自没列的平台的回复就停掉每一个兄弟。"""

    def __init__(self, prices: Mapping[str, Prices], *, fallback: PriceTable | None = None) -> None:
        for name, value in prices.items():
            if not isinstance(cast(object, name), str) or not name:
                raise ConfigError(
                    expected='价格表的键是模型名（非空字符串）',
                    received=f'键 {name!r}',
                    next_action='用模型配置里的那个 model 名作键',
                )
            if not isinstance(cast(object, value), Prices):
                raise ConfigError(
                    expected=f'模型 {name} 的价格是 Prices',
                    received=f'{value!r}，类型是 {type(value).__name__}',
                    next_action=(
                        "写成 Prices(input=Decimal('…'), output=Decimal('…'), "
                        "cached_input=Decimal('…'))"
                    ),
                )
        self._prices = dict(prices)
        self._fallback = fallback

    def price_of(self, model: str, platform: str | None = None) -> Prices | None:
        found = None if platform is None else self._prices.get(f'{platform}:{model}')
        if found is None:
            found = self._prices.get(model)
        if found is None:
            suffix = f':{model}'
            listed = [p for key, p in self._prices.items() if key.endswith(suffix)]
            if listed:
                found = max(listed, key=lambda p: (p.output, p.input, p.cached_input))
        if found is not None:
            return found
        return None if self._fallback is None else self._fallback.price_of(model, platform)


class Exhaustion(Enum):
    """预算耗尽时怎么办。缺省 `GRACEFUL_WRAPUP`：跑了十小时的任务被硬杀、产物全丢，是所有结局里
    最坏的。"""

    GRACEFUL_WRAPUP = 'graceful_wrapup'
    HARD_STOP = 'hard_stop'
    ASK_HUMAN = 'ask_human'


@dataclass(frozen=True)
class BudgetView:
    """一个 Agent 预算节点此刻的只读视图：上限、用掉的（含后代）、剩下的（不限的那一维是 None）、
    哪几维耗尽了。"""

    limit: Budget
    used: Usage
    remaining: Budget
    exhausted: tuple[str, ...] = ()


AskHuman = Callable[[str, BudgetView], Awaitable[Budget | None]]
"""`ask_human` 的处理器：收（agent id、此刻的预算视图），交回要追加的预算，或 None（不追加）。"""


@dataclass(frozen=True)
class BudgetPolicy:
    """一个 Runtime 的预算设置。`root` 是整棵派活树共用的那一份（顶层 Agent 直接从它取，子 Agent 从
    父那里切）；`prices` 把 token 折成钱（不给就只记 token，成本未知）；`ask_human` 只在
    `on_exhausted` 是 `ASK_HUMAN` 时用，`ask_timeout_s` 内没答复、抛出或没给处理器都按
    `GRACEFUL_WRAPUP`——没人值守时等人只会一直占着。"""

    root: Budget = field(default_factory=Budget)
    on_exhausted: Exhaustion = Exhaustion.GRACEFUL_WRAPUP
    prices: PriceTable | None = None
    ask_human: AskHuman | None = None
    ask_timeout_s: float = 300.0

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.root), Budget):
            raise ConfigError(
                expected='BudgetPolicy.root 是 Budget',
                received=f'root={self.root!r}，类型是 {type(self.root).__name__}',
                next_action='写成 BudgetPolicy(root=Budget(turns=..., tokens=...))',
            )
        if not isinstance(cast(object, self.on_exhausted), Exhaustion):
            raise ConfigError(
                expected=f'on_exhausted 是 Exhaustion 之一：{[m.value for m in Exhaustion]}',
                received=f'on_exhausted={self.on_exhausted!r}',
                next_action=(
                    "传 Exhaustion.GRACEFUL_WRAPUP 这样的成员；字符串用 Exhaustion('hard_stop') 转"
                ),
            )
        if self.prices is not None and not isinstance(cast(object, self.prices), PriceTable):
            raise ConfigError(
                expected='prices 有 price_of(model, platform=None) 方法，或 None',
                received=f'prices={self.prices!r}，类型是 {type(self.prices).__name__}',
                next_action='传 StaticPrices({...})，或实现 PriceTable 协议',
            )
        _validate_ask_human(self.ask_human)
        timeout = cast(object, self.ask_timeout_s)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ConfigError(
                expected='ask_timeout_s 是有限的正秒数',
                received=f'ask_timeout_s={timeout!r}',
                next_action='传一个 > 0 的秒数，例如 300',
            )


def _validate_ask_human(handler: object) -> None:
    if handler is None:
        return
    if isinstance(handler, type) or not callable(handler):
        raise ConfigError(
            expected='ask_human 是 async def ask(agent_id, view) -> Budget | None 的可调用对象',
            received=f'ask_human={handler!r}，类型是 {type(handler).__name__}',
            next_action='传一个 async 函数（或带 async __call__ 的实例），不要传类本身',
        )
    try:
        inspect.signature(handler).bind('agent', object())
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            expected='ask_human 能接收 agent_id 与 view 两个位置参数',
            received=f'签名不匹配：{exc}',
            next_action='改成 async def ask(agent_id, view) -> Budget | None',
        ) from exc
    call = cast(object, inspect.getattr_static(type(handler), '__call__', None))
    if not (inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(call)):
        raise ConfigError(
            expected='ask_human 是 async def',
            received=f'ask_human={handler!r} 是同步 callable',
            next_action='改成 async def ask(agent_id, view) -> Budget | None',
        )
