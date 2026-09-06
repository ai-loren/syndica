"""BaseAgent：单轮 turn 循环。

Agent 不管理自己的生命周期：外层由 Runtime 驱动，本类只跑「LLM + 工具」循环（spec §4）。

**System Prompt 每轮重建、置于请求最前、不进轨迹。** 它是请求的前缀而非对话的一部分；进轨迹会让
压缩与持久化重复携带它。

**空 assistant block 绝不入 context。** 既无 content 又无 tool_calls 的 block 会污染此后所有请求的
前缀，故障表现为后续全部请求异常，排查成本极高。因此模型的响应与 llm_failure 给出的替身都要过这道
守卫。守卫在 POST_LLM_RESPONSE **之后**——补全内容正是那个切点的用途之一。POST_LLM_RESPONSE 每轮
只跑一次，守卫取到的替身不再回流给它，否则一个总返回空块的 hook 会让本轮不终止。

**LLM 异常分三类，不可合并。** 超窗触发 CONTEXT_OVERFLOW，插件声明已处理则本轮结束、下一轮重试
（这一轮照样计数）；取消不是失败，透传；其余触发 LLM_FAILURE，插件可返回替身响应（重试与故障转移
在此实现），无人处理则给最近一条 user / tool block 打失败标记后上抛。

**Context 归 Agent 所有，跨 run 存活。** `run(query)` 把 query 追加到既有轨迹上继续跑。轮次计数同样
不重置，两者必须一致：计数跨周期累加而轨迹每次清零，第二次 run 会在一轮都没跑的情况下直接撞上限。
ZOMBIE 保持可寻址正是为了「一条消息复活、续用原上下文」（spec §4.1）。

`max_turns` 约束整个任务而非单个周期。剩余额度为零（`max_turns` 是 0，或已用满）时不调用 LLM，直接
以 MAX_TURNS 停下——这就是预算耗尽的自然终态。
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, cast

from syndica.kernel.context import Context, ContextBlock, StopReason
from syndica.kernel.errors import ContractViolation, LLMResponseError
from syndica.kernel.events import Event, EventBus
from syndica.kernel.hooks import HooksCenter, Junction
from syndica.kernel.llm import LLM, ContextOverflowError
from syndica.kernel.tool import Tool
from syndica.kernel.tool_executor import ToolExecutor

__all__ = ['AgentOptions', 'BaseAgent']


@dataclass
class AgentOptions:
    """一个 Agent 的静态配置。`max_turns=None` 表示不限轮次。"""

    name: str
    llm: LLM
    # 工厂写成带参数的泛型别名而非裸 dict：strict 模式下裸工厂推不出元素类型。
    tools: dict[str, Tool] = field(default_factory=dict[str, Tool])
    system_prompt: str = ''
    max_turns: int | None = 50


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


class BaseAgent:
    """一个 Agent 的 turn 循环。

    Context 归本对象所有并跨 `run` 存活；取消与失败时 `run` 不返回，轨迹只能从 `context` 读。
    """

    def __init__(self, options: AgentOptions, *, hooks: HooksCenter, events: EventBus) -> None:
        self.options: AgentOptions = options
        self.agent_id: str = uuid.uuid4().hex[:12]
        self._hooks = hooks
        self._events = events
        self._executor = ToolExecutor(tools=options.tools, hooks=hooks)
        self._context = Context()
        self._current_turn = 0

    @property
    def context(self) -> Context:
        return self._context

    def get_system_prompt(self) -> str:
        """必须逐轮返回字节一致的结果：SP 变一个字节，该会话此后所有请求缓存全失效。"""
        return self.options.system_prompt

    async def run(self, query: str) -> Context:
        ctx = self._context
        ctx.stop_reason = None
        ctx.append(ContextBlock(role='user', content=query))
        await self._emit('agent_start', {})
        await self._hooks.trigger(Junction.AGENT_START, ctx=ctx)
        # Exception 而非 BaseException：取消既不是正常结束也不是失败，展开的路上不再触发切点、
        # 不再发事件。
        try:
            await self._run_turns(ctx)
        except Exception:
            ctx.stop_reason = StopReason.ERROR
            await self._hooks.trigger(Junction.AGENT_ERROR, ctx=ctx)
            await self._emit('agent_error', {})
            raise
        await self._hooks.trigger(Junction.AGENT_END, ctx=ctx)
        await self._emit('agent_end', {'stop_reason': _reason(ctx.stop_reason)})
        return ctx

    async def _run_turns(self, ctx: Context) -> None:
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
                return

            await self._executor.execute_tool_calls(ctx, response.tool_calls)
            await self._end_turn(ctx, turn, response)
        ctx.stop_reason = StopReason.MAX_TURNS

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
        messages = request['messages']
        kwargs = {key: value for key, value in request.items() if key != 'messages'}

        try:
            response = await self.options.llm.create(messages, **kwargs)
        except ContextOverflowError as exc:
            outcome = await self._hooks.trigger(
                Junction.CONTEXT_OVERFLOW, ctx=ctx, request=request, error=exc
            )
            if outcome.skip:
                return None
            raise
        except Exception as exc:
            response = await self._substituted(ctx, request, exc)

        post = await self._hooks.trigger(
            Junction.POST_LLM_RESPONSE, ctx=ctx, request=request, response=response
        )
        reviewed = post.get('response', response)
        if not isinstance(reviewed, ContextBlock) or reviewed.role != 'assistant':
            raise _bad_response(reviewed)
        if _is_empty(reviewed):
            return await self._substituted(ctx, request, _empty_response_error(self.options.llm))
        return reviewed

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


def _reason(stop_reason: StopReason | None) -> str | None:
    """事件 payload 必须是纯数据：枚举成员走不出序列化的 SSE 出口。"""
    return stop_reason.value if stop_reason is not None else None
