"""项目配置的信任确认（M4 D15，T11）：确认之前不执行配置里的任何东西，摘要盖住实际加载的东西，
只认人。"""

from __future__ import annotations

import json
import sys
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
    TrustGate,
    config_of,
    describe,
    read_snapshot,
    store_path,
)
from syndica.kernel import ConfigError, ScriptedLLM, ScriptedTurn


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
