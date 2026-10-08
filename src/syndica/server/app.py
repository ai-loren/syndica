"""HTTP / SSE / WebSocket 路由。

处理器只经 server 自己的 Protocol 与内核公开类型做事；交出去的每个载荷都经 `outbound`（先脱敏再
截断）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.parse import quote

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect

from syndica.kernel import Budget, Redactor, SyndicaError
from syndica.server.auth import SECURITY_HEADERS, Credentials, check_host, check_origin
from syndica.server.errors import ApiError, error_body
from syndica.server.live import Live, LiveSession
from syndica.server.models import (
    ArtifactDiff,
    ArtifactList,
    ArtifactPreview,
    ArtifactVersions,
    BudgetRequest,
    CreateSession,
    DoctorReport,
    ErrorBody,
    FeedbackRequest,
    FeedbackResult,
    FlagList,
    Health,
    LoginRequest,
    LoginResponse,
    ResumeSession,
    Roles,
    RpcReply,
    RpcRequest,
    RubricList,
    SessionList,
    SessionSummary,
    Snapshot,
    TopologyList,
    TraceAnalysis,
    TraceList,
)
from syndica.server.outbound import encoded, json_response, sse_frame

__all__ = ['MAX_STREAMS', 'ServerSettings', 'create_app']

logger = logging.getLogger(__name__)

MAX_STREAMS = 16
# 同时在等认证的 WebSocket 上限；与 MAX_STREAMS 分开记，见 `_Handshakes`。
MAX_HANDSHAKES = 32
_AUTH_TIMEOUT_S = 5.0
# Agent 终端镜像里一行攒到这么长还没换行就照样发出去：不能为了等换行无限攒。
_PTY_LINE_CAP = 16_384
_ModelT = TypeVar('_ModelT', bound=BaseModel)
_Handler = Callable[[Request], Awaitable[Response]]


class ServerSettings:
    def __init__(
        self, *, port: int, nonce: str, version: str, static_dir: Path | None = None
    ) -> None:
        self.port = port
        self.nonce = nonce
        self.version = version
        self.static_dir = static_dir


class _Guard:
    """每个请求的 Host 检查与安全响应头。纯 ASGI：不经 BaseHTTPMiddleware，SSE 的流照常一块块走。"""

    def __init__(self, app: ASGIApp, settings: ServerSettings, redactor: Redactor) -> None:
        self._app = app
        self._settings = settings
        self._redactor = redactor

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] not in ('http', 'websocket'):
            await self._app(scope, receive, send)
            return
        headers = {
            key.decode('latin-1'): value.decode('latin-1') for key, value in scope['headers']
        }
        try:
            check_host(headers.get('host'), self._settings.port)
            check_origin(headers.get('origin'), self._settings.port)
        except ApiError as error:
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 4403})
                return
            response = Response(
                encoded(error_body(error), self._redactor),
                status_code=error.status,
                media_type='application/json',
                headers=SECURITY_HEADERS,
            )
            await response(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message['type'] == 'http.response.start':
                existing = {key.lower() for key, _ in message.get('headers', [])}
                extra = [
                    (key.lower().encode(), value.encode())
                    for key, value in SECURITY_HEADERS.items()
                    if key.lower().encode() not in existing
                ]
                message = {**message, 'headers': [*message.get('headers', []), *extra]}
            await send(message)

        await self._app(scope, receive, with_headers)


def create_app(live: Live, credentials: Credentials, settings: ServerSettings) -> ASGIApp:
    host_redactor = live.assembly.redactor()
    streams = _StreamLimit()
    handshakes = _Handshakes()

    def guarded(handler: _Handler, *, public: bool = False) -> _Handler:
        async def run(request: Request) -> Response:
            try:
                if not public:
                    credentials.require(request.headers.get('authorization'))
                return await handler(request)
            except ApiError as error:
                return _error(error, host_redactor)
            except SyndicaError as error:
                return _error(ApiError.of(400, error), host_redactor)
            except Exception:
                logger.exception('期望: 处理器正常返回。 实际: 抛出未预期的异常。 下一步: 看栈。')
                return _error(
                    ApiError(
                        500,
                        expected='server 正常处理这个请求',
                        received='处理时抛出了未预期的异常（细节在 server 日志里）',
                        next_action='看 syndica status 给出的日志文件；问题复现时附上它报告',
                    ),
                    host_redactor,
                )

        return run

    async def health(_: Request) -> Response:
        return json_response(
            Health(nonce=settings.nonce, version=settings.version).model_dump(), host_redactor
        )

    async def login(request: Request) -> Response:
        body = await _body(request, LoginRequest)
        token = credentials.exchange(body.code)
        # 只含 server 刚铸的令牌、不含任何用户数据，不经脱敏器：令牌被改一个字节就用不了。
        return Response(LoginResponse(token=token).model_dump_json(), media_type='application/json')

    async def doctor(_: Request) -> Response:
        checks = live.assembly.doctor()
        report = DoctorReport(ok=all(check.get('ok') for check in checks), checks=checks)  # pyright: ignore[reportArgumentType]
        return json_response(report.model_dump(), host_redactor)

    async def roles(_: Request) -> Response:
        return json_response(Roles(roles=live.assembly.roles()).model_dump(), host_redactor)

    async def list_sessions(_: Request) -> Response:
        stored = {meta['sid']: meta for meta in await live.assembly.list_sessions()}
        rows = [
            SessionSummary.model_validate({**meta, 'live': sid in live.sessions}).model_dump()
            for sid, meta in stored.items()
        ]
        for sid in live.sessions:
            if sid not in stored:
                rows.append(_unsaved(live.sessions[sid]).model_dump())
        return json_response(SessionList(sessions=rows).model_dump(), host_redactor)  # pyright: ignore[reportArgumentType]

    async def create_session(request: Request) -> Response:
        body = await _body(request, CreateSession, empty_ok=True)
        budget = body.budget or BudgetRequest()
        session = await live.open_session(
            resume=None,
            role=body.role,
            tokens=budget.tokens,
            cost=_decimal(budget.cost),
            wall_clock_s=budget.wall_clock_s,
            turns=budget.turns,
        )
        return await _snapshot(session, status=201)

    async def resume_session(request: Request) -> Response:
        sid = request.path_params['sid']
        body = await _body(request, ResumeSession, empty_ok=True)
        if body.budget is not None and sid in live.sessions:
            raise ApiError(
                409,
                expected='带预算恢复的是一个还没打开的会话：预算只在打开时生效',
                received=f'会话 {sid} 已经开着，这次给的预算不会生效',
                next_action=(
                    '不带 budget 再发一次拿到它；要追加用 WebSocket 的 grant，'
                    '要换上限先关掉它再带预算恢复'
                ),
            )
        budget = body.budget or BudgetRequest()
        session = await live.open_session(
            resume=sid,
            role='Orchestrator',
            tokens=budget.tokens,
            cost=_decimal(budget.cost),
            wall_clock_s=budget.wall_clock_s,
            turns=budget.turns,
        )
        return await _snapshot(session)

    async def close_session(request: Request) -> Response:
        await live.close(request.path_params['sid'])
        return Response(status_code=204)

    async def remove_session(request: Request) -> Response:
        sid = request.path_params['sid']
        await live.close(sid)
        await live.assembly.remove_session(sid)
        return Response(status_code=204)

    async def snapshot(request: Request) -> Response:
        return await _snapshot(live.get(request.path_params['sid']))

    async def _snapshot(session: LiveSession, *, status: int = 200) -> Response:
        state = Snapshot.model_validate(await session.snapshot())
        return json_response(state.model_dump(), session.handle.redactor(), status=status)

    async def topologies(_: Request) -> Response:
        listing = TopologyList(topologies=live.assembly.topologies())
        return json_response(listing.model_dump(), host_redactor)

    async def traces(_: Request) -> Response:
        rows = await live.assembly.traces()
        return json_response(TraceList.model_validate({'traces': rows}).model_dump(), host_redactor)

    async def trace(request: Request) -> Response:
        analysis = await live.assembly.trace(request.path_params['trace_id'])
        return json_response(TraceAnalysis.model_validate(analysis).model_dump(), host_redactor)

    async def session_trace(request: Request) -> Response:
        session = live.get(request.path_params['sid'])
        trace_id = session.handle.trace_id()
        if trace_id is None:
            raise ApiError(
                404,
                expected='这个会话在写 trace',
                received='它没有在记录（trace.level 是 off，或 trace 目录写不进去）',
                next_action='在 .syndica.yaml 里把 trace.level 设成 structure 或更高，重启 server',
            )
        analysis = await live.assembly.trace(trace_id)
        return json_response(
            TraceAnalysis.model_validate(analysis).model_dump(), session.handle.redactor()
        )

    async def flags(request: Request) -> Response:
        session = live.get(request.path_params['sid'])
        listing = FlagList.model_validate({'flags': session.handle.flags()})
        return json_response(listing.model_dump(), session.handle.redactor())

    async def artifacts(request: Request) -> Response:
        session = live.get(request.path_params['sid'])
        items = [
            {**item, 'note': session.notes.get(str(item.get('id')), '')}
            for item in await session.handle.artifact_list()
        ]
        listing = ArtifactList.model_validate({'artifacts': items})
        return json_response(listing.model_dump(), session.handle.redactor())

    async def artifact_versions(request: Request) -> Response:
        session = live.get(request.path_params['sid'])
        versions = await session.handle.artifact_versions(_query(request, 'id'))
        listing = ArtifactVersions.model_validate({'versions': versions})
        return json_response(listing.model_dump(), session.handle.redactor())

    async def artifact_content(request: Request) -> Response:
        session = live.get(request.path_params['sid'])
        artifact_id = _query(request, 'id')
        data, mime = await session.handle.artifact_content(artifact_id, _query(request, 'version'))
        name = artifact_id.rsplit(':', 1)[-1].rsplit('/', 1)[-1] or 'artifact'
        # 人取回自己工作区里的文件，同在磁盘上打开它：不脱敏。不可信的字节不能在这个源上执行：
        # 一律当附件、按 octet-stream 交出、沙箱化，登记的 mime 只放在旁边的头里给前端挑显示方式。
        return Response(
            data,
            media_type='application/octet-stream',
            headers={
                'Content-Disposition': f"attachment; filename*=UTF-8''{quote(name)}",
                'Content-Security-Policy': "sandbox; default-src 'none'",
                'X-Syndica-Mime': mime,
            },
        )

    async def artifact_preview(request: Request) -> Response:
        session = live.get(request.path_params['sid'])
        text = await session.handle.artifact_preview(
            _query(request, 'id'), _query(request, 'version')
        )
        return json_response(ArtifactPreview(text=text).model_dump(), session.handle.redactor())

    async def artifact_diff(request: Request) -> Response:
        session = live.get(request.path_params['sid'])
        diff = await session.handle.artifact_diff(
            _query(request, 'id'), _query(request, 'old'), _query(request, 'new')
        )
        return json_response(
            ArtifactDiff.model_validate(diff).model_dump(), session.handle.redactor()
        )

    async def artifact_feedback(request: Request) -> Response:
        session = live.get(request.path_params['sid'])
        body = await _body(request, FeedbackRequest)
        lines = None
        if body.start_line is not None:
            end = body.end_line or body.start_line
            if end < body.start_line:
                raise ApiError(
                    422,
                    expected='end_line 不小于 start_line',
                    received=f'start_line={body.start_line}，end_line={end}',
                    next_action='把行范围写成从小到大',
                )
            lines = (body.start_line, end)
        rubric = await session.handle.artifact_feedback(body.id, body.version, body.text, lines)
        result = FeedbackResult.model_validate({'rubric': rubric})
        return json_response(result.model_dump(), session.handle.redactor(), status=201)

    async def rubrics(request: Request) -> Response:
        session = live.get(request.path_params['sid'])
        listing = RubricList.model_validate(await session.handle.rubrics())
        return json_response(listing.model_dump(), session.handle.redactor())

    async def session_events(request: Request) -> Response:
        session = live.get(request.path_params['sid'])
        return _sse(request, session.stream.epoch, session.stream.follow, session.handle.redactor())

    async def global_events(request: Request) -> Response:
        return _sse(request, live.stream.epoch, live.stream.follow, host_redactor)

    def _sse(
        request: Request,
        epoch: str,
        follow: Callable[[str | None], AsyncIterator[Any]],
        redactor: Redactor,
    ) -> Response:
        streams.take()
        last = request.headers.get('last-event-id') or request.query_params.get('last_event_id')

        async def frames() -> AsyncIterator[str]:
            live.attach()
            try:
                async for item in follow(last):
                    yield sse_frame(epoch, item, redactor)
            finally:
                live.detach()
                streams.give()

        return StreamingResponse(
            frames(),
            media_type='text/event-stream',
            headers={'X-Accel-Buffering': 'no'},
        )

    async def _socket(
        websocket: WebSocket,
        body: Callable[[WebSocket, RpcRequest, LiveSession], Awaitable[None]],
    ) -> None:
        """WebSocket 的共同入口：先认证、再占流的名额。没认证的连接只占握手名额（满了挤掉最早的
        那个），不占人的 16 个：同一用户的进程不带令牌也连得上本机端口，让它们占着人的名额，人就打
        不开事件流与确认通道。"""
        await websocket.accept()
        handshakes.admit(websocket)
        try:
            authenticated = await _authenticated(websocket, websocket.path_params['sid'])
        finally:
            handshakes.leave(websocket)
        if authenticated is None:
            return
        frame, session = authenticated
        if not streams.try_take():
            await _refuse(websocket, 1013, frame.id, streams.refusal(), host_redactor)
            return
        try:
            await body(websocket, frame, session)
        finally:
            streams.give()

    async def rpc(websocket: WebSocket) -> None:
        await _socket(websocket, _rpc_session)

    async def _authenticated(
        websocket: WebSocket, sid: str
    ) -> tuple[RpcRequest, LiveSession] | None:
        """第一帧必须在时限内认证；认证不过、会话没打开都发一帧三件套再关掉连接，交回 None。"""
        try:
            async with asyncio.timeout(_AUTH_TIMEOUT_S):
                first = await _text_frame(websocket)
        except (TimeoutError, WebSocketDisconnect, RuntimeError):
            await _refuse(websocket, 4401, None, _unauthenticated(), host_redactor)
            return None
        frame = None if first is None else _frame(first)
        if frame is None or frame.op != 'auth' or not credentials.valid(_extra(frame).get('token')):
            await _refuse(
                websocket,
                4401,
                None if frame is None else frame.id,
                _unauthenticated(),
                host_redactor,
            )
            return None
        session = live.sessions.get(sid)
        if session is None:
            await _refuse(websocket, 4404, frame.id, _no_session(sid), host_redactor)
            return None
        return frame, session

    async def _rpc_session(websocket: WebSocket, frame: RpcRequest, session: LiveSession) -> None:
        redactor = session.handle.redactor()
        await websocket.send_text(encoded(RpcReply(id=frame.id, ok=True).model_dump(), redactor))
        live.attach()
        try:
            while True:
                try:
                    raw = await _text_frame(websocket)
                except WebSocketDisconnect:
                    return
                reply = _binary_frame() if raw is None else await _dispatch(session, raw)
                await websocket.send_text(encoded(reply.model_dump(), redactor))
        finally:
            live.detach()

    async def human_pty(websocket: WebSocket) -> None:
        await _socket(websocket, _human_pty)

    async def _human_pty(websocket: WebSocket, frame: RpcRequest, session: LiveSession) -> None:
        extra = _extra(frame)
        terminal = session.handle.open_terminal(
            _size(extra.get('cols'), 100), _size(extra.get('rows'), 30)
        )
        live.attach()

        async def output() -> None:
            while chunk := await terminal.read():
                # 人自己的 shell，原样回给本机已认证的人（同终端模拟器）：不脱敏。
                await websocket.send_bytes(chunk)
            await _close(websocket, 1000)

        pumping = asyncio.create_task(output())
        try:
            while True:
                try:
                    text = await _text_frame(websocket)
                    if text is None:
                        continue
                    message = json.loads(text)
                except (WebSocketDisconnect, RuntimeError):
                    return
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                fields = cast('dict[str, Any]', message)
                if fields.get('op') == 'input' and isinstance(fields.get('data'), str):
                    terminal.write(str(fields['data']).encode())
                elif fields.get('op') == 'resize':
                    terminal.resize(_size(fields.get('cols'), 100), _size(fields.get('rows'), 30))
        finally:
            pumping.cancel()
            await terminal.close()
            live.detach()

    async def agent_pty(websocket: WebSocket) -> None:
        await _socket(websocket, _agent_pty)

    async def _agent_pty(websocket: WebSocket, frame: RpcRequest, session: LiveSession) -> None:
        """Agent 终端的只读镜像：没有写入面——第二个写入方会绕过 Terminal 工具「半行不发」的不变式，
        危险判定就看不到整行。按整行脱敏再发：一个密钥可能跨两次读，只脱半截认不出来。"""
        agent_id, handle = websocket.path_params['agent_id'], websocket.path_params['handle']
        redactor = session.handle.redactor()
        live.attach()
        since, pending = 0, ''
        try:
            while True:
                try:
                    screen = await session.handle.agent_screen(agent_id, handle, since, 1.0)
                except SyndicaError as error:
                    body = {'error': error_body(ApiError.of(404, error))}
                    await websocket.send_text(encoded(body, redactor))
                    return
                since = int(screen['end'])
                text = pending + str(screen['text'])
                running = bool(screen['running'])
                cut = text.rfind('\n') + 1
                if not running or len(text) > _PTY_LINE_CAP:
                    cut = len(text)
                ready, pending = text[:cut], text[cut:]
                if ready or not running:
                    piece = {'text': ready, 'running': running, 'exit_code': screen['exit_code']}
                    await websocket.send_text(encoded(piece, redactor))
                if not running:
                    return
        except WebSocketDisconnect:
            return
        finally:
            live.detach()
            await _close(websocket, 1000)

    routes = [
        Route('/api/v1/health', guarded(health, public=True), methods=['GET']),
        Route('/api/v1/login', guarded(login, public=True), methods=['POST']),
        Route('/api/v1/doctor', guarded(doctor), methods=['GET']),
        Route('/api/v1/roles', guarded(roles), methods=['GET']),
        Route('/api/v1/sessions', guarded(list_sessions), methods=['GET']),
        Route('/api/v1/sessions', guarded(create_session), methods=['POST']),
        Route('/api/v1/sessions/{sid}/resume', guarded(resume_session), methods=['POST']),
        Route('/api/v1/sessions/{sid}/close', guarded(close_session), methods=['POST']),
        Route('/api/v1/sessions/{sid}', guarded(remove_session), methods=['DELETE']),
        Route('/api/v1/sessions/{sid}/snapshot', guarded(snapshot), methods=['GET']),
        Route('/api/v1/sessions/{sid}/events', guarded(session_events), methods=['GET']),
        Route('/api/v1/events', guarded(global_events), methods=['GET']),
        Route('/api/v1/topologies', guarded(topologies), methods=['GET']),
        Route('/api/v1/traces', guarded(traces), methods=['GET']),
        Route('/api/v1/traces/{trace_id}', guarded(trace), methods=['GET']),
        Route('/api/v1/sessions/{sid}/trace', guarded(session_trace), methods=['GET']),
        Route('/api/v1/sessions/{sid}/flags', guarded(flags), methods=['GET']),
        Route('/api/v1/sessions/{sid}/artifacts', guarded(artifacts), methods=['GET']),
        Route(
            '/api/v1/sessions/{sid}/artifacts/versions',
            guarded(artifact_versions),
            methods=['GET'],
        ),
        Route(
            '/api/v1/sessions/{sid}/artifacts/content', guarded(artifact_content), methods=['GET']
        ),
        Route(
            '/api/v1/sessions/{sid}/artifacts/preview', guarded(artifact_preview), methods=['GET']
        ),
        Route('/api/v1/sessions/{sid}/artifacts/diff', guarded(artifact_diff), methods=['GET']),
        Route(
            '/api/v1/sessions/{sid}/artifacts/feedback',
            guarded(artifact_feedback),
            methods=['POST'],
        ),
        Route('/api/v1/sessions/{sid}/rubrics', guarded(rubrics), methods=['GET']),
        WebSocketRoute('/api/v1/sessions/{sid}/rpc', rpc),
        WebSocketRoute('/api/v1/sessions/{sid}/pty', human_pty),
        WebSocketRoute('/api/v1/sessions/{sid}/agent-pty/{agent_id}/{handle}', agent_pty),
        Route('/{path:path}', _static(settings.static_dir, host_redactor), methods=['GET']),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            await live.shutdown()

    app = Starlette(routes=routes, lifespan=lifespan)
    return _Guard(app, settings, host_redactor)


class _StreamLimit:
    """已认证的事件流与 WebSocket 的名额。检查与占用在同一次调用里：分成两步、中间隔一个
    await，几个并发的连接就能都过了检查。"""

    def __init__(self) -> None:
        self._open = 0

    def refusal(self) -> ApiError:
        return ApiError(
            429,
            expected=f'同时开着的事件流与 WebSocket 不超过 {MAX_STREAMS} 个',
            received=f'已经开着 {self._open} 个',
            next_action='关掉多余的标签页；每个标签页开一个事件流',
        )

    def try_take(self) -> bool:
        if self._open >= MAX_STREAMS:
            return False
        self._open += 1
        return True

    def take(self) -> None:
        if not self.try_take():
            raise self.refusal()

    def give(self) -> None:
        self._open -= 1


class _Handshakes:
    """还没认证的 WebSocket。满了就关掉最早的那个：认证不需要等，人的页面一连上就发认证帧，
    被挤掉的只会是一直不认证的那些。"""

    def __init__(self) -> None:
        self._waiting: list[WebSocket] = []

    def admit(self, websocket: WebSocket) -> None:
        self._waiting.append(websocket)
        while len(self._waiting) > MAX_HANDSHAKES:
            oldest = self._waiting.pop(0)
            asyncio.get_running_loop().create_task(_close(oldest, 4408))

    def leave(self, websocket: WebSocket) -> None:
        with contextlib.suppress(ValueError):
            self._waiting.remove(websocket)


def _unauthenticated() -> ApiError:
    return ApiError(
        401,
        expected='连接后 5 秒内第一帧是 {"op": "auth", "token": <登录换来的令牌>}',
        received='没有收到认证帧，或令牌不对',
        next_action=(
            '从 syndica up 打印的链接重新打开页面；server 重启过就 syndica down 再 syndica up'
        ),
    )


def _no_session(sid: str) -> ApiError:
    return ApiError(
        404,
        expected=f'会话 {sid} 在这个 server 里开着',
        received='没有开着这个会话',
        next_action='先 POST /api/v1/sessions 或 /resume 打开它，再连接',
    )


async def _refuse(
    websocket: WebSocket,
    code: int,
    frame_id: str | int | None,
    error: ApiError,
    redactor: Redactor,
) -> None:
    """拒绝之前先发一帧三件套：关闭码与 123 字节的关闭原因装不下「期望 / 实际 / 下一步」。"""
    reply = RpcReply(id=frame_id, ok=False, error=ErrorBody.model_validate(error_body(error)))
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await websocket.send_text(encoded(reply.model_dump(), redactor))
    await _close(websocket, code)


async def _text_frame(websocket: WebSocket) -> str | None:
    """下一帧的文本；二进制帧交回 None。Starlette 的 `receive_text` 对二进制帧抛 `KeyError`，没接住
    就是整条连接异常断开，人的确认通道随之断掉。"""
    try:
        return await websocket.receive_text()
    except KeyError:
        return None


def _binary_frame() -> RpcReply:
    body = error_body(
        ApiError(
            422,
            expected='文本帧：JSON 对象 {"op": ..., "id": ..., ...}',
            received='一帧二进制数据',
            next_action='按 OpenAPI 文档 x-syndica-rpc 里的帧格式用文本帧发送',
        )
    )
    return RpcReply(id=None, ok=False, error=ErrorBody.model_validate(body))


async def _dispatch(session: LiveSession, raw: str) -> RpcReply:
    frame = _frame(raw)
    if frame is None:
        return RpcReply(id=None, ok=False, error=ErrorBody.model_validate(_bad_frame(raw)))
    try:
        result = await _run_op(session, frame)
    except ApiError as error:
        return RpcReply(id=frame.id, ok=False, error=ErrorBody.model_validate(error_body(error)))
    except SyndicaError as error:
        body = error_body(ApiError.of(400, error))
        return RpcReply(id=frame.id, ok=False, error=ErrorBody.model_validate(body))
    except Exception:
        # 别的异常（store 抛的 KeyError 之类）不能拆掉整条连接：客户端在等这一帧的回复，人的确认
        # 通道也在这条连接上。细节只进 server 日志（经脱敏 handler），不回显给客户端。
        logger.exception('WebSocket 操作 %s 抛出了未预期的异常', frame.op)
        body = error_body(
            ApiError(
                500,
                expected=f'操作 {frame.op} 完成',
                received='server 内部出错，细节在 server 日志里',
                next_action='重取快照确认状态，再试一次；反复出现就带着 server 日志报告',
            )
        )
        return RpcReply(id=frame.id, ok=False, error=ErrorBody.model_validate(body))
    return RpcReply(id=frame.id, ok=True, result=result)


async def _run_op(session: LiveSession, frame: RpcRequest) -> dict[str, Any] | None:
    extra = _extra(frame)
    handle = session.handle
    if frame.op == 'send':
        target = _text(extra, 'target', default=handle.root_id)
        delivered = await handle.deliver(target, _text(extra, 'text'))
        if not delivered.ok:
            raise ApiError(
                409,
                expected=f'把消息交给 agent {target}',
                received=delivered.reason or '内核没有给出原因',
                next_action='重取会话快照看它现在的状态，或换一个对象',
            )
        return {'message_id': delivered.message_id}
    if frame.op == 'answer':
        await session.answer(_text(extra, 'request_id'), _text(extra, 'text'))
        return None
    if frame.op == 'abort':
        await handle.abort(_text(extra, 'agent_id', default=handle.root_id))
        return None
    if frame.op == 'grant':
        budget = _budget(_model(BudgetRequest, extra.get('budget')))
        view = handle.grant(_text(extra, 'agent_id', default=handle.root_id), budget)
        return {'view': view}
    if frame.op == 'activate_rubric':
        await handle.activate_rubric(_text(extra, 'rubric_id'))
        return None
    if frame.op == 'budget_answer':
        raw = extra.get('budget')
        session.answer_budget(
            _text(extra, 'request_id'),
            None if raw is None else _budget(_model(BudgetRequest, raw)),
        )
        return None
    raise ApiError(
        400,
        expected=(
            '连接认证之后的帧是 send / answer / abort / grant / budget_answer / '
            'activate_rubric 之一'
        ),
        received=f'op={frame.op!r}',
        next_action='换成上面列出的操作',
    )


def _frame(raw: str) -> RpcRequest | None:
    try:
        return RpcRequest.model_validate_json(raw)
    except ValidationError:
        return None


def _extra(frame: RpcRequest) -> dict[str, Any]:
    return dict(frame.model_extra or {})


def _bad_frame(raw: str) -> dict[str, str]:
    return {
        'expected': 'JSON 对象 {"op": ..., "id": ..., ...}，op 是已知操作之一',
        # 不在这里截：截断必须在脱敏之后，而脱敏在出口（`encoded` 先脱敏再按字段截）。
        'received': f'收到 {raw!r}',
        'next_action': '按 OpenAPI 文档 x-syndica-rpc 里的帧格式发送',
    }


def _text(extra: dict[str, Any], key: str, *, default: str | None = None) -> str:
    value = extra.get(key, default)
    if not isinstance(value, str) or not value:
        raise ApiError(
            422,
            expected=f'帧里有非空字符串字段 {key!r}',
            received=f'{key}={value!r}',
            next_action=f'补上 {key} 再发',
        )
    return value


def _model(model: type[_ModelT], value: object) -> _ModelT:
    try:
        return model.model_validate(value)
    except ValidationError as error:
        raise ApiError(
            422,
            expected=f'{model.__name__} 形状的 JSON',
            received='; '.join(
                f'{".".join(str(part) for part in issue["loc"]) or "(根)"}: {issue["msg"]}'
                for issue in error.errors()
            ),
            next_action='按 OpenAPI 文档改好请求体再发',
        ) from error


async def _body(request: Request, model: type[_ModelT], *, empty_ok: bool = False) -> _ModelT:
    raw = await request.body()
    if not raw and empty_ok:
        return model()
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise ApiError(
            400,
            expected=f'请求体是 {model.__name__} 形状的 JSON',
            received='请求体不是合法的 JSON',
            next_action='发 Content-Type: application/json 的请求体',
        ) from error
    return _model(model, value)


def _decimal(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation as error:
        raise ApiError(
            422,
            expected='cost 是十进制金额字符串，例如 "0.50"',
            received=f'cost={raw!r}',
            next_action='写成十进制数字的字符串',
        ) from error
    if not amount.is_finite() or amount <= 0:
        raise ApiError(
            422,
            expected='cost 是大于 0 的有限金额',
            received=f'cost={raw!r}',
            next_action='给一个正数，或省略这一维表示不限',
        )
    return amount


def _budget(request: BudgetRequest) -> Budget:
    return Budget(
        tokens=request.tokens,
        cost=_decimal(request.cost),
        wall_clock_s=request.wall_clock_s,
        turns=request.turns,
    )


def _unsaved(session: LiveSession) -> SessionSummary:
    return SessionSummary(
        sid=session.sid,
        agent='',
        status='active',
        turns=0,
        created_at=0.0,
        updated_at=0.0,
        live=True,
    )


def _error(error: ApiError, redactor: Redactor) -> Response:
    # `received` 常常回显调用方给的值（Host 头、request_id、帧的原文）：同样先脱敏。
    return json_response(error_body(error), redactor, status=error.status)


async def _close(websocket: WebSocket, code: int) -> None:
    with contextlib.suppress(RuntimeError):
        await websocket.close(code=code)


def _static(root: Path | None, redactor: Redactor) -> _Handler:
    async def serve(request: Request) -> Response:
        path = request.path_params.get('path', '')
        if path.startswith('api/'):
            return _error(
                ApiError(
                    404,
                    expected='/api/v1 下已有的路由',
                    received=f'没有 /{path}',
                    next_action='看 docs/reference/openapi.json 里列出的路径',
                ),
                redactor,
            )
        if root is None or not (root / 'index.html').is_file():
            return _error(
                ApiError(
                    503,
                    expected='随包发布的前端构建产物（syndica/server/static/index.html）',
                    received='包里没有前端产物',
                    next_action=(
                        '重新安装 syndica[server]；从源码运行的开发者先跑 scripts/check_web.sh'
                    ),
                ),
                redactor,
            )
        return FileResponse(_static_file(root, path))

    return serve


def _static_file(root: Path, path: str) -> Path:
    """路径解析后必须仍在产物目录里：`..` 与符号链接都不能把请求带到包外的文件。"""
    candidate = (root / path).resolve()
    if path and candidate.is_file() and candidate.is_relative_to(root.resolve()):
        return candidate
    return root / 'index.html'


def _query(request: Request, name: str) -> str:
    value = request.query_params.get(name)
    if not value:
        raise ApiError(
            422,
            expected=f'查询参数 {name!r} 非空',
            received=f'{name}={value!r}',
            next_action=f'在地址里带上 ?{name}=…',
        )
    return value


def _size(value: object, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 1000:
        return value
    return default
