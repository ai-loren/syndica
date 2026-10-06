import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

export const REPO = resolve(import.meta.dirname, '..', '..');
export const STATE = resolve(import.meta.dirname, '.state');

export interface Server {
  url: string;
  login: string;
  workspace: string;
  env: Record<string, string>;
}

function environment(config: string, fake: number | null): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value === undefined) continue;
    if (/^(SYNDICA_|OPENAI_|GEMINI_|OPENROUTER_|XDG_)/.test(key)) continue;
    env[key] = value;
  }
  env.XDG_CONFIG_HOME = config;
  if (fake !== null) {
    env.SYNDICA_LLM_ORDER = 'openai';
    env.OPENAI_BASE_URL = `http://127.0.0.1:${fake}/v1`;
    env.OPENAI_MODEL = 'fake';
    env.OPENAI_API_KEY = 'e2e-dummy-key-0123456789';
  }
  return env;
}

export async function startFake(): Promise<{ port: number; process: ChildProcess }> {
  const child = spawn('uv', ['run', '--project', REPO, 'python', join(REPO, 'tests', 'e2e', 'fake_openai.py')], {
    stdio: ['ignore', 'pipe', 'inherit'],
    detached: true,
  });
  const port = await new Promise<number>((done, fail) => {
    child.stdout?.once('data', (chunk: Buffer) => done(Number(String(chunk).trim())));
    child.once('exit', () => fail(new Error('fake model exited')));
  });
  // 拿到端口就放手：留着管道，Node 会一直等它，测试跑完也不退出。
  child.stdout?.destroy();
  child.unref();
  return { port, process: child };
}

export function up(name: string, fake: number | null, yaml: string): Server {
  const root = mkdtempSync(join(tmpdir(), `syndica-e2e-${name}-`));
  const workspace = join(root, 'ws');
  mkdirSync(workspace);
  writeFileSync(join(workspace, '.syndica.yaml'), yaml);
  const env = environment(join(root, 'config'), fake);
  const syndica = (...args: string[]) =>
    spawnSync('uv', ['run', '--project', REPO, 'python', '-m', 'syndica', ...args], {
      cwd: workspace,
      env,
      encoding: 'utf8',
      timeout: 90_000,
    });
  // 工作区里的配置要人信任过才用：照错误信息说的，看过之后按摘要信任。
  const probe = syndica('config', 'trust');
  const digest = /--sha256 ([0-9a-f]{64})/.exec(`${probe.stdout}${probe.stderr}`);
  if (digest) syndica('config', 'trust', '--sha256', digest[1] ?? '');
  const result = spawnSync('uv', ['run', '--project', REPO, 'python', '-m', 'syndica', 'up', '--no-open'], {
    cwd: workspace,
    env,
    encoding: 'utf8',
    timeout: 90_000,
  });
  const match = /http:\/\/127\.0\.0\.1:(\d+)\/#login=([A-Za-z0-9_-]+)/.exec(result.stdout);
  if (result.status !== 0 || !match) {
    throw new Error(`syndica up failed: ${result.stdout}\n${result.stderr}`);
  }
  return { url: `http://127.0.0.1:${match[1]}`, login: match[2] ?? '', workspace, env };
}

export function down(server: Server): void {
  spawnSync('uv', ['run', '--project', REPO, 'python', '-m', 'syndica', 'down'], {
    cwd: server.workspace,
    env: server.env,
    timeout: 60_000,
  });
}

export function saved(): Server & { fake: number; fakePid: number } {
  return JSON.parse(readFileSync(join(STATE, 'server.json'), 'utf8')) as Server & { fake: number; fakePid: number };
}
