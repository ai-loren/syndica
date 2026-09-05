import dataclasses

import pytest

from syndica.kernel.context import TrustLevel
from syndica.kernel.message import DeliveryResult, Message, Trigger


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


def test_delivery_result_defaults_to_no_receipt_and_no_reason():
    result = DeliveryResult(ok=True)
    assert result.message_id is None
    assert result.reason is None


def test_delivery_result_rejects_mutation():
    """投递回执一旦交给调用方就不该被改写，否则失败可以被就地伪装成成功。"""
    result = DeliveryResult(ok=False, reason='nope')
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ok = True  # pyright: ignore[reportAttributeAccessIssue]
