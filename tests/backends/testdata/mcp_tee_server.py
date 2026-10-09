"""SDK 写的 echo MCP 服务，把 stdio 上收到与发出的每一行原样记进 `argv[1]`。stdout 只放协议字节。"""

from __future__ import annotations

import io
import sys
from collections.abc import AsyncIterator
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

_LOG = open(sys.argv[1], 'w', encoding='utf-8')  # noqa: SIM115


class _TeeIn:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __aiter__(self) -> AsyncIterator[str]:
        return self._lines()

    async def _lines(self) -> AsyncIterator[str]:
        async for line in self._inner:
            _LOG.write(line)
            _LOG.flush()
            yield line


class _TeeOut:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def write(self, data: str) -> None:
        _LOG.write(data)
        _LOG.flush()
        await self._inner.write(data)

    async def flush(self) -> None:
        await self._inner.flush()


async def on_list_tools(ctx: object, params: object) -> types.ListToolsResult:
    del ctx, params
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name='echo',
                description='echo text',
                input_schema={
                    'type': 'object',
                    'properties': {'text': {'type': 'string'}},
                    'required': ['text'],
                },
            )
        ]
    )


async def on_call_tool(ctx: object, params: Any) -> types.CallToolResult:
    del ctx
    text = (params.arguments or {}).get('text', '')
    return types.CallToolResult(content=[types.TextContent(type='text', text=str(text))])


async def main() -> None:
    server = Server('echo', on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    stdin = _TeeIn(anyio.wrap_file(io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')))
    stdout = _TeeOut(anyio.wrap_file(io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')))
    async with stdio_server(stdin, stdout) as (read_stream, write_stream):  # pyright: ignore[reportArgumentType]
        await server.run(read_stream, write_stream, server.create_initialization_options())


anyio.run(main)
