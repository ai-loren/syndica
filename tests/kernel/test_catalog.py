"""工具目录：按清单名字解析、登记时钉三轴。"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from syndica.kernel.catalog import register_tool, resolve_tool_names
from syndica.kernel.errors import ConfigError
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
