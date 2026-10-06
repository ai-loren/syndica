import type { SessionState } from '../session';

export function Timeline({ state }: { state: SessionState }) {
  return (
    <ol className="timeline" aria-label="事件时间线">
      {state.timeline.map((item, index) => (
        <li key={item.id ?? `local-${index}`} data-kind={item.kind}>
          <time>{item.time_ns ? new Date(item.time_ns / 1e6).toLocaleTimeString() : ''}</time>{' '}
          <code>{item.kind}</code>{' '}
          <span className="agent">{state.agents[item.agent_id ?? '']?.name ?? ''}</span>{' '}
          <span className="detail">{item.detail}</span>
        </li>
      ))}
    </ol>
  );
}
