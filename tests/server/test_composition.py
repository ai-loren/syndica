"""组合层：经真的会话装配（`cli.assembly`，与 chat 同一份代码）与脚本化模型。server 自己的用例用的
是假装配，漏接了任何一件安全件都照样绿；这里证明服务面的会话确实带着它们。"""

from __future__ import annotations

import asyncio
import json
import os
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
