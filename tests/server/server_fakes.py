from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import uvicorn

from syndica.kernel import (
    Budget,
    ConfigError,
    DeliveryResult,
    Event,
    EventBus,
    RuleRedactor,
    Subscription,
)
from syndica.server import Credentials, Live, ServerSettings, create_app

SECRET = 'sekrit-value-123456'


ARTIFACT: dict[str, Any] = {
    'id': 'file:report.html',
    'path': 'report.html',
    'kind': 'document',
    'mime': 'text/html',
    'version': 'sha256:old',
    'producer': 'worker',
    'is_deliverable': False,
}
CONTENT = b'<script>fetch("/api/v1/sessions")</script>'


class FakeTerminal:
    def __init__(self, cols: int, rows: int) -> None:
        self.size = (cols, rows)
        self.written: list[bytes] = []
        self.output: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = False

    async def read(self) -> bytes:
        return await self.output.get()

    def write(self, data: bytes) -> None:
        self.written.append(data)
        self.output.put_nowait(b'echo:' + data)

    def resize(self, cols: int, rows: int) -> None:
        self.size = (cols, rows)

    async def close(self) -> None:
        self.closed = True


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
        self.trace: str | None = 'trace-1'
        self.calls: list[tuple[Any, ...]] = []
        self.terminals: list[FakeTerminal] = []
        self.screens: dict[tuple[str, str], list[str]] = {}

    @property
    def sid(self) -> str:
        return self._sid

    @property
    def root_id(self) -> str:
        return self._sid

    @property
    def subscription(self) -> Subscription:
        return self._subscription

    def install_tools(self, tools: Mapping[str, Any]) -> None:
        self.calls.append(('install_tools', dict(tools)))

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

    def trace_id(self) -> str | None:
        return self.trace

    def flags(self) -> list[dict[str, Any]]:
        return [
            {'key': 'x', 'value': f'v {SECRET}', 'default': 1, 'description': 'd', 'choices': []}
        ]

    async def artifact_list(self) -> list[dict[str, Any]]:
        return [{**ARTIFACT, 'versions': 2}]

    async def artifact_versions(self, artifact_id: str) -> list[dict[str, Any]]:
        self.calls.append(('versions', artifact_id))
        return [ARTIFACT, {**ARTIFACT, 'version': 'sha256:new'}]

    async def artifact_content(self, artifact_id: str, version: str) -> tuple[bytes, str]:
        self.calls.append(('content', artifact_id, version))
        return CONTENT, 'text/html'

    async def artifact_preview(self, artifact_id: str, version: str) -> str:
        return f'<script>alert(1)</script> {SECRET}'

    async def artifact_diff(self, artifact_id: str, old: str, new: str) -> dict[str, Any]:
        self.calls.append(('diff', artifact_id, old, new))
        return {'summary': '1 行不同', 'changed': True, 'unified': f'-a\n+{SECRET}'}

    async def artifact_feedback(
        self, artifact_id: str, version: str, text: str, lines: tuple[int, int] | None
    ) -> dict[str, Any] | None:
        self.calls.append(('feedback', artifact_id, version, text, lines))
        return {
            'id': 'feedback-1',
            'statement': text,
            'kind': 'judgment',
            'status': 'proposed',
            'origin': 'feedback',
        }

    async def rubrics(self) -> dict[str, Any]:
        return {'goal': 'g', 'rubrics': []}

    async def activate_rubric(self, rubric_id: str) -> None:
        self.calls.append(('activate', rubric_id))

    def open_terminal(self, cols: int, rows: int) -> FakeTerminal:
        terminal = FakeTerminal(cols, rows)
        self.terminals.append(terminal)
        return terminal

    async def agent_screen(
        self, agent_id: str, handle: str, since: int, wait: float
    ) -> dict[str, Any]:
        if handle == 'missing':
            raise ConfigError(expected='a terminal', received='none', next_action='list them')
        chunks = self.screens.get((agent_id, handle), [])
        index = since
        if index >= len(chunks):
            await asyncio.sleep(wait)
            return {'text': '', 'end': index, 'running': False, 'exit_code': 0, 'truncated': False}
        return {
            'text': chunks[index],
            'end': index + 1,
            'running': True,
            'exit_code': None,
            'truncated': False,
        }

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

    def topologies(self) -> list[dict[str, Any]]:
        return [
            {'name': 'team', 'slots': [{'name': 'lead', 'agent': 'Orchestrator', 'is_root': True}]}
        ]

    async def traces(self) -> list[dict[str, Any]]:
        return [{'trace_id': 'trace-1', 'started_ns': 1, 'ended_ns': None, 'status': None}]

    async def trace(self, trace_id: str) -> dict[str, Any]:
        if trace_id != 'trace-1':
            raise ConfigError(expected='a trace', received='none', next_action='list them')
        return {
            'trace_id': trace_id,
            'status': None,
            'started_ns': 1,
            'ended_ns': None,
            'agents': [],
            'tool_calls': [
                {
                    'call_id': 'c1',
                    'name': 'Bash',
                    'agent_id': 'a1',
                    'outcome': 'ok',
                    'start_ns': 2,
                    'end_ns': 3,
                }
            ],
            'spawns': [],
            'llm_calls': 1,
            'tokens': 15,
        }


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
    adapters: tuple[Any, ...] = (),
    protocol_mounts: tuple[Any, ...] = (),
    mount_factory: Any = None,
) -> AsyncIterator[Server]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    sock.listen(16)
    port = int(sock.getsockname()[1])
    credentials = credentials or Credentials()
    live = Live(assembly, grace_s=grace_s)
    mounts = protocol_mounts
    if mount_factory is not None:
        mounts = (*mounts, *mount_factory(port))
    app = create_app(
        live,
        credentials,
        ServerSettings(port=port, nonce='nonce-1', version='test', static_dir=static_dir),
        adapters=adapters,
        protocol_mounts=mounts,
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
