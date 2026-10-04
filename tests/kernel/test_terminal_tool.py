"""`Terminal` 工具（M5 D7）：模型经它驱动伪终端里的交互式程序。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
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
    before = await _shown_until(
        terminal, handle, lambda text: 'ready' in text, created.meta['output']
    )
    replied = await terminal.execute(action='send', handle=handle, input='hi\n', wait=3)
    after = await _shown_until(
        terminal, handle, lambda text: 'got:hi' in text, replied.meta['output']
    )

    assert 'ready' in before
    assert 'ready' not in after
    assert (created.trust, replied.trust) == (TrustLevel.UNTRUSTED, TrustLevel.UNTRUSTED)


async def test_capture_hands_over_only_what_arrived_since_the_last_look(terminal: Terminal) -> None:
    created = await terminal.execute(action='create', command='echo first; sleep 30', wait=3)
    handle = created.meta['handle']
    shown = await _shown_until(
        terminal, handle, lambda text: 'first' in text, created.meta['output']
    )
    again = await terminal.execute(action='capture', handle=handle, wait=0.2)

    assert 'first' in shown
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


async def _waiting_for_a_line(terminal: Terminal) -> str:
    created = await terminal.execute(action='create', command='read line; echo "got:$line"', wait=0)
    return created.meta['handle']


@pytest.mark.parametrize(
    'typed',
    [
        '\x15git push\n',
        'echo \x15git push\n',
        '\x03 git push\n',
        'gitx\x7f push\n',
        'xgit push\x01\x04\n',
        'gi\tpush\n',
        '\x1b[Agit push\n',
    ],
    ids=[
        'line_kill_first',
        'line_kill_mid_line',
        'interrupt_first',
        'delete_a_character',
        'jump_to_start_and_delete',
        'tab_completion',
        'arrow_key_escape',
    ],
)
async def test_input_that_the_terminal_would_edit_is_refused_and_never_sent(
    terminal: Terminal, typed: str
) -> None:
    handle = await _waiting_for_a_line(terminal)

    refused = await terminal.execute(action='send', handle=handle, input=typed)
    after = await terminal.execute(action='capture', handle=handle, wait=0.3)

    assert 'U+' in refused.output and 'key=ctrl-c' in refused.output
    assert 'got:' not in after.meta['output']


async def test_a_line_split_across_two_sends_is_refused_at_the_second(terminal: Terminal) -> None:
    handle = await _waiting_for_a_line(terminal)

    first = await terminal.execute(action='send', handle=handle, input='gi', wait=0)
    second = await terminal.execute(action='send', handle=handle, input='t push\n', wait=0)

    assert '期望' not in first.output
    assert "'gi'" in second.output and 'key=ctrl-c' in second.output


async def test_ctrl_z_does_not_forget_a_half_typed_line(terminal: Terminal) -> None:
    handle = await _waiting_for_a_line(terminal)

    await terminal.execute(action='send', handle=handle, input='gi', wait=0)
    await terminal.execute(action='send', handle=handle, key='ctrl-z', wait=0)
    joined = await terminal.execute(action='send', handle=handle, input='t push\n', wait=0)

    assert "'gi'" in joined.output


async def test_ctrl_d_is_refused_while_a_half_typed_line_would_be_submitted(
    terminal: Terminal,
) -> None:
    handle = await _waiting_for_a_line(terminal)

    await terminal.execute(action='send', handle=handle, input='gi', wait=0)
    refused = await terminal.execute(action='send', handle=handle, key='ctrl-d', wait=0)

    assert 'ctrl-d' in refused.output and "'gi'" in refused.output


@pytest.mark.parametrize(
    ('key', 'typed', 'anchor'),
    [('ctrl-u', '', 'ctrl-c / ctrl-d / ctrl-z'), ('ctrl-c', 'x', '单独')],
    ids=['unknown_key', 'key_together_with_input'],
)
async def test_a_key_the_tool_does_not_take_says_what_to_send_instead(
    terminal: Terminal, key: str, typed: str, anchor: str
) -> None:
    handle = await _waiting_for_a_line(terminal)

    refused = await terminal.execute(action='send', handle=handle, input=typed, key=key, wait=0)

    assert anchor in refused.output and '下一步' in refused.output


@pytest.mark.skipif(not Path('/bin/bash').exists(), reason='需要 /bin/bash 的 readline')
async def test_ctrl_c_clears_a_half_typed_line_even_where_the_shell_ignores_interrupts(
    terminal: Terminal,
) -> None:
    created = await terminal.execute(
        action='create', command='exec /bin/bash --norc --noprofile -i', wait=2
    )
    handle = created.meta['handle']
    await _shown_until(terminal, handle, lambda text: '$ ' in text, created.meta['output'])
    trapped = await terminal.execute(
        action='send', handle=handle, input="trap '' INT; echo set-$((2-1))\n"
    )
    await _shown_until(
        terminal,
        handle,
        lambda text: '$ ' in text.partition('set-1')[2],
        trapped.meta['output'],
    )
    typed = await terminal.execute(action='send', handle=handle, input='echo $((40+')
    await _shown_until(terminal, handle, lambda text: '$((40+' in text, typed.meta['output'])
    await terminal.execute(action='send', handle=handle, key='ctrl-c', wait=0.3)
    finished = await terminal.execute(action='send', handle=handle, input='2))\n', wait=0)
    after = await _shown_until(terminal, handle, lambda text: '$ ' in text)

    assert '期望' not in finished.output
    assert '42' not in finished.meta['output'] + after


async def _shown_until(
    terminal: Terminal, handle: str, done: Callable[[str], bool], shown: str = ''
) -> str:
    for _ in range(50):
        if done(shown):
            return shown
        captured = await terminal.execute(action='capture', handle=handle, wait=0.2)
        shown += captured.meta['output']
    raise AssertionError(f'期望终端在 10 秒内到达预期状态，实际只看到 {shown!r}')


def test_the_danger_judge_reads_both_the_command_and_the_input() -> None:
    judge = DangerJudge()
    tool = Terminal.__new__(Terminal)

    sent = judge.judge(tool, {'action': 'send', 'handle': 'h', 'input': 'sudo rm -rf /\n'})
    started = judge.judge(tool, {'action': 'create', 'command': 'sudo rm -rf /'})

    assert Terminal.__tool_command_args__ == ('command', 'input')
    assert (sent.level, started.level) == (RiskLevel.HIGH, RiskLevel.HIGH)
