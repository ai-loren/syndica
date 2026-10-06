// 从 OpenAPI 快照的 x-syndica-events / x-syndica-rpc 生成前端类型：openapi-typescript 只生成
// paths 与 components，事件种类与 RPC 帧在扩展字段里。
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';

const spec = JSON.parse(readFileSync(new URL('../../docs/reference/openapi.json', import.meta.url), 'utf8'));
const events = spec['x-syndica-events'];
const ops = Object.keys(spec['x-syndica-rpc'].ops);
const lines = [
  '// 由 scripts/gen-events.mjs 从 docs/reference/openapi.json 生成；不要手改。',
  `export const EVENT_KINDS = ${JSON.stringify(Object.keys(events))} as const;`,
  'export type EventKind = (typeof EVENT_KINDS)[number];',
  `export const RPC_OPS = ${JSON.stringify(ops)} as const;`,
  'export type RpcOp = (typeof RPC_OPS)[number];',
  'export interface EventPayloads {',
  ...Object.entries(events).map(
    ([kind, schema]) =>
      `  ${kind}: { ${schema.required.map((key) => `${key}: unknown;`).join(' ')} [key: string]: unknown };`,
  ),
  '}',
  '',
];
mkdirSync(new URL('../src/generated/', import.meta.url), { recursive: true });
writeFileSync(new URL('../src/generated/events.ts', import.meta.url), lines.join('\n'));
