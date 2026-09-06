"""脚本化假 LLM。

它是唯一能按需触发失败路径的手段：LLM 异常的三级分类、空响应守卫、中断保留部分回复这些分支，
无法要求真实模型在指定的那一轮出错。它同时让编排逻辑的测试快速、确定、零成本——贡献者与 CI 都
不需要 API 密钥（spec §13.1）。

每次 `create` 消费一轮，抛异常的那一轮也算消费；脚本用尽后再调用是配置错误，不是空响应。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from syndica.kernel.context import ContextBlock, ToolCall
from syndica.kernel.errors import ConfigError
from syndica.kernel.llm.base import LLMConfig

__all__ = ['ScriptedLLM', 'ScriptedTurn']


@dataclass
class ScriptedTurn:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list[ToolCall])
    raises: BaseException | None = None

    def __post_init__(self) -> None:
        if self.raises is not None and (self.content is not None or self.tool_calls):
            raise ConfigError(
                expected='一轮要么抛异常、要么给回复，raises 与 content / tool_calls 不同时给',
                received=(
                    f'raises={type(self.raises).__name__} 同时带了'
                    f' content={self.content!r}、{len(self.tool_calls)} 个 tool_calls'
                ),
                next_action='拆成两轮：一轮 ScriptedTurn(raises=...)，一轮带回复的 ScriptedTurn',
            )


class ScriptedLLM:
    """按序回放预设响应的 LLM 替身。

    脚本与返回块之间不共享可变状态：同一个 `ScriptedTurn` 列表可以跨 ScriptedLLM 实例复用，
    消费者改动返回的块改不到脚本。

    `calls` 记的是收到那一刻的**深拷贝**。turn 循环跨轮复用同一个 SP block，记录若存共享对象，
    「前缀逐轮字节一致」这条前缀缓存性质就退化成恒等断言——重建前缀与递交同一对象无从区分。
    代价是传进来的块必须可深拷（`extra` 里别放活的连接）。`call_kwargs` 与 `calls` 逐位对应，
    但只记 kwargs 映射本身，值与调用方共享。
    """

    def __init__(self, script: list[ScriptedTurn], config: LLMConfig | None = None) -> None:
        self._script = list(script)
        self._index = 0
        # 默认 context_window 留 None：「窗口未知」是真实状态，编个数字会让压缩判定测到假前提。
        self._config = (
            config if config is not None else LLMConfig(provider='scripted', model='scripted')
        )
        self.calls: list[list[ContextBlock]] = []
        self.call_kwargs: list[dict[str, Any]] = []

    @property
    def config(self) -> LLMConfig:
        return self._config

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        self.calls.append(copy.deepcopy(messages))
        self.call_kwargs.append(kwargs)
        if self._index >= len(self._script):
            raise ConfigError(
                expected=f'脚本至少有 {self._index + 1} 轮',
                received=f'脚本已用尽，只提供了 {len(self._script)} 轮',
                next_action='给 ScriptedLLM 补一轮 ScriptedTurn，或检查被测循环是否多跑了一轮',
            )
        turn = self._script[self._index]
        self._index += 1
        if turn.raises is not None:
            raise turn.raises
        return ContextBlock(
            role='assistant', content=turn.content, tool_calls=copy.deepcopy(turn.tool_calls)
        )
