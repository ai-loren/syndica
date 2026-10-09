"""收到提示就写文件、开终端的 ACP Agent。

每一步的结果或拒绝原文以 JSON 交回；stdout 只放协议字节。
"""

from __future__ import annotations

import asyncio
import json
import os


async def _serve() -> None:
    from acp.agent.connection import AgentSideConnection
    from acp.schema import (
        AgentMessageChunk,
        Implementation,
        InitializeResponse,
        NewSessionResponse,
        PromptResponse,
        TextContentBlock,
    )
    from acp.stdio import stdio_streams

    state: dict[str, object] = {}

    def refusal(exc: Exception) -> dict[str, object]:
        return {'message': str(exc), 'data': getattr(exc, 'data', None)}

    class _SideEffects:
        def on_connect(self, conn: object) -> None:
            state['conn'] = conn

        async def initialize(self, protocol_version: int, **kwargs: object) -> InitializeResponse:
            del protocol_version, kwargs
            return InitializeResponse(
                protocol_version=1, agent_info=Implementation(name='side-effects', version='0')
            )

        async def new_session(self, cwd: str, **kwargs: object) -> NewSessionResponse:
            del kwargs
            state['cwd'] = cwd
            return NewSessionResponse(session_id='s1')

        async def prompt(
            self, session_id: str, prompt: list[object], **kwargs: object
        ) -> PromptResponse:
            del prompt, kwargs
            conn: object = state['conn']
            cwd = str(state['cwd'])
            outcome: dict[str, object] = {}
            try:
                await conn.write_text_file(  # type: ignore[attr-defined]
                    session_id=session_id, path=os.path.join(cwd, 'pwned.txt'), content='written'
                )
                outcome['write'] = 'ok'
            except Exception as exc:
                outcome['write'] = refusal(exc)
            try:
                term = await conn.create_terminal(  # type: ignore[attr-defined]
                    session_id=session_id, command='touch', args=['pwned-by-terminal']
                )
                await conn.wait_for_terminal_exit(  # type: ignore[attr-defined]
                    session_id=session_id, terminal_id=term.terminal_id
                )
                outcome['terminal'] = 'ok'
            except Exception as exc:
                outcome['terminal'] = refusal(exc)
            await conn.session_update(  # type: ignore[attr-defined]
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update='agent_message_chunk',
                    content=TextContentBlock(type='text', text=json.dumps(outcome)),
                ),
            )
            return PromptResponse(stop_reason='end_turn')

        async def cancel(self, session_id: str, **kwargs: object) -> None:
            del session_id, kwargs

    reader, writer = await stdio_streams()
    AgentSideConnection(_SideEffects(), writer, reader)  # pyright: ignore[reportArgumentType]
    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(_serve())
