"""连接一个 MCP server，列出工具，串行调用。

stdio 的环境必须整份交给 SDK：它只继承六个变量，漏交的名字不会出现在子进程里，交了的会盖掉
那六个里的同名项。协商版本不在接受表里就拒绝。调用超时取服务超时和墙钟剩余的较小者。
服务中途退出交回三件套，不自动重连。stderr 整行脱敏之后才截，超过上限仍继续读。stdio 服务关闭
之后它的后代一个不留，含离开了进程组的。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import logging
import os
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from types import MappingProxyType, TracebackType
from typing import Any, Protocol, TextIO, cast

import httpx2
from mcp.client._transport import TransportStreams
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from syndica.backends.mcp.process import ProcessTree, StderrLines
from syndica.backends.mcp.tools import (
    ExcludedTool,
    RemoteTool,
    build_tools,
    render_result,
)
from syndica.kernel import ConfigError, Redactor, Tool, ToolResult, TrustLevel
from syndica.kernel.driving import driving_agent

__all__ = [
    'ACCEPTED_PROTOCOL_VERSIONS',
    'McpClient',
    'connect_http',
    'connect_stdio',
]

logger = logging.getLogger(__name__)

# SDK 声明的五版。多出来的版本要先改这张表，不能跟着 SDK 自动放宽。
ACCEPTED_PROTOCOL_VERSIONS = (
    '2024-11-05',
    '2025-03-26',
    '2025-06-18',
    '2025-11-25',
    '2026-07-28',
)
_PAGE_CAP = 100
_DEFAULT_STDERR_LIMIT = 1_048_576


class _SessionView(Protocol):
    protocol_version: object


class _Wire(Protocol):
    session: _SessionView

    def __aenter__(self) -> Awaitable[object]: ...

    def __aexit__(self, exc_type: object, exc: object, traceback: object) -> Awaitable[object]: ...

    def list_tools(
        self, *, cursor: str | None = None, cache_mode: str = 'use'
    ) -> Awaitable[object]: ...

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: float | None = None,
    ) -> Awaitable[object]: ...


class McpClient:
    """一条已经或即将握手的连接。工具在进入上下文时取一次，之后不再翻页。"""

    def __init__(
        self,
        name: str,
        protocol: object,
        redactor: Redactor,
        timeout: object,
        *,
        high: object = (),
        low: object = (),
        tap: _StderrTap | None = None,
        http: httpx2.AsyncClient | None = None,
    ) -> None:
        shown_name = cast(object, name)
        if not isinstance(shown_name, str) or not shown_name:
            raise ConfigError(
                expected='MCP 服务名是非空字符串',
                received=f'收到 {name!r}，类型是 {type(name).__name__}',
                next_action='使用 interop.mcp.servers 里声明的那个 name',
            )
        self.name = shown_name
        self._protocol: object = protocol
        self._redactor = redactor
        self._timeout = _timeout(timeout)
        self._high = _names(high, 'high_risk')
        self._low = _names(low, 'low_risk')
        self._tap = tap
        self._http: httpx2.AsyncClient | None = http
        self._tools: Mapping[str, Tool] = MappingProxyType({})
        self.excluded: tuple[ExcludedTool, ...] = ()
        self.protocol_version: str | None = None
        self._entered = False
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()
        self._walls: dict[str, Callable[[], float | None]] = {}
        self._ready: asyncio.Future[None] | None = None
        self._stopped: asyncio.Event | None = None
        self._drive_task: asyncio.Task[None] | None = None

    @property
    def tools(self) -> Mapping[str, Tool]:
        return self._tools

    def bind_wall(self, agent_id: str, probe: Callable[[], float | None]) -> None:
        """这个 Agent 的墙钟剩余。调用时取 `min(服务超时, 剩余)`。"""
        self._walls[agent_id] = probe

    async def __aenter__(self) -> McpClient:
        # SDK 的取消域必须在进入它的那个任务里退出。Runtime 关机在另一个任务里调 aclose，
        # 所以握手、列工具和退出都留在这个任务上，调用方只等它。
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        stopped = self._stopped = asyncio.Event()
        drive = self._drive_task = asyncio.create_task(self._drive(), name=f'mcp-{self.name}')
        try:
            await self._ready
        except BaseException:
            stopped.set()
            with contextlib.suppress(Exception):
                await drive
            raise
        return self

    async def _drive(self) -> None:
        ready = self._ready
        assert ready is not None
        try:
            await _wire(self._protocol).__aenter__()
            self._entered = True
            version = _version_of(self._protocol)
            if version not in ACCEPTED_PROTOCOL_VERSIONS:
                raise _bad_version(self.name, version)
            self.protocol_version = version
            logger.info('mcp negotiated %s %s', self.name, version)
            remotes = await self._list_all()
            tools, excluded = build_tools(
                self.name,
                remotes,
                high=self._high,
                low=self._low,
                invoke=self.invoke,
            )
            self._tools = MappingProxyType(tools)
            self.excluded = excluded
            if not ready.done():
                ready.set_result(None)
            stopped = self._stopped
            assert stopped is not None
            await stopped.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise
        finally:
            if self._entered:
                self._entered = False
                await _wire(self._protocol).__aexit__(None, None, None)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # 任务已经被取消时，接下来的每一个 await 都会立刻再抛 CancelledError，连接就关不掉。
        task = asyncio.current_task()
        cancelled = exc_type is asyncio.CancelledError
        if cancelled and task is not None:
            while task.cancelling():
                task.uncancel()
        try:
            await self.aclose()
        finally:
            if cancelled:
                raise asyncio.CancelledError

    async def aclose(self) -> None:
        async with self._close_lock:
            first = not self._closed
            self._closed = True
            if first and self._stopped is not None:
                self._stopped.set()
            task = self._drive_task
            tap = self._tap if first else None
            http = self._http if first else None
            if first:
                self._tap = None
                self._http = None
        if task is not None:
            with contextlib.suppress(Exception):
                await task
        if tap is not None:
            tap.close()
        if http is not None:
            await http.aclose()

    async def invoke(self, remote: str, arguments: Mapping[str, object]) -> ToolResult:
        """串行调用。超时、断连和已经关闭都交回不可信的三件套，取消继续往上抛。"""
        if self._closed or not self._entered:
            return self._down('这个客户端已经关闭')
        async with self._call_lock:
            if self._closed or not self._entered:
                return self._down('这个客户端已经关闭')
            limit = self._limit_for_call()
            if limit <= 0:
                return self._timed_out(limit)
            try:
                async with asyncio.timeout(limit):
                    result = await _call(self._protocol, remote, dict(arguments), limit)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                return self._timed_out(limit)
            except Exception as exc:
                return self._broken(exc)
            content = getattr(result, 'content', ())
            if not isinstance(content, Sequence) or isinstance(content, str | bytes):
                content = ()
            return render_result(
                cast(Sequence[object], content),
                getattr(result, 'structured_content', None),
                is_error=getattr(result, 'is_error', False) is True,
            )

    async def _list_all(self) -> list[RemoteTool]:
        found: list[RemoteTool] = []
        seen: set[str] = set()
        cursor: str | None = None
        for page_index in range(1, _PAGE_CAP + 1):
            page = await _list_page(self._protocol, cursor)
            tools = getattr(page, 'tools', None)
            if isinstance(tools, Sequence) and not isinstance(tools, str | bytes):
                found.extend(_remote(tool) for tool in cast(Sequence[object], tools))
            nxt = getattr(page, 'next_cursor', None)
            if not isinstance(nxt, str) or not nxt:
                return found
            if nxt in seen:
                raise ConfigError(
                    expected=f'MCP 服务 {self.name!r} 的 next_cursor 不重复',
                    received=f'游标 {nxt!r} 再次出现',
                    next_action='让这个服务的分页向前走，或在最后一页省略 next_cursor',
                )
            if page_index == _PAGE_CAP:
                raise ConfigError(
                    expected=(
                        f'MCP 服务 {self.name!r} 的 tools/list 在 {_PAGE_CAP} 页内'
                        '用空的 next_cursor 结束'
                    ),
                    received=f'第 {_PAGE_CAP} 页之后 next_cursor 仍是 {nxt!r}',
                    next_action='让这个服务把工具放进更少的页；Syndica 不会一直翻下去',
                )
            seen.add(nxt)
            cursor = nxt
        return found

    def _limit_for_call(self) -> float:
        limit = self._timeout
        agent_id = driving_agent()
        if agent_id is None:
            return limit
        probe = self._walls.get(agent_id)
        if probe is None:
            return limit
        remaining = cast(object, probe())
        if remaining is None:
            return limit
        if isinstance(remaining, bool) or not isinstance(remaining, int | float):
            raise ConfigError(
                expected=f'Agent {agent_id!r} 的墙钟剩余是秒数或 None',
                received=f'收到 {remaining!r}，类型是 {type(remaining).__name__}',
                next_action='墙钟探针返回 float；没有墙钟限制就返回 None。不要传 True',
            )
        return min(limit, float(remaining))

    def _down(self, reason: str) -> ToolResult:
        return ToolResult(
            output=(
                f'期望 MCP 服务 {self.name!r} 仍在连接。'
                f'实际{reason}。'
                f'下一步：新开一轮以重新连接 {self.name!r}；关闭之后不会自动重连。'
            ),
            trust=TrustLevel.UNTRUSTED,
        )

    def _timed_out(self, limit: float) -> ToolResult:
        return ToolResult(
            output=(
                f'期望 MCP 服务 {self.name!r} 在 {limit:g} 秒内交回结果。'
                f'实际这次调用超时。'
                f'下一步：加大这个服务的超时或缩小这次调用；Syndica 不会自动重连。'
            ),
            trust=TrustLevel.UNTRUSTED,
        )

    def _broken(self, exc: Exception) -> ToolResult:
        detail = self._redactor.redact(f'{type(exc).__name__}: {exc}')
        return ToolResult(
            output=(
                f'期望 MCP 服务 {self.name!r} 在这次调用期间保持连接。'
                f'实际连接断了：{detail}。'
                f'下一步：修好 {self.name!r} 后新开一轮；这一轮不会自动重连。'
            ),
            trust=TrustLevel.UNTRUSTED,
        )


class _StderrTap:
    """子进程 stderr 的读端。整行脱敏之后才截；写满上限之后继续读，否则管道会把进程堵死。"""

    def __init__(self, redactor: Redactor, limit: int, label: str) -> None:
        read_fd, write_fd = os.pipe()
        self._read = os.fdopen(read_fd, 'rb', buffering=0)
        self._write = os.fdopen(write_fd, 'w', closefd=True)
        self._redactor = redactor
        self._limit = limit
        self._label = label
        self._thread = threading.Thread(target=self._run, name='mcp-stderr', daemon=True)
        self._thread.start()

    @property
    def file(self) -> TextIO:
        return self._write

    def close(self) -> None:
        try:
            self._write.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)
        try:
            self._read.close()
        except OSError:
            pass

    def _run(self) -> None:
        lines = StderrLines(
            self._redactor,
            self._limit,
            emit=self._log_line,
            capped=self._log_cap,
            clipped=self._log_clipped,
        )
        while True:
            try:
                chunk = self._read.read(4096)
            except OSError:
                break
            if not chunk:
                break
            lines.feed(chunk)
        lines.close()

    def _log_line(self, redacted: str) -> None:
        logger.info('external process %s stderr: %s', self._label, redacted)

    def _log_cap(self) -> None:
        logger.warning(
            '期望: 外部进程 %s 的 stderr 不超过 %s 字节。'
            ' 实际: 超出的部分已丢弃，读取仍在继续（停止读取会让管道写满、进程堵死）。'
            ' 下一步: 让这个进程少写 stderr，或把它自己的日志写到文件里。',
            self._label,
            self._limit,
        )

    def _log_clipped(self, line_cap: int) -> None:
        logger.warning(
            '期望: 外部进程 %s 的 stderr 每行不超过 %s 字节。'
            ' 实际: 有一行超出，只记了开头，刀口前那段连续的非空白也丢了（它可能是半截凭证）。'
            ' 下一步: 让这个进程按行写 stderr，或把大段输出写到文件里。',
            self._label,
            line_cap,
        )


@contextlib.asynccontextmanager
async def _supervised_stdio(
    params: StdioServerParameters, errlog: TextIO
) -> AsyncGenerator[TransportStreams]:
    # SDK 收尾时 server 按时退出就不再对它的进程组发信号，组里的孙进程留下；setsid 离开了组的
    # 后代 SDK 本来就够不着。这一层与 SDK 的上下文在同一个任务里进出（SDK 的取消域要求）。
    transport = stdio_client(params, errlog=errlog)
    tree: ProcessTree | None = None
    try:
        async with transport as streams:
            tree = ProcessTree(_server_pid(transport))
            tree.start()
            try:
                yield streams
            finally:
                # SDK 接下来关 stdin，server 一退出它的后代就挂到 init 名下，趁父子链还在再记一遍。
                tree.stop()
                tree.record()
    finally:
        if tree is not None:
            await tree.reap()


def _server_pid(transport: object) -> int:
    # SDK 不交出它拉起的进程，只能从挂起在 yield 上的生成器里读。读不到就不连：连上了也收不干净。
    frame = getattr(getattr(transport, 'gen', None), 'ag_frame', None)
    local_vars = cast(dict[str, object], getattr(frame, 'f_locals', None) or {})
    pid = getattr(local_vars.get('process'), 'pid', None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        try:
            installed = importlib.metadata.version('mcp')
        except importlib.metadata.PackageNotFoundError:
            installed = '未知版本'
        raise ConfigError(
            expected='mcp SDK 的 stdio_client 里有它拉起的 server 进程（局部变量 process）',
            received=f'mcp {installed} 里读到的是 {pid!r}，关闭之后没法收掉 server 的后代',
            next_action=(
                '安装 syndica[mcp] 声明的 mcp 版本；在 Syndica 适配这个版本之前，改用 HTTP 服务'
            ),
        )
    return pid


def connect_stdio(
    name: str,
    argv: object,
    *,
    env: object,
    cwd: str,
    redactor: Redactor,
    timeout: object,
    high: object = (),
    low: object = (),
    stderr_limit: object = _DEFAULT_STDERR_LIMIT,
) -> McpClient:
    """stdio。`env` 是完整环境，不是增量。返回的客户端还没握手。"""
    command = _argv(argv)
    environment = _env(env)
    limit = _limit(stderr_limit)
    seconds = _timeout(timeout)
    tap = _StderrTap(redactor, limit, name)
    params = StdioServerParameters(
        command=command[0],
        args=list(command[1:]),
        env=environment,
        cwd=cwd,
    )
    try:
        protocol = Client(
            _supervised_stdio(params, tap.file),
            mode='auto',
            cache=None,
            read_timeout_seconds=seconds,
        )
    except BaseException:
        tap.close()
        raise
    return McpClient(
        name,
        protocol,
        redactor,
        seconds,
        high=high,
        low=low,
        tap=tap,
    )


def connect_http(
    name: str,
    url: object,
    *,
    redactor: Redactor,
    timeout: object,
    bearer: object = None,
    high: object = (),
    low: object = (),
) -> McpClient:
    """Streamable HTTP。鉴权头来自调用方已经读出的令牌值，这里不读环境。"""
    endpoint = _http_url(url)
    seconds = _timeout(timeout)
    headers: dict[str, str] | None = None
    if bearer is not None:
        if not isinstance(bearer, str) or not bearer:
            raise ConfigError(
                expected='bearer 是非空字符串，或省略表示不带鉴权头',
                received=f'收到 {bearer!r}，类型是 {type(bearer).__name__}',
                next_action='从用户级 interop.yaml 点名的 bearer_env 里读出值再传入',
            )
        headers = {'Authorization': f'Bearer {bearer}'}
    # 调用方提供的客户端 SDK 不负责关。超时盖住连接和读，避免一次握手无限等。
    http = httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(seconds))
    try:
        protocol = Client(
            streamable_http_client(endpoint, http_client=http),
            mode='auto',
            cache=None,
            read_timeout_seconds=seconds,
        )
    except BaseException:
        _close_http(http)
        raise
    return McpClient(name, protocol, redactor, seconds, high=high, low=low, http=http)


def _wire(protocol: object) -> _Wire:
    return cast(_Wire, protocol)


def _version_of(protocol: object) -> object:
    return _wire(protocol).session.protocol_version


def _bad_version(name: str, version: object) -> ConfigError:
    accepted = '、'.join(ACCEPTED_PROTOCOL_VERSIONS)
    return ConfigError(
        expected=f'MCP 服务 {name!r} 协商到的协议版本是 {accepted} 之一',
        received=f'收到 {version!r}',
        next_action='让对端改用其中一版；版本表之外的协议直接拒绝',
    )


async def _list_page(protocol: object, cursor: str | None) -> object:
    return await _wire(protocol).list_tools(cursor=cursor, cache_mode='bypass')


async def _call(protocol: object, remote: str, arguments: dict[str, Any], limit: float) -> object:
    return await _wire(protocol).call_tool(remote, arguments, read_timeout_seconds=limit)


def _remote(tool: object) -> RemoteTool:
    annotations = getattr(tool, 'annotations', None)
    read_only = None if annotations is None else getattr(annotations, 'read_only_hint', None)
    destructive = None if annotations is None else getattr(annotations, 'destructive_hint', None)
    return RemoteTool(
        name=cast(str, getattr(tool, 'name', '')),
        description=getattr(tool, 'description', None),
        input_schema=getattr(tool, 'input_schema', None),
        read_only_hint=read_only,
        destructive_hint=destructive,
    )


def _argv(argv: object) -> tuple[str, ...]:
    if isinstance(argv, str | bytes) or not isinstance(argv, Sequence):
        raise ConfigError(
            expected='MCP stdio 的 argv 是非空字符串列表，不经 shell',
            received=f'收到 {argv!r}，类型是 {type(argv).__name__}',
            next_action='写成 [程序, 参数, ...]；一个字符串会被 shell 拆开',
        )
    items: list[str] = []
    for item in cast(Sequence[object], argv):
        if not isinstance(item, str) or not item:
            raise ConfigError(
                expected='argv 的每一项是非空字符串，且至少有程序名',
                received=f'收到 {argv!r}',
                next_action='去掉空项；程序名放在第一项',
            )
        items.append(item)
    if not items:
        raise ConfigError(
            expected='argv 至少有程序名',
            received='收到空列表',
            next_action='把可执行文件放在第一项',
        )
    return tuple(items)


def _env(env: object) -> dict[str, str]:
    if isinstance(env, str) or not isinstance(env, Mapping):
        raise ConfigError(
            expected='MCP stdio 的 env 是完整的环境映射，不是在宿主环境上追加',
            received=f'收到 {env!r}，类型是 {type(env).__name__}',
            next_action='传入 Agent 环境的整份副本；漏掉的名字不会出现在子进程里',
        )
    out: dict[str, str] = {}
    for key, value in cast(Mapping[object, object], env).items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfigError(
                expected='env 的键和值都是字符串',
                received=f'收到键 {key!r}、值 {value!r}',
                next_action='只放字符串；这个映射会原样成为子进程的环境',
            )
        out[key] = value
    return out


def _names(value: object, field: str) -> frozenset[str]:
    if isinstance(value, str) or not isinstance(value, Sequence | frozenset | set):
        raise ConfigError(
            expected=f'{field} 是远端工具名的集合',
            received=f'收到 {value!r}，类型是 {type(value).__name__}',
            next_action='传一组工具名；不按这个档位点名就传空元组。不要把名字串成一个字符串',
        )
    names: list[str] = []
    for item in cast(Sequence[object] | frozenset[object] | set[object], value):
        if not isinstance(item, str):
            raise ConfigError(
                expected=f'{field} 的每一项是字符串',
                received=f'收到 {item!r}，类型是 {type(item).__name__}',
                next_action='点名的是远端工具名，不是别的配置',
            )
        names.append(item)
    return frozenset(names)


def _timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConfigError(
            expected='MCP 调用超时是 > 0 的秒数',
            received=f'收到 {value!r}，类型是 {type(value).__name__}',
            next_action='传秒数，例如 30；不要传 True，YAML 的 yes 会被读成布尔',
        )
    return float(value)


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(
            expected='stderr_limit 是 >= 1 的字节数',
            received=f'收到 {value!r}，类型是 {type(value).__name__}',
            next_action='传一个正整数；0 会让第一行 stderr 就被丢掉，而读取上限的意义是留得下开头',
        )
    return value


def _close_http(http: httpx2.AsyncClient) -> None:
    """构造失败时关掉自己建的客户端。已经在事件循环里就排到稍后，避免在同步函数里 await。"""
    close = http.aclose
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(close())
        return
    loop.create_task(close())


def _http_url(url: object) -> str:
    if not isinstance(url, str) or not (url.startswith('http://') or url.startswith('https://')):
        raise ConfigError(
            expected='MCP HTTP 端点以 http:// 或 https:// 开头',
            received=f'收到 {url!r}',
            next_action='改成 Streamable HTTP 的 URL；stdio 服务走 command，不走这里',
        )
    return url
