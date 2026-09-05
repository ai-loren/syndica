import dataclasses

import pytest

from syndica.kernel.context import TrustLevel
from syndica.kernel.message import DeliveryResult, Message, Trigger

# Message 当前每个字段都是标量，「消息是快照」因此只在顶层可观测，嵌套容器的隔离用例会空转。
# 新增可变容器字段时 test_message_carries_no_mutable_container_field 会变红，提醒补上那条断言。
_SCALAR_ANNOTATIONS = frozenset({'str', 'str | None', 'Trigger', 'TrustLevel'})


def _msg() -> Message:
    return Message(sender='s', receiver='a1', content='hi')


def test_two_triggers_exist():
    """取值撞车会让成员静默塌成别名（DEFERRED 即 PENDING）；取值随 trace 落盘，属对外契约。"""
    assert {trigger.name: trigger.value for trigger in Trigger} == {
        'PENDING': 'pending',
        'DEFERRED': 'deferred',
    }


def test_message_defaults_to_pending_and_trusted():
    message = _msg()
    assert message.trigger is Trigger.PENDING
    assert message.trust is TrustLevel.TRUSTED
    assert message.correlation_id is None


def test_message_ids_differ_between_instances():
    """id 是 DeliveryResult 唯一的回执；共用一个默认值会让两条消息的投递结果无法区分。"""
    assert _msg().id != _msg().id


def test_message_requires_sender_receiver_and_content():
    with pytest.raises(TypeError):
        Message(receiver='a1', content='hi')  # pyright: ignore[reportCallIssue]
    with pytest.raises(TypeError):
        Message(sender='s', content='hi')  # pyright: ignore[reportCallIssue]
    with pytest.raises(TypeError):
        Message(sender='s', receiver='a1')  # pyright: ignore[reportCallIssue]


def test_message_carries_no_mutable_container_field():
    containers = {
        f.name: str(f.type)
        for f in dataclasses.fields(Message)
        if str(f.type) not in _SCALAR_ANNOTATIONS
    }
    assert not containers, (
        '期望 Message 的每个字段都是标量，快照性质因此在顶层就能验证完。'
        f' 实际出现了非标量字段: {containers}。'
        ' 给 MessageBrokerContract 补一条断言：改动该容器的内部元素之后，已入队的消息不受'
        ' 影响；然后把新字段的标注加进 _SCALAR_ANNOTATIONS 或改写本用例。'
    )


def test_delivery_result_defaults_to_no_receipt_and_no_reason():
    result = DeliveryResult(ok=True)
    assert result.message_id is None
    assert result.reason is None


def test_delivery_result_rejects_mutation():
    """投递回执一旦交给调用方就不该被改写，否则失败可以被就地伪装成成功。"""
    result = DeliveryResult(ok=False, reason='nope')
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ok = True  # pyright: ignore[reportAttributeAccessIssue]
