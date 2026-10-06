from __future__ import annotations

from syndica.kernel import (
    CausalityEdge,
    EdgeKind,
    RuleRedactor,
    Span,
    SpanKind,
    SpanStatus,
    TraceHeader,
    TraceLevel,
    TraceRedaction,
)
from syndica.kernel.redaction import RULES_VERSION
from syndica.kernel.trace_analysis import analyze_trace
from syndica.kernel.tracing import span_id_of

_LEAD = '1a' * 16
_CHILD = '2b' * 16


def _header() -> TraceHeader:
    redactor = RuleRedactor()
    return TraceHeader(
        trace_id='ab' * 16,
        started_ns=10,
        level=TraceLevel.STRUCTURE,
        redaction=TraceRedaction(
            digest=redactor.digest, rules_version=RULES_VERSION, rules=redactor.rules
        ),
        status=SpanStatus.OK,
        ended_ns=99,
    )


def _span(kind: SpanKind, agent: str, start: int, *key: str | int, **attributes: object) -> Span:
    return Span(
        span_id=span_id_of(kind, agent, *key),
        parent_span_id=None,
        kind=kind,
        name=str(attributes.pop('name', kind.value)),
        agent_id=agent,
        start_ns=start,
        end_ns=start + 1,
        status=SpanStatus.OK,
        attributes=dict(attributes),  # pyright: ignore[reportArgumentType]
    )


def test_tool_calls_come_in_start_order_and_spawns_name_both_ends():
    lead = _span(SpanKind.AGENT, _LEAD, 11, 1, name='Lead')
    child = _span(SpanKind.AGENT, _CHILD, 20, 1, name='Worker')
    spans = [
        child,
        _span(
            SpanKind.TOOL_CALL, _CHILD, 30, 1, 1, 0, **{'tool.call_id': 'c3', 'tool.name': 'Bash'}
        ),
        _span(
            SpanKind.TOOL_CALL,
            _LEAD,
            12,
            1,
            1,
            0,
            **{'tool.call_id': 'c1', 'tool.name': 'Delegate', 'tool.outcome': 'ok'},
        ),
        lead,
        _span(
            SpanKind.LLM_CALL, _LEAD, 13, 1, 1, **{'llm.input_tokens': 7, 'llm.output_tokens': 3}
        ),
        _span(SpanKind.LLM_CALL, _CHILD, 21, 1, 1, **{'llm.input_tokens': 5}),
    ]
    edges = [
        CausalityEdge(
            kind=EdgeKind.SPAWN,
            source_span_id=lead.span_id,
            target_span_id=child.span_id,
            attributes={'child.agent_id': _CHILD},
        )
    ]
    analysis = analyze_trace(_header(), spans, edges)
    assert [call['call_id'] for call in analysis['tool_calls']] == ['c1', 'c3']
    assert analysis['tool_calls'][0]['outcome'] == 'ok'
    assert analysis['spawns'] == [{'parent_id': _LEAD, 'child_id': _CHILD}]
    assert [agent['name'] for agent in analysis['agents']] == ['Lead', 'Worker']
    assert (analysis['llm_calls'], analysis['tokens']) == (2, 15)
    assert (analysis['status'], analysis['ended_ns']) == ('ok', 99)
