"""Runtime Module 形式的 LLM 重试与故障转移。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from syndica.kernel.context import ContextBlock
from syndica.kernel.errors import ConfigError
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import LLM
from syndica.kernel.modules import FlagDeclaration, RuntimeModule

if TYPE_CHECKING:
    from syndica.kernel.agent import BaseAgent
    from syndica.kernel.runtime import Runtime

__all__ = ['RetryModule']


def _config(expected: str, received: str, next_action: str) -> ConfigError:
    return ConfigError(expected=expected, received=received, next_action=next_action)


def _fallback_mapping(value: object) -> Mapping[str, LLM]:
    if not isinstance(value, Mapping):
        raise _config(
            'fallbacks 是名称到 LLM 实例的 Mapping',
            f'收到 {value!r}，类型是 {type(value).__name__}',
            '传 dict，例如 {"backup": backup_llm}；没有 fallback 就传空 dict',
        )
    owned: dict[str, LLM] = {}
    for name, candidate in cast(Mapping[object, object], value).items():
        if not isinstance(name, str) or not name or name.strip() != name or ',' in name:
            raise _config(
                'fallback 名是非空、无首尾空白且不含逗号的字符串',
                f'收到 fallback 名 {name!r}，类型是 {type(name).__name__}',
                '修正 fallback 名；llm_fallbacks 用逗号分隔这些名字',
            )
        if isinstance(candidate, type) or not isinstance(candidate, LLM):
            raise _config(
                f'fallbacks[{name!r}] 是 LLM 实例',
                f'收到 {candidate!r}，类型是 {type(candidate).__name__}',
                '传入带 config 属性与 async create(messages, **kwargs) 方法的 LLM 实例',
            )
        owned[name] = candidate
    return MappingProxyType(owned)


def _sleep_callback(value: object) -> Callable[[float], Awaitable[None]]:
    if not callable(value):
        raise _config(
            'sleep 是接收 1 个秒数参数的 async callable',
            f'收到 {value!r}，类型是 {type(value).__name__}',
            '传 async def sleep(delay: float)，测试可注入假时钟',
        )
    try:
        inspect.signature(value).bind(0.0)
    except (TypeError, ValueError) as exc:
        raise _config(
            'sleep 能接收 1 个位置参数',
            f'sleep={value!r} 的签名不匹配：{exc}',
            '把 sleep 改成 async def sleep(delay: float)',
        ) from exc
    if not (
        inspect.iscoroutinefunction(value) or inspect.iscoroutinefunction(type(value).__call__)
    ):
        raise _config(
            'sleep 是 async def',
            f'收到同步 callable {value!r}',
            '把 sleep 改成 async def；RetryModule 会 await 每次 backoff',
        )
    return cast(Callable[[float], Awaitable[None]], value)


def _fallback_chain(raw: str, fallbacks: Mapping[str, LLM]) -> tuple[tuple[str, LLM], ...]:
    if raw == '':
        return ()
    names = tuple(part.strip() for part in raw.split(','))
    if any(not name for name in names):
        raise _config(
            'llm_fallbacks 是逗号分隔的非空 fallback 名，或空字符串',
            f'收到 llm_fallbacks={raw!r}，其中含空项',
            '删除首尾逗号、连续逗号或只含空白的项',
        )
    duplicates = [name for index, name in enumerate(names) if name in names[:index]]
    if duplicates:
        raise _config(
            'llm_fallbacks 中每个名字只出现一次',
            f'收到 llm_fallbacks={raw!r}，重复项为 {duplicates!r}',
            '删除重复项；每个 fallback 在一次失败处理中只尝试一次',
        )
    unknown = [name for name in names if name not in fallbacks]
    if unknown:
        raise _config(
            'llm_fallbacks 中每个名字都存在于 RetryModule.fallbacks',
            f'收到未知名字 {unknown!r}；可用名字为 {list(fallbacks)!r}',
            '修正 llm_fallbacks，或把对应 LLM 实例加入 RetryModule.fallbacks',
        )
    return tuple((name, fallbacks[name]) for name in names)


class _RetryPlugin:
    def __init__(
        self,
        original: LLM,
        fallbacks: tuple[tuple[str, LLM], ...],
        sleep: Callable[[float], Awaitable[None]],
        *,
        attempts: int,
        backoff_ms: int,
    ) -> None:
        self._original = original
        self._fallbacks = fallbacks
        self._sleep = sleep
        self._attempts = attempts
        self._backoff_seconds = backoff_ms / 1000

    @staticmethod
    def _record_failure(error: Exception, candidate: str, exc: Exception) -> None:
        error.add_note(f'RetryModule {candidate} failed with {type(exc).__name__}: {exc}')

    @property
    def name(self) -> str:
        return 'retry'

    def register(self, hooks: HooksCenter) -> None:
        hooks.register(Junction.LLM_FAILURE, self._retry)

    async def _retry(self, **kwargs: Any) -> HookResult:
        if 'response' in kwargs:
            return HookResult()
        request = cast(dict[str, Any], kwargs['request'])
        error = cast(Exception, kwargs['error'])
        messages = cast(list[ContextBlock], request['messages'])
        call_kwargs = {key: value for key, value in request.items() if key != 'messages'}
        for attempt in range(1, self._attempts + 1):
            await self._sleep(self._backoff_seconds)
            try:
                response = await self._original.create(messages, **call_kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_failure(error, f'original retry {attempt}', exc)
                continue
            return HookResult(modified={'response': response})
        for name, candidate in self._fallbacks:
            await self._sleep(self._backoff_seconds)
            try:
                response = await candidate.create(messages, **call_kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_failure(error, f'fallback {name}', exc)
                continue
            return HookResult(modified={'response': response})
        return HookResult()


class RetryModule(RuntimeModule):
    """为每个 Agent 注入独立的 LLM_FAILURE 重试插件。"""

    def __init__(
        self,
        fallbacks: Mapping[str, LLM],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        owned_fallbacks = _fallback_mapping(cast(object, fallbacks))
        checked_sleep = _sleep_callback(cast(object, sleep))

        def plugin_factory(
            runtime: Runtime,
            agent: BaseAgent,
            flags: Mapping[str, bool | int | str],
        ) -> _RetryPlugin:
            del runtime
            return _RetryPlugin(
                agent.options.llm,
                _fallback_chain(cast(str, flags['llm_fallbacks']), owned_fallbacks),
                checked_sleep,
                attempts=cast(int, flags['llm_retry_attempts']),
                backoff_ms=cast(int, flags['llm_retry_backoff_ms']),
            )

        super().__init__(
            'retry',
            flags=(
                FlagDeclaration(
                    key='llm_retry_attempts',
                    default=0,
                    description='初次调用失败后，对原 LLM 额外重试的次数',
                    minimum=0,
                ),
                FlagDeclaration(
                    key='llm_retry_backoff_ms',
                    default=0,
                    description='每次重试或故障转移调用前等待的毫秒数',
                    minimum=0,
                ),
                FlagDeclaration(
                    key='llm_fallbacks',
                    default='',
                    description='按顺序各尝试一次的 fallback LLM 名称，使用逗号分隔',
                ),
            ),
            agent_plugins=(plugin_factory,),
        )
