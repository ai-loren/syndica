import { chromium } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { STATE, startFake, up } from './server';

const YAML = 'version: 1\nmodules: [acceptance]\n';

export default async function setup(): Promise<void> {
  mkdirSync(STATE, { recursive: true });
  const fake = await startFake();
  let server;
  try {
    server = up('main', fake.port, YAML);
  } catch (error) {
    process.kill(-(fake.process.pid ?? 0));
    throw error;
  }
  writeFileSync(
    join(STATE, 'server.json'),
    JSON.stringify({ ...server, fake: fake.port, fakePid: fake.process.pid }),
  );
  // 经真的 #login 链接登录一次：令牌进 localStorage，各用例沿用这份存储。
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(`${server.url}/#login=${server.login}`);
  await page.getByRole('heading', { name: 'Syndica' }).waitFor();
  if (new URL(page.url()).hash !== '') throw new Error('login fragment was not removed');
  await page.context().storageState({ path: join(STATE, 'storage.json') });
  await browser.close();
}
