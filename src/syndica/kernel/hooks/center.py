"""切点分发。

两类切点的失败语义相反。**可拦截 hook 抛出的异常向上传播**：它是策略——权限检查、预算守卫，
越过一个失败的策略检查等于放行未经检查的操作。**可观察 hook 抛出的异常只记 ERROR 日志**：
可观测性不得拖垮主流程。

`HookCallback` 对所有切点是同一个类型，但**可观察切点上返回的 `HookResult` 被整个丢弃**：
在 turn_start 上返回 `modified={...}` 既不报错也不生效。要改参数只能挂在可拦截切点上。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.hooks.junctions import INTERCEPTABLE, Junction
from syndica.kernel.hooks.result import HookResult

__all__ = ['HookCallback', 'HooksCenter']

logger = logging.getLogger(__name__)

HookCallback = Callable[..., Awaitable[HookResult | None]]


def _orphan_result(junction: Junction, cb: HookCallback, outcome: HookResult) -> ContractViolation:
    """`result` 不带 `skip=True`：这个值哪个切点都读不到。

    合并结论只在 `skip` 那一支带出 `result`，因此不报出来就是静默丢弃——作者以为自己顶上了本次操作的
    产出，实际上原操作照常执行。要改产出用 `modified`，要顶替产出用 `skip=True` + `result`。
    """
    return ContractViolation(
        expected=f'{junction.value} 上的 HookResult.result 只在 skip=True 时给出',
        received=(
            f'{getattr(cb, "__qualname__", cb)!r} 返回了 skip=False 而 result='
            f'{type(outcome.result).__name__} 类型的 {outcome.result!r}'
        ),
        next_action=(
            '要顶替原操作就同时置 skip=True；只想改写参数或产出就把值放进 HookResult.modified；'
            'result 不带 skip 时没有任何切点会读到它'
        ),
    )


def _validated_junction(junction: object) -> Junction:
    """校验 junction 是枚举成员。

    参数标注为 object 而非 Junction：要挡的就是违反标注的运行时取值（配置或插件清单里读来的
    切点字符串）。收窄成 Junction 会让下面的 isinstance 变成 pyright 眼里的死代码。
    """
    if not isinstance(junction, Junction):
        raise ConfigError(
            expected='junction 是 Junction 枚举成员',
            received=f'收到 {type(junction).__name__} 类型的 {junction!r}',
            next_action='改用 Junction.<NAME>；切点名计入公开 API，不接受裸字符串',
        )
    return junction


class HooksCenter:
    def __init__(self) -> None:
        self._hooks: dict[Junction, list[HookCallback]] = {junction: [] for junction in Junction}

    def register(self, junction: Junction, cb: HookCallback) -> None:
        """挂一个 hook。同一切点上按注册先后执行。"""
        self._hooks[_validated_junction(junction)].append(cb)

    async def trigger(self, junction: Junction, **kwargs: Any) -> HookResult:
        """触发切点，返回可拦截链的合并结论。

        可拦截切点按注册顺序串行：前一个 hook 的 `modified` 并入后续 hook 的 kwargs，返回的
        `modified` 只含被改过的键而非整份 kwargs；`skip=True` 终止后续 hook 并带出它的
        `result`，不带 `skip` 的 `result` 报 `ContractViolation`（见 `_orphan_result`）。

        `modified` 是 last-wins 的结论，`history` 另外交出每个键在链上被赋过的**全部**值：只能单
        向收紧的量（trust）看结论会漏掉链中间那次收紧，而那正是不可信标记该起作用的时候。

        可观察切点并行执行，返回值与异常一律丢弃，因此恒返回空 `HookResult`。

        两类切点都在触发时对 hook 列表取快照：hook 里注册的 hook 留到下次触发才生效。两条
        分发路径必须在这一点上一致，否则同一段代码的行为取决于它挂在哪类切点上。
        """
        validated = _validated_junction(junction)
        hooks = list(self._hooks[validated])
        if not hooks:
            return HookResult()
        if validated in INTERCEPTABLE:
            return await self._trigger_serial(validated, hooks, **kwargs)
        await self._trigger_parallel(validated, hooks, **kwargs)
        return HookResult()

    async def _trigger_serial(
        self, junction: Junction, hooks: list[HookCallback], **kwargs: Any
    ) -> HookResult:
        merged: dict[str, Any] = {}
        history: dict[str, list[Any]] = {}
        current = dict(kwargs)
        for cb in hooks:
            outcome = await cb(**current)
            if outcome is None:
                continue
            if outcome.result is not None and not outcome.skip:
                raise _orphan_result(junction, cb, outcome)
            merged.update(outcome.modified)
            for key, value in outcome.modified.items():
                history.setdefault(key, []).append(value)
            current.update(outcome.modified)
            if outcome.skip:
                return HookResult(
                    skip=True, modified=merged, result=outcome.result, history=history
                )
        return HookResult(modified=merged, history=history)

    async def _trigger_parallel(
        self, junction: Junction, hooks: list[HookCallback], **kwargs: Any
    ) -> None:
        outcomes = await asyncio.gather(*(cb(**kwargs) for cb in hooks), return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                logger.error('observable hook failed at %s', junction.value, exc_info=outcome)
