from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from syndica.kernel.agent import BaseAgent
from syndica.kernel.errors import ConfigError
from syndica.kernel.manifest import AgentManifest, ManifestSource, parse_mapping

_SOURCE = ManifestSource(kind='mapping', location='<mapping>')


def _body(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        'name': 'Worker',
        'description': '改文件时用我',
    }
    data.update(overrides)
    return data


def _parse(data: Mapping[str, Any], *, source: ManifestSource = _SOURCE) -> AgentManifest:
    return parse_mapping(data, source=source)


def test_the_manifest_field_order_is_exactly_this() -> None:
    assert [field.name for field in dataclasses.fields(AgentManifest)] == [
        'name',
        'description',
        'system_prompt',
        'tools',
        'subagents',
        'model',
        'extends',
        'skills',
        'budget',
        'artifact_kinds',
        'impl',
        'source',
    ]
    assert [field.name for field in dataclasses.fields(ManifestSource)] == [
        'kind',
        'location',
        'origin',
    ]


def test_a_mapping_with_unknown_keys_is_rejected_naming_every_unknown_key() -> None:
    data = _body(zeta='1', **{'class': 'Worker'}, alpha='2')
    with pytest.raises(ConfigError) as info:
        _parse(data)
    error = info.value
    named = {key for key in ('zeta', 'class', 'alpha') if key in error.received}
    assert named == {'zeta', 'class', 'alpha'}, (
        f'received 必须点出每一个未知键，不能只报第一个：{error.received!r}'
    )
    assert '未知键' in error.received
    assert 'impl' in error.next_action
    assert 'class' in error.next_action


def test_blank_name_or_description_is_rejected() -> None:
    with pytest.raises(ConfigError) as info:
        _parse(_body(name='   '))
    assert 'name' in info.value.expected
    assert info.value.received == "name='   '"
    assert '非空白' in info.value.next_action

    with pytest.raises(ConfigError) as info:
        _parse({'name': 'Worker', 'description': ''})
    assert 'description' in info.value.expected
    assert "description=''" in info.value.received


def test_tools_must_be_a_sequence_of_names_not_a_tool_mapping() -> None:
    tools = {'Grep': object(), 'Read': object(), 'Bash': object()}
    with pytest.raises(ConfigError) as info:
        _parse(_body(tools=tools))
    error = info.value
    assert '名字列表' in error.expected
    named = {key for key in tools if key in error.received}
    assert named == set(tools)
    assert 'AgentCatalog' in error.next_action or '工具目录' in error.next_action
    with pytest.raises(ConfigError) as info:
        _parse(_body(tools='Read'))
    assert 'str' in info.value.received
    assert '单个字符' in info.value.received
    parsed = _parse(_body(tools=('Grep', 'Read', 'Bash')))
    assert parsed.tools == ('Grep', 'Read', 'Bash')


def test_budget_rejects_keys_other_than_turns() -> None:
    with pytest.raises(ConfigError) as info:
        _parse(_body(budget={'turns': 40, 'tokens': 1, 'wall': 2}))
    error = info.value
    named = {key for key in ('tokens', 'wall') if key in error.received}
    assert named == {'tokens', 'wall'}
    assert 'turns' in error.expected
    assert 'max_turns' in error.next_action


def test_impl_absent_means_declarative() -> None:
    parsed = _parse(_body())
    assert parsed.impl is None
    assert parsed.model == 'default'
    assert parsed.tools == ()
    assert parsed.system_prompt == ''
    assert parsed.budget is None
    assert parsed.extends is None


def test_impl_type_must_be_a_base_agent_subclass() -> None:
    class _Typed(BaseAgent):
        pass

    parsed = _parse(_body(impl=_Typed))
    assert parsed.impl is _Typed

    parsed = _parse(_body(impl='pkg.mod:Worker'))
    assert parsed.impl == 'pkg.mod:Worker'

    with pytest.raises(ConfigError) as info:
        _parse(_body(impl=object))
    assert 'BaseAgent' in info.value.expected
    assert 'object' in info.value.received

    with pytest.raises(ConfigError) as info:
        _parse(_body(impl=object()))
    assert 'BaseAgent' in info.value.expected


def test_system_prompt_file_is_read_only_when_source_has_a_directory(
    tmp_path: Path,
) -> None:
    prompt = tmp_path / 'worker.md'
    prompt.write_text('你是改文件的 Worker。\n', encoding='utf-8')
    source = ManifestSource(
        kind='mapping',
        location='<mapping>',
        origin=str(tmp_path),
    )
    parsed = _parse(_body(system_prompt='worker.md'), source=source)
    assert parsed.system_prompt == '你是改文件的 Worker。\n'


def test_system_prompt_string_is_literal_without_an_origin_directory() -> None:
    parsed = _parse(_body(system_prompt='worker.md'))
    assert parsed.system_prompt == 'worker.md'
    parsed = _parse(_body(system_prompt='第一行\n第二行'))
    assert parsed.system_prompt == '第一行\n第二行'


def test_system_prompt_is_literal_even_when_that_string_is_an_existing_file(
    tmp_path: Path,
) -> None:
    prompt = tmp_path / 'worker.md'
    prompt.write_text('不该被读到。\n', encoding='utf-8')
    parsed = _parse(_body(system_prompt=str(prompt)))
    assert parsed.system_prompt == str(prompt)


def test_system_prompt_parent_relative_path_is_literal_not_a_file_outside_origin(
    tmp_path: Path,
) -> None:
    (tmp_path / 'outside.md').write_text('OUT\n', encoding='utf-8')
    origin = tmp_path / 'agents'
    origin.mkdir()
    source = ManifestSource(kind='mapping', location='<mapping>', origin=str(origin))
    parsed = _parse(_body(system_prompt='../outside.md'), source=source)
    assert parsed.system_prompt == '../outside.md'


def test_budget_turns_null_means_unlimited_and_host_mutation_does_not_leak() -> None:
    raw: dict[str, int | None] = {'turns': None}
    parsed = _parse(_body(budget=raw))
    assert parsed.budget is not None
    assert dict(parsed.budget) == {'turns': None}
    assert isinstance(parsed.budget, MappingProxyType)
    raw['turns'] = 1
    assert dict(parsed.budget) == {'turns': None}


def test_budget_empty_mapping_is_ambiguous_and_rejected() -> None:
    with pytest.raises(ConfigError) as info:
        _parse(_body(budget={}))
    assert 'turns' in info.value.expected
    assert info.value.received == 'budget={}，没有 turns'


def test_budget_turns_rejects_bool_because_yaml_yes_is_true() -> None:
    with pytest.raises(ConfigError) as info:
        _parse(_body(budget={'turns': True}))
    assert 'bool' in info.value.received
    assert 'yes' in info.value.received


def test_parse_mapping_does_not_mutate_the_input() -> None:
    data = _body(tools=['Read', 'Grep'])
    snapshot = dict(data)
    snapshot['tools'] = list(data['tools'])  # type: ignore[arg-type]
    _parse(data)
    assert data == snapshot


def test_a_non_mapping_is_rejected() -> None:
    with pytest.raises(ConfigError) as info:
        parse_mapping(['Worker'], source=_SOURCE)
    assert '映射' in info.value.expected
    assert 'list' in info.value.received


def test_name_list_fields_reject_blank_elements() -> None:
    with pytest.raises(ConfigError) as info:
        _parse(_body(skills=['  ']))
    assert 'skills[0]' in info.value.received
