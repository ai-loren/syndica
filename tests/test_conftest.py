"""测试侧的 shell 开关：`SYNDICA_TEST_SHELL` 真的换掉执行命令的 shell，写错时整轮测试拒绝开始。"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

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


@pytest.mark.skipif(not Path('/bin/dash').is_file(), reason='没有 /bin/dash，换 shell 无从验证')
def test_the_agent_bash_behind_the_cli_follows_the_selected_shell_too() -> None:
    probe = (
        'tests/cli/test_environment.py::'
        'test_the_agent_bash_runs_in_the_shell_this_test_run_selected'
    )
    completed = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', probe],
        cwd=_REPO,
        env={**os.environ, 'SYNDICA_TEST_SHELL': '/bin/dash'},
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '1 passed' in completed.stdout


def _conftest() -> Any:
    spec = importlib.util.spec_from_file_location('tests_conftest', _REPO / 'tests' / 'conftest.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_opentelemetry_settings_from_the_developers_shell_are_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT', 'http://collector.invalid:4318')
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_HEADERS', 'x-token=abc')
    monkeypatch.setenv('NOT_OTEL_SETTING', 'kept')

    _conftest().drop_opentelemetry(monkeypatch)

    assert [name for name in os.environ if name.startswith('OTEL_')] == []
    assert os.environ['NOT_OTEL_SETTING'] == 'kept'


def test_no_test_sees_an_opentelemetry_setting() -> None:
    assert [name for name in os.environ if name.startswith('OTEL_')] == []


def test_relative_search_path_entries_are_made_absolute_for_the_subprocesses_tests_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(['src', '', str(tmp_path / 'lib')]))

    _conftest().absolute_search_path()

    assert os.environ['PYTHONPATH'].split(os.pathsep) == [
        str((tmp_path / 'src').resolve()),
        str((tmp_path / 'lib').resolve()),
    ]


def test_every_search_path_entry_this_run_hands_to_a_subprocess_is_absolute() -> None:
    entries = [entry for entry in os.environ.get('PYTHONPATH', '').split(os.pathsep) if entry]

    assert all(Path(entry).is_absolute() for entry in entries), entries


def test_a_run_started_with_a_relative_search_path_hands_on_an_absolute_one() -> None:
    probe = (
        'tests/test_conftest.py::'
        'test_every_search_path_entry_this_run_hands_to_a_subprocess_is_absolute'
    )
    completed = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', probe],
        cwd=_REPO,
        env={**os.environ, 'PYTHONPATH': 'src'},
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '1 passed' in completed.stdout
