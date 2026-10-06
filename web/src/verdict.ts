// 裁决四值逐个映射：`unverified` 是「没能验证」，永远不能长得像通过。

export type Verdict = 'pass' | 'partial' | 'block' | 'unverified';

export const VERDICTS: Record<Verdict, { label: string; tone: 'ok' | 'warn' | 'bad' | 'unknown' }> = {
  pass: { label: '通过', tone: 'ok' },
  partial: { label: '部分通过', tone: 'warn' },
  block: { label: '未通过', tone: 'bad' },
  unverified: { label: '未验收', tone: 'unknown' },
};

export function verdictOf(raw: unknown): Verdict | null {
  switch (raw) {
    case 'pass':
    case 'partial':
    case 'block':
    case 'unverified':
      return raw;
    default:
      return null;
  }
}
