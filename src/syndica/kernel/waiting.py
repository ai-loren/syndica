"""等待一个答复：`Waiter` 密封类型与 `WaitResult` 四态。

spec §4.2 把等待收进**一个**公开方法（`Runtime.wait_for`）加一个密封类型，而不是给每种等待各开
一个方法：新增等待类型于是不扩大公开面。密封的实际含义在 `correlation_of` 上——名单之外的
`Waiter` **当场报错**，而不是落进某个默认分支去等一个永远不会来的答复。

**三种等待共用一条实现：按关联 ID 匹配。** `Confirmation` 用本次确认请求的一次性
`tool_call_id`，`Reply` 用 `correlation_id`，`UserInput` 用它自己生成的 `request_id`。于是
`wait_for` 就是
`MessageBroker.take_by_correlation`——那个方法刻意不接受任意谓词，因为谓词下推不到远端队列
（spec §4.4）。

**三种等待共用一个扁平的关联 ID 命名空间**：`correlation_of` 交出的是一个裸字符串，kind 在这里
被抹掉。`tool_call_id` 由模型厂商生成、`correlation_id` 由 `Runtime.send` 生成，两者都不带前缀
且共用同一个字符串空间，因此一条对派活的回复原则上能解掉一次工具确认的等待。当前没有已知的碰撞
来源；要不要给每种等待的关联 ID 加前缀，留给接上 `wait_for` 的那一层裁。

**新增一个 `Waiter` 子类要做两件事**：在 `_validate` 里校验自己的关联 ID，以及在
`_CORRELATION_FIELDS` 里登记它的字段。没登记的类型等不到任何答复。
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

    `aborted` 也可能**一秒都没等**：发起等待时这个 Agent 已经在收尾（`abort` 或 `kill` 写下的
    `aborting`），`Runtime.wait_for` 当场交回它，`waiter.timeout` 根本没交给 broker。不得把它读成
    「等过一段时间」，更不能据此做超时重试。

    刻意**不**混入 `str`：混入后 `WaitState.TIMEOUT == 'timeout'` 为真，于是一个漏了 `.value`
    的比较、或一个拿字符串当状态用的调用方都不会报错，掩盖掉本该现身的错误。
    """

    ANSWERED = 'answered'
    TIMEOUT = 'timeout'
    PREEMPTED = 'preempted'
    ABORTED = 'aborted'


def _refuse_timeout(kind: str, value: object, why: str, next_action: str) -> ConfigError:
    return ConfigError(
        expected=f'{kind} 的 timeout 是有限正数秒数：每个 Waiter 自带自己的上限（spec §4.2）',
        received=f'收到 timeout={value!r}，类型是 {type(value).__name__}：{why}',
        next_action=next_action,
    )


def _validate_timeout(kind: str, value: object) -> None:
    # value 标注为 object 而非 float：要挡的就是违反标注的运行时取值（配置字典给出的 None）。
    # 收窄成 float 会让下面的 isinstance 变成 pyright 眼里的死代码。
    if isinstance(value, bool):
        raise _refuse_timeout(
            kind,
            value,
            'bool 不是秒数',
            'timeout 传一个有限正数秒数。bool 单独拒掉：YAML 1.1 把 yes / on / true 解析成'
            ' True，而 True 当秒数用恰好等于 1.0 秒——每次等人工输入都在一秒后超时且不报错',
        )
    if not isinstance(value, int | float):
        raise _refuse_timeout(
            kind,
            value,
            '不是数值',
            'timeout 传一个有限正数秒数，int 与 float 都收。少了这道检查，配置字典给出的 None'
            ' 会在 None <= 0 处抛出一个不带下一步的 TypeError',
        )
    try:
        seconds = float(value)
    except OverflowError:
        raise _refuse_timeout(
            kind,
            value,
            '大到无法表示成浮点秒数',
            'timeout 传一个转得成 float 的正数秒数。超出 float 范围的 int 在换算成截止时刻时'
            '抛出一个不带下一步的 OverflowError',
        ) from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise _refuse_timeout(
            kind,
            value,
            '不是有限正数',
            'timeout 传一个有限正数秒数。0 与负数没有可等的区间；inf 不是合法的秒数，算不出'
            '截止时刻；nan 与任何数比较都为假，塞进定时器堆是未定义行为',
        )


def _validated_id(kind: str, field_name: str, value: object) -> str:
    """校验关联 ID 并**原样**交回。

    入参标 object 而非 str：关联 ID 与 timeout 来自同一批外部来源（配置、工具调用载荷、跨进程
    反序列化），标注挡不住违反标注的运行时取值。
    """
    if not isinstance(value, str):
        raise ConfigError(
            expected=f'{kind} 的 {field_name} 是 str：等待按关联 ID 逐字节匹配答复',
            received=f'收到 {field_name}={value!r}，类型是 {type(value).__name__}',
            next_action=(
                f'{field_name} 传字符串。它与消息的 correlation_id 逐字节比较，'
                "b'call-7' 与 'call-7' 不相等，表现为答复躺在邮箱里、等待跑满超时、零日志"
            ),
        )
    if not value.strip():
        raise ConfigError(
            expected=f'{kind} 的 {field_name} 非空白：等待按关联 ID 匹配答复',
            received=f'收到 {field_name}={value!r}',
            next_action=(
                f'{field_name} 填上要等的那条请求的 ID。空 ID 谁都匹配不上，表现为静默等到超时'
                '——比当场报错难查得多'
            ),
        )
    # 只拿整串空白当空、且原样交回：ID 是外部系统给的不透明串，两端与内部的空白都可能有意义，
    # strip 或改大小写后交出去就与消息里的 correlation_id 不再逐字节相等。
    return value


@dataclass(frozen=True)
class Waiter:
    """等待的基类。**密封**：名单就是 `_CORRELATION_FIELDS`，`correlation_of` 按它裁。

    frozen 是承重的：Waiter 交给 `wait_for` 之后还能被改掉关联 ID，那次等待就永远等不到答复。
    """

    timeout: float

    def __post_init__(self) -> None:
        _validate_timeout(type(self).__name__, self.timeout)
        self._validate()

    def _validate(self) -> None:
        """子类校验自己关联 ID 的地方。

        子类**在这里**校验、**不要覆写 `__post_init__`**：忘了调 `super().__post_init__()` 会让
        timeout 校验对该子类整条失效，而一次没有上限的等待只在长跑里现身。覆写本方法时**必须调
        `super()._validate()`**，否则父类那层 ID 校验对该子类失效。
        """


@dataclass(frozen=True)
class UserInput(Waiter):
    """等一个人来回话。`request_id` 就是这次提问的关联 ID，不给则自动生成。

    生成的 ID **必须不可预测**，这是安全属性而不是风格：它是三种等待里唯一一个调用方事先不知道
    的关联 ID，猜得到就意味着任何能往该 Agent 邮箱 publish 的组件——包括被注入内容说服的模型
    自己——能伪造一条「人工输入的答复」把等待解掉，绕过人工确认。因此不用顺序计数器，也不用内嵌
    MAC 与时钟的 uuid1（它另会把机器标识随关联 ID 写进 trace 与会话记录）。
    """

    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def _validate(self) -> None:
        _validated_id(type(self).__name__, 'request_id', self.request_id)


@dataclass(frozen=True)
class Confirmation(Waiter):
    """等一次工具调用的人工确认。

    字段名沿用 `tool_call_id`，值必须是确认通道为**这一次授权**生成的不可预测 ID，不是模型给出的
    原始调用 ID。原始 ID 可能复用或被猜到，直接拿来关联会让迟到答复批准后续另一条调用。
    """

    tool_call_id: str

    def _validate(self) -> None:
        _validated_id(type(self).__name__, 'tool_call_id', self.tool_call_id)


@dataclass(frozen=True)
class Reply(Waiter):
    """等对某条特定请求的回复（典型场景：派活出去之后等结果）。"""

    correlation_id: str

    def _validate(self) -> None:
        _validated_id(type(self).__name__, 'correlation_id', self.correlation_id)


@dataclass(frozen=True)
class WaitResult:
    """一次等待的结果。字段顺序计入公开 API。

    **只有 `answered` 带 `message`，且 `answered` 必须带。** 其余三种没有答复可交，硬凑一条消息
    会让调用方误以为等到了；而没有消息的 `answered` 在调用方那里表现为一次拿不到内容的成功。
    两者都是产出方的缺陷，在构造处拦下比在使用处现身便宜得多。

    frozen 只管住 `state` 与 `message` 这两个引用：`Message` 与别处的一样可变，`result.message`
    的字段改得动。连带的后果是**带消息的结果不可哈希**，而四态里只有 `answered` 带消息——于是
    不可哈希恰好落在成功路径上，别拿 `WaitResult` 当 dict 键或塞进 set。
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
                    'state 是 answered 就把匹配到的消息交出来；超时 / 被顶掉 / 被中止时把'
                    ' message 留空——调用方按 state 分支，一条对不上的消息会被当成答复用下去'
                ),
            )


# 名单：`Waiter` 的密封就是这份表。字段名必须与各子类 `_validate` 里校验的那个逐字一致，否则
# `correlation_of` 交出的是一个从未被校验过的值。表的**顺序不影响任何结果**：下面数的是命中数。
# 顺序跟文件里的定义顺序，刻意不按字典序——歧义错误里的类型名靠 `sorted` 稳定，名单本身若已是
# 字典序，那个 `sorted` 就成了删掉也没人出声的死代码。
_CORRELATION_FIELDS: tuple[tuple[type[Waiter], str], ...] = (
    (UserInput, 'request_id'),
    (Confirmation, 'tool_call_id'),
    (Reply, 'correlation_id'),
)


def correlation_of(waiter: Waiter) -> str:
    """取出这次等待要匹配的关联 ID，并在交出之前复检它。

    **这里就是「密封」落地的地方，也是关联 ID 唯一的出口。** 数命中数而不是取第一个命中：一个
    实例同时是两种已登记的等待时，交出哪个 ID 都是猜，而猜错的症状是一次等不到答复的等待，只在
    超时之后才现身。出口复检因此也拦下构造校验的两条逃逸——覆写 `_validate` 不调 `super()`、
    以及 `object.__setattr__` 与反序列化绕过 frozen 塞进来的非法 ID。

    未知类型与歧义类型抛 `ContractViolation`（值本身没问题，错在此刻不该这样调）；ID 本身不合法
    抛 `ConfigError`。
    """
    matched = [
        (cls.__name__, field_name)
        for cls, field_name in _CORRELATION_FIELDS
        if isinstance(waiter, cls)
    ]
    if not matched:
        raise ContractViolation(
            expected='waiter 是 UserInput / Confirmation / Reply 之一（Waiter 是密封类型）',
            received=f'收到 {type(waiter).__name__}，它不在这三种之内',
            next_action=(
                '用这三种之一。要加新的等待类型，就在 waiting.py 里加一个 Waiter 子类并在'
                ' correlation_of 的名单里登记它的关联 ID——没登记的类型等不到任何答复'
            ),
        )
    if len(matched) > 1:
        names = sorted(name for name, _ in matched)
        raise ContractViolation(
            expected='waiter 只是三种登记类型中的一种：一次等待只能有一个关联 ID',
            received=f'收到 {type(waiter).__name__}，它同时是 {"、".join(names)}',
            next_action=(
                '每种等待各用一个 Waiter，不要让一个类型同时继承两种。同时是两种时交出哪个'
                '关联 ID 都是猜；真要一个类型匹配两种答复，先在 correlation_of 的名单里'
                '定清楚它取哪个 ID'
            ),
        )
    _, field_name = matched[0]
    return _validated_id(type(waiter).__name__, field_name, getattr(waiter, field_name, None))
