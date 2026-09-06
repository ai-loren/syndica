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


def _rebuild(
    cls: type[SyndicaError], expected: str, received: str, next_action: str
) -> SyndicaError:
    """按关键字重建异常，供 `SyndicaError.__reduce__` 使用。"""
    return cls(expected=expected, received=received, next_action=next_action)


def _validated(name: str, value: object) -> str:
    """校验单个部件是非空 str，不合法则抛出同样带三件套的 ValueError。"""
    # value 标注为 object 而非 str：要挡的就是违反标注的运行时取值（如 dict.get() 给出的
    # None）。收窄成 str 会让下面的 isinstance 变成 pyright 眼里的死代码。
    if not isinstance(value, str):
        raise ValueError(
            f'SyndicaError 需要 str 类型的 {name}。'
            f' 收到: {name}={value!r}，类型是 {type(value).__name__}。'
            ' 传入字符串，或先转成 str，再重新构造。'
        )
    if not value.strip():
        raise ValueError(
            f'SyndicaError 需要非空的 {name}。'
            f' 收到: {name}={value!r}。'
            ' 三件套缺一不可，补齐后重新构造。'
        )
    return value


class SyndicaError(Exception):
    """携带「期望 / 实际 / 下一步」三件套的基类。"""

    def __init__(self, *, expected: str, received: str, next_action: str) -> None:
        self.expected = _validated('expected', expected)
        self.received = _validated('received', received)
        self.next_action = _validated('next_action', next_action)
        super().__init__(self.message)

    def __reduce__(self) -> tuple[object, ...]:
        # BaseException.__reduce__ 用位置参数重建，与本类 keyword-only 的签名不兼容：
        # 少了这个覆写，copy / pickle 一律 TypeError，而异常需要跨进程边界传递
        # （可替换的 Store 实现、子进程执行器）。
        # 第三项交回 __dict__，让 add_note() 等构造后附加的状态活过往返。
        return (
            _rebuild,
            (type(self), self.expected, self.received, self.next_action),
            self.__dict__,
        )

    @property
    def message(self) -> str:
        return f'期望: {self.expected}\n实际: {self.received}\n下一步: {self.next_action}'


class ContractViolation(SyndicaError):
    """Protocol 契约或文档化的调用方契约被违反。"""


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
