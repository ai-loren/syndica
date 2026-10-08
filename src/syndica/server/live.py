"""打开着的会话：每个会话一个泵，把内核事件抄进会话流与全局流，并维护待答表。

人的答复只答待答表里挂着的请求：没人等的答复会作为一条可信的人类消息进下一轮，所以重复或迟到的答复
在这里就拒掉，不交给内核。

无人值守默认拒绝：最后一个已认证的连接断开超过宽限期，挂着的确认按拒绝答复、提问中止（同 CLI 读到
EOF）、预算询问按不追加；此后新来的请求同样处理。等待只会白白占住会话与它的墙钟。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

from syndica.kernel import Budget, Event
from syndica.server.errors import ApiError
from syndica.server.protocols import SessionAssembly, SessionHandle
from syndica.server.streams import Stream

__all__ = ['UNATTENDED_GRACE_S', 'Live', 'LiveSession', 'Pending']

logger = logging.getLogger(__name__)

UNATTENDED_GRACE_S = 30.0

_GONE = frozenset({'aborting', 'zombie'})

_REQUEST_KINDS = {
    'tool_confirmation_requested': 'confirmation',
    'user_input_requested': 'user_input',
    'user_question_requested': 'user_question',
}


@dataclass
class Pending:
    request_id: str
    kind: str
    agent_id: str
    data: dict[str, Any]
    answer: asyncio.Future[Budget | None] | None = field(default=None, repr=False)

    def view(self) -> dict[str, Any]:
        return {
            'request_id': self.request_id,
            'kind': self.kind,
            'agent_id': self.agent_id,
            **self.data,
        }


class LiveSession:
    def __init__(self, live: Live, handle: SessionHandle) -> None:
        self._live = live
        self.handle = handle
        self.sid = handle.sid
        self.stream = Stream()
        self.pending: dict[str, Pending] = {}
        # 每个 Agent 最近一次量到的进度：进度只经事件出来，快照从这里取。
        self.progress: dict[str, dict[str, Any]] = {}
        # 产物 id → 最近一次交付时模型写的 note（`Deliver.note`）。产物索引不存自由文本，交付记录在
        # trace 与事件里；server 进程重启之后只剩 trace 里那一份。
        self.notes: dict[str, str] = {}
        self._dropped = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._pump = asyncio.create_task(self._run(), name=f'server-pump:{self.sid}')

    async def _run(self) -> None:
        async for event in self.handle.subscription:
            self._observe(event)

    def _observe(self, event: Event) -> None:
        dropped = self.handle.subscription.dropped
        if dropped > self._dropped:
            # 内核总线按订阅者丢最旧的：丢过一次，流里就有洞（也可能丢了请求或它的了结），客户端只能
            # 重取快照，待答表按内核此刻的状态对一遍。
            self._dropped = dropped
            self.stream.reset()
            self._spawn(self._reconcile())
        payload = dict(event.payload)
        request = _REQUEST_KINDS.get(event.kind)
        if request is not None:
            pending = Pending(str(payload.get('request_id')), request, event.agent_id, payload)
            self.pending[pending.request_id] = pending
            if self._live.unattended:
                self._spawn(self._decline(pending))
        elif event.kind == 'human_request_resolved':
            self.pending.pop(str(payload.get('request_id')), None)
        elif event.kind == 'agent_removed' or (
            event.kind == 'agent_status' and payload.get('status') in _GONE
        ):
            # 等待被中止或拆除时内核会发了结事件；这里再兜一层：这个 Agent 不会再等任何答复。
            self._forget(event.agent_id)
        elif event.kind == 'progress_measured':
            self.progress[event.agent_id] = payload
        elif event.kind == 'artifact_registered':
            ref = payload.get('ref')
            note = payload.get('note')
            if isinstance(ref, dict) and isinstance(note, str) and note:
                self.notes[str(cast('dict[str, object]', ref).get('id'))] = note
        self.publish(
            event.kind,
            {
                'agent_id': event.agent_id,
                'payload': payload,
                'time_ns': event.time_ns,
            },
        )

    def _forget(self, agent_id: str) -> None:
        for request_id, pending in list(self.pending.items()):
            if pending.agent_id == agent_id and pending.kind != 'budget':
                del self.pending[request_id]

    async def _reconcile(self) -> None:
        """丢过事件之后：不在等待的 Agent 名下的确认与提问不再挂着（它的了结可能正是丢掉的那条）。
        漏掉的新请求补不回来，它按自己的超时了结。"""
        state = await self.handle.snapshot()
        waiting = {
            agent['agent_id']
            for agent in state.get('agents', [])
            if agent.get('status') == 'waiting'
        }
        for request_id, pending in list(self.pending.items()):
            if pending.kind != 'budget' and pending.agent_id not in waiting:
                del self.pending[request_id]

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        entry = self.stream.append(kind, data)
        self._live.stream.append(kind, {'sid': self.sid, 'seq': entry.seq, **data})

    def _spawn(self, work: Any) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def answer(self, request_id: str, text: str) -> None:
        pending = self.pending.get(request_id)
        if pending is None or pending.kind == 'budget':
            raise ApiError(
                409,
                expected='答复的是这个会话里还挂着的确认或提问',
                received=(
                    f'request_id={request_id!r} 不在待答表里：已经答过、超时、被中止，或不存在'
                ),
                next_action='重取会话快照看还有哪些请求挂着；预算询问用 budget_answer',
            )
        _check_answer(pending, text)
        # 先摘掉再交：两个客户端同时答同一个请求，第二个在这里就拿到 409。
        del self.pending[request_id]
        try:
            delivered = await self.handle.answer(pending.agent_id, request_id, text)
        except BaseException:
            self.pending.setdefault(request_id, pending)
            raise
        if not delivered.ok:
            # 没交出去就还挂着，人可以再试；Agent 真的不等了，它的状态事件会把它摘掉。
            self.pending.setdefault(request_id, pending)
            raise ApiError(
                409,
                expected=f'把答复交给 agent {pending.agent_id}',
                received=delivered.reason or '内核没有给出原因',
                next_action='重取会话快照；这个 Agent 可能已经结束或被拆除',
            )

    async def ask_budget(self, agent_id: str, view: Mapping[str, Any]) -> Budget | None:
        if self._live.unattended:
            return None
        request_id = uuid.uuid4().hex
        answer: asyncio.Future[Budget | None] = asyncio.get_running_loop().create_future()
        pending = Pending(request_id, 'budget', agent_id, {'view': dict(view)}, answer)
        self.pending[request_id] = pending
        self.publish(
            'budget_question',
            {'agent_id': agent_id, 'payload': {'request_id': request_id, 'view': dict(view)}},
        )
        try:
            return await answer
        finally:
            self.pending.pop(request_id, None)
            self.publish(
                'budget_question_resolved',
                {'agent_id': agent_id, 'payload': {'request_id': request_id}},
            )

    def answer_budget(self, request_id: str, budget: Budget | None) -> None:
        pending = self.pending.get(request_id)
        if pending is None or pending.answer is None or pending.answer.done():
            raise ApiError(
                409,
                expected='答复的是这个会话里还挂着的预算询问',
                received=f'request_id={request_id!r} 不是挂着的预算询问',
                next_action='重取会话快照看还有哪些请求挂着',
            )
        pending.answer.set_result(budget)

    async def _decline(self, pending: Pending) -> None:
        """无人值守的处置：确认按拒绝、提问中止、预算不追加。只处置自己从待答表里摘下来的那一个：
        已经了结的（人答了、超时、被中止）再答一次就是一条没人等的可信消息。"""
        if pending.kind == 'budget':
            if pending.answer is not None and not pending.answer.done():
                pending.answer.set_result(None)
            return
        if self.pending.pop(pending.request_id, None) is None:
            return
        if pending.kind == 'confirmation':
            await self.handle.answer(pending.agent_id, pending.request_id, 'reject')
        else:
            await self.handle.abort(pending.agent_id)

    def decline_all(self) -> None:
        for pending in list(self.pending.values()):
            self._spawn(self._decline(pending))

    async def snapshot(self) -> dict[str, Any]:
        # 位置先取、快照后取：两者之间到的事件会再到一次（客户端按 id 幂等合并），反过来就会丢。
        as_of = self.stream.position
        state = await self.handle.snapshot()
        return {
            'sid': self.sid,
            'root_id': self.handle.root_id,
            'as_of': as_of,
            **state,
            'progress': dict(self.progress) or None,
            'pending': [pending.view() for pending in self.pending.values()],
        }

    async def close(self, failure: BaseException | None = None) -> None:
        for pending in list(self.pending.values()):
            await self._decline(pending)
        try:
            await self.handle.close(failure)
        finally:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
            for task in list(self._tasks):
                task.cancel()
            self.stream.close()


def _check_answer(pending: Pending, text: str) -> None:
    if pending.kind == 'confirmation' and text not in ('approve', 'reject'):
        raise ApiError(
            422,
            expected="确认的答复是 'approve' 或 'reject'",
            received=f'收到 {text!r}',
            next_action='在确认对话框里点批准或拒绝',
        )
    if pending.kind == 'user_question':
        options = pending.data.get('options')
        if isinstance(options, list) and text not in options:
            raise ApiError(
                422,
                expected=f'答复逐字等于选项之一：{options!r}',
                received=f'收到 {text!r}',
                next_action='从列出的选项里选一个',
            )


class Live:
    """server 进程里全部打开着的会话，以及全局流与无人值守的判定。"""

    def __init__(self, assembly: SessionAssembly, *, grace_s: float = UNATTENDED_GRACE_S) -> None:
        self.assembly = assembly
        self.stream = Stream()
        self.sessions: dict[str, LiveSession] = {}
        self.unattended = False
        self._grace_s = grace_s
        self._connections = 0
        self._timer: asyncio.Task[None] | None = None
        self._opening = asyncio.Lock()

    def attach(self) -> None:
        self._connections += 1
        self.unattended = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def detach(self) -> None:
        self._connections -= 1
        self._arm()

    async def _unattend(self) -> None:
        await asyncio.sleep(self._grace_s)
        self._timer = None
        if self._connections:
            return
        self.unattended = True
        for session in list(self.sessions.values()):
            session.decline_all()

    def get(self, sid: str) -> LiveSession:
        session = self.sessions.get(sid)
        if session is None:
            raise ApiError(
                404,
                expected=f'会话 {sid!r} 在这个 server 里打开着',
                received='它没有打开（可能只在磁盘上，或已经关闭）',
                next_action=f'POST /api/v1/sessions/{sid}/resume 把它打开，或在会话列表里重新进入',
            )
        return session

    def _arm(self) -> None:
        if self._connections == 0 and self._timer is None and not self.unattended:
            self._timer = asyncio.create_task(self._unattend(), name='server-unattended')

    async def open_session(
        self,
        *,
        resume: str | None,
        role: str,
        tokens: int | None = None,
        cost: Decimal | None = None,
        wall_clock_s: float | None = None,
        turns: int | None = None,
    ) -> LiveSession:
        async with self._opening:
            if resume is not None and resume in self.sessions:
                return self.sessions[resume]
            asker = _LateAsker()
            handle = await self.assembly.open(
                resume=resume,
                role=role,
                tokens=tokens,
                cost=cost,
                wall_clock_s=wall_clock_s,
                turns=turns,
                ask_budget=asker.ask,
            )
            session = LiveSession(self, handle)
            asker.session = session
            self.sessions[session.sid] = session
            self.stream.append('session_opened', {'sid': session.sid})
            # 只有 REST 客户端、从没连过事件流时同样算无人值守：从第一个会话起开始计时。
            self._arm()
            return session

    async def close(self, sid: str) -> None:
        session = self.sessions.pop(sid, None)
        if session is None:
            return
        try:
            await session.close()
        finally:
            self.stream.append('session_closed', {'sid': sid})

    def end_streams(self) -> None:
        """让每条事件流交完缓冲就结束。server 收到退出信号时先调它：uvicorn 的优雅关闭要等在飞的
        响应结束，而 SSE 响应不会自己结束，不先关流，关机就一直等到被强制结束。"""
        self.stream.close()
        for session in self.sessions.values():
            session.stream.close()

    async def _close_logged(self, sid: str) -> None:
        try:
            await self.close(sid)
        except Exception:
            logger.exception(
                '期望: 会话 %s 在 server 关机时存盘并释放锁。 实际: 收尾时抛出。'
                ' 下一步: 看上面的栈；会话锁随进程退出释放，磁盘上的会话停在最后一次存盘。',
                sid,
            )

    async def shutdown(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        # 并发收尾：`down` 给关机的总时限是固定的，一个一个关，会话一多后面的就来不及存盘。
        await asyncio.gather(*(self._close_logged(sid) for sid in list(self.sessions)))
        self.stream.close()


class _LateAsker:
    """预算询问要落到打开之后才有的那个会话上：装配在 spawn 之前就要拿到回调。"""

    def __init__(self) -> None:
        self.session: LiveSession | None = None

    async def ask(self, agent_id: str, view: Mapping[str, Any]) -> Budget | None:
        if self.session is None:
            return None
        return await self.session.ask_budget(agent_id, view)
