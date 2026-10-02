"""捕获即回放（M3 T14）：脚本化的 CLI run 捕获下来，回放出逐块相同的轨迹、逐项相同的闸门判定与
同样的结局；以错误结束的、被 Ctrl-C 打断的 run 也回放到同一处。"""

from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import string
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest

import syndica.cli.host as host_module
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, main
from syndica.kernel import (
    ContextBlock,
    FileTraceStore,
    LLMConfig,
    LLMResponseError,
    RuleRedactor,
    Runtime,
    SpanStatus,
    SyndicaError,
    ToolCall,
    TraceHeader,
    parse_scenario_document,
)
from syndica.kernel.tracing import current_llm_call
from syndica.runner.replay import ReplayResult, ReplayRunner

_WITHIN = 30.0

Step = ContextBlock | Callable[[Sequence[str]], ContextBlock] | BaseException


def _token(seed: int) -> str:
    rng = random.Random(seed)
    return 'ghp_' + ''.join(rng.choices(string.ascii_letters + string.digits, k=36))


def _calls(*calls: tuple[str, str, dict[str, object]]) -> ContextBlock:
    return ContextBlock(
        role='assistant',
        tool_calls=[
            ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))
            for call_id, name, arguments in calls
        ],
    )


def _say(text: str) -> ContextBlock:
    return ContextBlock(role='assistant', content=text)


class _PerAgent:
    """按 Agent 给回复：第一个来要回复的是根，第二个是它派出的子 Agent。一步可以是函数，按此刻已经
    知道的 Agent id 现写参数（子 Agent 的 id 在 run 之前不知道）。"""

    def __init__(self, *scripts: list[Step]) -> None:
        self.config = LLMConfig(provider='test', model='per-agent')
        self._scripts = scripts
        self._agents: list[str] = []
        self._next: dict[str, int] = {}

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        key = current_llm_call()
        assert key is not None
        agent_id = key[0]
        if agent_id not in self._agents:
            self._agents.append(agent_id)
        position = self._next.get(agent_id, 0)
        self._next[agent_id] = position + 1
        step = self._scripts[self._agents.index(agent_id)][position]
        if isinstance(step, BaseException):
            raise step
        return step if isinstance(step, ContextBlock) else step(self._agents)


class _Snapshotting(Runtime):
    """CLI 关掉 Runtime 的那一刻记下每个 Agent 的轨迹：原始 run 那一侧的比对对象。"""

    seen: list[dict[str, list[ContextBlock]]] = []

    async def shutdown(self) -> None:
        await super().shutdown()
        if getattr(self, '_snapshotted', False):
            return
        self._snapshotted = True
        trajectories: dict[str, list[ContextBlock]] = {}
        for info in await self.registry.list():
            try:
                trajectories[info.agent_id] = list((await self.wait(info.agent_id)).current_traj)
            except SyndicaError:
                continue
        _Snapshotting.seen.append(trajectories)


def _cli(tmp_path: Path, llm: Any, argv: list[str], *, stdin: str = '') -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(stdin), stdout=stdout, stderr=stderr)
    host = RuntimeCliHost(
        io=io,
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
        environ={key: value for key, value in os.environ.items() if not key.startswith('GIT_')},
    )
    try:
        code = main(argv, host=host, io=io)
    finally:
        host.close(interrupted=False)
    return code, stdout.getvalue(), stderr.getvalue()


def _trace(root: Path) -> tuple[TraceHeader, dict[str, Any]]:
    store = FileTraceStore(root / '.syndica' / 'traces')

    async def read() -> tuple[TraceHeader, str | None]:
        [header] = await store.traces()
        return header, await store.replay(header.trace_id)

    header, text = asyncio.run(read())
    assert text is not None, 'the trace has no replay document'
    return header, cast(dict[str, Any], json.loads(text))


def _replayed(header: TraceHeader, document: dict[str, Any]) -> ReplayResult:
    rules = RuleRedactor(rules=header.redaction.rules)
    return ReplayRunner(redactor=rules).run(parse_scenario_document(document, source='replay.json'))


def _selectors(document: dict[str, Any]) -> dict[str, str]:
    """agent id → 选择器（`角色[第几个]`），按回放文档里实例的顺序。"""
    ids = {tuple(item['key']): item['id'] for item in document['ids']}
    return {
        ids[tuple(instance['key'])]: f'{role}[{index}]'
        for role, body in document['agents'].items()
        for index, instance in enumerate(body['instances'])
    }


def _blocks(blocks: Sequence[ContextBlock], redactor: RuleRedactor) -> list[tuple[object, ...]]:
    """逐块比的那几项：`role`、`content`（原始 run 那一侧按同一套规则脱敏）、`trust`、工具调用按
    解析后的参数比；`extra` 不比。"""

    def text(value: object) -> object:
        return str(redactor.redact(value)) if isinstance(value, str) else value

    return [
        (
            block.role,
            text(block.content),
            block.trust.value,
            [(call.id, call.name, json.loads(call.arguments)) for call in block.tool_calls],
            block.tool_call_id,
        )
        for block in blocks
    ]


def _assert_same_trajectories(
    document: dict[str, Any], replayed: ReplayResult, original: dict[str, list[ContextBlock]]
) -> set[str]:
    selectors = _selectors(document)
    redactor = RuleRedactor()
    compared: set[str] = set()
    for agent_id, blocks in original.items():
        selector = selectors[agent_id]
        if selector not in replayed.trajectories:
            continue
        assert _blocks(replayed.trajectories[selector], redactor) == _blocks(blocks, redactor), (
            selector
        )
        compared.add(selector)
    return compared


@pytest.fixture
def snapshots(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, list[ContextBlock]]]:
    _Snapshotting.seen = []
    monkeypatch.setattr(host_module, 'Runtime', _Snapshotting)
    return _Snapshotting.seen


def test_a_run_with_every_kind_of_record_replays_block_for_block(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    token = _token(11)
    (tmp_path / 'notes.txt').write_text(f'deploy key: {token}\n', encoding='utf-8')
    root: list[Step] = [
        _calls(('c1', 'Bash', {'command': 'cat /etc/hosts'})),
        _calls(('q1', 'AskUser', {'question': 'colour?'})),
        _calls(('c2', 'Bash', {'command': 'git commit -m wip'})),
        _calls(('c3', 'Bash', {'command': 'sudo ls'})),
        _calls(('r1', 'Read', {'path': 'missing.txt'})),
        _calls(('r2', 'Read', {'path': 'notes.txt'})),
        _calls(('d1', 'Delegate', {'agent': 'Explorer', 'query': 'look around'})),
        lambda agents: _calls(('k1', 'KillAgent', {'agent_id': agents[1]})),
        _say('all done'),
    ]
    child: list[Step] = [
        lambda agents: _calls(
            ('s1', 'SendMessage', {'agent_id': agents[0], 'content': 'halfway'}),
        ),
        _say('child done'),
    ]

    code, stdout, stderr = _cli(
        tmp_path,
        _PerAgent(root, child),
        ['run', 'go', '--agent', 'Worker'],
        stdin='approve\nblue\n',
    )
    header, document = _trace(tmp_path)
    replayed = _replayed(header, document)

    assert code == 0 and stdout.rstrip().endswith('all done'), stderr
    assert replayed.reproduced, replayed.divergences
    [original] = snapshots
    assert 'Worker[0]' in _assert_same_trajectories(document, replayed, original)
    [lead] = document['agents']['Worker']['instances']
    dispositions = {gate['disposition'] for gate in lead['gate']}
    assert dispositions >= {'allow', 'confirm', 'block'}
    assert [item['call'][1] for item in lead['skipped']] == [3]
    assert {response['kind'] for response in document['responses']} == {
        'confirmation',
        'user_input',
    }
    assert len(document['agents']['Explorer']['instances']) == 1
    assert document['agents']['Orchestrator']['frozen']['impl'] == (
        'syndica.kernel.agents.orchestrator:Orchestrator'
    )
    assert document['agents']['Worker']['frozen']['impl'] is None
    assert token not in json.dumps(document)


def test_a_read_only_run_replays_with_its_refused_writes(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    (tmp_path / 'notes.txt').write_text('plain notes\n', encoding='utf-8')
    root: list[Step] = [
        _calls(('r1', 'Read', {'path': 'notes.txt'})),
        _calls(('w1', 'Write', {'path': 'out.txt', 'content': 'x'})),
        _calls(('q1', 'AskUser', {'question': 'ok?'})),
        _say('done'),
    ]

    code, _, stderr = _cli(
        tmp_path, _PerAgent(root), ['run', 'go', '--agent', 'Worker', '--no-write'], stdin='yes\n'
    )
    header, document = _trace(tmp_path)
    replayed = _replayed(header, document)

    assert code == 0, stderr
    assert not (tmp_path / 'out.txt').exists()
    assert document['host']['no_write'] is True
    assert replayed.reproduced, replayed.divergences
    [original] = snapshots
    assert 'Worker[0]' in _assert_same_trajectories(document, replayed, original)


def test_a_run_that_ends_in_an_error_replays_the_same_error(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    failure = LLMResponseError(expected='a reply', received='HTTP 503', next_action='retry later')
    root: list[Step] = [_calls(('r1', 'Read', {'path': 'missing.txt'})), failure]

    code, _, _ = _cli(tmp_path, _PerAgent(root), ['run', 'go', '--agent', 'Worker'])
    header, document = _trace(tmp_path)
    replayed = _replayed(header, document)

    assert code != 0
    assert header.status is SpanStatus.ERROR
    assert document['expect'] == {'stop_reasons': {'Worker[0]': 'error'}}
    assert replayed.reproduced, replayed.divergences
    assert replayed.result.stop_reasons == {'Worker[0]': 'error'}


def test_a_secret_in_what_a_tool_ran_with_replays_with_only_the_user_rules():
    from syndica.kernel import (
        AgentOptions,
        BaseAgent,
        EventBus,
        HooksCenter,
        InMemoryTraceStore,
        ScriptedLLM,
        ScriptedTurn,
        TraceRecorder,
        local_tools,
    )
    from syndica.kernel.local_workspace import LocalWorkspace

    secret = 'sk-' + ''.join(random.Random(5).choices(string.ascii_letters + string.digits, k=40))

    async def capture(root: Path) -> tuple[InMemoryTraceStore, str]:
        store = InMemoryTraceStore()
        recorder = TraceRecorder(store, RuleRedactor(secrets=[secret]))
        await recorder.begin()
        workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
        tools = local_tools(workspace.portal())
        llm = ScriptedLLM(
            [
                ScriptedTurn(
                    tool_calls=[
                        ToolCall(
                            id='c1',
                            name='Bash',
                            arguments=json.dumps({'command': f'echo {secret}'}),
                        )
                    ]
                ),
                ScriptedTurn(content='done'),
            ]
        )

        def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
            options = AgentOptions(
                name='Lead', llm=llm, tools={'Bash': tools['Bash']}, system_prompt='You lead.'
            )
            return BaseAgent(options, hooks=hooks, events=events)

        runtime = Runtime(
            agents={'Lead': build}, orchestration=True, tracer=recorder, ids=recorder.ids
        )
        recorder.describe_run(
            host={'modules': []},
            roles={'Lead': {'system_prompt': 'You lead.', 'tools': ['Bash']}},
            entry={'mode': 'query', 'agent': 'Lead', 'prompt': 'go'},
        )
        try:
            agent_id = await runtime.spawn(build, query='go')
            await asyncio.wait_for(runtime.wait(agent_id), timeout=_WITHIN)
        finally:
            await runtime.shutdown()
            await workspace.close()
        await recorder.finish(SpanStatus.OK)
        return store, recorder.trace_id

    store, trace_id = asyncio.run(capture(Path(tempfile.mkdtemp())))

    async def read() -> str | None:
        return await store.replay(trace_id)

    text = asyncio.run(read())
    assert text is not None and secret not in text
    replayed = ReplayRunner().run(parse_scenario_document(json.loads(text), source='replay.json'))
    assert replayed.reproduced, replayed.divergences


def _kernel_capture(
    scripts: list[list[Step]],
    *,
    role: str = 'Lead',
    hook: Callable[[Any], None] | None = None,
    interrupt_when: Callable[[], Any] | None = None,
) -> tuple[str, dict[str, list[ContextBlock]]]:
    """在内核里捕获一次 run（真的本地工具、开着编排、没有 Module），交回回放文档与原始轨迹。
    `interrupt_when` 是一个可等待的东西：等到它就像 Ctrl-C 那样打断这次 run。"""
    from syndica.kernel import (
        AgentOptions,
        BaseAgent,
        EventBus,
        HooksCenter,
        InMemoryTraceStore,
        TraceRecorder,
        local_tools,
    )
    from syndica.kernel.local_workspace import LocalWorkspace

    async def capture(root: Path) -> tuple[str, dict[str, list[ContextBlock]]]:
        store = InMemoryTraceStore()
        recorder = TraceRecorder(store, RuleRedactor())
        await recorder.begin()
        workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
        tools = local_tools(workspace.portal())
        llm = _PerAgent(*scripts)

        def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
            options = AgentOptions(
                name=role, llm=llm, tools={'Bash': tools['Bash']}, system_prompt='You lead.'
            )
            agent = BaseAgent(options, hooks=hooks, events=events)
            if hook is not None:
                hook(agent)
            return agent

        runtime = Runtime(
            agents={role: build}, orchestration=True, tracer=recorder, ids=recorder.ids
        )
        recorder.describe_run(
            host={'modules': []},
            roles={role: {'system_prompt': 'You lead.', 'tools': ['Bash']}},
            entry={'mode': 'query', 'agent': role, 'prompt': 'go'},
        )
        status = SpanStatus.OK
        try:
            agent_id = await runtime.spawn(build, query='go')
            if interrupt_when is None:
                await asyncio.wait_for(runtime.wait(agent_id), timeout=_WITHIN)
            else:
                await asyncio.wait_for(interrupt_when(), timeout=_WITHIN)
                recorder.interrupted()
                status = SpanStatus.CANCELLED
        finally:
            await runtime.shutdown()
            await workspace.close()
        trajectories = {
            info.agent_id: list((await runtime.wait(info.agent_id)).current_traj)
            for info in await runtime.registry.list()
        }
        await recorder.finish(status)
        text = await store.replay(recorder.trace_id)
        assert text is not None
        return text, trajectories

    return asyncio.run(capture(Path(tempfile.mkdtemp())))


def test_an_interrupt_after_a_tool_returned_but_before_its_result_landed_replays_there():
    landed = asyncio.Event()

    def slow_hook(agent: Any) -> None:
        from syndica.kernel import HookResult, Junction

        async def after(**_: Any) -> HookResult:
            landed.set()
            await asyncio.get_running_loop().create_future()
            return HookResult()

        agent.register_hook(Junction.POST_TOOL_USE, after)

    text, original = _kernel_capture(
        [[_calls(('c1', 'Bash', {'command': 'printf hi'})), _say('never')]],
        hook=slow_hook,
        interrupt_when=landed.wait,
    )
    document = json.loads(text)
    replayed = ReplayRunner().run(parse_scenario_document(document, source='replay.json'))

    agent_id = next(item['id'] for item in document['ids'] if item['key'] == ['host', 'spawn', 0])
    assert document['host_actions'] == [
        {'kind': 'interrupt', 'waiting': [['tool_call', agent_id, 1, 1, 0]]}
    ]
    assert replayed.reproduced, replayed.divergences
    assert 'Lead[0]' in _assert_same_trajectories(document, replayed, original)


def test_a_role_named_like_a_secret_keeps_the_provider_ids_under_it():
    provider_id = 'call_' + ''.join(
        random.Random(9).choices(string.ascii_letters + string.digits, k=24)
    )

    text, original = _kernel_capture(
        [[_calls((provider_id, 'Bash', {'command': 'printf hi'})), _say('done')]],
        role='session-notes',
    )
    document = json.loads(text)
    replayed = ReplayRunner().run(parse_scenario_document(document, source='replay.json'))

    assert provider_id in text
    assert replayed.reproduced, replayed.divergences
    assert 'session-notes[0]' in _assert_same_trajectories(document, replayed, original)


_TOPOLOGY = """version: 1
topologies:
  solo:
    slots:
      - name: root
        agent: Worker
        is_root: true
        cardinality: singleton
        boot: eager
"""


def _without_document(root: Path) -> bool:
    store = FileTraceStore(root / '.syndica' / 'traces')

    async def read() -> str | None:
        [header] = await store.traces()
        return await store.replay(header.trace_id)

    return asyncio.run(read()) is None


@pytest.mark.parametrize('asked', [True, False], ids=['replay-asked-for', 'default-level'])
def test_a_topology_run_writes_no_replay_document_and_says_so_only_when_asked(
    tmp_path: Path, asked: bool
):
    config = tmp_path / 'syndica.yaml'
    config.write_text(_TOPOLOGY, encoding='utf-8')
    trace = ['--trace', 'replay'] if asked else []

    code, _, stderr = _cli(
        tmp_path,
        _PerAgent([_say('done')]),
        ['--config', str(config), 'run', 'go', '--topology', 'solo', *trace],
    )

    assert code == 0, stderr
    assert _without_document(tmp_path)
    assert ('trace 里不写回放文档：--topology 的 run 还不能捕获' in stderr) is asked


@pytest.mark.parametrize('asked', [True, False], ids=['replay-asked-for', 'default-level'])
def test_a_custom_class_based_role_writes_no_replay_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asked: bool
):
    real = host_module.frozen_roles

    def with_a_custom_role(catalog: Any, factories: Any) -> dict[str, dict[str, object]]:
        roles = real(catalog, factories)
        roles['Worker'] = {**roles['Worker'], 'impl': 'acme.roles:Custom'}
        return roles

    monkeypatch.setattr(host_module, 'frozen_roles', with_a_custom_role)
    trace = ['--trace', 'replay'] if asked else []

    code, _, stderr = _cli(
        tmp_path, _PerAgent([_say('done')]), ['run', 'go', '--agent', 'Worker', *trace]
    )

    assert code == 0, stderr
    assert _without_document(tmp_path)
    assert ('角色 Worker 是自定义的类式角色' in stderr) is asked


def test_a_role_that_cannot_be_built_costs_only_the_replay_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def broken(catalog: Any, factories: Any) -> dict[str, dict[str, object]]:
        raise RuntimeError('constructor needs a network')

    monkeypatch.setattr(host_module, 'frozen_roles', broken)

    code, stdout, stderr = _cli(
        tmp_path, _PerAgent([_say('done')]), ['run', 'go', '--agent', 'Worker']
    )

    assert code == 0 and stdout.rstrip().endswith('done'), stderr
    assert _without_document(tmp_path)
    assert 'constructor needs a network' in stderr


# --- Ctrl-C: a real SIGINT to a CLI in a child process ------------------------------------

_DRIVER = """
import json
import sys
from pathlib import Path

import syndica.__main__ as entry
from syndica.kernel import ScriptedLLM, ScriptedTurn, ToolCall

spec = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
llm = ScriptedLLM(
    [
        ScriptedTurn(
            content=turn.get('content'),
            tool_calls=[
                ToolCall(id=call['id'], name=call['name'], arguments=json.dumps(call['arguments']))
                for call in turn.get('tool_calls', [])
            ],
        )
        for turn in spec['turns']
    ]
)
entry._configured_model = lambda name: llm
sys.argv = ['syndica', *spec['argv']]
entry.entrypoint()
"""


def _drain(stream: Any, into: list[bytes]) -> None:
    for chunk in iter(lambda: stream.read1(4096), b''):
        into.append(chunk)


def test_a_run_interrupted_by_ctrl_c_replays_to_the_same_place(tmp_path: Path):
    root = tmp_path / 'work'
    root.mkdir()
    waiting = 'echo $$ > cut.pid && touch cut.ready && exec sleep 60'
    turns = [
        {'tool_calls': [{'id': 'r1', 'name': 'Read', 'arguments': {'path': 'missing.txt'}}]},
        {'tool_calls': [{'id': 'b1', 'name': 'Bash', 'arguments': {'command': waiting}}]},
    ]
    (tmp_path / 'driver.py').write_text(_DRIVER, encoding='utf-8')
    (tmp_path / 'spec.json').write_text(
        json.dumps({'argv': ['run', 'go', '--agent', 'Worker'], 'turns': turns}), encoding='utf-8'
    )
    environ = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    process = subprocess.Popen(
        [sys.executable, str(tmp_path / 'driver.py'), str(tmp_path / 'spec.json')],
        cwd=root,
        env={**environ, 'HOME': str(tmp_path)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output: list[bytes] = []
    readers = [
        threading.Thread(target=_drain, args=(process.stdout, output)),
        threading.Thread(target=_drain, args=(process.stderr, output)),
    ]
    for reader in readers:
        reader.start()
    try:
        deadline = time.monotonic() + _WITHIN
        while not (root / 'cut.ready').exists():
            assert process.poll() is None, b''.join(output).decode('utf-8', 'replace')
            assert time.monotonic() < deadline, 'the agent command never started'
            time.sleep(0.05)
        process.send_signal(signal.SIGINT)
        code = process.wait(timeout=_WITHIN)
    finally:
        if process.poll() is None:
            process.kill()
        assert process.stdin is not None
        process.stdin.close()
        for reader in readers:
            reader.join(_WITHIN)

    header, document = _trace(root)
    replayed = _replayed(header, document)

    assert code == 130, b''.join(output).decode('utf-8', 'replace')
    assert header.status is SpanStatus.CANCELLED
    agent_id = next(item['id'] for item in document['ids'] if item['key'] == ['host', 'spawn', 0])
    assert document['host_actions'] == [
        {'kind': 'interrupt', 'waiting': [['tool_call', agent_id, 1, 2, 0]]}
    ]
    assert replayed.reproduced, replayed.divergences
