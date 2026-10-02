"""CLI 记录 trace（M3 D15 / T11）：一次 run、一个 chat 会话各一个 trace，写进
`<工作区>/.syndica/traces`。

记录不改变 run：trace 开不了头（`.syndica` 是 Agent 建出的普通文件、目录只读、是符号链接，或者
记录器自己出了错）时 stderr 一条说明，照常跑、不记。级别与脱敏规则在 run 开始时取定：Agent 中途改
`.syndica.yaml` 不影响。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from syndica import __version__
from syndica.cli.handoff import trace_directory
from syndica.kernel import (
    ConfigError,
    FileTraceStore,
    RuleRedactor,
    SpanStatus,
    TraceLevel,
    TraceRecorder,
    TraceSource,
)

__all__ = ['LEVELS', 'Finished', 'Recording', 'run_status', 'start_recording', 'trace_level']

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


def _source(argv: Sequence[str], config: Path | None) -> TraceSource:
    digest = None
    if config is not None:
        try:
            digest = hashlib.sha256(config.read_bytes()).hexdigest()
        except OSError:
            digest = None
    return TraceSource(argv=tuple(argv), config_sha256=digest, version=__version__)


def _described(error: BaseException) -> str:
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
) -> Recording:
    """开始记录：建 `.syndica/traces`、写下 trace 的头。写不进去时 stderr 说一句、这次不记。"""
    if level is TraceLevel.OFF:
        return Recording(None, None)
    try:
        store = FileTraceStore(trace_directory(root))
        recorder = TraceRecorder(store, redactor, level=level, source=_source(argv, config))
        await recorder.begin()
    except Exception as error:
        warn(
            redactor.redact(
                f'这次 run 不记录 trace：{_described(error)}。run 照常进行；要关掉这条提示就加 '
                '--trace off，或在 .syndica.yaml 写 trace: {level: off}。'
            )
        )
        return Recording(None, None)
    return Recording(recorder, store)
