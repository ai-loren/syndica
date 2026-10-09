"""在 Agent 循环之外执行一次工具调用。

MCP 客户端与托管的 ACP Agent 发起的调用不经过 `ToolExecutor`：没有模型的 tool_call 批次，也没有
`PRE_TOOL_USE`。判定、不可信脚本升级、强制升级与处置必须与执行器对同一组（工具、实参、溯源）
得出同一个结论，否则这条入口就是一道更松的闸。

两处有意比执行器更严，不能并回去：执行期间的调用信任恒为 `UNTRUSTED`（外部写入在文件污点里记成
不可信，即使用这次溯源是可信的）；工具交回的结果信任收紧为不可信。拒绝结果是内核写的说明，保持
可信——标成不可信会把随后把它放进轨迹的会话整段升级。

守卫命中即拒绝，不咨询处置策略，也不看 `untrusted_high_risk`。开发期那两族「永不允许」靠它：
读过不可信内容之后，高风险调用不再问宿主策略。
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Callable, Mapping
from contextvars import Token
from dataclasses import dataclass, replace
from typing import Any, cast

from syndica.kernel.context import (
    _CALL_TRUST,  # pyright: ignore[reportPrivateUsage]
    ToolCall,
    TrustLevel,
)
from syndica.kernel.danger import (
    NO_RULE_MATCHED,
    ConfirmationHandler,
    ConfirmationOutcome,
    ConfirmationRequest,
    DangerJudge,
    DangerPolicy,
    DangerRequest,
    DangerVerdict,
    Disposition,
    RiskLevel,
    _escalated,  # pyright: ignore[reportPrivateUsage]
    disposition_of,
    untrusted_script_verdict,
    validate_confirmation_handler,
)
from syndica.kernel.driving import _DRIVING_AGENT  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.errors import ConfigError, ContractViolation, SyndicaError
from syndica.kernel.ids import peek_key
from syndica.kernel.runtime import Runtime
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tool_executor import (
    _CONFIRMATION_OUTCOMES,  # pyright: ignore[reportPrivateUsage]
    _bad_tool_return,  # pyright: ignore[reportPrivateUsage]
    _confirmation_result,  # pyright: ignore[reportPrivateUsage]
    _gated_result,  # pyright: ignore[reportPrivateUsage]
    _report_allowed_low_risk_from_untrusted,  # pyright: ignore[reportPrivateUsage]
    _script_changed_result,  # pyright: ignore[reportPrivateUsage]
    _tool_raised_result,  # pyright: ignore[reportPrivateUsage]
    _untrusted_executed_scripts,  # pyright: ignore[reportPrivateUsage]
)
from syndica.kernel.trace import SpanKind
from syndica.kernel.tracing import (
    _OpenSpan,  # pyright: ignore[reportPrivateUsage]
    _Tracing,  # pyright: ignore[reportPrivateUsage]
)

__all__ = ['ExternalCallPolicy', 'call_external']


@dataclass(frozen=True)
class ExternalCallPolicy:
    """一次外部调用用的判定、处置与守卫。

    `guard` 命中即无条件拒绝。`confirm` 为 None 时，处置成确认按拒绝交回，不问人。
    """

    judge: DangerJudge
    policy: DangerPolicy
    untrusted_high_risk: Disposition = Disposition.BLOCK
    guard: DangerJudge | None = None
    confirm: ConfirmationHandler | None = None

    def __post_init__(self) -> None:
        judge = cast(object, self.judge)
        if not isinstance(judge, DangerJudge):
            raise ConfigError(
                expected='ExternalCallPolicy.judge 是 DangerJudge 的实例',
                received=f'收到 {judge!r}，类型是 {type(judge).__name__}',
                next_action='传 DangerJudge(rules) 或 DangerJudge()；不要传规则元组或类本身',
            )
        policy = cast(object, self.policy)
        decide = getattr(policy, 'decide', None)
        if not callable(decide):
            raise ConfigError(
                expected='ExternalCallPolicy.policy 有 decide(request) 方法',
                received=f'收到 {policy!r}，类型是 {type(policy).__name__}',
                next_action='传 BlockHighRisk()、AllowAll() 或 RuleTablePolicy(...)',
            )
        escalation = cast(object, self.untrusted_high_risk)
        if not isinstance(escalation, Disposition):
            raise ConfigError(
                expected='ExternalCallPolicy.untrusted_high_risk 是 Disposition 成员',
                received=f'收到 {escalation!r}，类型是 {type(escalation).__name__}',
                next_action=(
                    '传 Disposition.BLOCK / Disposition.CONFIRM / Disposition.ALLOW；'
                    '不要传 True 或 "block"：bool 会被当成整数，字符串不等于任何成员，'
                    '写错的升级处置会把高风险放行'
                ),
            )
        guard = cast(object, self.guard)
        if guard is not None and not isinstance(guard, DangerJudge):
            raise ConfigError(
                expected='ExternalCallPolicy.guard 是 DangerJudge 或 None',
                received=f'收到 {guard!r}，类型是 {type(guard).__name__}',
                next_action='传只含「永不允许」规则的 DangerJudge，或留成 None',
            )
        validate_confirmation_handler(self.confirm)


def _require_text(value: object, field: str, shape: str) -> str:
    if isinstance(value, str) and value.strip() and value == value.strip():
        return value
    raise ConfigError(
        expected=f'{field} 是不带首尾空白的非空字符串（{shape}）',
        received=f'收到 {field}={value!r}，类型是 {type(value).__name__}',
        next_action=f'传入 {field} 的稳定原文；它会写进 trace，空白与空串对不上后续的续接',
    )


def _check_call(
    runtime: object,
    tool: object,
    args: object,
    caller: object,
    agent_id: object,
    provenance: object,
) -> None:
    if runtime is not None and not isinstance(runtime, Runtime):
        raise ConfigError(
            expected='call_external 的 runtime 是 Runtime 或 None',
            received=f'收到 {runtime!r}，类型是 {type(runtime).__name__}',
            next_action=(
                '传入这次会话的 Runtime（要记 trace、按它的工作区认不可信脚本）；没有就传 None'
            ),
        )
    if isinstance(tool, type) or not isinstance(tool, Tool):
        raise ConfigError(
            expected='call_external 的 tool 是 Tool 的实例',
            received=f'收到 {tool!r}，类型是 {type(tool).__name__}',
            next_action='传入工具实例，不要传类本身',
        )
    if isinstance(args, (str, bytes)) or not isinstance(args, Mapping):
        raise ConfigError(
            expected='call_external 的 args 是「参数名 → 值」的映射',
            received=f'收到 {type(args).__name__} 类型的 {args!r}',
            next_action='传 dict，例如 {"command": "echo hi"}；字符串不是参数对象',
        )
    for key in cast(Mapping[object, object], args):
        if not isinstance(key, str):
            raise ConfigError(
                expected='call_external 的 args 的键是参数名字符串',
                received=f'收到键 {key!r}，类型是 {type(key).__name__}',
                next_action='用工具 schema 里的参数名做键',
            )
    _require_text(caller, 'caller', 'mcp-client、acp-agent:<名>')
    if agent_id is not None:
        _require_text(agent_id, 'agent_id', '这次调用所属 Agent 的 id')
    if not isinstance(provenance, TrustLevel):
        raise ConfigError(
            expected='call_external 的 provenance 是 TrustLevel.TRUSTED 或 TrustLevel.UNTRUSTED',
            received=f'收到 {provenance!r}，类型是 {type(provenance).__name__}',
            next_action=(
                '不传则按不可信；不要传 None 或字符串。None 表示没溯源过，强制升级会静默消失'
            ),
        )


def _mark(span: _OpenSpan | None, outcome: str) -> None:
    if span is not None:
        span.attributes['tool.outcome'] = outcome


def _mark_danger(
    span: _OpenSpan | None,
    verdict: DangerVerdict,
    provenance: TrustLevel,
    decided_by: str,
    disposition: Disposition,
) -> None:
    if span is None:
        return
    # 键序与 ToolExecutor 写进 span 的顺序一致：两边的 danger.* 才能逐项对照。
    span.attributes.update(
        {
            'danger.rule_ids': (
                [] if verdict.rule_id == NO_RULE_MATCHED.rule_id else [verdict.rule_id]
            ),
            'danger.risk': verdict.level.value,
            'danger.provenance': provenance.value,
            'danger.decided_by': decided_by,
            'danger.disposition': disposition.value,
        }
    )


def _guard_result(call: ToolCall, verdict: DangerVerdict) -> ToolResult:
    """守卫拒绝的说明保持可信：这段文字是内核写的，不是工具读到的外部内容。"""
    return ToolResult(
        output=(
            f'期望: {call.name}（id={call.id}）不触发无条件拒绝的守卫规则。'
            f' 实际: 守卫规则 {verdict.rule_id} 判定风险等级 {verdict.level.value}'
            f'（{verdict.reason}），本次调用已被拒绝，没有执行、也没有产生任何副作用。'
            ' 下一步: 换一种不触发该守卫的做法；这道拒绝不咨询处置策略，'
            '也不存在「此后均可」的放行，同样的调用重来一次仍会被拒绝。'
        ),
        trust=TrustLevel.TRUSTED,
    )


def _script_trust(
    runtime: Runtime | None,
) -> Callable[[str], TrustLevel | None] | None:
    if runtime is None or runtime.workspace is None:
        return None
    return runtime.workspace.script_trust


async def _execute(
    tool: Tool,
    call: ToolCall,
    args: dict[str, Any],
    span: _OpenSpan | None,
    tracing: _Tracing | None,
) -> tuple[ToolResult, str]:
    """在不可信的调用信任下执行。写入因此记成不可信，与这次溯源结论无关。

    `SyndicaError` 在复位调用信任之后原样上抛：三件套写给调用方，收成结果会让协议适配层
    把它当成工具的普通输出交回去。
    """
    token = _CALL_TRUST.set(TrustLevel.UNTRUSTED)
    try:
        try:
            returned = cast(object, await tool.execute(**args))
        except SyndicaError as exc:
            if span is not None and tracing is not None:
                tracing.tool_result(span, None, exc, arguments=args)
            raise
        except Exception as exc:
            if span is not None and tracing is not None:
                tracing.tool_result(span, None, exc, arguments=args)
            return _tool_raised_result(call, exc, TrustLevel.UNTRUSTED), 'error'
    finally:
        _CALL_TRUST.reset(token)
    if not isinstance(returned, ToolResult):
        raise _bad_tool_return(call, tool, returned)
    result = (
        returned
        if returned.trust is TrustLevel.UNTRUSTED
        else replace(returned, trust=TrustLevel.UNTRUSTED)
    )
    if span is not None and tracing is not None:
        tracing.tool_result(span, result, None, arguments=args)
    return result, 'ok'


async def _finish_execute(
    tool: Tool,
    call: ToolCall,
    args: dict[str, Any],
    span: _OpenSpan | None,
    tracing: _Tracing | None,
) -> ToolResult:
    try:
        result, outcome = await _execute(tool, call, args, span, tracing)
    except SyndicaError:
        _mark(span, 'error')
        raise
    _mark(span, outcome)
    return result


async def _confirmed(
    runtime: Runtime | None,
    tool: Tool,
    call: ToolCall,
    args: dict[str, Any],
    verdict: DangerVerdict,
    provenance: TrustLevel,
    policy: ExternalCallPolicy,
    agent_id: str | None,
    scripts: list[str],
    span: _OpenSpan | None,
    tracing: _Tracing | None,
) -> ToolResult:
    confirm = policy.confirm
    if confirm is None:
        _mark(span, 'no_channel')
        return _gated_result(call, verdict, Disposition.CONFIRM)
    if runtime is None or agent_id is None:
        raise ConfigError(
            expected='处置为 CONFIRM 且装了 confirm 时，call_external 同时拿到 Runtime 与 agent_id',
            received=(
                f'runtime={"None" if runtime is None else type(runtime).__name__}，'
                f'agent_id={agent_id!r}'
            ),
            next_action=(
                '把这次会话的 Runtime 和所属 Agent 的 id 传进来，确认才能送到那个 Agent 的邮箱；'
                '没有人可问就把 ExternalCallPolicy.confirm 留成 None，按拒绝处理'
            ),
        )
    try:
        arguments = json.dumps(args, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(
            expected='确认前的工具参数可编码为 JSON 快照',
            received=f'{call.name}（id={call.id}）参数编码失败：{type(exc).__name__}: {exc}',
            next_action='让参数只含 JSON 值；无法稳定展示给人的参数不得执行',
        ) from exc
    request = ConfirmationRequest(
        request_id=uuid.uuid4().hex,
        agent_id=agent_id,
        tool_call_id=call.id,
        tool_name=call.name,
        arguments=arguments,
        verdict=verdict,
        request_ref=peek_key('request'),
        untrusted=provenance is TrustLevel.UNTRUSTED,
    )
    outcome = cast('object', await confirm.confirm(runtime, request))
    if not isinstance(outcome, ConfirmationOutcome):
        raise ContractViolation(
            expected='confirmation handler 交回 ConfirmationOutcome',
            received=f'收到 {outcome!r}，类型是 {type(outcome).__name__}',
            next_action=(
                '交回 ConfirmationOutcome.APPROVED / REJECTED / TIMEOUT / '
                'PREEMPTED / ABORTED 之一，不要用 bool 或字符串'
            ),
        )
    if outcome is not ConfirmationOutcome.APPROVED:
        _mark(span, _CONFIRMATION_OUTCOMES[outcome])
        return _confirmation_result(call, verdict, request, outcome)
    execution_args = cast('dict[str, Any]', json.loads(arguments))
    changed = [
        script
        for script in _untrusted_executed_scripts(tool, execution_args, _script_trust(runtime))
        if script not in scripts
    ]
    if changed:
        _mark(span, 'blocked')
        return _script_changed_result(call, changed)
    return await _finish_execute(tool, call, execution_args, span, tracing)


async def call_external(
    runtime: Runtime | None,
    tool: Tool,
    args: Mapping[str, object],
    *,
    caller: str,
    agent_id: str | None,
    policy: ExternalCallPolicy,
    provenance: TrustLevel = TrustLevel.UNTRUSTED,
) -> ToolResult:
    """判定 → 守卫 → 处置 → 在不可信调用信任下执行 → 结果收紧为不可信 → 记 TOOL_CALL span。

    被拒绝时交回说明了规则 ID、等级、原因与下一步的 `ToolResult`，不抛。工具自己抛的
    `SyndicaError` 原样上抛；其余异常收成不可信的结果。`agent_id` 给定时，这次执行按那个
    Agent 的工作区路由（隔离副本因此接得住）。
    """
    if not isinstance(cast(object, policy), ExternalCallPolicy):
        raise ConfigError(
            expected='call_external 的 policy 是 ExternalCallPolicy',
            received=f'收到 {policy!r}，类型是 {type(policy).__name__}',
            next_action='用 ExternalCallPolicy(judge=..., policy=...) 装好再传入',
        )
    _check_call(runtime, tool, args, caller, agent_id, provenance)
    owned: dict[str, Any] = dict(args)
    call = ToolCall(id=uuid.uuid4().hex, name=tool.name, arguments='')
    tracing = None if runtime is None else runtime._tracing  # pyright: ignore[reportPrivateUsage]
    span_cm = (
        contextlib.nullcontext(None)
        if tracing is None
        else tracing.span(
            SpanKind.TOOL_CALL,
            ('external', caller, call.id),
            name=call.name,
            agent_id=agent_id or '',
            attributes={
                'tool.name': call.name,
                'tool.call_id': call.id,
                'tool.index': 0,
                'tool.caller': caller,
            },
        )
    )
    drive: Token[str | None] | None = None
    if agent_id is not None:
        drive = _DRIVING_AGENT.set(agent_id)
    try:
        with span_cm as span:
            return await _decide(
                runtime,
                tool,
                call,
                owned,
                policy,
                provenance,
                agent_id,
                span,
                tracing,
            )
    finally:
        if drive is not None:
            _DRIVING_AGENT.reset(drive)


async def _decide(
    runtime: Runtime | None,
    tool: Tool,
    call: ToolCall,
    args: dict[str, Any],
    policy: ExternalCallPolicy,
    provenance: TrustLevel,
    agent_id: str | None,
    span: _OpenSpan | None,
    tracing: _Tracing | None,
) -> ToolResult:
    try:
        verdict = policy.judge.judge(tool, args)
    except Exception:
        _mark(span, 'judge_failed')
        raise
    scripts = _untrusted_executed_scripts(tool, args, _script_trust(runtime))
    if scripts:
        provenance = TrustLevel.UNTRUSTED
        if verdict.level is not RiskLevel.HIGH:
            verdict = untrusted_script_verdict(scripts)
    if policy.guard is not None:
        try:
            guarded = policy.guard.judge(tool, args)
        except Exception:
            _mark(span, 'judge_failed')
            raise
        if guarded is not NO_RULE_MATCHED:
            _mark_danger(span, guarded, provenance, 'guard', Disposition.BLOCK)
            _mark(span, 'blocked')
            return _guard_result(call, guarded)
    request = DangerRequest(verdict=verdict, call=call, provenance=provenance)
    disposition = disposition_of(
        policy.policy, request, untrusted_high_risk=policy.untrusted_high_risk
    )
    _mark_danger(
        span,
        verdict,
        provenance,
        'flag' if _escalated(request) else 'policy',
        disposition,
    )
    if disposition is Disposition.ALLOW:
        if verdict.level is RiskLevel.LOW and provenance is TrustLevel.UNTRUSTED:
            _report_allowed_low_risk_from_untrusted(call, verdict)
        return await _finish_execute(tool, call, args, span, tracing)
    if disposition is Disposition.CONFIRM:
        return await _confirmed(
            runtime,
            tool,
            call,
            args,
            verdict,
            provenance,
            policy,
            agent_id,
            scripts,
            span,
            tracing,
        )
    _mark(span, 'blocked')
    return _gated_result(call, verdict, disposition)
