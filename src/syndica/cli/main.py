"""Dependency-light command-line routing."""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from typing import Any, Literal, NoReturn, Protocol, TextIO, cast

from syndica import __version__
from syndica.cli.terminal import terminal_safe

__all__ = [
    'EXIT_INTERRUPTED',
    'CliHost',
    'CliIO',
    'CliOutcome',
    'InspectRequest',
    'RunRequest',
    'TraceRequest',
    'main',
]

# 被 Ctrl-C 中断：优先于 1（没通过）与 3（被拒绝），中断本身就是没做完的原因。
EXIT_INTERRUPTED = 130
_COMMANDS = frozenset(
    {
        'run',
        'chat',
        'agents',
        'topology',
        'sessions',
        'flags',
        'modules',
        'config',
        'doctor',
        'version',
        'trace',
    }
)
# `trace prune` 不点名时留下的、有结局的 trace 个数。
PRUNE_KEEP = 50
_TRACE_LEVELS = ('off', 'structure', 'replay', 'full')
_TRACE_ID_HELP = 'trace id、它的唯一前缀，或 run 结束时 stderr 上打出的 trace 目录名'


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
    trust_module_output: tuple[str, ...] = ()
    no_write: bool = False
    handoff: bool = False
    git_mode: str | None = None
    trace: str | None = None
    argv: tuple[str, ...] = ()
    # 跨进程续一份落盘会话是显式动作，只有顶层 `run --session` 置真；chat 里的 `:use` 不隐式恢复别
    # 的会话（评审 M4）。session 为 None 时无关紧要。
    allow_resume: bool = False


@dataclass(frozen=True)
class CliOutcome:
    """一条命令的结果与退出码。host 交回 `str` 等同于 `CliOutcome(str, 0)`。"""

    output: str
    exit_code: int


@dataclass(frozen=True)
class InspectRequest:
    """One list/show request for a declarative CLI resource."""

    resource: Literal['agents', 'topology', 'flags', 'modules']
    action: Literal['list', 'show']
    name: str | None
    config: str = '.syndica.yaml'


@dataclass(frozen=True)
class TraceRequest:
    """`syndica trace ...` 的一次请求：`list`、`show <id>`、`replay <id>`、`export <id>`、`prune`。
    `trace_id` 是 id、它的前缀或目录名；`names` 是 `prune` 点名要删的那几个（点了名就只删它们）；
    `otlp` 是 `export` 发往的端点。"""

    action: Literal['list', 'show', 'replay', 'export', 'prune']
    trace_id: str | None = None
    allow_divergence: bool = False
    keep: int = PRUNE_KEEP
    names: tuple[str, ...] = ()
    otlp: str | None = None
    config: str = '.syndica.yaml'
    argv: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionsRequest:
    """`syndica sessions ...`：`list`、`show <sid>`、`resume <sid>`、`rm <sid>`。`name` 是会话 id
    （根 Agent id），`list` 不用它。"""

    action: Literal['list', 'show', 'resume', 'rm']
    name: str | None = None
    config: str = '.syndica.yaml'
    argv: tuple[str, ...] = ()


@dataclass(frozen=True)
class CliIO:
    """Text streams owned by the embedding console process."""

    stdin: TextIO
    stdout: TextIO
    stderr: TextIO


class CliHost(Protocol):
    """Runtime behavior injected behind the dependency-light router."""

    def run_request(self, request: RunRequest) -> str | CliOutcome: ...

    def inspect(self, request: InspectRequest) -> str: ...

    def doctor(self, config: str, *, verify: bool = False) -> str | CliOutcome: ...

    def open_chat(
        self,
        config: str,
        max_turns: int | None = None,
        *,
        trace: str | None = None,
        argv: Sequence[str] = (),
        trusted_output: Sequence[str] = (),
        resume: str | None = None,
    ) -> str: ...

    def active_agents(self) -> str: ...

    def trust_config(self, config: str, sha256: str | None) -> str: ...

    def trace(self, request: TraceRequest) -> str | CliOutcome: ...

    def sessions(self, request: SessionsRequest) -> str | CliOutcome: ...


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


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('must be a non-negative integer') from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError('must be a non-negative integer')
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
    run.add_argument(
        '--no-write',
        action='store_true',
        help=(
            '只读 run：Agent 只能用 Read、Grep、Glob 与派活、问人的工具，不会改动工作区；'
            '不能与 --session 同用'
        ),
    )
    run.add_argument(
        '--handoff',
        action='store_true',
        help=(
            'run 结束后由宿主复核（workspace.verify），把交接文件写到 .syndica/handoffs/ 并打印到 '
            'stdout；交互提示改走 stderr；不能与 --session 同用'
        ),
    )
    run.add_argument(
        '--git-mode',
        choices=('commit', 'push', 'manual'),
        help=(
            '--handoff 的 git 收尾不询问：commit 只提交、push 提交并推送（前置条件不满足时退出 '
            '3）、manual 不提交；不能与 --no-write 同用'
        ),
    )
    chat = commands.add_parser('chat')
    chat.add_argument('--max-turns', type=_positive_int)
    for command in (run, chat):
        command.add_argument(
            '--trust-module-output',
            action='append',
            default=[],
            metavar='MODULE',
            help=(
                '把这个第三方 Module 的工具结果当作可信（可重复）；缺省一律标成不可信，'
                '项目配置放不开这一项'
            ),
        )
        command.add_argument(
            '--trace',
            choices=_TRACE_LEVELS,
            help=(
                '记录级别：off 不记，structure 只记 span 与边，replay 再记回放要的东西（默认，或 '
                '.syndica.yaml 的 trace.level），full 再记完整请求；chat 最高 structure'
            ),
        )
    for resource in ('agents', 'topology', 'flags', 'modules'):
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
    config = commands.add_parser('config')
    config_actions = config.add_subparsers(dest='action', required=True)
    trust = config_actions.add_parser('trust')
    trust.add_argument('--sha256')
    doctor = commands.add_parser('doctor')
    doctor.add_argument(
        '--verify',
        action='store_true',
        help='自检之后在 Agent 的环境里逐条跑 workspace.verify，任一条失败退出 1',
    )
    commands.add_parser('version')
    trace = commands.add_parser('trace')
    trace_actions = trace.add_subparsers(dest='action', required=True)
    trace_actions.add_parser(
        'list', help='列出 trace：开始时间（UTC）、id、状态、Agent 数、span 数、大小、命令'
    )
    show = trace_actions.add_parser(
        'show', help='时间线，以及谁创建了谁、谁在等谁、最慢的工具调用、各 Agent 的模型调用'
    )
    show.add_argument('trace_id', help=_TRACE_ID_HELP)
    replay = trace_actions.add_parser(
        'replay', help='离线回放：0 复现，1 分叉或结局不同，2 回放文档用不了'
    )
    replay.add_argument('trace_id', help=_TRACE_ID_HELP)
    replay.add_argument(
        '--allow-divergence',
        action='store_true',
        help='分叉之后接着回放、把每一处分叉都报出来（仍以 1 退出）',
    )
    export = trace_actions.add_parser(
        'export', help='把一个 trace 按 OTLP/HTTP JSON 发给 collector（失败以 1 退出）'
    )
    export.add_argument('trace_id', help=_TRACE_ID_HELP)
    export.add_argument(
        '--otlp',
        help='collector 的端点（如 http://localhost:4318），不带 OTEL_EXPORTER_OTLP_HEADERS；'
        '不给时用环境里的端点与头',
    )
    prune = trace_actions.add_parser(
        'prune', help='删掉旧 trace：只留最近 --keep 个有结局的（连同它们的回放）；点名的只删点名的'
    )
    prune.add_argument('trace_ids', nargs='*', help=_TRACE_ID_HELP)
    prune.add_argument(
        '--keep', type=_non_negative_int, help=f'留下最近几个有结局的（默认 {PRUNE_KEEP}）'
    )
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


def _host_call(action: Callable[[], str | CliOutcome], streams: CliIO) -> int:
    try:
        result = action()
    except Exception as exc:
        return _host_failure(exc, streams)
    outcome = result if isinstance(result, CliOutcome) else CliOutcome(result, 0)
    _write(streams.stdout, outcome.output)
    return outcome.exit_code


def _redacted(text: str) -> str:
    """没被宿主接住的异常文字可能出自第三方代码：按内置规则脱敏再显示。内核只在出错时才 import：
    `version` 命令不加载内核。"""
    from syndica.kernel import RuleRedactor

    return str(RuleRedactor().redact(text))


def _host_failure(exc: Exception, streams: CliIO) -> int:
    parts = (
        getattr(exc, 'expected', None),
        getattr(exc, 'received', None),
        getattr(exc, 'next_action', None),
    )
    if all(isinstance(part, str) and part.strip() for part in parts):
        expected, received, next_action = parts
    else:
        notes = [str(note) for note in getattr(exc, '__notes__', ())]
        expected = 'CLI host 完成当前命令'
        received = _redacted(
            f'{type(exc).__name__}: {exc}' + ''.join(f'（{note}）' for note in notes)
        )
        next_action = (
            '修好上面说的那个 Runtime Module，或从配置的 modules 里删掉它，再重跑'
            if any('Runtime Module' in note for note in notes)
            else '检查 Runtime host 配置和错误日志后重试'
        )
    text = f'期望: {expected}\n实际: {received}\n下一步: {next_action}'
    # 宿主可以给一行说明放在三件套前面（比如是哪个 Agent 以错误结束）。
    headline = getattr(exc, 'headline', None)
    if isinstance(headline, str) and headline.strip():
        text = f'{headline}\n{text}'
    # 异常文字可能带着模型或提供方回来的内容：转义之后才进终端。
    _write(streams.stderr, terminal_safe(text))
    # 宿主可以说这是哪一类失败：2 是「给的东西用不了」（trace 不存在、回放文档读不进来）。
    code = getattr(exc, 'exit_code', 1)
    return code if type(code) is int and code in (1, 2) else 1


def _chat(
    host: CliHost,
    config: str,
    streams: CliIO,
    max_turns: int | None,
    *,
    trace: str | None,
    argv: Sequence[str],
    trusted_output: Sequence[str] = (),
    resume: str | None = None,
) -> int:
    # 只在给了 --trust-module-output 时才传这个参数：没有它的宿主实现照样能开 chat。
    extra: dict[str, Any] = {'trusted_output': tuple(trusted_output)} if trusted_output else {}
    try:
        target = host.open_chat(config, max_turns, trace=trace, argv=argv, resume=resume, **extra)
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
            result = host.run_request(request)
        except Exception as exc:
            return _host_failure(exc, streams)
        _write(streams.stdout, result.output if isinstance(result, CliOutcome) else result)


def main(
    argv: Sequence[str] | None = None,
    host: CliHost | None = None,
    io: CliIO | None = None,
) -> int:
    """Route one CLI invocation."""
    streams = io or CliIO(stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    try:
        return _route(list(sys.argv[1:] if argv is None else argv), host, streams)
    except KeyboardInterrupt:
        # `run --handoff` 的 Ctrl-C 在宿主里处理并写明停在哪；到这里的是普通 run、chat、强制退出，
        # 以及落在宿主交回结论与打印之间的按键——那时命令已经做完，所以只能说「可能」。
        _write(
            streams.stderr,
            '被中断（Ctrl-C）：命令可能没有做完。`run --handoff` 写过交接文件的话，路径打在上面的 '
            'stderr 里，以那个文件为准；没有的话重跑这条命令。',
        )
        return EXIT_INTERRUPTED


def _route(received: list[str], host: CliHost | None, streams: CliIO) -> int:
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
        if namespace.command == 'run' and namespace.no_write and namespace.session is not None:
            raise _ParserExit(
                2,
                '期望: --no-write 用于一次新的 run\n'
                f'实际: 同时收到 --session {namespace.session}\n'
                '下一步: 去掉 --session 开一次只读的新 run；会话里的 Agent 建好时已经带着写工具，'
                '中途收不回来',
            )
        if namespace.command == 'run' and namespace.handoff and namespace.session is not None:
            raise _ParserExit(
                2,
                '期望: --handoff 用于一次新的 run\n'
                f'实际: 同时收到 --session {namespace.session}\n'
                '下一步: 去掉 --session：交接要在 run 开始前取快照、在它结束后复核，会话里的 '
                'Agent 早已开始',
            )
        if namespace.command == 'run' and namespace.git_mode is not None:
            if not namespace.handoff:
                raise _ParserExit(
                    2,
                    '期望: --git-mode 与 --handoff 一起用\n'
                    f'实际: 收到 --git-mode {namespace.git_mode}，没有 --handoff\n'
                    '下一步: 加上 --handoff：git 收尾在交接之后、按复核结果做',
                )
            if namespace.no_write:
                raise _ParserExit(
                    2,
                    '期望: --git-mode 用于会改动工作区的 run\n'
                    f'实际: 同时收到 --no-write 与 --git-mode {namespace.git_mode}\n'
                    '下一步: 去掉其中一个：只读 run 没有改动，不做 git 收尾',
                )
        if namespace.command == 'trace' and getattr(namespace, 'trace_ids', None):
            if namespace.keep is not None:
                raise _ParserExit(
                    2,
                    '期望: trace prune 要么点名删、要么按 --keep 留\n'
                    f'实际: 同时收到 {len(namespace.trace_ids)} 个 id 与 --keep {namespace.keep}\n'
                    '下一步: 去掉其中一个：点了名就只删点名的那几个，不再按个数留',
                )
    except _ParserExit as exc:
        if exc.message:
            _write(streams.stderr, exc.message)
        return exc.status
    if namespace.command == 'version':
        _write(streams.stdout, f'syndica {__version__}')
        return 0
    if namespace.command == 'sessions':
        if host is None:
            _write(
                streams.stderr,
                '期望: sessions 有可用的 Runtime host\n'
                '实际: 当前入口没有注入 host\n'
                '下一步: 通过 console composition root 创建 host 后重试',
            )
            return 2
        if namespace.action == 'resume':
            # resume 是续一份落盘会话的交互 chat，走与 chat 同一个循环（读 stdin、逐条发、打印）。
            return _chat(
                host,
                namespace.config,
                streams,
                None,
                trace=None,
                argv=received,
                resume=namespace.name,
            )
        sessions_request = SessionsRequest(
            action=namespace.action,
            name=getattr(namespace, 'name', None),
            config=namespace.config,
            argv=tuple(received),
        )
        return _host_call(lambda: host.sessions(sessions_request), streams)
    if host is None:
        _write(
            streams.stderr,
            f'期望: {namespace.command} 有可用的 Runtime host\n'
            '实际: 当前入口没有注入 host\n'
            '下一步: 通过 console composition root 创建 host 后重试',
        )
        return 2
    if namespace.command in {'agents', 'topology', 'flags', 'modules'}:
        request = InspectRequest(
            resource=cast(Literal['agents', 'topology', 'flags', 'modules'], namespace.command),
            action=namespace.action,
            name=getattr(namespace, 'name', None),
            config=namespace.config,
        )
        return _host_call(lambda: host.inspect(request), streams)
    if namespace.command == 'config':
        return _host_call(lambda: host.trust_config(namespace.config, namespace.sha256), streams)
    if namespace.command == 'trace':
        keep: int | None = getattr(namespace, 'keep', None)
        trace_request = TraceRequest(
            action=namespace.action,
            trace_id=getattr(namespace, 'trace_id', None),
            allow_divergence=getattr(namespace, 'allow_divergence', False),
            keep=PRUNE_KEEP if keep is None else keep,
            names=tuple(getattr(namespace, 'trace_ids', ())),
            otlp=getattr(namespace, 'otlp', None),
            config=namespace.config,
            argv=tuple(received),
        )
        return _host_call(lambda: host.trace(trace_request), streams)
    if namespace.command == 'doctor':
        # 只在给了 --verify 时才传这个参数：没有它的宿主实现照样能跑普通的 doctor。
        if namespace.verify:
            return _host_call(lambda: host.doctor(namespace.config, verify=True), streams)
        return _host_call(lambda: host.doctor(namespace.config), streams)
    if namespace.command == 'chat':
        return _chat(
            host,
            namespace.config,
            streams,
            namespace.max_turns,
            trace=namespace.trace,
            argv=received,
            trusted_output=namespace.trust_module_output,
        )
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
        trust_module_output=tuple(namespace.trust_module_output),
        no_write=namespace.no_write,
        handoff=namespace.handoff,
        git_mode=namespace.git_mode,
        trace=namespace.trace,
        argv=tuple(received),
        allow_resume=True,
    )
    return _host_call(lambda: host.run_request(request), streams)
