import copy
import pickle
import sys
from importlib import import_module
from inspect import Parameter, getmembers, isclass, signature
from pathlib import Path
from pkgutil import walk_packages
from types import ModuleType

import pytest

from syndica import kernel
from syndica.kernel.errors import SyndicaError, ToolFatalError

ROUNDTRIPS = [copy.copy, copy.deepcopy, lambda err: pickle.loads(pickle.dumps(err))]
ROUNDTRIP_IDS = ['copy', 'deepcopy', 'pickle']
PARTS = {
    'expected': 'kind=spreadsheet 的处理器唯一',
    'received': 'syndica-office 与 syndica-sheets 都注册了 mime=xlsx',
    'next_action': '卸载其中之一，或用更具体的 mime 区分',
}


def _kernel_modules() -> list[ModuleType]:
    """kernel 包自身，加上它下面的每个模块（含子包里的）。"""
    modules = [kernel]
    for module_info in walk_packages(kernel.__path__, f'{kernel.__name__}.'):
        modules.append(import_module(module_info.name))
    return modules


def _classes_in(module: ModuleType) -> set[type]:
    """模块里的每个类，**包括嵌在类体里的**。

    `getmembers(module, isclass)` 只看模块属性：`class Store: class NotFound(SyndicaError)`
    这种写法它拿到的是外层 `Store`，内层类被 issubclass 过滤掉，于是覆盖静默漏一个。仓内有四个
    Store Protocol，这个写法很自然。只下潜本模块定义的类——嵌套子类与外层同模块，而外来类的
    成员是别人的事。
    """
    found: set[type] = set()
    pending = [obj for _name, obj in getmembers(module, isclass)]
    while pending:
        obj = pending.pop()
        if obj in found:
            continue
        found.add(obj)
        if obj.__module__ == module.__name__:
            pending.extend(nested for _name, nested in getmembers(obj, isclass))
    return found


def _kernel_error_classes() -> list[type[SyndicaError]]:
    """kernel 里出现的每个异常类，按类名排序。

    枚举包里的模块并逐个导入，而不是读 `SyndicaError.__subclasses__()`：后者只看得见已导入的
    类，某个模块没被导入不会报错，只会让覆盖看起来齐全而实际是空的。
    """
    found: set[type[SyndicaError]] = set()
    for module in _kernel_modules():
        for obj in _classes_in(module):
            if issubclass(obj, SyndicaError):
                found.add(obj)
    return sorted(found, key=lambda cls: cls.__name__)


def _probe(error_cls: type[SyndicaError]) -> SyndicaError:
    """按类自己的签名造一个探针实例：子类额外的**必填**参数用占位串补上。

    守卫要考的是「往返能不能无损」，不是「构造签名有没有多参数」。写死三件套会连合规子类
    （额外必填参数 + 自己覆写 `__reduce__`）一起拦下，而拦住正确代码的守卫会被人关掉。
    占位串意味着非 str 的必填参数仍需子类自备默认值。
    """
    extra = {
        name: 'probe'
        for name, parameter in signature(error_cls).parameters.items()
        if parameter.default is Parameter.empty
        and parameter.kind not in (Parameter.VAR_POSITIONAL, Parameter.VAR_KEYWORD)
        and name not in PARTS
    }
    return error_cls(**PARTS, **extra)


KERNEL_ERROR_CLASSES = _kernel_error_classes()


class _ProbeStore:
    class NotFound(SyndicaError):
        """探针：嵌在类体里的异常子类。它自身不是 kernel 模块的成员，不进覆盖清单。"""


class _KeyedError(SyndicaError):
    """合规子类：额外必填参数 + 覆写 `__reduce__`，正是 `_rebuild` 的 docstring 要求的形态。"""

    def __init__(self, *, key: str, expected: str, received: str, next_action: str) -> None:
        super().__init__(expected=expected, received=received, next_action=next_action)
        self.key = key

    def __reduce__(self) -> tuple[object, ...]:
        return (
            _rebuild_keyed,
            (type(self), self.key, self.expected, self.received, self.next_action),
            self.__dict__,
        )


class _UnreducedError(SyndicaError):
    """不合规子类：额外必填参数却沿用基类的 `__reduce__`，重建时必然缺参数。"""

    def __init__(self, *, key: str, expected: str, received: str, next_action: str) -> None:
        super().__init__(expected=expected, received=received, next_action=next_action)
        self.key = key


def _rebuild_keyed(
    cls: type[_KeyedError], key: str, expected: str, received: str, next_action: str
) -> _KeyedError:
    return cls(key=key, expected=expected, received=received, next_action=next_action)


# 分隔符带前导空格：这些消息是单行三件套，不带空格会匹配到被嵌进去的另一份。
_THREE_PART: tuple[str, ...] = ('期望: ', ' 实际: ', ' 下一步: ')


def _assert_three_part(text: str) -> None:
    positions = [text.find(mark) for mark in _THREE_PART]
    assert -1 not in positions
    assert positions == sorted(positions)


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


def test_discovery_reaches_a_subclass_nested_in_a_class_body():
    """`Store.NotFound` 这样藏起来的子类不是模块属性。实测它加一个必填参数后 copy / deepcopy /
    pickle 三条往返全坏，而只看模块属性的枚举让整套测试保持全绿。"""
    assert _ProbeStore.NotFound in _classes_in(sys.modules[__name__])


def test_discovery_covers_every_error_class_present_in_the_kernel():
    """往返用例参数化自这份清单，新子类因此自动落进覆盖；但清单少认一个不会报错，参数化只会
    跟着缩水。断言确切内容让缩水必须先变红——新增子类要么已被覆盖，要么在这里被拦住。"""
    assert [cls.__name__ for cls in KERNEL_ERROR_CLASSES] == [
        'ConfigError',
        'ContextOverflowError',
        'ContractViolation',
        'LLMResponseError',
        'NewerSessionSchema',
        'NewerTraceSchema',
        'PayloadRefused',
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
    """类型不符是 TypeError 而非 ValueError：调用方分不出「传错了类型」与「传了空串」，就只剩匹配
    文本这条路，而这两个异常类型是公开 API。"""
    parts = {'expected': 'a', 'received': 'b', 'next_action': 'c'}
    parts[field] = bad
    with pytest.raises(TypeError, match=field) as excinfo:
        SyndicaError(
            expected=parts['expected'],
            received=parts['received'],
            next_action=parts['next_action'],
        )
    assert type(bad).__name__ in str(excinfo.value)
    _assert_three_part(str(excinfo.value))


@pytest.mark.parametrize(
    'error_cls', [*KERNEL_ERROR_CLASSES, _KeyedError], ids=lambda cls: cls.__name__
)
@pytest.mark.parametrize(
    ('bad', 'raised'), [(None, TypeError), ('', ValueError)], ids=['wrong-type', 'blank']
)
def test_a_rejected_part_names_the_class_being_constructed_not_the_base(error_cls, bad, raised):
    """这条消息的读者正在写子类；报出基类名会指着一个没问题的地方。"""
    parts = {**PARTS, 'expected': bad}
    extra = {
        name: 'probe'
        for name, parameter in signature(error_cls).parameters.items()
        if parameter.default is Parameter.empty
        and parameter.kind not in (Parameter.VAR_POSITIONAL, Parameter.VAR_KEYWORD)
        and name not in PARTS
    }
    with pytest.raises(raised) as excinfo:
        error_cls(**parts, **extra)
    assert error_cls.__name__ in str(excinfo.value)
    if error_cls is not SyndicaError:
        assert SyndicaError.__name__ not in str(excinfo.value)


def test_a_blank_part_is_a_three_part_message_too():
    with pytest.raises(ValueError) as excinfo:
        SyndicaError(expected='a', received='b', next_action='  ')
    _assert_three_part(str(excinfo.value))


def test_multiple_invalid_parts_are_checked_in_constructor_order():
    """一次只报最先能修的字段；顺序漂移会让相同配置给出不同诊断。"""
    with pytest.raises(TypeError) as excinfo:
        SyndicaError(
            expected=None,  # pyright: ignore[reportArgumentType]
            received=0,  # pyright: ignore[reportArgumentType]
            next_action=[],  # pyright: ignore[reportArgumentType]
        )
    assert 'expected=None' in str(excinfo.value)
    assert 'received=0' not in str(excinfo.value)
    assert 'next_action=[]' not in str(excinfo.value)

    with pytest.raises(TypeError) as excinfo:
        SyndicaError(
            expected='valid',
            received=0,  # pyright: ignore[reportArgumentType]
            next_action=[],  # pyright: ignore[reportArgumentType]
        )
    assert 'received=0' in str(excinfo.value)
    assert 'next_action=[]' not in str(excinfo.value)


@pytest.mark.parametrize('error_cls', KERNEL_ERROR_CLASSES, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize('roundtrip', ROUNDTRIPS, ids=ROUNDTRIP_IDS)
def test_roundtrip_preserves_the_class_and_all_three_parts(roundtrip, error_cls):
    """异常会跨进程边界传递（Store 实现、子进程执行器），copy / pickle 必须无损。"""
    err = _probe(error_cls)
    clone = roundtrip(err)
    assert type(clone) is error_cls
    assert clone.expected == err.expected
    assert clone.received == err.received
    assert clone.next_action == err.next_action
    assert str(clone) == str(err)


@pytest.mark.parametrize('roundtrip', ROUNDTRIPS, ids=ROUNDTRIP_IDS)
def test_roundtrip_accepts_a_subclass_that_overrides_reduce_for_its_own_argument(roundtrip):
    clone = roundtrip(_probe(_KeyedError))
    assert type(clone) is _KeyedError
    assert clone.key == 'probe'
    assert clone.next_action == PARTS['next_action']


@pytest.mark.parametrize('roundtrip', ROUNDTRIPS, ids=ROUNDTRIP_IDS)
def test_roundtrip_still_catches_a_subclass_that_leaves_reduce_alone(roundtrip):
    """放宽构造方式不等于放宽守卫：额外必填参数没被 `__reduce__` 带上，重建就缺参数。"""
    with pytest.raises(TypeError):
        roundtrip(_probe(_UnreducedError))


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
