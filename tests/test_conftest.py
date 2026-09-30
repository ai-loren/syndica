"""测试侧的 shell 开关：`SYNDICA_TEST_SHELL` 真的换掉执行命令的 shell，写错时整轮测试拒绝开始。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def test_the_shell_under_test_is_the_one_the_environment_names(shell_path: str) -> None:
    assert shell_path == os.environ.get('SYNDICA_TEST_SHELL', '/bin/sh')


def test_a_shell_that_is_not_an_absolute_executable_stops_the_run_with_the_fix(
    tmp_path: Path,
) -> None:
    for bad in ('dash', str(tmp_path)):
        completed = subprocess.run(
            [sys.executable, '-m', 'pytest', '--collect-only', '-q', 'tests/kernel/test_errors.py'],
            cwd=_REPO,
            env={**os.environ, 'SYNDICA_TEST_SHELL': bad},
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert completed.returncode == 4, completed.stdout + completed.stderr
        assert 'SYNDICA_TEST_SHELL' in completed.stderr
        assert '/bin/dash' in completed.stderr
