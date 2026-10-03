from __future__ import annotations

import asyncio
import dataclasses
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.agent_types import AgentInfo
from syndica.kernel.context import ContextBlock, ToolCall, TrustLevel
from syndica.kernel.danger import AllowAll, Disposition
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.events import Event, EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.modules import FlagDeclaration, RuntimeModule, SafetyModule
from syndica.kernel.runtime import Runtime
from syndica.kernel.stores import InMemoryAgentRegistry, InMemoryMessageBroker
from syndica.kernel.tool import Tool, ToolResult


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


class _HookPlugin:
    def __init__(self, name: str, callback: Callable[..., Awaitable[HookResult]]) -> None:
        self._name = name
        self._callback = callback

    @property
    def name(self) -> str:
        return self._name

    def register(self, hooks: HooksCenter) -> None:
        hooks.register(Junction.PRE_LLM_REQUEST, self._callback)


class _InjectedTool(Tool):
    __tool_name__ = 'Injected'
    __tool_schema__ = {
        'name': 'Injected',
        'description': '返回绑定的 Agent id',
        'parameters': {'type': 'object', 'properties': {}},
    }
    __tool_command_args__ = ()

    def __init__(self, owner: str) -> None:
        self.owner = owner

    async def execute(self) -> ToolResult:
        return ToolResult(output=self.owner)


class _HostTool(Tool):
    __tool_name__ = 'HostAdded'
    __tool_schema__ = {
        'name': 'HostAdded',
        'description': '宿主追加工具',
        'parameters': {'type': 'object', 'properties': {}},
    }
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        return ToolResult(output='host')


class _DangerousTool(Tool):
    __tool_name__ = 'Dangerous'
    __tool_schema__ = {
        'name': 'Dangerous',
        'description': '执行命令',
        'parameters': {
            'type': 'object',
            'properties': {'command': {'type': 'string'}},
            'required': ['command'],
        },
    }
    __tool_command_args__ = ('command',)

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.executed: list[str] = []

    async def execute(self, command: str) -> ToolResult:
        self.executed.append(command)
        return ToolResult(output=f'ran {command}')


def _agent_factory(captured: list[BaseAgent]) -> Callable[[HooksCenter, EventBus], BaseAgent]:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM([ScriptedTurn(content='done')]),
            ),
            hooks=hooks,
            events=events,
        )
        captured.append(agent)
        return agent

    return build


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
        'version',
        'description',
        'capabilities',
        'requires',
        'junctions',
        'replay_safe',
        'security_notes',
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
        ('version', 'KEYWORD_ONLY', True),
        ('description', 'KEYWORD_ONLY', True),
        ('capabilities', 'KEYWORD_ONLY', True),
        ('requires', 'KEYWORD_ONLY', True),
        ('junctions', 'KEYWORD_ONLY', True),
        ('replay_safe', 'KEYWORD_ONLY', True),
        ('security_notes', 'KEYWORD_ONLY', True),
    ]
    assert RuntimeModule.__init__.__annotations__ == {
        'name': 'str',
        'flags': 'Sequence[FlagDeclaration]',
        'agent_plugins': 'Sequence[AgentPluginFactory]',
        'services': 'Mapping[str, RuntimeServiceFactory] | None',
        'tool_factories': 'Sequence[AgentToolFactory]',
        'on_start': 'RuntimeStartHook | None',
        'on_shutdown': 'RuntimeShutdownHook | None',
        'version': 'str',
        'description': 'str',
        'capabilities': 'Sequence[str] | None',
        'requires': 'Sequence[str]',
        'junctions': 'Sequence[ExtensionJunction]',
        'replay_safe': 'bool',
        'security_notes': 'str',
        'return': 'None',
    }
    assert parameters['flags'].default == ()
    assert parameters['agent_plugins'].default == ()
    assert parameters['services'].default is None
    assert parameters['tool_factories'].default == ()
    assert parameters['on_start'].default is None
    assert parameters['on_shutdown'].default is None
    assert parameters['version'].default == '0.0.0'
    assert parameters['description'].default == ''
    assert parameters['capabilities'].default is None
    assert parameters['requires'].default == ()
    assert parameters['junctions'].default == ()
    assert parameters['replay_safe'].default is False
    assert parameters['security_notes'].default == ''


def test_safety_module_declares_the_two_fixed_flags_exactly():
    module = SafetyModule()

    assert isinstance(module, RuntimeModule)
    assert module.name == 'safety'
    assert [
        (
            flag.key,
            flag.default,
            flag.choices,
            flag.minimum,
            flag.maximum,
            bool(flag.description.strip()),
        )
        for flag in module.flags
    ] == [
        ('max_wait_seconds', 0, (), 0, None, True),
        ('untrusted_high_risk', 'reject', ('reject', 'ask', 'allow'), None, None, True),
    ]
    assert module.agent_plugins == ()
    assert dict(module.services) == {}
    assert module.tool_factories == ()
    assert module.on_start is None
    assert module.on_shutdown is None


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
    await runtime.hooks._dispatch(Junction.AGENT_SPAWN, agent_id='a', parent_id=None)
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


async def test_agent_flags_are_snapshots_and_inherit_from_parent_then_explicit_override():
    captured: list[BaseAgent] = []
    supplied = {'count': 2}
    runtime = Runtime(
        modules=[RuntimeModule('counter', flags=(_count_flag(),))],
        flags={'count': 1},
    )
    try:
        root = await runtime.spawn(_agent_factory(captured), flags=supplied)
        supplied['count'] = 3
        child = await runtime.spawn(_agent_factory(captured), parent_id=root)
        overridden = await runtime.spawn(
            _agent_factory(captured),
            parent_id=root,
            flags={'count': 0},
        )

        root_info = await runtime.find(root)
        child_info = await runtime.find(child)
        overridden_info = await runtime.find(overridden)
        assert root_info is not None and root_info.flags == {'count': 2}
        assert child_info is not None and child_info.flags == {'count': 2}
        assert overridden_info is not None and overridden_info.flags == {'count': 0}
        root_info.flags['count'] = 3
        again = await runtime.find(root)
        assert again is not None and again.flags == {'count': 2}
    finally:
        await runtime.shutdown()


async def test_fork_flags_replace_different_parent_flags_before_explicit_overrides():
    captured: list[BaseAgent] = []
    runtime = Runtime(modules=[RuntimeModule('counter', flags=(_count_flag(),))])
    try:
        parent = await runtime.spawn(_agent_factory(captured), flags={'count': 2})
        source = await runtime.spawn(_agent_factory(captured), flags={'count': 3})
        fork = await runtime.spawn(
            _agent_factory(captured),
            parent_id=parent,
            fork_from=source,
            flags={'count': 0},
        )
        info = await runtime.find(fork)
        assert info is not None and info.flags == {'count': 0}

        inherited_fork = await runtime.spawn(
            _agent_factory(captured),
            parent_id=parent,
            fork_from=source,
        )
        inherited = await runtime.find(inherited_fork)
        assert inherited is not None and inherited.flags == {'count': 3}
    finally:
        await runtime.shutdown()


async def test_each_agent_gets_fresh_module_plugin_and_tool_instances():
    captured: list[BaseAgent] = []
    plugins: list[_HookPlugin] = []
    tools: list[_InjectedTool] = []

    async def observe(**_: object) -> HookResult:
        return HookResult()

    def plugin_factory(
        runtime: Runtime, agent: BaseAgent, flags: Mapping[str, object]
    ) -> _HookPlugin:
        del runtime, flags
        plugin = _HookPlugin(f'plugin-{agent.agent_id}', observe)
        plugins.append(plugin)
        return plugin

    def tool_factory(
        runtime: Runtime, agent: BaseAgent, flags: Mapping[str, object]
    ) -> Mapping[str, Tool]:
        del runtime, flags
        tool = _InjectedTool(agent.agent_id)
        tools.append(tool)
        return {'Injected': tool}

    runtime = Runtime(
        modules=[
            RuntimeModule(
                'per-agent',
                agent_plugins=(plugin_factory,),
                tool_factories=(tool_factory,),
            )
        ]
    )
    try:
        first = await runtime.spawn(_agent_factory(captured))
        second = await runtime.spawn(_agent_factory(captured))
        assert len({id(plugin) for plugin in plugins}) == 2
        assert len({id(tool) for tool in tools}) == 2
        assert cast(_InjectedTool, captured[0].options.tools['Injected']).owner == first
        assert cast(_InjectedTool, captured[1].options.tools['Injected']).owner == second
        assert captured[0].options.tools is not captured[1].options.tools
    finally:
        await runtime.shutdown()


async def test_late_local_hooks_still_run_before_modules_and_runtime_plugins():
    order: list[str] = []
    captured: list[BaseAgent] = []

    def callback(label: str) -> Callable[..., Awaitable[HookResult]]:
        async def record(**kwargs: object) -> HookResult:
            previous = cast(list[str], kwargs.get('probe', []))
            order.append(label)
            return HookResult(modified={'probe': [*previous, label]})

        return record

    class _RuntimePlugin:
        @property
        def name(self) -> str:
            return 'runtime'

        def register(self, hooks: HooksCenter) -> None:
            hooks.register(Junction.PRE_LLM_REQUEST, callback('runtime'))

    def plugin(label: str) -> Callable[..., _HookPlugin]:
        return lambda *_: _HookPlugin(label, callback(label))

    runtime = Runtime(
        plugins=[_RuntimePlugin()],
        modules=[
            RuntimeModule('a', agent_plugins=(plugin('module-a'),)),
            RuntimeModule('b', agent_plugins=(plugin('module-b'),)),
        ],
    )
    try:
        agent_id = await runtime.spawn(_agent_factory(captured))
        captured[0].register_hook(Junction.PRE_LLM_REQUEST, callback('late-local'))
        await runtime.send(agent_id, 'go')
        await runtime.wait(agent_id)
        assert order == ['late-local', 'module-a', 'module-b', 'runtime']
    finally:
        await runtime.shutdown()


async def test_local_skip_stops_module_and_runtime_plugin_layers():
    order: list[str] = []
    captured: list[BaseAgent] = []

    async def never(**_: object) -> HookResult:
        order.append('later')
        return HookResult()

    async def stop(**_: object) -> HookResult:
        order.append('local')
        return HookResult(
            skip=True,
            result=ContextBlock(role='assistant', content='from local'),
        )

    module = RuntimeModule(
        'module',
        agent_plugins=(lambda *_: _HookPlugin('module', never),),
    )
    runtime = Runtime(plugins=[_HookPlugin('runtime', never)], modules=[module])
    try:
        agent_id = await runtime.spawn(_agent_factory(captured))
        captured[0].register_hook(Junction.PRE_LLM_REQUEST, stop)
        await runtime.send(agent_id, 'go')
        ctx = await runtime.wait(agent_id)
        assert order == ['local']
        assert ctx.current_traj[-1].content == 'from local'
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize('returned', [None, [], 'tools'])
async def test_bad_tool_factory_results_leave_no_agent_state(returned: object):
    captured: list[BaseAgent] = []

    def bad_tools(*_: object) -> Any:
        return returned

    runtime = Runtime(
        modules=[
            RuntimeModule(
                'bad-tools',
                tool_factories=(cast(Any, bad_tools),),
            )
        ]
    )
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_agent_factory(captured))
        assert 'bad-tools' in excinfo.value.expected
        assert 'dict' in excinfo.value.next_action
        assert await runtime.registry.list() == []
        assert runtime._slots == {}  # pyright: ignore[reportPrivateUsage]
        assert captured[0].options.tools == {}
        assert captured[0]._gate_attached is False  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()


async def test_failed_agent_module_assembly_restores_the_same_instance_for_retry():
    captured: list[BaseAgent] = []
    host_tools: dict[str, Tool] = {}
    fail = True

    def reused(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        if not captured:
            captured.append(
                BaseAgent(
                    AgentOptions(
                        name='Worker',
                        llm=ScriptedLLM([ScriptedTurn(content='done')]),
                        tools=host_tools,
                    ),
                    hooks=hooks,
                    events=events,
                )
            )
        return captured[0]

    def tools(
        runtime: Runtime, agent: BaseAgent, flags: Mapping[str, object]
    ) -> Mapping[str, Tool]:
        del runtime, flags
        if fail:
            raise RuntimeError('factory failed')
        return {'Injected': _InjectedTool(agent.agent_id)}

    runtime = Runtime(
        modules=[RuntimeModule('retryable', tool_factories=(tools,))],
        orchestration=True,
    )
    try:
        with pytest.raises(RuntimeError, match='factory failed'):
            await runtime.spawn(reused)
        agent = captured[0]
        assert agent.options.tools == {}
        assert agent.options.tools is host_tools
        assert agent._gate_attached is False  # pyright: ignore[reportPrivateUsage]
        assert await runtime.registry.list() == []
        assert runtime._slots == {}  # pyright: ignore[reportPrivateUsage]

        host_tools['HostAdded'] = _HostTool()
        fail = False
        agent_id = await runtime.spawn(reused)
        assert agent_id == agent.agent_id
        assert tuple(agent.options.tools)[0] == 'HostAdded'
        assert tuple(agent.options.tools)[-1] == 'Injected'
    finally:
        await runtime.shutdown()


async def test_module_tool_name_cannot_replace_an_agent_tool():
    captured: list[BaseAgent] = []

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM([ScriptedTurn(content='done')]),
                tools={'Injected': _InjectedTool('agent')},
            ),
            hooks=hooks,
            events=events,
        )
        captured.append(agent)
        return agent

    module = RuntimeModule(
        'collision',
        tool_factories=(lambda *_: {'Injected': _InjectedTool('module')},),
    )
    runtime = Runtime(modules=[module])
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(build)
        assert 'Injected' in excinfo.value.received
        assert '换一个名字' in excinfo.value.next_action
        assert cast(_InjectedTool, captured[0].options.tools['Injected']).owner == 'agent'
        assert captured[0]._gate_attached is False  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()


async def test_spawn_flag_errors_happen_before_module_factories_and_registration():
    calls: list[str] = []
    captured: list[BaseAgent] = []

    def plugin(*_: object) -> _HookPlugin:
        calls.append('plugin')
        raise AssertionError('must not run')

    runtime = Runtime(
        modules=[
            RuntimeModule(
                'counter',
                flags=(_count_flag(),),
                agent_plugins=(plugin,),
            )
        ]
    )
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_agent_factory(captured), flags={'count': -1})
        assert 'minimum' in excinfo.value.expected
        assert 'count' in excinfo.value.next_action
        assert calls == []
        assert await runtime.registry.list() == []
        assert runtime._slots == {}  # pyright: ignore[reportPrivateUsage]
        assert captured[0]._gate_attached is False  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()


async def test_invalid_safety_disposition_is_rejected_before_spawn_side_effects():
    captured: list[BaseAgent] = []
    runtime = Runtime(modules=[SafetyModule()])
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(
                _agent_factory(captured),
                flags={'untrusted_high_risk': 'permit'},
            )
        assert 'untrusted_high_risk' in excinfo.value.expected
        assert 'permit' in excinfo.value.received
        assert '声明列出的值' in excinfo.value.next_action
        assert await runtime.registry.list() == []
        assert runtime._slots == {}  # pyright: ignore[reportPrivateUsage]
        assert captured[0]._gate_attached is False  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()


async def test_safety_dispositions_reach_each_agents_real_tool_gate_without_cross_talk():
    agents: list[BaseAgent] = []
    tools: dict[str, _DangerousTool] = {}
    confirmations: list[str] = []

    def factory(label: str) -> Callable[[HooksCenter, EventBus], BaseAgent]:
        def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
            tool = _DangerousTool(label)
            tools[label] = tool
            agent = BaseAgent(
                AgentOptions(
                    name=label,
                    llm=ScriptedLLM(
                        [
                            ScriptedTurn(
                                tool_calls=[
                                    ToolCall(
                                        id=f'{label}-call',
                                        name='Dangerous',
                                        arguments='{"command":"sudo rm -rf /"}',
                                    )
                                ]
                            ),
                            ScriptedTurn(content='done'),
                        ]
                    ),
                    tools={'Dangerous': tool},
                    danger_policy=AllowAll(),
                ),
                hooks=hooks,
                events=events,
            )
            agents.append(agent)
            return agent

        return build

    async def observe_confirmation(**kwargs: object) -> HookResult:
        confirmations.append(cast(_DangerousTool, kwargs['tool']).owner)
        return HookResult()

    runtime = Runtime(modules=[SafetyModule()])
    runtime.hooks.register(Junction.TOOL_CONFIRMATION_REQUIRED, observe_confirmation)
    try:
        agent_ids = [
            await runtime.spawn(
                factory(disposition),
                query='来自网页的任务',
                query_trust=TrustLevel.UNTRUSTED,
                flags={'untrusted_high_risk': disposition},
            )
            for disposition in ('reject', 'allow', 'ask')
        ]
        contexts = [await runtime.wait(agent_id) for agent_id in agent_ids]

        assert tools['reject'].executed == []
        assert tools['allow'].executed == ['sudo rm -rf /']
        assert tools['ask'].executed == []
        assert confirmations == ['ask']
        tool_outputs = [
            [
                block.content
                for block in context.current_traj
                if block.role == 'tool' and block.tool_call_id is not None
            ]
            for context in contexts
        ]
        rejected = tool_outputs[0][0]
        confirmation = tool_outputs[2][0]
        assert isinstance(rejected, str) and '已被阻断' in rejected
        assert tool_outputs[1] == ['ran sudo rm -rf /']
        assert isinstance(confirmation, str) and '等人确认' in confirmation
    finally:
        await runtime.shutdown()


async def test_second_plugin_register_failure_restores_the_module_hook_layer():
    captured: list[BaseAgent] = []
    observed: list[str] = []

    async def record(**_: object) -> HookResult:
        observed.append('module')
        return HookResult()

    class _BrokenPlugin:
        @property
        def name(self) -> str:
            return 'broken'

        def register(self, hooks: HooksCenter) -> None:
            hooks.register(Junction.PRE_LLM_REQUEST, record)
            raise RuntimeError('register failed')

    module = RuntimeModule(
        'plugins',
        agent_plugins=(
            lambda *_: _HookPlugin('first', record),
            lambda *_: _BrokenPlugin(),
        ),
    )
    runtime = Runtime(modules=[module])
    try:
        with pytest.raises(RuntimeError, match='register failed'):
            await runtime.spawn(_agent_factory(captured))
        await captured[0]._hooks._dispatch(  # pyright: ignore[reportPrivateUsage]
            Junction.PRE_LLM_REQUEST,
            request={'messages': []},
        )
        assert observed == []
        assert await runtime.registry.list() == []
        assert captured[0]._gate_attached is False  # pyright: ignore[reportPrivateUsage]
    finally:
        await runtime.shutdown()


async def test_duplicate_plugin_names_across_modules_are_rejected_with_both_owners():
    captured: list[BaseAgent] = []

    async def observe(**_: object) -> HookResult:
        return HookResult()

    runtime = Runtime(
        modules=[
            RuntimeModule(
                'first-module',
                agent_plugins=(lambda *_: _HookPlugin('same', observe),),
            ),
            RuntimeModule(
                'second-module',
                agent_plugins=(lambda *_: _HookPlugin('same', observe),),
            ),
        ]
    )
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_agent_factory(captured))
        assert 'same' in excinfo.value.received
        assert 'first-module' in excinfo.value.received
        assert 'second-module' in excinfo.value.received
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


async def test_second_tool_factory_failure_does_not_install_the_first_batch():
    captured: list[BaseAgent] = []

    def fail(*_: object) -> Mapping[str, Tool]:
        raise RuntimeError('second factory failed')

    runtime = Runtime(
        modules=[
            RuntimeModule(
                'tools',
                tool_factories=(
                    lambda _, agent, __: {'Injected': _InjectedTool(agent.agent_id)},
                    fail,
                ),
            )
        ]
    )
    try:
        with pytest.raises(RuntimeError, match='second factory failed'):
            await runtime.spawn(_agent_factory(captured))
        assert captured[0].options.tools == {}
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


async def test_invalid_tool_identity_is_rejected_before_any_batch_is_installed():
    captured: list[BaseAgent] = []
    runtime = Runtime(
        modules=[
            RuntimeModule(
                'identity',
                tool_factories=(lambda *_: {'Wrong': _InjectedTool('module')},),
            )
        ]
    )
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(_agent_factory(captured))
        assert 'Wrong' in excinfo.value.received
        assert 'Injected' in excinfo.value.next_action
        assert captured[0].options.tools == {}
    finally:
        await runtime.shutdown()


async def test_install_own_tools_failure_restores_tools_and_local_hooks():
    observed: list[str] = []
    captured: list[BaseAgent] = []

    async def added(**_: object) -> HookResult:
        observed.append('own')
        return HookResult()

    class _FailingOwnAgent(BaseAgent):
        def install_own_tools(self, runtime: object) -> None:
            del runtime
            self.install_runtime_tools({'Injected': _InjectedTool('own')})
            self.register_hook(Junction.PRE_LLM_REQUEST, added)
            raise RuntimeError('own tools failed')

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        agent = _FailingOwnAgent(
            AgentOptions(name='Worker', llm=ScriptedLLM([])),
            hooks=hooks,
            events=events,
        )
        captured.append(agent)
        return agent

    runtime = Runtime(
        modules=[
            RuntimeModule(
                'module-tools',
                tool_factories=(lambda *_: {'Injected': _InjectedTool('module')},),
            )
        ]
    )
    try:
        with pytest.raises(RuntimeError, match='own tools failed'):
            await runtime.spawn(build)
        agent = captured[0]
        assert agent.options.tools == {}
        await agent._hooks._dispatch(  # pyright: ignore[reportPrivateUsage]
            Junction.PRE_LLM_REQUEST,
            request={'messages': []},
        )
        assert observed == []
        assert agent._gate_attached is False  # pyright: ignore[reportPrivateUsage]
        assert await runtime.registry.list() == []
    finally:
        await runtime.shutdown()


async def test_registry_register_failure_rolls_back_agent_assembly_and_gate():
    captured: list[BaseAgent] = []

    def reused(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        if not captured:
            _agent_factory(captured)(hooks, events)
        return captured[0]

    runtime = Runtime(
        modules=[
            SafetyModule(),
            RuntimeModule(
                'tools',
                tool_factories=(lambda _, agent, __: {'Injected': _InjectedTool(agent.agent_id)},),
            ),
        ]
    )
    try:
        with pytest.raises(ContractViolation):
            await runtime.spawn(
                reused,
                parent_id='missing',
                flags={'untrusted_high_risk': 'allow'},
            )
        agent = captured[0]
        assert agent.options.tools == {}
        assert (
            agent._executor._untrusted_high_risk  # pyright: ignore[reportPrivateUsage]
            is Disposition.BLOCK
        )
        assert agent._gate_attached is False  # pyright: ignore[reportPrivateUsage]
        assert await runtime.registry.list() == []
        assert runtime._slots == {}  # pyright: ignore[reportPrivateUsage]

        agent_id = await runtime.spawn(reused, flags={'untrusted_high_risk': 'ask'})
        assert agent_id == agent.agent_id
        assert tuple(agent.options.tools) == ('Injected',)
        assert (
            agent._executor._untrusted_high_risk  # pyright: ignore[reportPrivateUsage]
            is Disposition.CONFIRM
        )
    finally:
        await runtime.shutdown()


class _FailOnceEnsureBroker(InMemoryMessageBroker):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    async def ensure_queue(self, agent_id: str) -> None:
        await super().ensure_queue(agent_id)
        if self.fail:
            raise RuntimeError('ensure failed')

    def has_queue(self, agent_id: str) -> bool:
        return agent_id in self._mailboxes  # pyright: ignore[reportPrivateUsage]


async def test_broker_ensure_failure_removes_registry_queue_and_agent_assembly():
    captured: list[BaseAgent] = []
    broker = _FailOnceEnsureBroker()

    def reused(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        if not captured:
            _agent_factory(captured)(hooks, events)
        return captured[0]

    runtime = Runtime(
        broker=broker,
        modules=[
            RuntimeModule(
                'tools',
                tool_factories=(lambda _, agent, __: {'Injected': _InjectedTool(agent.agent_id)},),
            )
        ],
    )
    try:
        with pytest.raises(RuntimeError, match='ensure failed'):
            await runtime.spawn(reused)
        agent = captured[0]
        assert await runtime.registry.get(agent.agent_id) is None
        assert broker.has_queue(agent.agent_id) is False
        assert agent.options.tools == {}
        assert agent._gate_attached is False  # pyright: ignore[reportPrivateUsage]
        assert runtime._slots == {}  # pyright: ignore[reportPrivateUsage]

        broker.fail = False
        agent_id = await runtime.spawn(reused)
        assert agent_id == agent.agent_id
        assert broker.has_queue(agent_id) is True
    finally:
        await runtime.shutdown()


async def test_duplicate_id_register_failure_does_not_delete_the_existing_agent():
    registry = InMemoryAgentRegistry()
    broker = _FailOnceEnsureBroker()
    broker.fail = False
    runtime = Runtime(
        registry=registry,
        broker=broker,
        modules=[
            RuntimeModule(
                'tools',
                tool_factories=(lambda _, agent, __: {'Injected': _InjectedTool(agent.agent_id)},),
            )
        ],
    )
    module_hooks = HooksCenter(outer=runtime.hooks)
    agent = BaseAgent(
        AgentOptions(name='Existing', llm=ScriptedLLM([])),
        hooks=module_hooks,
        events=runtime.events,
    )
    await registry.register(
        AgentInfo(
            agent_id=agent.agent_id,
            name='Existing',
            agent_type='remote',
        )
    )
    await broker.ensure_queue(agent.agent_id)
    try:
        with pytest.raises(ContractViolation):
            await runtime.spawn(lambda *_: agent)
        existing = await registry.get(agent.agent_id)
        assert existing is not None and existing.agent_type == 'remote'
        assert broker.has_queue(agent.agent_id) is True
        assert agent.options.tools == {}
        assert agent._gate_attached is False  # pyright: ignore[reportPrivateUsage]
    finally:
        await broker.remove_queue(agent.agent_id)
        await registry.unregister(agent.agent_id)
        await runtime.shutdown()
