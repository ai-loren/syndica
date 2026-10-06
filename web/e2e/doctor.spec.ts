import { expect, test } from '@playwright/test';
import { down, up, type Server } from './server';

let server: Server;

test.beforeAll(() => {
  server = up('nokey', null, 'version: 1\n');
});

test.afterAll(() => {
  down(server);
});

test.use({ storageState: { cookies: [], origins: [] } });

test('without an API key the page explains what to do instead of going blank', async ({ page }) => {
  await page.goto(`${server.url}/#login=${server.login}`);
  const doctor = page.getByRole('region', { name: '自检' });
  await expect(doctor).toBeVisible();
  await expect(doctor).toContainText('期望');
  await expect(doctor).toContainText('实际');
  await expect(doctor).toContainText('下一步');
  if ((page.viewportSize()?.width ?? 1280) < 768) await page.getByRole('button', { name: '会话' }).click();
  await page.getByRole('button', { name: '新会话' }).click();
  await expect(page.getByRole('alert').filter({ hasText: '下一步' }).first()).toBeVisible();
});
