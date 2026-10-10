"""第三方经 Runtime Module 装产物处理器：入口点交回的 `RuntimeModule(artifact_handlers=...)`
由宿主装进它那份注册表，L1 验收、`Deliver` 收的类别、服务面的预览与 diff 都查它；冲突按注册表的
规则报错。"""

from __future__ import annotations

import importlib
import json
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
from syndica.cli.modules import runs_code
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import (
    ArtifactRef,
    ConfigError,
    ContextBlock,
    ModuleHost,
    RuleRedactor,
    RuntimeModule,
    ScriptedLLM,
    ScriptedTurn,
    ToolCall,
)

_FIXTURES = Path(__file__).resolve().parent / 'fixtures'
_POINT = EntryPoint(name='table-check', value='syndica_table_module:build', group='syndica.modules')


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.syspath_prepend(str(_FIXTURES))
    yield importlib.import_module('syndica_table_module')
    sys.modules.pop('syndica_table_module', None)


def _host(
    workspace: Path, llm: ScriptedLLM, points: Callable[[], list[EntryPoint]], stdout: StringIO
) -> RuntimeCliHost:
    return RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=stdout, stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=workspace,
        environ={'PATH': '/usr/bin:/bin', 'HOME': str(workspace.parent)},
        entry_points=cast(Any, points),
    )


def _workspace(tmp_path: Path, modules: str) -> Path:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / '.syndica.yaml').write_text(
        f'version: 1\nmodules: [{modules}]\n', encoding='utf-8'
    )
    return workspace


def _call(name: str, call_id: str, **arguments: object) -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))]
    )


def _request() -> RunRequest:
    return RunRequest(prompt='write the table', config='.syndica.yaml', agent='Worker')


def _seen(llm: ScriptedLLM) -> str:
    blocks: list[ContextBlock] = [block for call in llm.calls for block in call]
    return '\n'.join(str(block.content) for block in blocks)


def _csv_ref(kind: str) -> ArtifactRef:
    return ArtifactRef(
        id='file:data.csv', path='data.csv', kind=kind, mime='text/csv', version='v', producer='a'
    )


def test_a_module_handler_validates_csv_deliverables_and_its_kind_is_accepted(
    table: ModuleType, tmp_path: Path
):
    workspace = _workspace(tmp_path, 'acceptance, table-check')
    llm = ScriptedLLM(
        [
            _call('Write', 'c1', path='data.csv', content='a,b\n1\n'),
            _call('Deliver', 'c2', path='data.csv', kind='table'),
            ScriptedTurn(content='done'),
            _call('Write', 'c3', path='data.csv', content='a,b\n1,2\n'),
            _call('Deliver', 'c4', path='data.csv', kind='table'),
            ScriptedTurn(content='done again'),
        ]
    )
    host = _host(workspace, llm, lambda: [_POINT], StringIO())
    try:
        answer = host.run_request(_request())
    finally:
        host.close()

    seen = _seen(llm)
    assert '已标记交付物：data.csv' in seen
    assert 'row 2 has 1 columns, expected 2（table.ragged）' in seen
    assert answer == 'done again'
    assert isinstance(host.artifact_handlers.for_(_csv_ref('spreadsheet')), table.TableHandler)
    assert isinstance(host.artifact_handlers.for_(_csv_ref('table')), table.TableHandler)


def test_without_the_module_the_table_kind_is_refused(table: ModuleType, tmp_path: Path):
    del table
    workspace = _workspace(tmp_path, 'acceptance')
    llm = ScriptedLLM(
        [
            _call('Write', 'c1', path='data.csv', content='a,b\n1,2\n'),
            _call('Deliver', 'c2', path='data.csv', kind='table'),
            ScriptedTurn(content='done'),
        ]
    )
    host = _host(workspace, llm, lambda: [_POINT], StringIO())
    try:
        host.run_request(_request())
    finally:
        host.close()

    assert "kind='table' 不认识" in _seen(llm)
    assert 'table' not in host.artifact_handlers.kinds()


def test_one_host_running_twice_installs_a_modules_handlers_once(table: ModuleType, tmp_path: Path):
    workspace = _workspace(tmp_path, 'table-check')
    llm = ScriptedLLM([ScriptedTurn(content='one'), ScriptedTurn(content='two')])
    host = _host(workspace, llm, lambda: [_POINT], StringIO())
    try:
        answers = [host.run_request(_request()), host.run_request(_request())]
    finally:
        host.close()

    assert answers == ['one', 'two']
    assert isinstance(host.artifact_handlers.for_(_csv_ref('spreadsheet')), table.TableHandler)


def test_two_modules_claiming_the_same_kind_and_mime_are_refused_naming_both(
    table: ModuleType, tmp_path: Path
):
    class RivalHandler(table.TableHandler):
        handles = (('spreadsheet', 'text/csv'),)

    def rival(host: ModuleHost) -> RuntimeModule:
        del host
        return RuntimeModule('rival', artifact_handlers=(RivalHandler(),))

    class _Rival:
        name = 'rival'
        value = 'rival_pkg:build'
        dist = None

        def load(self) -> object:
            return rival

    workspace = _workspace(tmp_path, 'table-check, rival')
    host = _host(workspace, ScriptedLLM([]), lambda: [_POINT, cast(Any, _Rival())], StringIO())
    try:
        with pytest.raises(ConfigError) as caught:
            host.run_request(_request())
    finally:
        host.close()

    assert "('spreadsheet', 'text/csv')" in caught.value.expected
    assert 'TableHandler' in caught.value.received
    assert 'RivalHandler' in caught.value.received
    assert "Module 'rival'" in caught.value.received


def test_modules_show_lists_the_kinds_a_module_handles(table: ModuleType, tmp_path: Path):
    del table
    workspace = _workspace(tmp_path, 'table-check')
    stdout = StringIO()
    io = CliIO(stdin=StringIO(), stdout=stdout, stderr=StringIO())
    host = _host(workspace, ScriptedLLM([]), lambda: [_POINT], stdout)
    try:
        code = main(
            ['--config', str(workspace / '.syndica.yaml'), 'modules', 'show', 'table-check'],
            host=host,
            io=io,
        )
    finally:
        host.close()

    assert code == 0
    assert json.loads(stdout.getvalue())['artifact_handlers'] == [
        ['spreadsheet', 'text/csv'],
        ['table', 'text/csv'],
    ]


def test_a_module_that_only_contributes_handlers_runs_code(table: ModuleType):
    module = table.build(
        ModuleHost(workspace_root=Path('.'), environment={}, redactor=RuleRedactor())
    )
    assert runs_code(module)
    assert not runs_code(RuntimeModule('flags-only'))
