"""按角色的危险处置（spec §5.6，M4 D12）：列出的角色用自己的处置，其余用缺省处置；回放文档 v3 逐个
角色记下。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from syndica.kernel.agent import BaseAgent
from syndica.kernel.catalog import AgentCatalog
from syndica.kernel.context import ToolCall
from syndica.kernel.danger import (
    AllowAll,
    BlockHighRisk,
    DangerPolicy,
    Disposition,
    RuleTablePolicy,
)
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import LLM, ScriptedLLM, ScriptedTurn
from syndica.kernel.replay_capture import (
    _document_version,  # pyright: ignore[reportPrivateUsage]
)
from syndica.kernel.runtime import Runtime
from syndica.kernel.scenario_schema import DangerPolicySpec, parse_scenario_document
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.topology import AgentSlot, Topology


class _Shell(Tool):
    __tool_command_args__ = ('command',)

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def execute(self, command: str) -> ToolResult:
        self.ran.append(command)
        return ToolResult(output=f'ran {command}')


def _catalog(*names: str) -> AgentCatalog:
    catalog = AgentCatalog()
    for name in names:
        catalog.load_mapping({'name': name, 'description': name, 'tools': ['Shell']})
    return catalog


def _models() -> dict[str, LLM]:
    return {'default': ScriptedLLM([ScriptedTurn(content='ok')])}


def _policy_of(factories: Any, name: str) -> object:
    return factories[name](HooksCenter(), EventBus()).options.danger_policy


def test_listed_roles_get_their_own_policy_and_the_rest_the_default():
    default = AllowAll()
    strict = BlockHighRisk()
    table = RuleTablePolicy({'shell.privilege_escalation': Disposition.CONFIRM})
    factories = _catalog('Kilo', 'Alpha', 'Mike').freeze(
        {'Shell': _Shell()},
        _models(),
        danger_policy=default,
        role_policies={'Mike': table, 'Kilo': strict},
    )
    assert [_policy_of(factories, name) for name in ('Kilo', 'Alpha', 'Mike')] == [
        strict,
        default,
        table,
    ]


def test_without_a_default_unlisted_roles_keep_the_kernel_default():
    factories = _catalog('Kilo', 'Alpha').freeze(
        {'Shell': _Shell()}, _models(), role_policies={'Kilo': BlockHighRisk()}
    )
    assert _policy_of(factories, 'Alpha') is None


def test_an_unknown_role_is_refused_with_the_known_ones():
    with pytest.raises(ConfigError) as excinfo:
        _catalog('Kilo', 'Alpha').freeze(
            {'Shell': _Shell()}, _models(), role_policies={'Worker': BlockHighRisk()}
        )
    assert "'Worker'" in excinfo.value.received
    assert "'Kilo'" in excinfo.value.next_action


@pytest.mark.parametrize(
    ('value', 'anchor'),
    [
        ('Kilo', 'Mapping'),
        ({'Kilo': BlockHighRisk}, '少了一对括号'),
        ({'Kilo': 'block_high_risk'}, 'DangerPolicy 实例'),
    ],
)
def test_role_policies_of_the_wrong_shape_are_refused(value: object, anchor: str):
    with pytest.raises(ConfigError) as excinfo:
        _catalog('Kilo').freeze(
            {'Shell': _Shell()},
            _models(),
            role_policies=value,  # pyright: ignore[reportArgumentType]
        )
    assert anchor in f'{excinfo.value.expected}{excinfo.value.received}'


async def _run_one_command(
    factories: Any, role: str, shell: _Shell, command: str = 'sudo rm -rf /tmp/x'
) -> str:
    del shell
    runtime = Runtime(agents=dict(factories))
    try:
        agent_id = await runtime.spawn(factories[role], query='go')
        context = await runtime.wait(agent_id)
        return str(context.current_traj[2].content)
    finally:
        await runtime.shutdown()


async def test_the_role_policy_decides_a_real_tool_call():
    shell = _Shell()
    call = ToolCall(id='c1', name='Shell', arguments=json.dumps({'command': 'sudo whoami'}))

    def llm() -> ScriptedLLM:
        return ScriptedLLM([ScriptedTurn(tool_calls=[call]), ScriptedTurn(content='done')])

    catalog = _catalog('Strict', 'Loose')
    blocking = RuleTablePolicy({'shell.privilege_escalation': Disposition.BLOCK})
    allowing = RuleTablePolicy({'shell.privilege_escalation': Disposition.ALLOW})
    strict_factories = catalog.freeze(
        {'Shell': shell},
        {'default': llm()},
        danger_policy=allowing,
        role_policies={'Strict': blocking},
    )
    loose_factories = catalog.freeze(
        {'Shell': shell},
        {'default': llm()},
        danger_policy=allowing,
        role_policies={'Strict': blocking},
    )
    strict_output = await _run_one_command(strict_factories, 'Strict', shell)
    assert shell.ran == []
    assert 'shell.privilege_escalation' in strict_output
    await _run_one_command(loose_factories, 'Loose', shell)
    assert shell.ran == ['sudo whoami']


async def test_assemble_hands_the_role_policies_to_slot_agents():
    strict = BlockHighRisk()
    runtime = Runtime()
    try:
        await runtime.assemble(
            Topology(
                slots=(AgentSlot(name='one', agent='Kilo', cardinality='singleton', boot='lazy'),)
            ),
            _catalog('Kilo', 'Alpha'),
            tools={'Shell': _Shell()},
            models=_models(),
            danger_policy=AllowAll(),
            role_policies={'Kilo': strict},
        )
        agent_id = await runtime.occupy('one')
        agent: BaseAgent | None = runtime._table.get(agent_id)  # pyright: ignore[reportPrivateUsage]
        assert agent is not None
        assert agent.options.danger_policy is strict
    finally:
        await runtime.shutdown()


def _document(version: int, roles: dict[str, Any]) -> dict[str, Any]:
    return {
        'version': version,
        'name': 'captured',
        'agents': {
            'Lead': {'frozen': {'system_prompt': 'You lead.', 'tools': []}, 'instances': []}
        },
        'entry': {'mode': 'query', 'agent': 'Lead', 'prompt': 'go'},
        'host': {
            'danger': {
                'policy': 'table',
                'table': {'shell.recursive_force_remove': 'block'},
                'roles': roles,
            }
        },
        'ids': [],
        'expect': {},
    }


def test_a_v3_document_records_one_policy_per_role():
    document = parse_scenario_document(
        _document(
            3,
            {
                'Lead': {'policy': 'table', 'table': {'shell.recursive_force_remove': 'confirm'}},
                'Helper': {'policy': 'block_high_risk'},
            },
        ),
        source='captured.json',
    )
    assert document.host is not None
    assert dict(document.host.danger_roles) == {
        'Lead': DangerPolicySpec(policy='table', table={'shell.recursive_force_remove': 'confirm'}),
        'Helper': DangerPolicySpec(policy='block_high_risk'),
    }


def test_per_role_policies_in_a_v2_document_point_to_version_3():
    with pytest.raises(ConfigError) as excinfo:
        parse_scenario_document(
            _document(2, {'Lead': {'policy': 'block_high_risk'}}), source='captured.json'
        )
    assert "['roles'] 是场景格式 v3 的键" in excinfo.value.next_action
    assert '把 version 改成 3' in excinfo.value.next_action


@pytest.mark.parametrize(
    ('entry', 'anchor'),
    [
        ({}, '写明一种处置策略'),
        ({'policy': 'table'}, 'table 时有'),
        ({'policy': 'block_high_risk', 'table': {}}, 'table 时有'),
        ({'policy': 'nonsense'}, 'policy'),
        ({'rules': []}, "只含 ['policy', 'table']"),
    ],
)
def test_a_malformed_role_policy_is_refused(entry: dict[str, Any], anchor: str):
    with pytest.raises(ConfigError) as excinfo:
        parse_scenario_document(_document(3, {'Lead': entry}), source='captured.json')
    assert anchor in f'{excinfo.value.expected}{excinfo.value.received}'
    assert 'host.danger.roles.Lead' in f'{excinfo.value.expected}{excinfo.value.received}'


def test_the_capture_writes_version_3_only_when_it_needs_it():
    assert _document_version({'danger': {'policy': 'allow_all'}}) == 2
    assert _document_version({}) == 2
    assert _document_version({'danger': {'roles': {'Lead': {'policy': 'allow_all'}}}}) == 3


def test_a_policy_type_is_a_protocol_not_a_base_class():
    class _Custom:
        def decide(self, request: object) -> Disposition:
            del request
            return Disposition.BLOCK

    custom: DangerPolicy = _Custom()  # pyright: ignore[reportAssignmentType]
    factories = _catalog('Kilo').freeze(
        {'Shell': _Shell()}, _models(), role_policies={'Kilo': custom}
    )
    assert _policy_of(factories, 'Kilo') is custom
