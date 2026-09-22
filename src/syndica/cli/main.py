"""Dependency-light command-line routing."""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from typing import Literal, NoReturn, Protocol, TextIO, cast

from syndica import __version__

__all__ = ['CliHost', 'CliIO', 'InspectRequest', 'RunRequest', 'main']

_COMMANDS = frozenset(
    {'run', 'chat', 'agents', 'topology', 'sessions', 'flags', 'doctor', 'version'}
)


@dataclass(frozen=True)
class RunRequest:
    """One normalized request shared by batch and interactive CLI modes."""

    prompt: str
    config: str = '.syndica.yaml'
    agent: str | None = None
    topology: str | None = None
    session: str | None = None
    max_turns: int | None = None
    max_cost: float | None = None
    max_tokens: int | None = None
    max_wall_clock: float | None = None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class InspectRequest:
    """One list/show request for a declarative CLI resource."""

    resource: Literal['agents', 'topology', 'flags']
    action: Literal['list', 'show']
    name: str | None
    config: str = '.syndica.yaml'


@dataclass(frozen=True)
class CliIO:
    """Text streams owned by the embedding console process."""

    stdin: TextIO
    stdout: TextIO
    stderr: TextIO


class CliHost(Protocol):
    """Runtime behavior injected behind the dependency-light router."""

    def run_request(self, request: RunRequest) -> str: ...

    def inspect(self, request: InspectRequest) -> str: ...

    def doctor(self, config: str) -> str: ...

    def open_chat(self, config: str) -> str: ...

    def active_agents(self) -> str: ...


class _ParserExit(Exception):
    def __init__(self, status: int, message: str | None = None) -> None:
        self.status = status
        self.message = message


class _ArgumentParser(argparse.ArgumentParser):
    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        raise _ParserExit(status, message)

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        raise _ParserExit(
            2,
            '期望: 参数符合当前命令的 usage\n'
            f'实际: {message}\n'
            f'下一步: 运行 {self.prog} --help 查看可用参数',
        )


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('must be a positive integer') from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('must be a finite positive number') from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return parsed


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser(prog='syndica')
    parser.add_argument('--config', default='.syndica.yaml')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('prompt', nargs='+')
    target = run.add_mutually_exclusive_group()
    target.add_argument('--agent')
    target.add_argument('--topology')
    target.add_argument('--session')
    run.add_argument('--max-turns', type=_positive_int)
    run.add_argument('--max-cost', type=_positive_float)
    run.add_argument('--max-tokens', type=_positive_int)
    run.add_argument('--max-wall-clock', type=_positive_float)
    run.add_argument('--flag', action='append', default=[])
    commands.add_parser('chat')
    for resource in ('agents', 'topology', 'flags'):
        noun = commands.add_parser(resource)
        actions = noun.add_subparsers(dest='action', required=True)
        actions.add_parser('list')
        show = actions.add_parser('show')
        show.add_argument('name')
    sessions = commands.add_parser('sessions')
    session_actions = sessions.add_subparsers(dest='action', required=True)
    session_actions.add_parser('list')
    for action in ('show', 'resume', 'rm'):
        session_action = session_actions.add_parser(action)
        session_action.add_argument('name')
    commands.add_parser('doctor')
    commands.add_parser('version')
    return parser


def _with_default_run(argv: Sequence[str]) -> list[str]:
    args = list(argv)
    index = 0
    while index < len(args):
        token = args[index]
        if token == '--config':
            index += 2
            continue
        if token.startswith('--config='):
            index += 1
            continue
        break
    if index >= len(args):
        return args
    first = args[index]
    if first in _COMMANDS or first.startswith('-'):
        return args
    args.insert(index, 'run')
    return args


def _first_positional(argv: Sequence[str]) -> str | None:
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == '--config':
            index += 2
            continue
        if token.startswith('--config='):
            index += 1
            continue
        return token
    return None


def _flag_assignments(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    assignments: list[str] = []
    for index, value in enumerate(values):
        key, separator, _ = value.partition('=')
        if not separator or not key:
            raise _ParserExit(
                2,
                '期望: 每个 --flag 使用非空 key=value\n'
                f'实际: --flag 第 {index + 1} 项是 {value!r}\n'
                '下一步: 改成 --flag key=value；类型和值域由对应 Flag 声明校验',
            )
        if key in seen:
            raise _ParserExit(
                2,
                '期望: 每个 --flag key 只出现一次\n'
                f'实际: key=value 中的 {key!r} 重复\n'
                '下一步: 每个 key 只保留一个 --flag key=value',
            )
        seen.add(key)
        assignments.append(value)
    return tuple(assignments)


def _write(stream: TextIO, text: str) -> None:
    stream.write(text)
    if text and not text.endswith('\n'):
        stream.write('\n')
    stream.flush()


def _host_call(action: Callable[[], str], streams: CliIO) -> int:
    try:
        output = action()
    except Exception as exc:
        return _host_failure(exc, streams)
    _write(streams.stdout, output)
    return 0


def _host_failure(exc: Exception, streams: CliIO) -> int:
    parts = (
        getattr(exc, 'expected', None),
        getattr(exc, 'received', None),
        getattr(exc, 'next_action', None),
    )
    if all(isinstance(part, str) and part.strip() for part in parts):
        expected, received, next_action = parts
    else:
        expected = 'CLI host 完成当前命令'
        received = f'{type(exc).__name__}: {exc}'
        next_action = '检查 Runtime host 配置和错误日志后重试'
    _write(
        streams.stderr,
        f'期望: {expected}\n实际: {received}\n下一步: {next_action}',
    )
    return 1


def _chat(host: CliHost, config: str, streams: CliIO) -> int:
    try:
        target = host.open_chat(config)
    except Exception as exc:
        return _host_failure(exc, streams)
    while True:
        streams.stdout.write(f'[{target}]> ')
        streams.stdout.flush()
        line = streams.stdin.readline()
        if line == '':
            return 0
        message = line.rstrip('\r\n')
        if not message:
            continue
        if message == ':quit':
            return 0
        if message == ':agents':
            try:
                _write(streams.stdout, host.active_agents())
            except Exception as exc:
                return _host_failure(exc, streams)
            continue
        if message.startswith(':use '):
            selected = message.removeprefix(':use ')
            if not selected:
                _write(
                    streams.stderr,
                    '期望: :use 后跟完整 agent id\n'
                    '实际: 目标为空\n'
                    '下一步: 先输入 :agents 查看活动 Agent，再输入 :use <agent-id>',
                )
                return 2
            target = selected
            continue
        if message.startswith(':'):
            _write(
                streams.stderr,
                '期望: chat 控制命令是 :agents / :use <agent-id> / :quit\n'
                f'实际: 收到 {message!r}\n'
                '下一步: 改用已列出的控制命令，普通消息不要以冒号开头',
            )
            return 2
        request = RunRequest(prompt=message, config=config, session=target)
        try:
            output = host.run_request(request)
        except Exception as exc:
            return _host_failure(exc, streams)
        _write(streams.stdout, output)


def main(
    argv: Sequence[str] | None = None,
    host: CliHost | None = None,
    io: CliIO | None = None,
) -> int:
    """Route one CLI invocation."""
    received = list(sys.argv[1:] if argv is None else argv)
    streams = io or CliIO(stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    first = _first_positional(received)
    if first is not None and first.startswith('-') and first not in {'-h', '--help'}:
        _write(
            streams.stderr,
            '期望: 首个位置参数是命令名或隐式 run 的 prompt\n'
            f'实际: 收到顶层选项 {first!r}，不会猜测它属于哪个子命令\n'
            '下一步: 把 run 写在其参数之前，或用 syndica --help 查看顶层参数',
        )
        return 2
    args = _with_default_run(received)
    parser = _parser()
    try:
        with redirect_stdout(streams.stdout), redirect_stderr(streams.stderr):
            namespace = parser.parse_args(args)
        flags = _flag_assignments(namespace.flag) if namespace.command == 'run' else ()
    except _ParserExit as exc:
        if exc.message:
            _write(streams.stderr, exc.message)
        return exc.status
    if namespace.command == 'version':
        _write(streams.stdout, f'syndica {__version__}')
        return 0
    if namespace.command == 'sessions':
        _write(
            streams.stderr,
            '期望: sessions 命令有可用的 SessionStore\n'
            '实际: 里程碑 1 不提供跨进程会话持久化\n'
            '下一步: 等里程碑 6 接入 SessionStore 后再使用 sessions list/show/resume/rm',
        )
        return 2
    if host is None:
        _write(
            streams.stderr,
            f'期望: {namespace.command} 有可用的 Runtime host\n'
            '实际: 当前入口没有注入 host\n'
            '下一步: 通过 console composition root 创建 host 后重试',
        )
        return 2
    if namespace.command in {'agents', 'topology', 'flags'}:
        request = InspectRequest(
            resource=cast(Literal['agents', 'topology', 'flags'], namespace.command),
            action=namespace.action,
            name=getattr(namespace, 'name', None),
            config=namespace.config,
        )
        return _host_call(lambda: host.inspect(request), streams)
    if namespace.command == 'doctor':
        return _host_call(lambda: host.doctor(namespace.config), streams)
    if namespace.command == 'chat':
        return _chat(host, namespace.config, streams)
    request = RunRequest(
        prompt=' '.join(namespace.prompt),
        config=namespace.config,
        agent=namespace.agent,
        topology=namespace.topology,
        session=namespace.session,
        max_turns=namespace.max_turns,
        max_cost=namespace.max_cost,
        max_tokens=namespace.max_tokens,
        max_wall_clock=namespace.max_wall_clock,
        flags=flags,
    )
    return _host_call(lambda: host.run_request(request), streams)
