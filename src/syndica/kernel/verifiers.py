"""L2 输出验收 + L3 结果验收（§7.5），以及把它们挂上 COMPLETION_PROPOSED 的 hook。

两个伴生都经伴生运行器跑（受限工具、绑主 Agent 的 Portal、独立预算、不进注册表）。L2 判定**保守**：
拿不准就判没做完（宁可多做一轮也不早收工）。L3 逐条判 Rubric，裁决由内核 `derive_verdict` 推导——不收
验收器自报的状态（评审 B4）。

fail-open（§10）：验收器自身失败（模型错、超时、回复解析不了）只接 `Exception`、兜成 `unverified`：
放行但显式标记、绝不伪装成通过；`CancelledError` 不是 `Exception`，照常上抛（abort → ABORTED）。超时
用 `asyncio.timeout`、不用 `wait_for`：后者在内层恰好就绪时会把到达的取消吃掉（AGENTS）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from syndica.kernel.acceptance import (
    Anchor,
    AnchorSource,
    OutcomeKind,
    Rubric,
    RubricOutcome,
    Verdict,
    VerdictStatus,
    derive_verdict,
    unfinished_verdict,
)
from syndica.kernel.companion import run_companion
from syndica.kernel.context import Context, ContextBlock, TrustLevel, provenance_of
from syndica.kernel.hooks import HookResult
from syndica.kernel.llm import LLM
from syndica.kernel.protocols.rubric import RubricStore
from syndica.kernel.workspace import Portal

__all__ = ['OutputCheck', 'acceptance_hook', 'render_transcript', 'verify_outcome', 'verify_output']

logger = logging.getLogger(__name__)

_BLOCK_CHARS = 2000
_TOTAL_CHARS = 20000
_SOURCES = {m.value: m for m in AnchorSource}

_OUTPUT_PROMPT = (
    '你是输出验收器。判断主 Agent 是否已完成用户的每一项要求、或说明了某项为何不做。判定保守：'
    '有任何一项没做完、或你拿不准，都判没完成——宁可让它多做一轮，也不要让它过早收工。\n'
    '只输出一个 JSON 对象：{"complete": true 或 false, "reason": "一句话说明"}'
)
_OUTCOME_PROMPT = (
    '你是结果验收器。按下面的验收标准逐条查主 Agent 最终回复与工作区证据（可用 Read/Grep/Glob）。'
    '每条给 result：satisfied（已满足）、unmet（有证据表明做错了，须给 evidence）、unconfirmed（'
    '没能确认）。不要判标「需人工确认」的条。\n'
    '只输出一个 JSON 对象：{"outcomes": [{"rubric_id": "R1", "result": "satisfied", "evidence": '
    '[{"source": "file", "locator": "report.md", "quote": "原文"}]}], "reason": "总体说明"}'
)


@dataclass(frozen=True)
class OutputCheck:
    """L2 的结论：要求是否都做完了（或已明确取消），以及理由。"""

    complete: bool
    reason: str


def render_transcript(ctx: Context) -> str:
    """把主 Agent 的轨迹渲染成伴生的受限视图：逐块 `[role] 正文`，每块与总长都截断、免撑爆伴生。"""
    lines: list[str] = []
    total = 0
    for block in ctx.current_traj:
        text = _block_text(block)
        if not text:
            continue
        line = f'[{block.role}] {text[:_BLOCK_CHARS]}'
        total += len(line)
        if total > _TOTAL_CHARS:
            lines.append('[…较早的轨迹已截断…]')
            break
        lines.append(line)
    return '\n'.join(lines)


async def verify_output(
    *,
    llm: LLM,
    transcript: str,
    portal: Portal | None,
    max_turns: int,
    time_limit: float,
    trust: TrustLevel,
) -> OutputCheck:
    """L2：要求是否都做完了。回复解析不了按验收器失败抛出（由 hook 兜成 unverified）；解析得了但
    `complete` 不是字面量 true 一律判没完成（保守，§7.5）。"""
    async with asyncio.timeout(time_limit):
        result = await run_companion(
            name='OutputVerifier',
            llm=llm,
            system_prompt=_OUTPUT_PROMPT,
            task=transcript,
            portal=portal,
            max_turns=max_turns,
            trust=trust,
        )
    data = _load(result.reply, 'OutputVerifier')
    reason = data.get('reason')
    return OutputCheck(
        complete=data.get('complete') is True,
        reason=reason if isinstance(reason, str) else '',
    )


async def verify_outcome(
    *,
    llm: LLM,
    rubrics: Sequence[Rubric],
    transcript: str,
    portal: Portal | None,
    max_turns: int,
    time_limit: float,
    trust: TrustLevel,
) -> Verdict:
    """L3：逐条判 Rubric，裁决由 `derive_verdict` 推导。回复解析不了按验收器失败抛出。"""
    async with asyncio.timeout(time_limit):
        result = await run_companion(
            name='OutcomeVerifier',
            llm=llm,
            system_prompt=_OUTCOME_PROMPT,
            task=_outcome_task(rubrics, transcript),
            portal=portal,
            max_turns=max_turns,
            trust=trust,
        )
    data = _load(result.reply, 'OutcomeVerifier')
    reason = data.get('reason')
    return derive_verdict(
        rubrics,
        _reported(data.get('outcomes')),
        reason=reason if isinstance(reason, str) else '',
        trust=trust,
    )


def acceptance_hook(
    *,
    store: RubricStore,
    scope: str,
    llm: LLM,
    portal: Portal | None,
    output: bool,
    outcome: bool,
    partial_retries: int,
    block_retries: int,
    max_turns: int,
    timeout: float,
) -> Callable[..., Awaitable[HookResult]]:
    """造挂在 COMPLETION_PROPOSED 上的 hook（每个主 Agent 一个，闭包绑它的 Portal 与 scope）。

    顺序：L2（开着时）→ 没做完就以 partial 打回、不跑 L3；做完了 → L3（开着时）出裁决；L3 没开则
    `unverified`（没验结果就不能说通过，评审 M1）。打回受 partial / block 各自的重试上限约束，计数在
    一次不打回的完成后清零（约「每个 run」）。验收器失败只接 `Exception` → unverified、不打回。"""
    counts = {VerdictStatus.PARTIAL: 0, VerdictStatus.BLOCK: 0}
    caps = {VerdictStatus.PARTIAL: partial_retries, VerdictStatus.BLOCK: block_retries}

    async def hook(**kwargs: Any) -> HookResult:
        ctx = cast('Context', kwargs['ctx'])
        if not output and not outcome:
            return HookResult()
        trust = provenance_of(ctx)
        rubrics = await store.rubrics(scope)
        transcript = render_transcript(ctx)
        verdict = await _judge(rubrics, transcript, trust)
        send_back = verdict.status in caps and counts[verdict.status] < caps[verdict.status]
        if send_back:
            counts[verdict.status] += 1
        else:
            counts[VerdictStatus.PARTIAL] = counts[VerdictStatus.BLOCK] = 0
        return HookResult(modified={'verdict': verdict, 'send_back': send_back})

    async def _judge(rubrics: list[Rubric], transcript: str, trust: TrustLevel) -> Verdict:
        try:
            if output:
                check = await verify_output(
                    llm=llm,
                    transcript=transcript,
                    portal=portal,
                    max_turns=max_turns,
                    time_limit=timeout,
                    trust=trust,
                )
                if not check.complete:
                    return unfinished_verdict(rubrics, reason=check.reason, trust=trust)
            if not outcome:
                return derive_verdict(rubrics, {}, trust=trust, failure='outcome_check_off')
            return await verify_outcome(
                llm=llm,
                rubrics=rubrics,
                transcript=transcript,
                portal=portal,
                max_turns=max_turns,
                time_limit=timeout,
                trust=trust,
            )
        except TimeoutError:
            logger.warning('期望: 验收在时限内完成。 实际: 超时。 下一步: 放行并标 unverified。')
            return derive_verdict(rubrics, {}, trust=trust, failure='verifier_timeout')
        except Exception as exc:
            logger.warning(
                '期望: 验收器正常给出结论。 实际: %s。 下一步: 放行并标 unverified（不装成通过）。',
                type(exc).__name__,
            )
            return derive_verdict(
                rubrics, {}, trust=trust, failure=f'verifier_error:{type(exc).__name__}'
            )

    return hook


def _block_text(block: ContextBlock) -> str:
    content = block.content
    if isinstance(content, str):
        return content
    if content is None:
        return ''
    return str(content)


def _outcome_task(rubrics: Sequence[Rubric], transcript: str) -> str:
    lines = ['验收标准：']
    for rubric in rubrics:
        lines.append(f'- [{rubric.id}]（{rubric.kind.value}）{rubric.statement}')
    lines += ['', '主 Agent 的轨迹：', transcript]
    return '\n'.join(lines)


def _load(reply: str, who: str) -> dict[str, Any]:
    text = reply.strip()
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        raise ValueError(f'{who} 的回复里没有 JSON 对象')
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError(f'{who} 的回复顶层不是 JSON 对象')
    return cast('dict[str, Any]', value)


def _reported(raw: object) -> dict[str, RubricOutcome]:
    if not isinstance(raw, list):
        return {}
    reported: dict[str, RubricOutcome] = {}
    for entry in cast('list[object]', raw):
        if not isinstance(entry, dict):
            continue
        item = cast('dict[str, Any]', entry)
        rid = item.get('rubric_id')
        if not isinstance(rid, str):
            continue
        evidence = _evidence(item.get('evidence'))
        result = item.get('result')
        if result == 'satisfied':
            reported[rid] = RubricOutcome(rid, OutcomeKind.SATISFIED, evidence)
        elif result == 'unmet' and evidence:
            reported[rid] = RubricOutcome(rid, OutcomeKind.UNMET, evidence)
        elif result in ('unmet', 'unconfirmed'):
            # 没有证据的 unmet 不是「已证伪」，只是「没能确认」（§7.5）。
            reported[rid] = RubricOutcome(rid, OutcomeKind.UNCONFIRMED, evidence)
    return reported


def _evidence(raw: object) -> tuple[Anchor, ...]:
    if not isinstance(raw, list):
        return ()
    anchors: list[Anchor] = []
    for entry in cast('list[object]', raw):
        if not isinstance(entry, dict):
            continue
        item = cast('dict[str, Any]', entry)
        raw_source = item.get('source')
        source = _SOURCES.get(raw_source) if isinstance(raw_source, str) else None
        locator = item.get('locator')
        quote = item.get('quote', '')
        if source is None or not isinstance(locator, str) or not isinstance(quote, str):
            continue
        anchors.append(Anchor(source=source, locator=locator, quote=quote))
    return tuple(anchors)
