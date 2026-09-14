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
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.hooks.junctions import (
    INTERCEPTABLE,
    OBSERVABLE,
    Junction,
    JunctionKind,
    JunctionRef,
)
from syndica.kernel.hooks.result import HookResult

__all__ = ['HookCallback', 'HooksCenter']

logger = logging.getLogger(__name__)

HookCallback = Callable[..., Awaitable[HookResult | None]]

_NAMESPACE_SEPARATOR = ':'
_KERNEL_JUNCTIONS_BY_VALUE: dict[str, Junction] = {j.value: j for j in Junction}


def _label(junction: JunctionRef) -> str:
    return junction.value if isinstance(junction, Junction) else junction


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
    """这个返回值里有没有东西**因为落在可观察切点上而丢失**。

    判据不是「返回了非 None」：`HookCallback` 对两类切点是同一个类型，`return HookResult()` 因此
    是类型正确的写法，而一个空结论在可拦截切点上也什么都不做——报它就是让这条记录说一件没发生的
    丢失。不是 `HookResult` 的返回值一律报：那个值没有任何槽位可读，本中心读不出它想做什么。
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


def _not_an_extension_name(name: object) -> ConfigError:
    return ConfigError(
        expected="扩展切点名是 '<owner>:<event>' 形式的 str",
        received=f'收到 {type(name).__name__} 类型的 {name!r}',
        next_action=(
            "传字符串（如 'artifact:created'）；内核切点不经这里声明，它们的分类写死在 "
            'junctions.py 的 INTERCEPTABLE / OBSERVABLE 里'
        ),
    )


def _validated_kind(kind: object) -> JunctionKind:
    """校验分类是枚举成员。

    参数标注为 object 而非 JunctionKind：要挡的就是违反标注的运行时取值。等价字符串尤其要挡——
    `'observable'` 与两个成员都不相等，分发时 `is JunctionKind.INTERCEPTABLE` 为假，于是静默按
    可观察跑，而那正是「吞异常」那一侧。
    """
    if not isinstance(kind, JunctionKind):
        raise ConfigError(
            expected='kind 是 JunctionKind 成员',
            received=f'收到 {type(kind).__name__} 类型的 {kind!r}',
            next_action=(
                'JunctionKind.INTERCEPTABLE（串行、可改 kwargs、异常上抛）或 '
                'JunctionKind.OBSERVABLE（并行、丢弃返回值、吞异常）'
            ),
        )
    return kind


def _validated_extension_name(name: object) -> str:
    """校验扩展切点名是 `<owner>:<event>`。

    参数标注为 object 而非 str，理由同 `_validated_kind`。

    每一种畸形单独报，不交给一条正则：读者要知道自己错在哪一段。四类畸形一律**拒绝而非归一化**
    （不 strip、不折叠大小写、不切首个分隔符）——切点名计入公开 API，允许两种拼法映射到同一个
    切点，日后把 `owner` 绑到注册者身份时就没有唯一的规范拼法可比。放宽接受面日后不是破坏性
    变更，收紧则是，所以现在按最窄的收。
    """
    if not isinstance(name, str):
        raise _not_an_extension_name(name)
    kernel = _KERNEL_JUNCTIONS_BY_VALUE.get(name)
    if kernel is not None:
        raise ConfigError(
            expected='扩展切点名带命名空间前缀，不得等于某个内核切点的取值',
            received=f'收到 {name!r}，它就是 Junction.{kernel.name} 的取值',
            next_action=(
                f'内核切点请传 Junction.{kernel.name}。字符串与枚举通到同一个切点会让按枚举触发'
                '时读不到按字符串注册的 hook——那是静默空转，最难查的一种'
            ),
        )
    if any(ch.isspace() for ch in name):
        raise ConfigError(
            expected='扩展切点名不含任何空白字符',
            received=f'收到 {name!r}',
            next_action=(
                '去掉空白后重新声明。这里刻意不自动 strip：两种拼法在日志与错误消息里长得一样，'
                '却是两个互不相干的切点'
            ),
        )
    separators = name.count(_NAMESPACE_SEPARATOR)
    if separators != 1:
        raise ConfigError(
            expected=f'扩展切点名恰好含一个 {_NAMESPACE_SEPARATOR!r} 分隔符',
            received=f'收到 {name!r}，其中有 {separators} 个 {_NAMESPACE_SEPARATOR!r}',
            next_action=(
                "写成 '<owner>:<event>'（如 'artifact:created'）。多级命名空间现在一律拒绝，"
                '日后放宽不是破坏性变更，现在收下了却再也收不回来'
            ),
        )
    owner, _, event = name.partition(_NAMESPACE_SEPARATOR)
    if not owner or not event:
        raise ConfigError(
            expected='扩展切点名的 owner 段与 event 段都非空',
            received=f'收到 {name!r}，owner={owner!r} event={event!r}',
            next_action=(
                'owner 段写这个切点属于谁（日后按注册者标识校验它，两个扩展才抢不到同一个名字，'
                '也冒用不了对方的切点名发事件），event 段写发生了什么'
            ),
        )
    return name


class HooksCenter:
    """切点分发中心。

    扩展切点必须先 `declare_extension_junction` 才能 `register` / `trigger`：分类没有默认值。
    因此**声明者必须先加载**——能力 A 声明 `artifact:created`、能力 B 往上挂 hook 时，B 晚于 A。
    反过来会在 B 的 `register` 上响亮失败，而这是对的：往一个没人声明过的切点上挂 hook，要么名字
    拼错了，要么缺了依赖，两者都该当场报出来而不是留一个永不触发的 hook。
    """

    def __init__(self, *, outer: HooksCenter | None = None) -> None:
        # 构造时就校验：错配的症状是第一次 trigger 沿链取 hook 时抛一条指着本类内部的
        # AttributeError，而那时调用栈里已经没有写下这个参数的那一行。
        self._hooks: dict[JunctionRef, list[HookCallback]] = {junction: [] for junction in Junction}
        self._extension_kinds: dict[str, JunctionKind] = {}
        self._outer = _validated_outer(outer)

    def declare_extension_junction(self, name: str, kind: JunctionKind) -> None:
        """声明一个扩展切点及其分类，之后它才能被 register / trigger。

        分类由**声明者**给出而非由内核推断，也没有默认值：可观察意味着「并行 + 丢弃返回值 +
        吞异常」，让一个策略型 hook（权限、预算）靠遗漏落进去，等于放行未经检查的操作。

        同名同分类重复声明是成功（后置条件已成立）。同名不同分类报 `ContractViolation`：今天还
        没有注册者标识可比，分类冲突是唯一能观测到的抢名信号，静默让先声明者胜会让后来者的 hook
        按它没预期的那套语义跑。**冲突沿装配链查**：外层已经声明成另一种分类时同样报错，否则内层
        那次声明会让同一个名字在两层上有两套执行语义，而 hook 按哪套跑取决于它挂在哪一层。
        """
        validated = _validated_extension_name(name)
        checked = _validated_kind(kind)
        declared = self._declared_kind(validated)
        if declared is not None and declared is not checked:
            raise ContractViolation(
                expected=f'{validated} 的分类与它已声明的 {declared.value} 一致',
                received=f'收到把它重新声明为 {checked.value} 的请求',
                next_action=(
                    f'{validated} 已被声明为 {declared.value}；换一个 owner 段，'
                    '或与先声明它的那个能力对齐分类——同一个名字不能有两套执行语义'
                ),
            )
        self._extension_kinds[validated] = checked
        self._hooks.setdefault(validated, [])

    def register(self, junction: JunctionRef, cb: HookCallback) -> None:
        """挂一个 hook。同一切点上按注册先后执行。

        挂到**可观察**切点上时，`cb` 返回的 `HookResult` 会被整个丢弃（见 `trigger`），且每一次
        丢弃都记一条 ERROR。这一点 typing 拦不住（`HookCallback` 对两类切点是同一个类型），因此挂
        之前先确认这个切点在 `INTERCEPTABLE` 里——要改 kwargs 或顶替产出只有那一侧读得到。

        挂在本层的 hook**先于**外层的执行，且只有本层的 trigger 看得到它们。
        """
        # setdefault 而非下标：外层声明的扩展切点在本层的表里没有键，而它是合法的注册目标。
        self._hooks.setdefault(self._resolved(junction), []).append(cb)

    async def trigger(self, junction: JunctionRef, **kwargs: Any) -> HookResult:
        """触发切点，返回可拦截链的合并结论。

        扩展切点走这里：spec §5.4 要求内核提供公开的触发接口，而扩展切点由声明它的能力自己触发。
        刻意不给它单开一个方法——两个方法就是两条分发路径，同一段 hook 代码的语义会取决于它挂在
        哪一种切点上。

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
        hooks = self._collect(resolved)
        if not hooks:
            return HookResult()
        if kind is JunctionKind.INTERCEPTABLE:
            return await self._trigger_serial(resolved, hooks, **kwargs)
        await self._trigger_parallel(resolved, hooks, **kwargs)
        return HookResult()

    def _collect(self, junction: JunctionRef) -> list[HookCallback]:
        """这个切点上要跑的整条链：本层在前，外层在后。"""
        own = list(self._hooks.get(junction, []))
        if self._outer is None:
            return own
        return own + self._outer._collect(junction)

    def _declared_kind(self, name: str) -> JunctionKind | None:
        """沿装配链取这个扩展切点的分类；没人声明过交回 None。

        本层优先只是查找顺序，不是覆盖：分类冲突在 `declare_extension_junction` 就被拒了，因此
        一条链上同一个名字至多有一种分类。
        """
        own = self._extension_kinds.get(name)
        if own is not None:
            return own
        return self._outer._declared_kind(name) if self._outer is not None else None

    def _declared_names(self) -> list[str]:
        """整条链上已声明的扩展切点名。它只进错误消息：读者要看的是「我能挂哪些」。"""
        names = set(self._extension_kinds)
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
        validated = _validated_extension_name(junction)
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
            raise ContractViolation(
                expected=f'扩展切点 {junction} 在本层或外层已经声明过',
                received='沿装配链找不到它的分类',
                next_action='先由声明它的那个能力调 declare_extension_junction(name, kind)',
            )
        return declared

    async def _trigger_serial(
        self, junction: JunctionRef, hooks: list[HookCallback], **kwargs: Any
    ) -> HookResult:
        merged: dict[str, Any] = {}
        history: dict[str, list[Any]] = {}
        current = dict(kwargs)
        for cb in hooks:
            outcome = await cb(**current)
            if outcome is None:
                continue
            if outcome.result is not None and not outcome.skip:
                raise _orphan_result(junction, cb, outcome)
            merged.update(outcome.modified)
            for key, value in outcome.modified.items():
                history.setdefault(key, []).append(value)
            current.update(outcome.modified)
            if outcome.skip:
                return HookResult(
                    skip=True, modified=merged, result=outcome.result, history=history
                )
        return HookResult(modified=merged, history=history)

    async def _trigger_parallel(
        self, junction: JunctionRef, hooks: list[HookCallback], **kwargs: Any
    ) -> None:
        outcomes = await asyncio.gather(*(cb(**kwargs) for cb in hooks), return_exceptions=True)
        for cb, outcome in zip(hooks, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                logger.error('observable hook failed at %s', _label(junction), exc_info=outcome)
            elif _carries_intent(outcome):
                _report_discarded_return(junction, cb, outcome)
