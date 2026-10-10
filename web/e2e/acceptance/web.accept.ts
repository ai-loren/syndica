import { expect, test } from '@playwright/test';
import { join } from 'node:path';

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`期望: 环境变量 ${name}。实际: 没有设。下一步: 用 scripts/acceptance.sh 运行这个用例。`);
  return value;
}

const url = required('ACCEPTANCE_URL');
const login = required('ACCEPTANCE_LOGIN');
const prompt = required('ACCEPTANCE_PROMPT');
const shots = required('ACCEPTANCE_OUT');

test('an Orchestrator delegates to a Worker, a person approves the risky step, the artifact previews', async ({
  page,
}) => {
  await page.goto(`${url}/#login=${login}`);
  await expect(page.getByRole('heading', { name: 'Syndica' })).toBeVisible();
  await page.getByRole('combobox', { name: '角色' }).selectOption('Orchestrator');
  await page.getByRole('button', { name: '新会话' }).click();
  const box = page.getByRole('textbox', { name: '消息' });
  await expect(box).toBeVisible();
  await box.fill(prompt);
  await page.getByRole('button', { name: '发送' }).click();

  const dialog = page.getByRole('dialog', { name: '需要你答复' });
  await expect(dialog).toBeVisible({ timeout: 90_000 });
  await expect(dialog).toContainText('shell.path_outside_workspace');
  await expect(dialog).toContainText('../outbox/incident.md');
  await page.screenshot({ path: join(shots, 'web-1-confirmation.png') });
  await dialog.getByRole('button', { name: '批准' }).click();
  await expect(dialog).toBeHidden();

  await expect(page.locator('.block.assistant').last()).toContainText('incident.md', { timeout: 90_000 });
  await page.getByRole('tab', { name: 'Agent' }).click();
  await expect(page.locator('.agents li[data-agent-id]')).toHaveCount(2);
  await expect(page.locator('.agents')).toContainText('Orchestrator');
  await expect(page.locator('.agents')).toContainText('Worker');
  await page.screenshot({ path: join(shots, 'web-2-agents.png') });

  await page.getByRole('tab', { name: '产物' }).click();
  await page.getByRole('button', { name: 'incident.md' }).click();
  await expect(page.locator('.preview')).toContainText('Incident');
  await page.screenshot({ path: join(shots, 'web-3-artifact.png') });
});
