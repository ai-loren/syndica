"""切点分发。

两类切点的失败语义相反。**可拦截 hook 抛出的异常向上传播**：它是策略——权限检查、预算守卫，
越过一个失败的策略检查等于放行未经检查的操作。**可观察 hook 抛出的异常只记 ERROR 日志**：
可观测性不得拖垮主流程。

`HookCallback` 对所有切点是同一个类型，但**可观察切点上返回的 `HookResult` 被整个丢弃**：
在 turn_start 上返回 `modified={...}` 既不报错也不生效。要改参数只能挂在可拦截切点上。

本中心同时收内核枚举切点与 `<owner>:<event>` 形式的扩展切点（spec §5.4），两者共用**同一条**
分发路径：分类一旦定下，「串行 / 并行」「异常上抛 / 吞掉」就与切点是哪一种无关。

**槽位语义的边界**：本中心只强制那条与切点无关的规则（`result` 不带 `skip` 谁都读不到）；
「本切点上哪个槽位表示我处理了」由**触发方**在自己的触发点强制——内核切点由 `agent` /
`tool_executor` 各自校验，扩展切点由声明它的能力自己校验，因为只有触发方知道自己读哪些槽位。

**分层装配**：`HooksCenter(outer=...)` 把两层串成**一条**链，本层在前。spec §5.3 要求 Agent 局部
钩子先于 Runtime 级插件执行，而「先于」在可拦截链上是有实质后果的——局部钩子的 `modified` 会成为
Runtime 级钩子看到的入参，局部钩子的 `skip` 会连 Runtime 级的一起终止。合成一条链而不是跑两遍：
跑两遍就得在外面再实现一遍合并（`modified` 累积、`history` 累积、`skip` 终止），而那份实现迟早与
单层的分叉。**外层不知道内层的存在**：从 Runtime 的中心触发只跑 Runtime 级的 hook，这样一个插件
不会因为某个 Agent 挂了局部钩子而行为改变。

`outer` 只在构造时给出、此后不可改，因此装配链恒为一条有限的链：环要求先构造出的那个实例事后指向
后构造的那个，而没有任何公开写入面做得到这件事。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, cast

from syndica.kernel.context import (
    Context,
    _frozen,  # pyright: ignore[reportPrivateUsage]
)
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.hooks.installing import Installer, current_installer
from syndica.kernel.hooks.junctions import (
    INTERCEPTABLE,
    MODULE_NAME,
    OBSERVABLE,
    RUNTIME_ONLY,
    ExtensionJunction,
    Junction,
    JunctionKind,
    JunctionRef,
    validated_extension_name,
    validated_kind,
)
from syndica.kernel.hooks.result import HookResult

__all__ = ['HookCallback', 'HooksCenter']

logger = logging.getLogger(__name__)

HookCallback = Callable[..., Awaitable[HookResult | None]]


@dataclass(frozen=True)
class Authorship:
    """一次可拦截分发里，写下 skip 与每个键的改写的那些 hook 是谁装的（`register` 时的装配期身份；
    None 是装配期之外的代码）。内核据此判一段结果出自谁：结论本身不带作者，而被收紧的 Module 能借
    一次 skip 或改写替任何工具交出一段带着缺省 `TRUSTED` 的文本。"""

    skipped_by: Installer | None = None
    wrote: Mapping[str, tuple[Installer | None, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )


_Authored = tuple[HookCallback, Installer | None]
_Snapshot = tuple[
    dict[JunctionRef, list[HookCallback]],
    dict[str, tuple[ExtensionJunction, str]],
    dict[JunctionRef, list[Installer | None]],
]


def _authorship(
    wrote: Mapping[str, list[Installer | None]], skipped_by: Installer | None
) -> Authorship:
    return Authorship(
        skipped_by=skipped_by,
        wrote=MappingProxyType({key: tuple(authors) for key, authors in wrote.items()}),
    )


def _label(junction: JunctionRef) -> str:
    return junction.value if isinstance(junction, Junction) else junction


def _window(
    junction: JunctionRef, kwargs: dict[str, Any]
) -> contextlib.AbstractContextManager[None]:
    """分发期间冻结 `ctx` 参数那一份轨迹，可拦截与可观察都一样（见 `junctions` 模块文档）；不带
    `ctx` 的切点不冻结任何轨迹。"""
    ctx = kwargs.get('ctx')
    if isinstance(ctx, Context):
        return _frozen(ctx, f'切点 {_label(junction)} 分发')
    return contextlib.nullcontext()


def _orphan_result(
    junction: JunctionRef, cb: HookCallback, outcome: HookResult
) -> ContractViolation:
    """`result` 不带 `skip=True`：这个值哪个切点都读不到。

    合并结论只在 `skip` 那一支带出 `result`，因此不报出来就是静默丢弃——作者以为自己顶上了本次操作的
    产出，实际上原操作照常执行。要改产出用 `modified`，要顶替产出用 `skip=True` + `result`。
    """
    return ContractViolation(
        expected=f'{_label(junction)} 上的 HookResult.result 只在 skip=True 时给出',
        received=(
            f'{getattr(cb, "__qualname__", cb)!r} 返回了 skip=False 而 result='
            f'{type(outcome.result).__name__} 类型的 {outcome.result!r}'
        ),
        next_action=(
            '要顶替原操作就同时置 skip=True；只想改写参数或产出就把值放进 HookResult.modified；'
            'result 不带 skip 时没有任何切点会读到它'
        ),
    )


def _carries_intent(outcome: object) -> bool:
    """`_trigger_serial` 会从这个返回值里读出东西吗。

    **判据的派生源是 `_trigger_serial` 的读取集，不是 `HookResult` 的字段清单。** 加第五个槽位不会
    自动让它被报出来——让它被报出来的是 `_trigger_serial` 开始读它。反过来，`history` 是字段而
    `_trigger_serial` 完全忽略 hook 自填的那一份（它按 `modified` 自己累积），所以报它就是一次误报。

    因此判据不是「返回了非 None」：`HookCallback` 对两类切点是同一个类型，`return HookResult()` 是
    类型正确的写法，而空结论经可拦截链什么也不做。不是 `HookResult` 的返回值一律报：
    `_trigger_serial` 读它的第一个槽位就抛。
    """
    if outcome is None:
        return False
    if not isinstance(outcome, HookResult):
        return True
    return outcome.skip or bool(outcome.modified) or outcome.result is not None


def _discarded(outcome: object) -> str:
    """被丢弃的那个返回值里，作者本来指望起作用的每一样。

    逐槽位报而不是一个 repr：读者要知道自己写的是哪一种意图（否决 / 改写 / 顶替产出），才知道该
    把这个 hook 挪到哪个可拦截切点上。
    """
    if isinstance(outcome, HookResult):
        return (
            f'HookResult(skip={outcome.skip}, modified={sorted(outcome.modified)}, '
            f'result={outcome.result!r})'
        )
    return f'{type(outcome).__name__} 类型的 {outcome!r}'


def _report_discarded_return(junction: JunctionRef, cb: HookCallback, outcome: object) -> None:
    """可观察切点上一个非 None 的返回值被丢弃时，唯一的痕迹。

    `HookCallback` 对两类切点是同一个类型，所以 typing 看不见这件事；而 `register` 时还不知道回调
    会返回什么，只能在触发时说。可观察切点的异常本来就被吞掉，抛出去也传不出这条链。

    `tool_confirmation_required` 是这条的第一个真实受害者：它报告「这次调用等人确认」，挂上去的人
    最自然的写法就是返回一个批准，而那个批准此前被整个丢弃且零提示。

    ERROR 而非 INFO：root logger 默认停在 WARNING，INFO 的记录根本不会被创建，而这次丢弃除了这条
    记录不留任何痕迹。
    """
    logger.error(
        '期望: 挂在可观察切点 %s 上的 hook 只做观察，返回 None。'
        ' 实际: %s 交回了 %s，本切点并行执行、返回值整个丢弃——'
        'skip 没有终止任何 hook，modified 没有进任何人的 kwargs，result 没有顶替任何产出。'
        ' 下一步: 要影响这次操作就把这个 hook 挂到可拦截切点上'
        '（pre_tool_use / post_tool_use / pre_llm_request / post_llm_response /'
        ' context_overflow / llm_failure）；只做观察就让它返回 None。',
        _label(junction),
        getattr(cb, '__qualname__', cb),
        _discarded(outcome),
    )


def _not_a_junction(junction: object) -> ConfigError:
    return ConfigError(
        expected="junction 是 Junction 枚举成员，或 '<owner>:<event>' 形式的扩展切点名",
        received=f'收到 {type(junction).__name__} 类型的 {junction!r}',
        next_action=(
            '内核切点传 Junction.<NAME>；第三方能力传带命名空间的字符串，'
            '并先经 declare_extension_junction 声明分类'
        ),
    )


def _runtime_only_here(junction: Junction) -> ContractViolation:
    return ContractViolation(
        expected=(
            f'挂在 Agent 这一层（局部层或 Module 层）的切点由这个 Agent 触发；{junction.value} '
            '不是其中之一'
        ),
        received=f'收到把 {junction.value} 挂到服务单个 Agent 的 hook 中心上的请求',
        next_action=(
            f'改挂到 Runtime 的中心：Runtime 插件的 register 里挂，或在 Module 的 on_start 里 '
            f'runtime.hooks.register(Junction.{junction.name}, cb)。{junction.value} 只从那里'
            '触发，而外层的 trigger 看不到 Agent 这一层的 hook——挂在这里的回调一次都不会跑'
        ),
    )


def _declared_twice(name: str, declarer: str) -> ContractViolation:
    return ContractViolation(
        expected=f'扩展切点 {name} 只声明一次：句柄只能有一个持有者',
        received=f'{name} 已由{declarer}声明',
        next_action='换一个 owner 段或 event 段；要挂 hook 就直接按名字 register，不必再声明',
    )


def _qualname(cb: object) -> str:
    return repr(getattr(cb, '__qualname__', cb))


def _not_callable(cb: object) -> ConfigError:
    return ConfigError(
        expected='cb 是可调用对象：async def 函数，或带 async __call__ 的对象',
        received=f'收到 {type(cb).__name__} 类型的 {cb!r}',
        next_action=(
            '传函数本身而不是它的调用结果：hooks.register(junction, on_event)，不是 on_event()'
        ),
    )


def _not_awaitable(junction: JunctionRef, cb: object, returned: object) -> ContractViolation:
    return ContractViolation(
        expected=f'{_label(junction)} 上的 hook 返回可等待对象（async def）',
        received=f'{_qualname(cb)} 同步返回了 {type(returned).__name__} 类型的 {returned!r}',
        next_action='把它改成 async def；切点按 await 收结论，同步函数的返回值没有人等得到',
    )


def _not_a_result(junction: JunctionRef, cb: object, outcome: object) -> ContractViolation:
    return ContractViolation(
        expected=f'可拦截切点 {_label(junction)} 上的 hook 返回 HookResult 或 None',
        received=f'{_qualname(cb)} 返回了 {type(outcome).__name__} 类型的 {outcome!r}',
        next_action=(
            '要改参数就 return HookResult(modified={...})，要顶替这次操作就 '
            'HookResult(skip=True, result=...)，什么都不改就 return None'
        ),
    )


def _kernel_junction_triggered(junction: Junction) -> ContractViolation:
    return ContractViolation(
        expected=(
            '挂在 Runtime 上的 hook 中心，公开的 trigger 只触发扩展切点；内核切点只由内核在固定位置'
            '触发'
        ),
        received=f'收到 Junction.{junction.name}，而这条装配链上有一层属于 Runtime',
        next_action=(
            f'要在 {junction.value} 发生时做事就 register 一个 hook；伪造一次内核切点会让挂在上面的'
            '策略 hook 对一件没发生的事下结论'
        ),
    )


def _not_a_trigger_target(junction: object) -> ConfigError:
    return ConfigError(
        expected='trigger 收声明时交回的 ExtensionJunction（独立的中心上也收内核的 Junction）',
        received=f'收到 {type(junction).__name__} 类型的 {junction!r}',
        next_action=(
            '传 declare_extension_junction 交回的句柄，或 Module 清单 junctions 里的那个对象'
        ),
    )


def _string_trigger(name: str) -> ContractViolation:
    return ContractViolation(
        expected=f'扩展切点 {name} 用声明时交回的那个 ExtensionJunction 触发',
        received=f'收到字符串 {name!r}',
        next_action=(
            '字符串谁都拼得出来：用 declare_extension_junction 的返回值，'
            '或 Module 清单 junctions 里的那个对象触发'
        ),
    )


def _foreign_handle(handle: ExtensionJunction, declarer: str) -> ContractViolation:
    return ContractViolation(
        expected=f'触发 {handle.name} 用的就是声明时交回的那个句柄',
        received=f'{handle.name} 已由{declarer}声明，收到的是另一个同名的 ExtensionJunction',
        next_action='拿声明者交出的那个对象触发；自己构造一个同名的句柄不算数，句柄按对象身份比较',
    )


def _reserved_owner(name: str, owner: str) -> ContractViolation:
    return ContractViolation(
        expected=f'{owner!r} 是已装 Module 的名字，它名下的切点只由它的清单声明',
        received=f'收到声明 {name!r} 的请求',
        next_action=(
            f'换一个 owner 段；{owner!r} 名下的切点写在 '
            f'RuntimeModule({owner!r}, junctions=...) 里，在任何第三方代码跑之前由 Runtime 声明'
        ),
    )


def _module_declares(name: str, module: str) -> ContractViolation:
    return ContractViolation(
        expected=f'Module {module!r} 的扩展切点写在它的清单 junctions 里',
        received=f'Module {module!r} 的代码里调了 declare_extension_junction({name!r})',
        next_action=(
            f'把 ExtensionJunction({name!r}, <kind>) 放进 '
            f'RuntimeModule({module!r}, junctions=...)，工具与插件拿同一个对象触发'
        ),
    )


def _owner_mismatch(name: str, owner: str, plugin: str) -> ContractViolation:
    return ContractViolation(
        expected=f'插件 {plugin!r} 在 register 里声明的切点，owner 段是 {plugin!r}',
        received=f'它声明了 {name!r}，owner 段是 {owner!r}',
        next_action=f'改名为 {plugin}:<event>：owner 段说明这个切点归谁，只能是声明它的那个插件',
    )


def _plugin_name_not_canonical(plugin: str) -> ContractViolation:
    return ContractViolation(
        expected=(
            '在 register 里声明扩展切点的插件，名字是 Module 名的规范拼法'
            '（小写字母开头、单个连字符分段）'
        ),
        received=f'插件名是 {plugin!r}',
        next_action='给插件改名为 audit、audit-log 这样的写法：它的名字就是这些切点的 owner 段',
    )


def _not_a_center(outer: object) -> ConfigError:
    return ConfigError(
        expected='outer 是外层装配的那个 HooksCenter，或 None（本层就是最外层）',
        received=f'收到 {type(outer).__name__} 类型的 {outer!r}',
        next_action=(
            '把宿主交来的那个 HooksCenter 原样传进来（Agent 侧就是构造参数里的 hooks）；'
            '不接外层就省略 outer——传别的东西会让第一次 trigger 在沿链取 hook 时抛一条'
            '指着本类内部的 AttributeError'
        ),
    )


def _validated_outer(outer: object) -> HooksCenter | None:
    """校验外层是一个 HooksCenter。

    参数标注为 object 而非 HooksCenter | None：要挡的就是违反标注的运行时取值。收窄了下面的
    isinstance 会变成 pyright 眼里的死代码，而这道检查存在的理由正是静态检查不在场的那些调用。
    """
    if outer is not None and not isinstance(outer, HooksCenter):
        raise _not_a_center(outer)
    return outer


class HooksCenter:
    """切点分发中心。

    扩展切点必须先 `declare_extension_junction` 才能 `register` / `trigger`：分类没有默认值。
    因此**声明者必须先加载**——能力 A 声明 `artifact:created`、能力 B 往上挂 hook 时，B 晚于 A。
    反过来会在 B 的 `register` 上响亮失败，而这是对的：往一个没人声明过的切点上挂 hook，要么名字
    拼错了，要么缺了依赖，两者都该当场报出来而不是留一个永不触发的 hook。

    **谁能声明**在装配时判，**谁能触发**靠持有声明时交回的 `ExtensionJunction`：挂 hook 可以按名字
    （监听不是冒用），触发必须交回那个对象。这是同进程里的约束，挡的是误用与抢名，不是恶意代码——
    同进程的代码能 import 别人的句柄、改任何东西（spec §12.4）。
    """

    def __init__(self, *, outer: HooksCenter | None = None) -> None:
        # 构造时就校验：错配的症状是第一次 trigger 沿链取 hook 时抛一条指着本类内部的
        # AttributeError，而那时调用栈里已经没有写下这个参数的那一行。
        self._hooks: dict[JunctionRef, list[HookCallback]] = {junction: [] for junction in Junction}
        # 与 `_hooks` 逐项对齐：每个回调注册时的装配期身份。
        self._authors: dict[JunctionRef, list[Installer | None]] = {
            junction: [] for junction in Junction
        }
        # 扩展切点名 → (句柄, 声明者)。分类在句柄上：一份状态，还原时不会只还原一半。
        self._declared: dict[str, tuple[ExtensionJunction, str]] = {}
        self._outer = _validated_outer(outer)
        self._one_agent = False
        self._runtime_owned = False
        self._reserved_owners: frozenset[str] = frozenset()

    def declare_extension_junction(self, name: str, kind: JunctionKind) -> ExtensionJunction:
        """声明一个扩展切点及其分类，交回触发它要用的句柄。

        分类由**声明者**给出而非由内核推断，也没有默认值：可观察意味着「并行 + 丢弃返回值 +
        吞异常」，让一个策略型 hook（权限、预算）靠遗漏落进去，等于放行未经检查的操作。

        同一个名字沿装配链只能声明一次，不论分类：句柄只能有一个持有者。已装 Module 的名字是保留
        的 owner，它名下的切点只由清单声明。在插件的 `register` 里声明时，owner 段必须等于插件名；
        Module 的代码（服务工厂、`on_start`、工具与插件工厂、Module 插件的 `register`）不在这里
        声明，切点写进清单。

        **冲突只沿「向外」的方向查得到**：外层没有指向内层的引用，一个后声明的外层可以与内层已声明的
        名字重名。
        """
        validated = validated_extension_name(name)
        checked = validated_kind(kind)
        owner = validated.partition(':')[0]
        installer = current_installer()
        if installer is not None and installer.kind == 'module':
            raise _module_declares(validated, installer.name)
        if installer is not None:
            if MODULE_NAME.fullmatch(installer.name) is None:
                raise _plugin_name_not_canonical(installer.name)
            if owner != installer.name:
                raise _owner_mismatch(validated, owner, installer.name)
        if self._is_reserved(owner):
            raise _reserved_owner(validated, owner)
        declarer = self._declarer_of(validated)
        if declarer is not None:
            raise _declared_twice(validated, declarer)
        handle = ExtensionJunction(validated, checked)
        self._declared[validated] = (
            handle,
            '装配期之外的代码' if installer is None else installer.describe(),
        )
        return handle

    def extension_junctions(self) -> Mapping[str, JunctionKind]:
        """整条装配链上已声明的扩展切点 → 分类，按名字排序的只读映射。"""
        return MappingProxyType(
            {
                name: kind
                for name in self._declared_names()
                if (kind := self._declared_kind(name)) is not None
            }
        )

    def register(self, junction: JunctionRef | ExtensionJunction, cb: HookCallback) -> None:
        """挂一个 hook。同一切点上按注册先后执行。扩展切点可以按名字挂，也可以传句柄。

        挂到**可观察**切点上时，`cb` 返回的 `HookResult` 会被整个丢弃（见 `trigger`），且每一次
        丢弃都记一条 ERROR。这一点 typing 拦不住（`HookCallback` 对两类切点是同一个类型），因此挂
        之前先确认这个切点在 `INTERCEPTABLE` 里——要改 kwargs 或顶替产出只有那一侧读得到。

        挂在本层的 hook**先于**外层的执行，且只有本层的 trigger 看得到它们。
        """
        if not callable(cb):
            raise _not_callable(cb)
        resolved = self._resolved(
            junction.name if isinstance(junction, ExtensionJunction) else junction
        )
        if self._one_agent and resolved in RUNTIME_ONLY:
            raise _runtime_only_here(resolved)
        # setdefault 而非下标：外层声明的扩展切点在本层的表里没有键，而它是合法的注册目标。
        self._hooks.setdefault(resolved, []).append(cb)
        self._authors.setdefault(resolved, []).append(current_installer())

    async def trigger(self, junction: ExtensionJunction | Junction, **kwargs: Any) -> HookResult:
        """触发一个扩展切点，返回可拦截链的合并结论（spec §5.4）。

        只收声明时交回的那个句柄：字符串谁都拼得出来。从 Runtime 的中心触发只跑 Runtime 级的
        hook（外层看不见内层）；要让一个 Agent 的局部层与 Module 层也跑，用
        `BaseAgent.trigger_extension`。

        内核切点只由内核在固定位置触发（经私有的 `_dispatch`，与这里是同一条分发路径）：装配链上
        只要有一层属于 Runtime，这里就拒绝内核切点——伪造一次会让挂在上面的策略 hook 对一件没发生的事
        下结论。整条链都不属于任何 Runtime 的独立中心照收，第三方靠它给自己的插件写单测。语义见
        `_dispatch`。
        """
        if isinstance(junction, Junction):
            if self._serves_a_runtime():
                raise _kernel_junction_triggered(junction)
            return await self._dispatch(junction, **kwargs)
        if isinstance(cast(object, junction), str):
            raise _string_trigger(validated_extension_name(junction))
        if not isinstance(cast(object, junction), ExtensionJunction):
            raise _not_a_trigger_target(junction)
        resolved = self._resolved(junction.name)
        declared = self._declared_handle(junction.name)
        if declared is not junction:
            raise _foreign_handle(junction, self._declarer_of(junction.name) or '别处')
        return await self._dispatch(resolved, **kwargs)

    async def _dispatch(self, junction: JunctionRef, **kwargs: Any) -> HookResult:
        """分发一次，只要结论。语义见 `_dispatch_attributed`。"""
        result, _ = await self._dispatch_attributed(junction, **kwargs)
        return result

    async def _dispatch_attributed(
        self, junction: JunctionRef, **kwargs: Any
    ) -> tuple[HookResult, Authorship]:
        """唯一的分发路径，另交回结论出自谁（`Authorship`）。内核切点由内核在固定位置经这里触发，
        扩展切点经公开的 `trigger`。

        可拦截切点按注册顺序串行：前一个 hook 的 `modified` 并入后续 hook 的 kwargs，返回的
        `modified` 只含被改过的键而非整份 kwargs；`skip=True` 终止后续 hook 并带出它的
        `result`，不带 `skip` 的 `result` 报 `ContractViolation`（见 `_orphan_result`）。

        `modified` 是 last-wins 的结论，`history` 另外交出每个键在链上被赋过的**全部**值：只能单
        向收紧的量（trust）看结论会漏掉链中间那次收紧，而那正是不可信标记该起作用的时候。

        可观察切点并行执行，返回值与异常一律丢弃，因此恒返回空 `HookResult`。

        两类切点都在触发时对 hook 列表取快照：hook 里注册的 hook 留到下次触发才生效。两条
        分发路径必须在这一点上一致，否则同一段代码的行为取决于它挂在哪类切点上。

        本层与外层的 hook 合成**一条**链，本层在前（见模块 docstring）。
        """
        resolved = self._resolved(junction)
        # 分类在空列表检查之前取：未归类的切点无论有没有 hook 都必须响亮失败。
        kind = self._kind(resolved)
        chain = self._collect_authored(resolved)
        if not chain:
            return HookResult(), Authorship()
        if kind is JunctionKind.INTERCEPTABLE:
            with _window(resolved, kwargs):
                return await self._trigger_serial(resolved, chain, **kwargs)
        with _window(resolved, kwargs):
            await self._trigger_parallel(resolved, [cb for cb, _ in chain], **kwargs)
        return HookResult(), Authorship()

    def _serve_one_agent(self) -> None:
        """本中心服务单个 Agent（局部层或 Module 层）：`RUNTIME_ONLY` 的切点从此挂不上来。"""
        self._one_agent = True

    def _serve_runtime(self) -> None:
        """本中心属于一个 Runtime（它自己的中心，或 spawn 给一个 Agent 建的 Module 层）：公开的
        `trigger` 从此拒绝内核切点，链上更内层的中心也一样。"""
        self._runtime_owned = True

    def _serves_a_runtime(self) -> bool:
        if self._runtime_owned:
            return True
        return self._outer._serves_a_runtime() if self._outer is not None else False

    def _declare_handle(self, handle: ExtensionJunction) -> None:
        """按对象声明一个扩展切点：Module 清单里的切点在任何第三方代码跑之前由 Runtime 声明。"""
        declarer = self._declarer_of(handle.name)
        if declarer is not None:
            raise _declared_twice(handle.name, declarer)
        self._declared[handle.name] = (handle, f'Module {handle.owner!r} 的清单')

    def _reserve_owners(self, owners: frozenset[str]) -> None:
        """已装 Module 的名字：它们名下的切点只由清单声明。Runtime 在任何第三方代码跑之前设一次。"""
        self._reserved_owners = owners

    def _is_reserved(self, owner: str) -> bool:
        if owner in self._reserved_owners:
            return True
        return self._outer._is_reserved(owner) if self._outer is not None else False

    def _declaration(self, name: str) -> tuple[ExtensionJunction, str] | None:
        own = self._declared.get(name)
        if own is not None:
            return own
        return self._outer._declaration(name) if self._outer is not None else None

    def _declared_handle(self, name: str) -> ExtensionJunction | None:
        declaration = self._declaration(name)
        return None if declaration is None else declaration[0]

    def _declarer_of(self, name: str) -> str | None:
        declaration = self._declaration(name)
        return None if declaration is None else declaration[1]

    def _assembly_snapshot(self) -> _Snapshot:
        """本中心此刻的全部可写状态，供一次失败的装配还原（见 `plugins.install_plugins`）。
        构造期一次设定的配置（服务单个 Agent、保留的 owner）不在其列：它们早于任何装配。

        槽位列表逐个浅拷：还原要换掉列表**内容**，只留住 dict 的话插件 append 上去的回调会活下来。

        新增任何可写状态都要一并进这里，否则那次还原就是一句假承诺。它刻意留在本类里而不是由调用方
        伸手取两个私有字段——那样加状态的人改不到还原的那一处。
        """
        return (
            {junction: list(cbs) for junction, cbs in self._hooks.items()},
            dict(self._declared),
            {junction: list(installers) for junction, installers in self._authors.items()},
        )

    def _assembly_restore(self, snapshot: _Snapshot) -> None:
        """还原一份快照。**本中心就此接管它**，同一份快照不得还原两遍。"""
        self._hooks, self._declared, self._authors = snapshot

    def _collect(self, junction: JunctionRef) -> list[HookCallback]:
        """这个切点上要跑的整条链：本层在前，外层在后。"""
        return [cb for cb, _ in self._collect_authored(junction)]

    def _collect_authored(self, junction: JunctionRef) -> list[_Authored]:
        own = list(zip(self._hooks.get(junction, []), self._authors.get(junction, []), strict=True))
        if self._outer is None:
            return own
        return own + self._outer._collect_authored(junction)

    def _declared_kind(self, name: str) -> JunctionKind | None:
        """沿装配链取这个扩展切点的分类；没人声明过交回 None。

        本层优先只是查找顺序，不是覆盖。**冲突只沿「向外」的方向查得到**：`declare_extension_junction`
        问的是本层与外层，而外层没有指向内层的引用，因此一个**后声明的外层**可以把内层已经声明过的
        名字分叉成另一种分类。危险的那个方向被挡住了（内层想把外层的可拦截切点重声明成可观察会被拒，
        策略 hook 因此落不进吞异常那一侧）；开着的是反方向，症状是同一个回调在两层的 `trigger` 下按
        两套语义跑。
        """
        handle = self._declared_handle(name)
        return None if handle is None else handle.kind

    def _declared_names(self) -> list[str]:
        """整条链上已声明的扩展切点名。它只进错误消息：读者要看的是「我能挂哪些」。"""
        names = set(self._declared)
        if self._outer is not None:
            names.update(self._outer._declared_names())
        return sorted(names)

    def _resolved(self, junction: object) -> JunctionRef:
        """把入参收敛成 `self._hooks` 的键，不合法则响亮失败。

        参数标注为 object 而非 JunctionRef：要挡的就是违反标注的运行时取值（配置或插件清单里读来
        的值）。收窄了下面的 isinstance 会变成 pyright 眼里的死代码。

        名字畸形（`ConfigError`：这个值作为声明就不合法）与没声明过（`ContractViolation`：值本身
        没问题，错在此刻还不该这么调）分属两个异常类。
        """
        if isinstance(junction, Junction):
            return junction
        if not isinstance(junction, str):
            raise _not_a_junction(junction)
        validated = validated_extension_name(junction)
        if self._declared_kind(validated) is None:
            raise ContractViolation(
                expected=f'扩展切点 {validated} 在本层或外层已经声明过',
                received=(
                    f'沿装配链找不到 {validated}；已声明的扩展切点: {self._declared_names()}'
                ),
                next_action=(
                    '先由声明它的那个能力调 declare_extension_junction(name, kind) 给出分类，'
                    '再 register / trigger；确认该能力已加载，且名字逐字一致（不折叠大小写）'
                ),
            )
        return validated

    def _kind(self, junction: JunctionRef) -> JunctionKind:
        """切点按哪套语义分发。

        内核切点查 `INTERCEPTABLE` / `OBSERVABLE` 两个集合，**两边都不在就报错**，不落进任一默认
        分支：那两个集合是划分，写成「不在可拦截即可观察」会让新增枚举成员靠遗漏拿到吞异常语义。
        """
        if isinstance(junction, Junction):
            if junction in INTERCEPTABLE:
                return JunctionKind.INTERCEPTABLE
            if junction in OBSERVABLE:
                return JunctionKind.OBSERVABLE
            raise ContractViolation(
                expected=f'内核切点 {junction.value} 已归入 INTERCEPTABLE 或 OBSERVABLE 之一',
                received=f'收到两个集合都不含的 Junction.{junction.name}',
                next_action=(
                    'junctions.py 里把它放进 INTERCEPTABLE（串行、可改 kwargs、异常上抛）或 '
                    'OBSERVABLE（并行、丢弃返回值、吞异常）；这里不给默认值'
                ),
            )
        declared = self._declared_kind(junction)
        if declared is None:
            # `trigger` 到不了这里（`_resolved` 已经拒过未声明的名字）。这道检查守的是 `_resolved`
            # 与 `_kind` 被分开调用的那条路：没有它，未归类的切点交回 None，`is
            # JunctionKind.INTERCEPTABLE` 为假，于是静默落进「并行 + 吞异常」那一侧。
            raise ContractViolation(
                expected=f'扩展切点 {junction} 在本层或外层已经声明过',
                received='沿装配链找不到它的分类',
                next_action='先由声明它的那个能力调 declare_extension_junction(name, kind)',
            )
        return declared

    async def _trigger_serial(
        self,
        junction: JunctionRef,
        chain: list[_Authored],
        **kwargs: Any,
    ) -> tuple[HookResult, Authorship]:
        merged: dict[str, Any] = {}
        history: dict[str, list[Any]] = {}
        wrote: dict[str, list[Installer | None]] = {}
        current = dict(kwargs)
        for cb, installer in chain:
            returned = cast(object, cb(**current))
            if not inspect.isawaitable(returned):
                raise _not_awaitable(junction, cb, returned)
            outcome = cast(object, await returned)
            if outcome is None:
                continue
            if not isinstance(outcome, HookResult):
                raise _not_a_result(junction, cb, outcome)
            if outcome.result is not None and not outcome.skip:
                raise _orphan_result(junction, cb, outcome)
            merged.update(outcome.modified)
            for key, value in outcome.modified.items():
                history.setdefault(key, []).append(value)
                wrote.setdefault(key, []).append(installer)
            current.update(outcome.modified)
            if outcome.skip:
                return (
                    HookResult(skip=True, modified=merged, result=outcome.result, history=history),
                    _authorship(wrote, installer),
                )
        return HookResult(modified=merged, history=history), _authorship(wrote, None)

    async def _trigger_parallel(
        self, junction: JunctionRef, hooks: list[HookCallback], **kwargs: Any
    ) -> None:
        started: list[HookCallback] = []
        awaitables: list[Awaitable[object]] = []
        for cb in hooks:
            try:
                returned = cast(object, cb(**kwargs))
            except Exception:
                logger.error('observable hook failed at %s', _label(junction), exc_info=True)
                continue
            if not inspect.isawaitable(returned):
                logger.error(
                    '期望: 挂在可观察切点 %s 上的 hook 返回可等待对象（async def）。'
                    ' 实际: %s 同步返回了 %s 类型的 %r，这个 hook 什么都没做成。'
                    ' 下一步: 把它改成 async def。',
                    _label(junction),
                    _qualname(cb),
                    type(returned).__name__,
                    returned,
                )
                continue
            started.append(cb)
            awaitables.append(returned)
        outcomes = await asyncio.gather(*awaitables, return_exceptions=True)
        for cb, outcome in zip(started, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                logger.error('observable hook failed at %s', _label(junction), exc_info=outcome)
            elif _carries_intent(outcome):
                _report_discarded_return(junction, cb, outcome)
