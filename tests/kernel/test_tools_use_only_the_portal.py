"""内核内置的工具只经 Portal 碰磁盘与子进程（路线图 M5 静态验收）。

工具绕过 Portal 自己开文件，换一个远程工作区时它就在宿主机上读写；而且越界、私有目录与版本前提
都在 Portal 里，绕过它的那一次写既不拒越界、也不检查别人改没改过。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import syndica.kernel.tools as tools_package

_TOOLS_DIR = Path(tools_package.__file__).parent
_FORBIDDEN_MODULES = frozenset(
    {
        'builtins',
        'importlib',
        'io',
        'os',
        'pathlib',
        'pty',
        'shutil',
        'socket',
        'subprocess',
        'sys',
        'tempfile',
    }
)
_FORBIDDEN_NAMES = frozenset({'open', '__import__', 'exec', 'eval'})
_FORBIDDEN_ATTRIBUTES = frozenset(
    {'create_subprocess_exec', 'create_subprocess_shell', 'subprocess_exec', 'subprocess_shell'}
)


def _scanned() -> list[Path]:
    return sorted(_TOOLS_DIR.rglob('*.py'))


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [
                f'{path.name}:{node.lineno} import {alias.name}'
                for alias in node.names
                if alias.name.split('.')[0] in _FORBIDDEN_MODULES
            ]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module.split('.')[0] in _FORBIDDEN_MODULES:
                found.append(f'{path.name}:{node.lineno} from {node.module} import ...')
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            found.append(f'{path.name}:{node.lineno} {node.id}')
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_ATTRIBUTES:
            found.append(f'{path.name}:{node.lineno} .{node.attr}')
    return found


def test_no_builtin_tool_reaches_the_disk_or_a_process_except_through_the_portal():
    violations = [item for path in _scanned() for item in _violations(path)]
    assert violations == [], (
        f'期望 syndica/kernel/tools 里的工具只经 Portal 读写文件、起进程；实际有 {violations}。'
        ' 把这次操作加成 Portal 的方法（LocalWorkspace 实现它、契约套件覆盖它），工具改调它。'
    )


def test_the_guard_scans_every_tool_module_including_the_file_tools():
    walked = sorted(
        Path(folder) / name
        for folder, _, names in os.walk(_TOOLS_DIR)
        for name in names
        if name.endswith('.py')
    )
    assert _scanned() == walked
    assert _TOOLS_DIR / 'local.py' in _scanned()


def test_the_guard_flags_a_tool_that_opens_a_file_itself(tmp_path: Path):
    sample = tmp_path / 'sample.py'
    sample.write_text(
        'import subprocess\n'
        'from os import path\n'
        'import builtins\n'
        'from sys import modules\n'
        'def f(loop):\n'
        '    open("x")\n'
        '    loop.subprocess_shell\n'
        '    return loop.create_subprocess_exec\n',
        encoding='utf-8',
    )
    assert {item.split(' ', 1)[1] for item in _violations(sample)} == {
        'import subprocess',
        'from os import ...',
        'import builtins',
        'from sys import ...',
        'open',
        '.subprocess_shell',
        '.create_subprocess_exec',
    }
