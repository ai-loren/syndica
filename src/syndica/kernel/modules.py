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
from syndica.kernel.plugins import Plugin
from syndica.kernel.tool import Tool

if TYPE_CHECKING:
    from syndica.kernel.agent import BaseAgent
    from syndica.kernel.runtime import Runtime

__all__ = ['FlagDeclaration', 'RuntimeModule']

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
