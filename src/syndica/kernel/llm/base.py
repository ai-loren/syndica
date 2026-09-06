"""LLM 抽象。

内核不认识具体 provider：provider 只是字符串标识，实现由 backend 提供。config 不读环境变量，
API 密钥、base URL 这类部署事实由宿主填进 `client_args`。

请求参数归实例：config 给默认值，调用方只传单次覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Any, Protocol, runtime_checkable

from syndica.kernel.context import ContextBlock
from syndica.kernel.errors import ConfigError, SyndicaError

__all__ = ['ContextOverflowError', 'LLM', 'LLMConfig']


class ContextOverflowError(SyndicaError):
    """请求超出上下文窗口。触发 CONTEXT_OVERFLOW 切点，由压缩能力处理。"""


def _validated_changes(changes: dict[str, Any]) -> dict[str, Any]:
    """校验 derive 的关键字都是 LLMConfig 的字段。"""
    known = sorted(f.name for f in fields(LLMConfig))
    unknown = sorted(set(changes) - set(known))
    if unknown:
        raise ConfigError(
            expected=f'derive 的关键字都是 LLMConfig 的字段: {known}',
            received=f'收到未声明的字段 {unknown}',
            next_action='改用上面列出的字段名；要加新参数先在 LLMConfig 上声明字段',
        )
    return changes


@dataclass(frozen=True)
class LLMConfig:
    """一次 LLM 调用的配置。provider 与 model 决定缓存键与计费归属。

    两个参数字典由本配置独占：构造与 `derive` 各拷一份，改一个碰不到另一个。**拷的是字典本身、
    不是里面的值**——嵌套容器仍与传入方共享，值当只读用。
    """

    provider: str
    model: str
    client_args: dict[str, Any] = field(default_factory=dict[str, Any])
    request_args: dict[str, Any] = field(default_factory=dict[str, Any])
    timeout: float = 600.0
    context_window: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'client_args', dict(self.client_args))
        object.__setattr__(self, 'request_args', dict(self.request_args))

    def derive(self, **changes: Any) -> LLMConfig:
        """派生必须走此方法：影响身份的参数直接改会让缓存键与计费归属静默错位。"""
        return replace(self, **_validated_changes(changes))


@runtime_checkable
class LLM(Protocol):
    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        """返回一个 assistant block。超窗抛 ContextOverflowError。"""
        ...
