import { defineConfig } from '@playwright/test';

// 发布验收用：不起服务。scripts/acceptance.sh 从构建出的 wheel 起 `syndica up`，把地址、一次性登录码
// 与截图目录经环境变量交进来。文件名不是 *.spec.ts：主配置的 testDir 是 e2e/，不能把它收进去。
export default defineConfig({
  testDir: '.',
  testMatch: '*.accept.ts',
  timeout: 180_000,
  expect: { timeout: 30_000 },
  workers: 1,
  retries: 0,
  reporter: [['list']],
  outputDir: process.env.ACCEPTANCE_OUT ? `${process.env.ACCEPTANCE_OUT}/playwright` : 'test-results',
  use: { viewport: { width: 1280, height: 800 }, trace: 'retain-on-failure' },
});
