import copy
import pickle
from importlib import import_module
from inspect import getmembers, isclass
from pathlib import Path
from pkgutil import walk_packages
from types import ModuleType

import pytest

from syndica import kernel
from syndica.kernel.errors import SyndicaError, ToolFatalError

ROUNDTRIPS = [copy.copy, copy.deepcopy, lambda err: pickle.loads(pickle.dumps(err))]
ROUNDTRIP_IDS = ['copy', 'deepcopy', 'pickle']


def _kernel_modules() -> list[ModuleType]:
    """kernel 包自身，加上它下面的每个模块（含子包里的）。"""
    modules = [kernel]
    for module_info in walk_packages(kernel.__path__, f'{kernel.__name__}.'):
        modules.append(import_module(module_info.name))
    return modules


def _kernel_error_classes() -> list[type[SyndicaError]]:
    """kernel 里出现的每个异常类，按类名排序。

    枚举包里的模块并逐个导入，而不是读 `SyndicaError.__subclasses__()`：后者只看得见已导入的
    类，某个模块没被导入不会报错，只会让覆盖看起来齐全而实际是空的。
    """
    found: set[type[SyndicaError]] = set()
    for module in _kernel_modules():
        for _name, obj in getmembers(module, isclass):
            if issubclass(obj, SyndicaError):
                found.add(obj)
    return sorted(found, key=lambda cls: cls.__name__)


KERNEL_ERROR_CLASSES = _kernel_error_classes()


def _module_name_on_disk(path: Path, root: Path) -> str:
    parts = path.relative_to(root).with_suffix('').parts
    if parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join((kernel.__name__, *parts))


def test_the_walk_imports_every_module_under_the_kernel():
    """期望值从文件树独立算一遍，不与 `_kernel_modules` 同源。发现依赖「每个模块都真的被导入」：
    只导入子包的 `__init__` 时，按类名比对的清单往往照样凑齐（`__init__` re-export 了它们），
    而子包里**没被 re-export** 的子类会无声缺席。"""
    root = Path(kernel.__path__[0])
    on_disk = {_module_name_on_disk(path, root) for path in root.rglob('*.py')}
    assert {module.__name__ for module in _kernel_modules()} == on_disk


def test_discovery_covers_every_error_class_present_in_the_kernel():
    """往返用例参数化自这份清单，新子类因此自动落进覆盖；但清单少认一个不会报错，参数化只会
    跟着缩水。断言确切内容让缩水必须先变红——新增子类要么已被覆盖，要么在这里被拦住。"""
    assert [cls.__name__ for cls in KERNEL_ERROR_CLASSES] == [
        'ConfigError',
        'ContextOverflowError',
        'ContractViolation',
        'LLMResponseError',
        'SyndicaError',
        'ToolFatalError',
    ]


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


@pytest.mark.parametrize('error_cls', KERNEL_ERROR_CLASSES, ids=lambda cls: cls.__name__)
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
