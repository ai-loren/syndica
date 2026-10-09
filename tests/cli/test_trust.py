"""项目配置的信任确认（M4 D15，T11）：确认之前不执行配置里的任何东西，摘要盖住实际加载的东西，
只认人。"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import json
import stat
import sys
import types
from io import StringIO
from pathlib import Path

import pytest

from syndica.cli.environment import agent_environment
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, main
from syndica.cli.modules import Discovered
from syndica.cli.trust import (
    NO_TRUST_GATE,
    JsonTrustStore,
    PinnedImports,
    TrustGate,
    code_location,
    config_of,
    describe,
    read_snapshot,
    store_path,
)
from syndica.kernel import ConfigError, ScriptedLLM, ScriptedTurn


@pytest.fixture(autouse=True)
def _forget_imported_role_packages():
    """`doctor` 在本进程里 import 工作区的包；留在 `sys.modules` 里，下一条用例的同名包就按那一份
    （另一个工作区的）去找了。"""
    yield
    for name in [name for name in sys.modules if name.split('.')[0] in {'rolepkg', 'edpkg'}]:
        del sys.modules[name]


class _Tty(StringIO):
    def isatty(self) -> bool:
        return True


class _EditingTty(_Tty):
    """答 y 之前把配置改掉：问题挂着的这段时间文件照样能被改。"""

    def __init__(self, path: Path) -> None:
        super().__init__('y\n')
        self._path = path

    def readline(self, size: int = -1) -> str:
        if self._path.exists():
            self._path.write_text(self._path.read_text(encoding='utf-8') + '# edited\n')
        return super().readline(size)


def _workspace(tmp_path: Path, config: str = 'version: 1\n') -> Path:
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / '.syndica.yaml').write_text(config, encoding='utf-8')
    return workspace


def _store(tmp_path: Path) -> JsonTrustStore:
    return JsonTrustStore(tmp_path / 'outside' / 'trusted.json')


def _host(
    workspace: Path, store: JsonTrustStore | None, *, stdin: StringIO, stderr: StringIO
) -> tuple[RuntimeCliHost, CliIO, list[str]]:
    io = CliIO(stdin=stdin, stdout=StringIO(), stderr=stderr)
    built: list[str] = []

    def model(name: str) -> ScriptedLLM:
        built.append(name)
        return ScriptedLLM([ScriptedTurn(content='done')])

    host = RuntimeCliHost(
        io=io,
        model_factory=model,
        workspace_root=workspace,
        environ={},
        trust=NO_TRUST_GATE if store is None else TrustGate(store_for=lambda _: store),
    )
    return host, io, built


def _run(
    workspace: Path,
    store: JsonTrustStore | None,
    argv: list[str] | None = None,
    *,
    stdin: StringIO | None = None,
) -> tuple[int, str, str, list[str]]:
    stderr = _Tty() if isinstance(stdin, _Tty) else StringIO()
    host, io, built = _host(workspace, store, stdin=stdin or StringIO(), stderr=stderr)
    try:
        code = main(argv or ['run', '--agent', 'Worker', 'go'], host=host, io=io)
    finally:
        host.close(interrupted=False)
    return code, io.stdout.getvalue(), stderr.getvalue(), built  # pyright: ignore[reportAttributeAccessIssue]


def test_an_untrusted_workspace_config_is_refused_before_anything_runs(tmp_path: Path):
    workspace = _workspace(tmp_path)
    store = _store(tmp_path)

    code, stdout, stderr, built = _run(workspace, store)

    digest = read_snapshot(workspace / '.syndica.yaml', workspace).digest
    assert code != 0
    assert stdout == ''
    assert built == []
    assert f'syndica config trust --sha256 {digest}' in stderr
    assert '不是交互终端' in stderr
    assert store.get((workspace / '.syndica.yaml').resolve()) is None


def test_a_person_at_a_terminal_confirms_once_and_is_not_asked_again(tmp_path: Path):
    workspace = _workspace(tmp_path)
    store = _store(tmp_path)

    code, stdout, stderr, _ = _run(workspace, store, stdin=_Tty('y\n'))
    assert code == 0, stderr
    assert '信任这份配置并继续？[y/N]' in stderr
    assert '信任这份配置' not in stdout
    assert stdout.rstrip().endswith('done')
    digest = read_snapshot(workspace / '.syndica.yaml', workspace).digest
    assert store.get((workspace / '.syndica.yaml').resolve()) == digest

    code, _, stderr, _ = _run(workspace, store, stdin=_Tty(''))
    assert code == 0
    assert '信任这份配置' not in stderr


@pytest.mark.parametrize('answer', ['n\n', '\n', 'yes please\n'])
def test_anything_but_yes_refuses(tmp_path: Path, answer: str):
    workspace = _workspace(tmp_path)
    store = _store(tmp_path)
    code, _, stderr, built = _run(workspace, store, stdin=_Tty(answer))
    assert code != 0
    assert '回答不是 y' in stderr
    assert built == []
    assert store.get((workspace / '.syndica.yaml').resolve()) is None


_INTEROP_CONFIG = """\
version: 1
interop:
  a2a:
    agents:
      - name: researcher
        url: https://attacker.example/a2a/
      - name: reviewer
        url: http://127.0.0.1:9999/a2a/
"""


def test_the_prompt_names_the_user_level_credentials_and_where_each_one_goes(tmp_path: Path):
    workspace = _workspace(tmp_path, _INTEROP_CONFIG)
    store = _store(tmp_path)
    credentials = store.path.parent / 'interop.yaml'
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text(
        'credentials:\n'
        "  a2a:researcher: {url: 'http://127.0.0.1:8000/a2a/', bearer_env: RESEARCHER_TOKEN}\n"
        "  a2a:reviewer: {url: 'http://127.0.0.1:9999/a2a/', bearer_env: REVIEWER_TOKEN}\n",
        encoding='utf-8',
    )
    credentials.chmod(0o600)
    stderr = _Tty()
    io = CliIO(stdin=_Tty('n\n'), stdout=StringIO(), stderr=stderr)
    host = RuntimeCliHost(
        io=io,
        model_factory=lambda _: ScriptedLLM([ScriptedTurn(content='done')]),
        workspace_root=workspace,
        environ={'SYNDICA_TRUST_STORE': str(store.path)},
        trust=TrustGate(store_for=lambda _: store),
    )
    try:
        code = main(['run', '--agent', 'Worker', 'go'], host=host, io=io)
    finally:
        host.close(interrupted=False)

    shown = stderr.getvalue()
    assert code != 0
    assert (
        'a2a researcher: https://attacker.example/a2a/；凭证：不交——用户级 a2a:researcher '
        '钉的是 url http://127.0.0.1:8000/a2a/，对不上，RESEARCHER_TOKEN（Authorization: Bearer） '
        '不发、启动时拒绝'
    ) in shown
    assert (
        'a2a reviewer: http://127.0.0.1:9999/a2a/；凭证：REVIEWER_TOKEN（Authorization: Bearer） '
        '交给这里'
    ) in shown


def test_a_broken_user_level_interop_file_does_not_block_a_config_without_interop(
    tmp_path: Path,
):
    workspace = _workspace(tmp_path)
    store = _store(tmp_path)
    credentials = store.path.parent / 'interop.yaml'
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text('credentials: {}\n', encoding='utf-8')
    credentials.chmod(0o644)
    io = CliIO(stdin=_Tty('y\n'), stdout=StringIO(), stderr=_Tty())
    host = RuntimeCliHost(
        io=io,
        model_factory=lambda _: ScriptedLLM([ScriptedTurn(content='done')]),
        workspace_root=workspace,
        environ={'SYNDICA_TRUST_STORE': str(store.path)},
        trust=TrustGate(store_for=lambda _: store),
    )
    try:
        code = main(['run', '--agent', 'Worker', 'go'], host=host, io=io)
    finally:
        host.close(interrupted=False)

    assert code == 0, io.stderr.getvalue()  # pyright: ignore[reportAttributeAccessIssue]


def test_a_change_during_the_confirmation_is_caught_by_a_second_read(tmp_path: Path):
    workspace = _workspace(tmp_path)
    store = _store(tmp_path)
    code, _, stderr, built = _run(workspace, store, stdin=_EditingTty(workspace / '.syndica.yaml'))
    assert code != 0
    assert '确认期间' in stderr
    assert built == []
    assert store.get((workspace / '.syndica.yaml').resolve()) is None


def test_an_edit_after_trust_asks_again(tmp_path: Path):
    workspace = _workspace(tmp_path)
    store = _store(tmp_path)
    assert _run(workspace, store, stdin=_Tty('y\n'))[0] == 0
    (workspace / '.syndica.yaml').write_text('version: 1\nmodules: [retry]\n', encoding='utf-8')
    code, _, stderr, _ = _run(workspace, store)
    assert code != 0
    assert 'syndica config trust --sha256' in stderr


def test_a_config_outside_the_workspace_was_pointed_at_by_a_person(tmp_path: Path):
    workspace = _workspace(tmp_path)
    outside = tmp_path / 'elsewhere.yaml'
    outside.write_text('version: 1\n', encoding='utf-8')
    code, _, stderr, _ = _run(
        workspace, _store(tmp_path), ['--config', str(outside), 'run', '--agent', 'Worker', 'go']
    )
    assert code == 0, stderr


def test_no_project_config_means_nothing_to_trust(tmp_path: Path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    code, _, stderr, _ = _run(workspace, _store(tmp_path))
    assert code == 0, stderr


def test_doctor_is_gated_too(tmp_path: Path):
    workspace = _workspace(tmp_path)
    code, _, stderr, _ = _run(workspace, _store(tmp_path), ['doctor'])
    assert code != 0
    assert 'syndica config trust --sha256' in stderr


def test_a_store_inside_the_workspace_is_refused(tmp_path: Path):
    workspace = _workspace(tmp_path)
    inside = JsonTrustStore(workspace / '.trust' / 'trusted.json')
    code, _, stderr, _ = _run(workspace, inside, stdin=_Tty('y\n'))
    assert code != 0
    assert '信任存储在工作区之外' in stderr
    assert not (workspace / '.trust').exists()


def test_a_host_built_without_a_gate_says_so_explicitly(tmp_path: Path):
    workspace = _workspace(tmp_path)
    code, _, stderr, _ = _run(workspace, None)
    assert code == 0, stderr


def test_config_trust_with_the_digest_from_the_refusal_records_it(tmp_path: Path):
    workspace = _workspace(tmp_path)
    store = _store(tmp_path)
    digest = read_snapshot(workspace / '.syndica.yaml', workspace).digest

    code, stdout, stderr, _ = _run(workspace, store, ['config', 'trust', '--sha256', digest])

    assert code == 0, stderr
    assert digest in stdout
    assert _run(workspace, store)[0] == 0


def test_config_trust_refuses_a_digest_that_is_no_longer_current(tmp_path: Path):
    workspace = _workspace(tmp_path)
    store = _store(tmp_path)
    code, _, stderr, _ = _run(workspace, store, ['config', 'trust', '--sha256', 'ab' * 32])
    assert code != 0
    assert '现在的摘要' in stderr
    assert store.get((workspace / '.syndica.yaml').resolve()) is None


def test_config_trust_without_a_digest_needs_a_terminal(tmp_path: Path):
    workspace = _workspace(tmp_path)
    code, _, stderr, _ = _run(workspace, _store(tmp_path), ['config', 'trust'])
    assert code != 0
    assert 'syndica config trust --sha256' in stderr


def test_config_trust_on_an_outside_config_needs_nothing(tmp_path: Path):
    workspace = _workspace(tmp_path)
    outside = tmp_path / 'elsewhere.yaml'
    outside.write_text('version: 1\n', encoding='utf-8')
    code, stdout, _, _ = _run(
        workspace, _store(tmp_path), ['--config', str(outside), 'config', 'trust']
    )
    assert code == 0
    assert '不需要信任确认' in stdout


def test_what_a_config_says_cannot_rewrite_the_terminal_or_forge_a_line(tmp_path: Path):
    workspace = _workspace(
        tmp_path,
        'version: 1\nworkspace:\n  verify:\n'
        '    - "echo ok\\x1b[2J\\n信任这份配置并继续？[y/N] y"\n',
    )
    _, _, stderr, _ = _run(workspace, _store(tmp_path), stdin=_Tty('n\n'))
    assert '\x1b' not in stderr
    verify_lines = [line for line in stderr.splitlines() if 'echo ok' in line]
    assert len(verify_lines) == 1
    assert verify_lines[0].startswith('    │ ')
    assert stderr.count('信任这份配置并继续？[y/N]') == 2


def _digest(workspace: Path, **kwargs: object) -> str:
    return read_snapshot(workspace / '.syndica.yaml', workspace, **kwargs).digest  # pyright: ignore[reportArgumentType]


def test_the_digest_covers_every_prompt_file_the_config_reads(tmp_path: Path):
    workspace = _workspace(
        tmp_path,
        'version: 1\nagents:\n'
        '  - {name: Kilo, description: k, system_prompt: prompts/kilo.md}\n'
        '  - {name: Alpha, description: a, system_prompt: {file: prompts/alpha.md}}\n',
    )
    (workspace / 'prompts').mkdir()
    (workspace / 'prompts' / 'kilo.md').write_text('implicit', encoding='utf-8')
    (workspace / 'prompts' / 'alpha.md').write_text('explicit', encoding='utf-8')
    before = _digest(workspace)
    (workspace / 'prompts' / 'kilo.md').write_text('implicit, edited', encoding='utf-8')
    after_implicit = _digest(workspace)
    (workspace / 'prompts' / 'alpha.md').write_text('explicit, edited', encoding='utf-8')
    after_explicit = _digest(workspace)
    assert len({before, after_implicit, after_explicit}) == 3


def test_the_config_is_built_from_the_prompt_text_that_was_digested(tmp_path: Path):
    workspace = _workspace(
        tmp_path,
        'version: 1\nagents:\n  - {name: Kilo, description: k, system_prompt: {file: kilo.md}}\n',
    )
    (workspace / 'kilo.md').write_text('what the person saw', encoding='utf-8')
    snapshot = read_snapshot(workspace / '.syndica.yaml', workspace)
    (workspace / 'kilo.md').write_text('swapped after the digest', encoding='utf-8')
    assert config_of(snapshot).catalog.get('Kilo').system_prompt == 'what the person saw'


_IMPL_CONFIG = 'version: 1\nagents:\n  - {name: Kilo, description: k, impl: rolepkg.roles:Role}\n'


def _role_package(workspace: Path) -> None:
    package = workspace / 'rolepkg'
    package.mkdir()
    (package / '__init__.py').write_text(
        "from pathlib import Path\nPath(__file__).with_name('imported').write_text('yes')\n",
        encoding='utf-8',
    )
    (package / 'helper.py').write_text('VALUE = 1\n', encoding='utf-8')
    (package / 'roles.py').write_text(
        'from syndica.kernel import BaseAgent\n\n\nclass Role(BaseAgent):\n    """r"""\n',
        encoding='utf-8',
    )


def test_an_in_workspace_impl_package_is_digested_whole_without_importing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = _workspace(tmp_path, _IMPL_CONFIG)
    _role_package(workspace)
    monkeypatch.syspath_prepend(str(workspace))

    snapshot = read_snapshot(workspace / '.syndica.yaml', workspace)
    describe(snapshot, config_of(snapshot))

    assert not (workspace / 'rolepkg' / 'imported').exists()
    (impl,) = snapshot.impls
    assert [name for name, _ in impl.files] == ['__init__.py', 'helper.py', 'roles.py']
    (workspace / 'rolepkg' / 'helper.py').write_text('VALUE = 2\n', encoding='utf-8')
    assert _digest(workspace) != snapshot.digest


def test_a_planted_bytecode_cache_is_not_part_of_the_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = _workspace(tmp_path, _IMPL_CONFIG)
    _role_package(workspace)
    monkeypatch.syspath_prepend(str(workspace))
    (workspace / 'rolepkg' / '__pycache__').mkdir()
    (workspace / 'rolepkg' / '__pycache__' / 'roles.cpython-311.pyc').write_bytes(b'x')
    (impl,) = read_snapshot(workspace / '.syndica.yaml', workspace).impls
    assert all('__pycache__' not in name for name, _ in impl.files)


def test_trusting_an_in_workspace_impl_moves_bytecode_out_of_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = _workspace(tmp_path, _IMPL_CONFIG)
    _role_package(workspace)
    monkeypatch.syspath_prepend(str(workspace))
    monkeypatch.setattr(sys, 'pycache_prefix', None)
    _run(workspace, _store(tmp_path), ['doctor'], stdin=_Tty('y\n'))
    assert sys.pycache_prefix is not None
    assert not Path(sys.pycache_prefix).resolve().is_relative_to(workspace.resolve())


def test_syndicas_own_code_and_outside_code_are_named_but_not_digested(tmp_path: Path):
    workspace = _workspace(
        tmp_path,
        'version: 1\nagents:\n'
        '  - name: Kilo\n    description: k\n'
        '    impl: "syndica.kernel.agents.orchestrator:Orchestrator"\n'
        '  - {name: Alpha, description: a, impl: "json.decoder:JSONDecoder"}\n',
    )
    impls = read_snapshot(workspace / '.syndica.yaml', workspace).impls
    assert [(impl.where, impl.files) for impl in impls] == [
        ('syndica 自己的代码', ()),
        ('工作区之外，不进摘要；信任它就是信任那份代码', ()),
    ]


def test_the_digest_covers_the_version_of_an_enabled_third_party_module(tmp_path: Path):
    workspace = _workspace(tmp_path, 'version: 1\nmodules: [example]\n')
    one = [Discovered('example', 'example:module', 'example-dist', '1.0')]
    other = [Discovered('example', 'example:module', 'example-dist', '1.1')]
    unrelated = [*one, Discovered('zeta', 'zeta:module', 'zeta-dist', '9.9')]
    assert _digest(workspace, discovered=one) != _digest(workspace, discovered=other)
    assert _digest(workspace, discovered=one) == _digest(workspace, discovered=unrelated)


def test_the_store_reads_back_what_it_wrote_and_leaves_no_temporary_file(tmp_path: Path):
    store = JsonTrustStore(tmp_path / 'deep' / 'trusted.json')
    store.put(Path('/ws/a.yaml'), 'aa')
    store.put(Path('/ws/b.yaml'), 'bb')
    assert (store.get(Path('/ws/a.yaml')), store.get(Path('/ws/b.yaml'))) == ('aa', 'bb')
    assert sorted(path.name for path in (tmp_path / 'deep').iterdir()) == ['trusted.json']
    assert json.loads((tmp_path / 'deep' / 'trusted.json').read_text())['version'] == 1


@pytest.mark.parametrize('content', ['not json', '[]', '{"configs": []}', ''])
def test_an_unreadable_store_means_untrusted_and_is_rewritten_cleanly(tmp_path: Path, content: str):
    path = tmp_path / 'trusted.json'
    path.write_text(content, encoding='utf-8')
    store = JsonTrustStore(path)
    assert store.get(Path('/ws/a.yaml')) is None
    store.put(Path('/ws/a.yaml'), 'aa')
    assert store.get(Path('/ws/a.yaml')) == 'aa'


@pytest.mark.parametrize(
    ('environ', 'expected'),
    [
        ({'SYNDICA_TRUST_STORE': '/s/t.json', 'XDG_CONFIG_HOME': '/x', 'HOME': '/h'}, '/s/t.json'),
        ({'XDG_CONFIG_HOME': '/x', 'HOME': '/h'}, '/x/syndica/trusted.json'),
        ({'HOME': '/h'}, '/h/.config/syndica/trusted.json'),
        ({}, None),
    ],
)
def test_the_store_location_follows_the_environment(environ: dict[str, str], expected: str | None):
    found = store_path(environ)
    assert (None if found is None else str(found)) == expected


def test_a_nested_syndica_in_the_agents_environment_uses_a_scratch_store(tmp_path: Path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    environment = agent_environment({'HOME': '/home/someone'}, (), workspace_root=workspace)
    try:
        assert environment.values['SYNDICA_TRUST_STORE'] == str(
            environment.scratch / 'trusted.json'
        )
    finally:
        environment.remove()


def test_pass_env_cannot_hand_the_agent_another_trust_store(tmp_path: Path):
    from syndica.cli.config import load_config

    path = tmp_path / '.syndica.yaml'
    path.write_text('version: 1\nworkspace:\n  pass_env: [SYNDICA_TRUST_STORE]\n', encoding='utf-8')
    with pytest.raises(ConfigError):
        load_config(path)


def test_a_crlf_prompt_file_reaches_the_kernel_as_read_text_would_read_it(tmp_path: Path):
    workspace = _workspace(
        tmp_path,
        'version: 1\nagents:\n  - {name: Kilo, description: k, system_prompt: prompts/kilo.md}\n',
    )
    (workspace / 'prompts').mkdir()
    prompt = workspace / 'prompts' / 'kilo.md'
    prompt.write_bytes(b'line one\r\nline two\rline three\r\n')

    snapshot = read_snapshot(workspace / '.syndica.yaml', workspace)

    read = config_of(snapshot).catalog.get('Kilo').system_prompt
    assert read == prompt.read_text(encoding='utf-8') == 'line one\nline two\nline three\n'


@pytest.mark.parametrize('name', ['roles.cpython-311-darwin.so', 'sourceless.pyc', 'table.json'])
def test_every_file_in_an_in_workspace_package_is_part_of_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
):
    workspace = _workspace(tmp_path, _IMPL_CONFIG)
    _role_package(workspace)
    monkeypatch.syspath_prepend(str(workspace))
    before = _digest(workspace)
    (workspace / 'rolepkg' / name).write_bytes(b'\x7fELF')
    assert _digest(workspace) != before


class _OneSpec:
    """只认一个名字的 meta-path finder，像可编辑安装装上的那种：`PathFinder` 不知道它的代码在哪。"""

    def __init__(self, name: str, spec: importlib.machinery.ModuleSpec) -> None:
        self._name = name
        self._spec = spec

    def find_spec(
        self, fullname: str, path: object, target: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        del path, target
        return self._spec if fullname == self._name else None


_ELSEWHERE_CONFIG = (
    'version: 1\nagents:\n  - {name: Kilo, description: k, impl: edpkg.roles:Role}\n'
)


def _elsewhere_package(workspace: Path) -> Path:
    package = workspace / 'elsewhere' / 'edpkg'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('', encoding='utf-8')
    (package / 'roles.py').write_text('VALUE = 1\n', encoding='utf-8')
    return package


def test_code_only_a_meta_path_finder_knows_about_is_still_digested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = _workspace(tmp_path, _ELSEWHERE_CONFIG)
    package = _elsewhere_package(workspace)
    spec = importlib.util.spec_from_file_location(
        'edpkg', package / '__init__.py', submodule_search_locations=[str(package)]
    )
    assert spec is not None
    monkeypatch.setattr(sys, 'meta_path', [_OneSpec('edpkg', spec), *sys.meta_path])

    (impl,) = read_snapshot(workspace / '.syndica.yaml', workspace).impls

    assert impl.where.startswith('工作区之内')
    assert [name for name, _ in impl.files] == ['__init__.py', 'roles.py']


def test_an_already_loaded_package_is_digested_from_its_own_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = _workspace(tmp_path, _ELSEWHERE_CONFIG)
    package = _elsewhere_package(workspace)
    spec = importlib.util.spec_from_file_location(
        'edpkg', package / '__init__.py', submodule_search_locations=[str(package)]
    )
    assert spec is not None
    monkeypatch.setitem(sys.modules, 'edpkg', importlib.util.module_from_spec(spec))

    (impl,) = read_snapshot(workspace / '.syndica.yaml', workspace).impls

    assert [name for name, _ in impl.files] == ['__init__.py', 'roles.py']


def _unpinnable(workspace: Path, kind: str) -> importlib.machinery.ModuleSpec:
    if kind == 'zip':
        spec = importlib.machinery.ModuleSpec(
            'edpkg', None, origin=str(workspace / 'bundle.zip' / 'edpkg' / '__init__.py')
        )
        spec.has_location = True
        return spec
    if kind == 'namespace':
        spec = importlib.machinery.ModuleSpec('edpkg', None, is_package=True)
        spec.submodule_search_locations = [str(_elsewhere_package(workspace))]
        return spec
    return importlib.machinery.ModuleSpec(
        'edpkg', importlib.machinery.SourcelessFileLoader('edpkg', '')
    )


@pytest.mark.parametrize('kind', ['zip', 'namespace', 'custom-loader'])
def test_code_the_digest_cannot_pin_down_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
):
    workspace = _workspace(tmp_path, _ELSEWHERE_CONFIG)
    monkeypatch.setattr(
        sys, 'meta_path', [_OneSpec('edpkg', _unpinnable(workspace, kind)), *sys.meta_path]
    )
    with pytest.raises(ConfigError) as excinfo:
        read_snapshot(workspace / '.syndica.yaml', workspace)
    assert "impl: 'edpkg.roles:Role'" in excinfo.value.expected
    assert '普通的包' in excinfo.value.next_action


def test_a_loaded_module_without_a_spec_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = _workspace(tmp_path, _ELSEWHERE_CONFIG)
    monkeypatch.setitem(sys.modules, 'edpkg', types.ModuleType('edpkg'))
    with pytest.raises(ConfigError) as excinfo:
        read_snapshot(workspace / '.syndica.yaml', workspace)
    assert '没有 __spec__' in excinfo.value.received


def test_sys_path_entries_inside_the_workspace_are_shown_but_not_digested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = _workspace(tmp_path)
    before = _digest(workspace)
    site = workspace / '.venv' / 'lib' / 'site-packages'
    site.mkdir(parents=True)
    monkeypatch.setattr(sys, 'path', [str(site), str(tmp_path / 'outside'), *sys.path])

    snapshot = read_snapshot(workspace / '.syndica.yaml', workspace)

    assert snapshot.path_entries == (str(site),)
    assert str(site) in describe(snapshot, config_of(snapshot))
    assert snapshot.digest == before


def test_trusting_an_in_workspace_impl_uses_a_fresh_private_bytecode_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = _workspace(tmp_path, _IMPL_CONFIG)
    _role_package(workspace)
    monkeypatch.syspath_prepend(str(workspace))
    fixed = tmp_path / 'fixed-pycache'
    monkeypatch.setattr(sys, 'pycache_prefix', str(fixed))

    _run(workspace, _store(tmp_path), ['doctor'], stdin=_Tty('y\n'))

    assert sys.pycache_prefix is not None
    prefix = Path(sys.pycache_prefix)
    assert prefix != fixed
    assert prefix.name.startswith('syndica-pycache-')
    assert stat.S_IMODE(prefix.stat().st_mode) == 0o700


def test_an_auto_discovered_config_that_links_outside_the_workspace_is_still_gated(
    tmp_path: Path,
):
    workspace = tmp_path / 'repo' / 'sub'
    workspace.mkdir(parents=True)
    (tmp_path / 'repo' / 'linked.yaml').write_text('version: 1\n', encoding='utf-8')
    (workspace / '.syndica.yaml').symlink_to('../linked.yaml')
    store = _store(tmp_path)

    code, stdout, stderr, built = _run(workspace, store)

    assert code != 0
    assert (stdout, built) == ('', [])
    assert 'syndica config trust --sha256' in stderr


def _linked_role_package(tmp_path: Path, name: str, marker: str) -> Path:
    package = tmp_path / name / 'rolepkg'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text(f'MARK = {marker!r}\n', encoding='utf-8')
    (package / 'roles.py').write_text(
        'from syndica.kernel import BaseAgent\n\n\nclass Role(BaseAgent):\n    """r"""\n',
        encoding='utf-8',
    )
    return package


def test_a_role_package_linked_into_the_workspace_is_digested_and_pinned_to_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = _workspace(tmp_path, _IMPL_CONFIG)
    reviewed = _linked_role_package(tmp_path, 'reviewed', 'reviewed')
    swapped = _linked_role_package(tmp_path, 'swapped', 'swapped')
    (workspace / 'rolepkg').symlink_to(reviewed, target_is_directory=True)
    monkeypatch.syspath_prepend(str(workspace))

    before = read_snapshot(workspace / '.syndica.yaml', workspace)
    (workspace / 'rolepkg').unlink()
    (workspace / 'rolepkg').symlink_to(swapped, target_is_directory=True)

    (impl,) = before.impls
    assert impl.inside
    assert [name for name, _ in impl.files] == ['__init__.py', 'roles.py']
    assert _digest(workspace) != before.digest


def test_a_directory_linked_inside_a_package_is_part_of_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = _workspace(tmp_path, _IMPL_CONFIG)
    _role_package(workspace)
    nested = tmp_path / 'elsewhere' / 'nested'
    nested.mkdir(parents=True)
    (nested / 'tool.py').write_text('VALUE = 1\n', encoding='utf-8')
    (workspace / 'rolepkg' / 'nested').symlink_to(nested, target_is_directory=True)
    (workspace / 'rolepkg' / 'loop').symlink_to(workspace / 'rolepkg', target_is_directory=True)
    monkeypatch.syspath_prepend(str(workspace))

    (impl,) = read_snapshot(workspace / '.syndica.yaml', workspace).impls
    before = _digest(workspace)
    (nested / 'tool.py').write_text('VALUE = 2\n', encoding='utf-8')

    assert 'nested/tool.py' in [name for name, _ in impl.files]
    assert not any(name.startswith('loop/') for name, _ in impl.files)
    assert _digest(workspace) != before


def test_a_sys_path_entry_spelled_in_another_case_still_reaches_in_workspace_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = _workspace(tmp_path, _IMPL_CONFIG)
    _role_package(workspace)
    upper = tmp_path / 'WS'
    if not upper.exists():
        pytest.skip('case-sensitive volume: the two spellings are different directories')
    monkeypatch.syspath_prepend(str(upper))

    (impl,) = read_snapshot(workspace / '.syndica.yaml', workspace).impls

    assert impl.inside
    assert [name for name, _ in impl.files] == ['__init__.py', 'helper.py', 'roles.py']


def test_the_digest_pins_where_outside_code_lives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = _workspace(tmp_path, _IMPL_CONFIG)
    first = _linked_role_package(tmp_path, 'first', 'first')
    second = _linked_role_package(tmp_path, 'second', 'second')
    monkeypatch.setattr(sys, 'path', [str(first.parent), *sys.path])
    before = _digest(workspace)
    monkeypatch.setattr(sys, 'path', [str(second.parent), *sys.path[1:]])

    (impl,) = read_snapshot(workspace / '.syndica.yaml', workspace).impls

    assert not impl.inside
    assert _digest(workspace) != before


def _in_workspace_module(workspace: Path) -> Discovered:
    package = workspace / 'extmod'
    package.mkdir()
    (package / '__init__.py').write_text(
        'from syndica.kernel import RuntimeModule\n\n\n'
        'def build(host):\n    return RuntimeModule("ext")\n',
        encoding='utf-8',
    )
    return Discovered('ext', 'extmod:build', 'ext-dist', '1.0')


def test_trusting_a_config_whose_module_code_is_in_the_workspace_moves_bytecode_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = _workspace(tmp_path, 'version: 1\nmodules: [ext]\n')
    entry = _in_workspace_module(workspace)
    monkeypatch.syspath_prepend(str(workspace))
    monkeypatch.setattr(sys, 'pycache_prefix', None)

    snapshot = read_snapshot(workspace / '.syndica.yaml', workspace, [entry])
    host, _, _ = _host(workspace, _store(tmp_path), stdin=_Tty('y\n'), stderr=_Tty())
    host._snapshot = lambda file: snapshot  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    try:
        host._trusted_config('.syndica.yaml')  # pyright: ignore[reportPrivateUsage]
        assert sys.pycache_prefix is not None
        assert not Path(sys.pycache_prefix).resolve().is_relative_to(workspace.resolve())
    finally:
        host.close(interrupted=False)
        sys.modules.pop('extmod', None)


def _pinned_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, PinnedImports]:
    workspace = tmp_path / 'ws'
    package = workspace / 'pinpkg'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('', encoding='utf-8')
    (package / 'later.py').write_text('VALUE = "reviewed"\n', encoding='utf-8')
    monkeypatch.syspath_prepend(str(workspace))
    monkeypatch.setattr(sys, 'pycache_prefix', str(tmp_path / 'private-pycache'))
    pins = PinnedImports([code_location('pinpkg', workspace)])
    pins.install()
    return package, pins


@pytest.fixture
def _forget_pinpkg():
    yield
    for name in [name for name in sys.modules if name.split('.')[0] == 'pinpkg']:
        del sys.modules[name]


@pytest.mark.usefixtures('_forget_pinpkg')
def test_trusted_code_imports_as_it_was_confirmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, pins = _pinned_package(tmp_path, monkeypatch)
    try:
        import pinpkg.later  # pyright: ignore[reportMissingImports]

        assert pinpkg.later.VALUE == 'reviewed'  # pyright: ignore[reportUnknownMemberType]
    finally:
        pins.remove()


@pytest.mark.usefixtures('_forget_pinpkg')
@pytest.mark.parametrize('change', ['edited', 'added'])
def test_code_changed_after_trust_is_refused_when_imported_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
):
    package, pins = _pinned_package(tmp_path, monkeypatch)
    name = 'later' if change == 'edited' else 'planted'
    (package / f'{name}.py').write_text('VALUE = "changed"\n', encoding='utf-8')
    try:
        with pytest.raises(ImportError) as excinfo:
            importlib.import_module(f'pinpkg.{name}')
    finally:
        pins.remove()
    assert f'{name}.py' in str(excinfo.value)
    assert '看过新的内容再确认' in str(excinfo.value)


@pytest.mark.usefixtures('_forget_pinpkg')
def test_removed_pins_no_longer_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    package, pins = _pinned_package(tmp_path, monkeypatch)
    pins.remove()
    (package / 'later.py').write_text('VALUE = "changed"\n', encoding='utf-8')

    assert importlib.import_module('pinpkg.later').VALUE == 'changed'


def test_the_confirmation_shows_the_flags_the_config_sets(tmp_path: Path):
    workspace = _workspace(tmp_path, 'version: 1\nflags:\n  max_wait_seconds: 30\n')
    snapshot = read_snapshot(workspace / '.syndica.yaml', workspace)

    shown = describe(snapshot, config_of(snapshot))

    assert 'max_wait_seconds: 30' in shown


def test_a_terminal_on_stdin_alone_is_not_a_person(tmp_path: Path):
    workspace = _workspace(tmp_path)
    store = _store(tmp_path)
    host, io, built = _host(workspace, store, stdin=_Tty('y\n'), stderr=StringIO())
    try:
        code = main(['run', '--agent', 'Worker', 'go'], host=host, io=io)
    finally:
        host.close(interrupted=False)

    assert code != 0
    assert built == []
    assert '不是交互终端' in io.stderr.getvalue()  # pyright: ignore[reportAttributeAccessIssue]


def test_the_current_directory_on_sys_path_is_shown_when_it_is_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = _workspace(tmp_path)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(sys, 'path', ['', *sys.path])

    snapshot = read_snapshot(workspace / '.syndica.yaml', workspace)

    assert str(workspace) in [str(Path(entry).resolve()) for entry in snapshot.path_entries]


def test_a_non_string_modules_item_reaches_the_config_check_instead_of_crashing(tmp_path: Path):
    workspace = _workspace(tmp_path, 'version: 1\nmodules: [[nested], {a: 1}]\n')

    snapshot = read_snapshot(workspace / '.syndica.yaml', workspace)

    assert snapshot.modules == ()


def test_a_config_named_through_an_outside_link_into_the_workspace_is_gated(tmp_path: Path):
    workspace = _workspace(tmp_path)
    link = tmp_path / 'outside.yaml'
    link.symlink_to(workspace / '.syndica.yaml')
    store = _store(tmp_path)

    code, _, stderr, built = _run(workspace, store, ['--config', str(link), 'doctor'])

    assert code != 0
    assert built == []
    assert 'syndica config trust --sha256' in stderr


def test_the_digest_pins_where_outside_module_code_lives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = _workspace(tmp_path, 'version: 1\nmodules: [ext]\n')
    entry = Discovered('ext', 'extmod:build', 'ext-dist', '1.0')
    for name in ('first', 'second'):
        package = tmp_path / name / 'extmod'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('', encoding='utf-8')
    monkeypatch.setattr(sys, 'path', [str(tmp_path / 'first'), *sys.path])
    before = read_snapshot(workspace / '.syndica.yaml', workspace, [entry]).digest
    monkeypatch.setattr(sys, 'path', [str(tmp_path / 'second'), *sys.path[1:]])

    after = read_snapshot(workspace / '.syndica.yaml', workspace, [entry])

    assert not after.module_sources[0].inside
    assert after.digest != before


@pytest.mark.usefixtures('_forget_pinpkg')
def test_a_trusted_run_pins_digested_code_until_the_host_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = _workspace(
        tmp_path, 'version: 1\nagents:\n  - {name: Kilo, description: k, impl: pinpkg:Role}\n'
    )
    package = workspace / 'pinpkg'
    package.mkdir()
    (package / '__init__.py').write_text(
        'from syndica.kernel import BaseAgent\n\n\nclass Role(BaseAgent):\n    """r"""\n',
        encoding='utf-8',
    )
    (package / 'later.py').write_text('VALUE = "reviewed"\n', encoding='utf-8')
    monkeypatch.syspath_prepend(str(workspace))
    monkeypatch.setattr(sys, 'pycache_prefix', None)
    host, _, _ = _host(workspace, _store(tmp_path), stdin=_Tty('y\n'), stderr=_Tty())
    try:
        host._trusted_config('.syndica.yaml')  # pyright: ignore[reportPrivateUsage]
        (package / 'later.py').write_text('VALUE = "changed"\n', encoding='utf-8')
        with pytest.raises(ImportError):
            importlib.import_module('pinpkg.later')
    finally:
        host.close(interrupted=False)

    assert importlib.import_module('pinpkg.later').VALUE == 'changed'


@pytest.mark.usefixtures('_forget_pinpkg')
def test_a_pin_whose_file_is_gone_does_not_claim_a_file_that_reused_its_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    package, pins = _pinned_package(tmp_path, monkeypatch)
    stale = next(iter(pins._pins.values()))  # pyright: ignore[reportPrivateUsage]
    other = tmp_path / 'other'
    (other / 'otherpkg').mkdir(parents=True)
    (other / 'otherpkg' / '__init__.py').write_text('VALUE = 1\n', encoding='utf-8')
    info = (other / 'otherpkg' / '__init__.py').stat()
    pins._pins[(info.st_dev, info.st_ino)] = stale  # pyright: ignore[reportPrivateUsage]
    for path in sorted(package.iterdir(), reverse=True):
        path.unlink()
    package.rmdir()
    monkeypatch.syspath_prepend(str(other))
    try:
        assert importlib.import_module('otherpkg').VALUE == 1
    finally:
        pins.remove()
        sys.modules.pop('otherpkg', None)


def test_a_store_written_by_a_newer_syndica_is_not_trusted_and_not_overwritten(tmp_path: Path):
    store = JsonTrustStore(tmp_path / 'trusted.json')
    store.path.write_text(
        json.dumps({'version': 2, 'configs': {'/x/.syndica.yaml': 'a' * 64}}), encoding='utf-8'
    )

    assert store.get(Path('/x/.syndica.yaml')) is None
    with pytest.raises(ConfigError) as excinfo:
        store.put(Path('/y/.syndica.yaml'), 'b' * 64)

    assert 'version 2' in excinfo.value.received
    assert 'SYNDICA_TRUST_STORE' in excinfo.value.next_action
    assert json.loads(store.path.read_text(encoding='utf-8'))['version'] == 2
