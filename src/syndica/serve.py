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
import secrets
import select
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from collections.abc import Mapping
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


def up(
    stdio: CliIO,
    workspace_root: Path,
    environ: Mapping[str, str],
    *,
    config: str,
    port: int,
    foreground: bool,
    open_browser: bool,
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
        code = serve_main(['--config', config, '--port', str(port), '--foreground'])
        return CliOutcome('', code)
    handshake = _spawn(workspace_root, paths, config, port)
    url = f'http://127.0.0.1:{handshake["port"]}/#login={handshake["login"]}'
    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open_new_tab(url)
    return CliOutcome(
        f'Syndica 在 http://127.0.0.1:{handshake["port"]}/ 运行（pid {handshake["pid"]}）。\n'
        f'用这个一次性链接登录（两分钟内有效，只能用一次）：\n{url}\n'
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


def _spawn(workspace_root: Path, paths: _Paths, config: str, port: int) -> dict[str, Any]:
    paths.make()
    # stderr 不接进日志文件：那会把没脱敏的文字直接落盘。server 自己的日志经脱敏 handler 写。
    process = subprocess.Popen(
        [sys.executable, '-m', 'syndica.serve', '--config', config, '--port', str(port)],
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
        live = Live(ServerAssembly(host, path=args.config, config=config))
        app = create_app(
            live,
            credentials,
            ServerSettings(
                port=port,
                nonce=nonce,
                version=__version__,
                static_dir=Path(__file__).parent / 'server' / 'static',
            ),
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
        if args.foreground:
            print(
                f'Syndica 在 http://127.0.0.1:{port}/ 运行。一次性登录链接（两分钟内有效）：\n'
                f'http://127.0.0.1:{port}/#login={code}\nCtrl-C 停止。',
                flush=True,
            )
        else:
            _handshake(handshake, {'ok': True, 'port': port, 'login': code})
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
            loop.set_default_executor(DaemonExecutor(workers=_BLOCKING_WORKERS))
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
