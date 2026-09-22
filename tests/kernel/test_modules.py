from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

import pytest

from syndica.kernel.errors import ConfigError
from syndica.kernel.modules import FlagDeclaration, RuntimeModule


def _noop(*_: object) -> None:
    return None


async def _async_noop(*_: object) -> None:
    return None


def _no_args() -> None:
    return None


async def _async_no_args() -> None:
    return None


def _wrapped_shutdown(_: object) -> Awaitable[None]:
    return _async_noop()


def _empty_tools(*_: object) -> Mapping[str, Any]:
    return {}


def _three_parts(error: BaseException) -> None:
    text = str(error)
    assert '期望:' in text
    assert '实际:' in text
    assert '下一步:' in text


def test_flag_declaration_field_order_is_public():
    assert [field.name for field in dataclasses.fields(FlagDeclaration)] == [
        'key',
        'default',
        'description',
        'choices',
        'minimum',
        'maximum',
    ]


def test_flag_declaration_constructor_signature_is_public():
    assert str(inspect.signature(FlagDeclaration)) == (
        "(key: 'str', default: 'FlagValue', description: 'str', "
        "choices: 'tuple[FlagValue, ...]' = (), minimum: 'int | None' = None, "
        "maximum: 'int | None' = None) -> None"
    )


def test_runtime_module_field_order_is_public():
    assert [field.name for field in dataclasses.fields(RuntimeModule)] == [
        'name',
        'flags',
        'agent_plugins',
        'services',
        'tool_factories',
        'on_start',
        'on_shutdown',
    ]


def test_runtime_module_constructor_shape_is_public():
    parameters = inspect.signature(RuntimeModule).parameters
    assert [
        (parameter.name, parameter.kind.name, parameter.default is not inspect.Parameter.empty)
        for parameter in parameters.values()
    ] == [
        ('name', 'POSITIONAL_OR_KEYWORD', False),
        ('flags', 'KEYWORD_ONLY', True),
        ('agent_plugins', 'KEYWORD_ONLY', True),
        ('services', 'KEYWORD_ONLY', True),
        ('tool_factories', 'KEYWORD_ONLY', True),
        ('on_start', 'KEYWORD_ONLY', True),
        ('on_shutdown', 'KEYWORD_ONLY', True),
    ]
    assert RuntimeModule.__init__.__annotations__ == {
        'name': 'str',
        'flags': 'Sequence[FlagDeclaration]',
        'agent_plugins': 'Sequence[AgentPluginFactory]',
        'services': 'Mapping[str, RuntimeServiceFactory] | None',
        'tool_factories': 'Sequence[AgentToolFactory]',
        'on_start': 'RuntimeStartHook | None',
        'on_shutdown': 'RuntimeShutdownHook | None',
        'return': 'None',
    }
    assert parameters['flags'].default == ()
    assert parameters['agent_plugins'].default == ()
    assert parameters['services'].default is None
    assert parameters['tool_factories'].default == ()
    assert parameters['on_start'].default is None
    assert parameters['on_shutdown'].default is None


@pytest.mark.parametrize('key', ['', 'UPPER', 'two__parts', '_private', 'has-dash', 3])
def test_flag_key_must_be_a_nonempty_snake_case_string(key: object):
    with pytest.raises(ConfigError) as excinfo:
        FlagDeclaration(
            key=key,  # pyright: ignore[reportArgumentType]
            default=True,
            description='开关',
        )
    _three_parts(excinfo.value)
    assert 'snake_case' in excinfo.value.expected
    assert '自动归一化' in excinfo.value.next_action


@pytest.mark.parametrize('description', ['', '  ', None])
def test_flag_description_must_be_a_nonblank_string(description: object):
    with pytest.raises(ConfigError) as excinfo:
        FlagDeclaration(
            key='feature_enabled',
            default=True,
            description=description,  # pyright: ignore[reportArgumentType]
        )
    _three_parts(excinfo.value)
    assert 'description' in excinfo.value.expected
    assert 'CLI' in excinfo.value.next_action


@pytest.mark.parametrize('default', [None, 1.5, (), b'yes'])
def test_flag_default_is_one_of_the_three_supported_exact_types(default: object):
    with pytest.raises(ConfigError) as excinfo:
        FlagDeclaration(
            key='feature_value',
            default=default,  # pyright: ignore[reportArgumentType]
            description='值',
        )
    _three_parts(excinfo.value)
    assert 'bool | int | str' in excinfo.value.expected
    assert '结构化配置' in excinfo.value.next_action


def test_bool_does_not_pass_as_an_int_choice():
    with pytest.raises(ConfigError) as excinfo:
        FlagDeclaration(
            key='retry_count',
            default=2,
            description='次数',
            choices=(True, 2),
        )
    assert 'choices[0]' in excinfo.value.received
    assert 'int' in excinfo.value.expected
    assert 'bool' in excinfo.value.next_action


def test_choices_are_snapshotted_and_must_include_the_default():
    choices = ['reject', 'ask', 'allow']
    declaration = FlagDeclaration(
        key='untrusted_high_risk',
        default='reject',
        description='高风险处置',
        choices=choices,  # pyright: ignore[reportArgumentType]
    )
    choices.append('later')
    assert declaration.choices == ('reject', 'ask', 'allow')

    with pytest.raises(ConfigError) as excinfo:
        FlagDeclaration(
            key='mode',
            default='missing',
            description='模式',
            choices=('one', 'two'),
        )
    assert 'default' in excinfo.value.received
    assert 'choices' in excinfo.value.next_action


def test_duplicate_choices_are_rejected():
    with pytest.raises(ConfigError) as excinfo:
        FlagDeclaration(
            key='mode',
            default='one',
            description='模式',
            choices=('one', 'two', 'one'),
        )
    assert 'one' in excinfo.value.received
    assert '去重' in excinfo.value.next_action


@pytest.mark.parametrize(
    ('minimum', 'maximum', 'anchor'),
    [
        (True, None, 'minimum'),
        (None, False, 'maximum'),
        (3, 2, 'minimum'),
    ],
)
def test_integer_bounds_are_validated(minimum: object, maximum: object, anchor: str):
    with pytest.raises(ConfigError) as excinfo:
        FlagDeclaration(
            key='count',
            default=2,
            description='数量',
            minimum=minimum,  # pyright: ignore[reportArgumentType]
            maximum=maximum,  # pyright: ignore[reportArgumentType]
        )
    assert anchor in str(excinfo.value)
    _three_parts(excinfo.value)
    expected_action = '调换' if minimum == 3 else '整数边界'
    assert expected_action in excinfo.value.next_action


def test_bounds_are_only_valid_for_integer_flags_and_cover_the_default():
    with pytest.raises(ConfigError) as excinfo:
        FlagDeclaration(
            key='mode',
            default='one',
            description='模式',
            minimum=0,
        )
    assert 'int Flag' in excinfo.value.expected
    assert '删掉整数边界' in excinfo.value.next_action

    with pytest.raises(ConfigError) as excinfo:
        FlagDeclaration(
            key='count',
            default=4,
            description='数量',
            minimum=0,
            maximum=3,
        )
    assert 'default=4' in excinfo.value.received
    assert '调整 default' in excinfo.value.next_action


@pytest.mark.parametrize(
    ('choices', 'anchor'),
    [
        ((-1, 1), '-1'),
        ((1, 3), '3'),
    ],
)
def test_integer_choices_must_stay_inside_the_declared_bounds(
    choices: tuple[int, ...], anchor: str
):
    with pytest.raises(ConfigError) as excinfo:
        FlagDeclaration(
            key='count',
            default=1,
            description='数量',
            choices=choices,
            minimum=0,
            maximum=2,
        )
    assert anchor in excinfo.value.received
    assert '删除越界 choice' in excinfo.value.next_action


def test_integer_choices_accept_both_closed_interval_endpoints():
    declaration = FlagDeclaration(
        key='count',
        default=1,
        description='数量',
        choices=(0, 1, 2),
        minimum=0,
        maximum=2,
    )
    assert declaration.choices == (0, 1, 2)


def test_valid_flag_declarations_are_frozen():
    declaration = FlagDeclaration(
        key='count',
        default=1,
        description='数量',
        minimum=0,
        maximum=2,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        declaration.default = 2  # pyright: ignore[reportAttributeAccessIssue]


def test_runtime_module_snapshots_all_input_containers():
    flag = FlagDeclaration(key='enabled', default=True, description='启用')
    flags = [flag]
    plugins: list[Any] = [_noop]
    services: dict[str, Any] = {'clock': _noop}
    tools: list[Any] = []
    module = RuntimeModule(
        'sample',
        flags=flags,
        agent_plugins=plugins,
        services=services,
        tool_factories=tools,
        on_start=_noop,
        on_shutdown=_async_noop,
    )

    flags.clear()
    plugins.clear()
    services.clear()
    tools.append(lambda *_: {})

    assert module.flags == (flag,)
    assert module.agent_plugins == (_noop,)
    assert dict(module.services) == {'clock': _noop}
    assert module.tool_factories == ()
    with pytest.raises(TypeError):
        module.services['later'] = _noop  # pyright: ignore[reportIndexIssue]


@pytest.mark.parametrize('name', ['', '  ', None])
def test_runtime_module_name_must_be_a_nonblank_string(name: object):
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule(name)  # pyright: ignore[reportArgumentType]
    assert 'Module.name' in excinfo.value.expected
    assert '稳定名字' in excinfo.value.next_action
    _three_parts(excinfo.value)


def test_runtime_module_rejects_duplicate_flag_keys():
    flag = FlagDeclaration(key='enabled', default=True, description='启用')
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('duplicate', flags=(flag, flag))
    assert 'enabled' in excinfo.value.received
    assert '每个 key' in excinfo.value.next_action


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('flags', 'not-a-sequence'),
        ('agent_plugins', 'not-a-sequence'),
        ('services', [('name', _noop)]),
        ('tool_factories', b'not-a-sequence'),
    ],
)
def test_runtime_module_rejects_the_wrong_container_shape(field: str, value: object):
    kwargs: dict[str, object] = {field: value}
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('bad-container', **kwargs)  # pyright: ignore[reportArgumentType]
    assert field in str(excinfo.value)
    _three_parts(excinfo.value)
    expected_action = 'dict' if field == 'services' else 'list'
    assert expected_action in excinfo.value.next_action


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('agent_plugins', (None,)),
        ('tool_factories', (42,)),
        ('on_start', 'start'),
        ('on_shutdown', object()),
    ],
)
def test_runtime_module_rejects_noncallable_callbacks(field: str, value: object):
    kwargs: dict[str, object] = {field: value}
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('bad-callback', **kwargs)  # pyright: ignore[reportArgumentType]
    assert field in str(excinfo.value)
    _three_parts(excinfo.value)
    assert '传入能接收' in excinfo.value.next_action


@pytest.mark.parametrize(
    'name', ['', '_private', 'two words', 'has-dash', '9clock', 'class', 'True', 3]
)
def test_runtime_service_names_are_public_python_identifiers(name: object):
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule(
            'service',
            services=cast(Any, {name: _noop}),
        )
    assert 'service' in str(excinfo.value)
    assert '标识符' in excinfo.value.next_action


def test_runtime_service_factories_must_be_callable():
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule(
            'service',
            services=cast(Any, {'clock': object()}),
        )
    assert 'clock' in excinfo.value.received
    assert '2 个位置参数' in excinfo.value.next_action


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('agent_plugins', (_no_args,)),
        ('services', {'clock': _no_args}),
        ('tool_factories', (_no_args,)),
        ('on_start', _no_args),
        ('on_shutdown', _async_no_args),
    ],
)
def test_runtime_module_rejects_callbacks_with_the_wrong_arity(field: str, value: object):
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('bad-arity', **cast(Any, {field: value}))
    assert field in str(excinfo.value)
    assert '构造签名' in excinfo.value.next_action


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('agent_plugins', (_async_noop,)),
        ('services', {'clock': _async_noop}),
        ('tool_factories', (_async_noop,)),
        ('on_start', _async_noop),
    ],
)
def test_runtime_module_rejects_the_wrong_callback_execution_kind(field: str, value: object):
    with pytest.raises(ConfigError) as excinfo:
        RuntimeModule('bad-kind', **cast(Any, {field: value}))
    assert field in str(excinfo.value)
    assert '不会被 await' in excinfo.value.next_action


def test_on_shutdown_accepts_a_sync_wrapper_that_returns_an_awaitable():
    module = RuntimeModule('wrapped-stop', on_shutdown=_wrapped_shutdown)
    assert module.on_shutdown is _wrapped_shutdown


def test_runtime_module_is_frozen():
    module = RuntimeModule('sample')
    with pytest.raises(dataclasses.FrozenInstanceError):
        module.name = 'changed'  # pyright: ignore[reportAttributeAccessIssue]


def test_callback_annotations_accept_the_documented_shapes():
    plugin_factory: Any = _noop
    service_factory: Callable[[Any, Mapping[str, bool | int | str]], object] = _noop
    tool_factory: Any = _empty_tools
    start: Callable[[Any, Mapping[str, bool | int | str]], None] = _noop
    stop: Callable[[Any], Awaitable[None]] = _async_noop
    module = RuntimeModule(
        'typed',
        agent_plugins=(plugin_factory,),
        services={'service': service_factory},
        tool_factories=(tool_factory,),
        on_start=start,
        on_shutdown=stop,
    )
    assert module.name == 'typed'
