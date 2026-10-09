"""把一个 ACP Agent 当成 Syndica 的模型。

每个 Syndica Agent 第一次 `create` 时握手并 `session/new`，拆除时收掉进程组。读、写、终端都经
`call_external`，调用方是 `acp-agent:<名字>`。回答一律不可信。协议版本不是 1 就拒绝。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import shlex
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

from acp.exceptions import RequestError
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    CreateTerminalResponse,
    DeniedOutcome,
    EnvVariable,
    FileSystemCapabilities,
    PermissionOption,
    ReadTextFileResponse,
    RequestPermissionResponse,
    TerminalExitStatus,
    TerminalOutputResponse,
    TextContentBlock,
    ToolCallUpdate,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

from syndica.backends.acp.process import ExternalProcess
from syndica.kernel import (
    ConfigError,
    ConfirmationOutcome,
    ConfirmationRequest,
    ContextBlock,
    DangerVerdict,
    Event,
    ExternalCallPolicy,
    LLMConfig,
    RiskLevel,
    Runtime,
    SyndicaError,
    Tool,
    ToolResult,
    TrustLevel,
    call_external,
)
from syndica.kernel.call_limit import BudgetDeadline, model_call_limit, wall_seconds_left
from syndica.kernel.driving import driving_agent
from syndica.kernel.redaction import Redactor, looks_secret

__all__ = ['AcpAgentModel']

logger = logging.getLogger(__name__)

AcpConnection = Callable[[object], AbstractAsyncContextManager[Any]]
_ALLOW_KINDS = frozenset({'read', 'search', 'think'})
_TOOL_NAMES = ('Read', 'Write', 'Bash', 'BashOutput', 'KillBash')
_CANCEL_WAIT_S = 0.2
_POLL_S = 0.05


class AcpAgentModel:
    """一个模型名对应的 ACP 托管。`connect` 与 `command` 给一个：测试注入连接，正式路径拉起进程。"""

    def __init__(
        self,
        name: str,
        *,
        connect: AcpConnection | None = None,
        command: tuple[str, ...] | None = None,
        tools: Mapping[str, Tool] | None = None,
        policy: ExternalCallPolicy | None = None,
        cwd: str = '',
        runtime: Runtime | None = None,
        redactor: Redactor | None = None,
        env: Mapping[str, str] | None = None,
        pass_env: tuple[str, ...] = (),
        timeout: float = 60.0,
    ) -> None:
        if (connect is None) == (command is None):
            raise ConfigError(
                expected='AcpAgentModel 要么拿到 connect，要么拿到 command',
                received=f'connect={"有" if connect is not None else "无"}，'
                f'command={"有" if command is not None else "无"}',
                next_action='测试注入 connect；正式路径传 command 字符串列表，不要两个都传',
            )
        self._name = name
        self._connect = connect
        self._command = command
        self._tools = dict(tools or {})
        self._policy = policy
        self._cwd = cwd
        self._runtime = runtime
        self._redactor = redactor
        self._env = dict(env or {})
        self._extra_env = pass_env
        self._config = LLMConfig(provider='acp', model=name, timeout=timeout)
        self._sessions: dict[str, _Live] = {}

    @property
    def config(self) -> LLMConfig:
        return self._config

    def bind_runtime(
        self,
        runtime: Runtime,
        tools: Mapping[str, Tool],
        policy: ExternalCallPolicy,
        *,
        agent_env: Mapping[str, str],
        host_env: Mapping[str, str],
        redactor: Redactor,
    ) -> None:
        """宿主在 Runtime 建好之后把工具、处置和 Agent 环境交进来。进程要等第一次 create 才拉起。"""
        missing = [name for name in _TOOL_NAMES if name not in tools]
        if missing:
            raise ConfigError(
                expected=f'ACP 回调能用的工具里有 {", ".join(_TOOL_NAMES)}',
                received=f'缺 {missing!r}',
                next_action='用 local_tools 装上 Read、Write、Bash、BashOutput、KillBash',
            )
        env = dict(agent_env)
        for key in self._extra_env:
            value = host_env.get(key)
            if value is not None:
                env[key] = value
        self._runtime = runtime
        self._tools = {name: tools[name] for name in _TOOL_NAMES}
        self._policy = policy
        self._env = env
        self._redactor = redactor
        workspace = runtime.workspace
        if workspace is not None:
            self._cwd = workspace.root

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        del kwargs
        self._require_bound()
        agent_id = driving_agent() or ''
        live = await self._open(agent_id)
        text = _fresh_prompt(messages, live.cursor)
        live.client.chunks.clear()
        try:
            await self._bounded(live, text)
        except TimeoutError:
            await self._drop(agent_id)
            raise BudgetDeadline() from None
        except asyncio.CancelledError:
            await self._survive(agent_id)
            raise
        except SyndicaError:
            raise
        except Exception as exc:
            await self._drop(agent_id)
            raise ConfigError(
                expected=f'{self._name} 把这次提示答完',
                received=f'连接中断：{type(exc).__name__}',
                next_action=f'看 {self._name} 的 stderr；不会自动重连，重新开会话再试',
            ) from None
        live.cursor = len(messages)
        reply = self._redact(''.join(live.client.chunks))
        return ContextBlock(role='assistant', content=reply, trust=TrustLevel.UNTRUSTED)

    async def aclose(self) -> None:
        for agent_id in list(self._sessions):
            await self._drop(agent_id)

    def _require_bound(self) -> None:
        if self._policy is None or not self._tools:
            raise ConfigError(
                expected='create 之前已经 bind_runtime，或构造时就给了 tools 与 policy',
                received=f'{self._name} 还没有工具或处置',
                next_action='等宿主把 Runtime 交进来再调用；单测直接把 tools 和 policy 传进构造',
            )

    async def _open(self, agent_id: str) -> _Live:
        live = self._sessions.get(agent_id)
        if live is not None:
            return live
        client = _Client(self, agent_id)
        opener = self._connect if self._connect is not None else self._spawn
        cm = opener(client)
        try:
            conn = await cm.__aenter__()
        except BaseException:
            await cm.__aexit__(None, None, None)
            raise
        live = _Live(cm, conn, client)
        self._sessions[agent_id] = live
        try:
            await self._handshake(live, agent_id)
        except BaseException:
            await self._drop(agent_id)
            raise
        return live

    async def _handshake(self, live: _Live, agent_id: str) -> None:
        initialized = await live.conn.initialize(
            protocol_version=1,
            client_capabilities=ClientCapabilities(
                fs=FileSystemCapabilities(read_text_file=True, write_text_file=True),
                terminal=True,
            ),
        )
        version = getattr(initialized, 'protocol_version', None)
        if type(version) is not int or version != 1:
            raise ConfigError(
                expected='ACP Agent 协商到协议版本 1',
                received=f'对端给出 protocolVersion={version!r}',
                next_action=f'换一个只说版本 1 的 ACP Agent，或检查 {self._name} 的启动命令',
            )
        opened = await live.conn.new_session(cwd=self._cwd_for(agent_id))
        live.session_id = str(opened.session_id)

    async def _bounded(self, live: _Live, text: str) -> None:
        limit = model_call_limit()
        if limit is not None and limit <= 0:
            raise BudgetDeadline()
        prompt = live.conn.prompt(session_id=live.session_id, prompt=_text_blocks(text))
        if limit is None:
            await prompt
            return
        async with asyncio.timeout(limit):
            await prompt

    async def _survive(self, agent_id: str) -> None:
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling():
                task.uncancel()
        await self._drop(agent_id)

    async def _drop(self, agent_id: str) -> None:
        live = self._sessions.pop(agent_id, None)
        if live is None:
            return
        cancel = getattr(live.conn, 'cancel', None)
        if cancel is not None and live.session_id:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(cancel(session_id=live.session_id), _CANCEL_WAIT_S)
        await live.cm.__aexit__(None, None, None)

    def _cwd_for(self, agent_id: str) -> str:
        runtime = self._runtime
        workspace = None if runtime is None else runtime.workspace
        if workspace is None:
            return self._cwd
        if agent_id:
            isolated = workspace.isolated(agent_id)
            if isolated is not None:
                return str(isolated.location)
        return workspace.root

    @asynccontextmanager
    async def _spawn(self, client: object) -> AsyncGenerator[Any]:
        if self._command is None or self._redactor is None:
            raise ConfigError(
                expected='拉起 ACP Agent 时 command 与 redactor 都在',
                received=f'command={self._command!r}，redactor={self._redactor!r}',
                next_action='bind_runtime 会交上 redactor；不要在没脱敏器时拉起外部进程',
            )
        from acp.client.connection import ClientSideConnection

        process = ExternalProcess(
            self._command,
            env=self._env,
            cwd=self._cwd or '.',
            redactor=self._redactor,
        )
        await process.start()
        writer, reader = process.streams()
        conn = ClientSideConnection(client, writer, reader)  # pyright: ignore[reportArgumentType]
        try:
            yield conn
        finally:
            closer = getattr(conn, 'close', None)
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer()
            await process.aclose()

    async def _call(self, agent_id: str, tool_name: str, args: Mapping[str, object]) -> ToolResult:
        tool = self._tools.get(tool_name)
        policy = self._policy
        if tool is None or policy is None:
            raise ConfigError(
                expected=f'ACP 回调能用的工具里有 {tool_name}',
                received=f'没有 {tool_name}',
                next_action='给 AcpAgentModel 装上 Read、Write、Bash、BashOutput 和 KillBash',
            )
        return await call_external(
            self._runtime,
            tool,
            args,
            caller=f'acp-agent:{self._name}',
            agent_id=agent_id or None,
            policy=policy,
            provenance=TrustLevel.UNTRUSTED,
        )

    async def _activity(self, agent_id: str, update: object) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        kind = str(getattr(update, 'session_update', ''))
        title = str(getattr(update, 'title', '') or getattr(update, 'kind', '') or kind)
        name = str(getattr(update, 'tool_call_id', '') or kind)
        await runtime.events.emit(
            Event(
                'external_activity',
                agent_id,
                {
                    'source': f'acp:{self._name}',
                    'name': name,
                    'activity_id': name,
                    'kind': kind,
                    'status': str(getattr(update, 'status', '') or 'update'),
                    'title': self._redact(title)[:200],
                },
            )
        )

    def _redact(self, text: str) -> str:
        if self._redactor is None:
            return text
        return self._redactor.redact(text)


class _Live:
    def __init__(self, cm: AbstractAsyncContextManager[Any], conn: Any, client: _Client) -> None:
        self.cm = cm
        self.conn = conn
        self.client = client
        self.session_id = ''
        self.cursor = 0
        self.terminals: dict[str, _Terminal] = {}


class _Terminal:
    def __init__(self, handle: str, limit: int | None) -> None:
        self.handle = handle
        self.limit = limit


class _Client:
    def __init__(self, model: AcpAgentModel, agent_id: str) -> None:
        self._model = model
        self._agent_id = agent_id
        self.chunks: list[str] = []

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        del session_id, kwargs
        kind = getattr(update, 'session_update', None)
        if kind == 'agent_message_chunk':
            content = getattr(update, 'content', None)
            text = getattr(content, 'text', '')
            if isinstance(text, str) and text:
                self.chunks.append(text)
            return
        if kind in {'tool_call', 'tool_call_update', 'plan'}:
            await self._model._activity(self._agent_id, update)  # pyright: ignore[reportPrivateUsage]

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: object,
    ) -> ReadTextFileResponse:
        del session_id, kwargs
        args: dict[str, object] = {'path': path}
        if line is not None:
            args['offset'] = line
        if limit is not None:
            args['limit'] = limit
        result = await self._model._call(self._agent_id, 'Read', args)  # pyright: ignore[reportPrivateUsage]
        return ReadTextFileResponse(content=_text(result))

    async def write_text_file(
        self, session_id: str, path: str, content: str, **kwargs: object
    ) -> WriteTextFileResponse:
        del session_id, kwargs
        await self._model._call(  # pyright: ignore[reportPrivateUsage]
            self._agent_id, 'Write', {'path': path, 'content': content}
        )
        return WriteTextFileResponse()

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: object,
    ) -> RequestPermissionResponse:
        del session_id, kwargs
        kind = str(getattr(tool_call, 'kind', '') or '')
        if kind in _ALLOW_KINDS:
            option_id = _option(options, 'allow_once')
            if option_id is not None:
                return _selected(option_id)
        if await self._approved(tool_call, kind):
            option_id = _option(options, 'allow_once')
            if option_id is not None:
                return _selected(option_id)
        return _rejected(options)

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[EnvVariable] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: object,
    ) -> CreateTerminalResponse:
        del session_id, kwargs
        try:
            return await self._create_terminal(command, args, env, cwd, output_byte_limit)
        except SyndicaError as exc:
            raise _request(exc) from None

    async def _create_terminal(
        self,
        command: str,
        args: list[str] | None,
        env: list[EnvVariable] | None,
        cwd: str | None,
        output_byte_limit: int | None,
    ) -> CreateTerminalResponse:
        limit = _byte_limit(output_byte_limit)
        result = await self._model._call(  # pyright: ignore[reportPrivateUsage]
            self._agent_id,
            'Bash',
            {
                'command': _shell_command(command, args, env),
                'run_in_background': True,
                'cwd': self._relative_cwd(cwd),
            },
        )
        handle = result.meta.get('handle')
        if not isinstance(handle, str) or not handle:
            raise _refused(result)
        terminal_id = secrets.token_hex(8)
        live = self._model._sessions.get(self._agent_id)  # pyright: ignore[reportPrivateUsage]
        if live is not None:
            live.terminals[terminal_id] = _Terminal(handle, limit)
        return CreateTerminalResponse(terminal_id=terminal_id)

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: object
    ) -> TerminalOutputResponse:
        del session_id, kwargs
        terminal = self._terminal(terminal_id)
        result = await self._model._call(  # pyright: ignore[reportPrivateUsage]
            self._agent_id, 'BashOutput', {'handle': terminal.handle, 'full': True}
        )
        output, truncated = _clipped(_text(result), terminal.limit)
        return TerminalOutputResponse(
            output=output,
            truncated=truncated or bool(result.meta.get('truncated')),
            exit_status=_exit_status(result),
        )

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: object
    ) -> WaitForTerminalExitResponse:
        del session_id, kwargs
        terminal = self._terminal(terminal_id)
        deadline = _deadline()
        while True:
            result = await self._model._call(  # pyright: ignore[reportPrivateUsage]
                self._agent_id, 'BashOutput', {'handle': terminal.handle, 'full': True}
            )
            if result.meta.get('running') is False:
                code = result.meta.get('exit_code')
                return WaitForTerminalExitResponse(
                    exit_code=code if isinstance(code, int) else None
                )
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                raise ConfigError(
                    expected='终端在墙钟剩余时间内退出',
                    received=f'{terminal_id} 仍在运行',
                    next_action='缩短命令，或等下一次调用再查；到点不会一直轮询',
                )
            await asyncio.sleep(_POLL_S)

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: object) -> None:
        del session_id, kwargs
        await self._stop(terminal_id, drop=False)

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: object) -> None:
        del session_id, kwargs
        await self._stop(terminal_id, drop=True)

    async def _approved(self, tool_call: ToolCallUpdate, kind: str) -> bool:
        model = self._model
        policy = model._policy  # pyright: ignore[reportPrivateUsage]
        runtime = model._runtime  # pyright: ignore[reportPrivateUsage]
        if policy is None or policy.confirm is None or runtime is None or not self._agent_id:
            return False
        title = str(getattr(tool_call, 'title', '') or kind)
        outcome = await policy.confirm.confirm(
            runtime,
            ConfirmationRequest(
                request_id=secrets.token_hex(16),
                agent_id=self._agent_id,
                tool_call_id=str(getattr(tool_call, 'tool_call_id', '') or 'acp-permission'),
                tool_name='acp-permission',
                arguments=model._redact(  # pyright: ignore[reportPrivateUsage]
                    json.dumps({'kind': kind, 'title': title}, ensure_ascii=False)
                )[:1000],
                verdict=DangerVerdict(
                    rule_id='acp.external_agent',
                    level=RiskLevel.HIGH,
                    reason=f'ACP Agent 请求 {kind} 权限',
                ),
                untrusted=True,
            ),
        )
        return outcome is ConfirmationOutcome.APPROVED

    def _terminal(self, terminal_id: str) -> _Terminal:
        live = self._model._sessions.get(self._agent_id)  # pyright: ignore[reportPrivateUsage]
        terminal = None if live is None else live.terminals.get(terminal_id)
        if terminal is None:
            raise ConfigError(
                expected='terminal_id 是这次会话 create 交回的那个',
                received=f'没有 {terminal_id!r}',
                next_action='用 create 交回的 terminalId；release 之后的 id 不能再查',
            )
        return terminal

    async def _stop(self, terminal_id: str, *, drop: bool) -> None:
        terminal = self._terminal(terminal_id)
        await self._model._call(  # pyright: ignore[reportPrivateUsage]
            self._agent_id, 'KillBash', {'handle': terminal.handle}
        )
        if drop:
            live = self._model._sessions.get(self._agent_id)  # pyright: ignore[reportPrivateUsage]
            if live is not None:
                live.terminals.pop(terminal_id, None)

    def _relative_cwd(self, cwd: str | None) -> str:
        if cwd is None or cwd == '.':
            return '.'
        root = self._model._cwd_for(self._agent_id).rstrip('/')  # pyright: ignore[reportPrivateUsage]
        if cwd == root:
            return '.'
        prefix = root + '/'
        if cwd.startswith(prefix):
            return cwd[len(prefix) :]
        return cwd


def _selected(option_id: str) -> RequestPermissionResponse:
    return RequestPermissionResponse(
        outcome=AllowedOutcome(outcome='selected', option_id=option_id)
    )


def _rejected(options: Sequence[PermissionOption]) -> RequestPermissionResponse:
    option_id = _option(options, 'reject_once')
    if option_id is None:
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome='cancelled'))
    return _selected(option_id)


def _option(options: Sequence[object], kind: str) -> str | None:
    for option in options:
        if getattr(option, 'kind', None) == kind:
            value = getattr(option, 'option_id', None)
            if isinstance(value, str):
                return value
    return None


def _fresh_prompt(messages: Sequence[ContextBlock], cursor: int) -> str:
    parts: list[str] = []
    for block in messages[cursor:]:
        if block.role not in {'system', 'user'}:
            continue
        if isinstance(block.content, str) and block.content:
            parts.append(block.content)
    return '\n'.join(parts)


def _text_blocks(text: str) -> list[TextContentBlock]:
    return [TextContentBlock(type='text', text=text)]


def _text(result: ToolResult) -> str:
    output = result.output
    return output if isinstance(output, str) else str(output)


def _refused(result: ToolResult) -> RequestError:
    return RequestError(-32000, _text(result)[:500])


def _request(exc: SyndicaError) -> RequestError:
    message = f'期望: {exc.expected}。实际: {exc.received}。下一步: {exc.next_action}'
    return RequestError(
        -32602,
        message[:500],
        {
            'expected': exc.expected,
            'received': exc.received,
            'next_action': exc.next_action,
        },
    )


def _shell_command(command: str, args: Sequence[str] | None, env: Sequence[object] | None) -> str:
    body = shlex.join((command, *(args or ())))
    if not env:
        return body
    assignments: list[str] = []
    for item in env:
        if isinstance(item, dict):
            raw = cast(dict[str, object], item)
            name: object = raw.get('name')
            value: object = raw.get('value')
        else:
            name = getattr(item, 'name', None)
            value = getattr(item, 'value', None)
        if not isinstance(name, str) or not isinstance(value, str):
            raise ConfigError(
                expected='terminal/create 的 env 每一项是 name 与 value 字符串',
                received=f'收到 {item!r}',
                next_action='按 ACP 的 EnvVariable 传；不要把密钥写进 env',
            )
        if looks_secret(name):
            raise ConfigError(
                expected='terminal/create 的 env 不含像密钥的名字',
                received=f'收到 {name!r}',
                next_action=f'删掉 {name}：终端环境会被命令打印进上下文',
            )
        assignments.append(f'{shlex.quote(name)}={shlex.quote(value)}')
    return f'env {" ".join(assignments)} {body}'


def _byte_limit(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(
            expected='outputByteLimit 是空或者 >= 1 的字节数',
            received=f'收到 {value!r}',
            next_action='不限制就不传；要限制就传正整数，不要传 true',
        )
    return value


def _clipped(text: str, limit: int | None) -> tuple[str, bool]:
    if limit is None:
        return text, False
    encoded = text.encode('utf-8')
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode('utf-8', errors='ignore'), True


def _exit_status(result: ToolResult) -> TerminalExitStatus | None:
    if result.meta.get('running') is not False:
        return None
    code = result.meta.get('exit_code')
    if not isinstance(code, int):
        return None
    return TerminalExitStatus(exit_code=code)


def _deadline() -> float | None:
    left = wall_seconds_left()
    if left is None:
        return None
    return asyncio.get_running_loop().time() + left
