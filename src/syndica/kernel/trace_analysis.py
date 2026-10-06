"""一份 trace 的纯数据分析：Agent、按开始时刻排的工具调用、派生边、模型调用计数。

服务面把它交给前端，前端据此核对自己的事件时间线（工具调用的 id 与派生出的 Agent 要与 trace 里的
一致）。只读 span 与边，不读 payload：属性在写 trace 之前已经脱过敏。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from syndica.kernel.trace import CausalityEdge, EdgeKind, Span, SpanKind, TraceHeader

__all__ = ['analyze_trace']


def analyze_trace(
    header: TraceHeader, spans: Sequence[Span], edges: Sequence[CausalityEdge]
) -> dict[str, Any]:
    by_id = {span.span_id: span for span in spans}
    agents: dict[str, dict[str, Any]] = {}
    for span in sorted(spans, key=lambda item: item.start_ns):
        if span.kind is SpanKind.AGENT and span.agent_id not in agents:
            agents[span.agent_id] = {
                'agent_id': span.agent_id,
                'name': span.name,
                'started_ns': span.start_ns,
            }
    tool_calls = [
        {
            'call_id': span.attributes.get('tool.call_id'),
            'name': span.attributes.get('tool.name'),
            'agent_id': span.agent_id,
            'outcome': span.attributes.get('tool.outcome'),
            'start_ns': span.start_ns,
            'end_ns': span.end_ns,
        }
        for span in sorted(spans, key=lambda item: (item.start_ns, item.span_id))
        if span.kind is SpanKind.TOOL_CALL
    ]
    spawns = [
        {
            'parent_id': by_id[edge.source_span_id].agent_id
            if edge.source_span_id in by_id
            else None,
            'child_id': edge.attributes.get('child.agent_id'),
        }
        for edge in edges
        if edge.kind is EdgeKind.SPAWN
    ]
    calls = [span for span in spans if span.kind is SpanKind.LLM_CALL]
    return {
        'trace_id': header.trace_id,
        'status': None if header.status is None else header.status.value,
        'started_ns': header.started_ns,
        'ended_ns': header.ended_ns,
        'agents': list(agents.values()),
        'tool_calls': tool_calls,
        'spawns': spawns,
        'llm_calls': len(calls),
        'tokens': sum(
            int(value)
            for span in calls
            for key in ('llm.input_tokens', 'llm.output_tokens')
            if isinstance(value := span.attributes.get(key), int)
        ),
    }
