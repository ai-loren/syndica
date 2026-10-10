"""组合层：经真的会话装配（`cli.assembly`，与 chat 同一份代码）与脚本化模型。server 自己的用例用的
是假装配，漏接了任何一件安全件都照样绿；这里证明服务面的会话确实带着它们。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from syndica.cli.assembly import ServerAssembly
from syndica.cli.handoff import session_root
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO
from syndica.cli.session_lock import acquire_session_lock
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import ConfigError, ScriptedLLM, ScriptedTurn, ToolCall
from syndica.kernel.driving import _DRIVING_AGENT  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.stores.file_rubric import FileRubricStore
from syndica.server import Live
from syndica.server.outbound import encoded

_KEY = 'sk-test-0123456789abcdefghijkl'
_TIMEOUT = 10.0


def _bash(command: str, call_id: str = 'c1') -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ToolCall(id=call_id, name='Bash', arguments=json.dumps({'command': command}))]
    )


class _Scripts:
    """每次建模型交出下一份脚本：一个会话建一次。"""

    def __init__(self, *scripts: list[ScriptedTurn]) -> None:
        self._scripts = [ScriptedLLM(script) for script in scripts]
        self.built: list[ScriptedLLM] = []

    def __call__(self, _: str) -> ScriptedLLM:
        llm = self._scripts[len(self.built)]
        self.built.append(llm)
        return llm


def _assembly(
    tmp_path: Path, scripts: Callable[[str], ScriptedLLM]
) -> tuple[ServerAssembly, RuntimeCliHost]:
    environ = {**os.environ, 'OPENAI_API_KEY': _KEY}
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(''), stdout=StringIO(), stderr=StringIO()),
        model_factory=scripts,
        workspace_root=tmp_path,
        environ=environ,
    )
    config = host.trusted_config('.syndica.yaml')
    return ServerAssembly(host, path='.syndica.yaml', config=config), host


async def _finished(live: Live, sid: str) -> list[dict[str, Any]]:
    """等根 Agent 这一轮结束，交回会话流里的全部条目。"""
    session = live.sessions[sid]
    for _ in range(int(_TIMEOUT / 0.02)):
        if any(entry.kind == 'agent_end' for entry in session.stream._ring):  # pyright: ignore[reportPrivateUsage]
            break
        await asyncio.sleep(0.02)
    return [
        {'kind': entry.kind, **entry.data}
        for entry in session.stream._ring  # pyright: ignore[reportPrivateUsage]
    ]


async def _run_one(tmp_path: Path, script: list[ScriptedTurn]) -> tuple[Live, list[dict[str, Any]]]:
    assembly, _ = _assembly(tmp_path, _Scripts(script))
    live = Live(assembly)
    session = await live.open_session(resume=None, role='Worker')
    delivered = await session.handle.deliver(session.handle.root_id, 'go')
    assert delivered.ok, delivered.reason
    return live, await _finished(live, session.sid)


def _tool_output(entries: list[dict[str, Any]]) -> str:
    return '\n'.join(
        entry['payload']['block']['text']
        for entry in entries
        if entry['kind'] == 'message_appended' and entry['payload']['block']['role'] == 'tool'
    )


async def test_three_sessions_each_hold_their_session_lock(tmp_path: Path):
    scripts = _Scripts(*([ScriptedTurn(content='ok')] for _ in range(3)))
    assembly, _ = _assembly(tmp_path, scripts)
    live = Live(assembly)
    try:
        sessions = [await live.open_session(resume=None, role='Worker') for _ in range(3)]
        assert len({session.sid for session in sessions}) == 3
        for session in sessions:
            with pytest.raises(ConfigError):
                acquire_session_lock(session_root(tmp_path), session.sid)
    finally:
        await live.shutdown()
    for session in sessions:
        acquire_session_lock(session_root(tmp_path), session.sid).release()


def _read(path: str, call_id: str = 'r1') -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ToolCall(id=call_id, name='Read', arguments=json.dumps({'path': path}))]
    )


async def test_a_file_one_session_wrote_by_command_is_untrusted_in_a_later_session(tmp_path: Path):
    scripts = _Scripts(
        [_bash('echo planted > planted.md'), ScriptedTurn(content='done')],
        [_read('planted.md'), ScriptedTurn(content='done')],
    )
    assembly, _ = _assembly(tmp_path, scripts)
    live = Live(assembly)
    try:
        first = await live.open_session(resume=None, role='Worker')
        assert (await first.handle.deliver(first.handle.root_id, 'go')).ok
        await _finished(live, first.sid)
        assert (tmp_path / 'planted.md').exists()
        second = await live.open_session(resume=None, role='Worker')
        assert (await second.handle.deliver(second.handle.root_id, 'go')).ok
        entries = await _finished(live, second.sid)
    finally:
        await live.shutdown()
    reads = [
        entry['payload']['block']
        for entry in entries
        if entry['kind'] == 'message_appended'
        and entry['payload']['block']['role'] == 'tool'
        and entry['payload']['block']['tool_call_id'] == 'r1'
    ]
    assert [(block['trust'], 'planted' in block['text']) for block in reads] == [
        ('untrusted', True)
    ]


async def test_a_git_write_from_a_server_session_is_blocked_by_dev_mode(tmp_path: Path):
    live, entries = await _run_one(
        tmp_path, [_bash('git commit -m x'), ScriptedTurn(content='done')]
    )
    try:
        finished = [entry['payload'] for entry in entries if entry['kind'] == 'tool_call_finished']
        assert [payload['outcome'] for payload in finished] == ['skipped']
        assert 'git' in _tool_output(entries)
        assert not (tmp_path / '.git').exists()
    finally:
        await live.shutdown()


async def test_a_host_secret_is_not_in_the_agents_environment(tmp_path: Path):
    live, entries = await _run_one(tmp_path, [_bash('env'), ScriptedTurn(content='done')])
    try:
        output = _tool_output(entries)
        assert 'PATH=' in output
        assert 'OPENAI_API_KEY' not in output
        assert _KEY not in output
    finally:
        await live.shutdown()


async def test_a_secret_in_tool_output_is_redacted_on_the_way_out(tmp_path: Path):
    (tmp_path / 'notes.txt').write_text(f'the key is {_KEY}\n')
    live, entries = await _run_one(tmp_path, [_bash('cat notes.txt'), ScriptedTurn(content='done')])
    try:
        session = next(iter(live.sessions.values()))
        assert _KEY in _tool_output(entries)
        outbound = encoded(entries, session.handle.redactor())
        assert _KEY not in outbound
        snapshot = await session.snapshot()
        assert _KEY not in encoded(snapshot, session.handle.redactor())
    finally:
        await live.shutdown()


async def test_a_config_edited_after_start_is_not_used_and_doctor_says_so(tmp_path: Path):
    (tmp_path / '.syndica.yaml').write_text('version: 1\n')
    assembly, _ = _assembly(tmp_path, lambda _: ScriptedLLM([]))
    roles = assembly.roles()
    assert [check['ok'] for check in assembly.doctor()][0] is True
    (tmp_path / '.syndica.yaml').write_text('version: 1\n# edited after the server started\n')
    assert assembly.roles() == roles
    [config] = [check for check in assembly.doctor() if check['name'] == 'config']
    assert config['ok'] is False
    assert 'syndica down' in config['next_action']


async def test_doctor_reports_a_config_broken_after_start_as_one_failing_check(tmp_path: Path):
    (tmp_path / '.syndica.yaml').write_text('version: 1\n')
    assembly, _ = _assembly(tmp_path, lambda _: ScriptedLLM([]))
    (tmp_path / '.syndica.yaml').write_text('version: 99\n')
    checks = assembly.doctor()
    [config] = [check for check in checks if check['name'] == 'config']
    assert config['ok'] is False
    assert '读不出来' in config['received'] and 'syndica down' in config['next_action']
    assert len(checks) > 1


async def test_doctor_reports_a_model_that_cannot_be_built(tmp_path: Path):
    def broken(_: str) -> Any:
        raise ConfigError(
            expected='GEMINI_API_KEY 已设置',
            received='没有设置',
            next_action='export GEMINI_API_KEY=…',
        )

    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(''), stdout=StringIO(), stderr=StringIO()),
        model_factory=broken,
        workspace_root=tmp_path,
        environ=dict(os.environ),
    )
    assembly = ServerAssembly(
        host, path='.syndica.yaml', config=host.trusted_config('.syndica.yaml')
    )
    failing = [check for check in assembly.doctor() if not check['ok']]
    assert failing and all(check['expected'] == 'GEMINI_API_KEY 已设置' for check in failing)
    with pytest.raises(ConfigError):
        await Live(assembly).open_session(resume=None, role='Worker')


async def test_a_human_answer_from_the_server_approves_a_confirmation(tmp_path: Path):
    outside = tmp_path.parent / f'{tmp_path.name}-outside.txt'
    outside.write_text('outside content\n')
    assembly, _ = _assembly(
        tmp_path, _Scripts([_bash(f'cat {outside}'), ScriptedTurn(content='done')])
    )
    live = Live(assembly)
    try:
        session = await live.open_session(resume=None, role='Worker')
        await session.handle.deliver(session.handle.root_id, 'go')
        for _ in range(int(_TIMEOUT / 0.02)):
            if session.pending:
                break
            await asyncio.sleep(0.02)
        [pending] = list(session.pending.values())
        assert pending.kind == 'confirmation'
        await session.answer(pending.request_id, 'approve')
        entries = await _finished(live, session.sid)
        assert 'outside content' in _tool_output(entries)
        assert session.pending == {}
    finally:
        await live.shutdown()
        outside.unlink()


async def test_a_config_broken_after_start_does_not_stop_new_sessions(tmp_path: Path):
    assembly, _ = _assembly(tmp_path, lambda _: ScriptedLLM([ScriptedTurn(content='ok')]))
    (tmp_path / '.syndica.yaml').write_text('version: 99\n')
    live = Live(assembly)
    try:
        session = await live.open_session(resume=None, role='Worker')
        assert session.sid
    finally:
        await live.shutdown()


def _write(path: str, content: str, call_id: str) -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[
            ToolCall(
                id=call_id,
                name='Write',
                arguments=json.dumps({'path': path, 'content': content}),
            )
        ]
    )


async def test_artifacts_written_in_a_session_can_be_previewed_diffed_and_given_feedback(
    tmp_path: Path,
):
    (tmp_path / '.syndica.yaml').write_text('version: 1\nmodules: [acceptance]\n')
    script = [
        _write('report.md', '# 报告\n第一版\n', 'w1'),
        _write('report.md', f'# 报告\n第二版 {_KEY}\n结论\n', 'w2'),
        ScriptedTurn(content='done'),
    ]
    assembly, _ = _assembly(tmp_path, lambda _: ScriptedLLM(list(script)))
    live = Live(assembly)
    try:
        session = await live.open_session(resume=None, role='Worker')
        await session.handle.deliver(session.handle.root_id, 'go')
        await _finished(live, session.sid)
        handle: Any = session.handle
        [listed] = await handle.artifact_list()
        assert (listed['id'], listed['versions']) == ('file:report.md', 2)
        old, new = [row['version'] for row in await handle.artifact_versions('file:report.md')]
        assert '第一版' in await handle.artifact_preview('file:report.md', old)
        diff = await handle.artifact_diff('file:report.md', old, new)
        assert diff['changed'] is True and '+结论' in diff['unified']
        assert _KEY not in encoded(diff, handle.redactor())
        content, mime = await handle.artifact_content('file:report.md', new)
        assert content.decode().startswith('# 报告') and mime == 'text/markdown'
        with pytest.raises(ConfigError):
            await handle.artifact_feedback('file:report.md', new, '太短', None)
        rubric = await handle.artifact_feedback('file:report.md', new, '结论要给出具体数字', (2, 3))
        assert rubric is not None and rubric['status'] == 'proposed'
        store = FileRubricStore(tmp_path / '.syndica' / 'rubrics')
        [proposed] = await store.rubrics(session.sid, include_proposed=True)
        assert [anchor.locator for anchor in proposed.anchors][1:] == ['report.md#L2-L3']
        await handle.activate_rubric(rubric['id'])
        [stored] = (await handle.rubrics())['rubrics']
        assert (stored['id'], stored['status']) == (rubric['id'], 'active')
        with pytest.raises(ConfigError):
            await handle.activate_rubric(rubric['id'])
    finally:
        await live.shutdown()


async def test_the_human_terminal_runs_in_the_agent_environment_and_is_gone_after_close(
    tmp_path: Path,
):
    assembly, _ = _assembly(tmp_path, lambda _: ScriptedLLM([]))
    live = Live(assembly)
    try:
        session = await live.open_session(resume=None, role='Worker')
        handle: Any = session.handle
        terminal = handle.open_terminal(80, 24)
        terminal.write(b'echo key=[$OPENAI_API_KEY] marker=[$SYNDICA_AGENT_ENV] done=yes\n')
        seen = b''
        for _ in range(100):
            seen += await asyncio.wait_for(terminal.read(), 5)
            if b'done=yes' in seen and b'marker=[1]' in seen:
                break
        assert b'key=[]' in seen and b'marker=[1]' in seen
        assert _KEY.encode() not in seen
        pid = terminal._process.pid  # pyright: ignore[reportPrivateUsage]
        terminal.write(b"trap '' HUP; sleep 3017\n")
        await asyncio.sleep(0.5)
        assert _sleeping('3017')
    finally:
        await live.shutdown()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    for _ in range(50):
        if not _sleeping('3017'):
            break
        await asyncio.sleep(0.1)
    assert not _sleeping('3017')


async def _terminal_until(terminal: Any, marker: bytes) -> bytes:
    """读人的终端直到出现 marker、shell 退出或超时，交回读到的全部输出。"""
    seen = b''
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(_TIMEOUT):
            while not marker or marker not in seen:
                chunk = await terminal.read()
                if not chunk:
                    break
                seen += chunk
    return seen


async def test_ctrl_c_in_the_human_terminal_interrupts_the_foreground_command(tmp_path: Path):
    assembly, _ = _assembly(tmp_path, lambda _: ScriptedLLM([]))
    live = Live(assembly)
    try:
        session = await live.open_session(resume=None, role='Worker')
        handle: Any = session.handle
        terminal = handle.open_terminal(80, 24)
        terminal.write(b'echo started=$((40+2)); sleep 3019\n')
        await _terminal_until(terminal, b'started=42')
        await asyncio.sleep(0.3)
        terminal.write(b'\x03')
        terminal.write(b'echo after=$((1+1))\n')
        seen = await _terminal_until(terminal, b'after=2')
    finally:
        await live.shutdown()

    assert b'after=2' in seen, seen


async def test_closing_the_human_terminal_after_its_shell_exited_spares_the_servers_own_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    real_killpg = os.killpg
    own = os.getpgrp()
    signalled: list[int] = []

    def recording_killpg(group: int, signum: int) -> None:
        signalled.append(group)
        if group not in (0, own):
            real_killpg(group, signum)

    assembly, _ = _assembly(tmp_path, lambda _: ScriptedLLM([]))
    live = Live(assembly)
    try:
        session = await live.open_session(resume=None, role='Worker')
        handle: Any = session.handle
        terminal = handle.open_terminal(80, 24)
        terminal.write(b'exit\n')
        await _terminal_until(terminal, b'')
        await asyncio.to_thread(terminal._process.wait)
        monkeypatch.setattr(os, 'killpg', recording_killpg)
        await terminal.close()
    finally:
        await live.shutdown()

    assert signalled
    assert [group for group in signalled if group in (0, own)] == []


async def test_the_agent_terminal_mirror_finds_a_terminal_opened_inside_an_isolated_copy(
    tmp_path: Path,
):
    assembly, _ = _assembly(tmp_path, lambda _: ScriptedLLM([]))
    live = Live(assembly)
    try:
        session = await live.open_session(resume=None, role='Worker')
        handle: Any = session.handle
        router = handle._parts.workspace  # pyright: ignore[reportPrivateUsage]
        assert router.isolate('alpha', 'snapshot') is not None

        async def as_alpha() -> str:
            _DRIVING_AGENT.set('alpha')
            return await router.pty_create('echo from-the-copy; sleep 5', cwd='.')

        opened = await asyncio.create_task(as_alpha())
        screen = await handle.agent_screen('alpha', opened, 0, 2.0)
    finally:
        await live.shutdown()
    assert 'from-the-copy' in screen['text']


def _sleeping(seconds: str) -> bool:
    found = subprocess.run(['pgrep', '-f', f'sleep {seconds}'], capture_output=True, text=True)
    return bool(found.stdout.strip())


async def test_the_session_trace_lists_the_tool_calls_the_event_stream_showed(tmp_path: Path):
    live, entries = await _run_one(
        tmp_path, [_bash('echo hi', 'c-trace'), ScriptedTurn(content='ok')]
    )
    try:
        session = next(iter(live.sessions.values()))
        trace_id = session.handle.trace_id()  # pyright: ignore[reportAttributeAccessIssue]
        assert trace_id is not None
        analysis = await live.assembly.trace(trace_id)  # pyright: ignore[reportAttributeAccessIssue]
        streamed = [
            entry['payload']['tool_call_id']
            for entry in entries
            if entry['kind'] == 'tool_call_started'
        ]
        assert streamed == ['c-trace']
        assert [call['call_id'] for call in analysis['tool_calls']] == streamed
        assert any(row['trace_id'] == trace_id for row in await live.assembly.traces())  # pyright: ignore[reportAttributeAccessIssue]
    finally:
        await live.shutdown()
