"""内核插桩：同步的 `Tracer` 与因果边（M3 D2 / D3）。

**每一次调用都是同步的。** 内核里有几处正确性依赖「这两句之间没有挂起点」（spawn 的登记与建任务、
停在邮箱上、装等待令牌之后复检中止、`run` 的检查与置位）；一个会 await 的 tracer 会在这些窗口里插进
调度，而且只在会挂起的存储下才插，内存存储下的测试照绿。记录要落盘就由 tracer 自己缓冲、自己的任务
去写。

span id 由结构键确定（D3）：同一个结构键在两次运行里得到同一个 id，回放因此能逐个对上。键里只放与
调度无关的事实（agent id、周期、轮、批内位置、出处里的第几个），模型给的 `tool_call.id` 不进键——它
可以重复，可以是空串。

tracer 坏掉不改变 run：内核经 `_Tracing` 调它。同步调用里不可能收到 asyncio 的取消（取消只在 await
处进入协程），所以 tracer 抛出的任何东西都是它自己的缺陷，只有 `KeyboardInterrupt` 与 `SystemExit`
照常上抛——与 asyncio 运行同步回调的规矩一致。其余的第一次记一条 ERROR，此后只计数，Runtime 关闭时
报一次总数：插桩点坐在内核原本不会抛出的几段之间（建任务之前、拆除的收据之后、答复离开邮箱之后），
一个漏出来的 `CancelledError` 会让 Agent 停在半路且一行日志都没有。
"""

from __future__ import annotations

import hashlib
import inspect
import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from syndica.kernel.errors import ContractViolation
from syndica.kernel.trace import AttributeValue, CausalityEdge, EdgeKind, SpanKind, SpanStatus

if TYPE_CHECKING:
    from syndica.kernel.budget_tree import BudgetAccount
    from syndica.kernel.context import ContextBlock
    from syndica.kernel.message import Message
    from syndica.kernel.tool import ToolResult

logger = logging.getLogger(__name__)

__all__ = [
    'NullTracer',
    'Tracer',
    'back_in_spans',
    'current_llm_call',
    'current_span_id',
    'llm_attempt',
    'outside_any_span',
    'span_id_of',
]


@runtime_checkable
class Tracer(Protocol):
    """接收内核插桩的一方。

    方法全是同步的，**不得 await、不得让出控制权**：内核在不允许挂起的窗口里调它们。时间由 tracer
    自己取：内核不传时间戳，一个 tracer 用墙钟、另一个用注入的时钟都不需要内核知道。

    `attributes` 是这次调用专属的一份，tracer 可以留着它。span 的开始与结束一一配对：结束时
    `status` 说明它怎么结束（取消也是一种结束），结束时的属性补在开始时那一份之上。

    **已有方法的签名不再变**：不加参数、不改参数的名字与种类。要新的通道就加新方法，继承
    `NullTracer` 的实现自动拿到它的空实现；给已有方法多加一个参数，按旧签名写的每一个 tracer 在那个
    通道上全部失败，而 tracer 的失败只计数。
    """

    def start_span(
        self,
        span_id: str,
        parent_span_id: str | None,
        kind: SpanKind,
        name: str,
        agent_id: str,
        attributes: dict[str, AttributeValue],
    ) -> None: ...

    def end_span(
        self, span_id: str, status: SpanStatus, attributes: dict[str, AttributeValue]
    ) -> None: ...

    def add_event(self, span_id: str, name: str, attributes: dict[str, AttributeValue]) -> None: ...

    def add_edge(self, edge: CausalityEdge) -> None: ...

    def llm_exchange(
        self,
        span_id: str,
        request: Mapping[str, Any],
        reply: ContextBlock | None,
        error: BaseException | None,
    ) -> None:
        """一次模型调用（`span_id` 那个 llm_call span）的请求与结局：回复，或它抛出的异常。在 span
        收尾之前到达。对象由内核持有，要留着内容就在这次调用里取走。"""
        ...

    def tool_result(
        self,
        span_id: str,
        result: ToolResult | None,
        error: BaseException | None,
        *,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        """一次工具执行（`span_id` 那个 tool_call span）交回的结果，或它抛出的异常——工具自己的
        结局，`POST_TOOL_USE` 改写之前；被 `PRE_TOOL_USE` 跳过的调用交的是跳过它的那个结果。在 span
        收尾之前到达。内核自己写的结局（未知工具、坏参数、阻断、确认的结局）不经这里：回放时内核
        照样写得出来。`arguments` 是真正交给工具执行的参数（hook 与确认改写之后）；跳过的调用没有
        执行，是 None。"""
        ...

    def wait_answer(self, span_id: str, message: Message) -> None:
        """一次 `wait_for`（`span_id` 那个 wait span）等到的那条消息。只在等到了时到达，在 span
        收尾之前：此刻这条消息已经离开邮箱，由内核交给等待它的工具。"""
        ...


class NullTracer:
    """默认 tracer：什么都不记。"""

    def start_span(
        self,
        span_id: str,
        parent_span_id: str | None,
        kind: SpanKind,
        name: str,
        agent_id: str,
        attributes: dict[str, AttributeValue],
    ) -> None:
        return None

    def end_span(
        self, span_id: str, status: SpanStatus, attributes: dict[str, AttributeValue]
    ) -> None:
        return None

    def add_event(self, span_id: str, name: str, attributes: dict[str, AttributeValue]) -> None:
        return None

    def add_edge(self, edge: CausalityEdge) -> None:
        return None

    def llm_exchange(
        self,
        span_id: str,
        request: Mapping[str, Any],
        reply: ContextBlock | None,
        error: BaseException | None,
    ) -> None:
        return None

    def tool_result(
        self,
        span_id: str,
        result: ToolResult | None,
        error: BaseException | None,
        *,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        return None

    def wait_answer(self, span_id: str, message: Message) -> None:
        return None


def span_id_of(kind: SpanKind, *key: str | int) -> str:
    """结构键 → span id（16 个十六进制字符）。

    第一个分量是 span 种类：agent、turn、wait 的其余分量形状可以相同。每个分量带类型标记与长度前缀：
    不带长度，`('ab', 'c')` 与 `('a', 'bc')` 撞；不带类型，`'1'` 与 `1` 撞。
    """
    digest = hashlib.sha256()
    for part in cast(tuple[object, ...], (kind.value, *key)):
        if isinstance(part, bool) or not isinstance(part, str | int):
            raise ContractViolation(
                expected='span 的结构键只由字符串与整数组成',
                received=f'{(kind.value, *key)!r} 里有 {type(part).__name__}',
                next_action='把这个分量换成它的字符串或整数形式再取 span id',
            )
        tag, data = ('s', part.encode('utf-8')) if isinstance(part, str) else ('i', b'%d' % part)
        digest.update(b'%s%d:' % (tag.encode('ascii'), len(data)) + data)
    return digest.hexdigest()[:16]


@dataclass
class _OpenSpan:
    """一个还开着的 span。`attributes` 是结束时补上的那些；没有接 tracer 时也照样可写。

    `owner` 是开它的那一份 `_Tracing`：当前 span 是模块级的 contextvar，工具里嵌着跑的另一个
    Runtime 会继承它，而那不是它的 span——认了，它的事件与边就记进别人的 trace。

    `ended` 之后它仍可以当边的起点（边只引用 id），但不再收事件：span 收尾之后就交出去了。工具
    里 `create_task` 出来、活得比这次调用长的任务继承着它，会在它结束之后才发消息。
    """

    span_id: str
    owner: _Tracing
    attributes: dict[str, AttributeValue] = field(default_factory=dict[str, AttributeValue])
    ended: bool = False


_CURRENT_SPAN: ContextVar[_OpenSpan | None] = ContextVar('syndica_current_span', default=None)


def current_span_id() -> str | None:
    """此刻所在的 span；不在任何 span 里时是 None。"""
    span = _CURRENT_SPAN.get()
    return None if span is None else span.span_id


@dataclass
class _LlmCall:
    tracing: _Tracing
    agent_id: str
    turn: int
    attempts: int = 0
    in_flight: bool = False
    # 这次调用记在谁的预算上（spec §8.4）。每一次 `create` 都经 `llm_attempt`：准入与计量挂在这里，
    # 主轮、重试、轮换、辅助调用与伴生就没有一条能绕过预算。
    account: BudgetAccount | None = None


_LLM_CALL: ContextVar[_LlmCall | None] = ContextVar('syndica_current_llm_call', default=None)

_Detached = tuple[Token[_OpenSpan | None], Token[_LlmCall | None]]


def outside_any_span() -> _Detached:
    """在当前上下文里清掉当前 span 与「当前 LLM 调用」，交回复位用的凭据。

    内核自己起的任务（驱动、收尾、关机）经 `create_task` 建在调用方的上下文里：不清，它们的事件与边
    就记在调用方的 span 上（那个 span 可能早已结束），在模型调用的 hook 里派出的子 Agent 还会读到
    父的「当前 LLM 调用」——回放按它取回复。在任务一开始清的不必复位；在调用方的上下文里临时离开的
    要用 `back_in_spans` 复位。
    """
    return _CURRENT_SPAN.set(None), _LLM_CALL.set(None)


def back_in_spans(tokens: _Detached) -> None:
    span, call = tokens
    _LLM_CALL.reset(call)
    _CURRENT_SPAN.reset(span)


def current_llm_call() -> tuple[str, int, int] | None:
    """此刻在飞的那次模型调用：（agent id、轮、第几次）。首次是 1，`RetryModule` 的每次重试与
    备用模型各加 1。只在 `create` 在飞时有值：模型调用前后的 hook 里、工具里都是 None——它们自己
    发起的模型调用不在这份计数里，回放模型按这个键取记录的回复（D11），给它一个属于别人的键只会
    交回别人的那条回复。"""
    call = _LLM_CALL.get()
    if call is None or not call.in_flight:
        return None
    return (call.agent_id, call.turn, call.attempts)


def _reset_quietly(var: ContextVar[object], token: Token[object]) -> None:
    # 协程没被取消就被 GC 关掉时，finally 跑在另一份上下文里，reset 会抛 ValueError；那份上下文
    # 随即丢弃，复位与否无所谓，但 span 的收尾不能因此丢掉。
    try:
        var.reset(token)
    except ValueError:
        pass


class _Span:
    """`with` 一个 span：进入时开始并成为当前 span，退出时按退出的方式收尾并复位。"""

    def __init__(
        self,
        tracing: _Tracing,
        kind: SpanKind,
        key: tuple[str | int, ...],
        *,
        name: str,
        agent_id: str,
        parent: str | None,
        attributes: dict[str, AttributeValue],
    ) -> None:
        self._tracing = tracing
        self._kind = kind
        self._name = name
        self._agent_id = agent_id
        self._parent = parent
        self._attributes = attributes
        self.span = _OpenSpan(span_id_of(kind, *key), tracing)
        self._token: Token[_OpenSpan | None] | None = None

    def __enter__(self) -> _OpenSpan:
        span_id = self.span.span_id
        self._tracing.call(
            'start_span',
            lambda tracer: tracer.start_span(
                span_id, self._parent, self._kind, self._name, self._agent_id, self._attributes
            ),
        )
        self._tracing.open_spans += 1
        self._token = _CURRENT_SPAN.set(self.span)
        return self.span

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._token is not None:
            _reset_quietly(
                cast(ContextVar[object], _CURRENT_SPAN), cast(Token[object], self._token)
            )
        self.span.ended = True
        self._tracing.open_spans -= 1
        if exc_type is None:
            status = SpanStatus.OK
        elif issubclass(exc_type, Exception):
            status = SpanStatus.ERROR
        else:
            status = SpanStatus.CANCELLED
        span_id = self.span.span_id
        attributes = dict(self.span.attributes)
        self._tracing.call('end_span', lambda tracer: tracer.end_span(span_id, status, attributes))


class _Tracing:
    """内核与 tracer 之间的那一层：算 span id、维护当前 span、隔离 tracer 的失败、把消息边连上。

    一个 Runtime 一份，交给它管的每个 Agent 与执行器；不经 Runtime 构造的 Agent 用自己的一份
    （默认什么都不记）。
    """

    def __init__(self, tracer: Tracer | None = None) -> None:
        self.tracer: Tracer = NullTracer() if tracer is None else tracer
        # 当前这个 tracer 失败了几次。换 tracer 时由换的一方报掉并清零：总数报给谁、是谁的。
        self.failures = 0
        # 还开着的 span 数。不为零时换 tracer，开始与结束就落在两个 tracer 上。
        self.open_spans = 0
        # 消息 id → 发送时的 span 与收件人。消费时取走；投递失败、收件人被销毁、它的邮箱随一次收尾
        # 被摘掉时丢掉，否则没人消费的消息会让这张表只增不减。
        self._sent_from: dict[str, tuple[str, str]] = {}

    def call(self, step: str, invoke: Callable[[Tracer], object]) -> None:
        tracer = self.tracer
        try:
            returned = invoke(tracer)
            if (
                inspect.isawaitable(returned)
                or inspect.isgenerator(returned)
                or inspect.isasyncgen(returned)
            ):
                # 写成 async def 或生成器的方法交回一个对象就返回了，方法体一行都没跑：记录静默
                # 丢失，只在 GC 时留下一行看不出主体的警告。关掉它，按一次失败报。
                if inspect.iscoroutine(returned) or inspect.isgenerator(returned):
                    returned.close()
                raise TypeError(
                    f'{step} 交回了一个 {type(returned).__name__}，而内核不 await、不迭代 tracer'
                )
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            self.failures += 1
            if self.failures == 1:
                logger.error(
                    '期望: tracer %s 收下内核的每一次插桩。'
                    ' 实际: 它在 %s 里抛出 %s: %s；这一次的记录丢了，run 照常进行。'
                    ' 下一步: %s；此后它的失败只计数，Runtime 关闭时报一次总数。',
                    type(tracer).__name__,
                    step,
                    type(error).__name__,
                    error,
                    _repair(error),
                )

    def _current(self) -> _OpenSpan | None:
        span = _CURRENT_SPAN.get()
        return span if span is not None and span.owner is self else None

    def span(
        self,
        kind: SpanKind,
        key: tuple[str | int, ...],
        *,
        name: str,
        agent_id: str,
        attributes: Mapping[str, AttributeValue] | None = None,
        detached: bool = False,
    ) -> _Span:
        """开一个 span。父 span 是此刻的当前 span；`detached` 的没有父（agent span：Agent 之间的
        关系只由边表达，驱动任务建在发送方的上下文里，继承来的当前 span 是唤醒它的人）。"""
        current = None if detached else self._current()
        return _Span(
            self,
            kind,
            key,
            name=name,
            agent_id=agent_id,
            parent=None if current is None else current.span_id,
            attributes=dict(attributes or {}),
        )

    def event(self, name: str, attributes: Mapping[str, AttributeValue]) -> None:
        """当前 span 上的一个事件；不在本 Runtime 任何开着的 span 里时不记。"""
        span = self._current()
        if span is None or span.ended:
            return
        span_id = span.span_id
        owned = dict(attributes)
        self.call('add_event', lambda tracer: tracer.add_event(span_id, name, owned))

    def spawned(self, child_id: str, cycle: int) -> None:
        """派生边：此刻的当前 span → 子 Agent 下一个工作周期（`cycle`）的 agent span。子 Agent 也许
        从没跑过（惰性启动），边上因此带着它的 id。"""
        source = self._current()
        if source is None:
            return
        edge = CausalityEdge(
            kind=EdgeKind.SPAWN,
            source_span_id=source.span_id,
            target_span_id=span_id_of(SpanKind.AGENT, child_id, cycle),
            attributes={'child.agent_id': child_id},
        )
        self.call('add_edge', lambda tracer: tracer.add_edge(edge))

    def artifact(self, writer: str, path: str, version: str, trust: str) -> None:
        """产物边：写下这个文件版本的那次工具调用的 span → 此刻读到它的 span（M5 D13）。"""
        target = self._current()
        if target is None or target.span_id == writer:
            return
        edge = CausalityEdge(
            kind=EdgeKind.ARTIFACT,
            source_span_id=writer,
            target_span_id=target.span_id,
            attributes={
                'artifact.path': path,
                'artifact.version': version,
                'artifact.trust': trust,
            },
        )
        self.call('add_edge', lambda tracer: tracer.add_edge(edge))

    def provenance(self, source: str, trust: str) -> None:
        """读到的版本不是这次 run 里哪次工具调用写的（基线或外部改的）：标在读它的 span 上。"""
        span = self._current()
        if span is None or span.ended:
            return
        span.attributes.update({'provenance.source': source, 'provenance.trust': trust})

    def sending(self, message_id: str, receiver: str) -> None:
        """记下这条消息从哪个 span 发出。在投递之前记：会挂起的 broker 下，收件人可能在 publish
        返回之前就已经消费了它。"""
        span = self._current()
        if span is not None:
            self._sent_from[message_id] = (span.span_id, receiver)

    def sent(self, message_id: str, receiver: str, *, delivered: bool) -> None:
        """投递有了结局：投到了就在发出它的 span 上记一个事件；没投到，消费不会发生，来源丢掉。"""
        if not delivered:
            self._sent_from.pop(message_id, None)
            return
        self.event('message.sent', {'message.id': message_id, 'message.to': receiver})

    def deferred(self, message_id: str) -> None:
        """门把这条消息从邮箱取走、暂存到下一个周期：在此刻的 span（取走它的那一轮）上记一个事件。
        消息边仍在它真被注入的那一轮记。"""
        self.event('message.deferred', {'message.id': message_id})

    def consumed(self, message_id: str) -> None:
        """消息边：发送时的 span → 此刻消费它的 span（注入它的那一轮，或等到它的那次等待）。"""
        sent = self._sent_from.pop(message_id, None)
        target = self._current()
        if sent is None or target is None:
            return
        edge = CausalityEdge(
            kind=EdgeKind.MESSAGE,
            source_span_id=sent[0],
            target_span_id=target.span_id,
            attributes={'message.id': message_id},
        )
        self.call('add_edge', lambda tracer: tracer.add_edge(edge))

    def forget_receiver(self, agent_id: str, *, keep: Collection[str] = ()) -> None:
        """发给它、还没被消费的消息再也不会被消费（它被销毁，或邮箱随一次收尾被摘掉）。`keep` 是
        已经从邮箱取出、暂存在控制块里的那些：它们随唤醒注入，还会被消费。"""
        stale = [
            message_id
            for message_id, (_, receiver) in self._sent_from.items()
            if receiver == agent_id and message_id not in keep
        ]
        for message_id in stale:
            del self._sent_from[message_id]

    def llm_call(self, agent_id: str, turn: int, account: BudgetAccount | None = None) -> _LlmScope:
        """一轮里的模型调用：首次与重试共用这一份计数（D3 / D11），也共用 `account` 这份预算。"""
        return _LlmScope(_LlmCall(self, agent_id, turn, account=account))

    def tool_result(
        self,
        span: _OpenSpan,
        result: ToolResult | None,
        error: BaseException | None,
        *,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        span_id = span.span_id
        self.call(
            'tool_result',
            lambda tracer: tracer.tool_result(span_id, result, error, arguments=arguments),
        )

    def wait_answer(self, span: _OpenSpan, message: Message) -> None:
        span_id = span.span_id
        self.call('wait_answer', lambda tracer: tracer.wait_answer(span_id, message))


class _LlmScope:
    def __init__(self, call: _LlmCall) -> None:
        self._call = call
        self._token: Token[_LlmCall | None] | None = None

    def __enter__(self) -> None:
        self._token = _LLM_CALL.set(self._call)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._token is not None:
            _reset_quietly(cast(ContextVar[object], _LLM_CALL), cast(Token[object], self._token))


_TOKEN_COUNTS = (
    ('input_tokens', 'llm.input_tokens'),
    ('output_tokens', 'llm.output_tokens'),
    ('cached_input_tokens', 'llm.cached_input_tokens'),
)
# trace 的整数是 int64（OTLP 与 SQLite 都是 64 位）：超出的整个 span 都写不出去。
_MAX_COUNT = 2**63 - 1


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_COUNT


def _model_of(llm: object) -> str | None:
    """`llm.config.model`（`LLM` 协议给的是 config）；不是非空字符串时 None。"""
    model = getattr(getattr(llm, 'config', None), 'model', None)
    return model if isinstance(model, str) and model else None


class _Attempt:
    """一次 `llm.create`：在当前模型调用里数一次、开一个 llm_call span，在飞期间「当前 LLM 调用」
    有值。"""

    def __init__(self, call: _LlmCall | None, llm: object, request: Mapping[str, Any]) -> None:
        self._call = call
        self._llm = llm
        self._request = request
        self._reply: object = None
        self._span: _Span | None = None
        self._ticket: object = None
        self._admitted = False

    def observe(self, reply: object) -> None:
        """把回复里 backend 报的事实记到这次的 span 上：哪个平台答的（`extra['llm_route']`）、同一次
        调用里先换掉了哪些平台（`extra['llm_failures']`，各记一个 `llm.platform_failed` 事件）、
        token 计数（`extra['usage']`）。类型不对的不记：trace 的属性是闭集，一个越界的整数会让
        整个 span 写不出去。

        模型名在这里重取一次：轮换里的包装器在调用之前报的是上一次作答的平台，这次换了平台，
        开始时记下的就是另一个平台的模型。

        读回复出错不改变 run：这是一份已经付过钱的回复，记不下它的事实不是重新调模型的理由。
        """
        if self._span is None or self._call is None:
            return
        self._reply = reply
        try:
            self._record(reply, self._span.span.attributes, self._call.tracing)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            logger.error(
                '期望: 模型回复的 extra 是一个读得出的映射。'
                ' 实际: 读它时抛出 %s: %s；这一次的平台与 token 计数没记下，run 照常进行。'
                ' 下一步: 检查这个 LLM backend 交回的 ContextBlock.extra。',
                type(error).__name__,
                error,
            )
        account = self._call.account
        if account is None:
            return
        ticket, self._ticket = self._ticket, None
        try:
            account.settle(cast('Any', ticket), self._llm, self._request, reply)
        except Exception as error:
            logger.error(
                '期望: 这次模型调用的消耗记进 agent %s 的预算。'
                ' 实际: 记账时抛出 %s: %s；这一次没有记上，预算因此少算了一次调用。'
                ' 下一步: 检查价格表（price_of 必须交回 Prices 或 None）与 backend 报的 usage。',
                self._call.agent_id,
                type(error).__name__,
                error,
            )

    def _record(
        self, reply: object, attributes: dict[str, AttributeValue], tracing: _Tracing
    ) -> None:
        model = _model_of(self._llm)
        if model is not None:
            attributes['llm.model'] = model
        extra = cast(object, getattr(reply, 'extra', None))
        if not isinstance(extra, Mapping):
            return
        reported = cast(Mapping[str, object], extra)
        route = reported.get('llm_route')
        if isinstance(route, str) and route:
            attributes['llm.platform'] = route
        failures = reported.get('llm_failures')
        if isinstance(failures, list | tuple):
            for failure in cast(Sequence[object], failures):
                event = _failure_event(failure)
                if event is not None:
                    tracing.event('llm.platform_failed', event)
        usage = reported.get('usage')
        if isinstance(usage, Mapping):
            counts = cast(Mapping[str, object], usage)
            for reported_key, attribute in _TOKEN_COUNTS:
                value = counts.get(reported_key)
                if value is None or _count(value):
                    attributes[attribute] = cast(int | None, value)

    async def __aenter__(self) -> _Attempt:
        """内核走的路：只因为兄弟在飞的估算放不下时等它们回来再试，而不是当成耗尽。"""
        call = self._call
        if call is not None and call.account is not None:
            self._ticket = await call.account.admit_waiting(self._llm, self._request)
            self._admitted = True
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc, tb)

    def __enter__(self) -> _Attempt:
        call = self._call
        if call is None:
            return self
        if call.account is not None and not self._admitted:
            # 放不进剩余预算时在这里抛 `BudgetExhausted`：调用不发出去、不开 span、不算一次尝试。
            self._ticket = call.account.admit(self._llm, self._request)
        call.attempts += 1
        attributes: dict[str, AttributeValue] = {'llm.attempt': call.attempts}
        model = _model_of(self._llm)
        if model is not None:
            attributes['llm.model'] = model
        self._span = call.tracing.span(
            SpanKind.LLM_CALL,
            (call.agent_id, call.turn, call.attempts),
            name='llm',
            agent_id=call.agent_id,
            attributes=attributes,
        )
        self._span.__enter__()
        call.in_flight = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._call is not None:
            self._call.in_flight = False
            if self._ticket is not None and self._call.account is not None:
                self._call.account.release(cast('Any', self._ticket))
                self._ticket = None
        if self._span is None or self._call is None:
            return
        # 取消不是这次调用的结局：被取消的调用没有回复也没有错误可回放（D11：回放在同一处阻塞）。
        if exc is None or isinstance(exc, Exception):
            span_id = self._span.span.span_id
            request, error = self._request, exc
            reply = cast('ContextBlock | None', self._reply) if exc is None else None
            self._call.tracing.call(
                'llm_exchange',
                lambda tracer: tracer.llm_exchange(span_id, request, reply, error),
            )
        self._span.__exit__(exc_type, exc, tb)


def _repair(error: BaseException) -> str:
    """一次 tracer 失败的下一步。调用本身就失败的（参数个数、名字对不上）多半是照着另一个签名
    写的。"""
    if isinstance(error, TypeError) and 'argument' in str(error):
        return (
            '它的方法签名与 Tracer 协议对不上：照协议逐字写参数（继承 NullTracer、只覆写要的方法）'
        )
    return '修好这个 tracer（它的方法必须同步返回、不抛异常、不是 async def 或生成器）'


def _failure_event(failure: object) -> dict[str, AttributeValue] | None:
    """轮换记录里的一条平台故障 → 事件属性；形状不对的不记。"""
    if not isinstance(failure, Mapping):
        return None
    record = cast(Mapping[str, object], failure)
    platform, error, status = record.get('platform'), record.get('error'), record.get('status')
    if not (isinstance(platform, str) and platform and isinstance(error, str) and error):
        return None
    event: dict[str, AttributeValue] = {'llm.platform': platform, 'llm.error': error}
    event['llm.status'] = cast(int, status) if _count(status) else None
    return event


def llm_attempt(llm: object, request: Mapping[str, Any] | None = None) -> _Attempt:
    """包住一次 `llm.create`。不在 Agent 的模型调用里（没有 `llm_call` 作用域）时什么都不做。模型名
    取 `llm.config.model`（`LLM` 协议给的是 config）。`request` 是这次交给 `create` 的消息与参数
    （`{'messages': ..., **kwargs}`），随结局一起交给 tracer。"""
    return _Attempt(_LLM_CALL.get(), llm, {} if request is None else request)
