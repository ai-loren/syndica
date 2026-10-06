import type { components } from '../generated/openapi';

type Summary = components['schemas']['SessionSummary'];

export function Sessions(props: {
  sessions: Summary[];
  roles: string[];
  current: string | null;
  onOpen: (sid: string) => void;
  onCreate: (role: string) => void;
  onClose: (sid: string) => void;
  onRemove: (sid: string) => void;
}) {
  return (
    <nav className="sessions" aria-label="会话">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const role = new FormData(event.currentTarget).get('role');
          props.onCreate(typeof role === 'string' ? role : 'Orchestrator');
        }}
      >
        <select name="role" aria-label="角色" defaultValue="Orchestrator">
          {props.roles.map((role) => (
            <option key={role}>{role}</option>
          ))}
        </select>
        <button type="submit">新会话</button>
      </form>
      <ul>
        {props.sessions.map((session) => (
          <li key={session.sid} className={session.sid === props.current ? 'current' : undefined}>
            <button type="button" className="open" onClick={() => props.onOpen(session.sid)}>
              <span className="sid">{session.sid.slice(0, 8)}</span>
              <span className="meta">
                {session.agent || '—'} · {session.turns} 轮 · {session.live ? '打开着' : session.status}
              </span>
            </button>
            {session.live ? (
              <button type="button" onClick={() => props.onClose(session.sid)} aria-label="关闭会话">
                关闭
              </button>
            ) : (
              <button type="button" onClick={() => props.onRemove(session.sid)} aria-label="删除会话">
                删除
              </button>
            )}
          </li>
        ))}
      </ul>
    </nav>
  );
}
