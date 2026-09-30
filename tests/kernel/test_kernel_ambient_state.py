"""内核不读进程的环境：环境变量、cwd、家目录、PATH 查找、当前用户。

这些值只有宿主知道该不该用、用哪一份。内核读了它，就把一种部署方式写死进了库里；危险规则读了
它，同一条命令在两台机器上就给出两个结论。扫描而不是靠约定：违反时什么都不报错。

认不出的写法：相对路径上的 `resolve()` 同样按 cwd 解析；`getattr(os, 'environ')` 这类动态取值。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

_PACKAGE = Path(__file__).resolve().parents[2] / 'src' / 'syndica'
_KERNEL = _PACKAGE / 'kernel'
# 危险规则的 `matches` 坐在每一次工具调用上；守卫必须证明自己看过它。
_MUST_SCAN = 'kernel/danger.py'

# 任何对象上的这些属性都算：`os.environ`、`os.path.abspath`、`some_path.expanduser()`。
_FORBIDDEN_ATTRIBUTES = frozenset(
    {
        'abspath',
        'absolute',
        'chdir',
        'environ',
        'environb',
        'expanduser',
        'expandvars',
        'fchdir',
        'get_exec_path',
        'getcwd',
        'getcwdb',
        'getenv',
        'getenvb',
        'geteuid',
        'getlogin',
        'getpwnam',
        'getpwuid',
        'gettempdir',
        'gettempdirb',
        'getuid',
        'getuser',
        'putenv',
        'unsetenv',
    }
)
# 只在 `Path` 上算：`request.cwd` 这样的字段名不是在读进程状态。
_FORBIDDEN_PATH_METHODS = frozenset({'cwd', 'home'})
_PATH_CLASSES = frozenset({'Path', 'PosixPath', 'WindowsPath'})
_PATH_FUNCTIONS = frozenset({'abspath', 'expanduser', 'expandvars'})
_FORBIDDEN_IMPORTS: dict[str, frozenset[str]] = {
    'getpass': frozenset({'getuser'}),
    'ntpath': _PATH_FUNCTIONS,
    'os': frozenset(
        {
            'chdir',
            'environ',
            'environb',
            'fchdir',
            'get_exec_path',
            'getcwd',
            'getcwdb',
            'getenv',
            'getenvb',
            'geteuid',
            'getlogin',
            'getuid',
            'putenv',
            'unsetenv',
        }
    ),
    'os.path': _PATH_FUNCTIONS,
    'posixpath': _PATH_FUNCTIONS,
    'pwd': frozenset({'getpwnam', 'getpwuid'}),
    'shutil': frozenset({'which'}),
    'tempfile': frozenset({'gettempdir', 'gettempdirb'}),
}


def _names_a_path_class(node: ast.expr, classes: set[str]) -> bool:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id in classes
    return isinstance(node, ast.Attribute) and node.attr in _PATH_CLASSES


def _forbidden_uses(source: str) -> list[str]:
    tree = ast.parse(source)
    shutil_names = {'shutil'}
    path_classes = set(_PATH_CLASSES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            shutil_names.update(
                alias.asname for alias in node.names if alias.name == 'shutil' and alias.asname
            )
        elif isinstance(node, ast.ImportFrom) and node.module == 'pathlib':
            path_classes.update(
                alias.asname for alias in node.names if alias.name in _PATH_CLASSES and alias.asname
            )
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in _FORBIDDEN_ATTRIBUTES:
                found.append(node.attr)
            elif node.attr in _FORBIDDEN_PATH_METHODS and _names_a_path_class(
                node.value, path_classes
            ):
                found.append(f'Path.{node.attr}')
            elif (
                node.attr == 'which'
                and isinstance(node.value, ast.Name)
                and node.value.id in shutil_names
            ):
                found.append('shutil.which')
        elif isinstance(node, ast.ImportFrom) and node.module in _FORBIDDEN_IMPORTS:
            banned = _FORBIDDEN_IMPORTS[node.module]
            found.extend(
                f'from {node.module} import {alias.name}'
                for alias in node.names
                if alias.name in banned or alias.name == '*'
            )
    return found


def _scan(kernel: Path = _KERNEL) -> tuple[dict[str, list[str]], list[str]]:
    """(违规的文件 → 违规写法, 本次真的扫过的文件)，路径相对 kernel 的上一级。"""
    violations: dict[str, list[str]] = {}
    scanned: list[str] = []
    for path in sorted(kernel.rglob('*.py')):
        relative = path.relative_to(kernel.parent).as_posix()
        scanned.append(relative)
        found = _forbidden_uses(path.read_text(encoding='utf-8'))
        if found:
            violations[relative] = found
    return violations, scanned


def _kernel_python_files() -> set[str]:
    """内核里全部 `.py`，用 `os.walk` 独立走一遍：范围被收窄时两份清单只有同时改才对得上。"""
    found: set[str] = set()
    for root, _, names in os.walk(_KERNEL):
        for name in names:
            if name.endswith('.py'):
                found.add((Path(root) / name).relative_to(_PACKAGE).as_posix())
    return found


def test_the_kernel_reads_no_environment_variable_cwd_home_path_lookup_or_user():
    violations, _ = _scan()

    assert violations == {}, (
        f'期望 src/syndica/kernel 不读进程环境；实际 {violations}。'
        ' 把这个值做成参数，由宿主（cli / runner）读好再交给内核。'
    )


def test_the_scan_really_covered_the_danger_rules():
    _, scanned = _scan()

    assert _MUST_SCAN in scanned


def test_the_scan_covers_the_whole_kernel_package():
    _, scanned = _scan()

    assert set(scanned) == _kernel_python_files()


def test_a_violation_planted_in_the_danger_rules_is_reported(tmp_path: Path):
    kernel = tmp_path / 'kernel'
    kernel.mkdir()
    (kernel / 'danger.py').write_text('import os\nLEAK = os.environ.get("X")\n', encoding='utf-8')
    (kernel / 'clean.py').write_text('X = 1\n', encoding='utf-8')

    violations, scanned = _scan(kernel)

    assert violations == {_MUST_SCAN: ['environ']}
    assert scanned == ['kernel/clean.py', _MUST_SCAN]


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        ('import os\nos.environ["X"]', 'environ'),
        ('import os\nos.environb', 'environb'),
        ('import os\nos.getenv("X")', 'getenv'),
        ('import os\nos.getenvb(b"X")', 'getenvb'),
        ('import os\nos.putenv("X", "1")', 'putenv'),
        ('import os\nos.unsetenv("X")', 'unsetenv'),
        ('import os\nos.getcwd()', 'getcwd'),
        ('import os\nos.getcwdb()', 'getcwdb'),
        ('import os\nos.chdir("/")', 'chdir'),
        ('import os\nos.fchdir(3)', 'fchdir'),
        ('import os\nos.get_exec_path()', 'get_exec_path'),
        ('import os\nos.getlogin()', 'getlogin'),
        ('import os\nos.getuid()', 'getuid'),
        ('import os\nos.geteuid()', 'geteuid'),
        ('import pwd\npwd.getpwuid(0)', 'getpwuid'),
        ('import pwd\npwd.getpwnam("x")', 'getpwnam'),
        ('import tempfile\ntempfile.gettempdir()', 'gettempdir'),
        ('import tempfile\ntempfile.gettempdirb()', 'gettempdirb'),
        ('import os\nos.path.abspath(x)', 'abspath'),
        ('import os\nos.path.expanduser(x)', 'expanduser'),
        ('p.expanduser()', 'expanduser'),
        ('import os\nos.path.expandvars(x)', 'expandvars'),
        ('p.absolute()', 'absolute'),
        ('import getpass\ngetpass.getuser()', 'getuser'),
        ('import os as o\no.environ', 'environ'),
        ('from pathlib import Path\nPath.cwd()', 'Path.cwd'),
        ('from pathlib import Path\nPath.home()', 'Path.home'),
        ('import pathlib\npathlib.Path.home()', 'Path.home'),
        ('import pathlib as pl\npl.Path.cwd()', 'Path.cwd'),
        ('from pathlib import PosixPath\nPosixPath.cwd()', 'Path.cwd'),
        ('from pathlib import WindowsPath\nWindowsPath.home()', 'Path.home'),
        ('from pathlib import Path as P\nP.cwd()', 'Path.cwd'),
        ('from pathlib import Path\nPath("/x").home()', 'Path.home'),
        ('import shutil\nshutil.which("git")', 'shutil.which'),
        ('import shutil as sh\nsh.which("git")', 'shutil.which'),
        ('from os import environ', 'from os import environ'),
        ('from os import environb', 'from os import environb'),
        ('from os import getenv', 'from os import getenv'),
        ('from os import getenvb', 'from os import getenvb'),
        ('from os import putenv', 'from os import putenv'),
        ('from os import unsetenv', 'from os import unsetenv'),
        ('from os import getcwd', 'from os import getcwd'),
        ('from os import getcwdb', 'from os import getcwdb'),
        ('from os import chdir', 'from os import chdir'),
        ('from os import fchdir', 'from os import fchdir'),
        ('from os import get_exec_path', 'from os import get_exec_path'),
        ('from os import getlogin', 'from os import getlogin'),
        ('from os import getuid', 'from os import getuid'),
        ('from os import geteuid', 'from os import geteuid'),
        ('from os import *', 'from os import *'),
        ('from os.path import abspath', 'from os.path import abspath'),
        ('from os.path import expanduser', 'from os.path import expanduser'),
        ('from os.path import expandvars', 'from os.path import expandvars'),
        ('from posixpath import expanduser', 'from posixpath import expanduser'),
        ('from ntpath import abspath', 'from ntpath import abspath'),
        ('from posixpath import *', 'from posixpath import *'),
        ('from pwd import getpwuid', 'from pwd import getpwuid'),
        ('from pwd import getpwnam', 'from pwd import getpwnam'),
        ('from tempfile import gettempdir', 'from tempfile import gettempdir'),
        ('from tempfile import gettempdirb', 'from tempfile import gettempdirb'),
        ('from shutil import which', 'from shutil import which'),
        ('from shutil import which as w', 'from shutil import which'),
        ('from getpass import getuser', 'from getpass import getuser'),
    ],
)
def test_the_guard_recognises_every_banned_way_to_read_the_process_environment(
    source: str, expected: str
):
    assert expected in _forbidden_uses(source)


def test_the_guard_does_not_flag_a_field_that_merely_shares_a_name():
    source = (
        'request.cwd\noptions.home\nwhich = 1\nx.which()\nfrom os import path, sep\n'
        'import posixpath\nposixpath.normpath(x)\nfrom tempfile import mkdtemp\n'
        'import shutil as sh\nsh.copy(a, b)\nself.path.home'
    )

    assert _forbidden_uses(source) == []
