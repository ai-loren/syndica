import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
CHECKER = REPO_ROOT / 'scripts' / 'check_deps.py'
# 关掉 PEP 538 的 C locale 强制与 PEP 540 的 UTF-8 模式，只剩 locale 决定默认编码。缺任何一个，
# 解释器都会替你把默认编码修正回 UTF-8，于是这个环境测不出「忘了写 encoding=」。
C_LOCALE = {
    'LC_ALL': 'C',
    'LANG': 'C',
    'PYTHONCOERCECLOCALE': '0',
    'PYTHONUTF8': '0',
}


def _run_checker(
    *args: str, env: dict[str, str] | None = None
) -> 'subprocess.CompletedProcess[str]':
    # cwd 固定到仓库根：不传 --root 时校验器默认扫 'src'，那是相对路径。
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=None if env is None else {**os.environ, **env},
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


def test_missing_root_exits_2(tmp_path):
    missing = tmp_path / 'no-such-src'
    result = _run_checker('--root', str(missing))
    assert result.returncode == 2


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


@pytest.mark.parametrize(
    'source',
    [
        'from syndica import cli\n',
        'from syndica import kernel, cli\n',
        'from syndica import cli as c\n',
    ],
)
def test_importing_a_component_from_the_package_root_is_caught(tmp_path, source):
    """`from syndica import cli` 与 `import syndica.cli` 是同一次依赖：几个名字里的第二个、改了名
    的，照样是。"""
    module = tmp_path / 'src' / 'syndica' / 'kernel' / 'rooted.py'
    module.parent.mkdir(parents=True)
    module.write_text(source)
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'kernel/rooted.py' in result.stdout
    assert 'cli' in result.stdout


def test_a_backend_importing_another_backend_by_name_is_caught(tmp_path):
    module = tmp_path / 'src' / 'syndica' / 'backends' / 'alpha' / 'x.py'
    module.parent.mkdir(parents=True)
    module.write_text('from syndica.backends import beta\n')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'backends/alpha/x.py' in result.stdout
    assert 'beta' in result.stdout


def test_importing_a_plain_name_from_the_package_root_is_allowed(tmp_path):
    module = tmp_path / 'src' / 'syndica' / 'kernel' / 'version.py'
    module.parent.mkdir(parents=True)
    module.write_text('from syndica import __version__\n')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode == 0, result.stdout + result.stderr


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


def test_surface_importing_a_backend_is_caught(tmp_path):
    """surface 只允许依赖 kernel（spec §3.2 规则 2）。这条挡的是发布形态：`pip install syndica`
    承诺 kernel + CLI + 零重依赖，CLI 里一句 import backend 就推翻它，而它看起来完全无害。"""
    module = tmp_path / 'src' / 'syndica' / 'cli' / 'main.py'
    module.parent.mkdir(parents=True)
    module.write_text('from syndica.backends.llm_openai import Client\n')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'cli/main.py' in result.stdout
    assert 'backends.llm_openai' in result.stdout


def test_kernel_importing_yaml_is_caught_but_cli_may_use_it(tmp_path):
    kernel_module = tmp_path / 'src' / 'syndica' / 'kernel' / 'config.py'
    kernel_module.parent.mkdir(parents=True)
    kernel_module.write_text('import yaml\n')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode != 0
    assert 'kernel/config.py' in result.stdout
    assert 'PyYAML' in result.stdout

    kernel_module.unlink()
    cli_module = tmp_path / 'src' / 'syndica' / 'cli' / 'config.py'
    cli_module.parent.mkdir(parents=True)
    cli_module.write_text('import yaml\n')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode == 0, result.stdout + result.stderr


def test_backend_importing_a_surface_is_caught(tmp_path):
    """backend 只允许依赖 kernel（spec §3.2 规则 3）。"""
    module = tmp_path / 'src' / 'syndica' / 'backends' / 'alpha' / 'client.py'
    module.parent.mkdir(parents=True)
    module.write_text('from syndica.cli import main\n')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'backends/alpha/client.py' in result.stdout
    assert 'cli' in result.stdout


def test_violations_exit_1(tmp_path):
    module = tmp_path / 'src' / 'syndica' / 'backends' / 'alpha' / 'client.py'
    module.parent.mkdir(parents=True)
    module.write_text('from syndica.cli import main\n')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode == 1
    assert '把 backend 需要的类型与入口下沉到 kernel' in result.stdout


def test_a_backend_may_import_its_own_sibling_module(tmp_path):
    """规则 3 只禁跨 backend 与 backend→surface；一个 backend 内部的模块必须放行。"""
    module = tmp_path / 'src' / 'syndica' / 'backends' / 'alpha' / 'client.py'
    module.parent.mkdir(parents=True)
    module.write_text('from syndica.backends.alpha.auth import token\n')
    (module.parent / 'auth.py').write_text('')
    result = _run_checker('--root', str(tmp_path / 'src'))
    assert result.returncode == 0, result.stdout + result.stderr


def test_sources_are_read_as_utf_8_regardless_of_locale(tmp_path):
    """本仓每个模块都带中文。不写 `encoding='utf-8'` 时 `Path.read_text()` 按 locale 解码，C
    locale 下当场 UnicodeDecodeError——校验器非零退出，表现为一次与依赖方向毫无关系的构建失败。"""
    module = tmp_path / 'src' / 'syndica' / 'kernel' / 'chinese.py'
    module.parent.mkdir(parents=True)
    module.write_text('# 中文注释：locale 是 C 时按 ASCII 解码会当场失败\n', encoding='utf-8')
    result = _run_checker('--root', str(tmp_path / 'src'), env=C_LOCALE)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'UnicodeDecodeError' not in result.stderr
