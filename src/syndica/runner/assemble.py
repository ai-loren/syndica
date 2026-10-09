"""从会话、Goal/Rubric、trace 与产物登记拼一份中立轨迹。

只读这些 store。一条会话拼不出来、一个 trace 读不回来，都只记进名单，其余照常拼。

裁决取 trace 里根 Agent 的 span 上 `verdict.status`，按结束时间排，最后一个是这次会话的裁决，
前面的是验收打回之前的结论。验收伴生 Agent 也在同一个 trace 里，它们的 span 不算。回放写出的
trace（`source.replay_of` 不为空）用的是记录下来的 Agent id，也不算：否则回放的结论会顶替原来的。
根 Agent 上没有裁决记 `unverified`，并在 `verdict_source` 里分清是「trace 上没有」还是
「找不到 trace」。

`goal_id` 与 `rubric_id` 是 Goal 正文与 active Rubric（id 加陈述）的摘要：偏好对只在两次尝试按同一份
要求验收时才成立，模型起的 Rubric id（`R1`）在不同会话里可以指不同的陈述。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import cast

from syndica.kernel.acceptance import Rubric
from syndica.kernel.context import ArtifactRef, ContextBlock
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.protocols.artifact import ArtifactStore
from syndica.kernel.protocols.rubric import RubricStore
from syndica.kernel.protocols.session import SessionStore
from syndica.kernel.protocols.trace import TraceStore
from syndica.kernel.session_types import SessionMeta, SessionRecord
from syndica.kernel.trace import CausalityEdge, Span, SpanKind
from syndica.runner.training import Attempt, Call, Message

__all__ = ['Assembled', 'assemble_attempts', 'goal_digest', 'rubric_digest']

logger = logging.getLogger(__name__)

_VERDICTS = frozenset({'pass', 'partial', 'block', 'unverified'})
_ROLES = frozenset({'user', 'assistant', 'tool'})


@dataclass(frozen=True)
class Assembled:
    attempts: list[Attempt]
    # `sid: 期望` 或 `sid: 异常类型名`，不含会话正文。
    failed: list[str]
    # `trace id: 期望` 或 `trace id: 异常类型名`：读不回来、跳过了的 trace。
    skipped_traces: list[str]


def goal_digest(intent: str) -> str:
    return 'goal:' + hashlib.sha256(intent.encode('utf-8')).hexdigest()[:16]


def rubric_digest(rubrics: list[Rubric]) -> str:
    """没有 active Rubric 时是空串：这次尝试的裁决只可能是 unverified，进不了偏好对。"""
    if not rubrics:
        return ''
    lines = sorted(f'{item.id}\n{item.statement}' for item in rubrics)
    return 'rubric:' + hashlib.sha256('\n\n'.join(lines).encode('utf-8')).hexdigest()[:16]


async def assemble_attempts(
    sessions: SessionStore,
    traces: TraceStore,
    rubrics: RubricStore,
    artifacts: ArtifactStore,
) -> Assembled:
    """每个会话一份尝试。"""
    attempts: list[Attempt] = []
    failed: list[str] = []
    metas = await sessions.list_metas()
    latest, skipped = await _latest_traces(traces, {meta.sid for meta in metas})
    for meta in metas:
        try:
            trace_id = meta.last_trace or latest.get(meta.sid)
            attempts.append(await _one(meta, trace_id, sessions, traces, rubrics, artifacts))
        except (ConfigError, ContractViolation) as exc:
            failed.append(f'{meta.sid}: {exc.expected}')
        except Exception as exc:
            logger.exception('assemble session %s failed', meta.sid)
            failed.append(f'{meta.sid}: {type(exc).__name__}')
    return Assembled(attempts, failed, skipped)


async def _latest_traces(traces: TraceStore, sids: set[str]) -> tuple[dict[str, str], list[str]]:
    """会话 id → 根 Agent 出现过的最近一个原始 trace。`SessionMeta.last_trace` 宿主还没有写，只能
    按根 Agent 的 id 从 trace 里找；`traces()` 按开始时间排序，后来的覆盖先前的。"""
    found: dict[str, str] = {}
    skipped: list[str] = []
    for header in await traces.traces():
        if header.source.replay_of is not None:
            continue
        try:
            spans = await traces.spans(header.trace_id)
        except (ConfigError, ContractViolation) as exc:
            skipped.append(f'{header.trace_id}: {exc.expected}')
            continue
        except Exception as exc:
            logger.exception('read trace %s failed', header.trace_id)
            skipped.append(f'{header.trace_id}: {type(exc).__name__}')
            continue
        agents = {span.agent_id for span in spans}
        for sid in sids & agents:
            found[sid] = header.trace_id
    return found, skipped


async def _one(
    meta: SessionMeta,
    trace_id: str | None,
    sessions: SessionStore,
    traces: TraceStore,
    rubrics: RubricStore,
    artifacts: ArtifactStore,
) -> Attempt:
    record = await sessions.load(meta.sid)
    if record is None:
        raise ConfigError(
            expected=f'会话 {meta.sid} 的正文还在',
            received='load 返回 None',
            next_action='确认这个会话已经存盘，再导出',
        )
    goal = await rubrics.get_goal(meta.sid)
    if goal is None:
        raise ConfigError(
            expected=f'会话 {meta.sid} 有 Goal',
            received='这个 scope 没有 Goal',
            next_action='在配置的 modules 里启用 acceptance 再跑；没有 Goal 的会话无从验收',
        )
    active = await rubrics.rubrics(meta.sid)
    spans: list[Span] = []
    edges: list[CausalityEdge] = []
    if trace_id:
        spans = await traces.spans(trace_id)
        edges = await traces.edges(trace_id)
    verdicts, outcomes = _verdicts(spans, meta.sid)
    if trace_id is None:
        source = 'missing'
    elif verdicts:
        source = 'acceptance'
    else:
        source = 'none'
    listed = await artifacts.list(producer=meta.sid)
    return Attempt(
        id=meta.sid,
        goal_id=goal_digest(goal.intent),
        rubric_id=rubric_digest(active),
        messages=_messages(record),
        verdict=verdicts[-1] if verdicts else 'unverified',
        artifacts=tuple(_artifacts(record, listed)),
        causality=tuple(_edges(edges)),
        timing=_timing(spans, meta.sid),
        model=record.model,
        system_prompt=record.system_prompt,
        tools=tuple(record.tools),
        verdicts=tuple(verdicts),
        outcomes=outcomes,
        trace_id=trace_id,
        verdict_source=source,
    )


def _messages(record: SessionRecord) -> tuple[Message, ...]:
    built: list[Message] = []
    for block in record.content:
        if block.role not in _ROLES:
            continue
        calls = tuple(Call(call.id, call.name, call.arguments) for call in block.tool_calls)
        built.append(
            Message(block.role, _text(block), block.tool_call_id, calls, block.trust.value)
        )
    return tuple(built)


def _text(block: ContextBlock) -> str:
    content = block.content
    if content is None or isinstance(content, str):
        return content or ''
    parts: list[str] = []
    for item in cast(list[object], content):
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = cast(dict[str, object], item).get('text')
            if isinstance(text, str):
                parts.append(text)
    return ''.join(parts)


def _artifacts(record: SessionRecord, listed: list[ArtifactRef]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    owned = [ref for block in record.content for ref in block.artifacts]
    for ref in (*owned, *listed):
        key = (ref.path, ref.version)
        if key in seen:
            continue
        seen.add(key)
        rows.append({'path': ref.path, 'version': ref.version})
    return rows


def _root_agent_spans(spans: list[Span], root: str) -> list[Span]:
    chosen = [span for span in spans if span.agent_id == root and span.kind is SpanKind.AGENT]
    return sorted(chosen, key=lambda span: (span.end_ns, span.start_ns))


def _verdicts(spans: list[Span], root: str) -> tuple[list[str], tuple[str, ...]]:
    """根 Agent 先后得到的裁决与最后一次的逐条结论。内核每给一次裁决记一个 `verdict` 事件（含打回
    之前的）；更早写下的 trace 没有这个事件，退回 agent span 收尾时记的终态。"""
    events = sorted(
        (
            event
            for span in spans
            if span.agent_id == root
            for event in span.events
            if event.name == 'verdict'
        ),
        key=lambda event: event.time_ns,
    )
    marked = (
        [
            (span.attributes.get('verdict.status'), span.attributes.get('verdict.outcomes'))
            for span in _root_agent_spans(spans, root)
        ]
        if not events
        else [
            (event.attributes.get('verdict.status'), event.attributes.get('verdict.outcomes'))
            for event in events
        ]
    )
    verdicts: list[str] = []
    outcomes: tuple[str, ...] = ()
    for value, listed in marked:
        if isinstance(value, str) and value in _VERDICTS:
            verdicts.append(value)
            outcomes = tuple(listed) if isinstance(listed, list) else ()
    return verdicts, outcomes


def _edges(edges: list[CausalityEdge]) -> list[tuple[str, str, str]]:
    return [(edge.kind.value, edge.source_span_id, edge.target_span_id) for edge in edges]


def _timing(spans: list[Span], root: str) -> tuple[int, int, str | None] | None:
    """成本取根 Agent 最后结束的那个 span 上的 `budget.cost`：它含后代的用量。"""
    if not spans:
        return None
    start = min(span.start_ns for span in spans)
    end = max(span.end_ns for span in spans)
    cost: str | None = None
    for span in _root_agent_spans(spans, root):
        value = span.attributes.get('budget.cost')
        if isinstance(value, str):
            cost = value
    return (start, end, cost)
