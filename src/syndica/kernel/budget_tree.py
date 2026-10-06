"""预算树（spec §8.4）：子 Agent 从父 Agent 的预算里切分，且不得超额。

每个节点在可加维（tokens / cost / turns）上有一个 `cap`（切分时定下的上限）与一个 `limit`（此刻在父
那里占着的额度）。父的可用 = limit − 自己的消耗 − 在飞调用的估算 − 已拆除子树的消耗 − 活着的子节点
各自占着的额度，所以活着的子节点的额度之和永远不超过父（静态验收的不变量）。消耗只记在自己身上，
「含后代用掉多少」是派生出来的。

- **切分**：显式请求的那一维不得超过父此刻能给的（超了 `BudgetRefused` 三件套），且大于 0；没请求
  的那一维取父能给的一半（轮次向上取整，父剩一轮时子也拿一轮）。父那一维不限时子也不限（显式请求
  照收，当作它自己的上限）。Runtime 级预算是一个池：顶层 Agent 不切分，直接从池里取（CLI 的根拿到
  全部 `--max-turns`）；显式请求的顶层 Agent 照常切。
- **周期收尾结算**：一个周期结束时 limit 缩到实际占用，余量回父——做完的子 Agent 以 zombie 留着可以
  再叫醒，不能让它一直按住父的额度。
- **唤醒重切**：下个周期开始时 limit 长回 cap，从父的可用里取；父也在收尾态时沿祖先链往上取，直到
  一个正在跑的祖先或池。这次唤醒于是记在被唤醒者这一支血统的账上；一点都取不到就是耗尽，`send`
  据此拒收。
- **准入**：每次模型调用之前按估算预占（`pending`），调用回来后换成实报的消耗；放不进就
  `BudgetExhausted`。预占与检查之间没有挂起点，并发的兄弟不会一起越过同一个父；越界只剩估算误差。
- **wall clock** 不可加：子的截止时刻取父的与自己请求的里早的那个；池的时钟从第一次 spawn 开始走。

成本未知（有价格不认识的调用）会传染：设了成本上限的那一层于是按耗尽算（fail closed），不当 0 价
继续花。
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from syndica.kernel.budget import Budget, BudgetPolicy, BudgetView, Usage
from syndica.kernel.compaction import traj_tokens
from syndica.kernel.context import ContextBlock
from syndica.kernel.errors import ConfigError, SyndicaError

__all__ = [
    'RESOURCE_DIMENSIONS',
    'BudgetAccount',
    'BudgetExhausted',
    'BudgetRefused',
    'BudgetTree',
]

logger = logging.getLogger(__name__)

Amount = int | Decimal
_ADDITIVE = ('tokens', 'cost', 'turns')
_METERED = ('tokens', 'cost')
WALL = 'wall_clock_s'
# 伴生调用受约束的维：它们花的是被服务 Agent 的钱与时间，不占它的轮次（§7.5 伴生有自己的轮次上限）。
RESOURCE_DIMENSIONS = ('tokens', 'cost', WALL)
THRESHOLDS = (50, 25, 10)
# 请求里没写输出上限时，按这么多输出 token 估算一次调用。
OUTPUT_ALLOWANCE = 2048
_CALIBRATION = (0.5, 4.0)


class BudgetRefused(ConfigError):
    """切不出子预算：请求的超过了父此刻能给的、请求了 0，或父那一维已经用完。"""


class BudgetExhausted(SyndicaError):
    """这次模型调用按估算放不进剩余预算：不发出去。"""


def _zero(dim: str) -> Amount:
    return Decimal(0) if dim == 'cost' else 0


def _value(usage: Usage, dim: str) -> Amount | None:
    if dim == 'tokens':
        return usage.tokens
    if dim == 'turns':
        return usage.turns
    return usage.cost


class _Node:
    __slots__ = (
        'agent_id',
        'announced',
        'calibration',
        'cap',
        'children',
        'closed',
        'deadline',
        'generation',
        'limit',
        'own',
        'parent',
        'pending',
        'running',
        'started',
    )

    def __init__(
        self,
        agent_id: str | None,
        parent: _Node | None,
        cap: dict[str, Amount | None],
        *,
        running: bool,
        started: float,
        deadline: float | None,
    ) -> None:
        self.agent_id = agent_id
        self.parent = parent
        self.cap = cap
        self.limit: dict[str, Amount | None] = {
            dim: (value if running or value is None else _zero(dim)) for dim, value in cap.items()
        }
        self.running = running
        self.started = started
        self.deadline = deadline
        self.own = Usage()
        self.closed = Usage()
        self.pending: dict[str, Amount] = {dim: _zero(dim) for dim in _METERED}
        self.children: dict[str, _Node] = {}
        # 已经提醒过的阈值，按追加的次数分代：追加之后剩余回升，再跨过同一个阈值要再提醒一次。
        self.generation = 0
        self.announced: set[tuple[int, int]] = set()
        # 实报 / 估算的输入 token 之比：按字符估算对中文严重偏低，上一次实报校准下一次准入。
        self.calibration = 1.0

    def chain(self) -> Iterator[_Node]:
        node: _Node | None = self
        while node is not None:
            yield node
            node = node.parent

    def subtree(self) -> Iterator[_Node]:
        yield self
        for child in self.children.values():
            yield from child.subtree()


def _spent(node: _Node, dim: str) -> Amount | None:
    """这个节点在 `dim` 上已经占掉的：自己的消耗 + 在飞估算 + 已拆除子树 + 活着的子各自占着的。None
    = 成本未知。"""
    own, closed = _value(node.own, dim), _value(node.closed, dim)
    if own is None or closed is None:
        return None
    total = own + closed + node.pending.get(dim, _zero(dim))
    for child in node.children.values():
        held = _occupied(child, dim)
        if held is None:
            return None
        total += held
    return total


def _occupied(node: _Node, dim: str) -> Amount | None:
    limit = node.limit[dim]
    return limit if limit is not None else _spent(node, dim)


def _available(node: _Node, dim: str) -> Amount:
    limit = cast(Amount, node.limit[dim])
    spent = _spent(node, dim)
    if spent is None:
        return _zero(dim)
    return max(limit - spent, _zero(dim))


def _room(node: _Node, dim: str) -> Amount | None:
    """这个节点此刻自己还能花多少：最近一个有上限的那一层的可用；整条链都不限时 None。"""
    for level in node.chain():
        if level.limit[dim] is not None:
            return _available(level, dim)
    return None


def _capacity(parent: _Node, want: Amount, dim: str, *, commit: bool) -> Amount:
    """`parent` 还能再给一个子节点多少（至多 `want`）。`parent` 在收尾态时先从它自己的父那里
    长回来。"""
    if parent.limit[dim] is None:
        if parent.parent is None:
            return want
        return _capacity(parent.parent, want, dim, commit=commit)
    available = _available(parent, dim)
    cap = parent.cap[dim]
    if available < want and not parent.running and parent.parent is not None and cap is not None:
        headroom = cap - cast(Amount, parent.limit[dim])
        if headroom > 0:
            extra = _capacity(parent.parent, min(want - available, headroom), dim, commit=commit)
            if commit:
                parent.limit[dim] = cast(Amount, parent.limit[dim]) + extra
            available += extra
    return min(available, want)


def _half(value: Amount) -> Amount:
    if isinstance(value, Decimal):
        return value / 2
    return (value + 1) // 2


def _budget_dims(budget: Budget) -> dict[str, Amount | None]:
    return {dim: cast('Amount | None', getattr(budget, dim)) for dim in _ADDITIVE}


class BudgetTree:
    """一个 Runtime 的预算树：根是 `BudgetPolicy.root` 那个池（`Budget()` = 不限）；节点随 spawn
    建、随拆除结算并摘掉。"""

    def __init__(
        self, policy: BudgetPolicy | None = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.policy = policy or BudgetPolicy()
        self._clock = clock
        root = self.policy.root
        self._root = _Node(None, None, _budget_dims(root), running=True, started=0.0, deadline=None)
        self._root_started = False
        self._nodes: dict[str, _Node] = {}
        self._unpriced: set[str] = set()

    def now(self) -> float:
        return self._clock()

    def _parent(self, parent_id: str | None) -> _Node:
        if parent_id is not None:
            found = self._nodes.get(parent_id)
            if found is not None:
                return found
        return self._root

    def _start_root(self) -> None:
        if self._root_started:
            return
        self._root_started = True
        now = self.now()
        self._root.started = now
        if self.policy.root.wall_clock_s is not None:
            self._root.deadline = now + self.policy.root.wall_clock_s

    def split(
        self, parent_id: str | None, requested: Budget | None
    ) -> tuple[dict[str, Amount | None], float | None]:
        """按切分规则算出子的上限与截止时刻；切不出来就 `BudgetRefused`。不改树。"""
        self._start_root()
        parent = self._parent(parent_id)
        asked = requested or Budget()
        pooled = parent is self._root
        caps: dict[str, Amount | None] = {}
        for dim in _ADDITIVE:
            want = cast('Amount | None', getattr(asked, dim))
            if want is not None and want <= 0:
                raise _zero_request(dim)
            room = _room(parent, dim)
            if want is None and pooled:
                caps[dim] = None
            elif room is None:
                caps[dim] = want
            elif want is None:
                if room <= 0:
                    raise _nothing_left(parent_id, dim)
                caps[dim] = _half(room)
            elif want > room:
                raise _over_budget(parent_id, dim, want, room)
            else:
                caps[dim] = want
        now = self.now()
        deadline = parent.deadline
        if asked.wall_clock_s is not None:
            own = now + asked.wall_clock_s
            deadline = own if deadline is None else min(deadline, own)
        if deadline is not None and not pooled and deadline <= now:
            raise _nothing_left(parent_id, WALL)
        return caps, deadline

    def open(
        self,
        agent_id: str,
        parent_id: str | None,
        requested: Budget | None,
        *,
        running: bool = True,
    ) -> BudgetAccount:
        """建节点并挂到父下。`running=False`（起来等消息）的不先占额度，第一个周期开始时再切。"""
        caps, deadline = self.split(parent_id, requested)
        parent = self._parent(parent_id)
        node = _Node(agent_id, parent, caps, running=running, started=self.now(), deadline=deadline)
        parent.children[agent_id] = node
        self._nodes[agent_id] = node
        return BudgetAccount(self, node)

    def close(self, agent_id: str) -> None:
        """拆除：整棵子树的实际消耗记到父的 `closed` 上（花掉的不退），占着的额度随之回父。幂等。"""
        node = self._nodes.get(agent_id)
        if node is None:
            return
        parent = node.parent
        if parent is not None:
            parent.closed = parent.closed + actual(node)
            parent.children.pop(agent_id, None)
        for member in node.subtree():
            if member.agent_id is not None:
                self._nodes.pop(member.agent_id, None)

    def account(self, agent_id: str) -> BudgetAccount | None:
        node = self._nodes.get(agent_id)
        return None if node is None else BudgetAccount(self, node)

    def pool(self) -> BudgetAccount:
        return BudgetAccount(self, self._root)

    def parent_of(self, agent_id: str) -> str | None:
        node = self._nodes.get(agent_id)
        if node is None or node.parent is None:
            return None
        return node.parent.agent_id

    def _price(self, llm: object, platform: str | None) -> Any:
        prices = self.policy.prices
        model = _model_of(llm)
        if prices is None or model is None:
            return None
        return prices.price_of(model, platform)

    def _report_unpriced(self, llm: object) -> None:
        model = _model_of(llm) or '<没有 config.model>'
        if model in self._unpriced:
            return
        self._unpriced.add(model)
        logger.error(
            '期望: 设了成本上限时，每个作答的模型都有价格。'
            ' 实际: 模型 %s 不在价格表里，它的调用成本记为未知，设了成本上限的那一层因此按耗尽算。'
            ' 下一步: 在 .syndica.yaml 的 prices: 里给 %s 写三档单价（每百万 token），'
            '或去掉成本上限。',
            model,
            model,
        )


def actual(node: _Node) -> Usage:
    """一个节点含整棵子树的实际消耗（不含预留与在飞估算）。"""
    total = node.own + node.closed
    for child in node.children.values():
        total = total + actual(child)
    return total


@dataclass(frozen=True)
class _Ticket:
    tokens: int
    cost: Decimal | None
    raw_input: int


class BudgetAccount:
    """一个 Agent 在预算树上的那一格。`companion=True` 是给伴生用的那一份：花的记在同一个节点上，不
    占轮次、也不因轮次耗尽而停，不结算周期。"""

    def __init__(self, tree: BudgetTree, node: _Node, *, companion: bool = False) -> None:
        self._tree = tree
        self._node = node
        self._companion = companion

    @property
    def agent_id(self) -> str | None:
        return self._node.agent_id

    @property
    def tree(self) -> BudgetTree:
        return self._tree

    @property
    def generation(self) -> int:
        return self._node.generation

    def for_companion(self) -> BudgetAccount:
        return BudgetAccount(self._tree, self._node, companion=True)

    @property
    def limited(self) -> bool:
        """链上有没有哪一维有上限（没有就不装 GetBudget、不注入预算块）。"""
        if self._node.deadline is not None:
            return True
        return any(_room(self._node, dim) is not None for dim in _ADDITIVE)

    @property
    def cost_capped(self) -> bool:
        return _room(self._node, 'cost') is not None

    # ---- 周期 ----

    def begin_cycle(self) -> None:
        if self._companion:
            return
        node = self._node
        if node.parent is not None and not node.running:
            for dim in _ADDITIVE:
                cap, limit = node.cap[dim], node.limit[dim]
                if cap is None or limit is None or cap <= limit:
                    continue
                grown = _capacity(node.parent, cap - limit, dim, commit=True)
                node.limit[dim] = limit + grown
        node.running = True

    def end_cycle(self) -> None:
        if self._companion:
            return
        node = self._node
        node.running = False
        for dim in _ADDITIVE:
            if node.limit[dim] is None:
                continue
            spent = _spent(node, dim)
            if spent is not None:
                node.limit[dim] = spent

    # ---- 耗尽 ----

    def exhausted(self) -> tuple[str, ...]:
        dims = RESOURCE_DIMENSIONS if self._companion else (*_ADDITIVE, WALL)
        return tuple(dim for dim in dims if self._out(dim))

    def _out(self, dim: str) -> bool:
        if dim == WALL:
            deadline = self._node.deadline
            return deadline is not None and self._tree.now() >= deadline
        room = _room(self._node, dim)
        return room is not None and room <= 0

    def unwakeable(self) -> tuple[str, ...]:
        """叫醒它也干不了活的那几维：重切之后仍一点额度都没有。"""
        node = self._node
        out: list[str] = []
        for dim in _ADDITIVE:
            room = _room(node, dim)
            if room is None:
                continue
            cap, limit = node.cap[dim], node.limit[dim]
            if not node.running and cap is not None and limit is not None and cap > limit:
                assert node.parent is not None
                room += _capacity(node.parent, cap - limit, dim, commit=False)
            if room <= 0:
                out.append(dim)
        if self._out(WALL):
            out.append(WALL)
        return tuple(out)

    # ---- 记账 ----

    def take_turn(self) -> None:
        if not self._companion:
            self._node.own = self._node.own + Usage(turns=1)

    def charge(self, usage: Usage) -> None:
        self._node.own = self._node.own + usage

    def estimate(self, llm: object, request: Mapping[str, Any]) -> tuple[int, Decimal | None, int]:
        """一次调用的估算：（输入 + 输出 token、成本（价格未知为 None）、未校准的输入估算）。"""
        raw_input = _estimate(request.get('messages')) + _tool_tokens(request.get('tools'))
        input_tokens = math.ceil(raw_input * self._node.calibration)
        output_tokens = _output_allowance(llm, request)
        price = self._tree._price(llm, None)  # pyright: ignore[reportPrivateUsage]
        cost = None if price is None else price.cost_of(input_tokens, output_tokens, 0)
        return input_tokens + output_tokens, cost, raw_input

    def admit(self, llm: object, request: Mapping[str, Any]) -> _Ticket:
        """按估算预占；放不进剩余就 `BudgetExhausted`。预占与检查之间没有挂起点。"""
        tokens, cost, raw_input = self.estimate(llm, request)
        node = self._node
        room = _room(node, 'tokens')
        if room is not None and tokens > room:
            raise _not_admitted(node.agent_id, 'tokens', tokens, room)
        room_cost = _room(node, 'cost')
        if room_cost is not None and (cost is None or cost > room_cost):
            raise _not_admitted(node.agent_id, 'cost', cost, room_cost)
        if self._out(WALL):
            raise _not_admitted(node.agent_id, WALL, None, 0)
        node.pending['tokens'] = node.pending['tokens'] + tokens
        node.pending['cost'] = node.pending['cost'] + (cost or Decimal(0))
        return _Ticket(tokens=tokens, cost=cost, raw_input=raw_input)

    def release(self, ticket: _Ticket) -> None:
        node = self._node
        node.pending['tokens'] = node.pending['tokens'] - ticket.tokens
        node.pending['cost'] = node.pending['cost'] - (ticket.cost or Decimal(0))

    def settle(
        self, ticket: _Ticket | None, llm: object, request: Mapping[str, Any], reply: object
    ) -> None:
        """一次调用回来了：放掉预占，换成实报的三档 token（没报的那档按字符估、标 `estimated`），按
        作答的模型与平台折成钱（不认识的记未知）。"""
        if ticket is not None:
            self.release(ticket)
        reported = _reported_usage(reply)
        estimated = False
        input_tokens = reported.get('input_tokens')
        if input_tokens is None:
            input_tokens = _estimate(request.get('messages'))
            estimated = True
        elif ticket is not None and ticket.raw_input > 0:
            low, high = _CALIBRATION
            self._node.calibration = min(max(input_tokens / ticket.raw_input, low), high)
        output_tokens = reported.get('output_tokens')
        if output_tokens is None:
            output_tokens = _estimate([reply])
            estimated = True
        cached = min(reported.get('cached_input_tokens') or 0, input_tokens)
        price = self._tree._price(llm, _platform(reply))  # pyright: ignore[reportPrivateUsage]
        cost = None if price is None else price.cost_of(input_tokens, output_tokens, cached)
        if cost is None and self.cost_capped:
            self._tree._report_unpriced(llm)  # pyright: ignore[reportPrivateUsage]
        self.charge(
            Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached,
                cost=cost,
                estimated=estimated,
            )
        )

    # ---- 视图 ----

    def view(self) -> BudgetView:
        node = self._node
        limits: dict[str, Any] = {}
        remaining: dict[str, Any] = {}
        for dim in _ADDITIVE:
            binding = next((n for n in node.chain() if n.limit[dim] is not None), None)
            limits[dim] = None if binding is None else binding.limit[dim]
            remaining[dim] = _room(node, dim)
        if node.deadline is not None:
            limits[WALL] = max(node.deadline - node.started, 0.0)
            remaining[WALL] = max(node.deadline - self._tree.now(), 0.0)
        return BudgetView(
            limit=Budget(**limits),
            used=actual(node),
            remaining=Budget(**remaining),
            exhausted=self.exhausted(),
        )

    def fraction_left(self) -> float | None:
        """最紧的那一维还剩几成（0–1）；没有上限时 None。"""
        node = self._node
        fractions: list[float] = []
        for dim in _ADDITIVE:
            binding = next((n for n in node.chain() if n.limit[dim] is not None), None)
            if binding is None:
                continue
            limit = cast(Amount, binding.limit[dim])
            room = cast(Amount, _room(node, dim))
            fractions.append(0.0 if limit <= 0 else min(float(room) / float(limit), 1.0))
        if node.deadline is not None:
            span = node.deadline - node.started
            left = node.deadline - self._tree.now()
            fractions.append(0.0 if span <= 0 else max(left, 0.0) / span)
        return min(fractions) if fractions else None

    def crossed_threshold(self) -> int | None:
        """新跨过的最低那一个提醒阈值（百分比）；这一代里都提醒过了就 None。"""
        fraction = self.fraction_left()
        if fraction is None:
            return None
        node = self._node
        crossed = [t for t in THRESHOLDS if fraction * 100 <= t]
        fresh = [t for t in crossed if (node.generation, t) not in node.announced]
        if not fresh:
            return None
        node.announced.update((node.generation, t) for t in crossed)
        return min(fresh)

    # ---- 追加 ----

    def grant(self, extra: Budget) -> None:
        """宿主追加：钱从树外来，所以这个节点与链上每一个有上限的祖先（含池）在那一维上同加。"""
        for node in self._node.chain():
            for dim in _ADDITIVE:
                add = cast('Amount | None', getattr(extra, dim))
                if add is None or node.limit[dim] is None:
                    continue
                node.limit[dim] = cast(Amount, node.limit[dim]) + add
                cap = node.cap[dim]
                if cap is not None:
                    node.cap[dim] = cap + add
            if extra.wall_clock_s is not None and node.deadline is not None:
                node.deadline += extra.wall_clock_s
        self._node.generation += 1


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _extra(reply: object) -> Mapping[str, object]:
    extra = getattr(reply, 'extra', None)
    return cast(Mapping[str, object], extra) if isinstance(extra, Mapping) else {}


def _reported_usage(reply: object) -> dict[str, int | None]:
    usage = _extra(reply).get('usage')
    if not isinstance(usage, Mapping):
        return {}
    counts = cast(Mapping[str, object], usage)
    return {
        key: _count(counts.get(key))
        for key in ('input_tokens', 'output_tokens', 'cached_input_tokens')
    }


def _platform(reply: object) -> str | None:
    route = _extra(reply).get('llm_route')
    return route if isinstance(route, str) and route else None


def _estimate(messages: object) -> int:
    if not isinstance(messages, Sequence) or isinstance(messages, str | bytes):
        return 0
    blocks = [b for b in cast(Sequence[object], messages) if isinstance(b, ContextBlock)]
    return traj_tokens(blocks)


def _tool_tokens(tools: object) -> int:
    if not tools:
        return 0
    try:
        return len(json.dumps(tools, ensure_ascii=False, default=str)) // 4
    except (TypeError, ValueError):
        return 0


def _output_allowance(llm: object, request: Mapping[str, Any]) -> int:
    config = getattr(llm, 'config', None)
    defaults = getattr(config, 'request_args', None)
    sources: list[Mapping[str, object]] = [request]
    if isinstance(defaults, Mapping):
        sources.append(cast(Mapping[str, object], defaults))
    for source in sources:
        for key in ('max_tokens', 'max_completion_tokens', 'max_output_tokens'):
            value = _count(source.get(key))
            if value:
                return value
    return OUTPUT_ALLOWANCE


def _model_of(llm: object) -> str | None:
    model = getattr(getattr(llm, 'config', None), 'model', None)
    return model if isinstance(model, str) and model else None


def _who(parent_id: str | None) -> str:
    return 'Runtime 级预算' if parent_id is None else f'agent {parent_id} 的预算'


def _zero_request(dim: str) -> BudgetRefused:
    return BudgetRefused(
        expected=f'给子 Agent 的 {dim} 大于 0',
        received=f'请求了 {dim}=0',
        next_action=f'省略 {dim}（取父剩余的一半），或给一个正数',
    )


def _nothing_left(parent_id: str | None, dim: str) -> BudgetRefused:
    return BudgetRefused(
        expected=f'从{_who(parent_id)}里切出子 Agent 的 {dim}',
        received=f'{_who(parent_id)}的 {dim} 已经用完，切不出任何额度',
        next_action='先把已有的工作收尾交付；需要更多就请宿主追加预算',
    )


def _over_budget(parent_id: str | None, dim: str, want: Amount, available: Amount) -> BudgetRefused:
    return BudgetRefused(
        expected=f'子 Agent 的 {dim} 不超过{_who(parent_id)}此刻能给的 {available}',
        received=f'请求了 {dim}={want}',
        next_action=f'把 {dim} 降到 {available} 以内，或省略它（取剩余的一半），或缩小这件任务',
    )


def _not_admitted(
    agent_id: str | None, dim: str, wanted: Amount | None, room: Amount
) -> BudgetExhausted:
    who = 'Runtime 级预算' if agent_id is None else f'agent {agent_id}'
    asked = '价格未知' if wanted is None else f'估算要 {wanted}'
    return BudgetExhausted(
        expected=f'这次模型调用放得进 {who} 剩下的 {dim}',
        received=f'{asked}，只剩 {room}',
        next_action='这次调用不发出去；Agent 收尾交付已完成的部分，需要更多就追加预算',
    )


def seconds(value: float | None) -> int | None:
    """wall clock 进 trace 的形状：整秒（trace 的属性没有浮点）。"""
    return None if value is None or not math.isfinite(value) else round(value)
