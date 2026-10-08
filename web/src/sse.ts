import { authHeaders } from './api';

// EventSource 带不了 Authorization 头，所以用 fetch 读流，自己带 Last-Event-ID 重连。

export interface Frame {
  id: string | null;
  kind: string;
  data: Record<string, unknown>;
}

export interface Follow {
  stop(): void;
}

function parse(raw: string): Frame | null {
  let id: string | null = null;
  let kind = 'message';
  const data: string[] = [];
  for (const line of raw.split('\n')) {
    if (line.startsWith(':')) continue;
    if (line.startsWith('id: ')) id = line.slice(4);
    else if (line.startsWith('event: ')) kind = line.slice(7);
    else if (line.startsWith('data: ')) data.push(line.slice(6));
  }
  if (data.length === 0) return null;
  return { id, kind, data: JSON.parse(data.join('\n')) as Record<string, unknown> };
}

export function follow(
  path: string,
  from: string | null,
  onFrame: (frame: Frame) => void,
  onConnected: (connected: boolean) => void,
): Follow {
  let last = from;
  let stopped = false;
  let controller = new AbortController();
  let delay = 500;

  async function once(): Promise<void> {
    controller = new AbortController();
    const headers: Record<string, string> = { ...authHeaders() };
    if (last) headers['Last-Event-ID'] = last;
    const response = await fetch(path, { headers, signal: controller.signal });
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
    onConnected(true);
    delay = 500;
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += value;
      let cut = buffer.indexOf('\n\n');
      while (cut >= 0) {
        const frame = parse(buffer.slice(0, cut));
        buffer = buffer.slice(cut + 2);
        // 停下之后同一块里剩下的帧不再交出：换成新流时它们会由新流从快照的 as_of 重新补发。
        if (stopped) return;
        if (frame) {
          if (frame.id) last = frame.id;
          onFrame(frame);
        }
        cut = buffer.indexOf('\n\n');
      }
    }
  }

  void (async () => {
    while (!stopped) {
      try {
        await once();
      } catch {
        // 断线：稍后带着最后一个 id 重连，缓冲里还在的事件会补发。
      }
      if (stopped) return;
      onConnected(false);
      await new Promise((resolve) => setTimeout(resolve, delay));
      delay = Math.min(delay * 2, 8000);
    }
  })();

  return {
    stop() {
      stopped = true;
      controller.abort();
    },
  };
}
