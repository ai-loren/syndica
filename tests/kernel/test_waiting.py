from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import cast

import pytest

from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.message import Message
from syndica.kernel.stores import InMemoryMessageBroker
from syndica.kernel.waiting import (
    Confirmation,
    Reply,
    UserInput,
    Waiter,
    WaitResult,
    WaitState,
    correlation_of,
)

# 校验与登记都是**每个子类各一份**（各自的 `_validate`、名单里各自的条目），所以凡是「每种等待都
# 必须成立」的性质都参数化到这份清单上：只考一个子类的用例放得过「某个子类漏了校验」——而漏掉的
# 那个的表现是一次没有上限的等待，只在长跑里现身。
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
# 归一化下不是不动点的 ID：恒等、`strip`、`lower`、`casefold` 在小写无空白的 ID 上是同一个函数，
# 拿 `call-7` 之类考不出「关联 ID 原样交回」。
_UNNORMALIZED_ID = '  Call-7  '
_SAMPLE = 256


def _message(correlation_id: str = 'call-7') -> Message:
    return Message(sender='user', receiver='a1', content='yes', correlation_id=correlation_id)


def _kinds_defined_in_the_module() -> list[type[Waiter]]:
    """`waiting.py` 里定义的每个 `Waiter` 子类，**含子类的子类**。

    只走直接子类会漏掉挂在某个已登记类型下面的新类型。递归那一支目前没有覆盖：模块里三个类都是
    `Waiter` 的直接子类，所以 `pending.extend` 这一行现在恒不产出新项。
    """
    found: list[type[Waiter]] = []
    pending = list(Waiter.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if cls.__module__ == Waiter.__module__:
            found.append(cls)
    return found


def _refused_by_correlation_of(waiter: Waiter) -> ContractViolation:
    with pytest.raises(ContractViolation) as excinfo:
        correlation_of(waiter)
    return excinfo.value


def test_four_wait_states_exist_in_this_order():
    """取值撞车会让成员静默塌成别名（`PREEMPTED = 'timeout'` 即 `TIMEOUT` 的第二个名字），
    枚举迭代跳过别名，因此这份清单少一项就变红。取值随 trace 落盘，属对外契约。"""
    assert [(state.name, state.value) for state in WaitState] == [
        ('ANSWERED', 'answered'),
        ('TIMEOUT', 'timeout'),
        ('PREEMPTED', 'preempted'),
        ('ABORTED', 'aborted'),
    ]


def test_a_wait_state_is_not_also_a_string():
    """混入 `str` 会让 `WaitState.TIMEOUT == 'timeout'` 为真，于是漏了 `.value` 的比较、以及拿
    裸字符串当状态用的调用方都不报错——掩盖掉本该现身的错误。"""
    assert str not in WaitState.__mro__
    assert WaitState.TIMEOUT != 'timeout'


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


@pytest.mark.parametrize('kind', list(_BUILD_WITH_ID))
def test_the_correlation_id_is_handed_back_byte_for_byte(kind: str):
    """关联 ID 是外部系统给的不透明串，两端与内部的空白、大小写都可能有意义。在归一化下不是不动点
    的 ID 上考：小写无空白的 ID 让恒等、`strip`、`lower`、`casefold` 变成同一个函数。"""
    assert correlation_of(_BUILD_WITH_ID[kind](30.0, _UNNORMALIZED_ID)) == _UNNORMALIZED_ID


@pytest.mark.parametrize('kind', list(_BUILD_WITH_ID))
async def test_the_correlation_id_matches_a_published_message_on_the_real_seam(kind: str):
    """`correlation_of` 的输出唯一的用处是喂给 `take_by_correlation`，而那里是逐字节比较。性质在
    `correlation_of` 那一层钉住不等于在这道接缝上钉住：任何归一化都会让答复躺在邮箱里、等待跑满
    超时、零日志。"""
    broker = InMemoryMessageBroker()
    await broker.ensure_queue('a1')
    await broker.publish('a1', _message(_UNNORMALIZED_ID))

    waiter = _BUILD_WITH_ID[kind](30.0, _UNNORMALIZED_ID)
    got = await broker.take_by_correlation('a1', correlation_of(waiter), timeout=0.1)

    assert got is not None
    assert got.correlation_id == _UNNORMALIZED_ID


def test_user_input_generates_its_own_request_id_when_not_given():
    first = UserInput(timeout=30.0)
    second = UserInput(timeout=30.0)
    assert first.request_id and second.request_id
    assert first.request_id != second.request_id


def test_a_generated_request_id_is_unpredictable_not_merely_unique():
    """不可预测性是**安全属性**：`request_id` 是三种等待里唯一一个调用方事先不知道的关联 ID
    （`tool_call_id` 由模型自己发出、`correlation_id` 由派活方给出），猜得到就意味着任何能往该
    Agent 邮箱 publish 的组件——包括被注入内容说服的模型自己——能伪造一条「人工输入的答复」把
    等待解掉，而那条路径绕过了人工确认。唯一性挡不住这个：顺序计数器完全满足唯一性。

    因此钉生成器的性质而不是结果：32 位小写 hex（杀掉计数器与截短）、大样本下字典序与生成顺序
    不相关（杀掉任何单调生成器，包括以递增 `time_low` 开头的 `uuid1`）、末 12 位逐个不同（杀掉
    把 MAC 写进关联 ID 的 `uuid1`，它另会让机器标识随 ID 落进 trace 与会话记录）。
    """
    ids = [UserInput(timeout=30.0).request_id for _ in range(_SAMPLE)]

    assert {len(ident) for ident in ids} == {32}
    assert all(set(ident) <= set('0123456789abcdef') for ident in ids)
    assert len(set(ids)) == _SAMPLE
    assert len({ident[-12:] for ident in ids}) == _SAMPLE
    ascending = sum(1 for lower, higher in zip(ids, ids[1:], strict=False) if lower < higher)
    assert _SAMPLE // 4 < ascending < _SAMPLE * 3 // 4


def test_the_sealed_list_is_exactly_these_three():
    """清单从模块自己的子类关系独立算一遍：新增一个 `Waiter` 子类而忘了登记，它等不到任何答复
    ——这条断言让「忘了登记」在合入前就变红，而修红的动作是把新类型补进 `_BUILD_WITH_ID`，
    超时 / 空 ID / frozen 三组性质随之自动覆盖到它。"""
    assert {cls.__name__ for cls in _kinds_defined_in_the_module()} == set(_BUILD_WITH_ID)


def test_no_listed_kind_is_a_subclass_of_another():
    """互为兄弟让三种等待各自可达：若有一种是另一种的子类，它就必然同时命中两条，永远拿不到
    关联 ID。兄弟关系**不**排除共同子类，那条由下面那个用例管。"""
    kinds = _kinds_defined_in_the_module()
    assert [
        (sub.__name__, base.__name__)
        for sub in kinds
        for base in kinds
        if sub is not base and issubclass(sub, base)
    ] == []


def test_a_waiter_that_is_two_registered_kinds_at_once_is_refused_instead_of_picking_one():
    """Python 不禁止多重继承，所以「同时是两种已登记的等待」是一个可构造的输入。此时交出哪个
    关联 ID 都是猜，而猜错的症状是一次等不到答复的等待，只在超时之后才现身——正是密封要防的
    那一类。数命中数（而不是取第一个命中）让答案只取决于命中的**集合**：既然多命中一律被拒，
    就不存在任何一个输入的结果由名单顺序决定，所以重排名单是真等价变换。

    同一对类型的两种基类次序报出同一份（字典序的）类型名，名单顺序因此连错误文本都改不动。
    """

    # 合成类型的名字刻意不含任何基类名：`received` 里也带着实例的类型名，含基类名就会让下面
    # 「报出的类型名按字典序」这条断言在类型名上命中，测不到那份名单。
    @dataclasses.dataclass(frozen=True)
    class TwoKindsAtOnce(Confirmation, Reply):
        pass

    @dataclasses.dataclass(frozen=True)
    class TwoKindsFlipped(Reply, Confirmation):
        pass

    @dataclasses.dataclass(frozen=True)
    class AskAndConfirm(UserInput, Confirmation):
        pass

    @dataclasses.dataclass(frozen=True)
    class AskAndDispatch(UserInput, Reply):
        pass

    # 不套 `dataclass` 的那一种：它继承 `Confirmation.__init__`，于是 `correlation_id` 这个字段
    # 根本不存在。歧义必须在读任何字段之前判出来，否则这条输入换来的是一个裸 AttributeError。
    class HalfBuilt(Confirmation, Reply):
        pass

    combined: list[tuple[Waiter, list[str]]] = [
        (HalfBuilt(timeout=30.0, tool_call_id='call-7'), ['Confirmation', 'Reply']),
        (
            TwoKindsAtOnce(timeout=30.0, tool_call_id='call-7', correlation_id='req-3'),
            ['Confirmation', 'Reply'],
        ),
        (
            TwoKindsFlipped(timeout=30.0, tool_call_id='call-7', correlation_id='req-3'),
            ['Confirmation', 'Reply'],
        ),
        (
            AskAndConfirm(timeout=30.0, tool_call_id='call-7', request_id='ask-1'),
            ['Confirmation', 'UserInput'],
        ),
        (
            AskAndDispatch(timeout=30.0, correlation_id='req-3', request_id='ask-1'),
            ['Reply', 'UserInput'],
        ),
    ]
    for waiter, both in combined:
        refused = _refused_by_correlation_of(waiter)
        assert all(name in refused.received for name in both)
        assert [refused.received.index(name) for name in both] == sorted(
            refused.received.index(name) for name in both
        )
        assert 'correlation_of' in refused.next_action


def test_an_unknown_waiter_is_refused_instead_of_silently_treated_as_one_of_the_three():
    """密封的意思就是这个：不在名单上的等待类型必须响亮失败，而不是落进某个默认分支——回落的
    后果是一次永远等不到答复的等待，只在超时之后才现身。基类自己也不在名单上。"""

    @dataclasses.dataclass(frozen=True)
    class Invented(Waiter):
        pass

    for waiter in (Waiter(timeout=30.0), Invented(timeout=30.0)):
        refused = _refused_by_correlation_of(waiter)
        assert type(waiter).__name__ in refused.received
        assert 'correlation_of' in refused.next_action


def test_a_subclass_of_a_listed_kind_correlates_by_the_id_it_inherited():
    """继承来的 `tool_call_id` 正是那个有意义的 ID，按它匹配不制造回落；密封挡的是**没有**关联
    ID 可用的类型，以及同时有两个可用的类型。"""

    @dataclasses.dataclass(frozen=True)
    class Escalation(Confirmation):
        pass

    assert correlation_of(Escalation(timeout=30.0, tool_call_id='call-9')) == 'call-9'


def test_a_subclass_that_overrides_validate_without_super_is_still_caught_at_the_only_exit():
    """`_validate` 是钩子，子类覆写它而忘了调 `super()._validate()` 就绕过了 ID 校验。关联 ID
    只有一个出口，所以复检收在那里：否则这个非法 ID 会一路走到 `take_by_correlation`，表现为
    静默等到超时。"""

    @dataclasses.dataclass(frozen=True)
    class Loose(Confirmation):
        def _validate(self) -> None:
            pass

    loose = Loose(timeout=30.0, tool_call_id='   ')
    with pytest.raises(ConfigError) as excinfo:
        correlation_of(loose)
    assert 'Loose' in excinfo.value.expected
    assert 'tool_call_id' in excinfo.value.expected
    assert 'tool_call_id' in excinfo.value.next_action


def test_an_id_smuggled_past_the_constructor_is_caught_at_the_only_exit():
    """`frozen` 挡不住 `object.__setattr__`，反序列化也从不经过 `__post_init__`。构造处的校验
    因此不是唯一防线——出口复检才是。"""
    waiter = Confirmation(timeout=30.0, tool_call_id='call-7')
    object.__setattr__(waiter, 'tool_call_id', b'call-7')
    with pytest.raises(ConfigError) as excinfo:
        correlation_of(waiter)
    assert 'bytes' in excinfo.value.received


def test_a_waiter_whose_id_field_was_never_set_is_refused_with_the_three_part_message():
    """`__new__` 不经过 `__post_init__`，而 pickle / copy / ORM 那类重建走的正是这条路。关联 ID
    这时根本不存在，读它抛的是不带三件套的 `AttributeError`。"""
    waiter = Confirmation.__new__(Confirmation)
    with pytest.raises(ConfigError) as excinfo:
        correlation_of(waiter)
    assert 'tool_call_id' in excinfo.value.expected
    assert 'NoneType' in excinfo.value.received


@pytest.mark.parametrize(
    'bad', [0.0, -1.0, float('inf'), float('nan')], ids=['zero', 'negative', 'inf', 'nan']
)
@pytest.mark.parametrize('kind', list(_BUILD_ANY))
def test_a_timeout_that_is_not_a_finite_positive_number_is_refused(kind: str, bad: float):
    """spec §4.2：超时是必须的，每个 Waiter 自带自己的上限。0 与负数没有可等的区间；`inf` 不是
    合法的秒数，算不出截止时刻；`nan` 与任何数比较都为假，塞进定时器堆即未定义行为。

    四个子类逐个考：`__post_init__` 的 timeout 校验漏掉任何一个，那个子类就没有上限。
    """
    with pytest.raises(ConfigError) as excinfo:
        _BUILD_ANY[kind](bad, 'id-1')
    assert kind in excinfo.value.expected
    assert repr(bad) in excinfo.value.received
    assert 'timeout' in excinfo.value.next_action


@pytest.mark.parametrize('bad', [None, '30', b'30'], ids=['None', 'str', 'bytes'])
@pytest.mark.parametrize('kind', list(_BUILD_ANY))
def test_a_non_numeric_timeout_is_refused_with_the_three_part_message(kind: str, bad: object):
    """要挡的就是违反标注的运行时取值（配置字典给出的 None）。少了类型检查，`None <= 0` 抛出的
    `TypeError` 不带「下一步做什么」，而这条错误的读者正在写调用方。"""
    with pytest.raises(ConfigError) as excinfo:
        _BUILD_ANY[kind](cast(float, bad), 'id-1')
    assert kind in excinfo.value.expected
    assert repr(bad) in excinfo.value.received
    assert type(bad).__name__ in excinfo.value.received
    assert 'timeout' in excinfo.value.next_action


@pytest.mark.parametrize('bad', [True, False], ids=['True', 'False'])
@pytest.mark.parametrize('kind', list(_BUILD_ANY))
def test_a_bool_timeout_is_refused_because_yaml_parses_yes_into_it(kind: str, bad: bool):
    """`bool` 是 `int` 的子类，所以数值检查放它过去，而 `True` 当秒数用恰好等于 1.0 秒。真实来路
    是 YAML 1.1 把 `timeout: yes` 解析成 `True`——正是本模块自陈要挡的「配置字典给出的违规值」，
    而它会静默变成 1 秒超时：每次等人工输入都在一秒后超时且不报错。"""
    with pytest.raises(ConfigError) as excinfo:
        _BUILD_ANY[kind](cast(float, bad), 'id-1')
    assert kind in excinfo.value.expected
    assert repr(bad) in excinfo.value.received
    assert 'timeout' in excinfo.value.next_action
    assert 'YAML' in excinfo.value.next_action


@pytest.mark.parametrize('kind', list(_BUILD_ANY))
def test_an_int_timeout_is_accepted(kind: str):
    """另一个方向也要定死：只收 `float` 会拒掉 `timeout=30` 这种最自然的写法，而这套用例若只考
    非法值，「哪些数值类型可接受」两个方向都没有定义。"""
    assert _BUILD_ANY[kind](30, 'id-1').timeout == 30


@pytest.mark.parametrize('kind', list(_BUILD_ANY))
def test_a_timeout_too_large_for_a_float_is_refused_with_the_three_part_message(kind: str):
    """超出 float 范围的 int 让 `math.isfinite` 抛出 `OverflowError`，而那个异常不带三件套；
    `10 ** 308` 转得成 float 所以照收，两者的分界在此。"""
    with pytest.raises(ConfigError) as excinfo:
        _BUILD_ANY[kind](cast(float, 10**400), 'id-1')
    assert kind in excinfo.value.expected
    assert 'timeout' in excinfo.value.next_action


@pytest.mark.parametrize('bad', [None, 123, b'call-7', ['a']], ids=['None', 'int', 'bytes', 'list'])
@pytest.mark.parametrize('kind', list(_BUILD_WITH_ID))
def test_a_non_string_correlation_id_is_refused_with_the_three_part_message(kind: str, bad: object):
    """`take_by_correlation` 逐字节比较 `str`，`b'call-7' != 'call-7'`，于是答复躺在邮箱里、
    等待跑满超时、零日志。ID 与 timeout 来自同一批外部来源（配置、工具调用载荷、跨进程反序列化），
    只挡一半等于没挡；`None` / `123` 少了这道检查抛的是不带三件套的 `AttributeError`。"""
    with pytest.raises(ConfigError) as excinfo:
        _BUILD_WITH_ID[kind](30.0, cast(str, bad))
    assert kind in excinfo.value.expected
    assert _ID_FIELD[kind] in excinfo.value.expected
    assert repr(bad) in excinfo.value.received
    assert type(bad).__name__ in excinfo.value.received
    assert _ID_FIELD[kind] in excinfo.value.next_action


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
    assert _ID_FIELD[kind] in excinfo.value.next_action


def test_an_error_about_a_subclass_names_the_subclass_not_the_registered_base():
    """这条消息的读者正在写子类。写死基类名会指着一个没问题的地方，`errors._validated` 也是靠
    构造中的那个类取名的。"""

    @dataclasses.dataclass(frozen=True)
    class Escalation(Confirmation):
        pass

    with pytest.raises(ConfigError) as blank_id:
        Escalation(timeout=30.0, tool_call_id='')
    assert 'Escalation' in blank_id.value.expected
    assert 'Confirmation' not in blank_id.value.expected

    with pytest.raises(ConfigError) as bad_timeout:
        Escalation(timeout=0.0, tool_call_id='call-7')
    assert 'Escalation' in bad_timeout.value.expected
    assert 'Confirmation' not in bad_timeout.value.expected


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


@pytest.mark.parametrize('kind', list(_BUILD_ANY))
def test_two_waiters_with_the_same_fields_are_equal(kind: str):
    """调用方会拿值相等去判「这是同一次等待」（去重、幂等重试）；换成同一性相等会让每次重建的
    Waiter 都成为一个新的等待。基类也要考：每个子类各生成一份 `__eq__`，只考子类的用例挡不住
    基类上的 `eq=False`。"""
    assert _BUILD_ANY[kind](30.0, 'id-1') == _BUILD_ANY[kind](30.0, 'id-1')
    assert _BUILD_ANY[kind](30.0, 'id-1') != _BUILD_ANY[kind](1.0, 'id-1')


@pytest.mark.parametrize('kind', list(_BUILD_WITH_ID))
def test_two_waiters_for_different_requests_are_not_equal(kind: str):
    assert _BUILD_WITH_ID[kind](30.0, 'id-1') != _BUILD_WITH_ID[kind](30.0, 'id-2')


def test_wait_result_fields_are_state_then_message():
    """字段顺序计入公开 API：换序会让既有的位置构造把消息传成状态。"""
    assert [f.name for f in dataclasses.fields(WaitResult)] == ['state', 'message']


def test_two_wait_results_describing_the_same_outcome_are_equal():
    message = _message()
    assert WaitResult(state=WaitState.TIMEOUT) == WaitResult(state=WaitState.TIMEOUT)
    assert WaitResult(state=WaitState.TIMEOUT) != WaitResult(state=WaitState.ABORTED)
    assert WaitResult(state=WaitState.ANSWERED, message=message) == WaitResult(
        state=WaitState.ANSWERED, message=message
    )


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
    assert 'state' in excinfo.value.next_action
    assert 'message' in excinfo.value.next_action


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
    assert 'state' in excinfo.value.next_action
    assert 'message' in excinfo.value.next_action


def test_the_state_of_a_wait_result_cannot_be_rewritten_after_it_is_returned():
    """`state` 是调用方分支的依据，改得动就意味着一次超时可以被就地伪装成拿到了答复。frozen 只
    管住这两个引用本身，`message` 指向的 `Message` 与别处的一样可变。"""
    result = WaitResult(state=WaitState.TIMEOUT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.state = WaitState.ANSWERED  # pyright: ignore[reportAttributeAccessIssue]
    assert result.state is WaitState.TIMEOUT
