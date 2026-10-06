import { defineConfig } from '@playwright/test';

// 起真的 `syndica up`（模型指向 tests/e2e/fake_openai.py），桌面与移动两种视口各跑一遍。
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  workers: 1,
  fullyParallel: false,
  retries: 0,
  reporter: [['list']],
  globalSetup: './e2e/setup.ts',
  globalTeardown: './e2e/teardown.ts',
  use: {
    storageState: 'e2e/.state/storage.json',
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1280, height: 800 } } },
    { name: 'mobile', use: { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true } },
  ],
});
