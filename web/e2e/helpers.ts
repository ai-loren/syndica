import { expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { saved, STATE } from './server';

export const server = saved();

export function mobile(page: Page): boolean {
  return (page.viewportSize()?.width ?? 1280) < 768;
}

export function storedToken(): string {
  const state = JSON.parse(readFileSync(join(STATE, 'storage.json'), 'utf8')) as {
    origins: { localStorage: { name: string; value: string }[] }[];
  };
  for (const origin of state.origins) {
    for (const item of origin.localStorage) if (item.name === 'syndica.token') return item.value;
  }
  throw new Error('no stored token');
}

export async function openApp(page: Page): Promise<void> {
  await page.goto(`${server.url}/`);
  await expect(page.getByRole('heading', { name: 'Syndica' })).toBeVisible();
}

export async function newSession(page: Page, role: string): Promise<string> {
  if (mobile(page)) await page.getByRole('button', { name: '会话' }).click();
  await page.getByRole('combobox', { name: '角色' }).selectOption(role);
  await page.getByRole('button', { name: '新会话' }).click();
  await expect(page.getByRole('textbox', { name: '消息' })).toBeVisible();
  await expect(page).toHaveURL(/\?session=/);
  return new URL(page.url()).searchParams.get('session') ?? '';
}

export async function send(page: Page, text: string): Promise<void> {
  await page.getByRole('textbox', { name: '消息' }).fill(text);
  await page.getByRole('button', { name: '发送' }).click();
}

export async function showPanel(page: Page, tab: string): Promise<void> {
  if (mobile(page) && !(await page.getByRole('tab', { name: tab }).isVisible())) {
    await page.getByRole('button', { name: '面板' }).click();
  }
  await page.getByRole('tab', { name: tab }).click();
}

export async function hidePanel(page: Page): Promise<void> {
  if (mobile(page) && (await page.getByRole('tab', { name: 'Agent' }).isVisible())) {
    await page.getByRole('button', { name: '面板' }).click();
  }
}

export async function noHorizontalScroll(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}
