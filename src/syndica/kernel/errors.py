"""异常基类。

面向调用方的错误必须让对方在不读源码的前提下自行修复，因此三件套在构造时强制。
"""

from __future__ import annotations

__all__ = [
    'ConfigError',
    'ContractViolation',
    'LLMResponseError',
    'PayloadRefused',
    'SyndicaError',
    'ToolFatalError',
]


def _rebuild(
    cls: type[SyndicaError], expected: str, received: str, next_action: str
) -> SyndicaError:
    """按关键字重建异常，供 `SyndicaError.__reduce__` 使用。

    只传三件套：给 `__init__` 增加必填参数的子类必须自己覆写 `__reduce__`，否则 copy / pickle
    往返时重建缺参数。
    """
    return cls(expected=expected, received=received, next_action=next_action)


def _validated(cls: type[SyndicaError], name: str, value: object) -> str:
    """校验单个部件是非空 str，不合法则抛出同样带三件套的内建异常。

    类名由构造中的那个类给出，不写死基类名：这条消息的读者正在写子类，报出 `SyndicaError` 会指着
    一个没问题的地方。类型不符抛 `TypeError`、空值抛 `ValueError`——两者合成一个，调用方要区分
    「传错了类型」与「传了空串」就只剩匹配文本这条路。
    """
    # value 标注为 object 而非 str：要挡的就是违反标注的运行时取值（如 dict.get() 给出的
    # None）。收窄成 str 会让下面的 isinstance 变成 pyright 眼里的死代码。
    if not isinstance(value, str):
        raise TypeError(
            f'期望: {cls.__name__} 的 {name} 是 str。'
            f' 实际: 收到 {name}={value!r}，类型是 {type(value).__name__}。'
            ' 下一步: 传入字符串，或先转成 str，再重新构造。'
        )
    if not value.strip():
        raise ValueError(
            f'期望: {cls.__name__} 的 {name} 是非空 str。'
            f' 实际: 收到 {name}={value!r}。'
            ' 下一步: 三件套缺一不可，补齐后重新构造。'
        )
    return value


class SyndicaError(Exception):
    """携带「期望 / 实际 / 下一步」三件套的基类。"""

    def __init__(self, *, expected: str, received: str, next_action: str) -> None:
        cls = type(self)
        self.expected = _validated(cls, 'expected', expected)
        self.received = _validated(cls, 'received', received)
        self.next_action = _validated(cls, 'next_action', next_action)
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
    """Protocol 契约或文档化的调用方契约被违反。

    与 `ConfigError` 的分界：**值本身没问题、错在此刻不该这样调**（重复 id、未知 id、替换未结束
    的任务、shutdown 之后 spawn、往受保护字段上写、hook 用了这个切点读不到的槽位），或者一个
    第三方实现（store、工具、处理器）交回了违反 Protocol 承诺的东西，归本类。
    """


class ConfigError(SyndicaError):
    """调用方交来的值不合法：配置文件、声明，以及公开 API 的实参。

    与 `ContractViolation` 的分界：**值本身就不合法，换一个值重新调用就能好**（工具 schema 缺键、
    标注读不懂、工具表的键与 `tool.name` 不一致、capacity < 1、derive 了未声明的字段、实参的类型
    或取值不对）归本类。分界写在 `docs/user/reference/errors.md`，以后加子类不改这条分界。
    """


class LLMResponseError(SyndicaError):
    """LLM 响应无法作为有效结果使用。"""


class PayloadRefused(SyndicaError):
    """`TraceStore` 收不下这一段自由文本（太大、超出单项配额），而 store 本身没坏。

    与别的异常的分界：报它的写没有副作用，记录方只丢这一段、照常写其余的；别的异常意味着 store
    坏了，记录方从那一刻起不再写。
    """


class ToolFatalError(SyndicaError):
    """工具遇到重试也不可能成功的错误。

    继承本类的异常会**向上传播到 agent loop**，其余异常由 ToolExecutor 转成
    给模型看的 output（spec §10）。「什么算致命」由工具作者决定——ToolExecutor
    没有信息做这个判断。

    致命：目标资源不存在且无法创建、权限不足、依赖的外部服务已下线。
    非致命：参数不合法、临时超时、目标当前被占用——模型换个方式或换个参数可能成功。
    """
