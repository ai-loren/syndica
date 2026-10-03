"""示例第三方 Module（`examples/syndica-example-module`）经入口点装上之后的端到端行为（M4 T15）：
装上就有工具、Flag 与 hook，不装什么都看不见，装配失败整体回滚，运行期 hook 的错误按切点分类处置，
`flags list`、Agent 的工具与 trace 的 `module` span 说的是同一份声明，run 离线回放复现。"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
from collections.abc import Callable, Iterator
from importlib.metadata import EntryPoint
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, RunRequest, main
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import (
    AgentOptions,
    BaseAgent,
    ConfigError,
    ContextBlock,
    EventBus,
    FileTraceStore,
    HookResult,
    HooksCenter,
    Junction,
    ModuleHost,
    RuleRedactor,
    Runtime,
    RuntimeModule,
    ScriptedLLM,
    ScriptedTurn,
    SpanKind,
    StopReason,
    ToolCall,
    parse_scenario_document,
)
from syndica.runner.replay import ReplayRunner

_SOURCE = Path(__file__).resolve().parents[2] / 'examples' / 'syndica-example-module' / 'src'


@pytest.fixture
def example(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.syspath_prepend(str(_SOURCE))
    yield importlib.import_module('syndica_example_module')
    sys.modules.pop('syndica_example_module', None)


def _module(example: ModuleType) -> RuntimeModule:
    host = ModuleHost(workspace_root=Path.cwd(), environment={}, redactor=RuleRedactor())
    return example.build(host)


def _greet_llm(name: str = 'Ada') -> ScriptedLLM:
    call = ToolCall(id='g1', name='Greet', arguments=json.dumps({'name': name}))
    return ScriptedLLM([ScriptedTurn(tool_calls=[call]), ScriptedTurn(content='done')])


def _factory(
    llm: ScriptedLLM, captured: list[BaseAgent] | None = None
) -> Callable[[HooksCenter, EventBus], BaseAgent]:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = BaseAgent(AgentOptions(name='Worker', llm=llm), hooks=hooks, events=events)
        if captured is not None:
            captured.append(agent)
        return agent

    return build


async def _run(runtime: Runtime, llm: ScriptedLLM) -> list[ContextBlock]:
    try:
        agent_id = await runtime.spawn(_factory(llm), query='go')
        context = await runtime.wait(agent_id)
    finally:
        await runtime.shutdown()
    return context.current_traj


def _tool_output(trajectory: list[ContextBlock]) -> object:
    [tool] = [block for block in trajectory if block.role == 'tool']
    return tool.content


async def test_an_installed_example_gives_the_agent_its_tool_flag_and_hook(example: ModuleType):
    runtime = Runtime(modules=[_module(example)], flags={'example_greeting': 'hi'})
    greetings = runtime.example_greetings  # pyright: ignore[reportAttributeAccessIssue]

    trajectory = await _run(runtime, _greet_llm())

    assert _tool_output(trajectory) == 'hi, Ada'
    assert greetings.names == ['Ada']  # pyright: ignore[reportUnknownMemberType]


async def test_the_flag_default_applies_when_nothing_overrides_it(example: ModuleType):
    trajectory = await _run(Runtime(modules=[_module(example)]), _greet_llm())

    assert _tool_output(trajectory) == 'hello, Ada'


async def test_without_the_example_its_tool_hook_service_and_flag_are_not_there(
    example: ModuleType,
):
    del example
    runtime = Runtime()
    assert not hasattr(runtime, 'example_greetings')

    trajectory = await _run(runtime, _greet_llm())

    assert 'Greet' in str(_tool_output(trajectory))
    assert 'hello' not in str(_tool_output(trajectory))
    with pytest.raises(ConfigError) as excinfo:
        Runtime(flags={'example_greeting': 'hi'})
    assert 'example_greeting' in excinfo.value.received


async def test_a_second_module_whose_hook_fails_rolls_back_the_examples_hook(
    example: ModuleType,
):
    class _Explodes:
        name = 'broken-hooks'

        def register(self, hooks: HooksCenter) -> None:
            raise RuntimeError('broken plugin')

    broken = RuntimeModule('broken', agent_plugins=(lambda *_: _Explodes(),))
    runtime = Runtime(modules=[_module(example), broken])
    captured: list[BaseAgent] = []
    try:
        with pytest.raises(RuntimeError, match='broken plugin'):
            await runtime.spawn(_factory(_greet_llm(), captured), query='go')
        await captured[0].trigger_extension(example.GREETED, name='Ada')
        assert runtime.example_greetings.names == []  # pyright: ignore[reportAttributeAccessIssue]
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


class _Plugin:
    def __init__(self, name: str, register: Callable[[HooksCenter], None]) -> None:
        self._name = name
        self._register = register

    @property
    def name(self) -> str:
        return self._name

    def register(self, hooks: HooksCenter) -> None:
        self._register(hooks)


async def _raises(**_: Any) -> HookResult:
    raise RuntimeError('hook failed')


async def test_a_hook_that_raises_on_the_observable_junction_is_logged_and_the_run_completes(
    example: ModuleType, caplog: pytest.LogCaptureFixture
):
    noisy = _Plugin('noisy', lambda hooks: hooks.register(example.GREETED, _raises))
    runtime = Runtime(plugins=[noisy], modules=[_module(example)])
    greetings = runtime.example_greetings  # pyright: ignore[reportAttributeAccessIssue]

    with caplog.at_level(logging.ERROR):
        trajectory = await _run(runtime, _greet_llm())

    assert _tool_output(trajectory) == 'hello, Ada'
    assert greetings.names == ['Ada']  # pyright: ignore[reportUnknownMemberType]
    assert any('example:greeted' in record.getMessage() for record in caplog.records)


async def test_a_hook_that_raises_on_an_interceptable_junction_stops_the_run(
    example: ModuleType,
):
    strict = _Plugin('strict', lambda hooks: hooks.register(Junction.PRE_TOOL_USE, _raises))
    runtime = Runtime(plugins=[strict], modules=[_module(example)])
    greetings = runtime.example_greetings  # pyright: ignore[reportAttributeAccessIssue]
    try:
        agent_id = await runtime.spawn(_factory(_greet_llm()), query='go')
        context = await runtime.wait(agent_id)
    finally:
        await runtime.shutdown()

    assert context.stop_reason is StopReason.ERROR
    assert greetings.names == []  # pyright: ignore[reportUnknownMemberType]


def _entry_points() -> list[EntryPoint]:
    return [
        EntryPoint(name='example', value='syndica_example_module:build', group='syndica.modules')
    ]


def _cli(workspace: Path, argv: list[str], llm: ScriptedLLM | None = None) -> tuple[int, str]:
    io = CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO())
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=io,
        model_factory=lambda _: llm or ScriptedLLM([]),
        workspace_root=workspace,
        environ={'PATH': '/usr/bin:/bin', 'HOME': str(workspace.parent)},
        entry_points=cast(Any, _entry_points),
    )
    try:
        if argv[0] == 'run':
            host.run_request(RunRequest(prompt='go', config='.syndica.yaml', trace='replay'))
            code = 0
        else:
            code = main(argv, host=host, io=io)
    finally:
        host.close()
    return code, io.stdout.getvalue()  # pyright: ignore[reportAttributeAccessIssue]


def test_flags_list_the_agents_tools_and_the_trace_name_the_same_declaration_and_it_replays(
    example: ModuleType, tmp_path: Path
):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / '.syndica.yaml').write_text(
        'version: 1\nmodules: [example]\nflags:\n  example_greeting: hi\n', encoding='utf-8'
    )

    code, listed = _cli(workspace, ['flags', 'list'])
    _cli(workspace, ['run'], _greet_llm())

    assert code == 0
    [row] = [line for line in listed.splitlines() if line.startswith('example_greeting\t')]
    assert row.split('\t')[1] == 'example'
    store = FileTraceStore(workspace / '.syndica' / 'traces')

    async def read() -> tuple[list[Any], str | None]:
        [header] = await store.traces()
        return list(await store.spans(header.trace_id)), await store.replay(header.trace_id)

    spans, text = asyncio.run(read())
    modules = [span for span in spans if span.kind is SpanKind.MODULE]
    assert [span.attributes['module.name'] for span in modules] == [
        'safety',
        'human-interaction',
        'example',
        'dev',
    ]
    module = modules[2]
    assert module.attributes['module.version'] == '0.1.0'
    assert 'Greet' in str(module.attributes['module.tools'])
    assert 'example_greeting' in str(module.attributes['module.flags'])
    assert text is not None
    document = parse_scenario_document(json.loads(text), source='replay.json')
    result = ReplayRunner(modules=lambda record, host: example.build(host)).run(document)
    assert result.reproduced, result.divergences
