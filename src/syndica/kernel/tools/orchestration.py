"""编排工具：让 Agent 自己派活与分叉（spec §5.6）。

它们是 Runtime 控制面方法的薄封装，但**读者不同，因此错误处理相反**：控制面方法对**程序员**报
`ContractViolation`（三件套写给能改代码的人），而这些工具的调用方是**模型**——同一条消息交给它
毫无用处。所以「这个角色名不存在」这类模型改得动的情况写进 `output`，只让真正的程序错误上抛。
上抛的那些都是模型无从应对的：Runtime 已经关机、发起方自己正在被销毁、工厂违约、外部执行体把
这个子 Agent 拆掉了。

**边界写进每个工具的 `description`**，因为那是编排者派活的唯一依据（spec §4.6）：`Delegate`
冷启动、不继承上下文，用于需要独立视角的工作；`Fork` 继承上下文，用于已有结论有价值的延续；
已有 Agent 能继续同一工作时用 `SendMessage`，不要新建；**能直接做完的有界工作不该委派**。

**两者都阻塞发起方的这一轮。** 它们要等子 Agent 做完才拿得到结果，而工具是在轮里跑的。这是有意
的：派活出去却不等结果，父 Agent 就得自己再发明一套轮询。等待经 `Runtime.wait_in_turn`，因此
`abort(发起方)` 打断得了它；发起方此刻仍是 `RUNNING` 而不是 `WAITING`——`WAITING` 说的是「这一轮
悬在一条匹配的答复上」，等一个子 Agent 干活是这一轮的工作本身。

**子 Agent 的 `agent_id` 必须进 `output`，不能只进 `meta`。** `meta` 落在 block 的 `extra` 里、
不进模型上下文，只放那里等于模型拿不到 id：它于是既不能让这个 ZOMBIE 带着上下文继续干，也不能
停掉它，而 ZOMBIE 留在注册表本来就是为了前者（spec §4.1）。

**两者都不声明自己可并发**（`Tool.is_concurrency_safe` 默认 False，不覆写）。它们写共享状态
（注册表、实例表、邮箱），而并发段还会让 `Runtime._refuse_self` 认不出「自己」。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from syndica.kernel.agent_types import ExitMode
from syndica.kernel.context import Context, StopReason, TrustLevel, provenance_of
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.waiting import WaitState

if TYPE_CHECKING:
    from syndica.kernel.runtime import Runtime

__all__ = ['Delegate', 'Fork']


def _final_text(ctx: Context) -> str | None:
    """子 Agent 最后一次留下的文字。

    从后往前找而不是取最后一块：`_finish` 之后的轨迹末尾可以是工具结果块。
    """
    for block in reversed(ctx.current_traj):
        if block.role == 'assistant' and isinstance(block.content, str) and block.content:
            return block.content
    return None


_UNFINISHED: dict[StopReason | None, str] = {
    None: '它在给出结论之前被硬终止了（kill / 关机），轨迹可能停在一轮的中间',
    StopReason.ERROR: '它以错误结束',
    StopReason.MAX_TURNS: '它用完了轮次预算',
    StopReason.ABORTED: '它被中止了',
    StopReason.COMPLETED: '它正常结束，但最后一轮没有留下任何文字',
}

_INTERRUPTED: dict[WaitState, str] = {
    WaitState.ABORTED: '你这一轮被中止了，因此这次等待提前结束',
    WaitState.PREEMPTED: '你身上一次更晚发起的等待顶掉了这一次',
    WaitState.TIMEOUT: '这次等待以超时结束',
    WaitState.ANSWERED: '这次等待已经拿到结果',
}


def _delivered(child_id: str, ctx: Context) -> ToolResult:
    """把子 Agent 的终局翻译成模型看得懂的一段话。

    **不抛异常**：子 Agent 没干成是模型可以应对的情况（换个角色、换个说法、自己做），而不是程序
    错误。

    `trust` 取子 Agent 整份轨迹的溯源结论：它交回来的结论是它读过的东西的**衍生物**，标记必须随
    衍生内容传递（spec §12.2 第 2 条）。丢掉它，父 Agent 会拿着一段看起来像结论的不可信文字，去做
    本该被升级处置的高风险操作。
    """
    reason = ctx.stop_reason
    meta: dict[str, object] = {
        'agent_id': child_id,
        'stop_reason': reason.value if reason is not None else None,
    }
    trust = provenance_of(ctx)
    text = _final_text(ctx)
    if reason is StopReason.COMPLETED and text is not None:
        return ToolResult(output=f'agent {child_id} 交回:\n{text}', meta=meta, trust=trust)
    return ToolResult(
        output=(
            f'期望: agent {child_id} 完成这件工作并给出结论。'
            f' 实际: {_UNFINISHED[reason]}。'
            f' 下一步: 用 QueryAgent({child_id}) 看它的状态，'
            f'用 SendMessage({child_id}, ...) 让它带着已有上下文继续；'
            '要换条路就自己做，或者派给另一个角色。'
        ),
        meta=meta,
        trust=trust,
    )


def _interrupted(child_id: str, state: WaitState) -> ToolResult:
    """等待被打断时交给模型的那一块。

    **不说子 Agent 怎么样了**：它此刻可能还在跑、也可能刚好跑完，而这次等待没有读到它的轨迹。
    两个方向都不许断言——声称它没做完会让模型重复派同一件活，声称它做完了更糟。
    """
    return ToolResult(
        output=(
            f'期望: 等到 agent {child_id} 干完这件工作。'
            f' 实际: {_INTERRUPTED[state]}，它此刻是否已经干完、干到哪一步都未知；'
            '它仍然留在注册表里可寻址。'
            f' 下一步: 用 QueryAgent({child_id}) 看它的状态；'
            f'不再需要它就用 KillAgent({child_id}) 停掉它。'
            '不要重新派同一件活——那会多出一个干着同一件事的 Agent。'
        ),
        meta={'agent_id': child_id, 'stop_reason': None},
        trust=TrustLevel.UNTRUSTED,
    )


async def _collected(runtime: Runtime, agent_id: str, child_id: str) -> ToolResult:
    """等这个子 Agent 干完，把它的终局翻译给模型。`Delegate` 与 `Fork` 的共同出口。"""
    outcome = await runtime.wait_in_turn(agent_id, child_id)
    if outcome.context is None:
        return _interrupted(child_id, outcome.state)
    return _delivered(child_id, outcome.context)


class Delegate(Tool):
    """派活：冷启动一个角色，等它做完，取回结果。

    **模型看到的描述来自 `execute` 的 docstring 摘要，不是本段。** 反射出的 schema 取
    `execute.__doc__` 的摘要行（见 `tool._reflected_schema`），因此「什么时候该用我」必须写在
    那里；写在类 docstring 里的字一个也到不了模型手上。
    """

    __tool_command_args__ = ()

    def __init__(self, runtime: Runtime, agent_id: str) -> None:
        self._runtime = runtime
        self._agent_id = agent_id

    async def execute(self, agent: str, query: str) -> ToolResult:
        """把一件独立的工作派给一个 Agent 角色，等它做完并取回它的结果。
        它冷启动、**看不到你的上下文**，所以用于需要独立视角的工作。
        已有 Agent 能继续同一工作时用 SendMessage，不要新建；
        要在你自己已有的结论之上并行推进就用 Fork；能直接做完的有界工作不该委派。
        返回的第一行带着它的 agent_id——它做完之后仍然留在注册表里可寻址。
        agent: 角色名
        query: 交给它的完整任务描述，它看不到你的上下文，所以要自带全部前提
        """
        factory = self._runtime.agent_factory(agent)
        if factory is None:
            return ToolResult(
                output=(
                    '期望: agent 是已登记的角色之一。'
                    f' 实际: 收到 {agent!r}；已登记的角色: {list(self._runtime.agent_names)}。'
                    ' 下一步: 改用上面列出的角色之一，不要发明角色名；'
                    '没有合适的角色就自己做这件事。'
                )
            )
        child_id = await self._runtime.spawn(
            factory,
            query=query,
            # `query` 是本 Agent 的模型写下的文字，而模型读过什么决定了那段文字是什么的衍生物
            # （spec §12.2 第 2 条）。丢掉这一步，任何不可信内容都能经一次派活洗成子 Agent 的
            # 可信输入，而子 Agent 的高风险调用于是绕过那道关不掉的升级。
            query_trust=self._runtime.provenance_of(self._agent_id),
            parent_id=self._agent_id,
            # ZOMBIE 而不是 EXIT：EXIT 会让子 Agent 做完即连同条目一起消失，而这次调用交回的
            # 那个 agent_id 就成了悬空引用。
            exit_mode=ExitMode.ZOMBIE,
        )
        return await _collected(self._runtime, self._agent_id, child_id)


class Fork(Tool):
    """分叉：造一个继承了当前轨迹的同类型 Agent，等它做完。

    模型看到的描述来自 `execute` 的 docstring 摘要，理由见 `Delegate`。
    """

    __tool_command_args__ = ()

    def __init__(self, runtime: Runtime, agent_id: str) -> None:
        self._runtime = runtime
        self._agent_id = agent_id

    async def execute(self, query: str) -> ToolResult:
        """分叉出一个继承了你当前上下文的 Agent，交给它一件延续性的工作，等它做完。
        用于你已有的结论有价值、需要在此基础上并行推进的场景：
        分身看得到你到此刻为止的轨迹，因此不必重复前提。
        要一个不受你的结论影响的独立视角就用 Delegate；
        已有 Agent 能继续同一工作时用 SendMessage，不要新建。
        返回的第一行带着分身的 agent_id——它做完之后仍然留在注册表里可寻址。
        query: 交给分身的任务描述；它已经看得到你到此刻为止的轨迹，不必重复前提
        """
        factory = self._runtime.factory_of(self._agent_id)
        if factory is None:
            return ToolResult(
                output=(
                    f'期望: 本进程知道 agent {self._agent_id} 是怎么造出来的，'
                    '才能分叉出一个同类型的。'
                    ' 实际: 本进程没有它的构造记录。'
                    ' 下一步: 用 Delegate 派给一个已登记的角色（它冷启动、不继承上下文），'
                    '或者自己把这件事做完。'
                )
            )
        # 不传 query_trust：分叉的信任级别由 `spawn` 从源 Agent 的轨迹算出来，它才知道自己截掉了
        # 哪些块——而被截掉的那些可能携带这份轨迹里唯一的不可信标记。
        child_id = await self._runtime.spawn(
            factory,
            query=query,
            parent_id=self._agent_id,
            exit_mode=ExitMode.ZOMBIE,
            fork_from=self._agent_id,
        )
        return await _collected(self._runtime, self._agent_id, child_id)
