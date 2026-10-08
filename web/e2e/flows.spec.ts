import { expect, test } from '@playwright/test';
import { hidePanel, mobile, newSession, noHorizontalScroll, openApp, send, server, showPanel, storedToken } from './helpers';

test('a session delegates and shows the agent tree and the event timeline', async ({ page }) => {
  await openApp(page);
  await newSession(page, 'Orchestrator');
  await send(page, '请派活给 Worker');
  await expect(page.locator('.block.assistant').last()).toContainText('完成', { timeout: 30_000 });
  await showPanel(page, 'Agent');
  await expect(page.locator('.agents li[data-agent-id]')).toHaveCount(2);
  await expect(page.locator('.agents')).toContainText('Worker');
  await showPanel(page, '时间线');
  await expect(page.locator('.timeline li[data-kind="agent_spawned"]')).toHaveCount(2);
  await noHorizontalScroll(page);
});

test('an approved confirmation runs the tool', async ({ page }) => {
  await openApp(page);
  await newSession(page, 'Worker');
  await send(page, '请确认后读取');
  const dialog = page.getByRole('dialog', { name: '需要你答复' });
  await expect(dialog).toBeVisible({ timeout: 30_000 });
  await expect(dialog).toContainText('shell.path_outside_workspace');
  await expect(dialog).toContainText('cat /etc/hosts');
  await expect(dialog.getByRole('button', { name: '批准' })).toBeInViewport();
  await noHorizontalScroll(page);
  await dialog.getByRole('button', { name: '批准' }).click();
  await expect(dialog).toBeHidden();
  await expect(page.locator('details.block.tool pre').last()).toContainText('localhost', { timeout: 30_000 });
  await expect(page.locator('.tool-calls .outcome').last()).toHaveText('ok');
});

test('a rejected confirmation reaches the agent as a rejection', async ({ page }) => {
  await openApp(page);
  await newSession(page, 'Worker');
  await send(page, '请确认后读取');
  const dialog = page.getByRole('dialog', { name: '需要你答复' });
  await dialog.getByRole('button', { name: '拒绝' }).click({ timeout: 30_000 });
  await expect(dialog).toBeHidden();
  await expect(page.locator('.tool-calls .outcome').last()).not.toHaveText('ok', { timeout: 30_000 });
  await expect(page.locator('.tool-calls .outcome').last()).not.toHaveText('进行中');
  await expect(page.locator('details.block.tool pre').last()).not.toContainText('localhost');
});

test('artifacts can be previewed, diffed and given feedback that a human activates', async ({ page }) => {
  await openApp(page);
  await newSession(page, 'Worker');
  await send(page, '写文件');
  await expect(page.locator('.block.assistant').last()).toContainText('完成', { timeout: 30_000 });
  await send(page, '再写文件');
  await expect(page.locator('.block.assistant')).toHaveCount(2, { timeout: 30_000 });
  await showPanel(page, '产物');
  await page.getByRole('button', { name: 'report.md' }).click();
  await expect(page.locator('.preview')).toContainText('结论');
  await page.getByRole('button', { name: '对比最近两版' }).click();
  await expect(page.locator('.diff pre')).toContainText('+');
  await page.getByRole('textbox', { name: '对这个产物的反馈' }).fill('结论需要给出具体的数字');
  await page.getByRole('textbox', { name: '行范围' }).fill('3');
  await page.getByRole('button', { name: '提交反馈' }).click();
  const proposed = page.locator('.rubrics li[data-status="proposed"]');
  await expect(proposed).toContainText('结论需要给出具体的数字');
  await proposed.getByRole('button', { name: '生效' }).click();
  await expect(page.locator('.rubrics li[data-status="active"]')).toContainText('结论需要给出具体的数字');
  await noHorizontalScroll(page);
});

test('a script inside an artifact is shown as text and never runs', async ({ page }) => {
  const pwned: string[] = [];
  page.on('request', (request) => {
    if (request.url().includes('x=pwned')) pwned.push(request.url());
  });
  await openApp(page);
  await newSession(page, 'Worker');
  await send(page, '写一个网页');
  await expect(page.locator('.block.assistant').last()).toContainText('完成', { timeout: 30_000 });
  await showPanel(page, '产物');
  await page.getByRole('button', { name: 'page.html' }).click();
  await expect(page.locator('.preview')).toContainText('<script>');
  await page.waitForTimeout(500);
  expect(pwned).toEqual([]);
});

test('events that happen while offline arrive after reconnecting', async ({ page, context }) => {
  await openApp(page);
  const sid = await newSession(page, 'Worker');
  await send(page, '第一条');
  await expect(page.locator('.block.assistant').last()).toContainText('收到：第一条', { timeout: 30_000 });
  await context.setOffline(true);
  // 离线模拟不切断已经建立的流：从页面里中止在飞的请求，重连在离线期间只会失败。
  await page.evaluate(() => window.stop());
  await expect(page.locator('.offline')).toBeVisible({ timeout: 20_000 });
  const socket = new WebSocket(`${server.url.replace('http', 'ws')}/api/v1/sessions/${sid}/rpc`);
  await new Promise<void>((resolve) => socket.addEventListener('open', () => resolve()));
  const replies: unknown[] = [];
  socket.addEventListener('message', (message) => replies.push(JSON.parse(String(message.data))));
  socket.send(JSON.stringify({ op: 'auth', id: 0, token: storedToken() }));
  socket.send(JSON.stringify({ op: 'send', id: 1, text: '离线期间的消息' }));
  await expect.poll(() => replies.length).toBe(2);
  socket.close();
  await page.waitForTimeout(3000);
  await context.setOffline(false);
  await expect(page.locator('.block.assistant').last()).toContainText('收到：离线期间的消息', { timeout: 30_000 });
  await expect(page.locator('.offline')).toBeHidden();
});

test('after a reset the stream restarts from the refetched snapshot', async ({ page }) => {
  await openApp(page);
  const sid = await newSession(page, 'Worker');
  await send(page, '第一条');
  await expect(page.locator('.block.assistant').last()).toContainText('收到：第一条', { timeout: 30_000 });
  const positions: string[] = [];
  page.on('response', async (response) => {
    if (response.url().endsWith(`/api/v1/sessions/${sid}/snapshot`)) {
      positions.push(((await response.json()) as { as_of: string }).as_of);
    }
  });
  const resumedFrom: string[] = [];
  let release = () => {};
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  let first = true;
  await page.route(`**/api/v1/sessions/${sid}/events`, async (route) => {
    if (first) {
      first = false;
      await held;
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'event: reset\ndata: {}\n\n' });
      return;
    }
    resumedFrom.push(route.request().headers()['last-event-id'] ?? '');
    await route.continue();
  });
  await page.reload();
  await expect.poll(() => positions.length).toBe(1);
  // 页面看不见的这段时间里会话往前走：重取的快照的位置于是与第一次的不同。
  const socket = new WebSocket(`${server.url.replace('http', 'ws')}/api/v1/sessions/${sid}/rpc`);
  await new Promise<void>((resolve) => socket.addEventListener('open', () => resolve()));
  const replies: unknown[] = [];
  socket.addEventListener('message', (message) => replies.push(JSON.parse(String(message.data))));
  socket.send(JSON.stringify({ op: 'auth', id: 0, token: storedToken() }));
  socket.send(JSON.stringify({ op: 'send', id: 1, text: '第二条' }));
  await expect.poll(() => replies.length).toBe(2);
  socket.close();
  await page.waitForTimeout(1500);
  release();
  await expect.poll(() => positions.length, { timeout: 10_000 }).toBe(2);
  expect(positions[1]).not.toBe(positions[0]);
  await expect.poll(() => resumedFrom, { timeout: 10_000 }).toContain(positions[1]);
  await expect(page.locator('.block.assistant').last()).toContainText('收到：第二条', { timeout: 30_000 });
});

test('a refresh restores the session and its pending confirmation', async ({ page }) => {
  await openApp(page);
  await newSession(page, 'Worker');
  await send(page, '请确认后读取');
  await expect(page.getByRole('dialog', { name: '需要你答复' })).toBeVisible({ timeout: 30_000 });
  await page.reload();
  const dialog = page.getByRole('dialog', { name: '需要你答复' });
  await expect(dialog).toBeVisible();
  await expect(page.locator('.block.user').first()).toContainText('请确认后读取');
  await dialog.getByRole('button', { name: '批准' }).click();
  await expect(page.locator('details.block.tool pre').last()).toContainText('localhost', { timeout: 30_000 });
});

test('the timeline and the trace list the same tool calls', async ({ page }) => {
  await openApp(page);
  await newSession(page, 'Worker');
  await send(page, '写文件');
  await expect(page.locator('.block.assistant').last()).toContainText('完成', { timeout: 30_000 });
  const streamed = await page.locator('.tool-calls li[data-tool-call-id]').evaluateAll((items) =>
    items.map((item) => item.getAttribute('data-tool-call-id')),
  );
  await showPanel(page, 'trace');
  await expect(page.locator('[data-consistent="true"]')).toBeVisible();
  const traced = await page.locator('.trace tr[data-call-id]').evaluateAll((rows) =>
    rows.map((row) => row.getAttribute('data-call-id')),
  );
  expect(traced).toEqual(streamed);
  expect(streamed.length).toBeGreaterThan(0);
});

test('the human terminal works over a websocket', async ({ page }) => {
  await openApp(page);
  await newSession(page, 'Worker');
  await showPanel(page, '终端');
  const terminal = page.getByTestId('human-terminal');
  await expect(terminal.locator('.xterm-rows')).toBeVisible();
  await terminal.click();
  await page.keyboard.type('echo hello-$((40+2))');
  await page.keyboard.press('Enter');
  await expect(terminal.locator('.xterm-rows')).toContainText('hello-42', { timeout: 15_000 });
});

test("an agent's terminal is mirrored read-only and a background command is not listed as one", async ({
  page,
}) => {
  await openApp(page);
  await newSession(page, 'Operator');
  await send(page, '后台跑一个');
  await expect(page.locator('.block.assistant').last()).toContainText('完成', { timeout: 30_000 });
  await send(page, '开一个终端');
  await expect(page.locator('.block.assistant', { hasText: '完成' })).toHaveCount(2, { timeout: 30_000 });
  await showPanel(page, '终端');
  const tabs = page.locator('.terminals .tabs button');
  await expect(tabs).toHaveCount(2);
  await tabs.last().click();
  await expect(page.locator('.agent-screen pre')).toContainText('mirror-ok', { timeout: 15_000 });
  await expect(page.locator('.agent-screen .meta')).toContainText('运行中');
});

test('the chat stays usable in this viewport', async ({ page }) => {
  await openApp(page);
  await newSession(page, 'Worker');
  await hidePanel(page);
  await expect(page.getByRole('textbox', { name: '消息' })).toBeInViewport();
  await expect(page.getByRole('button', { name: '发送' })).toBeInViewport();
  await noHorizontalScroll(page);
  if (mobile(page)) {
    await expect(page.getByRole('button', { name: '会话' })).toBeVisible();
    await expect(page.getByRole('button', { name: '面板' })).toBeVisible();
  }
});

test('an unverified verdict is never shown as a pass', async ({ page }) => {
  await openApp(page);
  const sid = await newSession(page, 'Worker');
  const frame = { agent_id: sid, payload: { stop_reason: 'completed', verdict: 'unverified' }, time_ns: 1 };
  await page.route(`**/api/v1/sessions/${sid}/events`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: `id: e2e:999\nevent: agent_end\ndata: ${JSON.stringify(frame)}\n\n`,
    }),
  );
  await page.reload();
  await showPanel(page, 'Agent');
  const badge = page.locator(`.agents li[data-agent-id="${sid}"] .verdict`).first();
  await expect(badge).toHaveText('未验收');
  await expect(badge).toHaveAttribute('data-verdict', 'unverified');
  await expect(badge).not.toHaveClass(/\bok\b/);
  await expect(page.locator('.agents')).not.toContainText('通过');
});
