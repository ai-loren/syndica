"""异常基类。

面向调用方的错误必须让对方在不读源码的前提下自行修复，因此三件套在构造时强制。
"""

from __future__ import annotations

__all__ = [
    'ConfigError',
    'ContractViolation',
    'LLMResponseError',
    'SyndicaError',
    'ToolFatalError',
]


class SyndicaError(Exception):
    """携带「期望 / 实际 / 下一步」三件套的基类。"""

    def __init__(self, *, expected: str, received: str, next_action: str) -> None:
        for name, value in (
            ('expected', expected),
            ('received', received),
            ('next_action', next_action),
        ):
            if not value.strip():
                raise ValueError(
                    f'SyndicaError 需要非空的 {name}。'
                    f' 收到: {name}={value!r}。'
                    f' 三件套缺一不可，补齐后重新构造。'
                )
        self.expected = expected
        self.received = received
        self.next_action = next_action
        super().__init__(self.message)

    @property
    def message(self) -> str:
        return f'期望: {self.expected}\n实际: {self.received}\n下一步: {self.next_action}'


class ContractViolation(SyndicaError):
    """Protocol 契约被违反。"""


class ConfigError(SyndicaError):
    """配置或声明不合法。"""


class LLMResponseError(SyndicaError):
    """LLM 响应无法作为有效结果使用。"""


class ToolFatalError(SyndicaError):
    """工具遇到重试也不可能成功的错误。

    继承本类的异常会**向上传播到 agent loop**，其余异常由 ToolExecutor 转成
    给模型看的 output（spec §10）。「什么算致命」由工具作者决定——ToolExecutor
    没有信息做这个判断。

    致命：目标资源不存在且无法创建、权限不足、依赖的外部服务已下线。
    非致命：参数不合法、临时超时、目标当前被占用——模型换个方式或换个参数可能成功。
    """
