"""CLI 记录 trace（M3 D15 / T11）：一次 run、一个 chat 会话各一个 trace，写进
`<工作区>/.syndica/traces`。

记录不改变 run：trace 开不了头（`.syndica` 是 Agent 建出的普通文件、目录只读、是符号链接，或者
记录器自己出了错）时 stderr 一条说明，照常跑、不记。级别与脱敏规则在 run 开始时取定：Agent 中途改
`.syndica.yaml` 不影响。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from syndica import __version__
from syndica.cli.handoff import trace_directory
from syndica.kernel import (
    CausalityEdge,
    ConfigError,
    FileTraceStore,
    InMemoryTraceStore,
    RuleRedactor,
    Span,
    SpanStatus,
    TraceHeader,
    TraceLevel,
    TraceRecorder,
    TraceSource,
)

__all__ = [
    'LEVELS',
    'Finished',
    'MirroredTraceStore',
    'Recording',
    'described',
    'run_status',
    'start_recording',
    'trace_level',
    'trace_source',
]

LEVELS: dict[str, TraceLevel] = {level.value: level for level in TraceLevel}

_DEFAULT = TraceLevel.REPLAY
_CHAT_CEILING = (
    'chat 的一个会话最高只记 structure：回放 chat 要按消息记下发给了哪个 Agent，那随 M6 的会话'
    '持久化做。这个会话按 structure 记。'
)


def trace_level(
    requested: str | None,
    configured: TraceLevel | None,
    *,
    chat: bool,
    warn: Callable[[str], None],
) -> TraceLevel:
    """这次记什么：命令行的 `--trace`，没给就取配置的 `trace.level`，都没有是 `replay`。chat 最高
    `structure`（D15）；只有人点名要了更高的级别才在 stderr 说一句，默认级别不算点名。"""
    if requested is not None and requested not in LEVELS:
        raise ConfigError(
            expected=f'trace 级别是 {" / ".join(LEVELS)} 之一',
            received=f'收到 trace={requested!r}',
            next_action='改成其中之一；命令行写 --trace off|structure|replay|full',
        )
    asked = configured if requested is None else LEVELS[requested]
    level = _DEFAULT if asked is None else asked
    if chat and level in (TraceLevel.REPLAY, TraceLevel.FULL):
        if asked is not None:
            warn(_CHAT_CEILING)
        return TraceLevel.STRUCTURE
    return level


def run_status(error: BaseException | None) -> SpanStatus:
    """run 怎么结束的：没有异常是 `ok`，被取消或被 Ctrl-C 中断是 `cancelled`，其余是 `error`。"""
    if error is None:
        return SpanStatus.OK
    if isinstance(error, asyncio.CancelledError | KeyboardInterrupt):
        return SpanStatus.CANCELLED
    return SpanStatus.ERROR


@dataclass(frozen=True)
class Finished:
    """收尾之后的 trace。`complete` 为假时它没有结局：有记录没写进去（stderr 上已经有那条 ERROR），
    不能拿它回放。"""

    directory: Path
    complete: bool


class MirroredTraceStore(FileTraceStore):
    """写进 trace 目录的同时在内存里留一份：run 结束时导出的是记录器自己写的，而不是此刻目录里的
    文件。目录是 Agent 的 Bash 改得到的，从文件导出就是替 Agent 带着用户的凭证往 collector 写。
    payload 不留：导出只发它们的 sha256。"""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        super().__init__(root)
        self.mirror = InMemoryTraceStore()

    async def begin(self, header: TraceHeader) -> None:
        await super().begin(header)
        await self.mirror.begin(header)

    async def append(
        self,
        trace_id: str,
        spans: Sequence[Span] = (),
        edges: Sequence[CausalityEdge] = (),
    ) -> None:
        await super().append(trace_id, spans, edges)
        await self.mirror.append(trace_id, spans, edges)

    async def finish(self, trace_id: str, status: SpanStatus, ended_ns: int) -> None:
        await super().finish(trace_id, status, ended_ns)
        await self.mirror.finish(trace_id, status, ended_ns)


@dataclass
class Recording:
    """一次记录。`recorder` 为 None 时这次不记：级别是 `off`，或 trace 目录写不进去。"""

    recorder: TraceRecorder | None
    store: FileTraceStore | None

    async def finish(self, status: SpanStatus) -> Finished | None:
        """收尾，交回这个 trace 的目录与它完不完整；没在记录、或目录已经不在时交回 None。"""
        if self.recorder is None or self.store is None:
            return None
        await self.recorder.finish(status)
        trace_id = self.recorder.trace_id
        directory = await self.store.directory(trace_id)
        if directory is None:
            return None
        header = await self.store.get(trace_id)
        return Finished(directory, complete=header is not None and header.status is not None)


def trace_source(
    argv: Sequence[str],
    config: Path | None,
    *,
    replay_of: str | None = None,
    git_commit: str | None = None,
) -> TraceSource:
    """trace 头里的来源：命令行、配置文件的 sha256（读不到是 None）、版本、工作区所在仓库的
    HEAD，回放时加原 trace。"""
    digest = None
    if config is not None:
        try:
            digest = hashlib.sha256(config.read_bytes()).hexdigest()
        except OSError:
            digest = None
    return TraceSource(
        argv=tuple(argv),
        config_sha256=digest,
        version=__version__,
        git_commit=git_commit,
        replay_of=replay_of,
    )


def described(error: BaseException) -> str:
    """一个异常的一行说明：带三件套的写成三件套，别的写类型与文字。"""
    parts = [getattr(error, name, None) for name in ('expected', 'received', 'next_action')]
    if all(isinstance(part, str) and part for part in parts):
        expected, received, next_action = parts
        return f'期望: {expected}；实际: {received}；下一步: {next_action}'
    return f'{type(error).__name__}: {error}'


async def start_recording(
    root: Path,
    level: TraceLevel,
    redactor: RuleRedactor,
    *,
    argv: Sequence[str],
    config: Path | None,
    warn: Callable[[str], None],
    git_commit: str | None = None,
    mirror: bool = False,
) -> Recording:
    """开始记录：建 `.syndica/traces`、写下 trace 的头。写不进去时 stderr 说一句、这次不记。"""
    if level is TraceLevel.OFF:
        return Recording(None, None)
    try:
        directory = trace_directory(root)
        store = MirroredTraceStore(directory) if mirror else FileTraceStore(directory)
        source = trace_source(argv, config, git_commit=git_commit)
        recorder = TraceRecorder(store, redactor, level=level, source=source)
        await recorder.begin()
    except Exception as error:
        warn(
            redactor.redact(
                f'这次 run 不记录 trace：{described(error)}。run 照常进行；要关掉这条提示就加 '
                '--trace off，或在 .syndica.yaml 写 trace: {level: off}。'
            )
        )
        return Recording(None, None)
    return Recording(recorder, store)
