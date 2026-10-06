"""Flag 的参考（M4 D15，T10）：`flags list/show` 与 `docs/reference/flags.md` 出自同一个生成器；文档
另有一份独立写死的金标，防生成器自己写错。"""

from __future__ import annotations

import json
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from syndica.cli.flags import render_reference
from syndica.cli.host import RuntimeCliHost, reference_modules
from syndica.cli.main import CliIO, InspectRequest, RunRequest
from syndica.cli.trust import NO_TRUST_GATE, JsonTrustStore, TrustGate
from syndica.kernel import (
    ConfigError,
    FlagDeclaration,
    FlagRow,
    ModuleHost,
    RuntimeModule,
    ScriptedLLM,
    flag_reference,
)

_REFERENCE = Path(__file__).resolve().parents[2] / 'docs' / 'reference' / 'flags.md'


def test_the_reference_document_is_what_the_generator_writes_today():
    expected = render_reference(flag_reference(reference_modules()))
    assert _REFERENCE.read_text(encoding='utf-8') == expected, (
        '内置 Module 的 Flag 声明改了：运行 uv run python scripts/gen_flag_reference.py 重新生成'
    )


def _table_rows() -> list[list[str]]:
    lines = _REFERENCE.read_text(encoding='utf-8').splitlines()
    start = lines.index('| --- | --- | --- | --- | --- | --- | --- |') + 1
    return [[cell.strip() for cell in line.strip('|').split(' | ')] for line in lines[start:]]


def test_the_reference_document_matches_an_independent_golden():
    rows = _table_rows()
    assert [row[0] for row in rows] == [
        '`max_wait_seconds`',
        '`untrusted_high_risk`',
        '`llm_retry_attempts`',
        '`llm_retry_backoff_ms`',
        '`llm_fallbacks`',
        '`prefix_drift_detection`',
        '`compaction_strategy`',
        '`compaction_threshold_percent`',
        '`compaction_target_percent`',
        '`compaction_context_tokens`',
        '`memory_recall`',
        '`memory_consolidate_after`',
        '`acceptance_rubrics`',
        '`acceptance_output`',
        '`acceptance_output_retries`',
        '`acceptance_outcome`',
        '`acceptance_outcome_block_retries`',
        '`acceptance_outcome_partial_retries`',
        '`acceptance_feedback`',
        '`acceptance_verifier_timeout`',
        '`acceptance_verifier_max_turns`',
        '`long_horizon_progress_every`',
        '`long_horizon_stall_turns`',
        '`long_horizon_budget_low_percent`',
        '`long_horizon_drift_window`',
        '`long_horizon_semantic_drift`',
        '`long_horizon_max_continuations`',
        '`background_after_seconds`',
    ]
    by_key = {row[0]: row for row in rows}
    assert by_key['`untrusted_high_risk`'] == [
        '`untrusted_high_risk`',
        '`safety`',
        'str',
        '`"reject"`',
        '`"reject"` / `"ask"`',
        '不收 allow；项目配置里只收 reject',
        '高风险调用可溯源到不可信内容时的处置',
    ]
    assert by_key['`max_wait_seconds`'][2:5] == ['int', '`0`', '≥ 0']
    assert by_key['`prefix_drift_detection`'][1:5] == ['`prefix-drift`', 'bool', '`true`', '—']
    assert by_key['`llm_fallbacks`'][3:6] == [
        '`""`',
        '—',
        '只收空值：按名字启用的 retry 没有备用模型',
    ]
    assert by_key['`compaction_strategy`'][1:4] == ['`compaction`', 'str', '`"off"`']
    assert by_key['`memory_consolidate_after`'][1:4] == ['`memory`', 'int', '`50`']
    assert by_key['`acceptance_outcome`'][1:5] == ['`acceptance`', 'bool', '`false`', '—']
    assert by_key['`acceptance_output_retries`'][2:5] == ['int', '`1`', '≥ 0']
    assert by_key['`acceptance_verifier_timeout`'][2:5] == ['int', '`300`', '≥ 1']
    assert by_key['`long_horizon_semantic_drift`'][1:5] == [
        '`long-horizon`',
        'bool',
        '`false`',
        '—',
    ]
    assert by_key['`long_horizon_budget_low_percent`'][2:5] == ['int', '`20`', '0 到 99']


def _module(name: str, *keys: str) -> RuntimeModule:
    return RuntimeModule(
        name,
        flags=tuple(
            FlagDeclaration(key=key, default=0, description=f'{key} 的说明') for key in keys
        ),
    )


def test_the_generator_keeps_module_order_then_declaration_order():
    rows = flag_reference(
        [_module('zulu', 'zulu_b', 'zulu_c', 'zulu_a'), _module('alpha', 'alpha_y', 'alpha_x')]
    )
    assert [(row.module, row.key) for row in rows] == [
        ('zulu', 'zulu_b'),
        ('zulu', 'zulu_c'),
        ('zulu', 'zulu_a'),
        ('alpha', 'alpha_y'),
        ('alpha', 'alpha_x'),
    ]
    assert rows[0] == FlagRow(
        key='zulu_b',
        module='zulu',
        kind='int',
        default=0,
        choices=(),
        minimum=None,
        maximum=None,
        description='zulu_b 的说明',
    )


@pytest.mark.parametrize(
    'description', ['两行\n说明', 'bell\x07', 'rtl\u202eevil', 'esc\x1b[2J'], ids=repr
)
def test_a_flag_description_that_could_rewrite_the_terminal_is_refused(description: str):
    with pytest.raises(ConfigError) as excinfo:
        FlagDeclaration(key='alpha_x', default=0, description=description)
    assert '单行文本' in excinfo.value.expected


def _host(
    tmp_path: Path,
    entry_points: Callable[[], list[Any]] | None = None,
    *,
    trust: TrustGate = NO_TRUST_GATE,
) -> RuntimeCliHost:
    return RuntimeCliHost(
        trust=trust,
        io=CliIO(stdin=StringIO(), stdout=StringIO(), stderr=StringIO()),
        model_factory=lambda _: ScriptedLLM([]),
        workspace_root=tmp_path,
        environ={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path)},
        entry_points=cast(Any, entry_points or (lambda: [])),
    )


def _inspect(
    host: RuntimeCliHost, config: Path, action: Literal['list', 'show'], name: str | None = None
) -> str:
    return host.inspect(
        InspectRequest(resource='flags', action=action, name=name, config=str(config))
    )


def test_flags_list_follows_the_modules_the_config_enables(tmp_path: Path):
    config = tmp_path / '.syndica.yaml'
    config.write_text('version: 1\nmodules: [retry]\n', encoding='utf-8')
    host = _host(tmp_path)
    try:
        listed = _inspect(host, config, 'list')
    finally:
        host.close()
    assert [line.split('\t')[:2] for line in listed.splitlines()] == [
        ['max_wait_seconds', 'safety'],
        ['untrusted_high_risk', 'safety'],
        ['llm_retry_attempts', 'retry'],
        ['llm_retry_backoff_ms', 'retry'],
        ['llm_fallbacks', 'retry'],
    ]


@pytest.mark.parametrize(
    ('body', 'effective', 'source'),
    [('', 0, '缺省'), ('flags:\n  max_wait_seconds: 30\n', 30, 'flags:')],
    ids=['default', 'configured'],
)
def test_flags_show_gives_the_value_in_effect_and_where_it_comes_from(
    tmp_path: Path, body: str, effective: int, source: str
):
    config = tmp_path / '.syndica.yaml'
    config.write_text(f'version: 1\n{body}', encoding='utf-8')
    host = _host(tmp_path)
    try:
        shown = json.loads(_inspect(host, config, 'show', 'max_wait_seconds'))
    finally:
        host.close()
    assert (shown['effective'], shown['source']) == (effective, source)
    assert shown['default'] == 0


class _EntryPoint:
    def __init__(self, factory: Callable[[ModuleHost], RuntimeModule]) -> None:
        self.name = 'alpha'
        self.value = 'alpha_pkg.module:build'
        self.dist = None
        self._factory = factory

    def load(self) -> object:
        return self._factory


def _alpha(host: ModuleHost) -> RuntimeModule:
    del host
    return RuntimeModule(
        'alpha',
        flags=(FlagDeclaration(key='alpha_greeting', default='hello', description='问候语'),),
    )


def test_flags_of_an_enabled_third_party_module_are_listed_after_confirmation(tmp_path: Path):
    config = tmp_path / '.syndica.yaml'
    config.write_text('version: 1\nmodules: [alpha]\n', encoding='utf-8')
    host = _host(tmp_path, lambda: [_EntryPoint(_alpha)])
    try:
        listed = _inspect(host, config, 'list')
    finally:
        host.close()
    assert listed.splitlines()[-1] == 'alpha_greeting\talpha\tstr\t"hello"'


def test_listing_the_flags_of_a_third_party_module_needs_the_config_confirmed(tmp_path: Path):
    config = tmp_path / '.syndica.yaml'
    config.write_text('version: 1\nmodules: [alpha]\n', encoding='utf-8')
    imported: list[str] = []

    def load() -> object:
        imported.append('alpha')
        return _alpha

    point = _EntryPoint(_alpha)
    point.load = load  # type: ignore[method-assign]
    store = JsonTrustStore(tmp_path.parent / f'{tmp_path.name}-store' / 'trusted.json')
    host = _host(tmp_path, lambda: [point], trust=TrustGate(store_for=lambda _: store))
    try:
        with pytest.raises(ConfigError) as excinfo:
            _inspect(host, config, 'list')
    finally:
        host.close()
    assert 'config trust --sha256' in excinfo.value.next_action
    assert imported == []


def test_modules_show_imports_a_third_party_module_on_request(tmp_path: Path):
    config = tmp_path / '.syndica.yaml'
    config.write_text('version: 1\n', encoding='utf-8')
    host = _host(tmp_path, lambda: [_EntryPoint(_alpha)])
    try:
        shown = json.loads(
            host.inspect(
                InspectRequest(resource='modules', action='show', name='alpha', config=str(config))
            )
        )
    finally:
        host.close()
    assert shown['name'] == 'alpha'
    assert shown['flags'] == ['alpha_greeting']
    assert shown['capabilities'] == '未声明'


@pytest.mark.parametrize(
    ('body', 'flags'),
    [('flags:\n  llm_fallbacks: other\n', ()), ('', ('llm_fallbacks=other',))],
    ids=['config', 'command-line'],
)
def test_fallback_models_are_refused_because_the_cli_gives_retry_none(
    tmp_path: Path, body: str, flags: tuple[str, ...]
):
    config = tmp_path / '.syndica.yaml'
    config.write_text(f'version: 1\nmodules: [retry]\n{body}', encoding='utf-8')
    host = _host(tmp_path)
    try:
        with pytest.raises(ConfigError) as excinfo:
            host.run_request(RunRequest(prompt='go', config=str(config), flags=flags))
    finally:
        host.close()
    assert 'llm_fallbacks 是空的' in excinfo.value.expected
    assert '<平台>:<型号>' in excinfo.value.next_action


@pytest.mark.parametrize('character', ['\u202e', '\x9b', '\x1b'], ids=['rlo', 'c1-csi', 'esc'])
@pytest.mark.parametrize('where', ['default', 'choices'])
def test_a_flag_string_value_that_could_rewrite_the_terminal_is_refused(character: str, where: str):
    value = f'he{character}llo'
    with pytest.raises(ConfigError) as excinfo:
        if where == 'default':
            FlagDeclaration(key='alpha_x', default=value, description='d')
        else:
            FlagDeclaration(
                key='alpha_x', default='hello', description='d', choices=('hello', value)
            )
    assert '控制字符' in excinfo.value.expected
    assert repr(character) in excinfo.value.received


def test_flags_show_escapes_a_configured_value_that_could_rewrite_the_terminal(tmp_path: Path):
    config = tmp_path / '.syndica.yaml'
    config.write_text(
        'version: 1\nmodules: [alpha]\nflags:\n  alpha_greeting: "hi\\u202eyo"\n', encoding='utf-8'
    )
    host = _host(tmp_path, lambda: [_EntryPoint(_alpha)])
    try:
        shown = _inspect(host, config, 'show', 'alpha_greeting')
    finally:
        host.close()
    assert '\u202e' not in shown
    assert '\\u202e' in shown


@pytest.mark.parametrize(('value', 'refused'), [(10, False), (11, True)])
def test_a_configured_flag_above_its_maximum_is_refused_before_anything_runs(
    tmp_path: Path, value: int, refused: bool
):
    config = tmp_path / '.syndica.yaml'
    config.write_text(
        f'version: 1\nmodules: [retry]\nflags:\n  llm_retry_attempts: {value}\n', encoding='utf-8'
    )
    host = _host(tmp_path)
    try:
        if refused:
            with pytest.raises(ConfigError) as excinfo:
                _inspect(host, config, 'show', 'llm_retry_attempts')
            assert '小于或等于 10' in excinfo.value.expected
        else:
            shown = json.loads(_inspect(host, config, 'show', 'llm_retry_attempts'))
            assert shown['effective'] == 10
    finally:
        host.close()
