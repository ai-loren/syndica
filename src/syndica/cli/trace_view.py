"""`syndica trace list` 与 `trace show` 的文字（M3 T15）。只读 trace 模型，不碰 store、不碰终端。

trace 里的字符串（命令行参数、角色名、工具名、Agent id、等待的目标）有一部分来自模型与文件系统，
trace 目录又是 Agent 的 Bash 改得到的：每一个进输出之前都经 `one_line()`（控制字符与换行都转义，
一个带换行的参数伪造不出一行）。时间一律按 UTC 写，时长按 span 自己的起止算。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from syndica.cli.terminal import one_line
from syndica.kernel import CausalityEdge, EdgeKind, Span, SpanKind, TraceHeader

__all__ = ['TraceRow', 'render_list', 'render_show']

_SLOWEST = 3


@dataclass(frozen=True)
class TraceRow:
    """`trace list` 的一行：头，加上 Agent 数、span 数与大小（字节；store 报不出时是 None）。
    span 读不进来（文件被改坏）时 Agent 数与 span 数都是 None。"""

    header: TraceHeader
    agents: int | None
    spans: int | None
    size: int | None


@dataclass
class _Agent:
    agent_id: str
    role: str
    start_ns: int
    end_ns: int
    cycles: int = 0
    stop_reason: str | None = None
    children: list[str] = field(default_factory=list[str])


def render_list(rows: Sequence[TraceRow]) -> str:
    if not rows:
        return '还没有 trace（.syndica/traces 是空的）：run 与 chat 默认都会记录。'
    lines: list[str] = []
    for row in sorted(rows, key=lambda item: item.header.started_ns, reverse=True):
        header = row.header
        status = '未完成' if header.status is None else header.status.value
        size = '大小不明' if row.size is None else _size(row.size)
        command = one_line(' '.join(header.source.argv)) or '（没有记下命令）'
        replay = (
            '' if header.source.replay_of is None else f'  回放自 {header.source.replay_of[:8]}'
        )
        counts = (
            'span 读不进来'
            if row.agents is None or row.spans is None
            else f'{row.agents} 个 Agent  {row.spans} 个 span'
        )
        lines.append(
            f'{_utc(header.started_ns)}  {header.trace_id[:8]}  {status}  '
            f'{counts}  {size}  {command}{replay}'
        )
    return '\n'.join(lines)


def render_show(header: TraceHeader, spans: Sequence[Span], edges: Sequence[CausalityEdge]) -> str:
    agents = _agents(spans, edges)
    labels = {agent_id: _label(agent) for agent_id, agent in agents.items()}
    origin = header.started_ns
    sections = [
        *_summary(header),
        '',
        '谁创建了谁：',
        *_tree(agents, labels, origin),
        '',
        '谁在等谁：',
        *_waits(spans, agents, labels),
        '',
        f'最慢的工具调用（前 {_SLOWEST} 个）：',
        *_slowest(spans, labels),
        '',
        '时间线：',
        *_timeline(agents, labels, origin),
    ]
    return '\n'.join(sections)


def _summary(header: TraceHeader) -> list[str]:
    status = '未完成（记录到一半就断了）' if header.status is None else header.status.value
    lines = [
        f'trace {header.trace_id}  {status}  记录级别 {header.level.value}',
        f'命令：{one_line(" ".join(header.source.argv)) or "（没有记下）"}',
        f'开始：{_utc(header.started_ns)}'
        + (
            ''
            if header.ended_ns is None
            else f'  用时 {_duration(header.ended_ns - header.started_ns)}'
        ),
    ]
    if header.source.replay_of is not None:
        lines.append(f'回放自 trace {header.source.replay_of}')
    return lines


def _agents(spans: Sequence[Span], edges: Sequence[CausalityEdge]) -> dict[str, _Agent]:
    agents: dict[str, _Agent] = {}
    for span in sorted(spans, key=lambda item: item.start_ns):
        if span.kind is not SpanKind.AGENT:
            continue
        role = span.attributes.get('agent.role')
        agent = agents.get(span.agent_id)
        if agent is None:
            agent = agents[span.agent_id] = _Agent(
                span.agent_id, role if isinstance(role, str) else '?', span.start_ns, span.end_ns
            )
        agent.cycles += 1
        agent.end_ns = max(agent.end_ns, span.end_ns)
        # 最后一个工作周期的原因：被取消的周期没有原因，沿用前一个会把被切断的 Agent 报成做完了。
        reason = span.attributes.get('stop_reason')
        agent.stop_reason = reason if isinstance(reason, str) else None
    by_span = {span.span_id: span for span in spans}
    for edge in edges:
        child = edge.attributes.get('child.agent_id')
        source = by_span.get(edge.source_span_id)
        if edge.kind is not EdgeKind.SPAWN or not isinstance(child, str) or source is None:
            continue
        parent = agents.get(source.agent_id)
        if parent is not None and child != source.agent_id and child not in parent.children:
            parent.children.append(child)
    return agents


def _tree(agents: Mapping[str, _Agent], labels: Mapping[str, str], origin: int) -> list[str]:
    if not agents:
        return ['  （这个 trace 里没有 Agent）']
    spawned = {child for agent in agents.values() for child in agent.children}
    roots = sorted(
        (agent for agent_id, agent in agents.items() if agent_id not in spawned),
        key=lambda agent: agent.start_ns,
    )
    lines: list[str] = []
    seen: set[str] = set()

    # 用显式的栈走：派活的深度没有上限，递归会撞上解释器的栈。
    def visit(start: str) -> None:
        stack = [(start, 0)]
        while stack:
            agent_id, depth = stack.pop()
            agent = agents.get(agent_id)
            prefix = '  ' + '   ' * depth + ('└─ ' if depth else '')
            if agent is None:
                lines.append(f'{prefix}Agent {_short(agent_id)}（派了出去，没有跑起来）')
                continue
            if agent_id in seen:
                continue
            seen.add(agent_id)
            created = '' if depth == 0 else f'，创建于 +{_duration(agent.start_ns - origin)}'
            lines.append(f'{prefix}{labels[agent_id]}{created}')
            # 按子 Agent 真正跑起来的先后排：派活的先后与 id 的字典序都不是它。没跑起来的排在
            # 派它的 Agent 结束的位置。
            parent_end = agent.end_ns
            children = sorted(
                agent.children,
                key=lambda child: agents[child].start_ns if child in agents else parent_end,
            )
            stack.extend((child, depth + 1) for child in reversed(children))

    for root in roots:
        visit(root.agent_id)
    for agent_id in sorted(agents, key=lambda item: agents[item].start_ns):
        if agent_id not in seen:
            visit(agent_id)
    return lines


@dataclass(frozen=True)
class _Wait:
    agent_id: str
    kind: str
    target: str
    outcome: str
    start_ns: int
    end_ns: int


def _waits(
    spans: Sequence[Span], agents: Mapping[str, _Agent], labels: Mapping[str, str]
) -> list[str]:
    waits = sorted(
        (
            _Wait(
                span.agent_id,
                str(span.attributes.get('wait.kind', '')),
                str(span.attributes.get('wait.target', '')),
                str(span.attributes.get('wait.outcome', span.status.value)),
                span.start_ns,
                span.end_ns,
            )
            for span in spans
            if span.kind is SpanKind.WAIT
        ),
        key=lambda wait: wait.start_ns,
    )
    if not waits:
        return ['  （没有人在等）']
    by_agent: dict[str, list[_Wait]] = {}
    for wait in waits:
        by_agent.setdefault(wait.agent_id, []).append(wait)
    spawned = {child for agent in agents.values() for child in agent.children}

    def chain(wait: _Wait) -> list[_Wait]:
        """一个 Agent 等另一个 Agent 时，被等的那个此刻在等什么：一路接下去就是等待链。接回链上
        已有的 Agent 就是循环等待，到那里为止。"""
        links = [wait]
        seen: set[str] = set()
        current = wait
        while current.kind == 'agent' and current.target in agents and current.target not in seen:
            inner = next(
                (
                    candidate
                    for candidate in by_agent.get(current.target, [])
                    if candidate.start_ns < current.end_ns and candidate.end_ns > current.start_ns
                ),
                None,
            )
            if inner is None:
                break
            seen.add(current.agent_id)
            links.append(inner)
            current = inner
        return links

    chains = [chain(wait) for wait in waits]
    inside = {id(link) for links in chains for link in links[1:]}
    shown: set[int] = set()
    lines: list[str] = []

    def show(links: list[_Wait]) -> None:
        shown.update(id(link) for link in links)
        last = links[-1]
        steps = [labels.get(links[0].agent_id, f'Agent {_short(links[0].agent_id)}')]
        steps.extend(_target(link, labels, spawned) for link in links)
        cycle = last.kind == 'agent' and last.target in {link.agent_id for link in links}
        lines.append(
            f'  {" → ".join(steps)}（{_duration(links[0].end_ns - links[0].start_ns)}，'
            f'{one_line(last.outcome)}{"，循环等待" if cycle else ""}）'
        )

    for links in chains:
        if id(links[0]) not in inside:
            show(links)
    # 循环里每条链的开头都在别的链里：上面一条也不打。从最早开始的那一个打一次。
    for links in chains:
        if id(links[0]) not in shown:
            show(links)
    return lines


def _target(wait: _Wait, labels: Mapping[str, str], spawned: set[str]) -> str:
    if wait.kind == 'human':
        return '人'
    if wait.target in labels:
        return labels[wait.target]
    if wait.target in spawned:
        return f'Agent {_short(wait.target)}（派了出去，没有跑起来）'
    return f'一条答复（关联 id {one_line(wait.target[:16])}）'


def _slowest(spans: Sequence[Span], labels: Mapping[str, str]) -> list[str]:
    calls = sorted(
        (span for span in spans if span.kind is SpanKind.TOOL_CALL),
        key=lambda span: (-(span.end_ns - span.start_ns), span.start_ns),
    )[:_SLOWEST]
    if not calls:
        return ['  （没有工具调用）']
    lines: list[str] = []
    for number, span in enumerate(calls, start=1):
        name = span.attributes.get('tool.name', span.name)
        outcome = span.attributes.get('tool.outcome', span.status.value)
        lines.append(
            f'  {number}. {_duration(span.end_ns - span.start_ns)}  '
            f'{labels.get(span.agent_id, f"Agent {_short(span.agent_id)}")}  '
            f'{one_line(str(name))}  {one_line(str(outcome))}'
        )
    return lines


def _timeline(agents: Mapping[str, _Agent], labels: Mapping[str, str], origin: int) -> list[str]:
    if not agents:
        return ['  （没有 Agent）']
    lines: list[str] = []
    for agent in sorted(agents.values(), key=lambda item: item.start_ns):
        ended = '没有说明停下的原因' if agent.stop_reason is None else one_line(agent.stop_reason)
        lines.append(
            f'  +{_duration(agent.start_ns - origin)} → +{_duration(agent.end_ns - origin)}  '
            f'{labels[agent.agent_id]}  {agent.cycles} 个工作周期  {ended}'
        )
    return lines


def _label(agent: _Agent) -> str:
    return f'{one_line(agent.role)} {_short(agent.agent_id)}'


def _short(agent_id: str) -> str:
    """Agent id 的前 8 位。span 文件改得到，id 只校验了是字符串：照样转义。"""
    return one_line(agent_id[:8])


def _utc(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).strftime('%Y-%m-%d %H:%M:%SZ')


def _duration(ns: int) -> str:
    seconds = max(ns, 0) / 1e9
    if seconds < 1:
        return f'{seconds * 1000:.0f} ms'
    return f'{seconds:.1f} s'


def _size(size: int) -> str:
    if size < 1024:
        return f'{size} B'
    if size < 1024 * 1024:
        return f'{size / 1024:.1f} KB'
    if size < 1024 * 1024 * 1024:
        return f'{size / (1024 * 1024):.1f} MB'
    return f'{size / (1024 * 1024 * 1024):.1f} GB'
