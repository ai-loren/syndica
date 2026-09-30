"""全仓测试共用的装置。

`SYNDICA_TEST_SHELL` 换掉真正执行命令的测试用的 shell：本机的 `/bin/sh` 是 bash，Ubuntu 的是 dash，
`SYNDICA_TEST_SHELL=/bin/dash` 在本地近似 CI 上的 shell。只有测试读这个环境变量，内核不受影响。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_DEFAULT_SHELL = '/bin/sh'


def _shell_under_test() -> str:
    return os.environ.get('SYNDICA_TEST_SHELL', _DEFAULT_SHELL)


def pytest_configure(config: pytest.Config) -> None:
    shell = Path(_shell_under_test())
    if not shell.is_absolute() or not shell.is_file() or not os.access(shell, os.X_OK):
        raise pytest.UsageError(
            'SYNDICA_TEST_SHELL 期望是一个可执行文件的绝对路径（如 /bin/dash）；'
            f'实际是 {str(shell)!r}。改成存在的 shell 的绝对路径，或 unset 它回到默认的 /bin/sh。'
        )


@pytest.fixture
def shell_path() -> str:
    """真正执行命令的测试用的 shell 的绝对路径。"""
    return _shell_under_test()
