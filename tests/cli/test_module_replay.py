"""第三方 Module 的 run 回放（M4 D13，T14）：捕获时判回放得了吗，回放时按当前工作区判重建不重建，
检查次序是承重的——import 第三方代码排在信任、启用与版本之后。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest

import syndica.cli.trust as trust_module
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, RunRequest
from syndica.cli.modules import discover, installed_entry_points
from syndica.cli.replay_modules import module_rebuilder
from syndica.cli.trust import NO_TRUST_GATE, STORE_ENV, JsonTrustStore, read_snapshot
from syndica.kernel import (
    FileTraceStore,
    FlagDeclaration,
    ModuleHost,
    RuleRedactor,
    RuntimeModule,
    ScriptedLLM,
    ScriptedTurn,
    Tool,
    ToolCall,
    ToolResult,
    TrustLevel,
    parse_scenario_document,
)
from syndica.kernel.scenario_schema import ModuleRecord
from syndica.runner.replay import ModulesNotRebuilt, ReplayRunner


class _Echo(Tool):
    __tool_command_args__ = ()

    async def execute(self, text: str = '') -> ToolResult:
        return ToolResult(output=f'echo {text}')


class _Dist:
    def __init__(self, name: str = 'alpha-dist', version: str = '1.0') -> None:
        self.metadata = {'Name': name}
        self.version = version
        self.files = None

    def locate_file(self, path: object) -> str:
        raise AssertionError(f'not installed in the workspace: {path}')


class _EntryPoint:
    def __init__(self, build: Callable[[ModuleHost], RuntimeModule], dist: _Dist) -> None:
        self.name = 'alpha'
        self.value = 'alpha_pkg.module:build'
        self.dist = dist
        self._build = build
        self.loads = 0

    def load(self) -> object:
        self.loads += 1
        return self._build


def _module(
    *,
    replay_safe: bool = True,
    tools: bool = True,
    on_start: bool = False,
    version: str = '2.0.0',
    requires: Sequence[str] = (),
) -> Callable[[ModuleHost], RuntimeModule]:
    def build(host: ModuleHost) -> RuntimeModule:
        del host
        return RuntimeModule(
            'alpha',
            version=version,
            tool_factories=((lambda *_: {'Echo': _Echo()}),) if tools else (),
            on_start=(lambda runtime, flags: None) if on_start else None,
            flags=(FlagDeclaration(key='alpha_level', default=1, description='级别'),),
            replay_safe=replay_safe,
            requires=requires,
        )

    return build


def _echo_llm() -> ScriptedLLM:
    return ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[ToolCall(id='e1', name='Echo', arguments=json.dumps({'text': 'hi'}))]
            ),
            ScriptedTurn(content='done'),
        ]
    )


def _capture(
    tmp_path: Path, point: _EntryPoint, *, body: str = '', **request: Any
) -> tuple[str, str]:
    workspace = tmp_path / 'ws'
    workspace.mkdir(exist_ok=True)
    config = workspace / '.syndica.yaml'
    config.write_text(f'version: 1\nmodules: [alpha]\n{body}', encoding='utf-8')
    stderr = StringIO()
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr),
        model_factory=lambda _: _echo_llm(),
        workspace_root=workspace,
        environ={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path)},
        entry_points=lambda: [point],
    )
    try:
        host.run_request(RunRequest(prompt='go', config=str(config), trace='replay', **request))
    finally:
        host.close()
    return _document_text(workspace), stderr.getvalue()


def _document_text(workspace: Path) -> str:
    store = FileTraceStore(workspace / '.syndica' / 'traces')

    async def read() -> str:
        [header] = await store.traces()
        text = await store.replay(header.trace_id)
        return '' if text is None else text

    return asyncio.run(read())


def test_a_replay_safe_module_is_recorded_in_a_version_3_document(tmp_path: Path):
    text, _ = _capture(tmp_path, _EntryPoint(_module(), _Dist()))
    document = json.loads(text)
    assert document['version'] == 3
    assert document['host']['module_records'] == [
        {
            'name': 'alpha',
            'module_version': '2.0.0',
            'distribution': 'alpha-dist',
            'distribution_version': '1.0',
        }
    ]
    assert document['host']['untrusted_output_modules'] == ['alpha']


@pytest.mark.parametrize(
    ('options', 'written'),
    [
        ({'replay_safe': False}, False),
        ({'replay_safe': False, 'tools': False, 'on_start': True}, False),
        ({'replay_safe': False, 'tools': False}, True),
    ],
    ids=['tools-not-safe', 'on-start-only', 'flags-only'],
)
def test_whether_a_module_blocks_the_document_depends_on_code_it_runs(
    tmp_path: Path, options: dict[str, bool], written: bool
):
    text, stderr = _capture(tmp_path, _EntryPoint(_module(**cast(Any, options)), _Dist()))
    assert (text != '') is written
    if not written:
        assert '没声明 replay_safe' in stderr


def test_a_redaction_rule_that_would_rename_the_module_blocks_the_document(tmp_path: Path):
    body = "redaction:\n  rules:\n    - {id: custom.dist, pattern: 'alpha-dist'}\n"
    text, stderr = _capture(tmp_path, _EntryPoint(_module(), _Dist()), body=body)
    assert text == ''
    assert '回放认不出该重建哪一个' in stderr


_RECORD = ModuleRecord(
    name='alpha', module_version='2.0.0', distribution='alpha-dist', distribution_version='1.0'
)


def _workspace(tmp_path: Path, modules: str = '[alpha]') -> Path:
    workspace = tmp_path / 'ws'
    workspace.mkdir(exist_ok=True)
    (workspace / '.syndica.yaml').write_text(f'version: 1\nmodules: {modules}\n', encoding='utf-8')
    return workspace


def _trust(workspace: Path, store: Path, point: _EntryPoint) -> None:
    snapshot = read_snapshot(workspace / '.syndica.yaml', workspace, discover(lambda: [point]))
    JsonTrustStore(store).put(snapshot.path, snapshot.digest)


def _rebuild(
    workspace: Path, store: Path, point: _EntryPoint, record: ModuleRecord = _RECORD
) -> RuntimeModule | str:
    rebuild = module_rebuilder(
        workspace_root=workspace, environ={STORE_ENV: str(store)}, entry_points=lambda: [point]
    )
    host = ModuleHost(workspace_root=workspace, environment={}, redactor=RuleRedactor())
    return rebuild(record, host)


def test_an_untrusted_workspace_rebuilds_nothing_and_imports_nothing(tmp_path: Path):
    workspace = _workspace(tmp_path)
    point = _EntryPoint(_module(), _Dist())
    outcome = _rebuild(workspace, tmp_path / 'store.json', point)
    assert isinstance(outcome, str) and 'syndica config trust' in outcome
    assert point.loads == 0


def test_a_trusted_config_that_does_not_enable_the_module_imports_nothing(tmp_path: Path):
    workspace = _workspace(tmp_path, modules='[]')
    point = _EntryPoint(_module(), _Dist())
    _trust(workspace, tmp_path / 'store.json', point)
    outcome = _rebuild(workspace, tmp_path / 'store.json', point)
    assert isinstance(outcome, str) and 'modules 里写上它' in outcome
    assert point.loads == 0


def test_a_different_installed_version_imports_nothing(tmp_path: Path):
    workspace = _workspace(tmp_path)
    point = _EntryPoint(_module(), _Dist(version='1.1'))
    _trust(workspace, tmp_path / 'store.json', point)
    outcome = _rebuild(workspace, tmp_path / 'store.json', point)
    assert isinstance(outcome, str) and 'alpha-dist 1.1' in outcome
    assert '装上记录里的版本' in outcome
    assert point.loads == 0


@pytest.mark.parametrize(
    ('build', 'anchor'),
    [
        (_module(replay_safe=False), '请作者确认它不做 I/O'),
        (_module(version='3.0.0'), '装上记录时的那个版本'),
    ],
    ids=['not-replay-safe', 'module-version'],
)
def test_the_installed_module_itself_decides_after_the_import(
    tmp_path: Path, build: Callable[[ModuleHost], RuntimeModule], anchor: str
):
    workspace = _workspace(tmp_path)
    point = _EntryPoint(build, _Dist())
    _trust(workspace, tmp_path / 'store.json', point)
    outcome = _rebuild(workspace, tmp_path / 'store.json', point)
    assert isinstance(outcome, str) and anchor in outcome
    assert point.loads == 1


def test_a_trusted_enabled_matching_replay_safe_module_is_rebuilt(tmp_path: Path):
    workspace = _workspace(tmp_path)
    point = _EntryPoint(_module(), _Dist())
    _trust(workspace, tmp_path / 'store.json', point)
    outcome = _rebuild(workspace, tmp_path / 'store.json', point)
    assert isinstance(outcome, RuntimeModule) and outcome.name == 'alpha'


def _replayed(text: str, rebuild: Callable[[ModuleRecord, ModuleHost], RuntimeModule | str]) -> Any:
    document = parse_scenario_document(json.loads(text), source='replay.json')
    return ReplayRunner(modules=rebuild).run(document)


def test_a_captured_run_with_a_rebuilt_module_replays_its_untrusted_results(tmp_path: Path):
    text, _ = _capture(tmp_path, _EntryPoint(_module(), _Dist()))

    result = _replayed(text, lambda record, host: _module()(host))

    assert result.reproduced, result.divergences
    [trajectory] = result.trajectories.values()
    [tool] = [block for block in trajectory if block.role == 'tool']
    assert tool.trust is TrustLevel.UNTRUSTED


def test_a_third_party_module_the_replay_does_not_rebuild_makes_the_run_unreplayable(
    tmp_path: Path,
):
    text, _ = _capture(tmp_path, _EntryPoint(_module(), _Dist()))

    with pytest.raises(ModulesNotRebuilt) as excinfo:
        ReplayRunner(modules=lambda record, host: '这份工作区配置还没被信任').run(
            parse_scenario_document(json.loads(text), source='replay.json')
        )

    assert "没有重建 'alpha'：这份工作区配置还没被信任" in excinfo.value.received
    assert '修好当前工作区' in excinfo.value.next_action


def test_a_replay_without_a_rebuilder_refuses_a_document_with_a_third_party_module(
    tmp_path: Path,
):
    text, _ = _capture(tmp_path, _EntryPoint(_module(), _Dist()))

    with pytest.raises(ModulesNotRebuilt) as excinfo:
        ReplayRunner().run(parse_scenario_document(json.loads(text), source='replay.json'))

    assert 'ReplayRunner(modules=...)' in excinfo.value.next_action


def test_a_rebuilt_module_whose_requirement_was_not_rebuilt_stops_the_replay(tmp_path: Path):
    text, _ = _capture(tmp_path, _EntryPoint(_module(), _Dist()))

    with pytest.raises(ModulesNotRebuilt) as excinfo:
        _replayed(text, lambda record, host: _module(requires=('ghost',))(host))

    assert '依赖的 Module 也都重建了' in excinfo.value.expected
    assert "['ghost']" in excinfo.value.received


def test_rebuilding_a_module_whose_code_is_in_the_workspace_pins_it_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = _workspace(tmp_path)
    package = workspace / 'extmod'
    package.mkdir()
    (package / '__init__.py').write_text('', encoding='utf-8')
    monkeypatch.syspath_prepend(str(workspace))
    monkeypatch.setattr(sys, 'pycache_prefix', None)
    point = _EntryPoint(_module(), _Dist())
    point.value = 'extmod:build'
    _trust(workspace, tmp_path / 'store.json', point)
    active = list(trust_module._ACTIVE)  # pyright: ignore[reportPrivateUsage]

    outcome = _rebuild(workspace, tmp_path / 'store.json', point)

    added = [pins for pins in trust_module._ACTIVE if pins not in active]  # pyright: ignore[reportPrivateUsage]
    try:
        assert isinstance(outcome, RuntimeModule)
        assert sys.pycache_prefix is not None
        assert not Path(sys.pycache_prefix).resolve().is_relative_to(workspace.resolve())
        assert len(added) == 1
    finally:
        for pins in added:
            pins.remove()


def test_a_trust_store_inside_the_workspace_rebuilds_nothing_and_imports_nothing(tmp_path: Path):
    workspace = _workspace(tmp_path)
    point = _EntryPoint(_module(), _Dist())
    inside = workspace / '.config' / 'trusted.json'
    _trust(workspace, inside, point)

    outcome = _rebuild(workspace, inside, point)

    assert isinstance(outcome, str) and '在工作区之内' in outcome
    assert 'XDG_CONFIG_HOME' in outcome
    assert point.loads == 0


def test_the_refusal_names_a_rule_matched_module_by_placeholder_not_by_value(tmp_path: Path):
    body = "redaction:\n  rules:\n    - {id: custom.codename, pattern: 'acme-[a-z]+'}\n"
    dist = _Dist(name='acme-codename')

    text, stderr = _capture(tmp_path, _EntryPoint(_module(), dist), body=body)

    assert text == ''
    assert 'acme-codename' not in stderr
    assert '[REDACTED:custom.codename]' in stderr


def test_a_config_changed_after_trust_imports_nothing(tmp_path: Path):
    workspace = _workspace(tmp_path)
    point = _EntryPoint(_module(), _Dist())
    _trust(workspace, tmp_path / 'store.json', point)
    config = workspace / '.syndica.yaml'
    config.write_text(config.read_text(encoding='utf-8') + '# edited\n', encoding='utf-8')

    outcome = _rebuild(workspace, tmp_path / 'store.json', point)

    assert isinstance(outcome, str) and '信任之后改过' in outcome
    assert point.loads == 0


def test_an_ambiguous_entry_point_imports_nothing(tmp_path: Path):
    workspace = _workspace(tmp_path)
    first = _EntryPoint(_module(), _Dist())
    second = _EntryPoint(_module(), _Dist(name='other-dist'))
    rebuild = module_rebuilder(
        workspace_root=workspace,
        environ={STORE_ENV: str(tmp_path / 'store.json')},
        entry_points=lambda: [first, second],
    )
    snapshot = read_snapshot(
        workspace / '.syndica.yaml', workspace, discover(lambda: [first, second])
    )
    JsonTrustStore(tmp_path / 'store.json').put(snapshot.path, snapshot.digest)

    outcome = rebuild(
        _RECORD, ModuleHost(workspace_root=workspace, environment={}, redactor=RuleRedactor())
    )

    assert isinstance(outcome, str) and '有歧义' in outcome
    assert (first.loads, second.loads) == (0, 0)


def test_a_record_without_a_distribution_does_not_match_an_installed_one(tmp_path: Path):
    workspace = _workspace(tmp_path)
    point = _EntryPoint(_module(), _Dist())
    _trust(workspace, tmp_path / 'store.json', point)
    bare = ModuleRecord(name='alpha', module_version='2.0.0')

    outcome = _rebuild(workspace, tmp_path / 'store.json', point, bare)

    assert isinstance(outcome, str) and '装着的是 alpha-dist 1.0' in outcome
    assert point.loads == 0


@pytest.mark.parametrize('field', ['services', 'on_shutdown', 'agent_plugins'])
def test_each_kind_of_code_a_module_runs_blocks_the_document_unless_replay_safe(
    tmp_path: Path, field: str
):
    async def shutdown(runtime: Any) -> None:
        del runtime

    class _Quiet:
        name = 'alpha-hooks'

        def register(self, hooks: Any) -> None:
            del hooks

    pieces: dict[str, Any] = {
        'services': {'alpha_state': lambda *_: object()},
        'on_shutdown': shutdown,
        'agent_plugins': (lambda *_: _Quiet(),),
    }

    def build(host: ModuleHost) -> RuntimeModule:
        del host
        return RuntimeModule('alpha', version='2.0.0', **{field: pieces[field]})

    text, stderr = _capture(tmp_path, _EntryPoint(build, _Dist()))

    assert text == ''
    assert '没声明 replay_safe' in stderr


def test_a_module_whose_output_the_user_trusted_is_still_a_version_3_record(tmp_path: Path):
    text, _ = _capture(tmp_path, _EntryPoint(_module(), _Dist()), trust_module_output=('alpha',))
    document = json.loads(text)

    assert document['version'] == 3
    assert [record['name'] for record in document['host']['module_records']] == ['alpha']
    assert 'untrusted_output_modules' not in document['host']
    result = _replayed(text, lambda record, host: _module()(host))
    assert result.reproduced, result.divergences


_SITE_MODULE = """from syndica.kernel import RuntimeModule, Tool, ToolResult


class Echo(Tool):
    __tool_command_args__ = ()

    async def execute(self, text: str = '') -> ToolResult:
        return ToolResult(output=f'echo {text}')


def build(host):
    return RuntimeModule(
        'alpha',
        version='2.0.0',
        tool_factories=((lambda *_: {'Echo': Echo()}),),
        replay_safe=True,
    )
"""


def _installed_site(tmp_path: Path) -> Path:
    site = tmp_path / 'site'
    (site / 'alpha_pkg').mkdir(parents=True)
    (site / 'alpha_pkg' / '__init__.py').write_text('', encoding='utf-8')
    (site / 'alpha_pkg' / 'module.py').write_text(_SITE_MODULE, encoding='utf-8')
    info = site / 'alpha_dist-1.0.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text(
        'Metadata-Version: 2.1\nName: alpha-dist\nVersion: 1.0\n', encoding='utf-8'
    )
    (info / 'entry_points.txt').write_text(
        '[syndica.modules]\nalpha = alpha_pkg.module:build\n', encoding='utf-8'
    )
    return site


@pytest.fixture
def _forget_alpha_pkg() -> Iterator[None]:
    yield
    for name in [name for name in sys.modules if name.split('.')[0] == 'alpha_pkg']:
        del sys.modules[name]


@pytest.mark.usefixtures('_forget_alpha_pkg')
def test_trace_replay_through_the_command_line_rebuilds_a_module_only_while_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    site = _installed_site(tmp_path)
    monkeypatch.syspath_prepend(str(site))
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    config = workspace / '.syndica.yaml'
    config.write_text('version: 1\nmodules: [alpha]\n', encoding='utf-8')
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: _echo_llm(),
        workspace_root=workspace,
        environ={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path)},
        entry_points=installed_entry_points,
    )
    try:
        host.run_request(RunRequest(prompt='go', config='.syndica.yaml', trace='replay'))
    finally:
        host.close()
    store = tmp_path / 'xdg' / 'syndica' / 'trusted.json'
    snapshot = read_snapshot(config, workspace, discover(installed_entry_points))
    JsonTrustStore(store).put(snapshot.path, snapshot.digest)

    async def trace_id() -> str:
        [header] = await FileTraceStore(workspace / '.syndica' / 'traces').traces()
        return header.trace_id

    given = asyncio.run(trace_id())[:8]
    environ = {
        'PATH': '/usr/bin:/bin',
        'HOME': str(tmp_path),
        'XDG_CONFIG_HOME': str(tmp_path / 'xdg'),
        'PYTHONPATH': str(site),
    }

    def replay() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, '-m', 'syndica', 'trace', 'replay', given],
            cwd=workspace,
            env=environ,
            capture_output=True,
            text=True,
            timeout=120,
        )

    trusted = replay()
    store.unlink()
    untrusted = replay()

    assert trusted.returncode == 0, trusted.stderr
    assert '复现' in trusted.stdout
    assert 'alpha' not in trusted.stdout.split('没有重建的宿主 Module 与 Flag')[-1]
    assert untrusted.returncode == 2, untrusted.stderr
    assert "没有重建 'alpha'" in untrusted.stderr
    assert 'syndica config trust' in untrusted.stderr


def test_a_module_that_only_declares_flags_is_captured_and_rebuilt_without_replay_safe(
    tmp_path: Path,
):
    build = _module(replay_safe=False, tools=False)
    text, _ = _capture(tmp_path, _EntryPoint(build, _Dist()))
    workspace = tmp_path / 'ws'
    point = _EntryPoint(build, _Dist())
    store = tmp_path / 'store.json'
    _trust(workspace, store, point)

    rebuild = module_rebuilder(
        workspace_root=workspace, environ={STORE_ENV: str(store)}, entry_points=lambda: [point]
    )
    result = ReplayRunner(modules=rebuild).run(
        parse_scenario_document(json.loads(text), source='replay.json')
    )

    assert result.reproduced, result.divergences
    assert point.loads == 1


def test_replay_rebuilds_every_builtin_module_the_cli_can_install():
    from syndica.cli.modules import MANDATORY, OPTIONAL_BUILTINS
    from syndica.runner import replay as replay_module

    builders = replay_module._builtin_modules(cast(Any, None), '')  # pyright: ignore[reportPrivateUsage]

    assert sorted(builders) == sorted((*MANDATORY, *OPTIONAL_BUILTINS))


def test_a_config_named_outside_the_workspace_rebuilds_without_a_trust_entry(tmp_path: Path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    outside = tmp_path / 'team.yaml'
    outside.write_text('version: 1\nmodules: [alpha]\n', encoding='utf-8')
    point = _EntryPoint(_module(), _Dist())
    rebuild = module_rebuilder(
        workspace_root=workspace, environ={}, entry_points=lambda: [point], config_file=outside
    )

    outcome = rebuild(
        _RECORD, ModuleHost(workspace_root=workspace, environment={}, redactor=RuleRedactor())
    )

    assert isinstance(outcome, RuntimeModule) and outcome.name == 'alpha'


def test_an_untrusted_config_named_inside_the_workspace_is_named_in_the_refusal(tmp_path: Path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    team = workspace / 'team.yaml'
    team.write_text('version: 1\nmodules: [alpha]\n', encoding='utf-8')
    point = _EntryPoint(_module(), _Dist())
    rebuild = module_rebuilder(
        workspace_root=workspace,
        environ={STORE_ENV: str(tmp_path / 'store.json')},
        entry_points=lambda: [point],
        config_file=team,
    )

    outcome = rebuild(
        _RECORD, ModuleHost(workspace_root=workspace, environment={}, redactor=RuleRedactor())
    )

    assert isinstance(outcome, str) and 'team.yaml' in outcome and '还没被信任' in outcome
    assert point.loads == 0
