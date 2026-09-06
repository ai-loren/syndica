"""工具执行。

本计划只做串行；分段并行随计划 2 补入。

**一次工具调用恰好产出一个 role='tool' block。** provider 会拒掉一个声明了 N 个调用、后面却跟着少于
N 条结果的 assistant 消息，所以工具不存在、参数非法、执行抛异常、异常上抛这四条路都必须产出 block。
上抛前尚未执行的调用同样各得一个 block，如实写明「没有执行」——轨迹会被持久化，伪造结果会让 resume
与读轨迹的父 Agent 都按一件没发生过的事往下走。

**SyndicaError 上抛给 agent loop，其余异常转成 output 给模型（spec §10）。** 分界是「这条消息写给
谁」：SyndicaError 的三件套写给程序员或运维，模型执行不了它的 next_action；工具自身逻辑里逃出来的
RuntimeError / OSError 是那次操作的真实失败，模型换个方式可能绕开。按 `ToolFatalError` 划界不够——
ConfigError、ContractViolation、LLMResponseError、ContextOverflowError 都是它的**兄弟**而非子类。

`modified` 通道改写的是 kwargs，类型必须与传进去的一致（`args` 是 dict，`result` 是 ToolResult）；
不合就是 hook 的编程错误，报 ContractViolation 而不是静默丢弃作者的意图。`skip` 带出的 `result` 是
另一回事：它是通用的 Any 槽位，非 ToolResult 的值原样转成 output。

每一条写进 block 的失败消息都按错误三件套写给模型看：模型是它们的第一读者，说清「可用工具有哪些」
「参数 schema 是什么」它就能自我纠正，说「invalid tool」它只能重试同样的错误。
"""

from __future__ import annotations

import json
from typing import Any, cast

from syndica.kernel.context import Context, ContextBlock, ToolCall
from syndica.kernel.errors import ContractViolation, SyndicaError
from syndica.kernel.hooks import HooksCenter, Junction
from syndica.kernel.tool import Tool, ToolResult

__all__ = ['ToolExecutor']


def _rewrite_rejected(
    junction: Junction, key: str, expected: type, value: object
) -> ContractViolation:
    return ContractViolation(
        expected=f'{junction.value} hook 改写 {key!r} 时给出 {expected.__name__}',
        received=f'收到 {type(value).__name__} 类型的 {value!r}',
        next_action=(
            f'把 hook 的 HookResult.modified[{key!r}] 改成 {expected.__name__}；'
            f'{key!r} 是传给 hook 的 kwarg，改写不得改变它的类型'
        ),
    )


def _unknown_tool_result(call: ToolCall, available: list[str]) -> ToolResult:
    return ToolResult(
        output=(
            '期望: 工具名是已注册工具之一。'
            f' 实际: 收到 {call.name!r}；可用工具: {available}。'
            ' 下一步: 改用可用工具之一，不要发明工具名。'
        )
    )


def _bad_arguments_result(call: ToolCall, tool: Tool, detail: str) -> ToolResult:
    return ToolResult(
        output=(
            f'期望: {call.name} 的 arguments 是合法 JSON 对象。'
            f' 实际: 收到 {call.arguments!r}，{detail}。'
            f' 下一步: 按这份参数 schema 重新输出一个 JSON 对象: {tool.parameters}'
        )
    )


def _tool_raised_result(call: ToolCall, exc: Exception) -> ToolResult:
    return ToolResult(
        output=(
            f'期望: {call.name} 正常执行完成。'
            f' 实际: 抛出 {type(exc).__name__}: {exc}。'
            ' 下一步: 调整参数后重试，或换用其他方式达成同一目标。'
        )
    )


def _aborted_result(call: ToolCall, exc: BaseException) -> ToolResult:
    """上抛路径上，失败的那个调用自己的 block。

    措辞不说「工具抛出」：中止也可能来自 hook，而那时工具已经正常跑完了。
    """
    return ToolResult(
        output=(
            f'期望: {call.name}（id={call.id}）产出一个结果。'
            f' 实际: 本次调用中止于 {type(exc).__name__}: {exc}。'
            ' 下一步: 本批调用已在此停下，错误已交给上层处理；'
            '不要重复同一调用，换一种方式达成同一目标。'
        )
    )


def _not_executed_result(call: ToolCall, failed: ToolCall, exc: BaseException) -> ToolResult:
    return ToolResult(
        output=(
            f'期望: {call.name}（id={call.id}）在本批调用里被执行。'
            f' 实际: 没有执行——同批的 {failed.name}（id={failed.id}）'
            f'中止于 {type(exc).__name__}，本批调用就此停下。'
            ' 下一步: 这次调用没有执行、也没有产生任何副作用；'
            '如仍然需要它，在下一轮重新发起。'
        )
    )


def _skipped_result(call: ToolCall, substitute: object) -> ToolResult:
    if isinstance(substitute, ToolResult):
        return substitute
    if substitute is None:
        return ToolResult(
            output=(
                f'期望: {call.name}（id={call.id}）产出一个结果。'
                f' 实际: {Junction.PRE_TOOL_USE.value} hook 跳过了这次调用，'
                '且没有给出替代结果。'
                ' 下一步: 这次调用没有执行、也没有产生任何副作用；'
                '不要把本条当成结果解读，换一种方式达成同一目标。'
            )
        )
    return ToolResult(output=str(substitute))


class ToolExecutor:
    """串行执行一批工具调用，把每个调用的结果作为 role='tool' block 追加进 ctx。

    `tools` 每次调用时查表，不在构造时快照。
    """

    def __init__(self, tools: dict[str, Tool], hooks: HooksCenter) -> None:
        self._tools = tools
        self._hooks = hooks

    async def execute_tool_calls(self, ctx: Context, tool_calls: list[ToolCall]) -> None:
        for index, call in enumerate(tool_calls):
            try:
                result = await self._execute_one(call)
            # Exception 而非 BaseException：取消不该在展开的路上再往轨迹里写东西。
            except Exception as exc:
                self._append(ctx, call, _aborted_result(call, exc))
                for pending in tool_calls[index + 1 :]:
                    self._append(ctx, pending, _not_executed_result(pending, call, exc))
                raise
            self._append(ctx, call, result)

    def _append(self, ctx: Context, call: ToolCall, result: ToolResult) -> None:
        # 两个可变容器都拷一份：block 会被持久化，活得比这个 ToolResult 长。
        ctx.append(
            ContextBlock(
                role='tool',
                content=result.output,
                tool_call_id=call.id,
                artifacts=list(result.artifacts),
                trust=result.trust,
                extra={'tool_meta': dict(result.meta)} if result.meta else {},
            )
        )

    async def _execute_one(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return _unknown_tool_result(call, sorted(self._tools))

        args: dict[str, Any] = {}
        if call.arguments.strip():
            try:
                decoded = json.loads(call.arguments)
            except json.JSONDecodeError as exc:
                return _bad_arguments_result(call, tool, f'解析失败于 {exc.msg}（位置 {exc.pos}）')
            if not isinstance(decoded, dict):
                return _bad_arguments_result(
                    call, tool, f'解析出的是 {type(decoded).__name__}，不是 JSON 对象'
                )
            args = cast(dict[str, Any], decoded)

        pre = await self._hooks.trigger(Junction.PRE_TOOL_USE, tool_call=call, tool=tool, args=args)
        if pre.skip:
            return _skipped_result(call, pre.result)
        rewritten_args = pre.get('args', args)
        if not isinstance(rewritten_args, dict):
            raise _rewrite_rejected(Junction.PRE_TOOL_USE, 'args', dict, rewritten_args)
        args = cast(dict[str, Any], rewritten_args)

        try:
            result = await tool.execute(**args)
        except SyndicaError:
            # 三件套写给程序员或运维，模型执行不了它的 next_action。
            raise
        except Exception as exc:
            result = _tool_raised_result(call, exc)

        post = await self._hooks.trigger(
            Junction.POST_TOOL_USE, tool_call=call, tool=tool, args=args, result=result
        )
        rewritten = post.get('result', result)
        if not isinstance(rewritten, ToolResult):
            raise _rewrite_rejected(Junction.POST_TOOL_USE, 'result', ToolResult, rewritten)
        return rewritten
