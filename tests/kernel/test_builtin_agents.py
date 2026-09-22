"""四个内置角色：Orchestrator 自己装编排工具，描述在 freeze 时从目录现读。"""

from __future__ import annotations

import pytest

from syndica.kernel.agent import BaseAgent
from syndica.kernel.agents import Orchestrator, register_builtins
from syndica.kernel.catalog import AgentCatalog
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import ScriptedLLM
from syndica.kernel.runtime import Runtime
from syndica.kernel.tool import Tool, ToolResult

_BUILTIN_WORKER = '改文件、跑命令、验证结果时用我'
_CUSTOM_WORKER = 'CUSTOM-WORKER-ONLY'


def _stub(name: str) -> Tool:
    cls = type(
        f'Stub{name}',
        (_Stub,),
        {'__tool_name__': name, '__tool_command_args__': ()},
    )
    return cls()


class _Stub(Tool):
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        return ToolResult(output=self.name)


def _stubs(catalog: AgentCatalog) -> dict[str, Tool]:
    table: dict[str, Tool] = {}
    for role in catalog.names():
        for name in catalog.get(role).tools:
            table.setdefault(name, _stub(name))
    return table


def _loaded() -> AgentCatalog:
    catalog = AgentCatalog()
    register_builtins(catalog)
    return catalog


def test_builtin_names_are_orchestrator_planner_explorer_worker() -> None:
    catalog = _loaded()
    assert catalog.names() == ('Planner', 'Explorer', 'Worker', 'Orchestrator')
    assert catalog.get('Planner').tools == ('Read', 'Grep', 'Glob')
    assert catalog.get('Explorer').tools == ('Read', 'Grep', 'Glob')
    assert catalog.get('Worker').tools == ('Read', 'Write', 'Edit', 'Bash')
    assert catalog.get('Orchestrator').tools == ()
    assert catalog.get('Orchestrator').subagents == ('Planner', 'Explorer', 'Worker')
    assert catalog.get('Orchestrator').impl == 'syndica.kernel.agents.orchestrator:Orchestrator'
    assert catalog.get('Orchestrator').description == (
        '把工作拆给 Planner、Explorer、Worker，自己不改文件'
    )


def test_orchestrator_is_class_based_and_the_others_are_declarative() -> None:
    catalog = _loaded()
    assert catalog.get('Orchestrator').impl is not None
    for name in ('Planner', 'Explorer', 'Worker'):
        assert catalog.get(name).impl is None
    factories = catalog.freeze(_stubs(catalog), {'default': ScriptedLLM([])})
    orchestrator = factories['Orchestrator'](HooksCenter(), EventBus())
    worker = factories['Worker'](HooksCenter(), EventBus())
    assert type(orchestrator) is Orchestrator
    assert type(worker) is BaseAgent


def test_declarative_system_prompts_are_what_freeze_hands_the_agent() -> None:
    catalog = _loaded()
    expected = {
        'Planner': '先划定范围、决策、依赖和成功判据，再把执行交给别人。',
        'Explorer': '只定位文件、符号和引用。不要改文件，也不要跑会改状态的命令。',
        'Worker': '改文件、跑命令，并用结果验证。目录里没有这些工具时 freeze 会失败。',
    }
    factories = catalog.freeze(_stubs(catalog), {'default': ScriptedLLM([])})
    for name, prompt in expected.items():
        assert catalog.get(name).system_prompt == prompt
        agent = factories[name](HooksCenter(), EventBus())
        assert agent.options.system_prompt == prompt


def test_install_own_tools_rejects_a_non_runtime() -> None:
    catalog = _loaded()
    factories = catalog.freeze(_stubs(catalog), {'default': ScriptedLLM([])})
    orchestrator = factories['Orchestrator'](HooksCenter(), EventBus())
    with pytest.raises(ConfigError, match='Runtime'):
        orchestrator.install_own_tools(object())


def test_planner_explorer_worker_descriptions_say_when_to_use_them() -> None:
    catalog = _loaded()
    assert '规划范围 / 决策 / 依赖 / 成功判据时用我' in catalog.get('Planner').description
    assert '定位文件、符号、引用时用我，不要用我改文件' in catalog.get('Explorer').description
    assert _BUILTIN_WORKER in catalog.get('Worker').description


def test_user_mapping_overrides_worker_and_overrides_is_queryable() -> None:
    catalog = _loaded()
    catalog.load_mapping(
        {
            'name': 'Worker',
            'description': _CUSTOM_WORKER,
            'tools': ['Read'],
        }
    )
    records = catalog.overrides('Worker')
    assert len(records) == 1
    assert records[0].displaced_source.kind == 'builtin'
    assert records[0].by_source.kind == 'mapping'
    assert catalog.get('Worker').description == _CUSTOM_WORKER
    factories = catalog.freeze(_stubs(catalog), {'default': ScriptedLLM([])})
    orchestrator = factories['Orchestrator'](HooksCenter(), EventBus())
    prompt = orchestrator.options.system_prompt
    assert _CUSTOM_WORKER in prompt
    assert _BUILTIN_WORKER not in prompt


def test_register_builtins_twice_records_a_builtin_override() -> None:
    catalog = _loaded()
    register_builtins(catalog)
    records = catalog.overrides('Planner')
    assert len(records) == 1
    assert records[0].displaced_source.kind == 'builtin'
    assert records[0].by_source.kind == 'builtin'
    assert catalog.names() == ('Planner', 'Explorer', 'Worker', 'Orchestrator')


def test_register_builtins_rejects_a_duck_typed_catalog() -> None:
    class _Duck:
        def register_builtin(self, data: object, *, location: object) -> None:
            del data, location

    with pytest.raises(ConfigError, match='AgentCatalog'):
        register_builtins(_Duck())


async def test_orchestrator_factory_installs_orchestration_tools_when_runtime_asks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _loaded()
    factories = catalog.freeze(_stubs(catalog), {'default': ScriptedLLM([])})
    installs: list[str] = []
    real = BaseAgent.install_runtime_tools

    def _counted(self: BaseAgent, tools: dict[str, Tool]) -> None:
        installs.append(self.options.name)
        real(self, tools)

    monkeypatch.setattr(BaseAgent, 'install_runtime_tools', _counted)
    quiet = Runtime(agents=factories, orchestration=False)
    try:
        orchestrator_id = await quiet.spawn(factories['Orchestrator'])
        worker_id = await quiet.spawn(factories['Worker'])
        orchestrator = quiet._table.get(orchestrator_id)
        worker = quiet._table.get(worker_id)
        assert orchestrator is not None and 'Delegate' in orchestrator.options.tools
        assert worker is not None and 'Delegate' not in worker.options.tools
        assert 'Read' in worker.options.tools
        assert installs == ['Orchestrator']
    finally:
        await quiet.shutdown()

    installs.clear()
    flagged = Runtime(agents=factories, orchestration=True)
    try:
        orchestrator_id = await flagged.spawn(factories['Orchestrator'])
        worker_id = await flagged.spawn(factories['Worker'])
        orchestrator = flagged._table.get(orchestrator_id)
        worker = flagged._table.get(worker_id)
        assert orchestrator is not None and 'Delegate' in orchestrator.options.tools
        assert worker is not None and 'Delegate' in worker.options.tools
        assert installs == ['Orchestrator', 'Worker']
    finally:
        await flagged.shutdown()
