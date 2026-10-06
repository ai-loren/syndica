"""把内核的插桩记进一个 `TraceStore`。

`Tracer` 的方法是同步的、在内核不许挂起的窗口里被调：这里只往内存里记、往队列里放，落盘由自己的
一个写入任务按收到的先后去做，`finish` 等它排空。

记什么由级别定（D8）：`structure` 只记 span 与边；`replay` 再记回放要的东西——模型的回复（含带类型
的错误）、工具的结局、人的答复，以及每次请求的指纹（span 上是整份请求的一个，逐单元的只在回放文档里
存一份）；`full` 再记完整的请求。

一切自由文本在交给 store 之前逐叶脱敏（D4）：span 名、字符串属性、事件、边的属性、payload、头里的
命令行参数。脱敏发生在写入之前——store 不再判断内容，第三方 store 拿到的已经是脱过敏的。内核生成的
标识、摘要与闭集取值不是自由文本，原样保留（`_verbatim`）。

记录不改变 run：store 坏掉时（它抛出的任何异常，含它自己抛出的 `CancelledError`）第一次失败记一条
ERROR，此后的记录丢弃并计数（`dropped`），也不再算指纹、编码与记账；写入任务被宿主取消时同样如此。
store 收不下某一段 payload（`PayloadRefused`）时只丢那一段，一条记录编码不出来时只丢那一条，各记一条
ERROR、其余照常写。这几种情况下这个 trace 都不写结局——没有结局的 trace 是不完整的，回放与导出据此
拒绝它；只有回放文档被拒收时，trace 照常有结局、只是没有回放文档。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import math
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast

from syndica.kernel.context import ContextBlock, ToolCall
from syndica.kernel.errors import ConfigError, ContractViolation, PayloadRefused
from syndica.kernel.history_blocks import HistoryKind, kind_of
from syndica.kernel.ids import IdSource
from syndica.kernel.message import Message
from syndica.kernel.protocols.trace import TraceStore
from syndica.kernel.redaction import (
    RULES_VERSION,
    RedactedText,
    Redactor,
    RuleRedactor,
    _placeholder,  # pyright: ignore[reportPrivateUsage]
    encode_prepared,
    encode_redacted,
    redact_argv,
)
from syndica.kernel.replay_capture import ReplayCapture, Unreplayable
from syndica.kernel.scenario_schema import parse_scenario_document
from syndica.kernel.tool import ToolResult
from syndica.kernel.topology import Topology
from syndica.kernel.trace import (
    AttributeValue,
    CausalityEdge,
    EdgeKind,
    Span,
    SpanEvent,
    SpanKind,
    SpanStatus,
    TraceHeader,
    TraceLevel,
    TraceRedaction,
    TraceSource,
    verbatim_attribute,
)

logger = logging.getLogger(__name__)

# 宿主交给脱敏器逐字换掉的那些值（`RuleRedactor(secrets=...)`）的占位符用的规则 id。
_ENVIRONMENT_RULE = 'secret.env_value'

__all__ = ['RequestFingerprints', 'TraceRecorder', 'arguments_digest']

_LEVELS = (TraceLevel.STRUCTURE, TraceLevel.REPLAY, TraceLevel.FULL)
_TRACE_ID = re.compile(r'[0-9a-f]{32}')
_MAX_DEPTH = 64
# 一份 payload 至多展开这么多个节点：共享的子树按路径各展开一遍，不设上限就是指数，而它跑在同步的
# tracer 方法里、按住整个事件循环。
_MAX_NODES = 100_000
# 十进制转换有位数上限（`sys.get_int_max_str_digits`），JSON 编码这么大的整数会抛错；十六进制没有。
_MAX_INT_BITS = 8192


class _Walk:
    """一次 `_jsonable` 的状态：当前路径上的容器（认环）与还能展开的节点数。"""

    __slots__ = ('active', 'remaining')

    def __init__(self) -> None:
        self.active: set[int] = set()
        self.remaining = _MAX_NODES


def _jsonable(value: object) -> object:
    """交给 `encode_redacted` 之前换成 JSON 表达得了的值：枚举取 `.value`，dataclass 按字段，集合
    排成有序的列表（指纹要逐次相同），非字符串的键与认不出的叶子写成文字，非有限数写成文字，超大的
    整数写成十六进制文字；环写成 `<cycle>`，超出深度或节点数的部分写成标记。记录不能因为一个
    backend 在 `extra` 里放了别的东西就丢掉整条。"""
    return _walk(value, 0, _Walk())


def _walk(value: object, depth: int, state: _Walk) -> object:
    state.remaining -= 1
    if state.remaining < 0:
        return '<truncated>'
    if depth > _MAX_DEPTH:
        return '<nested too deep>'
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, int):
        return value if value.bit_length() <= _MAX_INT_BITS else hex(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Enum):
        return _walk(cast(object, value.value), depth + 1, state)
    is_record = dataclasses.is_dataclass(value) and not isinstance(value, type)
    if not (is_record or isinstance(value, Mapping | list | tuple | set | frozenset)):
        return _text_of(value)
    marker = id(cast(object, value))
    if marker in state.active:
        return '<cycle>'
    state.active.add(marker)
    try:
        if is_record:
            record = cast(Any, value)
            return {
                item.name: _walk(cast(object, getattr(record, item.name)), depth + 1, state)
                for item in dataclasses.fields(record)
            }
        if isinstance(value, Mapping):
            mapping = cast(Mapping[object, object], value)
            return {
                key if isinstance(key, str) else _text_of(key): _walk(item, depth + 1, state)
                for key, item in mapping.items()
            }
        if isinstance(value, list | tuple):
            return [_walk(item, depth + 1, state) for item in cast(Sequence[object], value)]
        items = [_walk(item, depth + 1, state) for item in cast(set[object], value)]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    finally:
        state.active.discard(marker)


def _text_of(value: object) -> str:
    """认不出的东西写成文字。默认的 repr 带内存地址、每次运行都不同（指纹要逐次相同），写成类型名；
    `__str__` 自己抛出时同样写成类型名。"""
    kind = type(value)
    name = f'<{kind.__module__}.{kind.__qualname__}>'
    if kind.__str__ is object.__str__ and kind.__repr__ is object.__repr__:
        return name
    try:
        return str(value)
    except Exception:
        return name


def _call_value(call: ToolCall) -> dict[str, object]:
    """一次工具调用。参数按解析后的值记在 `arguments`（规范编码改了键序，指纹与比对都按值）；解析
    不了的原串记在 `arguments_raw`：都记成一个字符串叶子的话，原串 `ls` 与解析出来恰好是字符串的
    `"ls"` 就分不开，回放交回的字节也就不同。"""
    try:
        parsed = json.loads(call.arguments)
    except ValueError:
        return {'id': call.id, 'name': call.name, 'arguments_raw': call.arguments}
    return {'id': call.id, 'name': call.name, 'arguments': _jsonable(parsed)}


def _block_value(block: ContextBlock) -> dict[str, object]:
    """一块上下文进指纹与请求记录的部分。`trust` 与 `extra` 不进：它们不发给提供方（D11）。"""
    return {
        'role': block.role,
        'content': _jsonable(block.content),
        'tool_calls': [_call_value(call) for call in block.tool_calls],
        'tool_call_id': block.tool_call_id,
    }


def _unit_value(block: object) -> object:
    """请求里的一条消息。不是 `ContextBlock` 的（`PRE_LLM_REQUEST` 的 hook 换进来的别的东西）
    按值记。"""
    return _block_value(block) if isinstance(block, ContextBlock) else _jsonable(block)


def _reply_value(reply: object) -> dict[str, object]:
    """回放交回的那条回复（D11）：比指纹单元多 `extra` 与 `trust`——它们不发给提供方，但回放出来的
    块要与原来的逐块相同。"""
    if not isinstance(reply, ContextBlock):
        return {'kind': 'reply', 'value': _jsonable(reply)}
    return {
        'kind': 'reply',
        **_block_value(reply),
        'extra': _jsonable(reply.extra),
        'trust': _jsonable(reply.trust),
    }


def _error_value(error: BaseException) -> dict[str, object]:
    value: dict[str, object] = {
        'kind': 'error',
        'type': f'{type(error).__module__}.{type(error).__qualname__}',
        'message': _text_of(error),
    }
    for name in ('status_code', 'retry_after'):
        found = getattr(error, name, None)
        if isinstance(found, int | float) and not isinstance(found, bool):
            value[name] = _jsonable(found)
    return value


def _result_value(result: ToolResult) -> dict[str, object]:
    return {
        'kind': 'result',
        'output': _jsonable(result.output),
        'meta': _jsonable(result.meta),
        'trust': _jsonable(result.trust),
        'artifacts': _jsonable(result.artifacts),
    }


def _answer_value(message: Message) -> dict[str, object]:
    """回放按请求键交回的那条答复（D11）。关联 id 不记：它是原始 run 里的请求 id，回放用自己的。"""
    return {
        'kind': 'answer',
        'sender': _jsonable(message.sender),
        'content': _jsonable(message.content),
        'trigger': _jsonable(message.trigger),
        'trust': _jsonable(message.trust),
    }


def _request_parts(request: Mapping[str, Any]) -> tuple[Sequence[object], dict[str, object]]:
    """一次模型请求拆成历史与模型参数：`create(messages, **kwargs)` 的那两部分。"""
    raw = cast(object, request.get('messages', ()))
    messages = cast(Sequence[object], raw) if isinstance(raw, list | tuple) else (raw,)
    params = {
        _text_of(key): _jsonable(value) for key, value in request.items() if key != 'messages'
    }
    return messages, params


def arguments_digest(arguments: Mapping[str, object], redactor: Redactor) -> str:
    """一次工具调用真正执行的参数的指纹：逐叶脱敏、规范编码，再取 sha256（回放文档的
    `tool_results[].arguments_digest`）。脱敏在取摘要之前：低熵的密钥经 sha256 照样猜得出。"""
    text = encode_redacted(_jsonable(dict(arguments)), redactor)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class RequestFingerprints:
    """一次模型请求的逐单元指纹（D11）：模型参数、system、工具定义、每条历史。每个单元先逐叶脱敏、
    再规范编码、再取 sha256；按单元原文的 sha256 缓存（原文按 ASCII 编码：落单的代理字符照样有键），
    每轮把整段上下文重新脱敏是轮数的平方。历史里正文是字符串的块另按它进指纹的四项（角色、正文、
    工具调用、`tool_call_id`）缓存：每轮重新编码一遍整段历史同样是轮数的平方，而每轮交来的块是拷贝、
    认不得同一个对象，正文却是同一个字符串。记录与回放用同一份：回放拿记录下的规则集对自己发出的
    请求算一遍，第一个对不上的单元就是分叉的地方。"""

    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor
        self._units: dict[str, str] = {}
        self._blocks: dict[tuple[object, ...], str] = {}

    def of(self, request: Mapping[str, Any]) -> list[str]:
        """`request` 是交给 `create` 的那一份：`messages` 加上其余关键字参数。"""
        return self.of_parts(*_request_parts(request))

    def of_parts(self, messages: Sequence[object], params: Mapping[str, object]) -> list[str]:
        system: object = None
        history: Sequence[object] = messages
        first = messages[0] if messages else None
        if isinstance(first, ContextBlock) and first.role == 'system':
            system = _block_value(first)
            history = messages[1:]
        model_params = {key: value for key, value in params.items() if key != 'tools'}
        digests = [
            self._digest(model_params),
            self._digest(system),
            self._digest(params.get('tools', [])),
        ]
        for block in history:
            # 预算块不进指纹：它写的是此刻还剩多少，而父的剩余取决于并发的子 Agent 此刻占着多少
            # （预留随周期结算、随唤醒重切），回放不复现跨 Agent 的先后。它只说事实、不改变别的块。
            if isinstance(block, ContextBlock) and kind_of(block) is HistoryKind.BUDGET:
                continue
            key = _block_key(block)
            digest = None if key is None else self._blocks.get(key)
            if digest is None:
                digest = self._digest(_unit_value(block))
                if key is not None:
                    self._blocks[key] = digest
            digests.append(digest)
        return digests

    def _digest(self, unit: object) -> str:
        raw = json.dumps(unit, sort_keys=True)
        key = hashlib.sha256(raw.encode('ascii')).hexdigest()
        digest = self._units.get(key)
        if digest is None:
            redacted = encode_redacted(unit, self._redactor)
            digest = hashlib.sha256(redacted.encode('utf-8')).hexdigest()
            self._units[key] = digest
        return digest


def _block_key(block: object) -> tuple[object, ...] | None:
    """一块历史进指纹的全部四项（`_block_value` 读的就是它们）；正文不是字符串或 None 时交回 None，
    走按原文的那一层缓存。"""
    if not isinstance(block, ContextBlock):
        return None
    content = cast(object, block.content)
    if content is not None and not isinstance(content, str):
        return None
    calls = tuple((call.id, call.name, call.arguments) for call in block.tool_calls)
    return (block.role, content, block.tool_call_id, calls)


@dataclass(frozen=True)
class _Payload:
    digest: str
    text: RedactedText


_STOP = object()


@dataclass
class _Open:
    parent: str | None
    kind: SpanKind
    name: str
    agent_id: str
    start_ns: int
    attributes: dict[str, AttributeValue]
    events: list[SpanEvent] = field(default_factory=list[SpanEvent])


class TraceRecorder:
    """实现 `Tracer`，把一次运行记成 `store` 里的一个 trace。

    用法：`await recorder.begin()`，把它交给 `Runtime(tracer=recorder)`，Runtime 关闭之后
    `await recorder.finish(status)`。`finish` 时还开着的 span 以 `cancelled` 收尾再写（D3）；不调
    `finish`，写入任务一直挂着、trace 没有结局。`begin` 抛出时这个 trace 没有开始，此后收到的记录
    丢弃并计数。一个 recorder 只交给一个 Runtime：span id 只在一个 Runtime 里唯一。

    时间只在 `begin` 读一次墙钟（`clock`，Unix 纳秒，可注入），此后全按 `monotonic` 量出的间隔往后
    推，且不回退：墙钟可以回拨，而模型拒收结束早于开始的 span，一个 span 也不该落在它的 trace 之外。

    请求指纹的「模型参数」单元是内核交给 `create` 的关键字参数（不含消息与工具定义）；后端自己的
    配置不在其中——模型名记在 `llm.model` 上。人的答复由内核在等待等到它时交过来（`wait_answer`），
    挂在那次等待上。

    `replay` 与 `full` 级还写一份回放文档（`replay.json`，M3 D12）：宿主在 run 开始之前把只有它知道
    的部分交给 `describe_run`、把 `ids` 交给 `Runtime(ids=...)`，`finish` 时与插桩攒下的部分合成
    一份、逐叶脱敏（内核生成的 id 与摘要原样）、按场景格式读一遍确认读得回来，再交给 store。写不出
    （Agent 的 id 不是 `ids` 交出的、宿主漏交了角色）只少这一份文档，记一条三件套的 WARNING；trace
    照常。
    """

    def __init__(
        self,
        store: TraceStore,
        redactor: RuleRedactor,
        *,
        level: TraceLevel = TraceLevel.REPLAY,
        source: TraceSource | None = None,
        clock: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], int] = time.monotonic_ns,
        trace_id: str | None = None,
    ) -> None:
        given = cast(object, store)
        if isinstance(given, type) or not isinstance(given, TraceStore):
            raise ConfigError(
                expected='store 是一个实现 TraceStore 的实例',
                received=f'收到 {store!r}（{type(store).__name__}）',
                next_action='传 InMemoryTraceStore() 或 FileTraceStore(...) 这样的实例，不要传类',
            )
        if not isinstance(cast(object, redactor), RuleRedactor):
            raise ConfigError(
                expected='redactor 是 RuleRedactor：trace 的头要记下它的规则集与摘要',
                received=f'收到 {type(redactor).__name__}',
                next_action='传 RuleRedactor(...)；CLI 用的就是同一个',
            )
        if cast(object, level) not in _LEVELS:
            raise ConfigError(
                expected='level 是 TraceLevel.STRUCTURE / REPLAY / FULL',
                received=f'收到 {level!r}',
                next_action='不记录就不要建 TraceRecorder（TraceLevel.OFF 没有可记的东西）',
            )
        if source is not None and not isinstance(cast(object, source), TraceSource):
            raise ConfigError(
                expected='source 是 TraceSource 或 None',
                received=f'收到 {type(source).__name__}',
                next_action='用 TraceSource(argv=..., version=...) 构造；不知道来源就不传',
            )
        for name, given_clock, default in (
            ('clock', cast(object, clock), 'time_ns'),
            ('monotonic', cast(object, monotonic), 'monotonic_ns'),
        ):
            if not callable(given_clock):
                raise ConfigError(
                    expected=f'{name} 是不带参数、交回整数纳秒的函数',
                    received=f'收到 {type(given_clock).__name__}',
                    next_action=f'传 time.{default} 这样的函数本身，不要传调用它的结果',
                )
        if trace_id is not None and not (
            isinstance(cast(object, trace_id), str) and _TRACE_ID.fullmatch(trace_id)
        ):
            raise ConfigError(
                expected='trace_id 是 32 个小写十六进制字符',
                received=f'收到 {trace_id!r}',
                next_action='不传就用 uuid4().hex',
            )
        self._store = store
        self._redactor = redactor
        self._level = level
        self._source = TraceSource() if source is None else source
        self._clock = clock
        self._monotonic = monotonic
        self._trace_id = uuid.uuid4().hex if trace_id is None else trace_id
        self._open: dict[str, _Open] = {}
        self._queue: asyncio.Queue[object] | None = None
        self._writer: asyncio.Task[None] | None = None
        # 单元原文的 sha256 → 脱敏后的指纹：每轮把整段上下文重新脱敏是轮数的平方（D11）。
        self._fingerprints = RequestFingerprints(redactor)
        self._capture = ReplayCapture(_jsonable, redactor)
        self._capture_failed = False
        self._began_ns = 0
        self._began_mono = 0
        self._last_ns = 0
        self._begun = False
        self._finished = False
        self._failure: BaseException | None = None
        self._incomplete = False
        self._refusals = 0
        self._writer_gone = False
        self._completed = False
        self._dropped = 0

    @property
    def trace_id(self) -> str:
        return self._trace_id

    @property
    def level(self) -> TraceLevel:
        return self._level

    @property
    def ids(self) -> IdSource:
        """交给 `Runtime(ids=...)` 的 id 源：交出 uuid4，并记下每个稳定键交出了什么（回放文档的
        `ids`）。不交给 Runtime 就写不出回放文档。"""
        return self._capture.ids

    def describe_run(
        self,
        *,
        host: Mapping[str, object],
        roles: Mapping[str, Mapping[str, object]],
        entry: Mapping[str, object],
        topology: Topology | None = None,
    ) -> None:
        """回放文档里只有宿主知道的部分，写法同场景格式 v2：`host`（宿主档案）、`roles`（角色名
        → 冻结后的角色：`system_prompt` 正文、`tools` 按顺序、`max_turns`、`impl` 等）、`entry`
        （入口）。拓扑 run 另交 `topology`：实际装配的那一份（宿主改写过预算与工具之后），入口
        写成 `{mode: slot, slot: <根槽>}`。run 开始之前调；不调就不写回放文档。值在这里取走一份，
        此后宿主改它们不影响文档。"""
        for name, value in (('host', host), ('roles', roles), ('entry', entry)):
            if not isinstance(cast(object, value), Mapping):
                raise ConfigError(
                    expected=f'describe_run 的 {name} 是一个映射（写法同场景格式 v2）',
                    received=f'收到 {type(value).__name__}',
                    next_action=(
                        '传 dict，例如 entry={"mode": "query", "agent": "Lead", "prompt": "…"}'
                    ),
                )
        if topology is not None and not isinstance(cast(object, topology), Topology):
            raise ConfigError(
                expected='describe_run 的 topology 是 Topology（宿主实际装配的那一份），或省略',
                received=f'收到 {type(topology).__name__}',
                next_action=(
                    '传交给 Runtime.assemble 的同一个 Topology 对象，而不是配置里的映射或拓扑名'
                ),
            )
        for role, value in roles.items():
            if not isinstance(cast(object, role), str) or not isinstance(
                cast(object, value), Mapping
            ):
                raise ConfigError(
                    expected='describe_run 的 roles 是角色名（str）→ 冻结后的角色（映射）',
                    received=f'其中一项是 {type(role).__name__} → {type(value).__name__}',
                    next_action='每个角色写成 {"system_prompt": …, "tools": […], …}',
                )
        if self._capture_failed:
            return
        try:
            self._capture.describe(host, roles, entry, topology)
        except Unreplayable as reason:
            self._capture_failed = True
            logger.warning(
                '期望: %s。 实际: %s。 下一步: %s。',
                reason.expected,
                reason.received,
                reason.next_action,
            )
        except Exception as error:
            self._capture_broken(error)

    def interrupted(self) -> None:
        """宿主因为人打断（Ctrl-C）要拆掉这次 run：在拆之前调。此刻还在等外部世界的调用记成回放
        文档里那条 `interrupt` 的位置，回放恰好停在这些调用上时才发出它。不调时，以 `cancelled`
        结束的 trace 记一条不带位置的 `interrupt`。"""
        self._capturing(self._capture.interrupted)

    @property
    def complete(self) -> bool:
        """这个 recorder 写下了 trace 的结局：`finish` 走完了，每一条记录都写进了 store（`finish`
        之后才到的不算）。为假时 store 里这个 trace 没有结局，回放与导出会拒绝它。判据是 recorder
        自己的账，不是读回来的头：头在 Agent 写得到的地方。"""
        return self._completed

    @property
    def dropped(self) -> int:
        """没写进 store 的记录数（payload、span、边各算一条）：store 失败或写入任务被取消之后的、
        store 收不下的、编码不出来的，以及 `finish` 之后才开始的 span 与才到的边。被 `finish` 截断的
        span 以 `cancelled` 写下，此后才到的回复与工具结局不记：取消不是那次调用的结局。"""
        return self._dropped

    async def begin(self) -> None:
        """写下 trace 的头，起写入任务。`Runtime` 跑起来之前调，只调一次。store 拒绝、或时钟交回的
        不是整数时原样上抛，这个 trace 没有开始。"""
        if self._begun:
            raise ConfigError(
                expected='每个 TraceRecorder 只 begin 一次',
                received=f'trace {self._trace_id} 已经开始过',
                next_action='每次运行新建一个 TraceRecorder',
            )
        self._begun = True
        try:
            began_ns, began_mono = self._clock(), self._monotonic()
            for name, value in (('clock', began_ns), ('monotonic', began_mono)):
                if not isinstance(cast(object, value), int) or isinstance(value, bool):
                    raise ConfigError(
                        expected=f'{name}() 交回整数纳秒',
                        received=f'交回 {type(value).__name__} 类型的 {value!r}',
                        next_action='传 time.time_ns / time.monotonic_ns 这样交回整数的函数',
                    )
            self._began_ns = self._last_ns = began_ns
            self._began_mono = began_mono
            header = TraceHeader(
                trace_id=self._trace_id,
                started_ns=began_ns,
                level=self._level,
                redaction=TraceRedaction(
                    digest=self._redactor.digest,
                    rules_version=RULES_VERSION,
                    rules=self._redactor.rules,
                ),
                source=dataclasses.replace(
                    self._source, argv=redact_argv(self._source.argv, self._redactor)
                ),
            )
            await self._store.begin(header)
        except BaseException as error:
            self._failure = error
            raise
        self._queue = asyncio.Queue()
        self._writer = asyncio.create_task(
            self._write_loop(self._queue), name=f'trace:{self._trace_id}'
        )
        self._writer.add_done_callback(self._writer_ended)

    async def finish(self, status: SpanStatus = SpanStatus.OK) -> None:
        """收尾：还开着的 span 以 `cancelled` 写下，等写入任务排空，写下 trace 的结局。只调一次。
        被取消时写入任务照样排空（不留下写了一半的批），结局不写；trace 不完整时结局也不写。写入任务
        先被别人取消了不往这里抛：那是这个 trace 的失败，不是这次调用的。"""
        if not isinstance(cast(object, status), SpanStatus):
            raise ConfigError(
                expected='status 是 SpanStatus 的成员',
                received=f'收到 {status!r}',
                next_action='传 SpanStatus.OK / ERROR / CANCELLED 之一',
            )
        if not self._begun or self._finished:
            raise ConfigError(
                expected='finish 在 begin 之后调、且只调一次',
                received='还没有 begin' if not self._begun else '已经 finish 过',
                next_action='按 begin → 运行 → finish 的顺序用，每次运行新建一个 TraceRecorder',
            )
        if self._queue is None and self._failure is None:
            raise ConfigError(
                expected='finish 在 begin 返回之后调',
                received=f'trace {self._trace_id} 的 begin 还没完成',
                next_action='先 await recorder.begin()，等它返回再 finish',
            )
        self._finished = True
        try:
            for span_id in list(self._open):
                self.end_span(span_id, SpanStatus.CANCELLED, {})
        finally:
            if self._queue is not None:
                self._queue.put_nowait(_STOP)
            if self._writer is not None:
                await self._drained(self._writer)
        if self._failure is not None or self._incomplete:
            return
        await self._write_replay(status)
        if self._failure is not None:
            return
        try:
            await self._store.finish(self._trace_id, status, self._now())
        except BaseException as error:
            if not _raised_by_store(error):
                raise
            self._fail('finish', error)
            return
        self._completed = True

    # --- Tracer ---------------------------------------------------------------------------

    def start_span(
        self,
        span_id: str,
        parent_span_id: str | None,
        kind: SpanKind,
        name: str,
        agent_id: str,
        attributes: dict[str, AttributeValue],
    ) -> None:
        if self._finished:
            self._dropped += 1
            return
        if not self._begun:
            raise _not_begun(self._trace_id)
        if span_id in self._open:
            self._lose(
                f'{kind.value} span {span_id}',
                ContractViolation(
                    expected='一个 trace 里每个 span id 只开一次',
                    received=f'span {span_id} 还开着就又开了一次；先开的那一个丢了',
                    next_action='一个 TraceRecorder 只交给一个 Runtime',
                ),
            )
        self._open[span_id] = _Open(
            parent=parent_span_id,
            kind=kind,
            name=name,
            agent_id=agent_id,
            start_ns=self._now(),
            attributes=dict(attributes),
        )
        if self._level is not TraceLevel.STRUCTURE:
            parent = None if parent_span_id is None else self._open.get(parent_span_id)
            located = (
                None
                if parent is None or parent_span_id is None
                else (parent.kind, parent.attributes, parent_span_id)
            )
            self._capturing(
                lambda: self._capture.started(span_id, kind, agent_id, attributes, located)
            )

    def end_span(
        self, span_id: str, status: SpanStatus, attributes: dict[str, AttributeValue]
    ) -> None:
        span = self._open.pop(span_id, None)
        if span is None:
            return
        if self._failure is not None:
            self._dropped += 1
            return
        span.attributes.update(attributes)
        if self._level is not TraceLevel.STRUCTURE:
            self._capturing(
                lambda: self._capture.ended(
                    span_id, span.kind, span.agent_id, status, span.attributes
                )
            )
        try:
            record = Span(
                span_id=span_id,
                parent_span_id=span.parent,
                kind=span.kind,
                name=self._text(span.name),
                agent_id=span.agent_id,
                start_ns=span.start_ns,
                end_ns=self._now(),
                status=status,
                attributes=self._attributes(span.attributes),
                events=tuple(span.events),
            )
        except Exception as error:
            self._lose(f'{span.kind.value} span {span_id}', error)
            return
        self._put(record)

    def add_event(self, span_id: str, name: str, attributes: dict[str, AttributeValue]) -> None:
        message_id = attributes.get('message.id')
        if isinstance(message_id, str) and self._level is not TraceLevel.STRUCTURE:
            if name == 'message.sent':
                self._capturing(lambda: self._capture.sent(message_id))
            elif name == 'message.deferred':
                self._capturing(lambda: self._capture.deferred(span_id, message_id))
        span = self._open.get(span_id)
        if span is None or self._failure is not None:
            return
        try:
            event = SpanEvent(
                name=self._text(name), time_ns=self._now(), attributes=self._attributes(attributes)
            )
        except Exception as error:
            self._lose(f'span {span_id} 上的事件', error)
            return
        span.events.append(event)

    def add_edge(self, edge: CausalityEdge) -> None:
        if self._finished:
            self._dropped += 1
            return
        message_id = edge.attributes.get('message.id')
        if (
            edge.kind is EdgeKind.MESSAGE
            and isinstance(message_id, str)
            and self._level is not TraceLevel.STRUCTURE
        ):
            target = edge.target_span_id
            self._capturing(lambda: self._capture.consumed(target, message_id))
        try:
            record = dataclasses.replace(edge, attributes=self._attributes(edge.attributes))
        except Exception as error:
            self._lose('一条因果边', error)
            return
        self._put(record)

    def llm_exchange(
        self,
        span_id: str,
        request: Mapping[str, Any],
        reply: ContextBlock | None,
        error: BaseException | None,
    ) -> None:
        span = self._open.get(span_id)
        if span is None or self._level is TraceLevel.STRUCTURE:
            return
        units: list[str] | None = None
        if self._failure is not None:
            # 完整的请求本该排进去的那一段 payload：不算，只计数。
            self._dropped += 1 if self._level is TraceLevel.FULL else 0
        else:
            try:
                messages, params = _request_parts(request)
                units = self._fingerprints.of_parts(messages, params)
                span.attributes['llm.request_digest'] = hashlib.sha256(
                    ''.join(units).encode()
                ).hexdigest()
                if self._level is TraceLevel.FULL:
                    whole = {
                        'messages': [_unit_value(block) for block in messages],
                        'params': params,
                    }
                    span.attributes['llm.request'] = self._payload(whole)
            except Exception as failure:
                self._lose(f'llm_call span {span_id} 的请求', failure)
        if error is not None:
            self._attach(span, 'llm.response', lambda: _error_value(error))
        elif reply is not None:
            self._attach(span, 'llm.response', lambda: _reply_value(reply))
        self._capturing(lambda: self._capture.exchanged(span_id, units, reply, error))

    def tool_result(
        self,
        span_id: str,
        result: ToolResult | None,
        error: BaseException | None,
        *,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        span = self._open.get(span_id)
        if span is None or self._level is TraceLevel.STRUCTURE:
            return
        if error is not None:
            self._attach(span, 'tool.result', lambda: _error_value(error))
        elif result is not None:
            self._attach(span, 'tool.result', lambda: _result_value(result))

        def capture() -> None:
            digest = None if arguments is None else arguments_digest(arguments, self._redactor)
            self._capture.returned(span_id, result, error, digest)

        self._capturing(capture)

    def wait_answer(self, span_id: str, message: Message) -> None:
        """只记人的答复：等 Agent 的答复在回放里由那个 Agent 真跑出来（D11），记下来没人用；宿主
        直接发来的消息是宿主的动作，另记（D11 `host_actions`）。"""
        span = self._open.get(span_id)
        if span is None or self._level is TraceLevel.STRUCTURE:
            return
        if span.attributes.get('wait.kind') == 'human':
            self._attach(span, 'wait.answer', lambda: _answer_value(message))
            self._capturing(lambda: self._capture.answered(span_id, message))

    # --- internals ------------------------------------------------------------------------

    def _capturing(self, action: Callable[[], None]) -> None:
        """回放文档的账本出错只少这一份文档：trace 与 run 都不受影响，ERROR 恰好一条。宿主没有调
        `describe_run` 时、store 已经坏了时不记：不会写的文档，攒下来只占内存。"""
        if self._capture_failed or not self._capture.wanted or self._failure is not None:
            return
        try:
            action()
        except Exception as error:
            self._capture_broken(error)

    def _capture_broken(self, error: Exception) -> None:
        if not self._capture_failed:
            self._capture_failed = True
            logger.error(
                '期望: 回放文档要的每一项都记得下来。'
                ' 实际: 记录时抛出 %s: %s；trace %s 不写回放文档，其余照常记录。'
                ' 下一步: 这是 TraceRecorder 的缺陷，带上这条日志报告。',
                type(error).__name__,
                error,
                self._trace_id,
            )

    async def _write_replay(self, status: SpanStatus) -> None:
        if self._level is TraceLevel.STRUCTURE or not self._capture.wanted or self._capture_failed:
            return
        try:
            source = dataclasses.replace(
                self._source, argv=redact_argv(self._source.argv, self._redactor)
            )
            document, identifiers = self._capture.document(self._trace_id, source, status)
        except Unreplayable as reason:
            logger.warning(
                '期望: %s。 实际: %s。 下一步: %s。',
                reason.expected,
                reason.received,
                reason.next_action,
            )
            return
        except Exception as error:
            self._capture_failed = True
            logger.error(
                '期望: trace %s 的回放文档合得成一份。'
                ' 实际: 合成时抛出 %s: %s；这个 trace 不写回放文档，其余照常。'
                ' 下一步: 这是 TraceRecorder 的缺陷，带上这条日志报告。',
                self._trace_id,
                type(error).__name__,
                error,
            )
            return
        try:
            text = encode_prepared(document, self._redactor, identifiers=identifiers)
            parse_scenario_document(json.loads(text), source=f'trace {self._trace_id} 的回放文档')
        except Exception as error:
            # 里面那条错误的「下一步」写给的是手写文档的人，这里只取它说了什么。
            said = getattr(error, 'received', None)
            where = getattr(error, 'expected', None)
            detail = (
                f'{where}；{said}'
                if isinstance(said, str) and isinstance(where, str)
                else f'{type(error).__name__}: {error}'
            )
            rewriting = [
                rule.rule_id
                for rule in self._redactor.rules
                if _placeholder(rule.rule_id) in detail
            ]
            if rewriting:
                logger.warning(
                    '期望: 用户脱敏规则只换掉内容，不改写回放文档的结构（字段名与内核的标识）。'
                    ' 实际: %s 改写了其中一项：%s；这个 trace 不写回放文档，其余照常。'
                    ' 下一步: 收窄这条规则的 pattern，让它认不出上面那个值。',
                    ', '.join(rewriting),
                    detail,
                )
                return
            if _placeholder(_ENVIRONMENT_RULE) in detail:
                logger.warning(
                    '期望: 宿主环境里的密钥值只换掉内容，不改写回放文档的结构'
                    '（字段名与内核的标识）。'
                    ' 实际: 一个名字像密钥的环境变量，它的值恰好是结构里的一段，被逐字换掉了：%s；'
                    '这个 trace 不写回放文档，其余照常。'
                    ' 下一步: 那个变量的值太像一个普通的词：换成真的凭证，'
                    '或者别把它放进运行的环境。',
                    detail,
                )
                return
            logger.error(
                '期望: trace %s 写出的回放文档按场景格式读得回来。'
                ' 实际: %s；这个 trace 不写回放文档，其余照常。'
                ' 下一步: 对照上面说的那一项检查宿主交给 describe_run 的值；它们都合格时这是'
                ' TraceRecorder 的缺陷，带上这条日志报告。',
                self._trace_id,
                detail,
            )
            return
        try:
            await self._store.put_replay(self._trace_id, text)
        except PayloadRefused as refusal:
            logger.warning(
                '期望: store 收下 trace %s 的回放文档。'
                ' 实际: store 收不下这份 %d 字节的文档：%s；这个 trace 没有回放文档，其余照常。'
                ' 下一步: %s。',
                self._trace_id,
                len(text.encode('utf-8')),
                refusal.received,
                refusal.next_action,
            )
        except BaseException as error:
            if not _raised_by_store(error):
                raise
            self._fail('写回放文档', error)

    def _now(self) -> int:
        """`begin` 时的墙钟加上单调时钟走过的间隔，且不回退（从 `begin` 那一刻起取最大值）。"""
        measured = self._began_ns + self._monotonic() - self._began_mono
        self._last_ns = max(self._last_ns, measured)
        return self._last_ns

    def _text(self, text: str) -> str:
        return str(self._redactor.redact(text))

    def _attributes(self, attributes: Mapping[str, AttributeValue]) -> dict[str, AttributeValue]:
        redacted: dict[str, AttributeValue] = {}
        for key, value in attributes.items():
            if verbatim_attribute(key):
                redacted[key] = value
            elif isinstance(value, str):
                redacted[key] = self._text(value)
            elif isinstance(value, list | tuple):
                redacted[key] = [self._text(item) for item in value]
            else:
                redacted[key] = value
        return redacted

    def _attach(self, span: _Open, key: str, build: Callable[[], object]) -> None:
        if self._failure is not None:
            self._dropped += 1
            return
        try:
            span.attributes[key] = self._payload(build())
        except Exception as error:
            self._lose(f'{span.kind.value} span 的 {key}', error)

    def _payload(self, value: object) -> str:
        """脱敏、编码、排进写入队列，交回引用。"""
        return self._queued(encode_redacted(_jsonable(value), self._redactor))

    def _queued(self, text: RedactedText) -> str:
        """排进写入队列，交回引用（内容 UTF-8 编码的 sha256，与 store 算的同一个）。payload 排在
        引用它的 span 之前：span 在收尾时才排进去。"""
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        self._put(_Payload(digest, text))
        return digest

    def _put(self, item: _Payload | Span | CausalityEdge) -> None:
        if self._queue is None:
            if self._failure is not None:
                self._dropped += 1
                return
            raise _not_begun(self._trace_id)
        self._queue.put_nowait(item)

    async def _write_loop(self, queue: asyncio.Queue[object]) -> None:
        while True:
            batch = [await queue.get()]
            while not queue.empty():
                batch.append(queue.get_nowait())
            await self._write(
                [item for item in batch if isinstance(item, _Payload)],
                [item for item in batch if isinstance(item, Span)],
                [item for item in batch if isinstance(item, CausalityEdge)],
            )
            if any(item is _STOP for item in batch):
                return

    async def _write(
        self, payloads: list[_Payload], spans: list[Span], edges: list[CausalityEdge]
    ) -> None:
        if self._failure is not None:
            self._dropped += len(payloads) + len(spans) + len(edges)
            return
        handled = 0
        try:
            for payload in payloads:
                try:
                    ref = await self._store.put_payload(self._trace_id, payload.text)
                except PayloadRefused as refusal:
                    self._refused(payload, refusal)
                    handled += 1
                    continue
                if ref.trace_id != self._trace_id or ref.digest != payload.digest:
                    raise ContractViolation(
                        expected=(
                            'put_payload 交回这个 trace 的 id 与内容 UTF-8 编码的 sha256'
                            '（TraceStore 契约）'
                        ),
                        received=f'交回 {ref!r}；内容的 sha256 是 {payload.digest}',
                        next_action='修这个 store 的 put_payload：span 按内容的 sha256 引用 '
                        'payload，对不上就再也找不回内容',
                    )
                handled += 1
            if spans or edges:
                await self._store.append(self._trace_id, spans, edges)
        except BaseException as error:
            self._dropped += len(payloads) - handled + len(spans) + len(edges)
            if not _raised_by_store(error):
                raise
            self._fail('写入', error)

    async def _drained(self, writer: asyncio.Task[None]) -> None:
        try:
            await asyncio.shield(writer)
        except BaseException:
            task = asyncio.current_task()
            if not writer.done() or (task is not None and task.cancelling()):
                raise
            self._writer_ended(writer)

    def _writer_ended(self, writer: asyncio.Task[None]) -> None:
        """写入任务不是因为 `finish` 放进去的停止标记而结束的：宿主取消了它（关机时取消全部任务），
        或者它死于 recorder 的缺陷。还排着的计入 `dropped`，此后的只计数；ERROR 恰好一条。"""
        if self._writer_gone:
            return
        if writer.cancelled():
            error: BaseException = asyncio.CancelledError()
            cause = '写入任务被取消了（取消的是它本身，不是等它的那一方）'
        else:
            failure = writer.exception()
            if failure is None:
                return
            error = failure
            cause = f'写入任务抛出 {type(failure).__name__}: {failure}'
        self._writer_gone = True
        lost = 0
        if self._queue is not None:
            while not self._queue.empty():
                if self._queue.get_nowait() is not _STOP:
                    lost += 1
        self._dropped += lost
        if self._failure is not None:
            return
        self._failure = error
        logger.error(
            '期望: trace %s 的写入任务把每一条记录写进 %s。'
            ' 实际: %s；还排着的 %d 条记录丢了，此后的只计数，run 照常进行。'
            ' 下一步: 关机时先 await recorder.finish()，再取消别的任务（写入任务名为 trace:%s）；'
            '这个 trace 不完整，不要拿它回放或导出。',
            self._trace_id,
            type(self._store).__name__,
            cause,
            lost,
            self._trace_id,
        )

    def _refused(self, payload: _Payload, refusal: PayloadRefused) -> None:
        """store 收不下这一段：只丢它，其余照常写，这个 trace 不写结局。ERROR 只记第一段。"""
        self._dropped += 1
        self._incomplete = True
        self._refusals += 1
        if self._refusals > 1:
            return
        logger.error(
            '期望: trace %s 的每一条记录都写进 %s。'
            ' 实际: store 收不下一段 %d 字节的 payload：%s；只丢了这一段（再收不下的只计数），'
            '其余照常写，但这个 trace 不写结局。'
            ' 下一步: %s；这个 trace 不完整，不要拿它回放或导出。',
            self._trace_id,
            type(self._store).__name__,
            len(payload.text.encode('utf-8')),
            refusal.received,
            refusal.next_action,
        )

    def _fail(self, step: str, error: BaseException) -> None:
        """只在还没失败过时调：此后的记录走 `_write` 开头只计数的那条路，这条 ERROR 恰好一条。"""
        self._failure = error
        # 三件套的错误自己说得出下一步（store 发现目录被改过、recorder 发现 store 交回的引用不对），
        # 一律换成「store 有缺陷」会让人去修一个没问题的 store。
        own = getattr(error, 'next_action', None)
        repair = (
            own
            if isinstance(own, str) and own.strip()
            else '检查 store（磁盘空间、权限、远端连通）'
        )
        logger.error(
            '期望: trace %s 的每一条记录都写进 %s。'
            ' 实际: %s 时抛出 %s: %s；此后这个 trace 的记录全部丢弃，run 照常进行。'
            ' 下一步: %s；这个 trace 不完整，不要拿它回放或导出。',
            self._trace_id,
            type(self._store).__name__,
            step,
            type(error).__name__,
            error,
            repair,
        )

    def _lose(self, what: str, error: Exception) -> None:
        """一条记录编码不出来：丢掉它、计数，这个 trace 从此不完整（`finish` 不写结局）。第一次记
        一条 ERROR：这是插桩或 recorder 的缺陷，不是 store 的。"""
        self._dropped += 1
        if self._incomplete:
            return
        self._incomplete = True
        logger.error(
            '期望: trace %s 的每一条记录都编码得出来。'
            ' 实际: %s 编码时抛出 %s: %s；这一条丢了，此后照常记录，但这个 trace 不写结局。'
            ' 下一步: 这是插桩或 TraceRecorder 的缺陷，带上这条日志报告；不要拿这个 trace 回放。',
            self._trace_id,
            what,
            type(error).__name__,
            error,
        )


def _raised_by_store(error: BaseException) -> bool:
    """这是 store 的失败，而不是这个任务正在被取消、或解释器要退出：store 自己抛出的
    `CancelledError`（它内部的超时、它自己的任务被取消）也算 store 的失败。"""
    if isinstance(error, asyncio.CancelledError):
        task = asyncio.current_task()
        return task is not None and not task.cancelling()
    return isinstance(error, Exception)


def _not_begun(trace_id: str) -> ConfigError:
    return ConfigError(
        expected='TraceRecorder 在 begin 之后才收插桩',
        received=f'trace {trace_id} 还没有 begin',
        next_action='先 await recorder.begin()，再把它交给 Runtime',
    )
