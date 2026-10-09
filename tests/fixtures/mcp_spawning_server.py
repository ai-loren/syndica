"""留下两个孙进程的 SDK MCP 服务：一个留在它的进程组里，一个自己调 `os.setsid()` 离开。

`python mcp_spawning_server.py <marker>`：两个孙进程都就位之后，把三个 pid 原子地写进 `<marker>`
（JSON：`server` / `plain` / `detached`）。工具 `hold` 先写 `<marker>.held` 再一直等，注解是只读。
stdout 只放协议字节。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_DETACH = (
    'import os, sys, time\n'
    'os.setsid()\n'
    'tmp = sys.argv[1] + ".tmp"\n'
    'open(tmp, "w").write(str(os.getpid()))\n'
    'os.replace(tmp, sys.argv[1])\n'
    'time.sleep(300)\n'
)


def _spawn(marker: Path) -> None:
    # argv 里带着 marker：进程表里认得出是谁留下的。
    plain = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', str(marker)])
    reported = marker.with_name(marker.name + '.detached')
    subprocess.Popen([sys.executable, '-c', _DETACH, str(reported)])
    deadline = time.monotonic() + 20
    while not reported.exists():
        if time.monotonic() > deadline:
            raise SystemExit('the setsid grandchild never reported its pid')
        time.sleep(0.02)
    pids = {'server': os.getpid(), 'plain': plain.pid, 'detached': int(reported.read_text())}
    staged = marker.with_name(marker.name + '.tmp')
    staged.write_text(json.dumps(pids), encoding='utf-8')
    os.replace(staged, marker)


def main() -> None:
    marker = Path(sys.argv[1])
    _spawn(marker)

    import anyio
    import mcp_types as types
    from mcp.server.lowlevel.server import Server
    from mcp.server.stdio import stdio_server

    async def on_list_tools(ctx: object, params: object) -> types.ListToolsResult:
        del ctx, params
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name='hold',
                    description='hold',
                    input_schema={'type': 'object'},
                    annotations=types.ToolAnnotations(read_only_hint=True),
                )
            ]
        )

    async def on_call_tool(ctx: object, params: object) -> types.CallToolResult:
        del ctx, params
        marker.with_name(marker.name + '.held').write_text('held', encoding='utf-8')
        await anyio.sleep(300)
        return types.CallToolResult(content=[types.TextContent(type='text', text='released')])

    async def serve() -> None:
        server = Server('spawning', on_list_tools=on_list_tools, on_call_tool=on_call_tool)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(serve)


if __name__ == '__main__':
    main()
