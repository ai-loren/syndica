"""OpenAI 兼容 backend 的请求与响应契约。"""

from __future__ import annotations

import asyncio
import copy
import json
import pickle
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from syndica.backends.llm_openai import (
    OpenAICompatible,
    ProviderHTTPError,
    recoverable,
    retry_after,
)
from syndica.backends.llm_openai.client import (
    _retry_after_seconds,  # pyright: ignore[reportPrivateUsage]
)
from syndica.kernel import LocalWorkspace, local_tools
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


async def _tool_message(content: str | list[Any] | None) -> dict[str, Any]:
    call = ToolCall(id='call-1', name='Glob', arguments='{"pattern":"*"}')
    request = await _request_for(
        [
            ContextBlock(role='assistant', tool_calls=[call]),
            ContextBlock(role='tool', content=content, tool_call_id='call-1'),
        ]
    )
    return json.loads(request.content)['messages'][1]


async def test_tool_output_list_is_sent_as_json_text_in_its_original_order() -> None:
    message = await _tool_message(['src', 'README.md', 'docs'])

    assert message == {
        'role': 'tool',
        'content': '["src", "README.md", "docs"]',
        'tool_call_id': 'call-1',
    }


async def test_structured_tool_output_keeps_non_ascii_text_and_nested_values() -> None:
    output = [{'路径': '报告.md', '命中': 2}, ['嵌套', None, True]]

    message = await _tool_message(output)

    assert isinstance(message['content'], str)
    assert '报告.md' in message['content']
    assert json.loads(message['content']) == output


@pytest.mark.parametrize(('output', 'expected'), [('', ''), ([], '[]'), (None, '')])
async def test_empty_tool_output_still_carries_a_content_key(
    output: str | list[Any] | None,
    expected: str,
) -> None:
    message = await _tool_message(output)

    assert list(message) == ['role', 'content', 'tool_call_id']
    assert message['content'] == expected


async def test_tool_content_parts_pass_through_unchanged() -> None:
    parts = [{'type': 'text', 'text': 'first'}, {'type': 'text', 'text': 'second'}]

    message = await _tool_message(parts)

    assert message['content'] == parts


@pytest.mark.parametrize(
    'output',
    [
        [{'path': 'a.py', 'line': 3}, {'path': 'b.py', 'line': 7}],
        [{'type': 'text', 'text': 'kept'}, {'path': 'b.py'}],
        [{'type': 3, 'text': 'type is not a string'}],
    ],
)
async def test_lists_that_are_not_content_parts_are_encoded_as_json_text(
    output: list[Any],
) -> None:
    message = await _tool_message(output)

    assert message['content'] == json.dumps(output, ensure_ascii=False)


async def test_values_json_cannot_encode_are_sent_as_their_text() -> None:
    message = await _tool_message([Path('docs/guide.md'), 'README.md'])

    assert json.loads(message['content']) == ['docs/guide.md', 'README.md']


async def test_real_glob_and_grep_results_become_string_tool_content(tmp_path: Path) -> None:
    (tmp_path / 'notes.md').write_text('alpha\nbeta alpha\n', encoding='utf-8')
    (tmp_path / 'README.md').write_text('alpha\n', encoding='utf-8')
    workspace = LocalWorkspace(str(tmp_path), shell='/bin/sh', env={})
    try:
        tools = local_tools(workspace.portal())
        globbed = await tools['Glob'].execute(pattern='*.md')
        grepped = await tools['Grep'].execute(pattern='alpha')
    finally:
        await workspace.close()
    calls = [
        ToolCall(id='glob-1', name='Glob', arguments='{"pattern":"*.md"}'),
        ToolCall(id='grep-1', name='Grep', arguments='{"pattern":"alpha"}'),
    ]

    request = await _request_for(
        [
            ContextBlock(role='assistant', tool_calls=calls),
            ContextBlock(role='tool', content=globbed.output, tool_call_id='glob-1'),
            ContextBlock(role='tool', content=grepped.output, tool_call_id='grep-1'),
        ]
    )
    tool_messages = json.loads(request.content)['messages'][1:]

    assert isinstance(globbed.output, list)
    assert isinstance(grepped.output, list)
    assert [type(message['content']) for message in tool_messages] == [str, str]
    assert json.loads(tool_messages[0]['content']) == globbed.output
    assert json.loads(tool_messages[1]['content']) == grepped.output


_SIGNATURE = {'google': {'thought_signature': 'real-signature-from-this-endpoint'}}
_TEXT_SIGNATURE = {'google': {'thought_signature': 'signature-on-the-text-part'}}
_PLACEHOLDER = {'google': {'thought_signature': 'skip_thought_signature_validator'}}


def _signed_response(*call_extras: dict[str, Any] | None) -> httpx.Response:
    calls: list[dict[str, Any]] = []
    for index, extra in enumerate(call_extras, start=1):
        call: dict[str, Any] = {
            'id': f'call_{index}',
            'type': 'function',
            'function': {'name': 'Glob', 'arguments': '{"pattern":"*"}'},
        }
        if extra is not None:
            call['extra_content'] = extra
        calls.append(call)
    message = {'role': 'assistant', 'content': None, 'tool_calls': calls}
    message['extra_content'] = _TEXT_SIGNATURE
    return httpx.Response(200, json={'choices': [{'message': message}]})


def _config_for(model: str, **client_args: Any) -> LLMConfig:
    return LLMConfig(
        provider='openai-compatible',
        model=model,
        client_args={'base_url': 'https://llm.example.test/v1', **client_args},
    )


async def _second_request(
    first: httpx.Response,
    *,
    producer: LLMConfig,
    consumer: LLMConfig,
) -> dict[str, Any]:
    """`producer` 答出第一轮，`consumer` 带着这段历史发第二轮；交回第二轮的 assistant 消息。"""
    seen: list[httpx.Request] = []
    replies = [first, _response()]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reply = await OpenAICompatible(producer, client=client).create(
            [ContextBlock(role='user', content='go')]
        )
        history = [
            ContextBlock(role='user', content='go'),
            reply,
            *(
                ContextBlock(role='tool', content='["a"]', tool_call_id=call.id)
                for call in reply.tool_calls
            ),
        ]
        await OpenAICompatible(consumer, client=client).create(history)
    return json.loads(seen[1].content)['messages'][1]


async def test_provider_extra_content_goes_back_to_the_endpoint_that_produced_it() -> None:
    config = _config_for('model-a', foreign_tool_call_extra=_PLACEHOLDER)

    assistant = await _second_request(
        _signed_response(_SIGNATURE), producer=config, consumer=config
    )

    assert assistant['tool_calls'][0]['extra_content'] == _SIGNATURE
    assert assistant['extra_content'] == _TEXT_SIGNATURE
    assert list(assistant) == ['role', 'tool_calls', 'extra_content']
    assert list(assistant['tool_calls'][0]) == ['id', 'type', 'function', 'extra_content']


async def test_an_own_tool_call_that_came_without_extra_content_gets_no_placeholder() -> None:
    config = _config_for('model-a', foreign_tool_call_extra=_PLACEHOLDER)

    assistant = await _second_request(
        _signed_response(_SIGNATURE, None), producer=config, consumer=config
    )

    assert assistant['tool_calls'][0]['extra_content'] == _SIGNATURE
    assert 'extra_content' not in assistant['tool_calls'][1]


async def test_extra_content_from_another_model_on_the_same_url_is_treated_as_foreign() -> None:
    assistant = await _second_request(
        _signed_response(_SIGNATURE, _SIGNATURE),
        producer=_config_for('model-a'),
        consumer=_config_for('model-b', foreign_tool_call_extra=_PLACEHOLDER),
    )

    assert [call['extra_content'] for call in assistant['tool_calls']] == [_PLACEHOLDER] * 2
    assert 'extra_content' not in assistant


async def test_foreign_tool_calls_carry_nothing_when_no_placeholder_is_configured() -> None:
    assistant = await _second_request(
        _signed_response(_SIGNATURE),
        producer=_config_for('model-a'),
        consumer=_config_for('model-b'),
    )

    assert 'extra_content' not in assistant['tool_calls'][0]
    assert 'extra_content' not in assistant


async def test_a_reply_without_provider_extras_adds_nothing_to_extra() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _response())) as client:
        reply = await OpenAICompatible(_config(), client=client).create(
            [ContextBlock(role='user', content='go')]
        )

    assert reply.extra == {}


def test_foreign_tool_call_extra_must_be_a_json_object() -> None:
    with pytest.raises(ConfigError) as caught:
        OpenAICompatible(_config_for('model-a', foreign_tool_call_extra='skip'))

    assert 'foreign_tool_call_extra' in caught.value.expected
    assert "'skip'" in caught.value.received
    assert caught.value.next_action


def test_foreign_tool_call_extra_is_not_handed_to_httpx() -> None:
    llm = OpenAICompatible(_config_for('model-a', foreign_tool_call_extra=_PLACEHOLDER))

    assert 'foreign_tool_call_extra' not in llm._client_args  # pyright: ignore[reportPrivateUsage]


async def _status_error(response: httpx.Response) -> LLMResponseError:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(LLMResponseError) as caught:
            await OpenAICompatible(_config(), client=client).create(
                [ContextBlock(role='user', content='go')]
            )
    return caught.value


async def test_a_rate_limited_reply_carries_its_status_and_retry_after_seconds() -> None:
    error = await _status_error(httpx.Response(429, headers={'Retry-After': '7'}, json={}))

    assert isinstance(error, ProviderHTTPError)
    assert error.status_code == 429
    assert error.retry_after == 7.0
    assert retry_after(error) == 7.0
    assert 'HTTP 429' in error.received


async def test_a_server_error_without_hints_has_a_status_and_no_retry_after() -> None:
    error = await _status_error(httpx.Response(500, json={'error': 'boom'}))

    assert isinstance(error, ProviderHTTPError)
    assert error.status_code == 500
    assert retry_after(error) is None


def test_retry_after_is_none_for_errors_that_are_not_http_status_errors() -> None:
    plain = LLMResponseError(expected='e', received='r', next_action='n')

    assert retry_after(plain) is None
    assert retry_after(RuntimeError('boom')) is None


@pytest.mark.parametrize(
    ('status', 'waiting_helps'),
    [
        (429, True),
        (408, True),
        (425, True),
        (500, True),
        (501, True),
        (503, True),
        (520, True),
        (400, False),
        (401, False),
        (402, False),
        (403, False),
        (404, False),
        (422, False),
    ],
)
async def test_recoverable_tells_limits_and_outages_from_errors_waiting_cannot_fix(
    status: int, waiting_helps: bool
) -> None:
    error = await _status_error(httpx.Response(status, json={}))

    assert recoverable(error) is waiting_helps


def test_failures_without_an_http_status_count_as_recoverable() -> None:
    timeout = LLMResponseError(expected='e', received='请求在 60 秒内没有完成', next_action='n')

    assert recoverable(timeout) is True
    assert recoverable(ProviderHTTPError(expected='e', received='r', next_action='n')) is True
    assert recoverable(RuntimeError('boom')) is True


@pytest.mark.parametrize(
    'body',
    [
        {
            'error': {
                'details': [
                    {'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '31s'}
                ]
            }
        },
        [
            {
                'error': {
                    'details': [
                        {'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '31s'}
                    ]
                }
            }
        ],
    ],
)
def test_google_retry_info_is_read_from_the_error_body(body: object) -> None:
    assert _retry_after_seconds(httpx.Response(429, json=body), now=1000.0) == 31.0


def test_an_openrouter_reset_timestamp_in_milliseconds_becomes_seconds_from_now() -> None:
    response = httpx.Response(429, headers={'X-RateLimit-Reset': str(1_700_000_120_000)})

    assert _retry_after_seconds(response, now=1_700_000_000.0) == 120.0


def test_a_retry_after_http_date_becomes_seconds_from_now() -> None:
    response = httpx.Response(503, headers={'Retry-After': 'Tue, 14 Nov 2023 22:14:20 GMT'})

    assert _retry_after_seconds(response, now=1_700_000_000.0) == 60.0


def test_the_latest_of_several_retry_hints_wins() -> None:
    response = httpx.Response(
        429,
        headers={'Retry-After': '5', 'X-RateLimit-Reset': str(1_700_000_090_000)},
        json=[
            {
                'error': {
                    'details': [
                        {'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '40s'}
                    ]
                }
            }
        ],
    )

    assert _retry_after_seconds(response, now=1_700_000_000.0) == 90.0


def test_a_reset_time_already_in_the_past_means_retry_now() -> None:
    response = httpx.Response(429, headers={'X-RateLimit-Reset': str(1_699_999_000_000)})

    assert _retry_after_seconds(response, now=1_700_000_000.0) == 0.0


def test_retry_hints_are_ignored_on_statuses_that_do_not_ask_for_a_retry() -> None:
    response = httpx.Response(400, headers={'Retry-After': '30'})

    assert _retry_after_seconds(response, now=1000.0) is None


def test_provider_http_errors_keep_status_and_retry_after_across_copy_and_pickle() -> None:
    error = ProviderHTTPError(
        expected='e', received='r', next_action='n', status_code=429, retry_after=12.5
    )

    for clone in (copy.copy(error), copy.deepcopy(error), pickle.loads(pickle.dumps(error))):
        assert isinstance(clone, ProviderHTTPError)
        assert (clone.status_code, clone.retry_after) == (429, 12.5)


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


async def test_http_status_error_includes_provider_response_body() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                400,
                json={'error': {'message': 'No endpoints found that support tool use'}},
            )
        )
    ) as client:
        with pytest.raises(LLMResponseError) as caught:
            await OpenAICompatible(_config(), client=client).create(
                [ContextBlock(role='user', content='go')]
            )

    assert 'HTTP 400 Bad Request' in caught.value.received
    assert 'No endpoints found that support tool use' in caught.value.received
    assert '模型名' in caught.value.next_action


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
