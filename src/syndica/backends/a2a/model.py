"""把一个远端 A2A Agent 当成 Syndica 的模型。

卡片和 JSON-RPC 都走调用方交来的 HTTP。请求带 `A2A-Version: 1.0`。每个 Syndica Agent
一个 `contextId`。对方停在 INPUT_REQUIRED 时，问题作为不可信回答交回，下一次调用的新用户
内容发回同一个任务。回答一律不可信。
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import httpx

from syndica.kernel import ConfigError, ContextBlock, LLMConfig, TrustLevel
from syndica.kernel.call_limit import BudgetDeadline, model_call_limit
from syndica.kernel.driving import driving_agent
from syndica.kernel.redaction import Redactor, RuleRedactor

__all__ = ['A2AAgentModel', 'A2ATransport']

_VERSION = '1.0'
_INPUT = 'TASK_STATE_INPUT_REQUIRED'
_DONE = 'TASK_STATE_COMPLETED'
_FAILED = 'TASK_STATE_FAILED'
_CANCELED = 'TASK_STATE_CANCELED'


class A2ATransport(Protocol):
    async def get_json(
        self, url: str, headers: Mapping[str, str], deadline: float | None
    ) -> Any: ...

    async def post_json(
        self,
        url: str,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
        deadline: float | None,
    ) -> Any: ...

    async def aclose(self) -> None: ...


class HttpxA2A:
    """httpx 传输。超时是这次调用的墙钟，不是客户端一辈子的上限。"""

    def __init__(self, redactor: Redactor | None = None) -> None:
        self._client = httpx.AsyncClient()
        self._redactor = redactor if redactor is not None else RuleRedactor()

    async def get_json(self, url: str, headers: Mapping[str, str], deadline: float | None) -> Any:
        response = await self._client.get(url, headers=dict(headers), timeout=deadline)
        return _json_body(response)

    async def post_json(
        self,
        url: str,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
        deadline: float | None,
    ) -> Any:
        sent = dict(headers)
        sent['content-type'] = 'application/json'
        response = await self._client.post(
            url,
            content=self._redactor.redact(json.dumps(dict(body), ensure_ascii=False)).encode(),
            headers=sent,
            timeout=deadline,
        )
        return _json_body(response)

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass
class _Link:
    context_id: str | None = None
    task_id: str | None = None
    cursor: int = 0
    card_ok: bool = False


class A2AAgentModel:
    """一个远端 URL。`transport` 缺省是 httpx。"""

    def __init__(
        self,
        name: str,
        url: str,
        *,
        bearer: str | None = None,
        redactor: Redactor | None = None,
        transport: A2ATransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not url.startswith('http://') and not url.startswith('https://'):
            raise ConfigError(
                expected=f'a2a:{name} 的 url 以 http:// 或 https:// 开头',
                received=url,
                next_action='写成远端 agent card 上的 JSON-RPC 地址，例如 http://127.0.0.1:9/a2a/',
            )
        self._name = name
        self._url = url if url.endswith('/') else url + '/'
        self._bearer = bearer
        self._redactor = redactor
        self._transport = transport if transport is not None else HttpxA2A(redactor)
        self._owns_transport = transport is None
        self._config = LLMConfig(provider='a2a', model=name, timeout=timeout)
        self._links: dict[str, _Link] = {}
        self._next_id = 1

    @property
    def config(self) -> LLMConfig:
        return self._config

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        del kwargs
        agent_id = driving_agent() or ''
        link = self._links.setdefault(agent_id, _Link())
        text = _fresh(messages, link.cursor)
        limit = model_call_limit()
        if limit is not None and limit <= 0:
            raise BudgetDeadline()
        # 没有预算上限时用 config.timeout：交给 httpx 的 None 是永不超时，接了连接却不答的对端会把
        # 这次调用挂死。
        deadline = limit if limit is not None else self._config.timeout
        try:
            await self._card(link, deadline)
            body = await self._post(_send(self._take_id(), link, text), deadline)
        except httpx.TimeoutException:
            await self._cancel(link, self._config.timeout)
            if limit is not None:
                raise BudgetDeadline() from None
            raise ConfigError(
                expected=f'{self._name} 在 {deadline:g} 秒内答复',
                received=f'{self._url} 在 {deadline:g} 秒内没有答复',
                next_action=(
                    f'确认 {self._url} 上的 A2A Agent 在跑、没有卡住；不会自动重试，也不会自动重连'
                ),
            ) from None
        except ConfigError:
            raise
        link.cursor = len(messages)
        task = _task(body)
        state = str(_dig(task, 'status', 'state') or '')
        answer = self._redact(_task_text(task))
        link.context_id = str(task.get('contextId') or link.context_id or '')
        if state == _INPUT:
            link.task_id = str(task.get('id') or '')
            return ContextBlock(role='assistant', content=answer, trust=TrustLevel.UNTRUSTED)
        link.task_id = None
        if state == _DONE:
            return ContextBlock(role='assistant', content=answer, trust=TrustLevel.UNTRUSTED)
        if state == _CANCELED:
            raise ConfigError(
                expected=f'{self._name} 把这次消息答完',
                received='远端任务已取消',
                next_action='重新发一条消息；取消不会自动重试',
            )
        raise ConfigError(
            expected=f'{self._name} 的任务完成',
            received=answer or f'状态 {state or "未知"}',
            next_action='看远端任务；失败不会自动重试，也不会自动重连',
        )

    async def aclose(self) -> None:
        if self._owns_transport:
            await self._transport.aclose()

    async def _card(self, link: _Link, deadline: float | None) -> None:
        if link.card_ok:
            return
        # 卡片是公开的，而 Bearer 只钉给 JSON-RPC 这一个地址：取卡片不带它。
        card = _as_dict(
            await self._transport.get_json(
                _card_url(self._url), {'A2A-Version': _VERSION}, deadline
            )
        )
        if card is None:
            raise ConfigError(
                expected='agent card 是 JSON 对象',
                received='响应不是 JSON 对象',
                next_action=f'确认 {self._url} 对应的 /.well-known/agent-card.json 可访问',
            )
        versions: set[str] = set()
        interfaces = card.get('supportedInterfaces')
        if isinstance(interfaces, list):
            for item in cast(list[object], interfaces):
                row = _as_dict(item)
                if row is not None:
                    versions.add(str(row.get('protocolVersion')))
        if _VERSION not in versions and '0.3' not in versions:
            raise ConfigError(
                expected='远端卡片声明协议 1.0 或 0.3',
                received=f'声明了 {sorted(versions)!r}',
                next_action='换一个 Syndica 认得的 A2A Agent，或让对端在卡片里写上 1.0',
            )
        link.card_ok = True

    async def _post(self, body: Mapping[str, Any], deadline: float | None) -> dict[str, Any]:
        raw = await self._transport.post_json(self._url, body, self._headers(), deadline)
        if not isinstance(raw, dict):
            raise ConfigError(
                expected='JSON-RPC 响应是对象',
                received=f'收到 {type(raw).__name__}',
                next_action=f'确认 {self._url} 是 A2A JSON-RPC，而不是网页',
            )
        parsed = cast(dict[str, Any], raw)
        error = _as_dict(parsed.get('error'))
        if error is not None:
            data = _as_dict(error.get('data')) or {}
            expected = data.get('expected')
            if isinstance(expected, str):
                raise ConfigError(
                    expected=self._remote(expected),
                    received=self._remote(data.get('received')),
                    next_action=self._remote(data.get('next_action')),
                )
            raise ConfigError(
                expected=f'{self._name} 接受这次 JSON-RPC',
                received=self._remote(error.get('message') or error.get('code')),
                next_action='检查 URL、A2A-Version 和 Bearer；不会自动重连',
            )
        return parsed

    async def _cancel(self, link: _Link, deadline: float | None) -> None:
        if not link.task_id:
            return
        body = {
            'jsonrpc': '2.0',
            'id': self._take_id(),
            'method': 'CancelTask',
            'params': {'id': link.task_id},
        }
        try:
            await self._transport.post_json(self._url, body, self._headers(), deadline)
        except Exception:
            return

    def _headers(self) -> dict[str, str]:
        headers = {'A2A-Version': _VERSION}
        if self._bearer:
            headers['Authorization'] = f'Bearer {self._bearer}'
        return headers

    def _take_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _redact(self, text: str) -> str:
        if self._redactor is None:
            return text
        return self._redactor.redact(text)

    def _remote(self, value: object) -> str:
        """对端写进错误里的文字：先脱敏再截断，截断可能切在一个密钥中间。"""
        return self._redact(str(value))[:300]


def _send(request_id: int, link: _Link, text: str) -> dict[str, Any]:
    message: dict[str, Any] = {
        'messageId': secrets.token_hex(8),
        'role': 'ROLE_USER',
        'parts': [{'text': text}],
    }
    if link.context_id:
        message['contextId'] = link.context_id
    if link.task_id:
        message['taskId'] = link.task_id
    return {
        'jsonrpc': '2.0',
        'id': request_id,
        'method': 'SendMessage',
        'params': {'message': message},
    }


def _card_url(rpc: str) -> str:
    parts = urlsplit(rpc)
    return f'{parts.scheme}://{parts.netloc}/.well-known/agent-card.json'


def _json_body(response: httpx.Response) -> Any:
    if response.status_code >= 400:
        raise ConfigError(
            expected='远端 A2A 返回 2xx',
            received=f'HTTP {response.status_code}',
            next_action='确认 Bearer 和 Host；401 是令牌，421 是 Host 不是本机',
        )
    body = response.json()
    return body


def _task(body: Mapping[str, Any]) -> dict[str, object]:
    result = _as_dict(body.get('result'))
    if result is None:
        raise ConfigError(
            expected='JSON-RPC result 里有 task',
            received='没有 result 对象',
            next_action='确认对端说的是 A2A 1.0 的 SendMessage',
        )
    task = _as_dict(result.get('task')) or result
    if 'status' not in task:
        raise ConfigError(
            expected='SendMessage 的结果是带 status 的 task',
            received=f'顶层键 {sorted(str(key) for key in result)!r}',
            next_action='确认对端把任务放在 result.task',
        )
    return task


def _task_text(task: Mapping[str, object]) -> str:
    chunks: list[str] = []
    status = _as_dict(task.get('status'))
    if status is not None:
        chunks.extend(_parts(status.get('message')))
    artifacts = task.get('artifacts')
    if isinstance(artifacts, list):
        for artifact in cast(list[object], artifacts):
            chunks.extend(_parts(artifact))
    unique: list[str] = []
    for chunk in chunks:
        if chunk and chunk not in unique:
            unique.append(chunk)
    return '\n'.join(unique)


def _parts(message: object) -> list[str]:
    row = _as_dict(message)
    if row is None:
        return []
    parts = row.get('parts')
    if not isinstance(parts, list):
        return []
    found: list[str] = []
    for part in cast(list[object], parts):
        item = _as_dict(part)
        if item is None:
            continue
        text = item.get('text')
        if isinstance(text, str) and text:
            found.append(text)
    return found


def _as_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def _dig(mapping: Mapping[str, object], *keys: str) -> object:
    current: object = mapping
    for key in keys:
        row = _as_dict(current)
        if row is None:
            return None
        current = row.get(key)
    return current


def _fresh(messages: Sequence[ContextBlock], cursor: int) -> str:
    parts: list[str] = []
    for block in messages[cursor:]:
        if block.role not in {'system', 'user'}:
            continue
        if isinstance(block.content, str) and block.content:
            parts.append(block.content)
    return '\n'.join(parts)
