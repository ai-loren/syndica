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

function AgentScreen({ sid, handle }: { sid: string; handle: string }) {
  const [text, setText] = useState('');
  const [status, setStatus] = useState('连接中');
  useEffect(() => {
    const socket = new WebSocket(
      socketUrl(`/api/v1/sessions/${encodeURIComponent(sid)}/agent-pty/${encodeURIComponent(handle)}`),
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
  }, [sid, handle]);
  return (
    <div className="agent-screen">
      <p className="meta">
        <code>{handle}</code> · {status} · 只读
      </p>
      <pre>{text}</pre>
    </div>
  );
}

export function Terminals({ state }: { state: SessionState }) {
  const handles = new Set<string>();
  for (const transcript of Object.values(state.transcripts)) {
    for (const block of transcript.blocks) {
      const handle = block?.tool_meta?.handle;
      if (typeof handle === 'string') handles.add(handle);
    }
  }
  const [chosen, setChosen] = useState<string>('human');
  return (
    <section className="terminals" aria-label="终端">
      <div className="tabs">
        <button type="button" aria-pressed={chosen === 'human'} onClick={() => setChosen('human')}>
          我的终端
        </button>
        {[...handles].map((handle) => (
          <button type="button" key={handle} aria-pressed={chosen === handle} onClick={() => setChosen(handle)}>
            Agent {handle}
          </button>
        ))}
      </div>
      {chosen === 'human' ? (
        <HumanTerminal sid={state.sid} />
      ) : (
        <AgentScreen key={chosen} sid={state.sid} handle={chosen} />
      )}
    </section>
  );
}
