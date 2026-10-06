import { useCallback, useEffect, useRef, useState } from 'react';
import { api, loginFromHash, Problem, token } from './api';
import { Agents } from './components/Agents';
import { Artifacts } from './components/Artifacts';
import { Budget } from './components/Budget';
import { Chat } from './components/Chat';
import { Doctor } from './components/Doctor';
import { LoginHelp } from './components/LoginHelp';
import { PendingDialog } from './components/PendingDialog';
import { ProblemBox } from './components/ProblemBox';
import { Sessions } from './components/Sessions';
import { Terminals } from './components/Terminals';
import { Timeline } from './components/Timeline';
import { TracePanel } from './components/TracePanel';
import type { components } from './generated/openapi';
import { Rpc, type RpcReply } from './rpc';
import { apply, fromSnapshot, type Pending, type SessionState, type Snapshot, without } from './session';
import { follow, type Follow } from './sse';

type Summary = components['schemas']['SessionSummary'];
type Report = components['schemas']['DoctorReport'];
type Panel = 'agents' | 'timeline' | 'budget' | 'artifacts' | 'trace' | 'terminal';

function replyProblem(reply: RpcReply): Problem {
  const error = reply.error;
  return new Problem(
    409,
    error?.expected ?? '操作成功',
    error?.received ?? '没有拿到回复',
    error?.next_action ?? '重试',
  );
}

export function App() {
  const [phase, setPhase] = useState<'loading' | 'login' | 'expired' | 'ready'>('loading');
  const [problem, setProblem] = useState<unknown>(null);
  const [doctor, setDoctor] = useState<Report | null>(null);
  const [sessions, setSessions] = useState<Summary[]>([]);
  const [roles, setRoles] = useState<string[]>(['Orchestrator']);
  const [state, setState] = useState<SessionState | null>(null);
  const [target, setTarget] = useState<string | null>(null);
  const [connected, setConnected] = useState(true);
  const [panel, setPanel] = useState<Panel>('agents');
  const [drawer, setDrawer] = useState<'none' | 'sessions' | 'panel'>('none');
  const stream = useRef<Follow | null>(null);
  const rpc = useRef<Rpc | null>(null);

  const fail = useCallback((error: unknown) => {
    if (error instanceof Problem && error.status === 401) setPhase('expired');
    else setProblem(error);
  }, []);

  const refreshLists = useCallback(async () => {
    const [list, report, known] = await Promise.all([
      api<{ sessions: Summary[] }>('/api/v1/sessions'),
      api<Report>('/api/v1/doctor'),
      api<{ roles: string[] }>('/api/v1/roles'),
    ]);
    setSessions(list.sessions);
    setDoctor(report);
    setRoles(known.roles);
  }, []);

  const attach = useCallback((snapshot: Snapshot) => {
    stream.current?.stop();
    rpc.current?.close();
    setState((previous) => fromSnapshot(snapshot, previous ?? undefined));
    setTarget((current) => (current && snapshot.agents.some((a) => a.agent_id === current) ? current : snapshot.root_id));
    history.replaceState(null, '', `?session=${encodeURIComponent(snapshot.sid)}`);
    rpc.current = new Rpc(snapshot.sid);
    stream.current = follow(
      `/api/v1/sessions/${encodeURIComponent(snapshot.sid)}/events`,
      snapshot.as_of,
      (frame) => setState((previous) => (previous ? apply(previous, frame) : previous)),
      setConnected,
    );
  }, []);

  const open = useCallback(
    async (sid: string) => {
      try {
        let snapshot: Snapshot;
        try {
          snapshot = await api<Snapshot>(`/api/v1/sessions/${encodeURIComponent(sid)}/snapshot`);
        } catch (error) {
          if (!(error instanceof Problem) || error.status !== 404) throw error;
          snapshot = await api<Snapshot>(`/api/v1/sessions/${encodeURIComponent(sid)}/resume`, {
            method: 'POST',
          });
        }
        attach(snapshot);
        setDrawer('none');
        await refreshLists();
      } catch (error) {
        fail(error);
      }
    },
    [attach, fail, refreshLists],
  );

  useEffect(() => {
    void (async () => {
      try {
        await loginFromHash();
      } catch (error) {
        setProblem(error);
      }
      if (!token()) {
        setPhase('login');
        return;
      }
      try {
        await refreshLists();
        setPhase('ready');
        const sid = new URLSearchParams(location.search).get('session');
        if (sid) await open(sid);
      } catch (error) {
        fail(error);
        setPhase((current) => (current === 'loading' ? 'ready' : current));
      }
    })();
    return () => {
      stream.current?.stop();
      rpc.current?.close();
    };
  }, [fail, open, refreshLists]);

  const sid = state?.sid;
  const stale = state?.stale;
  useEffect(() => {
    if (!sid || !stale) return;
    void api<Snapshot>(`/api/v1/sessions/${encodeURIComponent(sid)}/snapshot`)
      .then((snapshot) => setState((previous) => fromSnapshot(snapshot, previous ?? undefined)))
      .catch(fail);
  }, [sid, stale, fail]);

  async function call(op: string, fields: Record<string, unknown>): Promise<boolean> {
    if (!rpc.current) return false;
    const reply = await rpc.current.call(op, fields);
    if (!reply.ok) setProblem(replyProblem(reply));
    return reply.ok;
  }

  async function create(role: string) {
    try {
      const snapshot = await api<Snapshot>('/api/v1/sessions', {
        method: 'POST',
        body: JSON.stringify({ role }),
      });
      attach(snapshot);
      setDrawer('none');
      await refreshLists();
    } catch (error) {
      fail(error);
    }
  }

  async function close(closing: string) {
    try {
      await api(`/api/v1/sessions/${encodeURIComponent(closing)}/close`, { method: 'POST' });
      if (closing === state?.sid) {
        stream.current?.stop();
        rpc.current?.close();
        setState(null);
        history.replaceState(null, '', location.pathname);
      }
      await refreshLists();
    } catch (error) {
      fail(error);
    }
  }

  async function remove(removing: string) {
    try {
      await api(`/api/v1/sessions/${encodeURIComponent(removing)}`, { method: 'DELETE' });
      await refreshLists();
    } catch (error) {
      fail(error);
    }
  }

  async function answer(pending: Pending, text: string) {
    if (await call('answer', { request_id: pending.request_id, text })) {
      setState((previous) =>
        previous ? { ...previous, pending: without(previous.pending, pending.request_id) } : previous,
      );
    }
  }

  async function budget(pending: Pending, turns: number | null) {
    await call('budget_answer', {
      request_id: pending.request_id,
      budget: turns === null ? null : { turns },
    });
  }

  const refresh = state
    ? state.timeline.filter((item) => item.kind === 'artifact_registered' || item.kind === 'agent_end').length
    : 0;

  if (phase === 'loading') return <p className="loading">加载中…</p>;
  if (phase === 'login' || phase === 'expired') return <LoginHelp expired={phase === 'expired'} />;
  return (
    <div className={`app drawer-${drawer}`}>
      <header className="bar">
        <button type="button" className="toggle" onClick={() => setDrawer(drawer === 'sessions' ? 'none' : 'sessions')}>
          会话
        </button>
        <h1>Syndica</h1>
        {!connected && <span className="offline">连接中断，正在重连…</span>}
        <button type="button" className="toggle" onClick={() => setDrawer(drawer === 'panel' ? 'none' : 'panel')}>
          面板
        </button>
      </header>
      <aside className="left">
        <Sessions
          sessions={sessions}
          roles={roles}
          current={state?.sid ?? null}
          onOpen={(opening) => void open(opening)}
          onCreate={(role) => void create(role)}
          onClose={(closing) => void close(closing)}
          onRemove={(removing) => void remove(removing)}
        />
      </aside>
      <main>
        {problem !== null && <ProblemBox problem={problem} onDismiss={() => setProblem(null)} />}
        <Doctor report={doctor} />
        {state && target ? (
          <Chat state={state} target={target} onSend={async (text) => void (await call('send', { text, target }))} />
        ) : (
          <p className="empty">选一个会话，或新建一个。</p>
        )}
      </main>
      <aside className="right">
        <nav className="tabs" role="tablist">
          {(['agents', 'timeline', 'budget', 'artifacts', 'trace', 'terminal'] as const).map((name) => (
            <button
              type="button"
              role="tab"
              key={name}
              aria-selected={panel === name}
              onClick={() => setPanel(name)}
            >
              {
                {
                  agents: 'Agent',
                  timeline: '时间线',
                  budget: '预算',
                  artifacts: '产物',
                  trace: 'trace',
                  terminal: '终端',
                }[name]
              }
            </button>
          ))}
        </nav>
        {state && panel === 'agents' && (
          <Agents
            state={state}
            target={target ?? state.rootId}
            onTarget={(id) => {
              setTarget(id);
              setDrawer('none');
            }}
            onAbort={(id) => void call('abort', { agent_id: id })}
          />
        )}
        {state && panel === 'timeline' && <Timeline state={state} />}
        {state && panel === 'budget' && <Budget state={state} />}
        {state && panel === 'artifacts' && (
          <Artifacts
            sid={state.sid}
            refresh={refresh}
            onActivate={(rubricId) => call('activate_rubric', { rubric_id: rubricId })}
            onProblem={fail}
          />
        )}
        {state && panel === 'trace' && <TracePanel state={state} refresh={refresh} onProblem={fail} />}
        {state && panel === 'terminal' && <Terminals state={state} />}
      </aside>
      {state && <PendingDialog state={state} onAnswer={answer} onBudget={budget} />}
    </div>
  );
}
