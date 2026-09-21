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

## 扩展切点

第三方能力用 `<owner>:<event>` 形式的命名空间字符串注册自己的切点（spec §5.4），无需改内核、
无需等发版，并由**声明它的那个能力自己触发**（内核只在固定位置触发枚举切点）。

扩展切点与枚举切点走同一套分发，因此同样必须有分类；但它的分类**不进 `INTERCEPTABLE` /
`OBSERVABLE`**——那两个集合是对 `Junction` 成员的一个划分，掺进字符串会让「新增枚举成员两边都
不在即变红」这条断言失效。扩展切点的分类由声明者在 `HooksCenter.declare_extension_junction`
上用 `JunctionKind` 显式给出，另存一处。
"""

from __future__ import annotations

from enum import Enum
from typing import TypeAlias

__all__ = ['INTERCEPTABLE', 'Junction', 'JunctionKind', 'JunctionRef', 'OBSERVABLE']


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
