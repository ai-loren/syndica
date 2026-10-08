// 凭证只在按源隔离的 localStorage 里，经 Authorization 头发送；server 从不发 cookie。

const KEY = 'syndica.token';

export class Problem extends Error {
  constructor(
    readonly status: number,
    readonly expected: string,
    readonly received: string,
    readonly nextAction: string,
  ) {
    super(`${expected} / ${received} / ${nextAction}`);
  }
}

/**
 * server 从不注册 Service Worker。页面却被一个控制着：多半是 server 没在跑时，别的进程在同一个
 * 端口上起过页面并注册了它，它会截下登录码与之后的每个 API 请求。这时不登录、不带令牌发请求。
 */
export function interceptedByServiceWorker(): Problem | null {
  if (!('serviceWorker' in navigator) || !navigator.serviceWorker.controller) return null;
  return new Problem(
    0,
    '这个页面不被任何 Service Worker 接管（Syndica 从不注册它）',
    `页面被 ${navigator.serviceWorker.controller.scriptURL} 接管着，它能截下登录码与令牌`,
    '在浏览器的站点设置里清除这个地址的站点数据，再从 syndica down / syndica up 新打印的链接打开',
  );
}

export function token(): string | null {
  return localStorage.getItem(KEY);
}

export function forget(): void {
  localStorage.removeItem(KEY);
}

async function problem(response: Response): Promise<Problem> {
  try {
    const body = (await response.json()) as Record<string, unknown>;
    return new Problem(
      response.status,
      String(body.expected ?? '请求成功'),
      String(body.received ?? `HTTP ${response.status}`),
      String(body.next_action ?? '看 server 日志'),
    );
  } catch {
    return new Problem(response.status, '请求成功', `HTTP ${response.status}`, '看 server 日志');
  }
}

/** `#login=<code>` 换令牌；换完把片段从地址栏去掉，刷新不会再用一次。 */
export async function loginFromHash(): Promise<void> {
  const match = /^#login=([A-Za-z0-9_-]+)$/.exec(location.hash);
  if (!match) return;
  history.replaceState(null, '', location.pathname);
  const response = await fetch('/api/v1/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code: match[1] }),
  });
  if (!response.ok) throw await problem(response);
  const body = (await response.json()) as { token: string };
  localStorage.setItem(KEY, body.token);
}

export function authHeaders(): Record<string, string> {
  const current = token();
  return current ? { Authorization: `Bearer ${current}` } : {};
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { ...authHeaders() };
  if (init.body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { ...init, headers: { ...headers, ...(init.headers ?? {}) } });
  if (response.status === 401) forget();
  if (!response.ok) throw await problem(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
