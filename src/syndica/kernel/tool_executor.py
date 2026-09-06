"""工具执行。

本计划只做串行；分段并行随计划 2 补入。

**一次工具调用恰好产出一个 role='tool' block。** provider 会拒掉一个声明了 N 个调用、后面却跟着少于
N 条结果的 assistant 消息，所以工具不存在、参数非法、执行抛异常、异常上抛这四条路都必须产出 block。
上抛前尚未执行的调用同样各得一个 block，如实写明「没有执行」——轨迹会被持久化，伪造结果会让 resume
与读轨迹的父 Agent 都按一件没发生过的事往下走。反方向也是撒谎：中止的那个调用可能在工具跑完之后才
失败（POST hook 抛异常就走这条路），因此它的 block 只说副作用未知，不得声称没有副作用；两条消息不可
互换，resume 正是按它们决定要不要重做。

**被取消的一批不补 block。** `CancelledError` 这类 BaseException 在批中途到达时直接上抛，展开路上不
再写轨迹，于是这一批留下的 block **少于** assistant 声明的调用数。消费持久化轨迹的一方（resume、读
轨迹的父 Agent）必须自己收拾这段不配对的 assistant 消息——取消可以落在 execute_tool_calls 被进入之
前，这个洞在本层补不上。

**SyndicaError 上抛给 agent loop，其余异常转成 output 给模型（spec §10）。** 分界是「这条消息写给
谁」：SyndicaError 的三件套写给程序员或运维，模型执行不了它的 next_action；工具自身逻辑里逃出来的
RuntimeError / OSError 是那次操作的真实失败，模型换个方式可能绕开。按 `ToolFatalError` 划界不够——
ConfigError、ContractViolation、LLMResponseError、ContextOverflowError 都是它的**兄弟**而非子类。

**trust 只能收紧，不能放松。** hook 改写结果时最终 trust 取原结果与**链上每一次改写**里最严的那
一个：ToolResult 上没有任何东西能区分「消毒之后刻意提升」与「忘了带 trust=」，而后者的后果是未标记
的不可信内容进模型上下文（spec §12.2），比前者常见得多。刻意提升需要一个显式机制，目前不存在。看
`HookResult.history` 而不是 `modified`：后者 last-wins，链中间那次收紧会被后一个 hook 的默认值抹掉。

**executor 自撰的 block，凡嵌进了既非工具、亦非本模块写下的文本，一律 UNTRUSTED。** `str(exc)` 经
常携带工具自己没写过的字节（fetch 回来的正文、读进来的文件、子进程的 stderr），且它是这些字节**第
一次**进入轨迹；异常对象上没有任何 trust 标记可读，而假定可信是不安全的那一侧。只嵌代码里的标识符
（异常类名、工具名、调用 id）或模型自己的产出（`call.name` / `call.arguments`）不算：后者已经以同样
或更松的 trust 躺在轨迹里，回显它不会让任何新字节洗成可信。

`modified` 通道改写的是 kwargs，类型必须与传进去的一致（`args` 是 dict，`result` 是 ToolResult）；
不合就是 hook 的编程错误，报 ContractViolation 而不是静默丢弃作者的意图。工具自己返回非 ToolResult
是另一件事，必须报另一条消息：混成一条会让零 hook 的调用方被指去修一个不存在的 hook。`skip` 带出的
`result` 是第三件事：它是通用的 Any 槽位，非 ToolResult 的值原样转成 output，并按 UNTRUSTED 落盘
——裸值上没有标记可读，而假定可信是不安全的那一侧；要声明可信就交出一个 ToolResult。

每一条写进 block 的失败消息都按错误三件套写给模型看：模型是它们的第一读者，说清「可用工具有哪些」
「参数 schema 是什么」它就能自我纠正，说「invalid tool」它只能重试同样的错误。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

from syndica.kernel.context import Context, ContextBlock, ToolCall, TrustLevel
from syndica.kernel.errors import ContractViolation, SyndicaError
from syndica.kernel.hooks import HooksCenter, Junction
from syndica.kernel.tool import Tool, ToolResult

__all__ = ['ToolExecutor']


def _described(exc: BaseException) -> str:
    """异常的可读描述；`__str__` 自己抛异常时退回类型名。

    中止与未执行两条消息在 except handler 内部构造，这里抛出会让它们一条都写不成——配对不变式
    被第三方异常里一个惰性求值的属性击穿，且原始异常被格式化异常顶掉。
    """
    try:
        return f'{type(exc).__name__}: {exc}'
    except Exception:
        return type(exc).__name__


def _tightened(baseline: TrustLevel, derived: ToolResult) -> ToolResult:
    """把派生结果的 trust 收紧到不松于 baseline。"""
    if baseline is TrustLevel.UNTRUSTED and derived.trust is not baseline:
        return replace(derived, trust=TrustLevel.UNTRUSTED)
    return derived


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


def _bad_tool_return(call: ToolCall, tool: Tool, value: object) -> ContractViolation:
    return ContractViolation(
        expected=f'工具 {call.name}（{type(tool).__name__}）的 execute 返回 ToolResult',
        received=f'收到 {type(value).__name__} 类型的 {value!r}',
        next_action=(
            f'修 {type(tool).__name__}.execute 的 return（漏写 return 时拿到的就是 None）；'
            '这个值来自工具自身的返回，不是任何 hook 的改写，注册了几个 hook 都与它无关'
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
            f' 实际: 抛出 {_described(exc)}。'
            ' 下一步: 调整参数后重试，或换用其他方式达成同一目标。'
        ),
        trust=TrustLevel.UNTRUSTED,
    )


def _aborted_result(call: ToolCall, exc: BaseException) -> ToolResult:
    """上抛路径上，失败的那个调用自己的 block。

    措辞不说「工具抛出」：中止也可能来自 hook，而那时工具已经正常跑完了。同一个理由下，这条消息
    只能说副作用未知——声称没有副作用是对已经发生的写入撒谎，resume 会据此跳过重做。
    """
    return ToolResult(
        output=(
            f'期望: {call.name}（id={call.id}）产出一个结果。'
            f' 实际: 本次调用中止于 {_described(exc)}，是否已经产生副作用未知。'
            ' 下一步: 本批调用已在此停下，错误已交给上层处理；'
            '不要重复同一调用，换一种方式达成同一目标。'
        ),
        trust=TrustLevel.UNTRUSTED,
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
    return ToolResult(output=str(substitute), trust=TrustLevel.UNTRUSTED)


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

        # 守卫必须坐在 POST_TOOL_USE 之前：让它去看 post.get('result', result) 就分不出坏值来自
        # 工具还是来自 hook。cast 到 object 是因为要挡的正是违反 execute 标注的运行时取值，不擦
        # 掉静态类型，下面的 isinstance 就成了 pyright 眼里的死代码。
        try:
            returned = cast(object, await tool.execute(**args))
        except SyndicaError:
            # 三件套写给程序员或运维，模型执行不了它的 next_action。
            raise
        except Exception as exc:
            returned = _tool_raised_result(call, exc)
        if not isinstance(returned, ToolResult):
            raise _bad_tool_return(call, tool, returned)
        result = returned

        post = await self._hooks.trigger(
            Junction.POST_TOOL_USE, tool_call=call, tool=tool, args=args, result=result
        )
        rewritten = post.get('result', result)
        if not isinstance(rewritten, ToolResult):
            raise _rewrite_rejected(Junction.POST_TOOL_USE, 'result', ToolResult, rewritten)
        return _tightened(result.trust, rewritten)
