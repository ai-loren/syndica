"""The CLI host composes real kernel components behind the lightweight router."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, InspectRequest, RunRequest
from syndica.kernel import (
    ConfigError,
    ContractViolation,
    ScriptedLLM,
    ScriptedTurn,
    ToolCall,
)


def test_run_request_uses_the_real_runtime_with_an_injected_model(
    tmp_path: Path,
) -> None:
    llm = ScriptedLLM([ScriptedTurn(content='runtime completed')])
    io = CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO())
    host = RuntimeCliHost(
        io=io,
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(RunRequest(prompt='do the work', agent='Worker'))
    finally:
        host.close()

    assert output == 'runtime completed'
    assert llm.calls[0][1].role == 'user'
    assert llm.calls[0][1].content == 'do the work'


def test_run_request_answers_human_tools_through_the_runtime_mailbox(
    tmp_path: Path,
) -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='ask-1',
                        name='AskUser',
                        arguments='{"question":"Your name?","timeout":30}',
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='choose-1',
                        name='AskUserQuestion',
                        arguments=(
                            '{"question":"Pick one","options":["alpha","beta"],"timeout":30}'
                        ),
                    )
                ]
            ),
            ScriptedTurn(content='answers received'),
        ]
    )
    stdout = StringIO()
    host = RuntimeCliHost(
        io=CliIO(
            stdin=StringIO('Loren\nbeta\n'),
            stdout=stdout,
            stderr=StringIO(),
        ),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(RunRequest(prompt='ask me', agent='Worker'))
    finally:
        host.close()

    assert output == 'answers received'
    assert 'Your name?' in stdout.getvalue()
    assert 'Pick one' in stdout.getvalue()
    assert 'alpha' in stdout.getvalue()
    assert 'beta' in stdout.getvalue()


def test_run_request_can_approve_one_dangerous_tool_call(
    tmp_path: Path,
) -> None:
    doomed = tmp_path / 'doomed.txt'
    doomed.write_text('remove me', encoding='utf-8')
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='taint-1',
                        name='Bash',
                        arguments='{"command":"printf taint"}',
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='dangerous-1',
                        name='Bash',
                        arguments='{"command":"rm -rf -- doomed.txt"}',
                    )
                ]
            ),
            ScriptedTurn(content='removed'),
        ]
    )
    stdout = StringIO()
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO('approve\n'), stdout=stdout, stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(
            RunRequest(
                prompt='remove it',
                agent='Worker',
                flags=('untrusted_high_risk=ask',),
            )
        )
    finally:
        host.close()

    assert output == 'removed'
    assert doomed.exists() is False
    assert 'dangerous-1' in stdout.getvalue()
    assert 'rm -rf -- doomed.txt' in stdout.getvalue()


def test_named_topology_runs_its_root_with_the_declared_model(tmp_path: Path) -> None:
    config = tmp_path / 'team.yaml'
    config.write_text(
        """
version: 1
agents:
  - {name: Specialist, description: custom, model: specialist-model}
topologies:
  review:
    slots:
      - {name: root, agent: Specialist, is_root: true, cardinality: singleton, boot: eager}
""",
        encoding='utf-8',
    )
    requested_models: list[str] = []

    def model_factory(name: str) -> ScriptedLLM:
        requested_models.append(name)
        content = 'topology completed' if name == 'specialist-model' else 'wrong model'
        return ScriptedLLM([ScriptedTurn(content=content)])

    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=model_factory,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(
            RunRequest(prompt='review', config=str(config), topology='review')
        )
    finally:
        host.close()

    assert output == 'topology completed'
    assert requested_models == ['default', 'specialist-model']


@pytest.mark.parametrize(
    ('changes', 'needle'),
    [
        ({'session': 'old-session'}, 'SessionStore'),
        ({'max_cost': 1.0}, 'max_cost'),
        ({'max_tokens': 100}, 'max_tokens'),
        ({'max_wall_clock': 10.0}, 'max_wall_clock'),
        ({'flags': ('missing=1',)}, 'missing'),
        ({'flags': ('max_wait_seconds=nope',)}, 'int'),
        ({'flags': ('untrusted_high_risk=permit',)}, 'permit'),
        ({'flags': ('max_wait_seconds=1', 'max_wait_seconds=2')}, '重复'),
    ],
)
def test_unsupported_or_invalid_run_options_fail_before_model_creation(
    tmp_path: Path,
    changes: dict[str, object],
    needle: str,
) -> None:
    calls: list[str] = []
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda name: calls.append(name) or ScriptedLLM([]),
        workspace_root=tmp_path,
    )
    request = RunRequest(prompt='go')
    request = RunRequest(
        **{
            **request.__dict__,
            **changes,
        }
    )
    try:
        with pytest.raises(ConfigError) as excinfo:
            host.run_request(request)
    finally:
        host.close()

    assert needle in str(excinfo.value)
    assert excinfo.value.expected
    assert excinfo.value.received
    assert excinfo.value.next_action
    assert calls == []


def test_inspect_reads_agents_topologies_and_flags_from_their_sources(
    tmp_path: Path,
) -> None:
    config = tmp_path / 'team.yaml'
    config.write_text(
        """
version: 1
agents:
  - {name: Specialist, description: custom role, tools: [Read]}
topologies:
  review:
    slots:
      - {name: root, agent: Specialist, is_root: true}
""",
        encoding='utf-8',
    )
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: ScriptedLLM([]),
        workspace_root=tmp_path,
    )
    try:
        agents = host.inspect(
            InspectRequest(resource='agents', action='list', name=None, config=str(config))
        )
        agent = host.inspect(
            InspectRequest(
                resource='agents',
                action='show',
                name='Specialist',
                config=str(config),
            )
        )
        topologies = host.inspect(
            InspectRequest(resource='topology', action='list', name=None, config=str(config))
        )
        topology = host.inspect(
            InspectRequest(
                resource='topology',
                action='show',
                name='review',
                config=str(config),
            )
        )
        flags = host.inspect(
            InspectRequest(resource='flags', action='list', name=None, config=str(config))
        )
        flag = host.inspect(
            InspectRequest(
                resource='flags',
                action='show',
                name='untrusted_high_risk',
                config=str(config),
            )
        )
    finally:
        host.close()

    assert agents.splitlines() == ['Planner', 'Explorer', 'Worker', 'Orchestrator', 'Specialist']
    assert json.loads(agent)['description'] == 'custom role'
    assert json.loads(agent)['tools'] == ['Read']
    assert topologies == 'review'
    assert json.loads(topology)['slots'][0]['agent'] == 'Specialist'
    assert flags.splitlines() == ['max_wait_seconds', 'untrusted_high_risk']
    assert json.loads(flag) == {
        'key': 'untrusted_high_risk',
        'default': 'reject',
        'description': '高风险调用可溯源到不可信内容时的处置',
        'choices': ['reject', 'ask', 'allow'],
        'minimum': None,
        'maximum': None,
    }


def test_doctor_validates_config_models_and_workspace(tmp_path: Path) -> None:
    requested_models: list[str] = []
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda name: requested_models.append(name) or ScriptedLLM([]),
        workspace_root=tmp_path,
    )
    try:
        result = host.doctor('.syndica.yaml')
    finally:
        host.close()

    assert requested_models == ['default']
    assert f'workspace: ok {tmp_path}' in result
    assert 'config: ok .syndica.yaml' in result
    assert 'models: ok default' in result


def test_chat_session_reuses_one_agent_and_lists_the_current_target(
    tmp_path: Path,
) -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(content='first answer'),
            ScriptedTurn(content='second answer'),
        ]
    )
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        target = host.open_chat('.syndica.yaml')
        first = host.run_request(RunRequest(prompt='first', session=target))
        second = host.run_request(RunRequest(prompt='second', session=target))
        active = host.active_agents()
    finally:
        host.close()

    assert first == 'first answer'
    assert second == 'second answer'
    assert target in active
    assert 'Orchestrator' in active
    assert len(llm.calls) == 2
    assert [block.content for block in llm.calls[1] if block.role == 'user'] == [
        'first',
        'second',
    ]


def test_eof_aborts_a_pending_human_question(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='ask-1',
                        name='AskUser',
                        arguments='{"question":"Need input","timeout":30}',
                    )
                ]
            )
        ]
    )
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(RunRequest(prompt='ask', agent='Worker'))
    finally:
        host.close()

    assert output == ''


def test_eof_rejects_confirmation_without_executing_the_tool(tmp_path: Path) -> None:
    doomed = tmp_path / 'doomed.txt'
    doomed.write_text('keep me', encoding='utf-8')
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='taint-1',
                        name='Bash',
                        arguments='{"command":"printf taint"}',
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='dangerous-1',
                        name='Bash',
                        arguments='{"command":"rm -rf -- doomed.txt"}',
                    )
                ]
            ),
            ScriptedTurn(content='rejected'),
        ]
    )
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(
            RunRequest(
                prompt='remove it',
                agent='Worker',
                flags=('untrusted_high_risk=ask',),
            )
        )
    finally:
        host.close()

    assert output == 'rejected'
    assert doomed.read_text(encoding='utf-8') == 'keep me'


def test_max_turns_limits_the_selected_agent(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                content='partial',
                tool_calls=[ToolCall(id='missing-1', name='Missing', arguments='{}')],
            ),
            ScriptedTurn(content='must not run'),
        ]
    )
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(RunRequest(prompt='one turn only', agent='Worker', max_turns=1))
    finally:
        host.close()

    assert output == 'partial'
    assert len(llm.calls) == 1


def test_agent_error_is_reported_as_a_three_part_cli_failure(tmp_path: Path) -> None:
    llm = ScriptedLLM([ScriptedTurn(raises=RuntimeError('injected failure'))])
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(ContractViolation) as excinfo:
            host.run_request(RunRequest(prompt='fail', agent='Worker'))
    finally:
        host.close()

    assert 'completed' in excinfo.value.expected
    assert 'error' in excinfo.value.received
    assert '日志' in excinfo.value.next_action


def test_running_a_topology_without_a_root_fails_with_actionable_guidance(
    tmp_path: Path,
) -> None:
    config = tmp_path / 'rootless.yaml'
    config.write_text(
        """
version: 1
topologies:
  rootless:
    slots:
      - {name: worker, agent: Worker, cardinality: singleton}
""",
        encoding='utf-8',
    )
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: ScriptedLLM([]),
        workspace_root=tmp_path,
    )
    try:
        with pytest.raises(ConfigError) as excinfo:
            host.run_request(RunRequest(prompt='go', config=str(config), topology='rootless'))
    finally:
        host.close()

    assert 'root' in excinfo.value.expected
    assert 'is_root' in excinfo.value.received
    assert 'is_root: true' in excinfo.value.next_action


def test_chat_can_switch_from_the_root_to_a_delegated_agent(tmp_path: Path) -> None:
    config = tmp_path / 'chat.yaml'
    config.write_text(
        """
version: 1
agents:
  - {name: Worker, description: custom worker, model: worker-model}
""",
        encoding='utf-8',
    )
    root_llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(
                        id='delegate-1',
                        name='Delegate',
                        arguments='{"agent":"Worker","query":"first child task"}',
                    )
                ]
            ),
            ScriptedTurn(content='root done'),
        ]
    )
    worker_llm = ScriptedLLM(
        [
            ScriptedTurn(content='child first'),
            ScriptedTurn(content='child continued'),
        ]
    )
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda name: worker_llm if name == 'worker-model' else root_llm,
        workspace_root=tmp_path,
    )
    try:
        root_id = host.open_chat(str(config))
        root_output = host.run_request(
            RunRequest(prompt='delegate', config=str(config), session=root_id)
        )
        assert root_output == 'root done'
        active = host.active_agents().splitlines()
        worker_id = next(line.split()[0] for line in active if ' Worker ' in line)
        continued = host.run_request(
            RunRequest(prompt='continue directly', config=str(config), session=worker_id)
        )
    finally:
        host.close()

    assert continued == 'child continued'
    assert [block.content for block in worker_llm.calls[1] if block.role == 'user'] == [
        'first child task',
        'continue directly',
    ]


@pytest.mark.parametrize(
    'changes',
    [
        {'max_turns': 1},
        {'flags': ('max_wait_seconds=1',)},
    ],
)
def test_chat_session_rejects_options_that_cannot_be_changed_after_spawn(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: ScriptedLLM([ScriptedTurn(content='unused')]),
        workspace_root=tmp_path,
    )
    try:
        target = host.open_chat('.syndica.yaml')
        request = RunRequest(
            **{
                **RunRequest(prompt='go', session=target).__dict__,
                **changes,
            }
        )
        with pytest.raises(ConfigError) as excinfo:
            host.run_request(request)
    finally:
        host.close()

    assert 'spawn' in excinfo.value.expected
    assert '忽略' in excinfo.value.received
    assert '新' in excinfo.value.next_action


def test_agent_max_turns_cannot_loosen_the_manifest_budget(tmp_path: Path) -> None:
    config = tmp_path / 'budget.yaml'
    config.write_text(
        """
version: 1
agents:
  - {name: Worker, description: limited, budget: {turns: 1}}
""",
        encoding='utf-8',
    )
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                content='first only',
                tool_calls=[ToolCall(id='missing-1', name='Missing', arguments='{}')],
            ),
            ScriptedTurn(content='must not run'),
        ]
    )
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(
            RunRequest(
                prompt='respect the cap',
                config=str(config),
                agent='Worker',
                max_turns=2,
            )
        )
    finally:
        host.close()

    assert output == 'first only'
    assert len(llm.calls) == 1


def test_topology_max_turns_cannot_loosen_the_root_manifest_budget(
    tmp_path: Path,
) -> None:
    config = tmp_path / 'topology-budget.yaml'
    config.write_text(
        """
version: 1
agents:
  - {name: Specialist, description: limited, budget: {turns: 1}}
topologies:
  limited:
    slots:
      - {name: root, agent: Specialist, is_root: true, cardinality: singleton, boot: eager}
""",
        encoding='utf-8',
    )
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                content='first only',
                tool_calls=[ToolCall(id='missing-1', name='Missing', arguments='{}')],
            ),
            ScriptedTurn(content='must not run'),
        ]
    )
    host = RuntimeCliHost(
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
    )
    try:
        output = host.run_request(
            RunRequest(
                prompt='respect the cap',
                config=str(config),
                topology='limited',
                max_turns=2,
            )
        )
    finally:
        host.close()

    assert output == 'first only'
    assert len(llm.calls) == 1
