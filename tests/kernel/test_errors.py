import pytest

from syndica.kernel.errors import SyndicaError


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


def test_tool_fatal_error_also_carries_the_contract():
    """致命工具错误会传播到 agent loop，因此它同样要说清下一步该做什么。"""
    from syndica.kernel.errors import ToolFatalError

    err = ToolFatalError(
        expected='目标仓库存在且可写',
        received='路径不存在，且父目录不可写',
        next_action='换一个可写路径，或先创建父目录',
    )
    assert isinstance(err, SyndicaError)
    assert '换一个可写路径' in str(err)
