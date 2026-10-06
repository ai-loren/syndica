import type { SessionState } from '../session';

interface View {
  limit?: Record<string, number | string | null>;
  used?: Record<string, number | string | boolean | null>;
  remaining?: Record<string, number | string | null>;
  exhausted?: string[];
}

const ROWS: [string, string, (view: View) => string][] = [
  ['轮数', 'turns', (view) => String(view.used?.turns ?? 0)],
  [
    'token',
    'tokens',
    (view) => String(Number(view.used?.input_tokens ?? 0) + Number(view.used?.output_tokens ?? 0)),
  ],
  ['成本', 'cost', (view) => (view.used?.cost === null ? '未知' : String(view.used?.cost ?? '0'))],
  ['墙钟（秒）', 'wall_clock_s', () => '—'],
];

function Progress({ state }: { state: SessionState }) {
  const progress = (state.progress ?? {}) as Record<string, Record<string, unknown>>;
  const rows = Object.entries(progress);
  if (rows.length === 0) return null;
  return (
    <ul className="progress" aria-label="进度">
      {rows.map(([agentId, item]) => {
        const done = Array.isArray(item.satisfied) ? item.satisfied.length : 0;
        const total = Array.isArray(item.mandatory) ? item.mandatory.length : 0;
        return (
          <li key={agentId}>
            {state.agents[agentId]?.name ?? agentId.slice(0, 8)}：强制验收条 {done} / {total} 满足（第{' '}
            {String(item.measured_turn ?? '?')} 轮量的）
          </li>
        );
      })}
    </ul>
  );
}

export function Budget({ state }: { state: SessionState }) {
  const view = state.budgets[state.rootId] as View | undefined;
  if (!view) return <><Progress state={state} /><p className="empty">这个会话没有预算数据。</p></>;
  return (
    <>
    <Progress state={state} />
    <table className="budget" aria-label="预算">
      <thead>
        <tr>
          <th>维度</th>
          <th>已用</th>
          <th>上限</th>
        </tr>
      </thead>
      <tbody>
        {ROWS.map(([label, key, used]) => (
          <tr key={key} className={view.exhausted?.includes(key) ? 'exhausted' : undefined}>
            <td>{label}</td>
            <td>{used(view)}</td>
            <td>{view.limit?.[key] ?? '不限'}</td>
          </tr>
        ))}
      </tbody>
      {view.used?.estimated ? (
        <caption>有一部分 token 是按字符估出来的（后端没报用量）</caption>
      ) : null}
    </table>
    </>
  );
}
