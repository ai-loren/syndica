"""Declarative L2 scenarios drive the real kernel with only the LLM scripted."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from syndica.kernel import ConfigError
from syndica.runner import ScenarioMismatch, ScenarioRunner, load_scenario

FIXTURES = Path(__file__).parent / 'fixtures'


async def test_delegated_write_scenario_runs_real_runtime_tools_and_workspace(
    tmp_path: Path,
) -> None:
    scenario = load_scenario(FIXTURES / 'delegated_write.yaml')
    result = await ScenarioRunner().run(scenario, workspace_root=tmp_path)

    assert result.name == 'delegated-write'
    assert result.final_output == {
        'Coordinator[0]': 'coordination complete',
        'Worker[0]': 'worker complete',
    }
    assert result.tool_order == {
        'Coordinator[0]': ('Delegate',),
        'Worker[0]': ('Write', 'Glob', 'Read', 'Grep'),
    }
    assert result.stop_reasons == {
        'Coordinator[0]': 'completed',
        'Worker[0]': 'completed',
    }
    assert result.statuses == {
        'Coordinator[0]': 'zombie',
        'Worker[0]': 'zombie',
    }
    assert result.workspace == {'result.txt': 'scenario-ok'}
    assert [event.sequence for event in result.events] == list(range(1, len(result.events) + 1))
    assert result.events[0].agent == 'Coordinator[0]'
    assert result.events[-1].kind == 'agent_end'


def test_scenario_loader_preserves_tool_call_and_mapping_order() -> None:
    scenario = load_scenario(FIXTURES / 'delegated_write.yaml')
    coordinator = scenario.agents['Coordinator'].instances[0]
    call = coordinator[0].tool_calls[0]
    assert call.id == 'delegate-1'
    assert call.name == 'Delegate'
    assert call.arguments == '{"agent":"Worker","query":"Write the requested file."}'
    assert list(scenario.agents) == ['Coordinator', 'Worker']
    assert [slot.name for slot in scenario.topology.slots] == ['root']


@pytest.mark.parametrize(
    ('text', 'needle'),
    [
        ('', 'Mapping'),
        ('version: true\nname: bad\n', 'version'),
        ('version: 2\nname: bad\n', 'version'),
        ('version: 1\nname: bad\nextra: value\n', 'extra'),
        ('version: 1\nname: bad\nagents: []\n', 'agents'),
        (
            """
version: 1
name: bad
agents: {}
topology: {slots: []}
entry: {slot: root, prompt: go}
responses: []
expect: {}
---
version: 1
""",
            '单文档',
        ),
    ],
)
def test_scenario_rejects_invalid_root_shapes(
    tmp_path: Path,
    text: str,
    needle: str,
) -> None:
    path = tmp_path / 'bad.yaml'
    path.write_text(text, encoding='utf-8')
    with pytest.raises(ConfigError) as excinfo:
        load_scenario(path)
    assert needle in str(excinfo.value)
    assert excinfo.value.expected
    assert excinfo.value.received
    assert excinfo.value.next_action


def test_scenario_rejects_unsafe_yaml_tags(tmp_path: Path) -> None:
    path = tmp_path / 'unsafe.yaml'
    path.write_text(
        """
!!python/object/apply:builtins.dict
  - - [version, 1]
""",
        encoding='utf-8',
    )
    with pytest.raises(ConfigError) as excinfo:
        load_scenario(path)
    assert 'safe' in excinfo.value.next_action


def test_scenario_rejects_duplicate_tool_call_ids(tmp_path: Path) -> None:
    path = tmp_path / 'duplicate-call.yaml'
    path.write_text(
        """
version: 1
name: duplicate-call
agents:
  Root:
    manifest: {name: Root, description: root}
    instances:
      - turns:
          - tool_calls:
              - {id: same, name: AskUser, arguments: {question: one}}
              - {id: same, name: AskUser, arguments: {question: two}}
topology:
  slots:
    - {name: root, agent: Root, is_root: true, cardinality: singleton, boot: eager}
entry: {slot: root, prompt: go}
responses: []
expect: {}
""",
        encoding='utf-8',
    )
    with pytest.raises(ConfigError) as excinfo:
        load_scenario(path)
    assert 'same' in excinfo.value.received
    assert '重复' in str(excinfo.value)


def test_scenario_rejects_arbitrary_exception_imports(tmp_path: Path) -> None:
    path = tmp_path / 'unsafe-error.yaml'
    path.write_text(
        """
version: 1
name: unsafe-error
agents:
  Root:
    manifest: {name: Root, description: root}
    instances:
      - turns:
          - raises: {kind: "os:system", message: nope}
topology:
  slots:
    - {name: root, agent: Root, is_root: true, cardinality: singleton, boot: eager}
entry: {slot: root, prompt: go}
responses: []
expect: {}
""",
        encoding='utf-8',
    )
    with pytest.raises(ConfigError) as excinfo:
        load_scenario(path)
    assert 'os:system' in excinfo.value.received
    assert 'runtime_error' in excinfo.value.next_action


async def test_scenario_replays_a_human_response_without_storing_dynamic_request_ids(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'human.yaml'
    path.write_text(
        """
version: 1
name: human
agents:
  Root:
    manifest: {name: Root, description: root}
    instances:
      - turns:
          - tool_calls:
              - id: ask-1
                name: AskUser
                arguments: {question: "Continue?", timeout: 30}
          - {content: continued}
topology:
  slots:
    - {name: root, agent: Root, is_root: true, cardinality: singleton, boot: eager}
entry: {slot: root, prompt: go}
responses:
  - {kind: user_input, agent: "Root[0]", answer: continue}
expect:
  final_output: {"Root[0]": continued}
  tool_order: {"Root[0]": [AskUser]}
  stop_reasons: {"Root[0]": completed}
  statuses: {"Root[0]": zombie}
""",
        encoding='utf-8',
    )
    result = await ScenarioRunner().run(
        load_scenario(path),
        workspace_root=tmp_path / 'workspace',
    )
    requests = [event for event in result.events if event.kind == 'user_input_requested']
    assert len(requests) == 1
    assert requests[0].payload['question'] == 'Continue?'
    assert requests[0].payload['request_id']


async def test_scenario_rejects_an_unconsumed_response(tmp_path: Path) -> None:
    path = tmp_path / 'extra-response.yaml'
    path.write_text(
        """
version: 1
name: extra-response
agents:
  Root:
    manifest: {name: Root, description: root}
    instances:
      - turns: [{content: done}]
topology:
  slots:
    - {name: root, agent: Root, is_root: true, cardinality: singleton, boot: eager}
entry: {slot: root, prompt: go}
responses:
  - {kind: user_input, agent: "Root[0]", answer: unused}
expect: {}
""",
        encoding='utf-8',
    )
    with pytest.raises(ScenarioMismatch) as excinfo:
        await ScenarioRunner().run(
            load_scenario(path),
            workspace_root=tmp_path / 'workspace',
        )
    assert '1' in excinfo.value.received
    assert 'AskUser' in excinfo.value.next_action


async def test_two_instances_of_one_role_consume_two_independent_scripts(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'two-workers.yaml'
    path.write_text(
        """
version: 1
name: two-workers
agents:
  Coordinator:
    manifest: {name: Coordinator, description: root}
    instances:
      - turns:
          - tool_calls:
              - {id: d1, name: Delegate, arguments: {agent: Worker, query: first}}
          - tool_calls:
              - {id: d2, name: Delegate, arguments: {agent: Worker, query: second}}
          - {content: all done}
  Worker:
    manifest: {name: Worker, description: worker}
    instances:
      - turns: [{content: first result}]
      - turns: [{content: second result}]
topology:
  slots:
    - {name: root, agent: Coordinator, is_root: true, cardinality: singleton, boot: eager}
entry: {slot: root, prompt: go}
responses: []
expect:
  final_output:
    "Coordinator[0]": all done
    "Worker[0]": first result
    "Worker[1]": second result
  stop_reasons:
    "Coordinator[0]": completed
    "Worker[0]": completed
    "Worker[1]": completed
  statuses:
    "Coordinator[0]": zombie
    "Worker[0]": zombie
    "Worker[1]": zombie
""",
        encoding='utf-8',
    )
    result = await ScenarioRunner().run(
        load_scenario(path),
        workspace_root=tmp_path / 'workspace',
    )
    assert result.final_output['Worker[0]'] == 'first result'
    assert result.final_output['Worker[1]'] == 'second result'


async def test_wrong_expectation_raises_a_three_part_scenario_mismatch(tmp_path: Path) -> None:
    text = (FIXTURES / 'delegated_write.yaml').read_text(encoding='utf-8')
    path = tmp_path / 'wrong.yaml'
    path.write_text(text.replace('coordination complete', 'wrong', 1), encoding='utf-8')
    scenario = load_scenario(path)
    with pytest.raises(ScenarioMismatch) as excinfo:
        await ScenarioRunner().run(scenario, workspace_root=tmp_path / 'workspace')
    assert 'final_output' in excinfo.value.expected
    assert 'wrong' in excinfo.value.received
    assert 'expect' in excinfo.value.next_action


async def test_scripted_runtime_error_is_replayed_as_an_error_stop_reason(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'runtime-error.yaml'
    path.write_text(
        """
version: 1
name: runtime-error
agents:
  Root:
    manifest: {name: Root, description: root}
    instances:
      - turns:
          - raises: {kind: runtime_error, message: injected failure}
topology:
  slots:
    - {name: root, agent: Root, is_root: true, cardinality: singleton, boot: eager}
entry: {slot: root, prompt: go}
responses: []
expect:
  stop_reasons: {"Root[0]": error}
  statuses: {"Root[0]": zombie}
""",
        encoding='utf-8',
    )
    result = await ScenarioRunner().run(
        load_scenario(path),
        workspace_root=tmp_path / 'workspace',
    )
    assert result.stop_reasons == {'Root[0]': 'error'}
    assert [event.kind for event in result.events][-1] == 'agent_error'


async def test_role_instance_script_exhaustion_is_visible_and_cleanup_completes(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'exhausted.yaml'
    path.write_text(
        """
version: 1
name: exhausted
agents:
  Coordinator:
    manifest: {name: Coordinator, description: root}
    instances:
      - turns:
          - tool_calls:
              - {id: d1, name: Delegate, arguments: {agent: Worker, query: first}}
          - tool_calls:
              - {id: d2, name: Delegate, arguments: {agent: Worker, query: second}}
  Worker:
    manifest: {name: Worker, description: worker}
    instances:
      - turns: [{content: first done}]
topology:
  slots:
    - {name: root, agent: Coordinator, is_root: true, cardinality: singleton, boot: eager}
entry: {slot: root, prompt: go}
responses: []
expect:
  stop_reasons: {"Coordinator[0]": completed}
""",
        encoding='utf-8',
    )
    scenario = load_scenario(path)
    with pytest.raises(ScenarioMismatch) as excinfo:
        await ScenarioRunner().run(scenario, workspace_root=tmp_path / 'workspace')
    assert 'Coordinator[0]' in excinfo.value.received
    assert 'error' in excinfo.value.received
    assert [
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and task.get_name().startswith('agent:')
    ] == []


async def test_explicit_empty_expectation_is_enforced(tmp_path: Path) -> None:
    path = tmp_path / 'empty-expectation.yaml'
    path.write_text(
        """
version: 1
name: empty-expectation
agents:
  Root:
    manifest: {name: Root, description: root}
    instances:
      - turns: [{content: done}]
topology:
  slots:
    - {name: root, agent: Root, is_root: true, cardinality: singleton, boot: eager}
entry: {slot: root, prompt: go}
responses: []
expect:
  final_output: {}
""",
        encoding='utf-8',
    )

    with pytest.raises(ScenarioMismatch) as excinfo:
        await ScenarioRunner().run(
            load_scenario(path),
            workspace_root=tmp_path / 'workspace',
        )

    assert 'final_output' in excinfo.value.expected
    assert "'Root[0]': 'done'" in excinfo.value.received
    assert 'expect' in excinfo.value.next_action


def test_confirmation_response_requires_the_original_tool_call_id(tmp_path: Path) -> None:
    path = tmp_path / 'confirmation-without-call-id.yaml'
    path.write_text(
        """
version: 1
name: confirmation-without-call-id
agents:
  Root:
    manifest: {name: Root, description: root}
    instances:
      - turns: [{content: done}]
topology:
  slots:
    - {name: root, agent: Root, is_root: true, cardinality: singleton, boot: eager}
entry: {slot: root, prompt: go}
responses:
  - {kind: confirmation, agent: "Root[0]", answer: approve}
expect: {}
""",
        encoding='utf-8',
    )

    with pytest.raises(ConfigError) as excinfo:
        load_scenario(path)

    assert 'tool_call_id' in excinfo.value.expected
    assert 'confirmation' in excinfo.value.received
    assert 'tool_call_id' in excinfo.value.next_action


async def test_non_default_scenario_models_do_not_require_unused_builtin_models(
    tmp_path: Path,
) -> None:
    text = (FIXTURES / 'delegated_write.yaml').read_text(encoding='utf-8')
    text = text.replace(
        'description: Coordinates one worker.',
        'description: Coordinates one worker.\n      model: scenario-model',
    ).replace(
        'description: Writes files.',
        'description: Writes files.\n      model: scenario-model',
    )
    path = tmp_path / 'non-default-model.yaml'
    path.write_text(text, encoding='utf-8')

    result = await ScenarioRunner().run(
        load_scenario(path),
        workspace_root=tmp_path / 'workspace',
    )

    assert result.final_output['Coordinator[0]'] == 'coordination complete'
    assert result.final_output['Worker[0]'] == 'worker complete'


async def test_scenario_reports_event_buffer_overflow(tmp_path: Path) -> None:
    turns = '\n'.join(
        (
            '          - tool_calls:\n'
            f'              - {{id: missing-{index}, name: Missing, arguments: {{}}}}'
        )
        for index in range(600)
    )
    path = tmp_path / 'event-overflow.yaml'
    path.write_text(
        f"""
version: 1
name: event-overflow
agents:
  Root:
    manifest:
      name: Root
      description: root
      budget: {{turns: null}}
    instances:
      - turns:
{turns}
          - {{content: done}}
topology:
  slots:
    - {{name: root, agent: Root, is_root: true, cardinality: singleton, boot: eager}}
entry: {{slot: root, prompt: go}}
responses: []
expect: {{}}
""",
        encoding='utf-8',
    )

    with pytest.raises(ScenarioMismatch) as excinfo:
        await ScenarioRunner().run(
            load_scenario(path),
            workspace_root=tmp_path / 'workspace',
        )

    assert '不丢事件' in excinfo.value.expected
    assert 'dropped=' in excinfo.value.received
    assert 'EventBus' in excinfo.value.next_action


async def test_untrusted_high_risk_call_replays_confirmation_through_mailbox(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'confirmation.yaml'
    path.write_text(
        """
version: 1
name: confirmation
agents:
  Root:
    manifest: {name: Root, description: root, tools: [Bash]}
    instances:
      - turns:
          - tool_calls:
              - {id: taint-1, name: Bash, arguments: {command: "printf taint"}}
          - tool_calls:
              - {id: dangerous-1, name: Bash, arguments: {command: "rm -rf -- doomed.txt"}}
          - {content: approved}
topology:
  slots:
    - {name: root, agent: Root, is_root: true, cardinality: singleton, boot: eager}
entry: {slot: root, prompt: go}
responses:
  - kind: confirmation
    agent: "Root[0]"
    tool_call_id: dangerous-1
    answer: approve
expect:
  final_output: {"Root[0]": approved}
  tool_order: {"Root[0]": [Bash, Bash]}
  stop_reasons: {"Root[0]": completed}
  statuses: {"Root[0]": zombie}
""",
        encoding='utf-8',
    )

    result = await ScenarioRunner().run(
        load_scenario(path),
        workspace_root=tmp_path / 'workspace',
    )

    confirmations = [
        event for event in result.events if event.kind == 'tool_confirmation_requested'
    ]
    assert len(confirmations) == 1
    assert confirmations[0].payload['tool_call_id'] == 'dangerous-1'
    assert confirmations[0].payload['request_id'] != 'dangerous-1'


async def test_binary_workspace_file_raises_a_three_part_scenario_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'binary-workspace.yaml'
    path.write_text(
        """
version: 1
name: binary-workspace
agents:
  Root:
    manifest: {name: Root, description: root, tools: [Bash]}
    instances:
      - turns:
          - tool_calls:
              - {id: binary-1, name: Bash, arguments: {command: 'printf "\\377" > blob.bin'}}
          - {content: done}
topology:
  slots:
    - {name: root, agent: Root, is_root: true, cardinality: singleton, boot: eager}
entry: {slot: root, prompt: go}
responses: []
expect: {}
""",
        encoding='utf-8',
    )

    with pytest.raises(ScenarioMismatch) as excinfo:
        await ScenarioRunner().run(
            load_scenario(path),
            workspace_root=tmp_path / 'workspace',
        )

    assert 'UTF-8' in excinfo.value.expected
    assert 'blob.bin' in excinfo.value.received
    assert 'expect.workspace' in excinfo.value.next_action
