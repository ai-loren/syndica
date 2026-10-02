"""捕获即回放（M3 T14）：recorder 在记录的同时攒下回放文档要的东西，`finish` 时写成 `replay.json`。

回放文档的大部分来自插桩本身：模型的回复与逐单元指纹、外部工具的结局与执行参数的指纹、跳过、
闸门判定、每一轮开始前注入的消息、哪些周期是紧接着上一个开的（Agent 没有停下来等）、哪些调用开了头
却没有返回（被打断）、人的答复、被宿主中止的请求、run 被打断时还在等外部世界的那些调用、各 Agent
停下的原因。
另一部分只有宿主知道——宿主档案、冻结后的角色、入口——由宿主在 run 开始之前交给
`TraceRecorder.describe_run`。

id 由这里的 id 源交出（`TraceRecorder.ids`，宿主交给 `Runtime(ids=...)`），每个稳定键交出了什么
都记下，回放按键交回同一个。一个 Agent 的 id 不是 id 源交出的（它的工厂交回了别处构造的 Agent，
或在它之前先构造了别的 Agent），回放就造不出它：捕获时指出，不写回放文档。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import cast

from syndica.kernel.context import ContextBlock, ToolCall
from syndica.kernel.ids import RandomIds, StableKey, current_tool_call
from syndica.kernel.message import Message
from syndica.kernel.redaction import Redactor, encode_redacted
from syndica.kernel.scenario_schema import FROZEN_IMPLS, REPLAYED_TOOLS
from syndica.kernel.tool import ToolResult
from syndica.kernel.trace import AttributeValue, SpanKind, SpanStatus, TraceSource

__all__ = ['RecordingIds', 'ReplayCapture', 'Unreplayable']


class RecordingIds:
    """交给 `Runtime(ids=...)` 的 id 源：交出 uuid4 的十六进制（与默认的源一样），并记下每个稳定键
    交出了什么。"""

    def __init__(self) -> None:
        self.issued: dict[StableKey, str] = {}
        self.reused: list[StableKey] = []
        self._random = RandomIds()

    def agent_id(self, key: StableKey) -> str:
        return self._issue(key, self._random.agent_id(key))

    def message_id(self, key: StableKey) -> str:
        return self._issue(key, self._random.message_id(key))

    def _issue(self, key: StableKey, value: str) -> str:
        stable = tuple(key)
        if stable in self.issued:
            # 一个键交出两个 id，回放只交得回其中一个：记下，写文档时拒绝。
            self.reused.append(stable)
        else:
            self.issued[stable] = value
        return value


class Unreplayable(Exception):
    """这次 run 写不出一份回放得了的文档。三件套写给宿主：期望什么、实际怎样、下一步做什么。"""

    def __init__(self, *, expected: str, received: str, next_action: str) -> None:
        super().__init__(received)
        self.expected = expected
        self.received = received
        self.next_action = next_action


@dataclass
class _Instance:
    role: str
    replies: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    tool_results: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    skipped: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    gate: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    messages: dict[int, list[str]] = field(default_factory=dict[int, list[str]])
    continued: set[int] = field(default_factory=set[int])
    cut: list[list[int]] = field(default_factory=list[list[int]])
    stop_reason: str | None = None


@dataclass
class _Call:
    agent_id: str
    position: tuple[int, int, int]
    tool: str
    returned: dict[str, object] | None = None


@dataclass
class _Wait:
    agent_id: str
    key: StableKey | None
    kind: str
    answer: dict[str, object] | None = None


class ReplayCapture:
    """回放文档的账本。recorder 在插桩到达时调这里的方法；值在到达的那一刻取走、转成 JSON 值（内核
    之后还会改那些对象）。脱敏在合成文档时逐项做（见 `document`）。"""

    def __init__(self, jsonable: Callable[[object], object], redactor: Redactor) -> None:
        self.ids = RecordingIds()
        self._jsonable = jsonable
        self._redactor = redactor
        self._run: dict[str, object] | None = None
        self._instances: dict[str, _Instance] = {}
        self._llm: dict[str, tuple[str, int | None, int]] = {}
        self._exchanged: set[str] = set()
        self._calls: dict[str, _Call] = {}
        # turn span → (Agent、第几轮、它开了一个工作周期时的周期号)。
        self._turns: dict[str, tuple[str, int, int | None]] = {}
        self._cycles_seen: set[tuple[str, int]] = set()
        # 插桩到达的先后：消息发出、工作周期结束各占一个序号。
        self._order = 0
        self._sent: dict[str, int] = {}
        self._cycle_ended: dict[str, int] = {}
        self._confirmations: dict[str, str] = {}
        self._waits: dict[str, _Wait] = {}
        self._responses: list[dict[str, object]] = []
        self._aborts: list[StableKey] = []
        self._interrupt: list[StableKey] | None = None
        self._digests: set[str] = set()
        self._gaps: list[str] = []

    @property
    def wanted(self) -> bool:
        return self._run is not None

    def describe(
        self,
        host: Mapping[str, object],
        roles: Mapping[str, Mapping[str, object]],
        entry: Mapping[str, object],
    ) -> None:
        self._run = {
            'host': self._jsonable(dict(host)),
            'roles': {str(name): self._jsonable(dict(role)) for name, role in roles.items()},
            'entry': self._jsonable(dict(entry)),
        }

    def interrupted(self) -> None:
        """宿主要因为人打断而拆掉 run：记下此刻还在等外部世界的调用——在飞的模型调用、开着的外部
        工具调用（在执行，或已经交回、结果还没落进轨迹）、还挂着的人的请求。底下开着一个确认的工具
        调用不算：它在等人，在飞的是那个请求。只记第一次（连按几次 Ctrl-C 不改位置）。"""
        if self._interrupt is not None:
            return
        asking = set(self._confirmations.values())
        waiting: list[StableKey] = []
        for span_id, (agent_id, turn, attempt) in self._llm.items():
            if span_id not in self._exchanged and turn is not None:
                waiting.append(('llm_call', agent_id, turn, attempt))
        for span_id, call in self._calls.items():
            if call.tool in REPLAYED_TOOLS and span_id not in asking:
                waiting.append(('tool_call', call.agent_id, *call.position))
        waiting.extend(wait.key for wait in self._waits.values() if wait.key is not None)
        self._interrupt = waiting

    # --- instrumentation -------------------------------------------------------------------

    def started(
        self,
        span_id: str,
        kind: SpanKind,
        agent_id: str,
        attributes: Mapping[str, AttributeValue],
        parent: tuple[SpanKind, Mapping[str, AttributeValue], str] | None,
    ) -> None:
        if kind is SpanKind.AGENT:
            role = attributes.get('agent.role')
            if agent_id not in self._instances:
                self._instances[agent_id] = _Instance(role if isinstance(role, str) else '')
        elif kind is SpanKind.TURN:
            turn = attributes.get('turn')
            if isinstance(turn, int) and not isinstance(turn, bool):
                cycle = None if parent is None else parent[1].get('agent.cycle')
                opens = (
                    parent is not None
                    and parent[0] is SpanKind.AGENT
                    and isinstance(cycle, int)
                    and not isinstance(cycle, bool)
                    and (agent_id, cycle) not in self._cycles_seen
                )
                if opens:
                    self._cycles_seen.add((agent_id, cast(int, cycle)))
                self._turns[span_id] = (agent_id, turn, cast(int, cycle) if opens else None)
        elif kind is SpanKind.LLM_CALL:
            turn = None
            if parent is not None and parent[0] is SpanKind.TURN:
                found = parent[1].get('turn')
                turn = found if isinstance(found, int) and not isinstance(found, bool) else None
            attempt = attributes.get('llm.attempt')
            self._llm[span_id] = (agent_id, turn, attempt if isinstance(attempt, int) else 0)
        elif kind is SpanKind.TOOL_CALL:
            call = current_tool_call()
            name = attributes.get('tool.name')
            if call is not None and call.agent_id == agent_id and isinstance(name, str):
                self._calls[span_id] = _Call(agent_id, (call.cycle, call.turn, call.index), name)
        elif kind is SpanKind.CONFIRMATION and parent is not None:
            self._confirmations[span_id] = parent[2]
        elif kind is SpanKind.WAIT and attributes.get('wait.kind') == 'human':
            self._waits[span_id] = _Wait(
                agent_id, _key_of(attributes.get('wait.target')), _kind(parent)
            )

    def exchanged(
        self,
        span_id: str,
        units: list[str] | None,
        reply: ContextBlock | None,
        error: BaseException | None,
    ) -> None:
        located = self._llm.get(span_id)
        if located is None:
            return
        self._exchanged.add(span_id)
        agent_id, turn, attempt = located
        instance = self._instances.get(agent_id)
        if instance is None:
            return
        if turn is None:
            self._gaps.append(f'agent {agent_id} 的一次模型调用不在任何一轮里')
            return
        entry: dict[str, object] = {'turn': turn, 'attempt': attempt}
        if units is not None:
            entry['request_digests'] = list(units)
            self._digests.update(units)
        if error is not None:
            entry['raises'] = self._raised(error)
        elif reply is not None:
            entry.update(
                {
                    'content': self._jsonable(reply.content),
                    'tool_calls': [_call_value(call, self._jsonable) for call in reply.tool_calls],
                    'extra': self._jsonable(dict(reply.extra)),
                    'trust': reply.trust.value,
                }
            )
        else:
            return
        instance.replies.append(entry)

    def returned(
        self,
        span_id: str,
        result: ToolResult | None,
        error: BaseException | None,
        digest: str | None,
    ) -> None:
        call = self._calls.get(span_id)
        if call is None:
            return
        value: dict[str, object] = (
            {'raised': self._raised(error)} if error is not None else self._result(result)
        )
        if digest is not None:
            value['arguments_digest'] = digest
            self._digests.add(digest)
        call.returned = value

    def sent(self, message_id: str) -> None:
        """一条消息投递成功了（发送方 span 上的 `message.sent`）。"""
        self._order += 1
        self._sent[message_id] = self._order

    def consumed(self, target_span_id: str, message_id: str) -> None:
        """一条消息被消费了。消费它的是一轮（门在这一轮开始前注入它）时记下：回放的门在同一轮取走
        同一批，消息早到晚到都一样。被一次等待取走的不记：等待的一方本来就停在那里等它。

        这一轮开了一个新的工作周期（第二个起），且这条消息在上一个周期结束之前就发出了：上一个周期
        结束时它已经在邮箱里，Agent 没有停下来等，紧接着开了这个周期——记下这一轮。否则 Agent 先停了
        下来（进了 zombie 或停在邮箱上），是后来的消息把它叫醒的。两者别人看得见：派活的一方要等子
        Agent 停下才拿到它的结果。"""
        located = self._turns.get(target_span_id)
        if located is None:
            return
        agent_id, turn, cycle = located
        instance = self._instances.get(agent_id)
        if instance is None:
            return
        instance.messages.setdefault(turn, []).append(message_id)
        ended = self._cycle_ended.get(agent_id)
        sent = self._sent.get(message_id)
        if cycle is not None and cycle >= 2 and ended is not None and sent is not None:
            if sent < ended:
                instance.continued.add(turn)

    def answered(self, span_id: str, message: Message) -> None:
        wait = self._waits.get(span_id)
        if wait is not None:
            wait.answer = {
                'answer': self._jsonable(message.content),
                'trust': message.trust.value,
            }

    def ended(
        self,
        span_id: str,
        kind: SpanKind,
        agent_id: str,
        status: SpanStatus,
        attributes: Mapping[str, AttributeValue],
    ) -> None:
        if kind is SpanKind.AGENT:
            self._order += 1
            self._cycle_ended[agent_id] = self._order
            reason = attributes.get('stop_reason')
            instance = self._instances.get(agent_id)
            if instance is not None:
                # 最后一个工作周期的原因：被取消的周期没有原因，前一个周期的那个已经不是它的结局。
                instance.stop_reason = reason if isinstance(reason, str) else None
        elif kind is SpanKind.TURN:
            self._turns.pop(span_id, None)
        elif kind is SpanKind.LLM_CALL:
            self._llm.pop(span_id, None)
            self._exchanged.discard(span_id)
        elif kind is SpanKind.TOOL_CALL:
            call = self._calls.pop(span_id, None)
            if call is not None:
                self._tool_ended(call, status, attributes)
        elif kind is SpanKind.CONFIRMATION:
            self._confirmations.pop(span_id, None)
        elif kind is SpanKind.WAIT:
            wait = self._waits.pop(span_id, None)
            if wait is not None:
                self._wait_ended(wait, attributes.get('wait.outcome'))

    def _tool_ended(
        self, call: _Call, status: SpanStatus, attributes: Mapping[str, AttributeValue]
    ) -> None:
        instance = self._instances.get(call.agent_id)
        if instance is None:
            return
        position = list(call.position)
        outcome = attributes.get('tool.outcome')
        recorded = call.tool in REPLAYED_TOOLS and call.returned is not None
        if status is SpanStatus.CANCELLED and not isinstance(outcome, str) and not recorded:
            # 开了头、被取消在半路上，结果没有落进轨迹。回放里真跑的内核工具（派活、发消息）在这里
            # 不能交回，否则 Agent 就走到了原始 run 没走到的地方；记录了结局的外部工具由宿主动作的
            # 等待名单停在原处。
            instance.cut.append(position)
        disposition = attributes.get('danger.disposition')
        if disposition is not None:
            gate: dict[str, object] = {
                'call': position,
                'rule_ids': list(cast(list[str], attributes.get('danger.rule_ids', []))),
                'risk': attributes.get('danger.risk'),
                'provenance': attributes.get('danger.provenance'),
                'decided_by': attributes.get('danger.decided_by'),
                'disposition': disposition,
            }
            if isinstance(outcome, str):
                gate['outcome'] = outcome
            instance.gate.append(gate)
        returned = call.returned
        if outcome == 'skipped' and returned is not None:
            instance.skipped.append({'call': position, **returned})
        elif call.tool in REPLAYED_TOOLS and returned is not None:
            instance.tool_results.append({'call': position, **returned})

    def _wait_ended(self, wait: _Wait, outcome: object) -> None:
        if wait.key is None:
            self._gaps.append(f'agent {wait.agent_id} 有一次人的等待没有请求键（宿主直接发起的）')
            return
        if outcome == 'answered' and wait.answer is not None:
            self._responses.append({'key': list(wait.key), 'kind': wait.kind, **wait.answer})
        elif outcome == 'timeout':
            self._responses.append({'key': list(wait.key), 'kind': 'timeout'})
        elif outcome == 'aborted':
            self._aborts.append(wait.key)

    # --- the document -----------------------------------------------------------------------

    def document(
        self, trace_id: str, source: TraceSource, status: SpanStatus
    ) -> tuple[dict[str, object], set[str]]:
        """回放文档与其中的内核标识。每一项在它自己的上下文里逐叶脱敏（与请求指纹的上下文相同）；
        写的时候交给 `encode_prepared`，它确认每个字符串都已经脱过敏，不再按外层的键给下面定性。
        写不出时抛 `Unreplayable`。"""
        run = self._run
        assert run is not None
        roles = cast(dict[str, dict[str, object]], run['roles'])
        self._refuse_unreplayable(roles, run)
        identifiers = {trace_id, *self.ids.issued.values(), *self._digests}

        def clean(value: object) -> object:
            return json.loads(encode_redacted(value, self._redactor, identifiers=identifiers))

        names: dict[str, str] = {}
        for raw in roles:
            name = str(self._redactor.redact(raw))
            if name in names:
                raise Unreplayable(
                    expected='角色名脱敏之后仍然各不相同',
                    received='有两个角色名被脱敏规则换成了同一个',
                    next_action='收窄认得角色名的那条用户规则；这个 trace 不写回放文档',
                )
            names[name] = raw
        keys = {value: key for key, value in self.ids.issued.items()}
        instances: dict[str, list[object]] = {raw: [] for raw in roles}
        stop_reasons: dict[str, str] = {}
        cleaned = {raw: name for name, raw in names.items()}
        for agent_id, instance in self._instances.items():
            key = keys.get(agent_id)
            if key is None:
                raise Unreplayable(
                    expected=(
                        '每个 Agent 的 id 是 Runtime 按它的稳定键向 id 源要的那一个（spawn 在调工厂'
                        '之前取好，交给构造的第一个 Agent）'
                    ),
                    received=(
                        f'角色 {instance.role!r} 的 Agent {agent_id} 不是 id 源交出的：'
                        '它的工厂交回了别处构造的 Agent，或在它之前先构造了别的 Agent'
                    ),
                    next_action=(
                        f'让角色 {instance.role!r} 的工厂每次都新建、且第一个构造的就是要交回的'
                        '那个 Agent；宿主要把 TraceRecorder.ids 交给 Runtime(ids=...)。'
                        '这个 trace 不写回放文档'
                    ),
                )
            listed = instances.get(instance.role)
            if listed is None:
                raise Unreplayable(
                    expected='run 里的每个角色都在 describe_run 交来的 roles 里',
                    received=f'角色 {instance.role!r} 不在其中（有 {sorted(roles)!r}）',
                    next_action=(
                        '宿主把 run 用得到的每个角色都交给 describe_run；这个 trace 不写回放文档'
                    ),
                )
            if instance.stop_reason is not None:
                stop_reasons[f'{cleaned[instance.role]}[{len(listed)}]'] = instance.stop_reason
            listed.append(
                clean(
                    {
                        'key': list(key),
                        'turns': _sharing_prefixes(
                            sorted(
                                instance.replies,
                                key=lambda reply: (
                                    cast(int, reply['turn']),
                                    cast(int, reply['attempt']),
                                ),
                            )
                        ),
                        'tool_results': instance.tool_results,
                        'skipped': instance.skipped,
                        'gate': instance.gate,
                        'messages': [
                            {'turn': turn, 'ids': ids}
                            for turn, ids in sorted(instance.messages.items())
                        ],
                        **({'continued': sorted(instance.continued)} if instance.continued else {}),
                        **({'cut': instance.cut} if instance.cut else {}),
                    }
                )
            )
        host_actions: list[dict[str, object]] = [
            {'kind': 'abort', 'key': list(key)} for key in dict.fromkeys(self._aborts)
        ]
        if self._interrupt is not None or status is SpanStatus.CANCELLED:
            action: dict[str, object] = {'kind': 'interrupt'}
            waiting = sorted(
                {json.dumps(list(key)): list(key) for key in self._interrupt or ()}.items()
            )
            if waiting:
                # 打断时没有任何调用在等外部世界（落在两次调用之间，或宿主没说打断在哪）就不写
                # 位置：回放在记录用完、安静下来的那一处发出它。
                action['waiting'] = [key for _, key in waiting]
            host_actions.append(action)
        document: dict[str, object] = {
            'version': 2,
            'name': clean(f'trace {trace_id}'),
            'source': clean(
                {
                    'trace_id': trace_id,
                    'argv': list(source.argv),
                    'version': source.version,
                    'config_sha256': source.config_sha256,
                    'git_commit': source.git_commit,
                }
            ),
            'host': clean(run['host']),
            'agents': {
                name: {'frozen': clean(roles[raw]), 'instances': instances[raw]}
                for name, raw in names.items()
            },
            'entry': clean(run['entry']),
            'responses': [
                clean(response)
                for response in sorted(self._responses, key=lambda item: json.dumps(item['key']))
            ],
            'host_actions': clean(host_actions),
            # 按键排：几个 Agent 并发时交出 id 的先后取决于调度，同一个 run 的两次捕获应当逐字节
            # 相同。
            'ids': clean(
                [
                    {'key': list(key), 'id': value}
                    for key, value in sorted(
                        self.ids.issued.items(), key=lambda item: json.dumps(list(item[0]))
                    )
                ]
            ),
            'expect': {'stop_reasons': stop_reasons},
        }
        return document, identifiers

    def _refuse_unreplayable(
        self, roles: Mapping[str, Mapping[str, object]], run: Mapping[str, object]
    ) -> None:
        """回放造不出来的 run：写不出一份回放得了的文档，抛 `Unreplayable` 说清是哪一类。"""
        if self.ids.reused:
            raise Unreplayable(
                expected='每个稳定键只向 id 源要一次 id',
                received=f'稳定键 {list(self.ids.reused[0])!r} 交出过两个 id',
                next_action=(
                    '一个 TraceRecorder 的 ids 只交给一个 Runtime；这个 trace 不写回放文档'
                ),
            )
        if self._gaps:
            raise Unreplayable(
                expected='回放要的每一项都有它的稳定键',
                received=self._gaps[0],
                next_action='这类调用回放不出来；这个 trace 不写回放文档，结构照常记录',
            )
        for name, role in roles.items():
            impl = role.get('impl')
            if impl is not None and impl not in FROZEN_IMPLS:
                raise Unreplayable(
                    expected='类式角色是内核内置的那几个（回放只重建它们）',
                    received=f'角色 {name!r} 是类式角色 {impl!r}',
                    next_action='这类角色回放不出来；这个 trace 不写回放文档，结构照常记录',
                )
            tools = role.get('tools')
            for index, tool in enumerate(
                cast(list[object], tools if isinstance(tools, list) else [])
            ):
                if isinstance(tool, str) and str(self._redactor.redact(tool)) != tool:
                    raise Unreplayable(
                        expected='脱敏规则不认得工具名（回放按名字找工具）',
                        received=f'角色 {name!r} 的工具表第 {index} 项被一条脱敏规则换掉了',
                        next_action='收窄那条用户规则，别让它认得工具名；这个 trace 不写回放文档',
                    )
        entry = cast(Mapping[str, object], run['entry'])
        allowed = 1 if entry.get('mode') in ('send', 'slot') else 0
        sent = [key for key in self.ids.issued if key[:2] == ('host', 'message')]
        if len(sent) > allowed:
            raise Unreplayable(
                expected='run 开始之后宿主不再自己给 Agent 发消息（回放只重放入口那一条）',
                received=f'宿主在这次 run 里发了 {len(sent)} 条消息',
                next_action='宿主的这类动作回放不出来；这个 trace 不写回放文档，结构照常记录',
            )
        for agent_id, instance in self._instances.items():
            if instance.stop_reason == 'aborted' and not any(
                agent_id in key for key in self._aborts
            ):
                raise Unreplayable(
                    expected='被中止的 Agent 是在某个人的请求处被中止的（回放在那个请求处重放它）',
                    received=f'角色 {instance.role!r} 的 Agent {agent_id} 被宿主在请求之外中止了',
                    next_action='宿主的这类动作回放不出来；这个 trace 不写回放文档，结构照常记录',
                )

    # --- values -----------------------------------------------------------------------------

    def _raised(self, error: BaseException) -> dict[str, object]:
        value: dict[str, object] = {
            'type': f'{type(error).__module__}.{type(error).__qualname__}',
            'message': self._jsonable(_message(error)),
        }
        for name in ('status_code', 'retry_after'):
            found = getattr(error, name, None)
            if isinstance(found, int | float) and not isinstance(found, bool):
                value[name] = found
        return value

    def _result(self, result: ToolResult | None) -> dict[str, object]:
        if result is None:
            return {'output': None}
        return {
            'output': self._jsonable(result.output),
            'meta': self._jsonable(dict(result.meta)),
            'trust': result.trust.value,
            'artifacts': self._jsonable(list(result.artifacts)),
        }


def _sharing_prefixes(replies: list[dict[str, object]]) -> list[dict[str, object]]:
    """逐单元指纹写成「这个实例里上一次带指纹的请求的前 n 个」（`request_prefix`）加上其余的：请求
    只在末尾追加（前缀缓存的约束），每次照抄整份是轮数的平方。"""
    previous: list[str] = []
    written: list[dict[str, object]] = []
    for reply in replies:
        found = reply.get('request_digests')
        if not isinstance(found, list):
            written.append(reply)
            continue
        units = cast(list[str], found)
        shared = 0
        for mine, theirs in zip(units, previous, strict=False):
            if mine != theirs:
                break
            shared += 1
        entry = {**reply, 'request_digests': units[shared:]}
        if shared:
            entry['request_prefix'] = shared
        written.append(entry)
        previous = units
    return written


def _call_value(call: ToolCall, jsonable: Callable[[object], object]) -> dict[str, object]:
    try:
        parsed = json.loads(call.arguments)
    except ValueError:
        return {'id': call.id, 'name': call.name, 'arguments_raw': call.arguments}
    return {'id': call.id, 'name': call.name, 'arguments': jsonable(parsed)}


def _key_of(target: object) -> StableKey | None:
    if not isinstance(target, str) or not target:
        return None
    try:
        parsed = json.loads(target)
    except ValueError:
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    return tuple(cast(list[str | int], parsed))


def _kind(parent: tuple[SpanKind, Mapping[str, AttributeValue], str] | None) -> str:
    """人的等待答的是哪种请求：确认在 confirmation span 里，`AskUserQuestion` 与 `AskUser` 在各自的
    工具调用里。"""
    if parent is not None and parent[0] is SpanKind.CONFIRMATION:
        return 'confirmation'
    if parent is not None and parent[1].get('tool.name') == 'AskUserQuestion':
        return 'user_question'
    return 'user_input'


def _message(error: BaseException) -> str:
    try:
        return str(error)
    except Exception:
        return f'<{type(error).__module__}.{type(error).__qualname__}>'
