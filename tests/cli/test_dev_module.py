"""开发期 Module 的记录（spec D11）：检查结果、`dev_log`、Reviewer 的工作树指纹。"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import syndica.cli.dev_module as dev_module
import syndica.kernel.local_workspace as local_workspace
from syndica.cli.dev_module import AgentRun, DevLog, DevModule
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, RunRequest
from syndica.kernel import (
    AgentInfo,
    ConfigError,
    Context,
    ContextBlock,
    HooksCenter,
    Junction,
    ScriptedLLM,
    ScriptedTurn,
    StopReason,
    Tool,
    ToolCall,
    ToolResult,
    TrustLevel,
)

# --- 切点层 -----------------------------------------------------------------------------------


class _Named(Tool):
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        """占位：记录插件只看工具名。"""
        return ToolResult(output='')


def _tool(name: str) -> Tool:
    return cast(type[Tool], type(name, (_Named,), {'__tool_name__': name}))()


class _Registry:
    def __init__(self, parent: str | None) -> None:
        self._parent = parent

    async def get(self, agent_id: str) -> AgentInfo:
        return AgentInfo(agent_id=agent_id, name='x', agent_type='x', parent_id=self._parent)


def _hooks(
    log: DevLog,
    *,
    agent_id: str = 'agent-1',
    role: str = 'Worker',
    parent: str | None = None,
    root: Path = Path('/nonexistent-workspace'),
    reviewers: tuple[str, ...] = ('Reviewer',),
) -> HooksCenter:
    """经 Module 自己的插件工厂装一个记录插件：工厂里的登记也一起走到。"""
    runtime = SimpleNamespace(dev_log=log, registry=_Registry(parent))
    agent = SimpleNamespace(agent_id=agent_id, options=SimpleNamespace(name=role))
    module = DevModule(root, {}, reviewer_roles=reviewers)
    plugin = cast(Any, module.agent_plugins[1])(runtime, agent, {})
    hooks = HooksCenter()
    plugin.register(hooks)
    return hooks


def _finished(stdout: str = '', *, handle: str | None = None, **overrides: object) -> ToolResult:
    """与 Bash / BashOutput 交回的 meta 同形：后台命令的 handle 在最前面。"""
    fields: dict[str, object] = {
        'exit_code': 0,
        'running': False,
        'truncated': False,
        'timed_out': False,
        'stdout': stdout,
        'stderr': '',
        **overrides,
    }
    meta = fields if handle is None else {'handle': handle, **fields}
    return ToolResult(output='shown', meta=meta, trust=TrustLevel.UNTRUSTED)


async def _post(
    hooks: HooksCenter, name: str, args: dict[str, Any], result: ToolResult
) -> ToolResult:
    call = ToolCall(id='call', name=name, arguments=json.dumps(args))
    outcome = await hooks.trigger(
        Junction.POST_TOOL_USE, tool_call=call, tool=_tool(name), args=args, result=result
    )
    return cast(ToolResult, outcome.get('result', result))


async def test_a_finished_command_gets_its_check_in_a_fixed_key_order():
    log = DevLog()
    hooks = _hooks(log)

    result = await _post(
        hooks, 'Bash', {'command': 'uv run pytest -q'}, _finished('3 passed, 1 skipped in 0.10s')
    )

    check = result.meta['check']
    assert list(result.meta)[-1] == 'check'
    assert list(check) == ['schema', 'tool', 'status', 'counts', 'summary']
    assert check['schema'] == 1
    assert list(check['counts']) == ['passed', 'failed', 'errors', 'skipped', 'xfailed', 'xpassed']
    assert (check['tool'], check['status'], check['summary']) == (
        'pytest',
        'passed',
        'passed=3 skipped=1',
    )


async def test_the_checked_result_keeps_its_trust_output_and_other_meta():
    log = DevLog()
    original = _finished('ok')

    result = await _post(_hooks(log), 'Bash', {'command': 'echo ok'}, original)

    assert result.trust is TrustLevel.UNTRUSTED
    assert result.output == original.output
    assert {key: result.meta[key] for key in original.meta} == original.meta
    assert 'check' not in original.meta


async def test_a_timed_out_command_is_checked_and_recorded_as_a_timeout():
    log = DevLog()

    result = await _post(
        _hooks(log), 'Bash', {'command': 'uv run pytest'}, _finished(exit_code=None, timed_out=True)
    )

    assert result.meta['check']['status'] == 'timeout'
    entry = log.snapshot().tools[0]
    assert (entry.tool, entry.exit_code, entry.check is not None and entry.check.status) == (
        'Bash',
        None,
        'timeout',
    )


async def test_a_background_command_is_recorded_once_when_first_seen_finished():
    log = DevLog()
    hooks = _hooks(log)
    started = ToolResult(output='started', meta={'handle': 'h1'})
    running = ToolResult(output='still', meta={'handle': 'h1', 'running': True, 'exit_code': None})

    done = _finished('2 passed in 0.05s', handle='h1')
    await _post(hooks, 'Bash', {'command': 'uv run pytest -q', 'run_in_background': True}, started)
    polled = await _post(hooks, 'BashOutput', {'handle': 'h1'}, running)
    first = await _post(hooks, 'BashOutput', {'handle': 'h1'}, done)
    again = await _post(hooks, 'BashOutput', {'handle': 'h1'}, done)

    tools = log.snapshot().tools
    assert [(entry.tool, entry.target, entry.exit_code, entry.handle) for entry in tools] == [
        ('Bash', 'uv run pytest -q', None, 'h1'),
        ('BashOutput', 'uv run pytest -q', 0, None),
    ]
    assert tools[1].check is not None and tools[1].check.tool == 'pytest'
    assert 'check' not in polled.meta
    assert first.meta['check']['summary'] == 'passed=2'
    assert 'check' not in again.meta


async def test_each_background_handle_is_deduplicated_on_its_own():
    log = DevLog()
    hooks = _hooks(log)
    for handle, command in (('h1', 'echo one'), ('h2', 'echo two')):
        started = ToolResult(output='started', meta={'handle': handle})
        await _post(hooks, 'Bash', {'command': command, 'run_in_background': True}, started)

    for handle in ('h2', 'h1', 'h2'):
        await _post(hooks, 'BashOutput', {'handle': handle}, _finished(handle=handle))

    finished = [entry.target for entry in log.snapshot().tools if entry.tool == 'BashOutput']
    assert finished == ['echo two', 'echo one']


async def test_a_background_handle_started_elsewhere_is_still_recorded_only_once():
    log = DevLog()
    hooks = _hooks(log)

    for _ in range(2):
        await _post(hooks, 'BashOutput', {'handle': 'other'}, _finished(handle='other'))

    assert [(entry.tool, entry.target) for entry in log.snapshot().tools] == [('BashOutput', '')]


async def test_a_bash_call_rejected_before_running_is_recorded_without_a_check():
    log = DevLog()
    rejected = ToolResult(output='期望: timeout 是有限正数秒数 …', trust=TrustLevel.TRUSTED)

    result = await _post(_hooks(log), 'Bash', {'command': 'echo x', 'timeout': -1}, rejected)

    assert result is rejected
    entry = log.snapshot().tools[0]
    assert (entry.tool, entry.target, entry.exit_code, entry.check) == (
        'Bash',
        'echo x',
        None,
        None,
    )


@pytest.mark.parametrize(
    'name', ['MyBash', 'Bashful', 'WriteNote', 'Editor', 'bash', 'TaskOutput', 'BashOutputs']
)
async def test_tools_are_recognised_by_their_exact_name(name: str):
    log = DevLog()
    args = {'command': 'echo x', 'path': 'a', 'handle': 'h'}

    result = await _post(_hooks(log), name, args, _finished(handle='h', path='a'))

    assert log.snapshot().tools == ()
    assert 'check' not in result.meta


def _wrote(path: str) -> ToolResult:
    """Write / Edit 写成了时交回的结果：meta 里带着路径。"""
    return ToolResult(output=f'已写入 {path}', meta={'path': path})


@pytest.mark.parametrize('name', ['Write', 'Edit'])
async def test_write_and_edit_are_recorded_with_their_path(name: str):
    log = DevLog()

    await _post(_hooks(log), name, {'path': 'src/a.py'}, _wrote('src/a.py'))

    entry = log.snapshot().tools[0]
    assert (entry.agent_id, entry.role, entry.tool, entry.target, entry.check) == (
        'agent-1',
        'Worker',
        name,
        'src/a.py',
        None,
    )


@pytest.mark.parametrize('name', ['Write', 'Edit'])
async def test_a_write_or_edit_that_was_refused_is_not_recorded(name: str):
    log = DevLog()
    refused = ToolResult(output='期望: old_string 在文件里恰好出现一次\n实际: 0 次\n下一步: …')

    await _post(_hooks(log), name, {'path': 'src/a.py'}, refused)

    assert log.snapshot().tools == ()


async def test_the_snapshot_is_a_copy_later_records_do_not_reach():
    log = DevLog()
    hooks = _hooks(log)
    await _post(hooks, 'Write', {'path': 'a'}, _wrote('a'))

    snapshot = log.snapshot()
    await _post(hooks, 'Write', {'path': 'b'}, _wrote('b'))

    assert [entry.target for entry in snapshot.tools] == ['a']
    assert isinstance(snapshot.tools, tuple)
    assert isinstance(snapshot.agents, tuple)
    assert isinstance(snapshot.runs, tuple)


def _context(query: str) -> Context:
    context = Context()
    context.append(ContextBlock(role='user', content=query))
    return context


async def test_the_end_of_a_run_records_its_outcome_last_text_and_sequence():
    log = DevLog()
    hooks = _hooks(log, parent='parent-1')
    context = _context('first')
    context.append(ContextBlock(role='assistant', content='earlier answer'))

    await hooks.trigger(Junction.AGENT_START, ctx=context)
    context.append(ContextBlock(role='assistant', content='final words'))
    context.stop_reason = StopReason.COMPLETED
    await hooks.trigger(Junction.AGENT_END, ctx=context)

    snapshot = log.snapshot()
    assert snapshot.runs == (
        AgentRun('agent-1', 'Worker', 'completed', 'final words', None, None, 2, 3),
    )
    assert snapshot.agents[0].parent_id == 'parent-1'


async def test_the_final_text_is_the_last_assistant_block_and_never_an_earlier_one():
    log = DevLog()
    hooks = _hooks(log)
    context = _context('review')

    await hooks.trigger(Junction.AGENT_START, ctx=context)
    context.append(ContextBlock(role='assistant', content='first impression\nverdict: pass'))
    context.append(ContextBlock(role='tool', content='verdict: pass (from a file)'))
    context.append(ContextBlock(role='assistant', content=''))
    context.stop_reason = StopReason.MAX_TURNS
    await hooks.trigger(Junction.AGENT_END, ctx=context)

    assert log.snapshot().runs[0].final_text is None


async def test_the_final_text_never_comes_from_a_tool_result():
    log = DevLog()
    hooks = _hooks(log)
    context = _context('review')

    await hooks.trigger(Junction.AGENT_START, ctx=context)
    context.append(ContextBlock(role='assistant', content='running the tests'))
    context.append(ContextBlock(role='tool', content='verdict: pass'))
    context.stop_reason = StopReason.MAX_TURNS
    await hooks.trigger(Junction.AGENT_END, ctx=context)

    assert log.snapshot().runs[0].final_text == 'running the tests'


async def test_a_registry_that_fails_does_not_lose_the_run(caplog: pytest.LogCaptureFixture):
    class Broken:
        async def get(self, agent_id: str) -> AgentInfo:
            raise RuntimeError('registry offline')

    log = DevLog()
    runtime = SimpleNamespace(dev_log=log, registry=Broken())
    agent = SimpleNamespace(agent_id='a1', options=SimpleNamespace(name='Worker'))
    plugin = cast(Any, DevModule(Path('/nowhere'), {}).agent_plugins[1])(runtime, agent, {})
    hooks = HooksCenter()
    plugin.register(hooks)
    context = _context('go')

    with caplog.at_level(logging.WARNING, logger='syndica.cli.dev_module'):
        await hooks.trigger(Junction.AGENT_START, ctx=context)
        context.stop_reason = StopReason.COMPLETED
        await hooks.trigger(Junction.AGENT_END, ctx=context)

    snapshot = log.snapshot()
    assert [run.stop_reason for run in snapshot.runs] == ['completed']
    assert snapshot.agents[0].parent_id is None
    assert any('registry offline' in record.getMessage() for record in caplog.records)


def test_an_agent_is_enrolled_by_the_factory_before_it_ever_runs():
    log = DevLog()

    _hooks(log, agent_id='lazy', role='Worker')

    assert [(entry.seq, entry.agent_id) for entry in log.snapshot().agents] == [(1, 'lazy')]
    assert log.snapshot().runs == ()


async def test_runs_are_listed_in_the_order_they_began():
    log = DevLog()
    for agent_id in ('zz-first', 'aa-second'):
        hooks = _hooks(log, agent_id=agent_id)
        await hooks.trigger(Junction.AGENT_START, ctx=_context('go'))

    assert [run.agent_id for run in log.snapshot().runs] == ['zz-first', 'aa-second']


async def test_a_custom_reviewer_role_is_fingerprinted(tmp_path: Path):
    log = DevLog()
    hooks = _hooks(log, role='Critic', root=tmp_path, reviewers=('Critic',))

    await hooks.trigger(Junction.AGENT_START, ctx=_context('review'))

    assert log.snapshot().runs[0].start is not None


async def test_any_failure_while_fingerprinting_is_recorded_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def exploding(self: object) -> tuple[object, ...]:
        raise RuntimeError('unexpected')

    monkeypatch.setattr(dev_module.HostGit, 'changes', exploding)
    log = DevLog()
    hooks = _hooks(log, role='Reviewer', root=tmp_path)

    await hooks.trigger(Junction.AGENT_START, ctx=_context('review'))

    start = log.snapshot().runs[0].start
    assert start is not None and start.changes is None
    assert start.error == 'RuntimeError: unexpected'


def test_a_reviewer_role_given_as_a_bare_string_is_refused(tmp_path: Path):
    with pytest.raises(ConfigError) as caught:
        DevModule(tmp_path, {}, reviewer_roles='Reviewer')

    assert "('Reviewer',)" in caught.value.next_action


def test_an_environ_that_is_not_a_mapping_is_refused(tmp_path: Path):
    with pytest.raises(ConfigError) as caught:
        DevModule(tmp_path, cast(Any, None))

    assert 'os.environ' in caught.value.next_action


async def test_a_final_answer_in_list_form_is_joined_from_its_text_parts():
    log = DevLog()
    hooks = _hooks(log)
    context = _context('review')

    await hooks.trigger(Junction.AGENT_START, ctx=context)
    context.append(ContextBlock(role='assistant', content='first impression\nverdict: pass'))
    parts = [
        {'type': 'thinking', 'thinking': 'verdict: pass'},
        {'type': 'reasoning', 'text': 'verdict: pass\n'},
        {'type': 'text', 'text': 'on reflection\n'},
        {'type': 'text', 'text': 'verdict: block'},
    ]
    context.append(ContextBlock(role='assistant', content=parts))
    context.stop_reason = StopReason.COMPLETED
    await hooks.trigger(Junction.AGENT_END, ctx=context)

    assert log.snapshot().runs[0].final_text == 'on reflection\nverdict: block'


async def test_a_run_without_new_text_has_no_final_text_from_the_previous_run():
    log = DevLog()
    hooks = _hooks(log)
    context = _context('first')
    context.append(ContextBlock(role='assistant', content='previous run'))

    await hooks.trigger(Junction.AGENT_START, ctx=context)
    context.stop_reason = StopReason.MAX_TURNS
    await hooks.trigger(Junction.AGENT_END, ctx=context)

    assert log.snapshot().runs[0].final_text is None


async def test_a_run_that_fails_is_recorded_as_an_error():
    log = DevLog()
    hooks = _hooks(log)
    context = _context('go')

    await hooks.trigger(Junction.AGENT_START, ctx=context)
    context.stop_reason = StopReason.ERROR
    await hooks.trigger(Junction.AGENT_ERROR, ctx=context)

    assert log.snapshot().runs == (AgentRun('agent-1', 'Worker', 'error', None, None, None, 2, 3),)


async def test_each_run_of_the_same_agent_is_recorded_separately():
    log = DevLog()
    hooks = _hooks(log)
    context = _context('one')
    for answer in ('first', 'second'):
        await hooks.trigger(Junction.AGENT_START, ctx=context)
        context.append(ContextBlock(role='assistant', content=answer))
        context.stop_reason = StopReason.COMPLETED
        await hooks.trigger(Junction.AGENT_END, ctx=context)
        context.append(ContextBlock(role='user', content='again'))

    assert [run.final_text for run in log.snapshot().runs] == ['first', 'second']


async def test_a_fingerprint_that_fails_is_recorded_as_missing_not_as_no_changes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    log = DevLog()
    hooks = _hooks(log, role='Reviewer', root=tmp_path)
    context = _context('review')

    with caplog.at_level(logging.WARNING, logger='syndica.cli.dev_module'):
        await hooks.trigger(Junction.AGENT_START, ctx=context)
        context.stop_reason = StopReason.COMPLETED
        await hooks.trigger(Junction.AGENT_END, ctx=context)

    run = log.snapshot().runs[0]
    assert run.start is not None and run.end is not None
    assert (run.start.changes, run.end.changes) == (None, None)
    assert run.start.error is not None and run.start.error.startswith('GitUnavailable')
    assert [record.args[:2] for record in caplog.records if isinstance(record.args, tuple)] == [
        ('agent-1', '开始'),
        ('agent-1', '结束'),
    ]


async def test_only_reviewers_are_fingerprinted(tmp_path: Path):
    log = DevLog()
    hooks = _hooks(log, role='Worker', root=tmp_path)
    context = _context('work')

    await hooks.trigger(Junction.AGENT_START, ctx=context)
    context.stop_reason = StopReason.COMPLETED
    await hooks.trigger(Junction.AGENT_END, ctx=context)

    run = log.snapshot().runs[0]
    assert (run.start, run.end) == (None, None)


# --- 经 CLI 宿主跑一次 run ------------------------------------------------------------------


def _call(call_id: str, name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> list[DevLog]:
    """每个 Runtime 建的那一份 `dev_log`。"""
    created: list[DevLog] = []

    class Captured(DevLog):
        def __init__(self) -> None:
            super().__init__()
            created.append(self)

    monkeypatch.setattr(dev_module, 'DevLog', Captured)
    return created


def _host_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    return {
        **os.environ,
        'HOME': str(home),
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_NOSYSTEM': '1',
    }


def _run(
    tmp_path: Path,
    llm: ScriptedLLM,
    *,
    agent: str | None = 'Worker',
    topology: str | None = None,
    config: str = 'version: 1\n',
    root: Path | None = None,
    environ: dict[str, str] | None = None,
) -> str:
    workspace = root or tmp_path / 'ws'
    workspace.mkdir(parents=True, exist_ok=True)
    path = tmp_path / 'syndica.yaml'
    path.write_text(config, encoding='utf-8')
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=workspace,
        environ=environ or _host_env(tmp_path),
    )
    try:
        answer = host.run_request(
            RunRequest(prompt='work', agent=agent, topology=topology, config=str(path))
        )
        assert isinstance(answer, str)
        return answer
    finally:
        host.close()


def _role(tmp_path: Path, name: str, tools: str) -> str:
    (tmp_path / f'{name.lower()}.md').write_text(f'You are {name}.\n', encoding='utf-8')
    return (
        f'version: 1\nagents:\n  - name: {name}\n    description: {name}\n'
        f'    system_prompt: {name.lower()}.md\n    tools: [{tools}]\n'
    )


_LEAD_WITH_BASH = """version: 1
topologies:
  lead:
    slots:
      - {name: root, agent: Orchestrator, is_root: true, cardinality: singleton, boot: eager,
         extra_tools: [Bash]}
"""


def test_two_agents_records_do_not_cross_and_each_is_enrolled_first(
    tmp_path: Path, logs: list[DevLog]
):
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('p', 'Bash', command='echo parent')]),
            ScriptedTurn(tool_calls=[_call('d', 'Delegate', agent='Worker', query='do it')]),
            ScriptedTurn(tool_calls=[_call('c', 'Bash', command='echo child')]),
            ScriptedTurn(content='child done'),
            ScriptedTurn(content='done'),
        ]
    )

    assert _run(tmp_path, llm, agent=None, topology='lead', config=_LEAD_WITH_BASH) == 'done'

    (log,) = logs
    snapshot = log.snapshot()
    orchestrator, worker = snapshot.agents
    assert (orchestrator.role, orchestrator.parent_id) == ('Orchestrator', None)
    assert (worker.role, worker.parent_id) == ('Worker', orchestrator.agent_id)
    by_command = {entry.target: entry for entry in snapshot.tools}
    assert (by_command['echo parent'].agent_id, by_command['echo parent'].role) == (
        orchestrator.agent_id,
        'Orchestrator',
    )
    assert (by_command['echo child'].agent_id, by_command['echo child'].role) == (
        worker.agent_id,
        'Worker',
    )
    assert orchestrator.seq < by_command['echo parent'].seq
    assert worker.seq < by_command['echo child'].seq
    assert [run.stop_reason for run in snapshot.runs] == ['completed', 'completed']


def test_the_real_write_and_edit_tools_are_recorded_only_when_they_succeed(
    tmp_path: Path, logs: list[DevLog]
):
    root = tmp_path / 'ws'
    root.mkdir()
    (root / 'keep.py').write_text('KEEP = 1\n', encoding='utf-8')
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('w', 'Write', path='new.py', content='x\n')]),
            ScriptedTurn(
                tool_calls=[
                    _call('e', 'Edit', path='keep.py', old_string='KEEP = 1', new_string='KEEP = 2')
                ]
            ),
            ScriptedTurn(
                tool_calls=[_call('f', 'Edit', path='keep.py', old_string='absent', new_string='y')]
            ),
            ScriptedTurn(content='done'),
        ]
    )

    _run(tmp_path, llm, root=root)

    (log,) = logs
    assert [(entry.tool, entry.target) for entry in log.snapshot().tools] == [
        ('Write', 'new.py'),
        ('Edit', 'keep.py'),
    ]


def test_a_real_background_command_polled_several_times_is_recorded_once(
    tmp_path: Path, logs: list[DevLog], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        local_workspace, 'uuid', SimpleNamespace(uuid4=lambda: SimpleNamespace(hex='bg1'))
    )
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[_call('s', 'Bash', command='echo 4 passed', run_in_background=True)]
            ),
            ScriptedTurn(tool_calls=[_call('w', 'Bash', command='sleep 0.5')]),
            ScriptedTurn(tool_calls=[_call('o1', 'BashOutput', handle='bg1')]),
            ScriptedTurn(tool_calls=[_call('o2', 'BashOutput', handle='bg1')]),
            ScriptedTurn(content='done'),
        ]
    )

    _run(tmp_path, llm, agent='Runner', config=_role(tmp_path, 'Runner', 'Bash, BashOutput'))

    (log,) = logs
    finished = [entry for entry in log.snapshot().tools if entry.tool == 'BashOutput']
    assert [(entry.target, entry.exit_code) for entry in finished] == [('echo 4 passed', 0)]


def _git(root: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_the_reviewer_fingerprints_the_worktree_when_it_starts_and_ends(
    tmp_path: Path, logs: list[DevLog]
):
    root = tmp_path / 'ws'
    (root / 'src').mkdir(parents=True)
    (root / 'src' / 'keep.txt').write_text('keep\n', encoding='utf-8')
    env = _host_env(tmp_path)
    _git(root, env, 'init', '-q')
    _git(root, env, 'add', '.')
    _git(root, env, 'commit', '-q', '-m', 'init')
    (root / 'src' / 'keep.txt').write_text('changed before the review\n', encoding='utf-8')
    config = _role(tmp_path, 'Reviewer', 'Bash')
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('b', 'Bash', command='echo new > made.txt')]),
            ScriptedTurn(content='verdict: pass'),
        ]
    )

    _run(tmp_path, llm, agent='Reviewer', config=config, root=root)

    (log,) = logs
    (run,) = log.snapshot().runs
    assert (run.role, run.stop_reason, run.final_text) == ('Reviewer', 'completed', 'verdict: pass')
    assert run.start is not None and run.start.changes is not None
    assert run.end is not None and run.end.changes is not None
    assert [(c.path, c.status) for c in run.start.changes] == [('src/keep.txt', ' M')]
    assert [(c.path, c.status) for c in run.end.changes] == [
        ('made.txt', '??'),
        ('src/keep.txt', ' M'),
    ]
    blobs = _git(root, env, 'hash-object', 'made.txt', 'src/keep.txt').split()
    assert [c.blob for c in run.end.changes] == blobs


def test_the_reviewer_fingerprint_uses_the_hosts_own_git_configuration(
    tmp_path: Path, logs: list[DevLog]
):
    root = tmp_path / 'ws'
    root.mkdir()
    (root / 'keep.txt').write_text('keep\n', encoding='utf-8')
    excludes = tmp_path / 'host-excludes'
    excludes.write_text('made.txt\n', encoding='utf-8')
    global_config = tmp_path / 'host-gitconfig'
    global_config.write_text(f'[core]\n\texcludesFile = {excludes}\n', encoding='utf-8')
    env = {**_host_env(tmp_path), 'GIT_CONFIG_GLOBAL': str(global_config)}
    _git(root, env, 'init', '-q')
    _git(root, env, 'add', '.')
    _git(root, env, 'commit', '-q', '-m', 'init')
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('b', 'Bash', command='echo new > made.txt')]),
            ScriptedTurn(content='verdict: pass'),
        ]
    )

    _run(
        tmp_path,
        llm,
        agent='Reviewer',
        config=_role(tmp_path, 'Reviewer', 'Bash'),
        root=root,
        environ=env,
    )

    (log,) = logs
    (run,) = log.snapshot().runs
    assert run.end is not None and run.end.changes == ()
