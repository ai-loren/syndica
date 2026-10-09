"""把一个 ACP Agent 当成 Syndica 的模型。

每个 Syndica Agent 第一次 `create` 时握手并 `session/new`，拆除时收掉进程组。读、写、终端都经
`call_external`，调用方是 `acp-agent:<名字>`。`call_external` 不过 PRE_TOOL_USE，所以回调只用驱动
它的那个 Agent 所属角色交进来的工具与处置：角色的 tools 里没有的工具、只读 run 里的写与终端，一律
拒绝。回答一律不可信。协议版本不是 1 就拒绝。

回调被拒、没做成或出错时一律回 `RequestError`，`data` 是脱敏后的三件套：答成功，外部 Agent 就会
以为写进去了、拿拒绝说明当文件内容；让异常漏给 SDK，它回 -32603 并把异常原文交给第三方进程。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import shlex
import signal
from collections.abc import AsyncGenerator, Callable, Coroutine, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, TypeVar, cast

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
# 这几类权限放行的是外部 Agent 自己的改文件、跑命令工具：角色没有对应的工具、或 run 是只读的，不问人
# 直接拒——人点了允许也不能给角色它没有的能力。
_KIND_TOOLS = {'edit': 'Write', 'delete': 'Write', 'move': 'Write', 'execute': 'Bash'}
# 只读 run 里拒的工具。终端的输出、等待、结束与释放都只碰 terminal/create 交出去的句柄，拒了 Bash
# 就一个句柄也交不出去。
_READ_ONLY_REFUSED = frozenset({'Write', 'Bash'})
_CANCEL_WAIT_S = 0.2
_POLL_S = 0.05
_MESSAGE_CHARS = 500
_FIELD_CHARS = 2000

_T = TypeVar('_T')


@dataclass(frozen=True)
class _Grant:
    """一个角色交给托管 Agent 的工具与处置。`role` 为空是构造时直接给的那一份。"""

    role: str
    tools: Mapping[str, Tool]
    policy: ExternalCallPolicy | None
    read_only: bool = False


class AcpAgentModel:
    """一个模型名对应的 ACP 托管。`connect` 与 `command` 给一个：测试注入连接，正式路径拉起进程。

    构造时给的 `tools` / `policy` 对每个调用方都一样；宿主经 `bind_runtime` 按角色交过之后，回调只认
    驱动它的那个 Agent 的角色。
    """

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
        self._own = _Grant(role='', tools=dict(tools or {}), policy=policy)
        self._grants: dict[str, _Grant] = {}
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
        role: str,
        read_only: bool = False,
        agent_env: Mapping[str, str],
        host_env: Mapping[str, str],
        redactor: Redactor,
    ) -> None:
        """宿主在 Runtime 建好之后，对每个用这个模型的角色交一次：`tools` 是那个角色冻结后的工具表
        （有 Bash 时连同 BashOutput、KillBash），`policy` 是它自己的处置。`read_only` 时写文件与终端
        一律拒绝。进程要等第一次 create 才拉起。"""
        env = dict(agent_env)
        for key in self._extra_env:
            value = host_env.get(key)
            if value is not None:
                env[key] = value
        self._runtime = runtime
        self._grants[role] = _Grant(
            role=role, tools=dict(tools), policy=policy, read_only=read_only
        )
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
        if not self._grants and self._own.policy is None:
            raise ConfigError(
                expected='create 之前已经 bind_runtime，或构造时就给了 tools 与 policy',
                received=f'{self._name} 还没有工具或处置',
                next_action='等宿主把 Runtime 交进来再调用；单测直接把 tools 和 policy 传进构造',
            )

    async def _grant_for(self, agent_id: str) -> _Grant:
        if not self._grants:
            return self._own
        runtime = self._runtime
        info = None if runtime is None or not agent_id else await runtime.find(agent_id)
        role = '' if info is None else info.name
        grant = self._grants.get(role)
        if grant is None:
            return _Grant(role=role or f'（agent {agent_id or "未知"}）', tools={}, policy=None)
        return grant

    async def _open(self, agent_id: str) -> _Live:
        live = self._sessions.get(agent_id)
        if live is not None:
            return live
        client = _Client(self, agent_id, await self._grant_for(agent_id))
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

    def _granted(
        self, grant: _Grant, method: str, tool_name: str
    ) -> tuple[Tool, ExternalCallPolicy]:
        agent = f'acp:{self._name}'
        owner = f'角色 {grant.role}' if grant.role else agent
        if grant.read_only and tool_name in _READ_ONLY_REFUSED:
            raise ConfigError(
                expected=f'只读 run 里 {agent} 只读文件：{method} 会改动工作区',
                received=f'{owner} 的 {agent} 发来 {method}，而这次 run 是只读的（--no-write）',
                next_action=(
                    '换成只读的做法；要改动就把改什么、怎么改写进回答，由人去掉 --no-write 再跑'
                ),
            )
        tool = grant.tools.get(tool_name)
        policy = grant.policy
        if tool is None or policy is None:
            raise ConfigError(
                expected=f'{owner} 的 tools 里有 {tool_name}：{agent} 的 {method} 要用它',
                received=f'{owner} 的 tools 只有 {sorted(grant.tools)!r}',
                next_action=(
                    f'在{owner} 的 tools 里加上 {tool_name}；不加就让 {agent} 换一种不用 '
                    f'{method} 的做法'
                ),
            )
        return tool, policy

    async def _call(
        self,
        agent_id: str,
        granted: tuple[Tool, ExternalCallPolicy],
        args: Mapping[str, object],
    ) -> ToolResult:
        tool, policy = granted
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
        await runtime.events.emit(
            Event(
                'external_activity',
                agent_id,
                {
                    # 契约里 source 是协议（acp / a2a / mcp），name 是外部对象的名字：界面靠这两项
                    # 认出是谁在做事，activity_id 只用来把同一个调用的几次更新串起来。
                    'source': 'acp',
                    'name': self._name,
                    'activity_id': str(getattr(update, 'tool_call_id', '') or kind),
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

    def _error(self, code: int, expected: str, received: str, next_action: str) -> RequestError:
        """交给对端的错误。先脱敏再截：截在密钥中间，剩下那截脱敏器就认不出了。"""
        data = {
            'expected': self._redact(expected)[:_FIELD_CHARS],
            'received': self._redact(received)[:_FIELD_CHARS],
            'next_action': self._redact(next_action)[:_FIELD_CHARS],
        }
        message = (
            f'期望: {data["expected"]}。实际: {data["received"]}。下一步: {data["next_action"]}'
        )
        return RequestError(code, message[:_MESSAGE_CHARS], data)

    def _refused(self, method: str, result: ToolResult) -> RequestError:
        return self._error(
            -32000,
            expected=f'acp:{self._name} 的 {method} 过了 Syndica 的危险判定与处置再执行',
            received=f'被 Syndica 拒绝，没有执行、也没有副作用。拒绝说明：{_text(result)}',
            next_action=(
                '照拒绝说明里的下一步换一种做法，或把这一步写进回答交给人去做；不要当成已经做了'
            ),
        )

    def _failed(self, method: str, result: ToolResult) -> RequestError:
        return self._error(
            -32000,
            expected=f'acp:{self._name} 的 {method} 做成',
            received=f'Syndica 的工具没有做成这件事：{_text(result)}',
            next_action='照「实际」里工具给的说明改了再试；这一次没有做成，不要当成已经做了',
        )


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
    def __init__(self, model: AcpAgentModel, agent_id: str, grant: _Grant) -> None:
        self._model = model
        self._agent_id = agent_id
        self._grant = grant
        self.chunks: list[str] = []

    def _granted(self, method: str, tool_name: str) -> tuple[Tool, ExternalCallPolicy]:
        return self._model._granted(self._grant, method, tool_name)  # pyright: ignore[reportPrivateUsage]

    async def _answer(self, method: str, work: Coroutine[Any, Any, _T]) -> _T:
        """每个回调都从这里交出去。SyndicaError 换成三件套；别的异常只报类型，原文不交给对端。"""
        model = self._model
        try:
            return await work
        except RequestError:
            raise
        except SyndicaError as exc:
            raise model._error(-32602, exc.expected, exc.received, exc.next_action) from None  # pyright: ignore[reportPrivateUsage]
        except Exception as exc:
            agent = f'acp:{model._name}'  # pyright: ignore[reportPrivateUsage]
            logger.warning(
                '期望: %s 的回调 %s 正常处理完。 实际: 抛出 %s，已回给对端 -32603，原文没有交出去。'
                ' 下一步: 看 trace 里这次调用的 span，在本机复现这次调用拿原文。',
                agent,
                method,
                type(exc).__name__,
            )
            raise model._error(  # pyright: ignore[reportPrivateUsage]
                -32603,
                expected=f'{agent} 的 {method} 由 Syndica 正常处理完',
                received=f'Syndica 处理时抛出 {type(exc).__name__}（异常原文不交给对端）',
                next_action='这一步没有做成；换一种做法，或把这一步写进回答交给人',
            ) from None

    async def _use(
        self, method: str, tool_name: str, args: Mapping[str, object], *, done: str | None
    ) -> ToolResult:
        """经 `call_external` 跑一次，没做成就抛。`done` 是这个工具只在做成时才放进 meta 的键。

        `call_external` 执行过的结果一律收紧成不可信，还是可信的就是内核写的拒绝说明：工具没有跑。
        """
        granted = self._granted(method, tool_name)
        result = await self._model._call(self._agent_id, granted, args)  # pyright: ignore[reportPrivateUsage]
        if result.trust is TrustLevel.TRUSTED:
            raise self._model._refused(method, result)  # pyright: ignore[reportPrivateUsage]
        if done is not None and done not in result.meta:
            raise self._model._failed(method, result)  # pyright: ignore[reportPrivateUsage]
        return result

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        del session_id, kwargs
        await self._answer('session/update', self._update(update))

    async def _update(self, update: object) -> None:
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
        return await self._answer('fs/read_text_file', self._read(path, line, limit))

    async def _read(self, path: str, line: int | None, limit: int | None) -> ReadTextFileResponse:
        args: dict[str, object] = {'path': path}
        if line is not None:
            args['offset'] = line
        if limit is not None:
            args['limit'] = limit
        result = await self._use('fs/read_text_file', 'Read', args, done=None)
        return ReadTextFileResponse(content=_text(result))

    async def write_text_file(
        self, session_id: str, path: str, content: str, **kwargs: object
    ) -> WriteTextFileResponse:
        del session_id, kwargs
        return await self._answer('fs/write_text_file', self._write(path, content))

    async def _write(self, path: str, content: str) -> WriteTextFileResponse:
        await self._use(
            'fs/write_text_file', 'Write', {'path': path, 'content': content}, done='path'
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
        return await self._answer('session/request_permission', self._permit(tool_call, options))

    async def _permit(
        self, tool_call: ToolCallUpdate, options: list[PermissionOption]
    ) -> RequestPermissionResponse:
        kind = str(getattr(tool_call, 'kind', '') or '')
        needed = _KIND_TOOLS.get(kind)
        if needed is not None and (self._grant.read_only or needed not in self._grant.tools):
            return _rejected(options)
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
        return await self._answer(
            'terminal/create', self._create_terminal(command, args, env, cwd, output_byte_limit)
        )

    async def _create_terminal(
        self,
        command: str,
        args: list[str] | None,
        env: list[EnvVariable] | None,
        cwd: str | None,
        output_byte_limit: int | None,
    ) -> CreateTerminalResponse:
        limit = _byte_limit(output_byte_limit)
        result = await self._use(
            'terminal/create',
            'Bash',
            {
                'command': _shell_command(command, args, env),
                'run_in_background': True,
                'cwd': self._relative_cwd(cwd),
            },
            done='handle',
        )
        handle = result.meta['handle']
        if not isinstance(handle, str) or not handle:
            raise self._model._failed('terminal/create', result)  # pyright: ignore[reportPrivateUsage]
        terminal_id = secrets.token_hex(8)
        live = self._model._sessions.get(self._agent_id)  # pyright: ignore[reportPrivateUsage]
        if live is not None:
            live.terminals[terminal_id] = _Terminal(handle, limit)
        return CreateTerminalResponse(terminal_id=terminal_id)

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: object
    ) -> TerminalOutputResponse:
        del session_id, kwargs
        return await self._answer('terminal/output', self._output(terminal_id))

    async def _output(self, terminal_id: str) -> TerminalOutputResponse:
        terminal = self._terminal(terminal_id)
        result = await self._use(
            'terminal/output',
            'BashOutput',
            {'handle': terminal.handle, 'full': True},
            done='running',
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
        return await self._answer('terminal/wait_for_exit', self._wait(terminal_id))

    async def _wait(self, terminal_id: str) -> WaitForTerminalExitResponse:
        terminal = self._terminal(terminal_id)
        deadline = _deadline()
        while True:
            result = await self._use(
                'terminal/wait_for_exit',
                'BashOutput',
                {'handle': terminal.handle, 'full': True},
                done='running',
            )
            if result.meta.get('running') is False:
                exit_code, signal_name = _exited(result)
                return WaitForTerminalExitResponse(exit_code=exit_code, signal=signal_name)
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                raise ConfigError(
                    expected='终端在墙钟剩余时间内退出',
                    received=f'{terminal_id} 仍在运行',
                    next_action='缩短命令，或等下一次调用再查；到点不会一直轮询',
                )
            await asyncio.sleep(_POLL_S)

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: object) -> None:
        del session_id, kwargs
        await self._answer('terminal/kill', self._stop('terminal/kill', terminal_id, drop=False))

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: object) -> None:
        del session_id, kwargs
        await self._answer(
            'terminal/release', self._stop('terminal/release', terminal_id, drop=True)
        )

    async def _approved(self, tool_call: ToolCallUpdate, kind: str) -> bool:
        model = self._model
        policy = self._grant.policy
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

    async def _stop(self, method: str, terminal_id: str, *, drop: bool) -> None:
        terminal = self._terminal(terminal_id)
        await self._use(method, 'KillBash', {'handle': terminal.handle}, done='handle')
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
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(
            expected='outputByteLimit 是空或者 >= 0 的字节数',
            received=f'收到 {value!r}',
            next_action='不限制就不传；要限制就传非负整数，不要传 true',
        )
    return value


def _clipped(text: str, limit: int | None) -> tuple[str, bool]:
    """ACP 要求超限时从开头截、留下结尾，且截在字符边界上（宁可比上限少几个字节）。"""
    if limit is None:
        return text, False
    encoded = text.encode('utf-8')
    if len(encoded) <= limit:
        return text, False
    # 不能写成 encoded[-limit:]：limit 为 0 时它是整段。
    return encoded[len(encoded) - limit :].decode('utf-8', errors='ignore'), True


def _exited(result: ToolResult) -> tuple[int | None, str | None]:
    """退出码与终止它的信号，按 ACP 二选一。

    子进程被信号杀掉时退出码是负的信号值；ACP 的 exitCode 必须 >= 0，SDK 会把负数静默换成 null，
    外部 Agent 于是既看不到退出码也看不到信号。
    """
    code = result.meta.get('exit_code')
    if isinstance(code, bool) or not isinstance(code, int):
        return None, None
    if code >= 0:
        return code, None
    try:
        return None, signal.Signals(-code).name
    except ValueError:
        return None, f'SIG{-code}'


def _exit_status(result: ToolResult) -> TerminalExitStatus | None:
    if result.meta.get('running') is not False:
        return None
    exit_code, signal_name = _exited(result)
    if exit_code is None and signal_name is None:
        return None
    return TerminalExitStatus(exit_code=exit_code, signal=signal_name)


def _deadline() -> float | None:
    left = wall_seconds_left()
    if left is None:
        return None
    return asyncio.get_running_loop().time() + left
