"""校验 spec §3.2 的依赖方向规则。

实现三条检查，编号与 spec §3.2 一致：

1. kernel 不得 import surface（cli / server / runner）或 backend。
2. surface 之间互不依赖。
3. backend 之间互不依赖。

规则 3 的另一半「backend 不得 import surface」不在本校验器范围内。
相对导入先按导入方所在的包解析成绝对模块路径，再套用以上规则。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

PACKAGE = 'syndica'
KERNEL = 'kernel'
BACKENDS = 'backends'
SURFACES = frozenset({'cli', 'server', 'runner'})
FORBIDDEN_FROM_KERNEL = SURFACES | {BACKENDS}


class LayoutError(Exception):
    """源码根目录不满足校验前提，无法判定有无违规。"""


def _resolve_package_root(root: Path) -> Path:
    """定位 root 下的 syndica 包；扫不到目标时必须报错而不是判定「无违规」。"""
    if not root.is_dir():
        raise LayoutError(
            f'期望 --root 指向存在的源码根目录，实际收到 {str(root)!r}：路径不存在或不是目录。'
            f' 请从仓库根目录运行，或把 --root 指向包含 {PACKAGE}/ 包的目录（默认 src）。'
        )
    package_root = root / PACKAGE
    if not package_root.is_dir():
        entries = sorted(entry.name for entry in root.iterdir())
        shown = entries[:10]
        omitted = f'（共 {len(entries)} 项，仅列前 10）' if len(entries) > 10 else ''
        raise LayoutError(
            f'期望 {str(root)!r} 下存在 {PACKAGE}/ 包，实际只有 {shown}{omitted}。'
            f' 请把 --root 指向包含 {PACKAGE}/ 包的目录（默认 src）。'
        )
    return package_root


def _module_parts(path: Path, package_root: Path) -> tuple[str, ...]:
    """文件路径 → syndica 包内的模块路径分段（不含 'syndica' 前缀）。

    `__init__.py` 代表其所在包本身，因此末段被丢弃。
    """
    parts = path.relative_to(package_root).with_suffix('').parts
    if parts and parts[-1] == '__init__':
        parts = parts[:-1]
    return parts


def _component(parts: tuple[str, ...]) -> tuple[str, ...]:
    """模块路径 → 所属组件。backend 的身份是 ('backends', <name>)，其余组件只看顶层段。"""
    if not parts:
        return ()
    if parts[0] == BACKENDS:
        return parts[:2]
    return parts[:1]


def _resolve_import_from(node: ast.ImportFrom, package: tuple[str, ...]) -> list[tuple[str, ...]]:
    """把一条 from-import 解析成绝对模块路径；相对导入以导入方所在包为基准。"""
    if node.level == 0:
        return [tuple(node.module.split('.'))] if node.module else []
    kept = len(package) - (node.level - 1)
    if kept <= 0:
        return []
    base = package[:kept]
    if node.module:
        return [(*base, *node.module.split('.'))]
    return [(*base, alias.name) for alias in node.names]


def _imported_components(path: Path, package_root: Path) -> set[tuple[str, ...]]:
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    package = (PACKAGE, *path.parent.relative_to(package_root).parts)
    modules: list[tuple[str, ...]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.extend(_resolve_import_from(node, package))
        elif isinstance(node, ast.Import):
            modules.extend(tuple(alias.name.split('.')) for alias in node.names)
    return {_component(parts[1:]) for parts in modules if len(parts) >= 2 and parts[0] == PACKAGE}


def _violation(rel: Path, owner: tuple[str, ...], imported: tuple[str, ...]) -> str | None:
    owner_top, imported_top = owner[0], imported[0]
    if owner_top == KERNEL and imported_top in FORBIDDEN_FROM_KERNEL:
        return (
            f'{rel}: kernel imports {imported_top!r}. '
            f'kernel 不得依赖 surface 或 backend（spec §3.2 规则 1）。'
            f' 改为在 kernel 定义 Protocol，由 {imported_top} 提供实现并由宿主注入。'
        )
    if owner_top in SURFACES and imported_top in SURFACES and imported_top != owner_top:
        return (
            f'{rel}: surface {owner_top!r} imports surface {imported_top!r}. '
            f'surface 之间必须互不依赖（spec §3.2 规则 2）。'
            f' 把共用逻辑下沉到 kernel。'
        )
    if len(owner) == 2 and len(imported) == 2 and owner_top == BACKENDS == imported_top:
        if owner[1] != imported[1]:
            return (
                f'{rel}: backend {owner[1]!r} imports backend {imported[1]!r}. '
                f'backend 之间必须互不依赖（spec §3.2 规则 3）。'
                f' 把共用逻辑下沉到 kernel，或由宿主装配层同时持有两个 backend。'
            )
    return None


def check(root: Path) -> list[str]:
    package_root = _resolve_package_root(root)
    violations: list[str] = []
    for path in sorted(package_root.rglob('*.py')):
        owner = _component(_module_parts(path, package_root))
        if not owner:
            continue
        rel = path.relative_to(package_root)
        for imported in sorted(_imported_components(path, package_root)):
            message = _violation(rel, owner, imported)
            if message:
                violations.append(message)
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='src', help='源码根目录，默认 src')
    args = parser.parse_args()
    try:
        violations = check(Path(args.root))
    except LayoutError as exc:
        print(f'依赖方向校验无法执行：{exc}', file=sys.stderr)
        return 2
    for line in violations:
        print(line)
    if violations:
        print(f'\n共 {len(violations)} 处违规。')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
