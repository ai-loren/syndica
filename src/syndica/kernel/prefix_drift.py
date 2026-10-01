"""逐轮检测模型请求的缓存前缀漂移。"""

from __future__ import annotations

import hashlib
import inspect
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from syndica.kernel.context import ContextBlock
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import LLM, CachePrefixProvider
from syndica.kernel.llm.base import (
    _logical_cache_prefix_units,  # pyright: ignore[reportPrivateUsage]
)
from syndica.kernel.modules import FlagDeclaration, RuntimeModule

if TYPE_CHECKING:
    from syndica.kernel.agent import BaseAgent
    from syndica.kernel.runtime import Runtime

__all__ = ['PrefixDriftModule', 'PrefixDriftRecord', 'PrefixDriftService']

logger = logging.getLogger(__name__)


def _config(expected: str, received: str, next_action: str) -> ConfigError:
    return ConfigError(expected=expected, received=received, next_action=next_action)


def _counter(value: object) -> Callable[[bytes], int]:
    if value is None:
        return lambda payload: (len(payload) + 3) // 4
    if not callable(value):
        raise _config(
            'token_counter 是接收 bytes 的同步 callable，或 None',
            f'收到 {value!r}，类型是 {type(value).__name__}',
            '传 callable，例如 lambda payload: len(payload) // 4；不需要替换就传 None',
        )
    try:
        inspect.signature(value).bind(b'')
    except (TypeError, ValueError) as exc:
        raise _config(
            'token_counter 能接收 1 个 bytes 位置参数',
            f'token_counter={value!r} 的签名不匹配：{exc}',
            '把 token_counter 改成 def token_counter(payload: bytes) -> int',
        ) from exc
    if inspect.iscoroutinefunction(value) or inspect.iscoroutinefunction(type(value).__call__):
        raise _config(
            'token_counter 是同步 callable',
            f'收到异步 callable {value!r}',
            '改成同步函数；前缀观察 hook 不会 await token_counter',
        )
    return cast(Callable[[bytes], int], value)


def _labels(count: int) -> tuple[str, ...]:
    fixed = ('identity', 'request_args', 'system', 'tools')
    if count <= len(fixed):
        return fixed[:count]
    return (*fixed, *(f'history[{index}]' for index in range(count - len(fixed))))


def _checked_units(value: object, provider: object) -> tuple[bytes, ...]:
    if not isinstance(value, tuple):
        raise ContractViolation(
            expected=(
                f'{type(provider).__name__}.cache_prefix_units 返回 tuple[bytes, ...]，'
                '给不出精确单位时返回 None'
            ),
            received=f'收到 {type(value).__name__} 类型的 {value!r}',
            next_action='把每个有序请求单位编码为 bytes，并用 tuple 按缓存前缀顺序返回',
        )
    units = cast(tuple[object, ...], value)
    for index, unit in enumerate(units):
        if type(unit) is not bytes:
            raise ContractViolation(
                expected=f'{type(provider).__name__}.cache_prefix_units[{index}] 是 bytes',
                received=f'收到 {type(unit).__name__} 类型的 {unit!r}',
                next_action='用稳定序列化把该单位编码成 bytes；不要返回 str 或 bytearray',
            )
    return cast(tuple[bytes, ...], units)


@dataclass(frozen=True)
class PrefixDriftRecord:
    agent_id: str
    unit: str
    before_digest: str | None
    after_digest: str | None
    lost_bytes: int
    estimated_tokens: int
    exact: bool


class PrefixDriftService:
    """保存每个 Agent 的相邻请求基线与不可变漂移记录。"""

    def __init__(self, token_counter: Callable[[bytes], int]) -> None:
        self._token_counter = token_counter
        self._baselines: dict[str, tuple[bytes, ...]] = {}
        self._last_requests: dict[str, dict[str, Any]] = {}
        self._records: list[PrefixDriftRecord] = []

    @property
    def records(self) -> tuple[PrefixDriftRecord, ...]:
        return tuple(self._records)

    def _forget(self, agent_id: str) -> None:
        self._baselines.pop(agent_id, None)
        self._last_requests.pop(agent_id, None)

    def _units(
        self,
        llm: LLM,
        request: dict[str, Any],
    ) -> tuple[tuple[bytes, ...], bool]:
        messages = cast(list[ContextBlock], request['messages'])
        kwargs = {key: value for key, value in request.items() if key != 'messages'}
        if isinstance(llm, CachePrefixProvider):
            units = cast(object, llm.cache_prefix_units(messages, **kwargs))
            if units is not None:
                return _checked_units(units, llm), True
        return _logical_cache_prefix_units(llm.config, messages, kwargs), False

    def _observe(self, agent_id: str, llm: LLM, request: dict[str, Any]) -> None:
        if self._last_requests.get(agent_id) is request:
            return
        units, exact = self._units(llm, request)
        digests = tuple(hashlib.sha256(unit).digest() for unit in units)
        previous = self._baselines.get(agent_id)
        if previous is None:
            self._last_requests[agent_id] = request
            self._baselines[agent_id] = digests
            return
        if digests[: len(previous)] == previous:
            self._last_requests[agent_id] = request
            self._baselines[agent_id] = digests
            return

        changed_at = next(
            (
                index
                for index, (before, after) in enumerate(zip(previous, digests, strict=False))
                if before != after
            ),
            min(len(previous), len(digests)),
        )
        labels = _labels(max(len(previous), len(digests)))
        lost = b''.join(units[changed_at:])
        counted = cast(object, self._token_counter(lost))
        if type(counted) is not int or counted < 0:
            raise ContractViolation(
                expected='token_counter 返回大于或等于 0 的 int',
                received=f'收到 {counted!r}，类型是 {type(counted).__name__}',
                next_action='修正 PrefixDriftModule(token_counter=...) 的返回值',
            )
        record = PrefixDriftRecord(
            agent_id=agent_id,
            unit=labels[changed_at],
            before_digest=(previous[changed_at].hex() if changed_at < len(previous) else None),
            after_digest=(digests[changed_at].hex() if changed_at < len(digests) else None),
            lost_bytes=len(lost),
            estimated_tokens=counted,
            exact=exact,
        )
        self._last_requests[agent_id] = request
        self._baselines[agent_id] = digests
        self._records.append(record)
        logger.warning(
            'agent %s 的缓存前缀在 %s 漂移：before=%s after=%s，'
            '失去复用 %d UTF-8 bytes，估算 token=%d，exact=%s',
            record.agent_id,
            record.unit,
            record.before_digest,
            record.after_digest,
            record.lost_bytes,
            record.estimated_tokens,
            record.exact,
        )


class _PrefixDriftPlugin:
    def __init__(
        self,
        service: PrefixDriftService,
        agent_id: str,
        llm: LLM,
        *,
        enabled: bool,
    ) -> None:
        self._service = service
        self._agent_id = agent_id
        self._llm = llm
        self._enabled = enabled

    @property
    def name(self) -> str:
        return 'prefix-drift'

    def register(self, hooks: HooksCenter) -> None:
        hooks.register(Junction.POST_LLM_RESPONSE, self._observe)
        hooks.register(Junction.LLM_FAILURE, self._observe)
        hooks.register(Junction.CONTEXT_OVERFLOW, self._observe)

    async def _observe(self, **kwargs: Any) -> HookResult:
        if self._enabled:
            self._service._observe(  # pyright: ignore[reportPrivateUsage]
                self._agent_id,
                self._llm,
                cast(dict[str, Any], kwargs['request']),
            )
        return HookResult()


class PrefixDriftModule(RuntimeModule):
    """注入前缀漂移记录服务与 per-Agent 观察插件。"""

    def __init__(
        self,
        *,
        token_counter: Callable[[bytes], int] | None = None,
    ) -> None:
        checked_counter = _counter(cast(object, token_counter))

        def service_factory(
            runtime: Runtime,
            flags: Mapping[str, bool | int | str],
        ) -> PrefixDriftService:
            del runtime, flags
            return PrefixDriftService(checked_counter)

        def on_start(runtime: Runtime, flags: Mapping[str, bool | int | str]) -> None:
            del flags
            service = cast(PrefixDriftService, cast(Any, runtime).prefix_drift)

            async def forget(**kwargs: Any) -> None:
                service._forget(  # pyright: ignore[reportPrivateUsage]
                    cast(str, kwargs['agent_id'])
                )

            runtime.hooks.register(Junction.AGENT_DESPAWN, forget)

        def plugin_factory(
            runtime: Runtime,
            agent: BaseAgent,
            flags: Mapping[str, bool | int | str],
        ) -> _PrefixDriftPlugin:
            service = cast(PrefixDriftService, cast(Any, runtime).prefix_drift)
            return _PrefixDriftPlugin(
                service,
                agent.agent_id,
                agent.options.llm,
                enabled=cast(bool, flags['prefix_drift_detection']),
            )

        super().__init__(
            'prefix-drift',
            flags=(
                FlagDeclaration(
                    key='prefix_drift_detection',
                    default=True,
                    description='逐轮检测模型请求的缓存前缀是否被改写',
                ),
            ),
            services={'prefix_drift': service_factory},
            agent_plugins=(plugin_factory,),
            on_start=on_start,
        )
