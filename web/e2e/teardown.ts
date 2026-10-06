import { down, saved } from './server';

export default function teardown(): void {
  const server = saved();
  down(server);
  try {
    process.kill(-server.fakePid);
  } catch {
    // 已经退出。
  }
}
