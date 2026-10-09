"""doctor 的 A2A 自检：取远端的 agent card，报它声明的协议版本与技能名。

卡片是公开的，而 Bearer 只钉给 JSON-RPC 那一个地址：取卡片不带它。卡片地址与认版本的规则与
`A2AAgentModel` 取卡片时相同（RPC 地址的主机上的 `/.well-known/agent-card.json`；
`supportedInterfaces` 里有 1.0 或 0.3），doctor 说 ok 的卡片 run 才认。总时长有上限（httpx 的
超时只管每一次读，慢慢滴字节的对端能拖过它），正文也有上限。卡片上的文字不可信：逐项截断后交回，
转义与脱敏由调用方做。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit

import httpx

from syndica.kernel import ConfigError

__all__ = ['A2aCard', 'probe_a2a']

_VERSION = '1.0'
_ACCEPTED = frozenset({'1.0', '0.3'})
_BODY_CAP = 1_048_576
_TEXT_CAP = 64
_SKILL_CAP = 32


@dataclass(frozen=True)
class A2aCard:
    """卡片声明的版本（按卡片顺序、去重）与技能名；`more_skills` 是超出上限没列出的个数。"""

    versions: tuple[str, ...]
    skills: tuple[str, ...]
    more_skills: int = 0


async def probe_a2a(name: str, url: str, *, timeout: float) -> A2aCard:  # noqa: ASYNC109
    card_url = _card_url(url)
    try:
        async with asyncio.timeout(timeout):
            status, body = await _fetch(card_url, timeout)
    except (TimeoutError, httpx.TimeoutException):
        raise ConfigError(
            expected=f'a2a:{name} 在 {timeout:g} 秒内交出 agent card',
            received=f'{card_url} 在 {timeout:g} 秒内没有交完',
            next_action=f'确认 {url} 上的 A2A Agent 在跑、没有卡住；doctor 不重试',
        ) from None
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise ConfigError(
            expected=f'a2a:{name} 的 agent card 能从 {card_url} 取到',
            received=f'连不上：{type(exc).__name__}: {str(exc)[:200]}',
            next_action=(
                f'确认远端 A2A Agent 在跑、interop.a2a.agents 里 {name} 的 url 写对了'
                '（取卡片不带 Bearer，卡片是公开的）'
            ),
        ) from None
    card = _card(name, card_url, status, body)
    versions = _versions(card)
    if not _ACCEPTED & set(versions):
        raise ConfigError(
            expected=f'a2a:{name} 的卡片在 supportedInterfaces 里声明协议 1.0 或 0.3',
            received=f'声明了 {list(versions)!r}',
            next_action='换一个 Syndica 认得的 A2A Agent，或让对端在卡片里写上 1.0',
        )
    skills = _skills(card)
    return A2aCard(
        versions=versions,
        skills=tuple(skills[:_SKILL_CAP]),
        more_skills=max(0, len(skills) - _SKILL_CAP),
    )


async def _fetch(card_url: str, timeout: float) -> tuple[int, bytes]:  # noqa: ASYNC109
    async with (
        httpx.AsyncClient() as client,
        client.stream(
            'GET', card_url, headers={'A2A-Version': _VERSION}, timeout=timeout
        ) as response,
    ):
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > _BODY_CAP:
                raise ConfigError(
                    expected=f'{card_url} 的 agent card 不超过 {_BODY_CAP} 字节',
                    received=f'读到 {len(body)} 字节还没完',
                    next_action='确认这个地址交出的是 A2A 的 agent card，而不是别的大文件',
                )
        return response.status_code, bytes(body)


def _card(name: str, card_url: str, status: int, body: bytes) -> dict[str, object]:
    if status >= 400:
        raise ConfigError(
            expected=f'{card_url} 返回 2xx 与 agent card',
            received=f'HTTP {status}',
            next_action=(
                f'确认 interop.a2a.agents 里 {name} 的 url 指向一个 A2A Agent；卡片是公开的，'
                '401 / 403 说明对端把卡片也锁上了'
            ),
        )
    try:
        parsed: object = json.loads(body)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        raise ConfigError(
            expected=f'{card_url} 交出的 agent card 是 JSON 对象',
            received=f'HTTP {status}，正文不是 JSON 对象',
            next_action=f'确认 interop.a2a.agents 里 {name} 的 url 是 A2A Agent，而不是网页',
        )
    return cast(dict[str, object], parsed)


def _versions(card: dict[str, object]) -> tuple[str, ...]:
    found: list[str] = []
    interfaces = card.get('supportedInterfaces')
    if not isinstance(interfaces, list):
        return ()
    for item in cast(list[object], interfaces):
        if not isinstance(item, dict):
            continue
        version = cast(dict[str, object], item).get('protocolVersion')
        if isinstance(version, str) and version[:_TEXT_CAP] not in found:
            found.append(version[:_TEXT_CAP])
    return tuple(found)


def _skills(card: dict[str, object]) -> list[str]:
    found: list[str] = []
    skills = card.get('skills')
    if not isinstance(skills, list):
        return found
    for item in cast(list[object], skills):
        if not isinstance(item, dict):
            continue
        row = cast(dict[str, object], item)
        label = row.get('name') or row.get('id')
        if isinstance(label, str) and label:
            found.append(label[:_TEXT_CAP])
    return found


def _card_url(rpc: str) -> str:
    parts = urlsplit(rpc if rpc.endswith('/') else rpc + '/')
    return f'{parts.scheme}://{parts.netloc}/.well-known/agent-card.json'
