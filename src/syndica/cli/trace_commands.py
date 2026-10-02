"""`syndica trace list / show / replay / prune`（M3 T15）。

回放跑在 runner 里，而 surface 之间互不依赖：宿主收一个 `Replayer`（组合根 `syndica.__main__`
接上 `ReplayRunner`），这里只认它交回的分叉与没能重建的东西。退出码：`replay` 复现是 0、分叉或
结局不同是 1、回放文档用不了（trace 不存在、记录到一半就断了、没有回放文档、读不进来）是 2；找不到
trace 的 `show` 与 `prune` 同样是 2。
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Mapping, Sequence
from typing import Protocol, TypeVar

from syndica.cli.export import ExportTarget, TraceExporter
from syndica.cli.main import CliOutcome
from syndica.cli.terminal import one_line
from syndica.cli.trace_view import TraceRow, render_list, render_show
from syndica.kernel import (
    ConfigError,
    FileTraceStore,
    RuleRedactor,
    ScenarioDocument,
    SyndicaError,
    TraceHeader,
    TraceRecorder,
    parse_scenario_document,
)
from syndica.kernel.redaction import RULES_VERSION, Redactor

__all__ = [
    'Replayed',
    'Replayer',
    'TraceExportFailed',
    'TraceUnusable',
    'resolved',
    'trace_export',
    'trace_list',
    'trace_prune',
    'trace_replay',
    'trace_show',
    'unusable_replay',
]


# `FileTraceStore` 的目录名：`<UTC 开始时间>-<id 前 8 位>`，同一秒撞上前 8 位时是完整的 id。
_DIRECTORY_NAME = re.compile(r'\d{8}T\d{6}Z-(?:[0-9a-f]{8}|[0-9a-f]{32})')
_HEX = re.compile(r'[0-9a-f]{1,32}')

_T = TypeVar('_T')


class _Divergence(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def where(self) -> str: ...

    @property
    def detail(self) -> str: ...


class Replayed(Protocol):
    """回放的结论：不一致的地方，以及宿主档案里没能重建的 Module 与 Flag。"""

    @property
    def divergences(self) -> Sequence[_Divergence]: ...

    @property
    def unrebuilt(self) -> Sequence[str]: ...


class Replayer(Protocol):
    def __call__(
        self,
        document: ScenarioDocument,
        *,
        redactor: Redactor,
        allow_divergence: bool,
        recorder: TraceRecorder | None,
    ) -> Replayed: ...


class TraceUnusable(ConfigError):
    """给的 trace 用不了：CLI 以 2 退出。"""

    exit_code = 2


class TraceExportFailed(SyndicaError):
    """collector 没收下（或者导出器装不上）：CLI 以 1 退出，三件套经过脱敏。"""


async def resolved(store: FileTraceStore, given: str | None) -> TraceHeader:
    """按 id、它的唯一前缀或 trace 目录名找到一个 trace。目录名可以带着 stderr 上打出的那段路径，
    只认最后一级。找不到、对上不止一个都以 2 退出。"""
    if given is None:
        raise TraceUnusable(
            expected='给一个 trace id 或它的前缀',
            received='没有给',
            next_action='从 `syndica trace list` 的 id 列抄一段开头',
        )
    name = given.rstrip('/').rsplit('/', 1)[-1]
    if _DIRECTORY_NAME.fullmatch(name):
        return await _by_directory(store, name)
    if not _HEX.fullmatch(given):
        raise TraceUnusable(
            expected='trace id、它的唯一前缀（小写十六进制），或 trace 目录名',
            received=f'收到 {given!r}',
            next_action=(
                '从 `syndica trace list` 的 id 列抄一段开头，'
                '或抄 run 结束时 stderr 上打出的 trace 目录'
            ),
        )
    header = await _read(store.find(given))
    if header is None:
        raise TraceUnusable(
            expected=f'有一个 id 以 {given} 开头的 trace',
            received='.syndica/traces 里没有',
            next_action='用 `syndica trace list` 看有哪些；被 prune 掉的找不回来',
        )
    return header


async def _by_directory(store: FileTraceStore, name: str) -> TraceHeader:
    suffix = name.rsplit('-', 1)[1]
    for header in await _read(store.traces()):
        if not header.trace_id.startswith(suffix):
            continue
        directory = await _quietly(store.directory(header.trace_id))
        if directory is not None and directory.name == name:
            return header
    raise TraceUnusable(
        expected=f'有一个目录名是 {name} 的 trace',
        received='.syndica/traces 里没有（或者它的头读不进来）',
        next_action='用 `syndica trace list` 看有哪些；被 prune 掉的找不回来',
    )


async def trace_list(store: FileTraceStore) -> str:
    """一个 trace 读不了不拖垮整张表：那一行报读不进来、大小不明，`trace show` 给出原因。"""
    rows: list[TraceRow] = []
    for header in await _read(store.traces()):
        size = await _quietly(store.size(header.trace_id))
        spans = await _quietly(store.spans(header.trace_id))
        if spans is None:
            rows.append(TraceRow(header, None, None, size))
            continue
        agents = {span.agent_id for span in spans}
        rows.append(TraceRow(header, len(agents), len(spans), size))
    return render_list(rows)


async def trace_show(store: FileTraceStore, prefix: str | None) -> str:
    header = await resolved(store, prefix)
    spans = await _read(store.spans(header.trace_id))
    edges = await _read(store.edges(header.trace_id))
    return render_show(header, spans, edges)


async def trace_export(
    store: FileTraceStore,
    prefix: str | None,
    *,
    target: ExportTarget,
    resource: Mapping[str, str],
    exporter: TraceExporter,
    redactor: Redactor,
) -> CliOutcome:
    """`trace export`：发出这个 trace 的全部 span。trace 用不了（找不到、没有结局、读不了）以 2
    退出，在发之前先读一遍；collector 没收下以 1 退出。导出器的三件套可能带着 collector 回的原文
    （有的会回显鉴权头），过一遍脱敏再报。"""
    header = await resolved(store, prefix)
    if header.status is None:
        raise TraceUnusable(
            expected='导出的 trace 有结局（记录完整）',
            received=f'trace {header.trace_id} 记录到一半就断了：写入失败，或者那次 run 还没结束',
            next_action='等它结束，或者换一个完整的 trace；不完整的 trace 不导出',
        )
    await _read(store.spans(header.trace_id))
    await _read(store.edges(header.trace_id))
    try:
        count = await exporter(
            store,
            header.trace_id,
            url=target.url,
            headers=target.headers,
            resource=resource,
            redactor=redactor,
        )
    except SyndicaError as error:
        parts = ('expected', 'received', 'next_action')
        raise TraceExportFailed(
            **{part: str(redactor.redact(getattr(error, part))) for part in parts}
        ) from error
    return CliOutcome(f'导出了 trace {header.trace_id} 的 {count} 个 span。', 0)


async def trace_prune(store: FileTraceStore, keep: int, names: Sequence[str]) -> CliOutcome:
    """点了名就只删点名的那几个（没有结局的也删：那是人看过之后的决定）；没点名就按开始时间留下
    最近 `keep` 个有结局的。没有结局的不动：一次长跑的 trace 开始得最早、却还在写，崩掉的 trace
    同样没有结局，分不出来。只报删完之后确实不在了的；有没删掉的以 1 退出。"""
    if names:
        found: dict[str, TraceHeader] = {}
        for name in names:
            header = await resolved(store, name)
            found[header.trace_id] = header
        doomed = list(found.values())
        kept_unfinished: list[TraceHeader] = []
    else:
        headers = list({h.trace_id: h for h in await _read(store.traces())}.values())
        finished = sorted(
            (header for header in headers if header.status is not None),
            key=lambda header: header.started_ns,
            reverse=True,
        )
        doomed = finished[keep:]
        kept_unfinished = [header for header in headers if header.status is None]
    removed: list[TraceHeader] = []
    failed: list[tuple[TraceHeader, str]] = []
    for header in doomed:
        reason = await _removed(store, header.trace_id)
        if reason is None:
            removed.append(header)
        else:
            failed.append((header, reason))
    lines = [
        f'删掉了 {len(removed)} 个 trace'
        + ('。' if not removed else f'：{", ".join(h.trace_id[:8] for h in removed)}。')
    ]
    if not names:
        lines.append(f'留下最近 {keep} 个有结局的。')
    if kept_unfinished:
        lines.append(
            f'没有结局的 {len(kept_unfinished)} 个没动：'
            f'{", ".join(h.trace_id[:8] for h in kept_unfinished)}'
            '（还在写，或者记录到一半就断了；确认之后点名删：syndica trace prune <id>）。'
        )
    lines.extend(f'没删掉 {header.trace_id[:8]}：{reason}' for header, reason in failed)
    return CliOutcome('\n'.join(lines), 1 if failed else 0)


async def _removed(store: FileTraceStore, trace_id: str) -> str | None:
    """删掉一个 trace，并确认它真的不在了；没删掉时交回原因。"""
    try:
        await store.delete(trace_id)
        still = await store.get(trace_id)
    except (SyndicaError, OSError) as error:
        return _said(error)
    return None if still is None else '删完之后它还在'


async def unusable_replay(store: FileTraceStore, prefix: str | None) -> tuple[TraceHeader, str]:
    """回放之前的那几道检查：trace 在、有结局、有回放文档。交回头与文档的原文。"""
    header = await resolved(store, prefix)
    if header.status is None:
        raise TraceUnusable(
            expected='回放的 trace 有结局（记录完整）',
            received=f'trace {header.trace_id} 记录到一半就断了：写入失败，或者那次 run 还没结束',
            next_action='等它结束，或者换一个完整的 trace；不完整的 trace 不回放',
        )
    text = await _read(store.replay(header.trace_id))
    if text is None and header.source.replay_of is not None:
        raise TraceUnusable(
            expected=f'trace {header.trace_id} 有回放文档（replay.json）',
            received=f'没有：它是回放 {header.source.replay_of} 时记下的 trace，回放不再捕获',
            next_action=f'回放原来那个：syndica trace replay {header.source.replay_of[:8]}',
        )
    if text is None:
        raise TraceUnusable(
            expected=f'trace {header.trace_id} 有回放文档（replay.json）',
            received=(
                '没有：它按 structure 记的、是 --topology 的 run，或者捕获时写不出'
                '（当时 stderr 有说明）'
            ),
            next_action='用 --trace replay 重新跑一次这条命令，再回放新的那个 trace',
        )
    return header, text


def trace_replay(
    header: TraceHeader,
    text: str,
    *,
    replayer: Replayer,
    allow_divergence: bool,
    recorder: TraceRecorder | None,
) -> CliOutcome:
    """回放一个 trace 的文档。规则集与记录时不同（内置规则改过）单独报一处分叉：指纹按记录时的规则
    算，换了规则多半处处对不上，先说清楚是为什么。"""
    try:
        document = parse_scenario_document(
            json.loads(text), source=f'trace {header.trace_id} 的回放文档'
        )
    except (ConfigError, ValueError, RecursionError) as error:
        raise TraceUnusable(
            expected=f'trace {header.trace_id} 的回放文档读得进来',
            received=_said(error),
            next_action='回放文档被改过或者是旧版本写的：重新记录这次 run',
        ) from error
    try:
        redactor = RuleRedactor(rules=header.redaction.rules)
    except ConfigError as error:
        raise TraceUnusable(
            expected=f'trace {header.trace_id} 头里记下的脱敏规则建得起来',
            received=_said(error),
            next_action='这个 trace 的头被改过：删掉它，重新记录这次 run',
        ) from error
    notes: list[str] = []
    if header.redaction.rules_version != RULES_VERSION:
        notes.append(
            f'  rules  脱敏规则集：记录时第 {header.redaction.rules_version} 版，'
            f'现在第 {RULES_VERSION} 版——指纹按记录时的规则算，换了规则，请求多半处处对不上'
        )
    elif redactor.digest != header.redaction.digest:
        notes.append(
            '  rules  脱敏规则集的摘要与记录时不同：版本一样，按 trace 头里的用户规则重算出来的'
            '对不上（头被改过，或者内置规则变了而版本没升）——请求多半处处对不上'
        )
    try:
        replayed = replayer(
            document, redactor=redactor, allow_divergence=allow_divergence, recorder=recorder
        )
    except ConfigError as error:
        # 回放器的「下一步」写给调用 ReplayRunner 的代码；命令行用户能做的是重新记录。
        raise TraceUnusable(
            expected=f'trace {header.trace_id} 的回放跑得完',
            received=_said(error),
            next_action=(
                '这份回放文档不是这个版本的 syndica 捕获写下的，或者被改过：'
                '用 --trace replay 重新跑一次那条命令，再回放新的 trace'
            ),
        ) from error
    except SyndicaError as error:
        raise TraceUnusable(
            expected=f'trace {header.trace_id} 的回放跑得完',
            received=_said(error),
            next_action=error.next_action,
        ) from error
    lines = notes + [
        f'  {divergence.kind}  {one_line(divergence.where)}：{one_line(divergence.detail)}'
        for divergence in replayed.divergences
    ]
    if lines:
        head = f'分叉：trace {header.trace_id} 的回放有 {len(lines)} 处与记录不同：'
    else:
        head = f'复现：trace {header.trace_id} 的回放与记录逐项相同。'
    body = [head, *lines]
    if replayed.unrebuilt:
        body.append(
            f'没有重建的宿主 Module 与 Flag：{one_line(", ".join(replayed.unrebuilt))}'
            '（它们的作用已经在记录里）。'
        )
    return CliOutcome('\n'.join(body), 1 if lines else 0)


async def _read(action: Awaitable[_T]) -> _T:
    """store 读不了这个 trace（前缀不合法、文件被改坏、没有权限）：三件套，以 2 退出。"""
    try:
        return await action
    except SyndicaError as error:
        raise _unusable(error) from error
    except OSError as error:
        raise TraceUnusable(
            expected='trace 的文件读得了',
            received=_said(error),
            next_action='看看 .syndica/traces 下这个 trace 的权限；不要了就删掉它，重新记录',
        ) from error


async def _quietly(action: Awaitable[_T]) -> _T | None:
    """`trace list` 的一格：读不了就空着，那一行照样列出来。"""
    try:
        return await action
    except (SyndicaError, OSError):
        return None


def _unusable(error: SyndicaError) -> TraceUnusable:
    return TraceUnusable(
        expected=error.expected, received=error.received, next_action=error.next_action
    )


def _said(error: BaseException) -> str:
    received = getattr(error, 'received', None)
    expected = getattr(error, 'expected', None)
    if isinstance(received, str) and isinstance(expected, str):
        return one_line(f'{expected}；{received}')
    return one_line(f'{type(error).__name__}: {error}')
