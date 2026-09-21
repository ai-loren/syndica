"""工具目录与 Agent 目录：按清单名字解析、登记时钉三轴；加载清单、同名覆盖可查询。"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from syndica.kernel.agent import BaseAgent
from syndica.kernel.catalog import (
    AgentCatalog,
    OverrideRecord,
    register_tool,
    resolve_tool_names,
)
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import ScriptedLLM
from syndica.kernel.manifest import ManifestSource
from syndica.kernel.tool import Tool, ToolResult


class _Named(Tool):
    def __init__(self, name: str) -> None:
        self._forced = name

    @property
    def name(self) -> str:
        return self._forced

    async def execute(self) -> ToolResult:
        return ToolResult(output=self._forced)


class _ForkedSchema(Tool):
    """name 与 schema['name'] 分叉：登记与解析都必须拒。"""

    __tool_name__ = 'HandWritten'
    __tool_schema__ = {
        'name': 'Custom',
        'description': 'd',
        'parameters': {'type': 'object', 'properties': {}},
    }

    async def execute(self) -> ToolResult:
        return ToolResult(output='ok')


def _catalog(*names: str) -> dict[str, Tool]:
    table: dict[str, Tool] = {}
    for name in names:
        register_tool(table, _Named(name))
    return table


def test_resolve_tool_names_preserves_manifest_order():
    catalog = _catalog('A', 'C', 'E')
    resolved = resolve_tool_names(('E', 'A', 'C'), catalog)
    assert list(resolved) == ['E', 'A', 'C']
    assert list(catalog) == ['A', 'C', 'E']


def test_resolve_tool_names_names_every_missing_tool():
    catalog = _catalog('A', 'C', 'E')
    with pytest.raises(ConfigError) as excinfo:
        resolve_tool_names(('B', 'C', 'D'), catalog)
    error = excinfo.value
    assert "'B'" in error.received
    assert "'D'" in error.received
    assert "'C'" not in error.received
    assert 'B' in error.next_action
    assert 'D' in error.next_action
    assert "'C'" not in error.next_action
    assert 'register_tool' in error.next_action


def test_register_tool_rejects_disagreement_among_the_three_identity_axes():
    table: dict[str, Tool] = {}
    with pytest.raises(ConfigError) as excinfo:
        register_tool(table, _ForkedSchema())
    error = excinfo.value
    assert '三条身份轴' in error.expected
    assert "'HandWritten'" in error.received
    assert "'Custom'" in error.received
    assert '__tool_schema__' in error.next_action
    assert "'HandWritten'" in error.next_action
    assert "'Custom'" in error.next_action
    assert table == {}


def test_resolve_rejects_a_catalog_entry_with_disagreeing_identity_axes():
    table: dict[str, Tool] = {'HandWritten': _ForkedSchema()}
    with pytest.raises(ConfigError) as excinfo:
        resolve_tool_names(('HandWritten',), table)
    error = excinfo.value
    assert '三条身份轴' in error.expected
    assert "'HandWritten'" in error.received
    assert "'Custom'" in error.received
    assert '__tool_schema__' in error.next_action


def test_resolved_dict_shares_no_mutation_with_the_catalog():
    catalog = _catalog('A', 'C', 'E')
    resolved = resolve_tool_names(('C', 'A'), catalog)
    resolved['X'] = _Named('X')
    del resolved['A']
    assert 'X' not in catalog
    assert 'A' in catalog
    catalog['Z'] = _Named('Z')
    del catalog['C']
    assert 'Z' not in resolved
    assert 'C' in resolved
    assert 'A' not in resolved


def test_register_tool_inserts_under_tool_name_not_a_caller_key():
    table: dict[str, Tool] = {}
    tool = _Named('Grep')
    register_tool(table, tool)
    assert list(table) == ['Grep']
    assert table['Grep'] is tool


def test_resolve_rejects_a_string_of_names_as_a_sequence():
    catalog = _catalog('A')
    with pytest.raises(ConfigError) as excinfo:
        resolve_tool_names('A', catalog)
    assert 'str' in excinfo.value.received
    assert '序列' in excinfo.value.expected


def test_register_tool_rejects_a_frozen_mapping():
    with pytest.raises(ConfigError) as excinfo:
        register_tool(MappingProxyType({'A': _Named('A')}), _Named('C'))
    assert 'mappingproxy' in excinfo.value.received
    assert 'dict' in excinfo.value.next_action


def _agent_body(name: str, description: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {'name': name, 'description': description}
    payload.update(extra)
    return payload


def _write_json(directory: Path, filename: str, payload: dict[str, object]) -> Path:
    path = directory / filename
    path.write_text(json.dumps(payload), encoding='utf-8')
    return path


class _StubAgent(BaseAgent):
    """规划范围时用我。"""


def test_later_source_overrides_the_same_name_and_the_displaced_record_is_queryable():
    catalog = AgentCatalog()
    first = ManifestSource(kind='mapping', location='v1')
    second = ManifestSource(kind='mapping', location='v2')
    catalog.load_mapping(_agent_body('Worker', '第一份'), source=first)
    catalog.load_mapping(_agent_body('Worker', '第二份'), source=second)
    current = catalog.get('Worker')
    assert current.description == '第二份'
    assert catalog.source_of('Worker') == second
    records = catalog.overrides('Worker')
    assert len(records) == 1
    displaced = records[0]
    assert isinstance(displaced, OverrideRecord)
    assert displaced.name == 'Worker'
    assert displaced.displaced_source == first
    assert displaced.by_source == second
    assert displaced.displaced_at_index == 0
    assert catalog.names() == ('Worker',)


def test_override_chain_keeps_every_displaced_source_oldest_first():
    catalog = AgentCatalog()
    one = ManifestSource(kind='mapping', location='one')
    two = ManifestSource(kind='mapping', location='two')
    three = ManifestSource(kind='mapping', location='three')
    catalog.load_mapping(_agent_body('Worker', '一'), source=one)
    catalog.load_mapping(_agent_body('Worker', '二'), source=two)
    catalog.load_mapping(_agent_body('Worker', '三'), source=three)
    records = catalog.overrides('Worker')
    assert len(records) == 2
    assert [item.displaced_source for item in records] == [one, two]
    assert [item.by_source for item in records] == [two, three]
    assert [item.displaced_at_index for item in records] == [0, 1]
    assert catalog.source_of('Worker').location == 'three'
    assert catalog.get('Worker').description == '三'


def test_names_keep_first_seen_order_when_overridden():
    catalog = AgentCatalog()
    catalog.load_mapping(_agent_body('Alpha', 'α'))
    catalog.load_mapping(_agent_body('Beta', 'β1'))
    catalog.load_mapping(_agent_body('Gamma', 'γ'))
    catalog.load_mapping(_agent_body('Beta', 'β2'))
    assert catalog.names() == ('Alpha', 'Beta', 'Gamma')
    assert catalog.get('Beta').description == 'β2'
    assert catalog.overrides('Alpha') == ()
    assert catalog.overrides('Gamma') == ()
    assert catalog.overrides('Beta')[0].displaced_source.location == '<mapping>'


def test_names_are_first_seen_not_sorted():
    catalog = AgentCatalog()
    catalog.load_mapping(_agent_body('Zeta', '末'))
    catalog.load_mapping(_agent_body('Alpha', '甲'))
    catalog.load_mapping(_agent_body('Mu', '中'))
    assert catalog.names() == ('Zeta', 'Alpha', 'Mu')


def test_two_json_files_in_one_directory_with_the_same_name_are_rejected(
    tmp_path: Path,
):
    catalog = AgentCatalog()
    catalog.load_mapping(_agent_body('Alpha', '先在的'))
    _write_json(tmp_path, 'a.json', _agent_body('Worker', '甲'))
    _write_json(tmp_path, 'b.json', _agent_body('Unique', '独一份'))
    _write_json(tmp_path, 'c.json', _agent_body('Worker', '乙'))
    with pytest.raises(ConfigError) as excinfo:
        catalog.load_json_directory(tmp_path)
    error = excinfo.value
    assert '不是覆盖' in error.expected
    assert 'Worker' in error.received
    assert 'a.json' in error.received
    assert 'c.json' in error.received
    assert 'Unique' not in error.received
    assert '分两次' in error.next_action
    assert catalog.names() == ('Alpha',)
    assert catalog.overrides('Worker') == ()
    assert catalog.get('Alpha').description == '先在的'
    with pytest.raises(ConfigError) as unique_exc:
        catalog.get('Unique')
    assert "'Unique'" in unique_exc.value.received
    with pytest.raises(ConfigError) as worker_exc:
        catalog.get('Worker')
    assert "'Worker'" in worker_exc.value.received


def test_load_json_directory_ignores_non_json_and_does_not_recurse(tmp_path: Path):
    catalog = AgentCatalog()
    _write_json(tmp_path, 'worker.json', _agent_body('Worker', '改文件时用我'))
    (tmp_path / 'notes.txt').write_text(
        json.dumps(_agent_body('Scout', '不该被加载')), encoding='utf-8'
    )
    (tmp_path / 'worker.json.bak').write_text(
        json.dumps(_agent_body('Bak', '后缀不是 json')), encoding='utf-8'
    )
    (tmp_path / 'readme.md').write_text('# ignore', encoding='utf-8')
    nested = tmp_path / 'nested'
    nested.mkdir()
    _write_json(nested, 'hidden.json', _agent_body('Hidden', '子目录不递归'))
    loaded = catalog.load_json_directory(tmp_path)
    assert [item.name for item in loaded] == ['Worker']
    assert catalog.names() == ('Worker',)
    with pytest.raises(ConfigError) as scout_exc:
        catalog.get('Scout')
    assert "'Scout'" in scout_exc.value.received
    with pytest.raises(ConfigError) as hidden_exc:
        catalog.get('Hidden')
    assert "'Hidden'" in hidden_exc.value.received
    with pytest.raises(ConfigError) as bak_exc:
        catalog.get('Bak')
    assert "'Bak'" in bak_exc.value.received


def test_get_names_every_missing_role_and_keeps_known_names():
    catalog = AgentCatalog()
    catalog.load_mapping(_agent_body('Alpha', 'α'))
    catalog.load_mapping(_agent_body('Beta', 'β'))
    catalog.load_mapping(_agent_body('Gamma', 'γ'))
    with pytest.raises(ConfigError) as excinfo:
        catalog.get('Missing')
    error = excinfo.value
    assert "'Missing'" in error.received
    assert "'Alpha'" in error.received
    assert "'Beta'" in error.received
    assert "'Gamma'" in error.received
    assert 'load_mapping' in error.next_action
    assert 'load_json_directory' in error.next_action
    assert 'register_builtin' in error.next_action
    assert catalog.names() == ('Alpha', 'Beta', 'Gamma')


def test_register_class_uses_docstring_and_does_not_instantiate():
    catalog = AgentCatalog()
    catalog.register_class('Planner', _StubAgent)
    manifest = catalog.get('Planner')
    assert manifest.impl is _StubAgent
    assert manifest.description == '规划范围时用我。'
    assert manifest.source.kind == 'class'
    assert catalog.names() == ('Planner',)


def test_load_json_file_sets_origin_to_the_parent_directory(tmp_path: Path):
    prompt = tmp_path / 'prompt.md'
    prompt.write_text('来自文件的提示词', encoding='utf-8')
    path = _write_json(
        tmp_path,
        'worker.json',
        _agent_body('Worker', '改文件时用我', system_prompt='prompt.md'),
    )
    catalog = AgentCatalog()
    manifest = catalog.load_json_file(path)
    assert manifest.system_prompt == '来自文件的提示词'
    assert manifest.source.kind == 'json_file'
    assert manifest.source.origin == str(tmp_path.resolve())
    assert manifest.source.location == str(path.resolve())


def test_directory_load_is_filename_order_then_later_directory_can_override(
    tmp_path: Path,
):
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    _write_json(first, 'm.json', _agent_body('Middle', '中1'))
    _write_json(first, 'a.json', _agent_body('Alpha', '甲'))
    _write_json(first, 'z.json', _agent_body('Zeta', '末'))
    catalog = AgentCatalog()
    catalog.load_json_directory(first)
    assert catalog.names() == ('Alpha', 'Middle', 'Zeta')
    _write_json(second, 'm.json', _agent_body('Middle', '中2'))
    catalog.load_json_directory(second)
    assert catalog.names() == ('Alpha', 'Middle', 'Zeta')
    assert catalog.get('Middle').description == '中2'
    assert catalog.overrides('Middle')[0].displaced_source.kind == 'json_directory'


def _models() -> dict[str, ScriptedLLM]:
    return {'default': ScriptedLLM([])}


def _spawn(factory):
    return factory(HooksCenter(), EventBus())


def test_missing_model_name_is_rejected():
    catalog = AgentCatalog()
    catalog.load_mapping(_agent_body('Alpha', 'α', model='alpha-llm'))
    catalog.load_mapping(_agent_body('Worker', '改文件时用我', model='gpt'))
    catalog.load_mapping(_agent_body('Gamma', 'γ', model='claude'))
    with pytest.raises(ConfigError) as excinfo:
        catalog.freeze({}, _models())
    error = excinfo.value
    assert "'alpha-llm'" in error.received
    assert "'gpt'" in error.received
    assert "'claude'" in error.received
    assert "'default'" in error.received
    assert "'Alpha'" in error.received
    assert "'Worker'" in error.received
    assert "'Gamma'" in error.received
    assert 'default' in error.next_action


def test_extends_a_declarative_agent_is_rejected():
    catalog = AgentCatalog()
    catalog.load_mapping(_agent_body('Worker', '声明式工人'))
    catalog.load_mapping(_agent_body('Scout', '侦察', extends='Worker'))
    with pytest.raises(ConfigError) as excinfo:
        catalog.freeze({}, _models())
    error = excinfo.value
    assert "'Scout'" in error.received
    assert "'Worker'" in error.received
    assert '声明式' in error.received
    assert '类式' in error.expected or 'register_class' in error.next_action


def test_extends_cycle_is_rejected():
    catalog = AgentCatalog()
    catalog.load_mapping(_agent_body('Alpha', '甲', impl=_StubAgent, extends='Beta'))
    catalog.load_mapping(_agent_body('Beta', '乙', impl=_StubAgent, extends='Alpha'))
    with pytest.raises(ConfigError) as excinfo:
        catalog.freeze({}, _models())
    error = excinfo.value
    assert "'Alpha'" in error.received
    assert "'Beta'" in error.received
    assert '环' in error.expected or '环' in error.received


def test_frozen_factory_does_not_see_later_catalog_tool_mutations():
    tools = _catalog('Read')
    original = tools['Read']
    catalog = AgentCatalog()
    catalog.load_mapping(_agent_body('Worker', '改文件时用我', tools=['Read']))
    factories = catalog.freeze(tools, _models())
    register_tool(tools, _Named('Read'))
    agent = _spawn(factories['Worker'])
    assert agent.options.tools['Read'] is original
    assert agent.options.tools['Read'] is not tools['Read']


def test_declarative_factory_signature_binds_hooks_and_events():
    catalog = AgentCatalog()
    catalog.load_mapping(_agent_body('Worker', '改文件时用我'))
    factories = catalog.freeze({}, _models())
    factory = factories['Worker']
    assert list(inspect.signature(factory).parameters) == ['hooks', 'events']
    inspect.signature(factory).bind(HooksCenter(), EventBus())
    agent = _spawn(factory)
    assert isinstance(agent, BaseAgent)
    assert type(agent) is BaseAgent


def test_freeze_returns_factories_in_first_seen_name_order():
    catalog = AgentCatalog()
    catalog.load_mapping(_agent_body('Zeta', '末'))
    catalog.load_mapping(_agent_body('Alpha', '甲'))
    catalog.load_mapping(_agent_body('Mu', '中'))
    factories = catalog.freeze({}, _models())
    assert list(factories) == ['Zeta', 'Alpha', 'Mu']


def test_child_prompt_is_not_the_parent_prompt():
    catalog = AgentCatalog()
    catalog.load_mapping(
        _agent_body('Planner', '规划范围时用我。', impl=_StubAgent, system_prompt='PARENT')
    )
    catalog.load_mapping(_agent_body('Scout', '侦察', extends='Planner'))
    factories = catalog.freeze({}, _models())
    child = _spawn(factories['Scout'])
    parent = _spawn(factories['Planner'])
    assert parent.options.system_prompt == 'PARENT'
    assert child.options.system_prompt == ''


def test_tool_names_union_child_first_then_parent_without_duplicates():
    tools = _catalog('Read', 'Grep', 'Write')
    catalog = AgentCatalog()
    catalog.load_mapping(
        _agent_body('Planner', '规划范围时用我。', impl=_StubAgent, tools=['Read', 'Grep'])
    )
    catalog.load_mapping(_agent_body('Scout', '侦察', extends='Planner', tools=['Grep', 'Write']))
    factories = catalog.freeze(tools, _models())
    scout = _spawn(factories['Scout'])
    assert list(scout.options.tools) == ['Grep', 'Write', 'Read']
    planner = _spawn(factories['Planner'])
    assert list(planner.options.tools) == ['Read', 'Grep']


def test_class_factory_constructs_the_registered_subclass():
    catalog = AgentCatalog()
    catalog.register_class('Planner', _StubAgent)
    factories = catalog.freeze({}, _models())
    agent = _spawn(factories['Planner'])
    assert type(agent) is _StubAgent
    assert list(inspect.signature(factories['Planner']).parameters) == ['hooks', 'events']


def test_string_impl_imports_the_class():
    catalog = AgentCatalog()
    catalog.load_mapping(
        _agent_body('Worker', '改文件时用我', impl='syndica.kernel.agent:BaseAgent')
    )
    factories = catalog.freeze({}, _models())
    agent = _spawn(factories['Worker'])
    assert type(agent) is BaseAgent


def test_absent_budget_freezes_to_the_default_turn_limit():
    catalog = AgentCatalog()
    catalog.load_mapping(_agent_body('Worker', '改文件时用我'))
    factories = catalog.freeze({}, _models())
    assert _spawn(factories['Worker']).options.max_turns == 50


def test_budget_turns_null_disables_the_turn_limit():
    catalog = AgentCatalog()
    catalog.load_mapping(_agent_body('Worker', '改文件时用我', budget={'turns': None}))
    factories = catalog.freeze({}, _models())
    assert _spawn(factories['Worker']).options.max_turns is None


def test_missing_subagents_are_all_named():
    catalog = AgentCatalog()
    catalog.load_mapping(
        _agent_body(
            'Worker',
            '改文件时用我',
            subagents=['Beta', 'Gamma', 'Delta', 'Epsilon'],
        )
    )
    catalog.load_mapping(_agent_body('Gamma', '已在'))
    with pytest.raises(ConfigError) as excinfo:
        catalog.freeze({}, _models())
    error = excinfo.value
    missing_part = error.received.split('没有', 1)[1]
    assert "'Beta'" in missing_part
    assert "'Delta'" in missing_part
    assert "'Epsilon'" in missing_part
    assert "'Gamma'" not in missing_part
    assert 'Beta' in error.next_action
    assert 'Delta' in error.next_action
    assert 'Epsilon' in error.next_action


def test_host_that_is_not_host_facts_is_rejected():
    catalog = AgentCatalog()
    catalog.load_mapping(_agent_body('Worker', '改文件时用我'))
    duck = SimpleNamespace(os='darwin', platform='arm64', shell='zsh', cwd='/tmp')
    with pytest.raises(ConfigError) as excinfo:
        catalog.freeze({}, _models(), host=duck)
    assert 'HostFacts' in excinfo.value.expected
    assert type(duck).__name__ in excinfo.value.received


def test_freeze_of_an_empty_catalog_returns_an_empty_mapping():
    factories = AgentCatalog().freeze({}, _models())
    assert dict(factories) == {}


def test_extends_unknown_name_is_rejected():
    catalog = AgentCatalog()
    catalog.load_mapping(_agent_body('Scout', '侦察', extends='Ghost'))
    with pytest.raises(ConfigError) as excinfo:
        catalog.freeze({}, _models())
    error = excinfo.value
    assert "'Ghost'" in error.received
    assert "'Scout'" in error.received
