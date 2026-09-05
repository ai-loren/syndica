import subprocess
import sys


def _run_checker(*args: str) -> 'subprocess.CompletedProcess[str]':
    return subprocess.run(
        [sys.executable, 'scripts/check_deps.py', *args],
        capture_output=True,
        text=True,
    )


def test_dependency_direction_check_passes():
    """kernel 不得 import surface 或 backend（Global Constraints 第 4 条）。"""
    result = subprocess.run(
        [sys.executable, 'scripts/check_deps.py'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_dependency_direction_check_catches_violation(tmp_path, monkeypatch):
    """校验脚本自身必须能抓到违规，否则它只是装饰。"""
    bad = tmp_path / 'src' / 'syndica' / 'kernel' / 'bad.py'
    bad.parent.mkdir(parents=True)
    bad.write_text('from syndica.cli import main\n')
    result = subprocess.run(
        [sys.executable, 'scripts/check_deps.py', '--root', str(tmp_path / 'src')],
        capture_output=True,
        text=True,
    )
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


def test_backend_importing_another_backend_is_caught(tmp_path):
    """backend 之间必须互不依赖（spec §3.2 规则 3）。"""
    module = tmp_path / 'src' / 'syndica' / 'backends' / 'alpha' / 'client.py'
    module.parent.mkdir(parents=True)
    module.write_text('from syndica.backends.beta import Thing\n')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'backends/alpha/client.py' in result.stdout
    assert 'beta' in result.stdout


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
