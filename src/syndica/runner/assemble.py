"""从会话、Goal/Rubric、trace 与产物登记拼一份中立轨迹。

只读这些 store。一条会话拼不出来就记进失败名单，其余照常拼。

裁决取 trace 里根 Agent 的 span 上最后一次 `verdict.status`。验收伴生 Agent 也在同一个 trace 里，
它们的 span 不算；根 Agent 上没有裁决就记 `unverified`，导出默认把它排除。

`goal_id` 与 `rubric_id` 是 Goal 正文与 active Rubric（id 加陈述）的摘要：偏好对只在两次尝试按同一份
要求验收时才成立，模型起的 Rubric id（`R1`）在不同会话里可以指不同的陈述。
"""

from __future__ import annotations

import hashlib
import logging
from typing import cast

from syndica.kernel.acceptance import Rubric
from syndica.kernel.context import ArtifactRef, ContextBlock
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.protocols.artifact import ArtifactStore
from syndica.kernel.protocols.rubric import RubricStore
from syndica.kernel.protocols.session import SessionStore
from syndica.kernel.protocols.trace import TraceStore
from syndica.kernel.session_types import SessionMeta, SessionRecord
from syndica.kernel.trace import CausalityEdge, Span
from syndica.runner.training import Attempt, Call, Message

__all__ = ['assemble_attempts', 'goal_digest', 'rubric_digest']

logger = logging.getLogger(__name__)

_VERDICTS = frozenset({'pass', 'partial', 'block', 'unverified'})
_ROLES = frozenset({'user', 'assistant', 'tool'})


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
) -> tuple[list[Attempt], list[str]]:
    """每个会话一份尝试。失败名单里是 `sid: 期望`，不含会话正文。"""
    attempts: list[Attempt] = []
    failed: list[str] = []
    metas = await sessions.list_metas()
    latest = await _latest_traces(traces, {meta.sid for meta in metas})
    for meta in metas:
        try:
            trace_id = meta.last_trace or latest.get(meta.sid)
            attempts.append(await _one(meta, trace_id, sessions, traces, rubrics, artifacts))
        except (ConfigError, ContractViolation) as exc:
            failed.append(f'{meta.sid}: {exc.expected}')
        except Exception as exc:
            logger.exception('assemble session %s failed', meta.sid)
            failed.append(f'{meta.sid}: {type(exc).__name__}')
    return attempts, failed


async def _latest_traces(traces: TraceStore, sids: set[str]) -> dict[str, str]:
    """会话 id → 根 Agent 出现过的最近一个 trace。`SessionMeta.last_trace` 宿主还没有写，只能按根
    Agent 的 id 从 trace 里找；`traces()` 按开始时间排序，后来的覆盖先前的。"""
    found: dict[str, str] = {}
    for header in await traces.traces():
        agents = {span.agent_id for span in await traces.spans(header.trace_id)}
        for sid in sids & agents:
            found[sid] = header.trace_id
    return found


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
    listed = await artifacts.list(producer=meta.sid)
    return Attempt(
        id=meta.sid,
        goal_id=goal_digest(goal.intent),
        rubric_id=rubric_digest(active),
        messages=_messages(record),
        verdict=_verdict(spans, meta.sid),
        artifacts=tuple(_artifacts(record, listed)),
        causality=tuple(_edges(edges)),
        timing=_timing(spans, meta.sid),
    )


def _messages(record: SessionRecord) -> tuple[Message, ...]:
    built: list[Message] = []
    for block in record.content:
        if block.role not in _ROLES:
            continue
        calls = tuple(Call(call.id, call.name, call.arguments) for call in block.tool_calls)
        built.append(Message(block.role, _text(block), block.tool_call_id, calls))
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


def _verdict(spans: list[Span], root: str) -> str:
    status = 'unverified'
    for span in spans:
        if span.agent_id != root:
            continue
        value = span.attributes.get('verdict.status')
        if isinstance(value, str) and value in _VERDICTS:
            status = value
    return status


def _edges(edges: list[CausalityEdge]) -> list[tuple[str, str, str]]:
    return [(edge.kind.value, edge.source_span_id, edge.target_span_id) for edge in edges]


def _timing(spans: list[Span], root: str) -> tuple[int, int, str | None] | None:
    """成本取根 Agent 最后一个 span 上的 `budget.cost`：它含后代的用量。"""
    if not spans:
        return None
    start = min(span.start_ns for span in spans)
    end = max(span.end_ns for span in spans)
    cost: str | None = None
    for span in spans:
        if span.agent_id != root:
            continue
        value = span.attributes.get('budget.cost')
        if isinstance(value, str):
            cost = value
    return (start, end, cost)
