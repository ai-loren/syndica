"""OpenAI 兼容 backend 的请求与响应契约。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from syndica.backends.llm_openai import OpenAICompatible
from syndica.kernel.context import ArtifactRef, ContextBlock, ToolCall, TrustLevel
from syndica.kernel.errors import ConfigError, LLMResponseError
from syndica.kernel.llm import CachePrefixProvider, LLMConfig


def _config(*, timeout: float = 3.5) -> LLMConfig:
    return LLMConfig(
        provider='openai-compatible',
        model='model-a',
        client_args={'base_url': 'https://llm.example.test/v1'},
        request_args={'temperature': 0},
        timeout=timeout,
    )


def _response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            'choices': [
                {
                    'message': {
                        'role': 'assistant',
                        'content': 'done',
                        'tool_calls': [],
                    }
                }
            ]
        },
    )


async def _request_for(
    messages: list[ContextBlock],
    *,
    tools: list[dict[str, Any]] | None = None,
    request_timeout: float = 3.5,
) -> httpx.Request:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        llm = OpenAICompatible(_config(timeout=request_timeout), client=client)
        await llm.create(messages, tools=tools or [])
    assert len(seen) == 1
    return seen[0]


async def test_request_drops_extra_trust_and_artifacts() -> None:
    block = ContextBlock(
        role='user',
        content='hello',
        artifacts=[
            ArtifactRef(
                id='a1',
                path='report.md',
                kind='document',
                mime='text/markdown',
                version='v1',
                producer='agent-1',
            )
        ],
        trust=TrustLevel.UNTRUSTED,
        extra={'private': 'metadata'},
    )

    request = await _request_for([block])
    body = json.loads(request.content)
    encoded = json.dumps(body)

    assert body['messages'] == [{'role': 'user', 'content': 'hello'}]
    assert 'artifacts' not in encoded
    assert 'trust' not in encoded
    assert 'private' not in encoded


async def test_message_keys_follow_role_content_tool_calls_tool_call_id() -> None:
    messages = [
        ContextBlock(role='system', content='system'),
        ContextBlock(
            role='assistant',
            content='thinking',
            tool_calls=[ToolCall(id='call-1', name='Read', arguments='{"path":"a.txt"}')],
        ),
        ContextBlock(role='tool', content='result', tool_call_id='call-1'),
    ]

    request = await _request_for(messages)
    body = json.loads(request.content)

    assert [list(message) for message in body['messages']] == [
        ['role', 'content'],
        ['role', 'content', 'tool_calls'],
        ['role', 'content', 'tool_call_id'],
    ]


def test_cache_prefix_units_reuse_the_exact_openai_message_and_tool_encoding() -> None:
    llm = OpenAICompatible(_config())
    tools = [
        {
            'name': 'Beta',
            'description': 'beta',
            'parameters': {
                'required': ['value'],
                'title': 'Removed',
                'type': 'object',
                'properties': {'value': {'title': 'Removed Too', 'type': 'string'}},
            },
        },
        {'name': 'Alpha', 'description': 'alpha', 'parameters': {'type': 'object'}},
        {'name': 'Gamma', 'description': 'gamma', 'parameters': {'type': 'object'}},
    ]
    messages = [
        ContextBlock(role='system', content='system'),
        ContextBlock(
            role='user',
            content='你好',
            trust=TrustLevel.UNTRUSTED,
            extra={'private': 'not visible'},
        ),
        ContextBlock(
            role='assistant',
            tool_calls=[ToolCall(id='c1', name='Beta', arguments='{"value":"x"}')],
        ),
    ]

    assert isinstance(llm, CachePrefixProvider)
    decoded = [
        json.loads(unit)
        for unit in llm.cache_prefix_units(
            messages,
            tools=tools,
            temperature=1,
            zeta='last',
        )
    ]

    assert decoded == [
        {'provider': 'openai-compatible', 'model': 'model-a'},
        {'temperature': 1, 'zeta': 'last'},
        {'role': 'system', 'content': 'system'},
        [
            {
                'type': 'function',
                'function': {
                    'name': 'Beta',
                    'description': 'beta',
                    'parameters': {
                        'required': ['value'],
                        'type': 'object',
                        'properties': {'value': {'type': 'string'}},
                    },
                },
            },
            {
                'type': 'function',
                'function': {
                    'name': 'Alpha',
                    'description': 'alpha',
                    'parameters': {'type': 'object'},
                },
            },
            {
                'type': 'function',
                'function': {
                    'name': 'Gamma',
                    'description': 'gamma',
                    'parameters': {'type': 'object'},
                },
            },
        ],
        {'role': 'user', 'content': '你好'},
        {
            'role': 'assistant',
            'tool_calls': [
                {
                    'id': 'c1',
                    'type': 'function',
                    'function': {'name': 'Beta', 'arguments': '{"value":"x"}'},
                }
            ],
        },
    ]
    assert [item['function']['name'] for item in decoded[3]] == ['Beta', 'Alpha', 'Gamma']
    assert list(decoded[3][0]['function']['parameters']) == ['required', 'type', 'properties']


async def test_schema_title_and_defs_are_stripped() -> None:
    schema = {
        'name': 'Lookup',
        'description': 'lookup',
        'parameters': {
            'title': 'LookupArgs',
            'type': 'object',
            'properties': {
                'query': {'title': 'Query', 'type': 'string'},
                'choice': {
                    'anyOf': [
                        {'title': 'TextChoice', 'type': 'string'},
                        {'title': 'NumberChoice', 'type': 'number'},
                    ]
                },
            },
            '$defs': {'Unused': {'title': 'Unused', 'type': 'string'}},
        },
    }

    request = await _request_for([ContextBlock(role='user', content='go')], tools=[schema])
    body = json.loads(request.content)
    encoded = json.dumps(body['tools'])

    assert 'title' not in encoded
    assert '$defs' not in encoded
    assert body['tools'][0]['type'] == 'function'
    assert body['tools'][0]['function']['name'] == 'Lookup'


async def test_schema_with_ref_is_rejected() -> None:
    schema = {
        'name': 'Lookup',
        'description': 'lookup',
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {
                    'anyOf': [
                        {'type': 'string'},
                        {'$ref': '#/$defs/Query'},
                    ]
                }
            },
        },
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _response())) as client:
        llm = OpenAICompatible(_config(), client=client)
        with pytest.raises(ConfigError) as caught:
            await llm.create([ContextBlock(role='user', content='go')], tools=[schema])

    assert '$ref' in caught.value.received
    assert '展开 $ref' in caught.value.next_action


async def test_timeout_sent_to_the_client_equals_config_timeout() -> None:
    request = await _request_for(
        [ContextBlock(role='user', content='go')],
        request_timeout=1.25,
    )

    assert set(request.extensions['timeout'].values()) == {1.25}


def test_source_does_not_call_asyncio_timeout() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / 'src' / 'syndica' / 'backends' / 'llm_openai' / 'client.py').read_text(
        encoding='utf-8'
    )

    assert 'asyncio.timeout' not in source


def test_kernel_does_not_import_httpx_or_backends() -> None:
    root = Path(__file__).resolve().parents[2] / 'src' / 'syndica' / 'kernel'
    for path in root.rglob('*.py'):
        source = path.read_text(encoding='utf-8')
        assert 'import httpx' not in source
        assert 'syndica.backends' not in source


async def test_response_parses_text_and_tool_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                'choices': [
                    {
                        'message': {
                            'role': 'assistant',
                            'content': 'working',
                            'tool_calls': [
                                {
                                    'id': 'call-9',
                                    'type': 'function',
                                    'function': {
                                        'name': 'Read',
                                        'arguments': '{"path":"a.txt"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAICompatible(_config(), client=client).create(
            [ContextBlock(role='user', content='go')]
        )

    assert result.role == 'assistant'
    assert result.content == 'working'
    assert result.tool_calls == [ToolCall(id='call-9', name='Read', arguments='{"path":"a.txt"}')]


async def test_malformed_response_is_a_three_part_llm_error() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'choices': []}))
    ) as client:
        with pytest.raises(LLMResponseError) as caught:
            await OpenAICompatible(_config(), client=client).create(
                [ContextBlock(role='user', content='go')]
            )

    assert caught.value.expected
    assert 'choices[0]' in caught.value.received
    assert '端点响应体' in caught.value.next_action


def test_headers_must_be_a_string_mapping() -> None:
    config = LLMConfig(
        provider='openai-compatible',
        model='model-a',
        client_args={'base_url': 'https://llm.example.test/v1', 'headers': None},
    )

    with pytest.raises(ConfigError) as caught:
        OpenAICompatible(config)

    assert 'headers' in caught.value.expected
    assert 'None' in caught.value.received
    assert caught.value.next_action


async def test_provider_enforces_total_wall_clock_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return _response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        llm = OpenAICompatible(_config(timeout=0.05), client=client)
        started = time.monotonic()
        with pytest.raises(LLMResponseError) as caught:
            await llm.create([ContextBlock(role='user', content='go')])

    assert time.monotonic() - started < 0.5
    assert '0.05' in caught.value.received
    assert '超时' in caught.value.next_action
