"""全仓测试共用的装置。

`SYNDICA_TEST_SHELL` 换掉真正执行命令的测试用的 shell：本机的 `/bin/sh` 是 bash，Ubuntu 的是 dash，
`SYNDICA_TEST_SHELL=/bin/dash` 在本地近似 CI 上的 shell。只有测试读这个环境变量，内核不受影响。
经公开 CLI 的用例同样跟着它走：CLI 宿主给 Agent 的 Bash 用的 shell 在每条用例里换成它。
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


@pytest.fixture(autouse=True)
def cli_agent_shell(monkeypatch: pytest.MonkeyPatch, shell_path: str) -> None:
    monkeypatch.setattr('syndica.cli.host._SHELL', shell_path)


def drop_opentelemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """开发者自己 shell 里的 `OTEL_*` 不进任何用例：经公开 CLI 的用例照抄 `os.environ` 当宿主的
    环境，漏进来的端点会让测试把 trace 发到开发者真正的 collector 上。要端点的用例自己给。"""
    for name in [name for name in os.environ if name.startswith('OTEL_')]:
        monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def without_opentelemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    drop_opentelemetry(monkeypatch)
