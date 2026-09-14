from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import cast

import pytest

from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.message import Message
from syndica.kernel.waiting import (
    Confirmation,
    Reply,
    UserInput,
    Waiter,
    WaitResult,
    WaitState,
    correlation_of,
)

# 校验与登记都是**每个子类各一份**（各自的 `_validate`、`correlation_of` 里各自的分支），所以
# 凡是「每种等待都必须成立」的性质都参数化到这份清单上：只考一个子类的用例放得过「某个子类漏了
# 校验」——而漏掉的那个的表现是一次没有上限的等待，只在长跑里现身。
_BUILD_WITH_ID: dict[str, Callable[[float, str], Waiter]] = {
    'UserInput': lambda timeout, ident: UserInput(timeout=timeout, request_id=ident),
    'Confirmation': lambda timeout, ident: Confirmation(timeout=timeout, tool_call_id=ident),
    'Reply': lambda timeout, ident: Reply(timeout=timeout, correlation_id=ident),
}
_ID_FIELD: dict[str, str] = {
    'UserInput': 'request_id',
    'Confirmation': 'tool_call_id',
    'Reply': 'correlation_id',
}
# 基类自己也走 `__post_init__`，timeout 与 frozen 两条性质对它同样成立。
_BUILD_ANY: dict[str, Callable[[float, str], Waiter]] = {
    'Waiter': lambda timeout, _ident: Waiter(timeout=timeout),
    **_BUILD_WITH_ID,
}


def _message() -> Message:
    return Message(sender='user', receiver='a1', content='yes', correlation_id='call-7')


def _kinds_defined_in_the_module() -> list[type[Waiter]]:
    """`waiting.py` 里定义的每个 `Waiter` 子类，**含子类的子类**。

    只走直接子类会漏掉挂在某个已登记类型下面的新类型——而那恰好是 isinstance 链会吃掉的那一种。
    """
    found: list[type[Waiter]] = []
    pending = list(Waiter.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if cls.__module__ == Waiter.__module__:
            found.append(cls)
    return found


def test_four_wait_states_exist_in_this_order():
    """取值撞车会让成员静默塌成别名（`PREEMPTED = 'timeout'` 即 `TIMEOUT` 的第二个名字），
    枚举迭代跳过别名，因此这份清单少一项就变红。取值随 trace 落盘，属对外契约。"""
    assert [(state.name, state.value) for state in WaitState] == [
        ('ANSWERED', 'answered'),
        ('TIMEOUT', 'timeout'),
        ('PREEMPTED', 'preempted'),
        ('ABORTED', 'aborted'),
    ]


@pytest.mark.parametrize(
    ('build', 'expected'),
    [
        (lambda: Confirmation(timeout=30.0, tool_call_id='call-7'), 'call-7'),
        (lambda: Confirmation(timeout=30.0, tool_call_id='call-8'), 'call-8'),
        (lambda: Reply(timeout=30.0, correlation_id='req-3'), 'req-3'),
        (lambda: Reply(timeout=30.0, correlation_id='req-4'), 'req-4'),
        (lambda: UserInput(timeout=30.0, request_id='ask-1'), 'ask-1'),
        (lambda: UserInput(timeout=30.0, request_id='ask-2'), 'ask-2'),
    ],
)
def test_each_waiter_kind_maps_to_its_own_correlation_id(
    build: Callable[[], Waiter], expected: str
):
    """三种等待共用一条实现（按关联 ID 匹配），映射写错就是等一个永远不会来的答复。每种两个实例：
    一个实例分不出「读了自己的字段」与「读了某份共享的东西」。"""
    assert correlation_of(build()) == expected


def test_user_input_generates_its_own_request_id_when_not_given():
    first = UserInput(timeout=30.0)
    second = UserInput(timeout=30.0)
    assert first.request_id and second.request_id
    assert first.request_id != second.request_id


def test_the_sealed_list_is_exactly_these_three():
    """清单从模块自己的子类关系独立算一遍：新增一个 `Waiter` 子类而忘了在 `correlation_of` 里
    登记，它等不到任何答复——这条断言让「忘了登记」在合入前就变红，而修红的动作是把新类型补进
    `_BUILD_WITH_ID`，超时 / 空 ID / frozen 三组性质随之自动覆盖到它。"""
    assert {cls.__name__ for cls in _kinds_defined_in_the_module()} == set(_BUILD_WITH_ID)


def test_no_listed_kind_is_a_subclass_of_another():
    """`correlation_of` 是一条 isinstance 链，只要有一种等待是另一种的子类，先出现的分支就会把
    它吃掉、交出父类那个关联 ID。互为兄弟是「分支顺序不影响结果」的前提，把前提钉在这里：
    新增的等待类型若挂在某个已登记类型下面，必须同时想清楚它在链上的位置。"""
    kinds = _kinds_defined_in_the_module()
    assert [
        (sub.__name__, base.__name__)
        for sub in kinds
        for base in kinds
        if sub is not base and issubclass(sub, base)
    ] == []


def test_an_unknown_waiter_is_refused_instead_of_silently_treated_as_one_of_the_three():
    """密封的意思就是这个：不在名单上的等待类型必须响亮失败，而不是落进某个默认分支——回落的
    后果是一次永远等不到答复的等待，只在超时之后才现身。基类自己也不在名单上。"""

    @dataclasses.dataclass(frozen=True)
    class Invented(Waiter):
        pass

    for waiter in (Waiter(timeout=30.0), Invented(timeout=30.0)):
        with pytest.raises(ContractViolation) as excinfo:
            correlation_of(waiter)
        assert type(waiter).__name__ in excinfo.value.received


def test_a_subclass_of_a_listed_kind_correlates_by_the_id_it_inherited():
    """继承来的 `tool_call_id` 正是那个有意义的 ID，按它匹配不制造回落；密封挡的是**没有**关联
    ID 可用的类型。"""

    @dataclasses.dataclass(frozen=True)
    class Escalation(Confirmation):
        pass

    assert correlation_of(Escalation(timeout=30.0, tool_call_id='call-9')) == 'call-9'


@pytest.mark.parametrize(
    'bad', [0.0, -1.0, float('inf'), float('nan')], ids=['zero', 'negative', 'inf', 'nan']
)
@pytest.mark.parametrize('kind', list(_BUILD_ANY))
def test_a_timeout_that_is_not_a_finite_positive_number_is_refused(kind: str, bad: float):
    """spec §4.2：超时是必须的——长跑里等人工输入可能几小时无人查看，没有上限就是永久挂起。
    `inf` 就是「没有上限」的另一种拼法；`nan` 与任何数比较都为假，塞进定时器堆即未定义行为。

    四个子类逐个考：`__post_init__` 的 timeout 校验漏掉任何一个，那个子类就没有上限。
    """
    with pytest.raises(ConfigError) as excinfo:
        _BUILD_ANY[kind](bad, 'id-1')
    assert kind in excinfo.value.expected
    assert repr(bad) in excinfo.value.received


@pytest.mark.parametrize('bad', [None, '30', b'30'], ids=['None', 'str', 'bytes'])
@pytest.mark.parametrize('kind', list(_BUILD_ANY))
def test_a_non_numeric_timeout_is_refused_with_the_three_part_message(kind: str, bad: object):
    """要挡的就是违反标注的运行时取值（配置字典给出的 None）。少了类型检查，`None <= 0` 抛出的
    `TypeError` 不带「下一步做什么」，而这条错误的读者正在写调用方。"""
    with pytest.raises(ConfigError) as excinfo:
        _BUILD_ANY[kind](cast(float, bad), 'id-1')
    assert kind in excinfo.value.expected
    assert repr(bad) in excinfo.value.received


@pytest.mark.parametrize('blank', ['', '   ', '\n\t'], ids=['empty', 'spaces', 'whitespace'])
@pytest.mark.parametrize('kind', list(_BUILD_WITH_ID))
def test_a_blank_correlation_id_is_refused_and_the_message_names_that_kind_and_field(
    kind: str, blank: str
):
    """空白关联 ID 谁都匹配不上，表现为静默等到超时——比当场报错难查得多。整串空白与空串同样是
    上游没填上，`errors._validated` 对三件套也是这么判的。消息必须指着**这个**子类的**那个**
    字段：指错地方的错误让读者去改一个没问题的地方。"""
    with pytest.raises(ConfigError) as excinfo:
        _BUILD_WITH_ID[kind](30.0, blank)
    assert kind in excinfo.value.expected
    assert _ID_FIELD[kind] in excinfo.value.expected
    assert repr(blank) in excinfo.value.received


@pytest.mark.parametrize('kind', list(_BUILD_ANY))
def test_a_waiters_timeout_cannot_be_changed_after_it_is_handed_over(kind: str):
    waiter = _BUILD_ANY[kind](30.0, 'id-1')
    with pytest.raises(dataclasses.FrozenInstanceError):
        waiter.timeout = 1.0  # pyright: ignore[reportAttributeAccessIssue]
    assert waiter.timeout == 30.0


@pytest.mark.parametrize('kind', list(_BUILD_WITH_ID))
def test_a_waiter_cannot_be_retargeted_after_it_is_handed_over(kind: str):
    """交给 `wait_for` 之后还能改掉关联 ID，那次等待就永远等不到答复。"""
    waiter = _BUILD_WITH_ID[kind](30.0, 'id-1')
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(waiter, _ID_FIELD[kind], 'id-2')
    assert correlation_of(waiter) == 'id-1'


def test_wait_result_fields_are_state_then_message():
    """字段顺序计入公开 API：换序会让既有的位置构造把消息传成状态。"""
    assert [f.name for f in dataclasses.fields(WaitResult)] == ['state', 'message']


@pytest.mark.parametrize(
    'state',
    [state for state in WaitState if state is not WaitState.ANSWERED],
    ids=lambda state: state.name,
)
def test_a_result_that_did_not_get_an_answer_carries_no_message(state: WaitState):
    assert WaitResult(state=state).state is state
    assert WaitResult(state=state).message is None


@pytest.mark.parametrize(
    'state',
    [state for state in WaitState if state is not WaitState.ANSWERED],
    ids=lambda state: state.name,
)
def test_a_result_that_did_not_get_an_answer_refuses_to_carry_one(state: WaitState):
    """超时 / 被顶掉 / 被中止都没有答复可交；硬塞一条消息会让调用方误以为等到了。"""
    with pytest.raises(ContractViolation) as excinfo:
        WaitResult(state=state, message=_message())
    assert state.name in excinfo.value.received


def test_an_answered_result_carries_the_message_that_answered_it():
    message = _message()
    result = WaitResult(state=WaitState.ANSWERED, message=message)
    assert result.message is message


def test_an_answered_result_without_a_message_is_refused():
    """`answered` 的全部含义就是「这条消息匹配上了」；没有消息的 `answered` 是产出方的缺陷，
    而它在调用方那里表现为一次拿不到内容的成功。"""
    with pytest.raises(ContractViolation) as excinfo:
        WaitResult(state=WaitState.ANSWERED)
    assert WaitState.ANSWERED.name in excinfo.value.received


def test_a_wait_result_cannot_be_rewritten_after_it_is_returned():
    """结果一旦交给调用方就不该被改写，否则一次超时可以被就地伪装成拿到了答复。"""
    result = WaitResult(state=WaitState.TIMEOUT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.state = WaitState.ANSWERED  # pyright: ignore[reportAttributeAccessIssue]
