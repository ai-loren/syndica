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

本计划只落地单 Agent 一轮所需的切点，其余（agent_spawn / message_injected / 背景任务等）
随后续计划补齐。
"""

from __future__ import annotations

from enum import Enum

__all__ = ['INTERCEPTABLE', 'Junction', 'OBSERVABLE']


class Junction(Enum):
    AGENT_START = 'agent_start'
    AGENT_END = 'agent_end'
    AGENT_ERROR = 'agent_error'
    TURN_START = 'turn_start'
    TURN_END = 'turn_end'
    PRE_TOOL_USE = 'pre_tool_use'
    POST_TOOL_USE = 'post_tool_use'
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
        Junction.AGENT_START,
        Junction.AGENT_END,
        Junction.AGENT_ERROR,
        Junction.TURN_START,
        Junction.TURN_END,
    }
)
