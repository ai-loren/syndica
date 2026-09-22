"""OpenAI 兼容 HTTP provider。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from typing import Any, cast

import httpx

from syndica.kernel.context import ContextBlock, ToolCall
from syndica.kernel.errors import ConfigError, LLMResponseError
from syndica.kernel.llm import LLMConfig

__all__ = ['OpenAICompatible']

_PROVIDER = 'openai-compatible'
_MESSAGE_KEYS: tuple[str, ...] = ('role', 'content', 'tool_calls', 'tool_call_id')
_RESERVED_REQUEST_KEYS: frozenset[str] = frozenset({'messages', 'model'})


def _config_error(expected: str, received: str, next_action: str) -> ConfigError:
    return ConfigError(expected=expected, received=received, next_action=next_action)


def _positive_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _config_error(
            'LLMConfig.timeout 是有限正数秒数',
            f'收到 timeout={value!r}，类型是 {type(value).__name__}',
            '传入大于 0 的 int 或 float，不要传布尔值',
        )
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise _config_error(
            'LLMConfig.timeout 是有限正数秒数',
            f'收到 timeout={value!r}',
            '传入大于 0 的有限秒数；0、负数、inf 与 nan 都不合法',
        )
    return seconds


def _headers(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise _config_error(
            'client_args.headers 是 str 到 str 的映射',
            f'收到 headers={value!r}，类型是 {type(value).__name__}',
            '传入 HTTP header 的字符串映射，或删掉 headers 使用空映射',
        )
    headers: dict[str, str] = {}
    for key, item in cast(Mapping[object, object], value).items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise _config_error(
                'client_args.headers 的键和值都是 str',
                f'收到键 {key!r}（{type(key).__name__}）和值 {item!r}（{type(item).__name__}）',
                '把 header 名和值都改成字符串',
            )
        headers[key] = item
    return headers


def _message(block: ContextBlock) -> dict[str, Any]:
    values: dict[str, Any] = {
        'role': block.role,
        'content': block.content,
        'tool_calls': [
            {
                'id': call.id,
                'type': 'function',
                'function': {'name': call.name, 'arguments': call.arguments},
            }
            for call in block.tool_calls
        ],
        'tool_call_id': block.tool_call_id,
    }
    return {key: values[key] for key in _MESSAGE_KEYS if values[key]}


def _clean_schema(value: object, path: str = 'schema') -> object:
    if isinstance(value, Mapping):
        cleaned: dict[str, object] = {}
        for raw_key, item in cast(Mapping[object, object], value).items():
            if not isinstance(raw_key, str):
                raise _config_error(
                    '工具 schema 的每个键都是 str',
                    f'{path} 含非字符串键 {raw_key!r}',
                    '把该键改成字符串后再交给这个客户端',
                )
            if raw_key in {'title', '$defs'}:
                continue
            if raw_key == '$ref':
                raise _config_error(
                    '工具 schema 不含 $ref',
                    f'{path} 含 $ref={item!r}',
                    '展开 $ref 后再交给这个客户端',
                )
            cleaned[raw_key] = _clean_schema(item, f'{path}.{raw_key}')
        return cleaned
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        items = cast(Sequence[object], value)
        return [_clean_schema(item, f'{path}[{index}]') for index, item in enumerate(items)]
    return value


def _tool_payloads(value: object) -> list[dict[str, object]]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise _config_error(
            'tools 是工具 schema 的序列',
            f'收到 {type(value).__name__} 类型的 {value!r}',
            '传入 BaseAgent 组装的工具 schema 列表',
        )
    payloads: list[dict[str, object]] = []
    for index, schema in enumerate(cast(Sequence[object], value)):
        cleaned = _clean_schema(schema, f'tools[{index}]')
        if not isinstance(cleaned, dict):
            raise _config_error(
                'tools 的每一项是 JSON 对象',
                f'tools[{index}] 清洗后是 {type(cleaned).__name__}',
                '传入 Tool.schema 生成的对象，不要传工具名字符串',
            )
        payloads.append({'type': 'function', 'function': cleaned})
    return payloads


def _response_error(received: str, next_action: str) -> LLMResponseError:
    return LLMResponseError(
        expected='OpenAI 兼容端点返回一条可解析的 assistant 消息',
        received=received,
        next_action=next_action,
    )


def _tool_calls(value: object) -> list[ToolCall]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _response_error(
            f'message.tool_calls 是 {type(value).__name__}，不是列表',
            '检查端点是否实现 OpenAI chat/completions 响应格式',
        )
    calls: list[ToolCall] = []
    for index, raw in enumerate(cast(list[object], value)):
        try:
            item = cast(Mapping[str, object], raw)
            function = cast(Mapping[str, object], item['function'])
            call_id = item['id']
            name = function['name']
            arguments = function['arguments']
        except (KeyError, TypeError) as exc:
            raise _response_error(
                f'tool_calls[{index}] 缺少 id / function.name / function.arguments',
                '检查端点的工具调用响应，并保留 OpenAI 的嵌套 function 结构',
            ) from exc
        if not all(isinstance(part, str) for part in (call_id, name, arguments)):
            raise _response_error(
                f'tool_calls[{index}] 的 id、name 或 arguments 不是字符串',
                '让端点把工具调用 id、函数名和 JSON arguments 都编码为字符串',
            )
        calls.append(
            ToolCall(
                id=cast(str, call_id),
                name=cast(str, name),
                arguments=cast(str, arguments),
            )
        )
    return calls


def _assistant(response: httpx.Response) -> ContextBlock:
    try:
        payload = cast(Mapping[str, object], response.json())
        choices = cast(Sequence[object], payload['choices'])
        first = cast(Mapping[str, object], choices[0])
        message = cast(Mapping[str, object], first['message'])
        role = message['role']
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise _response_error(
            '响应缺少 choices[0].message.role',
            '检查端点响应体；至少返回一个 choice，并在 message 中给出 role',
        ) from exc
    if role != 'assistant':
        raise _response_error(
            f'choices[0].message.role={role!r}',
            "让端点把模型回复的 role 设为 'assistant'",
        )
    content = message.get('content')
    if content is not None and not isinstance(content, str | list):
        raise _response_error(
            f'choices[0].message.content 是 {type(content).__name__}',
            '让端点返回字符串、内容块列表或 null',
        )
    return ContextBlock(
        role='assistant',
        content=cast(str | list[Any] | None, content),
        tool_calls=_tool_calls(message.get('tool_calls')),
    )


class OpenAICompatible:
    """经 OpenAI chat/completions 协议调用一个模型端点。"""

    def __init__(self, config: LLMConfig, *, client: httpx.AsyncClient | None = None) -> None:
        if config.provider != _PROVIDER:
            raise _config_error(
                f'provider 是 {_PROVIDER!r}',
                f'收到 provider={config.provider!r}',
                f'把 LLMConfig.provider 改成 {_PROVIDER!r}，或改用对应 provider 的 backend',
            )
        base_url = config.client_args.get('base_url')
        if not isinstance(base_url, str) or not base_url.strip():
            raise _config_error(
                "client_args 含非空字符串键 'base_url'",
                f'收到 base_url={base_url!r}',
                "在 LLMConfig.client_args 中设置 base_url，例如 'https://host/v1'",
            )
        timeout = _positive_timeout(config.timeout)
        client_args = dict(config.client_args)
        client_args.pop('base_url', None)
        api_key = client_args.pop('api_key', None)
        if api_key is not None and not isinstance(api_key, str):
            raise _config_error(
                'client_args.api_key 是 str 或省略',
                f'收到 api_key 类型 {type(api_key).__name__}',
                '传入字符串 API key，或删掉 api_key 由自定义 client 负责认证',
            )
        headers = _headers(client_args.pop('headers', {}))
        if isinstance(api_key, str) and api_key:
            headers.setdefault('Authorization', f'Bearer {api_key}')
        self._config = config
        self._base_url = base_url.rstrip('/')
        self._timeout = timeout
        self._client_args = client_args
        self._headers = headers
        self._client = client

    @property
    def config(self) -> LLMConfig:
        return self._config

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        request_args = {**self._config.request_args, **kwargs}
        reserved = sorted(_RESERVED_REQUEST_KEYS & set(request_args))
        if reserved:
            raise _config_error(
                '单次请求参数不覆盖 model 或 messages',
                f'收到保留键 {reserved}',
                '用 LLMConfig.derive(model=...) 改模型；消息只通过 create(messages) 传入',
            )
        raw_tools = request_args.pop('tools', [])
        body: dict[str, object] = {
            'model': self._config.model,
            'messages': [_message(block) for block in messages],
        }
        tools = _tool_payloads(raw_tools)
        if tools:
            body['tools'] = tools
        body.update(request_args)
        try:
            response = await self._post_with_deadline(body)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise _response_error(
                f'请求在 {self._config.timeout:g} 秒内没有完成：{type(exc).__name__}',
                '重试；若持续超时，增大 LLMConfig.timeout 或检查端点健康状态',
            ) from exc
        except httpx.HTTPError as exc:
            raise _response_error(
                f'HTTP 请求失败：{type(exc).__name__}: {exc}',
                '检查 base_url、认证信息和端点状态后重试',
            ) from exc
        return _assistant(response)

    async def _post_with_deadline(self, body: dict[str, object]) -> httpx.Response:
        request = asyncio.create_task(self._post(body))
        try:
            done, _ = await asyncio.wait({request}, timeout=self._timeout)
            if not done:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
                raise _response_error(
                    f'请求总耗时超过 {self._timeout:g} 秒',
                    '重试；若持续超时，增大 LLMConfig.timeout 或检查端点健康状态',
                )
            return request.result()
        except BaseException:
            if not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
            raise

    async def _post(self, body: dict[str, object]) -> httpx.Response:
        url = f'{self._base_url}/chat/completions'
        if self._client is not None:
            return await self._client.post(url, json=body, timeout=self._timeout)
        try:
            client = httpx.AsyncClient(headers=self._headers, **self._client_args)
        except TypeError as exc:
            raise _config_error(
                'client_args 只含 httpx.AsyncClient 支持的构造参数',
                f'构造客户端失败：{exc}',
                '删除不受支持的 client_args 键，或注入已经配置好的 httpx.AsyncClient',
            ) from exc
        async with client:
            return await client.post(url, json=body, timeout=self._timeout)
