"""工具执行。

**一轮内的多个调用分段执行：段内并发，段与段之间串行。** 只有**相邻**的安全调用会被并到一段；一个
不安全的调用把批次切开，它两侧的调用因此不会跨过它一起跑——不安全的那个可能正是要改它们都会读的
东西。段的大小另有上限（`max_concurrency`）。

**安全判定从严（spec §5.6）**：工具不存在、参数解析失败、命中任何危险规则——一律判不安全，排除这
三种之后才轮到工具自己回答。代价不对称：并发跑两个改同一份文件的工具、或并发跑一条本该有人看一眼
的危险命令，后果不可逆；串行的代价只是慢一点。

**分段是调度决策，闸是安全决策，两者看的参数不同。** 分段必须在任何 hook 跑之前算完（那时才有一整
批可分），因此它看的是**模型发出来的**那份参数；闸看的是 `PRE_TOOL_USE` 改写之后、真正会被执行的那
份。后果是改写参数的 hook 不会让批次重新分段，这是安全的那一侧：被闸拦下的调用根本不执行，所以
「它曾经和别人并发过」产生不了任何副作用。**闸不复用分段那次判定的结论**——「参数有没有被 hook 改
过」这个判据本身不完备（顶层的就地改已被只读视图挡住，嵌套容器里的仍然看不见），复用一次过时的结论
就等于让闸去判一份不会被执行的参数。

**分段只问判定，不问处置。** 判定是纯规则、无副作用，问几遍都一样；`DangerPolicy` 由宿主实现，可以
计数、可以记录，一次调用问它两遍（而且两遍看的参数还不同）就是在骗它。判据因此取「命中任何危险规则
即不安全」，它严于「需要人工授权」，且在 `AllowAll` 这类宿主下仍然起作用。

**结果块始终按调用顺序落进轨迹，与完成顺序无关。** 配对靠的是相邻性，而 provider 会拒掉配对断裂的
消息。

**一次工具调用恰好产出一个 role='tool' block。** provider 会拒掉一个声明了 N 个调用、后面却跟着少于
N 条结果的 assistant 消息，所以工具不存在、参数非法、执行抛异常、异常上抛这四条路都必须产出 block。
上抛前尚未执行的调用同样各得一个 block，如实写明「没有执行」——轨迹会被持久化，伪造结果会让 resume
与读轨迹的父 Agent 都按一件没发生过的事往下走。反方向也是撒谎：中止的那个调用可能在工具跑完之后才
失败（POST hook 抛异常就走这条路），因此它的 block 只说副作用未知，不得声称没有副作用；两条消息不可
互换，resume 正是按它们决定要不要重做。**分段判定本身因此不得抛出**：它坐在任何 block 落地之前，一
抛就是整批一个 block 都没有。答不上来的一律判不安全。

**同段里失败的每一个都如实说自己中止了。** `asyncio.gather(return_exceptions=True)` 不取消兄弟——一
个失败其余照跑到完，所以它们真的跑过、副作用未知，与「从未执行」两条消息不可互换。上抛的是**调用
顺序**最靠前的那个失败，不是最先失败的那个：同一批调用跑两次必须留下同一份轨迹。

**被取消的一批不补 block。** `CancelledError` 这类 BaseException 在批中途到达时直接上抛，展开路上不
再写轨迹，于是这一批留下的 block **少于** assistant 声明的调用数。消费持久化轨迹的一方（resume、读
轨迹的父 Agent）必须自己收拾这段不配对的 assistant 消息——取消可以落在 execute_tool_calls 被进入之
前，这个洞在本层补不上。**并发段要先整段扫一遍取消再写任何 block**：`gather(return_exceptions=True)`
把子协程自己抛的 `CancelledError` 收进结果列表而不是上抛，照结果处理就等于把一次取消写成一块工具
结果。

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
**在多元素段里这次重取本身就是一次失败的溯源**：hook 能从旁路拿到同段兄弟的产出，而兄弟的 block 要
等整段跑完才落进轨迹，重取在轨迹里一个字都看不到；因此那里直接按最坏情况取 `UNTRUSTED`，不再重取。
代价是「hook 改写参数 ∧ 多元素段」这个交集上多判（B-18），而少判的后果是那道关不掉的升级与 §12.2
低风险那一格的记录义务**静默**失效——**同一批输入在 `max_concurrency=1` 与 `=N` 下必须得出同一个
安全结论**，一个调度配置项改不掉安全结论。
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

**交给 `PRE_TOOL_USE` 的那份 `args` 是只读视图，就地写当场失败。** 就地改而不声明
`modified['args']` 也能改变真正被执行的参数，却不进 `HookResult.history`——于是上面那个「hook 是
这份参数的作者」的判据看不见它，重取不发生，那道关不掉的升级按一份已经失效的批快照放行。视图只护
住顶层：`args['config']['x'] = 1` 仍然逃得掉，见 `hooks.readonly`。

`modified` 通道改写的是 kwargs，类型必须与传进去的一致（`args` 是 dict，`result` 是 ToolResult）；
不合就是 hook 的编程错误，报 ContractViolation 而不是静默丢弃作者的意图。工具自己返回非 ToolResult
是另一件事，必须报另一条消息：混成一条会让零 hook 的调用方被指去修一个不存在的 hook。`skip` 带出的
`result` 是第三件事：它是通用的 Any 槽位，非 ToolResult 的值原样转成 output，并按 UNTRUSTED 落盘
——裸值上没有标记可读，而假定可信是不安全的那一侧；要声明可信就交出一个 ToolResult。

每一条写进 block 的失败消息都按错误三件套写给模型看：模型是它们的第一读者，说清「可用工具有哪些」
「参数 schema 是什么」它就能自我纠正，说「invalid tool」它只能重试同样的错误。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
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
from syndica.kernel.hooks import HookResult, HooksCenter, Junction, ReadOnlyKwarg
from syndica.kernel.tool import Tool, ToolResult, tool_name_disagreement, validate_tool_names

__all__ = ['DEFAULT_MAX_TOOL_CONCURRENCY', 'ToolExecutor', 'validate_max_concurrency']

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOOL_CONCURRENCY = 4


def validate_max_concurrency(value: object, field: str) -> None:
    """校验一段里最多同时跑几个工具。收下这个上限的每一处都要过它。

    `field` 由调用方给出：`AgentOptions` 上它叫 `max_tool_concurrency`，`ToolExecutor` 上叫
    `max_concurrency`。报错指着另一个名字，宿主会去找一个自己没写过的参数。

    参数标注为 `object`：要挡的正是违反标注的运行时取值。`bool` 单独挡，且挡在数值检查**之前**
    ——`bool` 是 `int` 的子类，而 YAML 1.1 把 `yes` / `on` / `true` 读成 `True`，于是 `True` 会被
    当成 1，一份声明式配置里的 `max_tool_concurrency: yes` 静默变成全串行且不报任何错。
    """
    if isinstance(value, bool):
        raise ConfigError(
            expected=f'{field} 是正整数：一段里最多同时跑几个工具',
            received=f'收到布尔值 {value!r}',
            next_action=(
                f'传一个 >= 1 的 int，要完全串行就传 1；'
                f'bool 是 int 的子类而 YAML 把 yes / on / true 读成 True，'
                f'不挡的话 {field}: yes 会静默变成全串行'
            ),
        )
    if not isinstance(value, int):
        raise ConfigError(
            expected=f'{field} 是正整数：一段里最多同时跑几个工具',
            received=f'收到 {value!r}，类型是 {type(value).__name__}',
            next_action=f'把 {field} 传成 int；要完全串行就传 1',
        )
    if value < 1:
        raise ConfigError(
            expected=f'{field} 是正整数：一段里最多同时跑几个工具',
            received=f'收到 {field}={value}',
            next_action=f'把 {field} 传成 >= 1 的值；要完全串行就传 1，0 与负数不表示「不限」',
        )


def _parsed_arguments(call: ToolCall) -> dict[str, Any] | None:
    """解析一次调用的参数；解析不出来交回 None。

    分段与执行各解析一次而不共享一份：分段的产物只用来决定「谁能和谁一起跑」，执行路径必须从原始
    字符串重新走一遍完整的错误分支——不同的失败要给模型不同的措辞。
    """
    if not call.arguments.strip():
        return {}
    try:
        decoded = cast('object', json.loads(call.arguments))
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return cast('dict[str, Any]', decoded)


def _segments(safety: Sequence[bool], limit: int) -> list[list[int]]:
    """把一批调用切成可以一起跑的段，交回逐段的下标列表。

    纯函数，于是「怎么切」这件事可以脱离工具、hook 与事件循环单独验证。

    每个下标恰好落进一段，段内与段间都保持原序：`execute_tool_calls` 靠这一条把「已经写过 block
    的调用数」当成尚未执行那一段的起点。
    """
    segments: list[list[int]] = []
    current: list[int] = []
    for index, is_safe in enumerate(safety):
        if not is_safe:
            if current:
                segments.append(current)
                current = []
            segments.append([index])
            continue
        current.append(index)
        if len(current) == limit:
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return segments


def _without_cancellation(
    outcomes: Sequence[ToolResult | BaseException],
) -> list[ToolResult | Exception]:
    """整段扫一遍取消：非 `Exception` 的 `BaseException` 直接上抛，展开路上一个 block 都不补。

    `asyncio.gather(return_exceptions=True)` 把子协程自己抛的 `CancelledError` **收进结果列表**而
    不是上抛（3.11 收到的还是一个丢了原消息的新实例）。照结果处理就等于把一次取消写成一块工具
    结果，而取消的语义恰恰是「这一批没跑完」。
    """
    settled: list[ToolResult | Exception] = []
    for outcome in outcomes:
        if isinstance(outcome, Exception):
            settled.append(outcome)
        elif isinstance(outcome, BaseException):
            raise outcome
        else:
            settled.append(outcome)
    return settled


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
        ' 实际: %s（id=%s）命中规则 %s（风险等级 %s），而本次调用的溯源结论是 %s；'
        '按 spec §12.2 的处置矩阵，低风险这一格是放行并记录，调用已经放行，'
        '本条是它留下的全部痕迹。'
        ' 下一步: 复核这次调用的参数是不是来自不可信内容；'
        '要让这一类调用改为拦下，给 ToolExecutor 换一个把低风险也判 BLOCK 的 DangerPolicy。',
        call.name,
        call.id,
        verdict.rule_id,
        verdict.level.value,
        TrustLevel.UNTRUSTED.value,
    )


def _report_unanswered_concurrency(tool: Tool, call: ToolCall, exc: Exception) -> None:
    """工具的 `is_concurrency_safe` 自己坏掉时留下的唯一痕迹。

    判定不得抛出，所以答不上来一律判不安全——而串行既不报错也不改结果，这个工具于是**永久**串行，
    没有这条记录，一次声明违约（抛出、或签名根本不收 `args`）留下的痕迹是零。

    危险判定抛出时**不**走这里：那次失败会在闸上原样再现，而那里有配对的 block 与一次上抛。

    WARNING 而非 INFO：root logger 默认停在 WARNING，INFO 的记录根本不会被创建。
    """
    logger.warning(
        '期望: 工具的 is_concurrency_safe 是纯函数：收下这次调用的 args，交回 True 或 False。'
        ' 实际: %s.is_concurrency_safe 在 %s（id=%s）上抛出 %s；判定坐在任何 block 落地之前、'
        '因此不得抛出，这次调用于是被判不安全，该工具在本批里只会单独跑。'
        ' 下一步: 让它收一个 dict 参数并且不抛出——它现在坐在每次工具调用的必经之路上，'
        '也会被问到本批里根本不会执行的调用；确认不了本次调用不写任何共享状态就交回 False。',
        type(tool).__name__,
        call.name,
        call.id,
        type(exc).__name__,
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
    """分段执行一批工具调用，把每个调用的结果作为 role='tool' block 追加进 ctx。

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
        max_concurrency: int = DEFAULT_MAX_TOOL_CONCURRENCY,
    ) -> None:
        validate_tool_names(tools)
        validate_danger_options(judge, policy)
        validate_max_concurrency(max_concurrency, 'max_concurrency')
        self._tools = tools
        self._hooks = hooks
        # 三者构造时快照，与活表 `tools` 不同：构造之后改 options 上的字段对本 executor 无效。
        self._judge = judge if judge is not None else DangerJudge()
        self._policy: DangerPolicy = policy if policy is not None else BlockHighRisk()
        self._max_concurrency = max_concurrency

    async def execute_tool_calls(self, ctx: Context, tool_calls: list[ToolCall]) -> None:
        provenance = _provenance(ctx)
        safety = [self._concurrency_safe(call) for call in tool_calls]
        handled = 0
        failure: Exception | None = None
        failed_call: ToolCall | None = None
        for segment in _segments(safety, self._max_concurrency):
            calls = [tool_calls[index] for index in segment]
            outcomes = _without_cancellation(await self._run_segment(ctx, calls, provenance))
            for call, outcome in zip(calls, outcomes, strict=True):
                # 判定抛出这条路单独接：它是唯一能断言「工具没运行」的抛出点。
                if isinstance(outcome, _JudgeFailure):
                    self._append(ctx, call, _judge_failed_result(call, outcome.original))
                    if failure is None:
                        failure, failed_call = outcome.original, call
                elif isinstance(outcome, Exception):
                    self._append(ctx, call, _aborted_result(call, outcome))
                    if failure is None:
                        failure, failed_call = outcome, call
                else:
                    self._append(ctx, call, outcome)
                handled += 1
            if failure is not None:
                break
        if failure is not None and failed_call is not None:
            self._append_not_executed(ctx, tool_calls[handled:], failed_call, failure)
            raise failure

    async def _run_segment(
        self, ctx: Context, calls: list[ToolCall], provenance: TrustLevel
    ) -> list[ToolResult | BaseException]:
        """跑一段：段内并发，交回与 `calls` 同序的结果或异常。

        单元素的段**不进 `asyncio.gather`**：那会把协程包成 Task，于是这次调用换了取消域、也拿到
        一份 contextvars 的副本。默认工具都不可并发（`Tool.is_concurrency_safe` 交回 False），单
        元素段是最常见的形态，不该因为引入分段就改掉它的执行环境。
        """
        if len(calls) == 1:
            try:
                return [await self._execute_one(ctx, calls[0], provenance, has_siblings=False)]
            # 收进结果列表的只有 Exception，取消沿这条路直接上抛。并发那条路做不到这件事
            # （gather 把子协程的取消一并收进列表），所以两条路都还要过 `_without_cancellation`
            # ——「取消不写 block」这条只由它保证。
            except Exception as exc:
                return [exc]
        return await asyncio.gather(
            *(self._execute_one(ctx, call, provenance, has_siblings=True) for call in calls),
            return_exceptions=True,
        )

    def _concurrency_safe(self, call: ToolCall) -> bool:
        """这次调用能不能与同一批里相邻的调用一起跑。

        判定坐在任何 block 落地之前，因此**不得抛出**：一抛就是整批一个 block 都没有，配对不变式
        被工具或危险判定里的一个异常击穿。答不上来一律判不安全——危险判定的那次失败会在闸上原样
        再现（那里有配对的 block），工具自己答不上来则只是被串行执行并留下一条 WARNING。

        这里兜住的是危险判定那次抛出，不出声：它会在闸上原样再现，那里有配对的 block 与一次上抛。
        """
        try:
            return self._joinable(call)
        except Exception:
            return False

    def _joinable(self, call: ToolCall) -> bool:
        """从严的四道关，逐条见模块 docstring。"""
        tool = self._tools.get(call.name)
        if tool is None:
            return False
        args = _parsed_arguments(call)
        if args is None:
            return False
        # 判定必须先于工具的自我声明：命中危险规则的调用无论工具怎么答都不安全，先问工具就是让
        # 第三方代码（会计数、会抛）跑在一次答案用不上的判断上。
        if self._judge.judge(tool, args).level is not RiskLevel.NONE:
            return False
        return self._tool_says_safe(tool, call, args)

    def _tool_says_safe(self, tool: Tool, call: ToolCall, args: dict[str, Any]) -> bool:
        """问工具自己这次调用能不能并发。

        只有 `True` 才算安全：`is_concurrency_safe` 由工具实现，一个真值对象在这里被读成「可以并
        发」，而这个方向的错是不可逆的。

        它坏掉时判不安全（串行），方向是安全的那一侧；但串行不报错也不改结果，所以那条 WARNING
        是这次声明违约留下的全部痕迹。
        """
        try:
            return cast('object', tool.is_concurrency_safe(args)) is True
        except Exception as exc:
            _report_unanswered_concurrency(tool, call, exc)
            return False

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
        self, ctx: Context, call: ToolCall, provenance: TrustLevel, *, has_siblings: bool
    ) -> ToolResult:
        """跑一次调用。

        `has_siblings` 没有默认值：它决定 hook 改写参数之后这次调用按什么溯源结论过闸，漏传一处
        就是那道关不掉的升级在那条路上静默失效，而默认值会把这件事变成静态检查看不见的疏漏。它由
        `_run_segment` 从决定要不要进 `gather` 的那同一个 `len(calls)` 判断算出，两者不可能错位。
        """
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

        # 只读视图只活在这一次触发里：就地改一份没有声明的参数躲得过下面那个作者判据，而那正是
        # 决定要不要重算溯源的依据。只包最初这一份就够——一旦链上有人声明过改写，重取已经触发，
        # 此后再有人就地改也改不掉那个结论。
        handed = ReadOnlyKwarg(Junction.PRE_TOOL_USE, 'args', args)
        pre = await self._hooks.trigger(
            Junction.PRE_TOOL_USE, tool_call=call, tool=tool, args=handed
        )
        if pre.skip:
            return _skipped_result(call, pre.result)
        rewritten_args = pre.get('args', handed)
        if not isinstance(rewritten_args, dict):
            raise _rewrite_rejected(Junction.PRE_TOOL_USE, 'args', dict, rewritten_args)
        # hook 写过这一份参数，批快照的前提（参数产于本批之前）就不成立了：它能把本批刚落进轨迹的
        # 不可信输出展开进参数。看 `rewrites` 而不只比对象身份——交回同一个 dict 对象的 hook 同样
        # 是作者，身份比对看不见它。
        if pre.rewrites('args') or rewritten_args is not handed:
            # 多元素段里这次重取**结构上就是一次失败的溯源**：hook 能从旁路拿到同段兄弟的产出并
            # 展开进参数，而兄弟的 block 要等整段跑完才落进轨迹，重取在轨迹里一个字都看不到。
            # spec §13：溯源失败时按最坏情况处理。少判的方向不可回收——那道关不掉的升级会静默失
            # 效，§12.2 低风险那一格的记录义务也会一起无声消失。
            provenance = (
                TrustLevel.UNTRUSTED
                if has_siblings
                else _strictest_trust(provenance, _provenance(ctx))
            )
        # 拆掉只读视图：闸、工具与 POST 切点拿到的永远是一份普通 dict。留着它，一个 POST hook 的
        # 就地写会撞上一条指名 pre_tool_use 的错误——而那个切点根本不读 modified['args']。
        args = dict(cast(dict[str, Any], rewritten_args))

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
