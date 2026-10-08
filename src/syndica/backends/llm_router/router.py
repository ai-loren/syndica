"""多平台轮换：按声明顺序逐个试，一个平台失败就换下一个，并让它单独指数退避。

轮换放在 LLM 这一层、不挂在 `LLM_FAILURE` 切点上（那是 `RetryModule` 的位置）：切点只在某个平台
已经失败之后才触发，没法在调用之前跳过一个正在退避的平台，也没法让多个 Agent 共享「这个平台
今天的额度用完了」——每个 Agent 都会先去撞一次。
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, cast

from syndica.kernel.call_limit import BudgetDeadline, model_call_limit
from syndica.kernel.context import ContextBlock
from syndica.kernel.errors import ConfigError, ContractViolation, LLMResponseError, SyndicaError
from syndica.kernel.llm import LLM, CachePrefixProvider, ContextOverflowError, LLMConfig

__all__ = ['BackoffPolicy', 'MemberHealth', 'ModelRouter']

logger = logging.getLogger(__name__)

# 换一个平台也修不好的错误：请求本身不合法、调用方违约、超窗（归压缩处理）。把它们记成平台故障，
# 会让一个坏请求把轮换里的每个平台都送进退避。
# BudgetDeadline 是墙钟到了，换平台也还是到了：记成平台故障会把轮换里每个平台都送进退避。
_NOT_A_PLATFORM_FAILURE = (ConfigError, ContractViolation, ContextOverflowError, BudgetDeadline)
_ERROR_PREVIEW = 300


def _config(expected: str, received: str, next_action: str) -> ConfigError:
    return ConfigError(expected=expected, received=received, next_action=next_action)


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise _config(
            f'BackoffPolicy.{field} 是有限数值',
            f'收到 {field}={value!r}，类型是 {type(value).__name__}',
            f'把 {field} 改成有限的 int 或 float；不要传布尔值或 inf',
        )
    return float(value)


@dataclass(frozen=True)
class BackoffPolicy:
    """每个平台各自的退避。

    第 n 次连续失败之后等 `min(maximum, base * factor ** (n - 1))` 秒，再乘一个落在
    `[1 - jitter, 1 + jitter)` 里的随机系数，免得几个进程在同一刻回来撞同一个限额窗口。平台给出的
    重试时间更晚时以它为准，可以超过 `maximum`（日额度要等到重置）。`max_wait` 是一次调用里等
    平台恢复的总秒数上限；0 表示只轮一遍、不等。
    """

    base: float = 2.0
    factor: float = 2.0
    maximum: float = 300.0
    jitter: float = 0.2
    max_wait: float = 120.0

    def __post_init__(self) -> None:
        base = _number(self.base, 'base')
        factor = _number(self.factor, 'factor')
        maximum = _number(self.maximum, 'maximum')
        jitter = _number(self.jitter, 'jitter')
        max_wait = _number(self.max_wait, 'max_wait')
        if base <= 0:
            raise _config(
                'BackoffPolicy.base 大于 0', f'收到 base={base}', '给第一次退避一个正的秒数'
            )
        if factor < 1:
            raise _config(
                'BackoffPolicy.factor 不小于 1',
                f'收到 factor={factor}',
                '用 1 表示固定间隔，大于 1 表示每次失败后拉长',
            )
        if maximum < base:
            raise _config(
                'BackoffPolicy.maximum 不小于 base',
                f'收到 base={base} maximum={maximum}',
                '把 maximum 调到不小于 base，它是单次退避的上限',
            )
        if not 0 <= jitter < 1:
            raise _config(
                'BackoffPolicy.jitter 落在 [0, 1)',
                f'收到 jitter={jitter}',
                '用 0 关掉抖动；等于或超过 1 会让退避变成 0 或负数',
            )
        if max_wait < 0:
            raise _config(
                'BackoffPolicy.max_wait 不小于 0',
                f'收到 max_wait={max_wait}',
                '用 0 表示只轮一遍、不等平台恢复',
            )


@dataclass(frozen=True)
class MemberHealth:
    """某个平台此刻的健康快照。`retry_at` 按路由器的时钟计，早于它不会再试这个平台。"""

    name: str
    failures: int
    retry_at: float
    last_error: str | None


@dataclass
class _State:
    failures: int = 0
    retry_at: float = -math.inf
    last_error: str | None = None
    recoverable: bool = True


def _no_hint(error: Exception) -> float | None:
    del error
    return None


def _always_recoverable(error: Exception) -> bool:
    del error
    return True


def _routed(reply: ContextBlock, name: str, failures: list[dict[str, object]]) -> ContextBlock:
    """作答成员的回复，带上这次轮换的记录。交回一份新的而不是改成员的那一份：会缓存回复的成员
    下次交回的还是同一个对象，就地写过的记录会跟着它出现在一次没换过平台的调用上。内层路由器
    留下的记录换成这一层的。"""
    extra = {
        key: value for key, value in reply.extra.items() if key not in ('llm_route', 'llm_failures')
    }
    extra['llm_route'] = name
    if failures:
        extra['llm_failures'] = failures
    return replace(reply, extra=extra)


def _status(error: Exception) -> int | None:
    """平台错误带的 HTTP 状态码。按属性认而不按类型认：backend 之间不互相依赖。"""
    status = getattr(error, 'status_code', None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _summary(error: Exception) -> str:
    text = error.received if isinstance(error, SyndicaError) else f'{type(error).__name__}: {error}'
    return text if len(text) <= _ERROR_PREVIEW else f'{text[:_ERROR_PREVIEW]}…'


def _checked_members(members: object) -> tuple[tuple[str, LLM], ...]:
    if isinstance(members, str | bytes) or not isinstance(members, Sequence):
        raise _config(
            'members 是 (平台名, LLM 实例) 的序列',
            f'收到 {type(members).__name__} 类型的 {members!r}',
            '传入 list 或 tuple，例如 [("gemini", gemini_llm), ("openrouter", openrouter_llm)]',
        )
    items = list(cast(Sequence[object], members))
    if not items:
        raise _config('members 至少有一个平台', '收到空序列', '传入至少一个 (平台名, LLM 实例)')
    checked: list[tuple[str, LLM]] = []
    for index, item in enumerate(items):
        if not isinstance(item, tuple) or len(cast(tuple[object, ...], item)) != 2:
            raise _config(
                f'members[{index}] 是 (平台名, LLM 实例) 二元组',
                f'收到 {item!r}',
                '把每个平台写成 (名字, LLM 实例)',
            )
        name, llm = cast(tuple[object, object], item)
        if not isinstance(name, str) or not name.strip():
            raise _config(
                f'members[{index}] 的平台名是非空字符串',
                f'收到 {name!r}',
                '给每个平台一个非空的名字，它会出现在错误信息与 llm_route 里',
            )
        if isinstance(llm, type) or not isinstance(llm, LLM):
            raise _config(
                f'members[{index}]（{name}）是 LLM 实例',
                f'收到 {type(llm).__name__} 类型的 {llm!r}',
                '传入带 config 属性与 async create(messages, **kwargs) 方法的实例',
            )
        if name in (known for known, _ in checked):
            raise _config(
                '每个平台名只出现一次',
                f'平台名 {name!r} 重复',
                '删掉重复的平台，或给它们起不同的名字',
            )
        checked.append((name, llm))
    return tuple(checked)


async def _create_within_budget(
    llm: LLM, messages: list[ContextBlock], kwargs: dict[str, Any]
) -> ContextBlock:
    """有墙钟上限时到点取消，并交回 `BudgetDeadline` 而不是平台故障。

    上限是调用开始时还剩的秒数（收尾轮已经加上宽限），用事件循环的时钟计，不跟路由器自己的退避
    时钟比：两边的时钟可以不是同一只。没有上限就原样调用，provider 自己的超时照旧算平台故障。
    """
    limit = model_call_limit()
    if limit is None:
        return await llm.create(messages, **kwargs)
    if limit <= 0:
        raise BudgetDeadline()
    try:
        async with asyncio.timeout(limit):
            return await llm.create(messages, **kwargs)
    except TimeoutError:
        raise BudgetDeadline() from None


class ModelRouter:
    """按声明顺序轮换多个平台；一个平台失败就换下一个，并让它单独退避。

    健康状态挂在路由器上，由所有持有它的 Agent 共享：一个 Agent 撞上的限额，别的 Agent 不必
    再撞一次。每次调用都从排在最前、且不在退避期的平台开始，所以主力平台一恢复就重新接手。
    只有一个平台时，它就是「失败后按退避重试同一个平台」。

    `retry_after(error)` 给出平台要求的最短等待秒数；`recoverable(error)` 回答这次失败等一会儿
    有没有可能自己好。答「不会」的平台照样换走、照样退避，但这次调用不为它等：认证失败、请求
    不合法这类错误等满 `max_wait` 也不会好，只会让调用方干等。
    """

    def __init__(
        self,
        members: Sequence[tuple[str, LLM]],
        *,
        policy: BackoffPolicy | None = None,
        retry_after: Callable[[Exception], float | None] = _no_hint,
        recoverable: Callable[[Exception], bool] = _always_recoverable,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._members = _checked_members(members)
        self._policy = policy if policy is not None else BackoffPolicy()
        self._retry_after = retry_after
        self._recoverable = recoverable
        self._clock = clock
        self._sleep = sleep
        self._rand = rand
        self._state = {name: _State() for name, _ in self._members}
        self._answered: LLM = self._members[0][1]
        # 这个调用方上一次是谁答的。路由器由几个 Agent 共用，而 `config` 与前缀单位要回答的是
        # 「我这次是谁答的」：只看全局的最近一次，别的 Agent 的一次换平台就会算到这个 Agent 头上。
        # 每个 Agent 在自己的任务里调 `create`，contextvar 在任务之间互不串。
        self._answered_here: ContextVar[LLM | None] = ContextVar(
            f'syndica_model_router_{id(self)}', default=None
        )

    def _last_answerer(self) -> LLM:
        here = self._answered_here.get()
        return self._answered if here is None else here

    @property
    def config(self) -> LLMConfig:
        """这个调用方最近一次作答的那个平台的 config；它还没调过就取所有调用方里最近作答的那个，
        还没人答过就是排在最前的那个。

        包装器转发委托对象的 config（见 `LLM.config`）；轮换里的委托对象随调用而变。
        """
        return self._last_answerer().config

    def health(self) -> tuple[MemberHealth, ...]:
        return tuple(
            MemberHealth(
                name=name,
                failures=self._state[name].failures,
                retry_at=self._state[name].retry_at,
                last_error=self._state[name].last_error,
            )
            for name, _ in self._members
        )

    def cache_prefix_units(
        self,
        messages: list[ContextBlock],
        **kwargs: Any,
    ) -> tuple[bytes, ...] | None:
        """这个调用方最近一次作答的那个平台的前缀单位：前缀缓存在平台那一侧，换了平台就是另一份
        缓存。两个平台的 provider 与模型名都相同时单位也相同，这种换平台看不出来。那个平台给不出
        单位时交回 None，漂移检测按 config 取逻辑单位并标成不精确。"""
        answered = self._last_answerer()
        if isinstance(answered, CachePrefixProvider):
            return answered.cache_prefix_units(messages, **kwargs)
        return None

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        deadline = self._clock() + self._policy.max_wait
        # 这次调用里被换掉的平台，按换掉的先后。主力平台在退避期里被跳过时不在这里：它失败的那一次
        # 已经记在当时那条回复上。
        failures: list[dict[str, object]] = []
        while True:
            for name, llm in self._members:
                state = self._state[name]
                if state.retry_at > self._clock():
                    continue
                try:
                    reply = await _create_within_budget(llm, messages, kwargs)
                except _NOT_A_PLATFORM_FAILURE:
                    raise
                except Exception as exc:
                    self._fail(name, state, exc)
                    failures.append(
                        {'platform': name, 'error': type(exc).__name__, 'status': _status(exc)}
                    )
                    continue
                state.failures = 0
                state.retry_at = -math.inf
                state.last_error = None
                state.recoverable = True
                self._answered = llm
                self._answered_here.set(llm)
                return _routed(reply, name, failures)
            wake = min(
                (state.retry_at for state in self._state.values() if state.recoverable),
                default=math.inf,
            )
            now = self._clock()
            if wake > deadline:
                raise self._exhausted(now)
            await self._sleep(max(0.0, wake - now))

    def _fail(self, name: str, state: _State, error: Exception) -> None:
        """记下一次平台故障并让它退避。

        后面的平台答出来时，这条 WARNING 是这次故障留下的唯一痕迹：一个一直在失败的主力平台
        （key 失效、额度耗尽）会被兜底平台静默接住，直到兜底的额度也被吃光才有人发现。
        """
        policy = self._policy
        state.failures += 1
        try:
            grown = policy.base * policy.factor ** (state.failures - 1)
        except OverflowError:
            grown = math.inf
        delay = min(policy.maximum, grown) * (1 + policy.jitter * (2 * self._rand() - 1))
        hint = self._retry_after(error)
        if hint is not None:
            delay = max(delay, hint)
        state.retry_at = self._clock() + delay
        state.last_error = _summary(error)
        state.recoverable = self._recoverable(error)
        logger.warning(
            '轮换中的平台 %s 失败（连续第 %d 次），接下来 %.1f 秒跳过它：%s',
            name,
            state.failures,
            delay,
            state.last_error,
        )

    def _exhausted(self, now: float) -> LLMResponseError:
        parts: list[str] = []
        for name, _ in self._members:
            state = self._state[name]
            wait = max(0.0, state.retry_at - now)
            parts.append(f'{name}：{state.last_error or "这次没轮到它"}（{wait:.0f} 秒后才会再试）')
        within = f'{self._policy.max_wait:g} 秒内答出这次请求'
        return LLMResponseError(
            expected=(
                f'平台 {self._members[0][0]} 在 {within}'
                if len(self._members) == 1
                else f'轮换中的 {len(self._members)} 个平台里，至少一个在 {within}'
            ),
            received='；'.join(parts),
            next_action=(
                '按每个平台的失败原因处理：认证失败（401 / 403，或错误里提到 API key）换 key，'
                '模型名或请求参数不对（400 / 404）改配置；限流与服务端故障等最早恢复的那个平台'
                '再试，或者给轮换再加一个平台。'
                '日额度耗尽的平台要等它的额度重置'
            ),
        )
