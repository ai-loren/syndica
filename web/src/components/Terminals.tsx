import { Terminal as Xterm } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { useEffect, useRef, useState } from 'react';
import { token } from '../api';
import type { SessionState } from '../session';

function socketUrl(path: string): string {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${location.host}${path}`;
}

function HumanTerminal({ sid }: { sid: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [closed, setClosed] = useState(false);
  useEffect(() => {
    if (!host.current) return;
    const terminal = new Xterm({ cols: 100, rows: 24, convertEol: false, fontSize: 13 });
    terminal.open(host.current);
    const socket = new WebSocket(socketUrl(`/api/v1/sessions/${encodeURIComponent(sid)}/pty`));
    socket.binaryType = 'arraybuffer';
    socket.onopen = () =>
      socket.send(JSON.stringify({ op: 'auth', id: 0, token: token(), cols: terminal.cols, rows: terminal.rows }));
    socket.onmessage = (message) => {
      if (message.data instanceof ArrayBuffer) terminal.write(new Uint8Array(message.data));
    };
    socket.onclose = () => setClosed(true);
    const input = terminal.onData((data) => socket.send(JSON.stringify({ op: 'input', data })));
    return () => {
      input.dispose();
      socket.close();
      terminal.dispose();
    };
  }, [sid]);
  return (
    <div className="human-terminal">
      <div ref={host} className="xterm-host" data-testid="human-terminal" />
      {closed && <p className="empty">终端已关闭。</p>}
    </div>
  );
}

function AgentScreen({ sid, agentId, handle }: { sid: string; agentId: string; handle: string }) {
  const [text, setText] = useState('');
  const [status, setStatus] = useState('连接中');
  useEffect(() => {
    const socket = new WebSocket(
      socketUrl(
        `/api/v1/sessions/${encodeURIComponent(sid)}/agent-pty/${encodeURIComponent(agentId)}/${encodeURIComponent(handle)}`,
      ),
    );
    socket.onopen = () => socket.send(JSON.stringify({ op: 'auth', id: 0, token: token() }));
    socket.onmessage = (message) => {
      const frame = JSON.parse(String(message.data)) as Record<string, unknown>;
      if (frame.error) {
        const error = frame.error as Record<string, string>;
        setStatus(`${error.received}；${error.next_action}`);
        return;
      }
      setText((current) => (current + String(frame.text ?? '')).slice(-200_000));
      setStatus(frame.running ? '运行中' : `已退出（${String(frame.exit_code)}）`);
    };
    return () => socket.close();
  }, [sid, agentId, handle]);
  return (
    <div className="agent-screen">
      <p className="meta">
        <code>{handle}</code> · {status} · 只读
      </p>
      <pre>{text}</pre>
    </div>
  );
}

interface Opened {
  key: string;
  agentId: string;
  handle: string;
}

// 只列 Terminal 开的会话：后台 Bash 的 tool_meta 里也有 handle，但它不是终端，镜像只会报找不到。
// 终端在开它的那个 Agent 的工作区里（可能是隔离副本），所以句柄要连同它所在的那份记录一起记。
function openedTerminals(state: SessionState): Opened[] {
  const opened = new Map<string, Opened>();
  for (const [agentId, transcript] of Object.entries(state.transcripts)) {
    const terminalCalls = new Set<string>();
    for (const block of transcript.blocks) {
      for (const call of block?.tool_calls ?? []) {
        if (call.name === 'Terminal') terminalCalls.add(call.id);
      }
      const handle = block?.tool_meta?.handle;
      if (typeof handle !== 'string' || !block?.tool_call_id || !terminalCalls.has(block.tool_call_id)) continue;
      const key = `${agentId}/${handle}`;
      opened.set(key, { key, agentId, handle });
    }
  }
  return [...opened.values()];
}

export function Terminals({ state }: { state: SessionState }) {
  const opened = openedTerminals(state);
  const [chosen, setChosen] = useState<string>('human');
  const screen = opened.find((terminal) => terminal.key === chosen);
  return (
    <section className="terminals" aria-label="终端">
      <div className="tabs">
        <button type="button" aria-pressed={chosen === 'human'} onClick={() => setChosen('human')}>
          我的终端
        </button>
        {opened.map((terminal) => (
          <button
            type="button"
            key={terminal.key}
            aria-pressed={chosen === terminal.key}
            onClick={() => setChosen(terminal.key)}
          >
            {state.agents[terminal.agentId]?.name ?? 'Agent'} {terminal.handle}
          </button>
        ))}
      </div>
      {screen ? (
        <AgentScreen key={screen.key} sid={state.sid} agentId={screen.agentId} handle={screen.handle} />
      ) : (
        <HumanTerminal sid={state.sid} />
      )}
    </section>
  );
}
