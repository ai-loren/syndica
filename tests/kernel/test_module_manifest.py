from __future__ import annotations

import copy
import dataclasses
from pathlib import Path

import pytest

from syndica import __version__
from syndica.kernel.errors import ConfigError
from syndica.kernel.hooks import ExtensionJunction, JunctionKind
from syndica.kernel.modules import MODULE_CAPABILITIES, ModuleHost, RuntimeModule, SafetyModule
from syndica.kernel.prefix_drift import PrefixDriftModule
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.retry import RetryModule
from syndica.kernel.runtime import Runtime
from syndica.kernel.tools.human import HumanInteractionModule


def _three_parts(error: BaseException) -> None:
    text = str(error)
    assert '期望:' in text
    assert '实际:' in text
    assert '下一步:' in text


def _handle(name: str, kind: JunctionKind = JunctionKind.OBSERVABLE) -> ExtensionJunction:
    return ExtensionJunction(name, kind)


@pytest.mark.parametrize(
    'name',
    [
        '',
        '  ',
        None,
        True,
        b'audit',
        'Audit',
        'my_module',
        '1audit',
        'audit-',
        '-audit',
        'au--dit',
        'audit:x',
        'au dit',
        ' audit',
        'audit\n',
        'audité',
    ],
)
def test_module_name_must_be_the_canonical_spelling(name: object):
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule(name)  # pyright: ignore[reportArgumentType]
    assert 'Module 名' in excinfo.value.expected
    assert repr(name) in excinfo.value.received
    assert '不做任何归一化' in excinfo.value.next_action
    _three_parts(excinfo.value)


@pytest.mark.parametrize('name', ['a', 'retry', 'prefix-drift', 'a1-b2-c3', 'x9'])
def test_module_name_accepts_lowercase_hyphenated_words(name: str):
    assert RuntimeModule(name).name == name


def test_manifest_defaults_mean_undeclared():
    module = RuntimeModule('audit')
    assert (
        module.version,
        module.description,
        module.capabilities,
        module.requires,
        module.junctions,
        module.replay_safe,
        module.security_notes,
    ) == ('0.0.0', '', None, (), (), False, '')


@pytest.mark.parametrize('version', ['1.2.0', '1.0.0rc1', '2026.10.3', '0.0.0+local.7'])
def test_version_accepts_release_strings_verbatim(version: str):
    assert RuntimeModule('audit', version=version).version == version


@pytest.mark.parametrize(
    'version',
    ['', ' 1.0', '1.0 ', '1 .0', '1.0\t', '1.0\u200e', '1.0\x1b', True, 1, None, b'1.0'],
)
def test_version_rejects_blank_spaced_controlled_and_non_string_values(version: object):
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('audit', version=version)  # pyright: ignore[reportArgumentType]
    assert 'version' in excinfo.value.expected
    assert repr(version) in excinfo.value.received
    assert '只比较是否相等' in excinfo.value.next_action
    _three_parts(excinfo.value)


@pytest.mark.parametrize('description', ['一行说明', 'tab\tinside', ''])
def test_description_accepts_one_line_of_plain_text(description: str):
    assert RuntimeModule('audit', description=description).description == description


def test_description_rejects_a_second_line_and_says_where():
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('audit', description='two\nlines')
    assert '只占一行' in excinfo.value.expected
    assert '第 3 个字符' in excinfo.value.received
    assert 'security_notes' in excinfo.value.next_action
    _three_parts(excinfo.value)


@pytest.mark.parametrize('field', ['description', 'security_notes'])
@pytest.mark.parametrize(
    ('text', 'position'),
    [
        ('\x1b[31mred', 0),
        ('ok\x9bx', 2),
        ('ab\u202ecd', 2),
        ('ab\u2066cd', 2),
        ('ab\u2028cd', 2),
        ('ab\ud800', 2),
        ('ab\rcd', 2),
    ],
)
def test_shown_text_rejects_characters_that_rewrite_the_terminal(
    field: str, text: str, position: int
):
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('audit', **{field: text})  # pyright: ignore[reportArgumentType]
    assert '控制字符' in excinfo.value.expected
    assert f'第 {position} 个字符' in excinfo.value.received
    assert '伪造显示' in excinfo.value.next_action
    _three_parts(excinfo.value)


@pytest.mark.parametrize('field', ['description', 'security_notes'])
@pytest.mark.parametrize('value', [None, True, 1, b'text'])
def test_shown_text_must_be_a_string(field: str, value: object):
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('audit', **{field: value})  # pyright: ignore[reportArgumentType]
    assert f'RuntimeModule.{field} 是字符串' == excinfo.value.expected
    assert repr(value) in excinfo.value.received
    _three_parts(excinfo.value)


def test_security_notes_keep_line_breaks_and_tabs():
    notes = '读 HOME 下的配置。\n\t只读，不外发。'
    assert RuntimeModule('audit', security_notes=notes).security_notes == notes


def test_capability_vocabulary_is_exactly_four_words():
    assert MODULE_CAPABILITIES == ('environment', 'filesystem', 'network', 'subprocess')


@pytest.mark.parametrize(
    ('declared', 'kept'),
    [
        ((), ()),
        (['network'], ('network',)),
        (('subprocess', 'environment', 'filesystem'), ('subprocess', 'environment', 'filesystem')),
    ],
)
def test_capabilities_keep_the_declared_order_as_a_tuple(
    declared: tuple[str, ...] | list[str], kept: tuple[str, ...]
):
    assert RuntimeModule('audit', capabilities=declared).capabilities == kept


@pytest.mark.parametrize(
    'declared',
    [[True], ['Network'], ['net'], [None], ['network ']],
)
def test_capabilities_reject_words_outside_the_vocabulary(declared: list[object]):
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('audit', capabilities=declared)  # pyright: ignore[reportArgumentType]
    assert 'environment' in excinfo.value.expected
    assert 'capabilities[0]' in excinfo.value.received
    assert '拿不准就省略' in excinfo.value.next_action
    _three_parts(excinfo.value)


def test_capabilities_reject_a_repeated_word():
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('audit', capabilities=['network', 'filesystem', 'network'])
    assert 'capabilities[2]' in excinfo.value.received
    _three_parts(excinfo.value)


@pytest.mark.parametrize('declared', ['network', {'network'}, 3])
def test_capabilities_must_be_a_list_or_tuple(declared: object):
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('audit', capabilities=declared)  # pyright: ignore[reportArgumentType]
    assert 'list / tuple' in excinfo.value.expected
    _three_parts(excinfo.value)


def test_requires_keeps_the_declared_order_as_a_tuple():
    assert RuntimeModule('audit', requires=['retry', 'prefix-drift']).requires == (
        'retry',
        'prefix-drift',
    )


@pytest.mark.parametrize(
    ('declared', 'anchor'),
    [
        (['Retry'], 'Module 名'),
        ([None], 'Module 名'),
        ([True], 'Module 名'),
        (['retry', 'retry'], '只写一次'),
        (['audit'], '不依赖自己'),
    ],
)
def test_requires_rejects_bad_names_repeats_and_itself(declared: list[object], anchor: str):
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('audit', requires=declared)  # pyright: ignore[reportArgumentType]
    assert anchor in excinfo.value.expected
    _three_parts(excinfo.value)


@pytest.mark.parametrize('declared', ['retry', {'retry'}])
def test_requires_must_be_a_list_or_tuple(declared: object):
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('audit', requires=declared)  # pyright: ignore[reportArgumentType]
    assert 'list / tuple' in excinfo.value.expected


def test_junctions_keep_the_handles_by_identity():
    first = _handle('prefix-drift:warned')
    second = _handle('prefix-drift:cleared', JunctionKind.INTERCEPTABLE)
    module = RuntimeModule('prefix-drift', junctions=[first, second])
    assert module.junctions[0] is first
    assert module.junctions[1] is second


def test_junctions_reject_a_name_instead_of_a_handle():
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('audit', junctions=['audit:recorded'])  # pyright: ignore[reportArgumentType]
    assert 'ExtensionJunction' in excinfo.value.expected
    assert "'audit:recorded'" in excinfo.value.received
    assert 'ExtensionJunction("audit:<event>"' in excinfo.value.next_action
    _three_parts(excinfo.value)


@pytest.mark.parametrize('foreign', ['other:recorded', 'audit-log:recorded', 'audi:recorded'])
def test_junctions_reject_a_handle_owned_by_another_name(foreign: str):
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('audit', junctions=[_handle(foreign)])
    assert "owner 段是 'audit'" in excinfo.value.expected
    assert repr(foreign) in excinfo.value.received
    assert 'audit:<event>' in excinfo.value.next_action
    _three_parts(excinfo.value)


def test_junctions_reject_two_handles_with_one_name():
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule(
            'audit',
            junctions=[_handle('audit:recorded'), _handle('audit:recorded')],
        )
    assert '只出现一次' in excinfo.value.expected
    assert 'junctions[1]' in excinfo.value.received
    _three_parts(excinfo.value)


@pytest.mark.parametrize('declared', ['audit:recorded', 3])
def test_junctions_must_be_a_list_or_tuple(declared: object):
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('audit', junctions=declared)  # pyright: ignore[reportArgumentType]
    assert 'list / tuple' in excinfo.value.expected


@pytest.mark.parametrize('value', [True, False])
def test_replay_safe_accepts_a_bool(value: bool):
    assert RuntimeModule('audit', replay_safe=value).replay_safe is value


@pytest.mark.parametrize('value', [1, 0, 'true', None])
def test_replay_safe_rejects_everything_but_a_bool(value: object):
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('audit', replay_safe=value)  # pyright: ignore[reportArgumentType]
    assert 'bool' in excinfo.value.expected
    assert repr(value) in excinfo.value.received
    assert '缺省 False' in excinfo.value.next_action
    _three_parts(excinfo.value)


def test_two_handles_with_one_name_are_different_capabilities():
    first = _handle('audit:recorded')
    second = _handle('audit:recorded')
    assert first != second
    assert len({first, second}) == 2


def test_copying_a_handle_keeps_its_identity():
    handle = _handle('audit:recorded')
    assert copy.copy(handle) is handle
    assert copy.deepcopy(handle) is handle
    assert copy.deepcopy({'junctions': [handle]})['junctions'][0] is handle


@pytest.mark.parametrize(
    ('name', 'owner'),
    [('audit:recorded', 'audit'), ('prefix-drift:warned', 'prefix-drift'), ('a:b-c', 'a')],
)
def test_handle_owner_is_the_segment_before_the_separator(name: str, owner: str):
    assert _handle(name).owner == owner


def test_handle_reports_name_kind_and_repr():
    handle = _handle('audit:recorded', JunctionKind.INTERCEPTABLE)
    assert (handle.name, handle.kind) == ('audit:recorded', JunctionKind.INTERCEPTABLE)
    assert repr(handle) == "ExtensionJunction('audit:recorded', JunctionKind.INTERCEPTABLE)"


def test_handle_cannot_be_renamed_or_extended():
    handle = _handle('audit:recorded')
    with pytest.raises(AttributeError):
        handle.name = 'audit:other'  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(AttributeError):
        handle.extra = 1  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.parametrize('name', ['recorded', 'a:b:c', ':recorded', 'audit:', 'a b:c', None])
def test_handle_rejects_malformed_names(name: object):
    with pytest.raises(ConfigError) as excinfo:
        ExtensionJunction(name, JunctionKind.OBSERVABLE)  # pyright: ignore[reportArgumentType]
    _three_parts(excinfo.value)


@pytest.mark.parametrize('kind', ['observable', None, 1])
def test_handle_rejects_a_kind_that_is_not_a_member(kind: object):
    with pytest.raises(ConfigError) as excinfo:
        ExtensionJunction('audit:recorded', kind)  # pyright: ignore[reportArgumentType]
    assert 'JunctionKind' in excinfo.value.expected


async def test_runtime_installs_modules_whose_dependencies_come_first():
    runtime = Runtime(modules=[RuntimeModule('audit'), RuntimeModule('report', requires=['audit'])])
    await runtime.shutdown()


def test_runtime_reports_a_dependency_installed_later_as_an_ordering_error():
    with pytest.raises(ConfigError) as excinfo:
        Runtime(modules=[RuntimeModule('report', requires=['audit']), RuntimeModule('audit')])
    assert '排在它前面' in excinfo.value.expected
    assert "modules[0]='report'" in excinfo.value.received
    assert "把 'audit' 挪到 'report' 前面" in excinfo.value.next_action
    _three_parts(excinfo.value)


def test_runtime_reports_a_missing_dependency_separately_from_ordering():
    with pytest.raises(ConfigError) as excinfo:
        Runtime(modules=[RuntimeModule('audit'), RuntimeModule('report', requires=['ledger'])])
    assert "没有 'ledger'" in excinfo.value.received
    assert "'audit'" in excinfo.value.received
    assert "把 'ledger' 加进 modules" in excinfo.value.next_action
    _three_parts(excinfo.value)


def test_a_missing_dependency_is_refused_before_any_module_starts():
    started: list[str] = []
    with pytest.raises(ConfigError):
        Runtime(
            modules=[
                RuntimeModule('audit', on_start=lambda runtime, flags: started.append('audit')),
                RuntimeModule('report', requires=['ledger']),
            ]
        )
    assert started == []


def test_module_host_fields_are_append_only_in_this_order():
    assert [field.name for field in dataclasses.fields(ModuleHost)] == [
        'workspace_root',
        'environment',
        'redactor',
    ]


def test_module_host_keeps_its_own_read_only_environment(tmp_path: Path):
    environment = {'PATH': '/usr/bin'}
    host = ModuleHost(tmp_path, environment, RuleRedactor())
    environment['PATH'] = '/tmp/evil'
    environment['EXTRA'] = '1'
    assert dict(host.environment) == {'PATH': '/usr/bin'}
    with pytest.raises(TypeError):
        host.environment['PATH'] = 'x'  # pyright: ignore[reportIndexIssue]
    with pytest.raises(dataclasses.FrozenInstanceError):
        host.workspace_root = Path('/')  # pyright: ignore[reportAttributeAccessIssue]


def test_module_host_rejects_a_string_workspace_root(tmp_path: Path):
    with pytest.raises(ConfigError) as excinfo:
        ModuleHost(str(tmp_path), {}, RuleRedactor())  # pyright: ignore[reportArgumentType]
    assert 'pathlib.Path' in excinfo.value.expected
    assert 'Path(' in excinfo.value.next_action
    _three_parts(excinfo.value)


@pytest.mark.parametrize('environment', [{'PATH': 1}, {1: 'x'}, ['PATH'], None])
def test_module_host_rejects_an_environment_that_is_not_str_to_str(
    tmp_path: Path, environment: object
):
    with pytest.raises(ConfigError) as excinfo:
        ModuleHost(tmp_path, environment, RuleRedactor())  # pyright: ignore[reportArgumentType]
    assert 'str 到 str' in excinfo.value.expected
    assert type(environment).__name__ in excinfo.value.received
    _three_parts(excinfo.value)


def test_module_host_rejects_something_that_cannot_redact(tmp_path: Path):
    with pytest.raises(ConfigError) as excinfo:
        ModuleHost(tmp_path, {}, object())  # pyright: ignore[reportArgumentType]
    assert 'redact' in excinfo.value.expected
    assert 'object' in excinfo.value.received
    _three_parts(excinfo.value)


def test_kernel_built_in_modules_declare_their_manifest_explicitly():
    modules = [SafetyModule(), RetryModule({}), PrefixDriftModule(), HumanInteractionModule()]
    assert {
        module.name: (module.version, module.capabilities, module.replay_safe) for module in modules
    } == {
        'safety': (__version__, (), True),
        'retry': (__version__, (), True),
        'prefix-drift': (__version__, (), True),
        'human-interaction': (__version__, (), True),
    }
    assert all(module.description and '\n' not in module.description for module in modules)
    assert 'untrusted_high_risk=allow' in SafetyModule().security_notes
