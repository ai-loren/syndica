"""Runtime Module 与 Flag 声明。

本模块只定义声明和校验；`Runtime` 是唯一装配者。Flag key 是公开 API，因此拒绝归一化：
大小写、空白或连字符写错时当场报错，不把两个拼法悄悄并成一个。
"""

from __future__ import annotations

import inspect
import keyword
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, TypeAlias, cast

from syndica.kernel.errors import ConfigError
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.plugins import Plugin
from syndica.kernel.tool import Tool, validate_tool_names

if TYPE_CHECKING:
    from syndica.kernel.agent import BaseAgent
    from syndica.kernel.runtime import Runtime

__all__ = ['FlagDeclaration', 'RuntimeModule', 'SafetyModule']

FlagValue: TypeAlias = bool | int | str
AgentPluginFactory: TypeAlias = Callable[['Runtime', 'BaseAgent', Mapping[str, FlagValue]], Plugin]
RuntimeServiceFactory: TypeAlias = Callable[['Runtime', Mapping[str, FlagValue]], object]
AgentToolFactory: TypeAlias = Callable[
    ['Runtime', 'BaseAgent', Mapping[str, FlagValue]], Mapping[str, Tool]
]
RuntimeStartHook: TypeAlias = Callable[['Runtime', Mapping[str, FlagValue]], None]
RuntimeShutdownHook: TypeAlias = Callable[['Runtime'], Awaitable[None]]

_FLAG_KEY = re.compile(r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z')


def _config(expected: str, received: str, next_action: str) -> ConfigError:
    return ConfigError(expected=expected, received=received, next_action=next_action)


def _flag_value(value: object, field: str) -> FlagValue:
    if type(value) not in (bool, int, str):
        raise _config(
            f'{field} 是 bool | int | str 之一',
            f'收到 {field}={value!r}，类型是 {type(value).__name__}',
            f'把 {field} 改成 bool、int 或 str；结构化配置请拆成多个 Flag',
        )
    return cast(FlagValue, value)


def _optional_bound(value: object, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise _config(
            f'{field} 是 int 或 None',
            f'收到 {field}={value!r}，类型是 {type(value).__name__}',
            f'给 {field} 传整数边界；不需要该边界就传 None',
        )
    return value


@dataclass(frozen=True)
class FlagDeclaration:
    """一条同时供代码、设置界面与文档消费的 Flag 声明。"""

    key: str
    default: FlagValue
    description: str
    choices: tuple[FlagValue, ...] = ()
    minimum: int | None = None
    maximum: int | None = None

    def __post_init__(self) -> None:
        key = cast(object, self.key)
        if not isinstance(key, str) or _FLAG_KEY.fullmatch(key) is None:
            raise _config(
                'Flag key 是非空 snake_case 字符串',
                f'收到 key={key!r}，类型是 {type(key).__name__}',
                '改成小写字母开头、只含小写字母/数字/单下划线分段的名字；不要依赖自动归一化',
            )
        description = cast(object, self.description)
        if not isinstance(description, str) or not description.strip():
            raise _config(
                'Flag description 是非空字符串',
                (f'收到 description={description!r}，类型是 {type(description).__name__}'),
                '写清这个 Flag 控制什么；同一份说明会进入 CLI、设置界面与文档',
            )
        default = _flag_value(cast(object, self.default), 'default')
        raw_choices = cast(object, self.choices)
        if isinstance(raw_choices, str | bytes | bytearray) or not isinstance(
            raw_choices, Sequence
        ):
            raise _config(
                'choices 是与 default 同类型的值序列',
                f'收到 choices={raw_choices!r}，类型是 {type(raw_choices).__name__}',
                '传 list 或 tuple；不限制枚举值就省略 choices',
            )
        choices = tuple(cast(Sequence[object], raw_choices))
        for index, choice in enumerate(choices):
            if type(choice) is not type(default):
                raise _config(
                    f'choices 每一项都与 default 的类型 {type(default).__name__} 精确相同',
                    (
                        f'choices[{index}]={choice!r}，类型是 {type(choice).__name__}；'
                        f'default={default!r}'
                    ),
                    '统一 default 与 choices 的类型；bool 不得冒充 int',
                )
        duplicates = [choice for index, choice in enumerate(choices) if choice in choices[:index]]
        if duplicates:
            raise _config(
                'choices 中每个值只出现一次',
                f'重复值为 {duplicates!r}',
                '给 choices 去重，并保留希望展示给用户的顺序',
            )
        if choices and default not in choices:
            raise _config(
                'default 是 choices 中的一项',
                f'default={default!r} 不在 choices={choices!r}',
                '把 default 加进 choices，或改成其中一个已有值',
            )
        minimum = _optional_bound(cast(object, self.minimum), 'minimum')
        maximum = _optional_bound(cast(object, self.maximum), 'maximum')
        if type(default) is not int and (minimum is not None or maximum is not None):
            raise _config(
                'minimum / maximum 只用于 int Flag',
                (
                    f'default={default!r} 的类型是 {type(default).__name__}，'
                    f'minimum={minimum!r} maximum={maximum!r}'
                ),
                '删掉整数边界，或把 default 与 choices 全部改成 int',
            )
        if minimum is not None and maximum is not None and minimum > maximum:
            raise _config(
                'minimum 小于或等于 maximum',
                f'minimum={minimum} maximum={maximum}',
                '调换或修正两个边界，使允许区间非空',
            )
        if type(default) is int and (
            (minimum is not None and default < minimum)
            or (maximum is not None and default > maximum)
        ):
            raise _config(
                'int Flag 的 default 落在声明的闭区间内',
                f'default={default} minimum={minimum!r} maximum={maximum!r}',
                '调整 default 或边界，让默认值本身也是合法配置',
            )
        if type(default) is int:
            outside = [
                choice
                for choice in choices
                if (minimum is not None and cast(int, choice) < minimum)
                or (maximum is not None and cast(int, choice) > maximum)
            ]
            if outside:
                raise _config(
                    'int Flag 的 choices 全部落在声明的闭区间内',
                    f'越界 choices={outside!r} minimum={minimum!r} maximum={maximum!r}',
                    '删除越界 choice，或调整 minimum / maximum',
                )
        object.__setattr__(self, 'choices', cast(tuple[FlagValue, ...], choices))


def _sequence(value: object, field: str) -> tuple[object, ...]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise _config(
            f'RuntimeModule.{field} 是 list / tuple 序列',
            f'收到 {field}={value!r}，类型是 {type(value).__name__}',
            f'把 {field} 包成 list 或 tuple；没有内容就省略',
        )
    return tuple(cast(Sequence[object], value))


def _is_async_callable(callback: Callable[..., object]) -> bool:
    return inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
        type(callback).__call__
    )


def _callback(
    value: object,
    field: str,
    *,
    positional: int,
    asynchronous: bool | None,
) -> Callable[..., object]:
    if not callable(value):
        raise _config(
            f'{field} 是 callable',
            f'收到 {field}={value!r}，类型是 {type(value).__name__}',
            f'传入能接收 {positional} 个位置参数的函数',
        )
    callback = value
    try:
        inspect.signature(callback).bind(*([object()] * positional))
    except (TypeError, ValueError) as exc:
        raise _config(
            f'{field} 能接收 {positional} 个位置参数',
            f'{field}={callback!r} 的签名不匹配：{exc}',
            f'按 RuntimeModule 构造签名中 {field} 的 callable 类型修改参数',
        ) from exc
    is_async = _is_async_callable(callback)
    if asynchronous is not None and is_async is not asynchronous:
        expected_kind = 'async def' if asynchronous else '同步 def'
        received_kind = 'async def' if is_async else '同步 callable'
        raise _config(
            f'{field} 是 {expected_kind}',
            f'收到 {received_kind} {callback!r}',
            (
                f'把 {field} 改成 {expected_kind}；'
                '同步启动钩子不会被 await，异步关闭钩子的返回值必须能被 await'
            ),
        )
    return callback


def _callbacks(value: object, field: str, *, positional: int) -> tuple[Callable[..., object], ...]:
    callbacks = _sequence(value, field)
    return tuple(
        _callback(
            callback,
            f'RuntimeModule.{field}[{index}]',
            positional=positional,
            asynchronous=False,
        )
        for index, callback in enumerate(callbacks)
    )


@dataclass(frozen=True, init=False)
class RuntimeModule:
    """一个带 Flag 的自包含 Runtime 能力声明。"""

    name: str
    flags: tuple[FlagDeclaration, ...]
    agent_plugins: tuple[AgentPluginFactory, ...]
    services: Mapping[str, RuntimeServiceFactory]
    tool_factories: tuple[AgentToolFactory, ...]
    on_start: RuntimeStartHook | None
    on_shutdown: RuntimeShutdownHook | None

    def __init__(
        self,
        name: str,
        *,
        flags: Sequence[FlagDeclaration] = (),
        agent_plugins: Sequence[AgentPluginFactory] = (),
        services: Mapping[str, RuntimeServiceFactory] | None = None,
        tool_factories: Sequence[AgentToolFactory] = (),
        on_start: RuntimeStartHook | None = None,
        on_shutdown: RuntimeShutdownHook | None = None,
    ) -> None:
        raw_name = cast(object, name)
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise _config(
                'Runtime Module.name 是非空字符串',
                f'收到 name={raw_name!r}，类型是 {type(raw_name).__name__}',
                '给 Module 一个稳定名字，用于查重与排障',
            )
        raw_flags = _sequence(cast(object, flags), 'flags')
        checked_flags: list[FlagDeclaration] = []
        keys: list[str] = []
        for index, flag in enumerate(raw_flags):
            if not isinstance(flag, FlagDeclaration):
                raise _config(
                    'RuntimeModule.flags 的每一项都是 FlagDeclaration',
                    f'flags[{index}]={flag!r}，类型是 {type(flag).__name__}',
                    '先构造 FlagDeclaration，再放进 flags 序列',
                )
            if flag.key in keys:
                raise _config(
                    '一个 RuntimeModule 内每个 Flag key 只声明一次',
                    f'name={name!r} 重复声明 key={flag.key!r}',
                    '删除重复项；每个 key 只能有一个默认值、类型与说明',
                )
            checked_flags.append(flag)
            keys.append(flag.key)

        checked_plugins = cast(
            tuple[AgentPluginFactory, ...],
            _callbacks(cast(object, agent_plugins), 'agent_plugins', positional=3),
        )
        checked_tools = cast(
            tuple[AgentToolFactory, ...],
            _callbacks(cast(object, tool_factories), 'tool_factories', positional=3),
        )
        raw_service_input = cast(object, services)
        if raw_service_input is None:
            raw_services: Mapping[object, object] = {}
        elif not isinstance(raw_service_input, Mapping):
            raise _config(
                'RuntimeModule.services 是 service 名到 factory 的 Mapping，或 None',
                (f'收到 services={raw_service_input!r}，类型是 {type(raw_service_input).__name__}'),
                '传 dict，例如 services={"clock": make_clock}；没有服务就省略',
            )
        else:
            raw_services = cast(Mapping[object, object], raw_service_input)
        checked_services: dict[str, RuntimeServiceFactory] = {}
        for service_name, factory in raw_services.items():
            if (
                not isinstance(service_name, str)
                or not service_name.isidentifier()
                or service_name.startswith('_')
                or keyword.iskeyword(service_name)
            ):
                raise _config(
                    'Runtime service 名是非下划线开头的公开 Python 标识符',
                    (f'收到 service 名 {service_name!r}，类型是 {type(service_name).__name__}'),
                    '改成可作为 runtime.<name> 访问的公开标识符',
                )
            checked_services[service_name] = cast(
                RuntimeServiceFactory,
                _callback(
                    factory,
                    f'RuntimeModule.services[{service_name!r}] factory',
                    positional=2,
                    asynchronous=False,
                ),
            )
        raw_start = cast(object, on_start)
        if raw_start is not None:
            on_start = cast(
                RuntimeStartHook,
                _callback(
                    raw_start,
                    'RuntimeModule.on_start',
                    positional=2,
                    asynchronous=False,
                ),
            )
        raw_shutdown = cast(object, on_shutdown)
        if raw_shutdown is not None:
            on_shutdown = cast(
                RuntimeShutdownHook,
                _callback(
                    raw_shutdown,
                    'RuntimeModule.on_shutdown',
                    positional=1,
                    asynchronous=None,
                ),
            )

        object.__setattr__(self, 'name', name)
        object.__setattr__(self, 'flags', tuple(checked_flags))
        object.__setattr__(self, 'agent_plugins', checked_plugins)
        object.__setattr__(self, 'services', MappingProxyType(dict(checked_services)))
        object.__setattr__(self, 'tool_factories', checked_tools)
        object.__setattr__(self, 'on_start', on_start)
        object.__setattr__(self, 'on_shutdown', on_shutdown)


class SafetyModule(RuntimeModule):
    """等待墙钟与不可信高风险处置的 Flag 声明。"""

    def __init__(self) -> None:
        super().__init__(
            'safety',
            flags=(
                FlagDeclaration(
                    key='max_wait_seconds',
                    default=0,
                    description='单个 Agent 等待答复或另一个 Agent 的最长墙钟秒数；0 表示不限制',
                    minimum=0,
                ),
                FlagDeclaration(
                    key='untrusted_high_risk',
                    default='reject',
                    description='高风险调用可溯源到不可信内容时的处置',
                    choices=('reject', 'ask', 'allow'),
                ),
            ),
        )


def _module_sequence(value: object) -> tuple[RuntimeModule, ...]:
    if value is None:
        return ()
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise _config(
            'modules 是 RuntimeModule 实例的 list / tuple 序列',
            f'收到 modules={value!r}，类型是 {type(value).__name__}',
            '传入 RuntimeModule 实例序列，例如 modules=[RuntimeModule("audit")]',
        )
    modules: list[RuntimeModule] = []
    names: list[str] = []
    for index, module in enumerate(cast(Sequence[object], value)):
        if isinstance(module, type) or not isinstance(module, RuntimeModule):
            raise _config(
                f'modules[{index}] 是 RuntimeModule 实例',
                f'收到 {module!r}，类型是 {type(module).__name__}',
                '传入构造后的实例；若传的是 RuntimeModule 类本身，请加上括号',
            )
        if module.name in names:
            raise _config(
                '一个 Runtime 内每个 Runtime Module 名只出现一次',
                f'modules[{index}] 的 name={module.name!r} 与前面重复',
                '去掉重复 Module；重复装配会让 hook、工具与清理各执行两次',
            )
        modules.append(module)
        names.append(module.name)
    return tuple(modules)


def _declarations(
    modules: Sequence[RuntimeModule],
) -> tuple[FlagDeclaration, ...]:
    declarations: list[FlagDeclaration] = []
    owners: dict[str, str] = {}
    for module in modules:
        for declaration in module.flags:
            previous = owners.get(declaration.key)
            if previous is not None:
                raise _config(
                    '一个 Runtime 内每个 Flag key 只由一个 Module 声明',
                    (f'key={declaration.key!r} 同时由 Module {previous!r} 与 {module.name!r} 声明'),
                    '给其中一项换一个稳定 key；即使声明相同，也必须只有一个所有者',
                )
            owners[declaration.key] = module.name
            declarations.append(declaration)
    return tuple(declarations)


def _override(declaration: FlagDeclaration, value: object, *, source: str) -> FlagValue:
    expected_type = type(declaration.default)
    if type(value) is not expected_type:
        raise _config(
            (
                f'Flag {declaration.key!r} 是 {expected_type.__name__}，'
                '与声明的 default 类型精确相同'
            ),
            f'{source}[{declaration.key!r}]={value!r}，类型是 {type(value).__name__}',
            f'修正 {declaration.key!r} 的值；bool 不得冒充 int',
        )
    checked = value
    if declaration.choices and checked not in declaration.choices:
        raise _config(
            f'Flag {declaration.key!r} 是 choices={declaration.choices!r} 中的一项',
            f'{source}[{declaration.key!r}]={checked!r}',
            f'修正 {declaration.key!r}，使用声明列出的值之一',
        )
    if type(checked) is int and (
        (declaration.minimum is not None and checked < declaration.minimum)
        or (declaration.maximum is not None and checked > declaration.maximum)
    ):
        raise _config(
            (
                f'Flag {declaration.key!r} 落在 '
                f'minimum={declaration.minimum!r} maximum={declaration.maximum!r} 的闭区间'
            ),
            f'{source}[{declaration.key!r}]={checked!r}',
            f'修正 {declaration.key!r}，使它不越过 minimum / maximum',
        )
    return checked


def _overrides(
    value: object,
    declarations: Mapping[str, FlagDeclaration],
    *,
    source: str,
) -> dict[str, FlagValue]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _config(
            f'{source} 是 Flag key 到 bool | int | str 的 Mapping，或 None',
            f'收到 {source}={value!r}，类型是 {type(value).__name__}',
            f'修正 {source}：传 dict，例如 {source}={{"feature_enabled": True}}',
        )
    resolved: dict[str, FlagValue] = {}
    for key, raw in cast(Mapping[object, object], value).items():
        if not isinstance(key, str) or key not in declarations:
            raise _config(
                f'{source} 的每个 key 都由已安装的 Runtime Module 声明',
                f'收到未知 key={key!r}；已声明: {sorted(declarations)}',
                f'修正 {source}：删除这个 key，或安装声明它的 Module',
            )
        resolved[key] = _override(declarations[key], raw, source=source)
    return resolved


class _ModulePlugin:
    """冻结一次读到的插件名，同时保留原插件的 register 行为。"""

    def __init__(self, name: str, plugin: Plugin) -> None:
        self._name = name
        self._plugin = plugin

    @property
    def name(self) -> str:
        return self._name

    def register(self, hooks: HooksCenter) -> None:
        self._plugin.register(hooks)


class _RuntimeModuleAssembly:  # pyright: ignore[reportUnusedClass]
    """一次 Runtime 的 Module 声明、Flag 默认值与生命周期回调。"""

    def __init__(self, modules: object, flags: object) -> None:
        self.module_objects = _module_sequence(modules)
        self.names = tuple(module.name for module in self.module_objects)
        self.declarations = _declarations(self.module_objects)
        by_key = {declaration.key: declaration for declaration in self.declarations}
        resolved = {declaration.key: declaration.default for declaration in self.declarations}
        resolved.update(_overrides(flags, by_key, source='flags'))
        self.flags: Mapping[str, FlagValue] = MappingProxyType(resolved)
        self._mounted_services: tuple[str, ...] = ()

    def agent_flags(
        self,
        overrides: object,
        *,
        inherited: Mapping[str, FlagValue] | None = None,
    ) -> Mapping[str, FlagValue]:
        """解析一个 Agent 的完整 Flag 快照。"""
        by_key = {declaration.key: declaration for declaration in self.declarations}
        resolved = dict(self.flags)
        if inherited is not None:
            resolved.update(_overrides(inherited, by_key, source='inherited flags'))
        resolved.update(_overrides(overrides, by_key, source='spawn flags'))
        return MappingProxyType(resolved)

    def install_agent(
        self,
        runtime: Runtime,
        agent: BaseAgent,
        flags: Mapping[str, FlagValue],
    ) -> None:
        """构造完全部注入物后，一次性装到 Agent 自己的 Module 层。"""
        plugins: list[Plugin] = []
        plugin_owners: dict[str, str] = {}
        tool_batches: list[tuple[str, dict[str, Tool]]] = []
        for module in self.module_objects:
            for index, factory in enumerate(module.agent_plugins):
                produced = cast(object, factory(runtime, agent, flags))
                if inspect.isawaitable(produced):
                    if inspect.iscoroutine(produced):
                        produced.close()
                    raise _config(
                        f'Module {module.name!r} 的 agent_plugins[{index}] 同步返回 Plugin',
                        f'factory 返回了 awaitable：{type(produced).__name__}',
                        '把异步初始化放进 Plugin 的 hook；factory 必须直接返回 Plugin 实例',
                    )
                if isinstance(produced, type):
                    raise _config(
                        f'Module {module.name!r} 的 agent_plugins[{index}] 返回 Plugin 实例',
                        f'实际返回类对象 {produced.__name__}',
                        f'给 {produced.__name__} 加上括号并返回构造后的实例',
                    )
                plugin_name = getattr(produced, 'name', None)
                register = getattr(produced, 'register', None)
                if (
                    not isinstance(plugin_name, str)
                    or not plugin_name.strip()
                    or not callable(register)
                ):
                    raise _config(
                        (
                            f'Module {module.name!r} 的 agent_plugins[{index}] '
                            '返回带非空 name 与 register(hooks) 的 Plugin'
                        ),
                        (
                            f'实际返回 {type(produced).__name__}：'
                            f'name={plugin_name!r} register callable={callable(register)}'
                        ),
                        '返回 Plugin 实例；name 用于查重，register 把 hook 挂到收到的中心',
                    )
                previous = plugin_owners.get(plugin_name)
                if previous is not None:
                    raise _config(
                        '同一 Agent 的 Runtime Module 插件名唯一',
                        (
                            f'plugin name={plugin_name!r} 同时来自 Module '
                            f'{previous!r} 与 {module.name!r}'
                        ),
                        '给其中一个插件换名，或只保留一个提供该插件的 Module',
                    )
                plugin_owners[plugin_name] = module.name
                plugins.append(_ModulePlugin(plugin_name, cast(Plugin, produced)))
            for index, factory in enumerate(module.tool_factories):
                produced = cast(object, factory(runtime, agent, flags))
                if inspect.isawaitable(produced):
                    if inspect.iscoroutine(produced):
                        produced.close()
                    raise _config(
                        f'Module {module.name!r} 的 tool_factories[{index}] 同步返回工具 Mapping',
                        f'factory 返回了 awaitable：{type(produced).__name__}',
                        '让 factory 直接返回 {工具名: Tool}；异步工作放进 Tool.execute',
                    )
                if not isinstance(produced, Mapping):
                    raise _config(
                        (
                            f'Module {module.name!r} 的 tool_factories[{index}] '
                            '返回 Mapping[str, Tool]'
                        ),
                        f'实际返回 {type(produced).__name__} 类型的 {produced!r}',
                        '返回 {工具名: Tool}；没有工具时返回空 dict',
                    )
                validate_tool_names(cast(object, produced))
                tool_batches.append((module.name, dict(cast(Mapping[str, Tool], produced))))

        tools: dict[str, Tool] = {}
        owners: dict[str, str] = {}
        existing = set(agent.options.tools)
        for module_name, batch in tool_batches:
            for name, tool in batch.items():
                previous = owners.get(name)
                if name in existing or previous is not None:
                    owner = 'Agent 已有工具' if name in existing else f'Module {previous!r}'
                    raise _config(
                        'Runtime Module 只新增工具，不覆盖已有名字',
                        f'Module {module_name!r} 的工具 {name!r} 与{owner}冲突',
                        '给工具换一个名字，或只保留一个提供该名字的 Module',
                    )
                owners[name] = module_name
                tools[name] = tool

        agent._install_module_plugins(plugins)  # pyright: ignore[reportPrivateUsage]
        if tools:
            agent.install_runtime_tools(tools)

    def install(self, runtime: Runtime) -> None:
        services = [
            (module, name, factory)
            for module in self.module_objects
            for name, factory in module.services.items()
        ]
        names: set[str] = set()
        existing = set(dir(runtime))
        for module, name, _ in services:
            if name in existing or name in names:
                owner = (
                    'Runtime 已有公开属性' if name in existing else '前面的另一个 Runtime Module'
                )
                raise _config(
                    'Runtime service 名不覆盖现有公开属性，且在 Module 间唯一',
                    f'Module {module.name!r} 的 service {name!r} 与{owner}冲突',
                    '换一个 service 名；服务会直接挂成 runtime.<name>',
                )
            names.add(name)

        hooks = runtime.hooks
        snapshot = hooks._assembly_snapshot()  # pyright: ignore[reportPrivateUsage]
        mounted: list[str] = []
        try:
            for _module, name, factory in services:
                service = factory(runtime, self.flags)
                if inspect.isawaitable(service):
                    if inspect.iscoroutine(service):
                        service.close()
                    raise _config(
                        f'Module {_module.name!r} 的 service {name!r} 由同步 factory 创建',
                        f'factory 返回了 awaitable：{type(service).__name__}',
                        (
                            '把异步初始化留在 service 自己的方法里；'
                            'service factory 必须直接返回可挂到 runtime 上的对象'
                        ),
                    )
                setattr(runtime, name, service)
                mounted.append(name)
            for module in self.module_objects:
                if module.on_start is None:
                    continue
                returned = module.on_start(runtime, self.flags)
                if returned is not None:
                    if inspect.iscoroutine(returned):
                        returned.close()
                    raise _config(
                        f'Module {module.name!r} 的 on_start 返回 None',
                        (
                            f'Module {module.name!r} 实际返回 '
                            f'{type(returned).__name__} 类型的 {returned!r}'
                        ),
                        '让 on_start 完成同步装配后 return None；异步初始化请放进 Runtime service',
                    )
        except BaseException:
            hooks._assembly_restore(snapshot)  # pyright: ignore[reportPrivateUsage]
            for name in reversed(mounted):
                delattr(runtime, name)
            raise
        self._mounted_services = tuple(mounted)

    async def shutdown(self, runtime: Runtime) -> list[BaseException]:
        failures: list[BaseException] = []
        for module in reversed(self.module_objects):
            callback = module.on_shutdown
            if callback is None:
                continue
            try:
                returned = callback(runtime)
                if not inspect.isawaitable(returned):
                    raise _config(
                        f'Module {module.name!r} 的 on_shutdown 返回 Awaitable[None]',
                        f'实际返回 {type(returned).__name__} 类型的 {returned!r}',
                        '让 on_shutdown 返回 coroutine / Future，并在完成时返回 None',
                    )
                completed = await returned
                if completed is not None:
                    raise _config(
                        f'Module {module.name!r} 的 on_shutdown await 后返回 None',
                        f'实际返回 {type(completed).__name__} 类型的 {completed!r}',
                        '让 on_shutdown 的异步清理结束时 return None',
                    )
            except BaseException as exc:
                failures.append(exc)
        return failures
