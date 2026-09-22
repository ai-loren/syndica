from __future__ import annotations

import asyncio
import dataclasses
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

import pytest

from syndica.kernel.errors import ConfigError
from syndica.kernel.events import Event
from syndica.kernel.hooks import HookResult, Junction
from syndica.kernel.modules import FlagDeclaration, RuntimeModule
from syndica.kernel.runtime import Runtime


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


def _count_flag(key: str = 'count') -> FlagDeclaration:
    return FlagDeclaration(
        key=key,
        default=1,
        description='数量',
        minimum=0,
        maximum=3,
    )


async def test_runtime_exposes_immutable_module_flag_and_declaration_snapshots():
    overrides = {'count': 2}
    modules = [RuntimeModule('counter', flags=(_count_flag(),))]
    runtime = Runtime(modules=modules, flags=overrides)
    modules.clear()
    overrides['count'] = 3

    assert runtime.modules == ('counter',)
    assert runtime.flags == {'count': 2}
    assert [flag.key for flag in runtime.flag_declarations] == ['count']
    with pytest.raises(TypeError):
        runtime.flags['count'] = 3  # pyright: ignore[reportIndexIssue]
    await runtime.shutdown()


@pytest.mark.parametrize(
    'modules',
    [
        'module',
        b'module',
        [object()],
        [RuntimeModule],
    ],
)
def test_runtime_rejects_invalid_module_collections_before_installing_anything(
    modules: object,
):
    with pytest.raises(ConfigError) as excinfo:
        Runtime(modules=modules)  # pyright: ignore[reportArgumentType]
    assert 'modules' in str(excinfo.value)
    assert '实例' in excinfo.value.next_action


def test_runtime_rejects_duplicate_module_names_before_service_factories_run():
    calls: list[str] = []

    def service(*_: object) -> object:
        calls.append('service')
        return object()

    module = RuntimeModule('same', services={'clock': service})
    with pytest.raises(ConfigError) as excinfo:
        Runtime(modules=[module, module])
    assert 'same' in excinfo.value.received
    assert '去掉重复' in excinfo.value.next_action
    assert calls == []


def test_runtime_rejects_duplicate_flag_keys_across_modules_before_start():
    calls: list[str] = []

    def start(*_: object) -> None:
        calls.append('start')

    first = RuntimeModule('first', flags=(_count_flag(),), on_start=start)
    second = RuntimeModule('second', flags=(_count_flag(),))
    with pytest.raises(ConfigError) as excinfo:
        Runtime(modules=[first, second])
    assert 'count' in excinfo.value.received
    assert 'first' in excinfo.value.received
    assert 'second' in excinfo.value.received
    assert calls == []


@pytest.mark.parametrize(
    ('flags', 'anchor'),
    [
        ('count=2', 'Mapping'),
        ({'missing': 1}, 'missing'),
        ({'count': True}, 'int'),
        ({'count': -1}, 'minimum'),
        ({'count': 4}, 'maximum'),
    ],
)
def test_runtime_rejects_invalid_flag_overrides_before_start(flags: object, anchor: str):
    calls: list[str] = []

    def start(*_: object) -> None:
        calls.append('start')

    module = RuntimeModule('counter', flags=(_count_flag(),), on_start=start)
    with pytest.raises(ConfigError) as excinfo:
        Runtime(modules=[module], flags=flags)  # pyright: ignore[reportArgumentType]
    assert anchor in str(excinfo.value)
    assert '修正' in excinfo.value.next_action
    assert calls == []


async def test_services_exist_before_start_and_shutdown_runs_in_reverse_module_order():
    order: list[object] = []

    def first_service(runtime: Runtime, flags: Mapping[str, object]) -> object:
        order.append(('service-a', dict(flags)))
        return object()

    def second_service(runtime: Runtime, flags: Mapping[str, object]) -> object:
        order.append(('service-b', dict(flags)))
        return object()

    def first_start(runtime: Runtime, flags: Mapping[str, object]) -> None:
        order.append(('start-a', hasattr(runtime, 'first_service'), dict(flags)))

    def second_start(runtime: Runtime, flags: Mapping[str, object]) -> None:
        order.append(
            (
                'start-b',
                hasattr(runtime, 'first_service'),
                hasattr(runtime, 'second_service'),
                dict(flags),
            )
        )

    async def first_stop(runtime: Runtime) -> None:
        order.append(('stop-a', hasattr(runtime, 'first_service')))

    async def second_stop(runtime: Runtime) -> None:
        order.append(('stop-b', hasattr(runtime, 'second_service')))

    runtime = Runtime(
        modules=[
            RuntimeModule(
                'a',
                flags=(_count_flag(),),
                services={'first_service': first_service},
                on_start=first_start,
                on_shutdown=first_stop,
            ),
            RuntimeModule(
                'b',
                services={'second_service': second_service},
                on_start=second_start,
                on_shutdown=second_stop,
            ),
        ],
        flags={'count': 2},
    )

    assert order == [
        ('service-a', {'count': 2}),
        ('service-b', {'count': 2}),
        ('start-a', True, {'count': 2}),
        ('start-b', True, True, {'count': 2}),
    ]
    assert cast(Any, runtime).first_service is not cast(Any, runtime).second_service
    await runtime.shutdown()
    assert order[-2:] == [('stop-b', True), ('stop-a', True)]


@pytest.mark.parametrize('service_name', ['events', 'modules', 'flags', 'shutdown'])
def test_service_names_cannot_shadow_runtime_public_state(service_name: str):
    module = RuntimeModule(
        'collision',
        services={service_name: lambda *_: object()},
    )
    with pytest.raises(ConfigError) as excinfo:
        Runtime(modules=[module])
    assert service_name in excinfo.value.received
    assert '换一个 service 名' in excinfo.value.next_action


async def test_start_failure_rolls_back_runtime_hooks_and_services():
    captured: list[Runtime] = []
    observed: list[str] = []

    async def observe(**_: object) -> HookResult:
        observed.append('called')
        return HookResult()

    def first_start(runtime: Runtime, _: Mapping[str, object]) -> None:
        captured.append(runtime)
        runtime.hooks.register(Junction.AGENT_SPAWN, observe)

    def fail(runtime: Runtime, _: Mapping[str, object]) -> None:
        captured.append(runtime)
        raise RuntimeError('start failed')

    with pytest.raises(RuntimeError, match='start failed'):
        Runtime(
            modules=[
                RuntimeModule(
                    'first',
                    services={'first_service': lambda *_: object()},
                    on_start=first_start,
                ),
                RuntimeModule(
                    'second',
                    services={'second_service': lambda *_: object()},
                    on_start=fail,
                ),
            ]
        )

    runtime = captured[0]
    assert captured == [runtime, runtime]
    assert not hasattr(runtime, 'first_service')
    assert not hasattr(runtime, 'second_service')
    await runtime.hooks.trigger(Junction.AGENT_SPAWN, agent_id='a', parent_id=None)
    assert observed == []


@pytest.mark.parametrize('returned', [1, object()])
def test_on_start_must_return_none_and_rolls_back_services(returned: object):
    captured: list[Runtime] = []

    def bad_start(runtime: Runtime, _: Mapping[str, object]) -> Any:
        captured.append(runtime)
        return returned

    with pytest.raises(ConfigError) as excinfo:
        Runtime(
            modules=[
                RuntimeModule(
                    'bad-start',
                    services={'temporary': lambda *_: object()},
                    on_start=cast(Any, bad_start),
                )
            ]
        )
    assert 'bad-start' in excinfo.value.received
    assert 'return None' in excinfo.value.next_action
    assert not hasattr(captured[0], 'temporary')


def test_service_factory_must_return_a_service_not_an_awaitable():
    created: list[Any] = []

    async def build() -> object:
        return object()

    def factory(*_: object) -> object:
        coroutine = build()
        created.append(coroutine)
        return coroutine

    with pytest.raises(ConfigError) as excinfo:
        Runtime(
            modules=[
                RuntimeModule(
                    'async-service',
                    services={'client': factory},
                )
            ]
        )
    assert 'async-service' in excinfo.value.expected
    assert 'awaitable' in excinfo.value.received
    assert '异步初始化' in excinfo.value.next_action
    assert created[0].cr_frame is None


async def test_concurrent_shutdown_calls_share_one_reverse_cleanup():
    entered = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def first(_: Runtime) -> None:
        order.append('first')

    async def second(_: Runtime) -> None:
        order.append('second-enter')
        entered.set()
        await release.wait()
        order.append('second-exit')

    runtime = Runtime(
        modules=[
            RuntimeModule('first', on_shutdown=first),
            RuntimeModule('second', on_shutdown=second),
        ]
    )
    calls = [asyncio.create_task(runtime.shutdown()) for _ in range(3)]
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.sleep(0)
    assert [call.done() for call in calls] == [False, False, False]
    release.set()
    await asyncio.gather(*calls)
    assert order == ['second-enter', 'second-exit', 'first']


async def test_cancelling_one_shutdown_waiter_does_not_cancel_shared_cleanup():
    entered = asyncio.Event()
    release = asyncio.Event()
    completed: list[str] = []

    async def stop(_: Runtime) -> None:
        entered.set()
        await release.wait()
        completed.append('stopped')

    runtime = Runtime(modules=[RuntimeModule('slow', on_shutdown=stop)])
    first = asyncio.create_task(runtime.shutdown())
    await asyncio.wait_for(entered.wait(), 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert completed == []
    release.set()
    await runtime.shutdown()
    assert completed == ['stopped']


async def test_module_shutdown_can_reenter_shutdown_without_waiting_on_itself():
    order: list[str] = []

    async def stop(runtime: Runtime) -> None:
        order.append('enter')
        await runtime.shutdown()
        order.append('exit')

    runtime = Runtime(modules=[RuntimeModule('reentrant', on_shutdown=stop)])
    await asyncio.wait_for(runtime.shutdown(), 1)
    assert order == ['enter', 'exit']


async def test_runtime_shutdown_hook_can_reenter_shutdown_without_waiting_on_itself():
    order: list[str] = []
    runtime = Runtime()

    async def stop(**_: object) -> HookResult:
        order.append('enter')
        await runtime.shutdown()
        order.append('exit')
        return HookResult()

    runtime.hooks.register(Junction.RUNTIME_SHUTDOWN, stop)
    await asyncio.wait_for(runtime.shutdown(), 1)
    assert order == ['enter', 'exit']


async def test_nested_runtime_shutdown_callbacks_do_not_form_an_a_to_b_to_a_cycle():
    order: list[str] = []
    first: Runtime
    second: Runtime

    async def stop_first(_: Runtime) -> None:
        order.append('first-enter')
        await second.shutdown()
        order.append('first-exit')

    async def stop_second(_: Runtime) -> None:
        order.append('second-enter')
        await first.shutdown()
        order.append('second-exit')

    first = Runtime(modules=[RuntimeModule('first', on_shutdown=stop_first)])
    second = Runtime(modules=[RuntimeModule('second', on_shutdown=stop_second)])

    await asyncio.wait_for(first.shutdown(), 1)
    assert order == ['first-enter', 'second-enter', 'second-exit', 'first-exit']


async def test_independently_started_runtime_shutdowns_do_not_deadlock_in_a_cycle():
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    order: list[str] = []
    first: Runtime
    second: Runtime

    async def stop_first(_: Runtime) -> None:
        first_entered.set()
        await second_entered.wait()
        order.append('first-waits-second')
        await second.shutdown()

    async def stop_second(_: Runtime) -> None:
        second_entered.set()
        await first_entered.wait()
        order.append('second-waits-first')
        await first.shutdown()

    first = Runtime(modules=[RuntimeModule('first', on_shutdown=stop_first)])
    second = Runtime(modules=[RuntimeModule('second', on_shutdown=stop_second)])

    await asyncio.wait_for(
        asyncio.gather(first.shutdown(), second.shutdown()),
        1,
    )
    assert sorted(order) == ['first-waits-second', 'second-waits-first']


async def test_shutdown_collects_callback_failures_and_still_closes_the_event_bus():
    order: list[str] = []
    subscription = None

    async def ordinary(_: Runtime) -> None:
        order.append('ordinary')
        raise ValueError('ordinary failure')

    async def cancelled(_: Runtime) -> None:
        order.append('cancelled')
        raise asyncio.CancelledError('callback cancelled itself')

    def not_awaitable(_: Runtime) -> None:
        order.append('not-awaitable')

    runtime = Runtime(
        modules=[
            RuntimeModule('ordinary', on_shutdown=ordinary),
            RuntimeModule('cancelled', on_shutdown=cancelled),
            RuntimeModule('not-awaitable', on_shutdown=cast(Any, not_awaitable)),
        ]
    )
    subscription = runtime.events.subscribe()
    with pytest.raises(BaseExceptionGroup) as excinfo:
        await runtime.shutdown()
    assert order == ['not-awaitable', 'cancelled', 'ordinary']
    assert [type(error) for error in excinfo.value.exceptions] == [
        ConfigError,
        asyncio.CancelledError,
        ValueError,
    ]
    await runtime.events.emit(Event(kind='after', agent_id='none'))
    with pytest.raises(StopAsyncIteration):
        await anext(subscription)


async def test_cancelled_only_waiter_does_not_leave_shutdown_failure_unretrieved(
    caplog: pytest.LogCaptureFixture,
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail(_: Runtime) -> None:
        entered.set()
        await release.wait()
        raise ValueError('late failure')

    runtime = Runtime(modules=[RuntimeModule('late', on_shutdown=fail)])
    waiter = asyncio.create_task(runtime.shutdown())
    await asyncio.wait_for(entered.wait(), 1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    with pytest.raises(ExceptionGroup) as excinfo:
        await runtime.shutdown()
    assert isinstance(excinfo.value.exceptions[0], ValueError)
    assert any('共享关机任务' in record.getMessage() for record in caplog.records)


async def test_sync_shutdown_wrapper_returning_an_awaitable_is_awaited():
    stopped: list[str] = []

    async def finish() -> None:
        stopped.append('done')

    def wrapped(_: Runtime) -> Awaitable[None]:
        return finish()

    runtime = Runtime(modules=[RuntimeModule('wrapped', on_shutdown=wrapped)])
    await runtime.shutdown()
    assert stopped == ['done']
