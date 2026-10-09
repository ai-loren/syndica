"""ACP agent：编辑器经 stdio 驱动 Syndica。

协议版本只答 1。`session/new` 的 cwd 必须是已经信任的工作区，这里不在 stdio 上问人。
提示里除文本以外的内容块拒绝。MCP 只收 stdio。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol, cast

from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    HttpMcpServer,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    McpCapabilities,
    McpServerStdio,
    NewSessionResponse,
    PermissionOption,
    PromptCapabilities,
    PromptResponse,
    SessionMode,
    SessionModeState,
    SetSessionModeResponse,
    SseMcpServer,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UserMessageChunk,
)

from syndica.kernel import (
    AgentStatus,
    ConfigError,
    ContextBlock,
    Event,
    Redactor,
    Runtime,
    Subscription,
    SyndicaError,
    TrustLevel,
)
from syndica.kernel.history_blocks import kind_of

__all__ = ['AcpBridge', 'AcpSession', 'OpenTurn', 'SessionIndex', 'SyndicaAcp', 'TurnBinding']

_OUTPUT_CAP = 4_000
_ROOT_ONLY = frozenset({'message_appended', 'agent_error', 'agent_end'})
# 等被取消的那一轮收完尾、等根 Agent 拆完邮箱的上限。到点照样往下走：宁可多一段尾巴，也不挂住提示。
_SETTLE_S = 10.0
_QUIET_POLLS = 200

logger = logging.getLogger(__name__)

PromptRunner = Callable[[str, str, str], Awaitable[str]]


class AcpSession:
    """一条 ACP 会话。`prompted` 之后不能再换模式。"""

    def __init__(self, cwd: str, mode: str, servers: tuple[McpServerStdio, ...]) -> None:
        self.cwd = cwd
        self.mode = mode
        self.servers = servers
        self.prompted = False
        self.bridge: AcpBridge | None = None


class TurnBinding:
    """一次已经装好的 Syndica 会话，ACP 的一轮提示在它上面跑。

    `history` 是从落盘会话恢复时存下的那份轨迹：恢复出来的根 Agent 停在空闲、等下一条消息，
    `Runtime.wait` 要等到下一轮结束才返回，重放只能读这一份。
    """

    def __init__(
        self,
        runtime: Runtime,
        agent_id: str,
        subscription: Subscription,
        *,
        history: Sequence[ContextBlock] | None = None,
    ) -> None:
        self.runtime = runtime
        self.agent_id = agent_id
        self.subscription = subscription
        self.history = None if history is None else tuple(history)


class OpenTurn(Protocol):
    """装好一次 Syndica 会话。`resume` 给定时续那个落盘会话，角色沿用落盘时的那个。"""

    def __call__(
        self,
        cwd: str,
        mode: str,
        servers: tuple[Any, ...] = (),
        *,
        resume: str | None = None,
    ) -> Awaitable[TurnBinding]: ...


class SessionIndex(Protocol):
    """ACP 的 sessionId 落在哪个 Syndica 会话上。新进程里的 `session/load` 靠它找回会话。"""

    def remember(self, session_id: str, sid: str) -> None: ...

    def recall(self, session_id: str) -> str | None: ...


class TurnEmit(Protocol):
    async def message(self, text: str) -> None: ...

    async def user_message(self, text: str) -> None: ...

    async def tool(
        self,
        tool_call_id: str,
        name: str,
        status: str,
        raw_input: str,
        raw_output: str,
    ) -> None: ...

    async def permission(self, tool_call_id: str, name: str, arguments: str) -> str: ...


class AcpBridge:
    """把一次根 Agent 的运行映射成 ACP 更新。

    提问不在这一轮里等答复：把选项写回编辑器后这次提示以 `end_turn` 结束，下一条提示必须是其中
    一个选项。确认在这一轮里问编辑器，答完 Agent 继续跑。
    """

    def __init__(
        self,
        binding: TurnBinding,
        emit: TurnEmit,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        self._runtime = binding.runtime
        self._agent_id = binding.agent_id
        self._history = binding.history
        self._emit = emit
        self._redactor = redactor
        self._question: tuple[str, str, tuple[str, ...]] | None = None
        # 正在问编辑器的确认：request_id -> 发起的 Agent。谁先从这里摘走谁答，答复只发一次。
        self._asking: dict[str, str] = {}
        self._stop = 'end_turn'
        self._cancelled = False
        self._failure: BaseException | None = None
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        # 取消之后，被中止那一轮的 agent_end 还在路上。它到之前放下一条提示进来，它就会被当成
        # 下一轮的结束，下一条提示带着上一轮的尾巴提前返回。
        self._settled = asyncio.Event()
        self._settled.set()
        self._pump = asyncio.create_task(
            self._run(binding.subscription), name=f'acp-pump:{binding.agent_id}'
        )

    async def submit(self, text: str) -> str:
        # 失败只属于报错的那一次提示：根 Agent 收到下一条消息会重新开一个周期，
        # 留着它会让会话此后每条提示都失败。
        if not self._settled.is_set():
            try:
                async with asyncio.timeout(_SETTLE_S):
                    await self._settled.wait()
            except TimeoutError:
                self._settled.set()
        question = self._question
        self._cancelled = False
        self._failure = None
        self._turn_done.clear()
        if question is not None:
            agent_id, request_id, options = question
            if options and text not in options:
                self._turn_done.set()
                raise ConfigError(
                    expected=f'答复是列出的选项之一：{list(options)}',
                    received=text,
                    next_action='原样回复其中一个选项；这一轮还停在那个问题上',
                )
            self._question = None
            delivered = await self._runtime.send(
                agent_id,
                text,
                activate=False,
                sender='human',
                correlation_id=request_id,
                trust=TrustLevel.TRUSTED,
            )
        else:
            # agent_end 发出时（正常结束或被取消）驱动还在拆邮箱；这时送达的提示会随邮箱一起丢掉。
            await self._quiet()
            delivered = await self._runtime.send(self._agent_id, text)
        if not delivered.ok:
            self._turn_done.set()
            raise ConfigError(
                expected='根 Agent 接受这条提示',
                received=delivered.reason or '投递没有给出原因',
                next_action='看这个会话是不是已经结束；结束了就 new_session',
            )
        self._history = None
        await self._turn_done.wait()
        if self._cancelled:
            return 'cancelled'
        if self._failure is not None:
            raise self._failure
        return self._stop

    async def _quiet(self) -> None:
        for _ in range(_QUIET_POLLS):
            info = await self._runtime.find(self._agent_id)
            if info is None or info.status in (AgentStatus.ZOMBIE, AgentStatus.IDLE):
                return
            await asyncio.sleep(0.01)

    async def replay(self) -> None:
        # 这一轮的驱动任务结束之后 `wait` 立刻返回。还在跑就等到它结束再重放，不读半截轨迹。
        if self._history is not None:
            blocks: Sequence[ContextBlock] = self._history
        else:
            blocks = (await self._runtime.wait(self._agent_id)).current_traj
        for block in blocks:
            text = block.content if isinstance(block.content, str) else ''
            # 内核署名的块（预算、恢复提示）是写给模型的，不是人说过的话。
            if not text or kind_of(block) is not None:
                continue
            text = self._redact(text)
            if block.role == 'user':
                await self._emit.user_message(text)
            elif block.role == 'assistant':
                await self._emit.message(text)

    async def abort(self) -> None:
        # 先放开正在等的 submit，再中止 Agent。确认回调可能还停着，不能等它才让这次提示返回。
        self._cancelled = True
        self._stop = 'cancelled'
        self._turn_done.set()
        info = await self._runtime.find(self._agent_id)
        if info is not None and info.status is not AgentStatus.ZOMBIE:
            self._settled.clear()
        await self._runtime.abort(self._agent_id)

    async def close(self) -> None:
        # 挂着的确认在拆泵之前按拒绝答掉：只取消泵，这次调用要等到确认超时或关机才落定，
        # 结局是超时或中止而不是拒绝，Agent 在那之前一直停在它上面。
        asking = list(self._asking.items())
        self._asking.clear()
        for request_id, agent_id in asking:
            try:
                await self._answer(agent_id, request_id, 'reject')
            except Exception:
                logger.exception('acp reject on close failed')
        self._pump.cancel()
        await asyncio.gather(self._pump, return_exceptions=True)

    async def _run(self, subscription: Subscription) -> None:
        async for event in subscription:
            try:
                await self._on(event)
            except Exception:
                logger.exception('acp event failed')

    async def _on(self, event: Event) -> None:
        # 回答、结束与失败只认根：子 Agent 先结束会让这次提示提前返回，根的回答落到提示返回之后。
        # 工具（含它的结果块）、确认与提问来自谁都推给编辑器，答复交回发起的那个 Agent。
        tool_result = event.kind == 'message_appended' and _is_tool_result(event.payload)
        if event.kind in _ROOT_ONLY and event.agent_id != self._agent_id and not tool_result:
            return
        if not self._settled.is_set():
            # 被取消的那一轮还在收尾：它的输出与结束都不属于下一条提示。
            if event.kind in {'agent_end', 'agent_error'}:
                self._settled.set()
            return
        if tool_result:
            await self._tool_output(event.payload)
            return
        if event.kind == 'message_appended':
            await self._message(event.payload)
        elif event.kind == 'tool_call_started':
            await self._emit.tool(
                str(event.payload.get('tool_call_id')),
                str(event.payload.get('name')),
                'in_progress',
                _clip(self._redact(str(event.payload.get('arguments', '')))),
                '',
            )
        elif event.kind == 'tool_call_finished':
            outcome = str(event.payload.get('outcome'))
            await self._emit.tool(
                str(event.payload.get('tool_call_id')),
                '',
                'completed' if outcome == 'ok' else 'failed',
                '',
                '',
            )
        elif event.kind == 'tool_confirmation_requested':
            await self._confirm(event)
        elif event.kind == 'user_question_requested':
            await self._ask(event)
        elif event.kind == 'user_input_requested':
            await self._ask(event)
        elif event.kind == 'agent_error':
            # 只报异常类型：内核事件里的 `received` 是异常原文，脱敏规则认不全它。
            self._failure = _refuse(
                '根 Agent 跑完这一轮',
                f'根 Agent 报错 {event.payload.get("error_type") or "未知类型"}，原文不在回复里',
                '看工作区之外的 acp.log 或 trace 里根 Agent 的 agent span；修好之后再发一条提示',
            )
            self._turn_done.set()
        elif event.kind == 'agent_end':
            if not self._cancelled:
                self._stop = _stop_reason(event.payload.get('stop_reason'))
            self._turn_done.set()
        elif event.kind == 'budget_exhausted':
            raw = event.payload.get('dimensions')
            dimensions = cast(list[object], raw) if isinstance(raw, list) else []
            named = '、'.join(str(item) for item in dimensions) or '未知的一维'
            await self._emit.message(self._redact(f'预算用尽：{named}'))

    async def _tool_output(self, payload: dict[str, Any]) -> None:
        """工具结果块作为那次调用的 `raw_output` 交给编辑器：先脱敏再截断，截断可能切在密钥中间。"""
        block = cast(dict[str, object], payload['block'])
        tool_call_id = block.get('tool_call_id')
        text = block.get('text')
        if not isinstance(tool_call_id, str) or not tool_call_id or not isinstance(text, str):
            return
        await self._emit.tool(tool_call_id, '', '', '', _clip(self._redact(text)))

    async def _message(self, payload: dict[str, Any]) -> None:
        raw = payload.get('block')
        if not isinstance(raw, dict):
            return
        block = cast(dict[str, object], raw)
        if str(block.get('role')) != 'assistant':
            return
        text = block.get('text')
        if not isinstance(text, str) or not text:
            return
        await self._emit.message(self._redact(text))

    async def _confirm(self, event: Event) -> None:
        name = str(event.payload.get('tool_name'))
        tool_call_id = str(event.payload.get('tool_call_id'))
        request_id = str(event.payload.get('request_id'))
        arguments = _clip(self._redact(str(event.payload.get('arguments', ''))))
        self._asking[request_id] = event.agent_id
        try:
            decision = await self._emit.permission(tool_call_id, name, arguments)
        except Exception:
            logger.exception('acp permission failed')
            decision = 'reject'
        if self._asking.pop(request_id, None) is None:
            return
        await self._answer(event.agent_id, request_id, decision)

    async def _answer(self, agent_id: str, request_id: str, decision: str) -> None:
        await self._runtime.send(
            agent_id,
            'approve' if decision == 'approve' else 'reject',
            activate=False,
            sender='human',
            correlation_id=request_id,
            trust=TrustLevel.TRUSTED,
        )

    async def _ask(self, event: Event) -> None:
        options = tuple(str(item) for item in event.payload.get('options', ()) or ())
        question = str(event.payload.get('question', ''))
        lines = [question, *(f'{index}. {option}' for index, option in enumerate(options, start=1))]
        await self._emit.message(self._redact('\n'.join(lines)))
        self._question = (event.agent_id, str(event.payload.get('request_id')), options)
        self._stop = 'end_turn'
        self._turn_done.set()

    def _redact(self, text: str) -> str:
        if self._redactor is None:
            return text
        return self._redactor.redact(text)


class SyndicaAcp:
    """低层 ACP agent。`trust` 不通过就三件套拒绝，不在这条 stdio 上问人。

    给了 `sessions` 时，每条会话第一次提示装好的 Syndica 会话记进去；这个进程里没有的 sessionId
    在 `session/load` 时从那里找回，按落盘会话恢复。
    """

    def __init__(
        self,
        roles: Sequence[tuple[str, str]],
        trust: Callable[[str], None],
        *,
        runner: PromptRunner | None = None,
        open_turn: OpenTurn | None = None,
        sessions: SessionIndex | None = None,
        redactor: Redactor | None = None,
        version: str = '',
    ) -> None:
        listed = tuple(roles)
        if not listed or any(not name.strip() for name, _description in listed):
            raise ConfigError(
                expected='ACP 的角色名单至少有一个非空名字',
                received=f'收到 {listed!r}',
                next_action='在 .syndica.yaml 里声明一个 Agent，再启动 syndica acp',
            )
        self._roles = listed
        self._trust = trust
        self._runner = runner
        self._open_turn = open_turn
        self._index = sessions
        self._redactor = redactor
        self._version = version
        self._sessions: dict[str, AcpSession] = {}
        self._conn: Any = None

    def on_connect(self, conn: object) -> None:
        self._conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: object = None,
        client_info: object = None,
        **kwargs: object,
    ) -> InitializeResponse:
        del client_capabilities, client_info, kwargs
        given = cast(object, protocol_version)
        if isinstance(given, bool) or not isinstance(given, int) or given < 1:
            raise _refuse(
                'ACP protocolVersion 是 1',
                f'收到 {given!r}',
                '用协议版本 1 再 initialize',
            )
        return InitializeResponse(
            protocol_version=1,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=False, audio=False, embedded_context=False
                ),
                mcp_capabilities=McpCapabilities(http=False, sse=False),
            ),
            agent_info=Implementation(name='syndica', title='Syndica', version=self._version),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[object] | None = None,
        **kwargs: object,
    ) -> NewSessionResponse:
        del additional_directories, kwargs
        servers = _stdio_servers(mcp_servers or [])
        try:
            self._trust(cwd)
        except SyndicaError as error:
            raise _refuse(error.expected, error.received, error.next_action) from None
        session_id = secrets.token_hex(16)
        mode = self._roles[0][0]
        self._sessions[session_id] = AcpSession(cwd, mode, servers)
        return NewSessionResponse(session_id=session_id, modes=self._mode_state(mode))

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[object] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: object,
    ) -> LoadSessionResponse:
        del additional_directories, kwargs
        session = self._sessions.get(session_id)
        if session is None:
            session = await self._resume(cwd, session_id, mcp_servers or [])
        elif session.bridge is not None:
            await session.bridge.replay()
        return LoadSessionResponse(modes=self._mode_state(session.mode))

    async def _resume(self, cwd: str, session_id: str, mcp_servers: list[object]) -> AcpSession:
        # 信任先于一切：没被信任的工作区里，不该先回答这个 sessionId 记没记下。
        try:
            self._trust(cwd)
        except SyndicaError as error:
            raise _refuse(error.expected, error.received, error.next_action) from None
        sid = None if self._index is None else self._index.recall(session_id)
        if sid is None or self._open_turn is None:
            raise _refuse(
                'sessionId 是这个工作区里 new_session 交回、并且提示过的那个',
                f'收到 {session_id!r}；这个进程里没有它，也没有记下它落在哪个会话上',
                '用 new_session 开一条新会话；续旧会话要在同一个工作区、同一个配置目录下启动 '
                'syndica acp 再 load',
            )
        servers = _stdio_servers(mcp_servers)
        try:
            binding = await self._open_turn(cwd, self._roles[0][0], servers, resume=sid)
        except SyndicaError as error:
            raise _refuse(error.expected, error.received, error.next_action) from None
        except Exception as exc:
            logger.exception('acp load failed')
            raise _refuse(
                'session/load 恢复落盘的会话',
                f'恢复时抛出了 {type(exc).__name__}，原文不回显',
                '看 acp 的日志',
            ) from None
        info = await binding.runtime.find(binding.agent_id)
        known = {name for name, _ in self._roles}
        mode = info.name if info is not None and info.name in known else self._roles[0][0]
        session = AcpSession(cwd, mode, servers)
        session.prompted = True
        session.bridge = AcpBridge(binding, _Sink(self, session_id), redactor=self._redactor)
        self._sessions[session_id] = session
        await session.bridge.replay()
        return session

    async def set_session_mode(
        self, session_id: str, mode_id: str, **kwargs: object
    ) -> SetSessionModeResponse:
        del kwargs
        session = self._require(session_id)
        if session.prompted:
            raise _refuse(
                '会话模式只在第一条提示之前更换',
                f'会话 {session_id} 已经提示过，还要换成 {mode_id!r}',
                '新开一条 session，或沿用现在的模式',
            )
        known = {name for name, _ in self._roles}
        if mode_id not in known:
            raise _refuse(
                f'会话模式是已列出的角色之一：{sorted(known)}',
                f'收到 {mode_id!r}',
                '用 initialize 之后 new_session 返回的 modes 里的 id',
            )
        session.mode = mode_id
        return SetSessionModeResponse()

    async def prompt(
        self, session_id: str, prompt: list[object], **kwargs: object
    ) -> PromptResponse:
        del kwargs
        session = self._require(session_id)
        texts: list[str] = []
        for block in prompt:
            kind = getattr(block, 'type', None)
            if kind != 'text':
                raise _refuse(
                    'session/prompt 的内容块只有 text',
                    f'收到 {kind!r}',
                    '把图片、音频和资源块留在编辑器里，只把文本发给 syndica acp',
                )
            texts.append(str(getattr(block, 'text', '')))
        if self._runner is None and self._open_turn is None:
            raise _refuse(
                'session/prompt 投递给根 Agent',
                '这个 ACP agent 还没有接上 Syndica 的会话',
                '用 syndica acp 启动，不要直接构造没有 runner 的 Agent',
            )
        session.prompted = True
        text = '\n'.join(texts)
        if self._open_turn is not None:
            return await self._prompt_session(session, session_id, text)
        assert self._runner is not None
        try:
            reply = await self._runner(session_id, session.mode, text)
        except SyndicaError as error:
            raise _refuse(error.expected, error.received, error.next_action) from None
        except Exception as exc:
            logger.exception('acp prompt failed')
            raise _refuse(
                'session/prompt 正常返回',
                f'处理时抛出了 {type(exc).__name__}，原文不回显',
                '看 acp 的日志',
            ) from None
        if self._redactor is not None:
            reply = self._redactor.redact(reply)
        await self.emit_assistant(session_id, reply)
        return PromptResponse(stop_reason='end_turn')

    async def aclose(self) -> None:
        for session in self._sessions.values():
            if session.bridge is not None:
                await session.bridge.close()

    async def cancel(self, session_id: str, **kwargs: object) -> None:
        del kwargs
        session = self._require(session_id)
        if session.bridge is not None:
            await session.bridge.abort()

    async def _prompt_session(
        self, session: AcpSession, session_id: str, text: str
    ) -> PromptResponse:
        assert self._open_turn is not None
        try:
            if session.bridge is None:
                binding = await self._open_turn(session.cwd, session.mode, session.servers)
                session.bridge = AcpBridge(
                    binding, _Sink(self, session_id), redactor=self._redactor
                )
                if self._index is not None:
                    self._index.remember(session_id, binding.agent_id)
            stop = await session.bridge.submit(text)
        except SyndicaError as error:
            raise _refuse(error.expected, error.received, error.next_action) from None
        except RequestError:
            raise
        except Exception as exc:
            logger.exception('acp prompt failed')
            raise _refuse(
                'session/prompt 正常返回',
                f'处理时抛出了 {type(exc).__name__}，原文不回显',
                '看 acp 的日志',
            ) from None
        return PromptResponse(stop_reason=_as_stop(stop))

    def _require(self, session_id: str) -> AcpSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise _refuse(
                'sessionId 是这个进程里 new_session 或 load_session 过的那个',
                f'收到 {session_id!r}',
                '用 new_session 的 sessionId；上一个 syndica acp 进程里的会话先 session/load 再用',
            )
        return session

    def _mode_state(self, current: str) -> SessionModeState:
        return SessionModeState(
            current_mode_id=current,
            available_modes=[
                SessionMode(id=name, name=name, description=description or name)
                for name, description in self._roles
            ],
        )

    async def emit_tool(
        self,
        session_id: str,
        tool_call_id: str,
        name: str,
        status: str,
        raw_input: str,
        raw_output: str,
    ) -> None:
        await self._tool(session_id, tool_call_id, name, status, raw_input, raw_output)

    async def _tool(
        self,
        session_id: str,
        tool_call_id: str,
        name: str,
        status: str,
        raw_input: str,
        raw_output: str,
    ) -> None:
        conn = self._conn
        send = None if conn is None else getattr(conn, 'session_update', None)
        if send is None:
            return
        if status == 'in_progress':
            update: object = ToolCallStart(
                session_update='tool_call',
                tool_call_id=tool_call_id,
                title=name,
                kind=_tool_kind(name),  # pyright: ignore[reportArgumentType]
                status='in_progress',
                raw_input=raw_input or None,
            )
        else:
            update = ToolCallProgress(
                session_update='tool_call_update',
                tool_call_id=tool_call_id,
                status=status or None,  # pyright: ignore[reportArgumentType]
                raw_output=raw_output or None,
            )
        await send(session_id=session_id, update=update)

    async def decide_permission(
        self, session_id: str, tool_call_id: str, name: str, arguments: str
    ) -> str:
        return await self._permission(session_id, tool_call_id, name, arguments)

    async def _permission(
        self, session_id: str, tool_call_id: str, name: str, arguments: str
    ) -> str:
        conn = self._conn
        request = None if conn is None else getattr(conn, 'request_permission', None)
        if request is None:
            return 'reject'
        response = await request(
            session_id=session_id,
            tool_call=ToolCallUpdate(
                tool_call_id=tool_call_id,
                title=name,
                kind=_tool_kind(name),  # pyright: ignore[reportArgumentType]
                status='pending',
                raw_input=arguments or None,
            ),
            options=[
                PermissionOption(option_id='allow_once', name='允许这一次', kind='allow_once'),
                PermissionOption(option_id='reject_once', name='拒绝这一次', kind='reject_once'),
            ],
        )
        outcome = getattr(response, 'outcome', None)
        selected = getattr(outcome, 'outcome', None) == 'selected'
        allowed = getattr(outcome, 'option_id', None) == 'allow_once'
        if selected and allowed:
            return 'approve'
        return 'reject'

    async def emit_user(self, session_id: str, text: str) -> None:
        await self._user(session_id, text)

    async def _user(self, session_id: str, text: str) -> None:
        conn = self._conn
        send = None if conn is None else getattr(conn, 'session_update', None)
        if send is None:
            return
        if self._redactor is not None:
            text = self._redactor.redact(text)
        await send(
            session_id=session_id,
            update=UserMessageChunk(
                session_update='user_message_chunk',
                content=TextContentBlock(type='text', text=text),
            ),
        )

    async def emit_assistant(self, session_id: str, text: str) -> None:
        await self._say(session_id, text)

    async def _say(self, session_id: str, text: str) -> None:
        conn = self._conn
        send = None if conn is None else getattr(conn, 'session_update', None)
        if send is None:
            return
        await send(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update='agent_message_chunk',
                content=TextContentBlock(type='text', text=text),
            ),
        )


def _stdio_servers(servers: list[object]) -> tuple[McpServerStdio, ...]:
    kept: list[McpServerStdio] = []
    for server in servers:
        if isinstance(server, HttpMcpServer | SseMcpServer):
            raise _refuse(
                'mcpServers 只有 stdio',
                f'收到 {type(server).__name__}',
                '编辑器里把 MCP 改成 stdio；http 与 sse 不接',
            )
        if not isinstance(server, McpServerStdio):
            raise _refuse(
                'mcpServers 的每一项是 stdio MCP server',
                f'收到 {type(server).__name__}',
                '按 ACP 的 stdio 形状传 name、command、args 与 env',
            )
        kept.append(server)
    return tuple(kept)


class _Sink:
    def __init__(self, agent: SyndicaAcp, session_id: str) -> None:
        self._agent = agent
        self._session_id = session_id

    async def message(self, text: str) -> None:
        await self._agent.emit_assistant(self._session_id, text)

    async def user_message(self, text: str) -> None:
        await self._agent.emit_user(self._session_id, text)

    async def tool(
        self,
        tool_call_id: str,
        name: str,
        status: str,
        raw_input: str,
        raw_output: str,
    ) -> None:
        await self._agent.emit_tool(
            self._session_id, tool_call_id, name, status, raw_input, raw_output
        )

    async def permission(self, tool_call_id: str, name: str, arguments: str) -> str:
        return await self._agent.decide_permission(self._session_id, tool_call_id, name, arguments)


def _is_tool_result(payload: dict[str, Any]) -> bool:
    block = payload.get('block')
    return isinstance(block, dict) and cast(dict[str, object], block).get('role') == 'tool'


def _clip(text: str) -> str:
    if len(text) <= _OUTPUT_CAP:
        return text
    return text[:_OUTPUT_CAP]


def _stop_reason(value: object) -> str:
    if value == 'max_turns':
        return 'max_turn_requests'
    if value == 'budget':
        return 'max_tokens'
    if value == 'aborted':
        return 'cancelled'
    if value == 'error':
        return 'end_turn'
    return 'end_turn'


def _as_stop(value: str) -> Any:
    if value in {'end_turn', 'max_tokens', 'max_turn_requests', 'refusal', 'cancelled'}:
        return value
    return 'end_turn'


def _tool_kind(name: str) -> str:
    if name in {'Read'}:
        return 'read'
    if name in {'Grep', 'Glob'}:
        return 'search'
    if name in {'Write', 'Edit'}:
        return 'edit'
    if name in {'Bash', 'BashOutput', 'KillBash', 'Terminal'}:
        return 'execute'
    return 'other'


def _refuse(expected: str, received: str, next_action: str) -> RequestError:
    return RequestError(
        -32602,
        f'期望: {expected} 实际: {received} 下一步: {next_action}',
        {'expected': expected, 'received': received, 'next_action': next_action},
    )
