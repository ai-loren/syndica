"""LLM 抽象。

内核不认识具体 provider：provider 只是字符串标识，实现由 backend 提供。config 不读环境变量，
API 密钥、base URL 这类部署事实由宿主填进 `client_args`。

请求参数归实例：config 给默认值，调用方只传单次覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Any, Protocol, Self, runtime_checkable

from syndica.kernel.context import ContextBlock
from syndica.kernel.errors import ConfigError, SyndicaError

__all__ = ['ContextOverflowError', 'LLM', 'LLMConfig']


class ContextOverflowError(SyndicaError):
    """请求超出上下文窗口。触发 CONTEXT_OVERFLOW 切点，由压缩能力处理。

    它**不属于** LLMResponseError：超窗是请求侧、压缩后可重试的状况，LLMResponseError 说的是
    响应不可用。嵌进去会被先写的 `except LLMResponseError` 吞掉，压缩永不触发，只表现为任务失败。
    """


def _validated_changes(cls: type[LLMConfig], changes: dict[str, Any]) -> dict[str, Any]:
    """校验 derive 的关键字都是 cls 的字段。字段从 cls 取，子类才能 derive 自己声明的字段。"""
    known = sorted(f.name for f in fields(cls))
    unknown = sorted(set(changes) - set(known))
    if unknown:
        raise ConfigError(
            expected=f'derive 的关键字都是 {cls.__name__} 的字段: {known}',
            received=f'收到未声明的字段 {unknown}',
            next_action=f'改用上面列出的字段名；要加新参数先在 {cls.__name__} 上声明字段',
        )
    return changes


@dataclass(frozen=True)
class LLMConfig:
    """一次 LLM 调用的配置。provider 与 model 决定缓存键与计费归属，**字段顺序计入公开 API**。

    构造与 `derive` 各把两个参数字典拷一份，改一个碰不到另一个。三条边界，调用方看代码看不出来：

    - 拷的是字典本身、**不是里面的值**——嵌套容器仍与传入方共享，值当只读用。这是有意的：
      `client_args` 装得下活的 client 或 auth callable，深拷要么当场失败要么产出坏副本。
    - `copy.copy` 绕过构造器，副本与母配置**共享**两个字典；`deepcopy` 与 `pickle` 不共享。
    - `frozen=True` 只挡字段重新绑定，字典本身仍可被就地改。

    `hash(cfg)` 因两个 dict 字段而抛 TypeError；要可哈希的身份用 `(cfg.provider, cfg.model)`。
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

    def derive(self, **changes: Any) -> Self:
        """派生必须走此方法：影响身份的参数直接改会让缓存键与计费归属静默错位。"""
        return replace(self, **_validated_changes(type(self), changes))


@runtime_checkable
class LLM(Protocol):
    @property
    def config(self) -> LLMConfig:
        """**生效后的**配置，不一定是调用方构造时传进去的那个：backend 可以补上它自己才知道的
        事实（如 `context_window`）。包装器（重试 / 故障转移 / 缓存）必须转发**委托对象的**
        config；自己存一份构造时的副本，就又造出两个必须彼此一致的东西。
        """
        ...

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        """返回一个 assistant block。超窗抛 ContextOverflowError。"""
        ...
