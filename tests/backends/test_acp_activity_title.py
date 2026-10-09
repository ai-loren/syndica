"""托管的 ACP Agent 报上来的活动标题先脱敏、再截到 200 字，才进 `external_activity` 事件。"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from acp.agent.connection import AgentSideConnection
from acp.client.connection import ClientSideConnection
from acp.schema import (
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    ToolCallStart,
)

from syndica.backends.acp.model import AcpAgentModel
from syndica.cli.policy import dev_guard_judge
from syndica.kernel import (
    BlockHighRisk,
    ContextBlock,
    DangerJudge,
    ExternalCallPolicy,
    Runtime,
    TrustLevel,
)
from syndica.kernel.file_taint import FileTaint
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.tools.local import Read
from syndica.kernel.workspace_router import WorkspaceRouter

_SECRET = 'quizSECRETvalue-918273'


class _Titled:
    def __init__(self, title: str) -> None:
        self.conn: Any = None
        self._title = title

    def on_connect(self, conn: object) -> None:
        self.conn = conn

    async def initialize(self, protocol_version: int, **kwargs: object) -> InitializeResponse:
        del protocol_version, kwargs
        return InitializeResponse(
            protocol_version=1, agent_info=Implementation(name='t', version='0')
        )

    async def new_session(self, cwd: str, **kwargs: object) -> NewSessionResponse:
        del cwd, kwargs
        return NewSessionResponse(session_id='sess')

    async def cancel(self, session_id: str, **kwargs: object) -> None:
        del session_id, kwargs

    async def prompt(
        self, session_id: str, prompt: list[object], **kwargs: object
    ) -> PromptResponse:
        del prompt, kwargs
        await self.conn.session_update(
            session_id=session_id,
            update=ToolCallStart(
                session_update='tool_call',
                tool_call_id='t-1',
                title=self._title,
                kind='read',
                status='in_progress',
            ),
        )
        return PromptResponse(stop_reason='end_turn')


def _link(agent: _Titled):
    @asynccontextmanager
    async def connect(client: object) -> AsyncIterator[ClientSideConnection]:
        left, right = socket.socketpair()
        left.setblocking(False)
        right.setblocking(False)
        client_reader, client_writer = await asyncio.open_connection(sock=left)
        agent_reader, agent_writer = await asyncio.open_connection(sock=right)
        server = AgentSideConnection(agent, agent_writer, agent_reader, listening=False)  # pyright: ignore[reportArgumentType]
        task = asyncio.create_task(server.listen())
        conn = ClientSideConnection(client, client_writer, client_reader)  # pyright: ignore[reportArgumentType]
        try:
            yield conn
        finally:
            task.cancel()
            await server.close()
            client_writer.close()
            agent_writer.close()

    return connect


async def test_an_activity_title_is_redacted_before_it_is_clipped_to_200_chars(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'ws'
    root.mkdir()
    router = WorkspaceRouter(
        LocalWorkspace(str(root), shell='/bin/sh', env={'PATH': os.environ.get('PATH', '')}),
        taint=FileTaint(root),
    )
    runtime = Runtime()
    model = AcpAgentModel(
        'titled',
        connect=_link(_Titled('y' * 190 + _SECRET + 'z' * 300)),
        tools={'Read': Read(router)},
        policy=ExternalCallPolicy(
            judge=DangerJudge(), policy=BlockHighRisk(), guard=dev_guard_judge(root)
        ),
        cwd=str(root),
        runtime=runtime,
        redactor=RuleRedactor(secrets=[_SECRET]),
    )
    subscription = runtime.events.subscribe()
    try:
        await model.create([ContextBlock(role='user', content='go', trust=TrustLevel.TRUSTED)])
        event = await asyncio.wait_for(anext(subscription), 1)
    finally:
        subscription.close()
        await model.aclose()
        await runtime.shutdown()
        await router.close()
    assert event.kind == 'external_activity'
    title = event.payload['title']
    assert len(title) == 200
    assert title.startswith('y' * 190 + '[REDACTED')
    assert _SECRET[:8] not in title
