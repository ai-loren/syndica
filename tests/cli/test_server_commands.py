"""`up` / `down` / `status` 交给组合根注入的 server 控制；它们是命令，不是隐式 run 的 prompt。"""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest

from syndica.cli.main import CliIO, CliOutcome, ServerControl, main


class _Control:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def up(
        self,
        config: str,
        *,
        port: int,
        foreground: bool,
        open_browser: bool,
        api_token: bool = False,
    ) -> CliOutcome:
        self.calls.append(('up', (config, port, foreground, open_browser, api_token)))
        return CliOutcome('started', 0)

    def down(self) -> str:
        self.calls.append(('down', None))
        return 'stopped'

    def status(self) -> CliOutcome:
        self.calls.append(('status', None))
        return CliOutcome('not running', 3)


def _run(argv: list[str], control: ServerControl | None) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    code = main(argv, io=CliIO(stdin=StringIO(''), stdout=stdout, stderr=stderr), server=control)
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize(
    ('argv', 'call', 'code'),
    [
        (['up'], ('up', ('.syndica.yaml', 0, False, True, False)), 0),
        (
            ['--config', 'x.yaml', 'up', '--port', '8765', '--foreground', '--no-open'],
            ('up', ('x.yaml', 8765, True, False, False)),
            0,
        ),
        (['up', '--api-token', '--no-open'], ('up', ('.syndica.yaml', 0, False, False, True)), 0),
        (['down'], ('down', None), 0),
        (['status'], ('status', None), 3),
    ],
)
def test_server_commands_reach_the_injected_control(
    argv: list[str], call: tuple[str, Any], code: int
):
    control = _Control()
    exit_code, _, _ = _run(argv, control)
    assert control.calls == [call]
    assert exit_code == code


def test_without_a_server_control_the_commands_explain_themselves():
    code, _, stderr = _run(['up'], None)
    assert code == 2
    assert '期望' in stderr and '下一步' in stderr


class _Mcp(_Control):
    def mcp(self, config: str) -> str:
        self.calls.append(('mcp', config))
        return 'mcp 已退出'


def test_mcp_reaches_the_injected_control():
    control = _Mcp()
    code, stdout, _ = _run(['--config', 'x.yaml', 'mcp'], control)
    assert control.calls == [('mcp', 'x.yaml')]
    assert code == 0
    assert stdout == 'mcp 已退出\n'


def test_mcp_without_a_runner_explains_itself():
    code, _, stderr = _run(['mcp'], _Control())
    assert code == 2
    assert 'server.mcp' in stderr and '下一步' in stderr


def test_acp_reaches_the_injected_control():
    class _Acp(_Control):
        def acp(self, config: str) -> str:
            self.calls.append(('acp', config))
            return ''

    control = _Acp()
    code, stdout, _ = _run(['--config', 'x.yaml', 'acp'], control)
    assert control.calls == [('acp', 'x.yaml')]
    assert code == 0
    assert stdout == ''


def test_acp_without_a_runner_explains_itself():
    code, _, stderr = _run(['acp'], _Control())
    assert code == 2
    assert 'server.acp' in stderr and '下一步' in stderr


def test_acp_in_the_agent_environment_is_refused(monkeypatch: pytest.MonkeyPatch):
    from syndica.__main__ import _ServerControl  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setenv('SYNDICA_AGENT_ENV', '1')
    control = _ServerControl(CliIO(stdin=StringIO(''), stdout=StringIO(), stderr=StringIO()))
    code, _, stderr = _run(['acp'], control)
    assert code == 1
    assert 'SYNDICA_AGENT_ENV' in stderr and '下一步' in stderr


def test_mcp_in_the_agent_environment_is_refused(monkeypatch: pytest.MonkeyPatch):
    from syndica.__main__ import _ServerControl  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setenv('SYNDICA_AGENT_ENV', '1')
    control = _ServerControl(CliIO(stdin=StringIO(''), stdout=StringIO(), stderr=StringIO()))
    code, _, stderr = _run(['mcp'], control)
    assert code == 1
    assert 'SYNDICA_AGENT_ENV' in stderr and '下一步' in stderr


@pytest.mark.parametrize('port', ['-1', '65536', 'http'])
def test_a_port_outside_the_range_is_refused_by_the_parser(port: str):
    control = _Control()
    code, _, stderr = _run(['up', '--port', port], control)
    assert code == 2
    assert control.calls == []
    assert 'port' in stderr
