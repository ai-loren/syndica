"""BaseAgent：单轮 turn 循环。

Agent 不管理自己的生命周期：外层由 Runtime 驱动，本类只跑「LLM + 工具」循环（spec §4）。

**System Prompt 每轮重建、置于请求最前、不进轨迹。** 它是请求的前缀而非对话的一部分；进轨迹会让
压缩与持久化重复携带它。工具定义同属前缀，**它们在列表里的次序也是前缀的一部分**：重排不报错，只
让整条会话的缓存静默失效。

**空 assistant block 绝不入 context。** 既无 content 又无 tool_calls 的 block 会污染此后所有请求的
前缀，故障表现为后续全部请求异常，排查成本极高。因此模型的响应、pre_llm_request 跳过时给出的响应、
llm_failure 给出的替身都要过这道守卫。守卫在 POST_LLM_RESPONSE **之后**——补全内容正是那个切点的用
途之一。POST_LLM_RESPONSE 每轮只跑一次，守卫取到的替身不再回流给它，否则一个总返回空块的 hook 会让
本轮不终止。

**LLM 异常分三类，不可合并。** 超窗触发 CONTEXT_OVERFLOW，插件声明已处理则本轮结束、下一轮重试
（这一轮照样计数）；取消不是失败，透传；其余触发 LLM_FAILURE，插件可返回替身响应（重试与故障转移
在此实现），无人处理则给最近一条 user / tool block 打失败标记后上抛。

分类只作用于 `llm.create` 本身：PRE_LLM_REQUEST hook 自己抛出的 ContextOverflowError 不触发
CONTEXT_OVERFLOW（spec §5.1 第 8 步的分类对象是「调用 LLM」）。那个 hook 手里就握着 request，要压缩
就地压缩即可；把它抛的异常也收进分类，等于让压缩链在自己出错时递归调用自己。

**两个失败切点的「我处理了」写法不同，不要互换。** CONTEXT_OVERFLOW 认 `skip=True`（本轮不产出
block，下一轮重试），LLM_FAILURE 只认 `modified['response']` 里的替身。**各自读不到的那个槽位一律
当场报 ContractViolation**：静默丢弃让作者以为处理了而任务照样以同一个错误失败，是最坏的一种反馈。
逐切点的槽位语义见 `hooks.junctions` 模块 docstring 的表。
**LLM_FAILURE 一轮内可以触发两次**：替身给出后被 POST_LLM_RESPONSE 清空时，空块守卫会再要一次替
身，第二次的 error 是 LLMResponseError。按轮计数的重试插件会把这一轮记成两次。

**请求里的消息是轨迹块的副本，不是轨迹块本身。** 见 `_isolated`：hook 改写 request 不得改到历史。

**PRE_LLM_REQUEST 的 `skip=True` 是一票否决。** 它带出的 `result` 直接成为本轮的响应，模型不再被调
用（响应缓存、预算否决走这条路）；`result` 不是 assistant ContextBlock 时报 ContractViolation——跳过
之后没有任何东西能顶替本轮的产出，静默照调等于把 hook 作者的否决扔了。此后它与真实响应同路：
POST_LLM_RESPONSE 与空块守卫都照过。

**Context 归 Agent 所有，跨 run 存活。** `run(query)` 把 query 追加到既有轨迹上继续跑。轮次计数同样
不重置，两者必须一致：计数跨周期累加而轨迹每次清零，第二次 run 会在一轮都没跑的情况下直接撞上限。
失败的一轮照样消耗预算——否则一个每轮都失败的 Agent 可以被无限次 run 而永不撞 `max_turns`。
ZOMBIE 保持可寻址正是为了「一条消息复活、续用原上下文」（spec §4.1）。

**同一个 Agent 不接受并发或重入的 `run()`。** 两次调用交错写同一份轨迹时，一个调用的工具结果会被另
一个调用的 user block 顶开，配对的**相邻性**断裂——而只有本类能保证相邻性。两次调用却都正常返回
COMPLETED，且 Context 归 Agent 所有，这段坏前缀此后每一次请求都带着走。检查与置位之间没有 await，单
事件循环下不需要锁。「什么时候跑、排队、抢占」归 Runtime，「进来的调用满不满足本类的前置条件」归本
类。

**轮与轮之间有一道门。** spec §5.1 的前三步（中止检查 / 挂起检查 / 消费待处理消息）依赖的状态
一样都不属于本类——它们归控制面（§4.3）。因此本类只在每轮开头问门一句「还能往下走吗、有什么要
先看的」，`TurnGate` 之外的一切（注册表、broker、控制块）本类都不认识。没装门时用
`NullTurnGate`，行为与计划 1 逐字一致。

**`ABORTED` 只从这道门出来。** 它是协作式中止落在轮开始前（第 1 步）或模型已经产出之后、工具
执行之前（第 9 步）的结果。第 9 步保留 assistant block，并为每个尚未执行的调用补一块如实说明
「没有执行」的 tool block，于是配对完整、可以直接 resume；硬取消落在轮中间，可能留下不配对的
调用，因此它**不设** `stop_reason`。合并两者会毁掉 resume 正需要的那个区分。

**入口修复上一次留下的不配对批次。** 硬取消落在轮中间时，ToolExecutor 按约定不在展开的路上写轨迹
（见 tool_executor 模块 docstring），于是留下一个声明了 N 个调用、后面不足 N 条结果的 assistant
block——provider 会拒掉这样的消息。本类是那份 Context 此后唯一的持有者，所以这段修复只能在这里做：
给每个未配对的调用补一块如实说明「中断、副作用未知」的 tool block，并在 `extra['interrupted']` 上留
下机器可读的事实。既不伪造结果，也不删掉 assistant block——两个方向都是对 resume 撒谎。

`max_turns` 约束整个任务而非单个周期。剩余额度为零（`max_turns` 是 0，或已用满）时不调用 LLM，直接
以 MAX_TURNS 停下——这就是预算耗尽的自然终态。

**本类自己持有一层 hook 中心，外层是宿主交来的那个**（spec §5.3 的前两层）。`register_hook` 挂进
本层，本层先于外层执行，而外层看不见本层——一个 Runtime 级插件的行为因此不会因为某个 Agent 挂了
局部钩子而改变。
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, cast

from syndica.kernel.context import Context, ContextBlock, StopReason, ToolCall, TrustLevel
from syndica.kernel.control import NullTurnGate, TurnGate, TurnGateDecision
from syndica.kernel.danger import DangerJudge, DangerPolicy, Disposition, validate_danger_options
from syndica.kernel.errors import ConfigError, ContractViolation, LLMResponseError
from syndica.kernel.events import Event, EventBus
from syndica.kernel.hooks import HookCallback, HookResult, HooksCenter, Junction, JunctionRef
from syndica.kernel.llm import LLM, ContextOverflowError
from syndica.kernel.plugins import Plugin, install_plugins
from syndica.kernel.tool import Tool, validate_tool_names
from syndica.kernel.tool_executor import (
    DEFAULT_MAX_TOOL_CONCURRENCY,
    ConfirmationCallback,
    ToolExecutor,
    validate_max_concurrency,
)

__all__ = ['AgentOptions', 'BaseAgent']

_RUNTIME_ONLY: frozenset[Junction] = frozenset(
    {Junction.RUNTIME_SHUTDOWN, Junction.AGENT_SPAWN, Junction.AGENT_DESPAWN}
)
"""只从 `Runtime` 的 hook 中心触发的切点：Agent 局部层上挂它们等于挂一个永不触发的 hook。

新增切点若也只由 `Runtime` 触发，要一并进这里，否则宿主会在这一层静默挂空。
"""


def _runtime_only_junction(junction: Junction) -> ContractViolation:
    return ContractViolation(
        expected=f'挂在 Agent 局部层的切点由本 Agent 自己触发；{junction.value} 不是其中之一',
        received=f'收到把 {junction.value} 挂到单个 Agent 上的请求',
        next_action=(
            f'改挂到 Runtime 的中心：runtime.hooks.register(Junction.{junction.name}, cb)。'
            f'{junction.value} 只从那里触发，而外层的 trigger 看不到 Agent 局部层的 hook——'
            '挂在这里的回调一次都不会跑'
        ),
    )


@dataclass
class AgentOptions:
    """一个 Agent 的静态配置。`max_turns=None` 表示不限轮次。

    `tools` 的**迭代顺序**逐字进请求前缀，因此它是配置的一部分而不只是一张查找表。它的键必须与
    `tool.name` 和 `schema['name']` 逐字相同（见 `validate_tool_names`）；校验在这里而不只在
    executor 里，宿主才能在声明的那一行拿到错误。**不替换成副本**：executor 与本字段共享同一个
    dict 对象，热插拔靠的就是这个。反过来，**内核不就地改这张表**——它可能是工厂在多次调用之间
    复用的同一个 dict，写它等于把它变成「属于第一个 Agent」；驱动方要装工具走
    `BaseAgent.install_runtime_tools`，它换上 Agent 自己的一份并让两侧一起换。

    `danger_judge` / `danger_policy` 为 `None` 时用内核默认（默认规则表 + 高风险阻断）。这两者与
    `max_tool_concurrency` 都与 `tools` 相反：**executor 在构造时把它们取走**，此后改本字段对已经
    建好的 Agent 无效。字段顺序计入公开 API，新字段只能追加在末尾。

    `max_tool_concurrency` 是一段里最多同时跑几个工具（spec §5.6「并发上限可配」），传 1 即完全
    串行。
    """

    name: str
    llm: LLM
    # 工厂写成带参数的泛型别名而非裸 dict：strict 模式下裸工厂推不出元素类型。
    tools: dict[str, Tool] = field(default_factory=dict[str, Tool])
    system_prompt: str = ''
    max_turns: int | None = 50
    danger_judge: DangerJudge | None = None
    danger_policy: DangerPolicy | None = None
    max_tool_concurrency: int = DEFAULT_MAX_TOOL_CONCURRENCY

    def __post_init__(self) -> None:
        validate_tool_names(self.tools)
        validate_danger_options(self.danger_judge, self.danger_policy)
        # 报出的名字必须是宿主写下的那个：executor 那道校验说的是 `max_concurrency`，而宿主写的是
        # `max_tool_concurrency`，指着一个它没写过的参数等于让它去猜。
        validate_max_concurrency(self.max_tool_concurrency, 'max_tool_concurrency')


def _is_empty(block: ContextBlock) -> bool:
    return not block.content and not block.tool_calls


def _described(value: object) -> str:
    """改写值的可读描述。ContextBlock 另说，因为要区分的是它的 role 与是否为空，不是类型。"""
    if isinstance(value, ContextBlock):
        return (
            f'收到 role={value.role!r} 的 ContextBlock'
            f'（content={value.content!r}，{len(value.tool_calls)} 个 tool_calls）'
        )
    return f'收到 {type(value).__name__} 类型的 {value!r}'


def _concurrent_run(agent_id: str) -> ContractViolation:
    return ContractViolation(
        expected='同一个 Agent 上，前一次 run 返回之后才发起下一次',
        received=f'agent_id={agent_id!r} 上已有一次 run 正在进行',
        next_action=(
            '串行化对这个实例的调用（await 上一次 run 再发起下一次），'
            '要并发就给每条并发任务各建一个 Agent 实例；'
            '两次调用交错写同一份轨迹会让工具调用与结果错位，此后每一次请求都带着这段坏前缀'
        ),
    )


def _bad_request(value: object) -> ContractViolation:
    return ContractViolation(
        expected=(
            f"{Junction.PRE_LLM_REQUEST.value} hook 改写后的 request 仍是含 'messages' 键的 dict"
        ),
        received=_described(value),
        next_action=(
            "把 HookResult.modified['request'] 改回一个含 'messages' 的 dict；"
            'request 是传给 hook 的 kwarg，改写不得改变它的形状'
        ),
    )


def _bad_skip_result(value: object) -> ContractViolation:
    return ContractViolation(
        expected=(
            f'{Junction.PRE_LLM_REQUEST.value} hook 用 skip=True 跳过模型调用时，'
            "HookResult.result 是 role='assistant' 的 ContextBlock"
        ),
        received=_described(value),
        next_action=(
            '把 HookResult.result 换成一块 assistant ContextBlock（缓存命中就交出缓存里的那一块），'
            '或去掉 skip=True 让模型照常被调用；跳过之后没有任何东西能顶替本轮的产出'
        ),
    )


def _bad_response(value: object) -> ContractViolation:
    return ContractViolation(
        expected=(
            f'{Junction.POST_LLM_RESPONSE.value} hook 改写后的 response '
            "仍是 role='assistant' 的 ContextBlock"
        ),
        received=_described(value),
        next_action=(
            "把 HookResult.modified['response'] 改成 role='assistant' 的 ContextBlock；"
            '这一块会作为模型本轮的产出进轨迹，换个 role 等于替别人伪造了一条消息'
        ),
    )


def _bad_substitute(value: object) -> ContractViolation:
    return ContractViolation(
        expected=(
            f"{Junction.LLM_FAILURE.value} hook 给出的替身是 role='assistant'、"
            '且至少含 content 或 tool_calls 之一的 ContextBlock'
        ),
        received=_described(value),
        next_action=(
            "把 HookResult.modified['response'] 换成一块非空的 assistant ContextBlock，"
            '或去掉它让原始错误上抛；空块会污染此后每一次请求的前缀'
        ),
    )


def _bad_overflow_idiom(outcome: HookResult) -> ContractViolation:
    """超窗切点收到 `modified['response']` 或 `result`。"""
    return ContractViolation(
        expected=(
            f'{Junction.CONTEXT_OVERFLOW.value} hook 表示「我处理了」的唯一写法是 skip=True'
            '（本轮不产出 block，下一轮用压缩后的轨迹重试）'
        ),
        received=(
            f"收到 modified['response']={_described(outcome.get('response'))}、"
            f'result={_described(outcome.result)}，本切点两者都不读'
        ),
        next_action=(
            '压缩 ctx（它就是传给 hook 的 kwarg，下一轮的请求按它重建）后返回 skip=True；'
            "要给一整块替身响应就把 hook 挂到 llm_failure 上，那个切点认 modified['response']"
        ),
    )


def _bad_failure_idiom(outcome: HookResult) -> ContractViolation:
    """失败切点收到 `skip` 或 `result`。"""
    return ContractViolation(
        expected=(
            f'{Junction.LLM_FAILURE.value} hook 表示「我处理了」的唯一写法是'
            " modified['response'] 给出替身"
        ),
        received=f'收到 skip={outcome.skip}、result={_described(outcome.result)}，本切点两者都不读',
        next_action=(
            "把替身放进 HookResult.modified['response']（重试与故障转移都走这条路）；"
            '不打算处理就返回 HookResult() 让原始错误上抛。'
            '静默忽略等于把作者的否决扔了，而任务照样以同一个错误失败'
        ),
    )


def _bad_post_response_idiom(outcome: HookResult) -> ContractViolation:
    """后置改写切点收到 `result`：那是前置拦截的槽位。"""
    return ContractViolation(
        expected=(
            f"{Junction.POST_LLM_RESPONSE.value} hook 改写产出的唯一写法是 modified['response']"
        ),
        received=f'收到 result={_described(outcome.result)}，本切点不读它',
        next_action=(
            "把它放进 HookResult.modified['response']；"
            'result 是前置切点跳过原操作时的替代结果槽位，模型这一轮已经被调用过了。'
            'skip=True 本身合法，它只表示终止后续 hook'
        ),
    )


def _empty_response_error(llm: LLM) -> LLMResponseError:
    return LLMResponseError(
        expected='assistant 响应至少含 content 或 tool_calls 之一',
        received=(
            f'provider={llm.config.provider!r} model={llm.config.model!r} 返回了两者皆空的响应'
        ),
        next_action=('检查 provider 与模型配置；若为偶发，挂一个 llm_failure hook 给出替身或重试'),
    )


def _isolated(block: ContextBlock) -> ContextBlock:
    """请求里那一份块的副本。**改这个副本改不到轨迹。**

    `Context.current_traj` 只拷列表、不拷块，于是不隔离的话 `request['messages']` 里除首块外全是
    轨迹里那些块**本身**，而这个切点上最自然的插件——发请求前脱敏——会因此永久改写历史：spec §5.2
    纪律 3 要求前缀只 append 不改动（改了不报错，只让此后每一轮的缓存静默全失效），而 spec §12.3
    的脱敏本该发生在写入持久化**之前**，在轨迹已经写入之后就地改是另一件事。

    只拷块与它的顶层容器，不深拷内容：这条路每轮都走一次，逐轮全量深拷的代价随轨迹长度线性增长。
    因此 `extra` 的值、`content` 列表里的元素仍与轨迹共享——挡得住脱敏 / 截断 / 打标这些真实写法，
    挡不住往嵌套容器里就地写。`artifacts` 的元素是 frozen 的，共享它们安全。

    `_mark_failed` 与本函数方向相反且都是承重的：它必须写到轨迹里那一块上，所以它读 ctx 而不读
    请求。两者不可互换。
    """
    return replace(
        block,
        content=list(block.content) if isinstance(block.content, list) else block.content,
        tool_calls=[replace(call) for call in block.tool_calls],
        artifacts=list(block.artifacts),
        extra=dict(block.extra),
    )


def _mark_failed(ctx: Context) -> None:
    """给最近一条 user / tool block 打失败标记（spec §5.1 第 8 步）。

    `Context.current_traj` 交出的是浅拷贝：列表是新的，block 与 ctx 共享，标记因此落在轨迹里那一
    块上。改成深拷贝，这里会静默失效——标记写进一个随即被丢弃的副本。
    """
    for block in reversed(ctx.current_traj):
        if block.role in ('user', 'tool'):
            block.extra['llm_call_failed'] = True
            return


def _unpaired_calls(blocks: list[ContextBlock]) -> list[ToolCall]:
    """轨迹末尾那一批已声明、却没有配对 tool block 的调用。

    只认「assistant 后面紧跟着若干 tool block」这一种尾形。中间隔着别的 role 说明这段轨迹已经被
    另一方改过，往后面追加结果只会让配对错得更远。
    """
    index = len(blocks) - 1
    while index >= 0 and blocks[index].role == 'tool':
        index -= 1
    if index < 0 or blocks[index].role != 'assistant' or not blocks[index].tool_calls:
        return []
    answered = {block.tool_call_id for block in blocks[index + 1 :]}
    return [call for call in blocks[index].tool_calls if call.id not in answered]


_ABORT_AFTER_LLM_WHY = '中止在模型已经产出之后、任何工具执行之前被兑现，本批调用就此停下。'


def _interrupted_output(call: ToolCall) -> str:
    """措辞与 ToolExecutor 的中止消息同源：只说副作用未知，两个方向都不许断言。"""
    return (
        f'期望: {call.name}（id={call.id}）产出一个结果。'
        ' 实际: 上一次运行在这次调用配对之前被中断，是否已经产生副作用未知。'
        ' 下一步: 不要假定它已完成、也不要假定它没发生；仍然需要这个结果就重新发起一次调用。'
    )


def _repair_interrupted_batch(ctx: Context) -> None:
    for call in _unpaired_calls(ctx.current_traj):
        ctx.append(
            ContextBlock(
                role='tool',
                content=_interrupted_output(call),
                tool_call_id=call.id,
                extra={'interrupted': True},
            )
        )


def _skipped_response(result: object) -> ContextBlock:
    """跳过模型调用时，hook 带出的 result 就是本轮的响应。

    空块不在这里拦：它与真实的空响应走同一道守卫，POST_LLM_RESPONSE 仍有机会补全内容。
    """
    if not isinstance(result, ContextBlock) or result.role != 'assistant':
        raise _bad_skip_result(result)
    return result


class BaseAgent:
    """一个 Agent 的 turn 循环。

    Context 归本对象所有并跨 `run` 存活；取消与失败时 `run` 不返回，轨迹只能从 `context` 读。
    """

    def __init__(
        self,
        options: AgentOptions,
        *,
        hooks: HooksCenter,
        events: EventBus,
    ) -> None:
        self.options: AgentOptions = options
        # 全量 uuid4 hex 而非截断：id 撞车不是软失败——AgentRegistry.register 拒绝重复 id，
        # 撞上的那个 Agent 直接注册不进去。
        self.agent_id: str = uuid.uuid4().hex
        # 本 Agent 自己的一层，外层是宿主交来的那个中心。两层合成一条链，本层在前（spec §5.3）。
        self._module_hooks = hooks
        self._hooks = HooksCenter(outer=hooks)
        self._events = events
        self._executor = ToolExecutor(
            tools=options.tools,
            hooks=self._hooks,
            judge=options.danger_judge,
            policy=options.danger_policy,
            max_concurrency=options.max_tool_concurrency,
        )
        self._context = Context()
        self._current_turn = 0
        self._running = False
        # 默认开着，但什么都不拦；真正的门由 `Runtime.spawn` 经 `_attach_gate` 装上。构造器不接
        # `gate=`：那个参数的类型不在公开面，放在这里就等于给宿主一个写不出标注的入口。
        self._gate: TurnGate = NullTurnGate()
        self._gate_attached = False

    def install_runtime_tools(self, tools: Mapping[str, Tool]) -> None:
        """装上由驱动方提供的工具（spec §5.6 的编排五件套走这条路）。

        只能在这里装，理由同 `_attach_gate`：这些工具绑着 `agent_id`，而它在本类构造时才生成，
        工厂手里没有它。

        **不就地改宿主交进来的那张表。** `AgentOptions.tools` 可能是工厂在多次调用之间复用的同一个
        dict，就地写会把它变成「属于第一个 Agent」：此后从同一个工厂造出来的每一个 Agent 都继承到
        一套绑着别人 id 的工具，而跨 Runtime 时连一条日志都没有。所以这里换上本 Agent 自己的一份，
        **两侧一起换**——换完 `self.options.tools` 与 executor 仍是同一个 dict 对象，`AgentOptions`
        承诺的热插拔因此照常成立。代价是宿主手里那张**原**表从此不再是本 Agent 的活表（要热插拔
        就改 `agent.options.tools`）：共享它正是上面那个缺陷本身，但装过与没装过的 Agent 因此不
        对称。

        同名键被新装的覆盖，判重名是调用方的事：静默覆盖会让这个 Agent 自己那一个永远调不到，而
        run 照常以 COMPLETED 返回。

        合并后的**整张**表重过一遍身份轴校验（`AgentOptions.__post_init__`）：工厂可以在
        `AgentOptions` 构造之后再往表里塞条目，那些条目没有人校验过。
        """
        merged = {**self.options.tools, **tools}
        # 先构造再赋值：校验在 __post_init__ 里抛时 self.options 还没换过，这次装配什么都没发生。
        options = replace(self.options, tools=merged)
        self.options = options
        self._executor._retarget(merged)  # pyright: ignore[reportPrivateUsage]

    def install_own_tools(self, runtime: object) -> None:
        """驱动方装完编排工具之后，角色再装只属于自己的那一套。缺省什么都不装。

        调用点在 `Runtime.spawn`，且在 `Runtime(orchestration=)` 那一支之后：编排旗子关掉时，
        需要派活的角色仍然得在这里自己装上 Delegate。已经有同名工具就不要再装——再装会盖掉
        旗子刚刚绑上的那一套。
        """
        del runtime

    @property
    def context(self) -> Context:
        return self._context

    def register_hook(self, junction: JunctionRef, cb: HookCallback) -> None:
        """给**这一个** Agent 挂一个 hook（spec §5.3 的第一层）。

        它先于 Runtime 级插件执行：局部增强要能先看到、先改，跨 Agent 的通用逻辑在它之后收口。
        两层是一条链，因此局部 hook 的 `modified` 会成为插件看到的入参，它的 `skip` 会连插件的
        hook 一起终止。反过来不成立——宿主从 Runtime 的中心触发时看不到这里挂的任何东西。

        `_RUNTIME_ONLY` 那三个切点在这一层上响亮失败：正因为外层看不见内层，它们只从 `Runtime` 的
        中心触发，挂在这里的回调**结构上永不触发**。
        """
        if junction in _RUNTIME_ONLY:
            raise _runtime_only_junction(junction)
        self._hooks.register(junction, cb)

    def _assembly_snapshot(
        self,
    ) -> tuple[
        AgentOptions,
        dict[str, Tool],
        Any,
        Any,
        TurnGate,
        bool,
        Disposition,
        str | None,
        ConfirmationCallback | None,
    ]:
        """保存 Runtime 装配前本对象自己拥有的可变状态。"""
        return (
            self.options,
            dict(self.options.tools),
            self._hooks._assembly_snapshot(),  # pyright: ignore[reportPrivateUsage]
            self._module_hooks._assembly_snapshot(),  # pyright: ignore[reportPrivateUsage]
            self._gate,
            self._gate_attached,
            self._executor._untrusted_high_risk,  # pyright: ignore[reportPrivateUsage]
            self._executor._confirmation_agent_id,  # pyright: ignore[reportPrivateUsage]
            self._executor._confirmation,  # pyright: ignore[reportPrivateUsage]
        )

    def _assembly_restore(
        self,
        snapshot: tuple[
            AgentOptions,
            dict[str, Tool],
            Any,
            Any,
            TurnGate,
            bool,
            Disposition,
            str | None,
            ConfirmationCallback | None,
        ],
    ) -> None:
        """恢复 `_assembly_snapshot`；被拒的 spawn 不留下半套 Agent。"""
        (
            options,
            tools,
            local_hooks,
            module_hooks,
            gate,
            gate_attached,
            disposition,
            confirmation_agent_id,
            confirmation,
        ) = snapshot
        options.tools.clear()
        options.tools.update(tools)
        self.options = options
        self._executor._retarget(options.tools)  # pyright: ignore[reportPrivateUsage]
        self._hooks._assembly_restore(local_hooks)  # pyright: ignore[reportArgumentType,reportPrivateUsage]
        self._module_hooks._assembly_restore(  # pyright: ignore[reportArgumentType,reportPrivateUsage]
            module_hooks
        )
        self._gate = gate
        self._gate_attached = gate_attached
        self._executor._configure_untrusted_high_risk(  # pyright: ignore[reportPrivateUsage]
            disposition
        )
        if confirmation_agent_id is None:
            self._executor._confirmation_agent_id = None  # pyright: ignore[reportPrivateUsage]
            self._executor._confirmation = confirmation  # pyright: ignore[reportPrivateUsage]
        else:
            self._executor._configure_confirmation(  # pyright: ignore[reportPrivateUsage]
                confirmation_agent_id,
                confirmation,
            )

    def _install_module_plugins(self, plugins: Iterable[Plugin]) -> None:
        """把 Module 插件装进 local 与 Runtime 之间的独立一层。"""
        install_plugins(self._module_hooks, tuple(plugins))

    def _configure_runtime_flags(self, flags: Mapping[str, bool | int | str]) -> None:
        """把已解析的 Runtime Flag 接到构造时已创建的执行器。"""
        value = flags.get('untrusted_high_risk', 'reject')
        dispositions = {
            'reject': Disposition.BLOCK,
            'ask': Disposition.CONFIRM,
            'allow': Disposition.ALLOW,
        }
        disposition = dispositions.get(value) if isinstance(value, str) else None
        if disposition is None:
            raise ConfigError(
                expected='untrusted_high_risk 是 reject / ask / allow',
                received=f'收到 {value!r}，类型是 {type(value).__name__}',
                next_action='经 SafetyModule 声明并解析该 Flag，不要直接调用私有装配入口',
            )
        self._executor._configure_untrusted_high_risk(  # pyright: ignore[reportPrivateUsage]
            disposition
        )

    def _configure_confirmation(
        self,
        callback: ConfirmationCallback | None,
    ) -> None:
        """Bind the host confirmation transport to this Agent's executor."""
        self._executor._configure_confirmation(  # pyright: ignore[reportPrivateUsage]
            self.agent_id,
            callback,
        )

    def _attach_gate(self, gate: TurnGate) -> None:
        """装上控制面的门。

        `Runtime` 只能在这里装：门要按 `agent_id` 认邮箱，而 `agent_id` 在本类构造时才生成，
        工厂手里没有它。

        已经装过就报错。两道门意味着两个消费同一个邮箱的人，`consume` 恰好一次，因此输的那个会
        **静默**收不到任何消息——一个只在有人插话时才现身的缺陷。
        """
        if self._gate_attached:
            raise ContractViolation(
                expected=f'agent {self.agent_id!r} 还没有装过 turn 门',
                received=f'它已经装了一个 {type(self._gate).__name__}',
                next_action=(
                    '一个 Agent 只归一个控制面驱动：要换驱动方就新建一个 Agent 实例。'
                    '两道门会各自 consume 同一个邮箱，而 consume 恰好一次，输的那个静默收不到消息'
                ),
            )
        self._gate = gate
        self._gate_attached = True

    def get_system_prompt(self) -> str:
        """必须逐轮返回字节一致的结果：SP 变一个字节，该会话此后所有请求缓存全失效。"""
        return self.options.system_prompt

    async def run(
        self, query: str | None = None, *, trust: TrustLevel = TrustLevel.TRUSTED
    ) -> Context:
        """跑一个工作周期。

        `query=None` 表示「这一次的输入从门里来」——事件循环被一条消息唤醒时走的就是这条路，
        消息由门在第一轮开始前消费并注入。**只有一条消费路径**：中途插话与唤醒用的是同一段代码，
        两者的行为因此不会分叉。

        `trust` 是 `query` 那一块的信任级别。门那条路早就带着它（`Message.trust` 经 `_inject`
        原样落进块），这一条也必须能表达同一件事：派活与分叉交给子 Agent 的第一条输入是模型写下
        的文字，而模型读过什么决定了那段文字是什么的衍生物（spec §12.2 第 2 条）。标记丢在这里，
        子 Agent 的高风险调用就绕过了 §12.2 第 3 条由宿主显式配置的升级处置。
        """
        if self._running:
            raise _concurrent_run(self.agent_id)
        # 置位与上面的检查之间没有挂起点，单事件循环下这对操作是原子的。finally 必须包住
        # BaseException：取消之后这个实例仍要能被 resume（入口的修复正是为它准备的）。
        self._running = True
        try:
            return await self._run(query, trust)
        finally:
            self._running = False

    async def _run(self, query: str | None, trust: TrustLevel) -> Context:
        ctx = self._context
        ctx.stop_reason = None
        _repair_interrupted_batch(ctx)
        if query is not None:
            ctx.append(ContextBlock(role='user', content=query, trust=trust))
        await self._emit('agent_start', {})
        await self._hooks.trigger(Junction.AGENT_START, ctx=ctx)
        # Exception 而非 BaseException：取消既不是正常结束也不是失败，展开的路上不再触发切点、
        # 不再发事件。
        try:
            stop_reason = await self._run_turns(ctx)
        except Exception:
            ctx.stop_reason = StopReason.ERROR
            await self._hooks.trigger(Junction.AGENT_ERROR, ctx=ctx)
            await self._emit('agent_error', {})
            raise
        await self._hooks.trigger(Junction.AGENT_END, ctx=ctx)
        # 事件 payload 必须是纯数据：枚举成员走不出序列化的 SSE 出口。
        await self._emit('agent_end', {'stop_reason': stop_reason.value})
        return ctx

    async def _run_turns(self, ctx: Context) -> StopReason:
        tool_defs = [tool.schema for tool in self.options.tools.values()]
        max_turns = self.options.max_turns
        turns: Iterable[int] = (
            itertools.count(self._current_turn + 1)
            if max_turns is None
            else range(self._current_turn + 1, max_turns + 1)
        )
        for turn in turns:
            # spec §5.1 的第 1–3 步都在计数之前：被中止的一轮不消耗预算，挂起的那段时间也不算
            # 进任何一轮。
            decision = await self._gate.before_turn(turn)
            if decision.abort:
                ctx.stop_reason = StopReason.ABORTED
                return StopReason.ABORTED
            await self._inject(ctx, decision)

            self._current_turn = turn
            await self._emit('turn_start', {'turn': turn})
            await self._hooks.trigger(Junction.TURN_START, ctx=ctx, turn=turn)

            response = await self._one_llm_call(ctx, tool_defs)
            if response is None:
                await self._end_turn(ctx, turn, None)
                continue

            # spec §5.1 第 9 步：模型已经产出之后、工具执行之前再问一次。必须消费中止位——留下
            # 的位会让下一周期的 `before_turn` 立刻再中止一次，而本轮已经结束。不得走
            # `before_turn`：它会顺带 consume 邮箱。
            if await self._gate.check_abort():
                ctx.append(response)
                if response.tool_calls:
                    self._executor.append_unexecuted(
                        ctx, response.tool_calls, why=_ABORT_AFTER_LLM_WHY
                    )
                ctx.stop_reason = StopReason.ABORTED
                await self._end_turn(ctx, turn, response)
                return StopReason.ABORTED

            ctx.append(response)
            if not response.tool_calls:
                ctx.stop_reason = StopReason.COMPLETED
                await self._end_turn(ctx, turn, response)
                return StopReason.COMPLETED

            await self._executor.execute_tool_calls(ctx, response.tool_calls)
            await self._end_turn(ctx, turn, response)
        ctx.stop_reason = StopReason.MAX_TURNS
        return StopReason.MAX_TURNS

    async def _inject(self, ctx: Context, decision: TurnGateDecision) -> None:
        """把门交来的消息追加成 user block，逐条触发 MESSAGE_INJECTED。

        `trust` 从消息上原样带过来：外部来源的消息经这条路进上下文时仍必须是不可信的
        （spec §12.2 第 2 条）。`extra` 只记来源事实（谁发的、什么时机），**不带任何展示策略**
        ——那是 surface 的事（spec §4.5）。
        """
        for message in decision.messages:
            ctx.append(
                ContextBlock(
                    role='user',
                    content=message.content,
                    trust=message.trust,
                    extra={
                        'message_id': message.id,
                        'sender': message.sender,
                        'trigger': message.trigger.value,
                    },
                )
            )
            await self._hooks.trigger(Junction.MESSAGE_INJECTED, ctx=ctx, message=message)

    async def _end_turn(self, ctx: Context, turn: int, message: ContextBlock | None) -> None:
        """turn 的可观测数据在 turn_end 切点之后才冻结，切点自身的耗时也计入（spec §5.1）。"""
        await self._hooks.trigger(Junction.TURN_END, ctx=ctx, turn=turn, message=message)
        await self._emit('turn_end', {'turn': turn})

    async def _one_llm_call(
        self, ctx: Context, tool_defs: list[dict[str, Any]]
    ) -> ContextBlock | None:
        """本轮的模型调用；返回 None 表示超窗已被插件处理，本轮不产出 assistant block。"""
        system_block = ContextBlock(role='system', content=self.get_system_prompt())
        request: dict[str, Any] = {
            'messages': [system_block, *(_isolated(block) for block in ctx.current_traj)],
            'tools': tool_defs,
        }
        hooked = await self._hooks.trigger(Junction.PRE_LLM_REQUEST, ctx=ctx, request=request)
        rewritten = hooked.get('request', request)
        if not isinstance(rewritten, dict):
            raise _bad_request(rewritten)
        request = cast(dict[str, Any], rewritten)
        if 'messages' not in request:
            raise _bad_request(request)

        if hooked.skip:
            response = _skipped_response(hooked.result)
        else:
            response = await self._called(ctx, request)
            if response is None:
                return None

        post = await self._hooks.trigger(
            Junction.POST_LLM_RESPONSE, ctx=ctx, request=request, response=response
        )
        if post.result is not None:
            raise _bad_post_response_idiom(post)
        reviewed = post.get('response', response)
        if not isinstance(reviewed, ContextBlock) or reviewed.role != 'assistant':
            raise _bad_response(reviewed)
        if _is_empty(reviewed):
            return await self._substituted(ctx, request, _empty_response_error(self.options.llm))
        return reviewed

    async def _called(self, ctx: Context, request: dict[str, Any]) -> ContextBlock | None:
        """递交请求；返回 None 表示超窗已被插件处理。"""
        messages = request['messages']
        kwargs = {key: value for key, value in request.items() if key != 'messages'}
        try:
            return await self.options.llm.create(messages, **kwargs)
        except ContextOverflowError as exc:
            outcome = await self._hooks.trigger(
                Junction.CONTEXT_OVERFLOW, ctx=ctx, request=request, error=exc
            )
            # 先拒后认：skip 与 response 同时给出时，认了 skip 就把 response 静默扔了。
            if 'response' in outcome.modified or outcome.result is not None:
                raise _bad_overflow_idiom(outcome) from exc
            if outcome.skip:
                return None
            raise
        except Exception as exc:
            return await self._substituted(ctx, request, exc)

    async def _substituted(
        self, ctx: Context, request: dict[str, Any], error: Exception
    ) -> ContextBlock:
        """向 LLM_FAILURE 要一个替身；无人给出则打上失败标记并上抛 error。"""
        outcome = await self._hooks.trigger(
            Junction.LLM_FAILURE, ctx=ctx, request=request, error=error
        )
        if outcome.skip or outcome.result is not None:
            raise _bad_failure_idiom(outcome) from error
        substitute = outcome.get('response')
        if substitute is None:
            _mark_failed(ctx)
            raise error
        if (
            not isinstance(substitute, ContextBlock)
            or substitute.role != 'assistant'
            or _is_empty(substitute)
        ):
            raise _bad_substitute(substitute)
        return substitute

    async def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        await self._events.emit(Event(kind=kind, agent_id=self.agent_id, payload=payload))
