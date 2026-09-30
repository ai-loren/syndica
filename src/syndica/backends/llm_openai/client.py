"""OpenAI 兼容 HTTP provider。"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Mapping, Sequence
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any, cast

import httpx

from syndica.kernel.context import ContextBlock, ToolCall
from syndica.kernel.errors import ConfigError, LLMResponseError
from syndica.kernel.llm import LLMConfig
from syndica.kernel.llm.base import _stable_json_bytes  # pyright: ignore[reportPrivateUsage]

__all__ = ['OpenAICompatible', 'ProviderHTTPError', 'recoverable', 'retry_after']

_PROVIDER = 'openai-compatible'
_MESSAGE_KEYS: tuple[str, ...] = ('role', 'content', 'tool_calls', 'tool_call_id')
_RESERVED_REQUEST_KEYS: frozenset[str] = frozenset({'messages', 'model'})
# 本 backend 在 assistant block 的 `extra` 里只写这一个键：端点要求原样回传的 `extra_content`
# （Gemini 的 thought_signature 就在这里），连同产生它的端点身份。
_EXTRA_KEY = 'openai_compatible'
_EXPECTED_REPLY = 'OpenAI 兼容端点返回一条可解析的 assistant 消息'
# 重试提示只在这两种状态上表示「多久之后再试」（RFC 6585 / RFC 9110）；别的状态上出现也不作数。
_HINTED_STATUSES: frozenset[int] = frozenset({429, 503})
# 除 5xx 外，等一会儿可能自己好的状态：请求超时、Too Early、限流。
_TRANSIENT_CLIENT_STATUSES: frozenset[int] = frozenset({408, 425, 429})


class ProviderHTTPError(LLMResponseError):
    """端点答了一个 HTTP 错误状态。

    `status_code` 与 `retry_after`（秒；端点没给就是 None）留给重试与轮换层读，它们不必解析文本。
    两个字段都有默认值：基类 `__reduce__` 按三件套重建、再用 `__dict__` 补回这两个字段。
    """

    def __init__(
        self,
        *,
        expected: str,
        received: str,
        next_action: str,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(expected=expected, received=received, next_action=next_action)
        self.status_code = status_code
        self.retry_after = retry_after


def retry_after(error: BaseException) -> float | None:
    """端点要求的最早重试间隔（秒）。不是本 backend 的 HTTP 错误、或端点没给，都是 None。"""
    return error.retry_after if isinstance(error, ProviderHTTPError) else None


def recoverable(error: BaseException) -> bool:
    """这次失败等一会儿有没有可能自己好。

    5xx、请求超时与限流会；别的 HTTP 状态（认证失败、请求不合法、模型不存在）不会，等多久都
    得到同一个答案。没有 HTTP 状态的失败（超时、连不上、响应无法解析）按会好算。
    """
    if isinstance(error, ProviderHTTPError) and error.status_code is not None:
        status = error.status_code
        return status >= 500 or status in _TRANSIENT_CLIENT_STATUSES
    return True


def _header_seconds(value: str, now: float) -> float | None:
    """`Retry-After` 的两种写法：秒数，或 HTTP 日期。"""
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, moment.timestamp() - now)


def _reset_seconds(value: str, now: float) -> float | None:
    """`X-RateLimit-Reset`：OpenRouter 给的是毫秒时间戳；也接受秒时间戳与相对秒数。"""
    try:
        number = float(value)
    except ValueError:
        return None
    if number > 1e12:
        return max(0.0, number / 1000 - now)
    if number > 1e9:
        return max(0.0, number - now)
    return max(0.0, number)


def _retry_info_seconds(body: object) -> float | None:
    """Google 的 `google.rpc.RetryInfo.retryDelay`（如 `"31s"`）；兼容端点把错误包在列表里。"""
    for item in cast(list[object], body) if isinstance(body, list) else [body]:
        error = cast(Mapping[str, object], item).get('error') if isinstance(item, Mapping) else None
        details = (
            cast(Mapping[str, object], error).get('details') if isinstance(error, Mapping) else None
        )
        for detail in cast(list[object], details) if isinstance(details, list) else []:
            if not isinstance(detail, Mapping):
                continue
            info = cast(Mapping[str, object], detail)
            delay = info.get('retryDelay')
            if str(info.get('@type', '')).endswith('google.rpc.RetryInfo') and isinstance(
                delay, str
            ):
                try:
                    return max(0.0, float(delay.removesuffix('s')))
                except ValueError:
                    return None
    return None


def _retry_after_seconds(response: httpx.Response, now: float) -> float | None:
    """这个错误响应要求至少等多久再试；几种提示并存时取最晚的那个。"""
    if response.status_code not in _HINTED_STATUSES:
        return None
    hints: list[float | None] = []
    header = response.headers.get('retry-after')
    if header is not None:
        hints.append(_header_seconds(header.strip(), now))
    reset = response.headers.get('x-ratelimit-reset')
    if reset is not None:
        hints.append(_reset_seconds(reset.strip(), now))
    try:
        hints.append(_retry_info_seconds(response.json()))
    except ValueError:
        pass
    known = [hint for hint in hints if hint is not None]
    return max(known) if known else None


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


def _is_content_parts(items: list[Any]) -> bool:
    return bool(items) and all(
        isinstance(item, Mapping)
        and isinstance(cast(Mapping[object, object], item).get('type'), str)
        for item in items
    )


def _tool_content(content: str | list[Any] | None) -> str | list[Any]:
    """tool 消息的 content 只能是字符串或内容块列表，缺键或别的形状都会被端点整条拒掉（HTTP 400）。

    `ToolResult.output` 允许任意列表，所以不是内容块的列表在这里编码成 JSON 文本；
    `ensure_ascii=False` 让模型读到原文而不是 `\\u` 转义。
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ''
    if _is_content_parts(content):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _own_extras(block: ContextBlock, endpoint: str) -> Mapping[str, Any] | None:
    """这块若由 `endpoint` 本身答出，交回它当时附带的 `extra_content`；否则 None。"""
    state = block.extra.get(_EXTRA_KEY)
    if isinstance(state, Mapping) and cast(Mapping[str, Any], state).get('endpoint') == endpoint:
        return cast(Mapping[str, Any], state)
    return None


def _call_payload(call: ToolCall, extra_content: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'id': call.id,
        'type': 'function',
        'function': {'name': call.name, 'arguments': call.arguments},
    }
    if extra_content is not None:
        payload['extra_content'] = extra_content
    return payload


def _message(
    block: ContextBlock,
    *,
    endpoint: str,
    foreign_tool_call_extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """一块历史的线上形态。

    `extra_content` 只回给产生它的那个端点（同一 URL 换了模型也不算）：签名这类字段是签发它的
    模型的私有状态，换一个模型不保证认，发给别家还会把它漏过去。别家答出的工具调用改挂
    `foreign_tool_call_extra`——Gemini 3.x 拒收没有 thought_signature 的函数调用，跨平台轮换时
    历史里的调用可能来自另一家模型。
    """
    is_tool = block.role == 'tool'
    own = _own_extras(block, endpoint) if block.role == 'assistant' else None
    own_calls: Mapping[str, Any] = (
        cast(Mapping[str, Any], own.get('tool_calls', {})) if own is not None else {}
    )
    values: dict[str, Any] = {
        'role': block.role,
        'content': _tool_content(block.content) if is_tool else block.content,
        'tool_calls': [
            _call_payload(
                call, own_calls.get(call.id) if own is not None else foreign_tool_call_extra
            )
            for call in block.tool_calls
        ],
        'tool_call_id': block.tool_call_id,
    }
    message = {
        key: values[key] for key in _MESSAGE_KEYS if values[key] or (is_tool and key == 'content')
    }
    if own is not None and 'message' in own:
        message['extra_content'] = own['message']
    return message


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
        expected=_EXPECTED_REPLY,
        received=received,
        next_action=next_action,
    )


def _response_excerpt(response: httpx.Response) -> str:
    text = response.text.strip()
    if not text:
        return '<empty>'
    if len(text) > 1000:
        return f'{text[:1000]}...<truncated>'
    return text


def _http_error_message(error: httpx.HTTPError) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        response = error.response
        body = _response_excerpt(response)
        return f'HTTP {response.status_code} {response.reason_phrase}: {body}'
    return f'{type(error).__name__}: {error}'


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


def _provider_extras(message: Mapping[str, object], endpoint: str) -> dict[str, Any]:
    """端点随回复附带、要求下一轮原样回传的 `extra_content`，按消息与各工具调用 id 收好。"""
    state: dict[str, Any] = {}
    extra = message.get('extra_content')
    if isinstance(extra, Mapping):
        state['message'] = dict(cast(Mapping[str, Any], extra))
    raw_calls = message.get('tool_calls')
    per_call: dict[str, Any] = {}
    for raw in cast(list[object], raw_calls) if isinstance(raw_calls, list) else []:
        if not isinstance(raw, Mapping):
            continue
        item = cast(Mapping[str, object], raw)
        call_id, call_extra = item.get('id'), item.get('extra_content')
        if isinstance(call_id, str) and isinstance(call_extra, Mapping):
            per_call[call_id] = dict(cast(Mapping[str, Any], call_extra))
    if per_call:
        state['tool_calls'] = per_call
    return {_EXTRA_KEY: {'endpoint': endpoint, **state}} if state else {}


def _assistant(response: httpx.Response, endpoint: str) -> ContextBlock:
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
        extra=_provider_extras(message, endpoint),
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
        foreign = client_args.pop('foreign_tool_call_extra', None)
        if foreign is not None and not isinstance(foreign, Mapping):
            raise _config_error(
                'client_args.foreign_tool_call_extra 是 JSON 对象（Mapping）或省略',
                f'收到 {type(foreign).__name__} 类型的 {foreign!r}',
                '传入端点要求挂在别家工具调用上的 extra_content，'
                '例如 Gemini 的占位 thought_signature；端点不需要就删掉这个键',
            )
        self._foreign_tool_call_extra: dict[str, Any] | None = (
            None if foreign is None else dict(cast(Mapping[str, Any], foreign))
        )
        self._endpoint = f'{base_url.rstrip("/")}#{config.model}'
        self._config = config
        self._base_url = base_url.rstrip('/')
        self._timeout = timeout
        self._client_args = client_args
        self._headers = headers
        self._client = client

    @property
    def config(self) -> LLMConfig:
        return self._config

    def _request_parts(
        self,
        messages: list[ContextBlock],
        kwargs: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, object]]]:
        request_args = {**self._config.request_args, **kwargs}
        reserved = sorted(_RESERVED_REQUEST_KEYS & set(request_args))
        if reserved:
            raise _config_error(
                '单次请求参数不覆盖 model 或 messages',
                f'收到保留键 {reserved}',
                '用 LLMConfig.derive(model=...) 改模型；消息只通过 create(messages) 传入',
            )
        raw_tools = request_args.pop('tools', [])
        encoded_messages = [
            _message(
                block,
                endpoint=self._endpoint,
                foreign_tool_call_extra=self._foreign_tool_call_extra,
            )
            for block in messages
        ]
        tools = _tool_payloads(raw_tools)
        return request_args, encoded_messages, tools

    def cache_prefix_units(
        self,
        messages: list[ContextBlock],
        **kwargs: Any,
    ) -> tuple[bytes, ...]:
        request_args, encoded_messages, tools = self._request_parts(messages, kwargs)
        system: dict[str, Any] | None = None
        history = encoded_messages
        if messages and messages[0].role == 'system':
            system = encoded_messages[0]
            history = encoded_messages[1:]
        return (
            _stable_json_bytes({'provider': self._config.provider, 'model': self._config.model}),
            _stable_json_bytes(request_args),
            _stable_json_bytes(system),
            _stable_json_bytes(tools),
            *(_stable_json_bytes(message) for message in history),
        )

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        request_args, encoded_messages, tools = self._request_parts(messages, kwargs)
        body: dict[str, object] = {
            'model': self._config.model,
            'messages': encoded_messages,
        }
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
        except httpx.HTTPStatusError as exc:
            raise ProviderHTTPError(
                expected=_EXPECTED_REPLY,
                received=f'HTTP 请求失败：{_http_error_message(exc)}',
                next_action='检查 base_url、认证信息、模型名、请求参数和端点状态后重试',
                status_code=exc.response.status_code,
                retry_after=_retry_after_seconds(exc.response, time.time()),
            ) from exc
        except httpx.HTTPError as exc:
            raise _response_error(
                f'HTTP 请求失败：{_http_error_message(exc)}',
                '检查 base_url、认证信息、模型名、请求参数和端点状态后重试',
            ) from exc
        return _assistant(response, self._endpoint)

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
