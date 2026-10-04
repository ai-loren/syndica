"""`Terminal` 工具（M5 D7）：模型经它驱动伪终端里的交互式程序。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from syndica.kernel.context import TrustLevel
from syndica.kernel.danger import DangerJudge, RiskLevel
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.tools.local import Terminal


@pytest.fixture
async def terminal(tmp_path: Path) -> AsyncIterator[Terminal]:
    workspace = LocalWorkspace(str(tmp_path), shell='/bin/sh', env={})
    yield Terminal(workspace.portal())
    await workspace.close()


async def test_create_send_and_capture_drive_an_interactive_program(terminal: Terminal) -> None:
    created = await terminal.execute(
        action='create', command='echo ready; read line; echo "got:$line"', wait=3
    )
    handle = created.meta['handle']
    replied = await terminal.execute(action='send', handle=handle, input='hi\n', wait=3)

    assert 'ready' in created.output
    assert 'got:hi' in replied.output
    assert 'ready' not in replied.output
    assert (created.trust, replied.trust) == (TrustLevel.UNTRUSTED, TrustLevel.UNTRUSTED)


async def test_capture_hands_over_only_what_arrived_since_the_last_look(terminal: Terminal) -> None:
    created = await terminal.execute(action='create', command='echo first; sleep 30', wait=3)
    again = await terminal.execute(action='capture', handle=created.meta['handle'], wait=0.2)

    assert 'first' in created.output
    assert again.meta['output'] == ''


async def test_an_unknown_action_lists_the_six_it_takes(terminal: Terminal) -> None:
    result = await terminal.execute(action='open', command='true')

    assert all(
        name in result.output for name in ('create', 'send', 'capture', 'resize', 'kill', 'status')
    )
    assert '下一步' in result.output


async def test_a_killed_session_is_gone_and_says_how_to_continue(terminal: Terminal) -> None:
    created = await terminal.execute(action='create', command='sleep 30', wait=0)
    handle = created.meta['handle']
    killed = await terminal.execute(action='kill', handle=handle)
    after = await terminal.execute(action='capture', handle=handle, wait=0)

    assert handle in killed.output
    assert 'create' in after.output


async def test_resize_and_status_report_the_window(terminal: Terminal) -> None:
    created = await terminal.execute(action='create', command='sleep 30', wait=0)
    handle = created.meta['handle']
    resized = await terminal.execute(action='resize', handle=handle, cols=90, rows=20)

    assert 'size: 90x20' in resized.output
    assert resized.meta['running'] is True


def test_the_danger_judge_reads_both_the_command_and_the_input() -> None:
    judge = DangerJudge()
    tool = Terminal.__new__(Terminal)

    sent = judge.judge(tool, {'action': 'send', 'handle': 'h', 'input': 'sudo rm -rf /\n'})
    started = judge.judge(tool, {'action': 'create', 'command': 'sudo rm -rf /'})

    assert Terminal.__tool_command_args__ == ('command', 'input')
    assert (sent.level, started.level) == (RiskLevel.HIGH, RiskLevel.HIGH)
