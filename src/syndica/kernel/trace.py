"""trace 的数据模型（spec §11、M3 D3 / D6）：span、因果边、trace 头与 payload 引用。

span 与边只记结构与闭集里的属性；自由文本（回复、工具结果、完整请求）进 payload 或回放文档，两者都是
`RedactedText`。字符串在交给 store 之前已经逐叶脱敏，store 不判断内容。

属性是闭集：每个键有固定的类型，键不在表里或类型不对都报 `ContractViolation`。插桩写错一个键名时，
trace 里不会悄悄多出一个读者认不得的字段。`bool` 不算 `int`：`agent.cycle=True` 是插桩写错了。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import cast

from syndica.kernel.errors import ContractViolation
from syndica.kernel.redaction import RedactionRule

__all__ = [
    'ATTRIBUTE_DOMAINS',
    'ATTRIBUTE_KINDS',
    'TRACE_SCHEMA',
    'AttributeValue',
    'CausalityEdge',
    'EdgeKind',
    'PayloadRef',
    'Span',
    'SpanEvent',
    'SpanKind',
    'SpanStatus',
    'TraceHeader',
    'TraceLevel',
    'TraceRedaction',
    'TraceSource',
]

TRACE_SCHEMA = 'syndica.trace/1'

AttributeValue = str | int | bool | list[str] | None


class SpanKind(Enum):
    AGENT = 'agent'
    TURN = 'turn'
    LLM_CALL = 'llm_call'
    TOOL_CALL = 'tool_call'
    CONFIRMATION = 'confirmation'
    WAIT = 'wait'
    MODULE = 'module'


class EdgeKind(Enum):
    """`artifact` 与 `verification` 留给产物与验收（M7）。"""

    SPAWN = 'spawn'
    MESSAGE = 'message'
    ARTIFACT = 'artifact'
    VERIFICATION = 'verification'


class SpanStatus(Enum):
    """span 与整个 trace 的结局。"""

    OK = 'ok'
    ERROR = 'error'
    CANCELLED = 'cancelled'


class TraceLevel(Enum):
    """记什么（M3 D8）：`structure` 只记 span 与边；`replay` 加上回放文档；`full` 再加完整请求。"""

    OFF = 'off'
    STRUCTURE = 'structure'
    REPLAY = 'replay'
    FULL = 'full'


# 属性的闭集：键 → 取值的种类。`?` 表示还可以是 None；`str[]` 是字符串列表；`payload` 是同一个
# trace 里一份 payload 的引用（内容的 sha256，64 个小写十六进制）。读者（`trace show`、导出器、
# 回放）按这张表认字段，新增一个属性就是在这里加一行。
ATTRIBUTE_KINDS: Mapping[str, str] = MappingProxyType(
    {
        'agent.role': 'str',
        'agent.cycle': 'int',
        'turn': 'int',
        'stop_reason': 'str',
        'llm.platform': 'str',
        'llm.error': 'str',
        'llm.status': 'int?',
        'llm.model': 'str',
        'llm.attempt': 'int',
        'llm.input_tokens': 'int?',
        'llm.output_tokens': 'int?',
        'llm.cached_input_tokens': 'int?',
        'llm.request_digest': 'str',
        'llm.request_units': 'payload',
        'llm.request': 'payload',
        'llm.response': 'payload',
        'tool.name': 'str',
        'tool.call_id': 'str',
        'tool.index': 'int',
        'tool.outcome': 'str',
        'tool.exit_code': 'int?',
        'tool.timed_out': 'bool',
        'tool.result': 'payload',
        'danger.rule_ids': 'str[]',
        'danger.risk': 'str',
        'danger.provenance': 'str',
        'danger.decided_by': 'str',
        'danger.disposition': 'str',
        'confirmation.outcome': 'str',
        'wait.kind': 'str',
        'wait.target': 'str',
        'wait.outcome': 'str',
        'wait.answer': 'payload',
        'message.id': 'str',
        'message.to': 'str',
        'child.agent_id': 'str',
        'agent.id': 'str',
    }
)

# 取值有限的属性：键 → 允许的取值。与内核枚举同名的几项（`StopReason`、`ConfirmationOutcome`、
# `WaitState`、`RiskLevel`、`Disposition`、`TrustLevel`）取它们的 `.value`；写在这里而不是 import，
# 是为了不让模型依赖执行层。`tool.outcome` 与追加结果块的位置一一对应（M3 trace schema）。
ATTRIBUTE_DOMAINS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        'stop_reason': frozenset({'completed', 'aborted', 'error', 'max_turns'}),
        'tool.outcome': frozenset(
            {
                'ok',
                'error',
                'aborted',
                'judge_failed',
                'not_executed',
                'skipped',
                'blocked',
                'rejected',
                'timeout',
                'preempted',
                'confirmation_aborted',
                'no_channel',
                'unknown_tool',
                'bad_arguments',
                'interrupted',
            }
        ),
        'danger.risk': frozenset({'none', 'low', 'high'}),
        'danger.provenance': frozenset({'trusted', 'untrusted'}),
        'danger.decided_by': frozenset({'flag', 'policy'}),
        'danger.disposition': frozenset({'allow', 'block', 'confirm'}),
        'confirmation.outcome': frozenset(
            {'approved', 'rejected', 'timeout', 'preempted', 'aborted'}
        ),
        'wait.kind': frozenset({'agent', 'human'}),
        'wait.outcome': frozenset({'answered', 'timeout', 'preempted', 'aborted'}),
    }
)

_SPAN_ID = re.compile(r'[0-9a-f]{16}')
_TRACE_ID = re.compile(r'[0-9a-f]{32}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
# 整数限在 int64 之内：OTLP 的时间与属性是 64 位整数，SQLite 也是；更大的值在写出去时才失败。
_INT64 = (-(2**63), 2**63 - 1)


def _accepts(kind: str, value: object) -> bool:
    if kind.endswith('?') and value is None:
        return True
    base = kind.rstrip('?')
    if base == 'str':
        return isinstance(value, str)
    if base == 'int':
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and _INT64[0] <= value <= _INT64[1]
        )
    if base == 'bool':
        return isinstance(value, bool)
    if base == 'payload':
        return isinstance(value, str) and _DIGEST.fullmatch(value) is not None
    return isinstance(value, list | tuple) and all(
        isinstance(item, str) for item in cast(Sequence[object], value)
    )


def _sequence(value: object, what: str) -> tuple[object, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise ContractViolation(
            expected=f'{what} 是列表或元组',
            received=f'收到 {type(value).__name__}',
            next_action=f'把 {what} 写成列表或元组；没有就传空元组',
        )
    return tuple(cast(Sequence[object], value))


def _attributes(value: object, where: str) -> dict[str, AttributeValue]:
    """校验过的属性，交回一份自己的拷贝（列表也拷）。"""
    if not isinstance(value, Mapping):
        raise ContractViolation(
            expected=f'{where} 的 attributes 是映射',
            received=f'收到 {type(value).__name__}',
            next_action='传一个 dict，键取自 syndica.kernel.trace.ATTRIBUTE_KINDS',
        )
    copied: dict[str, AttributeValue] = {}
    for key, item in cast(Mapping[object, object], value).items():
        kind = ATTRIBUTE_KINDS.get(key) if isinstance(key, str) else None
        if not isinstance(key, str) or kind is None:
            raise ContractViolation(
                expected=f'{where} 的属性键取自 ATTRIBUTE_KINDS（闭集）',
                received=f'收到键 {key!r}',
                next_action='改成表里的键；要新增属性，先在 ATTRIBUTE_KINDS 里加一行并写明类型',
            )
        if not _accepts(kind, item) and kind == 'payload':
            # 不回显取值：放错进来的可能正是本该进 payload、还没脱敏的内容。
            shape = f'长度 {len(item)} 的字符串' if isinstance(item, str) else type(item).__name__
            raise ContractViolation(
                expected=(
                    f'{where} 的属性 {key} 是 payload 引用：同一个 trace 里一份 payload 的 sha256'
                    '（64 个小写十六进制）'
                ),
                received=f'收到 {shape}',
                next_action=f'{key} 填 put_payload 交回的那个摘要；内容本身写进 payload',
            )
        if not _accepts(kind, item):
            raise ContractViolation(
                expected=f'{where} 的属性 {key} 是 {kind}（整数在 int64 之内）',
                received=f'收到 {type(item).__name__} 类型的值',
                next_action=f'把 {key} 转成 {kind}；布尔值不算整数',
            )
        domain = ATTRIBUTE_DOMAINS.get(key)
        if domain is not None and item not in domain:
            raise ContractViolation(
                expected=f'{where} 的属性 {key} 取 {sorted(domain)} 之一',
                received=f'收到 {item!r}',
                next_action=f'改成表里的取值；要新增一种，先在 ATTRIBUTE_DOMAINS 的 {key} 里加上',
            )
        if kind == 'str[]':
            copied[key] = list(cast(Sequence[str], item))
        else:
            copied[key] = cast(AttributeValue, item)
    return copied


def _identifier(value: object, pattern: re.Pattern[str], what: str, *, length: int) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ContractViolation(
            expected=f'{what} 是 {length} 个小写十六进制字符',
            received=f'收到 {value!r}',
            next_action=f'用 {length} 个小写十六进制字符的 id',
        )


def _nanoseconds(value: object, what: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _INT64[1]:
        raise ContractViolation(
            expected=f'{what} 是 int64 之内的非负整数纳秒',
            received=f'收到 {value!r}',
            next_action='传 time.time_ns() 这样的整数纳秒',
        )


def _member(value: object, enum: type[Enum], what: str) -> None:
    if not isinstance(value, enum):
        names = ', '.join(f'{enum.__name__}.{member.name}' for member in enum)
        raise ContractViolation(
            expected=f'{what} 是 {enum.__name__} 的成员',
            received=f'收到 {value!r}',
            next_action=f'传 {names} 之一，不要传字符串',
        )


def _text(value: object, what: str) -> None:
    if not isinstance(value, str):
        raise ContractViolation(
            expected=f'{what} 是字符串',
            received=f'收到 {type(value).__name__}',
            next_action=f'把 {what} 转成 str',
        )


@dataclass(frozen=True)
class SpanEvent:
    """span 上的一个时刻（消息的发送是发送方当前 span 上的一个事件，带 `message.to`）。"""

    name: str
    time_ns: int
    attributes: dict[str, AttributeValue] = field(default_factory=dict[str, AttributeValue])

    def __post_init__(self) -> None:
        _text(self.name, 'SpanEvent.name')
        _nanoseconds(self.time_ns, 'SpanEvent.time_ns')
        object.__setattr__(self, 'attributes', _attributes(self.attributes, 'SpanEvent'))


@dataclass(frozen=True)
class Span:
    """一段有起止的工作。`parent_span_id` 只在同一个 Agent 之内连（turn 挂在 agent
    下面）；Agent 之间的关系只由边表达。"""

    span_id: str
    parent_span_id: str | None
    kind: SpanKind
    name: str
    agent_id: str
    start_ns: int
    end_ns: int
    status: SpanStatus
    attributes: dict[str, AttributeValue] = field(default_factory=dict[str, AttributeValue])
    events: tuple[SpanEvent, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.span_id, _SPAN_ID, 'Span.span_id', length=16)
        if self.parent_span_id is not None:
            _identifier(self.parent_span_id, _SPAN_ID, 'Span.parent_span_id', length=16)
        _member(self.kind, SpanKind, 'Span.kind')
        _text(self.name, 'Span.name')
        _text(self.agent_id, 'Span.agent_id')
        _nanoseconds(self.start_ns, 'Span.start_ns')
        _nanoseconds(self.end_ns, 'Span.end_ns')
        if self.end_ns < self.start_ns:
            raise ContractViolation(
                expected='Span.end_ns 不早于 start_ns',
                received=f'start_ns={self.start_ns}，end_ns={self.end_ns}',
                next_action='结束时间取 span 收尾那一刻的时钟；注入的时钟必须单调不减',
            )
        _member(self.status, SpanStatus, 'Span.status')
        object.__setattr__(self, 'attributes', _attributes(self.attributes, 'Span'))
        events = _sequence(self.events, 'Span.events')
        for event in events:
            if not isinstance(event, SpanEvent):
                raise ContractViolation(
                    expected='Span.events 的每一项是 SpanEvent',
                    received=f'收到 {type(event).__name__}',
                    next_action='用 SpanEvent(name, time_ns, attributes) 构造事件',
                )
        object.__setattr__(self, 'events', events)


@dataclass(frozen=True)
class CausalityEdge:
    """两个 span 之间的因果：派生（`spawn` 里发出，带 `child.agent_id`）或消息（发送时的
    span → 接收方消费它的那一轮，带 `message.id`）。"""

    kind: EdgeKind
    source_span_id: str
    target_span_id: str
    attributes: dict[str, AttributeValue] = field(default_factory=dict[str, AttributeValue])

    def __post_init__(self) -> None:
        _member(self.kind, EdgeKind, 'CausalityEdge.kind')
        _identifier(self.source_span_id, _SPAN_ID, 'CausalityEdge.source_span_id', length=16)
        _identifier(self.target_span_id, _SPAN_ID, 'CausalityEdge.target_span_id', length=16)
        object.__setattr__(self, 'attributes', _attributes(self.attributes, 'CausalityEdge'))


@dataclass(frozen=True)
class TraceRedaction:
    """记录这个 trace 时用的规则集：`RuleRedactor.digest`、`RULES_VERSION` 与用户规则。回放拿它判断
    两边的规则是不是同一套。用户规则存成 `RedactionRule` 记录而不是 (id, pattern) 两项：
    `encode_redacted` 把两项的字符串列表当成一对键值，id 像密钥名（`custom.api_token`）时
    pattern 会被换成占位符。"""

    digest: str
    rules_version: int
    rules: tuple[RedactionRule, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.digest, _DIGEST, 'TraceRedaction.digest', length=64)
        version = cast(object, self.rules_version)
        if not isinstance(version, int) or isinstance(version, bool):
            raise ContractViolation(
                expected='TraceRedaction.rules_version 是整数',
                received=f'收到 {self.rules_version!r}',
                next_action='传 syndica.kernel.redaction.RULES_VERSION',
            )
        rules = _sequence(self.rules, 'TraceRedaction.rules')
        for rule in rules:
            if not isinstance(rule, RedactionRule):
                raise ContractViolation(
                    expected='TraceRedaction.rules 的每一项是 RedactionRule',
                    received=f'收到 {type(rule).__name__} 类型的 {rule!r}',
                    next_action='传 RuleRedactor.rules；从 JSON 读回时用 RedactionRule(**item)',
                )
        object.__setattr__(self, 'rules', rules)


@dataclass(frozen=True)
class TraceSource:
    """这个 trace 从哪来：命令行参数、配置文件的 sha256、syndica 版本、git commit，以及回放的是
    哪个 trace。`argv` 原样交进来：`TraceRecorder` 写下头之前逐项脱敏。"""

    argv: tuple[str, ...] = ()
    config_sha256: str | None = None
    version: str = ''
    git_commit: str | None = None
    replay_of: str | None = None

    def __post_init__(self) -> None:
        argv = _sequence(self.argv, 'TraceSource.argv')
        if not all(isinstance(item, str) for item in argv):
            raise ContractViolation(
                expected='TraceSource.argv 是字符串序列',
                received=f'收到 {self.argv!r}',
                next_action='传 sys.argv[1:] 这样的列表或元组',
            )
        object.__setattr__(self, 'argv', argv)
        if self.config_sha256 is not None:
            _identifier(self.config_sha256, _DIGEST, 'TraceSource.config_sha256', length=64)
        _text(self.version, 'TraceSource.version')
        if self.git_commit is not None:
            _text(self.git_commit, 'TraceSource.git_commit')
        if self.replay_of is not None:
            _identifier(self.replay_of, _TRACE_ID, 'TraceSource.replay_of', length=32)


@dataclass(frozen=True)
class TraceHeader:
    """trace 的头（`trace.json`）。`ended_ns` 与 `status` 在 `finish` 之前是 None：
    没有结局的 trace 是记录到一半就断了的。"""

    trace_id: str
    started_ns: int
    level: TraceLevel
    redaction: TraceRedaction
    source: TraceSource = field(default_factory=TraceSource)
    ended_ns: int | None = None
    status: SpanStatus | None = None
    schema: str = TRACE_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.trace_id, _TRACE_ID, 'TraceHeader.trace_id', length=32)
        _nanoseconds(self.started_ns, 'TraceHeader.started_ns')
        _member(self.level, TraceLevel, 'TraceHeader.level')
        if not isinstance(cast(object, self.redaction), TraceRedaction):
            raise ContractViolation(
                expected='TraceHeader.redaction 是 TraceRedaction',
                received=f'收到 {type(self.redaction).__name__}',
                next_action='用 TraceRedaction(digest, rules_version, rules) 构造',
            )
        if not isinstance(cast(object, self.source), TraceSource):
            raise ContractViolation(
                expected='TraceHeader.source 是 TraceSource',
                received=f'收到 {type(self.source).__name__}',
                next_action='用 TraceSource(argv=..., version=...) 构造',
            )
        if (self.ended_ns is None) != (self.status is None):
            raise ContractViolation(
                expected='TraceHeader.ended_ns 与 status 要么都有、要么都没有',
                received=f'ended_ns={self.ended_ns!r}，status={self.status!r}',
                next_action='结局由 TraceStore.finish 一起写入；begin 时两者都留 None',
            )
        if self.ended_ns is not None:
            _nanoseconds(self.ended_ns, 'TraceHeader.ended_ns')
            _member(self.status, SpanStatus, 'TraceHeader.status')
            if self.ended_ns < self.started_ns:
                raise ContractViolation(
                    expected='TraceHeader.ended_ns 不早于 started_ns',
                    received=f'started_ns={self.started_ns}，ended_ns={self.ended_ns}',
                    next_action='结束时间取 finish 那一刻的时钟；注入的时钟必须单调不减',
                )
        if self.schema != TRACE_SCHEMA:
            raise ContractViolation(
                expected=f'TraceHeader.schema 是 {TRACE_SCHEMA}',
                received=f'收到 {self.schema!r}',
                next_action='旧版本的 trace 先经读取兼容层转成当前版本',
            )


@dataclass(frozen=True)
class PayloadRef:
    """trace 里一段自由文本的引用：内容 UTF-8 编码的 sha256，由 store 算。"""

    trace_id: str
    digest: str

    def __post_init__(self) -> None:
        _identifier(self.trace_id, _TRACE_ID, 'PayloadRef.trace_id', length=32)
        _identifier(self.digest, _DIGEST, 'PayloadRef.digest', length=64)
