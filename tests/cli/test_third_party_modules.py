"""启用第三方 Module（M4 D15，T9b）：入口点收 `ModuleHost`、Flag 的键与取值、结果一律不可信直到
用户在命令行上放开、信任摘要盖住它在工作区里的代码。"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Sequence
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

import syndica.cli.host as host_module
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, RunRequest
from syndica.cli.modules import discover
from syndica.cli.trust import NO_TRUST_GATE, read_snapshot
from syndica.kernel import (
    ConfigError,
    FlagDeclaration,
    ModuleHost,
    Runtime,
    RuntimeModule,
    ScriptedLLM,
    ScriptedTurn,
    Tool,
    ToolCall,
    ToolResult,
    TrustLevel,
)


class _Echo(Tool):
    __tool_command_args__ = ()

    async def execute(self, text: str = '') -> ToolResult:
        return ToolResult(output=f'echo {text}')


class _Hash:
    def __init__(self, data: bytes) -> None:
        self.mode = 'sha256'
        self.value = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()


class _Recorded:
    def __init__(self, path: str, hash_: _Hash | None) -> None:
        self._path = path
        self.hash = hash_

    def __str__(self) -> str:
        return self._path


class _Dist:
    def __init__(
        self, name: str, version: str, root: Path | None = None, files: Sequence[_Recorded] = ()
    ) -> None:
        self.metadata = {'Name': name}
        self.version = version
        self._root = root
        self.files = list(files) if root is not None else None

    def locate_file(self, path: object) -> str:
        assert self._root is not None
        return str(self._root / str(path))


class _EntryPoint:
    def __init__(self, name: str, value: str, load: Callable[[], object], dist: _Dist) -> None:
        self.name = name
        self.value = value
        self._load = load
        self.dist = dist

    def load(self) -> object:
        return self._load()


def _module_factory(
    seen: list[ModuleHost], *, name: str = 'alpha', flags: Sequence[FlagDeclaration] = ()
) -> Callable[[ModuleHost], RuntimeModule]:
    def build(host: ModuleHost) -> RuntimeModule:
        seen.append(host)
        return RuntimeModule(name, tool_factories=(lambda *_: {'Echo': _Echo()},), flags=flags)

    return build


def _entry(
    factory: object, *, name: str = 'alpha', dist: _Dist | None = None
) -> Callable[[], list[_EntryPoint]]:
    point = _EntryPoint(
        name, 'alpha_pkg.module:build', lambda: factory, dist or _Dist('alpha', '1.0')
    )
    return lambda: [point]


def _echo_llm() -> ScriptedLLM:
    return ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall(id='echo-1', name='Echo', arguments=json.dumps({'text': 'hi'}))
                ]
            ),
            ScriptedTurn(content='done'),
        ]
    )


def _config(tmp_path: Path, body: str = '') -> str:
    (tmp_path / 'ws').mkdir(exist_ok=True)
    path = tmp_path / 'syndica.yaml'
    path.write_text(f'version: 1\nmodules: [alpha]\n{body}', encoding='utf-8')
    return str(path)


def _run(
    tmp_path: Path,
    entry_points: Callable[[], list[_EntryPoint]],
    llm: ScriptedLLM,
    *,
    body: str = '',
    **request: Any,
) -> object:
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: llm,
        workspace_root=tmp_path / 'ws',
        environ={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path)},
        entry_points=entry_points,
    )
    try:
        return host.run_request(RunRequest(prompt='go', config=_config(tmp_path, body), **request))
    finally:
        host.close()


def _tool_trust(llm: ScriptedLLM) -> TrustLevel:
    [block] = [block for block in llm.calls[-1] if block.role == 'tool']
    return block.trust


def test_an_enabled_module_is_built_from_its_entry_point_with_a_module_host(tmp_path: Path):
    seen: list[ModuleHost] = []
    llm = _echo_llm()

    _run(tmp_path, _entry(_module_factory(seen)), llm)

    [host] = seen
    assert host.workspace_root == tmp_path / 'ws'
    assert 'HOME' in host.environment
    assert callable(host.redactor.redact)


def test_a_third_party_tools_result_is_untrusted_unless_the_user_trusts_it(tmp_path: Path):
    floored = _echo_llm()
    _run(tmp_path, _entry(_module_factory([])), floored)

    trusted = _echo_llm()
    _run(tmp_path, _entry(_module_factory([])), trusted, trust_module_output=('alpha',))

    assert _tool_trust(floored) is TrustLevel.UNTRUSTED
    assert _tool_trust(trusted) is TrustLevel.TRUSTED


def test_trusting_a_module_that_is_not_enabled_is_refused(tmp_path: Path):
    with pytest.raises(ConfigError) as excinfo:
        _run(tmp_path, _entry(_module_factory([])), _echo_llm(), trust_module_output=('beta',))
    assert "['beta']" in excinfo.value.received
    assert "['alpha']" in excinfo.value.expected


@pytest.mark.parametrize(
    ('factory', 'anchor'),
    [
        ('not a factory', '收 ModuleHost 的可调用对象'),
        (lambda host: object(), '交回一个 RuntimeModule'),
        (lambda host: RuntimeModule('other'), "入口点名 'alpha' 等于它交回的 Module 的名字"),
    ],
    ids=['not-callable', 'not-a-module', 'renamed'],
)
def test_an_entry_point_that_does_not_hand_back_its_module_is_refused(
    tmp_path: Path, factory: object, anchor: str
):
    with pytest.raises(ConfigError) as excinfo:
        _run(tmp_path, _entry(factory), _echo_llm())
    assert anchor in excinfo.value.expected


def test_an_import_failure_is_shown_through_the_hosts_redactor(tmp_path: Path):
    def broken() -> object:
        raise ImportError('cannot reach zebra-1234 on the network')

    point = _EntryPoint('alpha', 'alpha_pkg.module:build', broken, _Dist('alpha', '1.0'))
    body = "redaction:\n  rules:\n    - {id: custom.zebra, pattern: 'zebra-[0-9]+'}\n"
    with pytest.raises(ConfigError) as excinfo:
        _run(tmp_path, lambda: [point], _echo_llm(), body=body)
    assert 'ImportError' in excinfo.value.received
    assert 'zebra-1234' not in str(excinfo.value)


@pytest.mark.parametrize(
    ('key', 'anchor'),
    [('greeting', "以 'alpha_' 开头"), ('alpha_api_token', '不像密钥名')],
    ids=['no-prefix', 'secret-like'],
)
def test_a_third_party_flag_key_needs_the_module_prefix_and_must_not_look_secret(
    tmp_path: Path, key: str, anchor: str
):
    flags = (FlagDeclaration(key=key, default='x', description='d'),)
    with pytest.raises(ConfigError) as excinfo:
        _run(tmp_path, _entry(_module_factory([], flags=flags)), _echo_llm())
    assert anchor in excinfo.value.expected


class _Capturing(Runtime):
    given: list[dict[str, object]] = []

    def __init__(self, **kwargs: Any) -> None:
        _Capturing.given.append(dict(kwargs.get('flags') or {}))
        super().__init__(**kwargs)


@pytest.fixture
def runtime_flags(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    _Capturing.given = []
    monkeypatch.setattr(host_module, 'Runtime', _Capturing)
    return _Capturing.given


_GREETING = (FlagDeclaration(key='alpha_greeting', default='hello', description='问候语'),)


def test_flags_in_the_config_reach_the_runtime_and_the_command_line_wins(
    tmp_path: Path, runtime_flags: list[dict[str, object]]
):
    body = 'flags:\n  alpha_greeting: hi\n  max_wait_seconds: 30\n'
    entry = _entry(_module_factory([], flags=_GREETING))
    _run(tmp_path, entry, _echo_llm(), body=body, flags=('max_wait_seconds=10',))
    assert runtime_flags == [{'alpha_greeting': 'hi', 'max_wait_seconds': 10}]


@pytest.mark.parametrize(
    ('body', 'anchor'),
    [
        ('flags:\n  untrusted_high_risk: ask\n', '项目配置只能收紧'),
        ('flags:\n  untrusted_high_risk: allow\n', '不取'),
        ('flags:\n  max_wait_seconds: yes\n', '是 int'),
        ('flags:\n  max_wait_seconds: "30"\n', '是 int'),
        ('flags:\n  nowhere_at_all: 1\n', '已装 Module 声明的 Flag'),
    ],
    ids=['loosens', 'refused-value', 'yaml-bool', 'string-for-int', 'unknown'],
)
def test_a_flag_value_the_config_may_not_set_is_refused(tmp_path: Path, body: str, anchor: str):
    with pytest.raises(ConfigError) as excinfo:
        _run(tmp_path, _entry(_module_factory([])), _echo_llm(), body=body)
    assert anchor in excinfo.value.expected


def test_a_float_flag_value_fails_when_the_config_loads(tmp_path: Path):
    with pytest.raises(ConfigError) as excinfo:
        _run(tmp_path, _entry(_module_factory([])), _echo_llm(), body='flags:\n  x: 2.5\n')
    assert '布尔值、整数或字符串' in excinfo.value.expected


def test_the_untrusted_floor_reaches_the_runtime_for_enabled_third_party_modules_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    seen: list[tuple[str, ...]] = []

    class _Recording(Runtime):
        def __init__(self, **kwargs: Any) -> None:
            seen.append(tuple(kwargs.get('untrusted_output_modules', ())))
            super().__init__(**kwargs)

    monkeypatch.setattr(host_module, 'Runtime', _Recording)
    _run(tmp_path, _entry(_module_factory([])), _echo_llm(), body='')
    assert seen == [('alpha',)]


def _installed_in_the_workspace(workspace: Path) -> _Dist:
    package = workspace / '.venv' / 'site' / 'alpha_pkg'
    package.mkdir(parents=True)
    module = package / 'module.py'
    module.write_text('VALUE = 1\n', encoding='utf-8')
    (package / '__pycache__').mkdir()
    (package / '__pycache__' / 'module.cpython-311.pyc').write_bytes(b'cache')
    return _Dist(
        'alpha',
        '1.0',
        root=workspace / '.venv' / 'site',
        files=[
            _Recorded('alpha_pkg/module.py', _Hash(b'VALUE = 1\n')),
            _Recorded('alpha_pkg/__pycache__/module.cpython-311.pyc', None),
            _Recorded('alpha-1.0.dist-info/RECORD', None),
        ],
    )


def _snapshot_with(workspace: Path, dist: _Dist) -> str:
    (workspace / 'alpha-1.0.dist-info').mkdir(exist_ok=True)
    record = workspace / '.venv' / 'site' / 'alpha-1.0.dist-info'
    record.mkdir(parents=True, exist_ok=True)
    (record / 'RECORD').write_text('alpha_pkg/module.py,sha256=...,10\n', encoding='utf-8')
    point = _EntryPoint('alpha', 'alpha_pkg.module:build', lambda: None, dist)
    return read_snapshot(workspace / '.syndica.yaml', workspace, discover(lambda: [point])).digest


def test_an_in_workspace_distribution_is_checked_against_its_record(tmp_path: Path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / '.syndica.yaml').write_text('version: 1\nmodules: [alpha]\n', encoding='utf-8')
    dist = _installed_in_the_workspace(workspace)
    before = _snapshot_with(workspace, dist)
    (
        workspace / '.venv' / 'site' / 'alpha_pkg' / '__pycache__' / 'module.cpython-311.pyc'
    ).write_bytes(b'rewritten by python')
    assert _snapshot_with(workspace, dist) == before

    (workspace / '.venv' / 'site' / 'alpha_pkg' / 'module.py').write_text(
        'VALUE = 2\n', encoding='utf-8'
    )
    with pytest.raises(ConfigError) as excinfo:
        _snapshot_with(workspace, dist)
    assert 'alpha_pkg/module.py' in excinfo.value.received
    assert '重新安装' in excinfo.value.next_action


def test_a_distribution_outside_the_workspace_is_not_read(tmp_path: Path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / '.syndica.yaml').write_text('version: 1\nmodules: [alpha]\n', encoding='utf-8')
    outside = tmp_path / 'site'
    dist = _Dist('alpha', '1.0', root=outside, files=[_Recorded('alpha_pkg/missing.py', None)])
    point = _EntryPoint('alpha', 'alpha_pkg.module:build', lambda: None, dist)
    read_snapshot(workspace / '.syndica.yaml', workspace, discover(lambda: [point]))


def test_a_run_with_a_module_that_runs_code_and_is_not_replay_safe_says_why_it_has_no_document(
    tmp_path: Path,
):
    stderr = StringIO()
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=stderr),
        model_factory=lambda _: _echo_llm(),
        workspace_root=tmp_path / 'ws',
        environ={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path)},
        entry_points=_entry(_module_factory([])),
    )
    try:
        host.run_request(RunRequest(prompt='go', config=_config(tmp_path), trace='replay'))
    finally:
        host.close()
    assert '第三方 Module alpha 有会跑的代码、又没声明 replay_safe' in stderr.getvalue()


def test_an_enabled_modules_code_inside_the_workspace_is_part_of_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / '.syndica.yaml').write_text('version: 1\nmodules: [alpha]\n', encoding='utf-8')
    package = workspace / 'alpha_pkg'
    package.mkdir()
    (package / '__init__.py').write_text('', encoding='utf-8')
    (package / 'module.py').write_text('VALUE = 1\n', encoding='utf-8')
    monkeypatch.syspath_prepend(str(workspace))
    point = _EntryPoint('alpha', 'alpha_pkg.module:build', lambda: None, _Dist('alpha', '1.0'))

    def digest() -> str:
        found = discover(lambda: [point])
        return read_snapshot(workspace / '.syndica.yaml', workspace, found).digest

    before = digest()
    (package / 'module.py').write_text('VALUE = 2\n', encoding='utf-8')
    assert digest() != before


def test_a_yaml_boolean_for_an_int_flag_fails_before_any_model_is_built(tmp_path: Path):
    built: list[str] = []
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda name: built.append(name) or _echo_llm(),
        workspace_root=tmp_path / 'ws',
        environ={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path)},
        entry_points=_entry(_module_factory([])),
    )
    config = _config(tmp_path, 'flags:\n  max_wait_seconds: yes\n')
    try:
        with pytest.raises(ConfigError) as excinfo:
            host.run_request(RunRequest(prompt='go', config=config))
    finally:
        host.close()
    assert 'yes / on' in excinfo.value.next_action
    assert built == []
