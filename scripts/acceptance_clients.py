"""发布验收 (c)：第三方 SDK 连一个装好的 `syndica up --api-token`。

在只装了 `openai`、`mcp` 与 `httpx` 的客户端 venv 里跑（不装 syndica：客户端只认协议）。

- OpenAI SDK：带一个客户端工具做 chat；模型调它时工具调用交回给客户端，客户端把结果送回同一个会话，
  拿到最终回答。客户端送回的结果在会话快照里必须是 `untrusted`。
- MCP SDK：在 `/mcp` 上列工具，对工作区里的文件调 `Read`：拿到文件内容，`_meta.trust` 是
  `untrusted`（交给宿主之外调用方的结果一律收紧为不可信）。

地址与令牌从环境变量 `ACCEPTANCE_URL`、`ACCEPTANCE_TOKEN` 读。每一项
打一行 `ok ...`；不符时按「期望 / 实际 / 下一步」报到 stderr、退出 1。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx
import httpx2
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageFunctionToolCall

_RESULT = '{"order_id": "88412", "status": "captured", "amount": "42.10 EUR"}'
_TOOLS: list[Any] = [
    {
        'type': 'function',
        'function': {
            'name': 'lookup_order',
            'description': 'Look up an order in the shop database by its id.',
            'parameters': {
                'type': 'object',
                'properties': {'order_id': {'type': 'string'}},
                'required': ['order_id'],
            },
        },
    }
]


class CheckFailed(Exception):
    pass


def _check(condition: bool, expected: str, received: object, next_action: str) -> None:
    if not condition:
        raise CheckFailed(f'期望: {expected}\n实际: {received}\n下一步: {next_action}')


def openai_round_trip(url: str, token: str) -> None:
    client = OpenAI(base_url=f'{url}/v1', api_key=token, timeout=60)
    models = [model.id for model in client.models.list()]
    _check('Worker' in models, '/v1/models 列出角色 Worker', models, '看 server.log')
    messages: list[Any] = [
        {'role': 'user', 'content': 'What is the status of order 88412? Ask lookup_order.'}
    ]
    raw = client.chat.completions.with_raw_response.create(
        model='Worker', messages=messages, tools=_TOOLS
    )
    sid = raw.headers.get('X-Syndica-Session', '')
    first = raw.parse().choices[0]
    calls = first.message.tool_calls or []
    _check(
        first.finish_reason == 'tool_calls' and len(calls) == 1,
        '第一轮以 finish_reason=tool_calls 交回一次客户端工具调用',
        f'finish_reason={first.finish_reason}，tool_calls={calls}',
        '看假端点的剧本（scripts/acceptance_interop.json）是否命中',
    )
    call = calls[0]
    _check(
        isinstance(call, ChatCompletionMessageFunctionToolCall),
        '交回的是 function 类型的工具调用',
        type(call).__name__,
        '看 server/openai_http.py 怎么交回 tool_calls',
    )
    assert isinstance(call, ChatCompletionMessageFunctionToolCall)
    name = call.function.name
    _check(name == 'lookup_order', '调用的是客户端工具 lookup_order', name, '看 server.log')
    print(f'ok openai: tool call {name} returned to the client (session {sid[:8]})')
    messages.append(first.message.model_dump(exclude_none=True))
    messages.append({'role': 'tool', 'tool_call_id': call.id, 'content': _RESULT})
    second = client.chat.completions.with_raw_response.create(
        model='Worker',
        messages=messages,
        tools=_TOOLS,
        extra_headers={'X-Syndica-Session': sid},
    )
    final = second.parse().choices[0]
    answer = final.message.content or ''
    _check(
        final.finish_reason == 'stop' and 'captured' in answer,
        '送回工具结果之后拿到 finish_reason=stop 的最终回答，且回答用到了结果（captured）',
        f'finish_reason={final.finish_reason}，回答 {answer!r}',
        '确认第二次请求带着同一个 X-Syndica-Session 与这个 tool_call_id',
    )
    print(f'ok openai: final answer after the tool result: {answer[:70]!r}')
    snapshot = httpx.get(
        f'{url}/api/v1/sessions/{sid}/snapshot',
        headers={'Authorization': f'Bearer {token}'},
        timeout=30,
    ).json()
    blocks = snapshot['transcripts'][snapshot['root_id']]['blocks']
    trust = [block['trust'] for block in blocks if block.get('tool_call_id') == call.id]
    _check(
        trust == ['untrusted'],
        '客户端送回的工具结果在会话里恰好一块，标 untrusted',
        trust,
        '这是信任标记的回归：查 server/openai_http.py 的 ClientProxy',
    )
    print('ok openai: the client tool result is marked untrusted in the session')


async def _mcp_session(url: str, token: str) -> tuple[list[str], str, object, bool]:
    """列工具、Read 一个工作区文件；交回（工具名, Read 的文本, `_meta.trust`, isError）。

    检查放在会话之外：在 `async with` 里抛出的异常会被 anyio 的任务组包成 ExceptionGroup。
    """
    http = httpx2.AsyncClient(
        headers={'Authorization': f'Bearer {token}'}, timeout=httpx2.Timeout(60)
    )
    try:
        async with Client(
            streamable_http_client(f'{url}/mcp', http_client=http),
            cache=None,
            read_timeout_seconds=60,
        ) as client:
            names = [tool.name for tool in (await client.list_tools()).tools]
            read = await client.call_tool('Read', {'path': 'logs/app.log'})
    finally:
        await http.aclose()
    text = ' '.join(getattr(part, 'text', '') for part in read.content)
    return names, text, (read.meta or {}).get('trust'), bool(read.is_error)


def mcp_tools(url: str, token: str) -> None:
    names, text, trust, failed = asyncio.run(_mcp_session(url, token))
    _check(
        {'Read', 'Grep', 'Glob'} <= set(names) and 'Bash' not in names,
        '/mcp 缺省只列出只读的 Read、Grep、Glob',
        names,
        '看 .syndica.yaml 的 interop.mcp.serve.tools',
    )
    print(f'ok mcp: tools {names}')
    _check(
        not failed and 'circuit breaker opened' in text,
        'Read logs/app.log 读到工作区里这个文件的内容',
        f'isError={failed}，内容开头 {text[:80]!r}',
        '看 server.log',
    )
    # 宿主之外的调用方拿到的结果一律收紧为不可信（kernel/external_calls.py 的 call_external）。
    _check(
        trust == 'untrusted',
        'MCP 调用的结果 _meta.trust 是 untrusted',
        f'trust={trust!r}',
        '这是信任标记的回归：查 kernel/external_calls.py 与 server/mcp_serve.py 的 _from_result',
    )
    print('ok mcp: Read logs/app.log returned the file, marked _meta.trust=untrusted')


def main() -> int:
    url = os.environ['ACCEPTANCE_URL']
    token = os.environ['ACCEPTANCE_TOKEN']
    try:
        openai_round_trip(url, token)
        mcp_tools(url, token)
    except CheckFailed as failure:
        print(str(failure), file=sys.stderr)
        return 1
    print(json.dumps({'interop': 'ok'}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
