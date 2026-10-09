"""OpenAI 兼容 HTTP。鉴权仍由 `create_app` 做。

采样参数收下并忽略，列在 `X-Syndica-Ignored`。改变语义的参数 400。错误体是 OpenAI 的形状：
`message` 是三件套，`type` 按状态码（5xx 是 `server_error`，其余 `invalid_request_error`），
`param` / `code` 用 OpenAI 的取值。不发 `x-should-retry`。客户端断开不中止已经开始的那一轮。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from syndica.kernel import (
    AgentStatus,
    ConfigError,
    Redactor,
    Reply,
    Tool,
    ToolResult,
    TrustLevel,
    WaitState,
)
from syndica.kernel.ids import current_tool_call
from syndica.server.live import Live, LiveSession
from syndica.server.outbound import json_response, redacted
from syndica.server.streams import Entry

__all__ = ['OpenAIHttp', 'chat_app', 'models_app', 'responses_app']

logger = logging.getLogger(__name__)

_IGNORED = (
    'temperature',
    'top_p',
    'max_tokens',
    'max_completion_tokens',
    'frequency_penalty',
    'presence_penalty',
    'stop',
    'seed',
    'user',
    'max_output_tokens',
    'tool_choice',
    'parallel_tool_calls',
)
_TOOL_WAIT_S = 60.0


class ClientProxy(Tool):
    """等下一次 HTTP 请求交回同一个 tool_call_id 的结果。结果不可信。"""

    __tool_command_args__ = ()
    __tool_external_input__ = True

    def __init__(
        self, runtime: Any, agent_id: str, name: str, description: str, parameters: object
    ) -> None:
        self._runtime = runtime
        self._agent_id = agent_id
        self._proxy_name = name
        schema_parameters: dict[str, Any] = (
            cast(dict[str, Any], parameters)
            if isinstance(parameters, dict)
            else {'type': 'object', 'properties': {}}
        )
        self._proxy_schema: dict[str, Any] = {
            'name': name,
            'description': description or name,
            'parameters': schema_parameters,
        }

    @property
    def name(self) -> str:
        return self._proxy_name

    def _schema(self) -> dict[str, Any]:
        return self._proxy_schema

    async def execute(self, **arguments: Any) -> ToolResult:
        del arguments
        call = current_tool_call()
        if call is None or not call.call_id.strip():
            return ToolResult(output=_missed('这次调用没有 tool_call_id'))
        try:
            result = await self._runtime.wait_for(
                self._agent_id, Reply(correlation_id=call.call_id, timeout=_TOOL_WAIT_S)
            )
        except ConfigError as exc:
            return ToolResult(
                output=f'期望: {exc.expected}。实际: {exc.received}。下一步: {exc.next_action}'
            )
        if result.state is not WaitState.ANSWERED or result.message is None:
            return ToolResult(output=_missed(result.state.value))
        return ToolResult(output=result.message.content, trust=TrustLevel.UNTRUSTED)


def _missed(state: str) -> str:
    return (
        '期望: 客户端在下一次请求里交回这个 tool_call_id 的结果。'
        f'实际: {state}。'
        '下一步: 用同一个 X-Syndica-Session 再请求，消息里带上这个 tool_call_id 的 tool 结果。'
    )


@dataclass
class _Seat:
    session: LiveSession
    tools: frozenset[str] = field(default_factory=lambda: frozenset[str]())
    consumed: int = 0
    response_seq: int = 0
    question: tuple[str, tuple[str, ...]] | None = None


@dataclass(frozen=True)
class _Turn:
    """根 Agent 这一轮的回答。`blocks` 是一个个 assistant 块的文本，流式时每块一个 delta。"""

    blocks: tuple[str, ...]
    tool_calls: tuple[dict[str, str], ...]
    finish: str

    @property
    def text(self) -> str:
        return '\n'.join(self.blocks)


@dataclass
class _Exchange:
    """一次 POST 点名的会话与它新开的会话。错误响应据此决定带不带 `X-Syndica-Session`。"""

    requested: str | None
    previous_response_id: str | None = None
    opened: str | None = None


class OpenAIHttp:
    """角色就是模型名。会话 id 放在 `X-Syndica-Session`，Responses 用 `previous_response_id`。"""

    def __init__(self, live: Live, roles: Sequence[str], redactor: Redactor) -> None:
        self._live = live
        self._roles = tuple(roles)
        self._redactor = redactor
        self._seats: dict[str, _Seat] = {}

    def redactor(self) -> Redactor:
        return self._redactor

    def models(self) -> dict[str, Any]:
        return {
            'object': 'list',
            'data': [
                {'id': role, 'object': 'model', 'created': 0, 'owned_by': 'syndica'}
                for role in self._roles
            ],
        }

    async def post(self, request: Request, *, responses: bool) -> Response:
        """一次 POST。请求落在一个开着的会话里时，错误响应同样带 `X-Syndica-Session`。"""
        exchange = _Exchange(request.headers.get('x-syndica-session'))
        try:
            body = await _request_body(request)
            if responses:
                return await self._responses_response(body, exchange)
            return await self._chat_response(body, exchange)
        except _Refusal as refusal:
            return self._refused(refusal, exchange)
        except ConfigError as exc:
            return self._refused(_Refusal(400, exc), exchange)
        except Exception as exc:
            logger.exception('openai %s failed', request.url.path)
            return self._refused(_Refusal(500, _unexpected(type(exc).__name__)), exchange)

    async def chat(
        self, body: Mapping[str, Any], exchange: _Exchange
    ) -> tuple[_Turn, str, tuple[str, ...]]:
        ignored = _ignored(body)
        _reject_semantic(body)
        model = self._model(body)
        messages = _messages(body.get('messages'))
        tools = _client_tools(body.get('tools'))
        seat = await self._seat(exchange, model, tools)
        turn = await self._run(seat, messages, tools)
        return turn, seat.session.sid, ignored

    async def responses(
        self, body: Mapping[str, Any], exchange: _Exchange
    ) -> tuple[_Turn, str, tuple[str, ...], str]:
        ignored = _ignored(body)
        _reject_semantic(body)
        model = self._model(body)
        messages = _response_input(body.get('input'))
        tools = _client_tools(body.get('tools'))
        seat = await self._seat(exchange, model, tools)
        turn = await self._run(seat, messages, tools)
        seat.response_seq += 1
        return turn, seat.session.sid, ignored, f'resp_{seat.session.sid}_{seat.response_seq}'

    async def _chat_response(self, body: Mapping[str, Any], exchange: _Exchange) -> Response:
        turn, sid, ignored = await self.chat(body, exchange)
        model = str(body.get('model') or '')
        include_usage = _include_usage(body)
        if body.get('stream') is True:
            return _chat_stream(
                model, turn, sid, ignored, self._redactor, include_usage=include_usage
            )
        return _json(
            self._redactor,
            _chat_body(model, turn, include_usage=include_usage),
            headers=_headers(sid, ignored),
        )

    async def _responses_response(self, body: Mapping[str, Any], exchange: _Exchange) -> Response:
        previous = body.get('previous_response_id')
        if isinstance(previous, str) and previous:
            exchange.previous_response_id = previous
            exchange.requested = _sid_from_response_id(previous)
        turn, sid, ignored, response_id = await self.responses(body, exchange)
        model = str(body.get('model') or '')
        tools = _client_tools(body.get('tools'))
        if body.get('stream') is True:
            return _responses_stream(model, response_id, turn, tools, sid, ignored, self._redactor)
        return _json(
            self._redactor,
            _response_object(model, response_id, turn, tools),
            headers=_headers(sid, ignored),
        )

    def _refused(self, refusal: _Refusal, exchange: _Exchange) -> Response:
        response = _json(self._redactor, _error_body(refusal), status=refusal.status)
        sid = self._session_of(exchange)
        if sid is not None:
            response.headers['X-Syndica-Session'] = sid
        return response

    def _session_of(self, exchange: _Exchange) -> str | None:
        """这次请求落在哪个还能续接的会话里。点名的会话不存在就是 None，不把它原样回显成会话。"""
        if exchange.opened is not None:
            return exchange.opened
        if exchange.requested is not None and exchange.requested in self._seats:
            return exchange.requested
        return None

    def _model(self, body: Mapping[str, Any]) -> str:
        model = _require_text(body.get('model'), 'model')
        if model not in self._roles:
            raise _Refusal(404, _missing_model(model, self._roles), code='model_not_found')
        return model

    async def _seat(
        self, exchange: _Exchange, role: str, tools: tuple[dict[str, Any], ...]
    ) -> _Seat:
        names = frozenset(str(tool['name']) for tool in tools)
        if exchange.requested is None:
            session = await self._live.open_session(resume=None, role=role)
            self._live.hold_questions.add(session.sid)
            seat = _Seat(session)
            try:
                self._install(seat, tools)
            except ConfigError as exc:
                # 调用方拿不到这个会话的 id：留着它就是一个再也续不上、一直开着的空会话。
                await self._live.close(session.sid)
                raise _Refusal(400, exc, code='invalid_value', param='tools') from exc
            self._seats[session.sid] = seat
            exchange.opened = session.sid
            return seat
        seat = self._seats.get(exchange.requested)
        if seat is None:
            raise _unknown_session(exchange)
        if names != seat.tools:
            raise _bad(
                'tools',
                'invalid_value',
                expected=f'工具集与建会话时相同：{sorted(seat.tools)!r}',
                received=f'这次是 {sorted(names)!r}',
                next_action='沿用第一次请求的 tools，或新开一个会话',
            )
        return seat

    def _install(self, seat: _Seat, tools: tuple[dict[str, Any], ...]) -> None:
        runtime = cast(Any, seat.session.handle).runtime
        proxies = {
            str(tool['name']): ClientProxy(
                runtime,
                seat.session.handle.root_id,
                str(tool['name']),
                str(tool.get('description') or tool['name']),
                tool.get('parameters'),
            )
            for tool in tools
        }
        seat.session.handle.install_tools(proxies)
        seat.tools = frozenset(proxies)

    async def _run(
        self, seat: _Seat, messages: list[dict[str, Any]], tools: tuple[dict[str, Any], ...]
    ) -> _Turn:
        del tools
        fresh, consumed = _unseen(messages, seat.consumed)
        position = seat.session.stream.position
        await self._deliver(seat, fresh)
        seat.consumed = consumed
        task = asyncio.create_task(self._wait(seat, position), name=f'openai:{seat.session.sid}')
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise

    async def _deliver(self, seat: _Seat, messages: list[dict[str, Any]]) -> None:
        question = seat.question
        for message in messages:
            if message['role'] != 'tool':
                continue
            call_id = str(message.get('tool_call_id') or '')
            if not call_id:
                continue
            await cast(Any, seat.session.handle).runtime.send(
                seat.session.handle.root_id,
                str(message.get('content') or ''),
                activate=False,
                sender='client',
                correlation_id=call_id,
                trust=TrustLevel.UNTRUSTED,
            )
        user_text = _render([message for message in messages if message['role'] != 'tool'])
        if question is not None and user_text:
            request_id, options = question
            if options and user_text not in options:
                raise _bad(
                    None,
                    'invalid_value',
                    expected=f'答复是列出的选项之一：{list(options)}',
                    received=user_text,
                    next_action='原样回复其中一个选项',
                )
            await seat.session.answer(request_id, user_text)
            seat.question = None
            return
        if not user_text:
            return
        # 工具结果已经按条不可信送过。剩下的说明以人的身份送：调用方持有 API 令牌。
        delivered = await seat.session.handle.deliver(seat.session.handle.root_id, user_text)
        if not delivered.ok:
            raise ConfigError(
                expected='根 Agent 接受这段消息',
                received=delivered.reason or '投递没有给出原因',
                next_action='用同一个 X-Syndica-Session 再试；会话结束了就新开一个',
            )

    async def _wait(self, seat: _Seat, position: str) -> _Turn:
        texts: list[str] = []
        calls: list[dict[str, str]] = []
        follower: Any = seat.session.stream.follow(position)
        try:
            async for entry in follower:
                if isinstance(entry, str):
                    continue
                try:
                    outcome = self._event(seat, entry, texts, calls)
                except _Refusal:
                    await self._until_quiet(seat)
                    raise
                if outcome is None:
                    continue
                if outcome.finish == 'stop' and seat.question is None:
                    # agent_end 发出时驱动还在拆邮箱。立刻再送会落进那个邮箱，拆完就没了。
                    await self._until_quiet(seat)
                return outcome
        finally:
            close = getattr(follower, 'aclose', None)
            if close is not None:
                await close()
        return _Turn(tuple(texts), (), 'stop')

    def _event(
        self, seat: _Seat, entry: Entry, texts: list[str], calls: list[dict[str, str]]
    ) -> _Turn | None:
        payload = _payload(entry.data)
        # 子 Agent 的提问照样交回：根停在派活里等它。答复经待答表交给提问的那个 Agent。
        if entry.kind in {'user_question_requested', 'user_input_requested'}:
            options = _strings(payload.get('options'))
            question = str(payload.get('question') or '')
            request_id = str(payload.get('request_id') or '')
            seat.question = (request_id, tuple(options))
            lines = [question, *(f'{index}. {option}' for index, option in enumerate(options, 1))]
            asked = '\n'.join(line for line in lines if line)
            return _Turn((*texts, asked) if asked else tuple(texts), (), 'stop')
        # 回答、结束、失败与客户端工具调用只认根：代理工具只装在根上，子 Agent 与客户端工具
        # 同名的调用是它自己在服务端跑的工具，交给客户端就是把服务端的命令当成客户端要执行的调用。
        if entry.data.get('agent_id') != seat.session.handle.root_id:
            return None
        if entry.kind == 'agent_error':
            raise _Refusal(500, _root_failed(str(payload.get('error_type') or '未知类型')))
        if entry.kind == 'message_appended':
            block = _as_dict(payload.get('block'))
            if block is None or block.get('role') != 'assistant':
                return None
            text = block.get('text')
            if isinstance(text, str) and text:
                texts.append(text)
            raw_calls = block.get('tool_calls')
            if isinstance(raw_calls, list):
                for item in cast(list[object], raw_calls):
                    call = _as_dict(item)
                    if call is None:
                        continue
                    name = str(call.get('name') or '')
                    if name not in seat.tools:
                        continue
                    calls.append(
                        {
                            'id': str(call.get('id') or ''),
                            'name': name,
                            'arguments': str(call.get('arguments') or '{}'),
                        }
                    )
            if calls:
                return _Turn(tuple(texts), tuple(calls), 'tool_calls')
            return None
        if entry.kind == 'agent_end':
            return _Turn(tuple(texts), (), 'stop')
        return None

    async def _until_quiet(self, seat: _Seat) -> None:
        runtime = cast(Any, seat.session.handle).runtime
        agent_id = seat.session.handle.root_id
        for _ in range(200):
            info = await runtime.find(agent_id)
            if info is None or info.status is AgentStatus.ZOMBIE:
                return
            await asyncio.sleep(0.01)


def models_app(api: OpenAIHttp) -> Any:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        response = json_response(api.models(), api.redactor())
        await response(scope, receive, send)

    return app


def chat_app(api: OpenAIHttp) -> Any:
    return _post_app(api, responses=False)


def responses_app(api: OpenAIHttp) -> Any:
    return _post_app(api, responses=True)


def _post_app(api: OpenAIHttp, *, responses: bool) -> Any:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        response = await api.post(Request(scope, receive), responses=responses)
        await response(scope, receive, send)

    return app


def _chat_body(model: str, turn: _Turn, *, include_usage: bool) -> dict[str, Any]:
    choice: dict[str, Any] = {
        'index': 0,
        'finish_reason': turn.finish,
        'message': {'role': 'assistant', 'content': turn.text or None},
    }
    if turn.tool_calls:
        choice['message']['tool_calls'] = [
            {
                'id': call['id'],
                'type': 'function',
                'function': {'name': call['name'], 'arguments': call['arguments']},
            }
            for call in turn.tool_calls
        ]
    body: dict[str, Any] = {
        'id': 'chatcmpl-' + secrets.token_hex(8),
        'object': 'chat.completion',
        'created': 0,
        'model': model,
        'choices': [choice],
    }
    if include_usage:
        body['usage'] = _usage(turn.text)
    return body


def _usage(text: str) -> dict[str, int]:
    tokens = len(text)
    return {'prompt_tokens': 0, 'completion_tokens': tokens, 'total_tokens': tokens}


def _chat_stream(
    model: str,
    turn: _Turn,
    sid: str,
    ignored: tuple[str, ...],
    redactor: Redactor,
    *,
    include_usage: bool,
) -> StreamingResponse:
    ident = 'chatcmpl-' + secrets.token_hex(8)

    async def chunks() -> AsyncIterator[str]:
        deltas: list[dict[str, Any]] = [{'content': piece} for piece in _pieces(turn.blocks)]
        if turn.tool_calls:
            deltas.append(
                {
                    'tool_calls': [
                        {
                            'index': index,
                            'id': call['id'],
                            'type': 'function',
                            'function': {'name': call['name'], 'arguments': call['arguments']},
                        }
                        for index, call in enumerate(turn.tool_calls)
                    ]
                }
            )
        if not deltas:
            deltas.append({'content': ''})
        deltas[0] = {'role': 'assistant', **deltas[0]}
        for delta in deltas:
            chunk = {
                'id': ident,
                'object': 'chat.completion.chunk',
                'created': 0,
                'model': model,
                'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}],
            }
            yield _sse(chunk, redactor)
        done: dict[str, Any] = {
            'id': ident,
            'object': 'chat.completion.chunk',
            'created': 0,
            'model': model,
            'choices': [{'index': 0, 'delta': {}, 'finish_reason': turn.finish}],
        }
        yield _sse(done, redactor)
        if include_usage:
            yield _sse(
                {
                    'id': ident,
                    'object': 'chat.completion.chunk',
                    'created': 0,
                    'model': model,
                    'choices': [],
                    'usage': _usage(turn.text),
                },
                redactor,
            )
        yield 'data: [DONE]\n\n'

    return StreamingResponse(
        chunks(), media_type='text/event-stream', headers=_headers(sid, ignored)
    )


def _pieces(blocks: Sequence[str]) -> list[str]:
    """每个 assistant 块一个 delta。后面的块带上换行：拼起来要与非流式的 `content` 逐字相同。"""
    return [block if index == 0 else '\n' + block for index, block in enumerate(blocks)]


def _response_object(
    model: str, response_id: str, turn: _Turn, tools: tuple[dict[str, Any], ...]
) -> dict[str, Any]:
    """`tools` 是这次请求的客户端工具，续接时与建会话时相同；模型自己决定调不调，所以有工具时
    `tool_choice` 如实写 auto。请求里的 `tool_choice` 不生效，列在 `X-Syndica-Ignored` 里。"""
    output: list[dict[str, Any]] = []
    if turn.blocks or not turn.tool_calls:
        output.append(
            {
                'type': 'message',
                'id': f'msg_{response_id}',
                'role': 'assistant',
                'status': 'completed',
                'content': [{'type': 'output_text', 'text': turn.text, 'annotations': []}],
            }
        )
    output.extend(
        {
            'type': 'function_call',
            'id': f'fc_{call["id"]}',
            'call_id': call['id'],
            'name': call['name'],
            'arguments': call['arguments'],
            'status': 'completed',
        }
        for call in turn.tool_calls
    )
    return {
        'id': response_id,
        'object': 'response',
        'created_at': 0,
        'model': model,
        'status': 'completed',
        'output': output,
        'parallel_tool_calls': False,
        'tool_choice': 'auto' if tools else 'none',
        'tools': [
            {
                'type': 'function',
                'name': tool['name'],
                'description': tool['description'] or None,
                'parameters': tool['parameters'],
                'strict': False,
            }
            for tool in tools
        ],
    }


def _responses_stream(
    model: str,
    response_id: str,
    turn: _Turn,
    tools: tuple[dict[str, Any], ...],
    sid: str,
    ignored: tuple[str, ...],
    redactor: Redactor,
) -> StreamingResponse:
    final = _response_object(model, response_id, turn, tools)
    opened: dict[str, Any] = {**final, 'status': 'in_progress', 'output': []}

    async def chunks() -> AsyncIterator[str]:
        sequence = 0

        def event(payload: dict[str, Any]) -> str:
            nonlocal sequence
            payload['sequence_number'] = sequence
            sequence += 1
            return _sse(payload, redactor)

        yield event({'type': 'response.created', 'response': opened})
        items: list[dict[str, Any]] = final['output']
        for index, item in enumerate(items):
            item_id = str(item['id'])
            if item['type'] == 'message':
                started: dict[str, Any] = {
                    **item,
                    'status': 'in_progress',
                    'content': [{'type': 'output_text', 'text': '', 'annotations': []}],
                }
                yield event(
                    {'type': 'response.output_item.added', 'output_index': index, 'item': started}
                )
                for piece in _pieces(turn.blocks) or ['']:
                    yield event(
                        {
                            'type': 'response.output_text.delta',
                            'output_index': index,
                            'content_index': 0,
                            'item_id': item_id,
                            'delta': piece,
                            'logprobs': [],
                        }
                    )
                yield event(
                    {
                        'type': 'response.output_text.done',
                        'output_index': index,
                        'content_index': 0,
                        'item_id': item_id,
                        'text': turn.text,
                        'logprobs': [],
                    }
                )
            else:
                started = {**item, 'arguments': '', 'status': 'in_progress'}
                yield event(
                    {'type': 'response.output_item.added', 'output_index': index, 'item': started}
                )
                yield event(
                    {
                        'type': 'response.function_call_arguments.delta',
                        'output_index': index,
                        'item_id': item_id,
                        'delta': item['arguments'],
                    }
                )
                yield event(
                    {
                        'type': 'response.function_call_arguments.done',
                        'output_index': index,
                        'item_id': item_id,
                        'name': item['name'],
                        'arguments': item['arguments'],
                    }
                )
            yield event({'type': 'response.output_item.done', 'output_index': index, 'item': item})
        yield event({'type': 'response.completed', 'response': final})

    return StreamingResponse(
        chunks(), media_type='text/event-stream', headers=_headers(sid, ignored)
    )


def _headers(sid: str, ignored: tuple[str, ...]) -> dict[str, str]:
    headers = {'X-Syndica-Session': sid}
    if ignored:
        headers['X-Syndica-Ignored'] = ','.join(ignored)
    return headers


def _json(
    redactor: Redactor,
    body: object,
    *,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
) -> Response:
    response = json_response(body, redactor, status=status)
    for key, value in (headers or {}).items():
        response.headers[key] = value
    return response


def _error_body(refusal: _Refusal) -> dict[str, Any]:
    error = refusal.error
    message = f'期望: {error.expected}。实际: {error.received}。下一步: {error.next_action}'
    return {
        'error': {
            'message': message,
            'type': 'server_error' if refusal.status >= 500 else 'invalid_request_error',
            'param': refusal.param,
            'code': refusal.code,
        }
    }


async def _request_body(request: Request) -> dict[str, Any]:
    try:
        raw: object = await request.json()
    except ValueError:
        raise _bad(
            None,
            None,
            expected='请求体是 UTF-8 编码的 JSON 对象',
            received='请求体解析不出 JSON',
            next_action='发送 Content-Type: application/json 的 JSON 对象',
        ) from None
    body = _as_dict(raw)
    if body is None:
        raise _bad(
            None,
            None,
            expected='请求体是 JSON 对象',
            received=f'收到 {type(raw).__name__}',
            next_action='发送 application/json 对象，例如 {"model": "Worker", ...}',
        )
    return body


def _include_usage(body: Mapping[str, Any]) -> bool:
    options = _as_dict(body.get('stream_options'))
    return options is not None and options.get('include_usage') is True


def _sid_from_response_id(value: str) -> str:
    prefix = 'resp_'
    if not value.startswith(prefix):
        return value
    sid, separator, seq = value[len(prefix) :].rpartition('_')
    if separator and sid and seq.isdigit():
        return sid
    return value


def _unseen(messages: list[dict[str, Any]], consumed: int) -> tuple[list[dict[str, Any]], int]:
    """历史变长时只交新增的尾巴。等长或更短是 Responses 的增量输入，整段都是新的。"""
    if len(messages) > consumed:
        fresh = list(messages[consumed:])
        if consumed:
            while fresh and fresh[0].get('role') == 'assistant':
                fresh.pop(0)
        return fresh, len(messages)
    return list(messages), consumed + len(messages)


def _ignored(body: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(name for name in _IGNORED if name in body)


def _reject_semantic(body: Mapping[str, Any]) -> None:
    count = body.get('n')
    # bool 是 int 的子类，而 True == 1：不先拒 bool，n=true 会被当成 n=1 放过。
    if count is not None and (isinstance(count, bool) or not isinstance(count, int)):
        raise _bad(
            'n',
            'invalid_type',
            expected='n 是整数 1 或缺省',
            received=f'n={count!r}，类型是 {type(count).__name__}',
            next_action='去掉 n，或传整数 1；JSON 的 true 与 1.0 都不算 1',
        )
    if count is not None and count != 1:
        raise _bad(
            'n',
            'unsupported_value',
            expected='n 是 1 或缺省',
            received=f'n={count!r}',
            next_action='一次只生成一条；要多条就分多次请求',
        )
    logprobs = body.get('logprobs')
    if logprobs is True:
        raise _bad(
            'logprobs',
            'unsupported_parameter',
            expected='不请求 logprobs',
            received='logprobs=true',
            next_action='去掉 logprobs；这条接口不返回 token 对数概率',
        )
    # 判 false 要用 is：0 == False，按相等判会把 logprobs=0 当成 false 放过。
    if logprobs is not None and logprobs is not False:
        raise _bad(
            'logprobs',
            'invalid_type',
            expected='logprobs 是布尔值 false 或缺省',
            received=f'logprobs={logprobs!r}，类型是 {type(logprobs).__name__}',
            next_action='去掉 logprobs；这条接口不返回 token 对数概率',
        )
    fmt = _as_dict(body.get('response_format'))
    if fmt is not None and fmt.get('type') == 'json_schema':
        raise _bad(
            'response_format',
            'unsupported_value',
            expected='response_format 不是 json_schema',
            received='type=json_schema',
            next_action='去掉 response_format，或只用 json_object 以外的纯文本',
        )
    if body.get('audio') is not None:
        raise _bad(
            'audio',
            'unsupported_parameter',
            expected='请求里没有音频',
            received='带了 audio',
            next_action='只发文本；这条接口不收也不发音频',
        )
    modalities = body.get('modalities')
    if isinstance(modalities, list) and 'audio' in modalities:
        raise _bad(
            'modalities',
            'unsupported_value',
            expected='请求里没有音频',
            received='modalities 含 audio',
            next_action='只发文本；这条接口不收也不发音频',
        )


def _client_tools(value: object) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _bad(
            'tools',
            'invalid_type',
            expected='tools 是 function 工具的列表',
            received=f'收到 {type(value).__name__}',
            next_action='传 [{"type": "function", "function": {"name": ..., "parameters": ...}}]',
        )
    tools: list[dict[str, Any]] = []
    for index, item in enumerate(cast(list[object], value)):
        row = _as_dict(item)
        if row is None:
            raise _bad(
                f'tools[{index}]',
                'invalid_type',
                expected='每个工具是一个对象',
                received=f'收到 {type(item).__name__}',
                next_action='传 {"type": "function", "function": {"name": ..., "parameters": ...}}',
            )
        kind = row.get('type')
        if kind not in (None, 'function'):
            raise _bad(
                f'tools[{index}].type',
                'invalid_value',
                expected='每个工具的 type 是 function',
                received=f'tools[{index}].type={kind!r}',
                next_action='不要用内置工具；只传 type=function',
            )
        nested = _as_dict(row.get('function'))
        function = nested or row
        name = function.get('name')
        if not isinstance(name, str) or not name.strip():
            field = f'tools[{index}].function.name' if nested else f'tools[{index}].name'
            raise _bad(
                field,
                _shape_code(name, str),
                expected='function.name 是非空字符串',
                received=f'收到 {name!r}',
                next_action='给工具起一个名字',
            )
        raw_parameters = function.get('parameters')
        parameters: dict[str, Any] = (
            cast(dict[str, Any], raw_parameters)
            if isinstance(raw_parameters, dict)
            else {'type': 'object', 'properties': {}}
        )
        tools.append(
            {
                'name': name,
                'description': str(function.get('description') or ''),
                'parameters': parameters,
            }
        )
    return tuple(tools)


def _messages(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(cast(list[object], value)) == 0:
        received = '空列表' if isinstance(value, list) else type(value).__name__
        raise _bad(
            'messages',
            _shape_code(cast(object, value), list),
            expected='messages 是非空列表',
            received=f'收到 {received}',
            next_action='至少放一条 user 消息',
        )
    messages: list[dict[str, Any]] = []
    for index, item in enumerate(cast(list[object], value)):
        row = _as_dict(item)
        if row is None:
            raise _bad(
                f'messages[{index}]',
                'invalid_type',
                expected='每条消息是带 role 的对象',
                received=f'收到 {type(item).__name__}',
                next_action='每条消息写成 {"role": "user", "content": "..."}',
            )
        if not isinstance(row.get('role'), str):
            raise _bad(
                f'messages[{index}].role',
                _shape_code(row.get('role'), str),
                expected='每条消息有 role',
                received=f'收到 {row.get("role")!r}',
                next_action='role 用 system、user、assistant 或 tool',
            )
        content = row.get('content')
        messages.append(
            {
                'role': row['role'],
                'content': content if isinstance(content, str) else '',
                'tool_call_id': row.get('tool_call_id'),
            }
        )
    return messages


def _response_input(value: object) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{'role': 'user', 'content': value, 'tool_call_id': None}]
    if not isinstance(value, list) or len(cast(list[object], value)) == 0:
        received = '空列表' if isinstance(value, list) else type(value).__name__
        raise _bad(
            'input',
            _shape_code(cast(object, value), list),
            expected='input 是非空字符串或输入项列表',
            received=f'收到 {received}',
            next_action='传一段文本，或 function_call_output / message 项',
        )
    messages: list[dict[str, Any]] = []
    for index, item in enumerate(cast(list[object], value)):
        row = _as_dict(item)
        if row is None:
            raise _bad(
                f'input[{index}]',
                'invalid_type',
                expected='每个 input 项是对象',
                received=f'收到 {type(item).__name__}',
                next_action='用 message 或 function_call_output',
            )
        if row.get('type') == 'function_call_output':
            messages.append(
                {
                    'role': 'tool',
                    'content': str(row.get('output') or ''),
                    'tool_call_id': str(row.get('call_id') or ''),
                }
            )
            continue
        content = row.get('content')
        if isinstance(content, list):
            texts: list[str] = []
            for part in cast(list[object], content):
                part_row = _as_dict(part)
                if part_row is not None and isinstance(part_row.get('text'), str):
                    texts.append(part_row['text'])
            content = '\n'.join(texts)
        role = row.get('role')
        if not isinstance(role, str):
            raise _bad(
                f'input[{index}].role',
                _shape_code(role, str),
                expected='input 项带 role，或 type=function_call_output',
                received=f'收到 {row.get("type")!r}',
                next_action='文本用 role=user；工具结果用 function_call_output',
            )
        messages.append(
            {
                'role': role,
                'content': content if isinstance(content, str) else '',
                'tool_call_id': row.get('call_id'),
            }
        )
    return messages


def _render(messages: Sequence[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        role = str(message['role'])
        content = str(message.get('content') or '')
        if not content:
            continue
        if role in {'system', 'developer'}:
            parts.append(f'[调用方说明]\n{content}')
        elif role == 'assistant':
            parts.append(f'[此前的助手]\n{content}')
        else:
            parts.append(content)
    return '\n'.join(parts)


def _require_text(value: object, field: str) -> str:
    if isinstance(value, str) and value.strip():
        return value
    raise _bad(
        field,
        _shape_code(value, str),
        expected=f'{field} 是非空字符串',
        received=f'收到 {value!r}',
        next_action=f'把 {field} 设成一个角色名，例如 Worker',
    )


class _Refusal(Exception):
    """交给调用方的错误：HTTP 状态、三件套，与 OpenAI 错误体里的 `code` / `param`。"""

    def __init__(
        self,
        status: int,
        error: ConfigError,
        *,
        code: str | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(status)
        self.status = status
        self.error = error
        self.code = code
        self.param = param


def _bad(
    param: str | None, code: str | None, *, expected: str, received: str, next_action: str
) -> _Refusal:
    return _Refusal(
        400,
        ConfigError(expected=expected, received=received, next_action=next_action),
        code=code,
        param=param,
    )


def _shape_code(value: object, kind: type) -> str:
    """缺了、类型不对、值不对，分别是 OpenAI 的三个 code。"""
    if value is None:
        return 'missing_required_parameter'
    if not isinstance(value, kind):
        return 'invalid_type'
    return 'invalid_value'


def _unknown_session(exchange: _Exchange) -> _Refusal:
    if exchange.previous_response_id is not None:
        return _Refusal(
            404,
            ConfigError(
                expected='previous_response_id 是这个 server 进程交出过的响应 id',
                received=(
                    f'previous_response_id={exchange.previous_response_id!r} 对不上开着的会话'
                ),
                next_action=(
                    '去掉 previous_response_id 重新开始，或用上一次响应的 id；'
                    'server 重启之后旧的 id 作废'
                ),
            ),
            code='previous_response_not_found',
            param='previous_response_id',
        )
    return _Refusal(
        404,
        ConfigError(
            expected='X-Syndica-Session 是这个进程里打开着的会话',
            received=f'没有 {exchange.requested!r}',
            next_action='去掉这个头重新开始，或用上一次响应里的 X-Syndica-Session',
        ),
        code='session_not_found',
    )


def _unexpected(error_type: str) -> ConfigError:
    return ConfigError(
        expected='这一轮完成',
        received=f'抛出 {error_type}',
        next_action='看 server 日志；异常原文不在响应里，POST 不要重放',
    )


def _root_failed(error_type: str) -> ConfigError:
    return ConfigError(
        expected='根 Agent 跑完这一轮',
        received=f'根 Agent 报错 {error_type}，原文不在响应里',
        next_action=(
            '看 server 日志或 trace 里根 Agent 的 agent span 拿到原因；修好之后新发一次请求，'
            '这个 POST 不要原样重放'
        ),
    )


def _missing_model(model: str, roles: Sequence[str]) -> ConfigError:
    return ConfigError(
        expected=f'model 是已有角色之一：{list(roles)}',
        received=f'model={model!r}',
        next_action='改成 GET /v1/models 列出的 id',
    )


def _payload(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get('payload')
    if isinstance(raw, dict):
        return cast(dict[str, Any], raw)
    return {}


def _as_dict(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, Any], value)


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in cast(list[object], value)]


def _sse(payload: Mapping[str, Any], redactor: Redactor) -> str:
    return 'data: ' + json.dumps(redacted(payload, redactor), ensure_ascii=False) + '\n\n'
