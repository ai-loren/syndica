"""交给 hook 的可变 kwarg 的只读视图。

**改 kwargs 的唯一写法是返回 `HookResult(modified={...})`。** 就地改一份传进来的 dict 也能改变
后续行为，但它**不进 `HookResult.history`**，于是触发方认不出「这份值的作者是 hook」——而
`tool_executor` 的那道强制升级正是按这个判据决定要不要重算溯源（spec §12.2 第 3 条）。这类写法
因此不是风格问题：它让宿主配置的那道闸静默失效。

检测这件事在触发方那一侧做不干净（要拿改写前的快照逐值比对，即在每次工具调用的必经之路上对模型
与 hook 控制的值调 `__eq__`，一个惰性或恶意的 `__eq__` 就能把整批打成错误），所以这里把它变成
**结构上不可能**：写入面当场抛 `ContractViolation`。裸 `TypeError`（`mappingproxy` 的默认行为）
不满足本仓的错误三件套，读者拿不到「那该怎么写」。

**只护住顶层。** `args['config']['x'] = 1` 仍然逃得掉——嵌套容器不是本视图持有的东西，逐层包起来
要深拷或代理整棵树，而那要在每次工具调用上做。这是一个**残留缺口**，不是已关闭的洞。

**凡是「另给我一份」的协议一律交回普通 `dict`**：`copy` / `deepcopy` / `pickle` / `fromkeys`，与
`dict(args)` / `{**args}` / `.copy()` / `|` 已有的行为一致。它们**是读**，不是写：
`copy._reconstruct` 往新建的那一份里逐键 `y[key] = value`，只拦 `__setitem__` 的包装会把它们拒掉，
还报出一条说作者就地写了的假消息。`deepcopy` 尤其不能拒——顶层之外那道缺口，hook 作者唯一的自助
补法就是它。代价是往返之后类型退化成 `dict`，而本视图只活在一次触发里，没有任何判据读它的类型。

是 `dict` 的子类而不是另一种 Mapping：`isinstance(value, dict)` 是触发方与 hook 作者都会写的形状
检查，换成 Mapping 会让一条「改写后的 args 仍是 dict」的守卫在这里误报。
"""

from __future__ import annotations

from typing import Any, NoReturn

from syndica.kernel.errors import ContractViolation
from syndica.kernel.hooks.junctions import JunctionRef

__all__ = ['ReadOnlyKwarg']


def _label(junction: JunctionRef) -> str:
    return junction if isinstance(junction, str) else junction.value


class ReadOnlyKwarg(dict[str, Any]):
    """一份传给 hook 的 kwarg：读随便，写当场失败。"""

    # 错误消息点名的切点与 kwarg 不留写入面：没有 __dict__，一个 hook 因此改不掉这条诊断指向谁。
    __slots__ = ('_junction', '_key')

    def __init__(self, junction: JunctionRef, key: str, values: dict[str, Any]) -> None:
        super().__init__(values)
        self._junction = junction
        self._key = key

    def _refuse(self, attempt: str) -> NoReturn:
        raise ContractViolation(
            expected=(
                f'{_label(self._junction)} hook 改 {self._key!r} 的唯一写法是返回 '
                f'HookResult(modified={{{self._key!r}: ...}})'
            ),
            received=f'收到对 {self._key!r} 的{attempt}',
            next_action=(
                f'把改动写进一份新的 dict 再交出去：'
                f'return HookResult(modified={{{self._key!r}: {{**{self._key}, ...}}}})。'
                f'就地改不会进 HookResult.history，触发方于是认不出这份 {self._key!r} 的作者是 '
                'hook——那道「参数可溯源到不可信内容就强制升级」的闸会按一份已经失效的批快照放行'
            ),
        )

    def __setitem__(self, key: str, value: Any) -> NoReturn:
        self._refuse(f'就地赋值（{key!r}）')

    def __delitem__(self, key: str) -> NoReturn:
        self._refuse(f'就地删除（{key!r}）')

    def __ior__(self, other: Any) -> NoReturn:
        self._refuse('就地合并（|=）')

    def clear(self) -> NoReturn:
        self._refuse('clear()')

    def pop(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._refuse('pop()')

    def popitem(self) -> NoReturn:
        self._refuse('popitem()')

    def setdefault(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._refuse('setdefault()')

    def update(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._refuse('update()')

    def __reduce__(self) -> tuple[Any, ...]:
        """`copy` / `deepcopy` / `pickle` 三条路都从这里取重建方式，交回的是**普通 dict**。

        交回本类会让 pickle 撞成一条裸 `AttributeError`：它先灌键值再装状态，`__setitem__` 那道拒绝
        跑在 `_junction` 存在之前。而 `copy` / `deepcopy` 没有各自的钩子正是因为这一个就够——单独给
        它们写一份，没有任何输入能区分两种写法。
        """
        return (dict, (list(self.items()),))

    @classmethod
    def fromkeys(cls, iterable: Any, value: Any = None) -> dict[str, Any]:  # pyright: ignore[reportIncompatibleMethodOverride]
        # 不经本类的三参构造：继承来的实现会拿 cls() 建实例，报一条指着 junction / key 缺失的
        # TypeError，而调用方要的只是一份新映射。
        return dict.fromkeys(iterable, value)
