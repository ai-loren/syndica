import { useEffect, useState } from 'react';
import { api } from '../api';
import type { components } from '../generated/openapi';
import type { SessionState } from '../session';

type Analysis = components['schemas']['TraceAnalysis'];

export function TracePanel(props: { state: SessionState; refresh: number; onProblem: (error: unknown) => void }) {
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const sid = props.state.sid;
  useEffect(() => {
    void api<Analysis>(`/api/v1/sessions/${encodeURIComponent(sid)}/trace`)
      .then(setAnalysis)
      .catch(props.onProblem);
  }, [sid, props.refresh, props.onProblem]);
  if (!analysis) return <p className="empty">加载 trace…</p>;
  const streamed = Object.keys(props.state.tools);
  const traced = analysis.tool_calls.map((call) => String(call.call_id));
  const missing = streamed.filter((id) => !traced.includes(id));
  return (
    <section className="trace" aria-label="trace">
      <p>
        trace <code>{analysis.trace_id.slice(0, 12)}</code> · {analysis.llm_calls} 次模型调用 · {analysis.tokens} token
      </p>
      <p className={missing.length ? 'mismatch' : 'match'} data-consistent={missing.length === 0}>
        {missing.length === 0
          ? '时间线里的工具调用与 trace 一致'
          : `时间线里有 ${missing.length} 个工具调用还没落进 trace（span 在调用结束时才写）`}
      </p>
      <table>
        <thead>
          <tr>
            <th>调用</th>
            <th>工具</th>
            <th>结局</th>
          </tr>
        </thead>
        <tbody>
          {analysis.tool_calls.map((call) => (
            <tr key={String(call.call_id)} data-call-id={String(call.call_id)}>
              <td>
                <code>{String(call.call_id)}</code>
              </td>
              <td>{String(call.name)}</td>
              <td>{String(call.outcome ?? '')}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {analysis.spawns.length > 0 && (
        <ul className="spawns">
          {analysis.spawns.map((edge) => (
            <li key={String(edge.child_id)} data-child-id={String(edge.child_id)}>
              {String(edge.parent_id ?? '').slice(0, 8)} → {String(edge.child_id).slice(0, 8)}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
