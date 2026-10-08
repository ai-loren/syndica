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
- **唤醒重切**：下个周期开始时 limit 朝 cap 长回去，至多拿父此刻能给的一半（与切分同一条规则），
  从父的可用里取；父也在收尾态时沿祖先链往上取，直到一个正在跑的祖先或池。这次唤醒于是记在被唤醒者
  这一支血统的账上；一点都取不到就是耗尽，`send` 据此拒收。
- **准入**：每次模型调用之前按估算预占（`pending`），调用回来后换成实报的消耗；放不进就
  `BudgetExhausted`。预占与检查之间没有挂起点，并发的兄弟不会一起越过同一个父；越界只剩估算误差。
- **wall clock** 不可加：子的截止时刻取父的与自己请求的里早的那个；池的时钟从第一次 spawn 开始走。

成本未知（有价格不认识的调用）会传染：设了成本上限的那一层于是按耗尽算（fail closed），不当 0 价
继续花。
"""

from __future__ import annotations

import asyncio
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
    'PriceUnknown',
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
# 输出余量按此前报过的最大输出校准：没写 max_tokens 时每次都按 2048 预占，真实输出常常只有几十，
# 并发的兄弟就会被一个虚高的在飞估算挤掉。
_OUTPUT_FLOOR = 256
# 准入只因为兄弟在飞的估算而放不下时，等它们回来再试，至多等这么久。
_CONTENTION_WAIT = 60.0


class BudgetRefused(ConfigError):
    """切不出子预算：请求的超过了父此刻能给的、请求了 0，或父那一维已经用完。"""


class BudgetExhausted(SyndicaError):
    """这次模型调用按估算放不进剩余预算：不发出去。`dimension` 是放不下的那一维。"""

    dimension: str = ''


class PriceUnknown(BudgetExhausted):
    """设了成本上限，而这次调用的模型没有价格：算不出它要花多少，不发出去（fail closed）。它说的是
    「这个模型不能用」，不是「钱花完了」——重试链换一个有价格的候选接着试。"""


class _Contended(Exception):
    """放不下只是因为兄弟在飞的估算：等它们回来再试。"""


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
        'output_seen',
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
        'woken',
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
        # 报过的最大输出 token：校准没写 max_tokens 时的输出余量。
        self.output_seen = 0
        # send 收下叫醒它的消息时已经把这次唤醒的额度长回来了：周期开始时不再长。
        self.woken = False

    def chain(self) -> Iterator[_Node]:
        node: _Node | None = self
        while node is not None:
            yield node
            node = node.parent

    def subtree(self) -> Iterator[_Node]:
        yield self
        for child in self.children.values():
            yield from child.subtree()


def _spent(node: _Node, dim: str, own: _Node | None = None) -> Amount | None:
    """这个节点在 `dim` 上已经占掉的：自己的消耗 + 在飞估算 + 已拆除子树 + 活着的子各自占着的。
    None = 成本未知。给了 `own` 时只算它自己的在飞估算：别的节点在飞的那些很快就回来。"""
    used, closed = _value(node.own, dim), _value(node.closed, dim)
    if used is None or closed is None:
        return None
    total = used + closed
    if own is None or node is own:
        total += node.pending.get(dim, _zero(dim))
    for child in node.children.values():
        held = _occupied(child, dim, own)
        if held is None:
            return None
        total += held
    return total


def _occupied(node: _Node, dim: str, own: _Node | None = None) -> Amount | None:
    limit = node.limit[dim]
    return limit if limit is not None else _spent(node, dim, own)


def _available(node: _Node, dim: str, own: _Node | None = None) -> Amount:
    limit = cast(Amount, node.limit[dim])
    spent = _spent(node, dim, own)
    if spent is None:
        return _zero(dim)
    return max(limit - spent, _zero(dim))


def _room(node: _Node, dim: str, own: _Node | None = None) -> Amount | None:
    """这个节点此刻自己还能花多少：最近一个有上限的那一层的可用；整条链都不限时 None。给了 `own`
    时不算别的节点在飞的估算（轮开始时的判定用：兄弟在飞的调用不是花掉了）。"""
    for level in node.chain():
        if level.limit[dim] is not None:
            return _available(level, dim, own)
    return None


def _capacity(parent: _Node, want: Amount | None, dim: str, *, commit: bool) -> Amount | None:
    """`parent` 还能再给一个子节点多少（至多 `want`；None = 能给多少给多少，整条链都不限时交回
    None）。`parent` 在收尾态时先从它自己的父那里长回来。"""
    if parent.limit[dim] is None:
        if parent.parent is None:
            return want
        return _capacity(parent.parent, want, dim, commit=commit)
    available = _available(parent, dim)
    cap = parent.cap[dim]
    short = want is None or available < want
    if short and not parent.running and parent.parent is not None and cap is not None:
        headroom = cap - cast(Amount, parent.limit[dim])
        if headroom > 0:
            ask = headroom if want is None else min(want - available, headroom)
            extra = cast(Amount, _capacity(parent.parent, ask, dim, commit=commit))
            if commit:
                parent.limit[dim] = cast(Amount, parent.limit[dim]) + extra
            available += extra
    return available if want is None else min(available, want)


def _offer(parent: _Node, dim: str) -> Amount | None:
    """父此刻能给一个新子节点多少：正在跑的父是它的可用；在收尾态（zombie、起来等消息）的父还能
    从它自己的父那里长回来，长得回来的也算。"""
    room = _room(parent, dim)
    cap, limit = parent.cap[dim], parent.limit[dim]
    if (
        room is not None
        and parent.parent is not None
        and not parent.running
        and cap is not None
        and limit is not None
        and cap > limit
    ):
        room += cast(Amount, _capacity(parent.parent, cap - limit, dim, commit=False))
    return room


def _grow_settled(parent: _Node, caps: Mapping[str, Amount | None]) -> None:
    """在收尾态的父下面切一个马上要跑的子：先把父长到装得下它。"""
    if parent.parent is None or parent.running:
        return
    for dim, want in caps.items():
        cap, limit = parent.cap[dim], parent.limit[dim]
        if want is None or cap is None or limit is None:
            continue
        short = want - _available(parent, dim)
        if short > 0:
            grow = min(short, cap - limit)
            if grow > 0:
                granted = cast(Amount, _capacity(parent.parent, grow, dim, commit=True))
                parent.limit[dim] = limit + granted


def _wake_grant(node: _Node, dim: str) -> Amount:
    """一个在收尾态的节点被叫醒时能长回多少。与切分同一套规则：至多父此刻能给的一半，父是 Agent
    时先给它自己留一轮——否则父把最后一轮让给被叫醒的子，拿回结果之后一轮都没有、交不出回答。
    `send` 判「叫醒了能不能干活」与周期开始时真的长回来用的是同一个数，回执才不说谎。"""
    cap, limit = node.cap[dim], node.limit[dim]
    if cap is None or limit is None or cap <= limit or node.parent is None:
        return _zero(dim)
    whole = _capacity(node.parent, None, dim, commit=False)
    if whole is None:
        return cap - limit
    if dim == 'turns' and node.parent.parent is not None:
        whole = max(whole - 1, 0)
    return min(cap - limit, _half(whole))


def _returnable(node: _Node, dim: str) -> Amount:
    """正在跑的子节点做完会退回来的那部分（它的预留减去它真占着的）。"""
    total = _zero(dim)
    for child in node.children.values():
        limit = child.limit[dim]
        if limit is None or not child.running:
            continue
        spent = _spent(child, dim)
        if spent is not None and limit > spent:
            total += limit - spent
    return total


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
        self._price_failures: set[str] = set()
        # 有预占放掉了：等在准入上的调用醒来再试。边沿信号：放的一方置位并换一个新的，等的一方只等它
        # 拿到的那一个。
        self._released: asyncio.Event | None = None

    def _notify(self) -> None:
        if self._released is not None:
            self._released.set()
            self._released = None

    def _release_signal(self) -> asyncio.Event:
        if self._released is None:
            self._released = asyncio.Event()
        return self._released

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
            room = _offer(parent, dim)
            if room is not None and dim == 'turns' and not pooled:
                # 父自己留一轮收尾：把最后一轮也切给子，父拿回结果之后就一轮都没有、交不出回答。
                room = max(room - 1, 0)
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

    def carve(
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
        if running:
            _grow_settled(parent, caps)
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

    def capped(self, parent_id: str | None, planned: Mapping[str, object], dim: str) -> bool:
        """按 `split` 的结论，新 Agent 在 `dim` 上是否受上限约束（自己的，或从池里取时池的）。"""
        return planned.get(dim) is not None or _room(self._parent(parent_id), dim) is not None

    def would_be_limited(self, parent_id: str | None, planned: Mapping[str, object]) -> bool:
        parent = self._parent(parent_id)
        if parent.deadline is not None or any(planned.get(dim) is not None for dim in _ADDITIVE):
            return True
        return any(_room(parent, dim) is not None for dim in _ADDITIVE)

    def account(self, agent_id: str) -> BudgetAccount | None:
        node = self._nodes.get(agent_id)
        return None if node is None else BudgetAccount(self, node)

    def pool(self) -> BudgetAccount:
        return BudgetAccount(self, self._root)

    def carry(self, agent_id: str, used: Usage) -> None:
        """把上一次会话的已用记到恢复出来的那个 Agent 名下：它自己的视图、池的余量与下一次存盘
        都看得到。上限仍是这次的 `BudgetPolicy`；墙钟的截止时刻不动。"""
        node = self._nodes.get(agent_id)
        if node is not None:
            node.own = node.own + used

    def parent_of(self, agent_id: str) -> str | None:
        node = self._nodes.get(agent_id)
        if node is None or node.parent is None:
            return None
        return node.parent.agent_id

    def _price(self, llm: object, platform: str | None) -> Any:
        """作答模型的价格；价格表抛出时按价格未知（设了成本上限的那一层于是按耗尽算），记一次
        ERROR。"""
        prices = self.policy.prices
        model = _model_of(llm)
        if prices is None or model is None:
            return None
        try:
            return prices.price_of(model, platform)
        except Exception as error:
            if model not in self._price_failures:
                self._price_failures.add(model)
                logger.error(
                    '期望: 价格表 %s 交回模型 %s 的价格（Prices 或 None）。'
                    ' 实际: 它抛出 %s: %s；按价格未知处理，设了成本上限的那一层因此按耗尽算。'
                    ' 下一步: 修好这个价格表，'
                    '或在 .syndica.yaml 的 prices: 里直接写这个模型的价格。',
                    type(prices).__name__,
                    model,
                    type(error).__name__,
                    error,
                )
            return None

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
    input_tokens: int = 0


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
    def metered(self) -> bool:
        """tokens 或 cost 有上限：轮开始时才值得为判定估算一次请求。"""
        return any(_room(self._node, dim) is not None for dim in _METERED)

    @property
    def cost_capped(self) -> bool:
        return _room(self._node, 'cost') is not None

    # ---- 周期 ----

    def begin_cycle(self) -> None:
        if self._companion:
            return
        node = self._node
        if not node.woken:
            self._grow_for_wake()
        node.woken = False
        node.running = True

    def _grow_for_wake(self) -> None:
        node = self._node
        if node.parent is None or node.running:
            return
        for dim in _ADDITIVE:
            # 一次至多拿走父此刻能给的一半：同时被叫醒的几个兄弟各自长回整个 cap 时，先到的两个
            # 就把父分光了，后到的一个周期都开不了。
            grow = _wake_grant(node, dim)
            if grow > 0:
                granted = cast(Amount, _capacity(node.parent, grow, dim, commit=True))
                node.limit[dim] = cast(Amount, node.limit[dim]) + granted

    def reserve_wake(self) -> None:
        """`send` 收下一条要叫醒它的消息时，这次唤醒长回来的额度就定下来（与周期开始时同一个数）。
        留到周期开始再长，发消息的父接着跑的那一轮会先把它花掉，回执「它会读到」就成了谎话。"""
        node = self._node
        if self._companion or node.running or node.woken:
            return
        self._grow_for_wake()
        node.woken = True

    def release_wake(self) -> None:
        """叫醒没成（投递失败）：预先长回来的还给父。"""
        node = self._node
        if not node.woken or node.running:
            return
        node.woken = False
        for dim in _ADDITIVE:
            spent = _spent(node, dim)
            if node.limit[dim] is not None and spent is not None:
                node.limit[dim] = spent
        self._tree._notify()  # pyright: ignore[reportPrivateUsage]

    def end_cycle(self) -> None:
        if self._companion:
            return
        node = self._node
        node.running = False
        # 自己缩完，再把沿途同样在收尾态的祖先也缩到实际占用：退回来的余量一直传到正在跑的那一层，
        # 不停在一个也许再也不会醒的 zombie 身上。
        for level in node.chain():
            if level is not node and (level.running or level.parent is None):
                break
            for dim in _ADDITIVE:
                if level.limit[dim] is None:
                    continue
                spent = _spent(level, dim)
                if spent is not None:
                    level.limit[dim] = spent
        self._tree._notify()  # pyright: ignore[reportPrivateUsage]

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
            if not node.running:
                room += _wake_grant(node, dim)
            else:
                # 正在跑的节点此刻的剩余可能都被它的子占着：子做完会退回来，不算用完。
                room += _returnable(node, dim)
            if room <= 0:
                out.append(dim)
        if self._out(WALL):
            out.append(WALL)
        return tuple(out)

    @property
    def companion(self) -> bool:
        return self._companion

    def plan_turn(
        self, tokens: int, cost: Decimal | None, last_turn_s: float, *, count_turn: bool = True
    ) -> tuple[str, tuple[str, ...]]:
        """轮开始时的判定：`stop`（一次调用都放不下了）、`wrap_up`（这一次放得下、下一次放不下——这一轮
        就是收尾轮）或 `normal`，连同触发它的那几维。`tokens` / `cost` 是下一次调用的估算。
        `count_turn=False` 用于这一轮已经计过之后的重判：轮次那一维已经判过，再判会把刚扣掉的这一轮
        当成少了一轮。"""
        node = self._node
        stop: list[str] = []
        wrap: list[str] = []
        if not self._companion and count_turn:
            turns = _room(node, 'turns', node)
            if turns is not None:
                if turns <= 0:
                    stop.append('turns')
                elif turns == 1:
                    wrap.append('turns')
        room = _room(node, 'tokens', node)
        if room is not None:
            if room < tokens:
                stop.append('tokens')
            elif room < 2 * tokens:
                wrap.append('tokens')
        room_cost = _room(node, 'cost', node)
        if room_cost is not None:
            if cost is None or room_cost < cost:
                stop.append('cost')
            elif room_cost < 2 * cost:
                wrap.append('cost')
        if node.deadline is not None:
            left = node.deadline - self._tree.now()
            if left <= 0:
                stop.append(WALL)
            elif left < 2 * last_turn_s:
                wrap.append(WALL)
        if stop:
            return 'stop', tuple(stop)
        if wrap:
            return 'wrap_up', tuple(wrap)
        return 'normal', ()

    def fits(self, tokens: int, cost: Decimal | None, seconds: float = 0.0) -> bool:
        """自己此刻落定的余量放得下这么多 tokens、成本与秒数吗（不限的维恒放得下；成本未知而设了成本
        上限的放不下）。"""
        node = self._node
        if node.deadline is not None and node.deadline - self._tree.now() < seconds:
            return False
        room = _room(node, 'tokens', node)
        if room is not None and room < tokens:
            return False
        room_cost = _room(node, 'cost', node)
        return room_cost is None or (cost is not None and room_cost >= cost)

    # ---- 记账 ----

    def take_turn(self) -> None:
        if not self._companion:
            self._node.own = self._node.own + Usage(turns=1)

    def charge(self, usage: Usage) -> None:
        self._node.own = self._node.own + usage

    def estimate(self, llm: object, request: Mapping[str, Any]) -> tuple[int, Decimal | None, int]:
        """一次调用的估算：（输入 + 输出 token、成本（价格未知为 None）、未校准的输入估算）。"""
        raw_input = _estimate(request.get('messages')) + _tool_tokens(request.get('tools'))
        input_tokens = self._calibrated(raw_input)
        output_tokens = _output_allowance(llm, request, self._node.output_seen)
        price = self._tree._price(llm, None)  # pyright: ignore[reportPrivateUsage]
        cost = None if price is None else price.cost_of(input_tokens, output_tokens, 0)
        return input_tokens + output_tokens, cost, raw_input

    def _calibrated(self, raw_input: int) -> int:
        return math.ceil(raw_input * self._node.calibration)

    def admit(self, llm: object, request: Mapping[str, Any]) -> _Ticket:
        """按估算预占；放不进剩余就 `BudgetExhausted`（只因为兄弟在飞的估算放不下时是 `_Contended`，
        交给 `admit_waiting` 等）。预占与检查之间没有挂起点。"""
        tokens, cost, raw_input = self.estimate(llm, request)
        node = self._node
        if self._out(WALL):
            raise _not_admitted(node.agent_id, WALL, None, 0)
        if _room(node, 'cost') is not None and cost is None:
            self._tree._report_unpriced(llm)  # pyright: ignore[reportPrivateUsage]
            raise _unpriced(node.agent_id, llm)
        wanted: tuple[tuple[str, Amount | None], ...] = (('tokens', tokens), ('cost', cost))
        for dim, want in wanted:
            room = _room(node, dim)
            if room is None or want is None or want <= room:
                continue
            if want <= cast(Amount, _room(node, dim, node)):
                raise _Contended
            raise _not_admitted(node.agent_id, dim, want, room)
        node.pending['tokens'] = node.pending['tokens'] + tokens
        node.pending['cost'] = node.pending['cost'] + (cost or Decimal(0))
        return _Ticket(
            tokens=tokens, cost=cost, raw_input=raw_input, input_tokens=self._calibrated(raw_input)
        )

    async def admit_waiting(self, llm: object, request: Mapping[str, Any]) -> _Ticket:
        """`admit`，但兄弟在飞的估算挡着时等它们回来再试（至多 `_CONTENTION_WAIT` 秒，不过截止
        时刻）。一次虚高的在飞估算不能把并发的兄弟挤成收尾或停下。"""
        deadline = self._tree.now() + _CONTENTION_WAIT
        if self._node.deadline is not None:
            deadline = min(deadline, self._node.deadline)
        while True:
            try:
                return self.admit(llm, request)
            except _Contended:
                pass
            left = deadline - self._tree.now()
            if left <= 0:
                raise _not_admitted(self._node.agent_id, 'tokens', None, 0) from None
            signal = self._tree._release_signal()  # pyright: ignore[reportPrivateUsage]
            try:
                async with asyncio.timeout(left):
                    await signal.wait()
            except TimeoutError:
                pass

    def release(self, ticket: _Ticket) -> None:
        node = self._node
        node.pending['tokens'] = node.pending['tokens'] - ticket.tokens
        node.pending['cost'] = node.pending['cost'] - (ticket.cost or Decimal(0))
        self._tree._notify()  # pyright: ignore[reportPrivateUsage]

    def abandon(self, ticket: _Ticket, llm: object) -> None:
        """调用发出去之后被取消（超时、被杀）：放掉预占，按估算的输入记一笔、标 `estimated`。服务商
        多半已经收下请求、按输入计费；什么都不记，超额就是「取消次数 × 一次调用」，没有上界。输出没
        收到，不记。"""
        self.release(ticket)
        price = self._tree._price(llm, None)  # pyright: ignore[reportPrivateUsage]
        cost = None if price is None else price.cost_of(ticket.input_tokens, 0, 0)
        self.charge(Usage(input_tokens=ticket.input_tokens, cost=cost, estimated=True))

    def settle(
        self, ticket: _Ticket | None, llm: object, request: Mapping[str, Any], reply: object
    ) -> None:
        """一次调用回来了：放掉预占，换成实报的三档 token（没报的那档按字符估、标 `estimated`），按
        作答的模型与平台折成钱（不认识的记未知）。"""
        if ticket is not None:
            self.release(ticket)
        reported, platform = _reported(reply)
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
        else:
            self._node.output_seen = max(self._node.output_seen, output_tokens)
        cached = min(reported.get('cached_input_tokens') or 0, input_tokens)
        price = self._tree._price(llm, platform)  # pyright: ignore[reportPrivateUsage]
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

    def _grant(self, extra: Budget) -> None:
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


def _reported(reply: object) -> tuple[dict[str, int | None], str | None]:
    """（报的三档计数、作答的平台）。读不出来的 extra 当作没报：trace 那一侧已经为它记过一次 ERROR，
    计量按字符估算并标 `estimated`。"""
    try:
        return _reported_usage(reply), _platform(reply)
    except Exception:
        return {}, None


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


def _output_allowance(llm: object, request: Mapping[str, Any], seen: int = 0) -> int:
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
    if seen:
        return min(max(2 * seen, _OUTPUT_FLOOR), OUTPUT_ALLOWANCE)
    return OUTPUT_ALLOWANCE


def _model_of(llm: object) -> str | None:
    model = getattr(getattr(llm, 'config', None), 'model', None)
    return model if isinstance(model, str) and model else None


def _who(parent_id: str | None) -> str:
    return 'Runtime 级预算' if parent_id is None else f'agent {parent_id} 的预算'


def _unpriced(agent_id: str | None, llm: object) -> PriceUnknown:
    who = 'Runtime 级预算' if agent_id is None else f'agent {agent_id}'
    model = _model_of(llm) or '<没有 config.model>'
    error = PriceUnknown(
        expected=f'{who} 设了成本上限，这次调用的模型有价格',
        received=f'模型 {model} 不在价格表里，算不出这次调用要花多少',
        next_action=f'在 .syndica.yaml 的 prices: 里给 {model} 写三档单价，或换一个有价格的模型',
    )
    error.dimension = 'cost'
    return error


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
    asked = '等不到在飞的调用回来' if wanted is None else f'估算要 {wanted}'
    error = BudgetExhausted(
        expected=f'这次模型调用放得进 {who} 剩下的 {dim}',
        received=f'{asked}，只剩 {room}',
        next_action='这次调用不发出去；Agent 收尾交付已完成的部分，需要更多就追加预算',
    )
    error.dimension = dim
    return error


def granted_dims(extra: Budget) -> list[str]:
    return [dim for dim in (*_ADDITIVE, WALL) if getattr(extra, dim) is not None]


def seconds(value: float | None) -> int | None:
    """wall clock 进 trace 的形状：整秒（trace 的属性没有浮点）。"""
    return None if value is None or not math.isfinite(value) else round(value)
