"""前端产物的来历：输入哈希（前端源码、构建配置与 OpenAPI 快照）与产物哈希（`static/` 里的文件）。

`scripts/check_web.sh` 构建之后用 `--write` 写 `build-info.json`；Python 用例不用 Node 就重算两样
比对，于是改了前端源码、或改了 HTTP 契约（前端类型由快照生成）却没重新构建，用例当场变红；手改了
`static/` 里的文件同样。用 `git archive` 导出的树里没有 `.git`，所以输入按显式清单收集，不按
「被跟踪的文件」。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'web'
STATIC = ROOT / 'src' / 'syndica' / 'server' / 'static'
INFO = STATIC / 'build-info.json'
NODE_MAJOR = 26
_INPUT_FILES = (
    'web/index.html',
    'web/package.json',
    'web/package-lock.json',
    'web/vite.config.ts',
    'web/tsconfig.json',
    'web/eslint.config.js',
    'docs/reference/openapi.json',
)


def input_files() -> list[Path]:
    files = [ROOT / name for name in _INPUT_FILES]
    files.extend(path for path in (WEB / 'src').rglob('*') if path.is_file())
    return sorted(files)


def static_files() -> list[Path]:
    return sorted(path for path in STATIC.rglob('*') if path.is_file() and path != INFO)


def digest(files: list[Path], base: Path) -> str:
    hasher = hashlib.sha256()
    for path in files:
        hasher.update(path.relative_to(base).as_posix().encode())
        hasher.update(b'\0')
        hasher.update(path.read_bytes())
        hasher.update(b'\0')
    return hasher.hexdigest()


def current() -> dict[str, object]:
    return {
        'inputs': digest(input_files(), ROOT),
        'static': digest(static_files(), STATIC),
        'node_major': NODE_MAJOR,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    if args.write:
        INFO.write_text(json.dumps(current(), indent=2) + '\n', encoding='utf-8')
        return 0
    recorded = json.loads(INFO.read_text(encoding='utf-8'))
    if recorded != current():
        print(
            '期望: src/syndica/server/static 是由当前的 web/ 源码与 OpenAPI 快照构建出来的\n'
            '实际: build-info.json 记的哈希与现在的不一致\n'
            '下一步: 运行 scripts/check_web.sh 重新构建并提交产物',
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
