"""文件 TraceStore（M3 D6 / D7）：`root` 下一个 trace 一个目录。语义见
`syndica.kernel.protocols.trace`。

布局：`<UTC 开始时间>-<trace id 前 8 位>/` 里 `trace.json`（头）、`spans.jsonl`、`edges.jsonl`（一行
一项，按追加顺序）、`payloads/<sha256>.txt`、`replay.json`。目录 0700，文件 0600。

Agent 运行期间就在写，而 Agent 的 Bash 改得到这些目录：`root` 以下的每一级都按名字以 `O_NOFOLLOW`
重新打开（目录另加 `O_DIRECTORY`），经 `dir_fd` 一级一级往下走，所以任何一级换成符号链接，写入就被
拒、不会写到链接指向的地方；按名字重开也让 `delete` 之后的写自然失败，而不是写进一个已经不在的目录。
文件另加 `O_NONBLOCK` 打开、打开之后核对是普通文件（换成 FIFO 不会把宿主挂住），要写的还得只有一个
硬链接（硬链接进来的外部文件不会被写），读之前按大小设上限（稀疏的大文件不会把内存撑爆）。`root`
记下第一次打开时的身份（设备与 inode），此后换成别的目录（经上级的符号链接、或者挪走再建）就拒绝。
payload 以 `O_EXCL` 创建，撞上已有的文件与读回时都核对 sha256。`root` 的上级（`.syndica`）由宿主
核对：内核不知道工作区在哪。

`begin` 记下自己建的目录名，此后按名字直接打开、核对头里的 id：每次写不必列一遍历史上所有的 trace，
后来冒出的同名前缀目录也顶替不了它。没记下的（另一个 store 实例读别人写的 trace）按 id 前缀找，两个
目录都自称是同一个 trace 时报错而不是挑一个。

所有 IO 都是同步的：检查与写入之间没有挂起点，同一个事件循环里并发的 `begin`、`put_replay`、`append`
因此天然是原子的。跨进程不做保证——一个 trace 只由记录它的那个进程写。

内容在交给 store 之前已经由 `TraceRecorder` 按宿主的规则逐叶脱敏。这里写盘时再经一次
`encode_redacted`，用的是只有内置规则的脱敏器：它对已经脱敏的文本是幂等的，又认不出内核生成的
标识——宿主的用户规则可能认得一段十六进制，在这里再用一遍，message id 与 payload 引用就被换掉，
trace 读回来时校验不过。
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.redaction import (
    RedactedText,
    RedactionRule,
    RuleRedactor,
    encode_redacted,
    write_redacted,
)
from syndica.kernel.trace import (
    AttributeValue,
    CausalityEdge,
    EdgeKind,
    NewerTraceSchema,
    PayloadRef,
    Span,
    SpanEvent,
    SpanKind,
    SpanStatus,
    TraceHeader,
    TraceLevel,
    TraceRedaction,
    TraceSource,
    readable_schema,
)

logger = logging.getLogger(__name__)

__all__ = ['FileTraceStore']

_PREFIX = re.compile(r'[0-9a-f]{1,32}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# 头只有几 KB（命令行参数最多 ARG_MAX）；别的文件给足余量，只挡住把宿主内存撑爆的那种。
_HEADER_LIMIT = 4 * 1024 * 1024
_FILE_LIMIT = 256 * 1024 * 1024
_ENCODER = RuleRedactor()
_HEADER = 'trace.json'
_SPANS = 'spans.jsonl'
_EDGES = 'edges.jsonl'
_PAYLOADS = 'payloads'
_REPLAY = 'replay.json'


def _header_value(header: TraceHeader) -> dict[str, object]:
    return {
        'schema': header.schema,
        'trace_id': header.trace_id,
        'started_ns': header.started_ns,
        'ended_ns': header.ended_ns,
        'status': None if header.status is None else header.status.value,
        'level': header.level.value,
        'redaction': {
            'digest': header.redaction.digest,
            'rules_version': header.redaction.rules_version,
            'rules': [
                {'rule_id': rule.rule_id, 'pattern': rule.pattern}
                for rule in header.redaction.rules
            ],
        },
        'source': {
            'argv': list(header.source.argv),
            'config_sha256': header.source.config_sha256,
            'version': header.source.version,
            'git_commit': header.source.git_commit,
            'replay_of': header.source.replay_of,
        },
    }


def _event_value(event: SpanEvent) -> dict[str, object]:
    return {'name': event.name, 'time_ns': event.time_ns, 'attributes': dict(event.attributes)}


def _span_value(span: Span) -> dict[str, object]:
    return {
        'span_id': span.span_id,
        'parent_span_id': span.parent_span_id,
        'kind': span.kind.value,
        'name': span.name,
        'agent_id': span.agent_id,
        'start_ns': span.start_ns,
        'end_ns': span.end_ns,
        'status': span.status.value,
        'attributes': dict(span.attributes),
        'events': [_event_value(event) for event in span.events],
    }


def _edge_value(edge: CausalityEdge) -> dict[str, object]:
    return {
        'kind': edge.kind.value,
        'source_span_id': edge.source_span_id,
        'target_span_id': edge.target_span_id,
        'attributes': dict(edge.attributes),
    }


def _attributes(value: object) -> dict[str, AttributeValue]:
    return cast(dict[str, AttributeValue], value)


def _header_from(value: Mapping[str, Any]) -> TraceHeader:
    readable_schema(value.get('schema'))
    redaction = cast(Mapping[str, Any], value['redaction'])
    source = cast(Mapping[str, Any], value['source'])
    status = value['status']
    return TraceHeader(
        trace_id=value['trace_id'],
        started_ns=value['started_ns'],
        level=TraceLevel(value['level']),
        redaction=TraceRedaction(
            digest=redaction['digest'],
            rules_version=redaction['rules_version'],
            rules=tuple(
                RedactionRule(rule['rule_id'], rule['pattern'])
                for rule in cast(list[Mapping[str, str]], redaction['rules'])
            ),
        ),
        source=TraceSource(
            argv=tuple(cast(list[str], source['argv'])),
            config_sha256=source['config_sha256'],
            version=source['version'],
            git_commit=source['git_commit'],
            replay_of=source['replay_of'],
        ),
        ended_ns=value['ended_ns'],
        status=None if status is None else SpanStatus(status),
        schema=value['schema'],
    )


def _span_from(value: Mapping[str, Any]) -> Span:
    return Span(
        span_id=value['span_id'],
        parent_span_id=value['parent_span_id'],
        kind=SpanKind(value['kind']),
        name=value['name'],
        agent_id=value['agent_id'],
        start_ns=value['start_ns'],
        end_ns=value['end_ns'],
        status=SpanStatus(value['status']),
        attributes=_attributes(value['attributes']),
        events=tuple(
            SpanEvent(event['name'], event['time_ns'], _attributes(event['attributes']))
            for event in cast(list[Mapping[str, Any]], value['events'])
        ),
    )


def _edge_from(value: Mapping[str, Any]) -> CausalityEdge:
    return CausalityEdge(
        kind=EdgeKind(value['kind']),
        source_span_id=value['source_span_id'],
        target_span_id=value['target_span_id'],
        attributes=_attributes(value['attributes']),
    )


def _checked_items(items: object, kind: type[Span] | type[CausalityEdge], name: str) -> list[Any]:
    if isinstance(items, str | bytes) or not isinstance(items, Sequence):
        raise ContractViolation(
            expected=f'append 的 {name} 是 {kind.__name__} 的序列',
            received=f'收到 {type(items).__name__}',
            next_action='传一个列表或元组',
        )
    checked = list(cast(Sequence[object], items))
    for index, item in enumerate(checked):
        if not isinstance(item, kind):
            raise ContractViolation(
                expected=f'append 的 {name} 每一项是 {kind.__name__}',
                received=f'{name}[{index}] 是 {type(item).__name__}',
                next_action=f'用 {kind.__name__}(...) 构造每一项',
            )
    return checked


def _redacted_text(text: object, method: str) -> RedactedText:
    if not isinstance(text, RedactedText):
        raise ContractViolation(
            expected=f'{method} 只收 RedactedText（逐叶脱敏之后的编码）',
            received=f'收到 {type(text).__name__}',
            next_action='先经 syndica.kernel.redaction.encode_redacted 编码；普通 str 可能带着密钥',
        )
    return text


def _corrupt(where: str, why: str) -> ContractViolation:
    return ContractViolation(
        expected=f'{where} 是这个 store 写下的、没被改过的内容',
        received=why,
        next_action=(
            '这个 trace 已经不可信（trace 目录被改过，多半是 Agent 的 Bash）：删掉它所在的目录，'
            '重新记录'
        ),
    )


class FileTraceStore:
    """`root` 是 trace 目录的上一级（CLI 传 `<工作区>/.syndica/traces`）。不存在时第一次 `begin`
    建出来（0700）；它自己不许是符号链接。"""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        if isinstance(cast(object, root), bytes) or not isinstance(
            cast(object, root), str | os.PathLike
        ):
            raise ConfigError(
                expected='root 是一个路径（str 或 pathlib.Path）',
                received=f'收到 {type(root).__name__}',
                next_action='传 Path(工作区) / ".syndica" / "traces"',
            )
        self._root = Path(root)
        self._identity: tuple[int, int] | None = None
        self._names: dict[str, str] = {}

    # --- writes ---------------------------------------------------------------------------

    async def begin(self, header: TraceHeader) -> None:
        if not isinstance(cast(object, header), TraceHeader):
            raise ContractViolation(
                expected='begin 收一个 TraceHeader',
                received=f'收到 {type(header).__name__}',
                next_action='用 TraceHeader(trace_id, started_ns, level, redaction) 构造',
            )
        if header.status is not None:
            raise ContractViolation(
                expected='begin 时 trace 还没有结局（ended_ns 与 status 都是 None）',
                received=f'trace {header.trace_id} 带着 status={header.status!r}',
                next_action='结局由 finish 写入',
            )
        with self._created_root() as root:
            with self._located(root, header.trace_id) as found:
                if found is not None:
                    raise ContractViolation(
                        expected='每个 trace id 只 begin 一次',
                        received=f'trace {header.trace_id} 已经开始过',
                        next_action='换一个新生成的 trace id；继续往已有的 trace 里写用 append',
                    )
            name = self._new_directory(root, header)
            with _opened(name, root) as trace:
                os.mkdir(_PAYLOADS, mode=0o700, dir_fd=trace)
                _write_new(trace, _HEADER, encode_redacted(_header_value(header), _ENCODER))
            self._names[header.trace_id] = name

    async def append(
        self,
        trace_id: str,
        spans: Sequence[Span] = (),
        edges: Sequence[CausalityEdge] = (),
    ) -> None:
        with self._writable(trace_id, 'append') as (_, trace, _header):
            new_spans = cast(list[Span], _checked_items(spans, Span, 'spans'))
            new_edges = cast(list[CausalityEdge], _checked_items(edges, CausalityEdge, 'edges'))
            lines = [
                (_SPANS, encode_redacted(_span_value(span), _ENCODER)) for span in new_spans
            ] + [(_EDGES, encode_redacted(_edge_value(edge), _ENCODER)) for edge in new_edges]
            for file, line in lines:
                _append_line(trace, file, line)

    async def put_payload(self, trace_id: str, payload: RedactedText) -> PayloadRef:
        text = _redacted_text(payload, 'put_payload')
        with self._writable(trace_id, 'put_payload') as (name, trace, _):
            encoded = text.encode('utf-8')
            digest = hashlib.sha256(encoded).hexdigest()
            with _opened(_PAYLOADS, trace) as payloads:
                try:
                    _write_new(payloads, f'{digest}.txt', text)
                except FileExistsError:
                    _read_payload(
                        payloads,
                        digest,
                        f'{self._root / name}/{_PAYLOADS}/{digest}.txt',
                        limit=len(encoded),
                    )
            return PayloadRef(trace_id, digest)

    async def put_replay(self, trace_id: str, document: RedactedText) -> None:
        text = _redacted_text(document, 'put_replay')
        with self._writable(trace_id, 'put_replay') as (_, trace, _header):
            try:
                _write_new(trace, _REPLAY, text)
            except FileExistsError:
                raise ContractViolation(
                    expected='每个 trace 至多一份回放文档',
                    received=f'trace {trace_id} 已经存过回放文档',
                    next_action='在 finish 之前把回放文档一次写完',
                ) from None

    async def finish(self, trace_id: str, status: SpanStatus, ended_ns: int) -> None:
        with self._writable(trace_id, 'finish') as (_, trace, header):
            given = cast(object, ended_ns)
            if (
                not isinstance(cast(object, status), SpanStatus)
                or not isinstance(given, int)
                or isinstance(given, bool)
                or given < header.started_ns
            ):
                raise ContractViolation(
                    expected='finish 收一个 SpanStatus 与不早于开始时间的整数纳秒',
                    received=(
                        f'status={status!r}，ended_ns={ended_ns!r}，'
                        f'trace 开始于 {header.started_ns}'
                    ),
                    next_action='传 SpanStatus.OK / ERROR / CANCELLED 与 finish 那一刻的时钟',
                )
            finished = TraceHeader(
                trace_id=header.trace_id,
                started_ns=header.started_ns,
                level=header.level,
                redaction=header.redaction,
                source=header.source,
                ended_ns=ended_ns,
                status=status,
                schema=header.schema,
            )
            temporary = f'.{_HEADER}.{uuid.uuid4().hex}.tmp'
            _write_new(trace, temporary, encode_redacted(_header_value(finished), _ENCODER))
            try:
                os.replace(temporary, _HEADER, src_dir_fd=trace, dst_dir_fd=trace)
            except BaseException:
                os.unlink(temporary, dir_fd=trace)
                raise

    # --- reads ----------------------------------------------------------------------------

    async def traces(self) -> list[TraceHeader]:
        with self._root_fd() as root:
            if root is None:
                return []
            headers = [header for _, header in self._headers(root)]
        return sorted(headers, key=lambda header: (header.started_ns, header.trace_id))

    async def find(self, prefix: str) -> TraceHeader | None:
        if not isinstance(cast(object, prefix), str) or not _PREFIX.fullmatch(prefix):
            raise ConfigError(
                expected='trace id 前缀是 1 到 32 个小写十六进制字符',
                received=f'收到 {prefix!r}',
                next_action='从 `syndica trace list` 的 id 列抄一段开头',
            )
        hits = sorted(
            (header for header in await self.traces() if header.trace_id.startswith(prefix)),
            key=lambda header: header.trace_id,
        )
        if len(hits) > 1:
            raise ConfigError(
                expected=f'前缀 {prefix} 只对应一个 trace',
                received=f'对应 {len(hits)} 个：{", ".join(h.trace_id for h in hits)}',
                next_action='多给几个字符，直到只剩一个',
            )
        return hits[0] if hits else None

    async def get(self, trace_id: str) -> TraceHeader | None:
        with self._trace_fd(trace_id) as found:
            return None if found is None else found[2]

    async def spans(self, trace_id: str) -> list[Span]:
        return [
            _decoded(_span_from, value, where) for value, where in self._lines(trace_id, _SPANS)
        ]

    async def edges(self, trace_id: str) -> list[CausalityEdge]:
        return [
            _decoded(_edge_from, value, where) for value, where in self._lines(trace_id, _EDGES)
        ]

    async def payload(self, ref: PayloadRef) -> str | None:
        with self._trace_fd(ref.trace_id) as found:
            if found is None:
                return None
            name, trace, _ = found
            try:
                with _opened(_PAYLOADS, trace) as payloads:
                    return _read_payload(
                        payloads,
                        ref.digest,
                        f'{self._root / name}/{_PAYLOADS}/{ref.digest}.txt',
                        limit=_FILE_LIMIT,
                    )
            except FileNotFoundError:
                return None

    async def replay(self, trace_id: str) -> str | None:
        with self._trace_fd(trace_id) as found:
            if found is None:
                return None
            name, trace, _ = found
            try:
                return _read_text(
                    trace, _REPLAY, f'{self._root / name}/{_REPLAY}', _FILE_LIMIT, growing=True
                )
            except FileNotFoundError:
                return None

    async def directory(self, trace_id: str) -> Path | None:
        """这个 trace 所在的目录（宿主打印给人看、`trace prune` 按它删）；没有这个 trace 时 None。
        不在 `TraceStore` 协议里：别的 store 没有目录可言。"""
        with self._root_fd() as root:
            if root is None:
                return None
            with self._located(root, trace_id) as found:
                name = None if found is None else found[0]
        return None if name is None else self._root / name

    async def size(self, trace_id: str) -> int | None:
        """这个 trace 占的字节数（目录里的文件与 payload 目录里的文件），`trace list` 报给人看；没有
        这个 trace 时 None。不跟符号链接、不进别的目录。不在 `TraceStore` 协议里：别的 store 不一定
        量得出来。"""
        with self._trace_fd(trace_id) as found:
            if found is None:
                return None
            _, trace, _ = found
            return _bytes_in(trace)

    async def delete(self, trace_id: str) -> None:
        with self._root_fd() as root:
            if root is None:
                return
            with self._located(root, trace_id) as found:
                if found is not None:
                    _remove(root, found[0], found[1])
            self._names.pop(trace_id, None)

    # --- internals ------------------------------------------------------------------------

    @contextmanager
    def _root_fd(self) -> Generator[int | None, None, None]:
        """`root` 的描述符；还没建出来时 None。按路径打开时 `O_NOFOLLOW` 只管最后一级，上级换成
        符号链接照样打得开，所以记下第一次打开的身份，此后打开的不是同一个目录就拒绝。"""
        try:
            descriptor = _open_directory(self._root, None, str(self._root))
        except FileNotFoundError:
            yield None
            return
        try:
            info = os.fstat(descriptor)
            identity = (info.st_dev, info.st_ino)
            if self._identity is None:
                self._identity = identity
            elif identity != self._identity:
                raise ContractViolation(
                    expected=f'{self._root} 一直是这个 store 第一次打开的那个目录',
                    received='它被换成了别的目录（上级换成了符号链接，或者挪走之后又建了一个）',
                    next_action=(
                        '这次 run 的 trace 不再往里写：查清是谁动了 .syndica，删掉换进来的那个，'
                        '重新记录'
                    ),
                )
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _created_root(self) -> Generator[int, None, None]:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._root_fd() as descriptor:
            if descriptor is None:
                raise ContractViolation(
                    expected=f'{self._root} 建出来之后还在',
                    received='刚建好它就不见了',
                    next_action='检查是不别的进程在同时删它，然后重新记录',
                )
            yield descriptor

    async def unreadable(self) -> list[tuple[str, str, str]]:
        """读不出头、因而不在 `traces()` 里的那些 trace 目录：（目录名，怎么了，下一步）。不记日志：
        `traces()` 已经为每一个记过一条。"""
        with self._root_fd() as root:
            if root is None:
                return []
            skipped: list[tuple[str, str, str]] = []
            for _ in self._headers(root, skipped=skipped):
                pass
            return skipped

    def _headers(
        self, root: int, *, skipped: list[tuple[str, str, str]] | None = None
    ) -> Iterator[tuple[str, TraceHeader]]:
        """每个 trace 目录与它的头。读不出头的目录跳过：给了 `skipped` 就记进去，没给就记一条
        WARNING——一个坏掉的 trace 不能让别的 trace 都列不出来。"""
        for name in sorted(os.listdir(root)):
            try:
                mode = os.stat(name, dir_fd=root, follow_symlinks=False).st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                continue
            try:
                trace, header = self._opened_trace(root, name)
                os.close(trace)
            except NewerTraceSchema as error:
                if skipped is not None:
                    skipped.append((name, error.received, error.next_action))
                    continue
                logger.warning(
                    '期望: %s 是这个版本的 syndica 读得懂的 trace。实际: %s。 下一步: %s。',
                    self._root / name,
                    error.received,
                    error.next_action,
                )
                continue
            except (OSError, ContractViolation) as error:
                if skipped is not None:
                    skipped.append(
                        (name, f'它的 {_HEADER} 读不进来（{error}）', '不要了就删掉这个目录')
                    )
                    continue
                logger.warning(
                    '期望: %s 是一个 trace 目录，里面有可读的 %s。实际: %s。'
                    ' 下一步: 它不会出现在 trace 列表里；不要了就删掉这个目录。',
                    self._root / name,
                    _HEADER,
                    error,
                )
                continue
            # 按 id 找、按 id 删都只认名字对得上的目录（`_candidates`）：列出别的，就会有查不到、
            # 删不掉、却照样报「删掉了」的 trace。
            if not name.endswith(_suffixes(header.trace_id)):
                if skipped is not None:
                    skipped.append(
                        (
                            name,
                            f'名字与头里的 trace {header.trace_id} 对不上（被改名或复制出来的）',
                            '不要了就删掉这个目录',
                        )
                    )
                    continue
                logger.warning(
                    '期望: %s 的名字以它的头里的 trace id（前 8 位或完整的）结尾。'
                    ' 实际: 头是 trace %s。'
                    ' 下一步: 它是被改名或复制出来的，不会出现在 trace 列表里；'
                    '不要了就删掉这个目录。',
                    self._root / name,
                    header.trace_id,
                )
                continue
            yield name, header

    @contextmanager
    def _located(
        self, root: int, trace_id: str
    ) -> Generator[tuple[str, int, TraceHeader] | None, None, None]:
        """按 id 找到这个 trace：目录名、打开着的目录描述符与从它读出的头；没有时 None。

        `begin` 记下的目录按名字直接打开（每一次写都要找一遍，逐个列出历史上的 trace 就让写入的开销
        随记过的 trace 数增长）。没记下的按目录名里的 id 前缀找、只读名字对得上的头；名字对得上而头
        读不出来的可能就是要找的那个，没找到时报它而不是「不存在」，两个目录都自称是它时也报错。"""
        remembered = self._names.get(trace_id)
        names = [remembered] if remembered is not None else self._candidates(root, trace_id)
        broken: ContractViolation | None = None
        found: list[tuple[str, int, TraceHeader]] = []
        try:
            for name in names:
                try:
                    trace, header = self._opened_trace(root, name)
                except FileNotFoundError:
                    continue
                except ContractViolation as error:
                    broken = broken or error
                    continue
                if header.trace_id == trace_id:
                    found.append((name, trace, header))
                else:
                    os.close(trace)
            if len(found) > 1:
                raise ContractViolation(
                    expected=f'{self._root} 下只有一个目录的头是 trace {trace_id}',
                    received=f'有 {len(found)} 个：{", ".join(name for name, _, _ in found)}',
                    next_action='多出来的是被人复制或伪造的：核对之后删掉不是这次 run 写的那几个',
                )
            if found:
                yield found[0]
                return
            if broken is not None:
                raise broken
            yield None
        finally:
            for _, trace, _ in found:
                os.close(trace)

    def _candidates(self, root: int, trace_id: str) -> list[str]:
        return [name for name in sorted(os.listdir(root)) if name.endswith(_suffixes(trace_id))]

    def _opened_trace(self, root: int, name: str) -> tuple[int, TraceHeader]:
        """打开 `name` 这个目录并读出它的头，交回描述符（调用方关）与头。它不是目录、读不出头时报
        `ContractViolation`，不存在时 `FileNotFoundError`。"""
        if not stat.S_ISDIR(os.stat(name, dir_fd=root, follow_symlinks=False).st_mode):
            raise _corrupt(f'{self._root / name}', '它不是目录')
        trace = _open_directory(name, root, name)
        try:
            return trace, _read_header(trace, f'{self._root / name}/{_HEADER}')
        except BaseException as error:
            os.close(trace)
            if isinstance(error, OSError) and not isinstance(error, FileNotFoundError):
                raise _not_a_file(
                    f'{name}/{_HEADER}', f'{type(error).__name__}: {error}'
                ) from error
            raise

    def _new_directory(self, root: int, header: TraceHeader) -> str:
        """`<UTC 开始时间>-<id 前 8 位>`；同一秒里撞上前 8 位相同的另一个 trace 时用完整的 id。"""
        moment = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(header.started_ns // 10**9))
        for name in (f'{moment}-{header.trace_id[:8]}', f'{moment}-{header.trace_id}'):
            try:
                os.mkdir(name, mode=0o700, dir_fd=root)
            except FileExistsError:
                continue
            return name
        raise ContractViolation(
            expected=f'{self._root} 下没有名为 {moment}-{header.trace_id} 的东西',
            received='它已经存在，而它的头不是这个 trace 的',
            next_action='删掉或挪开那个目录再记录',
        )

    @contextmanager
    def _trace_fd(
        self, trace_id: str
    ) -> Generator[tuple[str, int, TraceHeader] | None, None, None]:
        with self._root_fd() as root:
            if root is None:
                yield None
                return
            with self._located(root, trace_id) as found:
                yield found

    @contextmanager
    def _writable(
        self, trace_id: str, method: str
    ) -> Generator[tuple[str, int, TraceHeader], None, None]:
        with self._trace_fd(trace_id) as found:
            if found is None:
                raise ContractViolation(
                    expected=f'{method} 写的是一个 begin 过、还没删掉的 trace',
                    received=f'trace {trace_id!r} 不存在（没有开始过，或者已经删掉了）',
                    next_action='先 begin(header)，再往里写',
                )
            name, trace, header = found
            if header.status is not None:
                raise ContractViolation(
                    expected=f'{method} 写的是一个还没 finish 的 trace',
                    received=f'trace {trace_id} 已经以 {header.status.value} 结束',
                    next_action='trace 结束之后只读；新的记录开一个新的 trace',
                )
            yield name, trace, header

    def _lines(self, trace_id: str, file: str) -> list[tuple[Mapping[str, Any], str]]:
        """一行一项（与它在哪个文件的第几行）。最后一行写到一半（进程在写的时候死掉）时丢掉它并记
        一条 WARNING；中间坏掉的行说明文件被改过，报错。"""
        with self._trace_fd(trace_id) as found:
            if found is None:
                return []
            name, trace, _ = found
            where = f'{self._root / name}/{file}'
            try:
                data = _read_bytes(trace, file, where, _FILE_LIMIT, growing=True)
            except FileNotFoundError:
                return []
        # 先切行再逐行解码：写到一半的最后一行可能断在一个多字节字符中间，整份解码会把它报成篡改。
        lines = data.split(b'\n')
        complete, tail = lines[:-1], lines[-1]
        values: list[tuple[Mapping[str, Any], str]] = []
        for number, line in enumerate(complete, 1):
            try:
                text = line.decode('utf-8')
            except UnicodeDecodeError as error:
                raise _corrupt(where, f'第 {number} 行不是 UTF-8：{error}') from None
            try:
                values.append(
                    (cast(Mapping[str, Any], json.loads(text)), f'{where} 第 {number} 行')
                )
            except (ValueError, RecursionError):
                raise _corrupt(where, f'第 {number} 行不是 JSON') from None
        if tail:
            logger.warning(
                '期望: trace %s 的 %s 以换行结尾。实际: 最后一行只写了一半（写它的进程中途退出）。'
                ' 下一步: 丢掉的是那一行；这个 trace 不完整，不要拿它回放。',
                trace_id,
                file,
            )
        return values


def _decoded(build: Any, value: Mapping[str, Any], where: str) -> Any:
    # 任何异常都是「读不成记录」：模型类型自己的校验抛 ConfigError，嵌套过深的 JSON 抛
    # RecursionError。
    try:
        return build(value)
    except Exception as error:
        raise _corrupt(where, f'读不成记录：{type(error).__name__}: {error}') from None


def _not_a_directory(where: str, error: OSError) -> ContractViolation:
    return ContractViolation(
        expected=f'{where} 是一个真目录',
        received=f'打不开它：{type(error).__name__}: {error}（是符号链接、普通文件，或没有权限）',
        next_action='删掉或挪开它再记录：trace 不写到符号链接指向的地方',
    )


def _open_directory(name: str | os.PathLike[str], parent: int | None, where: str) -> int:
    try:
        return os.open(name, _DIRECTORY, dir_fd=parent)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise _not_a_directory(where, error) from error


@contextmanager
def _opened(name: str, parent: int) -> Generator[int, None, None]:
    descriptor = _open_directory(name, parent, name)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _not_a_file(name: str, why: str) -> ContractViolation:
    return ContractViolation(
        expected=f'trace 目录里的 {name} 是这个 store 建的、只有一个硬链接的普通文件',
        received=why,
        next_action='这个 trace 被人动过，已经不可信：删掉它所在的目录，重新记录',
    )


def _open_file(name: str, flags: int, directory: int, *, write: bool) -> tuple[int, int]:
    """按名字打开 trace 目录里的一个文件，交回描述符与大小。`O_NONBLOCK`：换成 FIFO 的文件读着不会
    挂住（写着打不开、报 ENXIO）；打开之后核对是普通文件，要写的还得只有一个硬链接——硬链接进来的
    外部文件按名字看就在 trace 目录里。"""
    try:
        descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    except (FileNotFoundError, FileExistsError):
        raise
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
            raise _not_a_file(
                name,
                f'打不开它：{type(error).__name__}: {error}（是符号链接、FIFO 或别的类型的文件）',
            ) from error
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise _not_a_file(name, '它不是普通文件（FIFO、设备或目录）')
        if write and info.st_nlink != 1:
            raise _not_a_file(name, f'它有 {info.st_nlink} 个硬链接，可能指向 trace 目录之外的文件')
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, info.st_size


def _write_new(directory: int, name: str, text: RedactedText) -> None:
    """建一个新文件写进去（0600）；已经存在时报 `FileExistsError`，是符号链接也一样。写失败时删掉
    半截文件：留着它，重试会撞上 `O_EXCL`，读回时 sha256 也对不上。"""
    descriptor, _ = _open_file(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, directory, write=True)
    try:
        try:
            write_redacted(descriptor, text)
        finally:
            os.close(descriptor)
    except BaseException:
        os.unlink(name, dir_fd=directory)
        raise


def _append_line(directory: int, name: str, text: RedactedText) -> None:
    descriptor, _ = _open_file(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT, directory, write=True)
    try:
        write_redacted(descriptor, text, newline=True)
    finally:
        os.close(descriptor)


def _read_bytes(
    directory: int, name: str, where: str, limit: int, *, growing: bool = False
) -> bytes:
    """整个文件读回；比 `limit` 大的不读（打开时看大小，读的时候它变大了也停下）。`growing` 的
    文件随 run 变长（span、边、回放文档），超过上限不说明它被改过。"""
    descriptor, size = _open_file(name, os.O_RDONLY, directory, write=False)
    chunks: list[bytes] = []
    try:
        if size > limit:
            raise (_too_large if growing else _over)(where, f'有 {size} 字节', limit)
        total = 0
        while chunk := os.read(descriptor, 1 << 16):
            total += len(chunk)
            if total > limit:
                raise (_too_large if growing else _over)(
                    where, f'读的时候它变大了，读到超过 {limit} 字节', limit
                )
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    return b''.join(chunks)


def _read_text(directory: int, name: str, where: str, limit: int, *, growing: bool = False) -> str:
    """整个文件按 UTF-8 读回，上限同 `_read_bytes`。"""
    try:
        return _read_bytes(directory, name, where, limit, growing=growing).decode('utf-8')
    except UnicodeDecodeError as error:
        raise _corrupt(where, f'不是 UTF-8：{error}') from None


def _over(where: str, received: str, limit: int) -> ContractViolation:
    return _corrupt(where, f'{received}，超过这种文件的上限 {limit}')


def _too_large(where: str, received: str, limit: int) -> ContractViolation:
    """写入不设上限，长跑的 trace 可以真的长到这么大：这不是「被改过」。"""
    return ContractViolation(
        expected=f'{where} 不超过读回的上限 {limit} 字节',
        received=received,
        next_action=(
            '这个 trace 大到读不回来：被人灌大的就删掉它所在的目录；'
            '真的是一次长跑，文件还在，直接看'
        ),
    )


def _read_header(directory: int, where: str) -> TraceHeader:
    text = _read_text(directory, _HEADER, where, _HEADER_LIMIT)
    # 同 `_decoded`：头里的规则由 `RedactionRule` 自己校验，抛的是 ConfigError。
    try:
        return _header_from(cast(Mapping[str, Any], json.loads(text)))
    except NewerTraceSchema:
        raise
    except Exception as error:
        raise _corrupt(where, f'读不成 trace 头：{type(error).__name__}: {error}') from None


def _read_payload(payloads: int, digest: str, where: str, *, limit: int) -> str | None:
    if not _DIGEST.fullmatch(digest):
        return None
    try:
        text = _read_text(payloads, f'{digest}.txt', where, limit)
    except FileNotFoundError:
        return None
    if hashlib.sha256(text.encode('utf-8')).hexdigest() != digest:
        raise _corrupt(where, '内容的 sha256 与文件名对不上')
    return text


def _suffixes(trace_id: str) -> tuple[str, str]:
    """`_new_directory` 起的名字以这两者之一结尾。"""
    return (f'-{trace_id[:8]}', f'-{trace_id}')


def _remove(root: int, name: str, trace: int) -> None:
    """删掉一个 trace 目录，头最后删：中途失败时它仍列得出来、能再删一次。先删了头，剩下的目录
    谁也认不出、也删不掉。"""
    for entry in os.listdir(trace):
        if entry == _HEADER:
            continue
        try:
            mode = os.stat(entry, dir_fd=trace, follow_symlinks=False).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(mode):
            shutil.rmtree(entry, dir_fd=trace)
        else:
            os.unlink(entry, dir_fd=trace)
    os.unlink(_HEADER, dir_fd=trace)
    shutil.rmtree(name, dir_fd=root)


def _bytes_in(trace: int) -> int:
    """trace 目录里普通文件的字节数，加 payload 目录里的。不跟符号链接（`payloads` 是链接或打不开
    时不算它）。"""
    total = _file_bytes(trace)
    try:
        payloads = os.open(_PAYLOADS, _DIRECTORY, dir_fd=trace)
    except OSError:
        return total
    try:
        return total + _file_bytes(payloads)
    finally:
        os.close(payloads)


def _file_bytes(directory: int) -> int:
    """`directory` 里普通文件的字节数之和。列出与量之间消失的项不算：还在写的 trace 换头时临时
    文件转瞬即逝。"""
    total = 0
    for name in os.listdir(directory):
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total
