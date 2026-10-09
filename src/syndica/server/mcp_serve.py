"""把 Syndica 的工具、产物和角色暴露成 MCP server。

`tools/call` 只经 `call_external`：判定、守卫和不可信溯源与会话里的工具调用同一套。处理器接住
异常，不把原文交回旧协议的错误码 0。日志走 logging，不写 stdout——stdio 上 stdout 只放协议。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote

import jsonschema
import mcp_types as types
from mcp.server.lowlevel.server import Server
from mcp.shared.exceptions import MCPError
from starlette.types import ASGIApp, Receive, Scope, Send

from syndica.kernel import (
    ArtifactRef,
    ConfigError,
    ExternalCallPolicy,
    Redactor,
    RuleRedactor,
    Runtime,
    SpanStatus,
    Tool,
    ToolResult,
    TraceLevel,
    TraceRecorder,
    TraceSource,
    TraceStore,
    TrustLevel,
    WorkspaceRouter,
    call_external,
)

__all__ = [
    'McpPrompt',
    'McpResource',
    'McpTraces',
    'build_mcp_server',
    'read_artifact_bytes',
    'resources_of',
    'select_serve_tools',
    'serve_stdio',
    'streamable_http',
]

logger = logging.getLogger(__name__)

# 与 MCP client 同一张表。多出来的版本要先改这两处，不能跟着 SDK 协商时自动放宽。
ACCEPTED_PROTOCOL_VERSIONS = (
    '2024-11-05',
    '2025-03-26',
    '2025-06-18',
    '2025-11-25',
    '2026-07-28',
)


class McpPrompt:
    """一个角色提示。`text` 是 system prompt 正文。"""

    def __init__(self, name: str, description: str, text: str) -> None:
        self.name = name
        self.description = description
        self.text = text


class McpResource:
    """一个产物。`text` 为 None 表示二进制：读回只给元数据，不给字节。"""

    def __init__(self, uri: str, name: str, *, text: str | None, mime: str = 'text/plain') -> None:
        self.uri = uri
        self.name = name
        self.text = text
        self.mime = mime


ResourceSource = Callable[[], Awaitable[Sequence[McpResource]]]


def artifact_uri(artifact_id: str) -> str:
    """`syndica://artifact/<id>`。id 里的 `:` 要百分号编码，否则读回时对不上。"""
    return 'syndica://artifact/' + quote(artifact_id, safe='')


def _text_mime(mime: str) -> bool:
    value = mime.lower()
    return (
        value.startswith('text/')
        or value in {'application/json', 'application/xml'}
        or value.endswith('+json')
        or value.endswith('+xml')
    )


async def read_artifact_bytes(router: WorkspaceRouter, ref: ArtifactRef) -> bytes:
    """读这一版的原文。文件还是这一版就直接读；否则从版本层取。不把读记成「已经看过」。"""
    data = await router.portal().read_bytes(ref.path)
    if f'sha256:{hashlib.sha256(data).hexdigest()}' == ref.version:
        return data
    versions = router.versions
    if versions is None:
        raise ConfigError(
            expected=f'{ref.path} 仍是版本 {ref.version}',
            received='文件已经不是这一版，而工作区没开版本层',
            next_action='在 .syndica.yaml 里打开 workspace.versions，新写的版本才取得回来',
        )
    payload = cast(object, versions.content(ref.path, ref.version))
    if not isinstance(payload, bytes):
        raise ConfigError(
            expected=f'{ref.path} 的版本 {ref.version} 是字节',
            received=f'收到 {type(payload).__name__}',
            next_action='版本层应交出这一版的原文',
        )
    return payload


async def resources_of(
    refs: Sequence[ArtifactRef],
    read_bytes: Callable[[ArtifactRef], Awaitable[bytes]],
) -> tuple[McpResource, ...]:
    """产物变成 MCP resource。文本带正文，二进制只留 mime；读失败不把异常原文放进资源。"""
    items: list[McpResource] = []
    for ref in refs:
        uri = artifact_uri(ref.id)
        name = ref.path or ref.id
        mime = ref.mime or 'application/octet-stream'
        if not _text_mime(mime):
            items.append(McpResource(uri, name, text=None, mime=mime))
            continue
        try:
            text = (await read_bytes(ref)).decode('utf-8')
        except UnicodeDecodeError:
            items.append(McpResource(uri, name, text=None, mime=mime))
            continue
        except Exception:
            logger.exception('mcp artifact unreadable')
            items.append(
                McpResource(
                    uri,
                    name,
                    text=(
                        f'期望读到产物 {ref.id} 的这一版。实际读的时候失败了，原文不回显。'
                        '下一步：看 server 日志。'
                    ),
                    mime='text/plain',
                )
            )
            continue
        items.append(McpResource(uri, name, text=text, mime=mime))
    return tuple(items)


def select_serve_tools(available: Mapping[str, Tool], names: Sequence[str]) -> dict[str, Tool]:
    """按配置点名。点了不存在的工具就停，不静默少暴露一个。"""
    missing = [name for name in names if name not in available]
    if missing:
        raise ConfigError(
            expected=f'interop.mcp.serve.tools 里的名字都是已装上的本地工具：{sorted(available)}',
            received=f'没有 {missing}',
            next_action='改成 Read、Grep、Glob，或 Write、Bash 这类已经装上的工具名',
        )
    return {name: available[name] for name in names}


class McpTraces:
    """一条连接一条 structure trace。工具 span 记在传给 `call_external` 的那个 Runtime 上。

    HTTP 用 `mcp-session-id` 当键。stdio 和进程内客户端没有这个头，整次服务共用 `process`：
    低层传输可能每次请求换一个会话对象，按对象 id 会把同一次连接拆成多条 trace。
    """

    def __init__(
        self, store: TraceStore, redactor: RuleRedactor, workspace: WorkspaceRouter
    ) -> None:
        if not isinstance(cast(object, redactor), RuleRedactor):
            raise ConfigError(
                expected='McpTraces 的 redactor 是 RuleRedactor',
                received=f'收到 {type(redactor).__name__}',
                next_action='用 host.redactor_for(config) 或 RuleRedactor()',
            )
        self._store = store
        self._redactor = redactor
        self._workspace = workspace
        self._lock = asyncio.Lock()
        self._open: dict[str, tuple[Runtime, TraceRecorder, object]] = {}

    def redactor(self) -> RuleRedactor:
        return self._redactor

    async def runtime_for(self, ctx: object) -> Runtime:
        key = _trace_key(ctx)
        async with self._lock:
            found = self._open.get(key)
            if found is not None:
                return found[0]
            recorder = TraceRecorder(
                self._store,
                self._redactor,
                level=TraceLevel.STRUCTURE,
                source=TraceSource(argv=('syndica', 'mcp')),
            )
            await recorder.begin()
            runtime = Runtime(tracer=recorder, workspace=self._workspace, redactor=self._redactor)
            self._open[key] = (runtime, recorder, getattr(ctx, 'session', None))
            return runtime

    async def finish(self, key: str) -> None:
        async with self._lock:
            found = self._open.pop(key, None)
        if found is not None:
            await _close_trace(found[0], found[1])

    async def aclose(self) -> None:
        async with self._lock:
            found = tuple(self._open.values())
            self._open.clear()
        for runtime, recorder, _session in found:
            await _close_trace(runtime, recorder)


def build_mcp_server(
    tools: Mapping[str, Tool],
    *,
    policy: ExternalCallPolicy,
    read_only: frozenset[str],
    runtime: Runtime | None = None,
    redactor: Redactor | None = None,
    prompts: Sequence[McpPrompt] = (),
    resources: Sequence[McpResource] = (),
    resource_source: ResourceSource | None = None,
    traces: McpTraces | None = None,
) -> Server[Any]:
    """按给定工具表建低层 Server。`read_only` 里的名字标 `readOnlyHint`。

    `resource_source` 每次 list / read 都重取：产物在服务期间会增加。没给就用 `resources` 这份快照。
    """
    owned = dict(tools)
    prompt_list = tuple(prompts)
    snapshot = tuple(resources)
    scrub = redactor

    async def current_resources() -> tuple[McpResource, ...]:
        if resource_source is None:
            return snapshot
        try:
            return tuple(await resource_source())
        except Exception:
            logger.exception('mcp resources did not load')
            return ()

    def input_schema(name: str) -> dict[str, Any]:
        tool = owned.get(name)
        if tool is None:
            return {'type': 'object'}
        parameters = cast(object, tool.schema.get('parameters'))
        if isinstance(parameters, dict):
            return cast(dict[str, Any], parameters)
        return {'type': 'object'}

    async def on_list_tools(ctx: object, params: object) -> types.ListToolsResult:
        del params
        await _touch_trace(traces, ctx)
        try:
            listed: list[types.Tool] = []
            for name, tool in owned.items():
                schema = input_schema(name)
                listed.append(
                    types.Tool(
                        name=name,
                        description=tool.description,
                        input_schema=schema,
                        annotations=types.ToolAnnotations(
                            read_only_hint=name in read_only,
                            destructive_hint=name not in read_only,
                        ),
                    )
                )
            return types.ListToolsResult(tools=listed)
        except Exception as exc:
            raise _handler_failed('tools/list', exc) from None

    async def on_call_tool(ctx: object, params: object) -> types.CallToolResult:
        try:
            name = getattr(params, 'name', '')
            tool = owned.get(name) if isinstance(name, str) else None
            if tool is None:
                return _error(
                    f'期望 tools/call 的名字是已列出的工具之一：{", ".join(owned)}。'
                    f'实际收到 {name!r}。'
                    '下一步：先 tools/list，再用列出的名字调用。'
                )
            raw_args = cast(object, getattr(params, 'arguments', None))
            if raw_args is None:
                arguments: dict[str, object] = {}
            elif isinstance(raw_args, dict):
                arguments = cast(dict[str, object], raw_args)
            else:
                return _error(
                    f'期望 {name} 的 arguments 是 JSON 对象。'
                    f'实际收到 {type(raw_args).__name__}。'
                    '下一步：按 inputSchema 传一个对象。'
                )
            schema = input_schema(name)
            try:
                jsonschema.validate(instance=arguments, schema=schema)
            except jsonschema.ValidationError as exc:
                return _error(
                    f'期望实参符合 {name} 的 inputSchema。'
                    f'实际 {exc.message}。'
                    f'下一步：按 inputSchema 改参数后再次调用 {name}。'
                )
            active = runtime if traces is None else await traces.runtime_for(ctx)
            result = await call_external(
                active,
                tool,
                arguments,
                caller=f'mcp-client:{_client_name(ctx)}',
                agent_id=None,
                policy=policy,
                provenance=TrustLevel.UNTRUSTED,
            )
            return _from_result(result, scrub)
        except Exception as exc:
            logger.exception('mcp tools/call failed')
            return _error(
                f'期望 tools/call 正常返回。'
                f'实际处理时抛出了 {type(exc).__name__}，原文不回显。'
                '下一步：看 server 的 stderr 日志。'
            )

    async def on_list_resources(ctx: object, params: object) -> types.ListResourcesResult:
        del params
        await _touch_trace(traces, ctx)
        try:
            items = await current_resources()
            return types.ListResourcesResult(
                resources=[
                    types.Resource(
                        uri=item.uri,
                        name=item.name,
                        mime_type=(
                            item.mime if item.text is not None else 'application/octet-stream'
                        ),
                    )
                    for item in items
                ]
            )
        except Exception:
            logger.exception('mcp resources/list failed')
            return types.ListResourcesResult(resources=[])

    async def on_read_resource(ctx: object, params: object) -> types.ReadResourceResult:
        del ctx
        uri = str(getattr(params, 'uri', ''))
        try:
            items = await current_resources()
            item = next((one for one in items if one.uri == uri), None)
            if item is None:
                body = f'期望 uri 是已列出的产物之一。实际收到 {uri!r}。下一步：先 resources/list。'
                mime = 'text/plain'
            elif item.text is None:
                body = f'[binary omitted mime={item.mime}]'
                mime = item.mime
            else:
                body = item.text if scrub is None else scrub.redact(item.text)
                mime = item.mime
        except Exception:
            logger.exception('mcp resources/read failed')
            body = '期望读到这个产物。实际读的时候失败了，原文不回显。下一步：看 server 日志。'
            mime = 'text/plain'
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=uri, mime_type=mime, text=body)]
        )

    async def on_list_prompts(ctx: object, params: object) -> types.ListPromptsResult:
        del ctx, params
        try:
            return types.ListPromptsResult(
                prompts=[
                    types.Prompt(name=item.name, description=item.description)
                    for item in prompt_list
                ]
            )
        except Exception as exc:
            raise _handler_failed('prompts/list', exc) from None

    async def on_get_prompt(ctx: object, params: object) -> types.GetPromptResult:
        del ctx
        try:
            name = str(getattr(params, 'name', ''))
            found = next((item for item in prompt_list if item.name == name), None)
            if found is None:
                text = (
                    f'期望 prompt 名字是已列出的角色之一。实际收到 {name!r}。'
                    '下一步：先 prompts/list。'
                )
            else:
                text = found.text if scrub is None else scrub.redact(found.text)
            return types.GetPromptResult(
                messages=[
                    types.PromptMessage(
                        role='user', content=types.TextContent(type='text', text=text)
                    )
                ]
            )
        except Exception as exc:
            raise _handler_failed('prompts/get', exc) from None

    return Server(
        'syndica',
        get_tool_input_schema=input_schema,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
        on_list_prompts=on_list_prompts,
        on_get_prompt=on_get_prompt,
    )


async def serve_stdio(server: Server[Any]) -> None:
    """在当前进程的 stdin/stdout 上跑。调用方要先把日志指到 stderr。"""
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def streamable_http(
    server: Server[Any],
    *,
    port: int,
    traces: McpTraces | None = None,
) -> tuple[ASGIApp, Callable[[], AbstractAsyncContextManager[None]]]:
    """Streamable HTTP。`lifespan` 必须在请求前进入：会话管理器只许 `run` 一次。

    Host 允许表与 `_Guard` 同一对回环地址。每个 POST 的版本（`initialize` 的 `protocolVersion`、
    `MCP-Protocol-Version` 头）在交给 SDK 之前按接受表核对。`DELETE` 带着 `mcp-session-id` 时结束
    那一条 trace。
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings

    given = cast(object, port)
    if isinstance(given, bool) or not isinstance(given, int) or not 0 < given < 65536:
        raise ConfigError(
            expected='streamable_http 的 port 是 1 到 65535 的整数',
            received=f'收到 {port!r}',
            next_action='传入 syndica up 实际绑上的端口',
        )
    hosts = [f'127.0.0.1:{port}', f'localhost:{port}']
    manager = StreamableHTTPSessionManager(
        app=server,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[f'http://{host}' for host in hosts],
        ),
    )

    class _App:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope.get('type') == 'http' and scope.get('method') == 'POST':
                messages, body = await _buffer_request(receive)
                rejection = _unsupported_version(body) or _unsupported_header(scope, body)
                if rejection is not None:
                    active = traces.redactor() if traces is not None else RuleRedactor()
                    await _reject_version(send, rejection, active)
                    return
                receive = _replay(messages, receive)
            await manager.handle_request(scope, receive, send)
            if traces is None or scope.get('method') != 'DELETE':
                return
            session_id = _header(scope, b'mcp-session-id')
            if session_id:
                await traces.finish(session_id)

    @contextlib.asynccontextmanager
    async def lifespan() -> AsyncGenerator[None, None]:
        async with manager.run():
            try:
                yield
            finally:
                if traces is not None:
                    await traces.aclose()

    return _App(), lifespan


def _handler_failed(method: str, exc: Exception) -> MCPError:
    """换成三件套的 JSON-RPC 错误。SDK 只原样交出 MCPError，别的异常它连原文一起回给对端。"""
    logger.exception('mcp %s failed', method)
    expected = f'{method} 正常返回'
    received = f'处理时抛出了 {type(exc).__name__}，原文不回显'
    next_action = '看 server 日志'
    return MCPError(
        code=types.INTERNAL_ERROR,
        message=f'期望 {expected}。实际{received}。下一步：{next_action}。',
        data={'expected': expected, 'received': received, 'next_action': next_action},
    )


def _client_name(ctx: object) -> str:
    session = getattr(ctx, 'session', None)
    params = getattr(session, 'client_params', None)
    info = getattr(params, 'clientInfo', None)
    if info is None:
        info = getattr(params, 'client_info', None)
    name = getattr(info, 'name', None)
    return name if isinstance(name, str) and name else 'unknown'


def _from_result(result: ToolResult, redactor: Redactor | None) -> types.CallToolResult:
    text = result.output if isinstance(result.output, str) else str(result.output)
    if redactor is not None:
        text = redactor.redact(text)
    refused = '没有执行' in text or '已被阻断' in text
    return types.CallToolResult(
        content=[types.TextContent(type='text', text=text)],
        is_error=refused,
        _meta={'trust': result.trust.value},
    )


def _trace_key(ctx: object) -> str:
    request = getattr(ctx, 'request', None)
    headers = getattr(request, 'headers', None)
    if headers is not None:
        value = headers.get('mcp-session-id')
        if isinstance(value, str) and value:
            return value
    return 'process'


async def _touch_trace(traces: McpTraces | None, ctx: object) -> None:
    if traces is None:
        return
    try:
        await traces.runtime_for(ctx)
    except Exception:
        logger.exception('mcp trace did not open')


async def _close_trace(runtime: Runtime, recorder: TraceRecorder) -> None:
    try:
        await runtime.shutdown()
    finally:
        await recorder.finish(SpanStatus.OK)


async def _buffer_request(receive: Receive) -> tuple[list[dict[str, Any]], bytes]:
    messages: list[dict[str, Any]] = []
    body = b''
    while True:
        message = cast(dict[str, Any], await receive())
        messages.append(message)
        if message.get('type') != 'http.request':
            break
        chunk = message.get('body') or b''
        if isinstance(chunk, bytes):
            body += chunk
        if not message.get('more_body', False):
            break
    return messages, body


def _replay(messages: list[dict[str, Any]], receive: Receive) -> Receive:
    index = 0

    async def inner() -> dict[str, Any]:
        nonlocal index
        if index < len(messages):
            message = messages[index]
            index += 1
            return message
        return cast(dict[str, Any], await receive())

    return inner


@dataclass(frozen=True)
class _VersionRejection:
    id: object
    requested: str
    field: str
    next_action: str
    status: int


# MCP 规范：HTTP 请求没带 MCP-Protocol-Version 头、又无从得知版本时，服务端按 2025-03-26 处理。
_ABSENT_HEADER_VERSION = '2025-03-26'


def _json_object(body: bytes) -> dict[str, object] | None:
    try:
        payload = cast(object, json.loads(body))
    except (json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return cast(dict[str, object], payload)


def _unsupported_version(body: bytes) -> _VersionRejection | None:
    """SDK 会把不认识的 initialize 版本协商成它自己的一版。这里先拒绝。"""
    message = _json_object(body)
    if message is None or message.get('method') != 'initialize':
        return None
    params = message.get('params')
    if isinstance(params, dict):
        requested = cast(dict[str, object], params).get('protocolVersion')
    else:
        requested = None
    if isinstance(requested, str) and requested in ACCEPTED_PROTOCOL_VERSIONS:
        return None
    return _VersionRejection(
        id=message.get('id'),
        requested=requested if isinstance(requested, str) else repr(requested),
        field='protocolVersion',
        next_action='改成 supported 里的一个版本再 initialize',
        status=200,
    )


def _unsupported_header(scope: Scope, body: bytes) -> _VersionRejection | None:
    """SDK 把接受表之外的头交给新时代的入口自己分类，不按这张表拒绝。这里先拒绝。"""
    raw = _header(scope, b'mcp-protocol-version')
    requested = _ABSENT_HEADER_VERSION if raw is None else raw
    if requested in ACCEPTED_PROTOCOL_VERSIONS:
        return None
    message = _json_object(body)
    return _VersionRejection(
        id=None if message is None else message.get('id'),
        requested=requested,
        field='MCP-Protocol-Version',
        next_action=(
            '把 MCP-Protocol-Version 头改成 supported 里的一个版本，'
            f'或省略它（按 {_ABSENT_HEADER_VERSION} 处理）'
        ),
        status=400,
    )


async def _reject_version(send: Send, rejection: _VersionRejection, redactor: RuleRedactor) -> None:
    supported = list(ACCEPTED_PROTOCOL_VERSIONS)
    requested = redactor.redact(rejection.requested)
    encoded = json.dumps(
        {
            'jsonrpc': '2.0',
            'id': rejection.id,
            'error': {
                'code': -32022,
                'message': 'Unsupported protocol version',
                'data': {
                    'supported': supported,
                    'requested': requested,
                    'expected': f'{rejection.field} 是 ' + '、'.join(supported),
                    'received': requested,
                    'next_action': rejection.next_action,
                },
            },
        }
    ).encode()
    await send(
        {
            'type': 'http.response.start',
            'status': rejection.status,
            'headers': [
                (b'content-type', b'application/json'),
                (b'content-length', str(len(encoded)).encode()),
            ],
        }
    )
    await send({'type': 'http.response.body', 'body': encoded})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get('headers', []):
        if key.lower() == name:
            return value.decode('latin-1')
    return None


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type='text', text=text)],
        is_error=True,
    )
