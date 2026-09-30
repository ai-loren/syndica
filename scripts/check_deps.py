"""校验 spec §3.2 的依赖方向规则。

编号与 spec §3.2 一致；每条规则都是「只允许依赖 kernel」的一个方向：

1. kernel 不得 import surface（cli / server / runner）或 backend。
2. surface 之间互不依赖，且 surface 不得 import backend。后半条挡的是发布形态：
   `pip install syndica` = kernel + CLI + **零重依赖**（spec §3.3），CLI 里一句
   `from syndica.backends.llm_openai import …` 就把这条承诺推翻，而它看起来完全无害。
3. backend 之间互不依赖，且 backend 不得 import surface。

相对导入先按导入方所在的包解析成绝对模块路径，再套用以上规则。

两处已知盲区：动态导入（`importlib.import_module('syndica.backends.x')`）看不见；
模块级单段导入（`import syndica`）被 `len(parts) >= 2` 过滤掉，它引不进任何组件。

源文件一律按 utf-8 读。**不能省掉 `encoding=`**：不带它时 `Path.read_text()` 按 locale 解码，
C locale（CI 容器的默认值之一）下遇到带中文的源文件直接 UnicodeDecodeError，校验器以非零退出、
表现为一次与依赖方向毫无关系的构建失败。
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
FORBIDDEN_KERNEL_EXTERNALS = frozenset({'yaml'})


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
        if not node.module:
            return []
        # 导入的名字可能就是组件本身：`from syndica import cli` 是 syndica.cli，
        # `from syndica.backends import beta` 是 syndica.backends.beta。只记模块名就少了这一段，
        # kernel 这样 import surface、backend 之间这样互相 import 都会整条漏过。
        base = tuple(node.module.split('.'))
        return [base if alias.name == '*' else (*base, alias.name) for alias in node.names]
    kept = len(package) - (node.level - 1)
    if kept <= 0:
        return []
    base = package[:kept]
    if node.module:
        return [(*base, *node.module.split('.'))]
    return [(*base, alias.name) for alias in node.names]


def _imported_modules(path: Path, package_root: Path) -> list[tuple[str, ...]]:
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    package = (PACKAGE, *path.parent.relative_to(package_root).parts)
    modules: list[tuple[str, ...]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.extend(_resolve_import_from(node, package))
        elif isinstance(node, ast.Import):
            modules.extend(tuple(alias.name.split('.')) for alias in node.names)
    return modules


def _imported_components(modules: list[tuple[str, ...]]) -> set[tuple[str, ...]]:
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
    if owner_top in SURFACES and imported_top == BACKENDS:
        return (
            f'{rel}: surface {owner_top!r} imports {".".join(imported)!r}. '
            f'surface 只允许依赖 kernel（spec §3.2 规则 2）。'
            f' 改为依赖 kernel 里的 Protocol，由宿主装配层注入 backend 实现；'
            f' surface 直接 import backend 会让 pip install syndica 拖进重依赖（spec §3.3）。'
        )
    if owner_top == BACKENDS and imported_top in SURFACES:
        return (
            f'{rel}: backend {".".join(owner)!r} imports surface {imported_top!r}. '
            f'backend 只允许依赖 kernel（spec §3.2 规则 3）。'
            f' 把 backend 需要的类型与入口下沉到 kernel，由 surface 单向依赖它。'
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
        modules = _imported_modules(path, package_root)
        if owner[0] == KERNEL:
            forbidden = sorted(
                module[0]
                for module in modules
                if module and module[0] in FORBIDDEN_KERNEL_EXTERNALS
            )
            for imported in forbidden:
                violations.append(
                    f'{rel}: kernel imports external package {imported!r} (PyYAML). '
                    'kernel 只允许标准库、pydantic 与一个 HTTP 客户端；'
                    '把 YAML 解析留在 cli / runner surface，再把 Mapping 交给 kernel。'
                )
        for imported in sorted(_imported_components(modules)):
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
