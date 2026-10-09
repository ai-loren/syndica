"""不理会取消、并留下一个孙进程的 ACP Agent。stdout 只放协议字节。"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
from pathlib import Path


def main() -> None:
    path = Path(sys.argv[1])
    child = subprocess.Popen(['/bin/sleep', '60'])
    path.write_text(f'{child.pid}\n', encoding='utf-8')
    sys.stderr.write('sk-hang-secret\n')
    sys.stderr.flush()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    asyncio.run(_serve(path, child.pid))


async def _serve(path: Path, pid: int) -> None:
    from acp.agent.connection import AgentSideConnection
    from acp.schema import Implementation, InitializeResponse, NewSessionResponse, PromptResponse
    from acp.stdio import stdio_streams

    class _Hang:
        async def initialize(self, protocol_version: int, **kwargs: object) -> InitializeResponse:
            del protocol_version, kwargs
            path.write_text(f'{pid}\ninit\n', encoding='utf-8')  # noqa: ASYNC240
            return InitializeResponse(
                protocol_version=1, agent_info=Implementation(name='hang', version='0')
            )

        async def new_session(self, cwd: str, **kwargs: object) -> NewSessionResponse:
            del cwd, kwargs
            return NewSessionResponse(session_id='hang')

        async def prompt(
            self, session_id: str, prompt: list[object], **kwargs: object
        ) -> PromptResponse:
            del session_id, prompt, kwargs
            await asyncio.Event().wait()
            return PromptResponse(stop_reason='end_turn')

        async def cancel(self, session_id: str, **kwargs: object) -> None:
            del session_id, kwargs

    reader, writer = await stdio_streams()
    AgentSideConnection(_Hang(), writer, reader)  # pyright: ignore[reportArgumentType]
    await asyncio.Event().wait()


if __name__ == '__main__':
    main()
