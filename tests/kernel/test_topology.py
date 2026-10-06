"""AgentSlot / Topology：字段顺序、基数与启动方式、槽名与 root、目录里的角色名。"""

from __future__ import annotations

import dataclasses
from types import MappingProxyType
from typing import Any, cast

import pytest

from syndica.kernel.agent_types import ExitMode
from syndica.kernel.catalog import AgentCatalog
from syndica.kernel.errors import ConfigError
from syndica.kernel.topology import AgentSlot, Topology

_SLOT_FIELDS = [
    'name',
    'agent',
    'is_root',
    'cardinality',
    'key_field',
    'boot',
    'exit_mode',
    'extra_tools',
    'budget',
    'metadata',
    'isolation',
]


def _slot(**kwargs: object) -> AgentSlot:
    return cast(Any, AgentSlot)(**kwargs)


def _catalog(*names: str) -> AgentCatalog:
    catalog = AgentCatalog()
    for name in names:
        catalog.load_mapping({'name': name, 'description': name})
    return catalog


def test_the_slot_field_order_is_exactly_this() -> None:
    assert [item.name for item in dataclasses.fields(AgentSlot)] == _SLOT_FIELDS


def test_defaults_are_on_demand_lazy_zombie() -> None:
    slot = AgentSlot(name='w', agent='Worker')
    assert slot.cardinality == 'on-demand'
    assert slot.boot == 'lazy'
    assert slot.exit_mode is ExitMode.ZOMBIE
    assert slot.is_root is False
    assert slot.key_field is None
    assert slot.extra_tools == ()
    assert slot.budget is None
    assert dict(slot.metadata) == {}
    assert slot.isolation == 'shared'


def test_singleton_eager_keyed_lazy_and_on_demand_manual_construct() -> None:
    AgentSlot(
        name='root', agent='Orchestrator', cardinality='singleton', boot='eager', is_root=True
    )
    AgentSlot(
        name='by-repo',
        agent='Worker',
        cardinality='keyed',
        key_field='repo',
        boot='lazy',
    )
    AgentSlot(name='scratch', agent='Explorer', cardinality='on-demand', boot='manual')
    AgentSlot(name='once', agent='Planner', cardinality='singleton', boot='manual')
    AgentSlot(
        name='by-hand',
        agent='Worker',
        cardinality='keyed',
        key_field='repo',
        boot='manual',
    )


def test_keyed_eager_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(
            name='by-repo',
            agent='Worker',
            cardinality='keyed',
            key_field='repo',
            boot='eager',
        )
    text = str(excinfo.value)
    assert 'keyed' in text
    assert 'eager' in text
    assert 'singleton' in text


def test_on_demand_eager_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='scratch', agent='Explorer', cardinality='on-demand', boot='eager')
    text = str(excinfo.value)
    assert 'on-demand' in text
    assert 'eager' in text
    assert 'lazy' in text


def test_keyed_without_key_field_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='by-repo', agent='Worker', cardinality='keyed')
    assert 'key_field' in str(excinfo.value)


def test_keyed_blank_key_field_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='by-repo', agent='Worker', cardinality='keyed', key_field='   ')
    assert 'key_field' in str(excinfo.value)


def test_singleton_with_key_field_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='root', agent='Orchestrator', cardinality='singleton', key_field='repo')
    text = str(excinfo.value)
    assert 'singleton' in text
    assert 'repo' in text


def test_on_demand_with_key_field_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='scratch', agent='Explorer', cardinality='on-demand', key_field='repo')
    text = str(excinfo.value)
    assert 'on-demand' in text
    assert 'repo' in text


def test_unknown_cardinality_lists_the_allowed_set() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='w', agent='Worker', cardinality='many')
    text = str(excinfo.value)
    assert 'singleton' in text
    assert 'keyed' in text
    assert 'on-demand' in text


def test_unknown_boot_lists_the_allowed_set() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='w', agent='Worker', boot='now')
    text = str(excinfo.value)
    assert 'eager' in text
    assert 'lazy' in text
    assert 'manual' in text


def test_blank_name_and_blank_agent_are_rejected() -> None:
    with pytest.raises(ConfigError) as name_exc:
        _slot(name='  ', agent='Worker')
    assert 'name' in str(name_exc.value)
    with pytest.raises(ConfigError) as agent_exc:
        _slot(name='w', agent='')
    assert 'agent' in str(agent_exc.value)


def test_is_root_rejects_int_one() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='w', agent='Worker', is_root=1)
    text = str(excinfo.value)
    assert 'bool' in text
    assert '1' in text


def test_exit_mode_rejects_a_raw_string() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='w', agent='Worker', exit_mode='zombie')
    text = str(excinfo.value)
    assert 'ExitMode' in text
    assert 'ZOMBIE' in text
    assert 'IDLE' in text
    assert 'EXIT' in text


def test_extra_tools_rejects_a_bare_string() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='w', agent='Worker', extra_tools='Read')
    assert 'Read' in str(excinfo.value)


def test_extra_tools_rejects_a_mapping() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='w', agent='Worker', extra_tools={'Read': object()})
    assert 'Read' in str(excinfo.value)


def test_extra_tools_reports_every_bad_item() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='w', agent='Worker', extra_tools=('Read', '', 1, '   '))
    text = str(excinfo.value)
    assert "''" in text or '""' in text or "''" in text
    assert '1' in text
    assert '   ' in text


def test_extra_tools_keeps_duplicate_names() -> None:
    slot = _slot(name='w', agent='Worker', extra_tools=('Read', 'Read', 'Grep'))
    assert slot.extra_tools == ('Read', 'Read', 'Grep')


def test_budget_rejects_bool_turns_and_every_extra_key() -> None:
    with pytest.raises(ConfigError) as bool_exc:
        _slot(name='w', agent='Worker', budget={'turns': True})
    assert 'bool' in str(bool_exc.value)
    with pytest.raises(ConfigError) as extra_exc:
        _slot(
            name='w',
            agent='Worker',
            budget={'turns': 3, 'tokens': 1, 'wall': 2, 'usd': 3},
        )
    text = str(extra_exc.value)
    assert 'tokens' in text
    assert 'wall' in text
    assert 'usd' in text


def test_budget_turns_null_is_unlimited() -> None:
    slot = _slot(name='w', agent='Worker', budget={'turns': None})
    assert slot.budget == {'turns': None}


def test_metadata_is_a_deep_copy() -> None:
    raw: dict[str, object] = {'lane': {'id': 'a'}}
    slot = _slot(name='w', agent='Worker', metadata=raw)
    cast(dict[str, object], raw['lane'])['id'] = 'b'
    raw['lane'] = {'id': 'c'}
    assert slot.metadata['lane'] == {'id': 'a'}


def test_metadata_rejects_every_non_string_key() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='w', agent='Worker', metadata={1: 'a', 2: 'b', 3: 'c'})
    text = str(excinfo.value)
    assert '1' in text
    assert '2' in text
    assert '3' in text


def test_metadata_rejects_artifact_kinds() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='w', agent='Worker', metadata={'artifact_kinds': (), 'lane': 'a'})
    assert 'artifact_kinds' in str(excinfo.value)


def test_metadata_rejects_a_value_that_cannot_be_copied() -> None:
    class _Boom:
        def __deepcopy__(self, memo: object) -> object:
            raise TypeError('live object')

    with pytest.raises(ConfigError) as excinfo:
        _slot(name='w', agent='Worker', metadata={'handle': _Boom()})
    text = str(excinfo.value)
    assert 'deepcopy' in text
    assert 'live object' in text


def test_at_most_one_root() -> None:
    Topology(
        slots=(
            AgentSlot(name='root', agent='Orchestrator', is_root=True, cardinality='singleton'),
            AgentSlot(name='hand', agent='Worker'),
        )
    )
    Topology(slots=())
    Topology(slots=(AgentSlot(name='hand', agent='Worker'),))
    with pytest.raises(ConfigError) as excinfo:
        Topology(
            slots=(
                AgentSlot(
                    name='Alpha', agent='Orchestrator', is_root=True, cardinality='singleton'
                ),
                AgentSlot(name='Beta', agent='Planner', is_root=True, cardinality='singleton'),
                AgentSlot(name='Gamma', agent='Worker', is_root=True, cardinality='singleton'),
            )
        )
    text = str(excinfo.value)
    assert 'Alpha' in text
    assert 'Beta' in text
    assert 'Gamma' in text


def test_duplicate_slot_names_lists_every_name_and_count() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Topology(
            slots=(
                AgentSlot(name='Alpha', agent='Worker'),
                AgentSlot(name='Alpha', agent='Explorer'),
                AgentSlot(name='Beta', agent='Worker'),
                AgentSlot(name='Beta', agent='Explorer'),
                AgentSlot(name='Beta', agent='Planner'),
                AgentSlot(name='Gamma', agent='Worker'),
            )
        )
    text = str(excinfo.value)
    assert 'Alpha×2' in text
    assert 'Beta×3' in text
    assert 'Gamma' not in text


def test_slots_reports_every_non_slot() -> None:
    good = AgentSlot(name='ok', agent='Worker')
    with pytest.raises(ConfigError) as excinfo:
        Topology(slots=cast(Any, (good, {'name': 'a'}, 'nope', 3)))
    text = str(excinfo.value)
    assert '1' in text
    assert '2' in text
    assert '3' in text
    assert 'dict' in text
    assert 'str' in text
    assert 'int' in text


def test_unknown_agent_names_are_all_reported() -> None:
    topology = Topology(
        slots=(
            AgentSlot(name='a', agent='Alpha'),
            AgentSlot(name='g', agent='Gamma'),
            AgentSlot(name='b', agent='Beta'),
            AgentSlot(name='d', agent='Delta'),
            AgentSlot(name='g2', agent='Gamma'),
        )
    )
    with pytest.raises(ConfigError) as excinfo:
        topology.validate(_catalog('Gamma', 'Other'))
    text = str(excinfo.value)
    assert 'Alpha' in text
    assert 'Beta' in text
    assert 'Delta' in text
    assert 'Gamma' not in text.split('缺少')[1]


def test_validate_accepts_a_catalog_that_has_every_role() -> None:
    topology = Topology(
        slots=(
            AgentSlot(
                name='root',
                agent='Orchestrator',
                is_root=True,
                cardinality='singleton',
                boot='eager',
            ),
            AgentSlot(name='hand', agent='Worker'),
        )
    )
    topology.validate(_catalog('Orchestrator', 'Worker'))


def test_validate_rejects_a_duck_typed_catalog() -> None:
    class _Duck:
        def names(self) -> tuple[str, ...]:
            return ('Worker',)

        def get(self, name: object) -> object:
            return object()

    topology = Topology(slots=(AgentSlot(name='w', agent='Worker'),))
    with pytest.raises(ConfigError) as excinfo:
        topology.validate(_Duck())
    text = str(excinfo.value)
    assert 'AgentCatalog' in text
    assert '_Duck' in text


def test_exit_mode_rejects_member_name_strings() -> None:
    for raw in ('ZOMBIE', 'IDLE', 'EXIT'):
        with pytest.raises(ConfigError) as excinfo:
            _slot(name='w', agent='Worker', exit_mode=raw)
        assert raw in str(excinfo.value)
        assert 'ExitMode' in str(excinfo.value)


def test_budget_rejects_an_integer_valued_float() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='w', agent='Worker', budget={'turns': 40.0})
    text = str(excinfo.value)
    assert '40.0' in text
    assert 'float' in text


def test_zero_turns_is_legal() -> None:
    slot = _slot(name='w', agent='Worker', budget={'turns': 0})
    assert slot.budget is not None
    assert slot.budget['turns'] == 0
    assert type(slot.budget) is MappingProxyType


def test_metadata_is_a_mapping_proxy() -> None:
    slot = _slot(name='w', agent='Worker', metadata={'lane': 'a'})
    assert type(slot.metadata) is MappingProxyType
    with pytest.raises(TypeError):
        cast(Any, slot.metadata)['lane'] = 'z'


def test_cardinality_and_boot_are_case_sensitive() -> None:
    with pytest.raises(ConfigError) as card_exc:
        _slot(name='w', agent='Worker', cardinality='Keyed')
    assert 'Keyed' in str(card_exc.value)
    with pytest.raises(ConfigError) as boot_exc:
        _slot(name='w', agent='Worker', boot='Eager')
    assert 'Eager' in str(boot_exc.value)


def test_validate_returns_none() -> None:
    topology = Topology(slots=(AgentSlot(name='w', agent='Worker'),))
    assert topology.validate(_catalog('Worker')) is None


def test_extra_tools_and_slots_are_tuples() -> None:
    slot = _slot(name='w', agent='Worker', extra_tools=['Read', 'Grep'])
    assert type(slot.extra_tools) is tuple
    topology = Topology(slots=cast(Any, [slot]))
    assert type(topology.slots) is tuple


def test_topology_does_not_assemble_or_start() -> None:
    assert not hasattr(Topology, 'assemble')
    assert not hasattr(Topology, 'start')
    assert not hasattr(AgentSlot, 'extra_plugins')


def test_two_slots_may_share_one_agent_name() -> None:
    topology = Topology(
        slots=(
            AgentSlot(name='left', agent='Worker'),
            AgentSlot(name='right', agent='Worker'),
        )
    )
    topology.validate(_catalog('Worker'))
    assert [slot.agent for slot in topology.slots] == ['Worker', 'Worker']


def test_empty_topology_validates_against_an_empty_catalog() -> None:
    Topology().validate(AgentCatalog())


def test_unknown_agent_is_legal_until_validate() -> None:
    topology = Topology(slots=(AgentSlot(name='w', agent='Nope'),))
    assert topology.slots[0].agent == 'Nope'


def test_negative_turns_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='w', agent='Worker', budget={'turns': -1})
    assert '-1' in str(excinfo.value)


def test_budget_without_turns_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='w', agent='Worker', budget={})
    assert 'turns' in str(excinfo.value)


def test_budget_rejects_a_bare_string() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='w', agent='Worker', budget='40')
    assert '40' in str(excinfo.value)


def test_metadata_rejects_a_list() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='w', agent='Worker', metadata=['lane'])
    assert 'list' in str(excinfo.value)


def test_slots_rejects_a_bare_string() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Topology(slots=cast(Any, 'abc'))
    text = str(excinfo.value)
    assert 'abc' in text
    assert 'Sequence' in text or '拆成' in text


def test_singleton_blank_key_field_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        _slot(name='root', agent='Orchestrator', cardinality='singleton', key_field='')
    assert 'key_field' in str(excinfo.value)


def test_exit_mode_idle_and_exit_are_accepted() -> None:
    idle = AgentSlot(name='w', agent='Worker', exit_mode=ExitMode.IDLE)
    gone = AgentSlot(name='x', agent='Worker', exit_mode=ExitMode.EXIT)
    assert idle.exit_mode is ExitMode.IDLE
    assert gone.exit_mode is ExitMode.EXIT


def test_missing_agent_names_are_listed_once_each() -> None:
    topology = Topology(
        slots=(
            AgentSlot(name='a1', agent='Alpha'),
            AgentSlot(name='a2', agent='Alpha'),
            AgentSlot(name='b', agent='Beta'),
            AgentSlot(name='d', agent='Delta'),
        )
    )
    with pytest.raises(ConfigError) as excinfo:
        topology.validate(_catalog('Gamma'))
    text = str(excinfo.value)
    assert "['Alpha', 'Beta', 'Delta']" in text
    assert text.count('Alpha') == 1
