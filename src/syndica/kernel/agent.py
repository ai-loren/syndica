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
block，下一轮重试），LLM_FAILURE 只认 `modified['response']` 里的替身、不读它的 `skip` / `result`。
**LLM_FAILURE 一轮内可以触发两次**：替身给出后被 POST_LLM_RESPONSE 清空时，空块守卫会再要一次替
身，第二次的 error 是 LLMResponseError。按轮计数的重试插件会把这一轮记成两次。

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

**入口修复上一次留下的不配对批次。** 硬取消落在轮中间时，ToolExecutor 按约定不在展开的路上写轨迹
（见 tool_executor 模块 docstring），于是留下一个声明了 N 个调用、后面不足 N 条结果的 assistant
block——provider 会拒掉这样的消息。本类是那份 Context 此后唯一的持有者，所以这段修复只能在这里做：
给每个未配对的调用补一块如实说明「中断、副作用未知」的 tool block，并在 `extra['interrupted']` 上留
下机器可读的事实。既不伪造结果，也不删掉 assistant block——两个方向都是对 resume 撒谎。

`max_turns` 约束整个任务而非单个周期。剩余额度为零（`max_turns` 是 0，或已用满）时不调用 LLM，直接
以 MAX_TURNS 停下——这就是预算耗尽的自然终态。
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, cast

from syndica.kernel.context import Context, ContextBlock, StopReason, ToolCall
from syndica.kernel.errors import ContractViolation, LLMResponseError
from syndica.kernel.events import Event, EventBus
from syndica.kernel.hooks import HooksCenter, Junction
from syndica.kernel.llm import LLM, ContextOverflowError
from syndica.kernel.tool import Tool, validate_tool_names
from syndica.kernel.tool_executor import ToolExecutor

__all__ = ['AgentOptions', 'BaseAgent']


@dataclass
class AgentOptions:
    """一个 Agent 的静态配置。`max_turns=None` 表示不限轮次。

    `tools` 的**迭代顺序**逐字进请求前缀，因此它是配置的一部分而不只是一张查找表。它的键必须与
    `tool.name` 和 `schema['name']` 逐字相同（见 `validate_tool_names`）；校验在这里而不只在
    executor 里，宿主才能在声明的那一行拿到错误。**不替换成副本**：executor 与本字段共享同一个
    dict 对象，热插拔靠的就是这个。
    """

    name: str
    llm: LLM
    # 工厂写成带参数的泛型别名而非裸 dict：strict 模式下裸工厂推不出元素类型。
    tools: dict[str, Tool] = field(default_factory=dict[str, Tool])
    system_prompt: str = ''
    max_turns: int | None = 50

    def __post_init__(self) -> None:
        validate_tool_names(self.tools)


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


def _empty_response_error(llm: LLM) -> LLMResponseError:
    return LLMResponseError(
        expected='assistant 响应至少含 content 或 tool_calls 之一',
        received=(
            f'provider={llm.config.provider!r} model={llm.config.model!r} 返回了两者皆空的响应'
        ),
        next_action=('检查 provider 与模型配置；若为偶发，挂一个 llm_failure hook 给出替身或重试'),
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

    def __init__(self, options: AgentOptions, *, hooks: HooksCenter, events: EventBus) -> None:
        self.options: AgentOptions = options
        # 全量 uuid4 hex 而非截断：id 撞车不是软失败——AgentRegistry.register 拒绝重复 id，
        # 撞上的那个 Agent 直接注册不进去。
        self.agent_id: str = uuid.uuid4().hex
        self._hooks = hooks
        self._events = events
        self._executor = ToolExecutor(tools=options.tools, hooks=hooks)
        self._context = Context()
        self._current_turn = 0
        self._running = False

    @property
    def context(self) -> Context:
        return self._context

    def get_system_prompt(self) -> str:
        """必须逐轮返回字节一致的结果：SP 变一个字节，该会话此后所有请求缓存全失效。"""
        return self.options.system_prompt

    async def run(self, query: str) -> Context:
        if self._running:
            raise _concurrent_run(self.agent_id)
        # 置位与上面的检查之间没有挂起点，单事件循环下这对操作是原子的。finally 必须包住
        # BaseException：取消之后这个实例仍要能被 resume（入口的修复正是为它准备的）。
        self._running = True
        try:
            return await self._run(query)
        finally:
            self._running = False

    async def _run(self, query: str) -> Context:
        ctx = self._context
        ctx.stop_reason = None
        _repair_interrupted_batch(ctx)
        ctx.append(ContextBlock(role='user', content=query))
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
            self._current_turn = turn
            await self._emit('turn_start', {'turn': turn})
            await self._hooks.trigger(Junction.TURN_START, ctx=ctx, turn=turn)

            response = await self._one_llm_call(ctx, tool_defs)
            if response is None:
                await self._end_turn(ctx, turn, None)
                continue

            ctx.append(response)
            if not response.tool_calls:
                ctx.stop_reason = StopReason.COMPLETED
                await self._end_turn(ctx, turn, response)
                return StopReason.COMPLETED

            await self._executor.execute_tool_calls(ctx, response.tool_calls)
            await self._end_turn(ctx, turn, response)
        ctx.stop_reason = StopReason.MAX_TURNS
        return StopReason.MAX_TURNS

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
            'messages': [system_block, *ctx.current_traj],
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
