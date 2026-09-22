"""LLM 抽象。

内核不认识具体 provider：provider 只是字符串标识，实现由 backend 提供。config 不读环境变量，
API 密钥、base URL 这类部署事实由宿主填进 `client_args`。

请求参数归实例：config 给默认值，调用方只传单次覆盖。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from typing import Any, Protocol, Self, cast, runtime_checkable

from syndica.kernel.context import ContextBlock
from syndica.kernel.errors import ConfigError, SyndicaError

__all__ = ['CachePrefixProvider', 'ContextOverflowError', 'LLM', 'LLMConfig']


class ContextOverflowError(SyndicaError):
    """请求超出上下文窗口。触发 CONTEXT_OVERFLOW 切点，由压缩能力处理。

    它**不属于** LLMResponseError：超窗是请求侧、压缩后可重试的状况，LLMResponseError 说的是
    响应不可用。嵌进去会被先写的 `except LLMResponseError` 吞掉，压缩永不触发，只表现为任务失败。
    """


def _copied_mapping(value: object, field: str) -> dict[str, Any]:
    """两个 dict 字段只浅拷：嵌套活对象必须保住。

    非映射在构造时拒绝，不要等 `dict()` 抛 TypeError。
    """
    if not isinstance(value, Mapping):
        raise ConfigError(
            expected=f'{field} 是映射（dict 或其子类）',
            received=f'{field}={value!r}，类型是 {type(value).__name__}',
            next_action=f'传一个 dict 给 {field}，或省略它用空 dict',
        )
    return dict(cast(Mapping[str, Any], value))


def _validated_changes(cls: type[LLMConfig], changes: dict[str, Any]) -> dict[str, Any]:
    """校验 derive 的关键字都是 cls 的字段。字段从 cls 取，子类才能 derive 自己声明的字段。"""
    known = sorted(f.name for f in fields(cls))
    unknown = sorted(set(changes) - set(known))
    if unknown:
        raise ConfigError(
            expected=f'derive 的关键字都是 {cls.__name__} 的字段: {known}',
            received=f'收到未声明的字段 {unknown}',
            next_action=f'改用上面列出的字段名；要加新参数先在 {cls.__name__} 上声明字段',
        )
    return changes


@dataclass(frozen=True)
class LLMConfig:
    """一次 LLM 调用的配置。provider 与 model 决定缓存键与计费归属，**字段顺序计入公开 API**。

    构造与 `derive` 各把两个参数字典拷一份，改一个碰不到另一个。三条边界，调用方看代码看不出来：

    - 拷的是字典本身、**不是里面的值**——嵌套容器仍与传入方共享，值当只读用。这是有意的：
      `client_args` 装得下活的 client 或 auth callable，深拷要么当场失败要么产出坏副本。
    - `copy.copy` 绕过构造器，副本与母配置**共享**两个字典；`deepcopy` 与 `pickle` 不共享。
    - `frozen=True` 只挡字段重新绑定，字典本身仍可被就地改。

    `hash(cfg)` 因两个 dict 字段而抛 TypeError；要可哈希的身份用 `(cfg.provider, cfg.model)`。
    """

    provider: str
    model: str
    client_args: dict[str, Any] = field(default_factory=dict[str, Any])
    request_args: dict[str, Any] = field(default_factory=dict[str, Any])
    timeout: float = 600.0
    context_window: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'client_args', _copied_mapping(self.client_args, 'client_args'))
        object.__setattr__(self, 'request_args', _copied_mapping(self.request_args, 'request_args'))

    def derive(self, **changes: Any) -> Self:
        """派生必须走此方法：影响身份的参数直接改会让缓存键与计费归属静默错位。"""
        return replace(self, **_validated_changes(type(self), changes))


@runtime_checkable
class LLM(Protocol):
    @property
    def config(self) -> LLMConfig:
        """**生效后的**配置，不一定是调用方构造时传进去的那个：backend 可以补上它自己才知道的
        事实（如 `context_window`）。包装器（重试 / 故障转移 / 缓存）必须转发**委托对象的**
        config；自己存一份构造时的副本，就又造出两个必须彼此一致的东西。
        """
        ...

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        """返回一个 assistant block。超窗抛 ContextOverflowError。

        **进请求的只有 `role` / `content` / `tool_calls` / `tool_call_id` 这四个字段**；
        `extra` / `trust` / `artifacts` 一律不进。它们是内核自己的旁路数据，模型看不到是内核
        依赖的性质，不是巧合：

        - `extra['tool_meta']` 装着每一次工具调用的 `meta`，而 spec §5.6 把「给模型看的」与
          「给代码用的」分开正是为了不让结构化数据进上下文。内核据此把某些东西**只**放进 `meta`
          （`QueryAgent` 报出的子代 id 就是一例：模型顺着它只能往上走，拿不到别人的子树）。把
          block 整个 `asdict` 出去的实现会把这些全部灌进模型上下文。
        - `trust` 不进字节是分叉能复用前缀缓存的前提（spec §5.2 纪律 4）：内核会收紧一块继承来的
          `trust` 来补偿截断丢掉的标记，而分身的前缀必须与源逐字节相同。

        实现必须在 `config.timeout` 内返回或抛出。违反后果：abort 不打断轮内调用，`create` 不返回
        时唯一出路是 `kill`。不要在内核里包 `asyncio.timeout`：那会把取消域从 provider 手里拿走、
        远端连接留在那里。
        """
        ...


@runtime_checkable
class CachePrefixProvider(Protocol):
    """给漂移检测器提供模型实际用于前缀匹配的有序请求单位。

    顺序固定为 provider/model、最终请求参数、system、tools、逐条历史。每项必须是稳定编码后的
    `bytes`；只有在末尾追加历史时，旧 tuple 才会是新 tuple 的前缀。
    """

    def cache_prefix_units(
        self,
        messages: list[ContextBlock],
        **kwargs: Any,
    ) -> tuple[bytes, ...]: ...


def _stable_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(',', ':'),
    ).encode('utf-8')


def _provider_visible_message(block: ContextBlock) -> dict[str, Any]:
    values: dict[str, Any] = {
        'role': block.role,
        'content': block.content,
        'tool_calls': [
            {'id': call.id, 'name': call.name, 'arguments': call.arguments}
            for call in block.tool_calls
        ],
        'tool_call_id': block.tool_call_id,
    }
    return {key: value for key, value in values.items() if value}


def _logical_cache_prefix_units(  # pyright: ignore[reportUnusedFunction]
    config: LLMConfig,
    messages: list[ContextBlock],
    request: Mapping[str, Any],
) -> tuple[bytes, ...]:
    """第三方 provider 的通用逻辑编码；只保留模型可见字段与原有顺序。"""
    request_args = {**config.request_args, **request}
    tools = request_args.pop('tools', [])
    system: ContextBlock | None = None
    history = messages
    if messages and messages[0].role == 'system':
        system = messages[0]
        history = messages[1:]
    return (
        _stable_json_bytes({'provider': config.provider, 'model': config.model}),
        _stable_json_bytes(request_args),
        _stable_json_bytes(None if system is None else _provider_visible_message(system)),
        _stable_json_bytes(tools),
        *(_stable_json_bytes(_provider_visible_message(block)) for block in history),
    )
