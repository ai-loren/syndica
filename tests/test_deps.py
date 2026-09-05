import subprocess
import sys


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
