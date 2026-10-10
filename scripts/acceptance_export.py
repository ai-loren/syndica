"""发布验收 (d)：给批量任务的剧本埋一个密钥，再核对 `syndica export` 写出的四种文件。

- `prepare <示例剧本> <输出剧本> <密钥>`：在 release-1.2 那个任务的最终回答里带上密钥，像模型
  把看到的凭据说了出来。宿主环境里有同一个值（`ACCEPTANCE_DEPLOY_TOKEN`），落盘之前就该被换成占位。
- `check <导出目录> <密钥> <unverified 个数> [--scripted]`：每种格式都有 manifest，
  `include_unverified` 是 false，`dropped_unverified` 的个数等于 batch 报出的 unverified 个数，
  导出的行里没有被排除的会话；任何导出文件里都没有密钥。`--scripted`（剧本跑的）再核对确切内容：
  三条样本恰好是 pass / block / partial，一对偏好 pass 对 block，RL 奖励 1.0 / 0.5 / 0.0，
  SFT 里有密钥换成的占位（证明它真的流过）。

只用标准库。不符时按「期望 / 实际 / 下一步」报到 stderr、退出 1；通过时把摘要以 JSON 打到 stdout。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

FORMATS = {
    'sft': 'messages.jsonl',
    'preference': 'preference.jsonl',
    'rl': 'rl.jsonl',
    'trajectory': 'trajectories.jsonl',
}
_TASK = 'version 1.2'
_PLACEHOLDER = '[REDACTED:secret.env_value]'


class CheckFailed(Exception):
    pass


def _check(condition: bool, expected: str, received: object, next_action: str) -> None:
    if not condition:
        raise CheckFailed(f'期望: {expected}\n实际: {received}\n下一步: {next_action}')


def prepare(source: Path, target: Path, secret: str) -> None:
    script = json.loads(source.read_text(encoding='utf-8'))
    rules = [rule for rule in script['rules'] if rule.get('when') == _TASK]
    _check(
        len(rules) == 1,
        f'示例剧本里恰好一条 when={_TASK!r} 的规则',
        f'{len(rules)} 条',
        '对照 examples/training-export/offline/script.json 改 scripts/acceptance_export.py',
    )
    last = rules[0]['steps'][-1]
    last['content'] = f'{last["content"]} The deploy token I used was {secret}.'
    target.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding='utf-8')


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]


def check(root: Path, secret: str, unverified: int, *, scripted: bool) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name, data in FORMATS.items():
        folder = root / name
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        rows = _rows(folder / data)
        dropped = list(manifest['dropped_unverified'])
        _check(
            manifest['include_unverified'] is False and len(dropped) == unverified,
            f'{name}：缺省不收 unverified，manifest 记下 batch 报出的 {unverified} 个这样的会话',
            f'include_unverified={manifest["include_unverified"]}，dropped_unverified={dropped}',
            '这是「unverified 缺省排除」的回归：查 runner/training 的导出',
        )
        last = [sample['verdicts'][-1] for sample in manifest['samples'] if sample['verdicts']]
        _check(
            'unverified' not in last
            and not {sample['id'] for sample in manifest['samples']} & set(dropped),
            f'{name}：manifest 的样本里没有 unverified、也没有被排除的会话',
            f'样本的最后裁决 {last}',
            '查导出的筛选',
        )
        if name != 'preference':
            ids = [row['id'] if 'id' in row else row['episode']['id'] for row in rows]
            _check(
                len(rows) == len(manifest['samples']) and not set(ids) & set(dropped),
                f'{name}：每个样本一行，被排除的会话不在导出的行里',
                f'{len(rows)} 行，{len(manifest["samples"])} 个样本，行的 id {ids}',
                '查导出的筛选与 manifest 的 samples',
            )
        summary[name] = {
            'rows': len(rows),
            'samples': sorted(last),
            'dropped_unverified': len(dropped),
        }
    leaked = sorted(
        str(path.relative_to(root))
        for path in root.rglob('*')
        if path.is_file() and secret.encode() in path.read_bytes()
    )
    _check(
        not leaked,
        '环境里的密钥不出现在任何导出文件里',
        f'出现在 {leaked}',
        '这是脱敏收口的回归：导出前查 Redactor',
    )
    if scripted:
        _scripted(root, summary)
    return summary


def _scripted(root: Path, summary: dict[str, Any]) -> None:
    _check(
        summary['sft']['samples'] == ['block', 'partial', 'pass'],
        '剧本跑出的三条样本恰好是 block / partial / pass',
        summary['sft']['samples'],
        '看 batch 的 stderr 里每个任务的「验收：」一行',
    )
    [pair] = _rows(root / 'preference' / 'preference.jsonl') or [{}]
    _check(
        (pair.get('chosen_verdict'), pair.get('rejected_verdict')) == ('pass', 'block'),
        '一对偏好：同一个 Goal 与 Rubric 下 pass 对 block',
        pair,
        '查 preference 导出按 Goal 与 Rubric 的内容配对',
    )
    rewards = sorted(row['reward'] for row in _rows(root / 'rl' / 'rl.jsonl'))
    _check(
        rewards == [0.0, 0.5, 1.0],
        'RL 奖励是 block 0.0、partial 0.5、pass 1.0',
        rewards,
        '查 rl 导出的奖励表',
    )
    sft = (root / 'sft' / 'messages.jsonl').read_text(encoding='utf-8')
    _check(
        _PLACEHOLDER in sft,
        f'密钥在会话里被换成了 {_PLACEHOLDER}（证明它真的流过、不是因为根本没出现才查不到）',
        '导出的 SFT 里没有这个占位',
        '看 scripts/acceptance_export.py prepare 是否把密钥写进了剧本',
    )


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 5 and argv[1] == 'prepare':
            prepare(Path(argv[2]), Path(argv[3]), argv[4])
            return 0
        if len(argv) in (5, 6) and argv[1] == 'check':
            scripted = argv[5:] == ['--scripted']
            summary = check(Path(argv[2]), argv[3], int(argv[4]), scripted=scripted)
            print(json.dumps(summary, ensure_ascii=False))
            return 0
    except CheckFailed as failure:
        print(str(failure), file=sys.stderr)
        return 1
    print(
        '用法: acceptance_export.py prepare <示例剧本> <输出剧本> <密钥>\n'
        '      acceptance_export.py check <导出目录> <密钥> <unverified 个数> [--scripted]',
        file=sys.stderr,
    )
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
