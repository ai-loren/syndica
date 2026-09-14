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

**危险判定的闸坐在 `PRE_TOOL_USE` 之后、执行之前。** 它必须看**真正会被执行的那份参数**——
`PRE_TOOL_USE` 是参数的最后一个写者，把闸放在它之前，一个改写参数的 hook 就能绕过整道闸。放在它之
后不会让 hook 有机会放宽：闸重新判定的正是 hook 自己的产出。`skip=True` 的 hook 顶替了这次调用、
什么都没执行，因此没有可判的东西。处置**只经 `danger.disposition_of` 取**，不直接问 policy：spec
§12.2 第 3 条的强制升级坐在咨询 policy 之前，绕过它这条升级就在整条生产路径上从不运行。

**溯源按批快照算一次，而这个快照只对模型产出的那一份参数成立。** 那一份是模型在本批开始之前一次
产出的，本批中途出现的不可信结果影响不了它；逐次重算会让同一条命令的处置取决于它排在批里第几个，
分段并发之后连这个位置都不确定。**`PRE_TOOL_USE` hook 写过参数时前提就不成立了**——它能把本批刚落
进轨迹的不可信输出展开进参数——那时重取一次并取两者中更严的（spec §13：溯源失败时按最坏情况处理）。
当前形态是「这份轨迹里出现过 `trust=UNTRUSTED` 的 block」——它是上界而非因果链，完整的溯源要接产物
溯源（spec §7.3）与 trace 因果链（§11.1）。**它不交 `None`**：`None` 是「没溯源过」，留在生产路径上
那道升级就恒不命中，而调用方以为有它。

**被阻断的调用仍然产出恰好一个 role='tool' block。** 阻断不是失败，是一次「这条路走不通」的回答，
模型据此换一种做法。它写给模型看，因此按三件套写。阻断与需要确认两种措辞**不可互换**：阻断是确定的
否（换做法），需要确认是还没有人回答（这个 Runtime 上没有确认通道）。塌成一句，模型就分不清该换做法
还是该等。

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
import logging
from dataclasses import replace
from typing import Any, cast

from syndica.kernel.context import Context, ContextBlock, ToolCall, TrustLevel
from syndica.kernel.danger import (
    BlockHighRisk,
    DangerJudge,
    DangerPolicy,
    DangerRequest,
    DangerVerdict,
    Disposition,
    RiskLevel,
    disposition_of,
    validate_danger_options,
)
from syndica.kernel.errors import ConfigError, ContractViolation, SyndicaError
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.tool import Tool, ToolResult, tool_name_disagreement, validate_tool_names

__all__ = ['ToolExecutor']

logger = logging.getLogger(__name__)


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


def _strictest(result: ToolResult, rewrites: list[Any]) -> TrustLevel:
    """工具的结果与链上**每一次**改写里最严的那个 trust。

    只看合并结论会漏掉链中间那次收紧：`modified` 是 last-wins，前一个 hook 标了 UNTRUSTED、后一个
    hook 交出带默认 trust 的新结果时，收紧信息在链上就没了。非 ToolResult 的中间值没有 trust 可读
    （最终值另有守卫拦），跳过。
    """
    for candidate in (result, *rewrites):
        if isinstance(candidate, ToolResult) and candidate.trust is TrustLevel.UNTRUSTED:
            return TrustLevel.UNTRUSTED
    return result.trust


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


def _post_result_slot_rejected(outcome: HookResult) -> ContractViolation:
    """后置切点收到 `HookResult.result`：那是前置拦截的槽位，本切点不读它。"""
    return ContractViolation(
        expected=(f"{Junction.POST_TOOL_USE.value} hook 改写结果的唯一写法是 modified['result']"),
        received=f'收到 result={type(outcome.result).__name__} 类型的 {outcome.result!r}',
        next_action=(
            "把它放进 HookResult.modified['result']；"
            'result 是前置切点跳过原操作时的替代结果槽位，而工具这次已经跑过了。'
            'skip=True 本身合法，它只表示终止后续 hook'
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


def _judge_failed_result(call: ToolCall, exc: Exception) -> ToolResult:
    """危险判定抛出时，失败的那个调用自己的 block。

    与 `_aborted_result` 分开只为一件事：判定无状态、无副作用且坐在执行之前，所以这条路上「有没有
    产生副作用」是**已知**的，说「未知」比事实弱，而 resume 正是按这句话决定要不要重做。处置
    （`DangerPolicy.decide`）不在此列——它由第三方实现，可以做任何事，走的仍是 `_aborted_result`。
    """
    return ToolResult(
        output=(
            f'期望: {call.name}（id={call.id}）产出一个结果。'
            f' 实际: 危险判定在这次调用上抛出 {_described(exc)}；判定坐在执行之前，'
            '工具没有运行，本次调用也没有产生任何副作用。'
            ' 下一步: 本批调用已在此停下，错误已交给上层处理；'
            '不要重复同一调用，先按那条错误修判定报的那处声明或参数。'
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


class _JudgeFailure(Exception):
    """危险判定自己抛出时的内部信号，只决定失败那条 block 的措辞，不出 `execute_tool_calls`。"""

    def __init__(self, original: Exception) -> None:
        super().__init__(original)
        self.original = original


def _provenance(ctx: Context) -> TrustLevel:
    """这一批调用的参数能不能溯源到不可信内容。

    交回 `TrustLevel` 而不是 `TrustLevel | None`：`None` 在 `DangerRequest` 上表示「没溯源过」，
    而这里刚刚看完整份轨迹，报 `None` 就是把做过的检查说成没做过，那道强制升级于是恒不命中。
    """
    if any(block.trust is TrustLevel.UNTRUSTED for block in ctx.current_traj):
        return TrustLevel.UNTRUSTED
    return TrustLevel.TRUSTED


def _strictest_trust(first: TrustLevel, second: TrustLevel) -> TrustLevel:
    """两个溯源结论里更严的那个。批快照是**下界**：一次重算不得洗掉它已经查到的不可信内容。"""
    if TrustLevel.UNTRUSTED in (first, second):
        return TrustLevel.UNTRUSTED
    return TrustLevel.TRUSTED


def _gated_result(call: ToolCall, verdict: DangerVerdict, disposition: Disposition) -> ToolResult:
    """被闸拦下的调用交给模型的那一块。

    两种措辞不可互换：阻断是**确定的否**（换做法），需要确认是**还没有人回答**（这个 Runtime 上
    内核不收确认答复）。塌成一句，模型就分不清该换做法还是该等。

    需要确认这条只断言内核知道的那件事：**内核不消费确认切点的答复**。「没有人会看到它」是内核
    不知道的——`TOOL_CONFIRMATION_REQUIRED` 上可以挂着观察者，而且此刻它已经被叫到过了。

    两条都说「没有产生任何副作用」：闸坐在执行之前，这是它与中止路径唯一能断言的差别。
    """
    if disposition is Disposition.CONFIRM:
        return ToolResult(
            output=(
                f'期望: {call.name}（id={call.id}）在执行前得到人工确认。'
                f' 实际: 规则 {verdict.rule_id} 判定风险等级 {verdict.level.value}'
                f'（{verdict.reason}），处置是等人确认；这次判定已经报给确认切点的观察者，'
                '但这个 Runtime 上没有把答复接回内核的通道，内核不会等它。'
                '本次调用没有执行、也没有产生任何副作用。'
                ' 下一步: 换一种不触发该规则的做法，或把这一步留给能确认它的人；'
                '不要重发同一个调用等它被批准——内核不消费这次确认的答复，重发只会再落回这里。'
            )
        )
    return ToolResult(
        output=(
            f'期望: {call.name}（id={call.id}）不触发危险规则。'
            f' 实际: 规则 {verdict.rule_id} 判定风险等级 {verdict.level.value}'
            f'（{verdict.reason}），本次调用已被阻断，没有执行、也没有产生任何副作用。'
            ' 下一步: 换一种达成同一目标的做法——缩小作用范围、去掉提权、避开不可逆操作；'
            '这次阻断不会被记住，也不存在「此后均可」的放行，同样的调用重来一次仍会被阻断。'
        )
    )


def _report_allowed_low_risk_from_untrusted(call: ToolCall, verdict: DangerVerdict) -> None:
    """spec §12.2 处置矩阵里「低风险 + 可溯源到不可信 → 放行并**记录**」的那条记录。

    判定是纯函数、不持有日志出口，而这里是唯一同时拿得到结论与溯源的地方。没有它，这一格与
    「低 + 不可溯源 → 放行」逐字节相同，那条记录义务无声消失。

    WARNING 而非 INFO：root logger 默认停在 WARNING，INFO 的记录根本不会被创建，而这次放行除了
    这条记录不留任何痕迹。
    """
    logger.warning(
        '期望: 能溯源到不可信内容的调用要么不命中危险规则，要么由人看一眼。'
        ' 实际: %s（id=%s）命中规则 %s（风险等级 %s），而本次调用之前的轨迹里有 %s 的内容；'
        '按 spec §12.2 的处置矩阵，低风险这一格是放行并记录，调用已经放行，'
        '本条是它留下的全部痕迹。'
        ' 下一步: 复核这次调用的参数是不是来自那段不可信内容；'
        '要让这一类调用改为拦下，给 ToolExecutor 换一个把低风险也判 BLOCK 的 DangerPolicy。',
        call.name,
        call.id,
        verdict.rule_id,
        verdict.level.value,
        TrustLevel.UNTRUSTED.value,
    )


def _misregistered(tools: dict[str, Tool], requested: str) -> ConfigError | None:
    """查表落空时，找出一个「名字对得上、注册表键对不上」的工具。

    构造时的校验挡不住此后往这张**活表**里塞条目（`tools` 每次调用时查表，不快照）。只在落空这条
    路上走一遍，正常调用零成本；不报出来的后果是每一次调用都失败而 run 报 COMPLETED。
    """
    for key, tool in tools.items():
        if tool.name == requested:
            return tool_name_disagreement(key, tool)
    return None


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

    `tools` 每次调用时查表，不在构造时快照——因此构造时的身份轴校验只覆盖当时表里的条目，此后塞进
    来的条目由查表落空那条路上的 `_misregistered` 兜住。
    """

    def __init__(
        self,
        tools: dict[str, Tool],
        hooks: HooksCenter,
        *,
        judge: DangerJudge | None = None,
        policy: DangerPolicy | None = None,
    ) -> None:
        validate_tool_names(tools)
        validate_danger_options(judge, policy)
        self._tools = tools
        self._hooks = hooks
        # 两者构造时快照，与活表 `tools` 不同：构造之后改 options 上的字段对本 executor 无效。
        self._judge = judge if judge is not None else DangerJudge()
        self._policy: DangerPolicy = policy if policy is not None else BlockHighRisk()

    async def execute_tool_calls(self, ctx: Context, tool_calls: list[ToolCall]) -> None:
        provenance = _provenance(ctx)
        for index, call in enumerate(tool_calls):
            pending = tool_calls[index + 1 :]
            try:
                result = await self._execute_one(ctx, call, provenance)
            # 判定抛出这条路单独接：它是唯一能断言「工具没运行」的抛出点。
            except _JudgeFailure as signal:
                self._append(ctx, call, _judge_failed_result(call, signal.original))
                self._append_not_executed(ctx, pending, call, signal.original)
                raise signal.original from None
            # Exception 而非 BaseException：取消不该在展开的路上再往轨迹里写东西。
            except Exception as exc:
                self._append(ctx, call, _aborted_result(call, exc))
                self._append_not_executed(ctx, pending, call, exc)
                raise
            self._append(ctx, call, result)

    def _append_not_executed(
        self, ctx: Context, pending: list[ToolCall], failed: ToolCall, exc: BaseException
    ) -> None:
        for call in pending:
            self._append(ctx, call, _not_executed_result(call, failed, exc))

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

    async def _execute_one(
        self, ctx: Context, call: ToolCall, provenance: TrustLevel
    ) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            misregistered = _misregistered(self._tools, call.name)
            if misregistered is not None:
                raise misregistered
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
        # hook 写过这一份参数，批快照的前提（参数产于本批之前）就不成立了：它能把本批刚落进轨迹的
        # 不可信输出展开进参数。看 `rewrites` 而不只比对象身份——交回同一个 dict 对象的 hook 同样
        # 是作者，身份比对看不见它。
        if pre.rewrites('args') or rewritten_args is not args:
            provenance = _strictest_trust(provenance, _provenance(ctx))
        args = cast(dict[str, Any], rewritten_args)

        gated = await self._gated(call, tool, args, provenance)
        if gated is not None:
            return gated

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
        if post.result is not None:
            raise _post_result_slot_rejected(post)
        rewritten = post.get('result', result)
        if not isinstance(rewritten, ToolResult):
            raise _rewrite_rejected(Junction.POST_TOOL_USE, 'result', ToolResult, rewritten)
        return _tightened(_strictest(result, post.rewrites('result')), rewritten)

    async def _gated(
        self, call: ToolCall, tool: Tool, args: dict[str, Any], provenance: TrustLevel
    ) -> ToolResult | None:
        """闸：放行交回 None，拦下交回给模型的那一块。

        处置经 `disposition_of` 取——它先施加 spec §12.2 第 3 条那道关不掉的升级，再咨询宿主
        策略。直接调 `self._policy.decide` 会把升级整个绕过去，而 typing 与运行期都不出声。
        """
        try:
            verdict = self._judge.judge(tool, args)
        except Exception as exc:
            raise _JudgeFailure(exc) from exc
        disposition = disposition_of(
            self._policy, DangerRequest(verdict=verdict, call=call, provenance=provenance)
        )
        if disposition is Disposition.ALLOW:
            if verdict.level is RiskLevel.LOW and provenance is TrustLevel.UNTRUSTED:
                _report_allowed_low_risk_from_untrusted(call, verdict)
            return None
        if disposition is Disposition.CONFIRM:
            await self._hooks.trigger(
                Junction.TOOL_CONFIRMATION_REQUIRED,
                tool_call=call,
                tool=tool,
                args=args,
                verdict=verdict,
            )
        return _gated_result(call, verdict, disposition)
