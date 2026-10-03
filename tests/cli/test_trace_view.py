"""`trace list` / `trace show` 的文字（M3 T15）：三个回答（谁创建了谁、谁在等谁、最慢的工具调用）
与列表；来自模型的字符串经 `terminal_safe()`。"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Iterator

import pytest

from syndica.cli.trace_view import TraceRow, render_list, render_show
from syndica.kernel import (
    CausalityEdge,
    EdgeKind,
    RuleRedactor,
    Span,
    SpanEvent,
    SpanKind,
    SpanStatus,
    TraceHeader,
    TraceLevel,
    TraceRedaction,
    TraceSource,
)
from syndica.kernel.redaction import RULES_VERSION
from syndica.kernel.tracing import span_id_of

_S = 1_000_000_000
_ORIGIN = 1_790_000_000 * _S
_LEAD = '1a' * 16
_EARLY = '2b' * 16
_LATE = '3c' * 16
_GRANDCHILD = '4d' * 16


def _header(
    trace_id: str = 'ab' * 16,
    *,
    started: int = _ORIGIN,
    status: SpanStatus | None = SpanStatus.OK,
    argv: tuple[str, ...] = ('run', 'go'),
    replay_of: str | None = None,
) -> TraceHeader:
    redactor = RuleRedactor()
    return TraceHeader(
        trace_id=trace_id,
        started_ns=started,
        level=TraceLevel.REPLAY,
        redaction=TraceRedaction(
            digest=redactor.digest, rules_version=RULES_VERSION, rules=redactor.rules
        ),
        source=TraceSource(argv=argv, version='0.3.0', replay_of=replay_of),
        ended_ns=None if status is None else started + 20 * _S,
        status=status,
    )


def _agent(
    agent_id: str,
    role: str,
    start: int,
    end: int,
    reason: str | None = 'completed',
    cycle: int = 1,
) -> Span:
    attributes: dict[str, str | int] = {'agent.role': role, 'agent.cycle': cycle}
    if reason is not None:
        attributes['stop_reason'] = reason
    return Span(
        span_id=span_id_of(SpanKind.AGENT, agent_id, cycle),
        parent_span_id=None,
        kind=SpanKind.AGENT,
        name=role,
        agent_id=agent_id,
        start_ns=_ORIGIN + start * _S,
        end_ns=_ORIGIN + end * _S,
        status=SpanStatus.OK if reason is not None else SpanStatus.CANCELLED,
        attributes=dict(attributes),
    )


def _tool(agent_id: str, index: int, name: str, start: int, end: int) -> Span:
    return Span(
        span_id=span_id_of(SpanKind.TOOL_CALL, agent_id, 1, 1, index),
        parent_span_id=None,
        kind=SpanKind.TOOL_CALL,
        name=name,
        agent_id=agent_id,
        start_ns=_ORIGIN + start * _S,
        end_ns=_ORIGIN + end * _S,
        status=SpanStatus.OK,
        attributes={'tool.name': name, 'tool.index': index, 'tool.outcome': 'ok'},
    )


def _wait(
    agent_id: str,
    number: int,
    kind: str,
    target: str,
    start: int,
    end: int,
    outcome: str = 'answered',
) -> Span:
    return Span(
        span_id=span_id_of(SpanKind.WAIT, agent_id, 'wait', number),
        parent_span_id=None,
        kind=SpanKind.WAIT,
        name=f'wait {kind}',
        agent_id=agent_id,
        start_ns=_ORIGIN + start * _S,
        end_ns=_ORIGIN + end * _S,
        status=SpanStatus.OK,
        attributes={'wait.kind': kind, 'wait.target': target, 'wait.outcome': outcome},
    )


def _spawned(parent: str, index: int, child: str) -> CausalityEdge:
    return CausalityEdge(
        kind=EdgeKind.SPAWN,
        source_span_id=span_id_of(SpanKind.TOOL_CALL, parent, 1, 1, index),
        target_span_id=span_id_of(SpanKind.AGENT, child, 1),
        attributes={'child.agent_id': child},
    )


def _run() -> tuple[list[Span], list[CausalityEdge]]:
    spans = [
        _agent(_LEAD, 'Lead', 0, 20),
        # 先派出去的 Late 后跑起来：树按跑起来的先后排，不按派活的先后或 id。
        _agent(_LATE, 'Worker', 9, 12),
        _agent(_EARLY, 'Explorer', 2, 8),
        _agent(_GRANDCHILD, 'Planner', 4, 7, reason='max_turns'),
        _tool(_LEAD, 0, 'Delegate', 1, 9),
        _tool(_LEAD, 1, 'Delegate', 1, 13),
        _tool(_EARLY, 0, 'Delegate', 3, 7),
        _tool(_LATE, 0, 'Bash', 9, 11),
        _wait(_LEAD, 0, 'agent', _EARLY, 2, 8),
        _wait(_EARLY, 0, 'agent', _GRANDCHILD, 4, 7),
        _wait(_GRANDCHILD, 0, 'human', '["tool_call", "x", 1, 1, 0, "request", 0]', 5, 6),
    ]
    edges = [
        _spawned(_LEAD, 1, _LATE),
        _spawned(_LEAD, 0, _EARLY),
        _spawned(_EARLY, 0, _GRANDCHILD),
    ]
    return spans, edges


_SLOWEST_TITLE = '最慢的工具调用（前 3 个，等别的 Agent 与人的时间不算）：'


def _section(text: str, title: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(title) + 1
    end = next((index for index in range(start, len(lines)) if not lines[index]), len(lines))
    return lines[start:end]


def test_who_created_whom_follows_the_order_the_agents_actually_ran():
    spans, edges = _run()

    tree = _section(render_show(_header(), spans, edges), '谁创建了谁：')

    assert [line.strip().removeprefix('└─ ').split('，')[0] for line in tree] == [
        f'Lead {_LEAD[:8]}',
        f'Explorer {_EARLY[:8]}',
        f'Planner {_GRANDCHILD[:8]}',
        f'Worker {_LATE[:8]}',
    ]
    assert [len(line) - len(line.lstrip()) for line in tree] == [2, 5, 8, 5]
    assert '创建于 +9.0 s' in tree[3]


def test_who_waits_on_whom_is_followed_down_the_chain():
    spans, edges = _run()

    waits = _section(render_show(_header(), spans, edges), '谁在等谁：')

    assert waits == [
        f'  Lead {_LEAD[:8]} → Explorer {_EARLY[:8]} → Planner {_GRANDCHILD[:8]} → 人'
        '（6.0 s，answered）'
    ]


def test_the_slowest_tool_calls_come_first_and_only_three_are_shown():
    spans, edges = _run()

    slowest = _section(render_show(_header(), spans, edges), _SLOWEST_TITLE)

    assert slowest == [
        f'  1. 12.0 s  Lead {_LEAD[:8]}  Delegate  ok',
        f'  2. 8.0 s  Lead {_LEAD[:8]}  Delegate  ok',
        f'  3. 4.0 s  Explorer {_EARLY[:8]}  Delegate  ok',
    ]


def test_strings_from_the_model_cannot_drive_the_terminal():
    spans = [_agent(_LEAD, 'Evil\x1b[31mRole', 0, 1), _tool(_LEAD, 0, 'Bash\x07', 0, 1)]

    text = render_show(_header(argv=('run', 'go\x1b]0;x\x07')), spans, [])

    assert '\x1b' not in text and '\x07' not in text
    assert 'Evil' in text and 'Role' in text


def test_the_list_is_newest_first_and_says_which_traces_never_finished():
    rows = [
        TraceRow(_header('aa' * 16, started=_ORIGIN), agents=1, spans=4, size=900),
        TraceRow(
            _header('bb' * 16, started=_ORIGIN + 60 * _S, status=None), agents=2, spans=9, size=None
        ),
        TraceRow(
            _header(
                'cc' * 16,
                started=_ORIGIN + 30 * _S,
                argv=('trace', 'replay', 'aaaaaaaa'),
                replay_of='aa' * 16,
            ),
            agents=1,
            spans=3,
            size=2048,
        ),
    ]

    lines = render_list(rows).splitlines()

    assert [line.split('  ')[1] for line in lines] == ['bbbbbbbb', 'cccccccc', 'aaaaaaaa']
    assert '未完成' in lines[0] and '大小不明' in lines[0]
    assert '2.0 KB' in lines[1] and '回放自 aaaaaaaa' in lines[1]
    assert lines[2].startswith('2026-') and '900 B' in lines[2] and 'run go' in lines[2]


def test_an_empty_store_says_how_traces_come_to_exist():
    assert 'run 与 chat 默认都会记录' in render_list([])


def test_an_agent_whose_last_cycle_was_cut_off_is_not_reported_as_done():
    spans = [
        _agent(_LEAD, 'Lead', 0, 5),
        _agent(_LEAD, 'Lead', 8, 9, reason=None, cycle=3),
        _agent(_LEAD, 'Lead', 6, 7, reason='max_turns', cycle=2),
    ]

    timeline = _section(render_show(_header(), spans, []), '时间线：')

    assert timeline == [f'  +0 ms → +9.0 s  Lead {_LEAD[:8]}  3 个工作周期  没有说明停下的原因']


@pytest.fixture
def far_from_utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv('TZ', 'Asia/Shanghai')
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_times_are_written_in_utc_whatever_the_local_zone(far_from_utc: None):
    [line] = render_list([TraceRow(_header(), agents=1, spans=1, size=1)]).splitlines()

    assert line.startswith('2026-09-21 14:13:20Z  abababab  ok  ')


def test_agent_ids_and_line_breaks_from_the_files_cannot_drive_the_terminal():
    evil = '\x1b[8m\x1b[1A\x1b[2K' + 'x' * 22
    child = '\x1b]0;X\x07' + 'y' * 26
    spans = [_agent(evil, 'Role', 0, 2), _tool(evil, 0, 'Bash', 0, 1)]
    edges = [_spawned(evil, 0, child)]

    text = render_show(_header(argv=('run', 'line one\nline two')), spans, edges)

    assert '\x1b' not in text and '\x07' not in text
    assert 'line one\\nline two' in text


def test_an_argument_with_a_line_break_stays_on_its_own_row():
    forged = 'first\n2026-01-01 00:00:00Z  deadbeef  ok  1 个 Agent'

    rows = render_list([TraceRow(_header(argv=('run', forged)), agents=1, spans=1, size=1)])

    assert len(rows.splitlines()) == 1


def test_two_agents_waiting_on_each_other_are_shown_once_as_a_cycle():
    spans = [
        _agent(_LEAD, 'Lead', 0, 20),
        _agent(_EARLY, 'Explorer', 1, 20),
        _wait(_LEAD, 0, 'agent', _EARLY, 2, 10),
        _wait(_EARLY, 0, 'agent', _LEAD, 3, 9),
    ]

    waits = _section(render_show(_header(), spans, []), '谁在等谁：')

    assert waits == [
        f'  Lead {_LEAD[:8]} → Explorer {_EARLY[:8]} → Lead {_LEAD[:8]}'
        '（8.0 s，answered，循环等待）'
    ]


def test_three_agents_waiting_in_a_ring_are_shown_once_from_the_first_to_wait():
    spans = [
        _agent(_LEAD, 'Lead', 0, 20),
        _agent(_EARLY, 'Explorer', 1, 20),
        _agent(_LATE, 'Worker', 1, 20),
        _wait(_EARLY, 0, 'agent', _LATE, 3, 9),
        _wait(_LATE, 0, 'agent', _LEAD, 4, 8),
        _wait(_LEAD, 0, 'agent', _EARLY, 2, 10),
    ]

    waits = _section(render_show(_header(), spans, []), '谁在等谁：')

    assert waits == [
        f'  Lead {_LEAD[:8]} → Explorer {_EARLY[:8]} → Worker {_LATE[:8]} → Lead {_LEAD[:8]}'
        '（8.0 s，answered，循环等待）'
    ]


def test_a_chain_reports_how_its_innermost_wait_ended():
    spans = [
        _agent(_LEAD, 'Lead', 0, 20),
        _agent(_EARLY, 'Explorer', 1, 20),
        _wait(_LEAD, 0, 'agent', _EARLY, 2, 10),
        _wait(_EARLY, 0, 'human', '["k"]', 3, 9, outcome='timeout'),
    ]

    waits = _section(render_show(_header(), spans, []), '谁在等谁：')

    assert waits == [f'  Lead {_LEAD[:8]} → Explorer {_EARLY[:8]} → 人（8.0 s，timeout）']


def test_the_chain_follows_what_the_awaited_agent_was_waiting_on_first():
    spans = [
        _agent(_LEAD, 'Lead', 0, 20),
        _agent(_EARLY, 'Explorer', 1, 20),
        _agent(_LATE, 'Worker', 1, 20),
        _agent(_GRANDCHILD, 'Planner', 1, 20),
        _wait(_LEAD, 0, 'agent', _EARLY, 1, 10),
        _wait(_EARLY, 0, 'agent', _LATE, 2, 5),
        _wait(_EARLY, 1, 'agent', _GRANDCHILD, 6, 9),
    ]

    waits = _section(render_show(_header(), spans, []), '谁在等谁：')

    assert waits == [
        f'  Lead {_LEAD[:8]} → Explorer {_EARLY[:8]} → Worker {_LATE[:8]}（9.0 s，answered）',
        f'  Explorer {_EARLY[:8]} → Planner {_GRANDCHILD[:8]}（3.0 s，answered）',
    ]


def test_waiting_on_an_agent_that_never_ran_says_so():
    spans = [
        _agent(_LEAD, 'Lead', 0, 20),
        _tool(_LEAD, 0, 'Delegate', 1, 9),
        _wait(_LEAD, 0, 'agent', _LATE, 1, 9),
    ]

    waits = _section(render_show(_header(), spans, [_spawned(_LEAD, 0, _LATE)]), '谁在等谁：')

    assert waits == [
        f'  Lead {_LEAD[:8]} → Agent {_LATE[:8]}（派了出去，没有跑起来）（8.0 s，answered）'
    ]


def test_a_spawn_chain_deeper_than_the_interpreter_stack_is_drawn():
    ids = [f'{number:032x}' for number in range(1, 1201)]
    spans = [_agent(agent_id, 'Worker', number, 3000) for number, agent_id in enumerate(ids)]
    spans += [
        _wait(agent_id, 0, 'agent', inner, number, 3000)
        for number, (agent_id, inner) in enumerate(zip(ids, ids[1:], strict=False))
    ]
    edges = [
        CausalityEdge(
            kind=EdgeKind.SPAWN,
            source_span_id=span_id_of(SpanKind.AGENT, parent, 1),
            target_span_id=span_id_of(SpanKind.AGENT, child, 1),
            attributes={'child.agent_id': child},
        )
        for parent, child in zip(ids, ids[1:], strict=False)
    ]

    text = render_show(_header(), spans, edges)

    assert len(_section(text, '谁创建了谁：')) == 1200
    [chain] = _section(text, '谁在等谁：')
    assert chain.count(' → ') == 1199


def test_slowest_calls_that_took_as_long_are_listed_in_the_order_they_started():
    spans = [
        _agent(_LEAD, 'Lead', 0, 20),
        _tool(_LEAD, 0, 'Read', 5, 7),
        _tool(_LEAD, 1, 'Grep', 2, 4),
    ]

    slowest = _section(render_show(_header(), spans, []), _SLOWEST_TITLE)

    assert slowest == [
        f'  1. 2.0 s  Lead {_LEAD[:8]}  Grep  ok',
        f'  2. 2.0 s  Lead {_LEAD[:8]}  Read  ok',
    ]


def test_a_replayed_trace_names_its_original_by_the_first_eight_characters():
    header = _header('cc' * 16, argv=('trace', 'replay', 'x'), replay_of='aaaaaaaa' + 'b' * 24)

    [line] = render_list([TraceRow(header, agents=1, spans=1, size=1)]).splitlines()

    assert line.endswith('  trace replay x  回放自 aaaaaaaa')


def test_a_child_that_never_ran_is_drawn_where_its_parent_stopped():
    spans = [
        _agent(_LEAD, 'Lead', 0, 20),
        _agent(_EARLY, 'Explorer', 5, 8),
        _tool(_LEAD, 0, 'Delegate', 1, 2),
        _tool(_LEAD, 1, 'Delegate', 3, 4),
    ]
    edges = [_spawned(_LEAD, 0, _LATE), _spawned(_LEAD, 1, _EARLY)]

    tree = _section(render_show(_header(), spans, edges), '谁创建了谁：')

    assert [line.strip() for line in tree] == [
        f'Lead {_LEAD[:8]}',
        f'└─ Explorer {_EARLY[:8]}，创建于 +5.0 s',
        f'└─ Agent {_LATE[:8]}（派了出去，没有跑起来）',
    ]


def test_agents_nobody_spawned_are_roots_in_the_order_they_started():
    spans = [_agent(_LEAD, 'Lead', 5, 9), _agent(_EARLY, 'Explorer', 1, 3)]

    tree = _section(render_show(_header(), spans, []), '谁创建了谁：')

    assert tree == [f'  Explorer {_EARLY[:8]}', f'  Lead {_LEAD[:8]}']


def test_sizes_of_a_gibibyte_or_more_are_shown_in_gb():
    row = TraceRow(_header(), agents=1, spans=1, size=int(1.5 * 1024**3))
    text = render_list([row])
    assert '1.5 GB' in text


@pytest.mark.parametrize(
    ('size', 'shown'),
    [(1023, '1023 B'), (1024, '1.0 KB'), (1024**2, '1.0 MB'), (1024**3, '1.0 GB')],
)
def test_each_size_unit_starts_at_its_power_of_1024(size: int, shown: str):
    [line] = render_list([TraceRow(_header(), agents=1, spans=1, size=size)]).splitlines()

    assert f'  {shown}  ' in line


def _llm(
    agent_id: str,
    turn: int,
    start: int,
    end: int,
    *,
    tokens: tuple[int, int] | None = (10, 2),
    status: SpanStatus = SpanStatus.OK,
    failovers: int = 0,
) -> Span:
    given, taken = (None, None) if tokens is None else tokens
    return Span(
        span_id=span_id_of(SpanKind.LLM_CALL, agent_id, turn, 1),
        parent_span_id=None,
        kind=SpanKind.LLM_CALL,
        name='llm',
        agent_id=agent_id,
        start_ns=_ORIGIN + start * _S,
        end_ns=_ORIGIN + end * _S,
        status=status,
        attributes={
            'llm.platform': 'main',
            'llm.model': 'm',
            'llm.attempt': 1,
            'llm.input_tokens': given,
            'llm.output_tokens': taken,
        },
        events=tuple(
            SpanEvent(
                'llm.platform_failed',
                _ORIGIN + start * _S,
                {'llm.platform': 'gemini', 'llm.error': 'HTTP 429', 'llm.status': 429},
            )
            for _ in range(failovers)
        ),
    )


def test_calls_are_ranked_by_their_own_time_without_their_waits_on_agents_or_people():
    delegate = _tool(_LEAD, 0, 'Delegate', 0, 15)
    explorer = dataclasses.replace(
        _agent(_EARLY, 'Explorer', 1, 17), parent_span_id=delegate.span_id
    )
    asked = dataclasses.replace(_tool(_EARLY, 1, 'AskUser', 6, 16), parent_span_id=explorer.span_id)
    confirmed = _tool(_LATE, 0, 'Bash', 1, 12)
    confirmation = Span(
        span_id=span_id_of(SpanKind.CONFIRMATION, _LATE, 1, 1, 0),
        parent_span_id=confirmed.span_id,
        kind=SpanKind.CONFIRMATION,
        name='confirmation',
        agent_id=_LATE,
        start_ns=_ORIGIN + 2 * _S,
        end_ns=_ORIGIN + 9 * _S,
        status=SpanStatus.OK,
        attributes={},
    )
    spans = [
        _agent(_LEAD, 'Lead', 0, 20),
        explorer,
        _agent(_LATE, 'Worker', 1, 13),
        delegate,
        dataclasses.replace(
            _wait(_LEAD, 0, 'agent', _EARLY, 1, 14), parent_span_id=delegate.span_id
        ),
        _tool(_EARLY, 0, 'Bash', 2, 5),
        asked,
        dataclasses.replace(
            _wait(_EARLY, 0, 'human', '["k"]', 7, 16), parent_span_id=asked.span_id
        ),
        confirmed,
        confirmation,
        dataclasses.replace(
            _wait(_LATE, 0, 'human', '["c"]', 2, 9), parent_span_id=confirmation.span_id
        ),
    ]

    shown = render_show(_header(), spans, [_spawned(_LEAD, 0, _EARLY)])
    slowest = _section(shown, _SLOWEST_TITLE)

    assert slowest == [
        f'  1. 4.0 s  Worker {_LATE[:8]}  Bash  ok',
        f'  2. 3.0 s  Explorer {_EARLY[:8]}  Bash  ok',
        f'  3. 2.0 s  Lead {_LEAD[:8]}  Delegate  ok',
    ]
    assert f'Lead {_LEAD[:8]} → Explorer {_EARLY[:8]} → 人（13.0 s' in shown


def test_model_calls_are_summed_per_agent_with_tokens_failures_and_failovers():
    spans = [
        _agent(_LEAD, 'Lead', 0, 20),
        _agent(_EARLY, 'Explorer', 2, 10),
        _llm(_LEAD, 1, 0, 2, tokens=(100, 10)),
        _llm(_LEAD, 2, 12, 13, tokens=(150, 5), failovers=2),
        _llm(_EARLY, 1, 3, 4, tokens=None),
        _llm(_EARLY, 2, 5, 6, tokens=(30, 3), status=SpanStatus.ERROR),
    ]

    models = _section(render_show(_header(), spans, []), '模型调用：')

    assert models == [
        f'  Lead {_LEAD[:8]}  2 次  3.0 s  输入 250 / 输出 15 token'
        '  平台故障 2 次（换了平台接着答）',
        f'  Explorer {_EARLY[:8]}  2 次  2.0 s  输入 30 / 输出 3 token（1 次没报）  失败 1 次',
    ]


def test_agents_whose_cycles_never_closed_still_show_up_as_unfinished():
    spans = [_llm(_LEAD, 1, 0, 1), _llm(_EARLY, 1, 2, 3)]

    shown = render_show(_header(status=None), spans, [_spawned(_LEAD, 0, _EARLY)])

    assert _section(shown, '谁创建了谁：') == [f'  ? {_LEAD[:8]}', f'  ? {_EARLY[:8]}']
    assert _section(shown, '时间线：') == [
        f'  +0 ms → +1.0 s  ? {_LEAD[:8]}  没有收尾的工作周期（还在跑，或者 run 被强行打断）',
        f'  +2.0 s → +3.0 s  ? {_EARLY[:8]}  没有收尾的工作周期（还在跑，或者 run 被强行打断）',
    ]


def test_a_list_says_how_many_directories_it_could_not_read_instead_of_calling_itself_empty():
    row = TraceRow(_header(), agents=1, spans=1, size=1)

    assert render_list([], unreadable=2) == (
        '另有 2 个 trace 目录读不进来，没有列出（原因见上面的 WARNING）。'
    )
    assert render_list([row], unreadable=1).splitlines()[1] == (
        '另有 1 个 trace 目录读不进来，没有列出（原因见上面的 WARNING）。'
    )
    assert render_list([]).startswith('还没有 trace')
    assert len(render_list([row]).splitlines()) == 1


def _module(agent_id: str, name: str, *, tools: list[str], plugins: list[str]) -> Span:
    return Span(
        span_id=span_id_of(SpanKind.MODULE, agent_id, name),
        parent_span_id=None,
        kind=SpanKind.MODULE,
        name=name,
        agent_id=agent_id,
        start_ns=_ORIGIN,
        end_ns=_ORIGIN,
        status=SpanStatus.OK,
        attributes={
            'module.name': name,
            'module.version': '1.2.0',
            'module.tools': tools,
            'module.plugins': plugins,
            'module.flags': [],
        },
    )


def test_the_module_section_says_what_each_module_gave_each_agent():
    tool = dataclasses.replace(
        _tool(_LEAD, 0, 'Record', 1, 2),
        events=(
            SpanEvent(
                name='junction.triggered',
                time_ns=_ORIGIN + 1 * _S,
                attributes={'junction.name': 'audit:recorded'},
            ),
        ),
    )
    spans = [
        _agent(_LEAD, 'Orchestrator', 0, 5),
        _module(_LEAD, 'audit', tools=['Zeta', 'Alpha'], plugins=['audit-hooks']),
        _module(_LEAD, 'quiet', tools=[], plugins=[]),
        tool,
    ]

    section = _section(render_show(_header(), spans, []), 'Module：')

    assert section[0].strip().startswith('Orchestrator')
    assert section[1].strip() == 'audit 1.2.0：工具 Zeta、Alpha；插件 audit-hooks'
    assert section[2].strip() == 'quiet 1.2.0：工具 无；插件 无'
    assert '扩展切点：audit:recorded ×1' in section[3]


def test_an_agent_that_only_has_module_spans_is_not_drawn_as_an_unfinished_agent():
    spans = [
        _agent(_LEAD, 'Orchestrator', 0, 5),
        _module(_EARLY, 'audit', tools=['Zeta'], plugins=[]),
    ]

    tree = _section(render_show(_header(), spans, []), '谁创建了谁：')

    assert len(tree) == 1
    assert 'Orchestrator' in tree[0]


def test_a_module_name_that_could_forge_a_line_is_escaped():
    spans = [
        _agent(_LEAD, 'Orchestrator', 0, 5),
        _module(_LEAD, 'audit', tools=['Zeta\nforged line'], plugins=[]),
    ]

    text = render_show(_header(), spans, [])

    assert '\nforged line' not in text
