"""编排工具：让 Agent 自己派活、分叉、通信、查询与终止（spec §5.6）。

它们是 Runtime 控制面方法的薄封装，但**读者不同，因此错误处理相反**：控制面方法对**程序员**报
`ContractViolation`（三件套写给能改代码的人），而这些工具的调用方是**模型**——同一条消息交给它
毫无用处。所以「这个角色名不存在」「这个 id 不是你派出去的」这类模型改得动的情况写进 `output`，
只让真正的程序错误上抛。上抛的那些都是模型无从应对的：Runtime 已经关机、发起方自己正在被销毁、
工厂违约、外部执行体把这个子 Agent 拆掉了。

**边界写进每个工具的 `description`**，因为那是编排者派活的唯一依据（spec §4.6）：`Delegate`
冷启动、不继承上下文，用于需要独立视角的工作；`Fork` 继承上下文，用于已有结论有价值的延续；
已有 Agent 能继续同一工作时用 `SendMessage`，不要新建；**能直接做完的有界工作不该委派**。

**`Delegate` 与 `Fork` 阻塞发起方的这一轮，`SendMessage` 不阻塞。** 前两者要等子 Agent 做完才拿
得到结果，而工具是在轮里跑的。这是有意的：派活出去却不等结果，父 Agent 就得自己再发明一套轮询。
等待经 `Runtime._wait_in_turn`，因此 `abort(发起方)` 打断得了它；发起方此刻仍是 `RUNNING` 而不是
`WAITING`——`WAITING` 说的是「这一轮悬在一条匹配的答复上」，等一个子 Agent 干活是这一轮的工作本身。

**模型写下的每一段文字进另一个 Agent 的上下文时都带着发起方的溯源结论**（`Delegate` 的 `query`、
`SendMessage` 的 `content`）。发起方读过的不可信内容只要被它复述进去，在接收方那里必须仍然不可信，
否则一次派活或一条消息就把标记洗掉了，而接收方的高风险调用于是绕过 spec §12.2 第 3 条由宿主
显式配置的升级处置。判据是 `context.provenance_of` 这一个定义，经 `Runtime._provenance_of` 读。

**`KillAgent` 的目标限制在发起方的后代。** 注册表是全局的，而模型只要顺着 `QueryAgent` 报出的
`parent` 往上走就能寻址到自己的父与根协调者——不限制的话，一个被说服的子 Agent 能停掉正等着它的
父。`Runtime.kill` 本身不限制是对的（宿主可信），限制属于工具这一层，因为模型不可信。判据沿用
`despawn` 级联用的同一次遍历：`KillAgent` 停得掉的，正是 `despawn(发起方)` 会一起带走的那些。
**`SendMessage` 刻意不受同一条限制**：子 Agent 向父汇报是 spec §4.5 的正常用法，而消息落进对方
上下文时带着发起方的溯源结论、对方自己的闸照常生效——它是加内容，不是拆东西。

**目标归别的执行体管时 `KillAgent` 不上抛。** 两个 Runtime 共享注册表与 broker 是
`Runtime._control_target` 明文预期的拓扑，那时 `kill` 对**程序员**报 `ContractViolation`；而这
件事模型应对得了（改用 `SendMessage`、或把它报给父 Agent），上抛则会打断整批调用、把调用者这次
run 打成 ERROR——而 `QueryAgent` 对同一个目标答的是「一切正常」。

**本模块读 `Runtime` 上四个带下划线的接缝**（`_wait_in_turn` / `_factory_of` / `_provenance_of` /
`_descendants`）。它们刻意不进公开面：加一个公开方法不是破坏性变更，去掉一个是，所以先私有、真有
第三方要再放出来。这些工具与 `Runtime` 同在内核里，因此每个读点单独豁免 `reportPrivateUsage`，
而不是整文件关掉它——关掉之后，此后任何一处真正不该有的私有访问都不会再有人出声。

**子 Agent 的 `agent_id` 必须进 `output`，不能只进 `meta`。** `meta` 落在 block 的 `extra` 里、
不进模型上下文，只放那里等于模型拿不到 id：它于是既不能让这个 ZOMBIE 带着上下文继续干，也不能
停掉它，而 ZOMBIE 留在注册表本来就是为了前者（spec §4.1）。

**五个都不声明自己可并发**（`Tool.is_concurrency_safe` 默认 False，不覆写）。它们写共享状态
（注册表、实例表、邮箱），而 `QueryAgent` 虽是只读，交回的答案也会随并发段里兄弟的动作而变。
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from syndica.kernel.agent_types import ExitMode
from syndica.kernel.context import Context, StopReason, TrustLevel, provenance_of
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.isolation import ISOLATIONS
from syndica.kernel.message import Trigger
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.waiting import WaitState

if TYPE_CHECKING:
    from syndica.kernel.runtime import Runtime

__all__ = [
    'Delegate',
    'Fork',
    'KillAgent',
    'QueryAgent',
    'SendMessage',
    'orchestration_tools',
]

# 交给模型的合法 trigger 取值，从枚举派生：写死一份会在新增成员时静默过期，而这行字是模型改这个
# 参数的唯一依据。
_TRIGGERS: str = ' / '.join(trigger.value for trigger in Trigger)


def _final_text(ctx: Context) -> str | None:
    """子 Agent 最后一次留下的文字。

    从后往前找而不是取最后一块：轨迹的末尾可以是 assistant 之外的块（恢复时补上的、被打断的那批
    调用的结果块；宿主在两次运行之间追加的），取最后一块就会把它们当成子 Agent 的结论。
    """
    for block in reversed(ctx.current_traj):
        if block.role == 'assistant' and isinstance(block.content, str) and block.content:
            return block.content
    return None


# 两张表都按枚举**取全**，缺一个键就是一个 KeyError 从工具里上抛、把整次 run 打成 error。
# 因此它们的完备性由用例按枚举断言，而不是靠「今天这一格到不了」。
_UNFINISHED: dict[StopReason | None, str] = {
    None: '它在给出结论之前被硬终止了（kill / 关机），轨迹可能停在一轮的中间',
    StopReason.ERROR: '它以错误结束',
    StopReason.MAX_TURNS: '它用完了轮次预算',
    StopReason.BUDGET: '它的预算（轮次 / token / 成本 / 时间之一）用完了',
    StopReason.ABORTED: '它被中止了',
    StopReason.COMPLETED: '它正常结束，但最后一轮没有留下任何文字',
}

_INTERRUPTED: dict[WaitState, str] = {
    WaitState.ABORTED: '你这一轮被中止了，因此这次等待提前结束',
    WaitState.PREEMPTED: '你身上一次更晚发起的等待顶掉了这一次',
    WaitState.TIMEOUT: '这次等待以超时结束',
    WaitState.ANSWERED: '这次等待报告拿到了结果，却没有交出那份轨迹',
}


def _next_step(child_id: str, reason: StopReason | None) -> str:
    """子 Agent 没干成时交给模型的下一步。

    `max_turns` 那一格不能建议 SendMessage：轮数跨 `run()` 累计且永不重置（登记 B-21），唤醒它只会
    让它立刻再次 `max_turns`，那条消息留在邮箱里随队列被摘掉——模型会一轮接一轮地叫醒一个再也
    干不了活的 Agent，把自己的预算也耗光。
    """
    if reason is StopReason.BUDGET:
        return (
            '预算用完的 Agent 拒收消息，SendMessage 叫不醒它；用 GetBudget 看你自己还剩多少，'
            '剩得够就用 Delegate 派一个新的 Agent、只交给它一件更小的事，不够就自己收尾交付。'
        )
    if reason is StopReason.MAX_TURNS:
        return (
            '它的轮次预算不会重置，再给它发 SendMessage 只会让它立刻停下、那条消息随之丢失；'
            '自己把剩下的部分做完，或者用 Delegate 派一个新的 Agent，'
            '只交给它一件更小、自带全部前提的事。'
        )
    return (
        f'用 QueryAgent({child_id}) 看它的状态，'
        f'用 SendMessage({child_id}, ...) 让它带着已有上下文继续；'
        '要换条路就自己做，或者派给另一个角色。'
    )


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
    verdict = ctx.verdict
    if verdict is not None:
        # 子 Agent 的验收裁决随交回（§7.5）：父 Agent 据此知道交回来的东西验过没有、过没过。
        meta['verdict'] = verdict.status.value
        meta['needs_human'] = list(verdict.needs_human)
        meta['unverified_cause'] = verdict.unverified_cause
    trust = provenance_of(ctx)
    text = _final_text(ctx)
    if reason is StopReason.COMPLETED and text is not None:
        suffix = '' if verdict is None else f'\n\n（验收裁决：{verdict.status.value}）'
        return ToolResult(output=f'agent {child_id} 交回:\n{text}{suffix}', meta=meta, trust=trust)
    return ToolResult(
        output=(
            f'期望: agent {child_id} 完成这件工作并给出结论。'
            f' 实际: {_UNFINISHED[reason]}。'
            f' 下一步: {_next_step(child_id, reason)}'
        ),
        meta=meta,
        trust=trust,
    )


def _interrupted(child_id: str, state: WaitState) -> ToolResult:
    """等待被打断时交给模型的那一块。

    **不说子 Agent 怎么样了**：它此刻可能还在跑、也可能刚好跑完，而这次等待没有读到它的轨迹。
    两个方向都不许断言——声称它没做完会让模型重复派同一件活，声称它做完了更糟。

    `trust` 是 `TRUSTED`：这一块只嵌内核自撰的文字与一个内核生成的 `child_id`，没有任何来自子
    Agent 或外部的字节（判据见 `tool_executor` 模块 docstring）。标成 `UNTRUSTED` 会让一次正当的
    `abort` 永久废掉这个 Agent 的 `AllowAll`——这块落进轨迹之后，`provenance_of` 是「出现过就算」
    的上界；宿主虽能用 SafetyModule 显式放宽，但默认仍会阻断。**日后往这段话里加入子 Agent 的
    部分产出，就必须改回读子 Agent 的真实溯源**。
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
        trust=TrustLevel.TRUSTED,
    )


async def _collected(
    runtime: Runtime, agent_id: str, child_id: str, isolation: str = 'shared'
) -> ToolResult:
    """等这个子 Agent 干完，把它的终局翻译给模型。`Delegate` 与 `Fork` 的共同出口。

    隔离起来的子 Agent 停下之后把副本合并回来，合并报告接在结果后面（它列的文件名出自子 Agent，
    所以跟着结果的溯源结论走）。等待被打断时它可能还在写，不合并。"""
    outcome = await runtime._wait_in_turn(agent_id, child_id)  # pyright: ignore[reportPrivateUsage]
    if outcome.context is None:
        interrupted = _interrupted(child_id, outcome.state)
        if isolation == 'shared':
            return interrupted
        return replace(
            interrupted,
            output=(
                f'{interrupted.output} 它的隔离副本还留着、没有合并：'
                f'等它停下之后由宿主调 Runtime.merge_isolated({child_id!r}) 合并。'
            ),
        )
    result = _delivered(child_id, outcome.context)
    if isolation == 'shared':
        return result
    try:
        report = await runtime.merge_isolated(child_id)
    except ConfigError as exc:
        summary = (
            f'期望: 把 agent {child_id} 的隔离副本合并回共享工作区。 实际: {exc.received}。'
            f' 下一步: 副本还留着，宿主可以调 Runtime.merge_isolated({child_id!r}) 再合并。'
        )
        return replace(result, output=f'{result.output}\n\n{summary}')
    meta = {
        **result.meta,
        'merged': list(report.merged),
        'conflicts': [conflict.path for conflict in report.conflicts],
    }
    revived = (
        ''
        if report.kept
        else (
            f'\n再用 SendMessage 叫醒 {child_id} 时它直接改共享工作区、不再在副本里，'
            '它的上下文里还写着「在副本里」：在消息里告诉它这一点；要隔离就重新 Delegate。'
        )
    )
    return replace(result, output=f'{result.output}\n\n{report.summary()}{revived}', meta=meta)


def _isolation_refused(isolation: str, runtime: Runtime) -> ToolResult | None:
    if isolation not in ISOLATIONS:
        return ToolResult(
            output=(
                f'期望: isolation 是 {" / ".join(ISOLATIONS)} 之一。 实际: {isolation!r}。'
                ' 下一步: 不需要隔离就省略它（shared）。'
            )
        )
    workspace = runtime.workspace
    if isolation != 'shared' and (workspace is None or not workspace.can_isolate):
        return ToolResult(
            output=(
                '期望: 这次运行能给子 Agent 建隔离副本。'
                f' 实际: 不能，却收到 isolation={isolation!r}。'
                ' 下一步: 省略 isolation（shared），让它直接在共享工作区里干活。'
            )
        )
    return None


class _AgentTool(Tool):
    """编排工具的共同构造：一个工具实例代表**某一个** Agent 对 Runtime 控制面的调用权。

    `agent_id` 在 `BaseAgent.__init__` 里才生成，所以这些工具只能在实例建好之后挂进它那张活的
    工具表——两个 Agent 各有各的实例，共用一份会让一次派活挂到别人名下。

    **每个子类自己声明 `__tool_command_args__`，基类不替它声明。** 继承一个 `()` 会让日后某个真
    会执行东西的子类静默失去那道闸，而漏声明本该是响亮的（登记 D-34）。
    """

    def __init__(self, runtime: Runtime, agent_id: str) -> None:
        self._runtime = runtime
        self._agent_id = agent_id


class Delegate(_AgentTool):
    """派活：冷启动一个角色，等它做完，取回结果。

    **模型看到的描述来自 `execute` 的 docstring 摘要，不是本段。** 反射出的 schema 取
    `execute.__doc__` 的摘要行（见 `tool._reflected_schema`），因此「什么时候该用我」必须写在
    那里；写在类 docstring 里的字一个也到不了模型手上。
    """

    __tool_command_args__ = ()
    __tool_external_input__ = True

    async def execute(self, agent: str, query: str, isolation: str = 'shared') -> ToolResult:
        """把一件独立的工作派给一个 Agent 角色，等它做完并取回它的结果。
        它冷启动、**看不到你的上下文**，所以用于需要独立视角的工作。
        已有 Agent 能继续同一工作时用 SendMessage，不要新建；
        要在你自己已有的结论之上并行推进就用 Fork；能直接做完的有界工作不该委派。
        返回的第一行带着它的 agent_id——它做完之后仍然留在注册表里可寻址。
        isolation 为 worktree 或 snapshot 时它在共享工作区的一份拷贝里改，做完逐文件合并回来，
        冲突的文件另存成旁边的 .theirs；缺省 shared 直接改共享工作区。
        agent: 角色名
        query: 交给它的完整任务描述，它看不到你的上下文，所以要自带全部前提
        isolation: shared（缺省）/ worktree / snapshot
        """
        refused = _isolation_refused(isolation, self._runtime)
        if refused is not None:
            return refused
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
        # `query` 是本 Agent 的模型写下的文字，而模型读过什么决定了那段文字是什么的衍生物
        # （spec §12.2 第 2 条）。丢掉这一步，任何不可信内容都能经一次派活洗成子 Agent 的可信
        # 输入，而子 Agent 的高风险调用于是绕过宿主配置的升级处置。
        trust = self._runtime._provenance_of(self._agent_id)  # pyright: ignore[reportPrivateUsage]
        child_id = await self._runtime.spawn(
            factory,
            query=query,
            query_trust=trust,
            parent_id=self._agent_id,
            # ZOMBIE 而不是 EXIT：EXIT 会让子 Agent 做完即连同条目一起消失，而这次调用交回的
            # 那个 agent_id 就成了悬空引用。
            exit_mode=ExitMode.ZOMBIE,
            isolation=isolation,
        )
        return await _collected(self._runtime, self._agent_id, child_id, isolation)


class Fork(_AgentTool):
    """分叉：造一个继承了当前轨迹的同类型 Agent，等它做完。

    模型看到的描述来自 `execute` 的 docstring 摘要，理由见 `Delegate`。
    """

    __tool_command_args__ = ()
    __tool_external_input__ = True

    async def execute(self, query: str, isolation: str = 'shared') -> ToolResult:
        """分叉出一个继承了你当前上下文的 Agent，交给它一件延续性的工作，等它做完。
        用于你已有的结论有价值、需要在此基础上并行推进的场景：
        分身看得到你到此刻为止的轨迹，因此不必重复前提。
        要一个不受你的结论影响的独立视角就用 Delegate；
        已有 Agent 能继续同一工作时用 SendMessage，不要新建。
        返回的第一行带着分身的 agent_id——它做完之后仍然留在注册表里可寻址。
        isolation 为 worktree 或 snapshot 时分身在共享工作区的一份拷贝里改，做完逐文件合并回来，
        冲突的文件另存成旁边的 .theirs；缺省 shared 直接改共享工作区。
        query: 交给分身的任务描述；它已经看得到你到此刻为止的轨迹，不必重复前提
        isolation: shared（缺省）/ worktree / snapshot
        """
        refused = _isolation_refused(isolation, self._runtime)
        if refused is not None:
            return refused
        factory = self._runtime._factory_of(self._agent_id)  # pyright: ignore[reportPrivateUsage]
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
        # 哪些块——而被截掉的那些可能携带这份轨迹里唯一的不可信标记。本工具恒带 query，所以走的是
        # `query_trust` 那条落点，不是 `_carry_truncated_taint`。
        child_id = await self._runtime.spawn(
            factory,
            query=query,
            parent_id=self._agent_id,
            exit_mode=ExitMode.ZOMBIE,
            fork_from=self._agent_id,
            isolation=isolation,
        )
        return await _collected(self._runtime, self._agent_id, child_id, isolation)


class SendMessage(_AgentTool):
    """通信：给一个已有的 Agent 投一条消息，不等它回话。

    模型看到的描述来自 `execute` 的 docstring 摘要，理由见 `Delegate`。
    """

    __tool_command_args__ = ()
    __tool_external_input__ = False

    async def execute(self, agent_id: str, content: str, trigger: str = 'pending') -> ToolResult:
        """给一个**已有的** Agent 发一条消息，不等它回话；它没在跑就把它叫醒。
        已有 Agent 能继续同一工作时用这个，不要新建
        ——新建会丢掉它已经攒下的全部上下文，而做完一件活的 Agent 仍然留在注册表里可寻址。
        要一个不受你影响的独立视角用 Delegate，要在你自己的结论之上并行推进用 Fork。
        agent_id: 收件人的 id（Delegate / Fork 交回的那一个）
        content: 消息正文；它看得到的只有这段话，所以要自带全部前提
        trigger: pending 表示它下一轮开始前就读到，deferred 表示等它手上这件活做完再读
        """
        try:
            when = Trigger(trigger)
        except ValueError:
            return ToolResult(
                output=(
                    f'期望: trigger 是 {_TRIGGERS} 之一。'
                    f' 实际: 收到 {trigger!r}。'
                    ' 下一步: 要它尽快看到就用 pending，'
                    '要等它把手上这件活做完再看就用 deferred。'
                ),
                meta={'ok': False, 'agent_id': agent_id},
            )
        # `content` 是本 Agent 的模型写下的文字，而模型读过什么决定了那段文字是什么的衍生物
        # （spec §12.2 第 2 条）。丢掉这一步，任何不可信内容都能经一条消息洗成收件人的可信输入，
        # 而收件人的高风险调用于是绕过宿主配置的升级处置。
        trust = self._runtime._provenance_of(self._agent_id)  # pyright: ignore[reportPrivateUsage]
        delivered = await self._runtime.send(
            agent_id, content, trigger=when, sender=self._agent_id, trust=trust
        )
        if not delivered.ok:
            return ToolResult(
                output=(
                    f'期望: 这条消息进 agent {agent_id!r} 的邮箱。'
                    f' 实际: 投递失败——{delivered.reason or "broker 没有给出原因"}。'
                    f' 下一步: 用 QueryAgent({agent_id}) 看它还在不在；'
                    '它还在就重发一次，已经不在就用 Delegate 派一个新的来做这件事。'
                ),
                meta={'ok': False, 'agent_id': agent_id},
            )
        return ToolResult(
            output=(
                f'已投递给 agent {agent_id}（trigger={when.value}）。'
                '它会在自己的下一轮读到它；这次调用不等它回话。'
            ),
            meta={'ok': True, 'agent_id': agent_id, 'message_id': delivered.message_id},
        )


class QueryAgent(_AgentTool):
    """查询：读一个 Agent 的状态。**只读，绝不激活。**

    模型看到的描述来自 `execute` 的 docstring 摘要，理由见 `Delegate`。
    """

    __tool_command_args__ = ()
    __tool_external_input__ = True

    async def execute(self, agent_id: str) -> ToolResult:
        """读一个 Agent 的名字、状态、父与子代数量；**只看不碰**，不会把它叫醒。
        派出去的活做完没有、还在不在，用它来问。
        要让一个停下来的 Agent 继续干用 SendMessage，不再需要它就用 KillAgent。
        agent_id: 要查的 Agent 的 id
        """
        info = await self._runtime.find(agent_id)
        if info is None:
            # 两条路的 meta 键必须一样：少一个键，读 `meta['children']` 的 surface 在这条路上撞
            # KeyError。「在不在」由 `status is None` 回答。
            return ToolResult(
                output=(
                    f'期望: agent {agent_id!r} 在注册表里。'
                    ' 实际: 没有这个条目——它从未存在，或已被销毁。'
                    ' 下一步: 核对 id（Delegate / Fork 交回的那一个）；'
                    '确实需要这件工作就用 Delegate 派一个新的。'
                ),
                meta={'agent_id': agent_id, 'status': None, 'children': []},
            )
        return ToolResult(
            output=(
                f'agent {agent_id}: name={info.name} status={info.status.value} '
                f'parent={info.parent_id} children={len(info.children)}'
            ),
            meta={
                'agent_id': agent_id,
                'status': info.status.value,
                'children': list(info.children),
            },
        )


class KillAgent(_AgentTool):
    """终止：停掉一个后代 Agent 的事件循环，条目留在注册表里。

    模型看到的描述来自 `execute` 的 docstring 摘要，理由见 `Delegate`。
    """

    __tool_command_args__ = ()
    __tool_external_input__ = False

    async def execute(self, agent_id: str) -> ToolResult:
        """终止一个**你派出去的** Agent 的事件循环，让它立刻停下手上的活。
        只停得了你自己派出去的那些（Delegate / Fork 造出来的，以及它们的后代）。
        它仍留在注册表里可寻址，一条 SendMessage 就能让它带着原有上下文继续。
        agent_id: 要终止的 Agent 的 id
        """
        if agent_id == self._agent_id:
            # 判据是**两个 id 相等**而不是任务身份：`Runtime._refuse_self` 比对 `current_task()`，
            # 而一个声明了可并发的工具跑在 `gather` 建的子任务里认不出自己。id 相等无法被执行位置
            # 改变。
            return ToolResult(
                output=(
                    '期望: KillAgent 的目标是别人。'
                    ' 实际: 收到的是你自己的 id。'
                    ' 下一步: 杀自己会在这次工具调用返回之前就把你取消掉，'
                    '这一批调用因此不会有结果、轨迹会停在半路。'
                    '想结束当前工作就直接给出最终回复；由别人来停你则请你的父 Agent 动手。'
                )
            )
        if await self._runtime.find(agent_id) is None:
            return ToolResult(
                output=(
                    f'期望: agent {agent_id!r} 在注册表里。'
                    ' 实际: 没有这个条目，它已经不在了。'
                    ' 下一步: 核对 id；已经不在的 Agent 不需要再终止一次。'
                )
            )
        descendants = await self._runtime._descendants(  # pyright: ignore[reportPrivateUsage]
            self._agent_id
        )
        if agent_id not in descendants:
            return ToolResult(
                output=(
                    '期望: KillAgent 的目标是你派出去的 Agent'
                    '（Delegate / Fork 造出来的，以及它们的后代）。'
                    f' 实际: agent {agent_id!r} 不在你这棵子树里——'
                    '它是你的父、你的同辈，或别人派出去的。'
                    ' 下一步: 只停你自己派出去的那些。'
                    f'用 QueryAgent({agent_id}) 读它的 parent：那是派出它的一方，'
                    '把「该停掉它」写进你的结论交给你的父 Agent 去找它；'
                    '它的 parent 是 None（宿主直接建的根）就没有哪个 Agent 停得了它，'
                    '如实报告这件事，不要重试。'
                )
            )
        try:
            await self._runtime.kill(agent_id)
        except ContractViolation as exc:
            # `kill` 这条拒绝是写给**程序员**的（本进程没有它的实例、驱动任务还没接管）。对模型
            # 它是可应对的：目标归别的执行体管，改用 SendMessage 或把这件事报上去。让它上抛会打断
            # 整批调用、把调用者这次 run 打成 ERROR，而 QueryAgent 对同一个目标答的是「一切正常」。
            return ToolResult(
                output=(
                    f'期望: 停下 agent {agent_id} 的事件循环。'
                    f' 实际: 本进程停不了它——{exc.received}。'
                    f' 下一步: 用 SendMessage({agent_id}, ...) 让它自己收手；'
                    '要真的终止它，把这件事写进你的结论交给你的父 Agent——'
                    '只有持有它那个执行体停得了它。'
                )
            )
        return ToolResult(
            output=(
                f'已终止 agent {agent_id} 的事件循环。它仍留在注册表里，'
                '一条 SendMessage 就能让它带着原有上下文继续。'
            ),
            meta={'agent_id': agent_id},
        )


def orchestration_tools(runtime: Runtime, agent_id: str) -> dict[str, Tool]:
    """给一个 Agent 造一套编排工具。

    **次序固定且不是字母序**：工具表的迭代顺序逐字进请求前缀（spec §5.2），重排不报错，只让整条
    会话的缓存静默失效。这个次序照 spec §5.6 的清单：先派活、再分叉、再通信、再查询、最后终止。

    每个 Agent 各造一套：工具实例绑着发起方的 `agent_id`，共用一套会让所有 Agent 的派活、投递与
    终止都记在同一个主体名下。
    """
    return {
        'Delegate': Delegate(runtime, agent_id),
        'Fork': Fork(runtime, agent_id),
        'SendMessage': SendMessage(runtime, agent_id),
        'QueryAgent': QueryAgent(runtime, agent_id),
        'KillAgent': KillAgent(runtime, agent_id),
    }
