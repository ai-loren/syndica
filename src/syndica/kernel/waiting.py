"""等待一个答复：`Waiter` 密封类型与 `WaitResult` 四态。

spec §4.2 把等待收进**一个**公开方法（`Runtime.wait_for`）加一个密封类型，而不是给每种等待各开
一个方法：新增等待类型于是不扩大公开面。密封的实际含义在 `correlation_of` 上——名单之外的
`Waiter` **当场报错**，而不是落进某个默认分支去等一个永远不会来的答复。

**三种等待共用一条实现：按关联 ID 匹配。** `Confirmation` 用 `tool_call_id`，`Reply` 用
`correlation_id`，`UserInput` 用它自己生成的 `request_id`。于是 `wait_for` 就是
`MessageBroker.take_by_correlation`——那个方法刻意不接受任意谓词，因为谓词下推不到远端队列
（spec §4.4）。

**新增一个 `Waiter` 子类要做两件事**：在 `_validate` 里校验自己的关联 ID，以及在
`correlation_of` 里登记它。没登记的类型等不到任何答复。
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from enum import Enum

from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.message import Message

__all__ = [
    'Confirmation',
    'Reply',
    'UserInput',
    'WaitResult',
    'WaitState',
    'Waiter',
    'correlation_of',
]


class WaitState(Enum):
    """一次等待的结局。

    四个都必须存在：`timeout` 是长跑的兜底，`aborted` 是协作式中止落在等待上的样子，`preempted`
    是「有人用一个新的等待顶掉了它」——三者的后续动作各不相同，塌成一个「没等到」就无从分辨。
    """

    ANSWERED = 'answered'
    TIMEOUT = 'timeout'
    PREEMPTED = 'preempted'
    ABORTED = 'aborted'


def _validate_timeout(kind: str, value: object) -> None:
    # value 标注为 object 而非 float：要挡的就是违反标注的运行时取值（配置字典给出的 None）。
    # 收窄成 float 会让下面的 isinstance 变成 pyright 眼里的死代码。
    if not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        raise ConfigError(
            expected=(
                f'{kind} 的 timeout 是有限正数秒数：每个 Waiter 自带自己的等待上限（spec §4.2）'
            ),
            received=f'收到 timeout={value!r}',
            next_action=(
                '传一个有限正数。刻意不接受 None、0 与 inf——长跑里等人工输入可能几小时无人'
                '查看，没有上限就是永久挂起，而挂住的 Agent 会一直占着预算与租约'
            ),
        )


def _validate_id(kind: str, field_name: str, value: str) -> None:
    # 只拿整串空白当空：ID 是外部系统给的不透明串，内部的空白可能有意义，因此不 strip 后存回。
    if not value.strip():
        raise ConfigError(
            expected=f'{kind} 的 {field_name} 非空白：等待按关联 ID 匹配答复',
            received=f'收到 {field_name}={value!r}',
            next_action=(
                '填上要等的那条请求的 ID。空 ID 谁都匹配不上，表现为静默等到超时'
                '——比当场报错难查得多'
            ),
        )


@dataclass(frozen=True)
class Waiter:
    """等待的基类。**密封**：名单就是下面三个子类，`correlation_of` 就是那份名单。

    frozen 是承重的：Waiter 交给 `wait_for` 之后还能被改掉关联 ID，那次等待就永远等不到答复。
    """

    timeout: float

    def __post_init__(self) -> None:
        _validate_timeout(type(self).__name__, self.timeout)
        self._validate()

    def _validate(self) -> None:
        """子类校验自己关联 ID 的地方。

        子类**在这里**校验、**不要覆写 `__post_init__`**：忘了调 `super().__post_init__()` 会让
        timeout 校验对该子类整条失效，而一次没有上限的等待只在长跑里现身。
        """


@dataclass(frozen=True)
class UserInput(Waiter):
    """等一个人来回话。`request_id` 就是这次提问的关联 ID，不给则自动生成。"""

    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def _validate(self) -> None:
        _validate_id('UserInput', 'request_id', self.request_id)


@dataclass(frozen=True)
class Confirmation(Waiter):
    """等一次工具调用的人工确认。关联 ID 就是那次调用的 id。"""

    tool_call_id: str

    def _validate(self) -> None:
        _validate_id('Confirmation', 'tool_call_id', self.tool_call_id)


@dataclass(frozen=True)
class Reply(Waiter):
    """等对某条特定请求的回复（典型场景：派活出去之后等结果）。"""

    correlation_id: str

    def _validate(self) -> None:
        _validate_id('Reply', 'correlation_id', self.correlation_id)


@dataclass(frozen=True)
class WaitResult:
    """一次等待的结果。字段顺序计入公开 API。

    **只有 `answered` 带 `message`，且 `answered` 必须带。** 其余三种没有答复可交，硬凑一条消息
    会让调用方误以为等到了；而没有消息的 `answered` 在调用方那里表现为一次拿不到内容的成功。
    两者都是产出方的缺陷，在构造处拦下比在使用处现身便宜得多。
    """

    state: WaitState
    message: Message | None = None

    def __post_init__(self) -> None:
        answered = self.state is WaitState.ANSWERED
        if answered != (self.message is not None):
            raise ContractViolation(
                expected='message 与 state 一致：answered 带上匹配到的那条消息，其余三种不带',
                received=(
                    f'收到 state={self.state.name}，message='
                    f'{"一条消息" if self.message is not None else "None"}'
                ),
                next_action=(
                    'answered 就把匹配到的消息交出来；超时 / 被顶掉 / 被中止时把 message 留空'
                    '——调用方按 state 分支，一条对不上的消息会被当成答复用下去'
                ),
            )


def correlation_of(waiter: Waiter) -> str:
    """取出这次等待要匹配的关联 ID。

    **这里就是「密封」落地的地方。** 名单之外的类型报错而不是回落到某个默认分支——回落的后果是
    一次永远等不到答复的等待，只在超时之后才现身，而那可能是几小时以后。

    名单上三个类互相之间没有继承关系，因此分支顺序不影响结果。它们各自的子类按继承来的那个 ID
    匹配：继承来的正是有意义的 ID，不制造回落。
    """
    if isinstance(waiter, Confirmation):
        return waiter.tool_call_id
    if isinstance(waiter, Reply):
        return waiter.correlation_id
    if isinstance(waiter, UserInput):
        return waiter.request_id
    raise ContractViolation(
        expected='waiter 是 UserInput / Confirmation / Reply 之一（Waiter 是密封类型）',
        received=f'收到 {type(waiter).__name__}，它不在这三种之内',
        next_action=(
            '用这三种之一。要加新的等待类型，就在 waiting.py 里加一个 Waiter 子类并在'
            ' correlation_of 里登记它的关联 ID——没登记的类型等不到任何答复'
        ),
    )
