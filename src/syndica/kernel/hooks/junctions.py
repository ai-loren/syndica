"""切点定义。

切点名与它的分类都计入公开 API，变更即破坏性变更——分类决定语义，因此与名字同等要紧：

- **可拦截**：按注册顺序串行，`modified` 并入后续 hook 的 kwargs，`skip` 终止其余 hook，
  hook 抛出的异常向上传播。
- **可观察**：并行执行，返回值与异常一律丢弃。

`INTERCEPTABLE` 与 `OBSERVABLE` 必须构成 `Junction` 的一个划分：新增切点必须落进其中之一，
否则它按哪套语义执行无人回答。**不要把其中一个改写成另一个的补集**——那样新增切点会默认落进
可观察，而「并行 + 丢弃返回值 + 吞异常」不该由遗漏来决定。

## 可拦截切点上「我处理了」怎么写

六个切点的写法**不统一，而这种不统一是承重的**：`skip` 是前置拦截（原操作不发生），
`modified` 是后置改写（操作已经发生，改它的产出）。合并两者会让「跳过」与「改写结果」无从区分。
下表逐切点写明哪个槽位表示「已处理」、哪些改写会被读取；**读不到的槽位一律当场报
`ContractViolation`，不静默丢弃**——作者以为自己处理了而任务照样失败，是最坏的一种反馈。

- `PRE_TOOL_USE` —— 已处理 = `skip=True`，`result` 作替代结果（非 ToolResult 转 output 并标
  UNTRUSTED）。读 `modified['args']`。
- `PRE_LLM_REQUEST` —— 已处理 = `skip=True`，`result` 作本轮响应（不是 assistant block 则报错）。
  读 `modified['request']`。
- `POST_TOOL_USE` —— 已处理 = `modified['result']`。带 `result` 槽位报错；`skip` 只终止链。
- `POST_LLM_RESPONSE` —— 已处理 = `modified['response']`。带 `result` 槽位报错；`skip` 只终止链。
- `CONTEXT_OVERFLOW` —— 已处理 = `skip=True`（本轮不产出 block，下一轮用压缩后的轨迹重试）。
  `modified['response']` 与 `result` 槽位都报错。
- `LLM_FAILURE` —— 已处理 = `modified['response']` 给出替身。`skip` 与 `result` 槽位都报错。

两个失败切点的写法相反，不要互换：超窗是「这一轮不该产出东西」，失败是「这一轮的产出由插件顶上」。

`MESSAGE_INJECTED` 是可观察切点：它报告「这条消息已经进上下文了」这个既成事实，改不了任何东西。
要在消息进上下文**之前**改写它，正确的位置是投递侧而不是这里。

`TOOL_CONFIRMATION_REQUIRED` 同样可观察：它报告「这次调用的处置是等人确认」这个判定结果。做成
可拦截等于让一个 hook 把闸放掉，而 hook 只能收紧不能放宽——与信任级别那条是同一条规则。
**观察者也答不了这次确认**：可观察切点的返回值被整个丢弃，答复通道在 surface 侧。

三个生命周期切点（`RUNTIME_SHUTDOWN` / `AGENT_SPAWN` / `AGENT_DESPAWN`）同样只报既成事实：
Agent 已经建好、条目已经摘掉、关机已经开始，没有一处还能被 hook 改。`AGENT_DESPAWN` 在条目消失
**之后**触发，因此想留下父子关系只能在 `AGENT_SPAWN` 上记（它带 `parent_id`）。`RUNTIME_START`
不在这里：`Runtime` 没有任何异步启动入口（`__init__` 是同步的），没有可以 await hook 的位置。
代价是第三方能力没地方做「Runtime 起来时初始化一次」——这是一次自觉的收窄，不是漏实现。不要
为此加 `async def start()`：那会给每个宿主加一步必须调的初始化，且动公开面。

## 轨迹只由内核写（B-19）

内核替一个 Agent 跑的第三方代码——带 `ctx` 参数的切点分发（可拦截与可观察都算）、它的工具段（单个
调用也算：`PRE_TOOL_USE`、`POST_TOOL_USE`、`TOOL_CONFIRMATION_REQUIRED` 的 hook 与工具本身都在段
里），以及从它们里面起的任务——往这个 Agent 的 `Context` 里追加报 `ContractViolation`。三条理由：几方
并发时谁先写取决于调度，同一批调用跑两次就留下两份不同的轨迹；写在一次调用与它的结果之间，提供方会拒掉
下一次请求；写在一次模型调用的请求与回复之间，轨迹会说模型看过它其实没看过的东西。轨迹会被持久化、回放
与父 Agent 消费，三条都不能放过。

要改一次调用的产出，在 `POST_TOOL_USE` 交回 `modified['result']`（或由工具自己交回 `ToolResult`）；
要告诉模型一件事，用 `Runtime.send` 发一条消息，Agent 下一轮按顺序注入；要改这一次发给模型的请求，在
`PRE_LLM_REQUEST` 交回 `modified['request']`。

窗口跟着代码走，不跟着对象走：别的任务开的窗口不冻结 Agent 自己的循环，轨迹的拷贝不带窗口，Agent 的
驱动任务从没有窗口开始。冻结认的是切点的 `ctx` 参数；Runtime 级的切点（`RUNTIME_SHUTDOWN` /
`AGENT_SPAWN` / `AGENT_DESPAWN`）不带它，不冻结任何轨迹——`AGENT_SPAWN` 分发时子 Agent 的任务已经
建好，冻住它，子 Agent 的第一次追加就会失败。冻结管的是追加：块本身是可变的，hook 就地改它不在冻结
之内，同样别那么做；压缩要改写轨迹，走内核给的接口（spec §8.8），不经追加。

## 扩展切点

第三方能力用 `<owner>:<event>` 形式的命名空间字符串注册自己的切点（spec §5.4），无需改内核、
无需等发版，并由**声明它的那个能力自己触发**（内核只在固定位置触发枚举切点）。

扩展切点与枚举切点走同一套分发，因此同样必须有分类；但它的分类**不进 `INTERCEPTABLE` /
`OBSERVABLE`**——那两个集合是对 `Junction` 成员的一个划分，掺进字符串会让「新增枚举成员两边都
不在即变红」这条断言失效。扩展切点的分类由声明者在 `HooksCenter.declare_extension_junction`
上用 `JunctionKind` 显式给出，另存一处。
"""

from __future__ import annotations

import re
from enum import Enum
from typing import TypeAlias

from syndica.kernel.errors import ConfigError

__all__ = [
    'ExtensionJunction',
    'INTERCEPTABLE',
    'Junction',
    'JunctionKind',
    'JunctionRef',
    'OBSERVABLE',
]


class Junction(Enum):
    RUNTIME_SHUTDOWN = 'runtime_shutdown'
    AGENT_SPAWN = 'agent_spawn'
    AGENT_DESPAWN = 'agent_despawn'
    AGENT_START = 'agent_start'
    AGENT_END = 'agent_end'
    AGENT_ERROR = 'agent_error'
    TURN_START = 'turn_start'
    TURN_END = 'turn_end'
    MESSAGE_INJECTED = 'message_injected'
    PRE_TOOL_USE = 'pre_tool_use'
    POST_TOOL_USE = 'post_tool_use'
    TOOL_CONFIRMATION_REQUIRED = 'tool_confirmation_required'
    PRE_LLM_REQUEST = 'pre_llm_request'
    POST_LLM_RESPONSE = 'post_llm_response'
    CONTEXT_OVERFLOW = 'context_overflow'
    LLM_FAILURE = 'llm_failure'


INTERCEPTABLE: frozenset[Junction] = frozenset(
    {
        Junction.PRE_TOOL_USE,
        Junction.POST_TOOL_USE,
        Junction.PRE_LLM_REQUEST,
        Junction.POST_LLM_RESPONSE,
        Junction.CONTEXT_OVERFLOW,
        Junction.LLM_FAILURE,
    }
)

OBSERVABLE: frozenset[Junction] = frozenset(
    {
        Junction.RUNTIME_SHUTDOWN,
        Junction.AGENT_SPAWN,
        Junction.AGENT_DESPAWN,
        Junction.AGENT_START,
        Junction.AGENT_END,
        Junction.AGENT_ERROR,
        Junction.TURN_START,
        Junction.TURN_END,
        Junction.MESSAGE_INJECTED,
        Junction.TOOL_CONFIRMATION_REQUIRED,
    }
)


class JunctionKind(Enum):
    """一个切点按哪套语义分发。

    只用于**扩展切点**：它没有编译期成员表可供划分，分类必须由声明者显式给出，本枚举就是那个
    参数的值域。内核切点的分类仍由 `INTERCEPTABLE` / `OBSERVABLE` 表达，**不要改用本枚举重写
    它们**——那两个集合的价值在于「新增枚举成员两边都不在」会变红，换成逐成员标注就丢了。
    """

    INTERCEPTABLE = 'interceptable'
    OBSERVABLE = 'observable'


JunctionRef: TypeAlias = Junction | str
"""切点引用：内核枚举成员，或 `<owner>:<event>` 形式的扩展切点名。"""

# Module 名同时是扩展切点的 owner 段、入口点名与配置里启用它的写法：三处必须是同一个拼法。在插件的
# `register` 里声明切点的插件，名字也要合这个拼法——它的名字就是那些切点的 owner 段。
MODULE_NAME = re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z')

RUNTIME_ONLY: frozenset[Junction] = frozenset(
    {Junction.RUNTIME_SHUTDOWN, Junction.AGENT_SPAWN, Junction.AGENT_DESPAWN}
)
"""只从 `Runtime` 的 hook 中心触发的切点：Agent 这一层（局部层与 Module 层）上挂它们等于挂一个永不
触发的 hook。新增切点若也只由 `Runtime` 触发，要一并进这里，否则宿主会在 Agent 这一层静默挂空。"""

_NAMESPACE_SEPARATOR = ':'
_KERNEL_JUNCTIONS_BY_VALUE: dict[str, Junction] = {j.value: j for j in Junction}


def _not_an_extension_name(name: object) -> ConfigError:
    return ConfigError(
        expected="扩展切点名是 '<owner>:<event>' 形式的 str",
        received=f'收到 {type(name).__name__} 类型的 {name!r}',
        next_action=(
            "传字符串（如 'artifact:created'）；内核切点不经这里声明，它们的分类写死在 "
            'junctions.py 的 INTERCEPTABLE / OBSERVABLE 里'
        ),
    )


def validated_kind(kind: object) -> JunctionKind:
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


def validated_extension_name(name: object) -> str:
    """校验扩展切点名是 `<owner>:<event>`。

    参数标注为 object 而非 str，理由同 `validated_kind`。

    每一种畸形单独报，不交给一条正则：读者要知道自己错在哪一段。四类畸形一律**拒绝而非归一化**
    （不 strip、不折叠大小写、不切首个分隔符）——切点名计入公开 API，`owner` 段又要与声明者的名字
    逐字比对（插件名、Module 名），允许两种拼法映射到同一个切点就没有唯一的规范拼法可比。放宽接受面
    不是破坏性变更，收紧则是，所以按最窄的收。
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
                'owner 段写这个切点属于谁（在插件里声明时它等于插件名，'
                'Module 的切点等于 Module 名），event 段写发生了什么'
            ),
        )
    return name


class ExtensionJunction:
    """一个扩展切点的句柄（spec §5.4）：声明它的能力拿着它，触发时交回同一个对象。

    **按对象身份比较**：两个同名的句柄不相等。名字谁都拼得出来，持有对象才是声明者的凭据——
    这挡的是误用与抢名，不是同进程里的恶意代码（它能 import 别人的句柄）。拷贝交回自己，与函数
    一样：深拷一份装着句柄的配置，不该让句柄换一个身份、从此再也触发不了。
    """

    __slots__ = ('_kind', '_name')

    def __init__(self, name: str, kind: JunctionKind) -> None:
        self._name = validated_extension_name(name)
        self._kind = validated_kind(kind)

    @property
    def name(self) -> str:
        return self._name

    @property
    def kind(self) -> JunctionKind:
        return self._kind

    @property
    def owner(self) -> str:
        """名字的 owner 段：声明它的那个能力的名字。"""
        return self._name.partition(_NAMESPACE_SEPARATOR)[0]

    def __copy__(self) -> ExtensionJunction:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> ExtensionJunction:
        return self

    def __repr__(self) -> str:
        return f'ExtensionJunction({self._name!r}, JunctionKind.{self._kind.name})'
