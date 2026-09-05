import copy
import pickle

import pytest

from syndica.kernel.errors import (
    ConfigError,
    ContractViolation,
    LLMResponseError,
    SyndicaError,
    ToolFatalError,
)

ROUNDTRIPS = [copy.copy, copy.deepcopy, lambda err: pickle.loads(pickle.dumps(err))]
ROUNDTRIP_IDS = ['copy', 'deepcopy', 'pickle']
ERROR_CLASSES = [SyndicaError, ContractViolation, ConfigError, LLMResponseError, ToolFatalError]


def test_message_contains_all_three_parts():
    err = SyndicaError(
        expected='kind=spreadsheet 的处理器唯一',
        received='syndica-office 与 syndica-sheets 都注册了 mime=xlsx',
        next_action='卸载其中之一，或用更具体的 mime 区分',
    )
    text = str(err)
    assert 'kind=spreadsheet 的处理器唯一' in text
    assert 'syndica-office' in text
    assert '卸载其中之一' in text


def test_all_three_parts_are_required():
    """三件套缺一不可（Global Constraints）——构造时就拦，不留到 code review。"""
    with pytest.raises(ValueError, match='next_action'):
        SyndicaError(expected='a', received='b', next_action='')


@pytest.mark.parametrize('blank', ['', '   ', '\n\t'])
@pytest.mark.parametrize('field', ['expected', 'received', 'next_action'])
def test_every_part_rejects_blank_and_names_itself(field, blank):
    parts = {'expected': 'a', 'received': 'b', 'next_action': 'c'}
    parts[field] = blank
    with pytest.raises(ValueError, match=field):
        SyndicaError(
            expected=parts['expected'],
            received=parts['received'],
            next_action=parts['next_action'],
        )


@pytest.mark.parametrize('bad', [None, 0, b'a', ['a']], ids=['None', 'int', 'bytes', 'list'])
@pytest.mark.parametrize('field', ['expected', 'received', 'next_action'])
def test_non_str_part_is_rejected_with_the_field_name_and_the_type_received(field, bad):
    parts = {'expected': 'a', 'received': 'b', 'next_action': 'c'}
    parts[field] = bad
    with pytest.raises(ValueError, match=field) as excinfo:
        SyndicaError(
            expected=parts['expected'],
            received=parts['received'],
            next_action=parts['next_action'],
        )
    assert type(bad).__name__ in str(excinfo.value)


@pytest.mark.parametrize('error_cls', ERROR_CLASSES, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize('roundtrip', ROUNDTRIPS, ids=ROUNDTRIP_IDS)
def test_roundtrip_preserves_the_class_and_all_three_parts(roundtrip, error_cls):
    """异常会跨进程边界传递（Store 实现、子进程执行器），copy / pickle 必须无损。"""
    err = error_cls(
        expected='kind=spreadsheet 的处理器唯一',
        received='syndica-office 与 syndica-sheets 都注册了 mime=xlsx',
        next_action='卸载其中之一，或用更具体的 mime 区分',
    )
    clone = roundtrip(err)
    assert type(clone) is error_cls
    assert clone.expected == err.expected
    assert clone.received == err.received
    assert clone.next_action == err.next_action
    assert str(clone) == str(err)


@pytest.mark.parametrize('roundtrip', ROUNDTRIPS, ids=ROUNDTRIP_IDS)
def test_roundtrip_keeps_notes_attached_after_construction(roundtrip):
    err = SyndicaError(expected='a', received='b', next_action='c')
    err.add_note('这条 note 必须活过往返')
    assert roundtrip(err).__notes__ == ['这条 note 必须活过往返']


def test_tool_fatal_error_also_carries_the_contract():
    """致命工具错误会传播到 agent loop，因此它同样要说清下一步该做什么。"""
    err = ToolFatalError(
        expected='目标仓库存在且可写',
        received='路径不存在，且父目录不可写',
        next_action='换一个可写路径，或先创建父目录',
    )
    assert isinstance(err, SyndicaError)
    assert '换一个可写路径' in str(err)
