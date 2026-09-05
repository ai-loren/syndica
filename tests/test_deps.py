import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CHECKER = REPO_ROOT / 'scripts' / 'check_deps.py'


def _run_checker(*args: str) -> 'subprocess.CompletedProcess[str]':
    # cwd 固定到仓库根：不传 --root 时校验器默认扫 'src'，那是相对路径。
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def test_dependency_direction_check_passes():
    """kernel 不得 import surface 或 backend（Global Constraints 第 4 条）。"""
    result = _run_checker()
    assert result.returncode == 0, result.stdout + result.stderr


def test_dependency_direction_check_catches_violation(tmp_path):
    """校验脚本自身必须能抓到违规，否则它只是装饰。"""
    bad = tmp_path / 'src' / 'syndica' / 'kernel' / 'bad.py'
    bad.parent.mkdir(parents=True)
    bad.write_text('from syndica.cli import main\n')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode != 0
    assert 'kernel/bad.py' in result.stdout


def test_missing_root_fails_loudly(tmp_path):
    """--root 指向不存在的路径必须非零退出：扫不到文件不等于没有违规。"""
    missing = tmp_path / 'no-such-src'
    result = _run_checker('--root', str(missing))
    assert result.returncode != 0, result.stdout + result.stderr
    assert str(missing) in result.stderr
    assert 'syndica' in result.stderr


def test_root_without_syndica_package_fails_loudly(tmp_path):
    """root 存在但没有 syndica 包是同一类假绿：扫到 0 个文件不等于合规。"""
    root = tmp_path / 'src'
    (root / 'unrelated').mkdir(parents=True)
    result = _run_checker('--root', str(root))
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'syndica' in result.stderr
    assert 'unrelated' in result.stderr


def test_surface_importing_another_surface_is_caught(tmp_path):
    """surface 之间必须互不依赖（spec §3.2 规则 2）。"""
    module = tmp_path / 'src' / 'syndica' / 'cli' / 'main.py'
    module.parent.mkdir(parents=True)
    module.write_text('from syndica.server import app\n')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'cli/main.py' in result.stdout
    assert 'server' in result.stdout


def test_surface_importing_itself_is_allowed(tmp_path):
    """规则 2 只禁跨 surface；cli 引用自己的子模块必须放行。"""
    module = tmp_path / 'src' / 'syndica' / 'cli' / 'main.py'
    module.parent.mkdir(parents=True)
    module.write_text('from syndica.cli.args import parse\n')
    (module.parent / 'args.py').write_text('')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode == 0, result.stdout + result.stderr


def test_backend_importing_another_backend_is_caught(tmp_path):
    """backend 之间必须互不依赖（spec §3.2 规则 3）。"""
    module = tmp_path / 'src' / 'syndica' / 'backends' / 'alpha' / 'client.py'
    module.parent.mkdir(parents=True)
    module.write_text('from syndica.backends.beta import Thing\n')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'backends/alpha/client.py' in result.stdout
    assert 'beta' in result.stdout


def test_plain_import_statement_is_caught(tmp_path):
    """`import syndica.cli` 与 `from syndica.cli import x` 必须同样被检出。"""
    module = tmp_path / 'src' / 'syndica' / 'kernel' / 'flat.py'
    module.parent.mkdir(parents=True)
    module.write_text('import syndica.cli\n')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'kernel/flat.py' in result.stdout
    assert 'cli' in result.stdout


def test_relative_import_crossing_boundary_is_caught(tmp_path):
    """相对导入须先解析成绝对模块路径，否则 kernel 能用 `..` 绕过规则 1。"""
    module = tmp_path / 'src' / 'syndica' / 'kernel' / 'sneaky.py'
    module.parent.mkdir(parents=True)
    module.write_text('from ..cli import main\n')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'kernel/sneaky.py' in result.stdout
    assert 'cli' in result.stdout


def test_relative_import_inside_own_package_is_allowed(tmp_path):
    """kernel 里的 `from . import cli` 指向 syndica.kernel.cli，不是 cli surface。"""
    module = tmp_path / 'src' / 'syndica' / 'kernel' / 'local.py'
    module.parent.mkdir(parents=True)
    module.write_text('from . import cli\n')
    (module.parent / 'cli.py').write_text('')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode == 0, result.stdout + result.stderr
