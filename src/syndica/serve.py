"""server 的组合根：`syndica up / down / status` 与 server 进程本身（`python -m syndica.serve`）。

与 `__main__` 一样不属于任何一层：它把 CLI 的会话装配（`cli.assembly`）注入 server。

凭证不落盘：登录码只经握手管道交回 `up`、由 `up` 打印在人自己的终端；状态文件只记 pid、端口与进程
nonce（`/api/v1/health` 公开交出的那个），放在信任存储所在的目录下、必须在工作区之外。
`LocalWorkspace` 下 Agent 与人是同一个系统用户，这挡的是失误与顺手够到，不挡蓄意的同用户进程；真正
的边界是沙箱化的工作区后端。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import logging
import os
import re
import secrets
import select
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from collections.abc import AsyncGenerator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from syndica import __version__
from syndica.__main__ import build_host
from syndica.cli.environment import AGENT_MARKER
from syndica.cli.executor import DaemonExecutor
from syndica.cli.main import CliIO, CliOutcome
from syndica.cli.redaction import host_redactor
from syndica.cli.trust import store_path
from syndica.kernel import ConfigError, Redactor, SyndicaError
from syndica.kernel.redaction import write_all

__all__ = ['down', 'serve_main', 'status', 'up']

_HANDSHAKE_TIMEOUT_S = 60.0
_STOP_TIMEOUT_S = 15.0
# 流关掉之后仍不结束的连接最多再等这么久：之后 uvicorn 取消它们，会话照样在 lifespan 里存盘。
_GRACEFUL_S = 5
_DEPENDENCIES = ('starlette', 'uvicorn', 'websockets')
# server 事件循环默认执行器的线程数（硬约束 14：有界）。
_BLOCKING_WORKERS = 8


def _blocking_executor() -> DaemonExecutor:
    """server 事件循环的默认执行器：线程数封顶。每个会话的文件读写、产物预览都经它，不封顶的话
    并发的请求能把线程数拉到没有上界。"""
    return DaemonExecutor(workers=_BLOCKING_WORKERS)


def up(
    stdio: CliIO,
    workspace_root: Path,
    environ: Mapping[str, str],
    *,
    config: str,
    port: int,
    foreground: bool,
    open_browser: bool,
    api_token: bool = False,
) -> CliOutcome:
    _refuse_in_agent(environ, 'up')
    _require_dependencies()
    paths = _Paths.of(workspace_root, environ)
    running = _running(paths)
    if running is not None:
        raise ConfigError(
            expected='这个工作区还没有在跑的 Web 服务',
            received=f'已经有一个在跑：http://127.0.0.1:{running["port"]}/（pid {running["pid"]}）',
            next_action=(
                '回到登录过的那个浏览器标签页；要新的登录链接就先 syndica down 再 syndica up'
            ),
        )
    host = build_host(stdio, workspace_root)
    try:
        # 在前台做信任检查：有终端时这里可以问人；后台的 server 进程不问，只再验一次摘要。
        host.trusted_config(config)
    finally:
        host.close()
    if foreground:
        argv = ['--config', config, '--port', str(port), '--foreground']
        if api_token:
            argv.append('--api-token')
        code = serve_main(argv)
        return CliOutcome('', code)
    handshake = _spawn(workspace_root, paths, config, port, api_token=api_token)
    url = f'http://127.0.0.1:{handshake["port"]}/#login={handshake["login"]}'
    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open_new_tab(url)
    token_line = ''
    if api_token:
        token_line = (
            '机器客户端令牌（只显示这一次，放在 Authorization: Bearer 里；重启即失效）：\n'
            f'{handshake["api_token"]}\n'
        )
    return CliOutcome(
        f'Syndica 在 http://127.0.0.1:{handshake["port"]}/ 运行（pid {handshake["pid"]}）。\n'
        f'用这个一次性链接登录（两分钟内有效，只能用一次）：\n{url}\n'
        f'{token_line}'
        f'日志：{paths.log}\n停止：syndica down',
        0,
    )


def down(workspace_root: Path, environ: Mapping[str, str]) -> CliOutcome:
    _refuse_in_agent(environ, 'down')
    paths = _Paths.of(workspace_root, environ)
    running = _running(paths)
    if running is None:
        return CliOutcome('这个工作区没有在跑的 Web 服务。', 0)
    pid = int(running['pid'])
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _STOP_TIMEOUT_S
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.1)
    killed = _alive(pid)
    if killed:
        os.kill(pid, signal.SIGKILL)
    paths.state.unlink(missing_ok=True)
    if killed:
        return CliOutcome(
            f'Web 服务（pid {pid}）{_STOP_TIMEOUT_S:.0f} 秒内没有退出，已强制结束；'
            '在跑的会话停在最后一次存盘。',
            1,
        )
    return CliOutcome(f'已停止 Web 服务（pid {pid}）；会话已存盘。', 0)


def status(workspace_root: Path, environ: Mapping[str, str]) -> CliOutcome:
    paths = _Paths.of(workspace_root, environ)
    running = _running(paths)
    if running is None:
        return CliOutcome('这个工作区没有在跑的 Web 服务。用 syndica up 启动。', 3)
    if not running['responding']:
        return CliOutcome(
            f'期望: 在跑的 Web 服务（pid {running["pid"]}）在 2 秒内答 /api/v1/health。\n'
            '实际: 它持着 server 锁、还在跑，但这一刻没有答（多半是在打开一个大工作区的会话）。\n'
            f'下一步: 过几秒再 syndica status；一直不答就看 {paths.log}，或 syndica down 停掉它。',
            1,
        )
    return CliOutcome(
        f'在跑：http://127.0.0.1:{running["port"]}/（pid {running["pid"]}）\n日志：{paths.log}',
        0,
    )


class _Paths:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def of(cls, workspace_root: Path, environ: Mapping[str, str]) -> _Paths:
        trust = store_path(environ)
        if trust is None:
            raise ConfigError(
                expected='找得到放 server 状态的地方（信任存储所在的目录）',
                received='SYNDICA_TRUST_STORE、XDG_CONFIG_HOME 与 HOME 都没有设',
                next_action='设置 XDG_CONFIG_HOME（或 HOME）后重试',
            )
        key = hashlib.sha256(str(workspace_root.resolve()).encode()).hexdigest()[:16]
        root = trust.parent / 'servers' / key
        workspace = workspace_root.resolve()
        if root.resolve() == workspace or workspace in root.resolve().parents:
            raise ConfigError(
                expected='server 的状态目录在工作区之外',
                received=f'{root} 在工作区 {workspace} 之内',
                next_action='把 XDG_CONFIG_HOME 或 SYNDICA_TRUST_STORE 指到工作区之外',
            )
        return cls(root)

    @property
    def state(self) -> Path:
        return self.root / 'server.json'

    @property
    def lock(self) -> Path:
        return self.root / 'server.lock'

    @property
    def log(self) -> Path:
        return self.root / 'server.log'

    def make(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)


def _refuse_in_agent(environ: Mapping[str, str], command: str) -> None:
    if environ.get(AGENT_MARKER) == '1':
        raise ConfigError(
            expected=f'syndica {command} 由人在自己的终端里运行',
            received='这条命令运行在 Agent 的环境里',
            next_action=(
                '起停 Web 服务是人的操作：起服务会发一份人的登录码，停服务会掐断人的会话；'
                '请让用户自己运行'
            ),
        )


def _require_dependencies() -> None:
    missing = [name for name in _DEPENDENCIES if importlib.util.find_spec(name) is None]
    if missing:
        raise ConfigError(
            expected='装好了 Web 服务的依赖',
            received=f'缺少 {", ".join(missing)}',
            next_action="pip install 'syndica[server]' 之后再运行 syndica up",
        )


def _require_extra(module: str, extra: str, command: str) -> None:
    if importlib.util.find_spec(module) is None:
        raise ConfigError(
            expected=f'syndica {command} 要的协议 SDK（模块 {module}）已经装好',
            received=f'找不到模块 {module}',
            next_action=f"pip install 'syndica[{extra}]' 之后再运行 syndica {command}",
        )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _running(paths: _Paths) -> dict[str, Any] | None:
    """状态文件指向的 server 还活着就交回它；`responding` 说它的 `/health` 这一刻答没答。

    活不活由 server 锁定：server 进程从启动到退出一直持着它。只看 `/health` 不行——一次会话打开
    里的同步工作就能让它几秒不答，那时删掉状态文件，这个 server 就再也找不到、停不掉。只看 pid
    也不行：pid 会被重用。锁空着才是真的没在跑，这时状态文件是上一次留下的，删掉。"""
    try:
        state = json.loads(paths.state.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    try:
        with urllib.request.urlopen(
            f'http://127.0.0.1:{int(state["port"])}/api/v1/health', timeout=2
        ) as response:
            health = json.loads(response.read())
    except Exception:
        health = {}
    if isinstance(health, dict) and health.get('nonce') == state.get('nonce'):  # pyright: ignore[reportUnknownMemberType]
        return {**state, 'responding': True}
    if _lock_held(paths):
        return {**state, 'responding': False}
    paths.state.unlink(missing_ok=True)
    return None


def _lock_held(paths: _Paths) -> bool:
    """有进程持着这个工作区的 server 锁吗。只试一下，拿到了马上放掉。"""
    try:
        descriptor = os.open(paths.lock, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _spawn(
    workspace_root: Path, paths: _Paths, config: str, port: int, *, api_token: bool = False
) -> dict[str, Any]:
    paths.make()
    # stderr 不接进日志文件：那会把没脱敏的文字直接落盘。server 自己的日志经脱敏 handler 写。
    # `-P`：`-m` 默认把当前目录放在 sys.path 最前，而当前目录是 Agent 能写的工作区根——Agent 写一个
    # `uvicorn.py` 或 `syndica/`，它就跑进这个带着宿主全部环境（provider 的 key）与登录码的进程。
    command = [
        sys.executable,
        '-P',
        '-m',
        'syndica.serve',
        '--config',
        config,
        '--port',
        str(port),
    ]
    if api_token:
        command.append('--api-token')
    process = subprocess.Popen(
        command,
        cwd=workspace_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert process.stdout is not None
    line = b''
    deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_S
    while not line.endswith(b'\n'):
        left = deadline - time.monotonic()
        if left <= 0 or process.poll() is not None:
            break
        ready, _, _ = select.select([process.stdout], [], [], left)
        if not ready:
            break
        chunk = os.read(process.stdout.fileno(), 4096)
        if not chunk:
            break
        line += chunk
    process.stdout.close()
    try:
        handshake: object = json.loads(line)
    except ValueError:
        handshake = None
    if not isinstance(handshake, dict):
        if process.poll() is None:
            process.terminate()
        raise ConfigError(
            expected=f'Web 服务在 {_HANDSHAKE_TIMEOUT_S:.0f} 秒内启动并报回端口',
            received='server 进程没有报回端口就退出了，或一直没有回应',
            next_action=f'看日志 {paths.log} 里的错误',
        )
    reply = cast('dict[str, Any]', handshake)
    if not reply.get('ok'):
        raise ConfigError(
            expected=str(reply.get('expected')),
            received=str(reply.get('received')),
            next_action=str(reply.get('next_action')),
        )
    return {**reply, 'pid': process.pid}


def with_editor_mcp(config: Any, servers: Sequence[Any], role: str) -> Any:
    """把编辑器 `session/new` 里的 stdio MCP 并进这份配置。密钥式变量名拒收。"""
    from dataclasses import replace

    from syndica.cli.environment import refuse_env_name
    from syndica.cli.interop_config import McpServer

    if not servers:
        return config
    extra: list[McpServer] = []
    seen = {server.name for server in config.interop.mcp_servers}
    for server in servers:
        name = str(getattr(server, 'name', ''))
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name):
            raise ConfigError(
                expected='编辑器 MCP 的 name 是小写字母开头、最多 32 个字符（字母、数字、_、-）',
                received=f'收到 {name!r}',
                next_action='改成 files 这样的名字；它会变成工具名前缀',
            )
        if name in seen:
            raise ConfigError(
                expected='编辑器 MCP 的 name 与已声明的服务都不重复',
                received=f'{name!r} 已经有了',
                next_action='给编辑器里的这个 MCP 换一个 name',
            )
        seen.add(name)
        command = str(getattr(server, 'command', '') or '')
        if not command.strip():
            raise ConfigError(
                expected=f'MCP 服务 {name} 的 command 是要执行的程序',
                received='command 是空的',
                next_action='写成可执行文件的路径，参数放在 args 里，不要经 shell',
            )
        args = tuple(str(item) for item in getattr(server, 'args', ()) or ())
        values: list[tuple[str, str]] = []
        for item in getattr(server, 'env', ()) or ():
            variable = str(getattr(item, 'name', ''))
            refuse_env_name(variable, f'mcp:{name} env')
            values.append((variable, str(getattr(item, 'value', ''))))
        extra.append(
            McpServer(
                name=name,
                command=(command, *args),
                url=None,
                pass_env=(),
                roles=(role,),
                high_risk=(),
                low_risk=(),
                env_values=tuple(values),
            )
        )
    interop = replace(
        config.interop,
        mcp_servers=(*config.interop.mcp_servers, *extra),
        present=True,
    )
    return replace(config, interop=interop)


def mcp_stdio(host: Any, config: str) -> str:
    """stdio MCP server。日志先于 SDK 的 import 指到工作区外的文件，`basicConfig` 此后是空操作。

    返回空串：stdout 只放协议，命令结束时不再往上面补一行说明。
    """
    _require_extra('mcp', 'mcp', 'mcp')
    paths = _Paths.of(host.workspace_root, os.environ)
    paths.make()
    log = _RedactingLog(paths.root / 'mcp.log', host_redactor(os.environ))
    logging.basicConfig(level=logging.INFO, handlers=[log], force=True)
    logging.captureWarnings(True)
    try:
        asyncio.run(_mcp_stdio(host, config, log))
    finally:
        log.close()
    return ''


def acp_stdio(host: Any, config: str) -> str:
    """stdio ACP agent。日志先于 SDK 的 import 指到工作区外的文件。stdout 只放协议。"""
    _require_extra('acp', 'acp', 'acp')
    paths = _Paths.of(host.workspace_root, os.environ)
    paths.make()
    log = _RedactingLog(paths.root / 'acp.log', host_redactor(os.environ))
    logging.basicConfig(level=logging.INFO, handlers=[log], force=True)
    logging.captureWarnings(True)
    try:
        asyncio.run(_acp_stdio(host, config, log, paths.root / 'acp-sessions'))
    finally:
        log.close()
    return ''


_HEX_ID = re.compile(r'[0-9a-f]{32}')


class _AcpSessions:
    """ACP 的 sessionId 落在哪个 Syndica 会话上：一条会话一个文件，放在工作区之外的 server 目录。

    文件名是 sessionId、内容是会话 id，两者都只认 32 位小写十六进制：`session/load` 的 sessionId
    是编辑器给的，不卡住它就能拼出这个目录之外的路径。记不下只告警：这个进程里的会话照常用，
    只是换了进程之后 load 不回来。
    """

    def __init__(self, root: Path, redactor: Redactor) -> None:
        self._root = root
        self._redactor = redactor

    def remember(self, session_id: str, sid: str) -> None:
        if not (_HEX_ID.fullmatch(session_id) and _HEX_ID.fullmatch(sid)):
            logging.getLogger(__name__).warning(
                '期望: ACP 会话与 Syndica 会话的 id 都是 32 位小写十六进制。 实际: 不是，这条对照'
                '没有记下。 下一步: 这个会话换了进程之后 load 不回来，用 new_session 另开。'
            )
            return
        try:
            self._root.mkdir(mode=0o700, exist_ok=True)
            (self._root / session_id).write_text(self._redactor.redact(sid), encoding='utf-8')
        except OSError as error:
            logging.getLogger(__name__).warning(
                '期望: 在 %s 记下 ACP 会话落在哪个 Syndica 会话上。 实际: 写不进去（%s）。'
                ' 下一步: 这个会话换了进程之后 load 不回来；修好这个目录的权限再开新会话。',
                self._root,
                type(error).__name__,
            )

    def recall(self, session_id: str) -> str | None:
        if not _HEX_ID.fullmatch(session_id):
            return None
        try:
            sid = (self._root / session_id).read_text(encoding='utf-8')
        except OSError:
            return None
        return sid if _HEX_ID.fullmatch(sid) else None


async def _acp_stdio(host: Any, config_path: str, log: _RedactingLog, index: Path) -> None:
    from acp import run_agent

    from syndica.cli.handoff import session_root
    from syndica.kernel import FileSessionStore
    from syndica.server.acp_agent import SyndicaAcp, TurnBinding

    try:
        config = host.trusted_without_asking(config_path)
    except ConfigError as untrusted:
        await _acp_untrusted(untrusted, log, index)
        return
    log.redactor = host.redactor_for(config)
    roles: list[tuple[str, str]] = []
    for name in config.catalog.names():
        manifest = config.catalog.get(name)
        description = manifest.description if isinstance(manifest.description, str) else name
        roles.append((name, description))
    opened: list[Any] = []

    def trust(cwd: str) -> None:
        root = Path(cwd).resolve()
        if root != Path(host.workspace_root).resolve():
            raise ConfigError(
                expected=f'cwd 是启动 syndica acp 的工作区 {host.workspace_root}',
                received=str(root),
                next_action='在那个目录里运行 syndica acp',
            )

    async def open_turn(
        cwd: str, mode: str, servers: tuple[Any, ...] = (), *, resume: str | None = None
    ) -> TurnBinding:
        trust(cwd)
        history = None
        if resume is not None:
            record = await FileSessionStore(session_root(host.workspace_root)).load(resume)
            if record is None:
                raise ConfigError(
                    expected='sessionId 落在这个工作区 .syndica/sessions 里还在的会话上',
                    received=f'会话 {resume} 不在了',
                    next_action='用 new_session 开一条新会话；删掉的会话续不回来',
                )
            mode = record.meta.agent
            history = record.content
        parts = await host.assemble_session(
            config_path,
            max_turns=None,
            trace=None,
            argv=('syndica', 'acp'),
            trusted_output=(),
            resume=resume,
            role=mode,
            config=with_editor_mcp(config, servers, mode),
        )
        opened.append(parts)
        return TurnBinding(parts.runtime, parts.agent_id, parts.subscription, history=history)

    agent = SyndicaAcp(
        roles,
        trust,
        open_turn=open_turn,
        sessions=_AcpSessions(index, log.redactor),
        redactor=log.redactor,
        version=__version__,
    )
    try:
        await run_agent(agent)  # pyright: ignore[reportArgumentType]
    finally:
        await agent.aclose()
        for parts in opened:
            await host.close_session(parts, None)


async def _acp_untrusted(untrusted: ConfigError, log: _RedactingLog, index: Path) -> None:
    """配置没被信任时照常握手，到 `session/new` / `session/load` 再交出这条三件套：编辑器拉起的进程
    没有终端可问，启动就退出的话，编辑器只看得到进程没了。配置一个字都不读（载入角色会 import
    `impl:`），所以角色名单是个占位，信任检查先于它被用到。"""
    from acp import run_agent

    from syndica.server.acp_agent import SyndicaAcp, TurnBinding

    def refuse(cwd: str) -> None:
        del cwd
        raise untrusted

    async def no_turn(*args: Any, **kwargs: Any) -> TurnBinding:
        del args, kwargs
        raise untrusted

    agent = SyndicaAcp(
        (('untrusted', '配置没被信任，这个进程不跑任何角色'),),
        refuse,
        open_turn=no_turn,
        sessions=_AcpSessions(index, log.redactor),
        redactor=log.redactor,
        version=__version__,
    )
    try:
        await run_agent(agent)  # pyright: ignore[reportArgumentType]
    finally:
        await agent.aclose()


async def _mcp_stdio(host: Any, config_path: str, log: _RedactingLog) -> None:
    config = host.trusted_config(config_path)
    log.redactor = host.redactor_for(config)
    server, traces, router, environment = await _bind_mcp(host, config, None)
    try:
        from syndica.server.mcp_serve import serve_stdio

        await serve_stdio(server)
    finally:
        await traces.aclose()
        await router.close()
        environment.remove()


def mcp_http_mount(host: Any, config: Any, port: int, taint: Any) -> Any:
    """`/mcp` 的挂载。没装 `syndica[mcp]` 时返回 None，路由保持 501。"""
    if importlib.util.find_spec('mcp') is None:
        return None
    from syndica.cli.redaction import host_redactor
    from syndica.server import ProtocolMount
    from syndica.server.outbound import json_response

    state: dict[str, Any] = {}

    class _App:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            app = state.get('app')
            if app is None:
                response = json_response(
                    {
                        'expected': '/mcp 的会话管理器已经进入 lifespan',
                        'received': '请求到了，管理器还没准备好',
                        'next_action': '等 server 启动完成再连；刚重启时重试一次',
                    },
                    host_redactor(os.environ),
                    status=503,
                )
                await response(scope, receive, send)
                return
            await app(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan() -> AsyncGenerator[None, None]:
        server, traces, router, environment = await _bind_mcp(host, config, taint)
        try:
            from syndica.server.mcp_serve import streamable_http

            app, life = streamable_http(server, port=port, traces=traces)
            state['app'] = app
            async with life():
                yield
        finally:
            state.pop('app', None)
            await traces.aclose()
            await router.close()
            environment.remove()

    return ProtocolMount('/mcp', _App(), lifespan=lifespan)


def a2a_http_mounts(config: Any, port: int, live: Any, workspace_name: str) -> tuple[Any, ...]:
    """卡片与 `/a2a/`。没装 `syndica[a2a]` 时返回空，两条路由保持 501。"""
    if importlib.util.find_spec('a2a') is None:
        return ()
    from syndica.server import ProtocolMount
    from syndica.server.a2a_serve import (
        A2ADesk,
        SyndicaA2A,
        build_agent_card,
        card_app,
        jsonrpc_app,
    )

    skills: list[tuple[str, str]] = []
    for name in config.catalog.names():
        manifest = config.catalog.get(name)
        description = manifest.description if isinstance(manifest.description, str) else name
        skills.append((name, description))
    redactor = live.assembly.redactor()
    card = build_agent_card(workspace_name, skills, port=port)
    handler = SyndicaA2A(card=card, redactor=redactor, desk=A2ADesk(live, redactor=redactor))
    return (
        ProtocolMount('/.well-known/agent-card.json', card_app(card, redactor)),
        ProtocolMount('/a2a/', jsonrpc_app(handler)),
    )


def openai_http_mounts(config: Any, live: Any) -> tuple[Any, ...]:
    """`/v1/models`、chat completions 与 responses。不另装 SDK。"""
    from syndica.server import ProtocolMount
    from syndica.server.openai_http import (
        OpenAIHttp,
        chat_app,
        models_app,
        responses_app,
        unauthorized,
    )

    api = OpenAIHttp(live, tuple(config.catalog.names()), live.assembly.redactor())
    return (
        ProtocolMount('/v1/models', models_app(api), unauthorized=unauthorized),
        ProtocolMount('/v1/chat/completions', chat_app(api), unauthorized=unauthorized),
        ProtocolMount('/v1/responses', responses_app(api), unauthorized=unauthorized),
    )


async def _bind_mcp(host: Any, config: Any, taint: Any) -> tuple[Any, Any, Any, Any]:
    """打开与 run 同一份工作区，按配置点名工具，并给每个连接一条 trace。"""
    from syndica.cli.handoff import artifact_root, trace_root
    from syndica.cli.policy import dev_danger_options, dev_guard_judge
    from syndica.kernel import ArtifactRef, ExternalCallPolicy, FileTraceStore, RuleRedactor
    from syndica.kernel.stores.file_artifact import FileArtifactStore
    from syndica.kernel.tools.local import local_tools
    from syndica.server.mcp_serve import (
        McpPrompt,
        McpResource,
        McpTraces,
        build_mcp_server,
        read_artifact_bytes,
        resources_of,
        select_serve_tools,
    )

    router, environment = host._open_workspace(config, taint=taint)  # pyright: ignore[reportPrivateUsage]
    try:
        selected = select_serve_tools(local_tools(router.portal()), config.interop.mcp_serve_tools)
        root = host.workspace_root
        judge, table = dev_danger_options(root)
        redactor = host.redactor_for(config)
        if not isinstance(redactor, RuleRedactor):
            raise ConfigError(
                expected='MCP server 的 redactor 是 RuleRedactor',
                received=f'收到 {type(redactor).__name__}',
                next_action='用宿主的 redactor_for(config)，不要换一个不脱敏的实现',
            )
        prompts: list[McpPrompt] = []
        for name in config.catalog.names():
            manifest = config.catalog.get(name)
            description = manifest.description if isinstance(manifest.description, str) else name
            text = manifest.system_prompt
            prompts.append(
                McpPrompt(name, description, text if isinstance(text, str) else str(text))
            )
        traces = McpTraces(FileTraceStore(trace_root(root)), redactor, router)

        async def artifact_source() -> tuple[McpResource, ...]:
            location = artifact_root(root)
            if not location.is_dir():
                return ()
            refs = await FileArtifactStore(location).list()

            async def read(ref: ArtifactRef) -> bytes:
                return await read_artifact_bytes(router, ref)

            return await resources_of(refs, read)

        server = build_mcp_server(
            selected,
            policy=ExternalCallPolicy(judge=judge, policy=table, guard=dev_guard_judge(root)),
            read_only=frozenset(name for name in selected if name in {'Read', 'Grep', 'Glob'}),
            redactor=redactor,
            prompts=tuple(prompts),
            resource_source=artifact_source,
            traces=traces,
        )
    except BaseException:
        await router.close()
        environment.remove()
        raise
    return server, traces, router, environment


def serve_main(argv: list[str]) -> int:
    """server 进程本身。后台模式下，第一行 stdout 是给 `up` 的握手；握手之后 stdout / stderr 接到
    /dev/null，日志只经脱敏 handler 写。"""
    # 这个入口照样会发一份人的登录码：与 up / down 一样在 Agent 环境里拒绝。
    try:
        _refuse_in_agent(os.environ, 'serve')
    except ConfigError as error:
        sys.stderr.write(f'{error.message}\n')
        return 2
    parser = argparse.ArgumentParser(prog='syndica.serve')
    parser.add_argument('--config', default='.syndica.yaml')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--foreground', action='store_true')
    parser.add_argument('--api-token', action='store_true')
    args = parser.parse_args(argv)
    workspace_root = Path.cwd()
    handshake = sys.stdout
    paths = _Paths.of(workspace_root, os.environ)
    paths.make()
    log = _RedactingLog(paths.log, host_redactor(os.environ))
    logging.basicConfig(level=logging.WARNING, handlers=[log], force=True)
    logging.captureWarnings(True)
    host = build_host(
        CliIO(stdin=io.StringIO(''), stdout=sys.stderr, stderr=sys.stderr), workspace_root
    )
    try:
        return _serve(args, workspace_root, paths, log, host, handshake)
    except SyndicaError as error:
        _handshake(
            handshake,
            {
                'ok': False,
                'expected': error.expected,
                'received': error.received,
                'next_action': error.next_action,
            },
        )
        return 2
    except Exception:
        logging.getLogger(__name__).exception(
            '期望: Web 服务启动并一直运行到 syndica down。 实际: 以异常结束。'
            ' 下一步: 看这条记录的栈。'
        )
        _handshake(
            handshake,
            {
                'ok': False,
                'expected': 'Web 服务正常启动',
                'received': 'server 进程启动时抛出了未预期的异常',
                'next_action': f'看日志 {paths.log}',
            },
        )
        return 1
    finally:
        host.close()
        log.close()


def _serve(
    args: argparse.Namespace,
    workspace_root: Path,
    paths: _Paths,
    log: _RedactingLog,
    host: Any,
    handshake: Any,
) -> int:
    import uvicorn

    from syndica.cli.assembly import ServerAssembly
    from syndica.server import Credentials, Live, ServerSettings, create_app

    config = host.trusted_config(args.config)
    log.redactor = host.redactor_for(config)
    lock = os.open(paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(lock)
        raise ConfigError(
            expected='这个工作区还没有在跑的 Web 服务',
            received='另一个 server 进程持着这个工作区的 server 锁',
            next_action='用 syndica status 看它在哪；要换一个就先 syndica down',
        ) from error
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', args.port))
        except OSError as error:
            raise ConfigError(
                expected=f'端口 {args.port} 在 127.0.0.1 上空闲',
                received=f'绑定失败：{error}',
                next_action='换一个 --port，或不给 --port 让系统挑一个空闲的',
            ) from error
        sock.listen(64)
        port = int(sock.getsockname()[1])
        credentials = Credentials()
        nonce = secrets.token_hex(16)
        assembly = ServerAssembly(host, path=args.config, config=config)
        live = Live(assembly)
        mount = mcp_http_mount(
            host,
            config,
            port,
            assembly._taint,  # pyright: ignore[reportPrivateUsage]
        )
        mounts: list[Any] = [] if mount is None else [mount]
        mounts.extend(a2a_http_mounts(config, port, live, workspace_root.name))
        mounts.extend(openai_http_mounts(config, live))
        app = create_app(
            live,
            credentials,
            ServerSettings(
                port=port,
                nonce=nonce,
                version=__version__,
                static_dir=Path(__file__).parent / 'server' / 'static',
            ),
            protocol_mounts=tuple(mounts),
        )
        state = {
            'pid': os.getpid(),
            'port': port,
            'nonce': nonce,
            'workspace': str(workspace_root.resolve()),
            'started_at': time.time(),
        }
        temporary = paths.state.with_suffix('.tmp')
        temporary.write_text(json.dumps(state), encoding='utf-8')
        temporary.chmod(0o600)
        os.replace(temporary, paths.state)
        code = credentials.issue_code()
        api_token = credentials.issue_api_token() if args.api_token else None
        if args.foreground:
            token_line = ''
            if api_token is not None:
                token_line = (
                    '机器客户端令牌（只显示这一次，放在 Authorization: Bearer 里；重启即失效）：\n'
                    f'{api_token}\n'
                )
            print(
                f'Syndica 在 http://127.0.0.1:{port}/ 运行。一次性登录链接（两分钟内有效）：\n'
                f'http://127.0.0.1:{port}/#login={code}\n{token_line}Ctrl-C 停止。',
                flush=True,
            )
        else:
            payload: dict[str, Any] = {'ok': True, 'port': port, 'login': code}
            if api_token is not None:
                payload['api_token'] = api_token
            _handshake(handshake, payload)
            # 握手之后 `up` 已经退出、管道没人读：stdout 与 stderr 接到 /dev/null，什么都不写。
            quiet = os.open(os.devnull, os.O_WRONLY)
            os.dup2(quiet, sys.stdout.fileno())
            os.dup2(quiet, sys.stderr.fileno())
            os.close(quiet)

        class _Server(uvicorn.Server):
            """收到退出信号时先结束事件流，再走 uvicorn 的优雅关闭（会话随后在 lifespan 里存盘、
            放锁）。"""

            loop: asyncio.AbstractEventLoop | None = None

            def handle_exit(self, sig: int, frame: Any) -> None:
                super().handle_exit(sig, frame)
                if self.loop is not None:
                    self.loop.call_soon_threadsafe(live.end_streams)

        server = _Server(
            uvicorn.Config(
                app,
                log_level='warning',
                lifespan='on',
                ws='websockets-sansio',
                timeout_graceful_shutdown=_GRACEFUL_S,
                # 不给 uvicorn 自己的日志配置：它的记录要传到根 logger、经脱敏 handler 进 server
                # 日志。它自己的 handler 写 stderr，握手之后 stderr 是 /dev/null。
                log_config=None,
            )
        )

        async def run() -> None:
            loop = asyncio.get_running_loop()
            loop.set_default_executor(_blocking_executor())
            server.loop = loop
            await server.serve(sockets=[sock])

        asyncio.run(run())
        return 0
    finally:
        with contextlib.suppress(OSError):
            state_now = json.loads(paths.state.read_text(encoding='utf-8'))
            if state_now.get('pid') == os.getpid():
                paths.state.unlink()
        os.close(lock)


class _RedactingLog(logging.Handler):
    """server 进程的日志：先脱敏再写盘。内核日志里可能有工具输出与异常文字。"""

    def __init__(self, path: Path, redactor: Redactor) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
        self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self.redactor = redactor

    def emit(self, record: logging.LogRecord) -> None:
        try:
            write_all(self._fd, self.redactor.redact(self.format(record) + '\n').encode())
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self._fd)
        super().close()


def _handshake(stream: Any, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False) + '\n')
    stream.flush()


if __name__ == '__main__':
    raise SystemExit(serve_main(sys.argv[1:]))
