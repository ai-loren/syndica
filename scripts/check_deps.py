"""校验 spec §3.2 的依赖方向规则。

kernel 不得 import surface（cli / server / runner）或 backend。
surface 之间互不依赖。backend 之间互不依赖。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

SURFACES = frozenset({'cli', 'server', 'runner'})
FORBIDDEN_FROM_KERNEL = SURFACES | {'backends'}


def _imported_syndica_subpackages(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split('.')
        elif isinstance(node, ast.Import):
            parts = []
            for alias in node.names:
                parts = alias.name.split('.')
                if len(parts) >= 2 and parts[0] == 'syndica':
                    found.add(parts[1])
            continue
        else:
            continue
        if len(parts) >= 2 and parts[0] == 'syndica':
            found.add(parts[1])
    return found


def check(root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted((root / 'syndica' / 'kernel').rglob('*.py')):
        for pkg in _imported_syndica_subpackages(path):
            if pkg in FORBIDDEN_FROM_KERNEL:
                rel = path.relative_to(root / 'syndica')
                violations.append(
                    f'{rel}: kernel imports {pkg!r}. '
                    f'kernel 不得依赖 surface 或 backend（spec §3.2 规则 1）。'
                    f' 改为在 kernel 定义 Protocol，由 {pkg} 提供实现并由宿主注入。'
                )
    for surface in sorted(SURFACES):
        base = root / 'syndica' / surface
        if not base.exists():
            continue
        for path in sorted(base.rglob('*.py')):
            for pkg in _imported_syndica_subpackages(path):
                if pkg in SURFACES and pkg != surface:
                    rel = path.relative_to(root / 'syndica')
                    violations.append(
                        f'{rel}: surface {surface!r} imports surface {pkg!r}. '
                        f'surface 之间必须互不依赖（spec §3.2 规则 2）。'
                        f' 把共用逻辑下沉到 kernel。'
                    )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='src', help='源码根目录，默认 src')
    args = parser.parse_args()
    violations = check(Path(args.root))
    for line in violations:
        print(line)
    if violations:
        print(f'\n共 {len(violations)} 处违规。')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
