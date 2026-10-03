"""Module 的发现、`modules:` 与 `modules list/show`（M4 D15，T9a）：发现只读元数据，不 import。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from syndica.cli.config import load_config
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, main
from syndica.cli.modules import (
    Discovered,
    discover,
    module_rows,
    validated_modules,
)
from syndica.cli.trust import NO_TRUST_GATE
from syndica.kernel import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]


class _Metadata(dict[str, str]):
    pass


class _Dist:
    def __init__(self, name: str, version: str) -> None:
        self.metadata = _Metadata(Name=name)
        self.version = version


class _EntryPoint:
    def __init__(self, name: str, value: str, dist: _Dist | None) -> None:
        self.name = name
        self.value = value
        self.dist = dist

    def load(self) -> Any:
        raise AssertionError(f'{self.name} was imported during discovery')


def _source(*entries: _EntryPoint):
    return lambda: entries


def _ep(name: str, dist: str = 'syndica-example-module', version: str = '1.2.0') -> _EntryPoint:
    return _EntryPoint(name, f'{dist.replace("-", "_")}:module', _Dist(dist, version))


def test_discovery_reads_metadata_sorted_by_name_without_importing():
    found = discover(_source(_ep('zeta'), _ep('alpha', 'other-dist', '0.1'), _ep('mid')))
    assert [(entry.name, entry.distribution, entry.version, entry.problem) for entry in found] == [
        ('alpha', 'other-dist', '0.1', None),
        ('mid', 'syndica-example-module', '1.2.0', None),
        ('zeta', 'syndica-example-module', '1.2.0', None),
    ]
    assert found[0].target == 'other_dist:module'


@pytest.mark.parametrize('name', ['retry', 'safety', 'dev', 'read-only'])
def test_an_entry_point_named_like_a_builtin_is_not_accepted(name: str):
    (entry,) = discover(_source(_ep(name)))
    assert entry.problem == '与内置 Module 重名，不收'


@pytest.mark.parametrize('name', ['My_Module', 'audit-', '1audit'])
def test_an_entry_point_with_a_non_canonical_name_is_not_accepted(name: str):
    (entry,) = discover(_source(_ep(name)))
    assert entry.problem is not None
    assert '规范拼法' in entry.problem


def test_two_distributions_offering_one_name_are_both_ambiguous():
    found = discover(_source(_ep('audit', 'dist-b', '2.0'), _ep('audit', 'dist-a', '1.0')))
    assert len(found) == 2
    assert all(entry.problem is not None and '有歧义' in entry.problem for entry in found)
    assert "'dist-a 1.0'" in str(found[0].problem)


def test_an_entry_point_without_a_distribution_is_still_listed():
    (entry,) = discover(_source(_EntryPoint('loose', 'loose:module', None)))
    assert (entry.distribution, entry.version, entry.problem) == (None, None, None)


def test_module_rows_put_builtins_first_in_install_order_then_third_parties():
    discovered = discover(
        _source(_ep('zeta'), _ep('retry'), _EntryPoint('loose', 'x:y', None), _ep('alpha'))
    )
    rows = module_rows(('prefix-drift', 'alpha'), discovered)
    assert [(row.name, row.status) for row in rows] == [
        ('safety', '必装'),
        ('human-interaction', '必装'),
        ('retry', '可启用'),
        ('prefix-drift', '已启用'),
        ('read-only', '开发期（命令行决定）'),
        ('dev', '开发期（命令行决定）'),
        ('alpha', '已启用'),
        ('loose', '可启用'),
        ('retry', '坏的：与内置 Module 重名，不收'),
        ('zeta', '可启用'),
    ]
    assert rows[0].source.startswith('内置 ')
    assert rows[7].source == '来源不明'
    assert rows[6].source == 'syndica-example-module 1.2.0'


def test_only_optional_builtins_can_be_enabled_in_this_version():
    discovered = discover(_source(_ep('alpha')))
    assert validated_modules(('prefix-drift', 'retry'), discovered) == ('prefix-drift', 'retry')


@pytest.mark.parametrize(
    ('name', 'anchor'),
    [
        ('safety', '宿主必装'),
        ('human-interaction', '宿主必装'),
        ('dev', '开发期'),
        ('read-only', '开发期'),
        ('alpha', '信任确认'),
        ('nowhere', 'syndica modules list'),
        ('retry-x', 'syndica modules list'),
    ],
)
def test_names_that_cannot_be_enabled_are_refused(name: str, anchor: str):
    discovered = discover(_source(_ep('alpha')))
    with pytest.raises(ConfigError) as excinfo:
        validated_modules((name,), discovered)
    assert anchor in f'{excinfo.value.received}{excinfo.value.next_action}'


def test_an_ambiguous_module_is_refused_with_the_reason():
    discovered = discover(_source(_ep('audit', 'dist-a'), _ep('audit', 'dist-b')))
    with pytest.raises(ConfigError) as excinfo:
        validated_modules(('audit',), discovered)
    assert '有歧义' in excinfo.value.received


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / '.syndica.yaml'
    path.write_text(f'version: 1\n{body}', encoding='utf-8')
    return path


def test_the_config_keeps_the_order_modules_are_written_in(tmp_path: Path):
    assert load_config(_write(tmp_path, 'modules: [prefix-drift, retry]\n')).modules == (
        'prefix-drift',
        'retry',
    )


@pytest.mark.parametrize(
    ('body', 'anchor'),
    [
        ('modules: retry\n', 'YAML 列表'),
        ('modules: [Retry]\n', 'Module 名'),
        ('modules: [1]\n', 'Module 名'),
        ('modules: [retry, retry]\n', '只写一次'),
    ],
)
def test_malformed_module_lists_are_refused(tmp_path: Path, body: str, anchor: str):
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write(tmp_path, body))
    assert anchor in excinfo.value.expected


def _host(tmp_path: Path, *entries: _EntryPoint) -> tuple[RuntimeCliHost, CliIO]:
    io = CliIO(stdin=StringIO(''), stdout=StringIO(), stderr=StringIO())
    host = RuntimeCliHost(
        trust=NO_TRUST_GATE,
        io=io,
        model_factory=lambda _: None,  # pyright: ignore[reportArgumentType]
        workspace_root=tmp_path,
        environ={},
        entry_points=_source(*entries),
    )
    return host, io


def _cli(tmp_path: Path, argv: list[str], *entries: _EntryPoint) -> tuple[int, str, str]:
    host, io = _host(tmp_path, *entries)
    try:
        code = main(argv, host=host, io=io)
    finally:
        host.close(interrupted=False)
    return code, io.stdout.getvalue(), io.stderr.getvalue()  # pyright: ignore[reportAttributeAccessIssue]


def test_modules_list_prints_name_source_and_status_without_importing(tmp_path: Path):
    _write(tmp_path, 'modules: [retry]\n')
    code, stdout, stderr = _cli(tmp_path, ['modules', 'list'], _ep('alpha'))
    assert code == 0, stderr
    lines = stdout.splitlines()
    assert lines[2].split('\t') == ['retry', lines[2].split('\t')[1], '已启用']
    assert lines[-1] == 'alpha\tsyndica-example-module 1.2.0\t可启用'


def test_modules_list_escapes_what_a_distribution_says_about_itself(tmp_path: Path):
    _write(tmp_path, '')
    spoof = _EntryPoint('alpha', 'x:y', _Dist('evil\x1b[2J\nsafety\t内置\t必装', '1\u202e0'))
    code, stdout, _ = _cli(tmp_path, ['modules', 'list'], spoof)
    assert code == 0
    assert '\x1b' not in stdout
    assert '\u202e' not in stdout
    assert stdout.splitlines()[-1].startswith('alpha\t')
    assert len(stdout.splitlines()) == 7


def test_modules_show_prints_a_builtin_manifest(tmp_path: Path):
    _write(tmp_path, '')
    code, stdout, stderr = _cli(tmp_path, ['modules', 'show', 'retry'])
    assert code == 0, stderr
    shown = json.loads(stdout)
    assert list(shown) == [
        'name',
        'version',
        'description',
        'capabilities',
        'requires',
        'junctions',
        'replay_safe',
        'security_notes',
        'flags',
    ]
    assert (shown['name'], shown['capabilities'], shown['replay_safe']) == ('retry', [], True)
    assert shown['flags'] == ['llm_retry_attempts', 'llm_retry_backoff_ms', 'llm_fallbacks']


def test_modules_show_of_a_module_it_does_not_know_says_where_to_look(tmp_path: Path):
    _write(tmp_path, '')
    code, _, stderr = _cli(tmp_path, ['modules', 'show', 'alpha'], _ep('alpha'))
    assert code != 0
    assert 'syndica modules list' in stderr


def test_the_module_command_lists_builtins_from_the_real_entry_point(tmp_path: Path):
    completed = subprocess.run(
        [sys.executable, '-m', 'syndica', 'modules', 'list'],
        cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(REPO_ROOT / 'src')},
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines()[0].startswith('safety\t内置 ')


def test_a_discovered_module_is_plain_data():
    assert Discovered('a', 'x:y', None, None) == Discovered('a', 'x:y', None, None)
