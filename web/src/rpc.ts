import { token } from './api';

export interface RpcError {
  expected: string;
  received: string;
  next_action: string;
}

export interface RpcReply {
  id: number | null;
  ok: boolean;
  error: RpcError | null;
  result: Record<string, unknown> | null;
}

// 一个会话一条 WebSocket：第一帧认证，此后逐条请求、按 id 对回复。断开后自动重连。
export class Rpc {
  private socket: WebSocket | null = null;
  private next = 1;
  private waiting = new Map<number, (reply: RpcReply) => void>();
  private closed = false;
  private ready: Promise<void> | null = null;

  constructor(private readonly sid: string) {}

  private open(): Promise<void> {
    if (this.ready) return this.ready;
    this.ready = new Promise<void>((resolve, reject) => {
      const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
      const socket = new WebSocket(`${scheme}://${location.host}/api/v1/sessions/${this.sid}/rpc`);
      this.socket = socket;
      socket.onopen = () => socket.send(JSON.stringify({ op: 'auth', id: 0, token: token() }));
      socket.onmessage = (message) => {
        const reply = JSON.parse(String(message.data)) as RpcReply;
        if (reply.id === 0) {
          if (reply.ok) resolve();
          else reject(new Error('认证失败'));
          return;
        }
        const done = reply.id === null ? undefined : this.waiting.get(reply.id);
        if (done && reply.id !== null) {
          this.waiting.delete(reply.id);
          done(reply);
        }
      };
      socket.onclose = () => {
        this.ready = null;
        this.socket = null;
        for (const [, done] of this.waiting) {
          done({
            id: null,
            ok: false,
            error: {
              expected: '与 server 的连接保持着',
              received: '连接断开了',
              next_action: '稍等自动重连后再试一次',
            },
            result: null,
          });
        }
        this.waiting.clear();
        reject(new Error('连接断开'));
      };
    });
    this.ready.catch(() => undefined);
    return this.ready;
  }

  async call(op: string, fields: Record<string, unknown> = {}): Promise<RpcReply> {
    if (this.closed) throw new Error('会话已关闭');
    await this.open();
    const id = this.next++;
    return new Promise<RpcReply>((resolve) => {
      this.waiting.set(id, resolve);
      this.socket?.send(JSON.stringify({ op, id, ...fields }));
    });
  }

  close(): void {
    this.closed = true;
    this.socket?.close();
  }
}
