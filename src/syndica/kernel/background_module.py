"""背景化 Module（spec §5.6）：一次跑得比 `background_after_seconds` 久的调用转到后台，这一轮先拿到
占位结果，真结果在之后某一轮进历史。缺省关（0）。

只有工具类声明了 `__tool_backgroundable__ = True` 的才会被转（内置的只有 `Bash`）；需要人工授权、获
批之后的调用从不转——它必须独占执行。派活与分叉不声明：它们的在轮等待一个 Agent 只有一个，转到后台的
那一个会被下一次派活顶掉。

开着它的 run 不写回放文档：结果在哪一轮进历史取决于真实耗时。`timer` 是转后台之前等的那段时间，测试
里换成一个由测试放行的等待。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, cast

from syndica import __version__
from syndica.kernel.background import BackgroundCalls
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.modules import FlagDeclaration, FlagValue, RuntimeModule
from syndica.kernel.tool import Tool
from syndica.kernel.tools.background import CancelBackgroundTool, QueryBackgroundTool

if TYPE_CHECKING:
    from syndica.kernel.agent import BaseAgent
    from syndica.kernel.runtime import Runtime

__all__ = ['BackgroundModule']


class _NoHooks:
    @property
    def name(self) -> str:
        return 'background'

    def register(self, hooks: HooksCenter) -> None:
        return None


def _calls_of(agent: BaseAgent) -> BackgroundCalls:
    calls = agent._background  # pyright: ignore[reportPrivateUsage]
    if calls is None:
        calls = BackgroundCalls(history=lambda: agent.context.current_traj)
        agent._background = calls  # pyright: ignore[reportPrivateUsage]
    return calls


class BackgroundModule(RuntimeModule):
    def __init__(self, *, timer: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        def plugin_factory(
            runtime: Runtime, agent: BaseAgent, flags: Mapping[str, FlagValue]
        ) -> _NoHooks:
            del runtime
            after = cast(int, flags['background_after_seconds'])
            if after > 0:
                agent._executor._configure_background(_calls_of(agent), float(after), timer)  # pyright: ignore[reportPrivateUsage]
            return _NoHooks()

        def tool_factory(
            runtime: Runtime, agent: BaseAgent, flags: Mapping[str, FlagValue]
        ) -> Mapping[str, Tool]:
            del runtime
            if not cast(int, flags['background_after_seconds']):
                return {}
            calls = _calls_of(agent)
            return {
                'QueryBackgroundTool': QueryBackgroundTool(calls),
                'CancelBackgroundTool': CancelBackgroundTool(calls),
            }

        super().__init__(
            'background',
            version=__version__,
            description='跑得太久的调用转到后台，结果在之后的轮次进历史（spec §5.6）',
            capabilities=(),
            replay_safe=False,
            flags=(
                FlagDeclaration(
                    key='background_after_seconds',
                    default=0,
                    description='声明了可后台化的调用跑过几秒就转到后台；0 = 关',
                    minimum=0,
                ),
            ),
            agent_plugins=(plugin_factory,),
            tool_factories=(tool_factory,),
        )
