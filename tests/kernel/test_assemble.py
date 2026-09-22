"""装配拓扑：eager 才在 assemble 时起来；槽名不是 send 的第一个参数。"""

from __future__ import annotations

import ast
import inspect
import logging
from collections.abc import Mapping

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.catalog import AgentCatalog
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import LLM, ScriptedLLM, ScriptedTurn
from syndica.kernel.runtime import Runtime
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.topology import AgentSlot, Topology


class _Read(Tool):
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        return ToolResult(output='read')


class _Grep(Tool):
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        return ToolResult(output='grep')


class _Marker(BaseAgent):
    """Runtime(agents=) 里的旧工厂。被目录覆盖之后，实例类型不该还是它。"""


def _body(name: str, **extra: object) -> dict[str, object]:
    data: dict[str, object] = {'name': name, 'description': name}
    data.update(extra)
    return data


def _models() -> dict[str, LLM]:
    return {'default': ScriptedLLM([ScriptedTurn(content='ok')])}


def _catalog(*names: str, **extra: object) -> AgentCatalog:
    catalog = AgentCatalog()
    for name in names:
        catalog.load_mapping(_body(name, **extra))
    return catalog


def _tools() -> dict[str, Tool]:
    return {'Read': _Read(), 'Grep': _Grep()}


async def _assemble(
    runtime: Runtime,
    topology: Topology,
    catalog: AgentCatalog,
    *,
    tools: Mapping[str, Tool] | None = None,
) -> None:
    await runtime.assemble(
        topology,
        catalog,
        tools={} if tools is None else tools,
        models=_models(),
    )


async def test_assemble_spawns_eager_singleton_and_not_lazy_or_manual() -> None:
    catalog = _catalog('Eager', 'Lazy', 'Manual')
    topology = Topology(
        slots=(
            AgentSlot(
                name='root',
                agent='Eager',
                is_root=True,
                cardinality='singleton',
                boot='eager',
            ),
            AgentSlot(name='later', agent='Lazy', cardinality='singleton', boot='lazy'),
            AgentSlot(name='hand', agent='Manual', cardinality='singleton', boot='manual'),
        )
    )
    runtime = Runtime()
    try:
        await _assemble(runtime, topology, catalog)
        listed = await runtime.registry.list()
        assert [info.name for info in listed] == ['Eager']
        assert await runtime.resolve('root') == listed[0].agent_id
        assert await runtime.resolve('later') is None
        assert await runtime.resolve('hand') is None
    finally:
        await runtime.shutdown()


async def test_second_assemble_is_a_contract_violation() -> None:
    catalog = _catalog('Eager')
    topology = Topology(
        slots=(AgentSlot(name='root', agent='Eager', cardinality='singleton', boot='eager'),)
    )
    runtime = Runtime()
    try:
        await _assemble(runtime, topology, catalog)
        with pytest.raises(ContractViolation, match='只 assemble 一次'):
            await _assemble(runtime, topology, catalog)
    finally:
        await runtime.shutdown()


async def test_lazy_singleton_appears_only_after_send_to_slot() -> None:
    catalog = _catalog('Lazy')
    topology = Topology(
        slots=(AgentSlot(name='later', agent='Lazy', cardinality='singleton', boot='lazy'),)
    )
    runtime = Runtime()
    try:
        await _assemble(runtime, topology, catalog)
        assert await runtime.registry.list() == []
        delivered = await runtime.send_to_slot('later', 'hi')
        assert delivered.ok
        found = await runtime.find('later')
        assert found is not None
        assert found.agent_id == await runtime.resolve('later')
    finally:
        await runtime.shutdown()


async def test_find_does_not_spawn_a_lazy_slot() -> None:
    catalog = _catalog('Lazy')
    topology = Topology(
        slots=(AgentSlot(name='later', agent='Lazy', cardinality='singleton', boot='lazy'),)
    )
    runtime = Runtime()
    try:
        await _assemble(runtime, topology, catalog)
        assert await runtime.find('later') is None
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


async def test_manual_slot_refuses_resolve_with_spawn() -> None:
    catalog = _catalog('Manual')
    topology = Topology(
        slots=(AgentSlot(name='hand', agent='Manual', cardinality='singleton', boot='manual'),)
    )
    runtime = Runtime()
    try:
        await _assemble(runtime, topology, catalog)
        with pytest.raises(ConfigError, match='用 occupy'):
            await runtime.resolve('hand', spawn=True)
        assert await runtime.registry.list() == []
        with pytest.raises(ConfigError, match='用 occupy'):
            await runtime.send_to_slot('hand', 'hi')
    finally:
        await runtime.shutdown()


async def test_occupy_singleton_is_idempotent() -> None:
    catalog = _catalog('Role')
    topology = Topology(
        slots=(AgentSlot(name='one', agent='Role', cardinality='singleton', boot='lazy'),)
    )
    runtime = Runtime()
    try:
        await _assemble(runtime, topology, catalog)
        first = await runtime.occupy('one')
        second = await runtime.occupy('one')
        assert first == second
        assert len(await runtime.registry.list()) == 1
    finally:
        await runtime.shutdown()


async def test_keyed_same_key_is_idempotent_and_different_keys_are_not() -> None:
    catalog = _catalog('Role')
    topology = Topology(
        slots=(
            AgentSlot(
                name='workers',
                agent='Role',
                cardinality='keyed',
                key_field='id',
                boot='lazy',
            ),
        )
    )
    runtime = Runtime()
    try:
        await _assemble(runtime, topology, catalog)
        first = await runtime.occupy('workers', key='a')
        again = await runtime.occupy('workers', key='a')
        other = await runtime.occupy('workers', key='b')
        assert first == again
        assert first != other
        assert await runtime.resolve('workers', key='a') == first
        with pytest.raises(ConfigError, match='keyed'):
            await runtime.resolve('workers')
    finally:
        await runtime.shutdown()


async def test_on_demand_occupy_always_creates() -> None:
    catalog = _catalog('Role')
    topology = Topology(slots=(AgentSlot(name='scratch', agent='Role', cardinality='on-demand'),))
    runtime = Runtime()
    try:
        await _assemble(runtime, topology, catalog)
        first = await runtime.occupy('scratch')
        second = await runtime.occupy('scratch')
        assert first != second
        assert await runtime.resolve('scratch') is None
        assert await runtime.find('scratch') is None
        with pytest.raises(ConfigError, match='用 occupy'):
            await runtime.send_to_slot('scratch', 'hi')
    finally:
        await runtime.shutdown()


async def test_extra_tools_union_rejects_replacing_an_existing_name() -> None:
    catalog = _catalog('Role', tools=['Read'])
    clash = Topology(
        slots=(
            AgentSlot(
                name='bad',
                agent='Role',
                cardinality='singleton',
                boot='lazy',
                extra_tools=('Read',),
            ),
        )
    )
    runtime = Runtime()
    try:
        await _assemble(runtime, clash, catalog, tools=_tools())
        with pytest.raises(ConfigError, match='增强不得替换'):
            await runtime.occupy('bad')
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


async def test_extra_tools_union_keeps_the_role_tool_and_adds_the_new_one() -> None:
    catalog = _catalog('Role', tools=['Read'], budget={'turns': 10})
    topology = Topology(
        slots=(
            AgentSlot(
                name='one',
                agent='Role',
                cardinality='singleton',
                boot='lazy',
                extra_tools=('Grep',),
                budget={'turns': 4},
            ),
        )
    )
    runtime = Runtime()
    try:
        await _assemble(runtime, topology, catalog, tools=_tools())
        agent_id = await runtime.occupy('one')
        agent = runtime._table.get(agent_id)
        assert agent is not None
        assert set(agent.options.tools) == {'Read', 'Grep'}
        assert agent.options.max_turns == 4
        fresh_factory = runtime.agent_factory('Role')
        assert fresh_factory is not None
        fresh = fresh_factory(runtime.hooks, runtime.events)
        assert set(fresh.options.tools) == {'Read'}
        assert fresh.options.max_turns == 10
        assert fresh.options is not agent.options
    finally:
        await runtime.shutdown()


async def test_slot_budget_refuses_to_loosen_the_role() -> None:
    catalog = _catalog('Role', budget={'turns': 10})
    topology = Topology(
        slots=(
            AgentSlot(
                name='one',
                agent='Role',
                cardinality='singleton',
                boot='lazy',
                budget={'turns': 40},
            ),
        )
    )
    runtime = Runtime()
    try:
        await _assemble(runtime, topology, catalog)
        with pytest.raises(ConfigError, match='只收紧'):
            await runtime.occupy('one')
        assert await runtime.registry.list() == []
        assert catalog.get('Role').budget == {'turns': 10}
    finally:
        await runtime.shutdown()


async def test_slot_budget_null_does_not_erase_a_finite_role_limit() -> None:
    catalog = _catalog('Role', budget={'turns': 10})
    topology = Topology(
        slots=(
            AgentSlot(
                name='one',
                agent='Role',
                cardinality='singleton',
                boot='lazy',
                budget={'turns': None},
            ),
        )
    )
    runtime = Runtime()
    try:
        await _assemble(runtime, topology, catalog)
        with pytest.raises(ConfigError, match='只收紧'):
            await runtime.occupy('one')
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


async def test_equal_budget_is_kept_and_unlimited_role_can_be_tightened() -> None:
    equal = _catalog('Role', budget={'turns': 10})
    same = Topology(
        slots=(
            AgentSlot(
                name='one',
                agent='Role',
                cardinality='singleton',
                boot='lazy',
                budget={'turns': 10},
            ),
        )
    )
    runtime = Runtime()
    try:
        await _assemble(runtime, same, equal)
        agent_id = await runtime.occupy('one')
        agent = runtime._table.get(agent_id)
        assert agent is not None
        assert agent.options.max_turns == 10
    finally:
        await runtime.shutdown()

    unlimited = AgentCatalog()
    unlimited.load_mapping(_body('Role', budget={'turns': None}))
    tighter = Topology(
        slots=(
            AgentSlot(
                name='one',
                agent='Role',
                cardinality='singleton',
                boot='lazy',
                budget={'turns': 4},
            ),
        )
    )
    runtime = Runtime()
    try:
        await _assemble(runtime, tighter, unlimited)
        agent_id = await runtime.occupy('one')
        agent = runtime._table.get(agent_id)
        assert agent is not None
        assert agent.options.max_turns == 4
    finally:
        await runtime.shutdown()


async def test_partial_eager_failure_still_locks_assemble() -> None:
    catalog = AgentCatalog()
    catalog.load_mapping(_body('Ok'))
    catalog.load_mapping(_body('Bad', tools=['Read']))
    topology = Topology(
        slots=(
            AgentSlot(name='first', agent='Ok', cardinality='singleton', boot='eager'),
            AgentSlot(
                name='second',
                agent='Bad',
                cardinality='singleton',
                boot='eager',
                extra_tools=('Read',),
            ),
        )
    )
    runtime = Runtime()
    try:
        with pytest.raises(ConfigError, match='增强不得替换'):
            await _assemble(runtime, topology, catalog, tools=_tools())
        listed = await runtime.registry.list()
        assert [info.name for info in listed] == ['Ok']
        with pytest.raises(ContractViolation, match='只 assemble 一次'):
            await _assemble(runtime, topology, catalog, tools=_tools())
        assert [info.name for info in await runtime.registry.list()] == ['Ok']
    finally:
        await runtime.shutdown()
    catalog = _catalog('Role', budget={'turns': 10})
    topology = Topology(
        slots=(
            AgentSlot(
                name='one',
                agent='Role',
                cardinality='singleton',
                boot='lazy',
                budget={'turns': None},
            ),
        )
    )
    runtime = Runtime()
    try:
        await _assemble(runtime, topology, catalog)
        with pytest.raises(ConfigError, match='只收紧'):
            await runtime.occupy('one')
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


async def test_occupy_copies_metadata_and_merges_artifact_kinds() -> None:
    raw: dict[str, object] = {'note': 'keep'}
    catalog = _catalog('Role', artifact_kinds=['patch'])
    slot = AgentSlot(name='scratch', agent='Role', cardinality='on-demand', metadata=raw)
    raw['note'] = 'changed'
    runtime = Runtime()
    try:
        await _assemble(runtime, Topology(slots=(slot,)), catalog)
        first = await runtime.occupy('scratch')
        second = await runtime.occupy('scratch')
        left = await runtime.find(first)
        right = await runtime.find(second)
        assert left is not None and right is not None
        assert left.metadata['note'] == 'keep'
        assert right.metadata['note'] == 'keep'
        assert left.metadata['artifact_kinds'] == ('patch',)
        assert right.metadata['artifact_kinds'] == ('patch',)
    finally:
        await runtime.shutdown()


async def test_catalog_factory_overrides_a_same_named_runtime_agent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def old(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return _Marker(
            AgentOptions(name='Role', llm=ScriptedLLM([])),
            hooks=hooks,
            events=events,
        )

    catalog = _catalog('Role', artifact_kinds=['note'])
    topology = Topology(
        slots=(AgentSlot(name='root', agent='Role', cardinality='singleton', boot='eager'),)
    )
    runtime = Runtime(agents={'Role': old})
    try:
        with caplog.at_level(logging.INFO):
            await _assemble(runtime, topology, catalog)
        listed = await runtime.registry.list()
        assert [info.agent_type for info in listed] == ['BaseAgent']
        assert 'catalog 覆盖了 Runtime(agents=) 里同名工厂 Role' in caplog.text
    finally:
        await runtime.shutdown()


def test_assemble_does_not_add_runtime_start_or_a_start_method() -> None:
    public = [name for name in dir(Runtime) if not name.startswith('_')]
    assert 'start' not in public
    params = inspect.signature(Runtime.__init__).parameters
    assert tuple(params) == (
        'self',
        'registry',
        'broker',
        'plugins',
        'agents',
        'orchestration',
        'modules',
        'flags',
    )
    source = inspect.getsource(Runtime.assemble)
    assert 'RUNTIME_START' not in source
    tree = ast.parse(inspect.getsource(Runtime))
    methods = [
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    assert 'start' not in methods


async def test_failed_validate_does_not_lock_the_runtime() -> None:
    catalog = AgentCatalog()
    topology = Topology(
        slots=(AgentSlot(name='one', agent='Missing', cardinality='singleton', boot='lazy'),)
    )
    runtime = Runtime()
    try:
        with pytest.raises(ConfigError, match='Missing'):
            await _assemble(runtime, topology, catalog)
        catalog.load_mapping(_body('Missing'))
        await _assemble(runtime, topology, catalog)
        assert await runtime.resolve('one') is None
    finally:
        await runtime.shutdown()


async def test_alias_collision_is_rejected() -> None:
    catalog = _catalog('Role')
    topology = Topology(
        slots=(
            AgentSlot(name='workers:a', agent='Role', cardinality='singleton', boot='lazy'),
            AgentSlot(
                name='workers',
                agent='Role',
                cardinality='keyed',
                key_field='id',
                boot='lazy',
            ),
        )
    )
    runtime = Runtime()
    try:
        await _assemble(runtime, topology, catalog)
        held = await runtime.occupy('workers:a')
        with pytest.raises(ConfigError, match='别名已被占用'):
            await runtime.occupy('workers', key='a')
        assert await runtime.resolve('workers:a') == held
        assert len(await runtime.registry.list()) == 1
    finally:
        await runtime.shutdown()
