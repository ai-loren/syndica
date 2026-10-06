from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import uvicorn

from syndica.kernel import Budget, DeliveryResult, Event, EventBus, RuleRedactor, Subscription
from syndica.server import Credentials, Live, ServerSettings, create_app

SECRET = 'sekrit-value-123456'


class FakeHandle:
    def __init__(self, sid: str) -> None:
        self.bus = EventBus(capacity=64)
        self._subscription = self.bus.subscribe()
        self._sid = sid
        self.delivered: list[tuple[str, str]] = []
        self.answers: list[tuple[str, str, str]] = []
        self.aborted: list[str] = []
        self.granted: list[tuple[str, Budget]] = []
        self.closed: list[BaseException | None] = []
        self.deliver_ok = True

    @property
    def sid(self) -> str:
        return self._sid

    @property
    def root_id(self) -> str:
        return self._sid

    @property
    def subscription(self) -> Subscription:
        return self._subscription

    async def deliver(self, target: str, text: str) -> DeliveryResult:
        self.delivered.append((target, text))
        if not self.deliver_ok:
            return DeliveryResult(ok=False, reason='agent is gone')
        return DeliveryResult(ok=True, message_id='m1')

    async def answer(self, agent_id: str, request_id: str, text: str) -> DeliveryResult:
        self.answers.append((agent_id, request_id, text))
        return DeliveryResult(ok=True)

    async def abort(self, agent_id: str) -> None:
        self.aborted.append(agent_id)

    async def snapshot(self) -> dict[str, Any]:
        return {
            'agents': [
                {'agent_id': self._sid, 'name': 'Orchestrator', 'parent_id': None, 'status': 'idle'}
            ],
            'transcripts': {
                self._sid: {
                    'generation': 0,
                    'blocks': [
                        {
                            'role': 'user',
                            'text': f'token={SECRET}',
                            'trust': 'trusted',
                            'kind': None,
                            'tool_calls': [],
                            'tool_call_id': None,
                            'artifacts': [],
                            'tool_meta': {},
                        }
                    ],
                }
            },
            'budget': None,
            'progress': None,
        }

    def grant(self, agent_id: str, budget: Budget) -> dict[str, Any]:
        self.granted.append((agent_id, budget))
        return {'limit': {'turns': budget.turns}}

    def redactor(self) -> RuleRedactor:
        return RuleRedactor(secrets=(SECRET,))

    async def close(self, failure: BaseException | None) -> None:
        self.closed.append(failure)
        await self.bus.close()

    async def emit(self, kind: str, agent_id: str = 'a1', **payload: Any) -> None:
        await self.bus.emit(Event(kind, agent_id, payload))


class FakeAssembly:
    def __init__(self) -> None:
        self.handles: list[FakeHandle] = []
        self.opened: list[dict[str, Any]] = []
        self.removed: list[str] = []
        self.checks: list[dict[str, Any]] = [{'name': 'config', 'ok': True}]

    def redactor(self) -> RuleRedactor:
        return RuleRedactor(secrets=(SECRET,))

    async def open(self, **options: Any) -> FakeHandle:
        self.opened.append(options)
        handle = FakeHandle(options['resume'] or f's{len(self.handles)}')
        self.handles.append(handle)
        return handle

    async def list_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                'sid': 'stored',
                'agent': 'Orchestrator',
                'status': 'active',
                'turns': 2,
                'created_at': 1.0,
                'updated_at': 2.0,
            }
        ]

    async def remove_session(self, sid: str) -> None:
        self.removed.append(sid)

    def roles(self) -> list[str]:
        return ['Orchestrator', 'Worker']

    def doctor(self) -> list[dict[str, Any]]:
        return self.checks


@dataclass
class Server:
    base: str
    port: int
    credentials: Credentials
    live: Live
    assembly: Any

    def login(self) -> dict[str, str]:
        return {'authorization': f'Bearer {self.token}'}

    token: str = ''


@asynccontextmanager
async def running(
    assembly: Any,
    *,
    grace_s: float = 60.0,
    static_dir: Any = None,
    credentials: Credentials | None = None,
) -> AsyncIterator[Server]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    sock.listen(16)
    port = int(sock.getsockname()[1])
    credentials = credentials or Credentials()
    live = Live(assembly, grace_s=grace_s)
    app = create_app(
        live,
        credentials,
        ServerSettings(port=port, nonce='nonce-1', version='test', static_dir=static_dir),
    )
    server = uvicorn.Server(
        uvicorn.Config(app, log_level='warning', lifespan='on', ws='websockets-sansio')
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(1000):
        if server.started:
            break
        await asyncio.sleep(0.01)
    running = Server(f'http://127.0.0.1:{port}', port, credentials, live, assembly)
    async with httpx.AsyncClient(base_url=running.base) as client:
        response = await client.post('/api/v1/login', json={'code': credentials.issue_code()})
        running.token = response.json()['token']
    try:
        yield running
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


async def sse_events(
    client: httpx.AsyncClient,
    path: str,
    headers: dict[str, str],
    count: int,
    *,
    last_event_id: str | None = None,
) -> list[dict[str, Any]]:
    """读 `count` 帧（reset 也算一帧）就断开。"""
    events: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    sent = dict(headers)
    if last_event_id is not None:
        sent['last-event-id'] = last_event_id
    async with client.stream('GET', path, headers=sent) as response:
        assert response.status_code == 200, await response.aread()
        async for line in response.aiter_lines():
            if line.startswith('id: '):
                current['id'] = line[4:]
            elif line.startswith('event: '):
                current['event'] = line[7:]
            elif line.startswith('data: '):
                current['data'] = json.loads(line[6:])
            elif line == '' and current:
                events.append(current)
                current = {}
                if len(events) >= count:
                    break
    return events
