"""重新生成本目录的检查结果样例。

在临时目录里造几个小项目，经 `LocalWorkspace.run`（Agent 的 Bash 走的同一条路，`/bin/sh`）跑真实
工具，把 `(command, exit_code, stdout, stderr, timed_out)` 写成 JSON。落盘前把仓库路径、临时目录与
家目录换成 `/repo`、`/work`、`/home/user`，样例里不留本机路径。

用法（仓库根目录，离线可跑）：`uv run python tests/cli/fixtures/checks/generate.py`
"""

from __future__ import annotations

import asyncio
import getpass
import json
import re
import shlex
import shutil
import tempfile
from pathlib import Path

from syndica.kernel.local_workspace import LocalWorkspace

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[3]

_CALC_TESTS = (
    'from pkg.calc import add\n\n\n'
    'def test_add() -> None:\n    assert add(1, 2) == 3\n\n\n'
    'def test_zero() -> None:\n    assert add(0, 0) == 0\n'
)
_CONFIG = {
    'pytest.ini': '[pytest]\npythonpath = .\n',
    'ruff.toml': '[lint]\nselect = ["E", "F", "I"]\n',
}
_PROJECTS: dict[str, dict[str, str]] = {
    'good': {
        **_CONFIG,
        'pkg/__init__.py': '',
        'pkg/calc.py': 'def add(a: int, b: int) -> int:\n    return a + b\n',
        'tests/test_calc.py': _CALC_TESTS,
    },
    'bad': {
        **_CONFIG,
        'pkg/__init__.py': '',
        'pkg/calc.py': (
            'import os\ndef add(a: int, b: int) -> int:\n    return a - b\nx: int = "nope"\n'
        ),
        'tests/test_calc.py': _CALC_TESTS,
    },
    'broken': {**_CONFIG, 'tests/test_broken.py': 'def test_x(:\n    pass\n'},
    'empty': {**_CONFIG, 'README.md': 'no tests here\n'},
    'slow': {
        **_CONFIG,
        'tests/test_slow.py': (
            'import time\n\n\ndef test_fast() -> None:\n    pass\n\n\n'
            'def test_slow() -> None:\n    time.sleep(60)\n'
        ),
    },
    'layering': {
        'src/syndica/__init__.py': '',
        'src/syndica/cli/__init__.py': '',
        'src/syndica/kernel/__init__.py': 'import syndica.cli\n',
    },
    'docs_good': {'README.md': 'See [the guide](docs/guide.md).\n', 'docs/guide.md': '# Guide\n'},
    'docs_bad': {'README.md': 'See [the spec](docs/spec.md).\n'},
}
_CHECK_DEPS = f'uv run python {shlex.quote(str(_REPO / "scripts" / "check_deps.py"))}'
_CHECK_LINKS = f'uv run python {shlex.quote(str(_REPO / "scripts" / "check_links.py"))}'
# (样例名, 项目目录, 命令, 超时秒数)
_CASES: tuple[tuple[str, str, str, float], ...] = (
    ('pytest_passed', 'good', 'uv run pytest -q', 120),
    ('pytest_failed', 'bad', 'uv run pytest -q', 120),
    ('pytest_collection_error', 'broken', 'uv run pytest -q', 120),
    ('pytest_no_tests', 'empty', 'uv run pytest -q', 120),
    ('pytest_no_output', 'good', 'uv run pytest -q > /dev/null 2>&1', 120),
    ('pytest_timeout', 'slow', 'uv run pytest -v', 10),
    ('ruff_check_passed', 'good', 'uv run ruff check .', 120),
    ('ruff_check_failed', 'bad', 'uv run ruff check .', 120),
    ('ruff_format_passed', 'good', 'uv run ruff format --check .', 120),
    ('ruff_format_failed', 'bad', 'uv run ruff format --check .', 120),
    ('pyright_passed', 'good', 'uv run pyright pkg', 300),
    ('pyright_failed', 'bad', 'uv run pyright pkg', 300),
    ('check_deps_passed', 'good', f'{_CHECK_DEPS} --root {shlex.quote(str(_REPO / "src"))}', 120),
    ('check_deps_failed', 'layering', f'{_CHECK_DEPS} --root src', 120),
    ('check_deps_error', 'good', f'{_CHECK_DEPS} --root missing', 120),
    ('check_links_passed', 'docs_good', f'{_CHECK_LINKS} --root .', 120),
    ('check_links_failed', 'docs_bad', f'{_CHECK_LINKS} --root .', 120),
    ('other_passed', 'good', 'printf hello', 30),
    # `ls` 找不到文件时 macOS 退出 1、GNU 退出 2；`false` 两边都是 1。
    ('other_failed', 'good', 'false', 30),
)


def _environment() -> dict[str, str]:
    uv = shutil.which('uv')
    if uv is None:
        raise SystemExit('生成样例要 uv 在 PATH 上；装好 uv 或把它所在目录加进 PATH 后再跑。')
    return {
        'PATH': f'{Path(uv).parent}:/usr/bin:/bin',
        'HOME': str(Path.home()),
        'LANG': 'C.UTF-8',
        'VIRTUAL_ENV': str(_REPO / '.venv'),
        'UV_OFFLINE': '1',
        # pyright-python 每次都去 PyPI 查新版本、把提示打进输出：样例随网络与日期变。
        'PYRIGHT_PYTHON_IGNORE_WARNINGS': '1',
    }


# 用时每跑一次都不同；换成一个固定值，重新生成时样例只在工具的写法变了时才变。
_DURATION = re.compile(r'( in )\d+\.\d+s\b')


def _sanitized(text: str, work: Path) -> str:
    for local, placeholder in (
        (str(_REPO), '/repo'),
        (str(work.resolve()), '/work'),
        (str(work), '/work'),
        (str(Path.home()), '/home/user'),
    ):
        text = text.replace(local, placeholder)
    text = _DURATION.sub(r'\g<1>0.01s', text)
    # 按前缀替换只换得掉逐字相同的路径：工具从别的目录解析出来的路径会原样留下。
    leaks = [
        marker
        for marker in ('/Users/', '/private/', '/var/folders/', getpass.getuser())
        if marker in text and marker != 'user'
    ]
    if leaks:
        raise SystemExit(
            f'样例里还留着本机路径 {leaks}：把它们加进 _sanitized 的替换表再重新生成，'
            '不要把这样的样例提交进仓库。'
        )
    return text


async def _generate(work: Path) -> None:
    for project, files in _PROJECTS.items():
        for relative, content in files.items():
            path = work / project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
    workspace = LocalWorkspace(str(work), shell='/bin/sh', env=_environment())
    try:
        for name, project, command, timeout in _CASES:
            result = await workspace.run(command, timeout=timeout, cwd=project)
            fixture = {
                'command': command,
                'exit_code': result.exit_code,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'timed_out': result.timed_out,
            }
            text = json.dumps(fixture, ensure_ascii=False, indent=1)
            (_HERE / f'{name}.json').write_text(_sanitized(text, work) + '\n', encoding='utf-8')
            print(f'{name}: exit={result.exit_code} timed_out={result.timed_out}')
    finally:
        await workspace.close()


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix='syndica-checks-'))
    try:
        asyncio.run(_generate(work))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
