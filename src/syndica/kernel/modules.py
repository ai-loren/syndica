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
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, TypeAlias, cast

from syndica import __version__
from syndica.kernel.danger import require_own_command_args
from syndica.kernel.errors import ConfigError
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.hooks.installing import installing
from syndica.kernel.hooks.junctions import MODULE_NAME, ExtensionJunction
from syndica.kernel.plugins import Plugin
from syndica.kernel.terminal_text import UNSAFE_TEXT
from syndica.kernel.tool import Tool, validate_tool_names

if TYPE_CHECKING:
    from syndica.kernel.agent import BaseAgent
    from syndica.kernel.redaction import Redactor
    from syndica.kernel.runtime import Runtime

__all__ = [
    'MODULE_CAPABILITIES',
    'FlagDeclaration',
    'FlagRow',
    'ModuleHost',
    'RuntimeModule',
    'SafetyModule',
    'flag_reference',
]

FlagValue: TypeAlias = bool | int | str
AgentPluginFactory: TypeAlias = Callable[['Runtime', 'BaseAgent', Mapping[str, FlagValue]], Plugin]
RuntimeServiceFactory: TypeAlias = Callable[['Runtime', Mapping[str, FlagValue]], object]
AgentToolFactory: TypeAlias = Callable[
    ['Runtime', 'BaseAgent', Mapping[str, FlagValue]], Mapping[str, Tool]
]
RuntimeStartHook: TypeAlias = Callable[['Runtime', Mapping[str, FlagValue]], None]
RuntimeShutdownHook: TypeAlias = Callable[['Runtime'], Awaitable[None]]

_FLAG_KEY = re.compile(r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z')
# 第三方能力包声明它要什么（spec §12.4），供人审查。闭集：词表外的词没有人知道该怎么审。
MODULE_CAPABILITIES: tuple[str, ...] = ('environment', 'filesystem', 'network', 'subprocess')


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
        unsafe = UNSAFE_TEXT.search(description)
        position = description.find('\n') if unsafe is None else unsafe.start()
        if position >= 0:
            raise _config(
                'Flag description 是不含控制字符的单行文本',
                f'description 在第 {position} 个字符处有 {description[position]!r}',
                '写成一行、删掉这个字符：说明会显示在终端、设置界面与参考文档里，'
                '控制字符能在终端上伪造显示，换行会拆开文档的表格',
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


def _module_name(value: object, field: str) -> str:
    if not isinstance(value, str) or MODULE_NAME.fullmatch(value) is None:
        raise _config(
            f'{field} 是 Module 名的规范拼法：小写字母开头，只含小写字母、数字与单个连字符分段',
            f'收到 {field}={value!r}，类型是 {type(value).__name__}',
            '改成 retry、prefix-drift 这样的名字；它同时是扩展切点的 owner 段、入口点名与配置里'
            '启用它的写法，三处必须是同一个拼法，因此不做任何归一化',
        )
    return value


def _shown_text(value: object, field: str, *, one_line: bool) -> str:
    """要拿去给人看的清单字符串：不含会在终端上改写显示的字符。"""
    if not isinstance(value, str):
        raise _config(
            f'RuntimeModule.{field} 是字符串',
            f'收到 {field}={value!r}，类型是 {type(value).__name__}',
            f'传 str；没有要说的就省略 {field}',
        )
    found = UNSAFE_TEXT.search(value)
    if found is None and one_line:
        newline = value.find('\n')
        if newline >= 0:
            raise _config(
                f'RuntimeModule.{field} 只占一行',
                f'{field} 在第 {newline} 个字符处换行',
                f'把 {field} 写成一行；详细说明放进 security_notes',
            )
    if found is not None:
        raise _config(
            f'RuntimeModule.{field} 不含控制字符（C0、C1、双向文字控制字符）',
            f'{field} 在第 {found.start()} 个字符处有 {found.group()!r}',
            f'删掉这个字符：{field} 会显示给要决定信不信任这个 Module 的人，'
            '控制字符能在终端上伪造显示',
        )
    return value


def _version(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(character.isspace() for character in value)
        or UNSAFE_TEXT.search(value) is not None
    ):
        raise _config(
            'RuntimeModule.version 是非空、不含空白与控制字符的字符串',
            f'收到 version={value!r}，类型是 {type(value).__name__}',
            '写成发行版本号，例如 1.2.0 或 1.0.0rc1；版本只比较是否相等，不做解析',
        )
    return value


def _capabilities(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    declared: list[str] = []
    for index, item in enumerate(_sequence(value, 'capabilities')):
        if not isinstance(item, str) or item not in MODULE_CAPABILITIES:
            raise _config(
                f'capabilities 的每一项是 {list(MODULE_CAPABILITIES)} 之一',
                f'capabilities[{index}]={item!r}，类型是 {type(item).__name__}',
                '只写这个 Module 真的需要的能力；什么都不要就写 ()，'
                '拿不准就省略（按未声明、即拥有全部能力对待）',
            )
        if item in declared:
            raise _config(
                'capabilities 里每项能力只写一次',
                f'capabilities[{index}]={item!r} 与前面重复',
                '删掉重复的那一项',
            )
        declared.append(item)
    return tuple(declared)


def _requires(value: object, name: str) -> tuple[str, ...]:
    required: list[str] = []
    for index, item in enumerate(_sequence(value, 'requires')):
        dependency = _module_name(item, f'requires[{index}]')
        if dependency == name:
            raise _config(
                f'Module {name!r} 不依赖自己',
                f'requires[{index}]={dependency!r} 就是它自己',
                '删掉这一项',
            )
        if dependency in required:
            raise _config(
                'requires 里每个依赖只写一次',
                f'requires[{index}]={dependency!r} 与前面重复',
                '删掉重复的那一项',
            )
        required.append(dependency)
    return tuple(required)


def _junctions(value: object, name: str) -> tuple[ExtensionJunction, ...]:
    owned: list[ExtensionJunction] = []
    for index, item in enumerate(_sequence(value, 'junctions')):
        if not isinstance(item, ExtensionJunction):
            raise _config(
                'junctions 的每一项是 ExtensionJunction',
                f'junctions[{index}]={item!r}，类型是 {type(item).__name__}',
                f'先构造 ExtensionJunction("{name}:<event>", JunctionKind.<KIND>)，'
                '再放进 junctions；工具与插件拿同一个对象触发它',
            )
        if item.owner != name:
            raise _config(
                f'Module {name!r} 拥有的扩展切点 owner 段是 {name!r}',
                f'junctions[{index}] 是 {item.name!r}，owner 段是 {item.owner!r}',
                f'改名为 {name}:<event>：Module 名是保留的 owner，别人用不了它，它也不用别人的',
            )
        if any(existing.name == item.name for existing in owned):
            raise _config(
                'junctions 里每个切点名只出现一次',
                f'junctions[{index}] 的名字 {item.name!r} 与前面重复',
                '删掉重复的那一项；一个切点只有一个句柄',
            )
        owned.append(item)
    return tuple(owned)


def _replay_safe(value: object) -> bool:
    if type(value) is not bool:
        raise _config(
            'RuntimeModule.replay_safe 是 bool',
            f'收到 replay_safe={value!r}，类型是 {type(value).__name__}',
            '传 True 或 False；不确定就省略（缺省 False，回放不会跑这个 Module 的代码）',
        )
    return value


@dataclass(frozen=True, init=False)
class RuntimeModule:
    """一个带 Flag 的自包含 Runtime 能力声明。

    清单字段（`version` 起）供人审查与回放判据：`capabilities=None` 是**未声明**，按拥有全部
    能力对待；`replay_safe=True` 是作者声明它的工具、hook、服务与启停钩子不做 I/O、不读时钟与
    随机数、不起线程或执行器——回放跑在虚拟时钟上，等一个线程会让事件循环看起来空闲、定时器提前
    触发。声明错了，回放报分叉。

    装配失败按快照回滚内核持有的状态（hook 中心、服务）；已经 `on_start` 过的 Module 不补偿：
    `on_shutdown` 是异步的，同步的 Runtime 构造里调不了。第三方在 `on_start` 里起的任务、写的
    文件不在回滚承诺内，副作用请放进服务的惰性方法里。
    """

    name: str
    flags: tuple[FlagDeclaration, ...]
    agent_plugins: tuple[AgentPluginFactory, ...]
    services: Mapping[str, RuntimeServiceFactory]
    tool_factories: tuple[AgentToolFactory, ...]
    on_start: RuntimeStartHook | None
    on_shutdown: RuntimeShutdownHook | None
    version: str
    description: str
    capabilities: tuple[str, ...] | None
    requires: tuple[str, ...]
    junctions: tuple[ExtensionJunction, ...]
    replay_safe: bool
    security_notes: str

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
        version: str = '0.0.0',
        description: str = '',
        capabilities: Sequence[str] | None = None,
        requires: Sequence[str] = (),
        junctions: Sequence[ExtensionJunction] = (),
        replay_safe: bool = False,
        security_notes: str = '',
    ) -> None:
        name = _module_name(cast(object, name), 'RuntimeModule.name')
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
        object.__setattr__(self, 'version', _version(cast(object, version)))
        shown = _shown_text(cast(object, description), 'description', one_line=True)
        object.__setattr__(self, 'description', shown)
        object.__setattr__(self, 'capabilities', _capabilities(cast(object, capabilities)))
        object.__setattr__(self, 'requires', _requires(cast(object, requires), name))
        object.__setattr__(self, 'junctions', _junctions(cast(object, junctions), name))
        object.__setattr__(self, 'replay_safe', _replay_safe(cast(object, replay_safe)))
        object.__setattr__(
            self,
            'security_notes',
            _shown_text(cast(object, security_notes), 'security_notes', one_line=False),
        )


class SafetyModule(RuntimeModule):
    """等待墙钟与不可信高风险处置的 Flag 声明。"""

    def __init__(self) -> None:
        super().__init__(
            'safety',
            version=__version__,
            description='等待的墙钟上限与不可信高风险调用的处置',
            capabilities=(),
            replay_safe=True,
            security_notes=(
                'untrusted_high_risk=allow 让可溯源到不可信内容的高风险调用直接执行，'
                '跳过 spec §12.2 的强制升级。'
            ),
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


def _refuse_direct_installs(
    agent: BaseAgent, before: Mapping[str, Tool], module: str, where: str
) -> None:
    """Module 的工具只经 `tool_factories` 的返回值装上：D7 的声明检查与之后按 Module 认的一切都按
    那张表来，工厂里直接往 Agent 上装的工具绕过了它们。"""
    added = sorted(
        name for name, tool in agent.options.tools.items() if before.get(name) is not tool
    )
    if added:
        raise _config(
            f'Module {module!r} 的工具只经 tool_factories 交回的映射装上',
            f'它的 {where} 在调用期间直接往 Agent 上装了 {added}',
            '把这些工具放进 tool_factories 交回的映射里；直接装的工具绕过了命令参数声明的检查',
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
    for index, module in enumerate(modules):
        for dependency in module.requires:
            if dependency in names[:index]:
                continue
            if dependency in names:
                raise _config(
                    f'Module {module.name!r} 依赖的 {dependency!r} 排在它前面',
                    f'modules[{index}]={module.name!r} 排在 {dependency!r} 之前',
                    f'把 {dependency!r} 挪到 {module.name!r} 前面：依赖的 Module 先装',
                )
            raise _config(
                f'Module {module.name!r} 依赖的 {dependency!r} 也在 modules 里',
                f'modules 里没有 {dependency!r}（已有 {names}）',
                f'把 {dependency!r} 加进 modules、放在 {module.name!r} 前面，或去掉这个依赖',
            )
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
    """冻结一次读到的插件名，同时保留原插件的 register 行为。register 期间的装配期身份是 Module 名，
    不是插件名：Module 的切点写在清单里，它的插件不在 register 里声明。"""

    def __init__(self, name: str, plugin: Plugin, module: str, agent: BaseAgent) -> None:
        self._name = name
        self._plugin = plugin
        self._module = module
        self._agent = agent

    @property
    def name(self) -> str:
        return self._name

    def register(self, hooks: HooksCenter) -> None:
        before = dict(self._agent.options.tools)
        with installing('module', self._module):
            self._plugin.register(hooks)
        where = f'插件 {self._name!r} 的 register'
        _refuse_direct_installs(self._agent, before, self._module, where)


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
    ) -> tuple[dict[str, str], dict[str, str]]:
        """构造完全部注入物后，一次性装到 Agent 自己的 Module 层。交回「工具名 → 装它的 Module
        名」与「插件名 → 装它的 Module 名」：之后装上的工具撞名时据此点名对方，trace 的 `module`
        span 据此写出每个 Module 给了这个 Agent 什么。"""
        plugins: list[Plugin] = []
        plugin_owners: dict[str, str] = {}
        tool_batches: list[tuple[str, dict[str, Tool]]] = []
        for module in self.module_objects:
            for index, factory in enumerate(module.agent_plugins):
                before = dict(agent.options.tools)
                with installing('module', module.name):
                    produced = cast(object, factory(runtime, agent, flags))
                _refuse_direct_installs(agent, before, module.name, f'agent_plugins[{index}]')
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
                plugins.append(
                    _ModulePlugin(plugin_name, cast(Plugin, produced), module.name, agent)
                )
            for index, factory in enumerate(module.tool_factories):
                before = dict(agent.options.tools)
                with installing('module', module.name):
                    produced = cast(object, factory(runtime, agent, flags))
                _refuse_direct_installs(agent, before, module.name, f'tool_factories[{index}]')
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
                for tool_name, tool in cast(Mapping[str, Tool], produced).items():
                    require_own_command_args(tool, tool_name, f'Module {module.name!r}')
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
        return owners, plugin_owners

    def declare_junctions(self, hooks: HooksCenter) -> None:
        """按对象声明全部 Module 清单里的切点。Module 名各不相同、切点的 owner 段等于 Module 名，
        因此这里不会撞名。"""
        for module in self.module_objects:
            for handle in module.junctions:
                hooks._declare_handle(handle)  # pyright: ignore[reportPrivateUsage]

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
                with installing('module', _module.name):
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
                with installing('module', module.name):
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


@dataclass(frozen=True)
class FlagRow:
    """一条 Flag 的参考行（M4 D15）：CLI 的 `flags list/show`、参考文档与以后的设置界面用同一份。
    `kind` 是取值的类型名（`bool` / `int` / `str`）。"""

    key: str
    module: str
    kind: str
    default: FlagValue
    choices: tuple[FlagValue, ...]
    minimum: int | None
    maximum: int | None
    description: str


def flag_reference(modules: Sequence[RuntimeModule]) -> tuple[FlagRow, ...]:
    """这些 Module 声明的全部 Flag：按 Module 的次序、再按各自声明的次序，不重排——次序就是装配
    次序。纯函数：不读环境，渲染与取舍（比如宿主拒收的取值）交给调用方。"""
    return tuple(
        FlagRow(
            key=declaration.key,
            module=module.name,
            kind=type(declaration.default).__name__,
            default=declaration.default,
            choices=declaration.choices,
            minimum=declaration.minimum,
            maximum=declaration.maximum,
            description=declaration.description,
        )
        for module in _module_sequence(modules)
        for declaration in module.flags
    )


@dataclass(frozen=True)
class ModuleHost:
    """宿主交给第三方 Module 工厂的环境（M4 D15）：入口点是 `factory(host) -> RuntimeModule`。

    **只追加字段，且新字段带默认值**：已发布的 Module 收它作唯一参数，第三方的测试会自己构造它；
    加一个没有默认值的字段，那些构造就全断了，删字段同理。`environment`
    是宿主交给 Agent 的那份白名单环境，不是进程的完整环境；Module 与 syndica 同进程，真要读完整
    环境它也读得到，这份是让守规矩的 Module 不必去碰 `os.environ`。
    """

    workspace_root: Path
    environment: Mapping[str, str]
    redactor: Redactor

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.workspace_root), Path):
            raise _config(
                'ModuleHost.workspace_root 是 pathlib.Path',
                f'收到 {type(self.workspace_root).__name__} 类型的 {self.workspace_root!r}',
                '传 Path(...)；字符串路径在不同调用方之间会按不同的当前目录解析',
            )
        raw = cast(object, self.environment)
        received = type(raw).__name__
        if not isinstance(raw, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in cast(Mapping[object, object], raw).items()
        ):
            raise _config(
                'ModuleHost.environment 是 str 到 str 的 Mapping',
                f'收到 {received} 类型的值',
                '传宿主交给 Agent 的那份白名单环境（dict[str, str]）',
            )
        object.__setattr__(
            self, 'environment', MappingProxyType(dict(cast(Mapping[str, str], raw)))
        )
        if not callable(getattr(cast(object, self.redactor), 'redact', None)):
            raise _config(
                'ModuleHost.redactor 有 redact(value) 方法（内核的 Redactor）',
                f'收到 {type(self.redactor).__name__} 类型的值，没有可调用的 redact',
                '传宿主自己用的那个脱敏器：Module 写盘或外发的东西要过同一套规则',
            )
